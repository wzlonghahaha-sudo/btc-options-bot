"""
机会扫描器 v2 — 放宽筛选 + 分层展示 + 账户风控

核心改动:
  旧版: 极保守硬门槛 (delta<=0.05, OTM>=25%) → 大量好机会被埋没
  新版: 三档机会 (保守/均衡/激进) + 每个机会附带完整评判依据 + 账户风控评估

三档机会:
  🟢 保守型: delta<=0.05, OTM>=25%, 安全垫极大, 权利金薄但胜率极高
  🟡 均衡型: delta 0.05-0.10, OTM 18-25%, 权利金和安全的平衡点
  🔴 激进型: delta 0.10-0.15, OTM 12-18%, 权利金厚但需要更多关注

每个机会的评判依据:
  - 年化收益 / 安全垫 / IV溢价 / 流动性 / Theta效率
  - IV/HV 卖方优势
  - 开仓后的账户级风控: 组合Delta / 保证金使用率 / 最大亏损
"""

import math
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from margin_calc import calc_put_margin_per_contract, calc_put_margin

log = logging.getLogger(__name__)


# ============================================================
#  配置
# ============================================================
class ScanConfig:
    # 三档筛选门槛
    TIERS = {
        "conservative": {
            "label": "🟢 保守型",
            "delta_max": 0.05,
            "otm_min": 25.0,
            "safety_min": 25.0,
            "desc": "胜率极高, 权利金薄",
        },
        "balanced": {
            "label": "🟡 均衡型",
            "delta_max": 0.10,
            "otm_min": 18.0,
            "safety_min": 18.0,
            "desc": "权利金与安全的平衡",
        },
        "aggressive": {
            "label": "🔴 激进型",
            "delta_max": 0.15,
            "otm_min": 12.0,
            "safety_min": 12.0,
            "desc": "权利金厚, 需密切关注",
        },
    }

    # 通用门槛
    DTE_MIN = 7
    DTE_MAX = 120
    MIN_BID = 20.0
    MAX_SPREAD_PCT = 15.0

    # 评分标准 (更宽松, 展示更多)
    SCORE_SIGNAL = 55     # 可入场门槛 (命令查看)
    SCORE_STRONG = 75     # 强信号
    SCORE_PUSH = 78       # 自动推送门槛 (78+才主动推送详情)

    # 账户风控
    MARGIN_RATE = 0.15
    MAX_PORTFOLIO_DELTA = 0.50       # 组合 |delta| 上限
    MAX_MARGIN_USAGE = 0.70          # 最大保证金使用率 70%
    MAX_SINGLE_POSITION_PCT = 0.30   # 单仓不超过总资金 30%


# ============================================================
#  账户风控评估
# ============================================================
@dataclass
class AccountRisk:
    """账户级风控状态"""
    total_balance: float = 0           # 预估总资金
    used_margin: float = 0             # 已用保证金
    available_margin: float = 0        # 可用保证金
    margin_usage_pct: float = 0        # 保证金使用率
    portfolio_delta: float = 0         # 组合总 delta
    portfolio_gamma: float = 0         # 组合总 gamma
    portfolio_vega: float = 0          # 组合总 vega
    portfolio_theta: float = 0         # 组合总 theta (日收入)
    total_unrealized_pnl: float = 0    # 总浮盈亏
    position_count: int = 0            # 持仓数
    max_new_margin: float = 0          # 还能开多少保证金的新仓
    positions: list = field(default_factory=list)


