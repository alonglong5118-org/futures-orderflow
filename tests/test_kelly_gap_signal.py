#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kelly因子 + 缺口止损 + 信号触发 — 单元测试
==============================================

1. compute_kelly_factor — Kelly 仓位缩放
   - edge=None → 1.0
   - edge 格式错误 → 1.0
   - edge=0 → kelly_min
   - edge=target → kelly_max
   - edge 在中间 → 线性插值
   - edge > target → 封顶 kelly_max
   - 负 edge → 按 0 处理（kelly_min）
   - kelly_min > kelly_max → 自动交换
   - target_edge=0 → 直接拉满
   - 近景正 → 允许 >1.0 杠杆
   - 近景负 → 封顶 1.0
   - 近景 None → 用远 edge 符号
   - 近景数据异常 → 退回远 edge
   - 参数格式错误 → 返回 1.0

2. check_gap_stop_triggered — 缺口击穿判定
   - 方向为 0 → 不触发
   - stop=None → 不触发
   - entry_price=None → 不触发
   - 多单不利方向（px<stop）+ 穿透>0.5R → 触发
   - 多单有利方向 → 不触发
   - 空单不利方向（px>stop）+ 穿透>0.5R → 触发
   - 空单有利方向 → 不触发
   - 穿透恰好 0.5R → 不触发（严格大于）
   - 穿透小于 0.5R → 不触发
   - oneR=0 → 不触发（除零保护）
   - 价格格式错误 → 不触发
   - 返回 5 字段
   - pen_ratio = pen / oneR
   - is_adverse 正确标记

3. check_hard_veto — F/C 反向硬否决
   - 同向且强 → 不决绝
   - 反向且强 → 否决
   - 反向但弱 → 不决绝
   - dir_T=0 → 不决绝
   - bias_FC=0 → 不决绝
   - 恰好等于阈值 → 否决（>=）
   - 返回 (bool, str) 二元组

4. check_fc_confirmation — F/C 同向确认
   - 同向且强 → True
   - 反向 → False
   - 同向但弱 → False
   - dir_T=0 → False
   - bias_FC=0 → False
   - 恰好等于阈值 → True

5. compute_effective_threshold — 有效触发阈值
   - 有确认 → 阈值 × confirm_relief
   - 无确认 → 原阈值
   - confirm_relief < 1 → 降低阈值
   - 返回 float

6. check_same_direction — bias_G 与 T 同向判断
   - bias_G 正 + dir_T 正 → True
   - bias_G 负 + dir_T 负 → True
   - bias_G 正 + dir_T 负 → False
   - bias_G 负 + dir_T 正 → False
   - bias_G≈0 → True（中性背景）
   - dir_T=0 → False

