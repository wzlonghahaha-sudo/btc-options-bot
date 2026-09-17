"""
用户可见文案 — 单一事实来源

所有 /help /rules /strategy /now /status 数字从
ScanConfig / risk_rules / push_control 实时读取, 避免口口径不一致。
"""

from __future__ import annotations

from typing import Optional


def _live_thresholds() -> dict:
    """从代码配置读取当前门槛"""
    from opportunity_scanner import ScanConfig
    from risk_rules import (
        LOSS_WARN_RATIO, LOSS_DANGER_RATIO, LOSS_CRITICAL_RATIO,
        DIST_WARN_PCT, DIST_SAFE_PCT, DELTA_HIGH,
    )
    from push_control import MAX_SIGNAL_PUSH_PER_DAY, MIN_PUSH_SCORE

    cfg = ScanConfig()
    return {
        "score_signal": cfg.SCORE_SIGNAL,
        "score_strong": cfg.SCORE_STRONG,
        "score_push": cfg.SCORE_PUSH,
        "dte_min": cfg.DTE_MIN,
        "dte_max": cfg.DTE_MAX,
        "min_bid": cfg.MIN_BID,
        "max_spread": cfg.MAX_SPREAD_PCT,
        "tiers": cfg.TIERS,
        "loss_warn": LOSS_WARN_RATIO,
        "loss_danger": LOSS_DANGER_RATIO,
        "loss_critical": LOSS_CRITICAL_RATIO,
        "dist_warn": DIST_WARN_PCT,
        "dist_safe": DIST_SAFE_PCT,
        "delta_high": DELTA_HIGH,
        "daily_push_limit": MAX_SIGNAL_PUSH_PER_DAY,
        "min_push_score": MIN_PUSH_SCORE,
    }


def help_msg(mode: str = "hunt") -> str:
    """分层帮助: 日常命令优先, 研究/设置折叠"""
    t = _live_thresholds()
    mode_label = {
        "hunt": "🎯 猎机",
        "hold": "🛡️ 持仓",
        "quiet": "🤫 安静",
    }.get(mode, mode)

    return (
        "🤖 <b>BTC OTM Put 监控</b>\n"
        f"当前模式: <b>{mode_label}</b>  (/mode)\n\n"

        "<b>日常决策</b>\n"
        "/now — 一屏决策首页 (推荐)\n"
        "/positions — 持仓 + 挂单\n"
        "/top — 可开仓机会 (默认仅可开)\n"
        "/risk — 风控报告\n"
        "/hedge — 对冲方案\n"
        "/scan — 立即扫描并刷新 /now\n\n"

        "<b>研究</b>\n"
        "/iv /map /payoff /profit /ai /overview\n"
        "/top70 /top80 /top all — 按分数或看全部\n"
        "/perf /journal /calibration\n\n"

        "<b>设置</b>\n"
        "/mode hunt|hold|quiet — 推送情境\n"
        "/config /set — 调参\n"
        "/strategy /rules — 策略说明\n"
        "/help — 本帮助\n\n"

        "<b>自动推送</b>\n"
        f"• ≥{t['score_push']} 分且可开仓: 详情推送 "
        f"(日限 {t['daily_push_limit']} 条)\n"
        f"• &lt;{t['score_push']} 分: 不推送, /top 自查\n"
        "• 持仓/风控告警: 即时 (可确认/静音)\n"
        "• 每日日报 + 概览定时推送\n\n"
        "💡 命令请在私聊使用; 群组主要接收推送。"
    )


def strategy_msg() -> str:
    """短版策略说明 (30 秒读完)"""
    t = _live_thresholds()
    bal = t["tiers"]["balanced"]
    return (
        "📖 <b>策略说明 (30秒版)</b>\n\n"
        "<b>一句话:</b> 卖 BTC「崩盘保险」(OTM Put), 收权利金; "
        "靠时间衰减赚钱, 怕的是暴跌穿行权价。\n\n"
        "<b>什么时候出手?</b>\n"
        f"• 评分 ≥{t['score_push']} 且账户可开仓才主动推\n"
        f"• 偏好约 {bal['otm_min']:.0f}%+ 虚值、IV 偏贵、流动性尚可\n"
        "• 大部分时间安静 = 没好机会 = 正确\n\n"
        "<b>风险:</b> BTC 暴跌 → 浮亏放大 → 可能强平\n"
        f"浮亏 {t['loss_warn']:.0f}x/{t['loss_danger']:.0f}x/"
        f"{t['loss_critical']:.0f}x 权利金会升级告警; "
        f"距行权 &lt;{t['dist_warn']:.0f}% 或 delta&gt;{t['delta_high']:.2f} "
        "才按级别当真止损。\n\n"
        "👉 /rules 看具体数字  ·  /now 看当前该做什么"
    )


