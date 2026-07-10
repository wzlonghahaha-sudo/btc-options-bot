"""
Playbook 引擎 (R4-3)

把模糊的 RiskAlert.action 替换为结构化决策卡:
  - 每个 DANGER/CRITICAL 告警输出 2-3 个按优先级排序的备选动作
  - 所有价格给出可直接下单的 limit 参考价
  - 每个操作卡必须含 deadline/失效条件

机会推送的操作卡包含:
  - 开仓 limit 价、止损触发价、滚仓触发线、追价失效线
"""

from dataclasses import dataclass, field


@dataclass
class PlaybookAction:
    """单个操作选项"""
    label: str            # "滚仓" / "买回止损" / "持有观察"
    instruction: str      # 含具体价格与数量
    params: dict = field(default_factory=dict)
    condition: str = ""   # 适用前提
    deadline: str = ""    # 时效/失效条件


def calc_limit_price(bid: float, ask: float, side: str,
                     buffer_pct: float = 0.25) -> float:
    """
    计算可直接下单的 limit 参考价

    Args:
        bid, ask: 当前 bid/ask
        side: "BUY" (买回平仓) 或 "SELL" (开新仓)
        buffer_pct: spread 的百分比作为缓冲 (默认 25%)

    Returns:
        limit 参考价
    """
    spread = ask - bid
    buffer = spread * buffer_pct

    if side == "BUY":
        # 买回: ask + 缓冲 (确保成交)
        return round(ask + buffer, 2)
    else:
        # 开仓: bid - 缓冲 (稍低于 bid, 等一等)
        return round(max(bid - buffer, 0.01), 2)


def build_opportunity_playbook(
    symbol: str,
    qty: int,
    bid: float,
    ask: float,
    entry_price: float,
    stop_price: float,
    spot: float,
    strike: float,
    safety_pct: float,
) -> str:
    """
    构建机会推送的操作卡

    Returns:
        TG HTML 格式化的操作卡
    """
    limit = calc_limit_price(bid, ask, "SELL")
    short_sym = symbol.split("BTC-")[-1]

    # 追价失效线: BTC 跌破 (现价 - 安全垫的一半) 时不追
    invalidation = spot * (1 - safety_pct / 100 / 2)

    lines = [
        "📋 <b>操作卡</b>",
        f"  开仓: SELL {qty}x @ ≥${limit:,.0f} limit",
        f"  止损: 期权价 ≥${stop_price:,.0f} 且距行权 &lt;15% → 买回",
        f"  滚仓线: delta &gt;0.30 → 触发 roll 建议",
        f"  失效: BTC 跌破 ${invalidation:,.0f} 前不追价",
    ]

    return "\n".join(lines)


def build_position_playbook(
    symbol: str,
    qty: float,
    entry: float,
    mark: float,
    bid: float,
    ask: float,
    spot: float,
    strike: float,
    dist_pct: float,
    loss_ratio: float,
    roll_candidates: list = None,
) -> str:
    """
    构建持仓预警的操作卡 (2-3 个备选方案)

    Returns:
        TG HTML 格式化的操作卡
    """
    actions = []
    short_sym = symbol.split("BTC-")[-1]

    # === 方案 A: 滚仓 (如果有候选) ===
    if roll_candidates:
        best = roll_candidates[0]
        roll_sym = best["symbol"].split("BTC-")[-1]
        net_str = f"净收 ${best['net_credit']:,.0f}" if best["net_credit"] >= 0 else f"净付 ${abs(best['net_credit']):,.0f}"
        actions.append(PlaybookAction(
            label="滚仓",
            instruction=f"→ {roll_sym}: {net_str}, 安全垫回到 {best['new_safety_pct']:.0f}%",
            params={"target": best["symbol"], "net_credit": best["net_credit"]},
            condition="",
            deadline=f"BTC 再跌 2% (${spot * 0.98:,.0f}) 后净收可能变负",
        ))

    # === 方案 B: 止损买回 ===
    buyback_limit = calc_limit_price(bid, ask, "BUY")
    actions.append(PlaybookAction(
        label="买回止损",
        instruction=f"@ ≤${buyback_limit:,.0f} (当前 ask ${ask:,.0f})",
        params={"price": buyback_limit, "qty": abs(qty)},
        condition="",
        deadline="",
    ))

    # === 方案 C: 持有观察 ===
    hold_condition = ""
    if dist_pct >= 10:
        support = spot * 0.95
        hold_condition = f"仅当你判断 ${support:,.0f} 支撑不破"
    elif dist_pct >= 5:
        hold_condition = "高风险, 仅适合有对冲的情况"
    else:
        hold_condition = "极高风险, 不建议"

    actions.append(PlaybookAction(
        label="持有观察",
        instruction="不操作, 继续监控",
        condition=hold_condition,
        deadline=f"若 BTC 再跌 2% (${spot * 0.98:,.0f}), 滚仓方案失效, 建议 30 分钟内决策" if roll_candidates else "",
    ))

    # === 格式化 ===
    n = len(actions)
    lines = [f"📋 <b>{'三' if n >= 3 else '两'}选一 (按优先级)</b>"]
    labels = "ABCDE"
    for i, a in enumerate(actions[:3]):
        prefix = f"  {labels[i]}."
        lines.append(f"{prefix} <b>{a.label}</b> {a.instruction}")
        if a.condition:
            lines.append(f"     — {a.condition}")

    # 时效提醒 (取第一个有 deadline 的)
    for a in actions:
        if a.deadline:
            lines.append(f"  ⏰ {a.deadline}")
            break

    return "\n".join(lines)


def build_risk_playbook(alerts: list, spot: float) -> str:
    """
    构建风控告警的简化操作提示

    Args:
        alerts: RiskAlert 列表
        spot: BTC 现价
    """
    if not alerts:
        return ""

    top = max(alerts, key=lambda a: {"CRITICAL": 3, "DANGER": 2, "WARNING": 1}.get(a.level, 0))

    if top.level == "CRITICAL":
        return (
            "📋 <b>立即行动</b>\n"
            f"  1. 检查所有持仓, 距行权 &lt;5% 的立即平仓\n"
            f"  2. 确认保证金余额, 必要时追加\n"
            f"  3. /risk 查看完整风控报告"
        )
    elif top.level == "DANGER":
        return (
            "📋 <b>建议操作</b>\n"
            f"  1. /positions 检查持仓盈亏\n"
            f"  2. 设置止损单 (见各合约止损价)\n"
            f"  3. /hedge 查看对冲建议"
        )
    else:
        return (
            "📋 <b>关注要点</b>\n"
            f"  留意 BTC 走势, /risk 查看详情"
        )
