#!/bin/bash
# Start/restart the Cerebras IPO monitor scheduler
# Usage: bash start.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEDULER="$SCRIPT_DIR/scheduler.py"
LOGFILE="$SCRIPT_DIR/scheduler.log"
PIDFILE="$SCRIPT_DIR/scheduler.pid"

# Kill existing instance
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing scheduler (PID $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
fi

# Also kill any stray scheduler processes
pkill -f "python3.*scheduler.py" 2>/dev/null

# Start cron daemon if available
/usr/sbin/cron 2>/dev/null

# Start scheduler in background
echo "Starting scheduler..."
nohup python3 "$SCHEDULER" >> "$LOGFILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
disown

echo "Scheduler started (PID $NEW_PID)"
echo "Log: $LOGFILE"
echo "To stop: kill $NEW_PID"
