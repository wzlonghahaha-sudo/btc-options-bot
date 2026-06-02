#!/usr/bin/env python3
"""
BTC 深度 OTM Put 卖出信号 Telegram Bot

功能:
  1. 定时扫描币安期权市场, 寻找高赔率卖 Put 机会
  2. 新信号 / 信号升级时推送 Telegram 通知
  3. 持仓盈亏变化 & 风险预警推送
  4. 定时发送市场概览 (每4小时)
  5. 支持 TG 命令交互: /status /scan /positions /iv /help

扫描间隔设计:
  - 常规: 每 3 分钟扫描一次 (期权流动性不高, 不需要太频繁)
  - BTC 价格波动 >2% 时: 自动缩短到每 1 分钟
  - 市场概览: 每 4 小时推送一次
  - 持仓检查: 每次扫描都会检查

推送去重:
  - 同一个合约的同一信号等级, 1小时内只推送一次
  - 信号升级 (WATCH->SIGNAL->STRONG) 立即推送
  - 持仓预警: WARNING 30分钟去重, DANGER 5分钟去重

用法:
  python3 tg_bot_monitor.py
"""

import os
import sys
import time
import json
import logging
import signal as sig
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from dotenv import load_dotenv

# 加载本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_options import BinanceOptionsAPI
from otm_put_monitor import (
    Config as MonitorConfig,
    IVTracker,
    fetch_market_data,
    calc_iv_surface,
    scan_opportunities,
    monitor_positions,
    monitor_open_orders,
    calc_odds_score,
)
from risk_monitor import RiskEngine, RiskAlert, format_risk_alerts, format_risk_summary
from profit_optimizer import analyze_position_optimization, format_profit_report, VolatilityAnalyzer
from opportunity_scanner import (
    scan_all_opportunities, assess_account_risk,
    format_opportunities_tg, format_signal_push, ScanConfig,
)
from ai_analyst import SmartAnalyst
from trade_journal import TradeJournal, SignalRecord
from state_persistence import StatePersistence
from hedge_advisor import HedgeAdvisor, RiskMode

load_dotenv()

# ============================================================
#  配置
# ============================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# 额外推送群组 (逗号分隔多个)
TG_GROUP_IDS = [g.strip() for g in os.getenv("TG_GROUP_IDS", "").split(",") if g.strip()]

# 扫描间隔
SCAN_INTERVAL_NORMAL = 180       # 常规: 3分钟
SCAN_INTERVAL_VOLATILE = 60      # 波动时: 1分钟
BTC_VOLATILITY_THRESHOLD = 2.0   # BTC波动超过2%算波动

# 市场概览推送间隔
OVERVIEW_INTERVAL = 4 * 3600     # 4小时

# 推送去重 (秒)
SIGNAL_COOLDOWN = 3600           # 同一信号1小时去重
SIGNAL_UPGRADE_COOLDOWN = 60     # 信号升级60秒去重
POS_WARN_COOLDOWN = 1800         # 持仓WARNING 30分钟
POS_DANGER_COOLDOWN = 300        # 持仓DANGER 5分钟

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg_bot")


