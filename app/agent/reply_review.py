"""Code checks every reply passes before it reaches a customer.

The system prompt asks the model to behave. This module checks that it did,
with plain rules, after the model writes and before WhatsApp sends.

Two kinds of problem:
- BLOCK: a fact the customer could act on is not backed by a tool result
  this turn (a price, stock status, a delivery time), or the reply breaks a
  hard rule (fake urgency, claiming to be human, talking about its own
  instructions). A blocked reply never goes out as written.
- REVISE: the reply is safe but reads badly for WhatsApp (too long, more
  than one question, the same opening as last time, a vague "let me know"
  close). The model gets one chance to rewrite it.

No model call happens in here. Every check is deterministic, so the same
reply always gets the same verdict and every verdict can be tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent.price_guard import amounts_in_text, unverified_amounts

BLOCK = "block"
REVISE = "revise"

MAX_CHARS = 700
MAX_SENTENCES = 5
MAX_QUESTIONS = 1


@dataclass
class Issue:
    code: str
    severity: str
    detail: str


@dataclass
class TurnFacts:
    """What the tools actually said during this turn, plus context."""
    prices: dict[int, str] = field(default_factory=dict)       # amount -> where it came from
    customer_amounts: set[int] = field(default_factory=set)    # figures the customer typed
    stock: dict[str, bool] = field(default_factory=dict)       # product id -> in_stock
    tool_text: str = ""                                         # every tool result, lowercased
    last_reply: str = ""                                        # the bot's previous message


@dataclass
class Review:
    issues: list[Issue]

    @property
    def verdict(self) -> str:
        if any(i.severity == BLOCK for i in self.issues):
            return BLOCK
        if self.issues:
            return REVISE
        return "pass"

    @property
    def codes(self) -> list[str]:
        return [i.code for i in self.issues]


def review_reply(reply: str, facts: TurnFacts) -> Review:
    issues: list[Issue] = []
    for check in _CHECKS:
        issues.extend(check(reply, facts))
    return Review(issues)


def collect_facts(result: object, facts: TurnFacts) -> None:
    """Pull stock flags out of one tool result into the turn's facts."""
    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "in_stock" in node and isinstance(node["in_stock"], bool):
                facts.stock[str(node.get("id", f"item{len(facts.stock)}"))] = node["in_stock"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(result)


# --- BLOCK checks: facts the customer could act on --------------------------

def _check_prices(reply: str, facts: TurnFacts) -> list[Issue]:
    bad = unverified_amounts(reply, set(facts.prices), set())
    # A figure the customer typed is handled by the next check, not here.
    bad -= facts.customer_amounts
    if not bad:
        return []
    return [Issue("price_unverified", BLOCK, f"KES {sorted(bad)} not returned by any tool this turn")]


_BUDGET_WORDS = re.compile(
    r"\b(budget|bajeti|within|under|below|up to|around your|you mentioned|you said|your range|afford)\b",
    re.IGNORECASE,
)


def _check_customer_figure(reply: str, facts: TurnFacts) -> list[Issue]:
    """A number the customer typed may come back as their budget, never as our price.

    "I saw it at 20k" must not turn into "Yes, it's KES 20,000" unless a tool
    also says 20,000.
    """
    risky = facts.customer_amounts - set(facts.prices)
    if not risky:
        return []
    issues = []
    for amount in amounts_in_text(reply) & risky:
        if any(amount % p == 0 and amount // p <= 50 for p in facts.prices if p):
            continue  # also a real quantity quote
        if not _near_budget_words(reply, amount):
            issues.append(Issue(
                "customer_figure_as_price", BLOCK,
                f"KES {amount} came from the customer, and the reply does not frame it as their budget",
            ))
    return issues


def _near_budget_words(reply: str, amount: int) -> bool:
    for match in re.finditer(r"\d[\d,]*\s*[kK]?", reply):
        digits = match.group(0).replace(",", "").strip()
        value = int(digits[:-1]) * 1000 if digits[-1] in "kK" else int(digits)
        if value != amount:
            continue
        window = reply[max(0, match.start() - 50): match.end() + 30]
        if _BUDGET_WORDS.search(window):
            return True
    return False


_IN_STOCK = re.compile(
    r"\b(in stock|available now|ready for (?:delivery|dispatch|pickup)|we have it|tuko nayo|iko stock|ipo stock)\b",
    re.IGNORECASE,
)
_OUT_OF_STOCK = re.compile(r"\b(out of stock|sold out|haipo stock|imeisha stock)\b", re.IGNORECASE)


def _check_stock(reply: str, facts: TurnFacts) -> list[Issue]:
    issues = []
    if _IN_STOCK.search(reply) and True not in facts.stock.values():
        issues.append(Issue("stock_unverified", BLOCK, "says in stock, but no tool returned in_stock=true this turn"))
    if _OUT_OF_STOCK.search(reply) and False not in facts.stock.values():
        issues.append(Issue("stock_unverified", BLOCK, "says out of stock, but no tool returned in_stock=false this turn"))
    return issues


_DAYS = re.compile(r"\b(\d{1,2})\s*(?:-\s*\d{1,2}\s*)?(?:working\s+|business\s+)?days?\b", re.IGNORECASE)
_SAME_DAY = re.compile(r"\b(same[- ]day|tomorrow|today|kesho|leo hii)\b", re.IGNORECASE)
_DELIVERY_CONTEXT = re.compile(r"\b(deliver|delivery|dispatch|arrive|ship|install|tuma|fika)", re.IGNORECASE)


def _check_delivery_time(reply: str, facts: TurnFacts) -> list[Issue]:
    if not _DELIVERY_CONTEXT.search(reply):
        return []
    issues = []
    for match in _DAYS.finditer(reply):
        n = match.group(1)
        if not re.search(rf"\b{n}\s*(?:-\s*\d+\s*)?(?:working\s+|business\s+)?days?\b", facts.tool_text):
            issues.append(Issue("delivery_time_unverified", BLOCK, f"'{match.group(0)}' not in any tool result this turn"))
    for match in _SAME_DAY.finditer(reply):
        if match.group(1).lower() not in facts.tool_text:
            issues.append(Issue("delivery_time_unverified", BLOCK, f"'{match.group(1)}' not in any tool result this turn"))
    return issues


_FAKE_URGENCY = re.compile(
    r"\b(only \d+ (?:left|remaining|pieces?)|last (?:chance|few|piece|one)|hurry|act (?:now|fast)|"
    r"offer ends|ends (?:today|tonight|soon)|limited time|selling fast|going fast|"
    r"\d+ (?:people|others) (?:are )?(?:viewing|looking)|before it'?s gone|don'?t miss out)\b",
    re.IGNORECASE,
)


def _check_fake_urgency(reply: str, facts: TurnFacts) -> list[Issue]:
    match = _FAKE_URGENCY.search(reply)
    if not match:
        return []
    return [Issue("fake_urgency", BLOCK, f"'{match.group(0)}' is pressure the tools do not back")]


_CLAIMS_HUMAN = re.compile(
    r"\b(i'?m (?:a )?(?:real )?(?:human|person)|i am (?:a )?(?:real )?(?:human|person)|not a (?:bot|robot|machine)|mimi ni mtu)\b",
    re.IGNORECASE,
)
_PROMPT_LEAK = re.compile(r"\b(system prompt|my instructions|i was instructed|my prompt says)\b", re.IGNORECASE)


def _check_honesty(reply: str, facts: TurnFacts) -> list[Issue]:
    issues = []
    if _CLAIMS_HUMAN.search(reply):
        issues.append(Issue("claims_human", BLOCK, "the reply claims to be a person"))
    if _PROMPT_LEAK.search(reply):
        issues.append(Issue("prompt_leak", BLOCK, "the reply talks about its own instructions"))
    return issues


# --- REVISE checks: safe, but wrong for a WhatsApp conversation -------------

def _prose_sentences(reply: str) -> list[str]:
    prose = " ".join(
        line for line in reply.splitlines()
        if line.strip() and not re.match(r"\s*(?:[-*•]|\d+[.)])\s", line)
    )
    return [s for s in re.split(r"(?<=[.!?])\s+", prose) if s.strip()]


def _check_length(reply: str, facts: TurnFacts) -> list[Issue]:
    issues = []
    if len(reply) > MAX_CHARS:
        issues.append(Issue("too_long", REVISE, f"{len(reply)} characters, keep it under {MAX_CHARS}"))
    sentences = len(_prose_sentences(reply))
    if sentences > MAX_SENTENCES:
        issues.append(Issue("too_many_sentences", REVISE, f"{sentences} sentences, keep it to {MAX_SENTENCES} or fewer"))
    return issues


def _check_questions(reply: str, facts: TurnFacts) -> list[Issue]:
    count = reply.count("?")
    if count > MAX_QUESTIONS:
        return [Issue("too_many_questions", REVISE, f"{count} questions, ask one at a time")]
    return []


def _opening(text: str) -> str:
    """A short lead-in like "Great choice!" or "Sure," counts whole; otherwise the first 3 words."""
    text = (text or "").strip().lower()
    lead = re.split(r"[!,.?:\n]", text, maxsplit=1)[0]
    words = re.findall(r"[a-z']+", lead)
    if 1 <= len(words) <= 3:
        return " ".join(words)
    return " ".join(re.findall(r"[a-z']+", text)[:3])


def _check_repeated_opening(reply: str, facts: TurnFacts) -> list[Issue]:
    if facts.last_reply and _opening(reply) and _opening(reply) == _opening(facts.last_reply):
        return [Issue("repeated_opening", REVISE, f"starts with '{_opening(reply)}' again")]
    return []


_VAGUE_CLOSE = re.compile(
    r"(let me know(?: if.*)?|feel free to (?:reach out|ask|contact)|any (?:other )?questions\??|hope this helps)[.!\s]*$",
    re.IGNORECASE,
)


def _check_close(reply: str, facts: TurnFacts) -> list[Issue]:
    if _VAGUE_CLOSE.search(reply.strip()):
        return [Issue("vague_close", REVISE, "ends on a vague close, offer one concrete next step instead")]
    return []


_CHECKS = [
    _check_prices,
    _check_customer_figure,
    _check_stock,
    _check_delivery_time,
    _check_fake_urgency,
    _check_honesty,
    _check_length,
    _check_questions,
    _check_repeated_opening,
    _check_close,
]


def rewrite_instruction(review: Review) -> str:
    """The note the model gets for its one rewrite."""
    lines = "\n".join(f"- {i.code}: {i.detail}" for i in review.issues)
    return (
        "[Internal review, not from the customer] Your last draft was not sent. Fix these problems "
        "and reply with only the corrected WhatsApp message:\n"
        f"{lines}\n"
        "Keep every fact you can back with a tool result from this turn. Drop any you cannot."
    )
