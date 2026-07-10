"""
价格轴风险地图 (R4-4)

横向价格标尺图, 一张图回答"我现在站在哪里":
  - 元素: 强平价 💀, 各行权价, 止损价, 现价, 今日开盘
  - 区间着色: 强平区红色, 行权到现价黄色, 现价以上绿色
  - 深色背景, 宽高比 2:1
"""

import os
import logging
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

log = logging.getLogger(__name__)

CHART_DIR = os.path.join(os.path.dirname(__file__), "charts")


def generate_risk_map(
    spot: float,
    daily_open: float,
    positions: list,
    liq_price: float = 0,
    output_path: str = None,
) -> str:
    """
    生成价格轴风险地图 PNG

    Args:
        spot: BTC 现价
        daily_open: 今日开盘价
        positions: 持仓列表, 每个含:
            {symbol, strike, qty, entry, mark, pnl, direction}
        liq_price: 预估强平价 (0=未知)
        output_path: 输出路径 (默认 charts/risk_map.png)

    Returns:
        PNG 文件路径
    """
    if not output_path:
        os.makedirs(CHART_DIR, exist_ok=True)
        output_path = os.path.join(CHART_DIR, "risk_map.png")

    try:
        fig, ax = plt.subplots(figsize=(12, 6))
        fig.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        # 收集所有价格点
        strikes = [p["strike"] for p in positions if p.get("strike", 0) > 0]
        all_prices = [spot] + strikes
        if daily_open > 0:
            all_prices.append(daily_open)
        if liq_price > 0:
            all_prices.append(liq_price)

        if not all_prices:
            plt.close(fig)
            return ""

        price_min = min(all_prices) * 0.92
        price_max = max(all_prices) * 1.05

        # 区间着色
        y_bottom, y_top = 0, 1

        # 强平区 (红色渐变)
        if liq_price > 0:
            liq_zone_left = max(price_min, liq_price * 0.95)
            liq_zone_right = liq_price * 1.05
            ax.axvspan(liq_zone_left, liq_zone_right,
                      alpha=0.3, color='#ff4444', zorder=1)

        # 行权到现价之间 (黄色)
        if strikes:
            highest_strike = max(strikes)
            if highest_strike < spot:
                ax.axvspan(highest_strike, spot,
                          alpha=0.15, color='#ffaa00', zorder=1)

        # 现价以上 (绿色)
        ax.axvspan(spot, price_max, alpha=0.1, color='#44ff44', zorder=1)

        # --- 现价竖线 (醒目) ---
        ax.axvline(x=spot, color='#00ff88', linewidth=3, linestyle='-', zorder=10)
        ax.annotate(f'BTC ${spot:,.0f}\n(现价)',
                   xy=(spot, 0.85), fontsize=11, fontweight='bold',
                   color='#00ff88', ha='center',
                   bbox=dict(boxstyle='round,pad=0.3', facecolor='#1a1a2e',
                            edgecolor='#00ff88', alpha=0.9),
                   zorder=15)

        # --- 今日开盘 ---
        if daily_open > 0:
            ax.axvline(x=daily_open, color='#aaaaaa', linewidth=1.5,
                      linestyle='--', zorder=5)
            change_pct = (spot - daily_open) / daily_open * 100
            ax.annotate(f'开盘 ${daily_open:,.0f}\n({change_pct:+.1f}%)',
                       xy=(daily_open, 0.95), fontsize=8,
                       color='#aaaaaa', ha='center', zorder=15)

        # --- 强平价 ---
        if liq_price > 0:
            ax.axvline(x=liq_price, color='#ff4444', linewidth=2.5,
                      linestyle=':', zorder=8)
            dist_liq = (spot - liq_price) / spot * 100
            ax.annotate(f'💀 强平 ${liq_price:,.0f}\n(距 {dist_liq:.0f}%)',
                       xy=(liq_price, 0.75), fontsize=9,
                       color='#ff4444', ha='center',
                       bbox=dict(boxstyle='round,pad=0.2', facecolor='#330000',
                                edgecolor='#ff4444', alpha=0.9),
                       zorder=15)

        # --- 各行权价 ---
        short_positions = [p for p in positions if p.get("direction") == "Short"]
        long_positions = [p for p in positions if p.get("direction") == "Long"]

        y_offsets = [0.20, 0.35, 0.50, 0.15, 0.45]  # 错开防重叠

        for i, p in enumerate(short_positions):
            strike = p["strike"]
            pnl = p.get("pnl", 0)
            short_sym = p["symbol"].split("BTC-")[-1]
            dist = (spot - strike) / spot * 100 if spot > 0 else 0
            qty = abs(p.get("qty", 0))

            color = '#ff6644' if dist < 10 else '#ffaa00' if dist < 20 else '#88aaff'
            ax.axvline(x=strike, color=color, linewidth=1.5,
                      linestyle='-', alpha=0.7, zorder=6)

            y_pos = y_offsets[i % len(y_offsets)]
            label = f'{short_sym}\n×{qty:.1f} · 距{dist:.0f}%\nPnL ${pnl:+,.0f}'
            ax.annotate(label,
                       xy=(strike, y_pos), fontsize=7.5,
                       color=color, ha='center',
                       bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a2e',
                                edgecolor=color, alpha=0.8),
                       zorder=14)

        for i, p in enumerate(long_positions):
            strike = p["strike"]
            short_sym = p["symbol"].split("BTC-")[-1]
            qty = abs(p.get("qty", 0))
            pnl = p.get("pnl", 0)

            ax.axvline(x=strike, color='#44aaff', linewidth=1,
                      linestyle='--', alpha=0.5, zorder=5)
            y_pos = 0.60 + (i % 2) * 0.12
            ax.annotate(f'🛡 {short_sym}\nLong ×{qty:.1f}\n${pnl:+,.0f}',
                       xy=(strike, y_pos), fontsize=7,
                       color='#44aaff', ha='center', zorder=14)

        # 坐标轴设置
        ax.set_xlim(price_min, price_max)
        ax.set_ylim(0, 1)
        ax.set_xlabel("BTC 价格 ($)", color='#cccccc', fontsize=10)
        ax.tick_params(axis='x', colors='#888888', labelsize=9)
        ax.yaxis.set_visible(False)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_visible(False)
        ax.spines['bottom'].set_color('#444444')

        # X 轴格式化
        ax.xaxis.set_major_formatter(plt.FuncFormatter(
            lambda x, p: f"${x:,.0f}"))

        # 标题
        total_pnl = sum(p.get("pnl", 0) for p in positions)
        n_short = len(short_positions)
        ax.set_title(f"风险地图  |  {n_short} 个卖 Put · 组合浮盈 ${total_pnl:+,.0f}",
                    color='#eeeeee', fontsize=13, fontweight='bold', pad=15)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        log.info(f"风险地图已生成: {output_path}")
        return output_path

    except Exception as e:
        log.error(f"风险地图生成失败: {e}", exc_info=True)
        plt.close("all")
        return ""


