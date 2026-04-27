"""
高赔率卖 BTC Put 策略

核心思路:
  卖 Put 的盈利来源是时间价值衰减(theta)，风险是BTC大跌穿过行权价。
  "高赔率"意味着: 胜率高、收益/风险比合理、期权定价偏贵(IV偏高)。

策略框架 - 多维度评分筛选:

  1. Delta 筛选 (虚值程度)
     - 目标: delta -0.05 ~ -0.30 (OTM Put)
     - delta 越小(绝对值) → 越虚值 → 胜率越高但权利金越少
     - 甜蜜区: -0.10 ~ -0.20 (大约 80-90% 胜率)

  2. IV Rank / IV 偏度
     - 当 Put 的 IV 高于历史均值 → 期权"贵" → 卖方有优势
     - markIV vs 同期限其他行权价的 IV → 检测 volatility skew
     - IV skew 陡峭时, OTM Put 相对更贵 → 更适合卖

  3. Theta / Price 比 (日衰减效率)
     - theta / markPrice → 每天收割权利金的百分比
     - 越高说明时间衰减越快, 赔率越好

  4. 到期日选择
     - 卖方最佳: 14-45 天到期 (theta 加速衰减的甜蜜区)
     - 太短: 权利金太少, gamma 风险大
     - 太长: 资金占用久, 不确定性高

  5. 流动性
     - bid-ask spread 要小 (< 5%)
     - 有足够的成交量和 OI

  6. 风险收益比
     - 最大收益 = 权利金
     - 盈亏平衡点 = 行权价 - 权利金
     - 安全垫 = (当前价 - 盈亏平衡) / 当前价
     - 年化收益率 = 权利金 / 保证金 * (365 / 到期天数)

综合评分公式:
  score = w1 * theta_efficiency    (时间衰减效率)
        + w2 * iv_percentile       (IV 偏高程度)
        + w3 * safety_cushion      (安全垫)
        + w4 * liquidity_score     (流动性)
        + w5 * dte_score           (到期日合理性)
        - penalty * gamma_risk     (gamma 风险惩罚)
"""

import math
import json
from datetime import datetime, timezone, timedelta
from binance_options import BinanceOptionsAPI, ts_to_str


# ============================================================
#  配置参数
# ============================================================
class Config:
    # Delta 筛选范围 (绝对值)
    DELTA_MIN = 0.03        # 最小 |delta| (太虚值没有权利金)
    DELTA_MAX = 0.35        # 最大 |delta| (太实值风险大)
    DELTA_SWEET_MIN = 0.08  # 甜蜜区
    DELTA_SWEET_MAX = 0.22  # 甜蜜区

    # 到期天数
    DTE_MIN = 2             # 最少天数
    DTE_MAX = 90            # 最多天数
    DTE_SWEET_MIN = 14      # 甜蜜区
    DTE_SWEET_MAX = 45      # 甜蜜区

    # 流动性
    MAX_SPREAD_PCT = 8.0    # 最大允许 bid-ask spread %
    MIN_BID_PRICE = 5.0     # 最低 bid 价格 (USDT), 太便宜没意义

    # 评分权重
    W_THETA_EFF = 25        # 时间衰减效率
    W_IV_PREMIUM = 20       # IV 溢价
    W_SAFETY = 25           # 安全垫
    W_LIQUIDITY = 15        # 流动性
    W_DTE = 15              # 到期日合理性

    # 保证金估算 (简化)
    MARGIN_RATE = 0.15      # 初始保证金比例


