"""
Daily Digest 仪表盘 (R4-5)

2x2 子图 dashboard PNG:
  ① 价格轴风险地图 (复用 price_axis_chart)
  ② 组合 payoff 曲线 (复用 payoff_chart)
  ③ IV Rank 7 日走势线
  ④ 累计已实现收益柱状图 (按周)

Caption: 恰好 5 行
"""

import os
import logging
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

CHART_DIR = os.path.join(os.path.dirname(__file__), "charts")


def generate_digest_dashboard(
    spot: float,
    daily_open: float,
    positions: list,
    liq_price: float,
    iv_history: list,
    trade_data: dict,
    output_path: str = None,
) -> str:
    """
    生成 2x2 仪表盘 PNG

    Args:
        spot: BTC 现价
        daily_open: 今日开盘
        positions: 持仓列表
        liq_price: 强平价
        iv_history: IV 曲面历史 (来自 state_persistence)
        trade_data: TradeJournal.data

    Returns:
        PNG 路径
    """
    if not output_path:
        os.makedirs(CHART_DIR, exist_ok=True)
        output_path = os.path.join(CHART_DIR, "digest_dashboard.png")

    try:
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.set_facecolor("#1a1a2e")
        fig.suptitle("每日风控仪表盘", color="#eeeeee", fontsize=16,
                    fontweight="bold", y=0.98)

        for ax in axes.flat:
            ax.set_facecolor("#16213e")
            ax.tick_params(colors="#888888", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("#333333")

        # ① 价格轴 (简化版)
        _draw_price_axis(axes[0, 0], spot, daily_open, positions, liq_price)

        # ② Payoff 曲线 (简化)
        _draw_payoff(axes[0, 1], spot, positions)

        # ③ IV Rank 7 日走势
        _draw_iv_trend(axes[1, 0], iv_history)

        # ④ 累计收益 (按周)
        _draw_weekly_pnl(axes[1, 1], trade_data)

        plt.tight_layout(rect=[0, 0, 1, 0.96])
        plt.savefig(output_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        log.info(f"Digest 仪表盘已生成: {output_path}")
        return output_path

    except Exception as e:
        log.error(f"Digest 仪表盘生成失败: {e}", exc_info=True)
        plt.close("all")
        return ""


def _draw_price_axis(ax, spot, daily_open, positions, liq_price):
    """子图①: 价格轴风险地图"""
    ax.set_title("价格轴风险地图", color="#cccccc", fontsize=11, fontweight="bold")

    strikes = [p["strike"] for p in positions if p.get("strike", 0) > 0 and p.get("direction") == "Short"]
    all_prices = [spot] + strikes
    if liq_price > 0:
        all_prices.append(liq_price)

    if not all_prices or len(all_prices) < 2:
        ax.text(0.5, 0.5, "无持仓数据", color="#888888", ha="center", va="center",
               transform=ax.transAxes, fontsize=12)
        return

    price_min = min(all_prices) * 0.93
    price_max = max(all_prices) * 1.05

    # 着色
    if strikes:
        ax.axvspan(max(price_min, min(strikes) * 0.95), min(strikes),
                  alpha=0.2, color="#ff4444")
        ax.axvspan(max(strikes), spot, alpha=0.15, color="#ffaa00")
    ax.axvspan(spot, price_max, alpha=0.1, color="#44ff44")

    # 现价
    ax.axvline(x=spot, color="#00ff88", linewidth=2.5, zorder=10)
    ax.annotate(f"${spot:,.0f}", xy=(spot, 0.9), fontsize=9,
               color="#00ff88", ha="center", fontweight="bold", zorder=15)

    # 行权价
    for p in positions:
        if p.get("direction") != "Short":
            continue
        s = p["strike"]
        dist = (spot - s) / spot * 100
        ax.axvline(x=s, color="#ff8844", linewidth=1.5, alpha=0.7, zorder=6)
        ax.annotate(f"${s/1000:.0f}k\n{dist:.0f}%", xy=(s, 0.3),
                   fontsize=7, color="#ff8844", ha="center", zorder=14)

    # 强平价
    if liq_price > 0:
        ax.axvline(x=liq_price, color="#ff4444", linewidth=2, linestyle=":", zorder=8)
        ax.annotate(f"💀${liq_price/1000:.0f}k", xy=(liq_price, 0.7),
                   fontsize=8, color="#ff4444", ha="center", zorder=15)

    ax.set_xlim(price_min, price_max)
    ax.set_ylim(0, 1)
    ax.yaxis.set_visible(False)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x/1000:.0f}k"))


def _draw_payoff(ax, spot, positions):
    """子图②: 组合到期 payoff"""
    ax.set_title("组合到期 Payoff", color="#cccccc", fontsize=11, fontweight="bold")

    if not positions:
        ax.text(0.5, 0.5, "无持仓", color="#888888", ha="center", va="center",
               transform=ax.transAxes, fontsize=12)
        return

    strikes = [p["strike"] for p in positions if p.get("strike", 0) > 0]
    if not strikes:
        return

    x_min = min(strikes) * 0.85
    x_max = spot * 1.15
    prices = np.linspace(x_min, x_max, 200)

    total_payoff = np.zeros_like(prices)
    for p in positions:
        strike = p.get("strike", 0)
        qty = p.get("qty", 0)
        entry = p.get("entry", 0)
        if strike <= 0:
            continue

        # Put payoff at expiry
        intrinsic = np.maximum(strike - prices, 0)
        if qty < 0:  # Short Put
            payoff = (entry - intrinsic) * abs(qty)
        else:  # Long Put
            payoff = (intrinsic - entry) * abs(qty)
        total_payoff += payoff

    ax.fill_between(prices, total_payoff, 0,
                   where=total_payoff >= 0, alpha=0.3, color="#44ff44")
    ax.fill_between(prices, total_payoff, 0,
                   where=total_payoff < 0, alpha=0.3, color="#ff4444")
    ax.plot(prices, total_payoff, color="#ffffff", linewidth=1.5, zorder=5)
    ax.axhline(y=0, color="#666666", linewidth=0.5)
    ax.axvline(x=spot, color="#00ff88", linewidth=1.5, linestyle="--", alpha=0.7)
    ax.set_xlabel("BTC 到期价", color="#888888", fontsize=8)
    ax.set_ylabel("盈亏 ($)", color="#888888", fontsize=8)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x/1000:.0f}k"))


