"""
IV 曲面可视化 + 市场解读

生成图表:
  1. IV Term Structure (期限结构): 各到期日的 IV 中位数
  2. IV Skew (微笑曲线): 选定到期日下, 不同行权价的 IV
  3. Report Chart (报告封面图): 紧凑三面板 — 指标 + Term Structure + Skew

同时输出文字解读:
  - 当前 IV 水平高/低
  - 期限结构正常/倒挂
  - Skew 陡峭程度 → 对卖 Put 的影响
"""

import os
import logging
import matplotlib
matplotlib.use("Agg")  # 无头模式
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Patch, FancyBboxPatch
from datetime import datetime, timezone, timedelta
from collections import defaultdict

log = logging.getLogger("iv_chart")


# 中文字体回退: 优先尝试系统中文字体, 没有就用默认
def setup_font():
    """配置 matplotlib 字体"""
    plt.rcParams.update({
        "font.size": 11,
        "axes.titlesize": 13,
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


setup_font()

CHART_DIR = "/root/projects/charts"
os.makedirs(CHART_DIR, exist_ok=True)


def generate_iv_charts(data: dict, iv_surface: dict, spot: float) -> tuple[str, str]:
    """
    生成 IV 图表

    返回: (图片路径, 文字解读)
    """
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))

    # ========== 左图: IV Term Structure ==========
    ax1 = axes[0]

    exps = sorted(iv_surface["by_exp"].keys())
    now = datetime.now(timezone.utc)

    # 计算到期天数
    exp_labels = []
    exp_dte = []
    iv_medians = []
    iv_means = []
    iv_mins = []
    iv_maxs = []
    iv_p25 = []
    iv_p75 = []

    for exp in exps:
        # 解析到期日 (格式: 260529 -> 2026-05-29)
        try:
            exp_date = datetime.strptime("20" + exp, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dte = (exp_date - now).total_seconds() / 86400
        if dte < 0 or dte > 400:
            continue

        s = iv_surface["by_exp"][exp]
        exp_labels.append(exp)
        exp_dte.append(dte)
        iv_medians.append(s["median"])
        iv_means.append(s["mean"])
        iv_mins.append(s["min"])
        iv_maxs.append(s["max"])
        iv_p25.append(s["p25"])
        iv_p75.append(s["p75"])

    if exp_dte:
        # 范围带
        ax1.fill_between(exp_dte, iv_mins, iv_maxs, alpha=0.1, color="#4fc3f7", label="Min-Max")
        ax1.fill_between(exp_dte, iv_p25, iv_p75, alpha=0.25, color="#4fc3f7", label="P25-P75")
        # 中位线
        ax1.plot(exp_dte, iv_medians, "o-", color="#00e676", linewidth=2.5,
                 markersize=6, label="Median IV", zorder=5)
        ax1.plot(exp_dte, iv_means, "s--", color="#ffa726", linewidth=1.5,
                 markersize=4, label="Mean IV", alpha=0.8)

        # 标注数值
        for i, (d, m) in enumerate(zip(exp_dte, iv_medians)):
            ax1.annotate(f"{m:.2f}", (d, m), textcoords="offset points",
                         xytext=(0, 12), ha="center", fontsize=8, color="#e0e0e0")

        # 标注到期日
        for d, label in zip(exp_dte, exp_labels):
            ax1.annotate(label, (d, iv_mins[exp_dte.index(d)]),
                         textcoords="offset points", xytext=(0, -15),
                         ha="center", fontsize=7, color="#888888", rotation=45)

    ax1.set_title("IV Term Structure (Put)", fontweight="bold", pad=15)
    ax1.set_xlabel("Days to Expiry")
    ax1.set_ylabel("Implied Volatility")
    ax1.legend(loc="upper right", fontsize=8, framealpha=0.3)
    ax1.grid(True, linestyle="--", alpha=0.3)
    ax1.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

    # ========== 右图: IV Skew (选最近有量的月到期) ==========
    ax2 = axes[1]

    # 选一个合适的到期日: 14-60天, 合约最多的
    best_exp = None
    best_count = 0
    for exp in exps:
        try:
            exp_date = datetime.strptime("20" + exp, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dte = (exp_date - now).total_seconds() / 86400
        if 14 <= dte <= 90:
            count = len([sym for sym in data["marks"] if f"BTC-{exp}" in sym and "-P" in sym])
            if count > best_count:
                best_count = count
                best_exp = exp

    skew_data = []
    if best_exp:
        for sym, m in data["marks"].items():
            if f"BTC-{best_exp}" not in sym or "-P" not in sym:
                continue
            parts = sym.split("-")
            strike = float(parts[2])
            iv = float(m.get("markIV", 0))
            if iv > 0:
                moneyness = (strike / spot - 1) * 100  # % from spot
                skew_data.append((strike, iv, moneyness))

        skew_data.sort(key=lambda x: x[0])

    if skew_data:
        strikes = [s[0] for s in skew_data]
        ivs = [s[1] for s in skew_data]
        moneyness = [s[2] for s in skew_data]

        # 颜色: OTM Put 区域高亮
        colors = []
        for s in skew_data:
            if s[2] < -20:      # 深度 OTM Put
                colors.append("#ff5252")
            elif s[2] < -10:    # OTM Put
                colors.append("#ffa726")
            elif s[2] < 0:      # 轻度 OTM
                colors.append("#ffee58")
            else:               # ATM / ITM
                colors.append("#4fc3f7")

        ax2.bar(moneyness, ivs, width=1.8, color=colors, alpha=0.7, edgecolor="none")
        ax2.plot(moneyness, ivs, "-", color="#e0e0e0", linewidth=1.5, alpha=0.8)

        # 标注 ATM
        ax2.axvline(x=0, color="#ffffff", linewidth=1, linestyle="--", alpha=0.5)
        ax2.text(0.5, max(ivs) * 0.95, f"ATM\n${spot:,.0f}",
                 color="#ffffff", fontsize=8, ha="left", va="top")

        # 标注关键区域
        ax2.axvspan(-50, -25, alpha=0.08, color="#ff5252")
        ax2.text(-37, max(ivs) * 0.85, "Target\nZone",
                 color="#ff5252", fontsize=9, fontweight="bold", ha="center", va="top")

        ax2.set_title(f"IV Smile / Skew (Put {best_exp})", fontweight="bold", pad=15)
        ax2.set_xlabel("Moneyness (% from Spot)")
        ax2.set_ylabel("Implied Volatility")
        ax2.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
        ax2.grid(True, linestyle="--", alpha=0.3)

        # 图例
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor="#ff5252", alpha=0.7, label="Deep OTM (>20%)"),
            Patch(facecolor="#ffa726", alpha=0.7, label="OTM (10-20%)"),
            Patch(facecolor="#ffee58", alpha=0.7, label="Near OTM (<10%)"),
            Patch(facecolor="#4fc3f7", alpha=0.7, label="ATM / ITM"),
        ]
        ax2.legend(handles=legend_elements, loc="upper right", fontsize=7, framealpha=0.3)

    plt.tight_layout(pad=2.0)

    chart_path = os.path.join(CHART_DIR, "iv_surface.png")
    fig.savefig(chart_path, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)

    # ========== 文字解读 ==========
    analysis = generate_iv_analysis(iv_surface, spot, data, skew_data, best_exp)

    return chart_path, analysis