7. signal_trigger_decision — 信号触发决策
   - dir_T=0 → 不触发
   - 被硬否决 → 不触发，hard_veto=True
   - 同向确认 → 阈值降低
   - 同向且 T 足够强 → 触发
   - 不同向 → 不触发
   - T 不够强 → 不触发
   - 完整流程：确认+同向+强T → 触发
   - 返回 6 字段
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kelly_utils import compute_kelly_factor
from gap_stop_utils import check_gap_stop_triggered
from signal_trigger_utils import (
    check_hard_veto, check_fc_confirmation, compute_effective_threshold,
    check_same_direction, signal_trigger_decision,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. compute_kelly_factor
# ═══════════════════════════════════════════════════════════════════════════

class TestKellyFactor(unittest.TestCase):
    """compute_kelly_factor Kelly 仓位缩放。"""

    def test_none_edge_returns_one(self):
        """edge=None → 1.0"""
        self.assertEqual(compute_kelly_factor(None), 1.0)

    def test_invalid_edge_returns_one(self):
        """edge 格式错误 → 1.0"""
        self.assertEqual(compute_kelly_factor("abc"), 1.0)

    def test_zero_edge_returns_min(self):
        """edge=0 → kelly_min"""
        self.assertEqual(compute_kelly_factor(0, kelly_min=0.6, kelly_max=1.2, target_edge=0.5), 0.6)

    def test_full_edge_returns_max(self):
        """edge=target → kelly_max"""
        # edge=0.5, target=0.5 → ratio=1 → 0.6 + 0.6*1 = 1.2
        self.assertEqual(compute_kelly_factor(0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5), 1.2)

    def test_half_edge_linear_interp(self):
        """edge 在中间 → 线性插值"""
        # edge=0.25, target=0.5 → ratio=0.5 → 0.6 + 0.6*0.5 = 0.9
        self.assertAlmostEqual(
            compute_kelly_factor(0.25, kelly_min=0.6, kelly_max=1.2, target_edge=0.5),
            0.9, places=6
        )

    def test_edge_exceeds_target_capped(self):
        """edge > target → 封顶 kelly_max"""
        self.assertEqual(compute_kelly_factor(1.0, kelly_min=0.6, kelly_max=1.2, target_edge=0.5), 1.2)

    def test_negative_edge_treated_as_zero(self):
        """负 edge → 按 0 处理（kelly_min）"""
        self.assertEqual(compute_kelly_factor(-0.3, kelly_min=0.6, kelly_max=1.2, target_edge=0.5), 0.6)

    def test_min_greater_than_max_swapped(self):
        """kelly_min > kelly_max → 自动交换"""
        # min=1.2, max=0.6 → 交换后 min=0.6, max=1.2
        self.assertEqual(compute_kelly_factor(0, kelly_min=1.2, kelly_max=0.6, target_edge=0.5), 0.6)
        self.assertEqual(compute_kelly_factor(0.5, kelly_min=1.2, kelly_max=0.6, target_edge=0.5), 1.2)

    def test_zero_target_edge_pulls_max(self):
        """target_edge=0 → 直接拉满（异常配置保护）"""
        self.assertEqual(compute_kelly_factor(0.1, kelly_min=0.6, kelly_max=1.2, target_edge=0), 1.2)

    def test_positive_near_allows_above_one(self):
        """近景正 → 允许 >1.0 杠杆"""
        result = compute_kelly_factor(0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5,
                                       cur_full_expR=0.3)
        self.assertEqual(result, 1.2)
        self.assertGreater(result, 1.0)

    def test_negative_near_caps_at_one(self):
        """近景负 → 封顶 1.0"""
        result = compute_kelly_factor(0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5,
                                       cur_full_expR=-0.2)
        self.assertEqual(result, 1.0)

    def test_none_near_uses_far_edge_sign(self):
        """近景 None → 用远 edge 符号"""
        # 远 edge 正 → 允许 >1.0
        r1 = compute_kelly_factor(0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5,
                                   cur_full_expR=None)
        self.assertEqual(r1, 1.2)
        # 远 edge 负 → 封顶 1.0
        r2 = compute_kelly_factor(-0.1, kelly_min=0.6, kelly_max=1.2, target_edge=0.5,
                                   cur_full_expR=None)
        # 负 edge 按 0 算 → mult=0.6，且 near_pos=False → min(0.6, 1.0) = 0.6
        self.assertEqual(r2, 0.6)

    def test_invalid_near_falls_back(self):
        """近景数据异常 → 退回远 edge"""
        result = compute_kelly_factor(0.5, kelly_min=0.6, kelly_max=1.2, target_edge=0.5,
                                       cur_full_expR="bad")
        self.assertEqual(result, 1.2)

    def test_invalid_params_return_one(self):
        """参数格式错误 → 返回 1.0"""
        self.assertEqual(compute_kelly_factor(0.5, kelly_min="bad"), 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(compute_kelly_factor(0.3), float)


# ═══════════════════════════════════════════════════════════════════════════
#  2. check_gap_stop_triggered
# ═══════════════════════════════════════════════════════════════════════════

class TestGapStopTriggered(unittest.TestCase):
    """check_gap_stop_triggered 缺口击穿判定。"""

    def test_zero_direction_not_triggered(self):
        """方向为 0 → 不触发"""
        r = check_gap_stop_triggered(0, 90, 95, 100)
        self.assertFalse(r["triggered"])

    def test_none_stop_not_triggered(self):
        """stop=None → 不触发"""
        r = check_gap_stop_triggered(1, 90, None, 100)
        self.assertFalse(r["triggered"])

    def test_none_entry_not_triggered(self):
        """entry_price=None → 不触发"""
        r = check_gap_stop_triggered(1, 90, 95, None)
        self.assertFalse(r["triggered"])

    def test_long_adverse_deep_penetration_triggers(self):
        """多单不利方向（px<stop）+ 穿透>0.5R → 触发"""
        # 多单：entry=100, stop=90 → oneR=10
        # px=82 → pen=8 → 8/10=0.8R > 0.5R → 触发
        r = check_gap_stop_triggered(1, 82, 90, 100)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["is_adverse"])
        self.assertEqual(r["oneR"], 10.0)
        self.assertEqual(r["pen"], 8.0)
        self.assertAlmostEqual(r["pen_ratio"], 0.8)

    def test_long_favorable_not_triggered(self):
        """多单有利方向 → 不触发"""
        # 多单：px > stop → 有利
        r = check_gap_stop_triggered(1, 110, 90, 100)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["is_adverse"])

    def test_short_adverse_deep_penetration_triggers(self):
        """空单不利方向（px>stop）+ 穿透>0.5R → 触发"""
        # 空单：entry=100, stop=110 → oneR=10
        # px=118 → pen=8 → 0.8R > 0.5R → 触发
        r = check_gap_stop_triggered(-1, 118, 110, 100)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["is_adverse"])

    def test_short_favorable_not_triggered(self):
        """空单有利方向 → 不触发"""
        # 空单：px < stop → 有利
        r = check_gap_stop_triggered(-1, 90, 110, 100)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["is_adverse"])

    def test_exactly_half_R_not_triggered(self):
        """穿透恰好 0.5R → 不触发（严格大于）"""
        # oneR=10, pen=5 → 0.5R → 不触发
        r = check_gap_stop_triggered(1, 85, 90, 100)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["is_adverse"])
        self.assertEqual(r["pen_ratio"], 0.5)

    def test_below_half_R_not_triggered(self):
        """穿透小于 0.5R → 不触发"""
        # oneR=10, pen=3 → 0.3R → 不触发
        r = check_gap_stop_triggered(1, 87, 90, 100)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["is_adverse"])

    def test_zero_oneR_not_triggered(self):
        """oneR=0 → 不触发（除零保护）"""
        r = check_gap_stop_triggered(1, 100, 100, 100)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["oneR"], 0.0)
        self.assertEqual(r["pen_ratio"], 0.0)

    def test_bad_price_format_not_triggered(self):
        """价格格式错误 → 不触发"""
        r = check_gap_stop_triggered(1, "abc", 90, 100)
        self.assertFalse(r["triggered"])

    def test_return_five_fields(self):
        """返回 5 字段"""
        r = check_gap_stop_triggered(1, 82, 90, 100)
        for key in ("triggered", "is_adverse", "oneR", "pen", "pen_ratio"):
            self.assertIn(key, r)

    def test_pen_ratio_formula(self):
        """pen_ratio = pen / oneR"""
        r = check_gap_stop_triggered(1, 80, 90, 100)
        # oneR=10, pen=10 → ratio=1.0
        self.assertEqual(r["pen_ratio"], 1.0)

    def test_is_adverse_long_below_stop(self):
        """多单 px < stop → is_adverse=True"""
        r = check_gap_stop_triggered(1, 85, 90, 100)
        self.assertTrue(r["is_adverse"])

    def test_is_adverse_short_above_stop(self):
        """空单 px > stop → is_adverse=True"""
        r = check_gap_stop_triggered(-1, 115, 110, 100)
        self.assertTrue(r["is_adverse"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. check_hard_veto
# ═══════════════════════════════════════════════════════════════════════════

class TestHardVeto(unittest.TestCase):
    """check_hard_veto F/C 反向硬否决。"""

    def test_same_direction_strong_no_veto(self):
        """同向且强 → 不决绝"""
        veto, reason = check_hard_veto(30, 1, fc_hard=25)
        self.assertFalse(veto)
        self.assertEqual(reason, "")

    def test_opposite_strong_veto(self):
        """反向且强 → 否决"""
        veto, reason = check_hard_veto(-30, 1, fc_hard=25)
        self.assertTrue(veto)
        self.assertIn("硬否决", reason)

    def test_opposite_weak_no_veto(self):
        """反向但弱 → 不决绝"""
        veto, _ = check_hard_veto(-10, 1, fc_hard=25)
        self.assertFalse(veto)

    def test_zero_dir_no_veto(self):
        """dir_T=0 → 不决绝"""
        veto, _ = check_hard_veto(-30, 0, fc_hard=25)
        self.assertFalse(veto)

    def test_zero_bias_no_veto(self):
        """bias_FC=0 → 不决绝"""
        veto, _ = check_hard_veto(0, 1, fc_hard=25)
        self.assertFalse(veto)

    def test_at_threshold_veto(self):
        """恰好等于阈值 → 否决（>=）"""
        veto, _ = check_hard_veto(-25, 1, fc_hard=25)
        self.assertTrue(veto)

    def test_returns_tuple(self):
        """返回 (bool, str) 二元组"""
        result = check_hard_veto(30, 1)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], str)

    def test_short_bias_opposite_veto(self):
        """空单方向，正 bias_FC → 反向否决"""
        veto, _ = check_hard_veto(30, -1, fc_hard=25)
        self.assertTrue(veto)


