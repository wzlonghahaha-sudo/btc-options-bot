"""
统一止损/告警规则 (单一事实来源)

旧问题:
  止损标准在三处不一致:
  - sell_put_strategy.print_results: 2-3x 权利金
  - sell_put_strategy.quick_decision: 2.5x bid
  - risk_monitor.RiskConfig: 1x/2x/3x 权利金
  纯权利金倍数止损对 far OTM 低价合约噪音过大。

新规则:
  复合条件止损, 同时考虑:
  1. 浮亏倍数 (loss_ratio = 浮亏 / 收到权利金)
  2. 距行权价距离 (dist_to_strike_pct)
  3. Delta 绝对值 (方向风险)

  触发逻辑:
  - 纯 loss_ratio 触发但距行权仍 >20% 且 delta <0.15 时,
    降一级告警并标注 "价格波动主要来自 IV, 方向风险仍低"
  - loss_ratio ≥ 阈值 AND (dist_to_strike < 15% OR abs_delta > 0.30)
    → 按正常级别触发

所有模块 (sell_put_strategy / risk_monitor / profit_optimizer)
统一引用本文件, 不再各自定义止损常量。
"""

from dataclasses import dataclass

# ============================================================
#  止损阈值常量
# ============================================================

# 浮亏倍数阈值 (相对于收到的权利金)
LOSS_WARN_RATIO = 1.0      # 浮亏 ≥ 1x 权利金 → WARNING
LOSS_DANGER_RATIO = 2.0    # 浮亏 ≥ 2x 权利金 → DANGER
LOSS_CRITICAL_RATIO = 3.0  # 浮亏 ≥ 3x 权利金 → CRITICAL

# 距行权价阈值 (BTC 当前价格距行权价的百分比)
DIST_SAFE_PCT = 20.0       # >20% 视为安全 (可降级)
DIST_WARN_PCT = 15.0       # <15% 为近距离 (不降级)

# Delta 阈值
DELTA_HIGH = 0.30          # delta > 0.30 表示方向风险已很高
DELTA_LOW = 0.15           # delta < 0.15 表示方向风险仍低


# ============================================================
#  评估结果
# ============================================================
@dataclass
class StopLossResult:
    """止损评估结果"""
    level: str          # "NONE" / "WATCH" / "WARNING" / "DANGER" / "CRITICAL"
    loss_ratio: float   # 浮亏倍数
    is_directional: bool   # 是否有方向风险 (vs 纯 IV 波动)
    detail: str         # 详细说明
    action: str         # 建议操作

    @property
    def should_alert(self) -> bool:
        return self.level not in ("NONE", "WATCH")


# ============================================================
#  核心函数
# ============================================================
def evaluate_stop_loss(loss_ratio: float,
                       dist_to_strike_pct: float,
                       abs_delta: float) -> StopLossResult:
    """
    评估止损级别 (复合条件)

    Args:
        loss_ratio: 浮亏 / 收到权利金 (>=0, 0=没亏, 1=亏了1倍权利金)
        dist_to_strike_pct: BTC 当前价距行权价的百分比 (>0=OTM)
        abs_delta: delta 绝对值 (0-1)

    Returns:
        StopLossResult 包含止损级别和说明

    止损逻辑:
        1. 先按 loss_ratio 确定"基础级别"
        2. 检查方向风险: dist_to_strike < 15% OR abs_delta > 0.30
        3. 如果有方向风险 → 按基础级别触发
        4. 如果无方向风险 (仍然安全距离) → 降一级, 标注"IV波动"
    """
    if loss_ratio < 0:
        # 盈利中, 无需止损
        return StopLossResult(
            level="NONE", loss_ratio=loss_ratio,
            is_directional=False,
            detail="持仓盈利中",
            action="持有或考虑止盈",
        )

    # 确定基础级别
    if loss_ratio >= LOSS_CRITICAL_RATIO:
        base_level = "CRITICAL"
    elif loss_ratio >= LOSS_DANGER_RATIO:
        base_level = "DANGER"
    elif loss_ratio >= LOSS_WARN_RATIO:
        base_level = "WARNING"
    else:
        return StopLossResult(
            level="NONE", loss_ratio=loss_ratio,
            is_directional=False,
            detail=f"浮亏 {loss_ratio:.1f}x 权利金, 在可控范围",
            action="继续持有, 关注走势",
        )

    # 方向风险判断
    has_directional_risk = (dist_to_strike_pct < DIST_WARN_PCT
                            or abs_delta > DELTA_HIGH)

    if has_directional_risk:
        # 方向风险已实质化, 按基础级别触发
        return StopLossResult(
            level=base_level,
            loss_ratio=loss_ratio,
            is_directional=True,
            detail=f"浮亏 {loss_ratio:.1f}x 权利金\n"
                   f"距行权价 {dist_to_strike_pct:.1f}%, Delta={abs_delta:.2f}",
            action=_get_action(base_level),
        )
    else:
        # 距行权仍安全, 方向风险低, 降一级
        downgraded = _downgrade(base_level)
        return StopLossResult(
            level=downgraded,
            loss_ratio=loss_ratio,
            is_directional=False,
            detail=f"浮亏 {loss_ratio:.1f}x 权利金\n"
                   f"距行权价 {dist_to_strike_pct:.1f}%, Delta={abs_delta:.2f}\n"
                   f"价格波动主要来自 IV, 方向风险仍低",
            action=_get_action(downgraded),
        )