def assess_account_risk(api, data: dict) -> AccountRisk:
    """评估当前账户风控状态"""
    cfg = ScanConfig()
    spot = data["spot"]
    marks = data["marks"]

    try:
        positions = api.get_position()
    except Exception as e:
        log.warning(f"获取持仓失败: {e}")
        positions = []

    # 获取账户权益 (优先 API 直读, fallback 流水法)
    from binance_options import get_account_equity
    acct = get_account_equity(api)
    total_balance = acct["equity"] if acct["equity"] > 0 else acct["margin_balance"]
    if total_balance <= 0:
        total_balance = spot * cfg.MARGIN_RATE * 5  # 最终兜底

    ar = AccountRisk(total_balance=max(total_balance, 1))
    ar.positions = []

    for p in positions:
        qty = float(p.get("quantity", 0))
        if qty == 0:
            continue

        sym = p["symbol"]
        abs_qty = abs(qty)
        entry = float(p.get("entryPrice", 0))
        mark_price = float(p.get("markPrice", 0))
        strike = float(p.get("strikePrice", 0))

        m = marks.get(sym, {})
        delta = float(m.get("delta", 0))
        gamma = float(m.get("gamma", 0))
        vega = float(m.get("vega", 0))
        theta = float(m.get("theta", 0))

        # 保证金估算 (统一公式)
        margin = calc_put_margin(spot, strike, abs_qty)

        # 卖 Put: delta 为负, qty 为负, 组合 delta = delta * |qty| (正数=看多暴露)
        if qty < 0:
            pos_delta = abs(delta) * abs_qty  # 卖 Put 的等效多头暴露
            pnl = (entry - mark_price) * abs_qty
        else:
            pos_delta = delta * abs_qty
            pnl = (mark_price - entry) * abs_qty

        ar.used_margin += margin
        ar.portfolio_delta += pos_delta
        ar.portfolio_gamma += abs(gamma) * abs_qty
        ar.portfolio_vega += abs(vega) * abs_qty
        ar.portfolio_theta += abs(theta) * abs_qty  # 日 theta 收入
        ar.total_unrealized_pnl += pnl
        ar.position_count += 1
        ar.positions.append({
            "symbol": sym, "qty": qty, "delta": delta,
            "margin": margin, "pnl": pnl,
        })

    ar.available_margin = max(ar.total_balance - ar.used_margin + ar.total_unrealized_pnl, 0)
    ar.margin_usage_pct = ar.used_margin / ar.total_balance * 100 if ar.total_balance > 0 else 0
    ar.max_new_margin = max(ar.total_balance * cfg.MAX_MARGIN_USAGE - ar.used_margin, 0)

    return ar


# ============================================================
#  机会评估
# ============================================================
@dataclass
class Opportunity:
    symbol: str
    tier: str           # conservative, balanced, aggressive
    tier_label: str
    strike: float
    expiry: str
    dte: float
    delta: float
    iv: float
    mark_price: float
    bid: float
    ask: float
    spread_pct: float
    volume: float
    oi: float

    # 核心指标
    otm_pct: float
    safety_pct: float
    annual_return: float
    iv_premium: float       # vs 同期限中位数
    theta_daily_pct: float
    iv_hv_ratio: float

    # 评分
    score: float

    # 开仓评估
    margin_required: float       # 开1张需要的保证金
    new_portfolio_delta: float   # 开仓后组合 delta
    new_margin_usage: float      # 开仓后保证金使用率
    max_loss_1_contract: float   # 1张最大亏损 (到行权价)
    can_open: bool               # 是否风控允许
    risk_notes: list             # 风控备注

    # 评判依据
    pros: list                   # 优点
    cons: list                   # 缺点


