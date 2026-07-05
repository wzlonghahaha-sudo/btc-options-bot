"""
组合到期 Payoff 图

绘制当前持仓组合的到期损益曲线:
  - 各仓位 (Short Put / Long Put) 分别计算到期 payoff
  - 合成总 payoff 曲线
  - 标注: 行权价, 当前BTC价, 盈亏平衡点, 强平价
"""

import os
import logging
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

log = logging.getLogger("payoff_chart")

CHART_DIR = "/root/projects/charts"
os.makedirs(CHART_DIR, exist_ok=True)

# 复用 iv_chart 的暗色主题
plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.labelsize": 11,
    "figure.facecolor": "#1a1a2e",
    "axes.facecolor": "#16213e",
    "text.color": "#e0e0e0",
    "axes.labelcolor": "#e0e0e0",
    "xtick.color": "#a0a0a0",
    "ytick.color": "#a0a0a0",
    "axes.edgecolor": "#333355",
    "grid.color": "#333355",
    "grid.alpha": 0.5,
})


def _calc_position_payoff(S: np.ndarray, qty: float, strike: float,
                          entry_price: float) -> np.ndarray:
    """
    计算单个 Put 仓位到期 payoff (每张)

    Short Put (qty < 0):
        卖方收取权利金, 到期时:
        payoff = entry_price * |qty| - max(K - S, 0) * |qty|

    Long Put (qty > 0):
        买方支出权利金, 到期时:
        payoff = max(K - S, 0) * qty - entry_price * qty
    """
    intrinsic = np.maximum(strike - S, 0)

    if qty < 0:
        # Short Put: 收了权利金, 承担行权亏损
        abs_qty = abs(qty)
        payoff = entry_price * abs_qty - intrinsic * abs_qty
    else:
        # Long Put: 付了权利金, 获得行权收益
        payoff = intrinsic * qty - entry_price * qty

    return payoff


