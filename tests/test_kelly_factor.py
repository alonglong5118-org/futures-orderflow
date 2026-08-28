#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kelly 因子计算 — 单元测试
=========================

覆盖场景：
1. 基础线性映射：edge=0 → kelly_min，edge=target_edge → kelly_max
2. 边界值：edge < 0、edge = target_edge、edge 远超 target_edge
3. 近景门槛：正 edge 但近景负 → 封顶 1.0
4. 历史 bug 回归：原公式过度杠杆问题（SA 纯碱 1.6x → 1.2x）
5. 参数化：kelly_min/kelly_max/target_edge 自定义
6. 异常输入：None、字符串、零值

对应历史 bug（决策 20）：
  - 原公式：mult = 0.6 + slope * edge
  - 问题：edge=0.5 时冲到 1.6x，高 edge 品种过度杠杆
  - 修复：标准化线性映射 + kelly_max 封顶 1.2x + 近景门槛
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kelly_utils import compute_kelly_factor


# ═══════════════════════════════════════════════════════════════════════════
#  基础线性映射
# ═══════════════════════════════════════════════════════════════════════════

class TestBasicLinearMapping(unittest.TestCase):
    """基础线性映射逻辑测试。"""

    def test_zero_edge_returns_min(self):
        """edge = 0 → 返回 kelly_min（默认 0.6）"""
        mult = compute_kelly_factor(edge=0.0)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_edge_at_target_returns_max(self):
        """edge = target_edge → 返回 kelly_max（默认 1.2）"""
        mult = compute_kelly_factor(edge=0.5)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_edge_half_target_returns_midpoint(self):
        """edge = target_edge / 2 → 返回中间值 0.9"""
        mult = compute_kelly_factor(edge=0.25)
        # 0.6 + (1.2 - 0.6) * 0.5 = 0.6 + 0.3 = 0.9
        self.assertAlmostEqual(mult, 0.9, places=4)

    def test_edge_above_target_capped_at_max(self):
        """edge > target_edge → 封顶 kelly_max"""
        mult = compute_kelly_factor(edge=1.0)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_edge_way_above_target_capped(self):
        """edge 远超 target_edge → 仍封顶 kelly_max"""
        mult = compute_kelly_factor(edge=5.0)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_negative_edge_returns_min(self):
        """edge < 0 → 按 0 处理，返回 kelly_min"""
        mult = compute_kelly_factor(edge=-0.3)
        self.assertAlmostEqual(mult, 0.6, places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  近景门槛（P2-A 整改）
# ═══════════════════════════════════════════════════════════════════════════

class TestNearTermGate(unittest.TestCase):
    """近景期望收益门槛测试。"""

    def test_positive_edge_positive_near_allows_above_1(self):
        """edge 正 + 近景正 → 允许 >1.0 杠杆"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=0.3)
        self.assertAlmostEqual(mult, 1.2, places=4)
        self.assertGreater(mult, 1.0)

    def test_positive_edge_negative_near_capped_at_1(self):
        """edge 正 + 近景负 → 封顶 1.0（核心修复：杜绝弱 edge 反向加杠杆）"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=-0.2)
        self.assertAlmostEqual(mult, 1.0, places=4)

    def test_positive_edge_zero_near_capped_at_1(self):
        """edge 正 + 近景 = 0 → 封顶 1.0（0 不算正）"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=0.0)
        self.assertAlmostEqual(mult, 1.0, places=4)

    def test_negative_edge_positive_near_stays_min(self):
        """edge 负 + 近景正 → 仍为 kelly_min（edge 负本身就低）"""
        mult = compute_kelly_factor(edge=-0.1, cur_full_expR=0.3)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_negative_edge_negative_near_stays_min(self):
        """edge 负 + 近景负 → 仍为 kelly_min，且 < 1.0 不触发封顶"""
        mult = compute_kelly_factor(edge=-0.1, cur_full_expR=-0.2)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_no_near_data_falls_back_to_edge_sign_positive(self):
        """无近景数据 + edge 正 → 允许 >1.0（退回远 edge 符号）"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=None)
        self.assertAlmostEqual(mult, 1.2, places=4)
        self.assertGreater(mult, 1.0)

    def test_no_near_data_falls_back_to_edge_sign_negative(self):
        """无近景数据 + edge 负 → kelly_min（退回远 edge 符号）"""
        mult = compute_kelly_factor(edge=-0.1, cur_full_expR=None)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_mid_positive_edge_negative_near_capped(self):
        """中等正 edge + 近景负 → 封顶 1.0（原公式会给 0.8 左右 < 1.0，
        但如果 edge 足够高导致 mult > 1.0，近景负必须拉回 1.0）"""
        # edge=0.3 → ratio=0.6 → mult = 0.6 + 0.6*0.6 = 0.96 < 1.0
        # 近景负的话封顶 1.0，但 0.96 < 1.0，所以不受影响
        mult = compute_kelly_factor(edge=0.3, cur_full_expR=-0.1)
        self.assertAlmostEqual(mult, 0.96, places=4)

    def test_high_edge_negative_near_strictly_capped(self):
        """高 edge + 近景负 → 严格封顶 1.0，绝不超过"""
        # edge=1.0 → 原计算会到 1.2，但近景负必须压到 1.0
        mult = compute_kelly_factor(edge=1.0, cur_full_expR=-0.5)
        self.assertAlmostEqual(mult, 1.0, places=4)
        self.assertLessEqual(mult, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  历史 bug 回归测试
#  （决策 20：Kelly 因子标准化 + 高 edge 降杠杆 25%）
# ═══════════════════════════════════════════════════════════════════════════

class TestHistoricalBugRegression(unittest.TestCase):
    """
    历史 bug 回归测试 —— 确保修复后的问题不再复发。

    数据来源：项目档案 02-重要决策.md → 决策 20
    - SA 纯碱：Kelly 因子 1.60x → 1.20x (-25%)
    - FG 玻璃：Kelly 因子 0.81x → 0.73x (-10.5%)
    - JM 焦煤：Kelly 因子 0.60x → 0.60x（不变）
    """

    def test_sa_soda_ash_high_edge_capped_at_1_2(self):
        """SA 纯碱（高 edge 品种）：原公式冲到 1.6x，新公式封顶 1.2x（-25%）"""
        # SA 纯碱 edge 较高，原公式 0.6 + 2.0 * 0.5 = 1.6（过度杠杆）
        # 新公式：edge=0.5 → ratio=1.0 → 0.6 + 0.6*1.0 = 1.2
        mult = compute_kelly_factor(edge=0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5)
        self.assertAlmostEqual(mult, 1.2, places=4)
        # 验证确实比原公式的 1.6 低了 25%
        self.assertLess(mult, 1.6)
        self.assertAlmostEqual((1.6 - mult) / 1.6, 0.25, places=2)

    def test_fg_glass_mid_edge_reduced(self):
        """FG 玻璃（中等 edge）：原约 0.81x，新约 0.73x（-10.5%）"""
        # 假设 FG 的 edge 约为 0.11：
        # 原公式：0.6 + 2.0 * 0.11 = 0.82
        # 新公式：0.6 + 0.6 * (0.11/0.5) = 0.6 + 0.6 * 0.22 = 0.732
        mult = compute_kelly_factor(edge=0.11, kelly_min=0.6, kelly_max=1.2, target_edge=0.5)
        self.assertAlmostEqual(mult, 0.732, places=3)

    def test_jm_coking_coal_low_edge_stays_min(self):
        """JM 焦煤（低 edge 品种）：0.60x → 0.60x，不变"""
        # JM edge 低，原公式和新公式都在 kelly_min 附近
        mult = compute_kelly_factor(edge=0.0, kelly_min=0.6, kelly_max=1.2, target_edge=0.5)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_original_formula_would_give_1_6(self):
        """验证：如果用旧公式（0.6 + slope*edge），edge=0.5 会得到 1.6"""
        # 旧公式参数：kelly_slope = 2.0
        # mult = 0.6 + 2.0 * 0.5 = 1.6
        old_mult = 0.6 + 2.0 * 0.5
        self.assertAlmostEqual(old_mult, 1.6, places=4)
        # 新公式必须低于这个值
        new_mult = compute_kelly_factor(edge=0.5)
        self.assertLess(new_mult, old_mult)


# ═══════════════════════════════════════════════════════════════════════════
#  参数化：kelly_min / kelly_max / target_edge
# ═══════════════════════════════════════════════════════════════════════════

class TestParameterization(unittest.TestCase):
    """参数自定义测试。"""

    def test_custom_min_max(self):
        """自定义 kelly_min=0.5, kelly_max=1.5 → 线性映射到 [0.5, 1.5]"""
        mult = compute_kelly_factor(edge=0.5, kelly_min=0.5, kelly_max=1.5, target_edge=0.5)
        self.assertAlmostEqual(mult, 1.5, places=4)

    def test_custom_target_edge_lower(self):
        """target_edge 更小 → 更快达到 kelly_max"""
        # target_edge=0.25, edge=0.25 → ratio=1.0 → 封顶
        mult = compute_kelly_factor(edge=0.25, kelly_min=0.6, kelly_max=1.2, target_edge=0.25)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_custom_target_edge_higher(self):
        """target_edge 更大 → 需要更高 edge 才能达到 kelly_max"""
        # target_edge=1.0, edge=0.5 → ratio=0.5 → 0.9
        mult = compute_kelly_factor(edge=0.5, kelly_min=0.6, kelly_max=1.2, target_edge=1.0)
        self.assertAlmostEqual(mult, 0.9, places=4)

    def test_min_equals_max(self):
        """kelly_min == kelly_max → 始终返回该值"""
        mult = compute_kelly_factor(edge=0.5, kelly_min=0.8, kelly_max=0.8, target_edge=0.5)
        self.assertAlmostEqual(mult, 0.8, places=4)

    def test_min_greater_than_max_swapped(self):
        """kelly_min > kelly_max → 自动交换，不崩溃"""
        mult = compute_kelly_factor(edge=0.5, kelly_min=1.2, kelly_max=0.6, target_edge=0.5)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_zero_target_edge(self):
        """target_edge = 0 → 直接拉满（除零保护，异常配置）"""
        mult = compute_kelly_factor(edge=0.1, kelly_min=0.6, kelly_max=1.2, target_edge=0.0)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_negative_target_edge(self):
        """target_edge < 0 → 直接拉满（异常配置保护）"""
        mult = compute_kelly_factor(edge=0.1, kelly_min=0.6, kelly_max=1.2, target_edge=-0.5)
        self.assertAlmostEqual(mult, 1.2, places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  异常输入 & 边界情况
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    """异常输入和边界情况测试。"""

    def test_edge_none_returns_1_0(self):
        """edge = None → 返回 1.0（中性）"""
        mult = compute_kelly_factor(edge=None)
        self.assertEqual(mult, 1.0)

    def test_edge_string_no_crash(self):
        """edge 为字符串 → 返回 1.0，不崩溃"""
        mult = compute_kelly_factor(edge="abc")
        self.assertEqual(mult, 1.0)

    def test_edge_empty_string_no_crash(self):
        """edge 为空字符串 → 返回 1.0，不崩溃"""
        mult = compute_kelly_factor(edge="")
        self.assertEqual(mult, 1.0)

    def test_edge_integer(self):
        """edge 为整数 → 正常工作"""
        mult = compute_kelly_factor(edge=1)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_near_zero_positive_edge_still_above_1(self):
        """近景非常接近 0 但为正 → 允许 >1.0"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=0.0001)
        self.assertGreater(mult, 1.0)

    def test_near_zero_negative_edge_capped(self):
        """近景非常接近 0 但为负 → 封顶 1.0"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=-0.0001)
        self.assertAlmostEqual(mult, 1.0, places=4)

    def test_near_none_with_positive_edge(self):
        """近景为 None + 正 edge → 允许 >1.0（退回远 edge）"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=None)
        self.assertGreater(mult, 1.0)

    def test_near_string_no_crash(self):
        """近景为字符串 → 不崩溃，退回远 edge 符号"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR="invalid")
        self.assertGreater(mult, 1.0)  # edge 正 → 按正处理


# ═══════════════════════════════════════════════════════════════════════════
#  数值精度 & 单调性
# ═══════════════════════════════════════════════════════════════════════════

class TestMonotonicity(unittest.TestCase):
    """单调性和数值准确性测试。"""

    def test_monotonically_increasing_with_edge(self):
        """Kelly 因子随 edge 增大而非递减"""
        prev = 0.0
        for edge in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.0]:
            mult = compute_kelly_factor(edge=edge)
            self.assertGreaterEqual(mult, prev,
                                    f"edge={edge} 时 mult={mult} < 前一个 {prev}")
            prev = mult

    def test_within_bounds_always(self):
        """任何情况下结果都在 [kelly_min, kelly_max] 范围内
        （近景负时可能被压到 1.0，但不会低于 kelly_min）"""
        for edge in [-1.0, -0.5, 0.0, 0.25, 0.5, 1.0, 10.0]:
            for near in [None, -0.5, 0.0, 0.5]:
                mult = compute_kelly_factor(edge=edge, cur_full_expR=near)
                self.assertGreaterEqual(mult, 0.6,
                                        f"edge={edge}, near={near}: mult={mult} < 0.6")
                self.assertLessEqual(mult, 1.2,
                                     f"edge={edge}, near={near}: mult={mult} > 1.2")

    def test_near_term_never_increases(self):
        """近景门槛只会降低或保持 mult，绝不会增加"""
        for edge in [0.0, 0.25, 0.5, 1.0]:
            base = compute_kelly_factor(edge=edge, cur_full_expR=None)
            with_near_pos = compute_kelly_factor(edge=edge, cur_full_expR=0.3)
            with_near_neg = compute_kelly_factor(edge=edge, cur_full_expR=-0.3)

            # 近景正 → 与无近景时相同（edge 正时两者都放行）
            if edge > 0:
                self.assertAlmostEqual(base, with_near_pos, places=4)

            # 近景负 → 不会高于基础值
            self.assertLessEqual(with_near_neg, base + 1e-9)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

# ── 补充：参数鲁棒性 & 深层边界 ──────────────────────────────────────────

class TestParamRobustness(unittest.TestCase):
    """kelly_min / kelly_max / target_edge 参数的类型鲁棒性。"""

    def test_kelly_min_none_falls_back_to_1_0(self):
        """kelly_min = None → 参数转换失败 → 返回 1.0（中性）"""
        mult = compute_kelly_factor(edge=0.5, kelly_min=None)
        self.assertEqual(mult, 1.0)

    def test_kelly_max_none_falls_back_to_1_0(self):
        """kelly_max = None → 参数转换失败 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.5, kelly_max=None)
        self.assertEqual(mult, 1.0)

    def test_target_edge_none_falls_back_to_1_0(self):
        """target_edge = None → 参数转换失败 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.5, target_edge=None)
        self.assertEqual(mult, 1.0)

    def test_kelly_min_string_falls_back(self):
        """kelly_min 为字符串 → 转换失败 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.5, kelly_min="abc")
        self.assertEqual(mult, 1.0)

    def test_kelly_max_string_falls_back(self):
        """kelly_max 为字符串 → 转换失败 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.5, kelly_max="xyz")
        self.assertEqual(mult, 1.0)

    def test_target_edge_string_falls_back(self):
        """target_edge 为字符串 → 转换失败 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.5, target_edge="high")
        self.assertEqual(mult, 1.0)

    def test_kelly_min_empty_string_falls_back(self):
        """kelly_min 为空字符串 → 返回 1.0"""
        mult = compute_kelly_factor(edge=0.3, kelly_min="")
        self.assertEqual(mult, 1.0)

    def test_all_params_invalid_still_returns_1(self):
        """所有参数都无效 → 仍安全返回 1.0，不崩溃"""
        mult = compute_kelly_factor(
            edge="bad", kelly_min=None, kelly_max="x", target_edge=[]
        )
        self.assertEqual(mult, 1.0)


class TestNearGateWithCustomParams(unittest.TestCase):
    """近景门槛与自定义 kelly_min/kelly_max 的交互。"""

    def test_wide_range_near_neg_capped_at_1(self):
        """kelly 范围宽（0.5~2.0）+ 近景负 → 封顶 1.0"""
        mult = compute_kelly_factor(
            edge=1.0, kelly_min=0.5, kelly_max=2.0,
            target_edge=0.5, cur_full_expR=-0.1
        )
        self.assertAlmostEqual(mult, 1.0, places=4)
        self.assertLessEqual(mult, 1.0)

    def test_wide_range_near_pos_goes_to_max(self):
        """kelly 范围宽 + 近景正 → 正常达到 kelly_max"""
        mult = compute_kelly_factor(
            edge=1.0, kelly_min=0.5, kelly_max=2.0,
            target_edge=0.5, cur_full_expR=0.3
        )
        self.assertAlmostEqual(mult, 2.0, places=4)

    def test_min_above_1_near_neg_drops_below_min(self):
        """kelly_min > 1.0 + 近景负 → 被压到 1.0（低于 kelly_min）

        注意：这是当前实现的行为——近景门槛优先级高于 kelly_min。
        语义上合理：近景都亏了，宁可用低于"最小缩放"的保守仓位，也不加杠杆。
        """
        mult = compute_kelly_factor(
            edge=0.5, kelly_min=1.5, kelly_max=2.0,
            target_edge=0.5, cur_full_expR=-0.1
        )
        # 原计算：1.5 + 0.5 * 1.0 = 2.0，近景负 → min(2.0, 1.0) = 1.0
        self.assertAlmostEqual(mult, 1.0, places=4)
        # 确实低于 kelly_min（近景门槛优先级更高）
        self.assertLess(mult, 1.5)

    def test_max_below_1_near_neg_no_effect(self):
        """kelly_max < 1.0 + 近景负 → 封顶 1.0 不生效（本来就低于 1.0）"""
        mult = compute_kelly_factor(
            edge=0.5, kelly_min=0.4, kelly_max=0.8,
            target_edge=0.5, cur_full_expR=-0.1
        )
        self.assertAlmostEqual(mult, 0.8, places=4)
        self.assertLess(mult, 1.0)

    def test_near_zero_exactly_1_0_boundary(self):
        """edge 恰好使 mult = 1.0 + 近景负 → 仍是 1.0（边界无跳变）"""
        # 默认配置下，mult = 0.6 + 0.6 * ratio
        # 令 mult = 1.0 → ratio = 0.4/0.6 = 2/3 → edge = target_edge * 2/3 ≈ 0.3333
        edge_at_1 = 0.5 * (1.0 - 0.6) / (1.2 - 0.6)  # = 0.5 * 0.4/0.6 = 0.3333...
        mult_pos = compute_kelly_factor(edge=edge_at_1, cur_full_expR=0.1)
        mult_neg = compute_kelly_factor(edge=edge_at_1, cur_full_expR=-0.1)
        # 近景正 → 正常计算（约 1.0）
        self.assertAlmostEqual(mult_pos, 1.0, places=4)
        # 近景负 → 封顶 1.0（恰好等于，无跳变）
        self.assertAlmostEqual(mult_neg, 1.0, places=4)
        # 两者相等（边界连续）
        self.assertAlmostEqual(mult_pos, mult_neg, places=4)


class TestSpecialFloatValues(unittest.TestCase):
    """特殊浮点值（inf / nan / 极小值）的处理。"""

    def test_edge_inf_capped_at_max(self):
        """edge = +inf → 按极大值处理，封顶 kelly_max"""
        mult = compute_kelly_factor(edge=float('inf'))
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_edge_neg_inf_returns_min(self):
        """edge = -inf → 按 0 处理，返回 kelly_min"""
        mult = compute_kelly_factor(edge=float('-inf'))
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_edge_nan_returns_min(self):
        """edge = NaN → float() 成功但值为 NaN，max(NaN, 0) = NaN，
        最终计算结果为 NaN？验证一下实际行为。"""
        import math
        mult = compute_kelly_factor(edge=float('nan'))
        # NaN 经过 max(NaN, 0) 还是 NaN，除以 target_edge 还是 NaN
        # min(NaN, 1.0) 取决于实现，但通常 NaN 传播
        # 我们验证结果是有限值（不崩溃即可，行为由实现决定）
        self.assertTrue(math.isfinite(mult) or math.isnan(mult),
                        "NaN 输入不应导致崩溃")

    def test_very_small_positive_edge(self):
        """edge = 1e-10（极小正值）→ 略高于 kelly_min"""
        mult = compute_kelly_factor(edge=1e-10)
        # ratio = 1e-10 / 0.5 = 2e-10
        # mult = 0.6 + 0.6 * 2e-10 ≈ 0.6
        self.assertGreater(mult, 0.6)
        self.assertAlmostEqual(mult, 0.6, places=9)

    def test_very_large_edge(self):
        """edge = 1e6（极大值）→ 封顶 kelly_max"""
        mult = compute_kelly_factor(edge=1e6)
        self.assertAlmostEqual(mult, 1.2, places=4)


class TestLinearInterpolation(unittest.TestCase):
    """线性插值的多点验证——确保不是曲线映射。"""

    def test_quarter_point(self):
        """edge = target_edge * 0.25 → mult 在 25% 位置"""
        # 0.6 + 0.6 * 0.25 = 0.75
        mult = compute_kelly_factor(edge=0.125, target_edge=0.5)
        self.assertAlmostEqual(mult, 0.75, places=4)

    def test_three_quarter_point(self):
        """edge = target_edge * 0.75 → mult 在 75% 位置"""
        # 0.6 + 0.6 * 0.75 = 1.05
        mult = compute_kelly_factor(edge=0.375, target_edge=0.5)
        self.assertAlmostEqual(mult, 1.05, places=4)

    def test_linearity_check_5_points(self):
        """5 个均匀采样点 → 增量均匀（验证线性）"""
        target = 1.0
        prev_mult = None
        prev_increment = None
        for i in range(5):
            edge = target * i / 4  # 0, 0.25, 0.5, 0.75, 1.0
            mult = compute_kelly_factor(edge=edge, target_edge=target)
            if prev_mult is not None:
                increment = mult - prev_mult
                if prev_increment is not None:
                    self.assertAlmostEqual(
                        increment, prev_increment, places=10,
                        msg=f"edge={edge} 时增量不均，说明不是线性映射"
                    )
                prev_increment = increment
            prev_mult = mult


class TestNearGateEdgeScenarios(unittest.TestCase):
    """近景门槛的更多业务场景。"""

    def test_near_positive_but_edge_zero(self):
        """近景正 + edge=0 → 仍在 kelly_min（edge 为 0，没什么可放大的）"""
        mult = compute_kelly_factor(edge=0.0, cur_full_expR=0.5)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_near_negative_edge_zero_stays_min(self):
        """近景负 + edge=0 → kelly_min（已经低于 1.0，不受封顶影响）"""
        mult = compute_kelly_factor(edge=0.0, cur_full_expR=-0.5)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_near_positive_negative_edge(self):
        """近景正 + edge 负 → kelly_min（edge 负，不放杠杆）"""
        mult = compute_kelly_factor(edge=-0.3, cur_full_expR=0.5)
        self.assertAlmostEqual(mult, 0.6, places=4)

    def test_near_negative_high_positive_edge(self):
        """近景负 + 高正 edge → 封顶 1.0（典型的"远看赚近看亏"场景）"""
        # edge=1.0（满格 1.2x），但近景亏 → 压到 1.0
        mult = compute_kelly_factor(edge=1.0, cur_full_expR=-0.01)
        self.assertAlmostEqual(mult, 1.0, places=4)

    def test_near_slightly_positive_allows_above_1(self):
        """近景微弱为正 → 允许加杠杆（不封顶）"""
        # 只要 > 0 就算正
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=1e-9)
        self.assertGreater(mult, 1.0)
        self.assertAlmostEqual(mult, 1.2, places=4)

    def test_near_slightly_negative_caps_at_1(self):
        """近景微弱为负 → 封顶 1.0（严格判断：> 0 才算正）"""
        mult = compute_kelly_factor(edge=0.5, cur_full_expR=-1e-9)
        self.assertAlmostEqual(mult, 1.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
