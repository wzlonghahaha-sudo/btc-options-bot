#!/bin/bash
# 全球风投日报 - 运行脚本
# 由 cron 调用，执行完整的 采集->分析->推送 流程

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/data/cron_run.log"

echo "========================================" >> "$LOG_FILE"
echo "$(date) - 风投日报 Pipeline 启动" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

cd "$SCRIPT_DIR"
/usr/bin/python3 main.py >> "$LOG_FILE" 2>&1

echo "$(date) - Pipeline 结束" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
