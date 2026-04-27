"""
风控监控模块

监控维度:
  1. 持仓浮亏 - 实时盈亏、亏损速度
  2. 爆仓风险 - 基于保证金/维持保证金估算
  3. BTC 市场波动 - 价格跌幅、波动率突变
  4. 希腊值风险 - Delta/Gamma 暴露、Vega 风险
  5. 到期风险 - 临近到期的 gamma 爆炸

告警等级:
  INFO    - 正常, 仅记录
  WATCH   - 需关注, 静默推送
  WARNING - 警告, 推送通知
  DANGER  - 危险, 强制推送 + 建议操作
  CRITICAL- 极度危险, 连续推送直到处理
"""

import time
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field


# ============================================================
#  风控配置
# ============================================================
class RiskConfig:
    # --- 持仓浮亏 ---
    PNL_WARN_RATIO = 1.0       # 浮亏 >= 1x 权利金 → WARNING
    PNL_DANGER_RATIO = 2.0     # 浮亏 >= 2x → DANGER
    PNL_CRITICAL_RATIO = 3.0   # 浮亏 >= 3x → CRITICAL, 建议立即平仓

    # --- 爆仓风险 (基于保证金估算) ---
    # 币安期权保证金 = max(标的价格 * 保证金率 - OTM金额, 最低保证金率 * 标的价格)
    MARGIN_RATE = 0.15              # 初始保证金率
    MAINT_MARGIN_RATE = 0.075       # 维持保证金率
    MARGIN_USAGE_WARN = 0.60        # 保证金使用率 60% → WARNING
    MARGIN_USAGE_DANGER = 0.80      # 80% → DANGER
    MARGIN_USAGE_CRITICAL = 0.95    # 95% → CRITICAL, 接近爆仓

    # --- BTC 市场波动 ---
    # 短期 (本次扫描 vs 上次)
    BTC_DROP_WATCH = 1.0       # 单次扫描间跌1% → WATCH
    BTC_DROP_WARN = 2.0        # 跌2% → WARNING
    BTC_DROP_DANGER = 4.0      # 跌4% → DANGER
    BTC_DROP_CRITICAL = 7.0    # 跌7% → CRITICAL, 可能闪崩

    # 累计 (从开仓以来或当日)
    BTC_DAILY_DROP_WARN = 5.0      # 日内跌5% → WARNING
    BTC_DAILY_DROP_DANGER = 8.0    # 日内跌8% → DANGER
    BTC_DAILY_DROP_CRITICAL = 12.0 # 日内跌12% → CRITICAL

    # --- 距行权价 ---
    DIST_STRIKE_WATCH = 20.0       # 距行权 20% → WATCH
    DIST_STRIKE_WARN = 15.0        # 15% → WARNING
    DIST_STRIKE_DANGER = 10.0      # 10% → DANGER
    DIST_STRIKE_CRITICAL = 5.0     # 5% → CRITICAL, 即将 ITM

    # --- 希腊值 ---
    DELTA_WARN = 0.15          # |delta| 超过 0.15 → WARNING (不再是深度OTM)
    DELTA_DANGER = 0.25        # 0.25 → DANGER
    GAMMA_WARN = 0.0001        # gamma 过大 → WARNING (gamma * qty * spot)
    VEGA_EXPOSURE_WARN = 50.0  # vega * 持仓量 > 50 → WARNING (IV涨1%亏$50)

    # --- 到期风险 ---
    DTE_WATCH = 7              # 7天内到期 → WATCH
    DTE_WARN = 3               # 3天内 → WARNING
    DTE_DANGER = 1             # 1天内 → DANGER

    # --- 推送冷却 ---
    COOLDOWN_INFO = 3600       # 1小时
    COOLDOWN_WATCH = 1800      # 30分钟
    COOLDOWN_WARNING = 600     # 10分钟
    COOLDOWN_DANGER = 180      # 3分钟
    COOLDOWN_CRITICAL = 60     # 1分钟, 持续提醒


