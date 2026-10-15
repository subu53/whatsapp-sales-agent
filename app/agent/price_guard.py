"""Code-level check that every price in a reply came from a tool result.

The system prompt already tells the model never to invent a price. A prompt
is a request, so this module enforces it: before a reply goes out, every
KES amount in it must match a price that a tool returned during this turn
(or a whole multiple of one, for "2 of these" quotes). Anything else is
blocked and the conversation goes to a human.

A "price" means a price field or a currency-marked amount in tool text,
never any number that happens to appear in a tool result. Each allowed
price keeps a note of the tool, product and field it came from, so a
blocked or passed reply can be traced back to its source.
"""
from __future__ import annotations

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


def prices_in_tool_result(result: object, tool_name: str = "tool") -> dict[int, str]:
    """Every price a tool result actually states, mapped to where it came from.

    Only two things count as a price:
    - the value of a field whose name contains "price" (price_kes,
      original_price_kes, price_kes_max, price_kes_per_kg), and
    - an amount written with a currency marker inside any text field
      ("KSh 400 per kg", a live page that says "KES 29,000").

    A bare number is never a price. A load rating, a stock count or a
    product weight cannot license a KES amount in the reply. Before this,
    any number of 100 or more counted, so a bench rated 120kg let the bot
    quote KES 6,000 (120 x 50).
    """
    found: dict[int, str] = {}

    def walk(node: object, path: str, product: str) -> None:
        if isinstance(node, dict):
            product = str(node.get("id") or product)
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if "price" in str(key).lower() and _is_amount(value):
                    found.setdefault(int(value), f"{tool_name}:{product}:{child}")
                else:
                    walk(value, child, product)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", product)
        elif isinstance(node, str):
            for amount in amounts_in_text(node):
                found.setdefault(amount, f"{tool_name}:{product}:{path} (text)")

    walk(result, "", "")
    return found


def _is_amount(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0 and float(value).is_integer()
    if isinstance(value, str):
        digits = value.replace(",", "").strip()
        return digits.isdigit() and int(digits) > 0
    return False


def numbers_in_tool_result(result: object) -> set[int]:
    """Prices in a tool result (kept for callers that only need the values)."""
    return set(prices_in_tool_result(result))


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