# ============================================================
#  数据获取
# ============================================================
def fetch_all_data(api: BinanceOptionsAPI) -> dict:
    """获取策略所需的全部数据"""

    print("正在获取数据...")

    # 1. BTC 指数价格
    idx = api.get_index_price("BTCUSDT")
    spot_price = float(idx["indexPrice"])
    print(f"  BTC 指数价格: ${spot_price:,.2f}")

    # 2. 所有 BTC 期权合约信息
    info = api.get_exchange_info()
    btc_options = [s for s in info["optionSymbols"] if s["underlying"] == "BTCUSDT"]
    btc_puts = {s["symbol"]: s for s in btc_options if s["side"] == "PUT"}
    btc_calls = {s["symbol"]: s for s in btc_options if s["side"] == "CALL"}
    print(f"  BTC Put 合约: {len(btc_puts)} 个")

    # 3. 标记价格 + 希腊值
    all_marks = api.get_mark_price()
    btc_marks = {m["symbol"]: m for m in all_marks if m["symbol"].startswith("BTC")}

    # 4. Ticker 行情
    all_tickers = api.get_ticker()
    btc_tickers = {t["symbol"]: t for t in all_tickers if t["symbol"].startswith("BTC")}

    # 5. 收集所有到期日的 OI
    expirations = set()
    for sym in btc_puts:
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

    print(f"  OI 数据: {len(oi_map)} 条")

    return {
        "spot_price": spot_price,
        "btc_puts": btc_puts,
        "btc_calls": btc_calls,
        "btc_marks": btc_marks,
        "btc_tickers": btc_tickers,
        "oi_map": oi_map,
    }


