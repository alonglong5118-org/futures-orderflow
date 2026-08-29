"""
信号触发判断 — 纯函数工具
============================

从 four_dim_strategy.py pipeline 的触发判断逻辑中提取的纯函数，
覆盖 threshold 模式下的核心决策链路：
  - bias_FC 合成（F/C 背景偏置）
  - F/C 反向硬否决
  - F/C 同向确认降阈值
  - T_5m 阈值触发判断

不包含（依赖外部模块 / 非核心路径）：
  - combined 方向模式（用得少，逻辑复杂）
  - sentiment 情绪过滤（依赖 sentiment_engine）
  - corr_gate（已单独测试）

历史 bug 覆盖：
  - P-C：硬否决阈值太高（bias_G≥60 几乎不可达）→ 改为 bias_FC + fc_hard(25)
  - P-B：同向确认没生效（F/C 强同向应该降阈值，但逻辑没接上）
  - T 方向为 0 时误触发（dir_T=0 不应触发）
"""

import math
from typing import Tuple


def compute_bias_FC(F: float, C: float) -> float:
    """
    计算 F/C 合成背景偏置（非技术面背景偏置）。

    公式：bias_FC = 0.25 * F + 0.15 * C

    这是 P-B/P-C 改造的核心：让 F/C 真正参与触发决策，
    而不是原来的"只看 T，F/C 形同虚设"。
    """
    return round(0.25 * F + 0.15 * C, 1)


def check_hard_veto(
    bias_FC: float,
    dir_T: int,
    fc_hard: float = 25.0,
) -> Tuple[bool, str]:
    """
    F/C 反向硬否决判断。

    规则：
    - bias_FC 的绝对值 >= fc_hard 阈值
    - 且 bias_FC 的符号与 dir_T 相反
    → 触发硬否决，信号被抑制

    P-C 历史 bug：原硬否决用 bias_G≥60，几乎永远达不到 → F/C 形同虚设。
    修复后改用 bias_FC + fc_hard=25，阈值可达，F/C 真正有否决权。

    返回: (hard_veto: bool, reason: str)
    """
    if dir_T == 0:
        return False, ""

    abs_fc = abs(bias_FC)
    opposite_dir = math.copysign(1, bias_FC) != dir_T if bias_FC != 0 else False

    if abs_fc >= fc_hard and opposite_dir:
        reason = f"F/C反向硬否决(|bias_FC|={abs_fc:.1f}≥{fc_hard:.0f})"
        return True, reason

    return False, ""


def check_fc_confirmation(
    bias_FC: float,
    dir_T: int,
    fc_confirm: float = 25.0,
) -> bool:
    """
    F/C 同向确认判断。

    规则：
    - bias_FC 的符号与 dir_T 相同
    - 且 abs(bias_FC) >= fc_confirm 阈值
    → 同向确认成立，T 阈值可以降低（正向加成）

    P-B 历史 bug：原逻辑 F/C 强同向但没有实际降阈值，属于"空转"。
    """
    if dir_T == 0 or bias_FC == 0:
        return False

    same_dir = math.copysign(1, bias_FC) == dir_T
    strong_enough = abs(bias_FC) >= fc_confirm

    return same_dir and strong_enough


def compute_effective_threshold(
    T_thresh_eff: float,
    fc_confirmed: bool,
    confirm_relief: float = 0.85,
) -> float:
    """
    计算实际触发阈值。

    规则：
    - F/C 同向确认 → 阈值 = T_thresh_eff × confirm_relief（降低阈值，更容易触发）
    - 无确认 → 阈值 = T_thresh_eff（保持原阈值）
    """
    if fc_confirmed:
        return T_thresh_eff * confirm_relief
    return T_thresh_eff


def check_same_direction(bias_G: float, dir_T: int) -> bool:
    """
    判断 bias_G（全局背景偏置）与 T 方向是否同向。

    规则：
    - bias_G 正 + dir_T 正 → 同向
    - bias_G 负 + dir_T 负 → 同向
    - bias_G 接近 0（< 1e-6）→ 也算同向（中性背景不阻挡）
    - dir_T = 0 → 无方向，返回 False
    """
    if dir_T == 0:
        return False

    if abs(bias_G) < 1e-6:
        return True  # 中性背景

    return (bias_G > 0 and dir_T > 0) or (bias_G < 0 and dir_T < 0)


def signal_trigger_decision(
    T_5m: float,
    dir_T: int,
    T_thresh_eff: float,
    bias_G: float,
    bias_FC: float,
    fc_confirm: float = 25.0,
    confirm_relief: float = 0.85,
    fc_hard: float = 25.0,
) -> dict:
    """
    信号触发决策（threshold 模式）。

    这是四维策略触发判断的核心纯函数版本，
    对应 pipeline 中 P-B/P-C 改造后的触发逻辑。

    决策流程：
    1. dir_T = 0 → 不触发
    2. F/C 反向硬否决 → 不触发
    3. F/C 同向确认 → 降低 T 阈值
    4. 同向（bias_G 与 T 同方向）且 |T_5m| >= 有效阈值 → 触发

    参数:
        T_5m:           5m 技术面 T 值
        dir_T:          T 方向（1 多，-1 空，0 中性）
        T_thresh_eff:   T 触发阈值（已考虑 regime 系数等）
        bias_G:         全局背景偏置（T 方向 + F/C 合成）
        bias_FC:        F/C 合成背景偏置（0.25F + 0.15C）
        fc_confirm:     F/C 同向确认阈值
        confirm_relief: 同向确认时阈值降低系数（<1 表示降低）
        fc_hard:        F/C 反向硬否决阈值

    返回 dict:
        triggered:      bool，是否触发
        hard_veto:      bool，是否被硬否决
        hard_veto_reason: str，硬否决原因
        fc_confirmed:   bool，F/C 是否同向确认
        effective_thr:  float，实际使用的触发阈值
        same_dir:       bool，bias_G 与 T 是否同向
    """
    result = {
        "triggered": False,
        "hard_veto": False,
        "hard_veto_reason": "",
        "fc_confirmed": False,
        "effective_thr": T_thresh_eff,
        "same_dir": False,
    }

    # dir_T = 0 → 不触发
    if dir_T == 0:
        return result

    # 硬否决
    hard_veto, veto_reason = check_hard_veto(bias_FC, dir_T, fc_hard)
    if hard_veto:
        result["hard_veto"] = True
        result["hard_veto_reason"] = veto_reason
        return result

    # 同向确认
    fc_confirmed = check_fc_confirmation(bias_FC, dir_T, fc_confirm)
    result["fc_confirmed"] = fc_confirmed

    # 有效阈值
    eff_thr = compute_effective_threshold(T_thresh_eff, fc_confirmed, confirm_relief)
    result["effective_thr"] = eff_thr

    # 同向判断
    same_dir = check_same_direction(bias_G, dir_T)
    result["same_dir"] = same_dir

    # 触发判断：同向 + |T_5m| >= 有效阈值
    if same_dir and abs(T_5m) >= eff_thr:
        result["triggered"] = True

    return result
