"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from twilio.rest import Client as TwilioClient

from app.agent.brain import SalesAgent
from app.channels.twilio_whatsapp import router as twilio_router
from app.config import settings
from app.rag.catalogue_search import CatalogueIndex
from app.rag.site_search import SiteIndex
from app.storage import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_path)
    catalogue = CatalogueIndex(settings.catalogue_path)
    site_index = SiteIndex(settings.site_info_path)

    twilio_client = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    app.state.agent = SalesAgent(settings, catalogue, site_index, twilio_client)
    print(
        f"[startup] Loaded {len(catalogue.products)} products, "
        f"{len(site_index.documents)} site-info topics. "
        f"Twilio configured: {twilio_client is not None}. "
        f"Anthropic key set: {bool(settings.anthropic_api_key)}."
    )
    yield


app = FastAPI(title="Alpha Fitness WhatsApp Sales Agent", lifespan=lifespan)
app.include_router(twilio_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/debug/simulate")
def simulate(payload: dict, request: Request):
    """Local testing endpoint — bypasses Twilio and WhatsApp entirely.

    POST {"phone": "whatsapp:+254700000000", "message": "hi, how much is a treadmill"}
    """
    agent: SalesAgent = request.app.state.agent
    reply = agent.handle_message(payload["phone"], payload["message"], payload.get("name"))
    return {"reply": reply}


@app.get("/debug/leads")
def leads():
    """Quick read-only view of every lead captured so far."""
    return db.all_leads(settings.database_path)
