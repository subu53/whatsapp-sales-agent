"""The price guard blocks any KES amount the tools did not return this turn."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import brain  # noqa: E402
from app.agent.price_guard import (  # noqa: E402
    amounts_in_text,
    numbers_in_customer_text,
    numbers_in_tool_result,
    unverified_amounts,
)


def test_reads_the_ways_kenyans_write_prices():
    text = "Bench is KES 29,000, plates Ksh 1500, mat 2,500/-, rack KSh. 80k. Load 350kg."
    assert amounts_in_text(text) == {29000, 1500, 2500, 80000}


def test_specs_without_currency_are_ignored():
    assert amounts_in_text("Rated for 350kg, 30kg frame, 2 year warranty") == set()


def test_price_from_tool_result_passes():
    tool = numbers_in_tool_result({"results": [{"id": "bench", "price_kes": 29000}]})
    assert unverified_amounts("That bench is KES 29,000.", tool) == set()


def test_invented_price_is_caught():
    tool = numbers_in_tool_result({"results": [{"id": "bench", "price_kes": 29000}]})
    assert unverified_amounts("That bench is KES 27,500 this week.", tool) == {27500}


def test_quantity_quote_passes_but_odd_total_does_not():
    tool = {29000}
    assert unverified_amounts("Two benches come to KES 58,000.", tool) == set()
    assert unverified_amounts("Two benches come to KES 55,000.", tool) == {55000}


def test_customer_budget_can_be_repeated_back():
    customer = numbers_in_customer_text("my budget is 50k")
    assert unverified_amounts("With KES 50,000 you can get the bench.", {29000}, customer) == set()


def test_no_tool_call_means_no_price_allowed():
    assert unverified_amounts("It's around KES 30,000.", set()) == {30000}


def test_agent_replaces_unverified_reply_and_escalates(monkeypatch):
    calls = []
    monkeypatch.setitem(brain.TOOL_IMPLS, "escalate_to_human", lambda ctx, reason: calls.append(reason))
    agent = brain.SalesAgent.__new__(brain.SalesAgent)
    ctx = SimpleNamespace(phone_number="whatsapp:+254700000001")

    out = agent._guard_prices("Sure, the treadmill is KES 95,000.", {80000}, set(), ctx)

    assert out == brain.PRICE_CHECK_REPLY
    assert calls and "95000" in calls[0]


def test_agent_leaves_verified_reply_alone(monkeypatch):
    monkeypatch.setitem(brain.TOOL_IMPLS, "escalate_to_human", lambda ctx, reason: None)
    agent = brain.SalesAgent.__new__(brain.SalesAgent)
    ctx = SimpleNamespace(phone_number="whatsapp:+254700000001")
    reply = "The treadmill is KES 80,000 and delivery is confirmed by location."

    assert agent._guard_prices(reply, {80000}, set(), ctx) == reply
