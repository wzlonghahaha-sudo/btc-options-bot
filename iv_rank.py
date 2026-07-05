"""
IV Rank 计算模块 — 时序波动率百分位

问题:
  sell_put_strategy.py 的 iv_premium 拿合约 IV 与同到期日横截面中位数比较。
  由于 vol smile, 深度 OTM Put 天然高于中位数 — 该指标测的是 skew 陡峭度,
  不是 vol 贵不贵, 导致评分系统性偏好尾部风险定价不足的深虚值合约。

解决:
  用时序 IV Rank 衡量 "当前 IV 在历史分布中的位置",
  数据来源: state_persistence.py 的 iv_surface_history
  (每 3 分钟一次, 最长 7 天, 含 global mean/median 和各到期日 p25/p75)

  IV Rank = 当前 global median IV 在历史 global median 分布中的百分位 (0-100)
  100 = IV 处于 7 天最高点 (适合卖方)
  0 = IV 处于 7 天最低点 (不适合卖方)
"""

import logging

log = logging.getLogger(__name__)


def calc_iv_rank(current_iv: float,
                 history: list,
                 lookback_hours: int = 168) -> tuple[float, str]:
    """
    计算时序 IV Rank

    Args:
        current_iv: 当前全局 median IV
        history: state_persistence.get_iv_surface_history() 返回的快照列表
                 每个元素: {"time": float, "global_median": float, ...}
        lookback_hours: 回看小时数 (默认 168 = 7 天)

    Returns:
        (iv_rank, status_note)
        iv_rank: 0-100 的百分位, 数据不足时返回 50 (中性)
        status_note: "" 或 "数据积累中" 等说明

    冷启动处理:
        - history 为空 → 返回 (50, "数据积累中")
        - history 不足 24 小时 → 返回 (50, "数据积累中(N小时)")
        - history 为 None → 返回 (50, "数据积累中")
    """
    if not history:
        return 50.0, "数据积累中"

    # 提取历史 global median IV 序列
    historical_ivs = []
    for snap in history:
        gm = snap.get("global_median", 0)
        if gm > 0:
            historical_ivs.append(gm)

    if len(historical_ivs) < 8:
        # 不到 8 个数据点 (~24 分钟), 数据太少
        return 50.0, "数据积累中"

    # 检查是否积累超过 24 小时
    import time
    now = time.time()
    earliest = min(snap.get("time", now) for snap in history)
    hours_accumulated = (now - earliest) / 3600

    if hours_accumulated < 24:
        # 不足 24 小时, 返回中性分并标注
        return 50.0, f"数据积累中({hours_accumulated:.0f}h)"

    # 计算百分位: 当前 IV 在历史分布中排第几
    count_below = sum(1 for iv in historical_ivs if iv < current_iv)
    count_equal = sum(1 for iv in historical_ivs if iv == current_iv)
    n = len(historical_ivs)

    # 百分位 = (低于当前值的比例 + 等于当前值的一半比例) × 100
    iv_rank = (count_below + count_equal * 0.5) / n * 100

    return round(iv_rank, 1), ""


def calc_iv_rank_score(iv_rank: float) -> float:
    """
    将 IV Rank (0-100) 映射到评分 (0-100)

    规则:
      IV Rank ≥ 70 → 满分 100 (IV 高, 卖方最佳时机)
      IV Rank ≤ 30 → 零分 0 (IV 低, 不适合卖方)
      30-70 之间线性插值

    Args:
        iv_rank: 0-100

    Returns:
        评分 0-100
    """
    if iv_rank >= 70:
        return 100.0
    elif iv_rank <= 30:
        return 0.0
    else:
        return (iv_rank - 30) / 40 * 100


def blend_iv_scores(iv_rank_score: float,
                    cross_section_score: float,
                    has_sufficient_history: bool) -> float:
    """
    混合时序 IV Rank 和横截面 IV Premium 评分

    权重:
      时序 IV Rank: 70% (历史数据充足时)
      横截面 IV Premium: 30% (仍有捕捉个别合约错价的价值)

    如果历史数据不足, 权重反转 (横截面 70%, 时序 30%)

    Args:
        iv_rank_score: 时序 IV Rank 的评分 (0-100)
        cross_section_score: 横截面 IV Premium 的评分 (0-100)
        has_sufficient_history: 是否有足够的历史数据

    Returns:
        混合评分 (0-100)
    """
    if has_sufficient_history:
        return iv_rank_score * 0.70 + cross_section_score * 0.30
    else:
        return iv_rank_score * 0.30 + cross_section_score * 0.70


# ============================================================
#  模块自测
# ============================================================
if __name__ == "__main__":
    import time

    print("=== iv_rank 模块测试 ===\n")

    # 1. 空历史
    rank, note = calc_iv_rank(0.45, [])
    print(f"空历史: rank={rank}, note='{note}'")
    assert rank == 50 and "积累" in note

    # 2. 模拟历史数据 (7天, 每3分钟, IV 从 0.30 到 0.60 波动)
    import random
    random.seed(42)
    now = time.time()
    history = []
    for i in range(3360):  # 7天 * 24h * 60m / 3m
        t = now - (3360 - i) * 180
        iv = 0.40 + 0.10 * (i / 3360) + random.gauss(0, 0.02)
        history.append({"time": t, "global_median": max(iv, 0.10)})

    # 当前 IV = 0.50 (应该在历史中偏高)
    rank, note = calc_iv_rank(0.50, history)
    print(f"IV=0.50, history=7天: rank={rank}, note='{note}'")
    assert note == "", f"应该有足够数据, 但 note='{note}'"

    # IV = 0.35 (应该在历史中偏低)
    rank2, _ = calc_iv_rank(0.35, history)
    print(f"IV=0.35: rank={rank2}")

    # IV = 0.55 (应该在历史中很高)
    rank3, _ = calc_iv_rank(0.55, history)
    print(f"IV=0.55: rank={rank3}")

    assert rank3 > rank > rank2, "排名应该 0.55 > 0.50 > 0.35"

    # 3. 评分映射
    for r in [0, 20, 30, 50, 60, 70, 80, 100]:
        s = calc_iv_rank_score(r)
        print(f"  IV Rank {r:>3} → Score {s:>5.1f}")

    # 4. 混合评分
    blended = blend_iv_scores(80, 60, True)
    print(f"\n  混合(充足): IV_Rank=80 × 70% + CrossSection=60 × 30% = {blended:.1f}")
    assert abs(blended - 74) < 0.1

    blended2 = blend_iv_scores(80, 60, False)
    print(f"  混合(不足): IV_Rank=80 × 30% + CrossSection=60 × 70% = {blended2:.1f}")
    assert abs(blended2 - 66) < 0.1

    print("\n✅ iv_rank 模块测试通过")
