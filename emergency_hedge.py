"""
应急自动对冲模块 (P0-12)

在强平距离极度危险 (<15%) 且人工无响应时, 自动买入保护性 Long Put。

========== 安全设计 ==========
1. 默认关闭 (EMERGENCY_AUTO_HEDGE=false), 必须显式 opt-in
2. 仅买保护性 Long Put — 永不卖出, 永不平仓
3. 每 24 小时最多执行 1 次
4. 预算硬上限: EMERGENCY_MAX_HEDGE_COST_USDT (默认 $1000)
5. 需等待 ACK_TIMEOUT 分钟人工无确认才执行
6. 每次动作前后均推送 TG 消息
7. 所有动作以 ERROR 级别写入日志 (审计追踪)
8. place_order 前断言 side == 'BUY' (编译时安全网)

触发条件 (全部满足才执行):
  a. EMERGENCY_AUTO_HEDGE=true
  b. 强平距离 < 15% (CRITICAL)
  c. 已发送 TG CRITICAL 告警, 等待 ACK_TIMEOUT 分钟无人确认
  d. 24 小时内未执行过自动对冲

本模块全部代码集中在此文件, 便于审计。
"""

import os
import time
import logging
from datetime import datetime, timezone

from hedge_advisor import HedgeAdvisor

log = logging.getLogger("emergency_hedge")

# 24 小时 (秒)
_24H = 86400