def scan_all_opportunities(data: dict, iv_surface: dict, account: AccountRisk,
                           hv_20: float = 0) -> list[Opportunity]:
    """扫描所有机会, 三档分层"""
    cfg = ScanConfig()
    spot = data["spot"]
    now = datetime.now(timezone.utc)

    # 计算每个到期日的 skew ratio (deep_otm_iv / atm_iv)
    _skew_data = {}
    for _sym, _m in data["marks"].items():
        if "-P" not in _sym:
            continue
        _parts = _sym.split("-")
        if len(_parts) < 4:
            continue
        _exp = _parts[1]
        _strike = float(_parts[2])
        _iv = float(_m.get("markIV", 0))
        if _iv <= 0:
            continue
        _mon = (_strike / spot - 1) * 100
        if _exp not in _skew_data:
            _skew_data[_exp] = {"atm": [], "deep_otm": []}
        if abs(_mon) < 5:
            _skew_data[_exp]["atm"].append(_iv)
        elif -40 < _mon < -20:
            _skew_data[_exp]["deep_otm"].append(_iv)
    _skew_ratios = {}
    for _exp, _ivs in _skew_data.items():
        if _ivs["atm"] and _ivs["deep_otm"]:
            _skew_ratios[_exp] = (sum(_ivs["deep_otm"]) / len(_ivs["deep_otm"])) / (sum(_ivs["atm"]) / len(_ivs["atm"]))
        else:
            _skew_ratios[_exp] = 1.0

    results = []

    for sym, contract in data["contracts"].items():
        if contract.get("side") != "PUT":
            continue

        mark = data["marks"].get(sym, {})
        ticker = data["tickers"].get(sym, {})
        if not mark:
            continue

        strike = float(contract["strikePrice"])
        expiry_ts = contract["expiryDate"]
        expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
        dte = max((expiry - now).total_seconds() / 86400, 0.01)

        delta = float(mark.get("delta", 0))
        abs_delta = abs(delta)
        theta = float(mark.get("theta", 0))
        iv = float(mark.get("markIV", 0))
        mark_price = float(mark.get("markPrice", 0))

        bid = float(ticker.get("bidPrice", 0))
        ask = float(ticker.get("askPrice", 0))
        volume = float(ticker.get("volume", 0))
        oi = data["oi"].get(sym, 0)

        otm_pct = (spot - strike) / spot * 100
        if mark_price <= 0 or bid < cfg.MIN_BID or dte < cfg.DTE_MIN or dte > cfg.DTE_MAX:
            continue

        spread_pct = ((ask - bid) / mark_price * 100) if ask > 0 and bid > 0 else 999
        if spread_pct > cfg.MAX_SPREAD_PCT:
            continue

        # 判断属于哪个档位
        tier = None
        for t_name, t_cfg in cfg.TIERS.items():
            if abs_delta <= t_cfg["delta_max"] and otm_pct >= t_cfg["otm_min"]:
                tier = t_name
                break
        if tier is None:
            continue

        tier_cfg = cfg.TIERS[tier]

        # 核心指标
        breakeven = strike - bid
        safety_pct = (spot - breakeven) / spot * 100 if spot > 0 else 0
        margin_1 = calc_put_margin_per_contract(spot, strike)
        annual_return = (bid / margin_1) * (365 / dte) * 100 if margin_1 > 0 else 0

        exp_key = sym.split("-")[1]
        median_iv = iv_surface["by_exp"].get(exp_key, {}).get("median", iv)
        iv_premium = (iv - median_iv) / median_iv * 100 if median_iv > 0 else 0

        theta_daily_pct = abs(theta) / mark_price * 100 if mark_price > 0 else 0
        iv_hv_ratio = iv / hv_20 if hv_20 > 0 else 0

        # 获取该到期日的 skew ratio
        exp_key_for_skew = sym.split("-")[1] if "-" in sym else ""
        skew_ratio = _skew_ratios.get(exp_key_for_skew, 1.0)

        # === 评分 (Sinclair 风险溢价框架) ===
        #
        # 核心理念 (Euan Sinclair):
        #   - 卖方的 Edge 来自 variance premium (IV > HV), 不是 theta 衰减
        #   - theta 不是免费午餐, 它只是风险溢价的会计表达
        #   - 你被付费是因为承担了不愉快的风险 (尾部亏损)
        #   - 安全垫 = 风险管理, variance premium = Edge 来源
        #   - 流动性是前提条件而非评分维度 (极差已被硬门槛过滤)
        #
        # 权重设计:
        #   安全垫      30% — 风控第一, 决定你能否活下来
        #   风险溢价    30% — Edge的真正来源 (IV溢价 + IV/HV综合)
        #   年化收益    20% — 收益要看, 但高收益≠高质量
        #   Skew        10% — 深度OTM定价溢价, 越陡权利金越厚
        #   时间结构     7% — DTE甜蜜区 + theta效率 (辅助, 非核心)
        #   流动性       3% — 保底权重, 极差已被 MAX_SPREAD_PCT 过滤

        # 1. 安全垫评分 (30%) — 风控基础
        t_safety_min = tier_cfg["otm_min"]
        if safety_pct >= t_safety_min + 20:
            safe_score = 100
        elif safety_pct >= t_safety_min + 10:
            safe_score = 50 + (safety_pct - t_safety_min - 10) / 10 * 50
        elif safety_pct >= t_safety_min:
            safe_score = (safety_pct - t_safety_min) / 10 * 50
        else:
            safe_score = 0

        # 2. 风险溢价评分 (30%) — Edge 的核心来源
        #    综合 IV溢价 和 IV/HV比值, 反映 variance premium 的厚度
        #    "如果期权不贵, 就没有卖的理由" — Sinclair

        # 2a. IV溢价子分 (占风险溢价的60%)
        if iv_premium >= 40:
            iv_sub = 100
        elif iv_premium >= 20:
            iv_sub = 40 + (iv_premium - 20) / 20 * 60
        elif iv_premium >= 5:
            iv_sub = (iv_premium - 5) / 15 * 40
        else:
            iv_sub = 0

        # 2b. IV/HV 比值子分 (占风险溢价的40%)
        #     IV/HV > 1 意味着市场定价的波动率高于实际波动率 = 卖方有优势
        if iv_hv_ratio >= 1.5:
            ivhv_sub = 100
        elif iv_hv_ratio >= 1.3:
            ivhv_sub = 60 + (iv_hv_ratio - 1.3) / 0.2 * 40
        elif iv_hv_ratio >= 1.1:
            ivhv_sub = 20 + (iv_hv_ratio - 1.1) / 0.2 * 40
        elif iv_hv_ratio >= 1.0:
            ivhv_sub = (iv_hv_ratio - 1.0) / 0.1 * 20
        else:
            ivhv_sub = 0  # IV < HV: 卖方无优势

        vp_score = iv_sub * 0.60 + ivhv_sub * 0.40

        # 3. 年化收益评分 (20%)
        if annual_return >= 60:
            ret_score = 100
        elif annual_return >= 30:
            ret_score = 40 + (annual_return - 30) / 30 * 60
        elif annual_return >= 10:
            ret_score = (annual_return - 10) / 20 * 40
        else:
            ret_score = 0

        # 4. Skew 评分 (10%) — 深度OTM Put 定价溢价
        #    skew_ratio = deep_otm_iv / atm_iv
        #    陡峭 = 权利金更厚 = 卖方更好的赔率
        if skew_ratio >= 1.6:
            skew_score = 100
        elif skew_ratio >= 1.3:
            skew_score = 40 + (skew_ratio - 1.3) / 0.3 * 60
        elif skew_ratio >= 1.1:
            skew_score = (skew_ratio - 1.1) / 0.2 * 40
        else:
            skew_score = 0

        # 5. 时间结构评分 (7%) — DTE甜蜜区 + theta效率
        #    14-45天: theta衰减加速但gamma风险可控
        #    theta本身不是edge, 但合理的时间窗口提升资金效率
        theta_score = min(theta_daily_pct / 5 * 100, 100) * 0.5  # theta效率 (占50%)
        if 14 <= dte <= 45:
            dte_sub = 100
        elif 7 <= dte < 14:
            dte_sub = 30 + (dte - 7) / 7 * 70   # 7天=30, 14天=100
        elif 45 < dte <= 60:
            dte_sub = 60 + (60 - dte) / 15 * 40  # 45天=100, 60天=60
        elif 60 < dte <= 90:
            dte_sub = 30 + (90 - dte) / 30 * 30   # 60天=60, 90天=30
        else:
            dte_sub = max(0, 20)  # >90天: 基础分20
        time_score = theta_score + dte_sub * 0.5  # DTE甜蜜区 (占50%)

        # 6. 流动性 (3%) — 保底权重, 极差已被 MAX_SPREAD_PCT 硬过滤
        if spread_pct <= 2:
            liq_score = 100
        elif spread_pct <= 5:
            liq_score = 50 + (5 - spread_pct) / 3 * 50
        elif spread_pct <= 10:
            liq_score = (10 - spread_pct) / 5 * 50
        else:
            liq_score = 0
        liq_score = min(liq_score + min(volume / 10, 10), 100)

        # 流动性警告标签
        liq_warning = ""
        if spread_pct > 10:
            liq_warning = f"流动性极差 spread {spread_pct:.1f}%"
        elif spread_pct > 6:
            liq_warning = f"流动性偏差 spread {spread_pct:.1f}%, 建议限价挂单"
        elif volume == 0 and oi < 5:
            liq_warning = "零成交+低OI, 流动性存疑"

        # === 综合评分 ===
        score = (
            safe_score * 0.30     # 安全垫: 活下来
            + vp_score * 0.30     # 风险溢价: Edge来源
            + ret_score * 0.20    # 年化收益: 回报合理性
            + skew_score * 0.10   # Skew: 深OTM定价溢价
            + time_score * 0.07   # 时间结构: 资金效率
            + liq_score * 0.03    # 流动性: 保底权重
        )

        # 流动性惩罚: spread > 8% 额外扣分
        if spread_pct > 8:
            score = score * 0.92

        # IV/HV 惩罚: IV < HV 时卖方无 edge, 总分 ×0.5
        iv_hv_edge_str = "N/A"
        if iv_hv_ratio < 1.0:
            score *= 0.5
            iv_hv_edge_str = "NONE"
        elif iv_hv_ratio >= 1.5:
            score += 10
            iv_hv_edge_str = "STRONG"
        elif iv_hv_ratio >= 1.25:
            score += 5
            iv_hv_edge_str = "MODERATE"
        elif iv_hv_ratio >= 1.0:
            iv_hv_edge_str = "SLIGHT"

        # 事件日历扣减: 存续期内每个 HIGH 事件 -8 分
        event_descs = []
        try:
            from event_calendar import score_penalty_for_events
            from datetime import date as _date
            ev_penalty, event_descs = score_penalty_for_events(
                _date.today(), expiry_date.date() if hasattr(expiry_date, 'date') else expiry_date)
            score += ev_penalty
        except Exception as e:
            log.warning(f"事件日历评分失败 [{sym}]: {e}")

        # === 账户风控评估 ===
        new_portfolio_delta = account.portfolio_delta + abs_delta
        new_margin = account.used_margin + margin_1
        new_margin_usage = new_margin / account.total_balance * 100 if account.total_balance > 0 else 999
        max_loss = (strike - bid) * 1  # 1张

        risk_notes = []
        can_open = True

        if new_portfolio_delta > cfg.MAX_PORTFOLIO_DELTA:
            risk_notes.append(f"组合Delta将达{new_portfolio_delta:.2f} (上限{cfg.MAX_PORTFOLIO_DELTA})")
            if new_portfolio_delta > cfg.MAX_PORTFOLIO_DELTA * 1.2:
                can_open = False

        if new_margin_usage > cfg.MAX_MARGIN_USAGE * 100:
            risk_notes.append(f"保证金使用率将达{new_margin_usage:.0f}% (上限{cfg.MAX_MARGIN_USAGE*100:.0f}%)")
            can_open = False

        if margin_1 > account.available_margin:
            risk_notes.append(f"保证金不足 (需${margin_1:,.0f}, 可用${account.available_margin:,.0f})")
            can_open = False

        if margin_1 > account.total_balance * cfg.MAX_SINGLE_POSITION_PCT:
            risk_notes.append(f"单仓保证金超总资金{cfg.MAX_SINGLE_POSITION_PCT*100:.0f}%")

        # 和已有持仓的到期重叠检查
        for pos in account.positions:
            if pos["symbol"].split("-")[1] == exp_key:
                risk_notes.append(f"与 {pos['symbol']} 同到期日, 注意集中风险")
                break

        if not risk_notes:
            risk_notes.append("风控通过 ✅")

        # === 评判依据 ===
        pros = []
        cons = []

        if safety_pct >= 30:
            pros.append(f"安全垫极厚 {safety_pct:.0f}%")
        elif safety_pct >= 20:
            pros.append(f"安全垫良好 {safety_pct:.0f}%")
        else:
            cons.append(f"安全垫偏薄 {safety_pct:.0f}%")

        if annual_return >= 40:
            pros.append(f"年化收益优秀 {annual_return:.0f}%")
        elif annual_return >= 20:
            pros.append(f"年化收益不错 {annual_return:.0f}%")
        elif annual_return < 10:
            cons.append(f"年化收益偏低 {annual_return:.0f}%")

        # 风险溢价 (variance premium) 综合评判
        if iv_premium >= 30 and iv_hv_ratio >= 1.3:
            pros.append(f"风险溢价厚 IV溢价+{iv_premium:.0f}% IV/HV={iv_hv_ratio:.1f}x")
        elif iv_premium >= 20 or iv_hv_ratio >= 1.3:
            pros.append(f"风险溢价良好 IV溢价+{iv_premium:.0f}% IV/HV={iv_hv_ratio:.1f}x")
        elif iv_premium >= 10 or iv_hv_ratio >= 1.1:
            pros.append(f"风险溢价一般 IV溢价+{iv_premium:.0f}% IV/HV={iv_hv_ratio:.1f}x")
        else:
            cons.append(f"风险溢价薄 IV溢价+{iv_premium:.0f}% IV/HV={iv_hv_ratio:.1f}x (卖方Edge弱)")

        if spread_pct <= 2:
            pros.append(f"流动性好 spread {spread_pct:.1f}%")
        elif liq_warning:
            cons.append(f"⚠️ {liq_warning}")

        if skew_ratio >= 1.4:
            pros.append(f"Skew陡峭 {skew_ratio:.1f}x (OTM定价贵)")
        elif skew_ratio >= 1.2:
            pros.append(f"Skew正常 {skew_ratio:.1f}x")
        elif skew_ratio < 1.0:
            cons.append(f"Skew倒挂 {skew_ratio:.1f}x (OTM定价便宜)")

        if 14 <= dte <= 45:
            pros.append(f"Theta甜蜜区 {dte:.0f}天")
        elif dte < 14:
            cons.append(f"临近到期 {dte:.0f}天, gamma大")
        elif dte > 60:
            cons.append(f"到期较远 {dte:.0f}天, 资金占用久")

        results.append(Opportunity(
            symbol=sym, tier=tier, tier_label=tier_cfg["label"],
            strike=strike, expiry=expiry.strftime("%Y-%m-%d"), dte=round(dte, 1),
            delta=delta, iv=iv, mark_price=mark_price,
            bid=bid, ask=ask, spread_pct=round(spread_pct, 1),
            volume=volume, oi=oi,
            otm_pct=round(otm_pct, 1), safety_pct=round(safety_pct, 1),
            annual_return=round(annual_return, 1), iv_premium=round(iv_premium, 1),
            theta_daily_pct=round(theta_daily_pct, 2), iv_hv_ratio=round(iv_hv_ratio, 2),
            score=round(score, 1),
            margin_required=round(margin_1, 0), new_portfolio_delta=round(new_portfolio_delta, 3),
            new_margin_usage=round(new_margin_usage, 1), max_loss_1_contract=round(max_loss, 0),
            can_open=can_open, risk_notes=risk_notes,
            pros=pros, cons=cons,
        ))

    # 每个档位内按 score 排序
    results.sort(key=lambda x: ({"conservative": 0, "balanced": 1, "aggressive": 2}[x.tier], -x.score))
    return results


