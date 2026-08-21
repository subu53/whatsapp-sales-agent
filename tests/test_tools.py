"""Tests for tool implementations, including the domain-restricted live
fetch tool (mocked — no real network call, per this project's own rule
that only WebFetch-equivalent, sanctioned paths touch the live internet
during automated test runs)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools import ToolContext, _tool_escalate_to_human, _tool_fetch_live_page, _tool_log_lead_info  # noqa: E402
from app.rag.catalogue_search import CatalogueIndex  # noqa: E402
from app.rag.site_search import SiteIndex  # noqa: E402
from app.storage import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _make_ctx(tmp_path, phone="whatsapp:+254700000099"):
    db_path = str(tmp_path / "test_leads.db")
    db.init_db(db_path)
    catalogue = CatalogueIndex(str(ROOT / "data" / "catalogue" / "products.json"))
    site_index = SiteIndex(str(ROOT / "data" / "site_info.json"))
    return ToolContext(
        phone_number=phone,
        db_path=db_path,
        catalogue=catalogue,
        site_index=site_index,
        allowed_fetch_domain="alphafitness.co.ke",
    )


def test_fetch_live_page_refuses_other_domains(tmp_path):
    ctx = _make_ctx(tmp_path)
    result = _tool_fetch_live_page(ctx, "https://evil.example.com/steal-data")
    assert "error" in result
    assert "alphafitness.co.ke" in result["error"]


def test_fetch_live_page_allows_relative_paths_on_own_domain(tmp_path):
    ctx = _make_ctx(tmp_path)
    fake_response = MagicMock()
    fake_response.text = "<html><body><p>Returns accepted within 7 days.</p></body></html>"
    fake_response.raise_for_status = MagicMock()
    with patch("app.agent.tools.httpx.get", return_value=fake_response) as mock_get:
        result = _tool_fetch_live_page(ctx, "/returns-refunds-policy/")
    assert "error" not in result
    assert "Returns accepted" in result["text"]
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://alphafitness.co.ke/returns-refunds-policy/"


def test_log_lead_info_persists_fields(tmp_path):
    ctx = _make_ctx(tmp_path)
    _tool_log_lead_info(ctx, customer_name="Jane", use_case="hotel gym", lead_stage="discovering")
    with db._connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT customer_name, use_case, lead_stage FROM conversations WHERE phone_number = ?",
            (ctx.phone_number,),
        ).fetchone()
    assert row is None  # no conversation row exists yet -- log_lead_info doesn't create one


def test_log_lead_info_updates_existing_conversation(tmp_path):
    ctx = _make_ctx(tmp_path)
    db.get_or_create_conversation(ctx.db_path, ctx.phone_number)
    _tool_log_lead_info(ctx, customer_name="Jane", use_case="hotel gym", lead_stage="discovering")
    with db._connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT customer_name, use_case, lead_stage FROM conversations WHERE phone_number = ?",
            (ctx.phone_number,),
        ).fetchone()
    assert row["customer_name"] == "Jane"
    assert row["use_case"] == "hotel gym"
    assert row["lead_stage"] == "discovering"


def test_escalate_to_human_marks_conversation(tmp_path):
    ctx = _make_ctx(tmp_path)
    db.get_or_create_conversation(ctx.db_path, ctx.phone_number)
    result = _tool_escalate_to_human(ctx, reason="Customer wants a tender quotation")
    assert result["status"] == "escalated"
    with db._connect(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT escalated, escalation_reason, lead_stage FROM conversations WHERE phone_number = ?",
            (ctx.phone_number,),
        ).fetchone()
    assert row["escalated"] == 1
    assert "tender" in row["escalation_reason"]
    assert row["lead_stage"] == "handed_off"


def test_escalate_to_human_notification_failure_does_not_raise(tmp_path):
    ctx = _make_ctx(tmp_path)
    db.get_or_create_conversation(ctx.db_path, ctx.phone_number)
    ctx.twilio_client = MagicMock()
    ctx.twilio_client.messages.create.side_effect = RuntimeError("network down")
    ctx.human_escalation_whatsapp = "+254700000000"
    result = _tool_escalate_to_human(ctx, reason="test")
    assert result["status"] == "escalated"
    assert result["human_notified"] is False
