#!/usr/bin/env python3
"""
深度 OTM 卖 BTC Put 监控系统

策略核心:
  - 卖深度虚值 Put, 稳收权利金, 靠时间站在我们这边
  - 不急着入场, 等"赔率"好的时候卖: IV 飙升 / BTC 急跌恐慌 / 权利金异常膨胀
  - 行权价远离现价 20-40%+, |delta| < 0.08, 到期概率极高

"赔率" 的定义:
  赔率 = 你收到的权利金 / 你承担的实际风险
  具体拆解为:
    1. 权利金厚度: bid 价格相对于保证金的比例 (年化)
    2. IV 偏高:    当前 IV 相对历史中位数偏高多少 (IV 贵 = 卖方赚)
    3. 安全距离:   BTC 需要跌多少才到盈亏平衡
    4. Theta 效率:  每天吃到权利金的速度
    5. IV Skew:    Put skew 越陡, 深度 OTM 越"贵"

监控功能:
  1. 持续监控所有深度 OTM BTC Put 的赔率
  2. 监控 IV 指标 (IV 均值, IV percentile, IV skew)
  3. 当赔率达到阈值时发出入场信号
  4. 监控已有持仓的盈亏和风险
  5. BTC 价格大幅波动预警

用法:
  python3 otm_put_monitor.py              # 运行一次快照
  python3 otm_put_monitor.py --loop       # 持续监控 (每60秒)
  python3 otm_put_monitor.py --loop 30    # 持续监控 (每30秒)
"""

import sys
import os
import time
import json
import math
import signal
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from binance_options import BinanceOptionsAPI, ts_to_str


# ============================================================
#  配置
# ============================================================
class Config:
    # --- 目标合约筛选 (硬门槛, 不满足直接淘汰) ---
    DELTA_MAX = 0.05          # |delta| 上限, 极深度 OTM
    OTM_MIN_PCT = 25.0        # 最小虚值程度 %, BTC至少跌25%才到行权价
    DTE_MIN = 14              # 最少到期天数, 太短gamma风险大
    DTE_MAX = 90              # 最多到期天数
    MIN_BID = 50.0            # 最低 bid (USDT), 低于此权利金太薄不值得
    MAX_SPREAD_PCT = 10.0     # 最大允许 spread %, 必须有基本流动性

    # --- 硬性安全门槛 (任一不满足直接淘汰) ---
    SAFETY_FLOOR = 25.0       # 安全垫硬底线 %, 低于此不考虑
    IV_PREMIUM_FLOOR = 15.0   # IV溢价硬底线 %, 低于此说明期权不够贵

    # --- 赔率评估阈值 ---
    # 年化收益率 (基于保证金)
    ANNUAL_RETURN_WATCH = 30.0     # % 开始关注
    ANNUAL_RETURN_GOOD = 50.0      # % 不错的赔率
    ANNUAL_RETURN_GREAT = 80.0     # % 极佳赔率

    # IV 偏高百分比 (vs 同期限中位数)
    IV_PREMIUM_WATCH = 20.0        # % 开始关注
    IV_PREMIUM_GOOD = 35.0         # % 不错
    IV_PREMIUM_GREAT = 60.0        # % 恐慌级别, 必须抓住

    # 安全垫评分标准
    SAFETY_MIN = 25.0              # % 最低可接受 (和硬底线一致)
    SAFETY_GOOD = 35.0             # % 不错
    SAFETY_GREAT = 45.0            # % 极安全, 满分

    # 综合赔率评分触发 (大幅提高门槛)
    ODDS_WATCH = 60                # 关注, 60分以下不推送
    ODDS_SIGNAL = 75               # 入场信号, 要75分
    ODDS_STRONG = 88               # 强信号, 极高赔率才触发

    # --- 持仓监控 (收紧风控) ---
    # 浮亏预警倍数 (相对权利金)
    LOSS_WARN_RATIO = 1.0          # 浮亏 = 1x 权利金就警告
    LOSS_ALERT_RATIO = 2.0         # 浮亏 = 2x 严重警告

    # BTC 跌幅预警
    BTC_DROP_WARN = 2.0            # % 日内跌2%就通知
    BTC_DROP_ALERT = 4.0           # % 日内跌4%紧急

    # 距行权价预警
    DIST_STRIKE_WARN = 18.0        # % 距行权价18%就注意
    DIST_STRIKE_ALERT = 12.0       # % 距行权价12%紧急

    # --- 保证金估算 ---
    MARGIN_RATE = 0.15

    # --- 监控 ---
    DEFAULT_INTERVAL = 60          # 默认监控间隔 (秒)