def rules_msg() -> str:
    """入场/风控规则 — 与代码同步"""
    t = _live_thresholds()
    tiers = t["tiers"]

    lines = [
        "📏 <b>入场规则 & 风控</b> (与代码同步)",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "<b>三档机会</b>",
    ]
    for key in ("conservative", "balanced", "aggressive"):
        c = tiers[key]
        lines.append(
            f"{c['label']}: |δ|≤{c['delta_max']:.2f}, "
            f"OTM≥{c['otm_min']:.0f}%, 安全垫≥{c['safety_min']:.0f}% — {c['desc']}"
        )

    lines += [
        "",
        "<b>通用硬门槛</b>",
        f"• 到期 {t['dte_min']}–{t['dte_max']} 天 · Bid≥${t['min_bid']:.0f} · "
        f"Spread≤{t['max_spread']:.0f}%",
        "",
        "<b>信号等级</b>",
        f"• &lt;{t['score_signal']}: 不展示为可入场",
        f"• {t['score_signal']}–{t['score_strong'] - 1}: 可入场 (仅 /top)",
        f"• ≥{t['score_strong']}: 强信号",
        f"• ≥{t['score_push']}: 自动推送详情 (日限 {t['daily_push_limit']} 条, "
        f"且 score≥{t['min_push_score']})",
        "",
        "<b>复合止损</b>",
        f"• 浮亏 ≥{t['loss_warn']:.0f}x / {t['loss_danger']:.0f}x / "
        f"{t['loss_critical']:.0f}x 权利金 → 警告/危险/紧急",
        f"• 且 (距行权 &lt;{t['dist_warn']:.0f}% 或 |δ|&gt;{t['delta_high']:.2f}) "
        "→ 按该级别处理",
        f"• 距行权仍 &gt;{t['dist_safe']:.0f}% 且方向风险低 → 降一级 (多半 IV 波动)",
        "",
        "👉 /now 看当前状态",
    ]
    return "\n".join(lines)


