#!/bin/bash
# ============================================================
#  BTC Put Monitor Bot — 自愈启动脚本
#
#  功能:
#    1. 自动检测并安装 Python3 + pip3
#    2. 自动检测并安装所有 pip 依赖
#    3. 停止旧进程, 启动新进程
#    4. 验证启动成功
#    5. 启动 watchdog (后台守护, 每60秒检查, 挂了自动拉起)
#
#  用法:
#    ./start_bot.sh           # 正常启动 (含 watchdog)
#    ./start_bot.sh --no-watch  # 启动但不开 watchdog
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"
WATCHDOG_PID_FILE="$SCRIPT_DIR/watchdog.pid"
DEPS="requests python-dotenv matplotlib numpy"

echo "============================================================"
echo "  BTC Put Monitor Bot — 自愈启动"
echo "============================================================"

# ---- 1. 检测 Python3 ----
if ! command -v python3 &>/dev/null; then
    echo "[修复] Python3 未安装, 正在安装..."
    apt-get update -qq && apt-get install -y -qq python3 python3-pip 2>&1 | tail -3
    if ! command -v python3 &>/dev/null; then
        echo "[错误] Python3 安装失败, 退出"
        exit 1
    fi
    echo "[修复] Python3 安装完成"
else
    echo "[检查] Python3: $(python3 --version 2>&1) ✅"
fi

# ---- 2. 检测 pip3 ----
if ! command -v pip3 &>/dev/null; then
    echo "[修复] pip3 未安装, 正在安装..."
    apt-get install -y -qq python3-pip 2>&1 | tail -3
    echo "[修复] pip3 安装完成"
else
    echo "[检查] pip3: 已安装 ✅"
fi

# ---- 3. 检测 pip 依赖 ----
MISSING=""
for pkg in $DEPS; do
    # pip 包名和 import 名不一定相同
    import_name=$pkg
    case $pkg in
        python-dotenv) import_name="dotenv" ;;
    esac
    if ! python3 -c "import $import_name" 2>/dev/null; then
        MISSING="$MISSING $pkg"
    fi
done

if [ -n "$MISSING" ]; then
    echo "[修复] 缺失依赖:$MISSING, 正在安装..."
    pip3 install --break-system-packages $MISSING 2>&1 | tail -3
    echo "[修复] 依赖安装完成"
else
    echo "[检查] pip 依赖: 全部就绪 ✅"
fi

# ---- 4. 验证 .env ----
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[错误] .env 文件不存在! 请先配置 API Key 和 TG Token"
    exit 1
fi
echo "[检查] .env: 存在 ✅"

# ---- 5. 停止旧进程 ----
# Bot 进程
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[停止] 旧 Bot 进程 (PID: $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null
        sleep 2
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

# Watchdog 进程 (--no-watch 模式下不杀 watchdog, 因为是 watchdog 在调我们)
if [ "$1" != "--no-watch" ]; then
    if [ -f "$WATCHDOG_PID_FILE" ]; then
        OLD_WD=$(cat "$WATCHDOG_PID_FILE")
        if kill -0 "$OLD_WD" 2>/dev/null; then
            echo "[停止] 旧 Watchdog (PID: $OLD_WD)..."
            kill "$OLD_WD" 2>/dev/null
            sleep 1
            kill -9 "$OLD_WD" 2>/dev/null || true
        fi
        rm -f "$WATCHDOG_PID_FILE"
    fi
    pkill -f "bot_watchdog.sh" 2>/dev/null || true
fi

# 清理残留 Bot 进程
pkill -f "tg_bot_monitor.py" 2>/dev/null || true
sleep 1

# ---- 6. 启动 Bot ----
echo "[启动] BTC Put Monitor Bot..."
nohup python3 "$SCRIPT_DIR/tg_bot_monitor.py" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# 等待并验证
sleep 5
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[启动] Bot 已启动 (PID: $NEW_PID) ✅"
else
    echo "[错误] Bot 启动失败! 查看日志:"
    tail -20 "$LOG_FILE"
    exit 1
fi

# ---- 7. 启动 Watchdog ----
if [ "$1" != "--no-watch" ]; then
    echo "[启动] Watchdog 守护进程..."
    nohup "$SCRIPT_DIR/bot_watchdog.sh" >> "$SCRIPT_DIR/watchdog.log" 2>&1 &
    WD_PID=$!
    echo "$WD_PID" > "$WATCHDOG_PID_FILE"
    echo "[启动] Watchdog 已启动 (PID: $WD_PID) ✅"
fi

echo "============================================================"
echo "  Bot: PID $NEW_PID"
echo "  日志: tail -f $LOG_FILE"
echo "  Watchdog 日志: tail -f $SCRIPT_DIR/watchdog.log"
echo "============================================================"
