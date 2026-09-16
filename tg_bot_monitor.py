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
from daily_digest import generate_daily_digest

load_dotenv()

# ============================================================
#  配置
# ============================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# 额外推送群组 (逗号分隔多个)
TG_GROUP_IDS = [g.strip() for g in os.getenv("TG_GROUP_IDS", "").split(",") if g.strip()]

# 外部心跳 (Dead Man's Switch)
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "")

# 每日 Digest 推送时间 (UTC 小时, 默认 0 = HK 8:00)
DAILY_DIGEST_HOUR_UTC = int(os.getenv("DAILY_DIGEST_HOUR_UTC", "0"))

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
ACK_COOLDOWN = 86400             # ACK 确认后静默 24 小时
MUTE_COOLDOWN = 3600             # 手动静音 1 小时

# ============================================================
#  可调参数白名单 (P2-3)
# ============================================================
ADJUSTABLE_PARAMS = {
    "scan_interval": {"type": int, "min": 30, "max": 600, "desc": "扫描间隔(秒)"},
    "overview_interval": {"type": int, "min": 1800, "max": 14400, "desc": "概览间隔(秒)"},
    "score_push": {"type": int, "min": 50, "max": 95, "desc": "自动推送评分门槛"},
    "pnl_warn_ratio": {"type": float, "min": 0.5, "max": 5.0, "desc": "浮亏警告倍数"},
    "pnl_danger_ratio": {"type": float, "min": 1.0, "max": 10.0, "desc": "浮亏危险倍数"},
    "liq_warning_pct": {"type": int, "min": 10, "max": 50, "desc": "强平距离警告%"},
    "daily_digest_hour": {"type": int, "min": 0, "max": 23, "desc": "日报推送UTC小时"},
}

# 运行时配置 (从 bot_state.json 加载, 通过 /set 修改)
runtime_config: dict = {}

# 参数名 → 对应的模块级全局变量名 (用于 get_runtime_param 默认值)
_PARAM_DEFAULTS = {
    "scan_interval": "SCAN_INTERVAL_NORMAL",
    "overview_interval": "OVERVIEW_INTERVAL",
    "score_push": None,       # 无直接全局变量, 使用 ScanConfig.SCORE_PUSH
    "pnl_warn_ratio": None,
    "pnl_danger_ratio": None,
    "liq_warning_pct": None,
    "daily_digest_hour": None,
}


def get_runtime_param(name: str, default=None):
    """获取运行时参数值: 优先 runtime_config, 否则用 default"""
    return runtime_config.get(name, default)


# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg_bot")

# 告警确认按钮 (语义拆分: 已处理 vs 稍后)
from ux_copy import alert_action_buttons, opportunity_action_buttons
ALERT_ACK_BUTTONS = alert_action_buttons()

