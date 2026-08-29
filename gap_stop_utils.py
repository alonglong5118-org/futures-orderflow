#!/usr/bin/env python3
"""
gap_stop 缺口击穿告警 — 核心逻辑工具模块
==========================================

把 gap_stop 的纯判断逻辑从 four_dim_live_runner.py 中提取出来，
便于单元测试，同时避免导入 runner 时的全局副作用。

使用方式：
    from gap_stop_utils import check_gap_stop_triggered

    result = check_gap_stop_triggered(ds=1, px=24000, stop=25000, entry_price=26000)
    if result["triggered"]:
        ...
"""


def check_gap_stop_triggered(ds, px, stop, entry_price):
    """检查缺口击穿止损是否触发（纯函数，无副作用，便于单元测试）。

    Args:
        ds: int，方向，1=多，-1=空，0=未知
        px: float，当前价格
        stop: float or None，止损价
        entry_price: float or None，入场价（用于计算 1R）

    Returns:
        dict: {
            "triggered": bool,   # 是否触发缺口击穿告警
            "is_adverse": bool,  # 是否为不利方向
            "oneR": float,       # 1R 风险（入场价到止损价的距离）
            "pen": float,        # 当前价格到止损价的穿透距离
            "pen_ratio": float   # 穿透比例（pen / oneR）
        }

    触发条件（全部满足）：
    1. 方向有效（ds != 0）
    2. stop 有效（不为 None）
    3. entry_price 有效（不为 None，且 oneR > 0）
    4. 价格在不利方向（is_adverse = True）
    5. 穿透距离 > 0.5R（严格大于，边界值不触发）

    方向规则：
    - 多单（ds > 0）：不利方向 = 价格 < 止损（从上方跌到止损下方）
    - 空单（ds < 0）：不利方向 = 价格 > 止损（从下方涨到止损上方）
    """
    result = {
        "triggered": False,
        "is_adverse": False,
        "oneR": 0.0,
        "pen": 0.0,
        "pen_ratio": 0.0,
    }

    # ── 基础有效性检查 ──────────────────────────────────────────────────────
    if ds == 0 or stop is None or entry_price is None:
        return result

    # ── 类型转换（防御脏数据） ──────────────────────────────────────────────
    try:
        px = float(px)
        stop = float(stop)
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        return result

    # ── 计算 1R 和穿透距离 ─────────────────────────────────────────────────
    oneR = abs(entry_price - stop)
    pen = abs(px - stop)

    result["oneR"] = oneR
    result["pen"] = pen

    if oneR <= 0:
        return result  # 除零保护：入场价等于止损价时无意义

    result["pen_ratio"] = pen / oneR

    # ── 方向检查（核心修复：2026-08-28 gap_stop 假阳性 bug） ───────────────
    # 必须是不利方向穿越止损才叫"缺口击穿"
    # 多单：价格从上方跌到止损下方（px < stop）
    # 空单：价格从下方涨到止损上方（px > stop）
    is_adverse = (ds > 0 and px < stop) or (ds < 0 and px > stop)
    result["is_adverse"] = is_adverse

    # ── 触发判定 ───────────────────────────────────────────────────────────
    # 严格大于 0.5R 才触发（等于 0.5R 是边界，不触发）
    if is_adverse and pen > 0.5 * oneR:
        result["triggered"] = True

    return result
