"""Code-level check that every price in a reply came from a tool result.

The system prompt already tells the model never to invent a price. A prompt
is a request, so this module enforces it: before a reply goes out, every
KES amount in it must match a number that a tool returned during this turn
(or a whole multiple of one, for "2 of these" quotes). Anything else is
blocked and the conversation goes to a human.
"""
from __future__ import annotations

import json
import re

# "KES 29,000", "Ksh 29000", "KSh. 1,500", "29,000/-", "29k" are all common
# on Kenyan WhatsApp. Amounts are matched only when a currency marker sits
# next to them, so plain numbers (specs like "350kg") are left alone.
_AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(k|K)?"
_PATTERNS = [
    re.compile(r"(?:KES|KSH|Kshs?|KSh|Ksh)\.?\s*" + _AMOUNT),
    re.compile(_AMOUNT + r"\s*(?:/-|KES|bob\b)"),
]
_MAX_MULTIPLE = 50


def amounts_in_text(text: str) -> set[int]:
    found: set[int] = set()
    for pattern in _PATTERNS:
        for match in pattern.finditer(text or ""):
            value = int(match.group(1).replace(",", ""))
            if match.group(2):
                value *= 1000
            found.add(value)
    return found


def numbers_in_tool_result(result: object) -> set[int]:
    """Every whole number of 100 or more anywhere in a tool result."""
    blob = json.dumps(result, default=str)
    return {int(n) for n in re.findall(r"(?<![\d.])(\d{3,9})(?![\d.])", blob.replace(",", ""))}


def numbers_in_customer_text(text: str) -> set[int]:
    """Figures the customer typed ("budget 50k", "I saw it at 27,500"), so a
    reply that repeats their own number back is not treated as invented."""
    out = set()
    for digits, k in re.findall(r"(\d{1,3}(?:,\d{3})+|\d+)\s*([kK])?\b", text or ""):
        value = int(digits.replace(",", ""))
        out.add(value * 1000 if k else value)
    return out


def unverified_amounts(reply: str, tool_amounts: set[int], customer_amounts: set[int] = frozenset()) -> set[int]:
    """Amounts in the reply that no tool returned this turn. A whole multiple
    of a tool amount passes (quantity quotes); the customer's own figures pass
    only as typed."""
    bad = set()
    for amount in amounts_in_text(reply):
        if amount in tool_amounts or amount in customer_amounts:
            continue
        if any(a and amount % a == 0 and amount // a <= _MAX_MULTIPLE for a in tool_amounts):
            continue
        bad.add(amount)
    return bad
