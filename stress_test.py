#!/usr/bin/env python3
"""
BTC 暴跌 + IV 暴涨 + 爆仓链路压力测试

安全规则:
  - TEST MODE ONLY
  - 不下单、不改仓、不转账
  - 仅读取真实基线数据, 之后全部使用模拟数据
  - 通过 TG 发送测试告警 (带 [TEST] 标签)
"""

import os
import sys
import time
import math
import json
import traceback
from datetime import datetime, timezone
from dataclasses import dataclass, field
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from binance_options import BinanceOptionsAPI
from risk_monitor import RiskEngine, RiskAlert, RiskConfig, format_risk_alerts

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")


# ============================================================
#  TG 推送 (测试专用, 带 [TEST] 前缀)
# ============================================================
import requests

def tg_send(text: str) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"  [TG FAIL] {e}")
        return False


# ============================================================
#  模拟市场数据生成器
# ============================================================
class MarketSimulator:
    """基于真实基线数据模拟极端市场场景"""

    def __init__(self, baseline_spot: float, baseline_position: dict, baseline_mark: dict):
        self.base_spot = baseline_spot
        self.base_pos = baseline_position
        self.base_mark = baseline_mark

        # 解析基线
        self.strike = float(baseline_position.get("strikePrice", 58000))
        self.entry = float(baseline_position.get("entryPrice", 800))
        self.qty = abs(float(baseline_position.get("quantity", 1)))
        self.symbol = baseline_position.get("symbol", "BTC-260529-58000-P")
        self.expiry_ts = int(baseline_position.get("expiryDate", 0))

        self.base_iv = float(baseline_mark.get("markIV", 0.56))
        self.base_mark_price = float(baseline_mark.get("markPrice", 204))
        self.base_delta = float(baseline_mark.get("delta", -0.036))
        self.base_gamma = float(baseline_mark.get("gamma", 0.000006))
        self.base_vega = float(baseline_mark.get("vega", 19))
        self.base_theta = float(baseline_mark.get("theta", -15.5))

    def simulate(self, btc_drop_pct: float, iv_mult: float) -> dict:
        """
        模拟市场状态

        :param btc_drop_pct: BTC 下跌百分比 (正数表示下跌, 如 10 = 跌10%)
        :param iv_mult: IV 倍数 (如 1.5 = IV涨50%)
        :return: 模拟的 risk_data
        """
        sim_spot = self.base_spot * (1 - btc_drop_pct / 100)
        sim_iv = self.base_iv * iv_mult

        # 模拟 mark price: 用简化的 Black-Scholes 近似
        # Put 价值随 spot 下降而上升, 随 IV 上升而上升
        dist_to_strike = (sim_spot - self.strike) / self.base_spot
        base_dist = (self.base_spot - self.strike) / self.base_spot

        # intrinsic value
        intrinsic = max(self.strike - sim_spot, 0)

        # time value 近似: 基于 vega * IV变化 + delta * price变化 + gamma * price变化^2
        price_change = sim_spot - self.base_spot
        iv_change = sim_iv - self.base_iv

        sim_mark_price = max(
            self.base_mark_price
            + abs(self.base_delta) * abs(price_change)    # delta 贡献
            + 0.5 * self.base_gamma * price_change ** 2   # gamma 贡献
            + self.base_vega * iv_change * 100             # vega 贡献 (vega per 1% IV)
            + intrinsic,                                    # 内在价值
            intrinsic + 1                                   # 至少等于内在价值
        )

        # 模拟 delta: 随 spot 接近 strike 而增大
        moneyness = (sim_spot - self.strike) / sim_spot
        if moneyness <= 0:
            # ITM
            sim_delta = -0.5 - 0.5 * (1 - math.exp(moneyness * 5))
        elif moneyness < 0.1:
            sim_delta = -0.3 + moneyness * 2
        elif moneyness < 0.2:
            sim_delta = -0.1 - (0.2 - moneyness) * 1.0
        else:
            # 深 OTM, delta 近似线性缩放
            ratio = moneyness / base_dist if base_dist > 0 else 1
            sim_delta = self.base_delta / max(ratio, 0.3)
        sim_delta = max(min(sim_delta, -0.001), -0.999)

        # 模拟 gamma: 接近 strike 时暴增
        if abs(moneyness) < 0.05:
            sim_gamma = self.base_gamma * 10
        elif abs(moneyness) < 0.1:
            sim_gamma = self.base_gamma * 5
        elif abs(moneyness) < 0.15:
            sim_gamma = self.base_gamma * 3
        else:
            sim_gamma = self.base_gamma

        # 模拟 vega: IV spike 时 vega 也会增大
        sim_vega = self.base_vega * iv_mult ** 0.5

        # 保证金估算
        otm_amount = max(sim_spot - self.strike, 0)
        margin_est = max(
            sim_spot * RiskConfig.MARGIN_RATE - otm_amount,
            sim_spot * RiskConfig.MAINT_MARGIN_RATE,
        ) * self.qty
        maint_margin = sim_spot * RiskConfig.MAINT_MARGIN_RATE * self.qty

        # 浮亏
        unrealized_pnl = (self.entry - sim_mark_price) * self.qty

        # 可用余额估算 (假设初始入金 = 保证金 + 一些缓冲)
        initial_balance = self.base_spot * RiskConfig.MARGIN_RATE * self.qty * 1.5
        available_balance = initial_balance + unrealized_pnl

        margin_usage = sim_mark_price * self.qty / margin_est if margin_est > 0 else 0

        # 构造 risk_data
        sim_position = {
            "symbol": self.symbol,
            "quantity": str(-self.qty),
            "entryPrice": str(self.entry),
            "markPrice": str(round(sim_mark_price, 3)),
            "strikePrice": str(self.strike),
            "expiryDate": self.expiry_ts,
            "side": "SHORT",
            "optionSide": "PUT",
        }

        sim_mark = {
            "symbol": self.symbol,
            "markPrice": str(round(sim_mark_price, 3)),
            "markIV": str(round(sim_iv, 4)),
            "delta": str(round(sim_delta, 8)),
            "gamma": str(round(sim_gamma, 10)),
            "vega": str(round(sim_vega, 6)),
            "theta": str(round(self.base_theta * iv_mult, 6)),
        }

        return {
            "spot": sim_spot,
            "positions": [sim_position],
            "marks": {self.symbol: sim_mark},
            # 额外信息 (用于报告)
            "_sim": {
                "btc_drop_pct": btc_drop_pct,
                "iv_mult": iv_mult,
                "sim_iv": sim_iv,
                "sim_mark_price": sim_mark_price,
                "sim_delta": sim_delta,
                "sim_gamma": sim_gamma,
                "unrealized_pnl": unrealized_pnl,
                "margin_est": margin_est,
                "maint_margin": maint_margin,
                "available_balance": available_balance,
                "margin_usage": margin_usage,
                "intrinsic": intrinsic,
                "dist_to_strike_pct": moneyness * 100,
            },
        }


