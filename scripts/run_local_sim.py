#!/usr/bin/env python3
"""Chat with the agent directly from the terminal — no Twilio, no WhatsApp,
no tunnel needed. This is the fastest way to iterate on the system prompt
and tool behavior.

Start the server first, in one terminal:
    uvicorn app.main:app --reload

Then, in another terminal:
    python scripts/run_local_sim.py
"""
import sys

import httpx

BASE_URL = "http://127.0.0.1:8000"
TEST_PHONE = "whatsapp:+254700000001"


def main():
    print("Alpha Fitness sales agent -- local simulator. Ctrl+C to quit.\n")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        try:
            resp = httpx.post(
                f"{BASE_URL}/debug/simulate",
                json={"phone": TEST_PHONE, "message": text, "name": "Test Customer"},
                timeout=60,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[error] Could not reach the agent server: {exc}", file=sys.stderr)
            print("Is `uvicorn app.main:app --reload` running in another terminal?")
            continue
        print(f"Maya: {resp.json()['reply']}\n")


if __name__ == "__main__":
    main()
