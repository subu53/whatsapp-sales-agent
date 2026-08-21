#!/usr/bin/env bash
# Starts the FastAPI app + a free Cloudflare "quick tunnel" (no account
# needed), then prints the exact webhook URL to paste into the Twilio
# console. Good for demos/testing; for anything longer-lived, deploy
# properly instead (see README.md "Deploying for real").
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
LOG_DIR="$(pwd)/.run"
mkdir -p "$LOG_DIR"

echo "Starting FastAPI app on port $PORT..."
nohup uvicorn app.main:app --host 0.0.0.0 --port "$PORT" > "$LOG_DIR/uvicorn.log" 2>&1 &
echo $! > "$LOG_DIR/uvicorn.pid"
sleep 3

echo "Starting Cloudflare quick tunnel..."
nohup ./bin/cloudflared tunnel --url "http://localhost:$PORT" > "$LOG_DIR/cloudflared.log" 2>&1 &
echo $! > "$LOG_DIR/cloudflared.pid"

echo "Waiting for the public tunnel URL..."
URL=""
for i in $(seq 1 20); do
  URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" | head -n1 || true)
  if [ -n "$URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$URL" ]; then
  echo "Could not detect the tunnel URL yet -- check $LOG_DIR/cloudflared.log"
  exit 1
fi

echo ""
echo "=========================================================="
echo "Public URL:         $URL"
echo "Webhook URL to use: $URL/webhook/whatsapp"
echo "=========================================================="
echo ""
echo "Paste the webhook URL into:"
echo "  Twilio Console > Messaging > Try it out > Send a WhatsApp message"
echo "  > Sandbox settings > 'When a message comes in'"
echo ""
echo "Logs: $LOG_DIR/uvicorn.log and $LOG_DIR/cloudflared.log"
echo "Stop both with: kill \$(cat $LOG_DIR/uvicorn.pid) \$(cat $LOG_DIR/cloudflared.pid)"
