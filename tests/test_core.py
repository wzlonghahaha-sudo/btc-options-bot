"""
核心纯函数测试 — 不依赖网络或 API key

覆盖模块:
  - margin_calc: BS定价, 保证金计算, 压力IV
  - iv_rank: IV百分位计算
  - risk_rules: 止损评估
  - event_calendar: 事件日历跨越检测
"""

import time
import pytest
from datetime import date

from margin_calc import (
    bs_put_price,
    calc_put_margin,
    calc_maint_margin,
    _calc_stressed_iv,
)
from iv_rank import calc_iv_rank
from risk_rules import evaluate_stop_loss
from event_calendar import position_crosses_event


# ============================================================
#  a. bs_put_price: 3组已知 Black-Scholes 值回归测试
# ============================================================

class TestBSPutPrice:
    """Black-Scholes 欧式 Put 定价回归测试"""

    def test_atm_1yr(self):
        """S=100, K=100, T=1yr, σ=0.20, r=0.05 → ~5.57"""
        price = bs_put_price(spot=100, strike=100, dte_days=365, iv=0.20, r=0.05)
        assert price == pytest.approx(5.57, rel=0.02)

    def test_itm_put_half_year(self):
        """S=100, K=110, T=0.5yr, σ=0.30, r=0.05 → ~12.87"""
        price = bs_put_price(spot=100, strike=110, dte_days=182.5, iv=0.30, r=0.05)
        assert price == pytest.approx(12.87, rel=0.02)

    def test_btc_otm_put_quarter(self):
        """S=50000, K=45000, T=0.25yr, σ=0.50, r=0.05 → ~2427"""
        price = bs_put_price(spot=50000, strike=45000, dte_days=91.25, iv=0.50, r=0.05)
        assert price == pytest.approx(2427, rel=0.02)

    def test_expired_itm(self):
        """到期且ITM: 返回内在价值"""
        price = bs_put_price(spot=90, strike=100, dte_days=0, iv=0.30)
        assert price == 10.0

    def test_expired_otm(self):
        """到期且OTM: 返回0"""
        price = bs_put_price(spot=110, strike=100, dte_days=0, iv=0.30)
        assert price == 0.0


# ============================================================
#  b. calc_put_margin / calc_maint_margin: 校准数据验证
# ============================================================

class TestMarginCalc:
    """保证金计算 (币安校准公式)"""

    def test_initial_margin_includes_mark(self):
        """初始保证金应包含 mark_price 分量"""
        spot, strike, qty, mark = 64000, 56000, 1, 2000

        margin_with_mark = calc_put_margin(spot, strike, qty, mark_price=mark)
        margin_no_mark = calc_put_margin(spot, strike, qty, mark_price=0)

        # mark_price 应直接加到保证金上
        assert margin_with_mark == margin_no_mark + mark

    def test_maint_less_than_initial(self):
        """维持保证金应小于初始保证金"""
        spot, strike, qty, mark = 64000, 56000, 1, 2000

        initial = calc_put_margin(spot, strike, qty, mark_price=mark)
        maint = calc_maint_margin(spot, strike, qty, mark_price=mark)

        assert maint < initial

    def test_otm_margin_formula(self):
        """OTM Put: 验证保证金公式计算正确"""
        spot, strike = 64000, 56000
        # OTM amount = 64000 - 56000 = 8000
        # margin_component = max(64000*0.15 - 8000, 64000*0.10)
        #                  = max(9600 - 8000, 6400) = max(1600, 6400) = 6400
        # margin_per = 0 + 6400 = 6400 (mark_price=0)
        margin = calc_put_margin(spot, strike, 1, mark_price=0)
        assert margin == pytest.approx(6400, rel=0.001)

    def test_atm_margin_formula(self):
        """ATM Put: OTM amount = 0, 使用主公式"""
        spot = 64000
        strike = 64000
        # OTM amount = 0
        # margin_component = max(64000*0.15 - 0, 64000*0.10) = max(9600, 6400) = 9600
        margin = calc_put_margin(spot, strike, 1, mark_price=0)
        assert margin == pytest.approx(9600, rel=0.001)

    def test_qty_scaling(self):
        """保证金应按数量线性缩放"""
        spot, strike, mark = 64000, 56000, 2000
        m1 = calc_put_margin(spot, strike, 1, mark)
        m3 = calc_put_margin(spot, strike, 3, mark)
        assert m3 == pytest.approx(m1 * 3, rel=0.001)