# ============================================================
#  TG 消息格式化
# ============================================================
def format_opportunities_tg(opps: list, account: AccountRisk,
                            hv_20: float, iv_mean: float,
                            push_mode: bool = False) -> str:
    """
    格式化机会推送

    push_mode=True: 只推送 score >= SIGNAL 的机会, 精简格式
    push_mode=False: /top 命令, 展示全部
    """
    cfg = ScanConfig()
    lines = []

    if not push_mode:
        # 完整版: 先展示账户概况
        lines.append("🔍 <b>机会扫描报告</b>")
        lines.append("")
        lines.append("<b>📊 账户 & 市场</b>")
        lines.append(f"  资金: ${account.total_balance:,.0f}  "
                     f"已用: ${account.used_margin:,.0f} ({account.margin_usage_pct:.0f}%)  "
                     f"可用: ${account.available_margin:,.0f}")
        lines.append(f"  组合: Delta {account.portfolio_delta:.3f}  "
                     f"Theta ${account.portfolio_theta:.0f}/天  "
                     f"持仓 {account.position_count} 个")
        iv_hv = iv_mean / hv_20 if hv_20 > 0 else 0
        edge_icon = "🟢" if iv_hv >= 1.3 else "🟡" if iv_hv >= 1.1 else "⚪"
        lines.append(f"  {edge_icon} IV/HV: {iv_hv:.2f}x  (IV {iv_mean:.3f} vs HV20 {hv_20:.3f})")
        lines.append("")

    # 按档位分组
    tiers = {"conservative": [], "balanced": [], "aggressive": []}
    for o in opps:
        tiers[o.tier].append(o)

    for tier_name in ["balanced", "conservative", "aggressive"]:
        tier_opps = tiers[tier_name]
        if not tier_opps:
            continue

        if push_mode:
            tier_opps = [o for o in tier_opps if o.score >= cfg.SCORE_SIGNAL]
            if not tier_opps:
                continue

        tier_cfg = cfg.TIERS[tier_name]
        lines.append(f"<b>{tier_cfg['label']} ({tier_cfg['desc']})</b>")
        lines.append("")

        display_count = 3 if push_mode else 5
        for o in tier_opps[:display_count]:
            _format_one_opportunity(lines, o, push_mode)

        remaining = len(tier_opps) - display_count
        if remaining > 0:
            lines.append(f"  ... 还有 {remaining} 个\n")

    if not push_mode and not any(tiers.values()):
        lines.append("当前无符合条件的机会")

    # 汇总
    total = len(opps)
    signals = len([o for o in opps if o.score >= cfg.SCORE_SIGNAL])
    strong = len([o for o in opps if o.score >= cfg.SCORE_STRONG])
    lines.append(f"汇总: {total}个机会 | {signals}个可入场 | {strong}个强信号")

    return "\n".join(lines)


