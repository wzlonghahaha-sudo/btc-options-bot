#!/bin/bash
# 启动/重启 BTC Put Monitor Bot
# 用法: ./start_bot.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"

# 停止旧进程
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "停止旧进程 (PID: $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
    rm -f "$PID_FILE"
fi

# 启动
echo "启动 BTC Put Monitor Bot..."
nohup python3 "$SCRIPT_DIR/tg_bot_monitor.py" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "Bot 已启动 (PID: $NEW_PID)"
echo "日志: tail -f $LOG_FILE"
