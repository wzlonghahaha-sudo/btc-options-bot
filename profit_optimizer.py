"""
收益优化模块

三大核心功能:
  1. 智能止盈 - 动态决策矩阵 (不是机械50%平仓)
  2. HV vs IV  - 已实现波动率 vs 隐含波动率, 卖方核心alpha
  3. Rolling   - Roll Out/Down/Down&Out 建议

设计原则:
  止盈不是目的, "把保证金部署到更好机会"才是。
  如果没有更好机会, 继续持有吃theta。
  但如果已经吃了75%+, 为剩下的蝇头小利冒尾部风险不值得。
"""

import os
import sys
import math
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_options import BinanceOptionsAPI

log = logging.getLogger(__name__)


# ============================================================
#  1. HV (已实现波动率) 计算
# ============================================================
class VolatilityAnalyzer:
    """计算 BTC 已实现波动率, 和 IV 对比"""

    def __init__(self):
        self._cache = {}
        self._cache_time = 0

    def get_btc_daily_closes(self, days: int = 60) -> list[float]:
        """从币安合约获取 BTC 日K收盘价"""
        now = time.time()
        if now - self._cache_time < 300 and "closes" in self._cache:
            return self._cache["closes"]

        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": "BTCUSDT", "interval": "1d", "limit": days},
                timeout=10,
            )
            klines = r.json()
            closes = [float(k[4]) for k in klines]
            self._cache = {"closes": closes}
            self._cache_time = now
            return closes
        except Exception as e:
            log.warning(f"获取 BTC 日K数据失败: {e}")
            return []

    def calc_hv(self, window: int = 20) -> float:
        """计算 N 日已实现年化波动率"""
        closes = self.get_btc_daily_closes()
        if len(closes) < window + 1:
            return 0

        log_returns = [math.log(closes[i] / closes[i - 1])
                       for i in range(len(closes) - window, len(closes))]
        if not log_returns:
            return 0

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
        return math.sqrt(variance * 365)

    def get_full_analysis(self, current_iv: float) -> dict:
        """完整的波动率分析"""
        hv_20 = self.calc_hv(20)
        hv_30 = self.calc_hv(30)
        hv_10 = self.calc_hv(10)

        iv_hv_ratio = current_iv / hv_20 if hv_20 > 0 else 0
        iv_premium_pct = (current_iv - hv_20) / hv_20 * 100 if hv_20 > 0 else 0

        # 判断卖方优势
        if iv_hv_ratio >= 1.5:
            edge = "STRONG"
            edge_desc = "IV 远高于实际波动, 卖方有极大优势"
        elif iv_hv_ratio >= 1.25:
            edge = "MODERATE"
            edge_desc = "IV 高于实际波动, 卖方有优势"
        elif iv_hv_ratio >= 1.0:
            edge = "SLIGHT"
            edge_desc = "IV 略高于实际波动, 卖方有轻微优势"
        else:
            edge = "NONE"
            edge_desc = "IV 低于实际波动, 卖方没有优势"

        # HV 趋势 (10日 vs 30日)
        if hv_10 > 0 and hv_30 > 0:
            hv_trend = (hv_10 - hv_30) / hv_30 * 100
            if hv_trend > 20:
                trend_desc = "HV 急升, 市场正在变得不稳定"
            elif hv_trend > 5:
                trend_desc = "HV 上升中"
            elif hv_trend < -20:
                trend_desc = "HV 快速回落, 市场趋于平静"
            elif hv_trend < -5:
                trend_desc = "HV 下降中"
            else:
                trend_desc = "HV 平稳"
        else:
            hv_trend = 0
            trend_desc = "数据不足"

        return {
            "hv_10": round(hv_10, 4),
            "hv_20": round(hv_20, 4),
            "hv_30": round(hv_30, 4),
            "iv": round(current_iv, 4),
            "iv_hv_ratio": round(iv_hv_ratio, 2),
            "iv_premium_pct": round(iv_premium_pct, 1),
            "edge": edge,
            "edge_desc": edge_desc,
            "hv_trend": round(hv_trend, 1),
            "hv_trend_desc": trend_desc,
        }


# ============================================================
#  2. 智能止盈决策
# ============================================================
@dataclass
class TakeProfitAdvice:
    action: str        # HOLD, CLOSE, CLOSE_AND_ROLL
    urgency: str       # LOW, MEDIUM, HIGH
    reason: str
    detail: str
    roll_target: str = ""  # 建议 roll 到的合约

    @property
    def icon(self) -> str:
        return {"HOLD": "🔄", "CLOSE": "💰", "CLOSE_AND_ROLL": "🔁"}.get(self.action, "")


