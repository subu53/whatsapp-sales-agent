"""Tool schemas (Anthropic tool-use format) + their Python implementations.

Design note: every tool function takes the same trailing `ctx` argument
(a ToolContext) so the dispatch loop in brain.py can call any tool the
same way: `TOOL_IMPLS[name](**tool_input, ctx=ctx)`.
"""
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.agent.state import STAGES
from app.rag.catalogue_search import CatalogueIndex
from app.rag.site_search import SiteIndex
from app.storage import db


@dataclass
class ToolContext:
    phone_number: str
    db_path: str
    catalogue: CatalogueIndex
    site_index: SiteIndex
    allowed_fetch_domain: str
    twilio_client: object = None
    human_escalation_whatsapp: str = ""
    business_whatsapp_from: str = ""


TOOLS = [
    {
        "name": "search_catalogue",
        "description": (
            "Search the Alpha Fitness product catalogue by free-text query "
            "(product type, use case, budget, brand, etc). Returns matching "
            "products with real prices, specs, and URLs. Always use this "
            "before recommending or pricing any product."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the customer is looking for, in plain words."},
                "max_results": {"type": "integer", "description": "Max products to return.", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product_by_id",
        "description": "Look up one exact product by its catalogue id, once you know which product matters.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "search_site_info",
        "description": (
            "Search Alpha Fitness business info: delivery/shipping policy, "
            "payment methods, commercial/tender process, showroom, contact "
            "details, category structure. Use for anything that isn't a "
            "specific product."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch_live_page",
        "description": (
            "Fetch and read a live page from alphafitness.co.ke when the "
            "pre-indexed catalogue and site info don't cover what's needed "
            "(e.g. exact wording of a policy page). Only works on "
            "alphafitness.co.ke — any other domain is refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_or_path": {
                    "type": "string",
                    "description": "Full URL or path on alphafitness.co.ke, e.g. '/returns-refunds-policy/'.",
                }
            },
            "required": ["url_or_path"],
        },
    },
    {
        "name": "log_lead_info",
        "description": (
            "Record durable facts about this lead as you learn them. Call "
            "quietly whenever you learn the customer's name, location, use "
            "case, budget band, or when their lead stage changes. Never "
            "mention to the customer that you're logging anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "location": {"type": "string"},
                "use_case": {"type": "string", "description": "e.g. home gym, hotel gym, school, PT studio, corporate"},
                "budget_band": {"type": "string", "description": "e.g. 'under 20k', '20k-100k', '100k+'"},
                "lead_stage": {"type": "string", "enum": STAGES},
                "notes": {"type": "string", "description": "Any other short, useful fact."},
            },
            "required": [],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand this conversation to a human team member. Use for explicit "
            "human requests, tender/LPO/large commercial orders, complaints, "
            "returns/warranty claims, or negotiation beyond normal discounting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


def _tool_search_catalogue(ctx: ToolContext, query: str, max_results: int = 5) -> dict:
    results = ctx.catalogue.search(query, k=max_results)
    return {"results": results, "count": len(results)}


def _tool_get_product_by_id(ctx: ToolContext, product_id: str) -> dict:
    product = ctx.catalogue.get_by_id(product_id)
    if not product:
        return {"error": f"No product with id '{product_id}'. Use search_catalogue instead."}
    return {"product": product}


def _tool_search_site_info(ctx: ToolContext, query: str) -> dict:
    results = ctx.site_index.search(query, k=2)
    return {"results": results}


def _tool_fetch_live_page(ctx: ToolContext, url_or_path: str) -> dict:
    if url_or_path.startswith("http"):
        url = url_or_path
    else:
        url = urljoin(f"https://{ctx.allowed_fetch_domain}/", url_or_path.lstrip("/"))

    host = urlparse(url).netloc.replace("www.", "")
    if host != ctx.allowed_fetch_domain:
        return {"error": f"Refused: this tool can only fetch pages on {ctx.allowed_fetch_domain}."}

    try:
        resp = httpx.get(url, timeout=10, headers={"User-Agent": "AlphaFitnessSalesAgent/1.0"}, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"error": f"Could not fetch {url}: {exc}"}

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return {"url": url, "text": text[:4000]}


def _tool_log_lead_info(ctx: ToolContext, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if v}
    if fields:
        db.update_conversation(ctx.db_path, ctx.phone_number, **fields)
    return {"status": "logged", "fields": list(fields.keys())}


def _tool_escalate_to_human(ctx: ToolContext, reason: str) -> dict:
    db.mark_escalated(ctx.db_path, ctx.phone_number, reason)

    notified = False
    if ctx.twilio_client and ctx.human_escalation_whatsapp:
        try:
            ctx.twilio_client.messages.create(
                from_=ctx.business_whatsapp_from,
                to=f"whatsapp:{ctx.human_escalation_whatsapp}",
                body=f"[Alpha Fitness Agent] Escalation from {ctx.phone_number}: {reason}",
            )
            notified = True
        except Exception:
            # Never let a notification failure break the customer-facing flow.
            notified = False

    return {"status": "escalated", "human_notified": notified}


TOOL_IMPLS = {
    "search_catalogue": _tool_search_catalogue,
    "get_product_by_id": _tool_get_product_by_id,
    "search_site_info": _tool_search_site_info,
    "fetch_live_page": _tool_fetch_live_page,
    "log_lead_info": _tool_log_lead_info,
    "escalate_to_human": _tool_escalate_to_human,
}