# ============================================================
#  分析每个 Put 合约
# ============================================================
def analyze_put(symbol: str, data: dict, iv_stats: dict) -> dict | None:
    """分析单个 Put 合约, 返回分析结果或 None (不满足条件)"""

    cfg = Config()
    spot = data["spot_price"]
    contract = data["btc_puts"].get(symbol)
    mark = data["btc_marks"].get(symbol)
    ticker = data["btc_tickers"].get(symbol, {})

    if not contract or not mark:
        return None

    # --- 基础数据 ---
    strike = float(contract["strikePrice"])
    expiry_ts = contract["expiryDate"]
    now = datetime.now(timezone.utc)
    expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
    dte = max((expiry - now).total_seconds() / 86400, 0.01)  # 到期天数

    delta = float(mark.get("delta", 0))
    abs_delta = abs(delta)
    theta = float(mark.get("theta", 0))  # 通常为负数
    gamma = float(mark.get("gamma", 0))
    vega = float(mark.get("vega", 0))
    mark_iv = float(mark.get("markIV", 0))
    mark_price = float(mark.get("markPrice", 0))
    bid_iv = float(mark.get("bidIV", 0))
    ask_iv = float(mark.get("askIV", 0))

    bid = float(ticker.get("bidPrice", 0))
    ask = float(ticker.get("askPrice", 0))
    volume = float(ticker.get("volume", 0))
    oi = data["oi_map"].get(symbol, 0)

    # --- 基础筛选 ---
    # Delta 范围
    if abs_delta < cfg.DELTA_MIN or abs_delta > cfg.DELTA_MAX:
        return None

    # 到期天数
    if dte < cfg.DTE_MIN or dte > cfg.DTE_MAX:
        return None

    # 标记价格有效
    if mark_price <= 0:
        return None

    # Bid 价格有效 (要能卖出去)
    if bid < cfg.MIN_BID_PRICE:
        return None

    # Bid-Ask spread
    spread_pct = ((ask - bid) / mark_price * 100) if ask > 0 and bid > 0 else 999
    if spread_pct > cfg.MAX_SPREAD_PCT:
        return None

    # --- 计算各维度指标 ---

    # 1. Theta 效率: 每天衰减占 mark price 的百分比
    #    theta 是负数, 对卖方有利
    theta_daily_pct = abs(theta) / mark_price * 100 if mark_price > 0 else 0

    # 2. IV 溢价: 该合约的 IV 相对于同期限 Put 的 IV 中位数
    exp_key = symbol.split("-")[1]
    median_iv = iv_stats.get(exp_key, {}).get("median_iv", mark_iv)
    mean_iv = iv_stats.get(exp_key, {}).get("mean_iv", mark_iv)
    iv_premium = ((mark_iv - median_iv) / median_iv * 100) if median_iv > 0 else 0

    # 3. 安全垫: 当前价距离盈亏平衡点的百分比
    #    盈亏平衡 = 行权价 - 权利金(用bid, 因为卖出得到bid)
    breakeven = strike - bid
    safety_cushion = (spot - breakeven) / spot * 100 if spot > 0 else 0

    # 4. 虚值程度 (OTM %)
    otm_pct = (spot - strike) / spot * 100 if spot > 0 else 0

    # 5. 年化收益率 (基于保证金)
    #    保证金 ≈ max(标的价格 * margin_rate, 行权价 * margin_rate)
    margin_est = max(spot, strike) * cfg.MARGIN_RATE
    annualized_return = (bid / margin_est) * (365 / dte) * 100 if margin_est > 0 and dte > 0 else 0

    # 6. Gamma 风险: gamma / delta, gamma 越大相对 delta 越危险
    gamma_risk = abs(gamma) / abs_delta * spot if abs_delta > 0 else 0

    # --- 评分 ---

    # Theta 效率评分 (0-100): 日衰减 > 3% 满分
    theta_score = min(theta_daily_pct / 3.0 * 100, 100)

    # IV 溢价评分 (0-100): IV 比中位数高 20% 满分, 低于中位数扣分
    iv_score = min(max((iv_premium + 10) / 30 * 100, 0), 100)

    # 安全垫评分 (0-100): 安全垫 > 15% 满分
    safety_score = min(max(safety_cushion / 15 * 100, 0), 100)

    # 流动性评分 (0-100): spread < 1% 且有 volume 和 OI 满分
    spread_score = max(100 - spread_pct * 20, 0)  # spread 5% → 0分
    volume_score = min(volume / 10 * 50, 50)       # volume ≥ 10 → 50分
    oi_score_val = min(oi / 20 * 50, 50)           # OI ≥ 20 → 50分
    liquidity_score = spread_score * 0.5 + volume_score * 0.25 + oi_score_val * 0.25

    # DTE 评分: 14-45天甜蜜区满分
    if cfg.DTE_SWEET_MIN <= dte <= cfg.DTE_SWEET_MAX:
        dte_score = 100
    elif dte < cfg.DTE_SWEET_MIN:
        dte_score = max(dte / cfg.DTE_SWEET_MIN * 100, 0)
    else:
        dte_score = max(100 - (dte - cfg.DTE_SWEET_MAX) / cfg.DTE_SWEET_MAX * 100, 0)

    # Delta 甜蜜区加分
    delta_bonus = 0
    if cfg.DELTA_SWEET_MIN <= abs_delta <= cfg.DELTA_SWEET_MAX:
        delta_bonus = 10

    # Gamma 惩罚: gamma risk 越大越危险
    gamma_penalty = min(gamma_risk / 0.5 * 10, 15)

    # 综合评分
    total_score = (
        cfg.W_THETA_EFF * theta_score / 100
        + cfg.W_IV_PREMIUM * iv_score / 100
        + cfg.W_SAFETY * safety_score / 100
        + cfg.W_LIQUIDITY * liquidity_score / 100
        + cfg.W_DTE * dte_score / 100
        + delta_bonus
        - gamma_penalty
    )

    return {
        "symbol": symbol,
        "strike": strike,
        "expiry": expiry.strftime("%Y-%m-%d"),
        "dte": round(dte, 1),
        "delta": delta,
        "abs_delta": abs_delta,
        "theta": theta,
        "gamma": gamma,
        "vega": vega,
        "mark_iv": mark_iv,
        "mark_price": mark_price,
        "bid": bid,
        "ask": ask,
        "spread_pct": round(spread_pct, 2),
        "volume": volume,
        "oi": oi,
        "otm_pct": round(otm_pct, 2),
        "breakeven": round(breakeven, 2),
        "safety_cushion": round(safety_cushion, 2),
        "theta_daily_pct": round(theta_daily_pct, 2),
        "iv_premium": round(iv_premium, 2),
        "annualized_return": round(annualized_return, 2),
        "gamma_risk": round(gamma_risk, 4),
        # 子评分
        "theta_score": round(theta_score, 1),
        "iv_score": round(iv_score, 1),
        "safety_score": round(safety_score, 1),
        "liquidity_score": round(liquidity_score, 1),
        "dte_score": round(dte_score, 1),
        "delta_bonus": delta_bonus,
        "gamma_penalty": round(gamma_penalty, 1),
        "total_score": round(total_score, 2),
    }