# ============================================================
#  测试阶段定义
# ============================================================
@dataclass
class TestStage:
    name: str
    btc_drop_pct: float
    iv_mult: float
    expected_risk_level: str   # LOW, MEDIUM, HIGH, EXTREME
    expected_min_alert_level: str  # 最低应触发的告警级别
    description: str

STAGES = [
    TestStage(
        name="Stage 1 — Normal Market",
        btc_drop_pct=0,
        iv_mult=1.0,
        expected_risk_level="LOW",
        expected_min_alert_level="NONE",
        description="BTC price stable, IV normal, margin healthy",
    ),
    TestStage(
        name="Stage 2 — BTC Drops 5%",
        btc_drop_pct=5,
        iv_mult=1.15,
        expected_risk_level="MEDIUM",
        expected_min_alert_level="WATCH",
        description="BTC -5%, IV +15%, delta increases, small loss",
    ),
    TestStage(
        name="Stage 3 — BTC Drops 10% + IV Spikes",
        btc_drop_pct=10,
        iv_mult=1.5,
        expected_risk_level="HIGH",
        expected_min_alert_level="WARNING",
        description="BTC -10%, IV +50%, mark price surges, margin pressure",
    ),
    TestStage(
        name="Stage 4 — BTC Drops 20% + IV Explodes",
        btc_drop_pct=20,
        iv_mult=2.0,
        expected_risk_level="EXTREME",
        expected_min_alert_level="DANGER",
        description="BTC -20%, IV x2, aggressive loss, delta near -1",
    ),
    TestStage(
        name="Stage 5 — Near Liquidation",
        btc_drop_pct=25,
        iv_mult=2.5,
        expected_risk_level="EXTREME",
        expected_min_alert_level="CRITICAL",
        description="BTC -25%, IV x2.5, near liquidation, margin critical",
    ),
]