def _draw_iv_trend(ax, iv_history):
    """子图③: IV Rank 7 日走势"""
    ax.set_title("IV 中位数 7 日走势", color="#cccccc", fontsize=11, fontweight="bold")

    if not iv_history or len(iv_history) < 2:
        ax.text(0.5, 0.5, "数据积累中", color="#888888", ha="center", va="center",
               transform=ax.transAxes, fontsize=12)
        return

    times = []
    ivs = []
    for h in iv_history[-168:]:  # 最近 7 天
        t = h.get("time", 0)
        iv = h.get("global_median", 0)
        if t > 0 and iv > 0:
            times.append(datetime.fromtimestamp(t, tz=timezone.utc))
            ivs.append(iv * 100)  # 转为百分比

    if len(times) < 2:
        ax.text(0.5, 0.5, "数据不足", color="#888888", ha="center", va="center",
               transform=ax.transAxes, fontsize=12)
        return

    ax.plot(times, ivs, color="#ffaa44", linewidth=1.5, zorder=5)
    ax.fill_between(times, ivs, alpha=0.2, color="#ffaa44")
    ax.set_ylabel("IV %", color="#888888", fontsize=8)
    ax.tick_params(axis="x", rotation=30)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(
        lambda x, p: datetime.fromtimestamp(x, tz=timezone.utc).strftime("%m/%d")
        if x > 1e9 else ""))


def _draw_weekly_pnl(ax, trade_data):
    """子图④: 累计已实现收益 (按周)"""
    ax.set_title("已实现收益 (按周)", color="#cccccc", fontsize=11, fontweight="bold")

    trades = trade_data.get("trades", [])
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("exit_time", 0) > 0]

    if not closed:
        ax.text(0.5, 0.5, "暂无已平仓交易", color="#888888", ha="center", va="center",
               transform=ax.transAxes, fontsize=12)
        return

    # 按周分桶
    from collections import defaultdict
    weekly = defaultdict(float)
    for t in closed:
        dt = datetime.fromtimestamp(t["exit_time"], tz=timezone.utc)
        week_key = dt.strftime("%m/%d")
        weekly[week_key] += t.get("realized_pnl", 0)

    weeks = list(weekly.keys())[-8:]  # 最近 8 周
    pnls = [weekly[w] for w in weeks]

    colors = ["#44ff44" if p >= 0 else "#ff4444" for p in pnls]
    ax.bar(weeks, pnls, color=colors, alpha=0.7, edgecolor="#333333")
    ax.axhline(y=0, color="#666666", linewidth=0.5)
    ax.set_ylabel("PnL ($)", color="#888888", fontsize=8)
    ax.tick_params(axis="x", rotation=30)


def generate_digest_caption(
    theta_daily: float,
    top_alert_verdict: str,
    top_opp_verdict: str,
    events_7d: list,
    margin_usage_pct: float,
    liq_drop_pct: float,
) -> str:
    """
    生成 digest caption (恰好 5 行)

    Returns:
        5 行文字字符串
    """
    # 1. 昨日 theta 实收
    line1 = f"💰 昨日 Theta 实收 ~${theta_daily:,.0f}"

    # 2. 今日风险一句话
    if top_alert_verdict:
        line2 = f"⚠️ {top_alert_verdict}"
    else:
        line2 = "✅ 今日暂无风控告警"

    # 3. 今日最佳机会
    if top_opp_verdict:
        line3 = f"🔍 {top_opp_verdict}"
    else:
        line3 = "🔍 今日无 A/B 级机会"

    # 4. 未来 7 天事件
    if events_7d:
        evt_str = " / ".join(events_7d[:3])
        line4 = f"📅 7天内: {evt_str}"
    else:
        line4 = "📅 7天内无重大事件"

    # 5. 保证金 + 强平
    line5 = f"🏦 保证金 {margin_usage_pct:.0f}% · 强平距离 {abs(liq_drop_pct):.0f}%"

    return "\n".join([line1, line2, line3, line4, line5])
