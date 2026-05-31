"""
状态持久化模块

将重启后会丢失的关键运行时状态保存到 JSON 文件:
  - 信号推送冷却时间戳
  - BTC 价格追踪数据 (最近1小时)
  - 日内开盘价
  - 风控告警冷却
  - AI 分析缓存时间

每分钟自动保存一次, 启动时自动恢复。
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

log = logging.getLogger("state")

STATE_FILE = "/root/projects/bot_state.json"


class StatePersistence:
    """运行时状态持久化"""

    def __init__(self, filepath: str = STATE_FILE):
        self.filepath = filepath
        self._last_save = 0
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                age = time.time() - data.get("_save_time", 0)
                log.info(f"恢复状态文件 (保存于 {age:.0f}秒前)")
                return data
            except Exception as e:
                log.warning(f"状态文件加载失败: {e}, 使用空状态")
        return {}

    def save(self, force: bool = False):
        """保存状态 (默认每60秒一次)"""
        now = time.time()
        if not force and now - self._last_save < 60:
            return
        self.data["_save_time"] = now
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=2)
            self._last_save = now
        except Exception as e:
            log.error(f"状态保存失败: {e}")

    # --- 冷却时间 ---
    def save_cooldowns(self, signal_sent: dict):
        """保存信号推送冷却状态"""
        # 只保存1小时内的记录, 过期的不保存
        now = time.time()
        active = {}
        for key, val in signal_sent.items():
            if isinstance(val, dict) and now - val.get("time", 0) < 7200:
                active[key] = val
        self.data["cooldowns"] = active

    def load_cooldowns(self) -> dict:
        """恢复冷却状态"""
        cooldowns = self.data.get("cooldowns", {})
        now = time.time()
        # 过滤过期条目
        active = {}
        for key, val in cooldowns.items():
            if isinstance(val, dict) and now - val.get("time", 0) < 7200:
                active[key] = val
        return active

    # --- 价格追踪 ---
    def save_price_tracker(self, prices: list, daily_open: float, daily_open_date: str):
        """保存价格追踪数据"""
        now = time.time()
        # 只保存最近1小时的价格数据 (启动后几分钟就能重新积累)
        recent_prices = [(t, p) for t, p in prices if now - t < 3600]
        self.data["price_tracker"] = {
            "prices": recent_prices[-100:],   # 最多100个点
            "daily_open": daily_open,
            "daily_open_date": daily_open_date,
        }

    def load_price_tracker(self) -> dict:
        """恢复价格追踪数据"""
        pt = self.data.get("price_tracker", {})
        if not pt:
            return {"prices": [], "daily_open": None, "daily_open_date": None}

        now = time.time()
        # 只恢复5分钟内的价格数据 (太旧没意义)
        prices = [(t, p) for t, p in pt.get("prices", []) if now - t < 300]
        return {
            "prices": prices,
            "daily_open": pt.get("daily_open"),
            "daily_open_date": pt.get("daily_open_date"),
        }

    # --- 风控冷却 ---
    def save_risk_cooldowns(self, last_alerts: dict):
        """保存风控告警冷却"""
        now = time.time()
        active = {k: v for k, v in last_alerts.items() if now - v < 7200}
        self.data["risk_cooldowns"] = active

    def load_risk_cooldowns(self) -> dict:
        """恢复风控冷却"""
        cooldowns = self.data.get("risk_cooldowns", {})
        now = time.time()
        return {k: v for k, v in cooldowns.items() if now - v < 7200}

    # --- TG update offset ---
    def save_update_offset(self, offset: int):
        self.data["update_offset"] = offset

    def load_update_offset(self) -> int:
        return self.data.get("update_offset", 0)

    # --- IV 曲面快照 ---
    def save_iv_surface_snapshot(self, iv_surface: dict, timestamp: float):
        """保存 IV 曲面快照 (用于历史分析)"""
        snapshots = self.data.get("iv_surface_history", [])
        # 提取关键数据 (不保存完整的 by_exp 细节, 只保存摘要)
        summary = {
            "time": timestamp,
            "global_mean": iv_surface.get("global", {}).get("mean", 0),
            "global_median": iv_surface.get("global", {}).get("median", 0),
            "exps": {},
        }
        for exp, stats in iv_surface.get("by_exp", {}).items():
            summary["exps"][exp] = {
                "mean": round(stats.get("mean", 0), 4),
                "median": round(stats.get("median", 0), 4),
                "p25": round(stats.get("p25", 0), 4),
                "p75": round(stats.get("p75", 0), 4),
            }
        snapshots.append(summary)

        # 保留最近 7 天的曲面快照 (每3分钟一次 ≈ 3360条)
        if len(snapshots) > 3500:
            snapshots = snapshots[-3360:]
        self.data["iv_surface_history"] = snapshots

    def get_iv_surface_history(self, hours: int = 24) -> list:
        """获取最近N小时的 IV 曲面历史"""
        cutoff = time.time() - hours * 3600
        return [s for s in self.data.get("iv_surface_history", [])
                if s.get("time", 0) > cutoff]
