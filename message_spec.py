"""
消息三层规格 (R4-1)

统一消息构建器: verdict → evidence → playbook
所有推送走此构建器, /top 和 /risk 完整版保留原格式。

三层含义:
  verdict (结论层): 一句话说清"什么事 + 该做什么", 加粗, 最顶
  evidence (依据层): 3-5 行关键指标, 每行一个维度, 符号化
  playbook (操作层): 结构化操作指令
"""


def build_message(verdict: str, evidence: list[str],
                  playbook: str = "") -> str:
    """
    构建三层结构消息 (TG HTML)

    Args:
        verdict: 结论 (1-2 行, 会被加粗)
        evidence: 依据行列表 (每行一个维度, 不超过 5 行)
        playbook: 操作卡 HTML (可选, 已格式化)

    Returns:
        TG HTML 字符串
    """
    lines = []

    # --- 结论层 ---
    lines.append(verdict)
    lines.append("")

    # --- 依据层 ---
    for ev in evidence[:5]:
        lines.append(ev)

    # --- 操作层 ---
    if playbook:
        lines.append("")
        lines.append(playbook)

    return "\n".join(lines)


def build_opportunity_message(
    symbol: str,
    score: float,
    grade: str,
    bar: str,
    qty: int,
    limit_price: float,
    bid: float,
    mid: float,
    total_premium: float,
    margin_per: float,
    margin_pct_after: float,
    evidence_lines: list[str],
    playbook_text: str,
) -> str:
    """
    构建机会推送消息 (严格匹配 mockup)

    Mockup:
    🔥 卖出机会 A级 · 27JUN-72000P

    💰 卖 3 张 @ limit $310 (bid 305 / mid 312)
       预期收 $930 · 占用保证金 $9,870 (至41%)

    评分 82 ▰▰▰▰▱  ...evidence...

    📋 操作卡
      ...
    """
    short_sym = symbol.split("BTC-")[-1]
    icon = "🔥" if grade in ("A", "B") else "📋"

    verdict = (
        f"{icon} <b>卖出机会 {grade}级 · {short_sym}</b>\n\n"
        f"💰 卖 {qty} 张 @ limit ${limit_price:,.0f} (bid {bid:,.0f} / mid {mid:,.0f})\n"
        f"   预期收 ${total_premium:,.0f} · 占用保证金 ${margin_per * qty:,.0f} (至{margin_pct_after:.0f}%)"
    )

    # 第一行 evidence 是评分行
    ev_lines = [f"评分 {score:.0f} {bar}  {evidence_lines[0]}"] + evidence_lines[1:]

    return build_message(verdict, ev_lines, playbook_text)


def build_position_alert_message(
    symbol: str,
    level: str,
    pnl_line: str,
    cause_line: str,
    evidence_lines: list[str],
    playbook_text: str,
) -> str:
    """
    构建持仓预警消息 (严格匹配 mockup)

    Mockup:
    🔴 持仓危险 · 30MAY-78000P

    现在: 浮亏 $612 (-1.9x权利金) · 距行权仅 8.4%
    原因: BTC 4h 内 -3.2% → delta 升至 0.34

    📋 三选一 (按优先级)
      ...
    """
    short_sym = symbol.split("BTC-")[-1]

    level_map = {
        "CRITICAL": ("🔴🔴", "持仓紧急"),
        "DANGER": ("🔴", "持仓危险"),
        "WARNING": ("⚠️", "持仓关注"),
    }
    icon, desc = level_map.get(level, ("⚠️", "持仓提醒"))

    verdict = (
        f"{icon} <b>{desc} · {short_sym}</b>\n\n"
        f"现在: {pnl_line}\n"
        f"原因: {cause_line}"
    )

    return build_message(verdict, evidence_lines, playbook_text)


def build_risk_alert_message(alerts: list, playbook_text: str = "") -> str:
    """
    构建风控告警消息

    Args:
        alerts: RiskAlert 列表
        playbook_text: 操作卡 (可选)
    """
    if not alerts:
        return ""

    # 取最高级别
    level_order = {"CRITICAL": 3, "DANGER": 2, "WARNING": 1}
    top = max(alerts, key=lambda a: level_order.get(a.level, 0))

    level_map = {
        "CRITICAL": ("🚨", "紧急风控"),
        "DANGER": ("🔴", "风控告警"),
        "WARNING": ("⚠️", "风控提醒"),
    }
    icon, desc = level_map.get(top.level, ("⚠️", "风控提醒"))

    verdict = f"{icon} <b>{desc}: {top.title}</b>"

    evidence = []
    for a in alerts[:5]:
        prefix = {"CRITICAL": "🔴", "DANGER": "🟡", "WARNING": "⚪"}.get(a.level, "")
        short_sym = a.symbol.split("BTC-")[-1] if a.symbol != "BTC" else "BTC"
        evidence.append(f"{prefix} [{short_sym}] {a.title}")
        if a.detail:
            # 取 detail 第一行
            first_line = a.detail.split("\n")[0]
            evidence.append(f"   {first_line}")

    return build_message(verdict, evidence[:5], playbook_text)
