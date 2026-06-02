"""
对冲顾问引擎

在急跌场景下自动计算并推送对冲建议:
  1. 实时追踪强平价格和距离
  2. 当强平距离缩短到阈值时, 主动推送对冲建议
  3. 对比"补保证金 vs 买Put"的效率
  4. 推荐最优行权价和张数
  5. 对冲仓位到期续期提醒

触发条件:
  - 强平距离 < 40% → 每次 overview 展示
  - 强平距离 < 30% → 主动推送对冲建议 (WARNING)
  - 强平距离 < 20% → 紧急推送 (DANGER)
  - 强平距离 < 15% → 持续推送 (CRITICAL)
"""

import time
import logging
from datetime import datetime, timezone
from margin_calc import (
    bs_put_price, calc_put_margin, calc_maint_margin,
    estimate_liquidation_price, stress_test_portfolio,
)

log = logging.getLogger("hedge_advisor")


# ============================================================
#  配置
# ============================================================
class HedgeConfig:
    # 强平距离告警阈值
    LIQ_SHOW = 40       # < 40% 在 overview 展示
    LIQ_WARNING = 30     # < 30% 主动推送对冲建议
    LIQ_DANGER = 20      # < 20% 紧急推送
    LIQ_CRITICAL = 15    # < 15% 持续推送

    # 推送冷却
    HEDGE_COOLDOWN = 3600      # 对冲建议: 1小时
    HEDGE_DANGER_CD = 600      # 紧急: 10分钟
    HEDGE_CRITICAL_CD = 120    # 极危: 2分钟

    # 对冲候选参数
    HEDGE_DTE_MIN = 7          # 最少 7 天到期
    HEDGE_DTE_MAX = 120        # 最长 120 天
    HEDGE_BUDGET_OPTIONS = [500, 1000, 2000, 3000]  # 预算选项

    # 对冲仓位到期提醒
    HEDGE_EXPIRY_WARN = 5      # 对冲 Put 还剩 5 天到期时提醒