# ============================================================
#  c. _calc_stressed_iv: IV冲击双模式分段验证
# ============================================================

class TestStressedIV:
    """压力场景下的 IV 计算 (双模式: 线性加点 vs 乘数放大)"""

    def test_btc_up_iv_decreases(self):
        """BTC +5% → IV 应下降"""
        iv = 0.45
        stressed = _calc_stressed_iv(iv, drop_pct=5)
        assert stressed < iv

    def test_btc_down_10_iv_increases(self):
        """BTC -10% → IV 应上升, 且 >= original * 1.3"""
        iv = 0.45
        stressed = _calc_stressed_iv(iv, drop_pct=-10)
        assert stressed > iv
        assert stressed >= iv * 1.3

    def test_btc_down_30_iv_doubles(self):
        """BTC -30% → IV >= original * 2.0"""
        iv = 0.45
        stressed = _calc_stressed_iv(iv, drop_pct=-30)
        assert stressed >= iv * 2.0

    def test_btc_down_40_iv_extreme(self):
        """BTC -40% → IV >= original * 2.5"""
        iv = 0.45
        stressed = _calc_stressed_iv(iv, drop_pct=-40)
        assert stressed >= iv * 2.5

    def test_floor_at_010(self):
        """IV 不应低于 0.10"""
        stressed = _calc_stressed_iv(0.10, drop_pct=50)
        assert stressed >= 0.10

    def test_monotonic_with_drop(self):
        """IV 应随跌幅增大而单调递增"""
        iv = 0.45
        iv_5 = _calc_stressed_iv(iv, drop_pct=-5)
        iv_10 = _calc_stressed_iv(iv, drop_pct=-10)
        iv_20 = _calc_stressed_iv(iv, drop_pct=-20)
        iv_30 = _calc_stressed_iv(iv, drop_pct=-30)
        iv_40 = _calc_stressed_iv(iv, drop_pct=-40)
        assert iv_5 < iv_10 < iv_20 < iv_30 < iv_40


# ============================================================
#  d. calc_iv_rank: 空历史 + 已知分布百分位
# ============================================================

class TestIVRank:
    """IV Rank 时序百分位计算"""

    def test_empty_history_returns_50(self):
        """空历史 → 返回 50 (中性)"""
        rank, note = calc_iv_rank(0.45, [])
        assert rank == 50.0
        assert "积累" in note

    def test_none_history_returns_50(self):
        """None 历史 → 返回 50"""
        rank, note = calc_iv_rank(0.45, None)
        assert rank == 50.0

    def test_insufficient_data_returns_50(self):
        """数据点不足 (<8) → 返回 50"""
        history = [{"time": time.time(), "global_median": 0.40}] * 5
        rank, note = calc_iv_rank(0.45, history)
        assert rank == 50.0

    def test_known_distribution_percentile(self):
        """已知分布: IV=0.50 应在 [0.30..0.60] 均匀分布中排偏高"""
        now = time.time()
        history = []
        # 生成 30 天的数据 (足够 > 24h), 均匀分布 0.30 ~ 0.60
        n = 500
        for i in range(n):
            t = now - (n - i) * 3600  # 每小时一个点, 500 小时 ≈ 20 天
            iv = 0.30 + 0.30 * (i / (n - 1))  # 0.30 到 0.60
            history.append({"time": t, "global_median": iv})

        rank, note = calc_iv_rank(0.50, history)
        # 0.50 在 [0.30, 0.60] 中的位置: (0.50-0.30)/(0.60-0.30) ≈ 66.7%
        assert note == ""  # 数据充足, 无特殊标注
        assert 60 < rank < 75  # 大约 2/3 的位置

    def test_rank_ordering(self):
        """低/中/高 IV 的排名应递增"""
        now = time.time()
        history = []
        n = 500
        for i in range(n):
            t = now - (n - i) * 3600
            iv = 0.30 + 0.30 * (i / (n - 1))
            history.append({"time": t, "global_median": iv})

        rank_low, _ = calc_iv_rank(0.35, history)
        rank_mid, _ = calc_iv_rank(0.45, history)
        rank_high, _ = calc_iv_rank(0.55, history)

        assert rank_low < rank_mid < rank_high