def generate_opportunity_map(
    spot: float,
    strike: float,
    safety_pct: float,
    stop_price_btc: float,
    symbol: str,
    output_path: str = None,
) -> str:
    """
    简化版: 机会推送附图 — 现价/行权价/安全垫/止损

    Returns:
        PNG 文件路径
    """
    if not output_path:
        os.makedirs(CHART_DIR, exist_ok=True)
        output_path = os.path.join(CHART_DIR, "opp_map.png")

    try:
        fig, ax = plt.subplots(figsize=(10, 3))
        fig.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        breakeven = strike  # 简化: 行权价即盈亏平衡
        price_min = strike * 0.90
        price_max = spot * 1.05

        # 安全区 (绿色)
        ax.axvspan(strike, spot, alpha=0.15, color='#44ff44')
        # 危险区 (红色)
        ax.axvspan(price_min, strike, alpha=0.15, color='#ff4444')

        # 现价
        ax.axvline(x=spot, color='#00ff88', linewidth=3, zorder=10)
        ax.annotate(f'现价 ${spot:,.0f}', xy=(spot, 0.8), fontsize=10,
                   color='#00ff88', ha='center', fontweight='bold', zorder=15)

        # 行权价
        ax.axvline(x=strike, color='#ff6644', linewidth=2, zorder=8)
        short_sym = symbol.split("BTC-")[-1]
        ax.annotate(f'行权 ${strike:,.0f}\n({short_sym})',
                   xy=(strike, 0.4), fontsize=9, color='#ff6644', ha='center', zorder=15)

        # 安全垫标注
        mid = (spot + strike) / 2
        ax.annotate(f'← 安全垫 {safety_pct:.1f}% →',
                   xy=(mid, 0.6), fontsize=10, color='#ffdd44',
                   ha='center', fontweight='bold', zorder=15)

        ax.set_xlim(price_min, price_max)
        ax.set_ylim(0, 1)
        ax.yaxis.set_visible(False)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_visible(False)
        ax.spines['bottom'].set_color('#444444')
        ax.tick_params(axis='x', colors='#888888', labelsize=8)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:,.0f}"))

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        return output_path

    except Exception as e:
        log.error(f"机会地图生成失败: {e}")
        plt.close("all")
        return ""