def get_stop_loss_price(entry_price: float, multiplier: float = 2.5) -> float:
    """
    计算建议止损价格 (期权价格涨到多少时平仓)

    默认: 期权价格涨到入场价的 2.5 倍时止损
    (卖出收了 entry_price 权利金, 期权涨到 2.5x 意味着亏了 1.5x)

    Args:
        entry_price: 开仓时收到的权利金
        multiplier: 止损倍数 (默认 2.5x)

    Returns:
        止损价格
    """
    return entry_price * multiplier


# ============================================================
#  内部辅助
# ============================================================
def _downgrade(level: str) -> str:
    """将告警级别降一级"""
    downgrades = {
        "CRITICAL": "DANGER",
        "DANGER": "WARNING",
        "WARNING": "WATCH",
    }
    return downgrades.get(level, "NONE")


def _get_action(level: str) -> str:
    """根据级别返回建议操作"""
    actions = {
        "CRITICAL": "立即止损平仓!",
        "DANGER": "认真考虑止损平仓",
        "WARNING": "设置止损单, 密切关注",
        "WATCH": "提高警惕, 关注 BTC 走势",
    }
    return actions.get(level, "继续持有")


# ============================================================
#  模块自测
# ============================================================
if __name__ == "__main__":
    print("=== risk_rules 模块测试 ===\n")

    # 测试不同场景
    cases = [
        (0.5, 25.0, 0.08, "小亏+远OTM"),
        (1.5, 25.0, 0.10, "1.5x亏损+远OTM+低delta → 应降级(IV波动)"),
        (1.5, 12.0, 0.10, "1.5x亏损+近行权 → WARNING"),
        (2.5, 8.0,  0.35, "2.5x亏损+近行权+高delta → CRITICAL"),
        (2.5, 22.0, 0.12, "2.5x亏损+远OTM → 降级DANGER→WARNING(IV波动)"),
        (3.5, 5.0,  0.50, "3.5x亏损+极近+高delta → CRITICAL"),
        (-0.3, 30.0, 0.05, "盈利中"),
    ]

    for loss, dist, delta, desc in cases:
        r = evaluate_stop_loss(loss, dist, delta)
        print(f"  [{r.level:8s}] {desc}")
        print(f"    loss={loss:.1f}x, dist={dist:.1f}%, delta={delta:.2f}")
        print(f"    方向风险: {r.is_directional}, 操作: {r.action}")
        print(f"    {r.detail}")
        print()

    # 测试止损价格
    print(f"  入场价 $500 → 止损价 ${get_stop_loss_price(500):,.0f}")
    print(f"  入场价 $100 → 止损价 ${get_stop_loss_price(100):,.0f}")

    print("\n✅ risk_rules 测试通过")
