"""The agent's core loop: load conversation history, call Claude with tools,
execute any tool calls, feed results back, repeat until a final text reply
comes back (or we hit a safety cap and hand off to a human)."""
import json
import logging
from pathlib import Path
from typing import Optional

from app.agent.llm_client import build_llm_client
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
            final_text = self._run_loop(messages, ctx)
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

    def _run_loop(self, messages: list[dict], ctx: ToolContext) -> str:
        for _ in range(self.settings.max_tool_iterations):
            response = self.client.create(
                messages=messages,
                system=self.system_prompt,
                tools=TOOLS,
                max_tokens=self.settings.max_reply_tokens,
            )

            if response.stop_reason != "tool_use":
                return _extract_text(response)

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


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip() or "Sorry, could you rephrase that?"
