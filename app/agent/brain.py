"""The agent's core loop: load conversation history, call Claude with tools,
execute any tool calls, feed results back, repeat until a final text reply
comes back (or we hit a safety cap and hand off to a human)."""
import json
import logging
from pathlib import Path
from typing import Optional

from app.agent.llm_client import build_llm_client
from app.agent.price_guard import numbers_in_customer_text, prices_in_tool_result, unverified_amounts
from app.agent.reply_review import BLOCK, TurnFacts, collect_facts, review_reply, rewrite_instruction
from app.agent.tools import TOOL_IMPLS, TOOLS, ToolContext
from app.storage import db

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
logger = logging.getLogger("sales_agent")

# A customer must never see a raw stack trace or "Internal Server Error".
# Whatever breaks, they see this instead while it's logged for a human to
# check — this is the single most load-bearing line in the file.
FALLBACK_REPLY = (
    "Sorry, I'm having a technical hiccup on my end right now. "
    "The team's been notified and will follow up on this chat shortly — "
    "or call/WhatsApp us directly if it's urgent."
)


class SalesAgent:
    def __init__(self, settings, catalogue, site_index, twilio_client=None):
        self.settings = settings
        self.catalogue = catalogue
        self.site_index = site_index
        self.twilio_client = twilio_client
        self.client = build_llm_client(settings)
        self.system_prompt = _PROMPT_PATH.read_text()

    def handle_message(self, phone_number: str, incoming_text: str, profile_name: Optional[str] = None) -> str:
        db.get_or_create_conversation(self.settings.database_path, phone_number, profile_name)
        db.append_message(self.settings.database_path, phone_number, "user", incoming_text)

        history = db.get_recent_messages(self.settings.database_path, phone_number, limit=20)
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        last_reply = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")

        ctx = ToolContext(
            phone_number=phone_number,
            db_path=self.settings.database_path,
            catalogue=self.catalogue,
            site_index=self.site_index,
            allowed_fetch_domain=self.settings.allowed_fetch_domain,
            twilio_client=self.twilio_client,
            human_escalation_whatsapp=self.settings.human_escalation_whatsapp,
            business_whatsapp_from=self.settings.twilio_whatsapp_number,
        )

        try:
            final_text = self._run_loop(messages, ctx, numbers_in_customer_text(incoming_text), last_reply)
        except Exception:
            # Anything unexpected (auth failure, network error, rate limit,
            # a malformed response) must degrade to a safe reply, never a
            # crash the customer sees as "Internal Server Error". Log full
            # detail server-side; tell the customer the honest, calm
            # version; make sure a human actually gets looped in.
            logger.exception("Agent failed for %s", phone_number)
            final_text = FALLBACK_REPLY
            try:
                TOOL_IMPLS["escalate_to_human"](
                    ctx=ctx, reason="Agent raised an unhandled exception — see server logs."
                )
            except Exception:
                pass

        db.append_message(self.settings.database_path, phone_number, "assistant", final_text)
        return final_text

    def _run_loop(
        self,
        messages: list[dict],
        ctx: ToolContext,
        customer_amounts: Optional[set[int]] = None,
        last_reply: str = "",
    ) -> str:
        # Prices the tools actually returned during this turn (price fields
        # and currency-marked amounts only), each with a note of where it
        # came from, plus any figure the customer typed. A price in the final
        # reply has to come from here, whatever the model decided.
        allowed_amounts: dict[int, str] = {}
        customer_amounts = set(customer_amounts or ())
        facts = TurnFacts(customer_amounts=customer_amounts, last_reply=last_reply)
        tool_texts: list[str] = []
        for _ in range(self.settings.max_tool_iterations):
            response = self.client.create(
                messages=messages,
                system=self.system_prompt,
                tools=TOOLS,
                max_tokens=self.settings.max_reply_tokens,
            )

            if response.stop_reason != "tool_use":
                facts.prices = allowed_amounts
                facts.tool_text = "\n".join(tool_texts).lower()
                return self._review_and_send(_extract_text(response), facts, messages, ctx)

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                impl = TOOL_IMPLS.get(block.name)
                if impl is None:
                    result = {"error": f"Unknown tool {block.name}"}
                else:
                    try:
                        result = impl(ctx=ctx, **block.input)
                    except Exception as exc:  # a broken tool call must never crash the conversation
                        result = {"error": f"Tool '{block.name}' failed: {exc}"}
                if "error" not in result:
                    for amount, source in prices_in_tool_result(result, block.name).items():
                        allowed_amounts.setdefault(amount, source)
                    collect_facts(result, facts)
                    tool_texts.append(json.dumps(result, default=str))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
            messages.append({"role": "user", "content": tool_results})

        # Safety cap hit — fail into a human handoff rather than loop forever
        # or return a broken reply.
        try:
            TOOL_IMPLS["escalate_to_human"](
                ctx=ctx, reason="Agent hit max tool iterations without producing a final reply."
            )
        except Exception:
            pass
        return (
            "Let me get the team to confirm that for you exactly — "
            "someone will follow up on this chat shortly."
        )

    def _review_and_send(self, draft: str, facts: TurnFacts, messages: list[dict], ctx: ToolContext) -> str:
        """Every reply passes the code checks in reply_review before it goes out.

        pass   -> sent as written.
        revise -> the model gets one rewrite; the rewrite is checked again.
                  If only style problems remain, it is sent (style never
                  holds a customer hostage). A blocking problem is not.
        block  -> one rewrite too, since most blocks are a fact the model
                  can simply drop. If the rewrite still blocks, the customer
                  gets a holding reply and a human is called in.
        """
        review = review_reply(draft, facts)
        self._log_review(ctx, "draft", review)
        if review.verdict == "pass":
            return draft

        rewrite = self._rewrite(draft, review, messages)
        if rewrite:
            second = review_reply(rewrite, facts)
            self._log_review(ctx, "rewrite", second)
            if second.verdict != BLOCK:
                return rewrite
            review = second

        blocked = [i for i in review.issues if i.severity == BLOCK]
        if not blocked:
            return draft  # rewrite failed or came back empty; the draft was only a style problem
        reason = "reply_review: " + "; ".join(f"{i.code} ({i.detail})" for i in blocked)
        logger.warning("reply_review blocked a reply for %s: %s", ctx.phone_number, reason)
        try:
            TOOL_IMPLS["escalate_to_human"](ctx=ctx, reason=reason)
        except Exception:
            pass
        if all(i.code.startswith(("price", "customer_figure")) for i in blocked):
            return PRICE_CHECK_REPLY
        return HOLDING_REPLY

    def _rewrite(self, draft: str, review, messages: list[dict]) -> str:
        """One extra model call, no tools needed. Anything odd returns ""."""
        try:
            response = self.client.create(
                messages=messages + [
                    {"role": "assistant", "content": draft},
                    {"role": "user", "content": rewrite_instruction(review)},
                ],
                system=self.system_prompt,
                tools=TOOLS,
                max_tokens=self.settings.max_reply_tokens,
            )
        except Exception:
            logger.exception("reply_review rewrite call failed")
            return ""
        if response.stop_reason == "tool_use":
            return ""
        text = _extract_text(response)
        return "" if text == _EMPTY_REPLY else text

    def _log_review(self, ctx: ToolContext, stage: str, review) -> None:
        try:
            db.log_reply_review(ctx.db_path, ctx.phone_number, stage, review.verdict, review.codes)
        except Exception:
            logger.exception("could not store reply review")

    def _guard_prices(self, reply: str, tool_amounts, customer_amounts: set[int], ctx: ToolContext) -> str:
        bad = unverified_amounts(reply, set(tool_amounts), customer_amounts)
        if not bad:
            return reply
        sources = dict(tool_amounts) if isinstance(tool_amounts, dict) else {}
        logger.warning(
            "price_guard blocked a reply for %s: unverified amounts %s; prices available this turn: %s",
            ctx.phone_number, sorted(bad), sources or sorted(tool_amounts),
        )
        try:
            TOOL_IMPLS["escalate_to_human"](
                ctx=ctx,
                reason=f"price_guard: reply quoted KES {sorted(bad)} with no matching tool result this turn.",
            )
        except Exception:
            pass
        return PRICE_CHECK_REPLY


HOLDING_REPLY = (
    "Let me double-check that with the team so I give you the right answer. "
    "Someone will follow up on this chat shortly."
)

PRICE_CHECK_REPLY = (
    "Let me get the team to confirm the exact price for you so you get the right figure. "
    "Someone will follow up on this chat shortly."
)


_EMPTY_REPLY = "Sorry, could you rephrase that?"


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip() or _EMPTY_REPLY