# ============================================================
#  计算 IV 统计 (按到期日分组)
# ============================================================
def calc_iv_stats(data: dict) -> dict:
    """计算每个到期日的 IV 中位数和均值"""
    iv_by_exp = {}
    for sym, mark in data["btc_marks"].items():
        if not sym.startswith("BTC") or "-P" not in sym:
            continue
        parts = sym.split("-")
        if len(parts) < 2:
            continue
        exp = parts[1]
        iv = float(mark.get("markIV", 0))
        if iv > 0:
            iv_by_exp.setdefault(exp, []).append(iv)

    stats = {}
    for exp, ivs in iv_by_exp.items():
        ivs_sorted = sorted(ivs)
        n = len(ivs_sorted)
        median = ivs_sorted[n // 2] if n > 0 else 0
        mean = sum(ivs) / n if n > 0 else 0
        stats[exp] = {
            "median_iv": median,
            "mean_iv": mean,
            "min_iv": ivs_sorted[0] if ivs_sorted else 0,
            "max_iv": ivs_sorted[-1] if ivs_sorted else 0,
            "count": n,
        }
    return stats


# ============================================================
#  主策略: 筛选 + 评分 + 排序
# ============================================================
def run_strategy(api: BinanceOptionsAPI = None) -> list[dict]:
    """运行卖 Put 策略, 返回按评分排序的结果"""

    if api is None:
        api = BinanceOptionsAPI()

    data = fetch_all_data(api)
    iv_stats = calc_iv_stats(data)

    print(f"\n开始分析 {len(data['btc_puts'])} 个 BTC Put 合约...")

    results = []
    for symbol in data["btc_puts"]:
        analysis = analyze_put(symbol, data, iv_stats)
        if analysis:
            results.append(analysis)

    results.sort(key=lambda x: x["total_score"], reverse=True)

    print(f"通过筛选的合约: {len(results)} 个\n")

    return results


# ============================================================
#  输出格式化
# ============================================================
def print_results(results: list[dict], top_n: int = 15):
    """打印策略结果"""

    print("=" * 90)
    print("  高赔率卖 BTC Put 策略 - 推荐合约排名")
    print("=" * 90)

    if not results:
        print("  没有找到符合条件的合约。")
        return

    # Top picks 详细展示
    print(f"\n{'='*90}")
    print(f"  TOP {min(top_n, len(results))} 推荐")
    print(f"{'='*90}")

    for i, r in enumerate(results[:top_n], 1):
        # 信号强度
        if r["total_score"] >= 70:
            signal = "★★★ 强烈推荐"
        elif r["total_score"] >= 55:
            signal = "★★  推荐"
        elif r["total_score"] >= 40:
            signal = "★   可关注"
        else:
            signal = "    一般"

        print(f"\n  #{i} {r['symbol']}  [{signal}]  总评分: {r['total_score']}")
        print(f"  {'─'*70}")
        print(f"  合约信息: 行权价 ${r['strike']:,.0f} | 到期 {r['expiry']} ({r['dte']}天)")
        print(f"  定价:     标记价 ${r['mark_price']:,.1f} | Bid ${r['bid']:,.1f} / Ask ${r['ask']:,.1f} | Spread {r['spread_pct']:.1f}%")
        print(f"  希腊值:   Delta {r['delta']:.4f} | Theta {r['theta']:.2f} | Gamma {r['gamma']:.8f} | IV {r['mark_iv']:.3f}")
        print(f"  核心指标:")
        print(f"    虚值程度(OTM):  {r['otm_pct']:.1f}%  (当前价比行权价高 {r['otm_pct']:.1f}%)")
        print(f"    安全垫:         {r['safety_cushion']:.1f}%  (BTC跌到 ${r['breakeven']:,.0f} 才亏)")
        print(f"    日衰减效率:     {r['theta_daily_pct']:.2f}%/天")
        print(f"    IV溢价:         {r['iv_premium']:+.1f}%  (相对同期限中位数)")
        print(f"    年化收益率:     {r['annualized_return']:.1f}%  (基于预估保证金)")
        print(f"  评分明细: Theta={r['theta_score']:.0f} | IV={r['iv_score']:.0f} | "
              f"安全={r['safety_score']:.0f} | 流动={r['liquidity_score']:.0f} | "
              f"DTE={r['dte_score']:.0f} | Delta加分={r['delta_bonus']} | "
              f"Gamma罚分={r['gamma_penalty']:.0f}")

    # 汇总表
    print(f"\n\n{'='*90}")
    print("  汇总表")
    print(f"{'='*90}")
    header = f"{'#':>3} {'合约':<28} {'评分':>5} {'行权价':>8} {'到期天':>5} {'Delta':>7} {'Bid':>8} {'安全垫%':>7} {'年化%':>7} {'IV':>6} {'日衰%':>6}"
    print(header)
    print("-" * len(header))

    for i, r in enumerate(results[:top_n], 1):
        print(f"{i:>3} {r['symbol']:<28} {r['total_score']:>5.1f} "
              f"${r['strike']:>7,.0f} {r['dte']:>5.1f} {r['delta']:>7.4f} "
              f"${r['bid']:>7,.1f} {r['safety_cushion']:>6.1f}% "
              f"{r['annualized_return']:>6.1f}% {r['mark_iv']:>5.3f} {r['theta_daily_pct']:>5.2f}%")

    # 风险提醒
    print(f"\n\n{'='*90}")
    print("  风险提醒")
    print(f"{'='*90}")
    print("""
  1. 卖 Put 的最大亏损理论上可达 (行权价 - 权利金), BTC归零则亏损巨大
  2. 上述年化收益率基于保证金的粗略估算, 实际保证金可能更高
  3. IV 会突然飙升(如黑天鹅事件), 导致浮亏急剧扩大
  4. 建议单个合约不超过总仓位的 10-15%
  5. 设置心理止损位: 当亏损达到收取权利金的 2-3 倍时考虑平仓
  6. 避免在重大事件(如 FOMC, 减半等)前集中卖 Put
  7. Gamma 风险: 临近到期时, 如果接近行权价, PnL 波动会急剧放大
""")


# ============================================================
#  快速决策辅助
# ============================================================
def quick_decision(results: list[dict], max_positions: int = 3):
    """给出快速操作建议"""

    if not results:
        print("没有推荐的合约。")
        return

    print(f"\n{'='*90}")
    print(f"  快速决策 (建议最多同时持有 {max_positions} 个仓位)")
    print(f"{'='*90}")

    # 按到期日分散
    by_expiry = {}
    for r in results:
        exp = r["expiry"]
        if exp not in by_expiry:
            by_expiry[exp] = r

    picks = list(by_expiry.values())[:max_positions]

    if not picks:
        picks = results[:max_positions]

    for i, r in enumerate(picks, 1):
        print(f"\n  仓位 {i}: 卖出 {r['symbol']}")
        print(f"  操作: SELL PUT @ Bid ${r['bid']:,.1f}")
        print(f"  到期: {r['expiry']} ({r['dte']:.0f}天后)")
        print(f"  行权价: ${r['strike']:,.0f} (当前价需跌 {r['otm_pct']:.1f}% 才到行权价)")
        print(f"  安全垫: {r['safety_cushion']:.1f}% (跌到 ${r['breakeven']:,.0f} 才亏)")
        print(f"  最大收益: ${r['bid']:,.1f} / 张")
        margin_est = max(r['strike'], r['mark_price'] / abs(r['delta']) if r['delta'] != 0 else r['strike']) * Config.MARGIN_RATE
        print(f"  预估保证金: ~${margin_est:,.0f} / 张")
        print(f"  建议止损: 当期权价格涨到 ${r['bid'] * 2.5:,.0f} 时平仓 (亏损约 1.5x 权利金)")

    print(f"\n  ----")
    print(f"  组合总评分: {sum(p['total_score'] for p in picks) / len(picks):.1f}")
    print(f"  到期日覆盖: {', '.join(sorted(set(p['expiry'] for p in picks)))}")


# ============================================================
#  主入口
# ============================================================
def main():
    api = BinanceOptionsAPI()
    results = run_strategy(api)
    print_results(results, top_n=15)
    quick_decision(results, max_positions=3)


if __name__ == "__main__":
    main()