def _format_one_opportunity(lines: list, o: Opportunity, brief: bool):
    """格式化单个机会"""
    # 信号强度
    if o.score >= ScanConfig.SCORE_STRONG:
        sig_icon = "🔥"
    elif o.score >= ScanConfig.SCORE_SIGNAL:
        sig_icon = "⭐"
    else:
        sig_icon = "  "

    can_icon = "✅" if o.can_open else "🚫"

    lines.append(
        f"{sig_icon} <b>{o.symbol}</b>  评分 {o.score:.0f}  {can_icon}"
    )
    lines.append(
        f"  Bid <b>${o.bid:,.0f}</b>  |  年化 <b>{o.annual_return:.0f}%</b>  |  "
        f"安全垫 {o.safety_pct:.0f}%  |  OTM {o.otm_pct:.0f}%"
    )
    lines.append(
        f"  Delta {o.delta:.4f}  |  IV {o.iv:.3f} (溢价{o.iv_premium:+.0f}%)  |  "
        f"到期 {o.dte:.0f}天  |  Spread {o.spread_pct:.1f}%"
    )

    if not brief:
        # 评判依据
        if o.pros:
            lines.append(f"  ✅ {' / '.join(o.pros)}")
        if o.cons:
            lines.append(f"  ⚠️ {' / '.join(o.cons)}")

    # 风控
    lines.append(
        f"  保证金 ${o.margin_required:,.0f}  |  "
        f"开仓后Delta {o.new_portfolio_delta:.3f}  |  "
        f"保证金率 {o.new_margin_usage:.0f}%"
    )
    if o.risk_notes and o.risk_notes[0] != "风控通过 ✅":
        for note in o.risk_notes[:2]:
            lines.append(f"  ⚠️ {note}")
    lines.append("")