# ============================================================
#  对冲顾问
# ============================================================
class HedgeAdvisor:

    def __init__(self):
        self.cfg = HedgeConfig()
        self.last_liq_price = 0
        self.last_liq_drop = 0
        self.last_hedge_push = 0
        self.last_expiry_warn = {}  # {symbol: timestamp}

    def update_liquidation(self, pos_list: list, spot: float,
                           account_balance: float) -> dict:
        """
        更新强平价格追踪

        Returns: {
            liq_price, liq_drop_pct, cushion, details,
            alert_level: None/WARNING/DANGER/CRITICAL,
        }
        """
        liq = estimate_liquidation_price(pos_list, spot, account_balance)
        self.last_liq_price = liq["liq_price"]
        self.last_liq_drop = liq["liq_drop_pct"]

        drop = abs(liq["liq_drop_pct"])
        if drop < self.cfg.LIQ_CRITICAL:
            liq["alert_level"] = "CRITICAL"
        elif drop < self.cfg.LIQ_DANGER:
            liq["alert_level"] = "DANGER"
        elif drop < self.cfg.LIQ_WARNING:
            liq["alert_level"] = "WARNING"
        else:
            liq["alert_level"] = None

        return liq

    def should_push_hedge(self, liq: dict) -> bool:
        """判断是否应该推送对冲建议"""
        level = liq.get("alert_level")
        if not level:
            return False

        now = time.time()
        if level == "CRITICAL":
            cd = self.cfg.HEDGE_CRITICAL_CD
        elif level == "DANGER":
            cd = self.cfg.HEDGE_DANGER_CD
        else:
            cd = self.cfg.HEDGE_COOLDOWN

        if now - self.last_hedge_push < cd:
            return False

        self.last_hedge_push = now
        return True

    def calc_hedge_options(self, pos_list: list, spot: float,
                           account_balance: float,
                           available_puts: list) -> dict:
        """
        计算完整的对冲方案

        Args:
            pos_list: 当前持仓
            spot: BTC 现价
            account_balance: 账户余额 (marginBalance)
            available_puts: 可买的 Put 列表, 每个 dict:
                {symbol, strike, dte, ask, iv, delta}

        Returns: {
            liq_current: 当前强平价,
            cash_options: [{budget, liq_price, improve}],
            put_options: [{symbol, strike, dte, ask, qty, cost, liq_price, improve, efficiency}],
            best_put: {...},
            comparison: {cash_1k_improve, best_put_1k_improve, ratio},
        }
        """
        liq_base = estimate_liquidation_price(pos_list, spot, account_balance)

        # 补保证金方案
        cash_options = []
        for budget in self.cfg.HEDGE_BUDGET_OPTIONS:
            liq_c = estimate_liquidation_price(pos_list, spot, account_balance + budget)
            improve = liq_base["liq_price"] - liq_c["liq_price"]
            cash_options.append({
                "budget": budget,
                "liq_price": liq_c["liq_price"],
                "liq_drop": liq_c["liq_drop_pct"],
                "improve": improve,
            })

        # 买 Put 方案
        put_options = []
        for p in available_puts:
            ask = p.get("ask", 0)
            if ask <= 0:
                continue
            strike = p["strike"]
            dte = p["dte"]
            iv = p.get("iv", 0.48)

            # 按 $1000 预算计算
            for budget in self.cfg.HEDGE_BUDGET_OPTIONS:
                n = min(int(budget / ask * 10) / 10, 5.0)
                if n < 0.1:
                    continue
                cost = ask * n

                hedged = pos_list + [{
                    "symbol": p["symbol"], "qty": n, "strike": strike,
                    "entry_price": ask, "mark_price": ask, "dte": dte,
                    "iv": iv,
                }]
                liq_h = estimate_liquidation_price(hedged, spot, account_balance - cost)
                improve = liq_base["liq_price"] - liq_h["liq_price"]
                efficiency = improve / cost * 1000 if cost > 0 else 0

                put_options.append({
                    "symbol": p["symbol"],
                    "strike": strike,
                    "dte": dte,
                    "ask": ask,
                    "qty": n,
                    "cost": cost,
                    "budget": budget,
                    "liq_price": liq_h["liq_price"],
                    "liq_drop": liq_h["liq_drop_pct"],
                    "improve": improve,
                    "efficiency": efficiency,
                })

        # 每个预算下的最佳 Put
        best_by_budget = {}
        for po in put_options:
            b = po["budget"]
            if b not in best_by_budget or po["improve"] > best_by_budget[b]["improve"]:
                best_by_budget[b] = po

        # $1000 对比
        cash_1k = next((c for c in cash_options if c["budget"] == 1000), None)
        best_put_1k = best_by_budget.get(1000)

        comparison = {}
        if cash_1k and best_put_1k:
            ratio = best_put_1k["improve"] / cash_1k["improve"] if cash_1k["improve"] > 0 else float("inf")
            comparison = {
                "cash_1k_improve": cash_1k["improve"],
                "best_put_1k_improve": best_put_1k["improve"],
                "ratio": ratio,
            }

        return {
            "liq_current": liq_base,
            "cash_options": cash_options,
            "best_by_budget": best_by_budget,
            "comparison": comparison,
        }

    def format_hedge_alert(self, liq: dict, hedge_calc: dict,
                           spot: float) -> str:
        """格式化对冲建议消息"""
        level = liq.get("alert_level", "WARNING")
        liq_price = liq["liq_price"]
        liq_drop = abs(liq["liq_drop_pct"])

        if level == "CRITICAL":
            icon = "🚨🚨🚨"
            header = "极危: 强平距离过近!"
        elif level == "DANGER":
            icon = "🔴"
            header = "紧急: 建议立即对冲"
        else:
            icon = "⚠️"
            header = "预警: 考虑增加对冲"

        lines = [f"{icon} <b>{header}</b>\n"]
        lines.append(f"BTC ${spot:,.0f} → 强平价 ${liq_price:,.0f} (跌 {liq_drop:.0f}%)")
        lines.append(f"安全垫 ${liq['cushion']:,.0f}\n")

        # 对冲 vs 补保证金对比
        comp = hedge_calc.get("comparison", {})
        if comp:
            lines.append("<b>同样 $1,000:</b>")
            lines.append(f"  补保证金 → 强平下移 ${comp['cash_1k_improve']:,.0f}")
            lines.append(f"  买 Put   → 强平下移 ${comp['best_put_1k_improve']:,.0f}")
            lines.append(f"  <b>买 Put 效率 {comp['ratio']:.0f}x</b>\n")

        # 推荐方案
        best = hedge_calc.get("best_by_budget", {})
        if best:
            lines.append("<b>推荐对冲:</b>")
            for budget in [500, 1000, 2000]:
                b = best.get(budget)
                if not b:
                    continue
                short_sym = b["symbol"].split("BTC-")[-1]
                lines.append(
                    f"  ${budget:,}: 买 {short_sym} ×{b['qty']:.1f}张"
                    f" → 强平 ${b['liq_price']:,.0f} (跌{abs(b['liq_drop']):.0f}%)"
                )

        lines.append(f"\n👉 /hedge 查看详细方案")
        return "\n".join(lines)

    def format_liq_line(self, liq: dict, spot: float) -> str:
        """格式化一行强平价信息 (用于 overview)"""
        liq_price = liq["liq_price"]
        drop = abs(liq["liq_drop_pct"])

        if drop < 15:
            icon = "🚨"
        elif drop < 25:
            icon = "🔴"
        elif drop < 40:
            icon = "⚠️"
        else:
            icon = "🟢"

        return f"{icon} 强平 ${liq_price:,.0f} (跌{drop:.0f}%) 垫 ${liq['cushion']:,.0f}"

    def check_hedge_expiry(self, pos_list: list) -> list[dict]:
        """检查对冲仓位是否即将到期, 需要续期"""
        alerts = []
        now = time.time()

        for p in pos_list:
            if p.get("qty", 0) <= 0:
                continue  # 只检查 Long Put
            dte = p.get("dte", 999)
            sym = p.get("symbol", "")

            if dte <= self.cfg.HEDGE_EXPIRY_WARN:
                # 冷却: 每个 symbol 12小时提醒一次
                last = self.last_expiry_warn.get(sym, 0)
                if now - last < 43200:
                    continue
                self.last_expiry_warn[sym] = now

                short_sym = sym.split("BTC-")[-1]
                alerts.append({
                    "symbol": sym,
                    "short_sym": short_sym,
                    "dte": dte,
                    "msg": (
                        f"⏰ <b>对冲到期提醒</b>\n\n"
                        f"Long {short_sym} 仅剩 <b>{dte:.0f}天</b>到期\n"
                        f"到期后失去对冲保护, 强平价将上升\n\n"
                        f"👉 /hedge 查看续期方案"
                    ),
                })

        return alerts


