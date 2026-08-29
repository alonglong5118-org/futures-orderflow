#!/usr/bin/env python3
"""
Kelly 因子计算 — 核心逻辑工具模块
==================================

把 compute_kelly_factor 的纯数学计算逻辑从 four_dim_strategy.py 中提取出来，
便于单元测试，同时避免导入 strategy 时的全局副作用（加载数据文件等）。

使用方式：
    from kelly_utils import compute_kelly_factor

    mult = compute_kelly_factor(
        edge=0.3,
        kelly_min=0.6,
        kelly_max=1.2,
        target_edge=0.5,
        cur_full_expR=0.2,
    )
"""


def compute_kelly_factor(edge, kelly_min=0.6, kelly_max=1.2, target_edge=0.5, cur_full_expR=None):
    """
    计算 fractional-Kelly 仓位缩放系数。

    公式：mult = kelly_min + (kelly_max - kelly_min) * clip(edge / target_edge, 0, 1)

    近景门槛：仅当 edge 与近景期望收益(cur_full_expR) 同为正时，
    才允许 >1.0 的杠杆放大；否则强制封顶 1.0。

    Args:
        edge: float or None，walk-forward edge（mean_oos 或 full_expR）
              None 表示无校准数据 → 返回 1.0（中性）
        kelly_min: float，最小缩放系数（默认 0.6）
        kelly_max: float，最大缩放系数（默认 1.2，原 1.6 → P1-4 整改）
        target_edge: float，归一化目标 edge（默认 0.5）
        cur_full_expR: float or None，近景期望收益（用于近景门槛）
                       None 表示无近景数据 → 退回远 edge 符号

    Returns:
        float，Kelly 缩放系数 ∈ [kelly_min, kelly_max]（近景负时封顶 1.0）

    历史 bug 对应（决策 20：Kelly 因子从经验公式升级为标准化线性映射）：
    - 原公式：mult = 0.6 + slope * edge → edge=0.5 时冲到 1.6x，过度杠杆
    - 新公式：标准化线性映射，高 edge 品种杠杆降低 25%（1.6x → 1.2x）
    - 新增近景门槛：弱 edge 反向加杠杆被杜绝
    """
    # ── 无校准数据 → 中性 1.0 ──────────────────────────────────────────────
    if edge is None:
        return 1.0

    try:
        edge = float(edge)
    except (TypeError, ValueError):
        return 1.0

    # 参数防御
    try:
        kelly_min = float(kelly_min)
        kelly_max = float(kelly_max)
        target_edge = float(target_edge)
    except (TypeError, ValueError):
        return 1.0

    # 防止 kelly_min > kelly_max 的异常配置
    if kelly_min > kelly_max:
        kelly_min, kelly_max = kelly_max, kelly_min

    # ── 线性映射：edge → [kelly_min, kelly_max] ────────────────────────────
    # edge 取正（负 edge 按 0 处理，即最小缩放）
    edge_pos = max(edge, 0.0)

    # 归一化比例：edge / target_edge，封顶 1.0
    if target_edge > 0:
        ratio = min(edge_pos / target_edge, 1.0)
    else:
        ratio = 1.0  # target_edge 为 0 或负时，直接拉满（异常配置保护）

    mult = kelly_min + (kelly_max - kelly_min) * ratio

    # ── 近景门槛（P2-A 整改） ──────────────────────────────────────────────
    # 仅当近景期望收益 > 0 时，才允许 >1.0 的杠杆放大
    # 缺近景数据时，退回远 edge 的符号判断
    if cur_full_expR is not None:
        try:
            near_pos = float(cur_full_expR) > 0
        except (TypeError, ValueError):
            near_pos = edge > 0  # 近景数据异常时退回远 edge
    else:
        near_pos = edge > 0

    if not near_pos:
        mult = min(mult, 1.0)

    return mult
