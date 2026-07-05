# BTC Options Bot — OTM Put 卖方监控与告警系统

## 架构概览

```
用户 ←→ Telegram Bot ←→ MonitorService (主循环)
                              ├── BinanceOptionsAPI (数据获取)
                              ├── RiskEngine (风控引擎)
                              │     ├── PriceTracker (价格追踪)
                              │     ├── StressTest (压力测试)
                              │     └── LiquidationEstimator (强平估算)
                              ├── OpportunityScanner (机会扫描)
                              ├── SellPutStrategy (V1评分)
                              ├── ProfitOptimizer (止盈/止损)
                              ├── HedgeAdvisor (对冲顾问)
                              ├── TradeJournal (交易日志)
                              ├── IVTracker (IV可视化)
                              └── EmergencyHedge (应急对冲, opt-in)
```

## 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| Telegram Bot | `tg_bot_monitor.py` | 主循环入口, 定时扫描 + TG 命令交互 + 信号推送去重 |
| Binance API | `binance_options.py` | 币安欧式期权数据获取 (行情/持仓/下单), 公开+私有接口 |
| 风控监控 | `risk_monitor.py` | 多维风控 (浮亏/爆仓/波动/希腊值/到期), 五级告警 |
| 止损规则 | `risk_rules.py` | 统一复合止损 (浮亏倍数 + 距行权距离 + delta), 单一事实来源 |
| 保证金计算 | `margin_calc.py` | 保证金公式 + BS 定价 + 强平价格反算 + 压力测试 |
| 压力测试 | `stress_test.py` | BTC 暴跌 + IV 暴涨端到端压测, 纯模拟不下单 |
| 机会扫描 | `opportunity_scanner.py` | 三档机会 (保守/均衡/激进) 分层展示 + 账户级风控评估 |
| 策略评分 | `sell_put_strategy.py` | 多维度评分筛选 (delta/IV/theta/skew), 计算赔率 |
| 收益优化 | `profit_optimizer.py` | 智能止盈决策矩阵 + HV vs IV 分析 + Rolling 建议 |
| 对冲顾问 | `hedge_advisor.py` | 强平距离追踪, 补保证金 vs 买 Put 对比, 续期提醒 |
| 交易日志 | `trade_journal.py` | 信号→入场→平仓全生命周期记录, 胜率/PnL/回撤统计 |
| IV Rank | `iv_rank.py` | 时序 IV 百分位 (7 天历史), 判断 IV 贵/便宜 |
| IV 可视化 | `iv_chart.py` | IV 期限结构 + 微笑曲线图表 + 文字市场解读 |
| 应急对冲 | `emergency_hedge.py` | 自动买入保护性 Long Put (opt-in), 多重安全限制 |
| 告警通道 | `alert_channels.py` | CRITICAL 告警多通道降级: TG → SMTP → log.error |
| 状态持久化 | `state_persistence.py` | 运行时状态 JSON 持久化, 重启不丢失 |
| 事件日历 | `event_calendar.py` | 宏观经济事件 (FOMC/CPI/非农) 对开仓评分的影响 |
| AI 分析 | `ai_analyst.py` | LLM 驱动的市场判断 + 持仓评价 + 开仓点评 |

## 部署步骤

1. Clone repo
2. Copy `.env.example` to `.env`, fill in API keys
3. `pip install -r requirements.txt`
4. `./start_bot.sh`
5. (Optional) Set up `bot_watchdog.sh` in crontab
6. (Optional) Set up `HEARTBEAT_URL` for external monitoring

## TG 命令列表

| 命令 | 说明 |
|---|---|
| `/help` | 显示帮助 |
| `/scan` | 立即扫描 |
| `/positions` | 当前持仓 |
| `/risk` | 风控报告 |
| `/status` | 系统状态 |
| `/top` | 推荐合约 |
| `/hedge` | 对冲建议 |
| `/ai` | AI分析 |

## 风控模型说明

- **保证金公式**: 已校准到币安精确值 (误差<0.3%)
- **压力测试 IV 冲击**: 双模式取大 (线性加点 vs 乘数放大)
  - -10%→×1.3, -20%→×1.6, -30%→×2.0, -40%→×2.5
- **止损规则**: 复合条件 (浮亏倍数 + 距行权距离 + delta)
- **强平估算**: 二分法, 含 IV 冲击和 Long Put 对冲效应

## 免责声明

本系统仅用于监控和告警, 不构成投资建议。期权交易风险极高, 可能导致全部本金损失。用户应自行承担所有交易决策的后果。