# ============================================================
#  IV 追踪器: 记录历史 IV 用于计算 percentile
# ============================================================
class IVTracker:
    """追踪 IV 历史, 计算 IV percentile 和趋势"""

    def __init__(self, history_file: str = "/root/projects/iv_history.json"):
        self.history_file = history_file
        self.history = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"snapshots": [], "daily_iv": {}}

    def save(self):
        with open(self.history_file, "w") as f:
            json.dump(self.history, f, indent=2)

    def record_snapshot(self, iv_data: dict):
        """
        记录一次 IV 快照
        iv_data: {"mean_iv": float, "median_iv": float, "skew_25d": float, "timestamp": int}
        """
        self.history["snapshots"].append(iv_data)
        # 保留最近 7 天的快照 (假设每分钟一次 = 10080 条)
        max_records = 10080
        if len(self.history["snapshots"]) > max_records:
            self.history["snapshots"] = self.history["snapshots"][-max_records:]

        # 按天记录 IV 均值
        day_key = datetime.fromtimestamp(
            iv_data["timestamp"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        if day_key not in self.history["daily_iv"]:
            self.history["daily_iv"][day_key] = []
        self.history["daily_iv"][day_key].append(iv_data["mean_iv"])

        # 保留最近 90 天
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        self.history["daily_iv"] = {
            k: v for k, v in self.history["daily_iv"].items() if k >= cutoff
        }

    def get_iv_percentile(self, current_iv: float) -> float:
        """当前 IV 在历史中的 percentile (0-100)"""
        all_ivs = []
        for day_ivs in self.history["daily_iv"].values():
            all_ivs.extend(day_ivs)
        # 需要至少 100 条数据 (约2小时的快照) 才有参考价值
        # 数据不足时返回 50 (中性), 不产生加成也不产生惩罚
        MIN_SAMPLES = 100
        if len(all_ivs) < MIN_SAMPLES:
            all_ivs = [s["mean_iv"] for s in self.history["snapshots"]]
        if len(all_ivs) < MIN_SAMPLES:
            return 50.0  # 数据不足, 返回中性值
        below = sum(1 for iv in all_ivs if iv <= current_iv)
        return below / len(all_ivs) * 100

    def has_enough_data(self) -> bool:
        """是否有足够的历史数据来计算 percentile"""
        total = sum(len(v) for v in self.history["daily_iv"].values())
        total += len(self.history["snapshots"])
        return total >= 100

    def get_iv_trend(self, window: int = 10) -> str:
        """最近若干快照的 IV 趋势"""
        snaps = self.history["snapshots"]
        if len(snaps) < window:
            return "数据不足"
        recent = [s["mean_iv"] for s in snaps[-window:]]
        older = [s["mean_iv"] for s in snaps[-window * 2:-window]] if len(snaps) >= window * 2 else recent
        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        change = (avg_recent - avg_older) / avg_older * 100 if avg_older > 0 else 0
        if change > 5:
            return f"急升 +{change:.1f}%"
        elif change > 2:
            return f"上升 +{change:.1f}%"
        elif change < -5:
            return f"急降 {change:.1f}%"
        elif change < -2:
            return f"下降 {change:.1f}%"
        else:
            return f"平稳 {change:+.1f}%"


# ============================================================
#  赔率计算引擎
# ============================================================
def calc_odds_score(
    bid: float,
    strike: float,
    spot: float,
    dte: float,
    delta: float,
    mark_iv: float,
    median_iv: float,
    theta: float,
    mark_price: float,
    spread_pct: float,
    volume: float,
    oi: float,
    iv_percentile: float,
) -> dict:
    """
    计算深度 OTM Put 的卖出赔率 (严格版)

    设计原则:
      - 安全优先: 安全垫权重最高, 且有硬底线
      - IV必须贵: 只在期权定价偏高时卖, 否则赔率不够
      - 不追收益: 年化高不代表好, 可能是因为太接近行权价
      - 流动性是前提: 没流动性的信号等于废纸

    返回: 各维度分数 + 综合赔率评分
    """
    cfg = Config()

    # === 基础计算 ===
    margin = max(spot, strike) * cfg.MARGIN_RATE
    annual_return = (bid / margin) * (365 / dte) * 100 if margin > 0 and dte > 0 else 0

    iv_premium = (mark_iv - median_iv) / median_iv * 100 if median_iv > 0 else 0

    breakeven = strike - bid
    safety_pct = (spot - breakeven) / spot * 100 if spot > 0 else 0
    otm_pct = (spot - strike) / spot * 100 if spot > 0 else 0

    theta_daily_pct = abs(theta) / mark_price * 100 if mark_price > 0 else 0

    # === 硬门槛: 不满足直接返回低分 ===
    hard_fail = False
    fail_reasons = []

    if safety_pct < cfg.SAFETY_FLOOR:
        hard_fail = True
        fail_reasons.append(f"安全垫{safety_pct:.1f}% < {cfg.SAFETY_FLOOR}%")

    if iv_premium < cfg.IV_PREMIUM_FLOOR:
        hard_fail = True
        fail_reasons.append(f"IV溢价{iv_premium:.1f}% < {cfg.IV_PREMIUM_FLOOR}%")

    if spread_pct > cfg.MAX_SPREAD_PCT:
        hard_fail = True
        fail_reasons.append(f"Spread {spread_pct:.1f}% > {cfg.MAX_SPREAD_PCT}%")

    # === 各维度评分 ===

    # 1. 年化收益评分 (0-100), 标准更高
    if annual_return >= cfg.ANNUAL_RETURN_GREAT:
        return_score = 100
    elif annual_return >= cfg.ANNUAL_RETURN_GOOD:
        return_score = 50 + (annual_return - cfg.ANNUAL_RETURN_GOOD) / (cfg.ANNUAL_RETURN_GREAT - cfg.ANNUAL_RETURN_GOOD) * 50
    elif annual_return >= cfg.ANNUAL_RETURN_WATCH:
        return_score = 10 + (annual_return - cfg.ANNUAL_RETURN_WATCH) / (cfg.ANNUAL_RETURN_GOOD - cfg.ANNUAL_RETURN_WATCH) * 40
    else:
        return_score = annual_return / cfg.ANNUAL_RETURN_WATCH * 10

    # 2. IV 溢价评分, 标准更高
    if iv_premium >= cfg.IV_PREMIUM_GREAT:
        iv_score = 100
    elif iv_premium >= cfg.IV_PREMIUM_GOOD:
        iv_score = 50 + (iv_premium - cfg.IV_PREMIUM_GOOD) / (cfg.IV_PREMIUM_GREAT - cfg.IV_PREMIUM_GOOD) * 50
    elif iv_premium >= cfg.IV_PREMIUM_WATCH:
        iv_score = 10 + (iv_premium - cfg.IV_PREMIUM_WATCH) / (cfg.IV_PREMIUM_GOOD - cfg.IV_PREMIUM_WATCH) * 40
    else:
        iv_score = 0  # 低于 WATCH 直接 0 分

    # IV percentile 加成 (需要70%以上才有加成, 更严格)
    iv_pctl_bonus = max((iv_percentile - 70) / 30 * 15, 0) if iv_percentile > 70 else 0

    # 3. 安全垫评分, 满分标准大幅提高
    if safety_pct >= cfg.SAFETY_GREAT:
        safety_score = 100
    elif safety_pct >= cfg.SAFETY_GOOD:
        safety_score = 50 + (safety_pct - cfg.SAFETY_GOOD) / (cfg.SAFETY_GREAT - cfg.SAFETY_GOOD) * 50
    elif safety_pct >= cfg.SAFETY_MIN:
        safety_score = 10 + (safety_pct - cfg.SAFETY_MIN) / (cfg.SAFETY_GOOD - cfg.SAFETY_MIN) * 40
    else:
        safety_score = 0  # 低于底线直接 0

    # 4. Theta 效率评分, 提高满分标准
    theta_score = min(theta_daily_pct / 8.0 * 100, 100)

    # 5. 流动性评分 (更严格)
    if spread_pct <= 2:
        liq_score = 80 + (2 - spread_pct) / 2 * 20
    elif spread_pct <= 5:
        liq_score = 40 + (5 - spread_pct) / 3 * 40
    elif spread_pct <= 10:
        liq_score = (10 - spread_pct) / 5 * 40
    else:
        liq_score = 0

    vol_bonus = min(volume / 10 * 10, 10)
    oi_bonus = min(oi / 20 * 10, 10)
    liq_score = min(liq_score + vol_bonus + oi_bonus, 100)

    # === 综合赔率 ===
    # 权重: 安全 35%, IV 25%, 收益 20%, 流动 12%, Theta 8%
    # 安全和IV权重提到60%, 这才是深度OTM卖方的核心
    raw_score = (
        safety_score * 0.35
        + (iv_score + iv_pctl_bonus) * 0.25
        + return_score * 0.20
        + liq_score * 0.12
        + theta_score * 0.08
    )

    # 硬门槛惩罚: 不达标直接压到 WAIT 区间
    if hard_fail:
        odds_score = min(raw_score * 0.5, cfg.ODDS_WATCH - 1)
    else:
        odds_score = raw_score

    # 信号等级
    if odds_score >= cfg.ODDS_STRONG:
        signal = "STRONG"
    elif odds_score >= cfg.ODDS_SIGNAL:
        signal = "SIGNAL"
    elif odds_score >= cfg.ODDS_WATCH:
        signal = "WATCH"
    else:
        signal = "WAIT"

    return {
        "annual_return": round(annual_return, 1),
        "return_score": round(return_score, 1),
        "iv_premium": round(iv_premium, 1),
        "iv_score": round(iv_score, 1),
        "iv_pctl_bonus": round(iv_pctl_bonus, 1),
        "iv_percentile": round(iv_percentile, 1),
        "safety_pct": round(safety_pct, 1),
        "safety_score": round(safety_score, 1),
        "otm_pct": round(otm_pct, 1),
        "breakeven": round(breakeven, 2),
        "theta_daily_pct": round(theta_daily_pct, 2),
        "theta_score": round(theta_score, 1),
        "liq_score": round(liq_score, 1),
        "spread_pct": round(spread_pct, 2),
        "margin_est": round(margin, 0),
        "odds_score": round(odds_score, 1),
        "signal": signal,
    }


# ============================================================
#  数据采集
# ============================================================
def fetch_market_data(api: BinanceOptionsAPI) -> dict:
    """一次性获取所有需要的市场数据"""
    spot = float(api.get_index_price("BTCUSDT")["indexPrice"])

    info = api.get_exchange_info()
    contracts = {s["symbol"]: s for s in info["optionSymbols"] if s["underlying"] == "BTCUSDT"}

    marks = api.get_mark_price()
    mark_map = {m["symbol"]: m for m in marks if m["symbol"].startswith("BTC")}

    tickers = api.get_ticker()
    ticker_map = {t["symbol"]: t for t in tickers if t["symbol"].startswith("BTC")}

    # OI
    expirations = set()
    for sym in contracts:
        parts = sym.split("-")
        if len(parts) >= 2:
            expirations.add(parts[1])

    oi_map = {}
    for exp in expirations:
        try:
            oi_list = api.get_open_interest("BTC", exp)
            for oi in oi_list:
                oi_map[oi["symbol"]] = float(oi.get("sumOpenInterest", 0))
        except Exception:
            pass

    return {
        "spot": spot,
        "contracts": contracts,
        "marks": mark_map,
        "tickers": ticker_map,
        "oi": oi_map,
        "timestamp": int(time.time() * 1000),
    }


def calc_iv_surface(data: dict) -> dict:
    """计算 IV 曲面统计"""
    iv_by_exp = defaultdict(list)
    all_put_ivs = []

    for sym, m in data["marks"].items():
        if "-P" not in sym:
            continue
        iv = float(m.get("markIV", 0))
        if iv <= 0:
            continue
        parts = sym.split("-")
        if len(parts) >= 2:
            iv_by_exp[parts[1]].append(iv)
            all_put_ivs.append(iv)

    stats = {}
    for exp, ivs in iv_by_exp.items():
        ivs_sorted = sorted(ivs)
        n = len(ivs_sorted)
        stats[exp] = {
            "median": ivs_sorted[n // 2] if n > 0 else 0,
            "mean": sum(ivs) / n if n > 0 else 0,
            "p25": ivs_sorted[n // 4] if n >= 4 else (ivs_sorted[0] if ivs_sorted else 0),
            "p75": ivs_sorted[3 * n // 4] if n >= 4 else (ivs_sorted[-1] if ivs_sorted else 0),
            "min": ivs_sorted[0] if ivs_sorted else 0,
            "max": ivs_sorted[-1] if ivs_sorted else 0,
        }

    all_sorted = sorted(all_put_ivs)
    n = len(all_sorted)
    global_stats = {
        "mean": sum(all_put_ivs) / n if n > 0 else 0,
        "median": all_sorted[n // 2] if n > 0 else 0,
        "count": n,
    }

    return {"by_exp": stats, "global": global_stats}


# ============================================================
#  扫描深度 OTM 机会
# ============================================================
def scan_opportunities(data: dict, iv_surface: dict, iv_tracker: IVTracker) -> list[dict]:
    """扫描所有深度 OTM Put, 计算赔率"""
    cfg = Config()
    spot = data["spot"]
    now = datetime.now(timezone.utc)

    # 全局 IV percentile
    iv_pctl = iv_tracker.get_iv_percentile(iv_surface["global"]["mean"])

    results = []

    for sym, contract in data["contracts"].items():
        if contract.get("side") != "PUT":
            continue

        mark = data["marks"].get(sym)
        ticker = data["tickers"].get(sym, {})

        if not mark:
            continue

        # 解析
        strike = float(contract["strikePrice"])
        expiry_ts = contract["expiryDate"]
        expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
        dte = max((expiry - now).total_seconds() / 86400, 0.01)

        delta = float(mark.get("delta", 0))
        abs_delta = abs(delta)
        theta = float(mark.get("theta", 0))
        gamma = float(mark.get("gamma", 0))
        vega = float(mark.get("vega", 0))
        mark_iv = float(mark.get("markIV", 0))
        mark_price = float(mark.get("markPrice", 0))

        bid = float(ticker.get("bidPrice", 0))
        ask = float(ticker.get("askPrice", 0))
        volume = float(ticker.get("volume", 0))
        oi = data["oi"].get(sym, 0)

        otm_pct = (spot - strike) / spot * 100

        # --- 筛选 ---
        if abs_delta > cfg.DELTA_MAX:
            continue
        if otm_pct < cfg.OTM_MIN_PCT:
            continue
        if dte < cfg.DTE_MIN or dte > cfg.DTE_MAX:
            continue
        if mark_price <= 0:
            continue
        if bid < cfg.MIN_BID:
            continue

        # spread
        spread_pct = ((ask - bid) / mark_price * 100) if ask > 0 and bid > 0 and mark_price > 0 else 999
        if spread_pct > cfg.MAX_SPREAD_PCT:
            continue

        # 同期限 IV 中位数
        exp_key = sym.split("-")[1]
        median_iv = iv_surface["by_exp"].get(exp_key, {}).get("median", mark_iv)

        # 计算赔率
        odds = calc_odds_score(
            bid=bid, strike=strike, spot=spot, dte=dte,
            delta=delta, mark_iv=mark_iv, median_iv=median_iv,
            theta=theta, mark_price=mark_price,
            spread_pct=spread_pct, volume=volume, oi=oi,
            iv_percentile=iv_pctl,
        )

        results.append({
            "symbol": sym,
            "strike": strike,
            "expiry": expiry.strftime("%Y-%m-%d"),
            "dte": round(dte, 1),
            "delta": delta,
            "mark_iv": mark_iv,
            "mark_price": mark_price,
            "bid": bid,
            "ask": ask,
            "volume": volume,
            "oi": oi,
            **odds,
        })

    results.sort(key=lambda x: x["odds_score"], reverse=True)
    return results


# ============================================================
#  挂单监控
# ============================================================
def monitor_open_orders(api: BinanceOptionsAPI, data: dict) -> list[dict]:
    """监控当前挂单，计算与市场价的距离"""
    results = []

    try:
        orders = api.get_open_orders()
    except Exception as e:
        return [{"type": "ERROR", "msg": f"获取挂单失败: {e}"}]

    for o in orders:
        sym = o["symbol"]
        side = o.get("side", "")
        order_price = float(o.get("price", 0))
        qty = float(o.get("quantity", 0))
        executed = float(o.get("executedQty", 0))
        status = o.get("status", "")

        # 从 ticker 获取市场价
        ticker = data["tickers"].get(sym, {})
        bid = float(ticker.get("bidPrice", 0))
        ask = float(ticker.get("askPrice", 0))
        mark_data = data["marks"].get(sym, {})
        mark_price = float(mark_data.get("markPrice", 0))

        # 计算距离成交的差距
        if side == "SELL":
            # 卖单: 需要有人出 bid >= 你的挂单价, 或者 ask 跌到你附近
            gap = order_price - bid if bid > 0 else 0
            gap_pct = (gap / order_price * 100) if order_price > 0 else 0
            gap_desc = f"挂卖 ${order_price:,.0f} vs 当前Bid ${bid:,.0f}"
        else:
            # 买单: 需要 ask <= 你的挂单价
            gap = ask - order_price if ask > 0 else 0
            gap_pct = (gap / order_price * 100) if order_price > 0 else 0
            gap_desc = f"挂买 ${order_price:,.0f} vs 当前Ask ${ask:,.0f}"

        results.append({
            "type": "ORDER",
            "symbol": sym,
            "side": side,
            "price": order_price,
            "qty": qty,
            "executed": executed,
            "status": status,
            "bid": bid,
            "ask": ask,
            "mark": mark_price,
            "gap": round(gap, 2),
            "gap_pct": round(gap_pct, 1),
            "gap_desc": gap_desc,
            "create_time": o.get("createTime", 0),
        })

    return results


# ============================================================
#  持仓监控
# ============================================================
def monitor_positions(api: BinanceOptionsAPI, data: dict) -> list[dict]:
    """监控当前持仓"""
    cfg = Config()
    spot = data["spot"]
    alerts = []

    try:
        positions = api.get_position()
    except Exception as e:
        return [{"type": "ERROR", "msg": f"获取持仓失败: {e}"}]

    for p in positions:
        qty = float(p.get("quantity", 0))
        if qty == 0:
            continue

        sym = p["symbol"]
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        parts = sym.split("-")

        # 只关注卖出的 Put
        if qty > 0 or "-P" not in sym:
            continue

        strike = float(parts[2]) if len(parts) >= 4 else 0
        abs_qty = abs(qty)

        # 浮盈亏
        pnl = (entry - mark) * abs_qty  # 卖出: entry > mark 则盈利

        # 浮亏比例 (相对权利金)
        premium_collected = entry * abs_qty
        loss_ratio = (mark - entry) / entry if entry > 0 else 0

        # 距行权价距离
        dist_to_strike = (spot - strike) / spot * 100 if spot > 0 else 0

        info = {
            "type": "POSITION",
            "symbol": sym,
            "qty": qty,
            "entry": entry,
            "mark": mark,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / premium_collected * 100, 1) if premium_collected > 0 else 0,
            "dist_to_strike": round(dist_to_strike, 1),
        }

        # 预警判断 (严格风控)
        alert_level = "OK"
        msgs = []

        if loss_ratio > 0:
            msgs.append(f"浮亏 {loss_ratio:.1f}x 权利金")

        if loss_ratio >= cfg.LOSS_ALERT_RATIO:
            alert_level = "DANGER"
            msgs.append("建议立即平仓止损!")
        elif loss_ratio >= cfg.LOSS_WARN_RATIO:
            alert_level = "WARNING"
            msgs.append("接近止损线")

        if dist_to_strike < cfg.DIST_STRIKE_ALERT:
            alert_level = "DANGER"
            msgs.append(f"距行权价仅 {dist_to_strike:.1f}%! 极度危险")
        elif dist_to_strike < cfg.DIST_STRIKE_WARN:
            if alert_level != "DANGER":
                alert_level = "WARNING"
            msgs.append(f"距行权价 {dist_to_strike:.1f}%, 注意风险")

        if alert_level == "OK":
            msgs = [f"浮盈 {-loss_ratio:.0%}  距行权 {dist_to_strike:.1f}%"]

        info["alert"] = alert_level
        info["msg"] = " | ".join(msgs)

        alerts.append(info)

    return alerts


# ============================================================
#  输出渲染
# ============================================================
class Display:

    @staticmethod
    def clear():
        os.system("clear" if os.name != "nt" else "cls")

    @staticmethod
    def header(spot: float, iv_surface: dict, iv_tracker: IVTracker):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mean_iv = iv_surface["global"]["mean"]
        iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
        iv_trend = iv_tracker.get_iv_trend()

        print(f"""
{'='*94}
  BTC 深度 OTM Put 监控系统                                      {now}
{'='*94}
  BTC 价格: ${spot:>10,.2f}
  Put IV 均值: {mean_iv:.3f}  |  IV Percentile: {iv_pctl:.0f}%  |  IV 趋势: {iv_trend}
{'─'*94}""")

    @staticmethod
    def positions(pos_alerts: list):
        if not pos_alerts:
            return

        print(f"\n  [ 持仓监控 ]")
        print(f"  {'─'*88}")

        for p in pos_alerts:
            if p["type"] == "ERROR":
                print(f"  ! {p['msg']}")
                continue

            icon = {"OK": "+", "WARNING": "!", "DANGER": "!!!"}.get(p.get("alert", ""), " ")
            tag = {"OK": "   ", "WARNING": "注意", "DANGER": "危险"}.get(p.get("alert", ""), "   ")

            sym = p["symbol"]
            print(f"  {icon} [{tag}] {sym}  |  "
                  f"数量: {p['qty']}  |  入场: ${p['entry']:,.0f}  |  当前: ${p['mark']:,.0f}  |  "
                  f"盈亏: ${p['pnl']:>+,.0f} ({p['pnl_pct']:>+.0f}%)  |  "
                  f"距行权: {p['dist_to_strike']:.1f}%")
            if p.get("msg"):
                print(f"    └─ {p['msg']}")

    @staticmethod
    def iv_surface(iv_surface: dict):
        print(f"\n  [ IV 曲面 ]")
        print(f"  {'─'*88}")
        print(f"  {'到期日':<12} {'中位IV':>8} {'均值IV':>8} {'P25':>8} {'P75':>8} {'最低':>8} {'最高':>8}")
        for exp in sorted(iv_surface["by_exp"].keys()):
            s = iv_surface["by_exp"][exp]
            print(f"  {exp:<12} {s['median']:>7.3f}  {s['mean']:>7.3f}  "
                  f"{s['p25']:>7.3f}  {s['p75']:>7.3f}  {s['min']:>7.3f}  {s['max']:>7.3f}")

    @staticmethod
    def opportunities(results: list, show_all: bool = False):
        if not results:
            print(f"\n  [ 当前无符合条件的机会 ]")
            return

        # 分组显示
        strong = [r for r in results if r["signal"] == "STRONG"]
        signal = [r for r in results if r["signal"] == "SIGNAL"]
        watch = [r for r in results if r["signal"] == "WATCH"]
        wait = [r for r in results if r["signal"] == "WAIT"]

        if strong:
            print(f"\n  {'>'*5} 强信号 (赔率极佳, 建议入场) {'<'*5}")
            print(f"  {'─'*88}")
            for r in strong:
                Display._print_opportunity(r, ">>>")

        if signal:
            print(f"\n  [!] 入场信号 (赔率不错)")
            print(f"  {'─'*88}")
            for r in signal:
                Display._print_opportunity(r, " ! ")

        if watch:
            print(f"\n  [~] 关注中 (接近入场条件)")
            print(f"  {'─'*88}")
            for r in watch[:10]:
                Display._print_opportunity(r, " ~ ")
            if len(watch) > 10:
                print(f"       ... 还有 {len(watch)-10} 个关注中的合约")

        if show_all and wait:
            print(f"\n  [.] 等待中 ({len(wait)} 个)")
            print(f"  {'─'*88}")
            for r in wait[:5]:
                Display._print_opportunity(r, "   ")

        # 汇总
        print(f"\n  {'─'*88}")
        print(f"  汇总: 强信号 {len(strong)} | 入场信号 {len(signal)} | "
              f"关注 {len(watch)} | 等待 {len(wait)}")

    @staticmethod
    def _print_opportunity(r: dict, prefix: str):
        print(f"  {prefix} {r['symbol']:<28}  赔率: {r['odds_score']:>5.1f}  |  "
              f"Bid ${r['bid']:>7,.0f}  |  OTM {r['otm_pct']:>5.1f}%  |  "
              f"安全垫 {r['safety_pct']:>5.1f}%  |  年化 {r['annual_return']:>5.1f}%  |  "
              f"IV溢价 {r['iv_premium']:>+5.1f}%")
        print(f"       行权 ${r['strike']:>8,.0f}  到期 {r['expiry']}({r['dte']:.0f}d)  "
              f"Delta {r['delta']:.5f}  IV {r['mark_iv']:.3f}  "
              f"日衰 {r['theta_daily_pct']:.2f}%  Spread {r['spread_pct']:.1f}%  "
              f"Vol {r['volume']:.0f}")

    @staticmethod
    def footer(scan_time: float, next_scan: int = 0):
        print(f"\n{'='*94}")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if next_scan > 0:
            print(f"  扫描耗时 {scan_time:.1f}s  |  下次扫描: {next_scan}s 后  |  Ctrl+C 退出")
        else:
            print(f"  扫描耗时 {scan_time:.1f}s  |  单次扫描完成")
        print(f"{'='*94}")

    @staticmethod
    def signal_alert(results: list):
        """只打印信号变化 (用于持续监控模式)"""
        strong = [r for r in results if r["signal"] == "STRONG"]
        signal = [r for r in results if r["signal"] == "SIGNAL"]

        if strong or signal:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n  {'*'*40}")
            print(f"  *  {now}  新信号!")
            for r in strong:
                print(f"  *  >>> STRONG: {r['symbol']} 赔率 {r['odds_score']:.1f} "
                      f"Bid ${r['bid']:,.0f} 年化 {r['annual_return']:.0f}%")
            for r in signal:
                print(f"  *  !   SIGNAL: {r['symbol']} 赔率 {r['odds_score']:.1f} "
                      f"Bid ${r['bid']:,.0f} 年化 {r['annual_return']:.0f}%")
            print(f"  {'*'*40}")


# ============================================================
#  主运行逻辑
# ============================================================
def run_once(api: BinanceOptionsAPI, iv_tracker: IVTracker, show_all: bool = False):
    """运行一次完整扫描"""

    t0 = time.time()

    # 1. 获取数据
    data = fetch_market_data(api)

    # 2. 计算 IV 曲面
    iv_surface = calc_iv_surface(data)

    # 3. 记录 IV 快照
    iv_tracker.record_snapshot({
        "mean_iv": iv_surface["global"]["mean"],
        "median_iv": iv_surface["global"]["median"],
        "timestamp": data["timestamp"],
    })
    iv_tracker.save()

    # 4. 扫描机会
    results = scan_opportunities(data, iv_surface, iv_tracker)

    # 5. 监控持仓
    pos_alerts = monitor_positions(api, data)

    scan_time = time.time() - t0

    return {
        "data": data,
        "iv_surface": iv_surface,
        "results": results,
        "pos_alerts": pos_alerts,
        "scan_time": scan_time,
    }


def display_full(result: dict, iv_tracker: IVTracker, show_all: bool = False, interval: int = 0):
    """完整输出"""
    Display.clear()
    Display.header(result["data"]["spot"], result["iv_surface"], iv_tracker)
    Display.positions(result["pos_alerts"])
    Display.iv_surface(result["iv_surface"])
    Display.opportunities(result["results"], show_all=show_all)
    Display.footer(result["scan_time"], next_scan=interval)


def run_loop(api: BinanceOptionsAPI, iv_tracker: IVTracker, interval: int):
    """持续监控循环"""

    # 上一轮的信号合约, 用于检测新信号
    prev_signals = set()

    def handle_exit(sig, frame):
        print("\n\n  监控已停止。")
        iv_tracker.save()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    scan_count = 0

    while True:
        try:
            result = run_once(api, iv_tracker)
            scan_count += 1

            # 每 5 次完整刷新, 其他时候只显示信号变化
            if scan_count % 5 == 1:
                display_full(result, iv_tracker, show_all=False, interval=interval)
            else:
                # 检查新信号
                current_signals = {
                    r["symbol"] for r in result["results"]
                    if r["signal"] in ("STRONG", "SIGNAL")
                }
                new_signals = current_signals - prev_signals
                if new_signals:
                    new_results = [
                        r for r in result["results"]
                        if r["symbol"] in new_signals
                    ]
                    Display.signal_alert(new_results)

                # 检查持仓预警
                for p in result["pos_alerts"]:
                    if p.get("alert") in ("WARNING", "DANGER"):
                        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        print(f"  [{now}] 持仓预警: {p.get('symbol')} - {p.get('msg')}")

                # 简要状态行
                spot = result["data"]["spot"]
                n_strong = len([r for r in result["results"] if r["signal"] == "STRONG"])
                n_signal = len([r for r in result["results"] if r["signal"] == "SIGNAL"])
                n_watch = len([r for r in result["results"] if r["signal"] == "WATCH"])
                now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{now}] BTC ${spot:,.0f} | "
                      f"强信号:{n_strong} 信号:{n_signal} 关注:{n_watch} | "
                      f"{result['scan_time']:.1f}s",
                      end="\r")

            prev_signals = {
                r["symbol"] for r in result["results"]
                if r["signal"] in ("STRONG", "SIGNAL")
            }

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\n\n  监控已停止。")
            iv_tracker.save()
            break
        except Exception as e:
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n  [{now}] 错误: {e}")
            time.sleep(interval)


# ============================================================
#  入口
# ============================================================
def main():
    api = BinanceOptionsAPI()
    iv_tracker = IVTracker()

    args = sys.argv[1:]

    if "--loop" in args:
        idx = args.index("--loop")
        interval = int(args[idx + 1]) if idx + 1 < len(args) and args[idx + 1].isdigit() else Config.DEFAULT_INTERVAL
        print(f"  启动持续监控模式, 间隔 {interval}s ...")
        run_loop(api, iv_tracker, interval)
    else:
        result = run_once(api, iv_tracker)
        display_full(result, iv_tracker, show_all=True)


if __name__ == "__main__":
    main()