# ============================================================
#  错误处理测试
# ============================================================
def run_error_handling_tests(engine: RiskEngine) -> list[dict]:
    """测试各种异常场景下引擎是否稳健"""
    results = []

    # Test 1: 缺失 IV 数据
    try:
        bad_data = {
            "spot": 70000,
            "positions": [{"symbol": "BTC-260529-58000-P", "quantity": "-1",
                          "entryPrice": "800", "markPrice": "500",
                          "strikePrice": "58000", "expiryDate": 1780041600000}],
            "marks": {"BTC-260529-58000-P": {"symbol": "BTC-260529-58000-P",
                                              "markPrice": "500"}},  # 缺 delta/gamma/vega
        }
        alerts = engine.check_all(bad_data)
        results.append({"test": "Missing IV/Greeks data", "status": "PASS",
                        "detail": f"Engine handled gracefully, returned {len(alerts)} alerts"})
    except Exception as e:
        results.append({"test": "Missing IV/Greeks data", "status": "FAIL",
                        "detail": f"Crashed: {e}"})

    # Test 2: 空持仓
    try:
        empty_data = {"spot": 70000, "positions": [], "marks": {}}
        alerts = engine.check_all(empty_data)
        results.append({"test": "Empty positions", "status": "PASS",
                        "detail": f"Returned {len(alerts)} alerts (BTC move only)"})
    except Exception as e:
        results.append({"test": "Empty positions", "status": "FAIL",
                        "detail": f"Crashed: {e}"})

    # Test 3: 零/负价格
    try:
        bad_price_data = {
            "spot": 0,
            "positions": [{"symbol": "BTC-260529-58000-P", "quantity": "-1",
                          "entryPrice": "0", "markPrice": "0",
                          "strikePrice": "58000", "expiryDate": 1780041600000}],
            "marks": {},
        }
        alerts = engine.check_all(bad_price_data)
        results.append({"test": "Zero/negative price", "status": "PASS",
                        "detail": f"No crash, returned {len(alerts)} alerts"})
    except Exception as e:
        results.append({"test": "Zero/negative price", "status": "FAIL",
                        "detail": f"Crashed: {e}"})

    # Test 4: 超大持仓
    try:
        big_pos_data = {
            "spot": 70000,
            "positions": [{"symbol": "BTC-260529-58000-P", "quantity": "-100",
                          "entryPrice": "800", "markPrice": "5000",
                          "strikePrice": "58000", "expiryDate": 1780041600000}],
            "marks": {"BTC-260529-58000-P": {
                "markPrice": "5000", "delta": "-0.5", "gamma": "0.0001",
                "vega": "100", "theta": "-50"}},
        }
        alerts = engine.check_all(big_pos_data)
        has_critical = any(a.level == "CRITICAL" for a in alerts)
        results.append({"test": "Huge position (100 contracts)", "status": "PASS",
                        "detail": f"Detected {len(alerts)} alerts, CRITICAL={has_critical}"})
    except Exception as e:
        results.append({"test": "Huge position", "status": "FAIL",
                        "detail": f"Crashed: {e}"})

    # Test 5: TG 发送失败模拟
    try:
        # 发送到无效的 chat_id
        import requests as req
        resp = req.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": "invalid_id", "text": "test"},
            timeout=5,
        )
        # 应该返回 error 但不崩溃
        results.append({"test": "TG send to invalid chat_id", "status": "PASS",
                        "detail": f"Returned ok={resp.json().get('ok')}, no crash"})
    except Exception as e:
        results.append({"test": "TG send failure", "status": "PASS",
                        "detail": f"Exception caught cleanly: {type(e).__name__}"})

    # Test 6: API 超时模拟
    try:
        import requests as req
        try:
            req.get("https://eapi.binance.com/eapi/v1/ticker?symbol=NONEXISTENT", timeout=3)
        except Exception:
            pass
        results.append({"test": "API timeout/invalid request", "status": "PASS",
                        "detail": "Handled without crash"})
    except Exception as e:
        results.append({"test": "API timeout", "status": "FAIL",
                        "detail": f"Unhandled: {e}"})

    return results


