"""Twilio WhatsApp webhook: receives inbound messages, replies via TwiML.

Order of checks matters here and is deliberate: idempotency first (don't
even look at content twice), then opt-out (a "STOP" must never reach the
LLM or get a marketing-flavored reply), then rate limit (protects spend
before the expensive part runs), and only then the actual agent loop.
"""
import re

from fastapi import APIRouter, Form, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import settings
from app.storage import db

router = APIRouter()

_OPT_OUT_RE = re.compile(r"^\s*(stop|unsubscribe|cancel|opt\s*out)\s*$", re.IGNORECASE)
_OPT_IN_RE = re.compile(r"^\s*(start|subscribe|opt\s*in|unstop)\s*$", re.IGNORECASE)


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    ProfileName: str = Form(None),
    MessageSid: str = Form(None),
):
    if settings.twilio_validate_signature:
        if not await _valid_twilio_signature(request):
            return Response(status_code=403, content="Invalid Twilio signature")

    # --- Idempotency: Twilio retries a webhook that didn't respond fast
    # enough, or on a transient error on their side. Without this check, a
    # retried delivery re-runs the whole agent loop and the customer can
    # get the same question answered twice — or, worse, two different
    # answers if a tool result changed in between (a price update, say).
    if MessageSid and db.is_message_processed(settings.database_path, MessageSid):
        return _empty_twiml()

    phone_number = From.replace("whatsapp:", "")
    body = Body.strip()

    # --- Opt-out / opt-in: required by WhatsApp Business policy, and just
    # correct behavior — this must be handled before the message ever
    # reaches the LLM, not left to the prompt to interpret.
    if _OPT_OUT_RE.match(body):
        db.get_or_create_conversation(settings.database_path, phone_number, ProfileName)
        db.set_opted_out(settings.database_path, phone_number, True)
        if MessageSid:
            db.mark_message_processed(settings.database_path, MessageSid)
        return _reply_twiml("You're unsubscribed and won't get further messages from us here. Reply START anytime to resume.")

    if db.is_opted_out(settings.database_path, phone_number):
        if _OPT_IN_RE.match(body):
            db.set_opted_out(settings.database_path, phone_number, False)
            if MessageSid:
                db.mark_message_processed(settings.database_path, MessageSid)
            return _reply_twiml("You're resubscribed — how can we help?")
        # Opted out and not re-subscribing: stay silent rather than reply.
        if MessageSid:
            db.mark_message_processed(settings.database_path, MessageSid)
        return _empty_twiml()

    # --- Rate limit: cheap DB count, checked before the LLM call so a
    # burst from one number can't run up the bill or bury a human's
    # escalation queue.
    recent = db.count_recent_messages(
        settings.database_path, phone_number, settings.rate_limit_window_seconds
    )
    if recent >= settings.rate_limit_max_messages:
        if MessageSid:
            db.mark_message_processed(settings.database_path, MessageSid)
        return _reply_twiml(
            "You've sent a lot of messages in a short time — give us a few minutes "
            "and message again, or call/WhatsApp the team directly if it's urgent."
        )

    agent = request.app.state.agent
    reply_text = agent.handle_message(phone_number, body, ProfileName)

    if MessageSid:
        db.mark_message_processed(settings.database_path, MessageSid)

    return _reply_twiml(reply_text)


def _reply_twiml(text: str) -> Response:
    twiml = MessagingResponse()
    twiml.message(text)
    return Response(content=str(twiml), media_type="application/xml")


def _empty_twiml() -> Response:
    return Response(content=str(MessagingResponse()), media_type="application/xml")


async def _valid_twilio_signature(request: Request) -> bool:
    if not settings.twilio_auth_token:
        return True  # nothing to validate against in local/dev mode

    validator = RequestValidator(settings.twilio_auth_token)
    form = await request.form()

    # Behind a tunnel/proxy (cloudflared, ngrok, a real load balancer) the
    # URL Twilio actually signed is the public https URL, not the internal
    # http://127.0.0.1:8000 one uvicorn sees — reconstruct it from the
    # forwarded headers when present, or signature validation fails even
    # for genuine requests.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    url = f"{proto}://{host}{request.url.path}"

    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(url, dict(form), signature)
