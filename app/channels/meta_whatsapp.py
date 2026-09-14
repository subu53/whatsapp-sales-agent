"""Meta WhatsApp Cloud API webhook: receives inbound messages, replies asynchronously.

Same shape as app/channels/twilio_whatsapp.py — reused deliberately so the two
channels can run side by side (Twilio for later production use once a real
number is purchased and approved; Meta's own Cloud API test number for
free, immediate two-way testing tonight). All the actual sales-agent logic
(idempotency, opt-out, rate limiting, the agent call itself) is identical;
only the wire format and the outbound HTTP call differ:

  * Twilio POSTs form-encoded fields directly to the webhook.
  * Meta POSTs a nested JSON payload, and separately requires a one-time GET
    handshake (hub.mode / hub.verify_token / hub.challenge) before it will
    start forwarding messages at all.
  * Twilio sends replies via the `twilio` SDK; Meta sends replies via a
    plain HTTPS POST to the Graph API using a bearer token.
"""
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.channels.twilio_whatsapp import _OPT_IN_RE, _OPT_OUT_RE, _split_for_whatsapp
from app.config import settings
from app.storage import db

log = logging.getLogger(__name__)

router = APIRouter()

_FALLBACK_REPLY = (
    "Sorry — something went wrong on our side just now. A member of the team "
    "will follow up, or you can reach us directly on WhatsApp."
)


@router.get("/whatsapp/webhook")
async def verify_webhook(request: Request):
    """Meta's one-time handshake, required before it will POST anything here.

    Meta calls this with three query params and expects the raw
    hub.challenge value echoed back — as plain text, not JSON — if and only
    if hub.verify_token matches what you typed into the Meta dashboard.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.meta_webhook_verify_token:
        return Response(content=challenge, media_type="text/plain")

    log.warning("Meta webhook verification failed (mode=%s, token matched=%s)",
                mode, token == settings.meta_webhook_verify_token)
    return Response(status_code=403, content="Verification failed")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    # Meta sends delivery/read status callbacks through this same endpoint —
    # those have no "messages" key and must be accepted (200) and ignored,
    # not treated as a malformed inbound message.
    message, contact = _extract_message(payload)
    if message is None:
        return {"status": "ignored"}

    message_id = message.get("id")
    if message_id and db.is_message_processed(settings.database_path, message_id):
        return {"status": "duplicate"}
    if message_id:
        db.mark_message_processed(settings.database_path, message_id)

    phone_number = message.get("from", "")
    profile_name = (contact or {}).get("profile", {}).get("name")
    body = (message.get("text", {}) or {}).get("body", "").strip()

    if _OPT_OUT_RE.match(body):
        db.get_or_create_conversation(settings.database_path, phone_number, profile_name)
        db.set_opted_out(settings.database_path, phone_number, True)
        _send_whatsapp(
            phone_number,
            "You're unsubscribed and won't get further messages from us here. "
            "Reply START anytime to resume.",
        )
        return {"status": "opted_out"}

    if db.is_opted_out(settings.database_path, phone_number):
        if _OPT_IN_RE.match(body):
            db.set_opted_out(settings.database_path, phone_number, False)
            _send_whatsapp(phone_number, "You're resubscribed — how can we help?")
        return {"status": "silenced"}

    recent = db.count_recent_messages(
        settings.database_path, phone_number, settings.rate_limit_window_seconds
    )
    if recent >= settings.rate_limit_max_messages:
        _send_whatsapp(
            phone_number,
            "You've sent a lot of messages in a short time — give us a few minutes "
            "and message again, or call/WhatsApp the team directly if it's urgent.",
        )
        return {"status": "rate_limited"}

    background_tasks.add_task(
        _run_agent_and_reply,
        agent=request.app.state.agent,
        phone_number=phone_number,
        body=body,
        profile_name=profile_name,
    )
    return {"status": "accepted"}


def _extract_message(payload: dict):
    """Pull the first inbound message + its contact out of Meta's nested payload.

    Returns (None, None) for anything that isn't a customer message —
    status callbacks (sent/delivered/read) have the same top-level shape but
    no "messages" key, and must not be mistaken for silence/failure.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None, None
        contacts = value.get("contacts") or [{}]
        return messages[0], contacts[0]
    except (KeyError, IndexError, TypeError):
        log.warning("Unrecognized Meta webhook payload shape: %s", payload)
        return None, None


def _run_agent_and_reply(agent, phone_number: str, body: str, profile_name: str) -> None:
    try:
        reply_text = agent.handle_message(phone_number, body, profile_name)
    except Exception:
        log.exception("Agent failed for %s", phone_number)
        reply_text = _FALLBACK_REPLY

    if not reply_text or not reply_text.strip():
        log.error("Agent returned an empty reply for %s", phone_number)
        reply_text = _FALLBACK_REPLY

    try:
        _send_whatsapp(phone_number, reply_text)
    except Exception:
        log.exception("Failed to deliver reply to %s", phone_number)


def _send_whatsapp(to_number: str, body: str) -> None:
    if not (settings.meta_access_token and settings.meta_phone_number_id):
        log.error("Meta credentials missing — cannot send reply to %s", to_number)
        return

    url = (
        f"https://graph.facebook.com/{settings.meta_graph_api_version}/"
        f"{settings.meta_phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.meta_access_token}",
        "Content-Type": "application/json",
    }
    for chunk in _split_for_whatsapp(body):
        resp = httpx.post(
            url,
            headers=headers,
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_number,
                "type": "text",
                "text": {"body": chunk},
            },
            timeout=10.0,
        )
        if resp.status_code >= 400:
            log.error("Meta send failed (%s): %s", resp.status_code, resp.text)
