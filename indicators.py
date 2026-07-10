"""
符号化指标体系 (R4-2)

统一转换规则: 把裸数字翻译成 🟢/🟡/🔴 + 人话。
所有推送复用, /top 详细表格不受影响。

覆盖维度:
  总分 → 等级 + 进度条
  IV Rank → 红绿灯 + 人话
  IV/HV → 卖方优势判断
  安全垫 → 距离感描述
  P(ITM) → 胜率翻译
  流动性 → spread + OI 评价
  事件风险 → 事件提示
  保证金影响 → 占用比例
"""


# ============================================================
#  总分 → 等级 + 进度条
# ============================================================
def score_grade(score: float) -> tuple[str, str, str]:
    """
    总分 → (等级字母, 进度条, 等级描述)

    Returns:
        ("A", "▰▰▰▰▱", "强烈推荐")
    """
    if score >= 80:
        return ("A", "▰▰▰▰▱", "强烈推荐")
    elif score >= 70:
        return ("B", "▰▰▰▱▱", "推荐")
    elif score >= 60:
        return ("C", "▰▰▱▱▱", "可关注")
    else:
        return ("D", "▰▱▱▱▱", "一般")


# ============================================================
#  IV Rank
# ============================================================
def iv_rank_indicator(rank: float, prev_rank: float = None) -> str:
    """
    IV Rank → 红绿灯 + 人话

    Args:
        rank: 当前 IV Rank (0-100)
        prev_rank: 昨日 IV Rank (用于显示方向)
    """
    if rank >= 70:
        light = "🟢"
        desc = "高位"
    elif rank >= 40:
        light = "🟡"
        desc = "中性"
    else:
        light = "🔴"
        desc = "低位"

    direction = ""
    if prev_rank is not None and abs(rank - prev_rank) >= 3:
        direction = f" ↑ (昨日 {prev_rank:.0f})" if rank > prev_rank else f" ↓ (昨日 {prev_rank:.0f})"

    return f"{light} IV Rank {rank:.0f}{direction} ({desc})"


# ============================================================
#  IV/HV
# ============================================================
def iv_hv_indicator(ratio: float) -> str:
    """IV/HV → 卖方优势判断"""
    if ratio >= 1.5:
        return f"🟢 IV/HV {ratio:.2f} — 卖方优势明确"
    elif ratio >= 1.25:
        return f"🟢 IV/HV {ratio:.2f} — 卖方有优势"
    elif ratio >= 1.0:
        return f"🟡 IV/HV {ratio:.2f} — 优势一般"
    else:
        return f"🔴 IV/HV {ratio:.2f} — 卖方无优势, 慎卖"


# ============================================================
#  安全垫
# ============================================================
def safety_indicator(safety_pct: float, p_itm: float) -> str:
    """安全垫 + P(ITM) → 距离感"""
    if safety_pct >= 20:
        light = "🟢"
    elif safety_pct >= 12:
        light = "🟡"
    else:
        light = "🔴"

    return f"{light} 安全垫 {safety_pct:.1f}% · P(ITM) {p_itm:.1f}%"


# ============================================================
#  流动性
# ============================================================
def liquidity_indicator(spread_pct: float, oi: float) -> str:
    """流动性评价"""
    if spread_pct <= 1.0 and oi >= 50:
        return "🟢 流动性好 (spread {:.1f}%)".format(spread_pct)
    elif spread_pct <= 3.0:
        return "🟡 流动性一般 (spread {:.1f}%)".format(spread_pct)
    else:
        return "🔴 流动性差 (spread {:.1f}%), 注意滑点".format(spread_pct)


# ============================================================
#  事件风险
# ============================================================
def event_indicator(event_descs: list) -> str:
    """事件风险提示"""
    if not event_descs:
        return ""
    # 取最近的事件
    first = event_descs[0] if isinstance(event_descs[0], str) else str(event_descs[0])
    # 清理前缀
    first = first.lstrip("⚠️ ").strip()
    count = len(event_descs)
    if count == 1:
        return f"🟡 跨 {first} · 已计入扣分"
    else:
        return f"🟡 跨 {first} 等 {count} 事件 · 已计入扣分"


# ============================================================
#  保证金影响
# ============================================================
def margin_indicator(new_margin_usage_pct: float) -> str:
    """新开仓后保证金占用"""
    if new_margin_usage_pct >= 60:
        return f"🔴 保证金率将达 {new_margin_usage_pct:.0f}% (接近上限)"
    elif new_margin_usage_pct >= 40:
        return f"🟡 保证金率将达 {new_margin_usage_pct:.0f}%"
    else:
        return f"🟢 保证金率 {new_margin_usage_pct:.0f}% (宽裕)"


# ============================================================
#  持仓浮亏
# ============================================================
def pnl_indicator(pnl: float, loss_ratio: float, entry: float) -> str:
    """持仓浮亏描述"""
    if loss_ratio <= 0:
        return f"🟢 浮盈 ${abs(pnl):,.0f}"
    elif loss_ratio < 1.0:
        return f"🟡 浮亏 ${abs(pnl):,.0f} ({loss_ratio:.1f}x 权利金)"
    elif loss_ratio < 2.0:
        return f"🔴 浮亏 ${abs(pnl):,.0f} ({loss_ratio:.1f}x 权利金)"
    else:
        return f"🔴🔴 浮亏 ${abs(pnl):,.0f} ({loss_ratio:.1f}x 权利金)"


# ============================================================
#  距行权距离 (持仓用)
# ============================================================
def dist_strike_indicator(dist_pct: float) -> str:
    """距行权价距离"""
    if dist_pct >= 20:
        return f"🟢 距行权 {dist_pct:.1f}%"
    elif dist_pct >= 10:
        return f"🟡 距行权 {dist_pct:.1f}%"
    elif dist_pct >= 5:
        return f"🔴 距行权仅 {dist_pct:.1f}%"
    else:
        return f"🔴🔴 距行权仅 {dist_pct:.1f}%!"


# ============================================================
#  Theta 翻译
# ============================================================
def theta_indicator(theta: float, qty: float) -> str:
    """Theta → 日收/日亏"""
    daily = abs(theta * qty)
    if qty < 0:  # Short: theta 是正收入
        return f"日收 ${daily:,.0f}"
    else:
        return f"日亏 ${daily:,.0f}"