# ═══════════════════════════════════════════════════════════════════════════
#  4. check_fc_confirmation
# ═══════════════════════════════════════════════════════════════════════════

class TestFcConfirmation(unittest.TestCase):
    """check_fc_confirmation F/C 同向确认。"""

    def test_same_direction_strong_confirmed(self):
        """同向且强 → True"""
        self.assertTrue(check_fc_confirmation(30, 1, fc_confirm=25))

    def test_opposite_not_confirmed(self):
        """反向 → False"""
        self.assertFalse(check_fc_confirmation(-30, 1, fc_confirm=25))

    def test_same_but_weak_not_confirmed(self):
        """同向但弱 → False"""
        self.assertFalse(check_fc_confirmation(10, 1, fc_confirm=25))

    def test_zero_dir_not_confirmed(self):
        """dir_T=0 → False"""
        self.assertFalse(check_fc_confirmation(30, 0, fc_confirm=25))

    def test_zero_bias_not_confirmed(self):
        """bias_FC=0 → False"""
        self.assertFalse(check_fc_confirmation(0, 1, fc_confirm=25))

    def test_at_threshold_confirmed(self):
        """恰好等于阈值 → True"""
        self.assertTrue(check_fc_confirmation(25, 1, fc_confirm=25))

    def test_short_same_direction(self):
        """空单同向（负 bias_FC，-1 dir）→ True"""
        self.assertTrue(check_fc_confirmation(-30, -1, fc_confirm=25))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(check_fc_confirmation(30, 1), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  5. compute_effective_threshold
# ═══════════════════════════════════════════════════════════════════════════

class TestEffectiveThreshold(unittest.TestCase):
    """compute_effective_threshold 有效触发阈值。"""

    def test_confirmed_reduces_threshold(self):
        """有确认 → 阈值 × confirm_relief"""
        # 100 × 0.85 = 85
        self.assertEqual(compute_effective_threshold(100, True, confirm_relief=0.85), 85.0)

    def test_not_confirmed_same_threshold(self):
        """无确认 → 原阈值"""
        self.assertEqual(compute_effective_threshold(100, False, confirm_relief=0.85), 100)

    def test_relief_below_one_lowers_threshold(self):
        """confirm_relief < 1 → 降低阈值"""
        result = compute_effective_threshold(50, True, confirm_relief=0.7)
        self.assertEqual(result, 35.0)
        self.assertLess(result, 50)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(compute_effective_threshold(50, True), float)

    def test_relief_equals_one_no_change(self):
        """confirm_relief=1 → 不变"""
        self.assertEqual(compute_effective_threshold(50, True, confirm_relief=1.0), 50.0)


# ═══════════════════════════════════════════════════════════════════════════
#  6. check_same_direction
# ═══════════════════════════════════════════════════════════════════════════

class TestSameDirection(unittest.TestCase):
    """check_same_direction bias_G 与 T 同向判断。"""

    def test_both_positive_true(self):
        """bias_G 正 + dir_T 正 → True"""
        self.assertTrue(check_same_direction(10, 1))

    def test_both_negative_true(self):
        """bias_G 负 + dir_T 负 → True"""
        self.assertTrue(check_same_direction(-10, -1))

    def test_pos_neg_false(self):
        """bias_G 正 + dir_T 负 → False"""
        self.assertFalse(check_same_direction(10, -1))

    def test_neg_pos_false(self):
        """bias_G 负 + dir_T 正 → False"""
        self.assertFalse(check_same_direction(-10, 1))

    def test_neutral_bias_true(self):
        """bias_G≈0 → True（中性背景）"""
        self.assertTrue(check_same_direction(0, 1))
        self.assertTrue(check_same_direction(0, -1))
        self.assertTrue(check_same_direction(1e-7, 1))

    def test_zero_dir_false(self):
        """dir_T=0 → False"""
        self.assertFalse(check_same_direction(10, 0))
        self.assertFalse(check_same_direction(-10, 0))
        self.assertFalse(check_same_direction(0, 0))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(check_same_direction(10, 1), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  7. signal_trigger_decision
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalTriggerDecision(unittest.TestCase):
    """signal_trigger_decision 信号触发决策。"""

    def _base(self, **overrides):
        base = {
            "T_5m": 60.0,
            "dir_T": 1,
            "T_thresh_eff": 50.0,
            "bias_G": 20.0,
            "bias_FC": 10.0,
            "fc_confirm": 25.0,
            "confirm_relief": 0.85,
            "fc_hard": 25.0,
        }
        base.update(overrides)
        return signal_trigger_decision(**base)

    def test_zero_dir_not_triggered(self):
        """dir_T=0 → 不触发"""
        r = self._base(dir_T=0)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["hard_veto"])

    def test_hard_veto_blocks_trigger(self):
        """被硬否决 → 不触发，hard_veto=True"""
        # bias_FC=-30（反向且强）+ dir_T=1 → 硬否决
        r = self._base(bias_FC=-30, fc_hard=25)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["hard_veto"])
        self.assertIn("硬否决", r["hard_veto_reason"])

    def test_fc_confirmed_lowers_threshold(self):
        """同向确认 → 阈值降低"""
        # bias_FC=30, dir_T=1 → 同向确认
        r = self._base(bias_FC=30, fc_confirm=25, confirm_relief=0.8)
        self.assertTrue(r["fc_confirmed"])
        self.assertEqual(r["effective_thr"], 50.0 * 0.8)

    def test_same_dir_strong_T_triggers(self):
        """同向且 T 足够强 → 触发"""
        # T=60 > threshold=50, bias_G=20（同向）
        r = self._base(T_5m=60, T_thresh_eff=50, bias_G=20, bias_FC=10)
        self.assertTrue(r["triggered"])
        self.assertTrue(r["same_dir"])

    def test_opposite_dir_not_triggered(self):
        """不同向 → 不触发"""
        # bias_G=-20 与 dir_T=1 反向
        r = self._base(bias_G=-20)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["same_dir"])

    def test_weak_T_not_triggered(self):
        """T 不够强 → 不触发"""
        # T=30 < threshold=50
        r = self._base(T_5m=30, T_thresh_eff=50)
        self.assertFalse(r["triggered"])
        self.assertTrue(r["same_dir"])

    def test_full_pipeline_triggers(self):
        """完整流程：确认+同向+强T → 触发"""
        r = self._base(
            T_5m=45,          # 原本 45 < 50
            T_thresh_eff=50,
            bias_G=20,        # 同向
            bias_FC=30,       # 同向确认
            fc_confirm=25,
            confirm_relief=0.8,  # 阈值降到 40
        )
        # 确认后阈值 = 50*0.8 = 40，T=45 > 40 → 触发
        self.assertTrue(r["fc_confirmed"])
        self.assertEqual(r["effective_thr"], 40.0)
        self.assertTrue(r["same_dir"])
        self.assertTrue(r["triggered"])

    def test_return_six_fields(self):
        """返回 6 字段"""
        r = self._base()
        for key in ("triggered", "hard_veto", "hard_veto_reason",
                     "fc_confirmed", "effective_thr", "same_dir"):
            self.assertIn(key, r)

    def test_at_threshold_triggers(self):
        """T 恰好等于阈值 → 触发（>=）"""
        r = self._base(T_5m=50, T_thresh_eff=50, bias_G=10)
        self.assertTrue(r["triggered"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Kelly因子 + 缺口止损 + 信号触发 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
