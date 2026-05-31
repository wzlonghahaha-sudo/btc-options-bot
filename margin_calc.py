"""
统一保证金计算 + Black-Scholes 定价 + 强平价格估算

三层功能:
  1. 保证金公式 — 所有模块共用
  2. Black-Scholes 定价 — 给定 BTC 价格/IV/DTE 计算 Put 理论价
  3. 强平价格反算 — 找到 BTC 跌到多少时账户保证金不足
  4. 压力测试 — BTC 跌 X% 时组合的总亏损和保证金变化

币安欧式期权保证金公式 (卖 Put):
  初始保证金 = max(标的价格 * 初始保证金率 - OTM金额, 标的价格 * 最低保证金率)
  维持保证金 = max(标的价格 * 维持保证金率 - OTM金额, 标的价格 * 最低维持率)
"""

import math
from dataclasses import dataclass


# ============================================================
#  保证金率常量
# ============================================================
INITIAL_MARGIN_RATE = 0.15
MIN_MARGIN_RATE = 0.075
MAINT_MARGIN_RATE = 0.075  # 维持保证金 ≈ 最低保证金率


# ============================================================
#  保证金计算 (原有)
# ============================================================
def calc_put_margin(spot: float, strike: float, qty: float = 1.0) -> float:
    """计算卖出 Put 期权的初始保证金"""
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


# ============================================================
#  Black-Scholes 定价
# ============================================================
def _norm_cdf(x: float) -> float:
    """标准正态分布 CDF (近似, 精度 ~1e-7)"""
    # Abramowitz & Stegun approximation
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)
    return 0.5 * (1.0 + sign * y)


def bs_put_price(spot: float, strike: float, dte_days: float,
                 iv: float, r: float = 0.05) -> float:
    """
    Black-Scholes 欧式 Put 期权定价

    Args:
        spot: 标的价格
        strike: 行权价
        dte_days: 剩余天数
        iv: 隐含波动率 (年化, 如 0.45 = 45%)
        r: 无风险利率 (年化, 默认 5%)

    Returns:
        Put 期权理论价格 (USDT)
    """
    if dte_days <= 0:
        # 到期: intrinsic value
        return max(strike - spot, 0)
    if iv <= 0:
        return max(strike - spot, 0)

    T = dte_days / 365.0
    sqrt_T = math.sqrt(T)

    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * T) / (iv * sqrt_T)
    d2 = d1 - iv * sqrt_T

    put_price = (strike * math.exp(-r * T) * _norm_cdf(-d2)
                 - spot * _norm_cdf(-d1))
    return max(put_price, 0)


def bs_put_delta(spot: float, strike: float, dte_days: float,
                 iv: float, r: float = 0.05) -> float:
    """Black-Scholes Put Delta"""
    if dte_days <= 0 or iv <= 0:
        return -1.0 if spot < strike else 0.0
    T = dte_days / 365.0
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


# ============================================================
#  压力测试: BTC 跌 X% 时的组合亏损
# ============================================================
@dataclass
class StressResult:
    """单个压力场景的结果"""
    scenario: str          # "BTC -10%"
    btc_price: float       # 压力下的 BTC 价格
    btc_drop_pct: float    # 跌幅 %

    total_pnl: float           # 组合总 P&L
    total_margin_required: float  # 压力下需要的总保证金
    margin_shortfall: float    # 保证金缺口 (正数=不足)
    account_equity: float      # 账户净值 (余额 + 浮盈亏)

    positions: list            # 各持仓明细


