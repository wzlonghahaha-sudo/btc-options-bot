#!/usr/bin/env python3
"""
账户期权交易盈亏综合分析
拉取: 成交历史 / 资金流水(Bill) / 当前持仓 / 账户信息
计算: 已实现盈亏 + 未实现盈亏(浮盈浮亏) + 手续费 = 净盈亏
"""

import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from binance_options import BinanceOptionsAPI, ts_to_str

api = BinanceOptionsAPI()

# ============================================================
# 1. 拉取全部成交历史 (分页遍历)
# ============================================================
def fetch_all_trades():
    """分页拉取全部成交历史, 使用 fromId 分页"""
    all_trades = []
    from_id = None
    page = 0
    
    while True:
        page += 1
        params = {}
        if from_id:
            params["from_id"] = from_id
        
        try:
            trades = api.get_user_trades(limit=100, **params)
        except Exception as e:
            print(f"  [!] 拉取第{page}页失败: {e}")
            break
            
        if not trades:
            break
            
        all_trades.extend(trades)
        print(f"  第{page}页: 获取 {len(trades)} 条, 累计 {len(all_trades)} 条")
        
        if len(trades) < 100:
            break
            
        # 用最后一条的 id 作为下一页起点
        last_id = trades[-1].get("id", trades[-1].get("tradeId"))
        if last_id:
            from_id = int(last_id) + 1
        else:
            break
            
        time.sleep(0.3)
    
    return all_trades


# ============================================================
# 2. 拉取全部资金流水 (Bill) — 用 recordId 分页
# ============================================================
def fetch_all_bills():
    """分页拉取全部资金流水"""
    all_bills = []
    page = 0
    
    while True:
        page += 1
        params = {"currency": "USDT", "limit": 1000}
        
        # 如果已有数据, 用最小 id 继续往前翻页
        if all_bills:
            min_id = min(int(b["id"]) for b in all_bills)
            params["record_id"] = min_id - 1
            
        try:
            bills = api.get_bill(**params)
        except Exception as e:
            print(f"  [!] 拉取第{page}页流水失败: {e}")
            break
            
        if not bills:
            break
        
        # 检查是否有新数据
        existing_ids = set(b["id"] for b in all_bills)
        new_bills = [b for b in bills if b["id"] not in existing_ids]
        
        if not new_bills:
            break
            
        all_bills.extend(new_bills)
        print(f"  第{page}页: 获取 {len(new_bills)} 条新流水, 累计 {len(all_bills)} 条")
        
        if len(bills) < 1000:
            break
            
        time.sleep(0.3)
    
    return all_bills


# ============================================================
# 3. 拉取当前持仓
# ============================================================
def fetch_positions():
    """获取当前所有持仓"""
    try:
        positions = api.get_position()
        return [p for p in positions if float(p.get("quantity", 0)) != 0]
    except Exception as e:
        print(f"  [!] 获取持仓失败: {e}")
        return []


# ============================================================
# 4. 拉取保证金账户
# ============================================================
def fetch_margin_account():
    """获取保证金账户信息"""
    try:
        raw = api._get("/eapi/v1/marginAccount", signed=True)
        return raw
    except Exception as e:
        print(f"  [!] 获取账户信息失败: {e}")
        return {}


