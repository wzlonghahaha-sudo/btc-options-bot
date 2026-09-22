"""
UX 交互优化相关测试 — 纯函数, 不依赖网络
"""

from ux_copy import (
    help_msg, rules_msg, strategy_msg, build_now_message,
    status_msg, order_copy_text, mode_help, _live_thresholds,
)
from opportunity_scanner import ScanConfig
from risk_rules import LOSS_WARN_RATIO, DIST_WARN_PCT
from push_control import MAX_SIGNAL_PUSH_PER_DAY, MIN_PUSH_SCORE


class TestUxCopySyncedWithConfig:
    """用户可见文案必须与代码门槛一致"""

    def test_help_contains_live_push_score(self):
        text = help_msg("hunt")
        assert f"≥{ScanConfig.SCORE_PUSH}" in text
        assert str(MAX_SIGNAL_PUSH_PER_DAY) in text
        assert "/now" in text
        assert "/mode" in text

    def test_rules_contains_tiers_and_stop_loss(self):
        text = rules_msg()
        assert "保守型" in text
        assert "均衡型" in text
        assert "激进型" in text
        assert f"{LOSS_WARN_RATIO:.0f}x" in text
        assert f"{DIST_WARN_PCT:.0f}%" in text
        # 不再写死旧的「仅 delta≤0.05」单一门槛作为唯一规则
        assert "三档机会" in text

    def test_strategy_is_short(self):
        text = strategy_msg()
        assert "30秒" in text
        assert len(text) < 1200

    def test_live_thresholds_keys(self):
        t = _live_thresholds()
        assert t["score_push"] == ScanConfig.SCORE_PUSH
        assert t["min_push_score"] == MIN_PUSH_SCORE
        assert t["daily_push_limit"] == MAX_SIGNAL_PUSH_PER_DAY


class TestNowMessage:
    def test_cold_start(self):
        msg = build_now_message({"ready": False})
        assert "首次扫描尚未完成" in msg
        assert "/scan" in msg

    def test_hot_with_risk(self):
        msg = build_now_message({
            "ready": True,
            "spot": 95000,
            "change_pct": -1.2,
            "mode": "hold",
            "pos_count": 2,
            "total_pnl": -500,
            "nearest_dist": 9.5,
            "worst_alert": "DANGER",
            "liq_drop_pct": -18,
            "best_opp": None,
            "push_used": 1,
            "push_limit": 5,
            "push_suppressed": 0,
            "score_push": 78,
            "next_action": "处理风险持仓 → /hedge",
        })
        assert "DANGER" in msg
        assert "下一步" in msg
        assert "推送预算 1/5" in msg

    def test_order_copy(self):
        text = order_copy_text("BTC-27JUN-72000-P", 2, 300, 295)
        assert "SELL 2x" in text
        assert "27JUN-72000-P" in text
        assert "BTC-27JUN-72000-P" in text

    def test_mode_help(self):
        text = mode_help("quiet")
        assert "quiet" in text
        assert "hunt" in text
        assert "hold" in text

    def test_status_includes_push_budget(self):
        text = status_msg(
            90000, 3, "1h 2m", 12.5, 180,
            push_status={"daily_signal_count": 2, "daily_limit": 5, "suppressed_today": 1},
            mode="hunt",
        )
        assert "2/5" in text
        assert "压制" in text
        assert "/now" in text


class TestJournalMarkActed:
    def test_mark_signal_acted(self, tmp_path):
        from trade_journal import TradeJournal, SignalRecord
        path = tmp_path / "journal.json"
        j = TradeJournal(filepath=str(path))
        j.record_signal(SignalRecord(
            symbol="BTC-27JUN-72000-P",
            signal_level="SIGNAL",
            score=82,
            bid=300,
            annual_return=40,
            safety_pct=25,
            iv_premium=10,
            spot=95000,
            timestamp=1.0,
            source="v2",
        ))
        assert j.mark_signal_acted("27JUN-72000-P") is True
        assert j.data["signals"][-1]["user_acted"] is True
        hit = j.signal_hit_rate()
        assert hit["acted_on"] == 1


class TestOpportunityButtons:
    def test_callback_data_length(self):
        from ux_copy import opportunity_action_buttons
        buttons = opportunity_action_buttons("27JUN-72000-P")
        assert len(buttons) == 4
        for b in buttons:
            assert len(b["callback_data"]) <= 64


class TestFormatTopOpenable:
    def test_openable_only_filters(self):
        from opportunity_scanner import format_opportunities_tg, Opportunity, AccountRisk

        class FakeOpp:
            def __init__(self, symbol, score, can_open, tier="balanced"):
                self.symbol = symbol
                self.score = score
                self.can_open = can_open
                self.tier = tier
                self.tier_label = "均衡"
                self.bid = 100
                self.ask = 110
                self.annual_return = 30
                self.safety_pct = 20
                self.otm_pct = 20
                self.delta = -0.08
                self.iv = 0.5
                self.iv_premium = 10
                self.dte = 30
                self.spread_pct = 2
                self.pros = ["ok"]
                self.cons = []
                self.margin_required = 5000
                self.new_portfolio_delta = 0.1
                self.new_margin_usage = 40
                self.risk_notes = ["风控通过 ✅"]
                self.iv_hv_ratio = 1.2

        account = AccountRisk(
            total_balance=100000, used_margin=10000,
            available_margin=90000, margin_usage_pct=10,
            portfolio_delta=0.05, portfolio_theta=50, position_count=1,
        )
        opps = [
            FakeOpp("BTC-A-P", 80, True),
            FakeOpp("BTC-B-P", 80, False),
            FakeOpp("BTC-C-P", 40, True),  # below SCORE_SIGNAL
        ]
        msg = format_opportunities_tg(opps, account, hv_20=0.4, iv_mean=0.5,
                                      openable_only=True)
        assert "BTC-A-P" in msg
        assert "BTC-B-P" not in msg
        assert "可开仓机会" in msg
