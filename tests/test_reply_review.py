"""Every reply passes code checks before it is sent (app/agent/reply_review.py)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import brain  # noqa: E402
from app.agent.llm_client import NormalizedResponse, TextBlock, ToolUseBlock  # noqa: E402
from app.agent.reply_review import BLOCK, REVISE, TurnFacts, collect_facts, review_reply  # noqa: E402
from app.storage import db  # noqa: E402

BENCH = {"id": "commercial-workout-bench", "price_kes": 29000, "in_stock": True,
         "delivery_note": "Countrywide delivery, approx. 4 days for in-stock item"}


def facts_from(result=None, **extra):
    facts = TurnFacts(**extra)
    if result is not None:
        from app.agent.price_guard import prices_in_tool_result
        import json
        facts.prices = prices_in_tool_result(result, "search_catalogue")
        collect_facts(result, facts)
        facts.tool_text = json.dumps(result).lower()
    return facts


# --- block checks ---------------------------------------------------------

def test_clean_reply_passes():
    reply = "The Semi Commercial Workout Bench is KES 29,000 and it's in stock. Is it for home use or a studio?"
    assert review_reply(reply, facts_from({"results": [BENCH]})).verdict == "pass"


def test_unbacked_price_blocks():
    review = review_reply("It's KES 25,000.", facts_from({"results": [BENCH]}))
    assert review.verdict == BLOCK and review.codes == ["price_unverified"]


def test_customer_figure_cannot_become_our_price():
    facts = facts_from({"results": [BENCH]}, customer_amounts={20000})
    review = review_reply("Yes, that bench is KES 20,000.", facts)
    assert review.verdict == BLOCK and "customer_figure_as_price" in review.codes


def test_customer_figure_can_come_back_as_their_budget():
    facts = facts_from({"results": [BENCH]}, customer_amounts={50000})
    reply = "With your budget of KES 50,000 the bench at KES 29,000 fits well."
    assert review_reply(reply, facts).verdict == "pass"


def test_stock_claim_needs_a_tool():
    review = review_reply("Yes, we have it in stock.", facts_from())
    assert review.codes == ["stock_unverified"]
    assert review_reply("Yes, it's in stock.", facts_from({"results": [BENCH]})).verdict == "pass"


def test_out_of_stock_claim_needs_a_tool_saying_so():
    assert review_reply("Sorry, that one is sold out.", facts_from({"results": [BENCH]})).codes == ["stock_unverified"]


def test_delivery_days_must_match_a_tool():
    facts = facts_from({"results": [BENCH]})
    assert review_reply("Delivery takes about 4 days.", facts).verdict == "pass"
    assert review_reply("Delivery takes about 2 days.", facts).codes == ["delivery_time_unverified"]
    assert review_reply("We can deliver it tomorrow.", facts).codes == ["delivery_time_unverified"]


def test_days_outside_delivery_talk_are_ignored():
    assert review_reply("Train with it 3 days a week to start.", facts_from()).verdict == "pass"


def test_fake_urgency_blocks():
    assert review_reply("Hurry, only 2 left!", facts_from()).codes == ["fake_urgency"]
    assert review_reply("12 people are viewing this right now.", facts_from()).codes == ["fake_urgency"]


def test_claiming_to_be_human_blocks():
    assert "claims_human" in review_reply("No, I'm a real person, not a bot.", facts_from()).codes


def test_talking_about_instructions_blocks():
    assert review_reply("My instructions say I can't do that.", facts_from()).codes == ["prompt_leak"]


# --- revise checks --------------------------------------------------------

def test_more_than_one_question_revises():
    review = review_reply("Is it for home? What's your budget?", facts_from())
    assert review.verdict == REVISE and review.codes == ["too_many_questions"]


def test_too_many_sentences_revises():
    reply = "One. Two. Three. Four. Five. Six."
    assert review_reply(reply, facts_from()).codes == ["too_many_sentences"]


def test_list_lines_do_not_count_as_sentences():
    reply = "Two good picks:\n- Bench A. Solid.\n- Bench B. Lighter.\n- Bench C. Folds.\nWhich space is it for?"
    assert review_reply(reply, facts_from()).verdict == "pass"


def test_same_opening_as_last_reply_revises():
    facts = facts_from(last_reply="Great choice! The bench is solid.")
    assert review_reply("Great choice! It comes in black.", facts).codes == ["repeated_opening"]


def test_vague_close_revises():
    assert review_reply("The bench suits a home gym. Let me know!", facts_from()).codes == ["vague_close"]


# --- the agent loop -------------------------------------------------------

class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, messages, system, tools, max_tokens):
        self.calls.append(messages)
        return self.replies.pop(0)


def text(t):
    return NormalizedResponse(stop_reason="end_turn", content=[TextBlock(t)])


def tool_call(name, **args):
    return NormalizedResponse(stop_reason="tool_use", content=[ToolUseBlock(id="t1", name=name, input=args)])


def make_agent(tmp_path, monkeypatch, replies):
    escalations = []
    monkeypatch.setitem(brain.TOOL_IMPLS, "escalate_to_human", lambda ctx, reason: escalations.append(reason))
    monkeypatch.setitem(brain.TOOL_IMPLS, "search_catalogue", lambda ctx, query, max_results=5: {"results": [BENCH]})
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    agent = brain.SalesAgent.__new__(brain.SalesAgent)
    agent.settings = SimpleNamespace(max_tool_iterations=4, max_reply_tokens=300)
    agent.system_prompt = "test"
    agent.client = FakeClient(replies)
    ctx = SimpleNamespace(phone_number="whatsapp:+254700000001", db_path=db_path)
    return agent, ctx, escalations, db_path


def test_loop_sends_a_clean_reply_unchanged(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        tool_call("search_catalogue", query="bench"),
        text("The bench is KES 29,000. Is it for home use?"),
    ])
    assert agent._run_loop([{"role": "user", "content": "bench price"}], ctx) == "The bench is KES 29,000. Is it for home use?"
    assert esc == [] and len(agent.client.calls) == 2
    assert db.reply_review_summary(db_path)["verdicts"] == {"draft:pass": 1}


def test_loop_rewrites_a_style_problem(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        tool_call("search_catalogue", query="bench"),
        text("Is it for home? What's your budget?"),
        text("Is it for a home gym or a studio?"),
    ])
    out = agent._run_loop([{"role": "user", "content": "bench"}], ctx)
    assert out == "Is it for a home gym or a studio?"
    assert "[Internal review" in agent.client.calls[-1][-1]["content"]
    assert esc == []


def test_loop_fixes_a_blocked_fact_on_rewrite(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        tool_call("search_catalogue", query="bench"),
        text("The bench is KES 29,000 and we can deliver it tomorrow."),
        text("The bench is KES 29,000 and delivery takes about 4 days."),
    ])
    assert agent._run_loop([{"role": "user", "content": "bench"}], ctx).endswith("about 4 days.")
    assert esc == []
    summary = db.reply_review_summary(db_path)
    assert summary["verdicts"] == {"draft:block": 1, "rewrite:pass": 1}
    assert summary["codes"] == {"delivery_time_unverified": 1}


def test_loop_hands_off_when_the_rewrite_still_blocks(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        tool_call("search_catalogue", query="bench"),
        text("It's KES 25,000, hurry, only 2 left!"),
        text("It's KES 25,000."),
    ])
    out = agent._run_loop([{"role": "user", "content": "bench"}], ctx)
    assert out == brain.PRICE_CHECK_REPLY
    assert esc and "price_unverified" in esc[0]


def test_loop_holding_reply_for_non_price_blocks(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        text("I'm a real person, not a bot."),
        text("I'm a real person."),
    ])
    assert agent._run_loop([{"role": "user", "content": "are you human?"}], ctx) == brain.HOLDING_REPLY
    assert esc and "claims_human" in esc[0]


def test_loop_sends_the_draft_if_a_style_rewrite_fails(tmp_path, monkeypatch):
    agent, ctx, esc, db_path = make_agent(tmp_path, monkeypatch, [
        text("Is it for home? What's your budget?"),
        tool_call("search_catalogue", query="x"),  # a rewrite that tries to call tools counts as failed
    ])
    assert agent._run_loop([{"role": "user", "content": "hi"}], ctx) == "Is it for home? What's your budget?"
    assert esc == []