# ============================================================
#  告警数据结构
# ============================================================
@dataclass
class RiskAlert:
    level: str          # INFO, WATCH, WARNING, DANGER, CRITICAL
    category: str       # PNL, MARGIN, BTC_MOVE, GREEKS, EXPIRY
    symbol: str         # 相关合约
    title: str          # 简短标题
    detail: str         # 详细信息
    action: str = ""    # 建议操作
    timestamp: float = field(default_factory=time.time)

    @property
    def level_rank(self) -> int:
        return {"INFO": 0, "WATCH": 1, "WARNING": 2, "DANGER": 3, "CRITICAL": 4}.get(self.level, 0)

    @property
    def icon(self) -> str:
        return {
            "INFO": "ℹ️", "WATCH": "👀", "WARNING": "⚠️",
            "DANGER": "🔴", "CRITICAL": "🚨"
        }.get(self.level, "")


# ============================================================
#  BTC 价格追踪器
# ============================================================
class PriceTracker:
    """追踪 BTC 价格变化"""

    def __init__(self):
        self.prices = []           # [(timestamp, price), ...]
        self.daily_open = None     # 当日开盘价
        self.daily_open_date = None

    def record(self, price: float):
        now = time.time()
        self.prices.append((now, price))
        # 保留最近 24 小时
        cutoff = now - 86400
        self.prices = [(t, p) for t, p in self.prices if t > cutoff]

        # 更新日开盘
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_open_date != today:
            self.daily_open = price
            self.daily_open_date = today

    def get_change_pct(self, window_seconds: int = 300) -> float:
        """获取最近 N 秒的变化百分比"""
        if len(self.prices) < 2:
            return 0
        now = time.time()
        cutoff = now - window_seconds
        older = [p for t, p in self.prices if t <= cutoff]
        if not older:
            return 0
        old_price = older[-1]
        cur_price = self.prices[-1][1]
        return (cur_price - old_price) / old_price * 100

    def get_daily_change_pct(self) -> float:
        """获取日内涨跌幅"""
        if not self.daily_open or not self.prices:
            return 0
        cur = self.prices[-1][1]
        return (cur - self.daily_open) / self.daily_open * 100

    def get_max_drawdown(self, window_hours: int = 24) -> float:
        """获取最近 N 小时内的最大回撤 %"""
        cutoff = time.time() - window_hours * 3600
        relevant = [p for t, p in self.prices if t > cutoff]
        if len(relevant) < 2:
            return 0
        peak = relevant[0]
        max_dd = 0
        for p in relevant:
            if p > peak:
                peak = p
            dd = (peak - p) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd


# ============================================================
#  风控引擎
# ============================================================
class RiskEngine:

    def __init__(self):
        self.cfg = RiskConfig()
        self.price_tracker = PriceTracker()
        self.last_alerts = {}   # {key: timestamp} 用于去重

    def check_all(self, data: dict) -> list[RiskAlert]:
        """执行全部风控检查"""
        alerts = []
        spot = data["spot"]

        # 记录价格
        self.price_tracker.record(spot)

        # 1. BTC 市场波动检查
        alerts.extend(self._check_btc_move(spot))

        # 2. 逐个持仓检查
        positions = data.get("positions", [])
        marks = data.get("marks", {})

        for pos in positions:
            qty = float(pos.get("quantity", 0))
            if qty == 0:
                continue
            sym = pos["symbol"]
            mark = marks.get(sym, {})
            alerts.extend(self._check_position(pos, mark, spot))

        # 按严重程度排序
        alerts.sort(key=lambda a: a.level_rank, reverse=True)

        return alerts

    def _check_btc_move(self, spot: float) -> list[RiskAlert]:
        """检查 BTC 价格波动"""
        alerts = []
        cfg = self.cfg

        # 短期变化 (最近5分钟)
        short_change = self.price_tracker.get_change_pct(300)
        if short_change < -cfg.BTC_DROP_CRITICAL:
            alerts.append(RiskAlert(
                level="CRITICAL", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 闪崩 {short_change:.1f}% (5分钟)",
                detail=f"BTC 在5分钟内暴跌 {abs(short_change):.1f}%, 当前 ${spot:,.0f}",
                action="立即检查所有持仓! 考虑平仓或对冲",
            ))
        elif short_change < -cfg.BTC_DROP_DANGER:
            alerts.append(RiskAlert(
                level="DANGER", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 急跌 {short_change:.1f}% (5分钟)",
                detail=f"BTC 短期大幅下跌, 当前 ${spot:,.0f}",
                action="密切关注持仓, 准备止损",
            ))
        elif short_change < -cfg.BTC_DROP_WARN:
            alerts.append(RiskAlert(
                level="WARNING", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 下跌 {short_change:.1f}% (5分钟)",
                detail=f"BTC 出现较大跌幅, 当前 ${spot:,.0f}",
                action="关注后续走势",
            ))

        # 日内变化
        daily_change = self.price_tracker.get_daily_change_pct()
        if daily_change < -cfg.BTC_DAILY_DROP_CRITICAL:
            alerts.append(RiskAlert(
                level="CRITICAL", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 日内暴跌 {daily_change:.1f}%",
                detail=f"BTC 今日已跌 {abs(daily_change):.1f}%, "
                       f"从 ${self.price_tracker.daily_open:,.0f} → ${spot:,.0f}",
                action="极端行情! 检查所有卖 Put 持仓, 可能需要全部平仓",
            ))
        elif daily_change < -cfg.BTC_DAILY_DROP_DANGER:
            alerts.append(RiskAlert(
                level="DANGER", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 日内大跌 {daily_change:.1f}%",
                detail=f"今日跌幅已达 {abs(daily_change):.1f}%, 当前 ${spot:,.0f}",
                action="检查持仓距行权价距离, 准备应急方案",
            ))
        elif daily_change < -cfg.BTC_DAILY_DROP_WARN:
            alerts.append(RiskAlert(
                level="WARNING", category="BTC_MOVE", symbol="BTC",
                title=f"BTC 日内跌 {daily_change:.1f}%",
                detail=f"今日跌幅 {abs(daily_change):.1f}%, 当前 ${spot:,.0f}",
                action="留意持仓风险",
            ))

        # 最大回撤
        max_dd = self.price_tracker.get_max_drawdown(24)
        if max_dd > 10:
            alerts.append(RiskAlert(
                level="DANGER", category="BTC_MOVE", symbol="BTC",
                title=f"24h 最大回撤 {max_dd:.1f}%",
                detail=f"过去24小时 BTC 从高点回撤 {max_dd:.1f}%",
                action="高波动环境, 谨慎操作",
            ))

        return alerts

    def _check_position(self, pos: dict, mark: dict, spot: float) -> list[RiskAlert]:
        """检查单个持仓的风险"""
        alerts = []
        cfg = self.cfg

        sym = pos["symbol"]
        qty = float(pos.get("quantity", 0))
        abs_qty = abs(qty)
        entry = float(pos.get("entryPrice", 0))
        mark_price = float(pos.get("markPrice", 0) or mark.get("markPrice", 0))
        strike = float(pos.get("strikePrice", 0))
        expiry_ts = int(pos.get("expiryDate", 0))

        # 希腊值
        delta = float(mark.get("delta", 0))
        gamma = float(mark.get("gamma", 0))
        vega = float(mark.get("vega", 0))
        theta = float(mark.get("theta", 0))

        # 只关注卖出的 Put
        if qty >= 0 or "-P" not in sym:
            return alerts

        # === 1. 浮亏检查 ===
        pnl = (entry - mark_price) * abs_qty
        loss_ratio = (mark_price - entry) / entry if entry > 0 else 0

        if loss_ratio >= cfg.PNL_CRITICAL_RATIO:
            alerts.append(RiskAlert(
                level="CRITICAL", category="PNL", symbol=sym,
                title=f"浮亏 {loss_ratio:.1f}x 权利金!",
                detail=f"入场 ${entry:,.0f} → 当前 ${mark_price:,.0f}\n"
                       f"浮亏 ${abs(pnl):,.0f} ({loss_ratio:.1f}x 权利金)",
                action="强烈建议立即平仓止损!",
            ))
        elif loss_ratio >= cfg.PNL_DANGER_RATIO:
            alerts.append(RiskAlert(
                level="DANGER", category="PNL", symbol=sym,
                title=f"浮亏 {loss_ratio:.1f}x 权利金",
                detail=f"入场 ${entry:,.0f} → 当前 ${mark_price:,.0f}\n"
                       f"浮亏 ${abs(pnl):,.0f}",
                action="认真考虑止损平仓",
            ))
        elif loss_ratio >= cfg.PNL_WARN_RATIO:
            alerts.append(RiskAlert(
                level="WARNING", category="PNL", symbol=sym,
                title=f"浮亏 {loss_ratio:.1f}x 权利金",
                detail=f"入场 ${entry:,.0f} → 当前 ${mark_price:,.0f}",
                action="设好止损价位",
            ))

        # === 2. 距行权价检查 ===
        dist = (spot - strike) / spot * 100 if spot > 0 else 0

        if dist < cfg.DIST_STRIKE_CRITICAL:
            alerts.append(RiskAlert(
                level="CRITICAL", category="MARGIN", symbol=sym,
                title=f"距行权价仅 {dist:.1f}%!",
                detail=f"BTC ${spot:,.0f} vs 行权价 ${strike:,.0f}\n"
                       f"再跌 ${spot - strike:,.0f} 即进入实值, 亏损将急剧扩大",
                action="立即平仓! 即将变成 ITM",
            ))
        elif dist < cfg.DIST_STRIKE_DANGER:
            alerts.append(RiskAlert(
                level="DANGER", category="MARGIN", symbol=sym,
                title=f"距行权价 {dist:.1f}%",
                detail=f"BTC ${spot:,.0f} vs 行权价 ${strike:,.0f}",
                action="准备平仓, 设置止损单",
            ))
        elif dist < cfg.DIST_STRIKE_WARN:
            alerts.append(RiskAlert(
                level="WARNING", category="MARGIN", symbol=sym,
                title=f"距行权价 {dist:.1f}%",
                detail=f"行权价 ${strike:,.0f} 不再那么远了",
                action="提高警惕, 关注 BTC 走势",
            ))
        elif dist < cfg.DIST_STRIKE_WATCH:
            alerts.append(RiskAlert(
                level="WATCH", category="MARGIN", symbol=sym,
                title=f"距行权价 {dist:.1f}%",
                detail=f"行权价 ${strike:,.0f}",
            ))

        # === 3. 保证金 / 爆仓估算 ===
        # 简化估算: 卖 Put 保证金 ≈ max(spot * margin_rate - OTM_amount, spot * min_margin_rate)
        otm_amount = max(spot - strike, 0)  # 如果 ITM 则为0
        margin_est = max(
            spot * cfg.MARGIN_RATE - otm_amount,
            spot * cfg.MAINT_MARGIN_RATE
        ) * abs_qty

        maint_margin = spot * cfg.MAINT_MARGIN_RATE * abs_qty

        # 保证金使用率 ≈ 当前持仓市值 / 保证金
        margin_usage = mark_price * abs_qty / margin_est if margin_est > 0 else 0

        if margin_usage > cfg.MARGIN_USAGE_CRITICAL:
            alerts.append(RiskAlert(
                level="CRITICAL", category="MARGIN", symbol=sym,
                title=f"保证金使用率 {margin_usage:.0%}!",
                detail=f"持仓市值 ${mark_price * abs_qty:,.0f} vs 预估保证金 ${margin_est:,.0f}\n"
                       f"极度接近爆仓线!",
                action="立即追加保证金或平仓!",
            ))
        elif margin_usage > cfg.MARGIN_USAGE_DANGER:
            alerts.append(RiskAlert(
                level="DANGER", category="MARGIN", symbol=sym,
                title=f"保证金使用率 {margin_usage:.0%}",
                detail=f"持仓市值 ${mark_price * abs_qty:,.0f} vs 保证金 ${margin_est:,.0f}",
                action="准备追加保证金或减仓",
            ))
        elif margin_usage > cfg.MARGIN_USAGE_WARN:
            alerts.append(RiskAlert(
                level="WARNING", category="MARGIN", symbol=sym,
                title=f"保证金使用率 {margin_usage:.0%}",
                detail=f"保证金开始吃紧",
                action="关注保证金情况",
            ))

        # === 4. 希腊值风险 ===
        abs_delta = abs(delta) * abs_qty
        if abs_delta > cfg.DELTA_DANGER:
            alerts.append(RiskAlert(
                level="DANGER", category="GREEKS", symbol=sym,
                title=f"Delta 暴露 {abs_delta:.3f}",
                detail=f"已不是深度 OTM! Delta={delta:.4f} x {abs_qty}张\n"
                       f"BTC 每跌 $1000, 你亏约 ${abs_delta * 1000:,.0f}",
                action="Delta 太大, 考虑平仓或对冲",
            ))
        elif abs_delta > cfg.DELTA_WARN:
            alerts.append(RiskAlert(
                level="WARNING", category="GREEKS", symbol=sym,
                title=f"Delta 偏大 {abs_delta:.3f}",
                detail=f"Delta={delta:.4f}, 不再是安全的深度 OTM",
                action="注意: BTC 继续下跌会导致 delta 加速增大",
            ))

        # Gamma 风险 (gamma * spot^2 * qty / 100 = BTC涨跌1%时delta变化量)
        gamma_exposure = abs(gamma) * abs_qty * spot * spot / 10000
        if gamma_exposure > cfg.GAMMA_WARN * spot:
            alerts.append(RiskAlert(
                level="WARNING", category="GREEKS", symbol=sym,
                title=f"Gamma 风险偏高",
                detail=f"Gamma={gamma:.8f}, 价格加速敏感度 {gamma_exposure:.4f}\n"
                       f"BTC 波动会导致 Delta 快速变化",
                action="临近到期或接近行权价时 gamma 会暴增",
            ))

        # Vega 暴露
        vega_exposure = abs(vega) * abs_qty
        if vega_exposure > cfg.VEGA_EXPOSURE_WARN:
            alerts.append(RiskAlert(
                level="WATCH", category="GREEKS", symbol=sym,
                title=f"Vega 暴露 ${vega_exposure:,.0f}",
                detail=f"IV 每上升 1%, 浮亏增加约 ${vega_exposure:,.0f}",
            ))

        # === 5. 到期风险 ===
        if expiry_ts > 0:
            now = datetime.now(timezone.utc)
            expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
            dte = max((expiry - now).total_seconds() / 86400, 0)

            if dte <= cfg.DTE_DANGER:
                alerts.append(RiskAlert(
                    level="DANGER", category="EXPIRY", symbol=sym,
                    title=f"明天到期! ({dte:.1f}天)",
                    detail=f"到期日 {expiry.strftime('%Y-%m-%d')}\n"
                           f"Gamma 风险极大, PnL 可能剧烈波动",
                    action="建议到期前平仓, 避免交割风险",
                ))
            elif dte <= cfg.DTE_WARN:
                alerts.append(RiskAlert(
                    level="WARNING", category="EXPIRY", symbol=sym,
                    title=f"{dte:.0f} 天后到期",
                    detail=f"到期日 {expiry.strftime('%Y-%m-%d')}, Gamma 开始放大",
                    action="关注是否需要提前平仓",
                ))
            elif dte <= cfg.DTE_WATCH:
                alerts.append(RiskAlert(
                    level="WATCH", category="EXPIRY", symbol=sym,
                    title=f"{dte:.0f} 天后到期",
                    detail=f"到期日 {expiry.strftime('%Y-%m-%d')}",
                ))

        return alerts

    def should_push(self, alert: RiskAlert) -> bool:
        """根据冷却时间判断是否应该推送"""
        key = f"{alert.category}:{alert.symbol}:{alert.level}"
        now = time.time()
        last = self.last_alerts.get(key, 0)

        cooldowns = {
            "INFO": self.cfg.COOLDOWN_INFO,
            "WATCH": self.cfg.COOLDOWN_WATCH,
            "WARNING": self.cfg.COOLDOWN_WARNING,
            "DANGER": self.cfg.COOLDOWN_DANGER,
            "CRITICAL": self.cfg.COOLDOWN_CRITICAL,
        }
        cooldown = cooldowns.get(alert.level, 3600)

        if now - last > cooldown:
            self.last_alerts[key] = now
            return True

        # 升级也推: 如果之前是低级别, 现在变高了
        lower_keys = [f"{alert.category}:{alert.symbol}:{lv}"
                      for lv in ("INFO", "WATCH", "WARNING", "DANGER")
                      if {"INFO": 0, "WATCH": 1, "WARNING": 2, "DANGER": 3}.get(lv, 0) < alert.level_rank]
        for lk in lower_keys:
            if lk in self.last_alerts:
                self.last_alerts[key] = now
                return True

        return False