def stress_test_portfolio(positions: list, spot: float,
                          account_balance: float,
                          scenarios: list[float] = None,
                          iv_shock: float = 0.10) -> list[StressResult]:
    """
    对持仓组合做压力测试

    Args:
        positions: 持仓列表, 每个 dict 含:
            symbol, qty, entry_price, strike, dte, iv, mark_price
        spot: 当前 BTC 价格
        account_balance: 账户可用余额 (含浮盈)
        scenarios: 跌幅场景列表 (如 [-5, -10, -15, -20, -30])
                   负数代表跌, 正数代表涨
        iv_shock: BTC 下跌时 IV 的上升幅度 (默认 +10%绝对值)
                  经验: BTC 每跌 10%, IV 大约上升 5-15%

    Returns:
        各场景的压力测试结果
    """
    if scenarios is None:
        scenarios = [-5, -10, -15, -20, -25, -30]

    results = []

    for drop_pct in scenarios:
        stressed_spot = spot * (1 + drop_pct / 100)
        scenario_name = f"BTC {drop_pct:+.0f}%"

        total_pnl = 0
        total_margin = 0
        pos_details = []

        for p in positions:
            qty = p.get("qty", 0)
            if qty == 0:
                continue

            abs_qty = abs(qty)
            strike = p.get("strike", 0)
            dte = p.get("dte", 30)
            iv = p.get("iv", 0.40)
            entry = p.get("entry_price", 0)
            current_mark = p.get("mark_price", 0)

            # IV 在下跌时上升 (vol-of-vol 效应)
            # 经验公式: BTC每跌10%, Put IV 约上升 +8%绝对值
            iv_increase = abs(drop_pct) / 10 * iv_shock if drop_pct < 0 else -abs(drop_pct) / 20 * iv_shock * 0.5
            stressed_iv = max(iv + iv_increase, 0.10)

            # BS 定价计算压力下的期权价格
            stressed_price = bs_put_price(stressed_spot, strike, dte, stressed_iv)

            # P&L
            if qty < 0:  # Short Put
                pnl = (entry - stressed_price) * abs_qty
            else:  # Long Put
                pnl = (stressed_price - entry) * abs_qty

            # 压力下的保证金需求 (仅 Short)
            margin = 0
            if qty < 0:
                margin = calc_put_margin(stressed_spot, strike, abs_qty)

            total_pnl += pnl
            total_margin += margin

            pos_details.append({
                "symbol": p.get("symbol", ""),
                "qty": qty,
                "strike": strike,
                "current_price": current_mark,
                "stressed_price": round(stressed_price, 2),
                "pnl": round(pnl, 2),
                "margin": round(margin, 2),
                "stressed_iv": round(stressed_iv, 4),
            })

        account_equity = account_balance + total_pnl
        margin_shortfall = max(total_margin - account_equity, 0)

        results.append(StressResult(
            scenario=scenario_name,
            btc_price=round(stressed_spot, 0),
            btc_drop_pct=drop_pct,
            total_pnl=round(total_pnl, 2),
            total_margin_required=round(total_margin, 2),
            margin_shortfall=round(margin_shortfall, 2),
            account_equity=round(account_equity, 2),
            positions=pos_details,
        ))

    return results