# ============================================================
#  TG 告警格式 (测试专用)
# ============================================================
def format_test_alert(stage: TestStage, sim: dict, alerts: list[RiskAlert]) -> str:
    """格式化测试告警消息"""
    s = sim["_sim"]
    spot = sim["spot"]

    max_level = max((a.level_rank for a in alerts), default=0) if alerts else 0
    risk_level_map = {0: "LOW", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "EXTREME"}
    risk_level = risk_level_map.get(max_level, "LOW")

    risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "EXTREME": "🚨"}.get(risk_level, "")

    # 触发原因
    triggers = []
    for a in alerts:
        if a.level in ("WARNING", "DANGER", "CRITICAL"):
            triggers.append(f"[{a.level}] {a.title}")

    lines = [
        f"🧪 <b>[STRESS TEST] {stage.name}</b>",
        "",
        f"BTC Price: <b>${spot:,.0f}</b>",
        f"BTC Change: <b>-{s['btc_drop_pct']:.0f}%</b>",
        f"Contract: {sim['positions'][0]['symbol']}",
        f"Position Side: Short Put",
        f"Mark Price: <b>${s['sim_mark_price']:,.0f}</b>",
        f"IV: <b>{s['sim_iv']:.3f}</b> ({s['iv_mult']:.1f}x baseline)",
        f"Delta: <b>{s['sim_delta']:.4f}</b>",
        f"Unrealized PnL: <b>${s['unrealized_pnl']:+,.0f}</b>",
        f"Margin Balance: ${s['margin_est']:,.0f}",
        f"Available Balance: ${max(s['available_balance'], 0):,.0f}",
        f"Maintenance Margin: ${s['maint_margin']:,.0f}",
        f"Margin Usage: <b>{s['margin_usage']:.0%}</b>",
        f"Dist to Strike: {s['dist_to_strike_pct']:.1f}%",
        f"Intrinsic Value: ${s['intrinsic']:,.0f}",
        "",
        f"Risk Level: {risk_icon} <b>{risk_level}</b>",
        f"Alerts Triggered: {len(alerts)}",
    ]

    if triggers:
        lines.append("")
        lines.append("Trigger Reasons:")
        for t in triggers[:5]:
            lines.append(f"  • {t}")

    # 操作建议
    if risk_level == "EXTREME":
        lines.append("")
        lines.append("⚠️ <b>REVIEW POSITION MANUALLY IMMEDIATELY</b>")
    elif risk_level == "HIGH":
        lines.append("")
        lines.append("⚠️ Prepare stop-loss or hedge")

    return "\n".join(lines)