def analyze_take_profit(
    symbol: str,
    entry_price: float,
    mark_price: float,
    qty: float,
    dte: float,
    delta: float,
    iv: float,
    spot: float,
    strike: float,
    has_signal_opportunities: bool,
    best_opportunity: dict | None,
    vol_analysis: dict,
    iv_trend: str,
) -> TakeProfitAdvice:
    """
    智能止盈决策

    决策矩阵:
      已赚比例 × 有无新机会 × DTE × IV趋势 × 安全垫 → 决策
    """
    profit_pct = (entry_price - mark_price) / entry_price * 100 if entry_price > 0 else 0
    remaining_premium = mark_price
    daily_theta_value = remaining_premium / dte if dte > 0 else 0
    dist_to_strike = (spot - strike) / spot * 100 if spot > 0 else 0

    # === 无条件平仓情况 ===

    # DTE < 7 且已赚 50%+ → 平仓 (gamma区, 不值得)
    if dte < 7 and profit_pct >= 50:
        return TakeProfitAdvice(
            action="CLOSE", urgency="HIGH",
            reason=f"临近到期({dte:.0f}天) + 已赚 {profit_pct:.0f}%",
            detail=f"剩余 {dte:.0f} 天 Gamma 风险急升, 为 ${remaining_premium:.0f} 的剩余利润不值得冒险",
        )

    # 已赚 90%+ → 无条件平仓
    if profit_pct >= 90:
        return TakeProfitAdvice(
            action="CLOSE", urgency="HIGH",
            reason=f"已赚 {profit_pct:.0f}%, 几乎吃满",
            detail=f"仅剩 ${remaining_premium:.0f} 未赚, 继续持有的风险收益比极差",
        )

    # 距行权价 < 15% + 已赚 40%+ → 平仓
    if dist_to_strike < 15 and profit_pct >= 40:
        return TakeProfitAdvice(
            action="CLOSE", urgency="HIGH",
            reason=f"安全垫不足({dist_to_strike:.1f}%) + 已有 {profit_pct:.0f}% 盈利",
            detail=f"BTC 距行权价只有 {dist_to_strike:.1f}%, 风险在增加",
        )

    # === 已赚 75%+ 的决策 ===
    if profit_pct >= 75:
        if has_signal_opportunities and best_opportunity:
            return TakeProfitAdvice(
                action="CLOSE_AND_ROLL", urgency="HIGH",
                reason=f"已赚 {profit_pct:.0f}% + 有新机会",
                detail=f"平仓释放保证金 → Roll 到赔率更好的合约\n"
                       f"剩余 ${remaining_premium:.0f} (日均仅 ${daily_theta_value:.1f}), 新机会更划算",
                roll_target=best_opportunity.get("symbol", ""),
            )
        else:
            # 没有新机会, 但已赚75%+, 风险收益比已经很差
            return TakeProfitAdvice(
                action="CLOSE", urgency="MEDIUM",
                reason=f"已赚 {profit_pct:.0f}%, 剩余收益太薄",
                detail=f"剩余 ${remaining_premium:.0f} 要 {dte:.0f} 天赚完 (日均 ${daily_theta_value:.1f})\n"
                       f"虽然暂无新机会 Roll, 但继续持有的风险/收益比很差\n"
                       f"保证金闲置总比被黑天鹅打亏好",
            )

    # === 已赚 50-75% 的决策 ===
    if profit_pct >= 50:
        if has_signal_opportunities and best_opportunity:
            return TakeProfitAdvice(
                action="CLOSE_AND_ROLL", urgency="MEDIUM",
                reason=f"已赚 {profit_pct:.0f}% + 有新机会可 Roll",
                detail=f"释放保证金部署到新机会, 资金效率更高",
                roll_target=best_opportunity.get("symbol", ""),
            )

        # 没有新机会 → 看 IV 趋势和 HV/IV 关系
        iv_hv_edge = vol_analysis.get("edge", "NONE")
        if iv_hv_edge in ("NONE",) and profit_pct >= 60:
            # IV 已经不比 HV 高了, 卖方优势消失
            return TakeProfitAdvice(
                action="CLOSE", urgency="MEDIUM",
                reason=f"已赚 {profit_pct:.0f}% + IV 卖方优势已消失",
                detail=f"IV/HV={vol_analysis.get('iv_hv_ratio', 0):.2f}x, {vol_analysis.get('edge_desc', '')}\n"
                       f"继续持有不再有统计优势",
            )

        if "急升" in iv_trend or "上升" in iv_trend:
            return TakeProfitAdvice(
                action="HOLD", urgency="LOW",
                reason=f"已赚 {profit_pct:.0f}%, IV 在上升 → 等新机会出现再 Roll",
                detail=f"IV 上升中, 可能很快出现高赔率卖出机会\n"
                       f"继续持有当前仓位, 同时密切关注新机会\n"
                       f"一旦有 SIGNAL 级别机会 → 立即平仓 + Roll",
            )

        # 默认: 继续持有 (没有更好的去处)
        return TakeProfitAdvice(
            action="HOLD", urgency="LOW",
            reason=f"已赚 {profit_pct:.0f}%, 暂无更好机会, 继续吃 Theta",
            detail=f"当前无 SIGNAL 级别新机会, 资金闲置无收益\n"
                   f"继续持有: 日均收 ${daily_theta_value:.1f}, 安全垫 {dist_to_strike:.1f}%\n"
                   f"但一旦新机会出现或赚到 75% → 果断平仓",
        )

    # === 赚 < 50% ===
    return TakeProfitAdvice(
        action="HOLD", urgency="LOW",
        reason=f"已赚 {profit_pct:.0f}%, 继续持有收租",
        detail=f"安全垫 {dist_to_strike:.1f}%, 日均 Theta ${daily_theta_value:.1f}\n"
               f"距离止盈目标还有空间, 继续持有",
    )