class EmergencyHedge:
    """
    应急自动对冲控制器

    Args:
        api: BinanceOptionsAPI 实例 (用于下单)
        tg_send_func: async/sync 函数, 签名 tg_send_func(text) 推送 TG 消息
        state_persistence: StatePersistence 实例 (读写 bot_state.json)
    """

    def __init__(self, api, tg_send_func, state_persistence):
        # --- 从 .env 读取配置 ---
        self.enabled = os.getenv("EMERGENCY_AUTO_HEDGE", "false").lower() == "true"
        self.max_cost = float(os.getenv("EMERGENCY_MAX_HEDGE_COST_USDT", "1000"))
        self.ack_timeout = int(os.getenv("EMERGENCY_ACK_TIMEOUT_MIN", "15"))

        self.api = api
        self.tg_send = tg_send_func
        self.state = state_persistence

        # 内部工具
        self._hedge_advisor = HedgeAdvisor()

        # 运行时状态 (从 state_persistence 恢复)
        self._load_state()

        if self.enabled:
            log.error(
                "[AUDIT] EmergencyHedge ENABLED — max_cost=$%.0f, "
                "ack_timeout=%d min, 24h cooldown enforced",
                self.max_cost, self.ack_timeout,
            )
        else:
            log.info("EmergencyHedge disabled (opt-in via EMERGENCY_AUTO_HEDGE=true)")

    # ================================================================
    #  状态持久化
    # ================================================================

    def _load_state(self):
        """从 bot_state.json 恢复状态"""
        eh = self.state.data.get("emergency_hedge", {})
        self.last_auto_hedge_time = eh.get("last_auto_hedge_time", 0)
        self.pending_emergency_alert_time = eh.get("pending_emergency_alert_time", 0)
        self.emergency_acked = eh.get("emergency_acked", False)

    def _save_state(self):
        """保存状态到 bot_state.json"""
        self.state.data["emergency_hedge"] = {
            "last_auto_hedge_time": self.last_auto_hedge_time,
            "pending_emergency_alert_time": self.pending_emergency_alert_time,
            "emergency_acked": self.emergency_acked,
        }
        self.state.save(force=True)

    # ================================================================
    #  公开接口
    # ================================================================

    def record_ack(self):
        """
        用户点击 TG "I've handled it" 按钮后调用。
        重置待处理状态, 阻止自动对冲。
        """
        log.error("[AUDIT] User ACKed emergency alert — auto-hedge cancelled")
        self.pending_emergency_alert_time = 0
        self.emergency_acked = True
        self._save_state()

    def record_critical_alert(self):
        """
        当系统发送 CRITICAL 强平告警时调用。
        记录告警时间, 启动 ACK 倒计时。

        仅在无 pending alert 时设置 (避免反复重置倒计时)。
        """
        if self.pending_emergency_alert_time > 0:
            return  # 已有 pending, 不重置

        now = time.time()
        self.pending_emergency_alert_time = now
        self.emergency_acked = False
        self._save_state()
        log.error(
            "[AUDIT] CRITICAL alert sent, ACK countdown started (%d min)",
            self.ack_timeout,
        )

    def check_and_act(
        self,
        liq_drop_pct: float,
        pos_list: list,
        spot: float,
        account_balance: float,
        available_puts: list,
        marks: dict,
    ) -> dict | None:
        """
        核心检查方法 — 在每次扫描循环中调用。

        Args:
            liq_drop_pct: 当前强平距离 (负数, 如 -12 表示跌 12% 触发强平)
            pos_list: 当前持仓列表 (hedge_advisor 格式)
            spot: BTC 现价
            account_balance: 账户余额 (marginBalance)
            available_puts: 可买的 Put 列表
            marks: 标记价格 dict

        Returns:
            None 如果未执行, 或 {'order': ..., 'put': ...} 执行结果
        """
        # ----- Gate 1: 功能是否启用 -----
        if not self.enabled:
            return None

        # ----- Gate 2: 强平距离是否 CRITICAL (<15%) -----
        drop = abs(liq_drop_pct)
        if drop >= 15:
            # 安全: 如果脱离危险区, 重置 pending 状态
            if self.pending_emergency_alert_time > 0:
                log.info(
                    "Liquidation distance recovered to %.0f%%, "
                    "clearing pending emergency alert", drop
                )
                self.pending_emergency_alert_time = 0
                self.emergency_acked = False
                self._save_state()
            return None

        # ----- Gate 3: 是否有 pending alert 且超时无 ACK -----
        if self.pending_emergency_alert_time <= 0:
            return None  # 没有发送过 CRITICAL alert

        if self.emergency_acked:
            return None  # 用户已确认

        elapsed_min = (time.time() - self.pending_emergency_alert_time) / 60
        if elapsed_min < self.ack_timeout:
            log.info(
                "Waiting for human ACK: %.1f / %d min elapsed",
                elapsed_min, self.ack_timeout,
            )
            return None

        # ----- Gate 4: 24h 冷却 -----
        now = time.time()
        if now - self.last_auto_hedge_time < _24H:
            remaining_h = (_24H - (now - self.last_auto_hedge_time)) / 3600
            log.warning(
                "24h cooldown active — last auto-hedge %.1f hours ago, "
                "%.1f hours remaining", (now - self.last_auto_hedge_time) / 3600,
                remaining_h,
            )
            return None

        # ===== 所有 Gate 通过, 执行自动对冲 =====
        log.error(
            "[AUDIT] All gates passed — executing emergency auto-hedge. "
            "liq_drop=%.1f%%, spot=$%.0f, ack_elapsed=%.0f min",
            drop, spot, elapsed_min,
        )

        return self._execute_hedge(pos_list, spot, account_balance,
                                   available_puts, marks)

    # ================================================================
    #  内部: 执行对冲
    # ================================================================

    def _execute_hedge(
        self,
        pos_list: list,
        spot: float,
        account_balance: float,
        available_puts: list,
        marks: dict,
    ) -> dict | None:
        """
        计算最优保护性 Long Put 并下单。

        安全保证:
        - 只买 (side='BUY'), assert 检查
        - 成本不超过 max_cost
        - 失败时不重试 (交给下个扫描周期)
        """
        try:
            # 1. 用 hedge_advisor 计算最优 Put
            hedge_calc = self._hedge_advisor.calc_hedge_options(
                pos_list, spot, account_balance, available_puts
            )

            # 在预算范围内找最佳 Put
            best_put = self._find_best_within_budget(hedge_calc)
            if not best_put:
                msg = (
                    "🚨🤖 <b>[应急对冲] 未找到合适的保护性 Put</b>\n\n"
                    f"预算上限: ${self.max_cost:,.0f}\n"
                    f"可用 Put 数量: {len(available_puts)}\n\n"
                    "⚠️ 请立即手动处理!"
                )
                self._tg_send_safe(msg)
                log.error("[AUDIT] No suitable Put found within budget $%.0f",
                          self.max_cost)
                return None

            symbol = best_put["symbol"]
            qty = best_put["qty"]
            ask_price = best_put["ask"]
            cost = best_put["cost"]
            short_sym = symbol.split("BTC-")[-1]

            # 2. 预执行 TG 通知
            pre_msg = (
                "🚨🤖 <b>[应急对冲] 即将自动执行</b>\n\n"
                f"动作: <b>BUY Long Put</b>\n"
                f"合约: {short_sym}\n"
                f"数量: {qty:.1f} 张\n"
                f"价格: ${ask_price:,.0f}\n"
                f"预计成本: ${cost:,.0f} (上限 ${self.max_cost:,.0f})\n\n"
                f"触发原因: 强平告警超时 {self.ack_timeout} 分钟无人确认\n"
                f"BTC 现价: ${spot:,.0f}"
            )
            self._tg_send_safe(pre_msg)
            log.error(
                "[AUDIT] About to BUY %s x%.1f @ $%.0f (cost=$%.0f)",
                symbol, qty, ask_price, cost,
            )

            # 3. 安全断言: 只允许 BUY
            side = "BUY"
            assert side == "BUY", f"FATAL: side must be BUY, got {side}"

            # 4. 成本硬检查
            if cost > self.max_cost:
                log.error(
                    "[AUDIT] BLOCKED: cost $%.0f exceeds max $%.0f",
                    cost, self.max_cost,
                )
                self._tg_send_safe(
                    f"🚨🤖 <b>[应急对冲] 已拦截</b>\n\n"
                    f"成本 ${cost:,.0f} 超出预算上限 ${self.max_cost:,.0f}\n"
                    f"请手动处理!"
                )
                return None

            # 5. 下单
            order_result = self.api.place_order(
                symbol=symbol,
                side=side,
                type_="LIMIT",
                quantity=qty,
                price=ask_price,
                time_in_force="IOC",  # 立即成交或取消, 不留挂单
            )

            # 6. 记录成功
            order_id = order_result.get("orderId", "unknown")
            status = order_result.get("status", "unknown")
            self.last_auto_hedge_time = time.time()
            self.pending_emergency_alert_time = 0  # 重置
            self.emergency_acked = False
            self._save_state()

            # 7. 后执行 TG 通知
            post_msg = (
                "🚨🤖 <b>[应急对冲] 已执行!</b>\n\n"
                f"订单号: <code>{order_id}</code>\n"
                f"状态: {status}\n"
                f"动作: BUY {short_sym} x{qty:.1f}\n"
                f"价格: ${ask_price:,.0f}\n"
                f"成本: ${cost:,.0f}\n\n"
                f"下次自动对冲最早: 24小时后\n"
                f"⚠️ 请尽快检查持仓!"
            )
            self._tg_send_safe(post_msg)
            log.error(
                "[AUDIT] Order executed: orderId=%s status=%s "
                "symbol=%s qty=%.1f price=%.0f cost=%.0f",
                order_id, status, symbol, qty, ask_price, cost,
            )

            return {
                "order": order_result,
                "put": best_put,
                "cost": cost,
                "timestamp": time.time(),
            }

        except Exception as e:
            log.error("[AUDIT] Emergency hedge FAILED: %s", e, exc_info=True)
            self._tg_send_safe(
                f"🚨🤖 <b>[应急对冲] 执行失败!</b>\n\n"
                f"错误: <code>{e}</code>\n\n"
                f"⚠️ 请立即手动处理!"
            )
            return None

    def _find_best_within_budget(self, hedge_calc: dict) -> dict | None:
        """
        从 hedge_advisor 计算结果中, 找到预算内最优的 Put。

        选择逻辑: 在 max_cost 以内, 选 improve 最大的。
        """
        best_by_budget = hedge_calc.get("best_by_budget", {})
        if not best_by_budget:
            return None

        # 按预算从高到低找, 但不超过 max_cost
        candidates = []
        for budget, put in best_by_budget.items():
            if put["cost"] <= self.max_cost:
                candidates.append(put)

        if not candidates:
            return None

        # 选 improve 最大的
        return max(candidates, key=lambda p: p.get("improve", 0))

    def _tg_send_safe(self, text: str):
        """安全地发送 TG 消息, 不抛异常"""
        try:
            self.tg_send(text)
        except Exception as e:
            log.error("Failed to send TG message: %s", e)

    # ================================================================
    #  状态查询 (供 /status 命令使用)
    # ================================================================

    def get_status(self) -> str:
        """返回当前应急对冲状态的可读文本"""
        if not self.enabled:
            return "🔒 应急自动对冲: 已关闭"

        lines = ["🤖 <b>应急自动对冲: 已启用</b>"]
        lines.append(f"  预算上限: ${self.max_cost:,.0f}")
        lines.append(f"  ACK 超时: {self.ack_timeout} 分钟")

        if self.last_auto_hedge_time > 0:
            elapsed_h = (time.time() - self.last_auto_hedge_time) / 3600
            cooldown_remaining = max(0, 24 - elapsed_h)
            ts = datetime.fromtimestamp(
                self.last_auto_hedge_time, tz=timezone.utc
            ).strftime("%m-%d %H:%M UTC")
            lines.append(f"  上次执行: {ts} ({elapsed_h:.1f}h 前)")
            if cooldown_remaining > 0:
                lines.append(f"  冷却剩余: {cooldown_remaining:.1f}h")
            else:
                lines.append("  冷却状态: ✅ 可执行")
        else:
            lines.append("  上次执行: 从未")

        if self.pending_emergency_alert_time > 0 and not self.emergency_acked:
            elapsed_min = (time.time() - self.pending_emergency_alert_time) / 60
            lines.append(f"  ⚠️ 待处理告警: {elapsed_min:.0f}/{self.ack_timeout} 分钟")
        elif self.emergency_acked:
            lines.append("  ✅ 用户已确认处理")
        else:
            lines.append("  状态: 待命")

        return "\n".join(lines)
