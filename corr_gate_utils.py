#!/usr/bin/env python3
"""
相关性闸门（corr_gate）— 核心逻辑工具模块
============================================

把 T/C 相关性降权的纯计算逻辑从 four_dim_strategy.py 中提取出来，
便于单元测试。

对应历史 bug（决策 26：corr_gate 空转修复）：
  - 问题：原修复只改了文本描述，权重并未实际降权（空转）
  - 修复：|corr(T,C)| > gate 时，把 T 和 C 中绝对值较小的一维强制降为 0
  - 目的：避免冗余维度重复加权，提高信号熵纯度

使用方式：
    from corr_gate_utils import apply_corr_gate

    result = apply_corr_gate(
        T_score=85.0,
        C_score=70.0,
        corr_hist=[[...], [...]],  # T 和 C 的历史值序列
        gate=0.70,
        min_history=10,
    )
"""

import math


def _pearson_corr(x, y):
    """
    计算皮尔逊相关系数（不依赖 numpy，纯 Python 实现）。
    返回 None 表示无法计算（数据不足或方差为 0）。
    """
    n = len(x)
    if n < 2 or n != len(y):
        return None

    # 均值
    mean_x = sum(x) / n
    mean_y = sum(y) / n

    # 协方差和方差
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / n
    var_x = sum((xi - mean_x) ** 2 for xi in x) / n
    var_y = sum((yi - mean_y) ** 2 for yi in y) / n

    # 方差为 0 → 无法计算
    if var_x <= 0 or var_y <= 0:
        return None

    # 皮尔逊相关系数
    corr = cov / math.sqrt(var_x * var_y)

    # 浮点误差保护：限制在 [-1, 1]
    return max(-1.0, min(1.0, corr))


def apply_corr_gate(T_score, C_score, corr_hist=None, gate=0.70, min_history=10):
    """
    应用相关性闸门：如果 T 和 C 高度相关，降权较弱的那一维。

    规则：
    1. 历史数据不足（< min_history）→ 不处理
    2. 计算 T 和 C 的皮尔逊相关系数
    3. |corr| > gate → 降权（把绝对值较小的那一维设为 0）
    4. |corr| <= gate → 正常计权

    Args:
        T_score: float，趋势维度得分（T_D）
        C_score: float，资金维度得分（C）
        corr_hist: list of [T_val, C_val]，T 和 C 的历史序列
                    None 或长度不足时跳过
        gate: float，相关性阈值（默认 0.70）
        min_history: int，最少历史样本数（默认 10）

    Returns:
        dict: {
            "T": float,            # 处理后的 T 得分
            "C": float,            # 处理后的 C 得分
            "corr": float or None, # 相关系数
            "action": str,         # 动作描述
            "applied": bool,       # 是否触发了降权
            "dropped": str,        # 被降权的维度："T"/"C"/"none"
        }
    """
    result = {
        "T": T_score,
        "C": C_score,
        "corr": None,
        "action": "无冗余,正常计权",
        "applied": False,
        "dropped": "none",
    }

    # ── 历史数据检查 ──────────────────────────────────────────────────────
    if corr_hist is None:
        result["action"] = "无历史数据,跳过corr_gate"
        return result

    if len(corr_hist) < min_history:
        result["action"] = f"历史数据不足({len(corr_hist)}<{min_history}),跳过corr_gate"
        return result

    # ── 提取 T 和 C 的序列 ───────────────────────────────────────────────
    try:
        T_vals = [float(row[0]) for row in corr_hist]
        C_vals = [float(row[1]) for row in corr_hist]
    except (TypeError, ValueError, IndexError):
        result["action"] = "历史数据格式错误,跳过corr_gate"
        return result

    # ── 检查数据有效性（不能全相同，否则方差为 0） ───────────────────────
    if len(set(T_vals)) <= 1 or len(set(C_vals)) <= 1:
        result["action"] = "某维度无波动,跳过corr_gate"
        return result

    # ── 计算相关系数 ──────────────────────────────────────────────────────
    corr = _pearson_corr(T_vals, C_vals)
    if corr is None or math.isnan(corr):
        result["action"] = "相关系数无法计算,跳过corr_gate"
        return result

    result["corr"] = corr

    # ── 判定是否触发降权 ──────────────────────────────────────────────────
    if abs(corr) <= gate:
        result["action"] = f"corr={corr:.2f}≤gate={gate},正常计权"
        return result

    # ── 触发降权：保留绝对值较大的，把较小的设为 0 ───────────────────────
    abs_T = abs(T_score)
    abs_C = abs(C_score)

    if abs_T <= abs_C:
        # T 更弱 → 降权 T
        result["T"] = 0.0
        result["dropped"] = "T"
        result["action"] = f"corr={corr:.2f}>gate={gate},降权T(|T|={abs_T:.1f}≤|C|={abs_C:.1f})"
    else:
        # C 更弱 → 降权 C
        result["C"] = 0.0
        result["dropped"] = "C"
        result["action"] = f"corr={corr:.2f}>gate={gate},降权C(|C|={abs_C:.1f}<|T|={abs_T:.1f})"

    result["applied"] = True
    return result