# ============================================================
#  3. Rolling 策略建议
# ============================================================
@dataclass
class RollAdvice:
    roll_type: str     # ROLL_OUT, ROLL_DOWN, ROLL_DOWN_OUT, NONE
    reason: str
    current: str       # 当前合约
    target: str        # 目标合约
    detail: str
    net_credit: float = 0  # 预估净收入 (正=收钱, 负=付钱)


def find_roll_targets(
    current_symbol: str,
    current_strike: float,
    current_mark: float,
    current_dte: float,
    spot: float,
    all_puts: list[dict],
    all_marks: dict,
    all_tickers: dict,
) -> list[RollAdvice]:
    """寻找最佳 Roll 目标"""
    advices = []

    current_bid = float(all_tickers.get(current_symbol, {}).get("bidPrice", current_mark))
    current_exp = current_symbol.split("-")[1] if "-" in current_symbol else ""

    candidates = []
    now = datetime.now(timezone.utc)

    for sym, contract in all_puts.items():
        if contract.get("side") != "PUT":
            continue
        if sym == current_symbol:
            continue

        mark = all_marks.get(sym, {})
        ticker = all_tickers.get(sym, {})

        strike = float(contract.get("strikePrice", 0))
        expiry_ts = contract.get("expiryDate", 0)
        expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
        dte = max((expiry - now).total_seconds() / 86400, 0.01)

        delta = float(mark.get("delta", 0))
        mark_iv = float(mark.get("markIV", 0))
        mark_price = float(mark.get("markPrice", 0))
        bid = float(ticker.get("bidPrice", 0))
        ask = float(ticker.get("askPrice", 0))

        otm_pct = (spot - strike) / spot * 100 if spot > 0 else 0

        # 筛选: 深度OTM, 有流动性, 合理的到期日
        if abs(delta) > 0.08:
            continue
        if otm_pct < 20:
            continue
        if dte < 20 or dte > 120:
            continue
        if bid < 30:
            continue
        if ask <= 0 or bid <= 0:
            continue
        spread_pct = (ask - bid) / mark_price * 100 if mark_price > 0 else 999
        if spread_pct > 15:
            continue

        exp_key = sym.split("-")[1]

        candidates.append({
            "symbol": sym,
            "strike": strike,
            "dte": dte,
            "delta": delta,
            "iv": mark_iv,
            "bid": bid,
            "ask": ask,
            "mark": mark_price,
            "otm_pct": otm_pct,
            "exp_key": exp_key,
        })

    if not candidates:
        return []

    # --- Roll Out: 同行权价, 更远到期日 ---
    roll_outs = [c for c in candidates
                 if abs(c["strike"] - current_strike) < 100  # 同行权价
                 and c["dte"] > current_dte + 14]  # 至少多14天
    if roll_outs:
        best = max(roll_outs, key=lambda c: c["bid"])
        net = best["bid"] - current_mark  # 买回当前 + 卖出新的
        advices.append(RollAdvice(
            roll_type="ROLL_OUT",
            reason="同行权价延期, 继续收租",
            current=current_symbol,
            target=best["symbol"],
            detail=f"平仓 {current_symbol} (~${current_mark:.0f}) → 卖 {best['symbol']} @ ${best['bid']:.0f}\n"
                   f"  新到期: {best['dte']:.0f}天 | OTM {best['otm_pct']:.0f}% | Delta {best['delta']:.4f}\n"
                   f"  净{'收入' if net > 0 else '支出'}: ${abs(net):.0f}",
            net_credit=net,
        ))

    # --- Roll Down: 同到期日, 更低行权价 ---
    roll_downs = [c for c in candidates
                  if c["exp_key"] == current_exp
                  and c["strike"] < current_strike - 1000]
    if roll_downs:
        best = max(roll_downs, key=lambda c: c["bid"])
        net = best["bid"] - current_mark
        advices.append(RollAdvice(
            roll_type="ROLL_DOWN",
            reason="降低行权价, 增加安全垫",
            current=current_symbol,
            target=best["symbol"],
            detail=f"平仓 {current_symbol} (~${current_mark:.0f}) → 卖 {best['symbol']} @ ${best['bid']:.0f}\n"
                   f"  行权价: ${current_strike:,.0f} → ${best['strike']:,.0f} (低 ${current_strike - best['strike']:,.0f})\n"
                   f"  安全垫: → {best['otm_pct']:.0f}%\n"
                   f"  净{'收入' if net > 0 else '支出'}: ${abs(net):.0f}",
            net_credit=net,
        ))

    # --- Roll Down & Out: 更低行权价 + 更远到期日 (最常用) ---
    roll_down_outs = [c for c in candidates
                      if c["strike"] < current_strike - 1000
                      and c["dte"] > current_dte + 7
                      and c["exp_key"] != current_exp]
    if roll_down_outs:
        # 按 bid 排序, 找权利金最厚的
        roll_down_outs.sort(key=lambda c: c["bid"], reverse=True)
        best = roll_down_outs[0]
        net = best["bid"] - current_mark
        advices.append(RollAdvice(
            roll_type="ROLL_DOWN_OUT",
            reason="降行权价 + 延期, 最佳防御性 Roll",
            current=current_symbol,
            target=best["symbol"],
            detail=f"平仓 {current_symbol} (~${current_mark:.0f}) → 卖 {best['symbol']} @ ${best['bid']:.0f}\n"
                   f"  行权价: ${current_strike:,.0f} → ${best['strike']:,.0f}\n"
                   f"  到期: {current_dte:.0f}天 → {best['dte']:.0f}天\n"
                   f"  安全垫: → {best['otm_pct']:.0f}% | Delta {best['delta']:.4f}\n"
                   f"  净{'收入' if net > 0 else '支出'}: ${abs(net):.0f}",
            net_credit=net,
        ))

    # 按净收入排序
    advices.sort(key=lambda a: a.net_credit, reverse=True)
    return advices