def generate_payoff_chart(positions, spot, liq_price=None,
                          save_path="charts/payoff.png"):
    """
    绘制当前组合到期 payoff 曲线

    Args:
        positions: list of dicts with keys: symbol, qty, strike, entry_price
        spot: current BTC price
        liq_price: estimated liquidation price (optional vertical line)
        save_path: output file path

    Returns:
        save_path if successful, None if failed
    """
    if not positions:
        log.warning("payoff chart: 无持仓, 跳过")
        return None

    try:
        # X 轴: BTC 到期价格区间
        s_min = spot * 0.5
        s_max = spot * 1.3
        S = np.linspace(s_min, s_max, 1000)

        # 计算各仓位 payoff 并求和
        total_payoff = np.zeros_like(S)
        individual_payoffs = []

        for pos in positions:
            qty = pos.get("qty", 0)
            strike = pos.get("strike", 0)
            entry_price = pos.get("entry_price", 0)
            if qty == 0 or strike == 0:
                continue

            payoff = _calc_position_payoff(S, qty, strike, entry_price)
            total_payoff += payoff
            individual_payoffs.append({
                "symbol": pos.get("symbol", ""),
                "qty": qty,
                "strike": strike,
                "payoff": payoff,
            })

        if not individual_payoffs:
            log.warning("payoff chart: 无有效仓位")
            return None

        # --- 绘图 ---
        fig, ax = plt.subplots(figsize=(14, 8), facecolor="#1a1a2e")

        # 1. 填充盈亏区域
        ax.fill_between(S, total_payoff, 0,
                        where=(total_payoff >= 0),
                        color="#66bb6a", alpha=0.15, interpolate=True)
        ax.fill_between(S, total_payoff, 0,
                        where=(total_payoff < 0),
                        color="#ff5252", alpha=0.15, interpolate=True)

        # 2. 各仓位单独的 payoff 线 (半透明)
        colors = ["#4fc3f7", "#ffa726", "#ce93d8", "#80cbc4", "#ef9a9a"]
        for i, ip in enumerate(individual_payoffs):
            color = colors[i % len(colors)]
            short_sym = ip["symbol"].split("BTC-")[-1] if ip["symbol"] else f"K={ip['strike']:,.0f}"
            direction = "Short" if ip["qty"] < 0 else "Long"
            label = f"{short_sym} ({direction} {abs(ip['qty']):.0f})"
            ax.plot(S, ip["payoff"], "--", color=color, linewidth=1.2,
                    alpha=0.6, label=label)

        # 3. 合成 payoff 曲线 (主线)
        ax.plot(S, total_payoff, "-", color="#00e676", linewidth=2.5,
                label="Combined Payoff", zorder=10)

        # 4. Y=0 水平线 (breakeven)
        ax.axhline(y=0, color="#ffffff", linewidth=0.8, alpha=0.4)

        # 5. 垂直标注线

        # 5a. 各行权价
        strikes_seen = set()
        for ip in individual_payoffs:
            k = ip["strike"]
            if k in strikes_seen:
                continue
            strikes_seen.add(k)
            ax.axvline(x=k, color="#4fc3f7", linewidth=1, linestyle="--",
                       alpha=0.6)
            ax.text(k, ax.get_ylim()[0] if ax.get_ylim()[0] != 0 else -100,
                    f"K={k:,.0f}",
                    color="#4fc3f7", fontsize=8, ha="center", va="bottom",
                    rotation=90, alpha=0.8)

        # 5b. 当前 BTC 价格 (绿色)
        ax.axvline(x=spot, color="#66bb6a", linewidth=1.5, linestyle="--",
                   alpha=0.8)
        # 标注在图顶部
        ax.text(spot, 0.97, f"  BTC ${spot:,.0f}\n  (Current)",
                color="#66bb6a", fontsize=9, fontweight="bold",
                ha="left", va="top",
                transform=ax.get_xaxis_transform())

        # 5c. 盈亏平衡点 (payoff 从正变负或从负变正的交叉点)
        breakevens = []
        for i in range(1, len(S)):
            if (total_payoff[i - 1] >= 0 and total_payoff[i] < 0) or \
               (total_payoff[i - 1] < 0 and total_payoff[i] >= 0):
                # 线性插值找精确交叉点
                s1, s2 = S[i - 1], S[i]
                p1, p2 = total_payoff[i - 1], total_payoff[i]
                be = s1 - p1 * (s2 - s1) / (p2 - p1)
                breakevens.append(be)

        for be in breakevens:
            ax.axvline(x=be, color="#ffee58", linewidth=1.2, linestyle="--",
                       alpha=0.8)
            pct_from_spot = (be / spot - 1) * 100
            ax.text(be, 0.03,
                    f"  BE ${be:,.0f}\n  ({pct_from_spot:+.1f}%)",
                    color="#ffee58", fontsize=8,
                    ha="left", va="bottom",
                    transform=ax.get_xaxis_transform())

        # 5d. 强平价 (红色)
        if liq_price and liq_price > 0:
            ax.axvline(x=liq_price, color="#ff5252", linewidth=1.5,
                       linestyle="--", alpha=0.9)
            liq_pct = (liq_price / spot - 1) * 100
            ax.text(liq_price, 0.90,
                    f"  Liq ${liq_price:,.0f}\n  ({liq_pct:+.1f}%)",
                    color="#ff5252", fontsize=9, fontweight="bold",
                    ha="left", va="top",
                    transform=ax.get_xaxis_transform())

        # 重新设置 y 轴范围 (需要在所有标注之后)
        y_min = float(np.min(total_payoff))
        y_max = float(np.max(total_payoff))
        y_pad = max(abs(y_max), abs(y_min)) * 0.15
        ax.set_ylim(y_min - y_pad, y_max + y_pad)

        # 重新标注行权价 (使用正确的 y 范围)
        for k in strikes_seen:
            ax.text(k, y_min - y_pad * 0.3,
                    f"K={k:,.0f}",
                    color="#4fc3f7", fontsize=8, ha="center", va="bottom",
                    rotation=90, alpha=0.8)

        # 格式化
        ax.set_title("Portfolio Expiry Payoff", fontweight="bold", pad=15,
                      fontsize=14, color="#e0e0e0")
        ax.set_xlabel("BTC Price at Expiry", fontsize=11)
        ax.set_ylabel("Portfolio P&L ($)", fontsize=11)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f"${x:,.0f}"))
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f"${x:+,.0f}"))
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.legend(loc="upper right", fontsize=8, framealpha=0.3)

        # 注释: 最大盈利 / 最大亏损
        max_profit = float(np.max(total_payoff))
        max_loss = float(np.min(total_payoff))
        summary_text = (
            f"Max Profit: ${max_profit:+,.0f}\n"
            f"Max Loss: ${max_loss:+,.0f}"
        )
        if breakevens:
            be_strs = [f"${be:,.0f}" for be in breakevens]
            summary_text += f"\nBreakeven: {', '.join(be_strs)}"

        ax.text(0.02, 0.97, summary_text,
                transform=ax.transAxes, fontsize=9,
                verticalalignment="top",
                bbox=dict(boxstyle="round,pad=0.5", facecolor="#16213e",
                          edgecolor="#333355", alpha=0.9),
                color="#e0e0e0")

        plt.tight_layout(pad=1.5)

        # 保存
        full_path = os.path.join(CHART_DIR, os.path.basename(save_path))
        fig.savefig(full_path, dpi=140, bbox_inches="tight",
                    facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)

        log.info(f"Payoff 图表已生成: {full_path}")
        return full_path

    except Exception as e:
        log.error(f"Payoff 图表生成失败: {e}", exc_info=True)
        plt.close("all")
        return None
