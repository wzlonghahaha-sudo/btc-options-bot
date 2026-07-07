"""
仓位建议模块 — 建议每个合约的开仓张数

规则 (保守, 全部可在 .env 覆盖):
  1. 总保证金占用上限 60% (含新仓后)
  2. 单一到期日的名义敞口 ≤ 账户 equity 的 40%
  3. 单笔新开仓保证金 ≤ 账户 equity 的 15%
  4. 三者取最小值向下取整; 结果为 0 时输出"当前不建议开仓"及卡住的约束名

用法:
  from position_sizer import suggest_qty
  result = suggest_qty(equity, used_margin, per_contract_margin, expiry_notional)
"""

import os
import math
import logging

log = logging.getLogger(__name__)

# 可通过 .env 覆盖的默认值
MAX_TOTAL_MARGIN_PCT = float(os.getenv("SIZER_MAX_TOTAL_MARGIN_PCT", "0.60"))
MAX_EXPIRY_NOTIONAL_PCT = float(os.getenv("SIZER_MAX_EXPIRY_NOTIONAL_PCT", "0.40"))
MAX_SINGLE_MARGIN_PCT = float(os.getenv("SIZER_MAX_SINGLE_MARGIN_PCT", "0.15"))


def suggest_qty(
    account_equity: float,
    current_used_margin: float,
    per_contract_margin: float,
    existing_expiry_notional: float,
    strike: float = 0,
) -> dict:
    """
    计算建议开仓张数

    Args:
        account_equity: 账户净值 (equity)
        current_used_margin: 当前已用保证金
        per_contract_margin: 该合约每张保证金
        existing_expiry_notional: 同到期日已有仓位的名义敞口 (strike × qty)
        strike: 新合约行权价 (用于计算名义敞口)

    Returns:
        {
            "qty": int,                  # 建议张数 (向下取整)
            "binding_constraint": str,   # 约束名 (如果 qty=0 则是卡住的约束)
            "details": dict,             # 各约束的最大允许张数明细
            "reason": str,               # 可读说明
        }
    """
    if account_equity <= 0 or per_contract_margin <= 0:
        return {
            "qty": 0,
            "binding_constraint": "insufficient_data",
            "details": {},
            "reason": "账户数据不足, 无法计算",
        }

    # --- 约束 1: 总保证金占用上限 ---
    margin_room = account_equity * MAX_TOTAL_MARGIN_PCT - current_used_margin
    qty_by_margin = max(0, math.floor(margin_room / per_contract_margin))

    # --- 约束 2: 单一到期日名义敞口上限 ---
    if strike > 0:
        notional_room = account_equity * MAX_EXPIRY_NOTIONAL_PCT - existing_expiry_notional
        notional_per_contract = strike  # 名义敞口 = strike × qty
        qty_by_expiry = max(0, math.floor(notional_room / notional_per_contract)) if notional_per_contract > 0 else 0
    else:
        qty_by_expiry = qty_by_margin  # 无行权价信息, 不限制

    # --- 约束 3: 单笔保证金上限 ---
    single_room = account_equity * MAX_SINGLE_MARGIN_PCT
    qty_by_single = max(0, math.floor(single_room / per_contract_margin))

    # --- 取最小值 ---
    constraints = {
        "total_margin_60pct": qty_by_margin,
        "expiry_notional_40pct": qty_by_expiry,
        "single_trade_15pct": qty_by_single,
    }

    qty = min(constraints.values())

    # 找出约束最紧的
    binding = min(constraints, key=constraints.get)

    constraint_names_cn = {
        "total_margin_60pct": "总保证金占用已达 60%",
        "expiry_notional_40pct": "同到期日名义敞口已达 40%",
        "single_trade_15pct": "单笔保证金超 15% 账户",
    }

    if qty == 0:
        reason = f"当前不建议开仓 (受限于: {constraint_names_cn.get(binding, binding)})"
    else:
        reason = f"建议 {qty} 张 (受限于: {constraint_names_cn.get(binding, binding)})"

    return {
        "qty": qty,
        "binding_constraint": binding,
        "details": constraints,
        "reason": reason,
    }
