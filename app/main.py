"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

The startup banner here exists because of a real debugging session: a
misconfigured Twilio auth token (the literal string "<current>", pasted from
a placeholder) and a silently-stale LLM_PROVIDER both cost hours to find,
because nothing in the logs showed what the process had actually loaded.
Every setting that has burned us once is now reported at boot — secrets as
fingerprints, never in full — and served from /debug/config so it can be
checked without a redeploy.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from twilio.rest import Client as TwilioClient

from app.agent.brain import SalesAgent
from app.channels.meta_whatsapp import router as meta_router
from app.channels.twilio_whatsapp import router as twilio_router
from app.config import settings
from app.rag.catalogue_search import CatalogueIndex
from app.rag.site_search import SiteIndex
from app.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _fingerprint(secret: str) -> str:
    """Identify a secret without disclosing it.

    The last four characters plus the length are enough to compare against a
    provider's console at a glance, and enough to catch the failure modes
    that actually happen: an unset value, a truncated paste, or a literal
    placeholder like "<current>" (which shows up as len 9, not len 32).
    """
    if not secret:
        return "UNSET"
    return f"...{secret[-4:]} (len {len(secret)})"


def _config_report() -> dict:
    """Non-secret view of what this process actually loaded."""
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "deepseek":
        llm_key, llm_model = settings.deepseek_api_key, settings.deepseek_model
    else:
        llm_key, llm_model = settings.anthropic_api_key, settings.anthropic_model

    return {
        "llm_provider": provider,
        "llm_model": llm_model,
        "llm_key": _fingerprint(llm_key),
        "twilio_account_sid": _fingerprint(settings.twilio_account_sid),
        "twilio_auth_token": _fingerprint(settings.twilio_auth_token),
        "twilio_whatsapp_number": settings.twilio_whatsapp_number,
        "validate_twilio_signature": settings.twilio_validate_signature,
        "database_path": settings.database_path,
        "escalation_whatsapp_set": bool(settings.human_escalation_whatsapp),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_path)
    catalogue = CatalogueIndex(settings.catalogue_path)
    site_index = SiteIndex(settings.site_info_path)

    twilio_client = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    app.state.agent = SalesAgent(settings, catalogue, site_index, twilio_client)

    cfg = _config_report()
    log.info(
        "[startup] %s products, %s site-info topics loaded | LLM %s (%s) key=%s | "
        "Twilio sid=%s token=%s number=%s validate_signature=%s | db=%s",
        len(catalogue.products),
        len(site_index.documents),
        cfg["llm_provider"],
        cfg["llm_model"],
        cfg["llm_key"],
        cfg["twilio_account_sid"],
        cfg["twilio_auth_token"],
        cfg["twilio_whatsapp_number"],
        cfg["validate_twilio_signature"],
        cfg["database_path"],
    )

    # Loud, unmissable warnings for the states that look fine at boot but
    # fail silently in front of a customer.
    if not cfg["llm_key"] or cfg["llm_key"] == "UNSET":
        log.error("[startup] No API key for provider '%s' — every reply will fail.", cfg["llm_provider"])
    if twilio_client is None:
        log.error("[startup] Twilio not configured — inbound may work, but no reply can be sent.")
    if not settings.twilio_validate_signature:
        log.warning(
            "[startup] Twilio signature validation is OFF. Anyone who finds the webhook URL "
            "can post messages as a customer. Acceptable for sandbox testing only."
        )

    yield


app = FastAPI(title="Alpha Fitness WhatsApp Sales Agent", lifespan=lifespan)
app.include_router(twilio_router)
app.include_router(meta_router)


@app.get("/")
def root():
    # Uptime checkers (and Azure's Always On ping) probe "/" by default; give
    # them a real 200 instead of a 404 so the logs aren't full of spurious
    # failures. Not a customer-facing route.
    return {"status": "ok", "service": "alpha-fitness-whatsapp-agent"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    # Minimal privacy policy page — required by Meta to publish the WhatsApp
    # app. Not the customer-facing agent; just satisfies the Meta App
    # settings "Privacy policy URL" requirement.
    return """<!DOCTYPE html>
<html><head><title>Privacy Policy — Alpha Fitness WhatsApp Sales Agent</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; line-height: 1.5;">
<h1>Privacy Policy</h1>
<p>This WhatsApp assistant is operated for Alpha Fitness by Esubutech.</p>
<p>When you message this number, we process your phone number and the
content of your messages solely to respond to your inquiries about Alpha
Fitness products and services. We do not sell or share your data with
third parties. Messages may be processed by third-party AI providers
(Anthropic or DeepSeek) solely to generate replies, and are retained only
as long as needed to support your conversation history with us.</p>
<p>You can stop receiving messages at any time by replying STOP, and
resume by replying START.</p>
<p>Contact: <a href="mailto:subaram5@gmail.com">subaram5@gmail.com</a></p>
</body></html>"""


@app.get("/debug/config")
def debug_config():
    """What this process actually loaded. Secrets appear as fingerprints only.

    Checking a setting this way takes one curl; checking it by redeploying
    and reading the boot log takes two minutes and a container restart.
    """
    return _config_report()


@app.post("/debug/simulate")
def simulate(payload: dict, request: Request):
    """Local testing endpoint — bypasses Twilio and WhatsApp entirely.

    POST {"phone": "whatsapp:+254700000000", "message": "hi, how much is a treadmill"}

    Note this runs the agent *synchronously* and returns the reply inline, so
    it also doubles as a timing check: if this takes over ~5s, the real
    webhook could not have answered inline either — which is exactly why the
    Twilio channel sends its reply asynchronously.
    """
    agent: SalesAgent = request.app.state.agent
    reply = agent.handle_message(payload["phone"], payload["message"], payload.get("name"))
    return {"reply": reply}


@app.get("/debug/leads")
def leads():
    """Quick read-only view of every lead captured so far.

    WARNING: this returns real customer phone numbers and conversation
    context with no authentication. Fine while the only contact is a test
    handset; it must be removed or put behind auth before real customers
    reach this deployment.
    """
    return db.all_leads(settings.database_path)
