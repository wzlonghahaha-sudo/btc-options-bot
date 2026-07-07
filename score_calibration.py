"""
评分校准模块 — 验证评分系统是否有 alpha

读取已平仓交易记录, 按 score 分桶统计:
  - 笔数、胜率、平均收益率、最大单笔亏损
  - p_itm 预测 vs 实际 ITM 频率对比

样本 < 10 笔时明确输出"样本不足, 统计无意义", 不给出误导性结论。

用法:
  from score_calibration import generate_calibration_report
  report = generate_calibration_report(journal_data)
"""

import logging

log = logging.getLogger(__name__)

# 评分分桶边界
SCORE_BUCKETS = [
    ("<60", 0, 60),
    ("60-70", 60, 70),
    ("70-80", 70, 80),
    ("80+", 80, 200),
]

MIN_SAMPLE_SIZE = 10


def generate_calibration_report(journal_data: dict) -> str:
    """
    生成评分校准报告 (TG HTML 格式)

    Args:
        journal_data: TradeJournal.data 字典 (含 trades, signals)

    Returns:
        HTML 格式化的校准报告字符串
    """
    trades = journal_data.get("trades", [])
    closed = [t for t in trades if t.get("status") == "CLOSED"]

    lines = ["📊 <b>评分校准报告</b>", ""]

    if len(closed) < MIN_SAMPLE_SIZE:
        lines.append(f"⚠️ <b>样本不足 ({len(closed)}/{MIN_SAMPLE_SIZE}), 统计无意义</b>")
        lines.append("")
        lines.append(f"已平仓交易: {len(closed)} 笔")
        lines.append(f"最少需要: {MIN_SAMPLE_SIZE} 笔")
        lines.append("")
        lines.append("请继续积累交易数据后再查看校准结果。")
        return "\n".join(lines)

    # === 按 score 分桶统计 ===
    lines.append("<b>按评分分桶统计</b>")
    lines.append(f"{'桶':<8} {'笔数':>5} {'胜率':>6} {'平均收益':>8} {'最大亏损':>8}")
    lines.append("─" * 40)

    for label, lo, hi in SCORE_BUCKETS:
        bucket_trades = [
            t for t in closed
            if lo <= t.get("entry_score", t.get("score", 50)) < hi
        ]
        if not bucket_trades:
            lines.append(f"{label:<8} {'0':>5} {'─':>6} {'─':>8} {'─':>8}")
            continue

        n = len(bucket_trades)
        wins = sum(1 for t in bucket_trades if t.get("realized_pnl", 0) >= 0)
        win_rate = wins / n * 100 if n > 0 else 0
        avg_return = sum(t.get("realized_pnl_pct", 0) for t in bucket_trades) / n
        worst = min(t.get("realized_pnl", 0) for t in bucket_trades)

        lines.append(
            f"{label:<8} {n:>5} {win_rate:>5.0f}% {avg_return:>+7.1f}% ${worst:>+7,.0f}"
        )

    lines.append("")

    # === p_itm 预测 vs 实际 ITM 频率 ===
    lines.append("<b>P(ITM) 预测 vs 实际</b>")

    # 收集有 p_itm 数据的交易
    pitm_trades = [t for t in closed if t.get("entry_p_itm", 0) > 0]

    if len(pitm_trades) < 5:
        lines.append(f"数据不足 ({len(pitm_trades)} 笔有 P(ITM) 记录)")
    else:
        # 按 p_itm 分段: 0-5%, 5-10%, 10-20%, 20%+
        pitm_buckets = [
            ("0-5%", 0, 0.05),
            ("5-10%", 0.05, 0.10),
            ("10-20%", 0.10, 0.20),
            ("20%+", 0.20, 1.0),
        ]
        lines.append(f"{'段':<8} {'笔数':>5} {'预测P(ITM)':>10} {'实际ITM':>8}")
        lines.append("─" * 35)

        for label, lo, hi in pitm_buckets:
            bucket = [t for t in pitm_trades if lo <= t.get("entry_p_itm", 0) < hi]
            if not bucket:
                continue
            n = len(bucket)
            avg_predicted = sum(t.get("entry_p_itm", 0) for t in bucket) / n * 100
            # 实际 ITM: 平仓时亏损的视为 ITM (简化)
            actual_itm = sum(1 for t in bucket if t.get("realized_pnl", 0) < 0) / n * 100
            lines.append(f"{label:<8} {n:>5} {avg_predicted:>9.1f}% {actual_itm:>7.1f}%")

    lines.append("")

    # === 总结 ===
    total_pnl = sum(t.get("realized_pnl", 0) for t in closed)
    total_wins = sum(1 for t in closed if t.get("realized_pnl", 0) >= 0)
    overall_wr = total_wins / len(closed) * 100 if closed else 0

    lines.append("<b>总计</b>")
    lines.append(f"已平仓: {len(closed)} 笔  |  胜率: {overall_wr:.0f}%  |  累计 PnL: ${total_pnl:+,.0f}")

    # 判断评分系统是否有效
    if len(closed) >= MIN_SAMPLE_SIZE:
        # 检查高分桶是否比低分桶表现更好
        high_trades = [t for t in closed if t.get("entry_score", t.get("score", 50)) >= 70]
        low_trades = [t for t in closed if t.get("entry_score", t.get("score", 50)) < 70]
        if high_trades and low_trades:
            high_wr = sum(1 for t in high_trades if t.get("realized_pnl", 0) >= 0) / len(high_trades) * 100
            low_wr = sum(1 for t in low_trades if t.get("realized_pnl", 0) >= 0) / len(low_trades) * 100
            if high_wr > low_wr + 10:
                lines.append("✅ 高分桶胜率显著高于低分桶, 评分系统有区分力")
            elif high_wr > low_wr:
                lines.append("🟡 高分桶胜率略高, 需更多数据验证")
            else:
                lines.append("⚠️ 高分桶胜率不优于低分桶, 评分系统可能需要调整")

    return "\n".join(lines)
