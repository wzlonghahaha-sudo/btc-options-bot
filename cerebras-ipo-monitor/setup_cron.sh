#!/bin/bash
# Setup crontab for Cerebras IPO monitor
# Schedule: Twice daily
#   UTC 00:00 = 北京时间 08:00 = 美东 20:00 (盘后/定价公告时间)
#   UTC 13:00 = 北京时间 21:00 = 美东 09:00 (盘前/开盘前)
#
# Additionally, during likely IPO week (May 12-19), add extra checks
# at UTC 14:30 = 美东 10:30 (开盘后1小时，捕捉首日交易)

SCRIPT="/root/projects/cerebras-ipo-monitor/monitor.py"

# Remove any existing cerebras monitor entries
crontab -l 2>/dev/null | grep -v "cerebras-ipo-monitor" > /tmp/crontab_clean

# Add new entries
cat >> /tmp/crontab_clean << 'EOF'
# Cerebras IPO Monitor — 每天两次常规检查
0 0 * * * /usr/bin/python3 /root/projects/cerebras-ipo-monitor/monitor.py >> /root/projects/cerebras-ipo-monitor/cron.log 2>&1
0 13 * * * /usr/bin/python3 /root/projects/cerebras-ipo-monitor/monitor.py >> /root/projects/cerebras-ipo-monitor/cron.log 2>&1
# Cerebras IPO Monitor — IPO周额外检查 (美东10:30, 覆盖开盘交易)
30 14 12-19 5 * /usr/bin/python3 /root/projects/cerebras-ipo-monitor/monitor.py >> /root/projects/cerebras-ipo-monitor/cron.log 2>&1
# Cerebras IPO Monitor — IPO周额外检查 (美东16:30, 覆盖收盘后定价)
30 20 12-19 5 * /usr/bin/python3 /root/projects/cerebras-ipo-monitor/monitor.py >> /root/projects/cerebras-ipo-monitor/cron.log 2>&1
EOF

crontab /tmp/crontab_clean
rm /tmp/crontab_clean

echo "Crontab updated. Current schedule:"
crontab -l | grep cerebras
