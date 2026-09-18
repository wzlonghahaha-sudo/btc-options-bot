#!/bin/bash
# 停止 Bot + Watchdog

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
WATCHDOG_PID_FILE="$SCRIPT_DIR/watchdog.pid"

# 先停 Watchdog (否则它会自动拉起 Bot)
if [ -f "$WATCHDOG_PID_FILE" ]; then
    WD_PID=$(cat "$WATCHDOG_PID_FILE")
    if kill -0 "$WD_PID" 2>/dev/null; then
        echo "停止 Watchdog (PID: $WD_PID)..."
        kill "$WD_PID" 2>/dev/null
        sleep 1
        kill -9 "$WD_PID" 2>/dev/null || true
    fi
    rm -f "$WATCHDOG_PID_FILE"
fi
pkill -f "bot_watchdog.sh" 2>/dev/null || true

# 再停 Bot
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "停止 Bot (PID: $PID)..."
        kill "$PID"
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo "强制停止..."
            kill -9 "$PID"
        fi
    fi
    rm -f "$PID_FILE"
fi
pkill -f "tg_bot_monitor.py" 2>/dev/null || true

echo "Bot + Watchdog 已停止"

# Stop proxy failover watchdog (optional companion)
if [ -f "$SCRIPT_DIR/proxy/proxy_watchdog.pid" ]; then
  _ppid=$(tr -cd "0-9" < "$SCRIPT_DIR/proxy/proxy_watchdog.pid")
  if [ -n "$_ppid" ] && kill -0 "$_ppid" 2>/dev/null; then
    kill "$_ppid" 2>/dev/null || true
  fi
  rm -f "$SCRIPT_DIR/proxy/proxy_watchdog.pid"
fi
if [ -f "$SCRIPT_DIR/proxy/failover_watch.pid" ]; then
  _fpid=$(tr -cd "0-9" < "$SCRIPT_DIR/proxy/failover_watch.pid")
  if [ -n "$_fpid" ] && kill -0 "$_fpid" 2>/dev/null; then
    kill "$_fpid" 2>/dev/null || true
  fi
  rm -f "$SCRIPT_DIR/proxy/failover_watch.pid"
fi

