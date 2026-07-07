"""
Roll Advisor — 卖方滚仓建议

当持仓被威胁时 (|delta| > 0.30 或距行权 < 10%),
在当前期权链中搜索候选: 更低行权价 × 更远到期日。

筛选条件:
  (a) 新仓 |delta| ≤ 0.20
  (b) 净权利金 ≥ 0 (net credit) 优先,
      允许净 debit ≤ 原收权利金 30% 的方案作为次选

仅建议, 不下单。
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# 触发滚仓建议的阈值
ROLL_DELTA_TRIGGER = 0.30    # |delta| > 此值触发
ROLL_DIST_TRIGGER = 10.0     # 距行权 < 此值(%) 触发
ROLL_NEW_DELTA_MAX = 0.20    # 新仓 |delta| 上限
ROLL_MAX_DEBIT_PCT = 0.30    # 最大净 debit 占原权利金比例


def should_trigger_roll(abs_delta: float, dist_to_strike_pct: float) -> bool:
    """判断是否应触发滚仓建议"""
    return abs_delta > ROLL_DELTA_TRIGGER or dist_to_strike_pct < ROLL_DIST_TRIGGER


def find_roll_candidates(
    current_symbol: str,
    current_strike: float,
    current_entry_price: float,
    current_mark_price: float,
    current_dte: float,
    spot: float,
    option_chain: list[dict],
) -> list[dict]:
    """
    在期权链中搜索滚仓候选

    Args:
        current_symbol: 当前持仓合约
        current_strike: 当前行权价
        current_entry_price: 原始开仓收到的权利金
        current_mark_price: 当前标记价 (买回成本)
        current_dte: 当前剩余天数
        spot: BTC 现价
        option_chain: 可用的 Put 合约列表, 每个含:
            {symbol, strike, dte, bid, ask, delta, mark_price, iv}

    Returns:
        最多 3 个方案, 按净权利金从优到劣排序:
        [{
            "symbol": str,
            "strike": float,
            "dte": float,
            "delta": float,
            "bid": float,
            "net_credit": float,      # 正=净收入, 负=净支出
            "new_safety_pct": float,
            "margin_change_est": float,
            "type": "credit" | "debit",
            "detail": str,
        }]
    """
    candidates = []
    buyback_cost = current_mark_price  # 买回当前仓位的成本

    for c in option_chain:
        sym = c.get("symbol", "")
        if sym == current_symbol:
            continue

        strike = c.get("strike", 0)
        dte = c.get("dte", 0)
        delta = abs(c.get("delta", 0))
        bid = c.get("bid", 0)
        ask = c.get("ask", 0)

        # 筛选: 更低行权价, 更远到期
        if strike >= current_strike:
            continue
        if dte <= current_dte + 7:  # 至少多 7 天
            continue
        # 新仓 delta 不超过上限
        if delta > ROLL_NEW_DELTA_MAX:
            continue
        # 流动性
        if bid <= 0 or ask <= 0:
            continue
        # 合理 DTE
        if dte < 14 or dte > 120:
            continue

        # 计算净权利金: 卖出新仓 bid - 买回旧仓 ask
        net_credit = bid - buyback_cost
        # 检查 debit 是否在允许范围
        if net_credit < 0:
            debit = abs(net_credit)
            if current_entry_price > 0 and debit > current_entry_price * ROLL_MAX_DEBIT_PCT:
                continue  # debit 超出允许范围

        new_safety_pct = (spot - strike) / spot * 100 if spot > 0 else 0

        candidates.append({
            "symbol": sym,
            "strike": strike,
            "dte": dte,
            "delta": c.get("delta", 0),
            "bid": bid,
            "net_credit": round(net_credit, 2),
            "new_safety_pct": round(new_safety_pct, 1),
            "margin_change_est": 0,  # 简化: 不精确估算保证金变化
            "type": "credit" if net_credit >= 0 else "debit",
        })

    # 排序: net credit 从大到小
    candidates.sort(key=lambda c: c["net_credit"], reverse=True)

    # 取前 3 个, 生成 detail
    results = []
    for c in candidates[:3]:
        short_sym = c["symbol"].split("BTC-")[-1]
        current_short = current_symbol.split("BTC-")[-1]
        credit_str = f"净收入 ${c['net_credit']:+,.0f}" if c['net_credit'] >= 0 else f"净支出 ${abs(c['net_credit']):,.0f}"
        c["detail"] = (
            f"平仓 {current_short} (~${buyback_cost:,.0f}) → "
            f"卖 {short_sym} @ ${c['bid']:,.0f}\n"
            f"  行权价: ${current_strike:,.0f} → ${c['strike']:,.0f}\n"
            f"  到期: {current_dte:.0f}天 → {c['dte']:.0f}天\n"
            f"  安全垫: → {c['new_safety_pct']:.0f}%  |  Delta {c['delta']:.4f}\n"
            f"  {credit_str}"
        )
        results.append(c)

    return results


def format_roll_advice(current_symbol: str, candidates: list[dict]) -> str:
    """格式化滚仓建议 (TG HTML)"""
    short_sym = current_symbol.split("BTC-")[-1]
    if not candidates:
        return (
            f"🔄 <b>滚仓方案 ({short_sym})</b>\n\n"
            f"未找到满足条件的滚仓候选\n"
            f"(需要: 更低行权价 + 更远到期 + |delta| ≤ {ROLL_NEW_DELTA_MAX})\n\n"
            f"⚠️ 此时止损/买保护是仅剩选项"
        )

    lines = [f"🔄 <b>滚仓方案 ({short_sym})</b>", ""]
    for i, c in enumerate(candidates, 1):
        type_icon = "💵" if c["type"] == "credit" else "💸"
        lines.append(f"  {i}. {type_icon} {c['detail']}")
        lines.append("")

    return "\n".join(lines)