def status_msg(
    spot: float,
    scan_count: int,
    uptime_str: str,
    last_scan_time: float,
    interval: int,
    push_status: Optional[dict] = None,
    mode: str = "hunt",
) -> str:
    """运行状态 + 推送预算"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    lines = [
        f"🤖 <b>Bot 状态</b>  {now}",
        "",
        f"BTC: ${spot:,.2f}",
        f"模式: {mode}",
        f"扫描: {scan_count} 次 · 间隔 {interval}s · 上次 {last_scan_time:.1f}s",
        f"运行: {uptime_str}",
    ]
    if push_status:
        used = push_status.get("daily_signal_count", 0)
        limit = push_status.get("daily_limit", 0)
        suppressed = push_status.get("suppressed_today", 0)
        lines.append(f"今日机会推送: {used}/{limit}")
        if suppressed:
            lines.append(f"今日被限额压制: {suppressed} 条 → /top")
    lines.append("")
    lines.append("👉 /now 看决策首页")
    return "\n".join(lines)


def build_now_message(snap: dict) -> str:
    """
    一屏决策首页。

    snap 字段:
      spot, change_pct, mode, ready,
      pos_count, total_pnl, nearest_dist, worst_alert,
      liq_drop_pct, best_opp (dict|None),
      push_used, push_limit, push_suppressed,
      next_action (str)
    """
    if not snap.get("ready"):
        return (
            "⏳ <b>决策首页</b>\n\n"
            "首次扫描尚未完成, 通常 30–60 秒。\n"
            "完成后会自动可用; 也可发 /scan 强制刷新。"
        )

    spot = snap.get("spot") or 0
    change = snap.get("change_pct")
    if change is None:
        chg = "—"
    else:
        sign = "+" if change >= 0 else ""
        chg = f"{sign}{change:.1f}%"

    lines = [
        f"📍 <b>现在</b>  BTC ${spot:,.0f} ({chg})",
        f"模式: {snap.get('mode', 'hunt')}",
        "",
    ]

    # 持仓块
    pos_count = snap.get("pos_count", 0)
    if pos_count <= 0:
        lines.append("持仓: 空仓")
    else:
        pnl = snap.get("total_pnl", 0)
        nearest = snap.get("nearest_dist")
        worst = snap.get("worst_alert") or "OK"
        alert_icon = {
            "CRITICAL": "🔴🔴", "DANGER": "🔴", "WARNING": "⚠️",
            "WATCH": "👀", "OK": "✅",
        }.get(worst, "•")
        dist_txt = f"{nearest:.1f}%" if nearest is not None else "—"
        lines.append(
            f"持仓: {pos_count} 个 · 浮盈 <b>${pnl:+,.0f}</b> · "
            f"最近行权 {dist_txt} · {alert_icon}{worst}"
        )

    liq = snap.get("liq_drop_pct")
    if liq is not None and liq != 0:
        lines.append(f"强平距离: 跌 {abs(liq):.0f}%")

    lines.append("")

    # 机会
    best = snap.get("best_opp")
    if best:
        short = best["symbol"].split("BTC-")[-1] if "BTC-" in best["symbol"] else best["symbol"]
        lines.append(
            f"机会: <b>{short}</b> 评分 {best['score']:.0f} · "
            f"Bid ${best['bid']:,.0f} · 安全垫 {best['safety_pct']:.0f}%"
        )
        lines.append("→ /top 看可开清单")
    else:
        push = snap.get("score_push", 78)
        lines.append(f"机会: 暂无 ≥{push} 分可开仓 → /top 浏览较低分")

    # 推送预算
    used = snap.get("push_used", 0)
    limit = snap.get("push_limit", 5)
    suppressed = snap.get("push_suppressed", 0)
    budget = f"推送预算 {used}/{limit}"
    if suppressed:
        budget += f" · 压制 {suppressed}"
    lines.append(budget)

    lines.append("")
    action = snap.get("next_action") or "继续观察"
    lines.append(f"👉 <b>下一步:</b> {action}")

    return "\n".join(lines)


def order_copy_text(symbol: str, qty: int, limit: float, bid: float,
                    side: str = "SELL") -> str:
    """一键复制的下单要点 (纯文本友好)"""
    short = symbol.split("BTC-")[-1] if "BTC-" in symbol else symbol
    return (
        f"📋 <b>下单要点</b>\n"
        f"<code>{side} {qty}x {short}</code>\n"
        f"Limit ≥ ${limit:,.0f}  (bid ${bid:,.0f})\n"
        f"完整: <code>{symbol}</code>"
    )


def mode_help(current: str) -> str:
    return (
        f"⚙️ <b>推送模式</b> (当前: <code>{current}</code>)\n\n"
        "<code>/mode hunt</code> — 猎机: 正常推机会+风控\n"
        "<code>/mode hold</code> — 持仓: 抑制新开仓机会, 保留风控/止盈\n"
        "<code>/mode quiet</code> — 安静: 仅 CRITICAL + 日报\n"
    )


def opportunity_action_buttons(short_sym: str) -> list:
    """机会推送 inline 按钮 (callback_data ≤64 字节)"""
    return [
        {"text": "下单要点 📋", "callback_data": f"opp_copy:{short_sym}"},
        {"text": "已开仓 ✅", "callback_data": f"opp_acted:{short_sym}"},
        {"text": "忽略24h ⏭", "callback_data": f"opp_ignore:{short_sym}"},
        {"text": "更多机会 🔍", "callback_data": "cmd_top"},
    ]


def alert_action_buttons() -> list:
    """告警确认按钮"""
    return [
        {"text": "已处理 ✅", "callback_data": "ack_alert"},
        {"text": "稍后·静音1h 🔇", "callback_data": "mute_1h"},
        {"text": "看持仓 📋", "callback_data": "cmd_positions"},
        {"text": "对冲 🛡️", "callback_data": "cmd_hedge"},
    ]
