"""Minimal offline smoke tests — no network, no Telegram sends."""

import margin_calc
import risk_rules
from message_spec import build_message


def test_core_imports_and_bs_put():
    price = margin_calc.bs_put_price(spot=100, strike=100, dte_days=365, iv=0.20)
    assert price > 0
    assert risk_rules.evaluate_stop_loss(0.5, 25, 0.05).level == "NONE"


def test_build_message_roundtrip():
    msg = build_message("**verdict**", ["line one"], "<b>play</b>")
    assert "verdict" in msg
    assert "line one" in msg
    assert "play" in msg
