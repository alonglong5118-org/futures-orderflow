# -*- coding: utf-8 -*-
"""
风控仓位计算 — 纯函数工具
============================

从 four_dim_strategy.risk_gate 中提取的核心计算逻辑，
覆盖仓位计算的所有关键约束：
  1. 风险预算手数（risk_pct / stop_pts / multiplier）
  2. 最小 1 手兜底（超风险标注）
  3. Kelly 因子缩放
  4. 保证金约束手数
  5. 单品种持仓上限
  6. T 强度缩放（弱过阈降仓）
  7. 已有持仓扣减
  8. 涨跌停闸门（gate3）

历史 bug / 决策覆盖：
  - P1-4：fractional-Kelly 缩放（0.6~1.2x，原 1.6x 过度杠杆）
  - P1-16：风险锁定/熔断前置否决
  - P2b：同品种持仓扣减（加仓不超配）
  - 2026-08-19：T 强度随动缩放（弱过阈降仓，|T|≥1.5×阈值满仓）
  - 2026-08-16：分品种保证金上限收紧（JM/J 低胜率品种）
"""

from typing import Optional, Dict, Any


def calc_risk_lots(
    equity: float,
    risk_pct: float,
    stop_pts: float,
    multiplier: float,
) -> int:
    """
    计算风险预算允许的手数（向下取整）。

    公式：N_risk_raw = equity * (risk_pct/100) // (stop_pts * multiplier)

    参数:
        equity:       账户权益
        risk_pct:     单笔风险占比（%），如 1.5 表示 1.5%
        stop_pts:     止损点数（价格单位）
        multiplier:   合约乘数（每手多少单位）

    返回:
        int，风险预算手数（0 表示风险预算不够一手）
    """
    risk_per_hand = stop_pts * multiplier
    if risk_per_hand <= 0:
        return 0
    risk_budget = equity * risk_pct / 100.0
    return int(risk_budget // risk_per_hand)


def calc_min_lot_floor(N_risk_raw: int, risk_per_hand: float) -> tuple:
    """
    最小 1 手兜底处理。

    规则：
    - N_risk_raw < 1 且有风险 → 强制 1 手，标注 over_risk=True（超风险预算）
    - 否则 → 正常手数，over_risk=False

    设计意图：不裸奔，但不加仓（只开最小 1 手，超风险也认了）。

    返回: (N_risk, over_risk)
    """
    if N_risk_raw < 1 and risk_per_hand > 0:
        return 1, True
    return N_risk_raw, False


def apply_kelly_scaling(N_risk: int, kelly_mult: float) -> int:
    """
    应用 Kelly 因子缩放。

    规则：
    - N_risk >= 1 → 乘以 kelly_mult，四舍五入取整，至少 1 手
    - N_risk < 1 → 保持 0（没风险预算就不开仓）

    P1-4 历史 bug：原公式 kelly 可达 1.6x，弱/中置信品种过度杠杆。
    修复后 kelly_max=1.2，且标准化映射。
    """
    if N_risk < 1:
        return 0
    scaled = int(round(N_risk * kelly_mult))
    return max(1, scaled)


def calc_margin_lots(
    equity: float,
    margin_cap_pct: float,
    price: float,
    multiplier: float,
    margin_rate: float,
) -> int:
    """
    计算保证金约束允许的手数（向下取整）。

    公式：N_margin = equity * (margin_cap_pct/100) // (price * multiplier * margin_rate)

    参数:
        equity:          账户权益
        margin_cap_pct:  单笔保证金上限占比（%），如 30 表示 30%
        price:           价格
        multiplier:      合约乘数
        margin_rate:     保证金率（如 0.12 表示 12%）

    返回:
        int，保证金约束手数
    """
    margin_per_hand = price * multiplier * margin_rate
    if margin_per_hand <= 0:
        return 0
    margin_budget = equity * margin_cap_pct / 100.0
    return int(margin_budget // margin_per_hand)


def calc_t_strength_scale(
    t_strength: float,
    t_thresh: float,
) -> float:
    """
    计算 T 强度缩放系数（弱过阈降仓）。

    规则：
    - scale = |T| / (1.5 × t_thresh)
    - 范围：[0.5, 1.0]（最小 0.5 半仓，|T| >= 1.5×阈值时满仓 1.0）
    - t_thresh <= 0 → 返回 1.0（异常配置，不缩放）

    设计意图：
    弱过阈（刚过线）时降仓，强信号时满仓，
    避免"刚过阈值就满仓"的假信号风险。
    """
    if t_thresh is None or t_thresh <= 0:
        return 1.0
    ratio = abs(t_strength) / (t_thresh * 1.5)
    return max(0.5, min(1.0, ratio))


def deduct_held_lots(N_plan: int, held_lots: int, max_lots: int) -> int:
    """
    扣减已有同品种持仓（加仓不超配）。

    规则：
    - 新开仓 = min(N_plan, max_lots - held_lots)
    - 结果 >= 0

    P2b 历史问题：加仓时忘记扣减已有持仓，导致单品种总持仓超限。
    """
    if held_lots <= 0:
        return max(0, N_plan)
    available = max_lots - held_lots
    return max(0, min(N_plan, available))


def check_limit_gate(
    stop_pts: float,
    limit_pts: float,
    limit_proximity: float = 0.9,
) -> bool:
    """
    涨跌停闸门（第三道闸门）。

    规则：
    - 止损距必须 < 涨跌停幅度 × limit_proximity
    - 否则否决（一个停板即直达止损 = 极端风险）
    - limit_pts <= 0 → 放行（无涨跌停数据时不卡）

    limit_proximity 是缓冲系数，0.9 表示止损距达到涨跌停 90% 就预警否决。
    """
    if limit_pts <= 0:
        return True
    return stop_pts < limit_pts * limit_proximity


def calc_position_plan(
    equity: float,
    risk_pct: float,
    stop_pts: float,
    multiplier: float,
    margin_rate: float,
    price: float,
    margin_cap_pct: float = 30.0,
    max_lots: int = 5,
    kelly_mult: float = 1.0,
    t_strength: Optional[float] = None,
    t_thresh: Optional[float] = None,
    held_lots: int = 0,
    limit_pts: float = 0.0,
    limit_proximity: float = 0.9,
) -> Dict[str, Any]:
    """
    完整仓位计划计算（risk_gate 核心纯函数版本）。

    计算链路：
    1. 风险预算手数 N_risk_raw
    2. 最小 1 手兜底（超风险标注）
    3. Kelly 因子缩放 → N_risk
    4. 保证金约束 → N_margin
    5. 单品种上限 → N_plan = min(N_risk, N_margin, max_lots)
    6. T 强度缩放 → 弱过阈降仓
    7. 已有持仓扣减 → 最终计划手数
    8. 涨跌停闸门 → gate3_ok

    参数:
        equity:           账户权益
        risk_pct:         单笔风险占比（%）
        stop_pts:         止损点数
        multiplier:       合约乘数
        margin_rate:      保证金率
        price:            当前价格
        margin_cap_pct:   单笔保证金上限（%）
        max_lots:         单品种最大持仓手数
        kelly_mult:       Kelly 缩放系数
        t_strength:       T 强度（None 表示不启用 T 缩放）
        t_thresh:         T 阈值（T 缩放的参考）
        held_lots:        已有持仓手数
        limit_pts:        涨跌停幅度（0 表示不检查）
        limit_proximity:  涨跌停缓冲系数

    返回 dict:
        N_risk_raw    int    风险预算原始手数（Kelly 前）
        N_risk        int    Kelly 缩放后的风险手数
        N_margin      int    保证金约束手数
        N_plan        int    最终计划手数（>= 0）
        over_risk     bool   是否超风险预算（最小 1 手兜底触发）
        t_scale       float  T 强度缩放系数（None 表示未启用）
        gate3_ok      bool   涨跌停闸门是否通过
        passed        bool   是否通过（N_plan >= 1 且 gate3_ok）
    """
    # 1. 风险预算手数
    risk_per_hand = stop_pts * multiplier
    N_risk_raw = calc_risk_lots(equity, risk_pct, stop_pts, multiplier)

    # 2. 最小 1 手兜底
    N_risk, over_risk = calc_min_lot_floor(N_risk_raw, risk_per_hand)

    # 3. Kelly 缩放
    N_risk = apply_kelly_scaling(N_risk, kelly_mult)

    # 4. 保证金约束
    N_margin = calc_margin_lots(equity, margin_cap_pct, price, multiplier, margin_rate)

    # 5. 取最小值（风险 + 保证金 + 单品种上限）
    N_plan = min(N_risk, N_margin, max_lots)

    # 6. T 强度缩放
    t_scale = None
    if t_strength is not None and t_thresh is not None and t_thresh > 0:
        t_scale = calc_t_strength_scale(t_strength, t_thresh)
        N_plan = max(0, int(N_plan * t_scale))

    # 7. 已有持仓扣减
    if held_lots > 0:
        N_plan = deduct_held_lots(N_plan, held_lots, max_lots)

    # 8. 涨跌停闸门
    gate3_ok = check_limit_gate(stop_pts, limit_pts, limit_proximity)

    # 最终判断
    passed = (N_plan >= 1) and gate3_ok

    return {
        "N_risk_raw": N_risk_raw,
        "N_risk": N_risk,
        "N_margin": N_margin,
        "N_plan": max(0, N_plan),
        "over_risk": over_risk,
        "t_scale": round(t_scale, 3) if t_scale is not None else None,
        "gate3_ok": gate3_ok,
        "passed": passed,
    }
