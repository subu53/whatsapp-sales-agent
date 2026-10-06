# WhatsApp sales agent for Alpha Fitness

An LLM sales agent that talks to inbound leads on WhatsApp for [Alpha Fitness](https://alphafitness.co.ke), a fitness-equipment retailer in Nairobi. It answers from the real product catalogue and site content, asks about use case and budget before recommending anything, logs the lead, and hands the chat to a salesperson when it should.

It runs on the Meta WhatsApp Cloud API, deployed on Azure App Service. The original Twilio channel is still in the codebase and shares the same agent code.

## How a message moves through the system

```
WhatsApp customer
   │
   ▼
POST /whatsapp/webhook            app/channels/meta_whatsapp.py
   │  verify X-Hub-Signature-256
   │  duplicate delivery?   → drop          ┐
   │  STOP / START?         → handle        │  no LLM call yet
   │  over rate limit?      → short notice  ┘
   ▼
SalesAgent.handle_message         app/agent/brain.py
   │  load last 20 messages (SQLite)
   │  tool loop, max 6 rounds:
   │    search_catalogue · get_product_by_id · search_site_info
   │    fetch_live_page (alphafitness.co.ke only)
   │    log_lead_info · escalate_to_human
   ▼
price guard                       app/agent/price_guard.py
   │  every KES amount must come from a tool result this turn
   ▼
reply via Graph API (appsecret_proof on every call)
```

## Guardrails that live in code

- **Price guard.** Every shilling amount in a reply has to match a number a tool returned in that turn, a whole multiple of one (quantity quotes, up to 50x), or a figure the customer typed. Anything else is blocked: the customer gets "the team will confirm the exact price" and the chat is escalated. The system prompt also forbids invented prices; the guard catches the times the model ignores it.
- **Cheap checks first.** Duplicate deliveries, opt-outs and the per-number rate limit (20 messages an hour by default) are all handled before the model is called, so retries and abuse cost nothing.
- **Bounded tool loop.** Six rounds at most. A failing tool returns an error object the model can recover from instead of crashing the turn.
- **No stack traces for customers.** Any unhandled error sends a short holding message and escalates to a person; the full traceback goes to the logs.
- **Domain-locked fetch.** `fetch_live_page` refuses any URL outside `ALLOWED_FETCH_DOMAIN`.
- **Signed webhooks only.** Inbound Meta requests must carry a valid `X-Hub-Signature-256`; anything else gets a 403 before it touches the database or the model.
- **Debug routes off in production.** `/debug/*` returns 404 unless `DEBUG_TOKEN` is set and sent as `X-Debug-Token`. It's set only in local `.env` files.

## Stack

Python 3.11 · FastAPI · Meta WhatsApp Cloud API (Twilio channel kept) · Claude or DeepSeek through one client (`app/agent/llm_client.py`) · BM25 retrieval (`rank_bm25`) · SQLite in WAL mode · Docker · Azure App Service

Switching model provider is one setting: `LLM_PROVIDER=anthropic` or `LLM_PROVIDER=deepseek`. The client converts both to the same response shape, so `brain.py` never knows which one it is talking to.

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # add your keys
pytest tests/ -v

uvicorn app.main:app --reload
export DEBUG_TOKEN=<same value as in .env>
python scripts/run_local_sim.py   # chat with the agent in the terminal, no WhatsApp needed
```

## Configuration

| Variable | What it does |
| --- | --- |
| `LLM_PROVIDER` | `anthropic` or `deepseek` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude settings |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` | DeepSeek settings |
| `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` | Cloud API sender |
| `META_WEBHOOK_VERIFY_TOKEN` | One-time webhook handshake |
| `META_APP_SECRET` | Inbound signature check and `appsecret_proof` |
| `TWILIO_*` | Only if you use the Twilio channel |
| `HUMAN_ESCALATION_WHATSAPP` | Number notified on handoff |
| `RATE_LIMIT_MAX_MESSAGES`, `RATE_LIMIT_WINDOW_SECONDS` | Per-contact limit |
| `ALLOWED_FETCH_DOMAIN` | The only domain the live-fetch tool may call |
| `DEBUG_TOKEN` | Local only. Unlocks `/debug/*` and `scripts/run_local_sim.py` |

Keys go in `.env`, which is gitignored. Never commit them.

## Project layout

```
app/
  channels/   meta_whatsapp.py, twilio_whatsapp.py
  agent/      brain.py, tools.py, llm_client.py, price_guard.py, prompts/
  rag/        catalogue_search.py, site_search.py
  storage/    db.py
scripts/      run_local_sim.py, purge_old_data.py
tests/        catalogue search, storage, LLM client, price guard, tools, webhook
```

## Cost

Estimated at about US$0.03 per resolved conversation on Claude Sonnet with prompt caching (around eight model calls). The working is in `FRAMEWORK.md`, section 11. It's an estimate and still needs checking against live billing.

## Known limits

- Prompt-injection defence is prompt-level. There is no separate filter model in front of the agent.
- SQLite is a single file. Running several app instances means moving to Postgres.
- The catalogue is a JSON export, so it has to be regenerated when products or prices change.
- `scripts/purge_old_data.py` deletes inactive conversations after a number of days you choose. The retention period itself is a decision for the business under Kenya's Data Protection Act 2019.

Design reasoning (why BM25 over embeddings, why SQLite for now, how the layers are split) is in [`FRAMEWORK.md`](FRAMEWORK.md).
