# -*- coding: utf-8 -*-
"""
止盈止损纯函数工具（exit_plan + sim_exit）
============================================

从 four_dim_strategy.py 提取的可测试纯函数：
  1. calc_exit_plan()   — 计算 stop/t1/t2/尾仓参数
  2. sim_exit_bars()    — 逐 bar 模拟出场（止损/止盈/尾仓移动止损）

设计原则：
  - 纯函数：相同输入 → 相同输出
  - 无副作用：不读文件、不写全局、不抛非预期异常
  - 可独立测试：不依赖 pandas / 数据库 / 外部模块

历史 bug 覆盖（在 test_take_profit.py 中验证）：
  - 方向搞反（多单止盈在入场下方 / 空单止损在入场下方）
  - regime 系数漏乘（stop_atr_mult 没有 × regime_coef.stop）
  - 尾仓跟踪方向搞反（多单用 min 而不是 max 更新尾仓止损）
  - t2 触发后未进入尾仓态（直接全平，尾仓逻辑空转）
  - 尾仓止损初始值算错（应该是 t2 ± tail_stop_dist）
"""

from typing import List, Tuple, Optional, Dict, Any


# ═══════════════════════════════════════════════════════════════════════════
#  1. 止盈止损参数计算（exit_plan 核心）
# ═══════════════════════════════════════════════════════════════════════════

def calc_exit_plan(
    entry: float,
    dir_T: float,
    atr_val: float,
    stop_atr_mult: float = 1.5,
    rr_ratio: float = 2.0,
    regime_stop_coef: float = 1.0,
    tail_enabled: bool = False,
    tail_trail_R: float = 2.0,
    tail_pct: float = 0.25,
) -> Dict[str, Any]:
    """
    计算一笔交易的止损/止盈/尾仓参数。

    纯函数版本：不依赖 cfg / symbol / feat_mgr，所有参数显式传入。

    参数:
        entry:              入场价
        dir_T:              方向（>0 多，<0 空）
        atr_val:            ATR 值
        stop_atr_mult:      止损 ATR 倍数（基础）
        rr_ratio:           盈亏比（t2 = rr_ratio × stop_dist）
        regime_stop_coef:   regime 止损系数（趋势 1.0 / 波动 1.2 / 震荡 1.0）
        tail_enabled:       是否启用尾仓
        tail_trail_R:       尾仓跟踪距离（单位：1R）
        tail_pct:           尾仓比例

    返回 dict:
        stop, t1, t2        止损价、第一止盈价(1R)、第二止盈价(rr_ratio R)
        stop_dist           止损距离（绝对值，正数）
        tail_enabled        尾仓是否启用
        tail_stop_dist      尾仓跟踪距离（绝对值 = tail_trail_R × stop_dist）
        tail_pct            尾仓比例
    """
    # 止损距离 = stop_atr_mult × regime_coef × ATR
    stop_mult = stop_atr_mult * regime_stop_coef
    stop_dist = stop_mult * atr_val

    is_long = dir_T > 0

    if is_long:
        # 多单：止损在下方，止盈在上方
        stop = entry - stop_dist
        t1 = entry + stop_dist        # 1R 平半
        t2 = entry + rr_ratio * stop_dist   # rr_ratio R 全平（或进入尾仓）
    else:
        # 空单：止损在上方，止盈在下方
        stop = entry + stop_dist
        t1 = entry - stop_dist
        t2 = entry - rr_ratio * stop_dist

    # 尾仓跟踪距离
    tail_stop_dist = tail_trail_R * stop_dist

    return {
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "stop_dist": round(stop_dist, 2),
        "tail_enabled": tail_enabled,
        "tail_stop_dist": round(tail_stop_dist, 2),
        "tail_pct": tail_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  2. 逐 bar 出场模拟（_sim_exit_5m 核心）
# ═══════════════════════════════════════════════════════════════════════════

def sim_exit_bars(
    bars: List[Tuple[float, float]],       # [(high, low), ...]
    dir_T: float,
    entry: float,
    ep: Dict[str, Any],
) -> Tuple[float, str, int]:
    """
    逐 bar 模拟出场，返回 (exit_price, exit_reason, exit_bar_index)。

    纯函数版本：不依赖 pandas，bars 用简单列表传入。

    出场优先级（同一根 bar 内）：
      1. 止损（先检查不利方向）
      2. 止盈 t2 → 进入尾仓态 / 全平
      3. 尾仓态下的移动止损

    参数:
        bars:   K 线列表，每个元素是 (high, low) 元组
        dir_T:  方向（>0 多，<0 空）
        entry:  入场价（用于边界检查）
        ep:     exit_plan 的返回结果（含 stop/t2/tail_enabled/tail_stop_dist）

    返回:
        (exit_price, exit_reason, exit_idx)
        exit_reason ∈ {"止损", "止盈2R", "尾仓离场", "期末平"}
    """
    if not bars:
        return entry, "期末平", -1

    is_long = dir_T > 0
    tail_active = False
    tail_stop = None

    for j in range(len(bars)):
        hi, lo = bars[j]

        # ── 尾仓态：只看移动止损 ──
        if tail_active:
            if is_long:
                # 多单尾仓：价格跌破尾仓止损 → 离场
                if lo <= tail_stop:
                    return tail_stop, "尾仓离场", j
                # 创新高 → 上移尾仓止损
                tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
            else:
                # 空单尾仓：价格涨破尾仓止损 → 离场
                if hi >= tail_stop:
                    return tail_stop, "尾仓离场", j
                # 创新低 → 下移尾仓止损
                tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
            continue

        # ── 非尾仓态：先检查止损，再检查止盈 ──
        if is_long:
            # 多单：low 跌破 stop → 止损
            if lo <= ep["stop"]:
                return ep["stop"], "止损", j
            # high 涨破 t2 → 止盈
            if hi >= ep["t2"]:
                if ep["tail_enabled"]:
                    # 进入尾仓态，初始尾仓止损 = t2 - tail_stop_dist
                    tail_active = True
                    tail_stop = ep["t2"] - ep["tail_stop_dist"]
                    continue
                return ep["t2"], "止盈2R", j
        else:
            # 空单：high 涨破 stop → 止损
            if hi >= ep["stop"]:
                return ep["stop"], "止损", j
            # low 跌破 t2 → 止盈
            if lo <= ep["t2"]:
                if ep["tail_enabled"]:
                    # 进入尾仓态，初始尾仓止损 = t2 + tail_stop_dist
                    tail_active = True
                    tail_stop = ep["t2"] + ep["tail_stop_dist"]
                    continue
                return ep["t2"], "止盈2R", j

    # 走完所有 bar 都没出场 → 期末平
    last_close = (bars[-1][0] + bars[-1][1]) / 2  # 近似收盘价
    return last_close, "期末平", len(bars) - 1
