"""
统一保证金计算模块

所有模块共用同一套保证金公式, 确保计算一致性。

币安欧式期权保证金公式 (卖 Put):
  初始保证金 = max(标的价格 * 初始保证金率 - OTM金额, 标的价格 * 最低保证金率)
  维持保证金 = max(标的价格 * 维持保证金率 - OTM金额, 标的价格 * 最低维持率)

其中:
  OTM金额 = max(spot - strike, 0)  (对 Put 而言)
  初始保证金率 = 15%
  最低保证金率 = 7.5%
  维持保证金率 ≈ 初始保证金率 * 0.6 (约9%)
"""


# 保证金率常量
INITIAL_MARGIN_RATE = 0.15
MIN_MARGIN_RATE = 0.075
MAINT_MARGIN_RATE = 0.075  # 维持保证金 ≈ 最低保证金率


def calc_put_margin(spot: float, strike: float, qty: float = 1.0) -> float:
    """
    计算卖出 Put 期权的初始保证金 (单张或指定数量)

    Args:
        spot: BTC 现价
        strike: 行权价
        qty: 合约数量 (正数, 传入abs(qty))

    Returns:
        保证金金额 (USDT)
    """
    otm_amount = max(spot - strike, 0)
    margin_per = max(
        spot * INITIAL_MARGIN_RATE - otm_amount,
        spot * MIN_MARGIN_RATE,
    )
    return margin_per * abs(qty)


def calc_put_margin_per_contract(spot: float, strike: float) -> float:
    """计算单张保证金"""
    return calc_put_margin(spot, strike, 1.0)


def calc_maint_margin(spot: float, strike: float, qty: float = 1.0) -> float:
    """计算维持保证金"""
    otm_amount = max(spot - strike, 0)
    margin_per = max(
        spot * MAINT_MARGIN_RATE - otm_amount,
        spot * MIN_MARGIN_RATE * 0.6,
    )
    return margin_per * abs(qty)


def calc_margin_usage(mark_value: float, margin: float) -> float:
    """计算保证金使用率 (0-1)"""
    if margin <= 0:
        return 0
    return mark_value / margin
