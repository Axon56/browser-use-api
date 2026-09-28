#!/usr/bin/env bash
# Stop the background browser-use HTTP API.
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=".bu-api.pid"
if [[ ! -f "$PIDFILE" ]]; then
  echo "not running (no pidfile)"
  exit 0
fi

pid=$(cat "$PIDFILE")
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "stopped pid $pid"
else
  echo "process $pid already gone"
fi
rm -f "$PIDFILE"