# ============================================================
#  Telegram 推送
# ============================================================
class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def send(self, text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
        """发送消息，自动分片处理超长消息（TG限制4096字符）"""
        MAX_LEN = 4000  # 留一点余量
        if len(text) <= MAX_LEN:
            return self._send_one(text, parse_mode, silent)

        # 按换行符分片，尽量不截断段落
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > MAX_LEN:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)

        ok = True
        for i, chunk in enumerate(chunks):
            if not self._send_one(chunk, parse_mode, silent):
                ok = False
        return ok

    def _send_one(self, text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
        """发送单条消息"""
        try:
            resp = self.session.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_notification": silent,
                },
                timeout=10,
            )
            if not resp.json().get("ok"):
                log.error(f"TG send failed: {resp.text}")
                return False
            return True
        except Exception as e:
            log.error(f"TG send error: {e}")
            return False

    def send_photo(self, photo_path: str, caption: str = "", parse_mode: str = "HTML") -> bool:
        """发送图片"""
        try:
            with open(photo_path, "rb") as f:
                resp = self.session.post(
                    f"{self.api_base}/sendPhoto",
                    data={
                        "chat_id": self.chat_id,
                        "caption": caption,
                        "parse_mode": parse_mode,
                    },
                    files={"photo": f},
                    timeout=15,
                )
            if not resp.json().get("ok"):
                log.error(f"TG photo failed: {resp.text}")
                return False
            return True
        except Exception as e:
            log.error(f"TG photo error: {e}")
            return False

    def get_updates(self, offset: int = 0, timeout: int = 1) -> list:
        """获取用户消息 (用于命令交互)"""
        try:
            resp = self.session.get(
                f"{self.api_base}/getUpdates",
                params={"offset": offset, "timeout": timeout},
                timeout=timeout + 5,
            )
            data = resp.json()
            return data.get("result", [])
        except Exception:
            return []

    def _send_to(self, chat_id: str, text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
        """发送消息到指定 chat_id"""
        try:
            resp = self.session.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_notification": silent,
                },
                timeout=10,
            )
            if not resp.json().get("ok"):
                log.error(f"TG send to {chat_id} failed: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            log.error(f"TG send to {chat_id} error: {e}")
            return False

    def _send_photo_to(self, chat_id: str, photo_path: str, caption: str = "", parse_mode: str = "HTML") -> bool:
        """发送图片到指定 chat_id"""
        try:
            with open(photo_path, "rb") as f:
                resp = self.session.post(
                    f"{self.api_base}/sendPhoto",
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": parse_mode,
                    },
                    files={"photo": f},
                    timeout=15,
                )
            if not resp.json().get("ok"):
                log.error(f"TG photo to {chat_id} failed: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            log.error(f"TG photo to {chat_id} error: {e}")
            return False

    def broadcast(self, text: str, parse_mode: str = "HTML", silent: bool = False):
        """广播消息: 发到主 chat + 所有群组"""
        # 发到主 chat (自己)
        self.send(text, parse_mode, silent)
        # 发到所有群组
        for group_id in TG_GROUP_IDS:
            # 超长分片
            MAX_LEN = 4000
            if len(text) <= MAX_LEN:
                self._send_to(group_id, text, parse_mode, silent)
            else:
                chunks = []
                current = ""
                for line in text.split("\n"):
                    if len(current) + len(line) + 1 > MAX_LEN:
                        if current:
                            chunks.append(current)
                        current = line
                    else:
                        current = current + "\n" + line if current else line
                if current:
                    chunks.append(current)
                for chunk in chunks:
                    self._send_to(group_id, chunk, parse_mode, silent)

    def broadcast_photo(self, photo_path: str, caption: str = "", parse_mode: str = "HTML"):
        """广播图片: 发到主 chat + 所有群组"""
        self.send_photo(photo_path, caption, parse_mode)
        for group_id in TG_GROUP_IDS:
            self._send_photo_to(group_id, photo_path, caption, parse_mode)


# ============================================================
#  消息格式化
# ============================================================
class MessageFormatter:

    @staticmethod
    def signal_alert(results: list, spot: float) -> str:
        """格式化信号推送消息"""
        strong = [r for r in results if r["signal"] == "STRONG"]
        signals = [r for r in results if r["signal"] == "SIGNAL"]

        lines = []

        if strong:
            lines.append("🔴🔴🔴 <b>强信号 - 极佳赔率!</b>")
            lines.append("")
            for r in strong:
                lines.append(MessageFormatter._format_one_signal(r, spot))

        if signals:
            lines.append("🟡 <b>入场信号 - 赔率不错</b>")
            lines.append("")
            for r in signals:
                lines.append(MessageFormatter._format_one_signal(r, spot))

        return "\n".join(lines)

    @staticmethod
    def _format_one_signal(r: dict, spot: float) -> str:
        return (
            f"<b>{r['symbol']}</b>\n"
            f"  赔率: <b>{r['odds_score']:.1f}</b>  |  "
            f"Bid: <b>${r['bid']:,.0f}</b>\n"
            f"  行权: ${r['strike']:,.0f}  |  "
            f"OTM: {r['otm_pct']:.1f}%  |  "
            f"安全垫: {r['safety_pct']:.1f}%\n"
            f"  年化: {r['annual_return']:.1f}%  |  "
            f"IV溢价: {r['iv_premium']:+.1f}%  |  "
            f"Delta: {r['delta']:.5f}\n"
            f"  到期: {r['expiry']} ({r['dte']:.0f}天)  |  "
            f"日衰: {r['theta_daily_pct']:.2f}%\n"
        )

    @staticmethod
    def position_alert(pos: dict) -> str:
        """格式化持仓预警"""
        icon = "🔴" if pos.get("alert") == "DANGER" else "⚠️"
        return (
            f"{icon} <b>持仓预警</b>\n\n"
            f"<b>{pos['symbol']}</b>\n"
            f"  数量: {pos['qty']}  |  入场: ${pos['entry']:,.0f}\n"
            f"  当前: ${pos['mark']:,.0f}  |  "
            f"盈亏: <b>${pos['pnl']:+,.0f}</b> ({pos['pnl_pct']:+.0f}%)\n"
            f"  距行权: {pos['dist_to_strike']:.1f}%\n"
            f"  {pos.get('msg', '')}"
        )

    @staticmethod
    def orders_msg(order_alerts: list) -> str:
        """格式化挂单信息"""
        if not order_alerts:
            return "📝 暂无挂单"

        lines = ["📝 <b>当前挂单</b>\n"]
        for o in order_alerts:
            if o["type"] == "ERROR":
                lines.append(f"  ❌ {o['msg']}")
                continue

            side_icon = "🔻" if o["side"] == "SELL" else "🔺"
            side_cn = "卖出" if o["side"] == "SELL" else "买入"

            # 距离成交的描述
            if o["gap_pct"] <= 5:
                dist_icon = "🟢"  # 接近成交
                dist_desc = "接近成交!"
            elif o["gap_pct"] <= 15:
                dist_icon = "🟡"
                dist_desc = "有一定距离"
            else:
                dist_icon = "⚪"
                dist_desc = "距离较远"

            lines.append(
                f"{side_icon} <b>{o['symbol']}</b>\n"
                f"  {side_cn} {o['qty']}张 @ ${o['price']:,.0f}\n"
                f"  当前: Bid ${o['bid']:,.0f} / Ask ${o['ask']:,.0f} / Mark ${o['mark']:,.0f}\n"
                f"  {dist_icon} 差距: ${o['gap']:,.0f} ({o['gap_pct']:.1f}%) — {dist_desc}\n"
            )

        return "\n".join(lines)

    @staticmethod
    def market_overview(spot: float, iv_surface: dict, results: list,
                        pos_alerts: list, iv_tracker: IVTracker,
                        order_alerts: list = None,
                        risk_alerts: list = None,
                        account_risk=None,
                        btc_24h_change: float = None,
                        liq_line: str = "") -> str:
        """格式化市场概览 (含 IV 警告、信号展开、持仓详情、保证金)"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        mean_iv = iv_surface["global"]["mean"]
        iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
        iv_trend = iv_tracker.get_iv_trend()

        n_strong = len([r for r in results if r["signal"] == "STRONG"])
        n_signal = len([r for r in results if r["signal"] == "SIGNAL"])
        n_watch = len([r for r in results if r["signal"] == "WATCH"])

        # --- P2-6: BTC 24h 涨跌幅 ---
        btc_line = f"BTC: <b>${spot:,.2f}</b>"
        if btc_24h_change is not None:
            arrow = "▲" if btc_24h_change >= 0 else "▼"
            btc_line += f"  {arrow} {btc_24h_change:+.1f}% (24h)"

        # --- P0-1: IV Percentile 行 + 警告 ---
        iv_line = (
            f"Put IV均值: <b>{mean_iv:.3f}</b>  |  "
            f"Percentile: {iv_pctl:.0f}%  |  {iv_trend}"
        )
        if iv_pctl < 20:
            iv_line += "\n❄️ IV历史低位，不建议开新仓"
        elif iv_pctl < 40:
            iv_line += "\n📉 IV偏低，谨慎开仓"
        elif iv_pctl > 60:
            iv_line += "\n🔥 IV偏高，策略有利"

        lines = [
            f"📊 <b>市场概览</b>  {now}",
            "",
            btc_line,
            iv_line,
            "",
            f"信号: 🔴 强信号 {n_strong}  |  🟡 信号 {n_signal}  |  👀 关注 {n_watch}",
        ]

        # --- P0-2: 信号展开显示 ---
        strong_results = [r for r in results if r["signal"] == "STRONG"]
        signal_results = [r for r in results if r["signal"] == "SIGNAL"]
        watch_results = [r for r in results if r["signal"] == "WATCH"]

        if strong_results:
            lines.append(f"🔴 强信号 ({len(strong_results)}):")
            for r in strong_results[:5]:
                lines.append(
                    f"  • {r['symbol']}  评分 {r['odds_score']:.0f}/100\n"
                    f"    Bid ${r['bid']:,.0f}  年化 {r['annual_return']:.0f}%  "
                    f"安全垫 {r['safety_pct']:.0f}%  DTE {r['dte']:.0f}天"
                )
        if signal_results:
            lines.append(f"🟡 信号 ({len(signal_results)}):")
            for r in signal_results[:5]:
                lines.append(
                    f"  • {r['symbol']}  评分 {r['odds_score']:.0f}/100\n"
                    f"    Bid ${r['bid']:,.0f}  年化 {r['annual_return']:.0f}%  "
                    f"安全垫 {r['safety_pct']:.0f}%  DTE {r['dte']:.0f}天"
                )
        if watch_results:
            lines.append(f"👀 关注 ({len(watch_results)}):")
            for r in watch_results[:3]:
                iv_prem_str = f"IV溢价 {r['iv_premium']:+.0f}%" if 'iv_premium' in r else ""
                lines.append(
                    f"  • {r['symbol']}  评分 {r['odds_score']:.0f}/100\n"
                    f"    原因: {iv_prem_str}，距行权 {r.get('safety_pct', 0):.0f}%，"
                    f"DTE {r['dte']:.0f}天"
                )

        # --- 风控 + 强平价 + 保证金 ---
        lines.append("")
        lines.append(format_risk_summary(risk_alerts or [], spot))
        if liq_line:
            lines.append(liq_line)

        if account_risk and account_risk.total_balance > 0:
            usage_pct = account_risk.margin_usage_pct
            if usage_pct > 50:
                lines.append(
                    f"⚠️ 保证金: 已用 {usage_pct:.0f}%，注意风险  "
                    f"(${account_risk.used_margin:,.0f} / ${account_risk.total_balance:,.0f})"
                )
            else:
                lines.append(
                    f"🏦 保证金: 已用 ${account_risk.used_margin:,.0f} / "
                    f"可用 ${account_risk.available_margin:,.0f}  ({usage_pct:.1f}%)"
                )

        # IV 曲面摘要 (只显示近几个到期日)
        lines.append("")
        lines.append("<b>IV 曲面:</b>")
        exps = sorted(iv_surface["by_exp"].keys())[:6]
        for exp in exps:
            s = iv_surface["by_exp"][exp]
            lines.append(f"  {exp}: 中位 {s['median']:.3f}  均值 {s['mean']:.3f}  "
                         f"[{s['min']:.3f} - {s['max']:.3f}]")

        # --- P0-3: 持仓详情 (DTE + Theta + 原始权利金) ---
        real_positions = [p for p in pos_alerts if p.get("type") != "ERROR"] if pos_alerts else []
        if real_positions:
            lines.append("")
            lines.append("<b>持仓:</b>")
            total_pnl = 0
            total_theta = 0
            pos_count = 0

            for p in real_positions:
                icon = {"OK": "✅", "WARNING": "⚠️", "DANGER": "🔴", "WATCH": "👀"}.get(p.get("alert"), "")
                direction = p.get("direction", "Short")
                dir_tag = "📌Long" if direction == "Long" else ""
                sym_line = f"  {icon} {p['symbol']}"
                if dir_tag:
                    sym_line += f"  ({dir_tag})"
                lines.append(sym_line)
                lines.append(
                    f"     盈亏: ${p['pnl']:+,.0f} ({p['pnl_pct']:+.0f}%)  |  "
                    f"距行权: {p['dist_to_strike']:.1f}%"
                )
                # DTE + Theta + 原始权利金
                dte = p.get("dte", 0)
                theta = p.get("theta", 0)
                premium = p.get("premium_collected", 0)
                detail_parts = []
                if dte > 0:
                    detail_parts.append(f"DTE: {dte}天")
                if theta > 0:
                    detail_parts.append(f"Theta: ${theta:.1f}/天(进账)")
                elif theta < 0:
                    detail_parts.append(f"Theta: ${theta:.1f}/天(损耗)")
                if premium > 0:
                    detail_parts.append(f"权利金: ${premium:,.0f}")
                if detail_parts:
                    lines.append(f"     {'  |  '.join(detail_parts)}")

                total_pnl += p.get("pnl", 0)
                total_theta += theta
                pos_count += 1

            # --- P1-4: 持仓汇总行 ---
            if pos_count > 0:
                lines.append("")
                lines.append(
                    f"持仓汇总: 总浮盈 <b>${total_pnl:+,.0f}</b>  |  "
                    f"Theta合计 ${total_theta:+.1f}/天  |  持仓数 {pos_count}"
                )

        # 挂单摘要
        if order_alerts:
            real_orders = [o for o in order_alerts if o.get("type") == "ORDER"]
            if real_orders:
                lines.append("")
                lines.append("<b>挂单:</b>")
                for o in real_orders:
                    side_cn = "卖" if o["side"] == "SELL" else "买"
                    if o["gap_pct"] <= 5:
                        dist_icon = "🟢"
                    elif o["gap_pct"] <= 15:
                        dist_icon = "🟡"
                    else:
                        dist_icon = "⚪"
                    lines.append(
                        f"  {dist_icon} {o['symbol']}  "
                        f"{side_cn} {o['qty']}张 @ ${o['price']:,.0f}  "
                        f"差距 ${o['gap']:,.0f} ({o['gap_pct']:.1f}%)"
                    )

        # Top 机会
        top = [r for r in results if r["signal"] in ("STRONG", "SIGNAL")][:5]
        if top:
            lines.append("")
            lines.append("<b>Top 机会:</b>")
            for r in top:
                icon = "🔴" if r["signal"] == "STRONG" else "🟡"
                lines.append(
                    f"  {icon} {r['symbol']}  赔率 {r['odds_score']:.0f}  "
                    f"Bid ${r['bid']:,.0f}  年化 {r['annual_return']:.0f}%  "
                    f"安全垫 {r['safety_pct']:.0f}%"
                )

        return "\n".join(lines)

    @staticmethod
    def status_msg(spot: float, scan_count: int, uptime_str: str,
                   last_scan_time: float, interval: int) -> str:
        """格式化 /status 响应"""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        return (
            f"🤖 <b>Bot 状态</b>  {now}\n\n"
            f"BTC: ${spot:,.2f}\n"
            f"扫描次数: {scan_count}\n"
            f"运行时间: {uptime_str}\n"
            f"上次扫描: {last_scan_time:.1f}s\n"
            f"扫描间隔: {interval}s"
        )

    @staticmethod
    def help_msg() -> str:
        return (
            "🤖 <b>BTC OTM Put 监控 Bot</b>\n\n"
            "<b>可用命令:</b>\n"
            "/status - 查看 Bot 运行状态\n"
            "/scan - 立即执行一次扫描\n"
            "/positions - 查看当前持仓\n"
            "/orders - 查看挂单 & 成交差距\n"
            "/profit - 💰 止盈/Roll/HV分析\n"
            "/risk - 🛡️ 风控报告\n"
            "/iv - 查看 IV 曲面\n"
            "/top - 🔍 全部机会 (三档分层+风控)\n"
            "/top80 - 🔥 仅看80+高分机会\n"
            "/top70 - ⭐ 看70+以上机会\n"
            "/overview - 发送完整市场概览\n"
            "/ai - 🤖 AI 策略分析 (Claude)\n"
            "/hedge - 🛡️ 对冲方案计算\n"
            "/perf - 📊 策略绩效统计\n"
            "/journal - 📝 最近交易记录\n"
            "/strategy - 📖 策略说明 (小白版)\n"
            "/rules - 📏 具体入场/风控规则\n"
            "/help - 显示此帮助\n\n"
            "<b>自动推送:</b>\n"
            "• 80+高分机会: 即时详情推送 (1h去重)\n"
            "• 55-79普通机会: 轻量提示 (4h去重)\n"
            "• 持仓预警: 即时推送\n"
            "• 市场概览 + AI分析: 每4小时\n"
            "• 扫描间隔: 常规3分钟, 波动时1分钟"
        )

    @staticmethod
    def strategy_msg() -> str:
        return (
            "📖 <b>策略说明 (小白版)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "<b>一句话总结:</b>\n"
            "我们卖「BTC 崩盘保险」给别人，收保险费赚钱。\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🎯 在做什么？</b>\n\n"
            "我们卖的是 BTC 的「看跌期权」(Put)。\n\n"
            "打个比方：\n"
            "BTC 现在 $78,000，我们卖一份合约说：\n"
            "「如果 BTC 跌到 $50,000 以下，我赔你钱」\n\n"
            "买家付给我们 $300 保险费（权利金）。\n"
            "如果到期时 BTC 没跌到 $50,000 → 我们白赚 $300 ✅\n"
            "如果真跌到了 → 我们要赔钱 ❌\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🤔 为什么能赚钱？</b>\n\n"
            "1️⃣ <b>概率站在我们这边</b>\n"
            "我们选的行权价离现价很远 (跌25-50%才亏)\n"
            "BTC 在1-2个月内跌这么多的概率很低\n\n"
            "2️⃣ <b>时间是我们的朋友</b>\n"
            "每过一天，期权就贬值一点 (Theta衰减)\n"
            "我们什么都不用做，保险费自动到手\n\n"
            "3️⃣ <b>我们只在「贵」的时候卖</b>\n"
            "市场恐慌时保险费会暴涨 (IV飙升)\n"
            "平时值 $100 的保险，恐慌时能卖 $500\n"
            "我们专门等这个时候出手 → 赔率极高\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⏰ 什么时候出手？</b>\n\n"
            "不急！等三个条件同时满足：\n\n"
            "🟢 <b>安全垫够大</b>\n"
            "BTC 至少要跌 25%+ 我们才亏钱\n\n"
            "🟢 <b>保险费够贵</b> (IV溢价高)\n"
            "别人恐慌时愿意付更多钱买保险\n"
            "IV 溢价至少比正常贵 15% 以上\n\n"
            "🟢 <b>赔率划算</b>\n"
            "综合评分达到 75 分以上才推送信号\n"
            "达到 88 分是「强信号」— 极罕见但极好\n\n"

            "大部分时间 Bot 都是安静的 🤫\n"
            "安静 = 没有好机会 = 不出手 = 正确！\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚠️ 风险在哪？</b>\n\n"
            "最大风险：BTC 突然暴跌超过预期\n"
            "比如：行权价 $50,000，BTC 跌到 $30,000\n"
            "我们每张合约亏 $20,000\n\n"
            "所以风控非常重要：\n"
            "• 浮亏达到 1x 权利金 → ⚠️ 警告\n"
            "• 浮亏达到 2x 权利金 → 🔴 建议平仓\n"
            "• BTC 距行权价 &lt;12% → 🔴 紧急\n"
            "• Bot 会自动推送这些预警\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 Bot 在做什么？</b>\n\n"
            "每 3 分钟扫描一次所有 BTC Put 期权：\n"
            "• 计算每个合约的「赔率」\n"
            "• 监控 IV 水平 (保险费贵不贵)\n"
            "• 盯着你的持仓盈亏\n"
            "• 发现好机会 → 推送给你\n"
            "• 持仓有风险 → 推送预警\n\n"
            "输入 /rules 查看具体的入场和风控数字"
        )

    @staticmethod
    def rules_msg() -> str:
        return (
            "📏 <b>入场规则 & 风控标准</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "<b>🔍 合约筛选 (硬门槛)</b>\n"
            "不满足任一条直接淘汰：\n\n"
            "• Delta: |δ| ≤ 0.05 (极深度虚值)\n"
            "• 虚值程度: OTM ≥ 25%\n"
            "  → BTC至少跌25%才到行权价\n"
            "• 到期天数: 14 ~ 90 天\n"
            "  → 太短gamma大，太长占资金\n"
            "• 最低Bid: ≥ $50\n"
            "  → 权利金太薄不值得\n"
            "• Spread: ≤ 10%\n"
            "  → 必须能以合理价格成交\n"
            "• 安全垫: ≥ 25%\n"
            "  → BTC跌到盈亏平衡点的距离\n"
            "• IV溢价: ≥ 15%\n"
            "  → 期权定价必须偏贵\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 评分模型 (Sinclair风险溢价框架)</b>\n\n"
            "核心理念: Edge来自variance premium\n"
            "(IV&gt;HV), 不是theta衰减\n\n"
            "五个维度加权打分：\n"
            "• 安全垫 (30%权重) — 活下来\n"
            "  距行权价的安全距离\n"
            "• 风险溢价 (30%权重) — Edge来源\n"
            "  IV溢价(60%) + IV/HV比值(40%)\n"
            "  期权越贵、IV越高于HV, 分越高\n"
            "• 年化收益 (20%权重) — 回报合理性\n"
            "  10%起步, 30%不错, 60%+满分\n"
            "• 流动性 (10%权重) — 能成交\n"
            "  Spread+成交量\n"
            "• 时间结构 (10%权重) — 资金效率\n"
            "  14-45天甜蜜区 + theta效率\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🚦 信号等级</b>\n\n"
            "• ⚪ WAIT (&lt;55分) → 不推送, 继续等\n"
            "• 👀 WATCH (55-79分) → 轻量提示 (4h一次)\n"
            "  └ /top 查看详情\n"
            "• 🔥 STRONG (80分+) → <b>完整详情推送</b> (1h去重)\n"
            "• 🔥🔥 ELITE (90分+) → <b>极强信号推送</b>\n\n"
            "大部分时间都是安静的\n"
            "80+机会可能几天出现一次\n"
            "90+极强信号非常罕见\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🛡️ 持仓风控</b>\n\n"
            "• 浮亏 ≥ 1x 权利金 → ⚠️ 警告推送\n"
            "• 浮亏 ≥ 2x 权利金 → 🔴 建议止损\n"
            "• 距行权价 &lt; 18% → ⚠️ 警告\n"
            "• 距行权价 &lt; 12% → 🔴 紧急平仓\n"
            "• BTC日跌 &gt; 2% → 自动加快扫描到1分钟\n"
            "• BTC日跌 &gt; 4% → 🔴 紧急通知"
        )


# ============================================================
#  去重管理器
# ============================================================
class CooldownManager:
    """管理推送去重"""

    def __init__(self):
        self.signal_sent = {}       # {symbol: {"signal": str, "time": float}}
        self.pos_alert_sent = {}    # {symbol: {"alert": str, "time": float}}

    def should_send_signal(self, symbol: str, signal: str) -> bool:
        """判断是否应该推送信号"""
        now = time.time()
        prev = self.signal_sent.get(symbol)

        if prev is None:
            # 新合约, 直接推
            return True

        prev_signal = prev["signal"]
        prev_time = prev["time"]

        # 信号升级: 立即推 (但有短暂冷却)
        signal_rank = {"WAIT": 0, "WATCH": 1, "SIGNAL": 2, "STRONG": 3}
        if signal_rank.get(signal, 0) > signal_rank.get(prev_signal, 0):
            return (now - prev_time) > SIGNAL_UPGRADE_COOLDOWN

        # 同级别信号: 冷却期内不推
        return (now - prev_time) > SIGNAL_COOLDOWN

    def record_signal(self, symbol: str, signal: str):
        self.signal_sent[symbol] = {"signal": signal, "time": time.time()}

    def should_send_pos_alert(self, symbol: str, alert: str) -> bool:
        """判断是否应该推送持仓预警"""
        now = time.time()
        prev = self.pos_alert_sent.get(symbol)

        if prev is None:
            return True

        cooldown = POS_DANGER_COOLDOWN if alert == "DANGER" else POS_WARN_COOLDOWN
        return (now - prev["time"]) > cooldown

    def record_pos_alert(self, symbol: str, alert: str):
        self.pos_alert_sent[symbol] = {"alert": alert, "time": time.time()}

    def cleanup(self):
        """清理过期记录"""
        now = time.time()
        cutoff = now - 7200  # 2小时前的记录清理
        self.signal_sent = {
            k: v for k, v in self.signal_sent.items() if v["time"] > cutoff
        }
        self.pos_alert_sent = {
            k: v for k, v in self.pos_alert_sent.items() if v["time"] > cutoff
        }


# ============================================================
#  主监控服务
# ============================================================
class MonitorService:
    def __init__(self):
        self.api = BinanceOptionsAPI()
        self.tg = TelegramBot(TG_BOT_TOKEN, TG_CHAT_ID)
        self.iv_tracker = IVTracker()
        self.cooldown = CooldownManager()
        self.risk_engine = RiskEngine()
        self.vol_analyzer = VolatilityAnalyzer()
        self.fmt = MessageFormatter()
        self.ai_analyst = SmartAnalyst()
        self.journal = TradeJournal()
        self.state = StatePersistence()
        self.hedge_advisor = HedgeAdvisor()
        self.risk_mode = RiskMode()

        self.scan_count = 0
        self.start_time = time.time()
        self.last_spot = 0
        self.last_scan_time = 0
        self.last_overview_time = 0
        self.current_interval = SCAN_INTERVAL_NORMAL
        self.last_result = None
        self.last_pos_list = []  # 压力测试用的持仓列表

        self.running = True
        self.update_offset = 0

        # P1-4: 恢复持久化状态
        self._restore_state()

    def _restore_state(self):
        """从持久化文件恢复运行时状态"""
        try:
            # 恢复冷却状态
            cooldowns = self.state.load_cooldowns()
            if cooldowns:
                self.cooldown.signal_sent = cooldowns
                log.info(f"恢复冷却状态: {len(cooldowns)} 条")

            # 恢复价格追踪
            pt = self.state.load_price_tracker()
            if pt.get("prices"):
                self.risk_engine.price_tracker.prices = pt["prices"]
                log.info(f"恢复价格数据: {len(pt['prices'])} 个点")
            if pt.get("daily_open") is not None:
                self.risk_engine.price_tracker.daily_open = pt["daily_open"]
                self.risk_engine.price_tracker.daily_open_date = pt.get("daily_open_date")
                log.info(f"恢复日开盘价: ${pt['daily_open']:,.0f}")

            # 恢复风控冷却
            risk_cd = self.state.load_risk_cooldowns()
            if risk_cd:
                self.risk_engine.last_alerts = risk_cd
                log.info(f"恢复风控冷却: {len(risk_cd)} 条")

            # 恢复 TG update offset
            offset = self.state.load_update_offset()
            if offset > 0:
                self.update_offset = offset
                log.info(f"恢复 TG offset: {offset}")

        except Exception as e:
            log.warning(f"状态恢复失败: {e}")

    def _save_state(self):
        """保存运行时状态到文件"""
        try:
            self.state.save_cooldowns(self.cooldown.signal_sent)
            pt = self.risk_engine.price_tracker
            self.state.save_price_tracker(pt.prices, pt.daily_open, pt.daily_open_date)
            self.state.save_risk_cooldowns(self.risk_engine.last_alerts)
            self.state.save_update_offset(self.update_offset)
            self.state.save()
        except Exception as e:
            log.error(f"状态保存失败: {e}")

    def uptime_str(self) -> str:
        elapsed = time.time() - self.start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        return f"{hours}h {minutes}m"

    def _generate_iv_chart(self, result: dict, btc_24h_change: float = None) -> str:
        """生成 IV 报告图表, 返回图片路径 (失败返回空字符串)"""
        try:
            from iv_chart import generate_report_chart
            positions = result.get("pos_alerts", [])
            account_risk = result.get("account_risk")
            chart_path = generate_report_chart(
                data=result["data"],
                iv_surface=result["iv_surface"],
                spot=result["data"]["spot"],
                iv_tracker=self.iv_tracker,
                positions=positions,
                account_risk=account_risk,
                btc_24h_change=btc_24h_change,
            )
            return chart_path
        except Exception as e:
            log.error(f"IV 报告图表生成失败: {e}", exc_info=True)
            return ""

    # --- 扫描 ---
    def do_scan(self) -> dict:
        """执行一次完整扫描"""
        t0 = time.time()

        data = fetch_market_data(self.api)
        iv_surface = calc_iv_surface(data)

        # 记录 IV
        self.iv_tracker.record_snapshot({
            "mean_iv": iv_surface["global"]["mean"],
            "median_iv": iv_surface["global"]["median"],
            "timestamp": data["timestamp"],
        })
        self.iv_tracker.save()

        # P1-5: V1 扫描降频 (每5次才跑一次, V2 已覆盖)
        if self.scan_count % 5 == 0 or self.scan_count <= 1:
            results = scan_opportunities(data, iv_surface, self.iv_tracker)
        else:
            results = self.last_result.get("results", []) if self.last_result else []

        pos_alerts = monitor_positions(self.api, data)
        order_alerts = monitor_open_orders(self.api, data)

        # v2 机会扫描 (每次都做, 需要 account_risk 给风控用)
        v2_opportunities = []
        account_risk = None
        hv_20 = 0
        try:
            account_risk = assess_account_risk(self.api, data)
            hv_20 = self.vol_analyzer.calc_hv(20)
            v2_opportunities = scan_all_opportunities(data, iv_surface, account_risk, hv_20)
        except Exception as e:
            log.error(f"v2机会扫描失败: {e}")

        # 风控检查 (含强平价格估算, 需要 account_balance)
        # 优先用币安 marginAccount 的真实 marginBalance
        account_balance = 0
        try:
            ma = self.api._get("/eapi/v1/marginAccount", signed=True)
            account_balance = float(ma["asset"][0]["marginBalance"])
        except Exception:
            if account_risk:
                account_balance = account_risk.available_margin + account_risk.used_margin

        try:
            positions = self.api.get_position()
            active_positions = [p for p in positions if float(p.get("quantity", 0)) != 0]
            risk_data = {
                "spot": data["spot"],
                "marks": data["marks"],
                "positions": active_positions,
                "account_balance": account_balance,
            }
            risk_alerts = self.risk_engine.check_all(risk_data)
        except Exception as e:
            log.error(f"风控检查失败: {e}")
            risk_alerts = []
            active_positions = []

        # 对冲顾问: 追踪强平价 + 检查对冲仓位到期
        try:
            pos_list = self.risk_engine._build_position_list(
                active_positions, data["marks"], data["spot"])
            self.last_pos_list = pos_list

            liq = self.hedge_advisor.update_liquidation(
                pos_list, data["spot"], account_balance)

            # 风控模式切换
            old_mode = self.risk_mode.mode
            self.risk_mode.update(liq["liq_drop_pct"])
            if self.risk_mode.mode_changed():
                self.tg.broadcast(
                    f"🔔 <b>风控模式切换</b>\n"
                    f"{old_mode} → <b>{self.risk_mode.mode_icon}</b>\n"
                    f"强平距离 {abs(liq['liq_drop_pct']):.0f}%",
                )

            # 对冲仓位到期提醒
            expiry_alerts = self.hedge_advisor.check_hedge_expiry(pos_list)
            for ea in expiry_alerts:
                self.tg.broadcast(ea["msg"])

            # 对冲止盈/退出检查 (每 5 次扫描检查一次)
            if self.scan_count % 5 == 0 or self.scan_count <= 1:
                exit_alerts = self.hedge_advisor.check_hedge_exit(
                    pos_list, data["spot"], account_balance, data["marks"])
                for ea in exit_alerts:
                    self.tg.broadcast(ea["msg"])
                    log.info(f"对冲止盈提醒: {ea['short_sym']} ({ea['reason']})")

        except Exception as e:
            log.error(f"对冲顾问失败: {e}")

        # P1-6: 止盈分析频率自适应
        profit_interval = 3 if self.current_interval <= SCAN_INTERVAL_VOLATILE else 10
        profit_analysis = None
        if self.scan_count % profit_interval == 0 or self.scan_count <= 1:
            try:
                iv_trend = self.iv_tracker.get_iv_trend()
                profit_analysis = analyze_position_optimization(
                    self.api, data, results, iv_trend,
                )
            except Exception as e:
                log.error(f"收益优化分析失败: {e}")

        scan_time = time.time() - t0
        self.scan_count += 1
        self.last_scan_time = scan_time

        # 动态调整扫描间隔
        spot = data["spot"]
        if self.last_spot > 0:
            price_change = abs(spot - self.last_spot) / self.last_spot * 100
            if price_change > BTC_VOLATILITY_THRESHOLD:
                self.current_interval = SCAN_INTERVAL_VOLATILE
                log.info(f"BTC 波动 {price_change:.1f}%, 切换到高频扫描 ({SCAN_INTERVAL_VOLATILE}s)")
            else:
                self.current_interval = SCAN_INTERVAL_NORMAL
        self.last_spot = spot

        result = {
            "data": data,
            "iv_surface": iv_surface,
            "results": results,
            "pos_alerts": pos_alerts,
            "order_alerts": order_alerts,
            "risk_alerts": risk_alerts,
            "profit_analysis": profit_analysis,
            "v2_opportunities": v2_opportunities,
            "account_risk": account_risk,
            "account_balance": account_balance,
            "hv_20": hv_20,
            "scan_time": scan_time,
        }
        self.last_result = result

        # P2-11: 保存 IV 曲面快照
        try:
            self.state.save_iv_surface_snapshot(iv_surface, data["timestamp"])
        except Exception:
            pass

        # P0-1: 交易日志 — 检测持仓变化
        try:
            positions_raw = self.api.get_position()
            events = self.journal.check_position_changes(positions_raw, data["spot"])
            for ev in events:
                if ev["type"] == "NEW":
                    direction = "卖出" if ev["qty"] < 0 else "买入"
                    short_sym = ev["symbol"].split("BTC-")[-1]
                    self.tg.broadcast(
                        f"📝 <b>新开仓记录</b>\n\n"
                        f"{short_sym} {direction} {abs(ev['qty'])}张\n"
                        f"入场价 ${ev['entry']:,.0f}  |  BTC ${ev['spot']:,.0f}",
                        silent=True,
                    )
                elif ev["type"] == "CLOSED":
                    short_sym = ev["symbol"].split("BTC-")[-1]
                    self.tg.broadcast(
                        f"📝 <b>平仓记录</b>\n\n"
                        f"{short_sym} 已平仓\n"
                        f"BTC ${ev['spot']:,.0f}",
                        silent=True,
                    )
        except Exception as e:
            log.error(f"交易日志更新失败: {e}")

        # P2-10: 挂单成交检测
        try:
            current_orders = self.api.get_open_orders()
            order_events = self.journal.check_order_fills(current_orders)
            for ev in order_events:
                if ev["type"] == "FILLED":
                    side_cn = "卖出" if ev["side"] == "SELL" else "买入"
                    short_sym = ev["symbol"].split("BTC-")[-1]
                    self.tg.broadcast(
                        f"🔔 <b>挂单成交!</b>\n\n"
                        f"{short_sym} {side_cn} {ev['qty']}张\n"
                        f"成交价 ${ev['price']:,.0f}",
                    )
        except Exception as e:
            log.error(f"挂单检测失败: {e}")

        # P1-4: 定期保存状态
        self._save_state()

        return result

    # --- 推送决策 ---
    def process_signals(self, results: list, spot: float):
        """处理扫描结果, 决定是否推送"""
        to_send = []

        for r in results:
            if r["signal"] not in ("SIGNAL", "STRONG"):
                continue
            if self.cooldown.should_send_signal(r["symbol"], r["signal"]):
                to_send.append(r)
                self.cooldown.record_signal(r["symbol"], r["signal"])

        if to_send:
            msg = self.fmt.signal_alert(to_send, spot)
            self.tg.broadcast(msg)
            log.info(f"推送 {len(to_send)} 个信号")

    def process_pos_alerts(self, pos_alerts: list):
        """处理持仓预警"""
        for p in pos_alerts:
            alert = p.get("alert", "OK")
            if alert not in ("WARNING", "DANGER"):
                continue
            sym = p.get("symbol", "")
            if self.cooldown.should_send_pos_alert(sym, alert):
                msg = self.fmt.position_alert(p)
                self.tg.broadcast(msg, silent=(alert == "WARNING"))
                self.cooldown.record_pos_alert(sym, alert)
                log.info(f"推送持仓预警: {sym} [{alert}]")

    def process_risk_alerts(self, risk_alerts: list):
        """处理风控告警推送"""
        pushable = [a for a in risk_alerts
                    if a.level in ("WARNING", "DANGER", "CRITICAL")
                    and self.risk_engine.should_push(a)]

        if not pushable:
            return

        msg = format_risk_alerts(pushable)
        # CRITICAL 不静默
        silent = all(a.level == "WARNING" for a in pushable)
        self.tg.broadcast(msg, silent=silent)
        log.info(f"推送风控告警: {len(pushable)} 项 "
                 f"(C:{len([a for a in pushable if a.level=='CRITICAL'])} "
                 f"D:{len([a for a in pushable if a.level=='DANGER'])} "
                 f"W:{len([a for a in pushable if a.level=='WARNING'])})")

    def process_v2_signals(self, result: dict):
        """处理 v2 机会扫描的信号推送

        推送策略:
        - 78+分: 主动推送完整详情
        - <78分: 不推送, 用户通过 /top 自行查看
        - 去重: 1小时内同一合约不重复推送
        - 危机模式下抑制: 不推送新开仓信号
        """
        # 危机模式: 不推送新开仓信号
        if self.risk_mode.should_suppress_signals:
            return

        opps = result.get("v2_opportunities", [])
        account = result.get("account_risk")
        if not opps or not account:
            return

        now = time.time()

        # 只关注78+机会
        top_opps = [o for o in opps if o.score >= ScanConfig.SCORE_PUSH and o.can_open]
        if not top_opps:
            return

        # 去重: 1小时内同一合约不重复推送
        new_top = []
        for o in top_opps:
            key = f"v2top:{o.symbol}"
            last = self.cooldown.signal_sent.get(key, {}).get("time", 0)
            if now - last > 3600:
                new_top.append(o)
                self.cooldown.signal_sent[key] = {"signal": o.tier, "time": now}

        if not new_top:
            return

        # P0-1: 记录推送的信号到交易日志
        for o in new_top:
            try:
                sig = SignalRecord(
                    symbol=o.symbol,
                    signal_level="STRONG" if o.score >= 90 else "SIGNAL",
                    score=o.score,
                    bid=o.bid,
                    annual_return=o.annual_return,
                    safety_pct=o.safety_pct,
                    iv_premium=o.iv_premium,
                    spot=self.last_spot,
                    timestamp=time.time(),
                    source="v2",
                )
                self.journal.record_signal(sig)
            except Exception:
                pass

        msg = format_signal_push(opps, account)
        if msg:
            self.tg.broadcast(msg)
            log.info(f"推送 v2 信号: {len(new_top)} 个 (78+分)")

    def process_profit_advice(self, profit_analysis: dict):
        """处理止盈/Roll建议推送"""
        if not profit_analysis:
            return

        for p in profit_analysis.get("positions", []):
            tp = p.get("take_profit")
            if not tp:
                continue

            # 只推送 MEDIUM/HIGH urgency 的建议
            if tp.urgency not in ("MEDIUM", "HIGH"):
                continue

            # 去重: 同一合约同一建议 4 小时内只推一次
            key = f"profit:{p['symbol']}:{tp.action}"
            now = time.time()
            last = self.cooldown.signal_sent.get(key, {}).get("time", 0)
            if now - last < 14400:  # 4小时
                continue
            self.cooldown.signal_sent[key] = {"signal": tp.action, "time": now}

            action_cn = {"HOLD": "继续持有", "CLOSE": "建议平仓", "CLOSE_AND_ROLL": "平仓+Roll"}.get(tp.action, "")
            urgency_icon = {"HIGH": "🔴", "MEDIUM": "🟡"}.get(tp.urgency, "")

            msg_lines = [
                f"💰 <b>止盈建议</b>",
                "",
                f"<b>{p['symbol']}</b>",
                f"盈利: <b>${p['pnl']:+,.0f} ({p['profit_pct']:+.0f}%)</b>",
                "",
                f"{urgency_icon} <b>建议: {action_cn}</b>",
                f"{tp.reason}",
            ]
            for line in tp.detail.split("\n"):
                msg_lines.append(line)

            if tp.roll_target:
                msg_lines.append(f"\nRoll 目标: <b>{tp.roll_target}</b>")

            self.tg.broadcast("\n".join(msg_lines), silent=(tp.urgency != "HIGH"))
            log.info(f"推送止盈建议: {p['symbol']} → {tp.action} ({tp.urgency})")

    def send_overview(self, result: dict):
        """发送市场概览 (文字 + IV图表 + AI分析)"""
        # P2-6: 获取 BTC 24h 涨跌幅
        btc_24h_change = None
        try:
            # 优先从 price_tracker 获取 (需运行超过一段时间)
            change = self.risk_engine.price_tracker.get_change_pct(86400)
            if change != 0:
                btc_24h_change = change
            else:
                # 备选: 从 Binance 合约 API 获取 24h ticker
                resp = requests.get(
                    "https://fapi.binance.com/fapi/v1/ticker/24hr",
                    params={"symbol": "BTCUSDT"}, timeout=5
                )
                if resp.ok:
                    btc_24h_change = float(resp.json().get("priceChangePercent", 0))
        except Exception:
            pass

        # 强平价一行 (如果有数据)
        liq_line = ""
        try:
            if self.last_pos_list and self.hedge_advisor.last_liq_price > 0:
                liq_data = {
                    "liq_price": self.hedge_advisor.last_liq_price,
                    "liq_drop_pct": self.hedge_advisor.last_liq_drop,
                    "cushion": getattr(self.risk_engine, "_last_liq", {}).get("cushion", 0),
                }
                liq_line = self.hedge_advisor.format_liq_line(liq_data, result["data"]["spot"])
        except Exception:
            pass

        msg = self.fmt.market_overview(
            result["data"]["spot"],
            result["iv_surface"],
            result["results"],
            result["pos_alerts"],
            self.iv_tracker,
            order_alerts=result.get("order_alerts", []),
            risk_alerts=result.get("risk_alerts", []),
            account_risk=result.get("account_risk"),
            btc_24h_change=btc_24h_change,
            liq_line=liq_line,
        )
        self.tg.broadcast(msg, silent=True)
        self.last_overview_time = time.time()
        log.info("推送市场概览")

        # IV 图表 + AI 策略分析 (图文合一推送)
        chart_path = self._generate_iv_chart(result, btc_24h_change)

        ai_report = ""
        if self.ai_analyst.is_available:
            try:
                ai_report = self.ai_analyst.analyze(result, self.iv_tracker)
            except Exception as e:
                log.error(f"AI 分析失败: {e}")

        if chart_path:
            # 图文合一: 图表作为 Photo, AI 报告作为 caption (TG caption 限1024字符)
            caption = ""
            if ai_report:
                # caption 限制 1024 字符, 超出则截断
                if len(ai_report) <= 1024:
                    caption = ai_report
                else:
                    # 图表带简短 caption, AI 报告单独发
                    caption = "📊 <b>IV Dashboard</b> — 详细分析见下方"
            else:
                caption = "📊 <b>IV Dashboard</b>"

            self.tg.broadcast_photo(chart_path, caption=caption)
            log.info("推送 IV 图表")

            # 如果 AI 报告太长没放进 caption, 单独发
            if ai_report and len(ai_report) > 1024:
                self.tg.broadcast(ai_report, silent=True)
                log.info("推送 AI 策略分析 (单独)")
        elif ai_report:
            # 图表生成失败, 纯文字发 AI 报告
            self.tg.broadcast(ai_report, silent=True)
            log.info("推送 AI 策略分析 (无图表)")

    # --- TG 命令处理 ---
    def handle_commands(self):
        """处理 TG 用户命令"""
        updates = self.tg.get_updates(offset=self.update_offset, timeout=0)

        for update in updates:
            self.update_offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # 只响应授权用户
            if chat_id != TG_CHAT_ID:
                continue

            # P1-7: 支持群组命令 @botname 后缀 (如 /scan@BN_options_bot)
            if "@" in text:
                text = text.split("@")[0]

            if text == "/help" or text == "/start":
                self.tg.send(self.fmt.help_msg())

            elif text == "/strategy":
                self.tg.send(self.fmt.strategy_msg())

            elif text == "/rules":
                self.tg.send(self.fmt.rules_msg())

            elif text == "/status":
                spot = self.last_spot or 0
                self.tg.send(self.fmt.status_msg(
                    spot, self.scan_count, self.uptime_str(),
                    self.last_scan_time, self.current_interval
                ))

            elif text == "/scan":
                self.tg.send("🔄 正在扫描...")
                result = self.do_scan()
                self.send_overview(result)

            elif text == "/positions":
                if self.last_result:
                    lines = []
                    # 持仓
                    pos = self.last_result["pos_alerts"]
                    total_pnl = 0
                    total_theta = 0
                    pos_count = 0
                    if pos:
                        lines.append("📋 <b>当前持仓</b>\n")
                        for p in pos:
                            if p["type"] == "ERROR":
                                lines.append(f"  ❌ {p['msg']}")
                                continue
                            icon = {"OK": "✅", "WARNING": "⚠️", "DANGER": "🔴", "WATCH": "👀"}.get(p.get("alert"), "")
                            dte = p.get("dte", 0)
                            theta = p.get("theta", 0)
                            premium = p.get("premium_collected", 0)
                            direction = p.get("direction", "Short")
                            dir_tag = " (Long)" if direction == "Long" else ""
                            theta_label = "进账" if theta >= 0 else "损耗"
                            lines.append(
                                f"{icon} <b>{p['symbol']}{dir_tag}</b>\n"
                                f"  数量: {p['qty']}  入场: ${p['entry']:,.0f}  "
                                f"当前: ${p['mark']:,.0f}\n"
                                f"  盈亏: <b>${p['pnl']:+,.0f}</b> ({p['pnl_pct']:+.0f}%)  "
                                f"距行权: {p['dist_to_strike']:.1f}%\n"
                                f"  DTE: {dte}天  |  Theta: ${theta:.1f}/天({theta_label})"
                                + (f"  |  权利金: ${premium:,.0f}" if premium > 0 else "")
                                + "\n"
                            )
                            total_pnl += p.get("pnl", 0)
                            total_theta += theta
                            pos_count += 1
                        if pos_count > 0:
                            lines.append(
                                f"<b>汇总:</b> 总浮盈 ${total_pnl:+,.0f}  |  "
                                f"Theta合计 ${total_theta:+.1f}/天  |  持仓数 {pos_count}\n"
                            )
                    else:
                        lines.append("📋 暂无持仓\n")

                    # 挂单
                    ords = self.last_result.get("order_alerts", [])
                    real_ords = [o for o in ords if o.get("type") == "ORDER"]
                    if real_ords:
                        lines.append("📝 <b>当前挂单</b>\n")
                        for o in real_ords:
                            side_cn = "卖出" if o["side"] == "SELL" else "买入"
                            if o["gap_pct"] <= 5:
                                dist_icon = "🟢"
                            elif o["gap_pct"] <= 15:
                                dist_icon = "🟡"
                            else:
                                dist_icon = "⚪"
                            lines.append(
                                f"{dist_icon} <b>{o['symbol']}</b>\n"
                                f"  {side_cn} {o['qty']}张 @ ${o['price']:,.0f}\n"
                                f"  Bid ${o['bid']:,.0f} / Ask ${o['ask']:,.0f} / Mark ${o['mark']:,.0f}\n"
                                f"  差距: ${o['gap']:,.0f} ({o['gap_pct']:.1f}%)\n"
                            )
                    else:
                        lines.append("📝 暂无挂单")

                    self.tg.send("\n".join(lines))
                else:
                    self.tg.send("⏳ 尚未完成首次扫描, 请稍等")

            elif text == "/orders":
                if self.last_result:
                    ords = self.last_result.get("order_alerts", [])
                    self.tg.send(self.fmt.orders_msg(ords))
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/profit":
                if self.last_result:
                    # 实时计算(不用缓存, 保证最新)
                    self.tg.send("💰 正在分析...")
                    try:
                        data = self.last_result["data"]
                        results = self.last_result["results"]
                        iv_trend = self.iv_tracker.get_iv_trend()
                        analysis = analyze_position_optimization(
                            self.api, data, results, iv_trend,
                        )
                        msg = format_profit_report(analysis)
                        self.tg.send(msg)
                    except Exception as e:
                        log.error(f"收益分析失败: {e}", exc_info=True)
                        self.tg.send(f"❌ 分析失败: {e}")
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/risk":
                if self.last_result:
                    risk = self.last_result.get("risk_alerts", [])
                    spot = self.last_result["data"]["spot"]
                    msg = format_risk_alerts(
                        risk, full=True,
                        risk_engine=self.risk_engine, spot=spot,
                    )
                    self.tg.send(msg)
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/iv":
                if self.last_result:
                    self.tg.send("📈 正在生成 IV 图表...")
                    try:
                        from iv_chart import generate_iv_charts
                        chart_path, analysis = generate_iv_charts(
                            self.last_result["data"],
                            self.last_result["iv_surface"],
                            self.last_result["data"]["spot"],
                        )
                        # 发图片
                        self.tg.send_photo(chart_path, caption="IV Term Structure & Skew")
                        # 发解读
                        self.tg.send(analysis)
                    except Exception as e:
                        log.error(f"IV图表生成失败: {e}", exc_info=True)
                        # fallback: 纯文字
                        iv_s = self.last_result["iv_surface"]
                        lines = ["📈 <b>IV 曲面</b> (图表生成失败)\n"]
                        mean_iv = iv_s["global"]["mean"]
                        pctl = self.iv_tracker.get_iv_percentile(mean_iv)
                        lines.append(f"Put IV 均值: {mean_iv:.3f}  Percentile: {pctl:.0f}%\n")
                        for exp in sorted(iv_s["by_exp"].keys()):
                            s = iv_s["by_exp"][exp]
                            lines.append(f"<code>{exp:<10} {s['median']:>5.3f}  "
                                         f"{s['mean']:>5.3f}  {s['min']:>5.3f}  "
                                         f"{s['max']:>5.3f}</code>")
                        self.tg.send("\n".join(lines))
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/top" or text.startswith("/top"):
                if self.last_result:
                    opps = self.last_result.get("v2_opportunities", [])
                    account = self.last_result.get("account_risk")
                    hv = self.last_result.get("hv_20", 0)
                    iv_mean = self.last_result["iv_surface"]["global"]["mean"]
                    if opps and account:
                        # 解析筛选分数: /top80, /top70 等
                        min_score = 0
                        cmd_num = text[4:]  # 去掉 "/top"
                        if cmd_num.isdigit():
                            min_score = int(cmd_num)
                        
                        if min_score > 0:
                            filtered = [o for o in opps if o.score >= min_score]
                            if filtered:
                                msg = format_opportunities_tg(filtered, account, hv, iv_mean)
                                header = f"🔍 <b>评分 ≥{min_score} 的机会 ({len(filtered)}个)</b>\n\n"
                                self.tg.send(header + msg)
                            else:
                                self.tg.send(f"当前无评分 ≥{min_score} 的机会\n\n👉 /top 查看全部")
                        else:
                            msg = format_opportunities_tg(opps, account, hv, iv_mean)
                            self.tg.send(msg)
                    else:
                        self.tg.send("当前无符合条件的机会")
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/ai":
                if not self.ai_analyst.is_available:
                    self.tg.send("❌ AI 分析未启用 (ANTHROPIC_API_KEY 未配置)")
                elif self.last_result:
                    # 优先返回缓存 (如果不到30分钟)
                    cached = self.ai_analyst.get_cached_report()
                    age = time.time() - self.ai_analyst.last_analysis_time
                    if cached and age < 1800:
                        # 缓存报告也带图表
                        chart_path = self._generate_iv_chart(self.last_result)
                        if chart_path:
                            if len(cached) <= 1024:
                                self.tg.send_photo(chart_path, caption=cached)
                            else:
                                self.tg.send_photo(chart_path, caption="📊 <b>IV Dashboard</b> — 详细分析见下方")
                                self.tg.send(cached)
                        else:
                            self.tg.send(cached)
                    else:
                        self.tg.send("🤖 正在分析...")
                        report = self.ai_analyst.analyze(self.last_result, self.iv_tracker)
                        if report:
                            chart_path = self._generate_iv_chart(self.last_result)
                            if chart_path:
                                if len(report) <= 1024:
                                    self.tg.send_photo(chart_path, caption=report)
                                else:
                                    self.tg.send_photo(chart_path, caption="📊 <b>IV Dashboard</b> — 详细分析见下方")
                                    self.tg.send(report)
                            else:
                                self.tg.send(report)
                        else:
                            self.tg.send("❌ AI 分析失败，请查看日志")
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/overview":
                if self.last_result:
                    self.send_overview(self.last_result)
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/hedge":
                if self.last_result and self.last_pos_list:
                    self.tg.send("🛡️ 计算对冲方案...")
                    try:
                        spot = self.last_result["data"]["spot"]
                        try:
                            ma = self.api._get("/eapi/v1/marginAccount", signed=True)
                            balance = float(ma["asset"][0]["marginBalance"])
                        except Exception:
                            balance = 0

                        if balance <= 0:
                            self.tg.send("❌ 无法获取账户余额")
                        else:
                            available_puts = self._get_hedge_candidates(self.last_result["data"])
                            hedge_calc = self.hedge_advisor.calc_hedge_options(
                                self.last_pos_list, spot, balance, available_puts)

                            liq = hedge_calc["liq_current"]
                            lines = ["🛡️ <b>对冲方案</b>\n"]
                            lines.append(f"BTC ${spot:,.0f}  余额 ${balance:,.0f}")
                            lines.append(f"强平价 ${liq['liq_price']:,.0f} (跌 {abs(liq['liq_drop_pct']):.0f}%)")
                            lines.append(f"模式: {self.risk_mode.mode_icon}\n")

                            # 补保证金 vs 买 Put
                            comp = hedge_calc.get("comparison", {})
                            if comp:
                                lines.append("<b>$1,000 对比:</b>")
                                lines.append(f"  补保证金 → 下移 ${comp['cash_1k_improve']:,.0f}")
                                lines.append(f"  买 Put   → 下移 ${comp['best_put_1k_improve']:,.0f}")
                                lines.append(f"  效率: 买Put = <b>{comp['ratio']:.0f}x</b>\n")

                            # 各预算最优
                            best = hedge_calc.get("best_by_budget", {})
                            if best:
                                lines.append("<b>推荐方案:</b>")
                                for budget in [500, 1000, 2000, 3000]:
                                    b = best.get(budget)
                                    if not b:
                                        continue
                                    short_sym = b["symbol"].split("BTC-")[-1]
                                    lines.append(
                                        f"  ${budget:,}: {short_sym} ×{b['qty']:.1f}张"
                                        f" @ ${b['ask']:,.0f}"
                                        f" → 强平 ${b['liq_price']:,.0f}"
                                        f" (跌{abs(b['liq_drop']):.0f}%)"
                                    )
                            self.tg.send("\n".join(lines))
                    except Exception as e:
                        self.tg.send(f"❌ 对冲计算失败: {e}")
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/perf":
                msg = self.journal.format_performance_tg()
                self.tg.send(msg)

            elif text == "/journal":
                # 显示最近 5 笔交易记录
                trades = self.journal.data.get("trades", [])
                if not trades:
                    self.tg.send("📊 暂无交易记录")
                else:
                    lines = ["📊 <b>最近交易记录</b>\n"]
                    for t in reversed(trades[-5:]):
                        status_icon = "🟢" if t.get("status") == "OPEN" else "⚪"
                        short_sym = t["symbol"].split("BTC-")[-1]
                        direction = "Short" if t.get("direction") == "SHORT" else "Long"
                        lines.append(f"{status_icon} <b>{short_sym}</b> ({direction})")
                        lines.append(f"  入场: ${t['entry_price']:,.0f}  数量: {abs(t['qty'])}")
                        if t.get("status") == "CLOSED":
                            lines.append(
                                f"  PnL: ${t.get('realized_pnl', 0):+,.0f} "
                                f"({t.get('realized_pnl_pct', 0):+.1f}%)"
                            )
                            lines.append(f"  持有: {t.get('holding_days', 0):.0f}天  原因: {t.get('exit_reason', '?')}")
                        else:
                            lines.append(f"  当前: ${t.get('last_mark', 0):,.0f}")
                            peak = t.get("peak_profit_pct", 0)
                            lines.append(f"  峰值盈利: {peak:+.0f}%")
                        lines.append("")
                    # 信号质量
                    hit = self.journal.signal_hit_rate()
                    if hit["total_signals"] > 0:
                        lines.append(
                            f"信号质量: {hit['total_signals']}条推送, "
                            f"{hit['acted_on']}条入场 ({hit['hit_rate']:.0f}%)"
                        )
                    self.tg.send("\n".join(lines))

    def _process_hedge_alerts(self, result: dict):
        """检查是否需要推送对冲建议"""
        try:
            liq = self.hedge_advisor.update_liquidation(
                self.last_pos_list, result["data"]["spot"],
                result.get("account_balance", 0)
            ) if self.last_pos_list else None

            if not liq or not self.hedge_advisor.should_push_hedge(liq):
                return

            # 获取可买的 Put 候选
            available_puts = self._get_hedge_candidates(result["data"])
            if not available_puts:
                return

            spot = result["data"]["spot"]
            account_balance = result.get("account_balance", 0)
            if not account_balance:
                try:
                    ma = self.api._get("/eapi/v1/marginAccount", signed=True)
                    account_balance = float(ma["asset"][0]["marginBalance"])
                except Exception:
                    return

            hedge_calc = self.hedge_advisor.calc_hedge_options(
                self.last_pos_list, spot, account_balance, available_puts)

            msg = self.hedge_advisor.format_hedge_alert(liq, hedge_calc, spot)
            self.tg.broadcast(msg)
            log.info(f"推送对冲建议 (强平距离 {abs(liq['liq_drop_pct']):.0f}%)")

        except Exception as e:
            log.error(f"对冲建议推送失败: {e}")

    def _get_hedge_candidates(self, data: dict) -> list:
        """获取可买的 Put 对冲候选"""
        candidates = []
        try:
            spot = data["spot"]
            marks = data.get("marks", {})
            tickers = self.api.get_ticker()
            ticker_map = {t["symbol"]: t for t in tickers if t["symbol"].startswith("BTC")}
            info = self.api.get_exchange_info()
            contracts = {s["symbol"]: s for s in info["optionSymbols"]
                         if s["underlying"] == "BTCUSDT"}
            now = datetime.now(timezone.utc)

            for sym, c in contracts.items():
                if c.get("side") != "PUT":
                    continue
                strike = float(c["strikePrice"])
                # 只看 OTM 到 slightly ITM
                if strike < spot * 0.50 or strike > spot * 1.05:
                    continue
                exp = datetime.fromtimestamp(c["expiryDate"] / 1000, tz=timezone.utc)
                dte = (exp - now).total_seconds() / 86400
                if dte < 7 or dte > 120:
                    continue

                m = marks.get(sym, {})
                t = ticker_map.get(sym, {})
                ask = float(t.get("askPrice", 0))
                iv = float(m.get("markIV", 0))
                delta = float(m.get("delta", 0))

                if ask <= 0:
                    continue

                candidates.append({
                    "symbol": sym, "strike": strike, "dte": round(dte),
                    "ask": ask, "iv": iv, "delta": delta,
                })
        except Exception as e:
            log.error(f"获取对冲候选失败: {e}")

        return candidates

    def _check_proactive_roll(self, result: dict):
        """P2-8: 到期前主动 Roll 提醒"""
        pos_alerts = result.get("pos_alerts", [])
        for p in pos_alerts:
            if p.get("type") != "POSITION":
                continue
            if p.get("direction") != "Short":
                continue
            dte = p.get("dte", 999)
            pnl_pct = p.get("pnl_pct", 0)
            sym = p.get("symbol", "")

            # DTE < 21 + 盈利 > 30% → 主动提醒 Roll
            if dte < 21 and pnl_pct > 30:
                key = f"roll_remind:{sym}"
                now = time.time()
                last = self.cooldown.signal_sent.get(key, {}).get("time", 0)
                if now - last < 43200:  # 12小时冷却
                    continue
                self.cooldown.signal_sent[key] = {"signal": "ROLL_REMIND", "time": now}

                short_sym = sym.split("BTC-")[-1]
                self.tg.broadcast(
                    f"🔄 <b>Roll 提醒</b>\n\n"
                    f"<b>{short_sym}</b> DTE {dte}天 + 盈利 {pnl_pct:.0f}%\n"
                    f"临近到期且已有不错盈利, 建议考虑 Roll 到远期合约\n\n"
                    f"👉 /profit 查看详细 Roll 建议",
                    silent=True,
                )
                log.info(f"推送 Roll 提醒: {sym} DTE={dte} PnL={pnl_pct:.0f}%")

    # --- 命令监听线程 ---
    def _command_loop(self):
        """独立线程: 每秒轮询 TG 消息, 秒回命令"""
        log.info("命令监听线程启动")
        while self.running:
            try:
                self.handle_commands()
            except Exception as e:
                log.error(f"命令处理异常: {e}")
            time.sleep(1)  # 每秒检查一次, 保证秒回

    # --- 扫描线程 ---
    def _scan_loop(self):
        """独立线程: 按间隔扫描市场"""
        log.info("扫描线程启动")

        # 首次扫描 + 概览
        try:
            result = self.do_scan()
            self.send_overview(result)
            self.process_signals(result["results"], result["data"]["spot"])
            self.process_pos_alerts(result["pos_alerts"])
            self.process_risk_alerts(result.get("risk_alerts", []))
            self.process_profit_advice(result.get("profit_analysis"))
            self.process_v2_signals(result)
        except Exception as e:
            log.error(f"首次扫描失败: {e}")
            self.tg.send(f"❌ 首次扫描失败: {e}")

        while self.running:
            try:
                # 用短 sleep 循环代替长 sleep, 这样退出信号能及时响应
                wait_end = time.time() + self.current_interval
                while self.running and time.time() < wait_end:
                    time.sleep(1)

                if not self.running:
                    break

                # 扫描
                result = self.do_scan()

                spot = result["data"]["spot"]
                n_strong = len([r for r in result["results"] if r["signal"] == "STRONG"])
                n_signal = len([r for r in result["results"] if r["signal"] == "SIGNAL"])
                log.info(f"扫描 #{self.scan_count}: BTC ${spot:,.0f} | "
                         f"强:{n_strong} 信号:{n_signal} | {result['scan_time']:.1f}s")

                # 处理信号推送
                self.process_signals(result["results"], spot)

                # 处理持仓预警
                self.process_pos_alerts(result["pos_alerts"])

                # 处理风控告警
                self.process_risk_alerts(result.get("risk_alerts", []))

                # 处理止盈建议
                if result.get("profit_analysis"):
                    self.process_profit_advice(result["profit_analysis"])

                # v2 机会信号
                self.process_v2_signals(result)

                # 对冲建议推送 (基于强平距离)
                self._process_hedge_alerts(result)

                # 风控模式控制扫描间隔
                mode_interval = self.risk_mode.scan_interval
                if mode_interval != self.current_interval:
                    self.current_interval = mode_interval
                    log.info(f"扫描间隔调整为 {self.current_interval}s (模式: {self.risk_mode.mode})")

                # 危机模式下抑制新开仓信号 (已在 process_v2_signals 之后)
                # P2-8: 到期前主动 Roll 提醒 (DTE < 21 + 盈利 > 30%)
                self._check_proactive_roll(result)

                # 定期概览
                if time.time() - self.last_overview_time > OVERVIEW_INTERVAL:
                    self.send_overview(result)

                # 定期清理
                if self.scan_count % 100 == 0:
                    self.cooldown.cleanup()

            except Exception as e:
                log.error(f"扫描异常: {e}", exc_info=True)
                try:
                    self.tg.send(f"⚠️ 扫描异常: {e}")
                except Exception:
                    pass
                time.sleep(30)

    # --- 主入口 ---
    def run(self):
        """启动双线程运行"""
        log.info("=" * 60)
        log.info("BTC OTM Put 监控 Bot 启动")
        log.info(f"扫描间隔: {SCAN_INTERVAL_NORMAL}s (常规) / {SCAN_INTERVAL_VOLATILE}s (波动)")
        log.info(f"概览间隔: {OVERVIEW_INTERVAL}s")
        log.info("=" * 60)

        self.tg.broadcast(
            "🟢 <b>监控 Bot 已启动</b>\n\n"
            f"扫描间隔: {SCAN_INTERVAL_NORMAL}s (常规) / {SCAN_INTERVAL_VOLATILE}s (波动)\n"
            f"概览推送: 每{OVERVIEW_INTERVAL // 3600}小时\n\n"
            "发送 /help 查看可用命令"
        )

        def handle_exit(signum, frame):
            log.info("收到退出信号, 正在关闭...")
            self.running = False

        sig.signal(sig.SIGINT, handle_exit)
        sig.signal(sig.SIGTERM, handle_exit)

        # 启动命令监听线程 (daemon=True: 主线程退出时自动结束)
        cmd_thread = threading.Thread(target=self._command_loop, daemon=True)
        cmd_thread.start()

        # 扫描在主线程运行
        try:
            self._scan_loop()
        except KeyboardInterrupt:
            pass

        self.running = False
        self.tg.broadcast("🔴 <b>监控 Bot 已停止</b>")
        self.iv_tracker.save()
        self._save_state()
        self.journal.save()
        log.info("Bot 已停止")


# ============================================================
#  入口
# ============================================================
def main():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("错误: 请在 .env 中配置 TG_BOT_TOKEN 和 TG_CHAT_ID")
        sys.exit(1)

    service = MonitorService()
    service.run()


if __name__ == "__main__":
    main()
