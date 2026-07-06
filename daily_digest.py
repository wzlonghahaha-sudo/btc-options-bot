"""
每日 Digest 日报生成器

在固定时间 (默认 UTC 0:00 = HK 8:00) 推送一份完整的每日摘要:
  1. 持仓概览 (symbol, qty, entry, mark, PnL)
  2. 昨日 theta 实收合计 (按每个 short position 的 theta × 1 天估算)
  3. 当前 IV Rank + IV/HV
  4. 强平距离
  5. 未来 7 天内事件日历
  6. 组合压测一行摘要 (worst scenario shortfall)

每个板块独立 try/except, 部分数据缺失不影响其他板块输出。
"""

import logging
from datetime import datetime, timezone, date, timedelta

log = logging.getLogger("daily_digest")


def generate_daily_digest(api, risk_engine, state_persistence) -> str:
    """
    生成每日 digest HTML 字符串 (供 TG 推送)

    Args:
        api: BinanceOptionsAPI 实例
        risk_engine: RiskEngine 实例
        state_persistence: StatePersistence 实例

    Returns:
        格式化的 HTML 字符串
    """
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"📋 <b>每日日报</b>  {date_str}",
        "",
    ]

    # === 获取基础数据 (后续板块共用) ===
    spot = 0
    positions_raw = []
    marks_map = {}
    account_balance = 0

    try:
        idx = api.get_index_price("BTCUSDT")
        spot = float(idx["indexPrice"])
        lines.append(f"BTC: <b>${spot:,.2f}</b>")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] 获取 BTC 价格失败: {e}")
        lines.append("BTC: <i>获取失败</i>")
        lines.append("")

    try:
        positions_raw = api.get_position()
        active_positions = [p for p in positions_raw
                           if float(p.get("quantity", 0)) != 0]
    except Exception as e:
        log.warning(f"[digest] 获取持仓失败: {e}")
        active_positions = []

    try:
        all_marks = api.get_mark_price()
        marks_map = {m["symbol"]: m for m in all_marks
                     if m["symbol"].startswith("BTC")}
    except Exception as e:
        log.warning(f"[digest] 获取 mark price 失败: {e}")

    try:
        from binance_options import get_account_equity
        _acct = get_account_equity(api)
        account_balance = _acct["margin_balance"]
    except Exception as e:
        log.warning(f"[digest] 获取账户余额失败: {e}")

    # ─────────────────────────────────────────────
    # 1. 持仓概览
    # ─────────────────────────────────────────────
    try:
        lines.append("<b>📊 持仓概览</b>")
        if not active_positions:
            lines.append("  暂无持仓")
        else:
            total_pnl = 0
            for pos in active_positions:
                sym = pos["symbol"]
                qty = float(pos.get("quantity", 0))
                entry = float(pos.get("entryPrice", 0))
                mark_price = float(pos.get("markPrice", 0))

                # 优先用 marks API 的实时价格
                m = marks_map.get(sym, {})
                if m:
                    mark_price = float(m.get("markPrice", mark_price))

                if qty < 0:
                    pnl = (entry - mark_price) * abs(qty)
                else:
                    pnl = (mark_price - entry) * abs(qty)
                total_pnl += pnl

                short_sym = sym.split("BTC-")[-1] if "BTC-" in sym else sym
                dir_tag = "L" if qty > 0 else "S"
                lines.append(
                    f"  {dir_tag} {short_sym}  "
                    f"×{abs(qty):.1f}  "
                    f"入 ${entry:,.0f}  "
                    f"现 ${mark_price:,.0f}  "
                    f"PnL ${pnl:+,.0f}"
                )
            lines.append(f"  <b>合计 PnL: ${total_pnl:+,.0f}</b>")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] 持仓概览失败: {e}")
        lines.append("  <i>持仓数据获取失败</i>")
        lines.append("")

    # ─────────────────────────────────────────────
    # 2. 昨日 theta 实收合计
    # ─────────────────────────────────────────────
    try:
        short_positions = [p for p in active_positions
                          if float(p.get("quantity", 0)) < 0]
        total_theta_1d = 0
        theta_details = []
        for pos in short_positions:
            sym = pos["symbol"]
            abs_qty = abs(float(pos.get("quantity", 0)))
            m = marks_map.get(sym, {})
            theta = float(m.get("theta", 0))
            # theta 通常是负的 (期权贬值), 对 short holder 是正收入
            daily_theta = abs(theta) * abs_qty
            total_theta_1d += daily_theta
            if daily_theta > 0:
                short_sym = sym.split("BTC-")[-1] if "BTC-" in sym else sym
                theta_details.append(f"    {short_sym}: ${daily_theta:.1f}")

        lines.append("<b>💰 昨日 Theta 估收</b>")
        if total_theta_1d > 0:
            lines.append(f"  合计: <b>${total_theta_1d:,.1f}</b> (1日估算)")
            for detail in theta_details:
                lines.append(detail)
        else:
            lines.append("  无 Short 持仓或 Theta 数据不可用")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] Theta 估算失败: {e}")
        lines.append("<b>💰 昨日 Theta 估收</b>")
        lines.append("  <i>计算失败</i>")
        lines.append("")

    # ─────────────────────────────────────────────
    # 3. IV Rank + IV/HV
    # ─────────────────────────────────────────────
    try:
        from profit_optimizer import VolatilityAnalyzer

        # 计算全局 Put IV 均值
        iv_sum = 0
        iv_count = 0
        for m in marks_map.values():
            iv = float(m.get("markIV", 0))
            sym = m.get("symbol", "")
            if iv > 0 and "-P" in sym:
                iv_sum += iv
                iv_count += 1
        mean_iv = iv_sum / iv_count if iv_count > 0 else 0

        vol_analyzer = VolatilityAnalyzer()
        vol_analysis = vol_analyzer.get_full_analysis(mean_iv)

        # IV Rank (从 iv_tracker / iv_history 估算)
        iv_rank_str = ""
        try:
            from otm_put_monitor import IVTracker
            iv_tracker = IVTracker()
            iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
            iv_rank_str = f"IV Rank: <b>{iv_pctl:.0f}%</b>"
        except Exception:
            iv_rank_str = "IV Rank: N/A"

        edge_icon = {
            "STRONG": "🟢", "MODERATE": "🟡",
            "SLIGHT": "⚪", "NONE": "🔴"
        }.get(vol_analysis["edge"], "")

        lines.append("<b>📈 波动率</b>")
        lines.append(
            f"  IV均值: {mean_iv:.3f}  |  {iv_rank_str}"
        )
        lines.append(
            f"  HV20: {vol_analysis['hv_20']:.3f}  |  "
            f"{edge_icon} IV/HV = <b>{vol_analysis['iv_hv_ratio']:.2f}x</b>"
        )
        lines.append(f"  {vol_analysis['edge_desc']}")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] IV/HV 分析失败: {e}")
        lines.append("<b>📈 波动率</b>")
        lines.append("  <i>分析失败</i>")
        lines.append("")

    # ─────────────────────────────────────────────
    # 4. 强平距离
    # ─────────────────────────────────────────────
    try:
        if active_positions and spot > 0 and account_balance > 0:
            pos_list = risk_engine._build_position_list(
                active_positions, marks_map, spot)

            if pos_list:
                from margin_calc import estimate_liquidation_price
                liq = estimate_liquidation_price(
                    pos_list, spot, account_balance)
                liq_price = liq["liq_price"]
                liq_drop = abs(liq["liq_drop_pct"])

                if liq_drop < 15:
                    icon = "🚨"
                elif liq_drop < 25:
                    icon = "🔴"
                elif liq_drop < 40:
                    icon = "⚠️"
                else:
                    icon = "🟢"

                lines.append("<b>💀 强平距离</b>")
                lines.append(
                    f"  {icon} 强平价 ${liq_price:,.0f}  "
                    f"(BTC 需跌 {liq_drop:.0f}%)  "
                    f"垫 ${liq['cushion']:,.0f}"
                )
            else:
                lines.append("<b>💀 强平距离</b>")
                lines.append("  无需计算 (无 Short 仓位)")
        else:
            lines.append("<b>💀 强平距离</b>")
            lines.append("  数据不足 (无仓位或余额未获取)")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] 强平距离计算失败: {e}")
        lines.append("<b>💀 强平距离</b>")
        lines.append("  <i>计算失败</i>")
        lines.append("")

    # ─────────────────────────────────────────────
    # 5. 未来 7 天内事件日历
    # ─────────────────────────────────────────────
    try:
        from event_calendar import _get_all_events

        today = date.today()
        cutoff = today + timedelta(days=7)
        all_events = _get_all_events()
        upcoming = [e for e in all_events if today <= e.date <= cutoff]

        lines.append("<b>📅 未来 7 天事件</b>")
        if upcoming:
            for ev in upcoming:
                days_away = (ev.date - today).days
                level_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(
                    ev.level, "")
                if days_away == 0:
                    day_str = "今天"
                elif days_away == 1:
                    day_str = "明天"
                else:
                    day_str = f"{days_away}天后"
                lines.append(
                    f"  {level_icon} {ev.date_str} ({day_str}) — {ev.name}"
                )
        else:
            lines.append("  未来 7 天无重大事件 ✅")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] 事件日历失败: {e}")
        lines.append("<b>📅 未来 7 天事件</b>")
        lines.append("  <i>获取失败</i>")
        lines.append("")

    # ─────────────────────────────────────────────
    # 6. 组合压测一行摘要 (worst scenario shortfall)
    # ─────────────────────────────────────────────
    try:
        if active_positions and spot > 0 and account_balance > 0:
            pos_list = risk_engine._build_position_list(
                active_positions, marks_map, spot)

            if pos_list:
                from margin_calc import stress_test_portfolio
                stress_results = stress_test_portfolio(
                    pos_list, spot, account_balance,
                    scenarios=[-10, -20, -30, -40, -50],
                )

                # 找 worst scenario (最大保证金缺口)
                worst = max(stress_results,
                           key=lambda s: s.margin_shortfall)

                lines.append("<b>📉 压测摘要</b>")
                if worst.margin_shortfall > 0:
                    lines.append(
                        f"  ⚠️ 最差场景 {worst.scenario}: "
                        f"BTC ${worst.btc_price:,.0f}  "
                        f"亏 ${abs(worst.total_pnl):,.0f}  "
                        f"缺口 <b>${worst.margin_shortfall:,.0f}</b>"
                    )
                else:
                    lines.append(
                        f"  ✅ BTC -50% 内无保证金缺口  "
                        f"(最差 {worst.scenario}: 亏 ${abs(worst.total_pnl):,.0f})"
                    )
            else:
                lines.append("<b>📉 压测摘要</b>")
                lines.append("  无 Short 仓位, 无需压测")
        else:
            lines.append("<b>📉 压测摘要</b>")
            lines.append("  数据不足")
        lines.append("")
    except Exception as e:
        log.warning(f"[digest] 压测摘要失败: {e}")
        lines.append("<b>📉 压测摘要</b>")
        lines.append("  <i>计算失败</i>")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>每日自动推送 · /help 查看命令</i>")

    return "\n".join(lines)