# ============================================================
#  e. evaluate_stop_loss: 4种止损场景
# ============================================================

class TestStopLoss:
    """止损评估 (复合条件)"""

    def test_small_loss_far_otm(self):
        """loss=0.5, dist=25, delta=0.05 → NONE"""
        result = evaluate_stop_loss(loss_ratio=0.5, dist_to_strike_pct=25, abs_delta=0.05)
        assert result.level == "NONE"

    def test_2x_loss_far_otm_downgraded(self):
        """loss=2.0, dist=25, delta=0.10 → WARNING (从 DANGER 降级, IV 驱动)"""
        result = evaluate_stop_loss(loss_ratio=2.0, dist_to_strike_pct=25, abs_delta=0.10)
        assert result.level == "WARNING"
        assert not result.is_directional
        assert "IV" in result.detail

    def test_2x_loss_near_strike_directional(self):
        """loss=2.0, dist=10, delta=0.35 → DANGER (方向风险)"""
        result = evaluate_stop_loss(loss_ratio=2.0, dist_to_strike_pct=10, abs_delta=0.35)
        assert result.level == "DANGER"
        assert result.is_directional

    def test_critical_close_strike(self):
        """loss=3.5, dist=5, delta=0.50 → CRITICAL"""
        result = evaluate_stop_loss(loss_ratio=3.5, dist_to_strike_pct=5, abs_delta=0.50)
        assert result.level == "CRITICAL"
        assert result.is_directional

    def test_profitable_position(self):
        """盈利中 → NONE"""
        result = evaluate_stop_loss(loss_ratio=-0.5, dist_to_strike_pct=30, abs_delta=0.05)
        assert result.level == "NONE"


# ============================================================
#  f. position_crosses_event: 事件日历跨越检测
# ============================================================

class TestEventCalendar:
    """事件日历跨越检测"""

    def test_crosses_fomc(self):
        """跨 FOMC 日期区间 → 返回事件"""
        # 2026-07-29 是 FOMC 利率决议
        open_date = date(2026, 7, 1)
        expiry_date = date(2026, 8, 15)
        events = position_crosses_event(open_date, expiry_date)
        assert len(events) > 0
        fomc_names = [e.name for e in events]
        assert any("FOMC" in n for n in fomc_names)

    def test_short_range_no_events(self):
        """短日期区间 (远离任何事件) → 空列表"""
        # 2026-08-01 ~ 2026-08-05: 无 FOMC/CPI
        open_date = date(2026, 8, 1)
        expiry_date = date(2026, 8, 5)
        events = position_crosses_event(open_date, expiry_date)
        assert len(events) == 0

    def test_level_filter(self):
        """按等级过滤事件"""
        open_date = date(2026, 7, 1)
        expiry_date = date(2026, 9, 30)
        high_events = position_crosses_event(open_date, expiry_date, level="HIGH")
        # 应至少包含 FOMC 7/29, CPI 7/14, CPI 8/12, CPI 9/10, FOMC 9/16
        assert len(high_events) >= 4

    def test_no_events_outside_range(self):
        """所有事件之外的范围 → 空"""
        open_date = date(2028, 1, 1)
        expiry_date = date(2028, 1, 5)
        events = position_crosses_event(open_date, expiry_date)
        assert len(events) == 0