# ============================================================
# 分析逻辑
# ============================================================
def analyze():
    print("=" * 70)
    print("  BTC 期权账户 — 交易盈亏综合分析")
    print(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)
    
    # --- 1. 成交历史 ---
    print("\n[1] 拉取成交历史...")
    trades = fetch_all_trades()
    print(f"  共获取 {len(trades)} 条成交记录")
    
    # --- 2. 资金流水 ---
    print("\n[2] 拉取资金流水 (Bill)...")
    bills = fetch_all_bills()
    print(f"  共获取 {len(bills)} 条流水记录")
    
    # --- 3. 当前持仓 ---
    print("\n[3] 拉取当前持仓...")
    positions = fetch_positions()
    print(f"  共 {len(positions)} 个活跃持仓")
    
    # --- 4. 账户信息 ---
    print("\n[4] 拉取保证金账户...")
    account_raw = fetch_margin_account()
    
    # 解析账户数据 (asset 是数组)
    acct = {}
    if account_raw:
        asset_list = account_raw.get("asset", [])
        if isinstance(asset_list, list) and asset_list:
            acct = asset_list[0]  # USDT 账户
        elif isinstance(asset_list, str):
            # 可能是序列化问题, 尝试解析
            import ast
            try:
                parsed = ast.literal_eval(asset_list)
                if isinstance(parsed, list) and parsed:
                    acct = parsed[0]
            except:
                pass
    
    margin_balance = float(acct.get("marginBalance", 0))
    equity = float(acct.get("equity", 0))
    unrealized_pnl_acct = float(acct.get("unrealizedPNL", 0))
    available = float(acct.get("available", 0))
    initial_margin = float(acct.get("initialMargin", 0))
    maint_margin = float(acct.get("maintMargin", 0))
    adjusted_equity = float(acct.get("adjustedEquity", 0))
    
    # ============================================================
    # 分析资金流水 (Bill) — 这是最可靠的数据源
    # ============================================================
    print("\n" + "=" * 70)
    print("  资金流水分析")
    print("=" * 70)
    
    # 按类型分组
    contract_total = 0.0
    fee_total = 0.0
    transfer_in_total = 0.0
    transfer_out_total = 0.0
    
    contract_bills = []
    fee_bills = []
    transfer_bills = []
    
    for b in bills:
        btype = b.get("type", "")
        amount = float(b.get("amount", 0))
        
        if btype == "CONTRACT":
            contract_total += amount
            contract_bills.append(b)
        elif btype == "FEE":
            fee_total += amount  # 负数
            fee_bills.append(b)
        elif btype == "TRANSFER":
            if amount > 0:
                transfer_in_total += amount
            else:
                transfer_out_total += amount  # 负数
            transfer_bills.append(b)
    
    net_transfer = transfer_in_total + transfer_out_total
    
    print(f"\n  转入资金:     +${transfer_in_total:>12,.2f}")
    print(f"  转出资金:      ${transfer_out_total:>12,.2f}")
    print(f"  净转入:         ${net_transfer:>12,.2f}")
    print(f"  ─────────────────────────────────")
    print(f"  交易权利金(CONTRACT): ${contract_total:>10,.2f} ({len(contract_bills)} 笔)")
    print(f"  手续费(FEE):          ${fee_total:>10,.2f} ({len(fee_bills)} 笔)")
    
    # ============================================================
    # 成交明细 — 按月分组
    # ============================================================
    print(f"\n  --- 交易流水明细 (CONTRACT) 按时间 ---")
    
    # 合并 CONTRACT 和对应的 FEE
    sorted_bills = sorted(bills, key=lambda x: int(x.get("createDate", 0)))
    
    # 按月汇总
    monthly = defaultdict(lambda: {"contract": 0, "fee": 0, "count": 0})
    
    for b in sorted_bills:
        btype = b.get("type", "")
        amount = float(b.get("amount", 0))
        ts = int(b.get("createDate", 0))
        month_key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        
        if btype == "CONTRACT":
            monthly[month_key]["contract"] += amount
            monthly[month_key]["count"] += 1
        elif btype == "FEE":
            monthly[month_key]["fee"] += amount
    
    print(f"\n  {'月份':10s} | {'权利金收支':>14s} | {'手续费':>12s} | {'净收入':>14s} | 笔数")
    print(f"  {'─'*70}")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        net = m["contract"] + m["fee"]
        print(f"  {month:10s} | ${m['contract']:>12,.2f} | ${m['fee']:>10,.2f} | ${net:>12,.2f} | {m['count']}")
    
    # ============================================================
    # 成交历史分析 (userTrades)
    # ============================================================
    if trades:
        print(f"\n" + "=" * 70)
        print(f"  成交记录分析 ({len(trades)} 笔)")
        print("=" * 70)
        
        total_sell_premium = 0
        total_buy_premium = 0
        total_trade_fee = 0
        
        for t in sorted(trades, key=lambda x: x.get("time", 0)):
            symbol = t.get("symbol", "")
            side = t.get("side", "")
            price = float(t.get("price", 0))
            qty = float(t.get("quantity", 0))
            fee = abs(float(t.get("fee", 0)))
            premium = price * qty
            time_str = ts_to_str(t.get("time", 0))
            
            total_trade_fee += fee
            if side == "SELL":
                total_sell_premium += premium
            else:
                total_buy_premium += premium
            
            direction = "卖" if side == "SELL" else "买"
            print(f"    {time_str} | {direction} | {symbol}")
            print(f"      ${price:.2f} × {qty} = ${premium:.2f} | fee: ${fee:.2f}")
        
        print(f"\n  卖出权利金: +${total_sell_premium:,.2f}")
        print(f"  买入权利金: -${total_buy_premium:,.2f}")
        print(f"  交易手续费: -${total_trade_fee:,.2f}")
    
    # ============================================================
    # 当前持仓 & 浮盈浮亏
    # ============================================================
    print("\n" + "=" * 70)
    print("  当前持仓 & 浮盈浮亏")
    print("=" * 70)
    
    total_unrealized = 0.0
    total_mark_value = 0.0
    total_entry_cost = 0.0  # 开仓时的成本/收入
    
    short_positions = []
    long_positions = []
    
    for p in positions:
        symbol = p.get("symbol", "")
        qty = float(p.get("quantity", 0))
        entry_price = float(p.get("entryPrice", 0))
        mark_price = float(p.get("markPrice", 0))
        unrealized = float(p.get("unrealizedPNL", 0))
        mark_value = float(p.get("markValue", 0))
        
        total_unrealized += unrealized
        total_mark_value += mark_value
        
        entry_cost = entry_price * abs(qty)
        current_value = mark_price * abs(qty)
        
        if qty < 0:
            # Short: 收到权利金(entry_cost), 现在要花 current_value 平仓
            # PnL = entry_cost - current_value
            implied_pnl = entry_cost - current_value
            short_positions.append((symbol, qty, entry_price, mark_price, entry_cost, current_value, implied_pnl, unrealized))
        else:
            # Long: 花了 entry_cost, 现在值 current_value
            # PnL = current_value - entry_cost
            implied_pnl = current_value - entry_cost
            long_positions.append((symbol, qty, entry_price, mark_price, entry_cost, current_value, implied_pnl, unrealized))
    
    print(f"\n  === Short 持仓 (卖 Put) ===")
    for sym, qty, ep, mp, ec, cv, pnl, unr in short_positions:
        pnl_pct = (pnl / ec * 100) if ec > 0 else 0
        print(f"\n  {sym}")
        print(f"    数量: {qty}, 入场价: ${ep:,.2f}, 标记价: ${mp:,.2f}")
        print(f"    收到权利金: ${ec:,.2f}")
        print(f"    当前平仓成本: ${cv:,.2f}")
        print(f"    浮盈/亏: ${pnl:,.2f} ({pnl_pct:+.1f}%)")
        print(f"    [API unrealizedPNL: ${unr:,.2f}]")
    
    print(f"\n  === Long 持仓 (买 Put 对冲) ===")
    for sym, qty, ep, mp, ec, cv, pnl, unr in long_positions:
        pnl_pct = (pnl / ec * 100) if ec > 0 else 0
        print(f"\n  {sym}")
        print(f"    数量: {qty}, 入场价: ${ep:,.2f}, 标记价: ${mp:,.2f}")
        print(f"    支付权利金: ${ec:,.2f}")
        print(f"    当前市值: ${cv:,.2f}")
        print(f"    浮盈/亏: ${pnl:,.2f} ({pnl_pct:+.1f}%)")
        print(f"    [API unrealizedPNL: ${unr:,.2f}]")
    
    short_pnl = sum(x[6] for x in short_positions)
    long_pnl = sum(x[6] for x in long_positions)
    
    print(f"\n  ─────────────────────────────────")
    print(f"  Short 持仓浮盈/亏合计: ${short_pnl:>10,.2f}")
    print(f"  Long 持仓浮盈/亏合计:  ${long_pnl:>10,.2f}")
    print(f"  持仓浮盈/亏总计:       ${short_pnl + long_pnl:>10,.2f}")
    print(f"  [API unrealizedPNL:    ${total_unrealized:>10,.2f}]")
    
    # ============================================================
    # 账户概览
    # ============================================================
    print("\n" + "=" * 70)
    print("  账户概览")
    print("=" * 70)
    
    print(f"    保证金余额 (marginBalance): ${margin_balance:>12,.2f}")
    print(f"    权益 (equity):              ${equity:>12,.2f}")
    print(f"    可用保证金 (available):      ${available:>12,.2f}")
    print(f"    初始保证金 (initialMargin):  ${initial_margin:>12,.2f}")
    print(f"    维持保证金 (maintMargin):    ${maint_margin:>12,.2f}")
    print(f"    未实现盈亏 (unrealizedPNL):  ${unrealized_pnl_acct:>12,.2f}")
    print(f"    调整后权益 (adjustedEquity): ${adjusted_equity:>12,.2f}")
    
    # ============================================================
    # === 最终盈亏汇总 ===
    # ============================================================
    print("\n" + "=" * 70)
    print("  ★★★ 最终盈亏汇总 ★★★")
    print("=" * 70)
    
    # 已实现盈亏 = CONTRACT 流水汇总 (这是已成交的权利金净收入, 包含开仓和平仓)
    # 注意: CONTRACT 里包含了:
    #   - 开仓卖 Put → 正数 (收权利金)
    #   - 开仓买 Put → 负数 (付权利金)
    #   - 平仓买回 Short → 负数 (付权利金)
    #   - 平仓卖出 Long → 正数 (收权利金)
    #   - 行权/到期结算 → 金额
    
    print(f"\n  1. 已实现权利金收支 (CONTRACT):  ${contract_total:>10,.2f}")
    print(f"  2. 累计手续费 (FEE):             ${fee_total:>10,.2f}")
    print(f"  ─────────────────────────────────────")
    realized_pnl = contract_total + fee_total
    print(f"  3. 已实现净盈亏 (1+2):           ${realized_pnl:>10,.2f}")
    
    print(f"\n  4. 当前持仓未实现盈亏:")
    print(f"     API unrealizedPNL:            ${unrealized_pnl_acct:>10,.2f}")
    
    print(f"\n  ─────────────────────────────────────")
    total_pnl = realized_pnl + unrealized_pnl_acct
    print(f"  5. 总盈亏 (3+4):                 ${total_pnl:>10,.2f}")
    
    # 交叉验证: equity = marginBalance + unrealizedPNL 的一部分
    # 更好的验证: equity = net_transfer + total_pnl
    print(f"\n  ─── 交叉验证 ───")
    print(f"  净转入资金:        ${net_transfer:>12,.2f}")
    print(f"  当前权益(equity):  ${equity:>12,.2f}")
    equity_implied_pnl = equity - net_transfer
    print(f"  权益法盈亏:        ${equity_implied_pnl:>12,.2f}")
    print(f"  流水法盈亏:        ${total_pnl:>12,.2f}")
    diff = equity_implied_pnl - total_pnl
    print(f"  差异:              ${diff:>12,.2f}")
    if abs(diff) > 1:
        print(f"  (差异说明: 可能存在流水未覆盖的期间或行权/到期结算)")
    
    # ============================================================
    # === 最终结论 ===
    # ============================================================
    print(f"\n" + "=" * 70)
    
    # 用权益法作为最终结论 (最准确, 包含所有因素)
    final_pnl = equity_implied_pnl
    pnl_pct = (final_pnl / net_transfer * 100) if net_transfer > 0 else 0
    
    if final_pnl >= 0:
        verdict = "盈利"
    else:
        verdict = "亏损"
    
    print(f"  ★ 结论: 期权交易整体 【{verdict}】")
    print(f"")
    print(f"    净转入资金:       ${net_transfer:>12,.2f}")
    print(f"    当前账户权益:     ${equity:>12,.2f}")
    print(f"    ─────────────────────────────")
    print(f"    总盈亏:           ${final_pnl:>12,.2f}")
    print(f"    收益率:           {pnl_pct:>+11.2f}%")
    print(f"")
    print(f"    其中:")
    print(f"      已实现权利金:   ${contract_total:>12,.2f}")
    print(f"      手续费消耗:     ${fee_total:>12,.2f}")
    print(f"      未实现浮亏:     ${unrealized_pnl_acct:>12,.2f}")
    print(f"")
    
    # 计算时间跨度
    if bills:
        all_dates = [int(b.get("createDate", 0)) for b in bills]
        first_date = datetime.fromtimestamp(min(all_dates) / 1000, tz=timezone.utc)
        last_date = datetime.fromtimestamp(max(all_dates) / 1000, tz=timezone.utc)
        days = (last_date - first_date).days
        print(f"    交易时间跨度: {first_date.strftime('%Y-%m-%d')} ~ {last_date.strftime('%Y-%m-%d')} ({days} 天)")
        if days > 0:
            daily_pnl = final_pnl / days
            annual_pnl_pct = pnl_pct / days * 365
            print(f"    日均盈亏:     ${daily_pnl:>10,.2f}/天")
            print(f"    年化收益率:   {annual_pnl_pct:>+9.2f}%")
    
    print(f"\n" + "=" * 70)
    
    # 保存原始数据
    with open("pnl_raw_data.json", "w") as f:
        json.dump({
            "trades": trades,
            "bills": bills,
            "positions": [dict(p) for p in positions],
            "account_raw": account_raw,
            "analysis": {
                "net_transfer": net_transfer,
                "equity": equity,
                "contract_total": contract_total,
                "fee_total": fee_total,
                "unrealized_pnl": unrealized_pnl_acct,
                "total_pnl": final_pnl,
            }
        }, f, indent=2, default=str)
    print(f"  原始数据已保存到 pnl_raw_data.json")


if __name__ == "__main__":
    analyze()
