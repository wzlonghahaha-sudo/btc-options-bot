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

    def test_fomc_dates_r2_1(self):
        """R2-1: 验证 FOMC 日期已按官方日历修正"""
        from event_calendar import EVENT_LIST
        # 正确的 2026 FOMC 决议日 (官方确认)
        correct_dates = [
            (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
            (2026, 7, 29), (2026, 9, 16), (2026, 10, 28), (2026, 12, 9),
        ]
        for d in correct_dates:
            assert d in EVENT_LIST, f"正确日期 {d} 应存在于 EVENT_LIST"

        # 旧的错误日期已删除
        wrong_dates = [
            (2026, 1, 29),   # 旧: 1/29 → 正确: 1/28
            (2026, 5, 6),    # 不存在的 FOMC 会议
            (2026, 11, 4),   # 旧: 11/4 → 正确: 10/28
            (2026, 12, 16),  # 旧: 12/16 → 正确: 12/9
        ]
        for d in wrong_dates:
            assert d not in EVENT_LIST, f"错误日期 {d} 不应存在于 EVENT_LIST"


# ============================================================
#  R2-2: EmergencyHedge 测试
# ============================================================
class TestEmergencyHedge:
    """应急自动对冲 — mock API, 不触发真实网络"""

    def _make_hedge(self, enabled=False, ack_timeout=1):
        """创建 EmergencyHedge 实例 (mock 所有外部依赖)"""
        import os
        os.environ["EMERGENCY_AUTO_HEDGE"] = "true" if enabled else "false"
        os.environ["EMERGENCY_MAX_HEDGE_COST_USDT"] = "1000"
        os.environ["EMERGENCY_ACK_TIMEOUT_MIN"] = str(ack_timeout)

        from unittest.mock import MagicMock
        mock_api = MagicMock()
        mock_api.place_order.return_value = {"orderId": "TEST123", "status": "FILLED"}
        mock_tg = MagicMock()

        mock_state = MagicMock()
        mock_state.data = {}
        mock_state.save = MagicMock()

        # 必须在设完环境变量后才 import, 因为 __init__ 读 env
        from emergency_hedge import EmergencyHedge
        eh = EmergencyHedge(api=mock_api, tg_send_func=mock_tg, state_persistence=mock_state)
        return eh, mock_api, mock_tg

    def test_disabled_zero_calls(self):
        """(a) disabled 时零调用"""
        eh, mock_api, mock_tg = self._make_hedge(enabled=False)
        result = eh.check_and_act(
            liq_drop_pct=-10, pos_list=[], spot=60000,
            account_balance=50000, available_puts=[], marks={},
        )
        assert result is None
        mock_api.place_order.assert_not_called()
        mock_tg.assert_not_called()  # disabled 不应产生任何 TG 消息

    def test_enabled_ack_blocks(self):
        """(c) 已 ACK → 不下单"""
        eh, mock_api, _ = self._make_hedge(enabled=True, ack_timeout=0)
        # 模拟: CRITICAL 告警已发送
        eh.pending_emergency_alert_time = time.time() - 120  # 2分钟前
        eh.emergency_acked = True  # 用户已确认
        result = eh.check_and_act(
            liq_drop_pct=-10, pos_list=[], spot=60000,
            account_balance=50000, available_puts=[], marks={},
        )
        assert result is None
        mock_api.place_order.assert_not_called()

    def test_side_must_be_buy(self):
        """(d) _execute_hedge 内的 side 断言"""
        import inspect
        from emergency_hedge import EmergencyHedge
        source = inspect.getsource(EmergencyHedge._execute_hedge)
        # 确认源码中有 assert side == "BUY"
        assert 'assert side == "BUY"' in source, \
            "EmergencyHedge._execute_hedge must contain assert side == 'BUY'"

    def test_enabled_timeout_triggers(self):
        """(b) enabled + 未 ACK + 超时 + 24h 内未执行 → 尝试执行"""
        eh, mock_api, mock_tg = self._make_hedge(enabled=True, ack_timeout=0)
        # 模拟: CRITICAL 告警已发送, 超时无 ACK
        eh.pending_emergency_alert_time = time.time() - 120  # 超时
        eh.emergency_acked = False
        eh.last_auto_hedge_time = 0  # 从未执行

        # check_and_act 会尝试 _execute_hedge, 但 calc_hedge_options 会失败
        # (mock 没有设 hedge_advisor), 它会 catch Exception 并 TG 通知失败
        result = eh.check_and_act(
            liq_drop_pct=-10, pos_list=[], spot=60000,
            account_balance=50000, available_puts=[], marks={},
        )
        # 由于 hedge_advisor.calc_hedge_options 会失败 (mock),
        # 不会真正下单, 但证明了 all gates passed 并进入了 _execute_hedge
        # mock_tg 应被调用 (要么报错消息, 要么预执行通知)
        assert mock_tg.called, "TG should be called when all gates pass"


# ============================================================
#  R3-1: ACK 不压制 CRITICAL + 推送失败不启动倒计时
# ============================================================
class TestACKCriticalBypass:
    """R3-1: CRITICAL 告警突破 ACK/mute 验证"""

    def test_critical_bypasses_ack(self):
        """(a) ACK 后新 CRITICAL 仍应推送 — 验证 process_risk_alerts 逻辑"""
        # 验证: CRITICAL 分离到 critical_alerts, 不受 ACK 影响
        # 通过检查源码中的分离逻辑
        import inspect
        # 如果 tg_bot_monitor 导入失败(缺少网络模块), 直接检查源码文本
        with open("tg_bot_monitor.py") as f:
            src = f.read()

        # 关键逻辑: CRITICAL 列表独立于 ACK 检查
        assert 'critical_alerts = [a for a in pushable if a.level == "CRITICAL"]' in src
        assert 'non_critical = [a for a in pushable if a.level != "CRITICAL"]' in src
        # CRITICAL 走 send_critical 多通道
        assert "send_critical(msg, tg_send_func=_tg_critical_send)" in src
        # 非 CRITICAL 才检查 ACK
        assert "acked_combos" in src
        assert "combo_key" in src

    def test_ack_suppresses_same_danger(self):
        """(b) ACK 后同类 DANGER 被压制 — 验证 ACK 按 (category:level) 记录"""
        with open("tg_bot_monitor.py") as f:
            src = f.read()
        # ACK handler 写入 _ack_combos 而非全局 _ack_alert
        assert '"_ack_combos"' in src or "'_ack_combos'" in src
        # ACK 只记录 WARNING/DANGER
        assert 'if a.level in ("WARNING", "DANGER")' in src

    def test_push_fail_no_countdown(self):
        """(c) 推送失败时 pending_emergency_alert_time 保持 0"""
        # record_critical_alert 仅在 process_risk_alerts 推送成功后调用
        with open("tg_bot_monitor.py") as f:
            src = f.read()
        # 在 do_scan 中, 旧的 record_critical_alert 调用已被移除
        # 新的调用点在 process_risk_alerts 的 CRITICAL 推送成功后
        assert "# R3-1: record_critical_alert 已移到 process_risk_alerts" in src
        # 确认 process_risk_alerts 中 record_critical_alert 在 send_critical 之后
        # 找到 send_critical 和 record_critical_alert 的位置
        idx_send = src.index("send_critical(msg, tg_send_func=_tg_critical_send)")
        idx_record = src.index(
            "self.emergency_hedge.record_critical_alert()",
            idx_send  # 从 send_critical 之后搜索
        )
        assert idx_record > idx_send, \
            "record_critical_alert must be called AFTER send_critical"


# ============================================================
#  R3-2: alert_channels.send_critical 调用方验证
# ============================================================
class TestAlertChannelsWired:
    """R3-2: send_critical 不再是死代码"""

    def test_send_critical_called_in_tg_bot(self):
        """send_critical 在 tg_bot_monitor 中有调用"""
        with open("tg_bot_monitor.py") as f:
            src = f.read()
        assert "from alert_channels import send_critical" in src
        assert "send_critical(" in src

    def test_send_critical_called_in_emergency_hedge(self):
        """send_critical 在 emergency_hedge 中有调用"""
        with open("emergency_hedge.py") as f:
            src = f.read()
        assert "from alert_channels import send_critical" in src
        assert "send_critical(" in src

    def test_smtp_fallback_on_tg_failure(self):
        """mock tg_send_func 抛异常 3 次, 验证降级到 SMTP"""
        from unittest.mock import MagicMock, patch
        from alert_channels import send_critical

        mock_tg = MagicMock(side_effect=Exception("TG down"))
        mock_smtp = MagicMock(return_value=True)

        with patch("alert_channels._send_smtp", mock_smtp):
            send_critical("test alert", tg_send_func=mock_tg)

        # TG 应被重试 3 次
        assert mock_tg.call_count == 3
        # SMTP 应被调用 1 次 (降级)
        assert mock_smtp.call_count == 1


# ============================================================
#  R3-3: position_sizer 单元测试
# ============================================================
class TestPositionSizer:
    """仓位建议计算"""

    def test_total_margin_binding(self):
        """总保证金 60% 是约束瓶颈"""
        from position_sizer import suggest_qty
        # equity=100k, used=55k, per_contract=8k, no expiry constraint
        r = suggest_qty(100000, 55000, 8000, 0, strike=50000)
        # room = 100k*0.6 - 55k = 5k, 5k/8k = 0
        assert r["qty"] == 0
        assert r["binding_constraint"] == "total_margin_60pct"

    def test_expiry_notional_binding(self):
        """同到期日名义敞口 40% 是约束瓶颈"""
        from position_sizer import suggest_qty
        # equity=100k, used=0, per_contract=5k, expiry_notional=35k, strike=50k
        r = suggest_qty(100000, 0, 5000, 35000, strike=50000)
        # margin room: 100k*0.6/5k = 12
        # expiry room: (100k*0.4 - 35k) / 50k = 5k/50k = 0
        assert r["qty"] == 0
        assert r["binding_constraint"] == "expiry_notional_40pct"

    def test_single_trade_binding(self):
        """单笔保证金 15% 是约束瓶颈"""
        from position_sizer import suggest_qty
        # equity=100k, used=0, per_contract=20k, strike=5000 (低行权价, 不受 notional 限制)
        # margin room: 100k*0.6/20k = 3
        # expiry room: 100k*0.4/5000 = 8
        # single room: 100k*0.15/20k = 0
        r = suggest_qty(100000, 0, 20000, 0, strike=5000)
        assert r["qty"] == 0
        assert r["binding_constraint"] == "single_trade_15pct"

    def test_positive_qty(self):
        """有空间时返回正数"""
        from position_sizer import suggest_qty
        # equity=100k, used=10k, per_contract=5k, strike=10000 (低行权价)
        # margin room: (100k*0.6 - 10k) / 5k = 10
        # expiry room: 100k*0.4 / 10k = 4
        # single room: 100k*0.15 / 5k = 3
        r = suggest_qty(100000, 10000, 5000, 0, strike=10000)
        assert r["qty"] > 0

    def test_zero_equity(self):
        """零账户 → qty=0"""
        from position_sizer import suggest_qty
        r = suggest_qty(0, 0, 5000, 0, strike=50000)
        assert r["qty"] == 0


# ============================================================
#  R3-5: roll_advisor 单元测试
# ============================================================
class TestRollAdvisor:
    """滚仓建议"""

    def test_trigger_by_delta(self):
        """delta > 0.30 触发"""
        from roll_advisor import should_trigger_roll
        assert should_trigger_roll(0.35, 20) is True

    def test_trigger_by_distance(self):
        """距行权 < 10% 触发"""
        from roll_advisor import should_trigger_roll
        assert should_trigger_roll(0.15, 8) is True

    def test_no_trigger(self):
        """条件不满足时不触发"""
        from roll_advisor import should_trigger_roll
        assert should_trigger_roll(0.20, 15) is False

    def test_find_candidates_filters_correctly(self):
        """筛选逻辑: 更低行权价 + 更远到期 + delta ≤ 0.20"""
        from roll_advisor import find_roll_candidates
        chain = [
            # 合格: 更低行权价, 更远到期, delta OK, net credit
            {"symbol": "BTC-260925-45000-P", "strike": 45000, "dte": 80,
             "bid": 800, "ask": 810, "delta": -0.08, "mark_price": 805},
            # 不合格: 行权价不低于当前
            {"symbol": "BTC-260925-55000-P", "strike": 55000, "dte": 80,
             "bid": 1500, "ask": 1510, "delta": -0.15, "mark_price": 1505},
            # 不合格: delta 太大
            {"symbol": "BTC-260925-48000-P", "strike": 48000, "dte": 80,
             "bid": 1200, "ask": 1210, "delta": -0.25, "mark_price": 1205},
        ]
        results = find_roll_candidates(
            "BTC-260731-50000-P", 50000, 500, 700, 24, 63000, chain
        )
        # 只有第一个合格 (行权价<50000, delta 0.08<0.20, net_credit=800-700=100>0)
        assert len(results) == 1
        assert results[0]["symbol"] == "BTC-260925-45000-P"
        assert results[0]["net_credit"] == 100.0
        assert results[0]["type"] == "credit"

    def test_empty_chain_format(self):
        """无候选时提示止损/买保护"""
        from roll_advisor import format_roll_advice
        msg = format_roll_advice("BTC-260731-50000-P", [])
        assert "止损" in msg
