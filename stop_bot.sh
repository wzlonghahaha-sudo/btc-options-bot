#!/bin/bash
# 停止 BTC Put Monitor Bot
# 用法: ./stop_bot.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"

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
        echo "Bot 已停止"
    else
        echo "进程 $PID 不存在"
    fi
    rm -f "$PID_FILE"
else
    echo "未找到 PID 文件, Bot 可能未运行"
    # 尝试找到并停止
    PIDS=$(pgrep -f "tg_bot_monitor.py")
    if [ -n "$PIDS" ]; then
        echo "找到运行中的进程: $PIDS"
        kill $PIDS
        echo "已停止"
    fi
fi
