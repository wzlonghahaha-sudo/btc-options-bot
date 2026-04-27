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
from profit_optimizer import analyze_position_optimization, format_profit_report

load_dotenv()

# ============================================================
#  配置
# ============================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

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
        """发送消息"""
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
                        risk_alerts: list = None) -> str:
        """格式化市场概览"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        mean_iv = iv_surface["global"]["mean"]
        iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
        iv_trend = iv_tracker.get_iv_trend()

        n_strong = len([r for r in results if r["signal"] == "STRONG"])
        n_signal = len([r for r in results if r["signal"] == "SIGNAL"])
        n_watch = len([r for r in results if r["signal"] == "WATCH"])

        lines = [
            f"📊 <b>市场概览</b>  {now}",
            "",
            f"BTC: <b>${spot:,.2f}</b>",
            f"Put IV均值: <b>{mean_iv:.3f}</b>  |  "
            f"Percentile: {iv_pctl:.0f}%  |  {iv_trend}",
            "",
            f"信号: 🔴 强信号 {n_strong}  |  🟡 信号 {n_signal}  |  👀 关注 {n_watch}",
            format_risk_summary(risk_alerts or [], spot),
        ]

        # IV 曲面摘要 (只显示近几个到期日)
        lines.append("")
        lines.append("<b>IV 曲面:</b>")
        exps = sorted(iv_surface["by_exp"].keys())[:6]
        for exp in exps:
            s = iv_surface["by_exp"][exp]
            lines.append(f"  {exp}: 中位 {s['median']:.3f}  均值 {s['mean']:.3f}  "
                         f"[{s['min']:.3f} - {s['max']:.3f}]")

        # 持仓摘要
        if pos_alerts:
            lines.append("")
            lines.append("<b>持仓:</b>")
            for p in pos_alerts:
                if p["type"] == "ERROR":
                    continue
                icon = {"OK": "✅", "WARNING": "⚠️", "DANGER": "🔴"}.get(p.get("alert"), "")
                lines.append(
                    f"  {icon} {p['symbol']}  ${p['pnl']:+,.0f} ({p['pnl_pct']:+.0f}%)  "
                    f"距行权 {p['dist_to_strike']:.1f}%"
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
            "/top - 查看当前 Top 机会\n"
            "/overview - 发送完整市场概览\n"
            "/strategy - 📖 策略说明 (小白版)\n"
            "/rules - 📏 具体入场/风控规则\n"
            "/help - 显示此帮助\n\n"
            "<b>自动推送:</b>\n"
            "• 新信号 / 信号升级: 即时推送\n"
            "• 持仓预警: 即时推送\n"
            "• 市场概览: 每4小时\n"
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
            "<b>📊 赔率评分 (满分100)</b>\n\n"
            "五个维度加权打分：\n"
            "• 安全垫 (35%权重)\n"
            "  25%起步, 35%不错, 45%+满分\n"
            "• IV溢价 (25%权重)\n"
            "  20%起步, 35%不错, 60%+满分\n"
            "• 年化收益 (20%权重)\n"
            "  30%起步, 50%不错, 80%+满分\n"
            "• 流动性 (12%权重)\n"
            "  Spread+成交量+持仓量\n"
            "• Theta效率 (8%权重)\n"
            "  每天吃掉多少权利金\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🚦 信号等级</b>\n\n"
            "• ⚪ WAIT (&lt;60分) → 不推送, 继续等\n"
            "• 👀 WATCH (60-74分) → 关注, 不推送\n"
            "• 🟡 SIGNAL (75-87分) → <b>推送通知</b>\n"
            "• 🔴 STRONG (88分+) → <b>强烈推送</b>\n\n"
            "大部分时间都是 WAIT/WATCH\n"
            "SIGNAL 可能几天出现一次\n"
            "STRONG 可能几周才出现一次\n\n"

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
        self.fmt = MessageFormatter()

        self.scan_count = 0
        self.start_time = time.time()
        self.last_spot = 0
        self.last_scan_time = 0
        self.last_overview_time = 0
        self.current_interval = SCAN_INTERVAL_NORMAL
        self.last_result = None

        self.running = True
        self.update_offset = 0

    def uptime_str(self) -> str:
        elapsed = time.time() - self.start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        return f"{hours}h {minutes}m"

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

        results = scan_opportunities(data, iv_surface, self.iv_tracker)
        pos_alerts = monitor_positions(self.api, data)
        order_alerts = monitor_open_orders(self.api, data)

        # 风控检查
        try:
            positions = self.api.get_position()
            risk_data = {
                "spot": data["spot"],
                "marks": data["marks"],
                "positions": [p for p in positions if float(p.get("quantity", 0)) != 0],
            }
            risk_alerts = self.risk_engine.check_all(risk_data)
        except Exception as e:
            log.error(f"风控检查失败: {e}")
            risk_alerts = []

        # 收益优化分析 (每10次扫描做一次, 避免频繁API调用)
        profit_analysis = None
        if self.scan_count % 10 == 0 or self.scan_count <= 1:
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
            "scan_time": scan_time,
        }
        self.last_result = result
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
            self.tg.send(msg)
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
                self.tg.send(msg, silent=(alert == "WARNING"))
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
        self.tg.send(msg, silent=silent)
        log.info(f"推送风控告警: {len(pushable)} 项 "
                 f"(C:{len([a for a in pushable if a.level=='CRITICAL'])} "
                 f"D:{len([a for a in pushable if a.level=='DANGER'])} "
                 f"W:{len([a for a in pushable if a.level=='WARNING'])})")

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

            self.tg.send("\n".join(msg_lines), silent=(tp.urgency != "HIGH"))
            log.info(f"推送止盈建议: {p['symbol']} → {tp.action} ({tp.urgency})")

    def send_overview(self, result: dict):
        """发送市场概览"""
        msg = self.fmt.market_overview(
            result["data"]["spot"],
            result["iv_surface"],
            result["results"],
            result["pos_alerts"],
            self.iv_tracker,
            order_alerts=result.get("order_alerts", []),
            risk_alerts=result.get("risk_alerts", []),
        )
        self.tg.send(msg, silent=True)
        self.last_overview_time = time.time()
        log.info("推送市场概览")

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
                    if pos:
                        lines.append("📋 <b>当前持仓</b>\n")
                        for p in pos:
                            if p["type"] == "ERROR":
                                lines.append(f"  ❌ {p['msg']}")
                                continue
                            icon = {"OK": "✅", "WARNING": "⚠️", "DANGER": "🔴"}.get(p.get("alert"), "")
                            lines.append(
                                f"{icon} <b>{p['symbol']}</b>\n"
                                f"  数量: {p['qty']}  入场: ${p['entry']:,.0f}  "
                                f"当前: ${p['mark']:,.0f}\n"
                                f"  盈亏: <b>${p['pnl']:+,.0f}</b> ({p['pnl_pct']:+.0f}%)  "
                                f"距行权: {p['dist_to_strike']:.1f}%\n"
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
                    msg = format_risk_alerts(risk, full=True)
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

            elif text == "/top":
                if self.last_result:
                    results = self.last_result["results"]
                    top = results[:10]
                    if top:
                        spot = self.last_result["data"]["spot"]
                        lines = [f"🏆 <b>Top 10 机会</b>  BTC ${spot:,.0f}\n"]
                        for i, r in enumerate(top, 1):
                            icon = {"STRONG": "🔴", "SIGNAL": "🟡", "WATCH": "👀"}.get(r["signal"], "⚪")
                            lines.append(
                                f"{icon} <b>#{i} {r['symbol']}</b>\n"
                                f"  赔率 {r['odds_score']:.1f}  "
                                f"Bid ${r['bid']:,.0f}  年化 {r['annual_return']:.0f}%  "
                                f"安全垫 {r['safety_pct']:.0f}%  "
                                f"IV溢 {r['iv_premium']:+.0f}%\n"
                            )
                        self.tg.send("\n".join(lines))
                    else:
                        self.tg.send("当前无符合条件的机会")
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

            elif text == "/overview":
                if self.last_result:
                    self.send_overview(self.last_result)
                else:
                    self.tg.send("⏳ 尚未完成首次扫描")

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

        self.tg.send(
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
        self.tg.send("🔴 <b>监控 Bot 已停止</b>")
        self.iv_tracker.save()
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
