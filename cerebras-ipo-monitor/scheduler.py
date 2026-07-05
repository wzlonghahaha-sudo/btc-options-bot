#!/usr/bin/env python3
"""
Standalone scheduler for Cerebras IPO Monitor.
Use this when cron is not available or unreliable.

Run in background:
    nohup python3 scheduler.py &

Or in tmux/screen session:
    python3 scheduler.py
"""

import subprocess
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MONITOR_SCRIPT = SCRIPT_DIR / "monitor.py"
LOG_FILE = SCRIPT_DIR / "scheduler.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHED] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scheduler")

# Schedule: UTC hours to run the monitor
# UTC 00:00 = 北京 08:00 = 美东 20:00 (盘后)
# UTC 13:00 = 北京 21:00 = 美东 09:00 (盘前)
# UTC 14:30 = 北京 22:30 = 美东 10:30 (开盘后, IPO周加密)
# UTC 20:30 = 北京 04:30 = 美东 16:30 (收盘后, IPO周加密)
REGULAR_HOURS = {0, 13}       # daily
IPO_WEEK_EXTRA = {14, 20}     # May 12-19 only, half-hour triggers

CHECK_INTERVAL = 60  # check every 60 seconds


def should_run(now_utc: datetime, last_run_key: str, ran_keys: set) -> bool:
    """Determine if we should run at this moment."""
    hour = now_utc.hour
    minute = now_utc.minute
    day = now_utc.day
    month = now_utc.month

    key = None

    # Regular daily checks (on the hour)
    if hour in REGULAR_HOURS and minute < 2:
        key = f"{now_utc.date()}-{hour}:00"

    # IPO week extra checks (at :30)
    if month == 5 and 12 <= day <= 19:
        if hour in IPO_WEEK_EXTRA and 30 <= minute < 32:
            key = f"{now_utc.date()}-{hour}:30"

    if key and key not in ran_keys:
        ran_keys.add(key)
        return True
    return False


def run_monitor():
    """Execute the monitor script."""
    log.info("Running monitor.py...")
    try:
        result = subprocess.run(
            [sys.executable, str(MONITOR_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            log.info("Monitor completed successfully")
        else:
            log.error(f"Monitor failed (rc={result.returncode}): {result.stderr}")
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                log.info(f"  | {line}")
    except subprocess.TimeoutExpired:
        log.error("Monitor timed out after 120s")
    except Exception as e:
        log.error(f"Failed to run monitor: {e}")


def main():
    log.info("=" * 50)
    log.info("Cerebras IPO Scheduler started")
    log.info(f"Monitor script: {MONITOR_SCRIPT}")
    log.info("Schedule (UTC): daily at 00:00, 13:00")
    log.info("IPO week (May 12-19): also at 14:30, 20:30")
    log.info("=" * 50)

    ran_keys: set = set()

    # Run once immediately on startup
    log.info("Initial run on startup...")
    run_monitor()

    while True:
        try:
            now = datetime.now(timezone.utc)
            if should_run(now, "", ran_keys):
                run_monitor()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log.info("Scheduler stopped by user")
            break
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
