#!/usr/bin/env python3
"""
全球顶级风投日报 - 定时调度器
每天北京时间 20:00 (UTC 12:00) 执行日报推送
也可通过 cron 直接调用 main.py
"""
import time
import logging
import schedule
from datetime import datetime, timezone, timedelta

from main import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("scheduler")

# 北京时间 = UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))


def job():
    """定时任务：执行完整的日报流水线"""
    now_beijing = datetime.now(BEIJING_TZ)
    logger.info(f"定时任务触发 - 北京时间: {now_beijing.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        run_pipeline()
    except Exception as e:
        logger.error(f"Pipeline 执行异常: {e}", exc_info=True)


def main():
    # 每天 UTC 12:00 = 北京时间 20:00
    schedule.every().day.at("12:00").do(job)

    logger.info("=" * 60)
    logger.info("风投日报调度器已启动")
    logger.info(f"推送时间: 每天北京时间 20:00 (UTC 12:00)")
    now_beijing = datetime.now(BEIJING_TZ)
    logger.info(f"当前北京时间: {now_beijing.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    while True:
        schedule.run_pending()
        time.sleep(30)  # 每30秒检查一次


if __name__ == "__main__":
    main()
