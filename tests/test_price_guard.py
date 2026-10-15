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
    prices_in_tool_result,
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


def test_spec_numbers_never_license_a_price():
    # The hole: a 120kg load rating used to let "KES 6,000" (120 x 50) through.
    tool = numbers_in_tool_result({"results": [{"id": "bench", "price_kes": 18500, "max_load_kg": 120, "stock": 150}]})
    assert tool == {18500}
    assert unverified_amounts("That bench is KES 6,000.", tool) == {6000}
    assert unverified_amounts("Only KES 150 delivery.", tool) == {150}


def test_spec_text_without_currency_is_not_a_price():
    tool = numbers_in_tool_result({"product": {"id": "bench", "price_kes": 29000,
                                               "specs": {"max_load_capacity": "350kg combined", "product_weight": "30kg"}}})
    assert tool == {29000}


def test_every_price_field_counts():
    tool = numbers_in_tool_result({"results": [
        {"id": "smith", "price_kes": 390000, "original_price_kes": 450000},
        {"id": "aerobic", "price_kes": 1000, "price_kes_max": 4500},
        {"id": "hex", "price_kes_per_kg": 400, "original_price_kes": None},
    ]})
    assert tool == {390000, 450000, 1000, 4500, 400}


def test_currency_marked_amount_in_text_counts():
    tool = numbers_in_tool_result({"url": "https://alphafitness.co.ke/x", "text": "Hex dumbbells KSh 400 per kg, sold in pairs. Rated 120kg."})
    assert tool == {400}
    assert unverified_amounts("A 20kg pair is KES 8,000.", tool) == set()


def test_price_keeps_its_source():
    found = prices_in_tool_result({"results": [{"id": "commercial-workout-bench", "price_kes": 29000}]}, "search_catalogue")
    assert found == {29000: "search_catalogue:commercial-workout-bench:results[0].price_kes"}


def test_real_catalogue_specs_add_no_prices():
    import json
    products = json.loads((Path(__file__).resolve().parents[1] / "data" / "catalogue" / "products.json").read_text())
    for product in products:
        allowed = numbers_in_tool_result({"product": product})
        price_fields = {int(v) for k, v in product.items() if "price" in k and isinstance(v, (int, float))}
        assert price_fields <= allowed
        for extra in allowed - price_fields:
            # Anything beyond the price fields must be a currency-marked amount in the product's own text.
            assert extra in amounts_in_text(json.dumps(product))


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
