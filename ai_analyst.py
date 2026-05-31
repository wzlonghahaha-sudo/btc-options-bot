#!/usr/bin/env python3
"""
AI 策略分析师 — 基于 LLM 的实时期权分析

每次 overview 推送时，将完整市场快照打包发给 LLM，生成：
  1. 市场环境判断 (多头/空头/震荡)
  2. 当前持仓评价 + 操作建议 (持有/止盈/Roll/加仓)
  3. 新开仓机会点评
  4. 风控提示

通过 OpenAI 兼容接口调用（支持 litellm 代理）。
输出控制在 Telegram 一条消息内 (< 2000 字符)。
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests
from margin_calc import calc_put_margin_per_contract

log = logging.getLogger("ai_analyst")

# ============================================================
#  配置
# ============================================================
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_BASE = os.getenv("AI_API_BASE", "")
AI_MODEL = os.getenv("AI_MODEL", "")
MAX_OUTPUT_TOKENS = 1024
TEMPERATURE = 0.3


# ============================================================
#  数据打包：把 scan result 压缩成 prompt 友好的文本
# ============================================================
def _pack_market_snapshot(result: dict, iv_tracker=None) -> str:
    """将 do_scan() 的 result dict 压缩成紧凑文本摘要"""
    data = result.get("data", {})
    iv_surface = result.get("iv_surface", {})
    pos_alerts = result.get("pos_alerts", [])
    order_alerts = result.get("order_alerts", [])
    risk_alerts = result.get("risk_alerts", [])
    v2_opps = result.get("v2_opportunities", [])
    account_risk = result.get("account_risk")
    hv_20 = result.get("hv_20", 0)

    spot = data.get("spot", 0)
    global_iv = iv_surface.get("global", {})
    mean_iv = global_iv.get("mean", 0)
    median_iv = global_iv.get("median", 0)

    lines = []

    # --- 时间 ---
    lines.append(f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # --- BTC 价格 ---
    lines.append(f"BTC 现价: ${spot:,.2f}")

    # --- IV 环境 ---
    iv_pctl = 50.0
    iv_trend = ""
    if iv_tracker:
        iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
        iv_trend = iv_tracker.get_iv_trend()
    lines.append(f"Put IV 均值: {mean_iv:.3f}  中位: {median_iv:.3f}")
    lines.append(f"IV Percentile: {iv_pctl:.0f}%  趋势: {iv_trend}")
    if hv_20 > 0:
        lines.append(f"HV(20d): {hv_20:.1f}%  IV/HV比: {mean_iv * 100 / hv_20:.2f}x")
    else:
        lines.append("HV(20d): N/A")

    # --- IV 曲面 (近期到期日) ---
    by_exp = iv_surface.get("by_exp", {})
    exps = sorted(by_exp.keys())[:4]
    if exps:
        lines.append("IV曲面(近期):")
        for exp in exps:
            s = by_exp[exp]
            lines.append(f"  {exp}: mean={s['mean']:.3f} [{s['min']:.3f}-{s['max']:.3f}]")

    # --- 账户 ---
    if account_risk:
        lines.append(f"账户: 总资金 ${account_risk.total_balance:,.0f}  "
                     f"已用保证金 ${account_risk.used_margin:,.0f}  "
                     f"使用率 {account_risk.margin_usage_pct:.1f}%")
        lines.append(f"组合 Delta: {account_risk.portfolio_delta:.3f}  "
                     f"Theta: ${account_risk.portfolio_theta:.1f}/天  "
                     f"浮盈亏: ${account_risk.total_unrealized_pnl:+,.0f}")

    # --- 当前持仓 ---
    real_pos = [p for p in pos_alerts if p.get("type") == "POSITION"]
    if real_pos:
        lines.append(f"持仓 ({len(real_pos)} 个):")
        for p in real_pos:
            dte = p.get("dte", "?")
            theta = p.get("theta", 0)
            lines.append(
                f"  {p['symbol']}: 数量={p['qty']}  入场=${p['entry']:,.0f}  "
                f"当前=${p['mark']:,.0f}  盈亏=${p['pnl']:+,.0f}({p['pnl_pct']:+.0f}%)  "
                f"距行权={p['dist_to_strike']:.1f}%  DTE={dte}天  "
                f"Theta=${theta:.1f}/天  状态={p.get('alert', 'OK')}"
            )
    else:
        lines.append("持仓: 无")

    # --- 挂单 ---
    real_orders = [o for o in order_alerts if o.get("type") == "ORDER"]
    if real_orders:
        lines.append(f"挂单 ({len(real_orders)} 个):")
        for o in real_orders:
            side = "卖" if o["side"] == "SELL" else "买"
            lines.append(
                f"  {o['symbol']}: {side} {o['qty']}张 @ ${o['price']:,.0f}  "
                f"差距={o['gap_pct']:.1f}%"
            )

    # --- 风控告警 ---
    if risk_alerts:
        serious = [a for a in risk_alerts if a.level in ("WARNING", "DANGER", "CRITICAL")]
        if serious:
            lines.append(f"风控告警 ({len(serious)} 项):")
            for a in serious[:5]:
                lines.append(f"  [{a.level}] {a.category}: {a.title} — {a.detail}")
        else:
            lines.append("风控: 正常")
    else:
        lines.append("风控: 正常")

    # --- Top 机会 (v2) ---
    if v2_opps:
        top = sorted(v2_opps, key=lambda o: o.score, reverse=True)[:5]
        lines.append(f"扫描到 {len(v2_opps)} 个机会, Top 5:")
        for o in top:
            lines.append(
                f"  {o.symbol}: 评分={o.score:.0f}  Bid=${o.bid:,.0f}  "
                f"年化={o.annual_return:.0f}%  安全垫={o.safety_pct:.0f}%  "
                f"OTM={o.otm_pct:.0f}%  IV={o.iv:.3f}  DTE={o.dte}天  "
                f"档位={o.tier_label}  可开={o.can_open}"
            )
    else:
        lines.append("机会: 无符合条件的机会")

    # --- v1 信号统计 ---
    results = result.get("results", [])
    n_strong = len([r for r in results if r.get("signal") == "STRONG"])
    n_signal = len([r for r in results if r.get("signal") == "SIGNAL"])
    n_watch = len([r for r in results if r.get("signal") == "WATCH"])
    lines.append(f"v1信号: STRONG={n_strong} SIGNAL={n_signal} WATCH={n_watch}")

    return "\n".join(lines)


# ============================================================
#  System Prompt：定义 AI 策略师的角色和输出格式
# ============================================================
SYSTEM_PROMPT = """你是一位专业的 BTC 期权卖方策略分析师。你的客户专门做"卖出深度 OTM BTC Put"策略，通过收取权利金获利。

