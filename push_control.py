"""
推送噪音治理 (R4-6)

- 推送预算: 非告警推送每日上限 N 条 (MAX_SIGNAL_PUSH_PER_DAY)
- 等级门槛: 机会推送只推 A/B 级 (score >= 70), C 级仅 /top 可见
- 合并窗口: 60 秒内产生的多条同级告警合并为一条消息
- 升级重推: 同一合约级别升级时突破所有冷却立即推
"""

import os
import time
import logging
from collections import defaultdict

log = logging.getLogger(__name__)

MAX_SIGNAL_PUSH_PER_DAY = int(os.getenv("MAX_SIGNAL_PUSH_PER_DAY", "5"))
MERGE_WINDOW_SEC = 60  # 合并窗口 (秒)
MIN_PUSH_SCORE = 70    # 最低推送分数 (A/B 级 = 70+)


class PushController:
    """推送噪音控制器"""

    def __init__(self):
        self.daily_signal_count = 0
        self.daily_reset_date = ""
        self.pending_alerts = []           # 合并窗口内待推送的告警
        self.pending_since = 0             # 窗口开始时间
        self.last_alert_level = {}         # {symbol: last_level} 用于升级重推
        self.suppressed_today = 0          # 今日被压制的信号数

    def _check_daily_reset(self):
        """每日重置计数"""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            if self.daily_signal_count > 0:
                log.info(f"每日推送计数重置 (昨日: {self.daily_signal_count} 条)")
            self.daily_signal_count = 0
            self.suppressed_today = 0
            self.daily_reset_date = today

    def should_push_signal(self, score: float) -> bool:
        """
        判断机会信号是否应该推送

        Returns:
            True = 推送, False = 压制 (进 digest 汇总)
        """
        self._check_daily_reset()

        # 等级门槛: C/D 级不推送
        if score < MIN_PUSH_SCORE:
            return False

        # 每日预算
        if self.daily_signal_count >= MAX_SIGNAL_PUSH_PER_DAY:
            self.suppressed_today += 1
            return False

        return True

    def record_signal_push(self):
        """记录一次信号推送"""
        self._check_daily_reset()
        self.daily_signal_count += 1

    def get_suppressed_summary(self) -> str:
        """获取被压制的信号汇总 (用于 digest)"""
        if self.suppressed_today > 0:
            return f"今日另有 {self.suppressed_today} 个机会被限额, /top 查看"
        return ""

    # ============================================================
    #  告警合并
    # ============================================================
    def add_alert_for_merge(self, alert) -> bool:
        """
        添加告警到合并窗口

        Returns:
            True = 窗口已满/超时, 应该推送; False = 继续等待
        """
        now = time.time()

        if not self.pending_alerts:
            self.pending_since = now

        self.pending_alerts.append(alert)

        # 窗口超时 → 触发推送
        if now - self.pending_since >= MERGE_WINDOW_SEC:
            return True

        return False

    def flush_pending_alerts(self) -> list:
        """取出并清空待推送的告警"""
        alerts = self.pending_alerts
        self.pending_alerts = []
        self.pending_since = 0
        return alerts

    def has_pending_alerts(self) -> bool:
        """是否有超时待推送的告警"""
        if not self.pending_alerts:
            return False
        return time.time() - self.pending_since >= MERGE_WINDOW_SEC

    # ============================================================
    #  升级重推
    # ============================================================
    def should_upgrade_push(self, symbol: str, new_level: str) -> bool:
        """
        同一合约级别升级时突破冷却立即推送

        Returns:
            True = 应该立即推送 (级别升级了)
        """
        level_order = {"WATCH": 0, "WARNING": 1, "DANGER": 2, "CRITICAL": 3}
        old_level = self.last_alert_level.get(symbol)
        new_order = level_order.get(new_level, 0)
        old_order = level_order.get(old_level, -1)

        # 记录新级别
        self.last_alert_level[symbol] = new_level

        if old_level is None:
            return False  # 首次, 不算升级

        return new_order > old_order  # 级别升高 = 升级重推

    def get_status(self) -> dict:
        """获取当前推送控制状态"""
        self._check_daily_reset()
        return {
            "daily_signal_count": self.daily_signal_count,
            "daily_limit": MAX_SIGNAL_PUSH_PER_DAY,
            "suppressed_today": self.suppressed_today,
            "pending_alerts": len(self.pending_alerts),
        }
