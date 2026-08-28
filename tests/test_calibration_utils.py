#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校准工具函数 — 单元测试
===========================

1. blend_weights — GA 权重与默认权重融合
   - alpha=1 → 完全用 GA 权重
   - alpha=0 → 完全用默认权重
   - alpha=0.5 → 各取一半
   - 返回 T/F/C 三个维度
   - 结果保留 6 位小数
   - 中间值线性插值

2. best_stop_rr — 从 sweep 结果挑最优参数
   - 优先选满足「胜率≥0.4 + 交易数≥min_trades」的
   - 没有合格的 → 放宽到只看交易数
   - 还是没有 → None
   - 在合格组里选 expR 最高的
   - 返回字段齐全：stop_atr_mult, rr_ratio, expR, win_rate, trades
   - 多组候选中选最优

3. _norm_tanh — tanh 归一化
   - x=0 → 0
   - x>0 → 正（<1）
   - x<0 → 负（>-1）
   - x 很大 → 接近 1
   - x 很小 → 接近 -1
   - scale=0 → 0（除零保护）
   - scale 越大 → 变化越平缓
   - 奇函数：f(-x) = -f(x)
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from apply_blended_weights import blend_weights
from four_dim_calibrate import best_stop_rr
from macro_context import _norm_tanh

# ═══════════════════════════════════════════════════════════════════════════
#  1. blend_weights
# ═══════════════════════════════════════════════════════════════════════════


class TestBlendWeights(unittest.TestCase):
    """blend_weights GA 权重与默认权重融合。"""

    def test_alpha_one_full_ga(self):
        """alpha=1 → 完全用 GA 权重"""
        ga_w = {"T": 0.6, "F": 0.3, "C": 0.1}
        result = blend_weights(ga_w, alpha=1.0)
        self.assertAlmostEqual(result["T"], 0.6, places=6)
        self.assertAlmostEqual(result["F"], 0.3, places=6)
        self.assertAlmostEqual(result["C"], 0.1, places=6)

    def test_alpha_zero_full_default(self):
        """alpha=0 → 完全用默认权重"""
        ga_w = {"T": 0.6, "F": 0.3, "C": 0.1}
        result = blend_weights(ga_w, alpha=0.0)
        from apply_blended_weights import DEFAULT_W

        self.assertAlmostEqual(result["T"], DEFAULT_W["T"], places=6)
        self.assertAlmostEqual(result["F"], DEFAULT_W["F"], places=6)
        self.assertAlmostEqual(result["C"], DEFAULT_W["C"], places=6)

    def test_alpha_half_middle(self):
        """alpha=0.5 → 各取一半"""
        ga_w = {"T": 0.6, "F": 0.3, "C": 0.1}
        from apply_blended_weights import DEFAULT_W

        result = blend_weights(ga_w, alpha=0.5)
        expected_T = 0.5 * 0.6 + 0.5 * DEFAULT_W["T"]
        expected_F = 0.5 * 0.3 + 0.5 * DEFAULT_W["F"]
        expected_C = 0.5 * 0.1 + 0.5 * DEFAULT_W["C"]
        self.assertAlmostEqual(result["T"], expected_T, places=6)
        self.assertAlmostEqual(result["F"], expected_F, places=6)
        self.assertAlmostEqual(result["C"], expected_C, places=6)

    def test_returns_three_dimensions(self):
        """返回 T/F/C 三个维度"""
        ga_w = {"T": 0.5, "F": 0.3, "C": 0.2}
        result = blend_weights(ga_w, alpha=0.5)
        self.assertEqual(set(result.keys()), {"T", "F", "C"})

    def test_rounds_to_6_decimals(self):
        """结果保留 6 位小数"""
        ga_w = {"T": 1 / 3, "F": 1 / 3, "C": 1 / 3}
        result = blend_weights(ga_w, alpha=0.5)
        # round(1/3, 6) = 0.333333
        for v in result.values():
            s = f"{v:.10f}"
            # 第 7 位以后应该是 0
            self.assertEqual(s[8:], "0000")

    def test_linear_interpolation(self):
        """中间值线性插值"""
        ga_w = {"T": 0.8, "F": 0.1, "C": 0.1}
        # alpha=0 → default, alpha=1 → ga
        r0 = blend_weights(ga_w, alpha=0.0)
        r1 = blend_weights(ga_w, alpha=1.0)
        # alpha=0.3 应该在两者之间 30% 位置
        r03 = blend_weights(ga_w, alpha=0.3)
        expected_T = r0["T"] + 0.3 * (r1["T"] - r0["T"])
        self.assertAlmostEqual(r03["T"], expected_T, places=5)


# ═══════════════════════════════════════════════════════════════════════════
#  2. best_stop_rr
# ═══════════════════════════════════════════════════════════════════════════