def generate_iv_analysis(iv_surface: dict, spot: float, data: dict,
                         skew_data: list, skew_exp: str) -> str:
    """生成 IV 市场解读"""
    lines = []
    lines.append("📈 <b>IV 市场解读</b>")
    lines.append("")

    global_mean = iv_surface["global"]["mean"]
    global_median = iv_surface["global"]["median"]

    # 1. 整体 IV 水平判断
    if global_mean > 0.6:
        iv_level = "极高"
        iv_icon = "🔴"
        iv_advice = "市场恐慌中, IV 极高 → 卖 Put 权利金极厚, 好时机!"
    elif global_mean > 0.5:
        iv_level = "偏高"
        iv_icon = "🟡"
        iv_advice = "IV 偏高, 卖方有一定优势, 可以留意机会"
    elif global_mean > 0.4:
        iv_level = "正常"
        iv_icon = "🟢"
        iv_advice = "IV 处于正常水平, 权利金一般, 耐心等待"
    else:
        iv_level = "偏低"
        iv_icon = "⚪"
        iv_advice = "IV 偏低, 权利金太便宜, 不适合卖 Put"

    lines.append(f"{iv_icon} <b>IV 水平: {iv_level}</b> (均值 {global_mean:.3f})")
    lines.append(f"  {iv_advice}")
    lines.append("")

    # 2. 期限结构分析
    exps = sorted(iv_surface["by_exp"].keys())
    now = datetime.now(timezone.utc)

    near_iv = None  # 近期 (7-14天)
    mid_iv = None   # 中期 (30-45天)
    far_iv = None   # 远期 (60-90天)

    for exp in exps:
        try:
            exp_date = datetime.strptime("20" + exp, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dte = (exp_date - now).total_seconds() / 86400
        s = iv_surface["by_exp"][exp]

        if 3 <= dte <= 14 and near_iv is None:
            near_iv = (exp, s["median"], dte)
        elif 25 <= dte <= 50 and mid_iv is None:
            mid_iv = (exp, s["median"], dte)
        elif 55 <= dte <= 100 and far_iv is None:
            far_iv = (exp, s["median"], dte)

    lines.append("<b>期限结构:</b>")
    if near_iv and mid_iv:
        if near_iv[1] > mid_iv[1] * 1.05:
            lines.append("  📍 近高远低 (Backwardation)")
            lines.append("  → 近期市场紧张, 短期 IV 溢价")
            lines.append("  → 卖近期 Put 权利金更厚, 但 gamma 大")
        elif mid_iv[1] > near_iv[1] * 1.05:
            lines.append("  📍 近低远高 (Contango)")
            lines.append("  → 正常结构, 远期不确定性高")
            lines.append("  → 卖中远期 Put 更稳, 权利金也不差")
        else:
            lines.append("  📍 基本平坦")
            lines.append("  → 各期限 IV 接近, 市场定价均匀")

        if near_iv:
            lines.append(f"  近期 ({near_iv[0]}, {near_iv[2]:.0f}d): {near_iv[1]:.3f}")
        if mid_iv:
            lines.append(f"  中期 ({mid_iv[0]}, {mid_iv[2]:.0f}d): {mid_iv[1]:.3f}")
        if far_iv:
            lines.append(f"  远期 ({far_iv[0]}, {far_iv[2]:.0f}d): {far_iv[1]:.3f}")
    lines.append("")

    # 3. Skew 分析
    if skew_data and skew_exp:
        # ATM IV
        atm_iv = None
        deep_otm_iv = None
        for strike, iv, moneyness in skew_data:
            if abs(moneyness) < 3:
                atm_iv = iv
            if -35 < moneyness < -25:
                deep_otm_iv = iv

        lines.append(f"<b>Skew 分析 ({skew_exp}):</b>")

        if atm_iv and deep_otm_iv:
            skew_ratio = deep_otm_iv / atm_iv
            skew_spread = (deep_otm_iv - atm_iv) * 100  # 转百分比点数

            if skew_ratio > 1.6:
                lines.append(f"  📍 Skew 非常陡峭 (深OTM/ATM = {skew_ratio:.2f}x)")
                lines.append("  → 市场对暴跌极度恐慌")
                lines.append("  → 深度 OTM Put 定价很贵 → <b>卖方好机会!</b>")
            elif skew_ratio > 1.3:
                lines.append(f"  📍 Skew 较陡 (深OTM/ATM = {skew_ratio:.2f}x)")
                lines.append("  → 市场有一定下行担忧")
                lines.append("  → 深度 OTM 有溢价, 可以关注")
            else:
                lines.append(f"  📍 Skew 平坦 (深OTM/ATM = {skew_ratio:.2f}x)")
                lines.append("  → 市场相对平静")
                lines.append("  → OTM Put 没有太多额外溢价, 等等")

            if atm_iv:
                lines.append(f"  ATM IV: {atm_iv:.3f}")
            if deep_otm_iv:
                lines.append(f"  Deep OTM IV (25-35%): {deep_otm_iv:.3f}")
        lines.append("")

    # 4. 操作建议
    lines.append("<b>操作建议:</b>")
    if global_mean > 0.55 and skew_data:
        # 找 skew_ratio
        skew_ratio = deep_otm_iv / atm_iv if (atm_iv and deep_otm_iv) else 1
        if skew_ratio > 1.4:
            lines.append("  ✅ IV高 + Skew陡 → 最佳卖出窗口")
            lines.append("  → 优先卖 30-45天到期, OTM 25-35% 的 Put")
        else:
            lines.append("  🟡 IV偏高但Skew一般 → 可以观望")
            lines.append("  → 等 Skew 进一步走陡再出手")
    elif global_mean > 0.45:
        lines.append("  ⏳ IV 正常, 继续等待")
        lines.append("  → 等市场恐慌、IV 飙升时再出手")
    else:
        lines.append("  ⛔ IV 偏低, 不建议卖 Put")
        lines.append("  → 权利金太薄, 风险收益比不划算")

    return "\n".join(lines)


# ================================================================
#  报告封面图: 紧凑三面板 (指标面板 + Term Structure + Skew)
# ================================================================

def generate_report_chart(data: dict, iv_surface: dict, spot: float,
                          iv_tracker=None, positions: list = None,
                          account_risk: dict = None,
                          btc_24h_change: float = None) -> str:
    """
    生成适合报告推送的紧凑 IV 曲面图表

    三面板布局:
      上: 关键指标面板 (BTC价格, IV水平, IV Percentile, 持仓汇总)
      左下: IV Term Structure (简化版)
      右下: IV Skew (简化版)

    返回: 图片路径 (str), 失败返回 ""
    """
    try:
        fig = plt.figure(figsize=(14, 10), facecolor="#1a1a2e")

        # GridSpec: 上面 1/3 给指标面板, 下面 2/3 给两图
        gs = fig.add_gridspec(2, 2, height_ratios=[0.38, 0.62],
                              hspace=0.35, wspace=0.28,
                              left=0.06, right=0.97, top=0.93, bottom=0.08)

        # ======== 上部: 指标面板 (跨两列) ========
        ax_info = fig.add_subplot(gs[0, :])
        ax_info.set_facecolor("#1a1a2e")
        ax_info.set_xlim(0, 10)
        ax_info.set_ylim(0, 10)
        ax_info.axis("off")

        # 标题
        now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        fig.suptitle(f"BTC Options IV Dashboard  |  {now_str} SGT",
                     fontsize=15, fontweight="bold", color="#e0e0e0", y=0.97)

        global_mean = iv_surface["global"]["mean"]
        global_median = iv_surface["global"]["median"]

        # IV Percentile
        iv_pctl = 50
        if iv_tracker:
            try:
                iv_pctl = iv_tracker.get_iv_percentile(global_mean)
            except Exception:
                pass

        # IV 水平颜色
        if global_mean > 0.55:
            iv_color = "#ff5252"
            iv_label = "EXTREME"
        elif global_mean > 0.45:
            iv_color = "#ffa726"
            iv_label = "HIGH"
        elif global_mean > 0.35:
            iv_color = "#66bb6a"
            iv_label = "NORMAL"
        else:
            iv_color = "#90a4ae"
            iv_label = "LOW"

        # Percentile 颜色
        if iv_pctl >= 70:
            pctl_color = "#ff5252"
        elif iv_pctl >= 40:
            pctl_color = "#ffa726"
        else:
            pctl_color = "#4fc3f7"

        # --- 指标卡片 ---
        card_specs = []

        # Card 1: BTC Price
        btc_str = f"${spot:,.0f}"
        change_str = ""
        change_color = "#e0e0e0"
        if btc_24h_change is not None:
            sign = "+" if btc_24h_change >= 0 else ""
            change_str = f"24h {sign}{btc_24h_change:.1f}%"
            change_color = "#66bb6a" if btc_24h_change >= 0 else "#ff5252"
        card_specs.append({
            "x": 0.8, "label": "BTC", "value": btc_str,
            "sub": change_str, "color": "#4fc3f7", "sub_color": change_color
        })

        # Card 2: IV Level
        card_specs.append({
            "x": 3.0, "label": "IV Mean", "value": f"{global_mean:.1%}",
            "sub": iv_label, "color": iv_color, "sub_color": iv_color
        })

        # Card 3: IV Percentile
        card_specs.append({
            "x": 5.2, "label": "IV Percentile", "value": f"{iv_pctl:.0f}%",
            "sub": "vs 30d history", "color": pctl_color, "sub_color": "#888888"
        })

        # Card 4: 持仓/账户
        pos_value = "-"
        pos_sub = ""
        pos_color = "#4fc3f7"
        pos_sub_color = "#888888"
        if positions:
            short_count = sum(1 for p in positions if p.get("qty", 0) < 0)
            long_count = sum(1 for p in positions if p.get("qty", 0) > 0)
            total_pnl = sum(p.get("pnl_pct", 0) for p in positions)
            avg_pnl = total_pnl / len(positions) if positions else 0
            pos_value = f"{short_count}S + {long_count}L"
            pos_sub = f"Avg PnL {avg_pnl:+.0f}%"
            pos_color = "#66bb6a" if avg_pnl >= 0 else "#ff5252"
            pos_sub_color = pos_color
        elif account_risk:
            margin_pct = account_risk.get("margin_ratio", 0) * 100
            pos_value = f"Margin {margin_pct:.0f}%"
            pos_sub = ""
        card_specs.append({
            "x": 7.5, "label": "Positions", "value": pos_value,
            "sub": pos_sub, "color": pos_color, "sub_color": pos_sub_color
        })

        # 画指标卡片
        for c in card_specs:
            # 卡片背景
            card_bg = FancyBboxPatch(
                (c["x"] - 0.6, 3.5), 2.0, 5.5,
                boxstyle="round,pad=0.2",
                facecolor="#16213e", edgecolor="#333355",
                linewidth=1.5, alpha=0.9
            )
            ax_info.add_patch(card_bg)
            # 标签
            ax_info.text(c["x"] + 0.4, 8.2, c["label"],
                         fontsize=9, color="#888888", ha="center", va="center",
                         fontweight="bold")
            # 主值
            ax_info.text(c["x"] + 0.4, 6.2, c["value"],
                         fontsize=17, color=c["color"], ha="center", va="center",
                         fontweight="bold")
            # 副值
            if c["sub"]:
                ax_info.text(c["x"] + 0.4, 4.5, c["sub"],
                             fontsize=9, color=c["sub_color"], ha="center", va="center")

        # ======== 左下: IV Term Structure (简化版) ========
        ax1 = fig.add_subplot(gs[1, 0])

        exps = sorted(iv_surface["by_exp"].keys())
        now = datetime.now(timezone.utc)

        exp_labels = []
        exp_dte = []
        iv_medians = []
        iv_p25 = []
        iv_p75 = []

        for exp in exps:
            try:
                exp_date = datetime.strptime("20" + exp, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            dte = (exp_date - now).total_seconds() / 86400
            if dte < 0 or dte > 400:
                continue
            s = iv_surface["by_exp"][exp]
            exp_labels.append(exp)
            exp_dte.append(dte)
            iv_medians.append(s["median"])
            iv_p25.append(s["p25"])
            iv_p75.append(s["p75"])

        if exp_dte:
            # P25-P75 范围带
            ax1.fill_between(exp_dte, iv_p25, iv_p75, alpha=0.2, color="#4fc3f7")
            # 中位线
            ax1.plot(exp_dte, iv_medians, "o-", color="#00e676", linewidth=2.5,
                     markersize=5, zorder=5)
            # 标注数值
            for i, (d, m) in enumerate(zip(exp_dte, iv_medians)):
                ax1.annotate(f"{m:.1%}", (d, m), textcoords="offset points",
                             xytext=(0, 10), ha="center", fontsize=7.5, color="#e0e0e0")
            # 标注到期日 (只标注部分，避免拥挤)
            step = max(1, len(exp_dte) // 6)
            for i in range(0, len(exp_dte), step):
                ax1.annotate(exp_labels[i], (exp_dte[i], iv_p25[i]),
                             textcoords="offset points", xytext=(0, -12),
                             ha="center", fontsize=6.5, color="#888888", rotation=30)

        ax1.set_title("IV Term Structure (Put)", fontweight="bold", fontsize=11, pad=10)
        ax1.set_xlabel("Days to Expiry", fontsize=9)
        ax1.set_ylabel("IV", fontsize=9)
        ax1.grid(True, linestyle="--", alpha=0.3)
        ax1.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

        # ======== 右下: IV Skew (简化版) ========
        ax2 = fig.add_subplot(gs[1, 1])

        # 选合适的到期日
        best_exp = None
        best_count = 0
        for exp in exps:
            try:
                exp_date = datetime.strptime("20" + exp, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            dte = (exp_date - now).total_seconds() / 86400
            if 14 <= dte <= 90:
                count = len([sym for sym in data["marks"] if f"BTC-{exp}" in sym and "-P" in sym])
                if count > best_count:
                    best_count = count
                    best_exp = exp

        skew_data = []
        if best_exp:
            for sym, m in data["marks"].items():
                if f"BTC-{best_exp}" not in sym or "-P" not in sym:
                    continue
                parts = sym.split("-")
                strike = float(parts[2])
                iv = float(m.get("markIV", 0))
                if iv > 0:
                    moneyness = (strike / spot - 1) * 100
                    skew_data.append((strike, iv, moneyness))
            skew_data.sort(key=lambda x: x[0])

        if skew_data:
            moneyness = [s[2] for s in skew_data]
            ivs = [s[1] for s in skew_data]

            # 颜色
            colors = []
            for s in skew_data:
                if s[2] < -20:
                    colors.append("#ff5252")
                elif s[2] < -10:
                    colors.append("#ffa726")
                elif s[2] < 0:
                    colors.append("#ffee58")
                else:
                    colors.append("#4fc3f7")

            ax2.bar(moneyness, ivs, width=1.8, color=colors, alpha=0.7, edgecolor="none")
            ax2.plot(moneyness, ivs, "-", color="#e0e0e0", linewidth=1.2, alpha=0.7)

            # ATM 标线
            ax2.axvline(x=0, color="#ffffff", linewidth=1, linestyle="--", alpha=0.4)
            ax2.text(1, max(ivs) * 0.95, f"ATM ${spot:,.0f}",
                     color="#ffffff", fontsize=7, ha="left", va="top")

            # Target Zone
            ax2.axvspan(-50, -25, alpha=0.06, color="#ff5252")
            ax2.text(-37, max(ivs) * 0.88, "Target\nZone",
                     color="#ff5252", fontsize=8, fontweight="bold", ha="center", va="top")

            # 图例
            legend_elements = [
                Patch(facecolor="#ff5252", alpha=0.7, label="Deep OTM >20%"),
                Patch(facecolor="#ffa726", alpha=0.7, label="OTM 10-20%"),
                Patch(facecolor="#ffee58", alpha=0.7, label="Near OTM <10%"),
                Patch(facecolor="#4fc3f7", alpha=0.7, label="ATM/ITM"),
            ]
            ax2.legend(handles=legend_elements, loc="upper right", fontsize=6.5, framealpha=0.3)

        skew_title = f"IV Skew (Put {best_exp})" if best_exp else "IV Skew (Put)"
        ax2.set_title(skew_title, fontweight="bold", fontsize=11, pad=10)
        ax2.set_xlabel("Moneyness (%)", fontsize=9)
        ax2.set_ylabel("IV", fontsize=9)
        ax2.grid(True, linestyle="--", alpha=0.3)
        ax2.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

        # ======== 保存 ========
        chart_path = os.path.join(CHART_DIR, "iv_report.png")
        fig.savefig(chart_path, dpi=140, bbox_inches="tight",
                    facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        log.info(f"报告图表已生成: {chart_path}")
        return chart_path

    except Exception as e:
        log.error(f"报告图表生成失败: {e}", exc_info=True)
        plt.close("all")
        return ""
