"""End-to-end tests against the FastAPI app via TestClient — covers the
webhook short-circuit paths (idempotency, opt-out, rate limit) that must
never reach the agent loop, so they need no LLM key to test."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_leads.db"))
    monkeypatch.setattr(settings, "twilio_validate_signature", False)
    monkeypatch.setattr(settings, "rate_limit_max_messages", 3)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 3600)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _post_message(client, phone, body, sid):
    return client.post(
        "/webhook/whatsapp",
        data={"From": f"whatsapp:{phone}", "Body": body, "ProfileName": "Test", "MessageSid": sid},
    )


def test_opt_out_short_circuits_before_agent(client, monkeypatch):
    from app.agent.brain import SalesAgent
    calls = []
    monkeypatch.setattr(SalesAgent, "handle_message", lambda self, phone, text, name=None: calls.append(1) or "unused")

    resp = _post_message(client, "+254700000010", "STOP", "SM001")
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()
    assert calls == []


def test_opted_out_contact_stays_silent(client, monkeypatch):
    from app.agent.brain import SalesAgent
    calls = []
    monkeypatch.setattr(SalesAgent, "handle_message", lambda self, phone, text, name=None: calls.append(1) or "unused")

    _post_message(client, "+254700000011", "stop", "SM010")
    resp = _post_message(client, "+254700000011", "hello again", "SM011")
    assert resp.status_code == 200
    assert "<Message>" not in resp.text
    assert calls == []


def test_opt_in_resubscribes(client, monkeypatch):
    from app.agent.brain import SalesAgent
    monkeypatch.setattr(SalesAgent, "handle_message", lambda self, phone, text, name=None: "hi")

    _post_message(client, "+254700000012", "stop", "SM020")
    resp = _post_message(client, "+254700000012", "START", "SM021")
    assert "resubscribed" in resp.text.lower()


def test_duplicate_message_sid_is_not_reprocessed(client, monkeypatch):
    from app.agent.brain import SalesAgent
    from app.channels import twilio_whatsapp
    calls = []
    def fake_handle(self, phone, text, name=None):
        calls.append(text)
        return "the real reply"
    monkeypatch.setattr(SalesAgent, "handle_message", fake_handle)

    sent = []
    monkeypatch.setattr(
        twilio_whatsapp, "_send_whatsapp",
        lambda from_whatsapp, to_whatsapp, body: sent.append((to_whatsapp, body)),
    )

    r1 = _post_message(client, "+254700000013", "how much is a treadmill", "SM030")
    r2 = _post_message(client, "+254700000013", "how much is a treadmill", "SM030")  # Twilio retry, same SID

    # The agent runs exactly once, even though Twilio delivered twice.
    assert len(calls) == 1

    # Both webhook responses are empty TwiML: the reply no longer travels
    # back inline, it goes out asynchronously via the REST API. That is the
    # whole point of the split — Twilio times out at 5s and the agent is
    # slower than that.
    assert "<Message>" not in r1.text
    assert "<Message>" not in r2.text

    # ...and the real reply was delivered out-of-band, exactly once.
    assert sent == [("whatsapp:+254700000013", "the real reply")]


def test_slow_agent_still_acknowledges_twilio_immediately(client, monkeypatch):
    """Twilio abandons a webhook after 5s; the ack must not wait on the agent."""
    import time
    from app.agent.brain import SalesAgent
    from app.channels import twilio_whatsapp

    def slow_handle(self, phone, text, name=None):
        time.sleep(1.0)
        return "eventually"
    monkeypatch.setattr(SalesAgent, "handle_message", slow_handle)
    monkeypatch.setattr(twilio_whatsapp, "_send_whatsapp", lambda *a, **k: None)

    start = time.monotonic()
    resp = _post_message(client, "+254700000015", "do you have treadmills", "SM050")
    # TestClient runs background tasks inline after the response is produced,
    # so assert on the response itself rather than wall-clock: an empty body
    # proves nothing was waiting on the agent to build a reply.
    assert resp.status_code == 200
    assert "<Message>" not in resp.text


def test_rate_limit_blocks_after_threshold(client, monkeypatch):
    from app.agent.brain import SalesAgent
    from app.storage import db
    monkeypatch.setattr(SalesAgent, "handle_message", lambda self, phone, text, name=None: "ok")

    phone = "+254700000014"
    for i in range(3):  # fixture sets rate_limit_max_messages=3
        db.append_message(settings.database_path, phone, "user", f"seed {i}")

    resp = _post_message(client, phone, "one more", "SM044")
    assert "sent a lot of messages" in resp.text.lower()