# ============================================================
#  风控模式 (平时/警戒/危机)
# ============================================================
class RiskMode:
    """
    三级风控模式, 根据强平距离自动切换:
      NORMAL:  强平 > 40%, 正常运行
      ALERT:   强平 20-40%, 加快扫描+推送对冲建议
      CRISIS:  强平 < 20%, 最快扫描+持续推送+关闭新开仓信号
    """
    NORMAL = "NORMAL"
    ALERT = "ALERT"
    CRISIS = "CRISIS"

    def __init__(self):
        self.mode = self.NORMAL
        self.last_mode = self.NORMAL

    def update(self, liq_drop_pct: float) -> str:
        """根据强平距离更新模式"""
        self.last_mode = self.mode
        drop = abs(liq_drop_pct)

        if drop < 20:
            self.mode = self.CRISIS
        elif drop < 40:
            self.mode = self.ALERT
        else:
            self.mode = self.NORMAL

        if self.mode != self.last_mode:
            log.warning(f"风控模式切换: {self.last_mode} → {self.mode} (强平距离 {drop:.0f}%)")

        return self.mode

    @property
    def scan_interval(self) -> int:
        """当前模式下的扫描间隔"""
        if self.mode == self.CRISIS:
            return 30   # 30秒
        elif self.mode == self.ALERT:
            return 60   # 1分钟
        return 180      # 3分钟

    @property
    def should_suppress_signals(self) -> bool:
        """危机模式下抑制新开仓信号"""
        return self.mode == self.CRISIS

    @property
    def mode_icon(self) -> str:
        if self.mode == self.CRISIS:
            return "🚨 CRISIS"
        elif self.mode == self.ALERT:
            return "⚠️ ALERT"
        return "🟢 NORMAL"

    def mode_changed(self) -> bool:
        return self.mode != self.last_mode