VALID_BOT_MODES = ("hunt", "hold", "quiet")


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
        except Exception as e:
            log.warning(f"TG getUpdates 失败: {e}")
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

    def send_with_buttons(self, chat_id: str, text: str, buttons: list,
                          parse_mode: str = "HTML") -> bool:
        """发送带 inline keyboard 按钮的消息 (默认每行 2 个)"""
        try:
            if buttons and isinstance(buttons[0], list):
                rows = buttons
            else:
                rows = []
                for i in range(0, len(buttons), 2):
                    rows.append([
                        {"text": b["text"], "callback_data": b["callback_data"]}
                        for b in buttons[i:i + 2]
                    ])
            reply_markup = {"inline_keyboard": rows}
            resp = self.session.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "reply_markup": reply_markup,
                },
                timeout=10,
            )
            if not resp.json().get("ok"):
                log.error(f"TG send_with_buttons failed: {resp.text}")
                return False
            return True
        except Exception as e:
            log.error(f"TG send_with_buttons error: {e}")
            return False
    def broadcast_with_buttons(self, text: str, buttons: list,
                                parse_mode: str = "HTML", silent: bool = False):
        """广播带按钮的消息: 发到主 chat + 所有群组"""
        self.send_with_buttons(self.chat_id, text, buttons, parse_mode)
        for group_id in TG_GROUP_IDS:
            self.send_with_buttons(group_id, text, buttons, parse_mode)

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
                   last_scan_time: float, interval: int,
                   push_status: dict = None, mode: str = "hunt") -> str:
        from ux_copy import status_msg as _status
        return _status(spot, scan_count, uptime_str, last_scan_time,
                       interval, push_status, mode)

    @staticmethod
    def help_msg(mode: str = "hunt") -> str:
        from ux_copy import help_msg as _help
        return _help(mode)

    @staticmethod
    def strategy_msg() -> str:
        from ux_copy import strategy_msg as _strategy
        return _strategy()

    @staticmethod
    def rules_msg() -> str:
        from ux_copy import rules_msg as _rules
        return _rules()

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

        # R4-6: 推送噪音控制
        from push_control import PushController
        self.push_ctrl = PushController()

        # 应急自动对冲 (opt-in, 默认关闭)
        from emergency_hedge import EmergencyHedge
        self.emergency_hedge = EmergencyHedge(
            api=self.api,
            tg_send_func=lambda text: self.tg.broadcast(text),
            state_persistence=self.state,
        )

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
        self.last_digest_date = None  # 每日 digest 去重: 记录上次发送日期

        # UX: 机会推送可操作上下文 {short_sym: {symbol, qty, limit, bid, score, ts}}
        self.pending_opp_actions: dict = {}
        # 首次扫描开始时间 (用于冷启动文案)
        self.first_scan_started_at = 0

        # P1-4: 恢复持久化状态
        self._restore_state()

    def get_bot_mode(self) -> str:
        mode = get_runtime_param("bot_mode", "hunt")
        return mode if mode in VALID_BOT_MODES else "hunt"
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

            # P2-3: 恢复运行时调参
            global runtime_config
            saved_cfg = self.state.load_runtime_config()
            if saved_cfg:
                runtime_config.update(saved_cfg)
                log.info(f"恢复运行时配置: {saved_cfg}")
                # 应用 scan_interval / overview_interval 到模块级变量
                self._apply_runtime_config()

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
            self.state.save_runtime_config(runtime_config)
            self.state.save()
        except Exception as e:
            log.error(f"状态保存失败: {e}")

    def _apply_runtime_config(self):
        """将 runtime_config 中的值应用到模块级变量"""
        global SCAN_INTERVAL_NORMAL, OVERVIEW_INTERVAL
        if "scan_interval" in runtime_config:
            SCAN_INTERVAL_NORMAL = runtime_config["scan_interval"]
            self.current_interval = SCAN_INTERVAL_NORMAL
        if "overview_interval" in runtime_config:
            OVERVIEW_INTERVAL = runtime_config["overview_interval"]

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
        # 统一通过 get_account_equity() 获取 (R2-4)
        from binance_options import get_account_equity
        acct_info = get_account_equity(self.api)
        account_balance = acct_info["margin_balance"]
        if account_balance <= 0 and account_risk:
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

            # 应急自动对冲: 检查是否需要触发
            # R3-1: record_critical_alert 已移到 process_risk_alerts 推送成功后调用
            # 这里只调用 check_and_act (disabled 时纯 no-op)

            # 调用应急对冲评估 (disabled 时纯 no-op)
            self.emergency_hedge.check_and_act(
                liq_drop_pct=liq.get("liq_drop_pct", -100),
                pos_list=pos_list,
                spot=data["spot"],
                account_balance=account_balance,
                available_puts=list(data.get("marks", {}).values()),
                marks=data.get("marks", {}),
            )

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
        except Exception as e:
            log.warning(f"保存 IV 曲面快照失败: {e}")

        # R3-6: 期权链快照落盘 (为回测积累数据)
        try:
            from chain_snapshot import save_chain_snapshot, cleanup_old_snapshots
            save_chain_snapshot(data)
            # 每天第一次扫描时清理过期文件
            if self.scan_count <= 1:
                cleanup_old_snapshots()
        except Exception as e:
            log.warning(f"期权链快照失败 (不影响主流程): {e}")

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
            order_events = self.journal.check_order_fills(current_orders, api=self.api)
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
            # R4: 三层格式 (V1 信号仍用旧格式, 因为 V2 是主推送通道)
            msg = self.fmt.signal_alert(to_send, spot)
            self.tg.broadcast(msg)
            log.info(f"推送 {len(to_send)} 个 V1 信号")

    def process_pos_alerts(self, pos_alerts: list):
        """处理持仓预警"""
        # 检查是否被用户静音/确认
        now = time.time()
        ack_until = self.cooldown.signal_sent.get("_ack_alert", {}).get("time", 0)
        mute_until = self.cooldown.signal_sent.get("_mute_1h", {}).get("time", 0)

        for p in pos_alerts:
            alert = p.get("alert", "OK")
            if alert not in ("WARNING", "DANGER"):
                continue
            sym = p.get("symbol", "")
            if self.cooldown.should_send_pos_alert(sym, alert):
                # 如果被用户静音/确认, 跳过 DANGER 推送
                if alert == "DANGER" and (now < ack_until or now < mute_until):
                    log.info(f"持仓预警 {sym} [{alert}] 被用户静音, 跳过")
                    continue
                # R4 接线: 三层格式持仓预警 + 操作卡 + 风险地图
                try:
                    from indicators import pnl_indicator, dist_strike_indicator
                    from message_spec import build_position_alert_message
                    from playbook import build_position_playbook

                    entry_p = p.get("entry", 0)
                    mark_p = p.get("mark", 0)
                    pnl_val = p.get("pnl", 0)
                    dist_val = p.get("dist_to_strike", 100)
                    loss_r = (mark_p - entry_p) / entry_p if entry_p > 0 else 0
                    qty_val = p.get("qty", 0)
                    strike_val = p.get("strike", 0)
                    bid_val = mark_p * 0.98  # 近似 bid
                    ask_val = mark_p * 1.02  # 近似 ask
                    spot_val = self.last_spot

                    pnl_line = f"浮亏 ${abs(pnl_val):,.0f} ({loss_r:.1f}x权利金) · 距行权仅 {dist_val:.1f}%"
                    cause_line = p.get("msg", "BTC 走势变化")

                    ev = [pnl_indicator(pnl_val, loss_r, entry_p),
                          dist_strike_indicator(dist_val)]

                    # 滚仓候选
                    roll_candidates = None
                    if alert == "DANGER" and self.last_result:
                        try:
                            from roll_advisor import should_trigger_roll, find_roll_candidates
                            abs_delta = abs(p.get("delta", 0))
                            if should_trigger_roll(abs_delta, dist_val):
                                chain = self._get_hedge_candidates(self.last_result["data"])
                                dte = p.get("dte", 30)
                                roll_candidates = find_roll_candidates(
                                    sym, strike_val, entry_p, mark_p, dte, spot_val, chain)
                        except Exception as e:
                            log.warning(f"滚仓搜索失败: {e}")

                    pb = build_position_playbook(
                        sym, qty_val, entry_p, mark_p, bid_val, ask_val,
                        spot_val, strike_val, dist_val, loss_r, roll_candidates)

                    msg = build_position_alert_message(
                        sym, alert, pnl_line, cause_line, ev, pb)
                except Exception as e:
                    log.warning(f"R4格式持仓预警失败, 降级旧格式: {e}")
                    msg = self.fmt.position_alert(p)

                if alert == "DANGER":
                    self.tg.broadcast_with_buttons(msg, ALERT_ACK_BUTTONS)
                    # DANGER 附带风险地图
                    try:
                        from price_axis_chart import generate_risk_map
                        positions_data = self._build_chart_positions()
                        liq = self.hedge_advisor.get_last_liquidation()
                        liq_price = liq.get("liq_price", 0) if liq else 0
                        daily_open = self.risk_engine.price_tracker.daily_open or 0
                        chart = generate_risk_map(self.last_spot, daily_open, positions_data, liq_price)
                        if chart:
                            self.tg.broadcast_photo(chart)
                    except Exception as e:
                        log.warning(f"风险地图附图失败: {e}")
                else:
                    self.tg.broadcast(msg, silent=True)
                self.cooldown.record_pos_alert(sym, alert)
                log.info(f"推送持仓预警 (R4格式): {sym} [{alert}]")

    def process_risk_alerts(self, risk_alerts: list):
        """处理风控告警推送

        R3-1 修复: CRITICAL 级告警永远突破 ACK/mute, 无条件推送。
        ACK 按 (category, level) 记录, 只压制匹配的组合。
        record_critical_alert 在推送成功后调用, 保证"倒计时 ⇒ 用户已被通知"。
        """
        pushable = [a for a in risk_alerts
                    if a.level in ("WARNING", "DANGER", "CRITICAL")
                    and self.risk_engine.should_push(a)]

        if not pushable:
            return

        # quiet 模式: 仅 CRITICAL
        if self.get_bot_mode() == "quiet":
            pushable = [a for a in pushable if a.level == "CRITICAL"]
            if not pushable:
                return

        # --- 分离 CRITICAL 和非 CRITICAL ---
        critical_alerts = [a for a in pushable if a.level == "CRITICAL"]
        non_critical = [a for a in pushable if a.level != "CRITICAL"]

        # --- 非 CRITICAL: 受 ACK/mute 压制 ---
        now = time.time()
        acked_combos = self.cooldown.signal_sent.get("_ack_combos", {})
        mute_until = self.cooldown.signal_sent.get("_mute_1h", {}).get("time", 0)

        allowed_non_critical = []
        for a in non_critical:
            combo_key = f"{a.category}:{a.level}"
            ack_until = acked_combos.get(combo_key, 0)
            if now < ack_until:
                log.info(f"告警 [{combo_key}] 已被 ACK, 跳过")
                continue
            if now < mute_until:
                log.info(f"告警 [{combo_key}] 已被静音, 跳过")
                continue
            allowed_non_critical.append(a)

        # --- 推送非 CRITICAL (如果有) ---
        if allowed_non_critical:
            # R4 接线: 三层格式风控告警
            try:
                from message_spec import build_risk_alert_message
                from playbook import build_risk_playbook
                pb = build_risk_playbook(allowed_non_critical, self.last_spot)
                msg = build_risk_alert_message(allowed_non_critical, pb)
            except Exception as e:
                log.warning(f"R4格式风控告警失败, 降级旧格式: {e}")
                msg = format_risk_alerts(allowed_non_critical)
            silent = all(a.level == "WARNING" for a in allowed_non_critical)
            has_danger = any(a.level == "DANGER" for a in allowed_non_critical)
            if has_danger:
                self.tg.broadcast_with_buttons(msg, ALERT_ACK_BUTTONS, silent=silent)
            else:
                self.tg.broadcast(msg, silent=silent)
            log.info(f"推送风控告警 (非CRITICAL): {len(allowed_non_critical)} 项")

        # --- CRITICAL: 无条件推送, 走多通道 (R3-2) ---
        if critical_alerts:
            from alert_channels import send_critical
            try:
                from message_spec import build_risk_alert_message
                from playbook import build_risk_playbook
                pb = build_risk_playbook(critical_alerts, self.last_spot)
                msg = build_risk_alert_message(critical_alerts, pb)
            except Exception as e:
                log.warning(f"R4格式CRITICAL告警失败, 降级旧格式: {e}")
                msg = format_risk_alerts(critical_alerts)

            def _tg_critical_send(text):
                """封装 broadcast_with_buttons, 供 send_critical 使用"""
                self.tg.broadcast_with_buttons(text, ALERT_ACK_BUTTONS)

            send_critical(msg, tg_send_func=_tg_critical_send)
            log.info(f"推送 CRITICAL 告警 (突破 ACK/mute): {len(critical_alerts)} 项")

            # R3-1.3: 推送成功后才启动 ACK 倒计时
            has_critical_margin = any(
                a.category == "MARGIN" for a in critical_alerts)
            if has_critical_margin:
                self.emergency_hedge.record_critical_alert()

    def process_v2_signals(self, result: dict):
        """处理 v2 机会扫描的信号推送

        推送策略:
        - SCORE_PUSH+分: 主动推送完整详情
        - 低于门槛: 不推送, 用户通过 /top 自行查看
        - 去重: 1小时内同一合约不重复推送
        - hold/quiet 模式抑制新开仓信号
        """
        # 危机模式 / 持仓防守 / 安静模式: 不推送新开仓信号
        if self.risk_mode.should_suppress_signals:
            return
        if self.get_bot_mode() in ("hold", "quiet"):
            return

        opps = result.get("v2_opportunities", [])
        account = result.get("account_risk")
        if not opps or not account:
            return

        now = time.time()

        # 只关注推送门槛以上的可开仓机会
        push_score = get_runtime_param("score_push", ScanConfig.SCORE_PUSH)
        top_opps = [o for o in opps if o.score >= push_score and o.can_open]
        if not top_opps:
            return

        # R4-6: 推送预算 + 等级门槛
        top_opps = [o for o in top_opps if self.push_ctrl.should_push_signal(o.score)]
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
            except Exception as e:
                log.warning(f"信号记录到交易日志失败 [{o.symbol}]: {e}")

        # R4 接线: 用三层格式 + 可操作按钮
        for o in new_top:
            try:
                from indicators import score_grade, iv_rank_indicator, iv_hv_indicator, safety_indicator, event_indicator
                from message_spec import build_opportunity_message
                from playbook import build_opportunity_playbook, calc_limit_price
                from risk_rules import get_stop_loss_price
                from position_sizer import suggest_qty

                grade, bar, _ = score_grade(o.score)
                # 建议张数
                qty = 1
                margin_pct_after = o.new_margin_usage
                try:
                    from binance_options import get_account_equity
                    _acct = get_account_equity(self.api)
                    if _acct["source"] != "error":
                        sizing = suggest_qty(_acct["equity"], _acct["initial_margin"],
                                            o.margin_required, 0, o.strike)
                        qty = max(sizing["qty"], 1)
                except Exception:
                    pass

                limit_price = calc_limit_price(o.bid, o.ask, "SELL")
                mid = (o.bid + o.ask) / 2
                stop_price = get_stop_loss_price(o.bid)

                ev_lines = []
                ev_lines.append(iv_rank_indicator(50))  # TODO: 传入真实 IV Rank
                ev_lines.append(iv_hv_indicator(o.iv_hv_ratio))
                ev_lines.append(safety_indicator(o.safety_pct, abs(o.delta) * 100))
                evt_ind = event_indicator(o.cons)
                if evt_ind:
                    ev_lines.append(evt_ind)

                pb = build_opportunity_playbook(
                    o.symbol, qty, o.bid, o.ask, o.bid, stop_price,
                    self.last_spot, o.strike, o.safety_pct)

                msg = build_opportunity_message(
                    symbol=o.symbol, score=o.score, grade=grade, bar=bar,
                    qty=qty, limit_price=limit_price, bid=o.bid, mid=mid,
                    total_premium=o.bid * qty, margin_per=o.margin_required,
                    margin_pct_after=margin_pct_after,
                    evidence_lines=ev_lines, playbook_text=pb)

                short_sym = o.symbol.split("BTC-")[-1]
                self.pending_opp_actions[short_sym] = {
                    "symbol": o.symbol,
                    "qty": qty,
                    "limit": limit_price,
                    "bid": o.bid,
                    "score": o.score,
                    "ts": now,
                }
                self.tg.broadcast_with_buttons(
                    msg, opportunity_action_buttons(short_sym))
                self.push_ctrl.record_signal_push()
                log.info(f"推送 v2 信号 (R4+按钮): {o.symbol} score={o.score:.0f} grade={grade}")
            except Exception as e:
                log.warning(f"R4格式推送失败 [{o.symbol}], 降级旧格式: {e}")
                # 降级: 用旧格式
                fallback = format_signal_push([o], account)
                if fallback:
                    self.tg.broadcast(fallback)
                    self.push_ctrl.record_signal_push()

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
        except Exception as e:
            log.warning(f"获取 BTC 24h 涨跌幅失败: {e}")

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
        except Exception as e:
            log.warning(f"获取强平价信息失败: {e}")

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
        updates = self.tg.get_updates(offset=self.update_offset, timeout=25)

        for update in updates:
            self.update_offset = update["update_id"] + 1

            # --- 处理 inline keyboard 回调 ---
            if "callback_query" in update:
                cq = update["callback_query"]
                callback_data = cq.get("data", "")
                cq_id = cq["id"]
                cq_chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))

                # 只响应授权用户
                if cq_chat_id != TG_CHAT_ID:
                    continue

                # 确认回调
                try:
                    self.tg.session.post(
                        f"{self.tg.api_base}/answerCallbackQuery",
                        json={"callback_query_id": cq_id, "text": "已确认"},
                        timeout=5,
                    )
                except Exception as e:
                    log.warning(f"answerCallbackQuery 失败: {e}")

                now = time.time()
                if callback_data == "ack_alert":
                    # R3-1: ACK 按类别+级别记录, 只压制匹配的组合
                    # 获取最近推送的 pushable 告警的 (category, level) 集合
                    acked_combos = self.cooldown.signal_sent.get("_ack_combos", {})
                    if self.last_result:
                        recent_alerts = self.last_result.get("risk_alerts", [])
                        for a in recent_alerts:
                            if a.level in ("WARNING", "DANGER"):
                                combo_key = f"{a.category}:{a.level}"
                                acked_combos[combo_key] = now + ACK_COOLDOWN
                    self.cooldown.signal_sent["_ack_combos"] = acked_combos
                    # 通知应急对冲模块: 用户已确认, 取消自动对冲
                    self.emergency_hedge.record_ack()
                    self.tg.send(
                        "✅ 已记录处理: 同类 WARNING/DANGER 24h 内不再推送\n"
                        "(CRITICAL 不受影响)\n"
                        "若已平仓/滚仓, 可用 /journal 核对。"
                    )
                    log.info("用户确认告警 (按类别+级别 ACK)")
                elif callback_data == "mute_1h":
                    # 静音1小时
                    self.cooldown.signal_sent["_mute_1h"] = {
                        "signal": "MUTE", "time": now + MUTE_COOLDOWN
                    }
                    self.tg.send("🔇 已静音1小时 (WARNING/DANGER); CRITICAL 仍会推送")
                    log.info("用户静音告警1h")
                elif callback_data == "cmd_positions":
                    self._cmd_positions()
                elif callback_data == "cmd_hedge":
                    self._cmd_hedge()
                elif callback_data == "cmd_top":
                    self._cmd_top("/top")
                elif callback_data.startswith("opp_copy:"):
                    short = callback_data.split(":", 1)[1]
                    info = self.pending_opp_actions.get(short)
                    if not info:
                        self.tg.send("⏳ 该机会上下文已过期, 请 /top 重新查看")
                    else:
                        from ux_copy import order_copy_text
                        self.tg.send(order_copy_text(
                            info["symbol"], info["qty"], info["limit"], info["bid"]))
                elif callback_data.startswith("opp_acted:"):
                    short = callback_data.split(":", 1)[1]
                    info = self.pending_opp_actions.get(short, {})
                    symbol = info.get("symbol", short)
                    ok = self.journal.mark_signal_acted(symbol)
                    if ok:
                        self.tg.send(f"✅ 已记入 journal: 按信号开仓 <code>{short}</code>")
                    else:
                        # 仍写入一条标记, 方便复盘
                        self.journal.mark_signal_acted(short)
                        self.tg.send(
                            f"✅ 已记录开仓意图: <code>{short}</code>\n"
                            f"(未找到原始信号记录, 仍已标记)"
                        )
                elif callback_data.startswith("opp_ignore:"):
                    short = callback_data.split(":", 1)[1]
                    info = self.pending_opp_actions.get(short, {})
                    symbol = info.get("symbol", short)
                    key = f"v2top:{symbol}" if symbol.startswith("BTC") else f"v2top:BTC-{short}"
                    # 也写短名 key
                    self.cooldown.signal_sent[key] = {"signal": "IGNORE", "time": now + 86400}
                    self.cooldown.signal_sent[f"v2top:BTC-{short}"] = {
                        "signal": "IGNORE", "time": now + 86400
                    }
                    self.tg.send(f"⏭ 已忽略 <code>{short}</code> 24 小时")
                continue

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
                self.tg.send(self.fmt.help_msg(self.get_bot_mode()))

            elif text == "/now":
                self._send_now()

            elif text.startswith("/mode"):
                self._cmd_mode(text)

            elif text == "/strategy":
                self.tg.send(self.fmt.strategy_msg())

            elif text == "/rules":
                self.tg.send(self.fmt.rules_msg())

            elif text == "/status":
                spot = self.last_spot or 0
                self.tg.send(self.fmt.status_msg(
                    spot, self.scan_count, self.uptime_str(),
                    self.last_scan_time, self.current_interval,
                    push_status=self.push_ctrl.get_status(),
                    mode=self.get_bot_mode(),
                ))

            elif text == "/scan":
                self.tg.send("🔄 正在扫描 (约 30–60 秒)...")
                if not self.first_scan_started_at:
                    self.first_scan_started_at = time.time()
                result = self.do_scan()
                self._send_now(result)

            elif text == "/positions":
                self._cmd_positions()

            elif text == "/orders":
                if not self._ensure_ready():
                    pass
                else:
                    ords = self.last_result.get("order_alerts", [])
                    self.tg.send(self.fmt.orders_msg(ords))

            elif text == "/profit":
                if not self._ensure_ready():
                    pass
                else:
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

            elif text == "/risk":
                if not self._ensure_ready():
                    pass
                else:
                    risk = self.last_result.get("risk_alerts", [])
                    spot = self.last_result["data"]["spot"]
                    msg = format_risk_alerts(
                        risk, full=True,
                        risk_engine=self.risk_engine, spot=spot,
                    )
                    self.tg.send(msg)

            elif text == "/iv":
                if not self._ensure_ready():
                    pass
                else:
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

            elif text == "/top" or text.startswith("/top"):
                self._cmd_top(text)

            elif text == "/ai":
                if not self.ai_analyst.is_available:
                    self.tg.send("❌ AI 分析未启用 (ANTHROPIC_API_KEY 未配置)")
                elif not self._ensure_ready():
                    pass
                else:
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

            elif text == "/overview":
                if not self._ensure_ready():
                    pass
                else:
                    self.send_overview(self.last_result)

            elif text == "/hedge":
                self._cmd_hedge()

            elif text == "/config":
                self._cmd_config()

            elif text.startswith("/set "):
                self._cmd_set(text)

            elif text == "/payoff":
                if not self._ensure_ready():
                    pass
                else:
                    self.tg.send("📈 正在生成 Payoff 图...")
                    try:
                        pos_alerts = self.last_result.get("pos_alerts", [])
                        real_positions = [
                            p for p in pos_alerts
                            if p.get("type") == "POSITION" and p.get("qty", 0) != 0
                        ]
                        if not real_positions:
                            self.tg.send("📋 当前无持仓, 无法生成 Payoff 图")
                        else:
                            # 构建 payoff_chart 所需的 positions 格式
                            payoff_positions = []
                            for p in real_positions:
                                sym = p["symbol"]
                                parts = sym.split("-")
                                strike = float(parts[2]) if len(parts) >= 4 else 0
                                payoff_positions.append({
                                    "symbol": sym,
                                    "qty": p["qty"],
                                    "strike": strike,
                                    "entry_price": p["entry"],
                                })

                            spot = self.last_result["data"]["spot"]

                            # 获取强平价
                            liq_price = None
                            if self.hedge_advisor.last_liq_price > 0:
                                liq_price = self.hedge_advisor.last_liq_price

                            from payoff_chart import generate_payoff_chart
                            chart_path = generate_payoff_chart(
                                payoff_positions, spot,
                                liq_price=liq_price,
                                save_path="charts/payoff.png",
                            )
                            if chart_path:
                                # 构建 caption
                                n_short = sum(1 for p in real_positions if p["qty"] < 0)
                                n_long = sum(1 for p in real_positions if p["qty"] > 0)
                                caption = (
                                    f"📈 <b>Portfolio Expiry Payoff</b>\n"
                                    f"BTC ${spot:,.0f}  |  "
                                    f"{n_short} Short + {n_long} Long"
                                )
                                if liq_price:
                                    liq_pct = (liq_price / spot - 1) * 100
                                    caption += f"\nLiq ${liq_price:,.0f} ({liq_pct:+.1f}%)"
                                self.tg.send_photo(chart_path, caption=caption)
                            else:
                                self.tg.send("❌ Payoff 图生成失败")
                    except Exception as e:
                        log.error(f"Payoff 图生成失败: {e}", exc_info=True)
                        self.tg.send(f"❌ Payoff 图生成失败: {e}")

            elif text == "/map":
                try:
                    from price_axis_chart import generate_risk_map
                    positions_data = self._build_chart_positions()
                    liq = self.hedge_advisor.get_last_liquidation()
                    liq_price = liq.get("liq_price", 0) if liq else 0
                    daily_open = self.risk_engine.price_tracker.daily_open or 0
                    chart_path = generate_risk_map(
                        self.last_spot, daily_open, positions_data, liq_price)
                    if chart_path:
                        self.tg.send_photo(chart_path, caption="🗺 价格轴风险地图")
                    else:
                        self.tg.send("❌ 风险地图生成失败 (无持仓数据)")
                except Exception as e:
                    log.error(f"风险地图失败: {e}")
                    self.tg.send(f"❌ 风险地图生成失败: {e}")

            elif text == "/calibration":
                try:
                    from score_calibration import generate_calibration_report
                    report = generate_calibration_report(self.journal.data)
                    self.tg.send(report)
                except Exception as e:
                    log.error(f"校准报告生成失败: {e}")
                    self.tg.send(f"❌ 校准报告生成失败: {e}")

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

    # --- UX helpers ---
    def _ensure_ready(self) -> bool:
        """冷启动提示: 未完成首次扫描时给可预期等待文案"""
        if self.last_result:
            return True
        elapsed = 0
        if self.first_scan_started_at:
            elapsed = int(time.time() - self.first_scan_started_at)
        tip = "通常 30–60 秒"
        if elapsed > 0:
            tip = f"已等待 {elapsed}s, 通常总计 30–60 秒"
        self.tg.send(
            f"⏳ 首次扫描尚未完成 ({tip})。\n"
            f"完成后 /now 可用; 也可发 /scan 强制刷新。"
        )
        return False

    def _build_now_snapshot(self, result: dict = None) -> dict:
        """构建 /now 决策快照"""
        result = result or self.last_result
        push = self.push_ctrl.get_status()
        snap = {
            "ready": bool(result),
            "mode": self.get_bot_mode(),
            "score_push": get_runtime_param("score_push", ScanConfig.SCORE_PUSH),
            "push_used": push.get("daily_signal_count", 0),
            "push_limit": push.get("daily_limit", 5),
            "push_suppressed": push.get("suppressed_today", 0),
        }
        if not result:
            return snap

        spot = result.get("data", {}).get("spot") or self.last_spot or 0
        snap["spot"] = spot

        change_pct = None
        try:
            ch = self.risk_engine.price_tracker.get_change_pct(86400)
            if ch != 0:
                change_pct = ch
        except Exception:
            pass
        snap["change_pct"] = change_pct

        pos = result.get("pos_alerts") or []
        real_pos = [p for p in pos if p.get("type") != "ERROR" and p.get("qty", 0) != 0]
        # type may be missing; accept any with pnl
        if not real_pos:
            real_pos = [p for p in pos if "pnl" in p and p.get("type") != "ERROR"]

        snap["pos_count"] = len(real_pos)
        snap["total_pnl"] = sum(p.get("pnl", 0) for p in real_pos)
        dists = [p.get("dist_to_strike") for p in real_pos
                 if p.get("dist_to_strike") is not None]
        snap["nearest_dist"] = min(dists) if dists else None

        rank = {"CRITICAL": 4, "DANGER": 3, "WARNING": 2, "WATCH": 1, "OK": 0}
        worst = "OK"
        for p in real_pos:
            a = p.get("alert", "OK")
            if rank.get(a, 0) > rank.get(worst, 0):
                worst = a
        for a in result.get("risk_alerts") or []:
            lvl = getattr(a, "level", None) or (a.get("level") if isinstance(a, dict) else None)
            if lvl and rank.get(lvl, 0) > rank.get(worst, 0):
                worst = lvl
        snap["worst_alert"] = worst

        liq_drop = None
        try:
            if getattr(self.hedge_advisor, "last_liq_drop", 0):
                liq_drop = self.hedge_advisor.last_liq_drop
        except Exception:
            pass
        snap["liq_drop_pct"] = liq_drop

        opps = result.get("v2_opportunities") or []
        push_score = snap["score_push"]
        openable = [o for o in opps if getattr(o, "can_open", False)
                    and getattr(o, "score", 0) >= push_score]
        if openable:
            best = max(openable, key=lambda o: o.score)
            snap["best_opp"] = {
                "symbol": best.symbol,
                "score": best.score,
                "bid": best.bid,
                "safety_pct": best.safety_pct,
            }
        else:
            snap["best_opp"] = None

        # 下一步一句话
        if worst in ("CRITICAL", "DANGER"):
            snap["next_action"] = "处理风险持仓 → /positions 或 /hedge"
        elif snap["best_opp"]:
            snap["next_action"] = "考虑开仓 → 看推送操作卡或 /top"
        elif snap["pos_count"] > 0:
            snap["next_action"] = "持有观察 · /profit 看止盈"
        else:
            snap["next_action"] = "继续等待好机会 (安静是正确的)"
        return snap

    def _send_now(self, result: dict = None):
        from ux_copy import build_now_message
        snap = self._build_now_snapshot(result)
        self.tg.send(build_now_message(snap))

    def _cmd_mode(self, text: str):
        from ux_copy import mode_help
        parts = text.split()
        if len(parts) == 1:
            self.tg.send(mode_help(self.get_bot_mode()))
            return
        new_mode = parts[1].strip().lower()
        if new_mode not in VALID_BOT_MODES:
            self.tg.send(mode_help(self.get_bot_mode()))
            return
        runtime_config["bot_mode"] = new_mode
        try:
            self.state.save_runtime_config(runtime_config)
            self.state.save(force=True)
        except Exception as e:
            log.warning(f"保存 bot_mode 失败: {e}")
        labels = {
            "hunt": "猎机: 正常推机会+风控",
            "hold": "持仓: 抑制新开仓机会, 保留风控",
            "quiet": "安静: 仅 CRITICAL + 日报",
        }
        self.tg.send(f"✅ 模式已切换为 <b>{new_mode}</b>\n{labels[new_mode]}")

    def _cmd_positions(self):
        if not self._ensure_ready():
            return
        lines = []
        pos = self.last_result["pos_alerts"]
        total_pnl = 0
        total_theta = 0
        pos_count = 0
        if pos:
            lines.append("📋 <b>当前持仓</b>\n")
            for p in pos:
                if p.get("type") == "ERROR":
                    lines.append(f"  ❌ {p['msg']}")
                    continue
                if "pnl" not in p and p.get("type") not in (None, "POSITION"):
                    continue
                icon = {"OK": "✅", "WARNING": "⚠️", "DANGER": "🔴", "WATCH": "👀"}.get(
                    p.get("alert"), "")
                dte = p.get("dte", 0)
                theta = p.get("theta", 0)
                premium = p.get("premium_collected", 0)
                direction = p.get("direction", "Short")
                dir_tag = " (Long)" if direction == "Long" else ""
                theta_label = "进账" if theta >= 0 else "损耗"
                lines.append(
                    f"{icon} <b>{p.get('symbol', '?')}{dir_tag}</b>\n"
                    f"  数量: {p.get('qty', 0)}  入场: ${p.get('entry', 0):,.0f}  "
                    f"当前: ${p.get('mark', 0):,.0f}\n"
                    f"  盈亏: <b>${p.get('pnl', 0):+,.0f}</b> ({p.get('pnl_pct', 0):+.0f}%)  "
                    f"距行权: {p.get('dist_to_strike', 0):.1f}%\n"
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

    def _cmd_top(self, text: str):
        if not self._ensure_ready():
            return
        opps = self.last_result.get("v2_opportunities", [])
        account = self.last_result.get("account_risk")
        hv = self.last_result.get("hv_20", 0)
        iv_mean = self.last_result["iv_surface"]["global"]["mean"]
        if not opps or not account:
            self.tg.send("当前无符合条件的机会")
            return

        suffix = text[4:].strip()  # after /top
        if suffix == "all":
            msg = format_opportunities_tg(opps, account, hv, iv_mean,
                                          openable_only=False)
            self.tg.send(msg)
            return

        min_score = 0
        if suffix.isdigit():
            min_score = int(suffix)

        if min_score > 0:
            filtered = [o for o in opps if o.score >= min_score]
            if filtered:
                msg = format_opportunities_tg(
                    filtered, account, hv, iv_mean, openable_only=True)
                header = f"🔍 <b>评分 ≥{min_score} 可开仓 ({len([o for o in filtered if o.can_open])}个)</b>\n\n"
                self.tg.send(header + msg)
            else:
                self.tg.send(
                    f"当前无评分 ≥{min_score} 的机会\n\n"
                    f"👉 /top 看可开 · /top all 看全部"
                )
        else:
            msg = format_opportunities_tg(
                opps, account, hv, iv_mean, openable_only=True)
            self.tg.send(msg)

    def _cmd_hedge(self):
        if not self._ensure_ready():
            return
        if not self.last_pos_list:
            self.tg.send("📋 当前无持仓, 无需对冲")
            return
        self.tg.send("🛡️ 计算对冲方案...")
        try:
            spot = self.last_result["data"]["spot"]
            from binance_options import get_account_equity
            _acct = get_account_equity(self.api)
            balance = _acct["margin_balance"]

            if balance <= 0:
                self.tg.send("❌ 无法获取账户余额")
                return

            available_puts = self._get_hedge_candidates(self.last_result["data"])
            hedge_calc = self.hedge_advisor.calc_hedge_options(
                self.last_pos_list, spot, balance, available_puts)

            liq = hedge_calc["liq_current"]
            lines = ["🛡️ <b>对冲方案</b>\n"]
            lines.append(f"BTC ${spot:,.0f}  余额 ${balance:,.0f}")
            lines.append(f"强平价 ${liq['liq_price']:,.0f} (跌 {abs(liq['liq_drop_pct']):.0f}%)")
            lines.append(f"模式: {self.risk_mode.mode_icon}\n")

            comp = hedge_calc.get("comparison", {})
            if comp:
                lines.append("<b>$1,000 对比:</b>")
                lines.append(f"  补保证金 → 下移 ${comp['cash_1k_improve']:,.0f}")
                lines.append(f"  买 Put   → 下移 ${comp['best_put_1k_improve']:,.0f}")
                lines.append(f"  效率: 买Put = <b>{comp['ratio']:.0f}x</b>\n")

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

    # --- P2-3: /config & /set 命令 ---
    def _cmd_config(self):
        """显示所有可调参数及当前值"""
        lines = ["⚙️ <b>可调参数</b>\n"]
        for name, spec in ADJUSTABLE_PARAMS.items():
            # 当前值: runtime_config 优先, 否则用默认值
            if name in runtime_config:
                val = runtime_config[name]
                source = "✏️"  # 已调整
            else:
                val = self._get_param_default(name)
                source = "📌"  # 默认值
            lines.append(
                f"{source} <code>{name}</code> = <b>{val}</b>\n"
                f"    {spec['desc']}  [{spec['type'].__name__}  {spec['min']}~{spec['max']}]"
            )
        lines.append("\n用法: <code>/set 参数名 值</code>")
        lines.append("示例: <code>/set scan_interval 120</code>")
        self.tg.send("\n".join(lines))

    def _cmd_set(self, text: str):
        """验证并设置运行时参数"""
        global runtime_config
        parts = text.split(None, 2)  # "/set", "param", "value"
        if len(parts) < 3:
            self.tg.send("❌ 用法: <code>/set 参数名 值</code>\n\n发送 /config 查看可调参数")
            return

        param_name = parts[1].strip()
        raw_value = parts[2].strip()

        # 检查白名单
        if param_name not in ADJUSTABLE_PARAMS:
            valid = ", ".join(ADJUSTABLE_PARAMS.keys())
            self.tg.send(f"❌ 未知参数: <code>{param_name}</code>\n\n可调参数: {valid}")
            return

        spec = ADJUSTABLE_PARAMS[param_name]

        # 类型转换
        try:
            value = spec["type"](raw_value)
        except (ValueError, TypeError):
            self.tg.send(
                f"❌ 类型错误: <code>{param_name}</code> 需要 {spec['type'].__name__}\n"
                f"输入值: {raw_value}"
            )
            return

        # 范围检查
        if value < spec["min"] or value > spec["max"]:
            self.tg.send(
                f"❌ 超出范围: <code>{param_name}</code>\n"
                f"允许: {spec['min']} ~ {spec['max']}, 输入: {value}"
            )
            return

        # 保存旧值
        old_val = runtime_config.get(param_name, self._get_param_default(param_name))

        # 更新
        runtime_config[param_name] = value

        # 应用到模块级变量
        self._apply_runtime_config()

        # 持久化
        self.state.save_runtime_config(runtime_config)
        self.state.save(force=True)

        self.tg.send(
            f"✅ <b>参数已更新</b>\n\n"
            f"<code>{param_name}</code>: {old_val} → <b>{value}</b>\n"
            f"({spec['desc']})"
        )
        log.info(f"运行时调参: {param_name} = {value} (原 {old_val})")

    def _get_param_default(self, name: str):
        """获取参数的默认值"""
        defaults = {
            "scan_interval": 180,
            "overview_interval": 4 * 3600,
            "score_push": getattr(ScanConfig, "SCORE_PUSH", 78),
            "pnl_warn_ratio": 1.0,
            "pnl_danger_ratio": 2.0,
            "liq_warning_pct": 18,
            "daily_digest_hour": 0,
        }
        return defaults.get(name, "?")

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
                from binance_options import get_account_equity
                _acct = get_account_equity(self.api)
                account_balance = _acct["margin_balance"]
                if account_balance <= 0:
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

    def _build_chart_positions(self) -> list:
        """构建 price_axis_chart 需要的持仓数据格式"""
        positions_data = []
        try:
            positions = self.api.get_position()
            marks = self.last_result.get("data", {}).get("marks", {}) if self.last_result else {}
            spot = self.last_spot
            for p in positions:
                qty = float(p.get("quantity", 0))
                if qty == 0:
                    continue
                sym = p.get("symbol", "")
                strike = float(p.get("strikePrice", 0))
                entry = float(p.get("entryPrice", 0))
                mark_price = float(p.get("markPrice", 0))
                m = marks.get(sym, {})
                if mark_price <= 0:
                    mark_price = float(m.get("markPrice", 0))
                direction = "Short" if qty < 0 else "Long"
                if qty < 0:
                    pnl = (entry - mark_price) * abs(qty)
                else:
                    pnl = (mark_price - entry) * abs(qty)
                positions_data.append({
                    "symbol": sym, "strike": strike, "qty": qty,
                    "entry": entry, "mark": mark_price,
                    "pnl": round(pnl, 2), "direction": direction,
                })
        except Exception as e:
            log.warning(f"构建持仓图表数据失败: {e}")
        return positions_data

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
        """独立线程: long polling TG 消息 (timeout=25s), 秒回命令"""
        log.info("命令监听线程启动 (long polling, timeout=25s)")
        while self.running:
            try:
                self.handle_commands()
            except Exception as e:
                log.error(f"命令处理异常: {e}")
            # long poll 本身会阻塞最多 25s (有消息时立即返回)
            # 每次返回后检查 self.running, 退出响应 ≤25s (通常更快)

    def _ping_heartbeat(self):
        """外部心跳 ping (Dead Man's Switch)"""
        if not HEARTBEAT_URL:
            return
        try:
            requests.get(HEARTBEAT_URL, timeout=5)
        except Exception as e:
            log.warning(f"Heartbeat ping failed: {e}")

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
            self._ping_heartbeat()
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

                # 每日 Digest 推送
                try:
                    now_utc = datetime.now(timezone.utc)
                    today_str = now_utc.strftime("%Y-%m-%d")
                    if (now_utc.hour == DAILY_DIGEST_HOUR_UTC
                            and self.last_digest_date != today_str):
                        log.info("触发每日 Digest 推送")
                        # R4-5: 尝试发送仪表盘 PNG + 5 行 caption
                        try:
                            from digest_dashboard import generate_digest_dashboard, generate_digest_caption
                            positions_data = self._build_chart_positions()
                            liq = self.hedge_advisor.get_last_liquidation()
                            liq_price = liq.get("liq_price", 0) if liq else 0
                            liq_drop = liq.get("liq_drop_pct", -50) if liq else -50
                            daily_open = self.risk_engine.price_tracker.daily_open or 0
                            iv_history = self.state.get_iv_surface_history(hours=168)
                            journal_data = self.journal.data
                            dashboard_path = generate_digest_dashboard(
                                self.last_spot, daily_open, positions_data,
                                liq_price, iv_history, journal_data)
                            # 5 行 caption
                            theta_est = sum(abs(p.get("pnl", 0)) * 0.02 for p in positions_data if p.get("direction") == "Short")
                            from binance_options import get_account_equity
                            acct = get_account_equity(self.api)
                            margin_pct = acct.get("initial_margin", 0) / acct.get("equity", 1) * 100 if acct.get("equity", 0) > 0 else 0
                            caption = generate_digest_caption(
                                theta_daily=theta_est,
                                top_alert_verdict="",
                                top_opp_verdict="",
                                events_7d=[],
                                margin_usage_pct=margin_pct,
                                liq_drop_pct=liq_drop)
                            if dashboard_path:
                                self.tg.broadcast_photo(dashboard_path, caption=caption)
                                log.info("每日 Digest 仪表盘已推送")
                            else:
                                raise Exception("仪表盘生成失败")
                        except Exception as e:
                            log.warning(f"仪表盘生成失败, 降级文字版: {e}")
                            digest_msg = generate_daily_digest(
                                self.api, self.risk_engine, self.state)
                            self.tg.broadcast(digest_msg, silent=True)
                        self.last_digest_date = today_str
                        log.info("每日 Digest 已推送")
                except Exception as e:
                    log.error(f"每日 Digest 推送失败: {e}")

                # 定期清理
                if self.scan_count % 100 == 0:
                    self.cooldown.cleanup()

                # 外部心跳 ping
                self._ping_heartbeat()

            except Exception as e:
                log.error(f"扫描异常: {e}", exc_info=True)
                try:
                    self.tg.send(f"⚠️ 扫描异常: {e}")
                except Exception as e2:
                    log.warning(f"扫描异常后 TG 通知也失败: {e2}")
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
            f"概览推送: 每{OVERVIEW_INTERVAL // 3600}小时\n"
            f"模式: {self.get_bot_mode()}\n\n"
            "发送 /now 看决策首页 · /help 看命令"
        )

        self.first_scan_started_at = time.time()

        # 用日 K 线初始化日开盘价 (避免重启后失真)
        self.risk_engine.price_tracker.init_daily_open_from_kline()

        def handle_exit(signum, frame):
            log.info("收到退出信号, 正在关闭...")
            self.running = False

        sig.signal(sig.SIGINT, handle_exit)
        sig.signal(sig.SIGTERM, handle_exit)
        # 忽略 SIGHUP: 终端关闭时不要退出 (等同于 nohup)
        sig.signal(sig.SIGHUP, sig.SIG_IGN)

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
