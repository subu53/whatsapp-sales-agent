# Alpha Fitness — WhatsApp AI Sales Agent

An AI sales agent ("Maya") that talks to Alpha Fitness leads on WhatsApp, answers from the real product catalogue and site info, and works the conversation toward a sale using honest, non-pushy sales psychology — never fake urgency, fake reviews, or pressure tactics.

**Status: built and verified, except the two things only you can provide** — an Anthropic API key and Twilio credentials. Everything else (routing, catalogue search, site-info search, lead tracking, error handling, the sales-psychology prompt) has been run and tested. See "What's already verified" below.

---

## 1. What's already verified (no keys needed for this part)

Run from this project's root folder:

```bash
pip install -r requirements.txt
pytest tests/ -v
```

All 35 automated tests pass: catalogue search returns the right products for real queries ("treadmill for a hotel gym", "cheap dumbbells for home"), the domain-restricted live-fetch tool refuses any non-Alpha-Fitness URL, lead logging writes to the database correctly, human-escalation both marks the lead and never crashes the conversation if the notification itself fails, the storage layer's WAL mode/opt-out/idempotency/rate-limit/retention functions all behave correctly in isolation, the Anthropic&harr;OpenAI translation used by the DeepSeek option is correct against hand-built fake responses, and the webhook itself correctly short-circuits on a STOP/START keyword, a duplicate Twilio retry, and a rate-limited contact — all *before* any of those ever reach the LLM.

The FastAPI app also starts cleanly, loads all 18 real products + 8 site-info topics, and every endpoint responds correctly — including degrading gracefully (a calm customer-facing message, not a crash) when something goes wrong internally.

## 2. See it actually think — 2 minutes, only needs an Anthropic key

This proves the hard part (catalogue RAG + site search + the sales-psychology prompt + tool use) works, with zero WhatsApp/Twilio setup.

1. Get a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) (sign in -> Create Key). New accounts get a small starter credit; this demo uses a trivial amount.
2. Copy `.env.example` to `.env` and paste your key into `ANTHROPIC_API_KEY=`.
3. Terminal 1: `uvicorn app.main:app --reload`
4. Terminal 2: `python scripts/run_local_sim.py`
5. Try real messages, e.g.:
   - `hi, I want to set up a small home gym, what do you recommend under 20k?`
   - `do you supply hotels? we need equipment for our gym`
   - `that bench is expensive, why should I buy it`
   - `are you a real person?`