# ============================================================
#  4. 综合持仓优化分析
# ============================================================
def analyze_position_optimization(
    api: BinanceOptionsAPI,
    data: dict,
    opportunities: list[dict],
    iv_tracker_trend: str,
) -> dict:
    """
    综合分析: 止盈 + HV/IV + Rolling

    返回所有建议
    """
    vol_analyzer = VolatilityAnalyzer()

    spot = data["spot"]
    marks = data["marks"]
    tickers = data["tickers"]
    contracts = data["contracts"]

    # HV/IV 分析
    global_iv = 0
    iv_count = 0
    for m in marks.values():
        iv = float(m.get("markIV", 0))
        if iv > 0 and m["symbol"].startswith("BTC") and "-P" in m["symbol"]:
            global_iv += iv
            iv_count += 1
    mean_iv = global_iv / iv_count if iv_count > 0 else 0

    vol_analysis = vol_analyzer.get_full_analysis(mean_iv)

    # 获取持仓
    try:
        positions = api.get_position()
    except Exception as e:
        log.warning(f"获取持仓失败: {e}")
        positions = []

    active_positions = [p for p in positions if float(p.get("quantity", 0)) != 0]

    # 是否有新机会
    has_signal = any(r.get("signal") in ("SIGNAL", "STRONG") for r in opportunities)
    best_opp = opportunities[0] if opportunities else None

    results = []

    for pos in active_positions:
        qty = float(pos.get("quantity", 0))
        if qty >= 0 or "-P" not in pos["symbol"]:
            continue

        sym = pos["symbol"]
        entry = float(pos.get("entryPrice", 0))
        mark_price = float(pos.get("markPrice", 0))
        strike = float(pos.get("strikePrice", 0))
        expiry_ts = int(pos.get("expiryDate", 0))

        mark = marks.get(sym, {})
        delta = float(mark.get("delta", 0))
        iv = float(mark.get("markIV", 0))

        now = datetime.now(timezone.utc)
        expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
        dte = max((expiry - now).total_seconds() / 86400, 0.01)

        profit_pct = (entry - mark_price) / entry * 100 if entry > 0 else 0

        # 1. 止盈建议
        tp_advice = analyze_take_profit(
            symbol=sym,
            entry_price=entry,
            mark_price=mark_price,
            qty=abs(qty),
            dte=dte,
            delta=delta,
            iv=iv,
            spot=spot,
            strike=strike,
            has_signal_opportunities=has_signal,
            best_opportunity=best_opp,
            vol_analysis=vol_analysis,
            iv_trend=iv_tracker_trend,
        )

        # 2. Roll 建议
        roll_advices = find_roll_targets(
            current_symbol=sym,
            current_strike=strike,
            current_mark=mark_price,
            current_dte=dte,
            spot=spot,
            all_puts=contracts,
            all_marks=marks,
            all_tickers=tickers,
        )

        results.append({
            "symbol": sym,
            "entry": entry,
            "mark": mark_price,
            "qty": qty,
            "strike": strike,
            "dte": dte,
            "delta": delta,
            "iv": iv,
            "profit_pct": round(profit_pct, 1),
            "pnl": round((entry - mark_price) * abs(qty), 2),
            "take_profit": tp_advice,
            "roll_options": roll_advices,
        })

    return {
        "vol_analysis": vol_analysis,
        "positions": results,
        "has_opportunities": has_signal,
    }


