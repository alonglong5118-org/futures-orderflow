#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格保护 — 核心逻辑工具模块
============================

把价格保护相关的纯计算逻辑从 four_dim_live_runner.py / trade_journal.py /
account_tracker.py 中提取出来，便于单元测试。

对应历史 bug（决策 24：价格保护3层防线）：
  - 问题：用户输入价格被 _auto_levels 内部修改（如 4830→4829.7，8732.3→8732.8）
  - 修复：3 层价格保护防线
    1. Handler 层：保存原始价，调用 _auto_levels 后强制还原
    2. record_entry 层：价格验证 + 非法价格拦截
    3. record_trade 层：价格验证 + 日志

使用方式：
    from price_protection import validate_price, validate_entry_stop, protect_user_price
"""


# ═══════════════════════════════════════════════════════════════════════════
#  1. 价格有效性校验
# ═══════════════════════════════════════════════════════════════════════════

def validate_price(price):
    """
    校验价格是否合法（必须 > 0）。

    Args:
        price: 价格（可以是 int/float/str/None）

    Returns:
        dict: {
            "valid": bool,       # 是否合法
            "price": float,      # 转换后的价格（不合法时为 0.0）
            "reason": str        # 不合法的原因（合法时为空串）
        }
    """
    result = {"valid": False, "price": 0.0, "reason": ""}

    if price is None:
        result["reason"] = "价格不能为空"
        return result

    try:
        p = float(price)
    except (TypeError, ValueError):
        result["reason"] = f"价格格式错误: {price}"
        return result

    if p <= 0:
        result["reason"] = f"非法价格 {price}，必须大于0"
        return result

    result["valid"] = True
    result["price"] = p
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  2. 止损方向校验 & 自动修正
# ═══════════════════════════════════════════════════════════════════════════

def _dir_sign(direction):
    """方向符号：多/long/1 → 1；空/short/-1 → -1；其他 → 0。"""
    if isinstance(direction, str):
        d = direction.strip().lower()
        if d in ("多", "long", "duo", "buy"):
            return 1
        if d in ("空", "short", "kong", "sell"):
            return -1
        return 0
    if isinstance(direction, (int, float)):
        if direction > 0:
            return 1
        if direction < 0:
            return -1
        return 0
    return 0


def validate_entry_stop(direction, entry_price, stop):
    """
    校验开仓时止损方向是否正确；若错误则以入场价为轴镜像修正。

    规则：
    - 多单：止损必须 < 入场价（在下方）
    - 空单：止损必须 > 入场价（在上方）
    - 方向错误时：以 entry_price 为对称轴，镜像修正止损价

    Args:
        direction: 方向（"多"/"空"/"long"/"short"/1/-1 等）
        entry_price: 入场价
        stop: 止损价（None 表示未设止损）

    Returns:
        dict: {
            "stop": float or None,   # 修正后的止损价（正确则原样返回）
            "fixed": bool,           # 是否被修正过
            "fix_note": str,         # 修正说明（未修正则为空串）
            "direction_valid": bool, # 方向本身是否有效
        }
    """
    result = {
        "stop": stop,
        "fixed": False,
        "fix_note": "",
        "direction_valid": True,
    }

    if stop is None:
        return result

    ds = _dir_sign(direction)
    if ds == 0:
        result["direction_valid"] = False
        return result  # 方向无效时不做修正，原样返回

    try:
        ep = float(entry_price)
        sv = float(stop)
    except (TypeError, ValueError):
        return result  # 价格无法转换时不做修正

    # 检查方向是否正确
    # 多单：止损 < 入场价；空单：止损 > 入场价
    correct = (sv < ep) if ds > 0 else (sv > ep)

    if correct:
        return result

    # 方向错误 → 镜像修正
    nv = round(ep + (ep - sv), 2)

    # 修正后等于入场价的极端情况，偏移一点点
    # 注意：偏移方向与常规止损方向相反（多单→上方+0.01，空单→下方-0.01）
    # 这是原 trade_journal.py 的行为，此处保持一致
    if abs(nv - ep) < 1e-9:
        nv = round(ep + 0.01 if ds > 0 else ep - 0.01, 2)

    result["stop"] = nv
    result["fixed"] = True
    result["fix_note"] = f"止损方向已自动修正为 {nv}"
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  3. 用户价格保护（防止被自动计算覆盖）
# ═══════════════════════════════════════════════════════════════════════════

def protect_user_price(original_price, computed_price, user_provided_price=True):
    """
    保护用户输入的价格，不被自动计算逻辑篡改。

    对应历史 bug：_auto_levels 内部修改了 price 变量，导致用户输入的
    4830 变成 4829.7，8732.3 变成 8732.8。

    Args:
        original_price: 用户原始输入价格
        computed_price: 自动计算后得到的价格
        user_provided_price: 用户是否明确提供了价格（True=用户输入，False=系统检测）

    Returns:
        dict: {
            "final_price": float,    # 最终使用的价格（用户价优先）
            "was_protected": bool,   # 是否触发了保护（即计算价与用户价不同）
            "price_changed": bool,   # 价格是否被修改过（计算价 != 原始价）
        }
    """
    try:
        orig = float(original_price) if original_price is not None else 0.0
    except (TypeError, ValueError):
        orig = 0.0

    try:
        comp = float(computed_price) if computed_price is not None else 0.0
    except (TypeError, ValueError):
        comp = 0.0

    result = {
        "final_price": comp,
        "was_protected": False,
        "price_changed": abs(comp - orig) > 1e-9,
    }

    if not user_provided_price:
        # 用户没提供价格，用计算值即可
        return result

    # 用户提供了价格 → 强制使用用户价，覆盖计算值
    result["final_price"] = orig
    result["was_protected"] = result["price_changed"]
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  4. 止盈方向校验（t1 / t2）
# ═══════════════════════════════════════════════════════════════════════════

def validate_take_profit(direction, entry_price, tp_price):
    """
    校验止盈价方向是否正确。

    规则：
    - 多单：止盈必须 > 入场价（在上方）
    - 空单：止盈必须 < 入场价（在下方）

    Args:
        direction: 方向
        entry_price: 入场价
        tp_price: 止盈价（None 表示未设）

    Returns:
        dict: {
            "valid": bool,           # 方向是否正确
            "tp_price": float/None,  # 止盈价
            "reason": str,           # 不正确的原因
        }
    """
    result = {"valid": True, "tp_price": tp_price, "reason": ""}

    if tp_price is None:
        return result

    ds = _dir_sign(direction)
    if ds == 0:
        result["valid"] = False
        result["reason"] = "方向无效，无法校验止盈方向"
        return result

    try:
        ep = float(entry_price)
        tp = float(tp_price)
    except (TypeError, ValueError):
        result["valid"] = False
        result["reason"] = "价格格式错误"
        return result

    # 多单：止盈 > 入场价；空单：止盈 < 入场价
    correct = (tp > ep) if ds > 0 else (tp < ep)

    if not correct:
        result["valid"] = False
        result["reason"] = "止盈方向错误（应在有利方向一侧）"

    return result
