"""Twilio WhatsApp webhook: receives inbound messages, replies asynchronously.

Why this is not a simple synchronous TwiML handler:

Twilio abandons any webhook that hasn't responded within **5 seconds**. The
agent loop routinely takes longer than that — a catalogue search plus two or
more LLM round-trips measured ~6s on a warm B1 instance, and a multi-tool
conversation can take far longer. Replying inline with TwiML therefore loses
the message silently: Twilio logs it as "Received", gives up waiting, and the
customer sees nothing at all.

So the handler splits in two:

  1. The webhook does only the cheap, deterministic checks (signature,
     idempotency, opt-out, rate limit) and returns empty TwiML immediately —
     well inside Twilio's budget.
  2. The agent runs in a background task, and its reply is sent as a *new
     outbound message* through Twilio's REST API when it's ready.

The customer experience is identical; the reply just arrives a few seconds
later, the way a human typing would.

Order of the inline checks still matters and is deliberate: idempotency first
(don't even look at content twice), then opt-out (a "STOP" must never reach
the LLM or get a marketing-flavored reply), then rate limit (protects spend
before the expensive part is scheduled).
"""
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

from app.config import settings
from app.storage import db

log = logging.getLogger(__name__)

router = APIRouter()

_OPT_OUT_RE = re.compile(r"^\s*(stop|unsubscribe|cancel|opt\s*out)\s*$", re.IGNORECASE)
_OPT_IN_RE = re.compile(r"^\s*(start|subscribe|opt\s*in|unstop)\s*$", re.IGNORECASE)

# WhatsApp rejects a single message body longer than 1600 characters.
_MAX_BODY = 1600

_FALLBACK_REPLY = (
    "Sorry — something went wrong on our side just now. A member of the team "
    "will follow up, or you can reach us directly on WhatsApp."
)


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    To: str = Form(None),
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

    # Claim the MessageSid now, before scheduling the background work. If we
    # waited until the agent finished, a Twilio retry arriving in the
    # meantime would start a second agent run for the same message.
    if MessageSid:
        db.mark_message_processed(settings.database_path, MessageSid)

    phone_number = From.replace("whatsapp:", "")
    body = Body.strip()

    # --- Opt-out / opt-in: required by WhatsApp Business policy, and just
    # correct behavior — this must be handled before the message ever
    # reaches the LLM, not left to the prompt to interpret. These replies
    # are instant, so they can still go back inline as TwiML.
    if _OPT_OUT_RE.match(body):
        db.get_or_create_conversation(settings.database_path, phone_number, ProfileName)
        db.set_opted_out(settings.database_path, phone_number, True)
        return _reply_twiml(
            "You're unsubscribed and won't get further messages from us here. "
            "Reply START anytime to resume."
        )

    if db.is_opted_out(settings.database_path, phone_number):
        if _OPT_IN_RE.match(body):
            db.set_opted_out(settings.database_path, phone_number, False)
            return _reply_twiml("You're resubscribed — how can we help?")
        # Opted out and not re-subscribing: stay silent rather than reply.
        return _empty_twiml()

    # --- Rate limit: cheap DB count, checked before the agent is scheduled
    # so a burst from one number can't run up the bill or bury a human's
    # escalation queue.
    recent = db.count_recent_messages(
        settings.database_path, phone_number, settings.rate_limit_window_seconds
    )
    if recent >= settings.rate_limit_max_messages:
        return _reply_twiml(
            "You've sent a lot of messages in a short time — give us a few minutes "
            "and message again, or call/WhatsApp the team directly if it's urgent."
        )

    # --- The expensive part. Hand it to the background and acknowledge
    # Twilio immediately; the reply goes out via the REST API when ready.
    background_tasks.add_task(
        _run_agent_and_reply,
        agent=request.app.state.agent,
        phone_number=phone_number,
        body=body,
        profile_name=ProfileName,
        customer_whatsapp=From,
        business_whatsapp=To or settings.twilio_whatsapp_number,
    )
    return _empty_twiml()


def _run_agent_and_reply(
    agent,
    phone_number: str,
    body: str,
    profile_name: str,
    customer_whatsapp: str,
    business_whatsapp: str,
) -> None:
    """Run the agent, then send its reply as an outbound WhatsApp message.

    Runs after the HTTP response has already gone back to Twilio, so nothing
    here can be reported to the caller — every failure path has to end in
    either a sent message or a log line, never a silent drop.
    """
    try:
        reply_text = agent.handle_message(phone_number, body, profile_name)
    except Exception:
        log.exception("Agent failed for %s", phone_number)
        reply_text = _FALLBACK_REPLY

    if not reply_text or not reply_text.strip():
        log.error("Agent returned an empty reply for %s", phone_number)
        reply_text = _FALLBACK_REPLY

    try:
        _send_whatsapp(business_whatsapp, customer_whatsapp, reply_text)
    except Exception:
        log.exception("Failed to deliver reply to %s", phone_number)


def _send_whatsapp(from_whatsapp: str, to_whatsapp: str, body: str) -> None:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        log.error("Twilio credentials missing — cannot send reply to %s", to_whatsapp)
        return

    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    for chunk in _split_for_whatsapp(body):
        client.messages.create(from_=from_whatsapp, to=to_whatsapp, body=chunk)


def _split_for_whatsapp(text: str) -> list:
    """Split on paragraph boundaries so a long reply doesn't get truncated."""
    if len(text) <= _MAX_BODY:
        return [text]

    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= _MAX_BODY:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single paragraph over the limit still has to be cut somewhere.
        while len(paragraph) > _MAX_BODY:
            chunks.append(paragraph[:_MAX_BODY])
            paragraph = paragraph[_MAX_BODY:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


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
    form = dict(await request.form())
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    # Twilio signs the exact public URL configured in its console. What the
    # app sees behind a proxy is not that URL, and every host exposes the
    # original differently: Render sets x-forwarded-host, Azure App Service
    # does not set it at all and terminates TLS in front of a plain-http
    # container, so request.url.scheme reads "http". Guessing one
    # reconstruction is what made this fail on Azure after working on
    # Render — so try each plausible public URL instead and accept the
    # request if any matches. An attacker still needs the auth token to
    # forge a valid signature for any of them.
    path = request.url.path
    hosts = [
        request.headers.get("x-forwarded-host"),
        request.headers.get("x-original-host"),
        request.headers.get("host"),
        request.url.netloc,
    ]
    protos = [request.headers.get("x-forwarded-proto"), "https", "http"]

    tried = set()
    for host in hosts:
        for proto in protos:
            if not host or not proto:
                continue
            url = f"{proto}://{host}{path}"
            if url in tried:
                continue
            tried.add(url)
            if validator.validate(url, form, signature):
                return True

    log.warning("Twilio signature did not match any candidate URL: %s", sorted(tried))
    return False