def format_signal_push(opps: list, account: AccountRisk) -> str:
    """
    格式化自动推送的信号消息

    策略:
    - 78+分: 完整详情推送
    - <78分: 不推送 (用户通过 /top 命令自行查看)
    """
    cfg = ScanConfig()

    # 事件前 48 小时内, 临时提高推送门槛 +10 (避免在高波动前开仓)
    push_threshold = cfg.SCORE_PUSH
    try:
        from event_calendar import is_pre_event_window
        in_window, evt = is_pre_event_window(48)
        if in_window and evt:
            push_threshold += 10
    except Exception as e:
        log.warning(f"事件窗口检查失败: {e}")

    top_opps = [o for o in opps if o.score >= push_threshold and o.can_open]

    if not top_opps:
        return ""

    lines = []
    lines.append("🔔 <b>新入场机会</b>")
    lines.append("")
    lines.append(f"账户可用: ${account.available_margin:,.0f}  "
                 f"组合Delta: {account.portfolio_delta:.3f}")
    lines.append("")

    for o in top_opps[:5]:
        if o.score >= 90:
            sig = "🔥🔥 极强信号"
        elif o.score >= 78:
            sig = "🔥 强信号"
        else:
            sig = "⭐ 可入场"

        lines.append(f"<b>{sig} {o.tier_label}</b>")
        lines.append(f"<b>{o.symbol}</b>  评分 <b>{o.score:.0f}</b>")
        lines.append(f"  Bid <b>${o.bid:,.0f}</b>  |  年化 <b>{o.annual_return:.0f}%</b>  |  安全垫 {o.safety_pct:.0f}%")
        lines.append(f"  Delta {o.delta:.4f}  |  IV溢价 {o.iv_premium:+.0f}%  |  到期 {o.dte:.0f}天")
        if o.pros:
            lines.append(f"  ✅ {' / '.join(o.pros[:3])}")
        if o.cons:
            lines.append(f"  ⚠️ {' / '.join(o.cons[:2])}")
        lines.append(f"  保证金 ${o.margin_required:,.0f}  |  开仓后Delta {o.new_portfolio_delta:.3f}")
        lines.append("")

    # 底部提示还有更多低分机会
    other_count = len([o for o in opps if cfg.SCORE_SIGNAL <= o.score < cfg.SCORE_PUSH and o.can_open])
    if other_count > 0:
        lines.append(f"另有 {other_count} 个低分机会 → /top 查看全部")

    return "\n".join(lines)
