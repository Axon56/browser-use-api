#!/usr/bin/env bash
# Start the browser-use HTTP API in the background (pidfile-managed).
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=".bu-api.pid"
LOGFILE="${BU_API_LOG:-/tmp/bu-api.log}"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDFILE"))"
  exit 0
fi

# Chrome must be reachable before the daemon can attach.
export BU_CDP_URL="${BU_CDP_URL:-http://127.0.0.1:9222}"
export BU_API_HOST="${BU_API_HOST:-127.0.0.1}"
export BU_API_PORT="${BU_API_PORT:-8000}"

setsid nohup .venv/bin/python -m bu_api.server > "$LOGFILE" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 2

for _ in $(seq 1 20); do
  if curl -sf -m 2 "http://${BU_API_HOST}:${BU_API_PORT}/health" > /dev/null; then
    echo "listening on http://${BU_API_HOST}:${BU_API_PORT} (pid $(cat "$PIDFILE"))"
    exit 0
  fi
  sleep 0.5
done

echo "failed to start; see $LOGFILE" >&2
tail -20 "$LOGFILE" >&2 || true
exit 1