Watch how it asks about use case before pitching, cites real prices, and handles the last question honestly (it's built to never claim to be human).

If you'd rather I ran this for you: paste your Anthropic API key here in chat and I'll run the live conversation myself and show you the transcript.

## 3. Choosing an LLM provider — Anthropic (default) or DeepSeek

The agent talks to whichever provider `LLM_PROVIDER` in `.env` names — this is a one-line config change, not a rewrite, because `app/agent/llm_client.py` normalizes both providers to the same response shape before `brain.py` ever sees it.

- **`LLM_PROVIDER=anthropic`** (the default) — uses Claude Sonnet 5 via the real Anthropic Messages API, with prompt caching switched on for the system prompt (see the cost-modeling section of `FRAMEWORK.md` for real numbers).
- **`LLM_PROVIDER=deepseek`** — uses DeepSeek's OpenAI-compatible chat-completions API instead. Set `DEEPSEEK_API_KEY` in `.env` (never paste a real key into a chat message — treat it the same as a password; see the note below).

**Honesty check on the DeepSeek path specifically:** it's written correctly against DeepSeek's documented API and covered by unit tests that check the message/tool-schema translation logic against hand-built fake responses (`tests/test_llm_client.py`) — but it has **not been exercised against a live DeepSeek response**. The sandbox this project was built in can reach `api.anthropic.com` but not `api.deepseek.com` at all (confirmed with a direct `curl` — connection-level failure, same signature as the Twilio/tunnel restriction in section 4 below). That's an environment restriction on my side, not a problem with your key or the code. The first time you run this outside the sandbox with `LLM_PROVIDER=deepseek` and a real key, treat it as a smoke test — try a couple of real messages and confirm a tool call round-trips correctly (e.g. "how much is a treadmill?" should trigger `search_catalogue` and come back with a real price) before trusting it with real customers.

**On the key you shared in chat:** a plaintext API key pasted into a conversation should be treated as compromised — rotate/regenerate it from DeepSeek's dashboard once you're done testing, and going forward, put keys only in `.env` (never in chat, never in a commit). This isn't a judgment on what happened here, just the standard, low-effort hygiene move any time a secret has been in plaintext anywhere outside a `.env` file.

## 4. Going live on WhatsApp — why this needs one extra step

I built and tested this entire project inside a sandboxed cloud workspace. That sandbox's network is locked down to a specific allowlist (package registries, the Anthropic API, a few others) — I confirmed it can reach `api.anthropic.com` fine, but **cannot reach `api.twilio.com` or any tunnel service** (Cloudflare Tunnel, ngrok) at all. That's a deliberate security boundary on my environment, not a flaw in the code — the exact same code runs the WhatsApp side perfectly once it's running somewhere with normal internet access, which is any real computer or cloud host.

So getting this onto real WhatsApp needs it to run *outside* my sandbox, in a place you control. Two options — pick whichever fits:

### Option A — Fastest: run it on your own computer + a free tunnel (~15 min)

Good for testing today. The connection is temporary (closes when you stop the terminal / after ~a few hours), so treat this as a live test, not the permanent setup.

1. **Install Python** (skip if you already have it): open PowerShell and run
   `winget install Python.Python.3.12`
   Close and reopen PowerShell afterward so the `python` command is recognized.
2. **Get the code onto your machine** — I'll send you `whatsapp-sales-agent.zip`; extract it anywhere, e.g. `C:\Users\<you>\whatsapp-sales-agent`.
3. Open PowerShell in that folder (`cd C:\Users\<you>\whatsapp-sales-agent`) and run:
   ```
   pip install -r requirements.txt
   copy .env.example .env
   ```
4. Open `.env` in Notepad and fill in:
   - `ANTHROPIC_API_KEY` — from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
   - `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` — from the dashboard at [console.twilio.com](https://console.twilio.com) (free account; no card required for the WhatsApp Sandbox)
5. Start the app: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
6. In a **second** PowerShell window, get a public URL for it. Easiest with ngrok:
   - `winget install ngrok.ngrok`
   - Sign up free at [dashboard.ngrok.com/signup](https://dashboard.ngrok.com/signup), then run the "connect your account" command it gives you (`ngrok config add-authtoken <token>`)
   - `ngrok http 8000`
   - Copy the `https://....ngrok-free.app` URL it prints.
7. **Join the Twilio Sandbox** (see section 5 below) and set the webhook to `https://....ngrok-free.app/webhook/whatsapp`.
8. Message the Twilio sandbox number on WhatsApp. You're live.

### Option B — Recommended: deploy it properly (~20 min, permanent URL, no local setup at all)

This is the version worth doing once, since it matches the "Deploy" step in your own AI-engineer roadmap and gives a link that keeps working after today.

1. Create a free GitHub account if you don't have one: [github.com/signup](https://github.com/signup)
2. Create a new **empty** repository, e.g. named `alpha-fitness-agent` (github.com -> the "+" icon top right -> New repository -> Create repository — don't check any "initialize with README" box).
3. On the new repo's page, click **"uploading an existing file"**, then drag in every file and folder from the `whatsapp-sales-agent` folder I sent you, and commit. (No git install needed — this is the browser upload flow.)
4. Sign up free at [render.com](https://render.com) using "Sign up with GitHub" (no card required for a free Web Service).
5. Dashboard -> **New +** -> **Web Service** -> connect the `alpha-fitness-agent` repo you just created. Render will detect the `Dockerfile` automatically — leave the build settings as-is.
6. Under **Environment**, add three variables — these are the only ones with no built-in default, so they're the only ones that *must* be set for the app to work:
   `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (real values this time, from the same two dashboards linked in step 4 above). Everything else in `.env.example` (`TWILIO_WHATSAPP_NUMBER`, `BUSINESS_NAME`, `BUSINESS_WHATSAPP`, `BUSINESS_WEBSITE`, etc.) already defaults to the correct Alpha Fitness value inside `app/config.py` — only add one of those on Render if you specifically want to override it (e.g. once you move off the shared sandbox number).
7. Click **Create Web Service**. First build takes ~5 minutes; you'll get a permanent URL like `https://alpha-fitness-agent.onrender.com`.
8. **Join the Twilio Sandbox** (below) and set the webhook to `https://alpha-fitness-agent.onrender.com/webhook/whatsapp`.
9. Message the sandbox number on WhatsApp. You're live — and this URL keeps working tomorrow too.

*Note: Render's free tier sleeps after 15 minutes of no traffic, so the very first message after a quiet spell can take ~30-50 seconds to get a reply while it wakes up. Fine for a demo; worth a paid tier ($7/mo) before real customers depend on it.*

## 5. Joining the Twilio WhatsApp Sandbox (needed for both options above)

1. Sign in at [console.twilio.com](https://console.twilio.com). Free account, no card needed for the sandbox.
2. Go to **Messaging -> Try it out -> Send a WhatsApp message**. This shows your unique sandbox join code and the sandbox number, **+1 415 523 8886**.
3. From your own WhatsApp, send the message `join <your-code>` (exactly as shown) to **+1 415 523 8886**. You'll get a confirmation reply. (Anyone else who wants to test the bot — e.g. Alpha Fitness staff — has to send that same join message too. It's a Twilio sandbox rule, not something this code controls.)
4. On the same page, open **Sandbox settings**, and in the field **"When a message comes in"**, paste your webhook URL ending in `/webhook/whatsapp` (from Option A or B above), method `POST`. Save.
5. Message the sandbox number on WhatsApp with something like *"hi, do you have dumbbells?"* — Maya should reply within a few seconds.

**Sandbox sessions expire after 3 days of inactivity** — anyone testing it will need to re-send the join message if it's been quiet a few days. That's a Twilio limitation; moving to a real WhatsApp Business number later removes it (see FRAMEWORK.md's "what changes for real production").

## 6. Environment variables reference

See `.env.example` for the full list with inline explanations. The two that must be real for anything to work with the default provider: `ANTHROPIC_API_KEY`, and the `TWILIO_*` pair for the WhatsApp channel specifically. If you switch `LLM_PROVIDER=deepseek` (section 3), `DEEPSEEK_API_KEY` replaces the Anthropic key as the one that must be real. `RATE_LIMIT_MAX_MESSAGES` / `RATE_LIMIT_WINDOW_SECONDS` (defaults: 20 messages per rolling hour, per phone number) control the per-contact rate limit described in section 7 below — raise the limit if a legitimate commercial buyer is going back and forth quickly and hitting it.

## 7. Architecture, at a glance

```
WhatsApp user
   |  (Twilio relays the message)
   v
POST /webhook/whatsapp  (app/channels/twilio_whatsapp.py)
   |  verifies the request really came from Twilio
   |  checks: already processed this exact message? (idempotency)
   |  checks: STOP/START keyword, or already opted out? (compliance)
   |  checks: over the rate limit for this number? (abuse/cost control)
   v
SalesAgent.handle_message()  (app/agent/brain.py)
   |  loads this contact's conversation history from SQLite
   |  calls the configured LLM (Anthropic or DeepSeek, section 3) with
   |  the system prompt + tool definitions
   |  the model decides: answer directly, or call a tool first
   v
Tools  (app/agent/tools.py)
   - search_catalogue / get_product_by_id -> app/rag/catalogue_search.py (BM25 over data/catalogue/products.json)
   - search_site_info -> app/rag/site_search.py (BM25 over data/site_info.json)
   - fetch_live_page -> live fetch, alphafitness.co.ke only
   - log_lead_info / escalate_to_human -> app/storage/db.py (SQLite, WAL mode)
   v
Model reasons over the tool result(s), replies in plain text
   v
Reply sent back to WhatsApp as TwiML
```

The three checks before the agent ever runs (idempotency, opt-out, rate limit) are deliberately ordered that way — each one is cheaper and more important than the one after it, and none of them cost an LLM call. Full rationale for these choices, plus everything else (why BM25 over embeddings, why SQLite, why this tool-use loop shape, real cost-per-conversation numbers, and what's honestly still missing) is in **FRAMEWORK.md** — it's worth reading in full, not just skimming the architecture diagram above.

## 8. Updating the catalogue with your real, complete data

Right now `data/catalogue/products.json` has 18 real products pulled live from alphafitness.co.ke (verified prices and specs where shown, honestly marked `"detail_level": "summary"` wherever I only had the name/price/category and didn't want to guess at specs). To load your *complete* WooCommerce catalogue instead:

1. WordPress admin -> **Products -> All Products -> Export** -> export all columns as CSV.
2. Send me that CSV and I'll regenerate `products.json` from it with full data (all products, real stock status, full specs) in one pass.
3. Redeploy (Render redeploys automatically on a new commit to the repo; locally, just restart uvicorn).

## 9. Data retention — deleting old conversations

`scripts/purge_old_data.py` permanently deletes conversations (and their message history) that have been inactive past a day threshold you choose:

```bash
python scripts/purge_old_data.py --days 180 --dry-run   # see what WOULD be deleted, deletes nothing
python scripts/purge_old_data.py --days 180             # deletes, after an interactive "type yes" confirmation
python scripts/purge_old_data.py --days 180 --yes       # skip the confirmation (for a cron job / scheduled task)
```

This script is a *mechanism*, not a *policy* — it deletes whatever number of days you give it and makes no judgment about what that number should be. Deciding the actual retention period (and whether "inactive N days" is even the right rule, versus honoring an individual deletion request faster) is a real decision for Alpha Fitness to make, with reference to Kenya's Data Protection Act 2019 (the applicable law for a Kenyan business handling Kenyan customers' data — GDPR would additionally apply for any EU/UK customers). Nothing here runs automatically; if you want it on a schedule, that's a cron job (locally) or a scheduled job on whatever host you land on (Render's cron jobs, for instance) calling the `--yes` form above.

## 10. Troubleshooting

- **Twilio signature validation fails behind a tunnel/proxy**: the webhook code reconstructs the URL from `X-Forwarded-Proto`/`X-Forwarded-Host` headers specifically because tunnels and Render both terminate HTTPS before your app sees the request — if you hit a 403 here, confirm your proxy is actually forwarding those two headers (both ngrok and Render do this by default).
- **"Internal Server Error" from `/debug/simulate` or a generic apology on WhatsApp**: check the server logs — every failure is logged with a full traceback before the customer-facing fallback message is sent, and the conversation is auto-escalated to a human in the database (`GET /debug/leads`) so nothing silently drops.
- **A message doesn't get a reply, or the same reply comes through only once even though you sent it twice**: expected if you (or Twilio) sent the exact same message twice — the idempotency check in section 7 treats a repeat `MessageSid` as already handled and stays silent on the retry, on purpose.
- **"Should I still see replies after I texted STOP?"**: no — that's the opt-out working as intended. Text `START` to resume (section 7).
- **DeepSeek returns an error or times out**: this hasn't been live-tested from my build environment (section 3) — first confirm the basics outside this project (a bare `curl` to `https://api.deepseek.com/chat/completions` with your key succeeds) before assuming it's this code.
- **Render free tier feels slow on the first message**: expected — see the note at the end of Option B.