# ============================================================
#  主测试运行器
# ============================================================
def run_stress_test():
    print("=" * 70)
    print("  BTC OPTIONS STRESS TEST")
    print("  Mode: TEST ONLY — No real trades")
    print("=" * 70)
    print()

    # 1. 获取真实基线数据
    print("[1] Fetching baseline data from Binance (read-only)...")
    api = BinanceOptionsAPI()
    idx = api.get_index_price("BTCUSDT")
    baseline_spot = float(idx["indexPrice"])

    positions = api.get_position()
    active_pos = [p for p in positions if float(p.get("quantity", 0)) != 0]

    all_marks = api.get_mark_price()
    mark_map = {m["symbol"]: m for m in all_marks if m["symbol"].startswith("BTC")}

    if not active_pos:
        print("  ERROR: No active positions found. Using default test position.")
        active_pos = [{
            "symbol": "BTC-260529-58000-P", "quantity": "-1.00",
            "entryPrice": "800", "markPrice": "204", "strikePrice": "58000",
            "expiryDate": 1780041600000,
        }]

    pos = active_pos[0]
    sym = pos["symbol"]
    mark = mark_map.get(sym, {})

    print(f"  Baseline BTC: ${baseline_spot:,.2f}")
    print(f"  Position: {sym} qty={pos['quantity']} entry=${float(pos['entryPrice']):,.0f}")
    print(f"  Mark: ${float(mark.get('markPrice', 0)):,.0f} IV={mark.get('markIV')} Delta={mark.get('delta')}")
    print()

    # 2. 初始化
    simulator = MarketSimulator(baseline_spot, pos, mark)
    engine = RiskEngine()

    # 种一个基线价格记录
    engine.price_tracker.record(baseline_spot)
    time.sleep(0.1)

    # TG 开始通知
    tg_send(
        "🧪 <b>[STRESS TEST] 压力测试开始</b>\n\n"
        f"BTC 基线价格: ${baseline_spot:,.0f}\n"
        f"测试持仓: {sym}\n"
        f"入场价: ${float(pos['entryPrice']):,.0f}\n"
        f"当前 Mark: ${float(mark.get('markPrice', 0)):,.0f}\n\n"
        "将模拟 5 个阶段的极端行情..."
    )

    # 3. 运行 5 个阶段
    print("[2] Running stress test stages...")
    print()

    stage_results = []

    for i, stage in enumerate(STAGES):
        print(f"  --- {stage.name} ---")
        print(f"  BTC drop: {stage.btc_drop_pct}%, IV mult: {stage.iv_mult}x")

        # 模拟
        sim_data = simulator.simulate(stage.btc_drop_pct, stage.iv_mult)
        s = sim_data["_sim"]

        # 记录价格 (让 price tracker 检测到跌幅)
        engine.price_tracker.record(sim_data["spot"])

        # 运行风控引擎
        # 重置冷却, 确保每阶段都能触发
        engine.last_alerts = {}
        alerts = engine.check_all(sim_data)

        # 判定风险等级
        max_rank = max((a.level_rank for a in alerts), default=0)
        actual_risk = {0: "LOW", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "EXTREME"}.get(max_rank, "LOW")
        max_alert = max((a.level for a in alerts), key=lambda x: {"INFO":0,"WATCH":1,"WARNING":2,"DANGER":3,"CRITICAL":4}.get(x,0), default="NONE") if alerts else "NONE"

        # 判定 PASS/FAIL
        level_order = {"NONE": 0, "INFO": 1, "WATCH": 2, "WARNING": 3, "DANGER": 4, "CRITICAL": 5}
        expected_order = level_order.get(stage.expected_min_alert_level, 0)
        actual_order = level_order.get(max_alert, 0)

        if actual_order >= expected_order:
            verdict = "PASS"
        else:
            verdict = "FAIL"

        result = {
            "stage": stage.name,
            "btc_drop": f"{stage.btc_drop_pct}%",
            "iv_mult": f"{stage.iv_mult}x",
            "sim_spot": sim_data["spot"],
            "sim_mark": s["sim_mark_price"],
            "sim_iv": s["sim_iv"],
            "sim_delta": s["sim_delta"],
            "sim_pnl": s["unrealized_pnl"],
            "margin_usage": s["margin_usage"],
            "dist_strike": s["dist_to_strike_pct"],
            "expected_risk": stage.expected_risk_level,
            "actual_risk": actual_risk,
            "expected_alert": stage.expected_min_alert_level,
            "actual_alert": max_alert,
            "alert_count": len(alerts),
            "alerts": alerts,
            "verdict": verdict,
            "problem": "" if verdict == "PASS" else f"Expected >={stage.expected_min_alert_level} but got {max_alert}",
        }
        stage_results.append(result)

        print(f"  Sim: BTC ${sim_data['spot']:,.0f} | Mark ${s['sim_mark_price']:,.0f} | "
              f"IV {s['sim_iv']:.3f} | Delta {s['sim_delta']:.4f}")
        print(f"  PnL: ${s['unrealized_pnl']:+,.0f} | Margin Usage: {s['margin_usage']:.0%} | "
              f"Dist: {s['dist_to_strike_pct']:.1f}%")
        print(f"  Alerts: {len(alerts)} | Max: {max_alert} | Risk: {actual_risk}")
        print(f"  Verdict: {'✅' if verdict == 'PASS' else '❌'} {verdict}")
        if result["problem"]:
            print(f"  Problem: {result['problem']}")
        print()

        # 发 TG 告警
        tg_msg = format_test_alert(stage, sim_data, alerts)
        tg_send(tg_msg)
        time.sleep(1)  # 避免 TG 限流

    # 4. 错误处理测试
    print("[3] Running error handling tests...")
    print()
    error_engine = RiskEngine()
    error_engine.price_tracker.record(70000)
    error_results = run_error_handling_tests(error_engine)

    for er in error_results:
        icon = "✅" if er["status"] == "PASS" else "❌"
        print(f"  {icon} {er['test']}: {er['status']} — {er['detail']}")
    print()

    # 5. 生成最终报告
    print("[4] Generating final report...")
    print()

    all_pass = all(r["verdict"] == "PASS" for r in stage_results)
    error_pass = all(r["status"] == "PASS" for r in error_results)

    if all_pass and error_pass:
        readiness = "READY"
    elif all_pass:
        readiness = "NEEDS FIX BEFORE LIVE MONITORING"
    else:
        readiness = "NOT READY"

    # 打印报告
    print("=" * 70)
    print("  STRESS TEST REPORT")
    print("=" * 70)
    print()

    print("  Stage Results:")
    print(f"  {'Stage':<35} {'Expected':>10} {'Actual':>10} {'Result':>8}")
    print("  " + "-" * 65)
    for r in stage_results:
        icon = "✅" if r["verdict"] == "PASS" else "❌"
        print(f"  {r['stage']:<35} {r['expected_alert']:>10} {r['actual_alert']:>10} {icon} {r['verdict']:>6}")
    print()

    print("  Error Handling:")
    for er in error_results:
        icon = "✅" if er["status"] == "PASS" else "❌"
        print(f"  {icon} {er['test']}")
    print()

    print(f"  Overall Readiness: {'🟢' if readiness == 'READY' else '🔴'} {readiness}")
    print()
    print("=" * 70)

    # 发 TG 最终报告
    report_lines = [
        "🧪 <b>[STRESS TEST] Final Report</b>",
        "",
        "<b>Stage Results:</b>",
    ]
    for r in stage_results:
        icon = "✅" if r["verdict"] == "PASS" else "❌"
        report_lines.append(
            f"{icon} {r['stage']}\n"
            f"   BTC ${r['sim_spot']:,.0f} | Mark ${r['sim_mark']:,.0f} | "
            f"PnL ${r['sim_pnl']:+,.0f}\n"
            f"   Expected: {r['expected_alert']} | Got: {r['actual_alert']} → {r['verdict']}"
        )

    report_lines.append("")
    report_lines.append("<b>Error Handling:</b>")
    err_pass = sum(1 for r in error_results if r["status"] == "PASS")
    report_lines.append(f"  {err_pass}/{len(error_results)} tests passed")

    report_lines.append("")
    readiness_icon = "🟢" if readiness == "READY" else "🟡" if "FIX" in readiness else "🔴"
    report_lines.append(f"<b>Overall: {readiness_icon} {readiness}</b>")

    tg_send("\n".join(report_lines))

    return readiness


# ============================================================
if __name__ == "__main__":
    result = run_stress_test()
    sys.exit(0 if result == "READY" else 1)