# ============================================================
#  5. TG 消息格式化
# ============================================================
def format_profit_report(analysis: dict) -> str:
    """格式化收益优化报告"""
    lines = ["💰 <b>收益优化报告</b>", ""]

    # HV vs IV
    va = analysis["vol_analysis"]
    edge_icon = {"STRONG": "🟢", "MODERATE": "🟡", "SLIGHT": "⚪", "NONE": "🔴"}.get(va["edge"], "")

    lines.append("<b>📊 波动率分析 (卖方Alpha)</b>")
    lines.append(f"  HV10: {va['hv_10']:.3f} | HV20: {va['hv_20']:.3f} | HV30: {va['hv_30']:.3f}")
    lines.append(f"  IV均值: {va['iv']:.3f}")
    lines.append(f"  {edge_icon} <b>IV/HV = {va['iv_hv_ratio']:.2f}x</b> ({va['edge_desc']})")
    lines.append(f"  HV趋势: {va['hv_trend_desc']} ({va['hv_trend']:+.0f}%)")
    lines.append("")

    # 逐持仓分析
    for p in analysis["positions"]:
        tp = p["take_profit"]
        lines.append(f"{'━' * 35}")
        lines.append(f"<b>{tp.icon} {p['symbol']}</b>")
        lines.append(f"  入场: ${p['entry']:,.0f} → 当前: ${p['mark']:,.0f}")
        lines.append(f"  盈利: <b>${p['pnl']:+,.0f} ({p['profit_pct']:+.0f}%)</b>")
        lines.append(f"  DTE: {p['dte']:.0f}天 | Delta: {p['delta']:.4f}")
        lines.append("")

        # 止盈建议
        urgency_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(tp.urgency, "")
        action_cn = {"HOLD": "继续持有", "CLOSE": "建议平仓", "CLOSE_AND_ROLL": "平仓 + Roll"}.get(tp.action, "")
        lines.append(f"  {urgency_icon} <b>建议: {action_cn}</b>")
        lines.append(f"  {tp.reason}")
        for detail_line in tp.detail.split("\n"):
            lines.append(f"  {detail_line}")
        lines.append("")

        # Roll 建议
        if p["roll_options"]:
            lines.append("  <b>Roll 选择:</b>")
            for i, roll in enumerate(p["roll_options"][:3], 1):
                type_cn = {
                    "ROLL_OUT": "延期", "ROLL_DOWN": "降行权价",
                    "ROLL_DOWN_OUT": "降价+延期",
                }.get(roll.roll_type, "")
                credit_icon = "💵" if roll.net_credit > 0 else "💸"
                lines.append(f"  {i}. [{type_cn}] → {roll.target}")
                lines.append(f"     {credit_icon} 净{'收入' if roll.net_credit > 0 else '支出'} ${abs(roll.net_credit):,.0f}")
                for detail_line in roll.detail.split("\n"):
                    lines.append(f"     {detail_line}")
                lines.append("")

    return "\n".join(lines)