你的角色：
- 基于实时市场数据，给出简洁、可操作的策略建议
- 你是卖方视角：高 IV 对你有利（权利金更肥），低 IV 不利
- 关注风险：距行权距离、保证金使用率、BTC 波动趋势
- 说人话，不要学术化，直接告诉客户该做什么

输出格式要求（严格遵守，Telegram 展示）：
1. 用中文
2. 总长度控制在 600 字以内
3. 分为以下几个板块，每个板块用 emoji 标题：

🔍 市场判断
(1-2句话：当前BTC走势判断 + IV环境对卖方有利/不利)

📋 持仓评价
(逐个点评现有持仓：安全吗、该不该止盈、该不该Roll。如果没有持仓写"当前无持仓")

💡 操作建议
(具体的建议：新开仓推荐哪个合约、挂单要不要调整、要不要等)

⚠️ 风险提示
(当前最大的风险是什么，1-2句话)

注意：
- 不要重复原始数据，数据已经在概览消息中展示过了
- 聚焦在"观点"和"建议"上
- 如果市场平淡无需操作，就直接说"当前无需操作，继续持有即可"
- 保守为主：宁可错过机会，不可放大风险"""


# ============================================================
#  AI 分析引擎 — 通过 OpenAI 兼容接口调用
# ============================================================
class AIAnalyst:
    """通过 OpenAI 兼容 API (litellm) 调用 LLM 生成策略分析"""

    def __init__(self, api_key: str = None, api_base: str = None, model: str = None):
        self.api_key = api_key or AI_API_KEY
        self.api_base = (api_base or AI_API_BASE).rstrip("/")
        self.model = model or AI_MODEL
        self.last_analysis_time = 0
        self.last_report = ""

        if not self.api_key or not self.api_base or not self.model:
            log.warning("AI 分析配置不完整 (AI_API_KEY/AI_API_BASE/AI_MODEL)，功能禁用")
        else:
            log.info(f"AI Analyst 初始化: model={self.model}, base={self.api_base}")

    @property
    def is_available(self) -> bool:
        return bool(self.api_key and self.api_base and self.model)

    def analyze(self, result: dict, iv_tracker=None) -> str:
        """
        基于 scan result 生成 AI 分析报告。

        Args:
            result: do_scan() 返回的完整结果 dict
            iv_tracker: IVTracker 实例

        Returns:
            格式化的 Telegram 消息文本，失败时返回空字符串
        """
        if not self.is_available:
            return ""

        t0 = time.time()

        # 1. 打包市场快照
        snapshot = _pack_market_snapshot(result, iv_tracker)

        # 2. 调用 LLM (OpenAI 兼容接口)
        try:
            url = f"{self.api_base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": TEMPERATURE,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"以下是当前市场实时数据快照，请给出分析：\n\n{snapshot}"},
                ],
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            raw_text = data["choices"][0]["message"]["content"]
            elapsed = time.time() - t0

            # 统计 token 用量
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            # 3. 包装成 TG 消息格式
            report = f"🤖 <b>AI 策略分析</b>\n\n{raw_text}"

            # 记录
            self.last_analysis_time = time.time()
            self.last_report = report
            log.info(f"AI 分析完成: {elapsed:.1f}s, "
                     f"input={input_tokens} output={output_tokens} tokens")

            return report

        except requests.exceptions.Timeout:
            log.error("AI 分析超时 (30s)")
            return ""
        except requests.exceptions.HTTPError as e:
            log.error(f"AI API 错误: {e.response.status_code} {e.response.text[:200]}")
            return ""
        except Exception as e:
            log.error(f"AI 分析失败: {e}")
            return ""

    def get_cached_report(self) -> str:
        """获取最近一次的 AI 报告 (用于 /ai 命令快速回复)"""
        if not self.last_report:
            return ""
        age_min = (time.time() - self.last_analysis_time) / 60
        return f"{self.last_report}\n\n<i>({age_min:.0f}分钟前生成)</i>"


# ============================================================
#  规则引擎分析师 — 纯代码逻辑，不依赖 AI API
# ============================================================
class RuleBasedAnalyst:
    """基于规则和决策矩阵的策略分析，作为 LLM 的 fallback"""

    def __init__(self):
        self.last_analysis_time = 0
        self.last_report = ""
        log.info("规则引擎分析师初始化 (无需 AI API)")

    @property
    def is_available(self) -> bool:
        return True

    def analyze(self, result: dict, iv_tracker=None) -> str:
        t0 = time.time()
        try:
            report = self._generate(result, iv_tracker)
            self.last_analysis_time = time.time()
            self.last_report = report
            log.info(f"规则引擎分析完成: {time.time() - t0:.2f}s")
            return report
        except Exception as e:
            log.error(f"规则引擎分析失败: {e}")
            return ""

    def get_cached_report(self) -> str:
        if not self.last_report:
            return ""
        age_min = (time.time() - self.last_analysis_time) / 60
        return f"{self.last_report}\n\n<i>({age_min:.0f}分钟前生成)</i>"

    def _generate(self, result: dict, iv_tracker=None) -> str:
        data = result.get("data", {})
        iv_surface = result.get("iv_surface", {})
        pos_alerts = result.get("pos_alerts", [])
        order_alerts = result.get("order_alerts", [])
        risk_alerts = result.get("risk_alerts", [])
        v2_opps = result.get("v2_opportunities", [])
        account_risk = result.get("account_risk")
        hv_20 = result.get("hv_20", 0)

        spot = data.get("spot", 0)
        global_iv = iv_surface.get("global", {})
        mean_iv = global_iv.get("mean", 0)

        iv_pctl = 50.0
        iv_trend_str = ""
        if iv_tracker:
            iv_pctl = iv_tracker.get_iv_percentile(mean_iv)
            iv_trend_str = iv_tracker.get_iv_trend()

        iv_hv_ratio = (mean_iv * 100 / hv_20) if hv_20 > 0 else 1.0

        lines = ["🤖 <b>AI 策略分析</b>\n"]

        # ─── 1. 市场判断 ───
        lines.append("🔍 <b>市场判断</b>")
        market_lines = []

        # BTC 趋势
        if iv_trend_str:
            if "急升" in iv_trend_str or "急跌" in iv_trend_str:
                market_lines.append(f"IV 近期变化剧烈 ({iv_trend_str})，市场情绪不稳定。")
            elif "下降" in iv_trend_str:
                market_lines.append(f"IV 在回落 ({iv_trend_str})，市场趋于平静。")

        # IV 环境评价
        if iv_pctl < 20:
            market_lines.append(f"IV Percentile 仅 {iv_pctl:.0f}%，处于历史低位，权利金偏薄，不是卖方的好时机。建议观望为主。")
        elif iv_pctl < 40:
            market_lines.append(f"IV Percentile {iv_pctl:.0f}%，略偏低，卖方优势不明显，谨慎开新仓。")
        elif iv_pctl > 80:
            market_lines.append(f"IV Percentile {iv_pctl:.0f}%，处于历史高位！权利金非常肥厚，卖方黄金窗口。")
        elif iv_pctl > 60:
            market_lines.append(f"IV Percentile {iv_pctl:.0f}%，偏高，卖方有优势，可以积极寻找机会。")
        else:
            market_lines.append(f"IV Percentile {iv_pctl:.0f}%，处于中性区间，无明显方向偏好。")

        # IV/HV 比较
        if iv_hv_ratio > 1.3:
            market_lines.append(f"IV/HV 比 {iv_hv_ratio:.1f}x，隐含波动率显著高于实际波动，卖方有 alpha。")
        elif iv_hv_ratio < 0.8:
            market_lines.append(f"IV/HV 比 {iv_hv_ratio:.1f}x，隐含波动率低于实际波动，卖方注意风险。")

        lines.append(" ".join(market_lines) if market_lines else "市场环境中性，无明显异常。")

        # ─── 2. 持仓评价 + 优化决策 ───
        lines.append("")
        lines.append("📋 <b>持仓评价</b>")
        real_pos = [p for p in pos_alerts if p.get("type") == "POSITION"]

        # 构建排除集合: 已持有 + 已挂单的合约
        held_symbols = {p["symbol"] for p in real_pos}
        real_orders = [o for o in order_alerts if o.get("type") == "ORDER"]
        order_symbols = {o["symbol"] for o in real_orders}
        exclude_symbols = held_symbols | order_symbols

        # 已持有的到期月份 (用于到期分散评分)
        held_expiries = set()
        for p in real_pos:
            parts = p["symbol"].split("-")
            if len(parts) >= 2:
                held_expiries.add(parts[1][:4])  # 取 YYMM, 如 "2607"

        # 预处理候选机会池: 排除已持有/挂单, 按评分排序
        candidate_opps = [o for o in v2_opps
                          if o.symbol not in exclude_symbols and o.score >= 65]
        candidate_opps.sort(key=lambda o: o.score, reverse=True)

        # 跟踪已推荐的机会 (避免同一合约推给多个持仓)
        used_opp_symbols = set()
        # 收集操作建议 (用于操作建议板块)
        position_actions = []  # [(type, short_sym, close_reason, roll_detail_dict_or_None)]

        if not real_pos:
            lines.append("当前无持仓。")
        else:
            total_pnl = 0
            for p in real_pos:
                sym = p["symbol"]
                pnl = p.get("pnl", 0)
                pnl_pct = p.get("pnl_pct", 0)
                dist = p.get("dist_to_strike", 0)
                dte = p.get("dte", 0)
                theta = p.get("theta", 0)
                mark = p.get("mark", 0)
                alert = p.get("alert", "OK")
                abs_qty = abs(p.get("qty", 0))
                total_pnl += pnl

                short_sym = sym.split("BTC-")[-1] if "BTC-" in sym else sym

                # 解析旧仓到期月
                old_parts = sym.split("-")
                old_exp_ym = old_parts[1][:4] if len(old_parts) >= 2 else ""
                old_strike = float(old_parts[2]) if len(old_parts) >= 4 else 0

                # 计算旧仓关键指标
                remaining_value = mark * abs_qty
                old_margin = calc_put_margin_per_contract(spot, old_strike) * abs_qty if spot > 0 else 1
                old_theta_on_margin = theta / max(old_margin, 1) * 100  # %/天
                # theta 年化效率: %/天 × 365
                theta_annual = old_theta_on_margin * 365

                # === 平仓条件检查 ===
                # 核心原则: theta 还在高效赚钱的仓位不轻易平掉
                # 阈值: theta/保证金 > 0.05%/天 (~18%年化) 视为"仍有效率"
                THETA_EFF_FLOOR = 0.05  # %/天
                theta_still_efficient = old_theta_on_margin >= THETA_EFF_FLOOR

                should_close = False
                close_reason = ""

                # 条件1: 危险状态 → 无条件平仓 (安全优先, 不看theta)
                if alert == "DANGER":
                    should_close = True
                    close_reason = "持仓处于危险状态，建议止损"

                # 条件2: 风险回报倒挂 → 无条件平仓 (冒大风险赚小钱)
                elif dist < 20 and pnl_pct < 30:
                    should_close = True
                    close_reason = f"距行权仅 {dist:.0f}% 但盈利仅 {pnl_pct:.0f}%，风险回报已倒挂"

                # 以下条件受 theta 效率门槛保护:
                # 如果 theta 还在高效赚钱 → 跳过，继续持有

                # 条件3: 盈利 > 60% 且 theta 效率已下降
                elif pnl_pct > 60 and not theta_still_efficient:
                    should_close = True
                    close_reason = (
                        f"盈利 {pnl_pct:.0f}% 且 theta 效率已降至 "
                        f"{theta_annual:.0f}%年化，剩余 ${remaining_value:,.0f} 收割空间有限"
                    )

                # 条件4: 盈利 > 80% → 即使 theta 还行也考虑平 (剩余空间太小)
                elif pnl_pct > 80:
                    should_close = True
                    close_reason = f"盈利已达 {pnl_pct:.0f}%，仅剩 ${remaining_value:,.0f} 未收割"

                # 条件5: 快到期 + 盈利 + theta 效率已降
                elif dte < 14 and pnl_pct > 40 and not theta_still_efficient:
                    should_close = True
                    close_reason = f"DTE {dte}天 + 盈利 {pnl_pct:.0f}% + theta 效率已低"

                # 条件6: IV 极低位 + theta 效率低
                elif iv_pctl < 15 and pnl_pct > 20 and not theta_still_efficient:
                    should_close = True
                    close_reason = f"IV 极低 ({iv_pctl:.0f}%)，theta 效率仅 {theta_annual:.0f}%年化"

                # === Roll 候选合约评分 ===
                roll_picks = []  # [(适配分, opp, 理由列表)]
                if should_close and iv_pctl >= 20:
                    for opp in candidate_opps:
                        if opp.symbol in used_opp_symbols:
                            continue

                        # 硬门槛: 安全垫必须 >= 旧仓-5% 或绝对 >= 25%
                        if opp.safety_pct < max(dist - 5, 25):
                            continue

                        # --- 计算 Roll 适配分 ---
                        reasons = []

                        # (1) 基础分 50%: 直接用 v2 评分 (已含安全/IV/收益/流动性)
                        base_score = opp.score  # 0-100

                        # (2) 到期分散 20%: 跟旧仓不同月+跟其他持仓不同月
                        opp_parts = opp.symbol.split("-")
                        opp_exp_ym = opp_parts[1][:4] if len(opp_parts) >= 2 else ""
                        exp_score = 0
                        if opp_exp_ym != old_exp_ym:
                            exp_score += 50
                            reasons.append("到期更远")
                        other_held = held_expiries - {old_exp_ym}
                        if opp_exp_ym not in other_held:
                            exp_score += 50
                            reasons.append("分散到期")

                        # (3) Theta 效率对比 20%: 新仓 theta/保证金 vs 旧仓残余
                        new_theta_daily = opp.bid / max(opp.dte, 1)
                        new_margin = calc_put_margin_per_contract(spot, opp.strike)
                        new_theta_on_margin = new_theta_daily / max(new_margin, 1) * 100
                        if old_theta_on_margin > 0:
                            theta_ratio = new_theta_on_margin / old_theta_on_margin
                        else:
                            theta_ratio = 2.0  # 旧仓几乎无theta, 新仓肯定更好
                        theta_score = min(theta_ratio / 3.0 * 100, 100)  # 3x 以上满分
                        if theta_ratio > 1.5:
                            reasons.append(f"theta效率 {theta_ratio:.1f}x")
                        elif theta_ratio > 1.0:
                            reasons.append("theta效率略优")

                        # (4) 安全垫提升 10%: 新仓安全垫 vs 旧仓
                        safety_delta = opp.safety_pct - dist
                        safety_score = min(max(safety_delta + 10, 0) / 20 * 100, 100)  # +10% 以上满分
                        if safety_delta > 5:
                            reasons.append(f"安全垫提升 +{safety_delta:.0f}%")
                        elif safety_delta > 0:
                            reasons.append(f"安全垫 +{safety_delta:.0f}%")

                        # 加权总分
                        fit_score = (base_score * 0.50
                                     + exp_score * 0.20
                                     + theta_score * 0.20
                                     + safety_score * 0.10)

                        roll_picks.append((fit_score, opp, reasons))

                    # 按适配分排序, 取 top 2
                    roll_picks.sort(key=lambda x: x[0], reverse=True)

                # === 生成评价文本 ===
                if should_close and roll_picks:
                    best_fit, best_opp, best_reasons = roll_picks[0]
                    used_opp_symbols.add(best_opp.symbol)
                    best_short = best_opp.symbol.split("BTC-")[-1]
                    new_theta_d = best_opp.bid / max(best_opp.dte, 1)
                    est_premium = best_opp.bid * abs_qty

                    lines.append(f"• 🔄 <b>{short_sym}</b>")
                    lines.append(f"  {close_reason}")
                    lines.append(f"  <b>首选 → 卖 {best_short}  {abs_qty}张 @ ${best_opp.bid:,.0f}</b>")
                    lines.append(
                        f"  评分 {best_opp.score:.0f} | 年化 {best_opp.annual_return:.0f}% | "
                        f"安全垫 {best_opp.safety_pct:.0f}% | DTE {best_opp.dte}天"
                    )
                    lines.append(
                        f"  预计权利金 ${est_premium:,.0f} | "
                        f"Theta ~${new_theta_d * abs_qty:.1f}/天"
                    )
                    if best_reasons:
                        lines.append(f"  理由: {' + '.join(best_reasons)}")

                    # 备选
                    alt_detail = None
                    if len(roll_picks) >= 2:
                        alt_fit, alt_opp, alt_reasons = roll_picks[1]
                        if alt_fit >= best_fit * 0.85:  # 备选不能比首选差太多
                            alt_short = alt_opp.symbol.split("BTC-")[-1]
                            alt_premium = alt_opp.bid * abs_qty
                            lines.append(
                                f"  备选 → 卖 {alt_short}  @ ${alt_opp.bid:,.0f}  "
                                f"评分 {alt_opp.score:.0f} | 安全垫 {alt_opp.safety_pct:.0f}%"
                            )
                            alt_detail = {"sym": alt_short, "opp": alt_opp}

                    position_actions.append(("ROLL", short_sym, close_reason, {
                        "primary": best_opp, "primary_short": best_short,
                        "qty": abs_qty, "reasons": best_reasons,
                        "alt": alt_detail,
                    }))

                elif should_close:
                    lines.append(f"• 💰 <b>{short_sym}</b>")
                    lines.append(f"  建议平仓: {close_reason}")
                    if iv_pctl < 20:
                        lines.append("  IV 低位，平仓后暂不开新仓")
                    else:
                        lines.append("  暂无合适替代 (评分均 &lt;65)，纯平仓收回保证金")
                    position_actions.append(("CLOSE", short_sym, close_reason, None))

                else:
                    # 继续持有
                    status_parts = []
                    if alert == "WARNING":
                        status_parts.append("⚠️ 需关注")
                    if pnl_pct > 30:
                        status_parts.append(f"盈利 {pnl_pct:.0f}%")
                    elif pnl_pct > 0:
                        status_parts.append(f"小幅盈利 {pnl_pct:.0f}%")
                    elif pnl_pct > -50:
                        status_parts.append(f"浮亏 {pnl_pct:.0f}%，可控")
                    else:
                        status_parts.append(f"浮亏 {pnl_pct:.0f}%")

                    if dist > 30:
                        status_parts.append("距行权远")
                    elif dist > 20:
                        status_parts.append(f"距行权 {dist:.0f}%")
                    else:
                        status_parts.append(f"距行权仅 {dist:.0f}%")

                    if dte < 14:
                        status_parts.append(f"DTE {dte}天，快到期")
                    elif dte < 30:
                        status_parts.append(f"DTE {dte}天")

                    lines.append(f"• ✅ {short_sym}: {'，'.join(status_parts)}，继续持有")
                    position_actions.append(("HOLD", short_sym, "", None))

            # 总评
            n_roll = sum(1 for a in position_actions if a[0] == "ROLL")
            n_close = sum(1 for a in position_actions if a[0] == "CLOSE")
            n_hold = sum(1 for a in position_actions if a[0] == "HOLD")

            if n_roll + n_close > 0:
                summary_parts = []
                if n_roll:
                    summary_parts.append(f"🔄 Roll {n_roll} 个")
                if n_close:
                    summary_parts.append(f"💰 平仓 {n_close} 个")
                if n_hold:
                    summary_parts.append(f"✅ 持有 {n_hold} 个")
                lines.append(f"总结: {' | '.join(summary_parts)}  总浮盈 ${total_pnl:+,.0f}")
            elif total_pnl > 0:
                lines.append(f"整体持仓健康，总浮盈 ${total_pnl:+,.0f}，全部继续持有。")
            else:
                lines.append(f"总浮盈亏 ${total_pnl:+,.0f}，暂无需调整。")

        # ─── 3. 操作建议 ───
        lines.append("")
        lines.append("💡 <b>操作建议</b>")

        action_lines = []

        # 汇总持仓操作 (带具体指令)
        for act_type, sym, reason, detail in position_actions:
            if act_type == "ROLL" and detail:
                opp = detail["primary"]
                qty = detail["qty"]
                p_short = detail["primary_short"]
                est_prem = opp.bid * qty
                action_lines.append(
                    f"平 {sym} → 卖 {p_short} {qty}张 @ ${opp.bid:,.0f} "
                    f"(权利金 ~${est_prem:,.0f})"
                )
            elif act_type == "CLOSE":
                action_lines.append(f"平仓 {sym}，锁定利润")

        # 检查挂单
        for o in real_orders:
            o_short = o['symbol'].split('BTC-')[-1]
            if o.get("gap_pct", 100) <= 5:
                action_lines.append(f"挂单 {o_short} 接近成交 ({o['gap_pct']:.0f}%)，保持耐心。")
            elif o.get("gap_pct", 0) > 25:
                action_lines.append(f"挂单 {o_short} 距成交较远 ({o['gap_pct']:.0f}%)，可考虑调整价格。")

        # 新开仓机会 (排除已用于 Roll + 已持有 + 已挂单)
        all_excluded = used_opp_symbols | exclude_symbols
        remaining_opps = [o for o in candidate_opps if o.symbol not in all_excluded]
        if remaining_opps:
            best = remaining_opps[0]
            if best.score >= 80 and iv_pctl >= 20:
                b_short = best.symbol.split("BTC-")[-1]
                action_lines.append(
                    f"新开仓: {b_short}，评分 {best.score:.0f}，"
                    f"年化 {best.annual_return:.0f}%，安全垫 {best.safety_pct:.0f}%"
                )
            elif best.score >= 65 and not any(a[0] in ("ROLL", "CLOSE") for a in position_actions):
                action_lines.append(
                    f"有中等机会 (最高评分 {best.score:.0f})，尚未达到强信号，继续等待。"
                )

        # IV 低位
        if iv_pctl < 20:
            action_lines.append("IV 历史低位，不建议开新仓。")

        # 保证金
        if account_risk and account_risk.margin_usage_pct > 50:
            action_lines.append(f"保证金使用率 {account_risk.margin_usage_pct:.0f}%，偏高，避免加仓。")

        if not action_lines:
            action_lines.append("当前无需操作，继续持有即可。")

        for al in action_lines:
            lines.append(f"• {al}")

        # ─── 4. 风险提示 ───
        lines.append("")
        lines.append("⚠️ <b>风险提示</b>")

        risk_lines = []
        serious_alerts = [a for a in risk_alerts if a.level in ("WARNING", "DANGER", "CRITICAL")]
        if serious_alerts:
            for a in serious_alerts[:3]:
                risk_lines.append(f"{a.title}: {a.detail}")

        # 位置风险
        danger_pos = [p for p in real_pos if p.get("dist_to_strike", 100) < 18]
        if danger_pos:
            risk_lines.append(f"{len(danger_pos)} 个持仓距行权价不足 18%，需密切关注。")

        # IV 快速变化
        if iv_trend_str and "急升" in iv_trend_str:
            risk_lines.append("IV 急升通常伴随 BTC 下跌，注意持仓安全。")

        if not risk_lines:
            risk_lines.append("当前无重大风险，各项指标正常。保持常规监控即可。")

        lines.append(" ".join(risk_lines))

        return "\n".join(lines)


# ============================================================
#  智能分析师 — 自动选择 LLM 或规则引擎
# ============================================================
class SmartAnalyst:
    """优先用 LLM，不可用时自动 fallback 到规则引擎。连续失败后自动禁用 LLM。"""

    def __init__(self):
        self.llm = AIAnalyst()
        self.rules = RuleBasedAnalyst()
        self._use_llm = self.llm.is_available
        self._llm_fail_count = 0
        self._llm_disabled_until = 0  # 禁用到这个时间戳
        if self._use_llm:
            log.info("AI 分析: 使用 LLM 模式")
        else:
            log.info("AI 分析: 使用规则引擎模式 (LLM 未配置)")

    @property
    def is_available(self) -> bool:
        return True  # 规则引擎始终可用

    @property
    def active_engine(self) -> str:
        if self._use_llm and time.time() >= self._llm_disabled_until:
            return "LLM"
        return "规则引擎"

    def analyze(self, result: dict, iv_tracker=None) -> str:
        # 优先 LLM (如果未被临时禁用)
        if self._use_llm and time.time() >= self._llm_disabled_until:
            report = self.llm.analyze(result, iv_tracker)
            if report:
                self._llm_fail_count = 0  # 成功，重置计数
                return report

            # LLM 失败
            self._llm_fail_count += 1
            if self._llm_fail_count >= 3:
                # 连续失败 3 次，禁用 1 小时后再试
                self._llm_disabled_until = time.time() + 3600
                log.warning(f"LLM 连续失败 {self._llm_fail_count} 次，禁用 1 小时，使用规则引擎")
            else:
                log.warning(f"LLM 分析失败 ({self._llm_fail_count}/3)，降级到规则引擎")

        return self.rules.analyze(result, iv_tracker)

    def get_cached_report(self) -> str:
        if self._use_llm and self.llm.last_report:
            return self.llm.get_cached_report()
        return self.rules.get_cached_report()

    @property
    def last_analysis_time(self) -> float:
        if self._use_llm and self.llm.last_analysis_time > self.rules.last_analysis_time:
            return self.llm.last_analysis_time
        return self.rules.last_analysis_time
