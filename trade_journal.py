#!/usr/bin/env python3
"""
交易记录与绩效追踪系统

记录完整的信号→入场→持有→平仓生命周期：
  1. 信号记录: 每个推送的 SIGNAL/STRONG 级别信号
  2. 持仓追踪: 检测新开仓/仓位变化/平仓事件
  3. 绩效统计: 胜率、累计P&L、平均持有天数、最大回撤

数据持久化到 trade_journal.json, 重启不丢失。
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("trade_journal")

JOURNAL_FILE = "/root/projects/trade_journal.json"


# ============================================================
#  数据结构
# ============================================================
@dataclass
class SignalRecord:
    """信号记录"""
    symbol: str
    signal_level: str       # WATCH / SIGNAL / STRONG
    score: float
    bid: float
    annual_return: float
    safety_pct: float
    iv_premium: float
    spot: float
    timestamp: float        # unix timestamp
    source: str = "v1"      # v1 or v2

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SignalRecord":
        return SignalRecord(**{k: d[k] for k in SignalRecord.__dataclass_fields__ if k in d})


@dataclass
class TradeRecord:
    """交易记录 (一次完整的开仓→平仓周期)"""
    trade_id: str               # 唯一ID: symbol_open_ts
    symbol: str
    direction: str              # SHORT or LONG
    qty: float
    entry_price: float
    entry_time: float           # unix timestamp
    entry_spot: float           # 开仓时BTC价格

    # 持仓中更新
    peak_profit_pct: float = 0      # 历史最高盈利%
    trough_profit_pct: float = 0    # 历史最低盈利%
    last_mark: float = 0
    last_update: float = 0

    # 平仓时填写
    exit_price: float = 0
    exit_time: float = 0
    exit_spot: float = 0
    exit_reason: str = ""       # expired / manual_close / stop_loss / roll
    realized_pnl: float = 0
    realized_pnl_pct: float = 0
    holding_days: float = 0

    status: str = "OPEN"       # OPEN / CLOSED / EXPIRED

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "TradeRecord":
        fields = {k: d.get(k, TradeRecord.__dataclass_fields__[k].default
                           if TradeRecord.__dataclass_fields__[k].default is not field
                           else 0)
                  for k in TradeRecord.__dataclass_fields__}
        return TradeRecord(**fields)


# ============================================================
#  交易日志
# ============================================================
class TradeJournal:
    """交易记录管理器"""

    def __init__(self, filepath: str = JOURNAL_FILE):
        self.filepath = filepath
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.error(f"加载交易日志失败: {e}")
        return {
            "signals": [],          # 信号记录列表
            "trades": [],           # 交易记录列表
            "known_positions": {},  # {symbol: {qty, entry, last_seen}} 用于检测变化
            "known_orders": {},     # {order_id: {symbol, side, price, qty, status}} 用于检测成交
            "stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0,
                "best_trade_pnl": 0,
                "worst_trade_pnl": 0,
            },
        }

    def save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
        except Exception as e:
            log.error(f"保存交易日志失败: {e}")

    # --- 信号记录 ---
    def record_signal(self, signal: SignalRecord):
        """记录一个推送的信号"""
        self.data["signals"].append(signal.to_dict())
        # 保留最近 500 条信号
        if len(self.data["signals"]) > 500:
            self.data["signals"] = self.data["signals"][-500:]
        self.save()
        log.info(f"记录信号: {signal.symbol} {signal.signal_level} score={signal.score:.0f}")

    # --- 持仓变化检测 ---
    def check_position_changes(self, current_positions: list, spot: float) -> list[dict]:
        """
        对比当前持仓和已知持仓，检测新开仓/平仓/数量变化

        Returns: 变化事件列表 [{type: NEW/CLOSED/CHANGED, ...}]
        """
        events = []
        known = self.data.get("known_positions", {})
        now = time.time()

        # 当前持仓集合
        current_map = {}
        for p in current_positions:
            qty = float(p.get("quantity", 0))
            if qty == 0:
                continue
            sym = p.get("symbol", "")
            if "-P" not in sym:
                continue
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            current_map[sym] = {
                "qty": qty,
                "entry": entry,
                "mark": mark,
            }

        # 检测新开仓
        for sym, info in current_map.items():
            if sym not in known:
                events.append({
                    "type": "NEW",
                    "symbol": sym,
                    "qty": info["qty"],
                    "entry": info["entry"],
                    "spot": spot,
                })
                # 创建交易记录
                direction = "SHORT" if info["qty"] < 0 else "LONG"
                trade = TradeRecord(
                    trade_id=f"{sym}_{int(now)}",
                    symbol=sym,
                    direction=direction,
                    qty=info["qty"],
                    entry_price=info["entry"],
                    entry_time=now,
                    entry_spot=spot,
                    last_mark=info["mark"],
                    last_update=now,
                )
                self.data["trades"].append(trade.to_dict())
                log.info(f"新开仓记录: {sym} {direction} {info['qty']} @ ${info['entry']:,.0f}")

            elif abs(info["qty"]) != abs(known[sym]["qty"]):
                # 数量变化 (加仓或减仓)
                events.append({
                    "type": "CHANGED",
                    "symbol": sym,
                    "old_qty": known[sym]["qty"],
                    "new_qty": info["qty"],
                    "entry": info["entry"],
                })
                log.info(f"仓位变化: {sym} {known[sym]['qty']} -> {info['qty']}")

        # 检测平仓 (已知持仓消失)
        for sym, old_info in known.items():
            if sym not in current_map:
                events.append({
                    "type": "CLOSED",
                    "symbol": sym,
                    "qty": old_info["qty"],
                    "entry": old_info["entry"],
                    "spot": spot,
                })
                # 更新交易记录
                self._close_trade(sym, old_info, spot, now)

        # 更新已知持仓
        self.data["known_positions"] = current_map

        # 更新活跃交易的 mark/peak/trough
        for trade_d in self.data["trades"]:
            if trade_d["status"] != "OPEN":
                continue
            sym = trade_d["symbol"]
            if sym in current_map:
                mark = current_map[sym]["mark"]
                entry = trade_d["entry_price"]
                qty = trade_d["qty"]
                if qty < 0:  # Short
                    pnl_pct = (entry - mark) / entry * 100 if entry > 0 else 0
                else:  # Long
                    pnl_pct = (mark - entry) / entry * 100 if entry > 0 else 0
                trade_d["last_mark"] = mark
                trade_d["last_update"] = now
                trade_d["peak_profit_pct"] = max(trade_d.get("peak_profit_pct", 0), pnl_pct)
                trade_d["trough_profit_pct"] = min(trade_d.get("trough_profit_pct", 0), pnl_pct)

        if events:
            self.save()

        return events

    def _close_trade(self, symbol: str, old_info: dict, spot: float, now: float):
        """关闭一笔交易记录"""
        for trade_d in reversed(self.data["trades"]):
            if trade_d["symbol"] == symbol and trade_d["status"] == "OPEN":
                entry = trade_d["entry_price"]
                qty = trade_d["qty"]
                mark = old_info.get("last_mark", old_info.get("mark", 0))

                if qty < 0:  # Short Put
                    pnl_per = entry - mark
                else:  # Long Put
                    pnl_per = mark - entry

                pnl = pnl_per * abs(qty)
                pnl_pct = pnl_per / entry * 100 if entry > 0 else 0

                trade_d["status"] = "CLOSED"
                trade_d["exit_price"] = mark
                trade_d["exit_time"] = now
                trade_d["exit_spot"] = spot
                trade_d["realized_pnl"] = round(pnl, 2)
                trade_d["realized_pnl_pct"] = round(pnl_pct, 1)
                trade_d["holding_days"] = round((now - trade_d["entry_time"]) / 86400, 1)

                # 判断原因
                parts = symbol.split("-")
                if len(parts) >= 2:
                    try:
                        exp_str = parts[1]
                        exp_date = datetime.strptime(exp_str, "%y%m%d")
                        if (exp_date.date() - datetime.now().date()).days <= 0:
                            trade_d["exit_reason"] = "expired"
                        else:
                            trade_d["exit_reason"] = "manual_close"
                    except ValueError:
                        trade_d["exit_reason"] = "manual_close"

                # 更新统计
                stats = self.data["stats"]
                stats["total_trades"] += 1
                stats["total_pnl"] = round(stats["total_pnl"] + pnl, 2)
                if pnl >= 0:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                stats["best_trade_pnl"] = max(stats["best_trade_pnl"], pnl)
                stats["worst_trade_pnl"] = min(stats["worst_trade_pnl"], pnl)

                log.info(f"交易结束: {symbol} PnL ${pnl:+,.0f} ({pnl_pct:+.1f}%) "
                         f"持有 {trade_d['holding_days']:.1f}天")
                break

    # --- 挂单成交检测 ---
    def check_order_fills(self, current_orders: list, api=None) -> list[dict]:
        """
        检测挂单状态变化 (成交/取消)

        通过 API 查询消失挂单的真实状态, 避免误报。

        Args:
            current_orders: 当前挂单列表 (来自 API)
            api: BinanceOptionsAPI 实例 (用于查询订单真实状态)

        Returns: 事件列表 [{type: FILLED/CANCELLED/EXPIRED, ...}]
        """
        events = []
        known = self.data.get("known_orders", {})
        now = time.time()

        # 当前挂单集合 (更新状态)
        current_order_ids = set()
        for o in current_orders:
            oid = str(o.get("orderId", ""))
            if not oid:
                continue
            current_order_ids.add(oid)

            if oid not in known:
                # 新挂单: 记录但不推送
                known[oid] = {
                    "symbol": o.get("symbol", ""),
                    "side": o.get("side", ""),
                    "price": float(o.get("price", 0)),
                    "qty": float(o.get("quantity", 0)),
                    "status": o.get("status", ""),
                    "first_seen": now,
                }
            else:
                # 已知挂单: 更新状态
                known[oid]["status"] = o.get("status", known[oid].get("status", ""))

        # 检测消失的挂单
        disappeared = [oid for oid in list(known.keys()) if oid not in current_order_ids]
        for oid in disappeared:
            info = known[oid]
            sym = info.get("symbol", "")

            # 通过 API 查询订单真实状态
            real_status = ""
            if api and sym:
                try:
                    order_detail = api.get_order(sym, order_id=int(oid))
                    real_status = order_detail.get("status", "")
                except Exception as e:
                    log.warning(f"查询订单 {oid} 状态失败: {e}")

            # 根据真实状态判断
            if real_status == "FILLED":
                event_type = "FILLED"
            elif real_status in ("CANCELLED", "REJECTED", "EXPIRED"):
                event_type = real_status
            elif real_status == "PARTIALLY_FILLED":
                # 部分成交: 记录但等待完全成交
                info["status"] = real_status
                continue
            elif real_status == "":
                # 查询失败: 不推送, 等下次再查
                log.warning(f"无法确认订单 {oid} ({sym}) 状态, 跳过")
                continue
            else:
                # 未知状态: 不推送
                log.warning(f"订单 {oid} 未知状态: {real_status}")
                del known[oid]
                continue

            events.append({
                "type": event_type,
                "order_id": oid,
                "symbol": sym,
                "side": info["side"],
                "price": info["price"],
                "qty": info["qty"],
            })

            if event_type == "FILLED":
                log.info(f"挂单成交: {sym} {info['side']} "
                         f"{info['qty']}张 @ ${info['price']:,.0f}")
            else:
                log.info(f"挂单{event_type}: {sym}")
            del known[oid]

        self.data["known_orders"] = known
        if events:
            self.save()

        return events

    # --- 绩效统计 ---
    def get_performance(self) -> dict:
        """计算完整的绩效统计"""
        stats = self.data.get("stats", {})
        trades = self.data.get("trades", [])

        closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
        open_trades = [t for t in trades if t.get("status") == "OPEN"]

        total = stats.get("total_trades", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)

        win_rate = wins / total * 100 if total > 0 else 0

        # 平均盈亏
        if closed_trades:
            avg_pnl = sum(t.get("realized_pnl", 0) for t in closed_trades) / len(closed_trades)
            avg_pnl_pct = sum(t.get("realized_pnl_pct", 0) for t in closed_trades) / len(closed_trades)
            avg_hold_days = sum(t.get("holding_days", 0) for t in closed_trades) / len(closed_trades)

            win_trades = [t for t in closed_trades if t.get("realized_pnl", 0) >= 0]
            loss_trades = [t for t in closed_trades if t.get("realized_pnl", 0) < 0]
            avg_win = sum(t["realized_pnl"] for t in win_trades) / len(win_trades) if win_trades else 0
            avg_loss = sum(t["realized_pnl"] for t in loss_trades) / len(loss_trades) if loss_trades else 0
            profit_factor = abs(avg_win * len(win_trades)) / abs(avg_loss * len(loss_trades)) if loss_trades and avg_loss != 0 else float('inf')
        else:
            avg_pnl = avg_pnl_pct = avg_hold_days = avg_win = avg_loss = 0
            profit_factor = 0

        # 当前持仓浮盈
        unrealized_pnl = 0
        for t in open_trades:
            entry = t.get("entry_price", 0)
            mark = t.get("last_mark", 0)
            qty = t.get("qty", 0)
            if qty < 0:
                unrealized_pnl += (entry - mark) * abs(qty)
            else:
                unrealized_pnl += (mark - entry) * abs(qty)

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(stats.get("total_pnl", 0), 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "avg_pnl_pct": round(avg_pnl_pct, 1),
            "avg_hold_days": round(avg_hold_days, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "best_trade": round(stats.get("best_trade_pnl", 0), 2),
            "worst_trade": round(stats.get("worst_trade_pnl", 0), 2),
            "open_positions": len(open_trades),
            "total_signals": len(self.data.get("signals", [])),
        }

    def format_performance_tg(self) -> str:
        """格式化绩效报告 (TG HTML)"""
        p = self.get_performance()
        lines = ["📊 <b>策略绩效报告</b>\n"]

        lines.append("<b>已平仓交易</b>")
        lines.append(f"  总交易: {p['total_trades']}笔  |  胜率: {p['win_rate']:.0f}%")
        lines.append(f"  盈: {p['wins']}  亏: {p['losses']}")
        lines.append(f"  累计已实现 PnL: <b>${p['total_pnl']:+,.0f}</b>")
        lines.append(f"  当前浮盈: ${p['unrealized_pnl']:+,.0f}")
        lines.append(f"  平均每笔: ${p['avg_pnl']:+,.0f} ({p['avg_pnl_pct']:+.1f}%)")
        lines.append(f"  平均持有: {p['avg_hold_days']:.0f}天")
        lines.append("")

        if p['total_trades'] > 0:
            lines.append("<b>收益分布</b>")
            lines.append(f"  平均盈利: ${p['avg_win']:+,.0f}")
            lines.append(f"  平均亏损: ${p['avg_loss']:+,.0f}")
            lines.append(f"  盈亏比: {p['profit_factor']:.1f}x")
            lines.append(f"  最佳: ${p['best_trade']:+,.0f}")
            lines.append(f"  最差: ${p['worst_trade']:+,.0f}")
            lines.append("")

        lines.append(f"活跃持仓: {p['open_positions']}个  |  历史信号: {p['total_signals']}条")

        return "\n".join(lines)

    # --- 信号召回率 ---
    def signal_hit_rate(self) -> dict:
        """
        计算信号质量: 推送的信号中有多少实际被入场了

        通过比对 signal.symbol 和 trade.symbol + 时间窗口来关联
        """
        signals = self.data.get("signals", [])
        trades = self.data.get("trades", [])

        if not signals:
            return {"total_signals": 0, "acted_on": 0, "hit_rate": 0}

        trade_syms_with_time = [(t["symbol"], t["entry_time"]) for t in trades]
        acted = 0

        for sig in signals:
            sig_sym = sig.get("symbol", "")
            sig_time = sig.get("timestamp", 0)
            # 信号发出后 24 小时内是否有该合约的入场
            for t_sym, t_time in trade_syms_with_time:
                if t_sym == sig_sym and 0 <= (t_time - sig_time) <= 86400:
                    acted += 1
                    break

        return {
            "total_signals": len(signals),
            "acted_on": acted,
            "hit_rate": round(acted / len(signals) * 100, 1) if signals else 0,
        }
