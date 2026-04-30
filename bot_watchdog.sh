#!/bin/bash
# ============================================================
#  Bot Watchdog — 每60秒检查, 挂了自动拉起
#
#  检查项:
#    1. Bot 进程是否存在
#    2. Bot 进程是否僵死 (日志超过10分钟没更新)
#    3. Python/pip 依赖是否完整
#    4. 如果异常 → 自动调用 start_bot.sh --no-watch 修复
#
#  自我保护:
#    - 连续失败超过5次 → 发TG告警并暂停30分钟
#    - 避免无限重启循环
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"
CHECK_INTERVAL=60      # 每60秒检查一次
MAX_LOG_AGE=600        # 日志超过600秒(10分钟)没更新认为僵死
MAX_FAIL=5             # 连续失败次数上限
COOLDOWN=1800          # 连续失败后冷却30分钟

fail_count=0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $1"
}

send_tg_alert() {
    # 从 .env 读取 TG 配置
    if [ -f "$SCRIPT_DIR/.env" ]; then
        TG_TOKEN=$(grep TG_BOT_TOKEN "$SCRIPT_DIR/.env" | cut -d= -f2)
        TG_CHAT=$(grep TG_CHAT_ID "$SCRIPT_DIR/.env" | cut -d= -f2)
        if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
            curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
                -H "Content-Type: application/json" \
                -d "{\"chat_id\":\"${TG_CHAT}\",\"text\":\"$1\",\"parse_mode\":\"HTML\"}" \
                >/dev/null 2>&1
        fi
    fi
}

log "Watchdog 启动, 检查间隔 ${CHECK_INTERVAL}s"

while true; do
    sleep $CHECK_INTERVAL

    needs_restart=false
    reason=""

    # 检查1: 进程是否存在
    if [ -f "$PID_FILE" ]; then
        BOT_PID=$(cat "$PID_FILE")
        if ! kill -0 "$BOT_PID" 2>/dev/null; then
            needs_restart=true
            reason="进程 $BOT_PID 不存在"
        fi
    else
        needs_restart=true
        reason="PID 文件不存在"
    fi

    # 检查2: 日志是否还在更新 (防僵死)
    if [ "$needs_restart" = false ] && [ -f "$LOG_FILE" ]; then
        last_mod=$(stat -c %Y "$LOG_FILE" 2>/dev/null || echo 0)
        now=$(date +%s)
        age=$((now - last_mod))
        if [ "$age" -gt "$MAX_LOG_AGE" ]; then
            needs_restart=true
            reason="日志 ${age}s 未更新, 进程可能僵死"
        fi
    fi

    # 检查3: Python 依赖
    if [ "$needs_restart" = false ]; then
        if ! python3 -c "import requests, dotenv, matplotlib" 2>/dev/null; then
            needs_restart=true
            reason="Python 依赖缺失"
        fi
    fi

    if [ "$needs_restart" = true ]; then
        fail_count=$((fail_count + 1))
        log "异常检测: $reason (连续第 ${fail_count} 次)"

        if [ "$fail_count" -ge "$MAX_FAIL" ]; then
            log "连续失败 ${fail_count} 次, 发送告警并冷却 ${COOLDOWN}s"
            send_tg_alert "🚨 <b>[WATCHDOG] Bot 连续 ${fail_count} 次重启失败!</b>%0A%0A原因: ${reason}%0A冷却 ${COOLDOWN}s 后重试%0A%0A请手动检查!"
            sleep $COOLDOWN
            fail_count=0
            continue
        fi

        log "正在自动修复..."
        send_tg_alert "🔧 <b>[WATCHDOG] Bot 异常, 自动修复中</b>%0A原因: ${reason}"

        # 调用自愈启动脚本 (不启动新 watchdog, 避免重复)
        "$SCRIPT_DIR/start_bot.sh" --no-watch 2>&1 | while read line; do log "$line"; done

        # 验证修复结果
        sleep 8
        if [ -f "$PID_FILE" ]; then
            NEW_PID=$(cat "$PID_FILE")
            if kill -0 "$NEW_PID" 2>/dev/null; then
                log "修复成功! 新 PID: $NEW_PID"
                send_tg_alert "✅ <b>[WATCHDOG] Bot 已自动恢复</b>%0APID: ${NEW_PID}"
                fail_count=0
            else
                log "修复失败, 下次循环重试"
            fi
        else
            log "修复失败 (无 PID 文件), 下次循环重试"
        fi
    else
        # 一切正常, 重置失败计数
        if [ "$fail_count" -gt 0 ]; then
            fail_count=0
            log "恢复正常"
        fi
    fi
done