class TestBestStopRr(unittest.TestCase):
    """best_stop_rr 从 sweep 结果挑最优参数。"""

    def _make_result(self, expR, win_rate, trades):
        return {"expR": expR, "win_rate": win_rate, "trades": trades}

    def test_picks_highest_expR_among_valid(self):
        """在合格组里选 expR 最高的"""
        sweep = {
            "all": [
                (1.0, 1.5, self._make_result(0.8, 0.5, 20)),
                (1.5, 2.0, self._make_result(1.2, 0.45, 25)),  # expR 最高
                (2.0, 2.5, self._make_result(0.5, 0.55, 30)),
            ]
        }
        best = best_stop_rr(sweep, min_trades=10)
        self.assertEqual(best["stop_atr_mult"], 1.5)
        self.assertEqual(best["rr_ratio"], 2.0)
        self.assertAlmostEqual(best["expR"], 1.2, places=6)

    def test_filters_low_win_rate_in_first_pass(self):
        """第一关：胜率 < 0.4 被过滤，先用胜率≥0.4的"""
        sweep = {
            "all": [
                (1.0, 1.5, self._make_result(2.0, 0.3, 20)),  # expR 高但胜率低
                (1.5, 2.0, self._make_result(1.0, 0.5, 25)),  # 胜率合格
            ]
        }
        best = best_stop_rr(sweep, min_trades=10)
        # 应该选胜率合格的那个，而不是 expR 更高但胜率不合格的
        self.assertEqual(best["stop_atr_mult"], 1.5)
        self.assertAlmostEqual(best["win_rate"], 0.5, places=6)

    def test_fallback_when_no_high_win_rate(self):
        """没有胜率≥0.4的 → 放宽到只看交易数"""
        sweep = {
            "all": [
                (1.0, 1.5, self._make_result(0.8, 0.3, 20)),  # 胜率都 < 0.4
                (1.5, 2.0, self._make_result(1.0, 0.35, 25)),
            ]
        }
        best = best_stop_rr(sweep, min_trades=10)
        # 放宽后选 expR 最高的
        self.assertEqual(best["stop_atr_mult"], 1.5)
        self.assertAlmostEqual(best["expR"], 1.0, places=6)

    def test_returns_none_when_no_valid(self):
        """交易数都不够 → None"""
        sweep = {
            "all": [
                (1.0, 1.5, self._make_result(2.0, 0.6, 5)),  # 交易数太少
            ]
        }
        best = best_stop_rr(sweep, min_trades=10)
        self.assertIsNone(best)

    def test_return_fields_complete(self):
        """返回字段齐全"""
        sweep = {"all": [(1.0, 1.5, self._make_result(1.0, 0.5, 20))]}
        best = best_stop_rr(sweep, min_trades=10)
        self.assertIn("stop_atr_mult", best)
        self.assertIn("rr_ratio", best)
        self.assertIn("expR", best)
        self.assertIn("win_rate", best)
        self.assertIn("trades", best)

    def test_empty_sweep_none(self):
        """空 sweep → None"""
        best = best_stop_rr({"all": []}, min_trades=10)
        self.assertIsNone(best)

    def test_no_all_key_none(self):
        """没有 all 键 → None"""
        best = best_stop_rr({}, min_trades=10)
        self.assertIsNone(best)

    def test_min_trades_threshold(self):
        """min_trades 门槛正确"""
        sweep = {
            "all": [
                (1.0, 1.5, self._make_result(1.0, 0.5, 9)),  # 不够
                (1.5, 2.0, self._make_result(0.8, 0.5, 10)),  # 刚好够
            ]
        }
        best = best_stop_rr(sweep, min_trades=10)
        self.assertEqual(best["trades"], 10)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _norm_tanh
# ═══════════════════════════════════════════════════════════════════════════


class TestNormTanh(unittest.TestCase):
    """_norm_tanh tanh 归一化。"""

    def test_zero_input_zero_output(self):
        """x=0 → 0"""
        self.assertEqual(_norm_tanh(0, scale=1.0), 0.0)

    def test_positive_input_positive_output(self):
        """x>0 → 正（<1）"""
        result = _norm_tanh(1.0, scale=1.0)
        self.assertGreater(result, 0)
        self.assertLess(result, 1.0)

    def test_negative_input_negative_output(self):
        """x<0 → 负（>-1）"""
        result = _norm_tanh(-1.0, scale=1.0)
        self.assertLess(result, 0)
        self.assertGreater(result, -1.0)

    def test_large_positive_near_one(self):
        """x 很大 → 接近 1"""
        result = _norm_tanh(10.0, scale=1.0)
        self.assertGreater(result, 0.99)
        self.assertLessEqual(result, 1.0)

    def test_large_negative_near_minus_one(self):
        """x 很小 → 接近 -1"""
        result = _norm_tanh(-10.0, scale=1.0)
        self.assertLess(result, -0.99)
        self.assertGreaterEqual(result, -1.0)

    def test_zero_scale_returns_zero(self):
        """scale=0 → 0（除零保护）"""
        self.assertEqual(_norm_tanh(5.0, scale=0), 0.0)
        self.assertEqual(_norm_tanh(5.0, scale=0.0), 0.0)

    def test_larger_scale_smoother(self):
        """scale 越大 → 相同 x 下变化越平缓（结果更接近 0）"""
        r_small = _norm_tanh(1.0, scale=1.0)
        r_large = _norm_tanh(1.0, scale=10.0)
        self.assertLess(abs(r_large), abs(r_small))

    def test_odd_function(self):
        """奇函数：f(-x) = -f(x)"""
        for x in [0.5, 1.0, 2.0, 5.0]:
            fp = _norm_tanh(x, scale=1.0)
            fn = _norm_tanh(-x, scale=1.0)
            self.assertAlmostEqual(fp, -fn, places=10)

    def test_scale_one_is_tanh_x(self):
        """scale=1 → 结果 = tanh(x)"""
        x = 0.5
        result = _norm_tanh(x, scale=1.0)
        self.assertAlmostEqual(result, math.tanh(x), places=10)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  校准工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