# ============================================================
#  强平价格估算 (二分法)
# ============================================================
def estimate_liquidation_price(positions: list, spot: float,
                               account_balance: float,
                               iv_shock: float = 0.10) -> dict:
    """
    估算预估强平价格: BTC 跌到多少时, 账户净值 < 维持保证金

    使用二分法搜索:
      在 [spot * 0.01, spot] 范围内找到使得
      account_equity(S) = account_balance + PnL(S)
      维持保证金(S) = sum(calc_maint_margin(S, strike, qty))
      满足 account_equity(S) <= 维持保证金(S) 的 S

    Args:
        positions: 持仓列表 (同 stress_test_portfolio)
        spot: 当前 BTC
        account_balance: 账户可用余额
        iv_shock: IV 冲击参数

    Returns:
        {
            liq_price: 预估强平价格,
            liq_drop_pct: 距当前价的跌幅%,
            cushion: 安全垫金额 (当前净值 - 维持保证金),
            details: 强平时各仓位明细,
        }
    """
    if not positions:
        return {"liq_price": 0, "liq_drop_pct": -100, "cushion": account_balance, "details": []}

    # 只计算 Short Put 持仓 (Long Put 在下跌时盈利, 反而是保护)
    short_puts = [p for p in positions if p.get("qty", 0) < 0]
    long_puts = [p for p in positions if p.get("qty", 0) > 0]

    if not short_puts:
        return {"liq_price": 0, "liq_drop_pct": -100, "cushion": account_balance, "details": []}

    def _calc_equity_vs_maint(test_spot: float) -> float:
        """返回 equity - maint_margin, 正数=安全, 负数=不足"""
        total_pnl = 0
        total_maint = 0
        drop_pct = (test_spot - spot) / spot * 100

        for p in short_puts:
            abs_qty = abs(p.get("qty", 0))
            strike = p.get("strike", 0)
            dte = p.get("dte", 30)
            iv = p.get("iv", 0.40)
            entry = p.get("entry_price", 0)

            iv_increase = abs(drop_pct) / 10 * iv_shock if drop_pct < 0 else 0
            stressed_iv = max(iv + iv_increase, 0.10)
            stressed_price = bs_put_price(test_spot, strike, dte, stressed_iv)

            pnl = (entry - stressed_price) * abs_qty
            maint = calc_maint_margin(test_spot, strike, abs_qty)

            total_pnl += pnl
            total_maint += maint

        # Long Put 在下跌时提供保护
        for p in long_puts:
            abs_qty = abs(p.get("qty", 0))
            strike = p.get("strike", 0)
            dte = p.get("dte", 30)
            iv = p.get("iv", 0.40)
            entry = p.get("entry_price", 0)

            iv_increase = abs(drop_pct) / 10 * iv_shock if drop_pct < 0 else 0
            stressed_iv = max(iv + iv_increase, 0.10)
            stressed_price = bs_put_price(test_spot, strike, dte, stressed_iv)

            pnl = (stressed_price - entry) * abs_qty
            total_pnl += pnl

        equity = account_balance + total_pnl
        return equity - total_maint

    # 当前安全垫
    current_cushion = _calc_equity_vs_maint(spot)

    # 如果当前已经不够, 直接返回
    if current_cushion <= 0:
        return {
            "liq_price": spot,
            "liq_drop_pct": 0,
            "cushion": round(current_cushion, 0),
            "details": [],
        }

    # 二分法搜索强平价格
    low = spot * 0.01   # BTC 跌到 1%
    high = spot
    liq_price = low     # 默认最坏情况

    for _ in range(60):  # 足够精度
        mid = (low + high) / 2
        gap = _calc_equity_vs_maint(mid)

        if gap > 0:
            # mid 处仍安全, 往更低搜
            high = mid
        else:
            # mid 处已不足, 往更高搜
            low = mid
            liq_price = mid

    # 精确到强平时的持仓明细
    liq_drop = (liq_price - spot) / spot * 100
    details = []
    for p in short_puts:
        abs_qty = abs(p.get("qty", 0))
        strike = p.get("strike", 0)
        dte = p.get("dte", 30)
        iv = p.get("iv", 0.40)
        entry = p.get("entry_price", 0)

        iv_increase = abs(liq_drop) / 10 * iv_shock
        stressed_iv = max(iv + iv_increase, 0.10)
        stressed_price = bs_put_price(liq_price, strike, dte, stressed_iv)
        pnl = (entry - stressed_price) * abs_qty

        details.append({
            "symbol": p.get("symbol", ""),
            "strike": strike,
            "stressed_price": round(stressed_price, 2),
            "pnl": round(pnl, 2),
            "maint_margin": round(calc_maint_margin(liq_price, strike, abs_qty), 2),
        })

    return {
        "liq_price": round(liq_price, 0),
        "liq_drop_pct": round(liq_drop, 1),
        "cushion": round(current_cushion, 0),
        "details": details,
    }
