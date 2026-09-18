#!/bin/bash
# Keep proxy_failover.py watch loop alive.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$SCRIPT_DIR/failover_watch.pid"
LOG_FILE="$SCRIPT_DIR/failover_watchdog.log"
PY="$ROOT/.venv/bin/python3"
INTERVAL=30

cd "$ROOT"
# Load .env so BINANCE_PROXY_URL / HTTPS_PROXY available if stored there
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

while true; do
  if [ -f "$PID_FILE" ]; then
    pid=$(tr -cd '0-9' < "$PID_FILE")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # still running
      sleep "$INTERVAL"
      continue
    fi
  fi
  echo "$(date -Iseconds) starting proxy_failover watch" >> "$LOG_FILE"
  nohup "$PY" "$SCRIPT_DIR/proxy_failover.py" watch >> "$SCRIPT_DIR/failover.log" 2>&1 &
  echo $! > "$PID_FILE"
  sleep "$INTERVAL"
done