# ============================================================
#  风控消息格式化
# ============================================================
def format_risk_alerts(alerts: list[RiskAlert], full: bool = False) -> str:
    """格式化风控告警消息"""
    if not alerts:
        return "🛡️ <b>风控状态: 一切正常</b> ✅"

    # 按级别分组
    critical = [a for a in alerts if a.level == "CRITICAL"]
    danger = [a for a in alerts if a.level == "DANGER"]
    warning = [a for a in alerts if a.level == "WARNING"]
    watch = [a for a in alerts if a.level == "WATCH"]

    lines = ["🛡️ <b>风控报告</b>"]
    lines.append("")

    # 总览
    if critical:
        lines.append(f"🚨 极度危险: {len(critical)} 项")
    if danger:
        lines.append(f"🔴 危险: {len(danger)} 项")
    if warning:
        lines.append(f"⚠️ 警告: {len(warning)} 项")
    if watch and full:
        lines.append(f"👀 关注: {len(watch)} 项")
    lines.append("")

    # 详细信息
    for a in critical:
        lines.append(f"🚨 <b>[极危] {a.title}</b>")
        lines.append(f"  {a.detail}")
        if a.action:
            lines.append(f"  → <b>{a.action}</b>")
        lines.append("")

    for a in danger:
        lines.append(f"🔴 <b>[危险] {a.title}</b>")
        lines.append(f"  {a.detail}")
        if a.action:
            lines.append(f"  → {a.action}")
        lines.append("")

    for a in warning:
        lines.append(f"⚠️ [警告] {a.title}")
        lines.append(f"  {a.detail}")
        if a.action:
            lines.append(f"  → {a.action}")
        lines.append("")

    if full:
        for a in watch:
            lines.append(f"👀 [关注] {a.title}")
            lines.append(f"  {a.detail}")
            lines.append("")

    return "\n".join(lines)


def format_risk_summary(alerts: list[RiskAlert], spot: float) -> str:
    """格式化简短的风控摘要 (用于市场概览)"""
    if not alerts or all(a.level in ("INFO", "WATCH") for a in alerts):
        return "🛡️ 风控: ✅ 正常"

    critical = len([a for a in alerts if a.level == "CRITICAL"])
    danger = len([a for a in alerts if a.level == "DANGER"])
    warning = len([a for a in alerts if a.level == "WARNING"])

    parts = []
    if critical:
        parts.append(f"🚨极危x{critical}")
    if danger:
        parts.append(f"🔴危险x{danger}")
    if warning:
        parts.append(f"⚠️警告x{warning}")

    return f"🛡️ 风控: {' '.join(parts)}"
