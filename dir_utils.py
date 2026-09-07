# -*- coding: utf-8 -*-
"""方向归一化公共实现（全仓库唯一真源）。

背景
----
「direction → +1(多) / -1(空) / 0(未知)」这个归一化此前散落 4 份实现，
且行为互不一致，导致同一输入在不同模块得到不同结果：

  - ``account_tracker._dir_sign``  : 不支持数字，``_dir_sign(1)`` → **0**
  - ``price_protection._dir_sign``: 不支持 做多/多头/bull/bear
  - ``trade_journal._dir_sign``   : 只认 "多"/"空"，``_dir_sign("long")`` → **0**
                                    → 止损方向校验被**静默跳过**不修正
  - ``risk_state_machine._pos_dir``: 最全（支持 做多/多头/±1/数字）

本模块取各实现的**并集**，保证任一处都不会因输入格式差异而静默返回 0。

契约
----
    dir_sign(多|long|duo|buy|bull|l|做多|多头|1|+1|任意正数) -> 1
    dir_sign(空|short|kong|sell|bear|s|做空|空头|-1|任意负数) -> -1
    dir_sign(None|""|"平"|"unknown"|0|[]|{}|其他)            -> 0

防御性：任何未知格式一律返回 0（不参与计算），绝不抛异常。
"""
from __future__ import annotations

__all__ = ["dir_sign", "dir_norm", "LONG_KEYS", "SHORT_KEYS"]

LONG_KEYS = frozenset({"多", "long", "duo", "buy", "bull", "l", "做多", "多头", "1", "+1"})
SHORT_KEYS = frozenset({"空", "short", "kong", "sell", "bear", "s", "做空", "空头", "-1"})


def dir_sign(direction) -> int:
    """把任意方向表示归一化为 +1 / -1 / 0。"""
    if direction is None:
        return 0
    # 数字方向（含 numpy 数值）：按符号判定
    if isinstance(direction, (int, float)):
        if direction > 0:
            return 1
        if direction < 0:
            return -1
        return 0
    if isinstance(direction, str):
        d = direction.strip().lower()
        if d in LONG_KEYS:
            return 1
        if d in SHORT_KEYS:
            return -1
        return 0
    # 其它类型（list/dict/object 等）：未知 → 0，不参与计算
    return 0


def dir_norm(direction) -> str:
    """把任意方向格式归一化为中文显示：long→多, short→空, 未知→—。"""
    s = dir_sign(direction)
    return "多" if s > 0 else ("空" if s < 0 else "—")
