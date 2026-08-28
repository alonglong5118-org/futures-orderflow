#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试工具函数 — 单元测试
=================================

1. calc_signal_agreement — 信号一致率
   - 双方空 → 1.0
   - 一方空 → 0.0
   - 完全一致 → 1.0
   - 完全不同 → 0.0
   - 部分重叠 → 交集 / 基线大小（宽松分母）
   - 当前比基线多 → 只看命中基线的比例
   - 列表去重（用 set）
   - 顺序无关
   - 单元素相同 → 1.0
   - 单元素不同 → 0.0

2. classify_status — 四维度状态分类
   - 全正常 → ok
   - 单个 warn → warn
   - 单个 critical → critical
   - 多个 warn → warn（计数累加）
   - 多个 critical → critical（计数累加）
   - warn + critical → critical（critical 优先）
   - 全 None → ok
   - sig_agree 低 → warn/critical
   - 返回 (status, criticals, warns) 三元组

3. fmt_delta — 带颜色格式化
   - None → N/A（灰色）
   - 正值 → 带 + 号
   - 负值 → 带 - 号
   - 超 crit → 红色加粗
   - 超 warn → 黄色
   - 正常 → 绿色
   - is_pct=True → ×100 显示
   - 包含 ANSI 转义码
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from regression_test import (
    C,
    calc_signal_agreement,
    classify_status,
    fmt_delta,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. calc_signal_agreement
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalAgreement(unittest.TestCase):
    """calc_signal_agreement 信号一致率。"""

    def test_both_empty_returns_one(self):
        """双方空 → 1.0"""
        self.assertEqual(calc_signal_agreement([], []), 1.0)

    def test_one_empty_returns_zero(self):
        """一方空 → 0.0"""
        self.assertEqual(calc_signal_agreement(["a"], []), 0.0)
        self.assertEqual(calc_signal_agreement([], ["a"]), 0.0)

    def test_identical_returns_one(self):
        """完全一致 → 1.0"""
        sigs = ["2026-01-01_多_趋势", "2026-01-02_空_震荡"]
        self.assertEqual(calc_signal_agreement(sigs, sigs), 1.0)

    def test_completely_different_returns_zero(self):
        """完全不同 → 0.0"""
        cur = ["a", "b", "c"]
        base = ["x", "y", "z"]
        self.assertEqual(calc_signal_agreement(cur, base), 0.0)

    def test_partial_overlap(self):
        """部分重叠 → 交集 / 基线大小"""
        # 基线 5 个，当前命中 3 个 → 3/5 = 0.6
        cur = ["a", "b", "c", "extra1", "extra2"]
        base = ["a", "b", "c", "d", "e"]
        result = calc_signal_agreement(cur, base)
        self.assertAlmostEqual(result, 0.6, places=6)

    def test_current_has_more(self):
        """当前比基线多 → 只看命中基线的比例"""
        # 基线 3 个，当前包含全部 3 个 + 额外 2 个 → 3/3 = 1.0
        cur = ["a", "b", "c", "d", "e"]
        base = ["a", "b", "c"]
        result = calc_signal_agreement(cur, base)
        self.assertAlmostEqual(result, 1.0, places=6)

    def test_current_has_fewer(self):
        """当前比基线少 → 交集/基线大小"""
        cur = ["a", "b"]
        base = ["a", "b", "c", "d"]
        result = calc_signal_agreement(cur, base)
        # 交集=2, 基线=4 → 0.5
        self.assertAlmostEqual(result, 0.5, places=6)

    def test_deduplicated_by_set(self):
        """列表去重（用 set）"""
        cur = ["a", "a", "b", "b", "c"]
        base = ["a", "b", "c", "c"]
        result = calc_signal_agreement(cur, base)
        self.assertAlmostEqual(result, 1.0, places=6)

    def test_order_independent(self):
        """顺序无关"""
        cur = ["a", "b", "c"]
        base = ["c", "a", "b"]
        result = calc_signal_agreement(cur, base)
        self.assertAlmostEqual(result, 1.0, places=6)

    def test_single_element_same(self):
        """单元素相同 → 1.0"""
        self.assertEqual(calc_signal_agreement(["x"], ["x"]), 1.0)

    def test_single_element_different(self):
        """单元素不同 → 0.0"""
        self.assertEqual(calc_signal_agreement(["x"], ["y"]), 0.0)

    def test_returns_float(self):
        """返回 float"""
        result = calc_signal_agreement(["a"], ["a"])
        self.assertIsInstance(result, float)


# ═══════════════════════════════════════════════════════════════════════════
#  2. classify_status
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyStatus(unittest.TestCase):
    """classify_status 四维度状态分类。"""

    def test_all_normal_ok(self):
        """全正常 → ok"""
        status, crit, warn = classify_status(0.0, 0.0, 0.0, 1.0)
        self.assertEqual(status, "ok")
        self.assertEqual(crit, 0)
        self.assertEqual(warn, 0)

    def test_single_warn(self):
        """单个 warn → warn"""
        # expr_delta = 0.02 → > 0.015 (warn), < 0.03 (crit) → warn
        status, crit, warn = classify_status(0.02, 0.0, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(crit, 0)
        self.assertEqual(warn, 1)

    def test_single_critical(self):
        """单个 critical → critical"""
        # expr_delta = 0.04 → > 0.03 (crit) → critical
        status, crit, warn = classify_status(0.04, 0.0, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crit, 1)
        self.assertEqual(warn, 0)

    def test_multiple_warns(self):
        """多个 warn → warn（计数累加）"""
        # expr=0.02 (warn), win=0.04 (warn), trades=0.2 (warn) → 3 warns
        status, crit, warn = classify_status(0.02, 0.04, 0.2, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(crit, 0)
        self.assertEqual(warn, 3)

    def test_multiple_criticals(self):
        """多个 critical → critical（计数累加）"""
        status, crit, warn = classify_status(0.04, 0.08, 0.5, 0.8)
        self.assertEqual(status, "critical")
        self.assertGreaterEqual(crit, 2)

    def test_warn_plus_critical_priority(self):
        """warn + critical → critical（critical 优先）"""
        # expr=0.04 (crit, >0.03), win=0.04 (warn, >0.03 but <0.06)
        status, crit, warn = classify_status(0.04, 0.04, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crit, 1)
        self.assertEqual(warn, 1)

    def test_all_none_ok(self):
        """全 None → ok"""
        status, crit, warn = classify_status(None, None, None, None)
        self.assertEqual(status, "ok")
        self.assertEqual(crit, 0)
        self.assertEqual(warn, 0)

    def test_sig_agree_low_warn(self):
        """sig_agree 略低 → warn"""
        # sig_agree = 0.93 → < 0.95 (warn), >= 0.90 (crit) → warn
        status, crit, warn = classify_status(0.0, 0.0, 0.0, 0.93)
        self.assertEqual(status, "warn")
        self.assertEqual(warn, 1)

    def test_sig_agree_very_low_critical(self):
        """sig_agree 很低 → critical"""
        status, crit, warn = classify_status(0.0, 0.0, 0.0, 0.80)
        self.assertEqual(status, "critical")
        self.assertEqual(crit, 1)

    def test_returns_triple(self):
        """返回 (status, criticals, warns) 三元组"""
        result = classify_status(0.0, 0.0, 0.0, 1.0)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], int)
        self.assertIsInstance(result[2], int)

    def test_negative_deltas_count_by_abs(self):
        """负 delta 用绝对值判断（方向不影响严重程度）"""
        status_pos, c1, w1 = classify_status(0.04, 0.0, 0.0, 1.0)
        status_neg, c2, w2 = classify_status(-0.04, 0.0, 0.0, 1.0)
        self.assertEqual(status_pos, status_neg)
        self.assertEqual(c1, c2)
        self.assertEqual(w1, w2)


# ═══════════════════════════════════════════════════════════════════════════
#  3. fmt_delta
# ═══════════════════════════════════════════════════════════════════════════

class TestFmtDelta(unittest.TestCase):
    """fmt_delta 带颜色格式化。"""

    def test_none_returns_na(self):
        """None → N/A"""
        result = fmt_delta(None, "{:.2f}", 0.1, 0.2)
        self.assertIn("N/A", result)

    def test_positive_has_plus_sign(self):
        """正值 → 带 + 号"""
        result = fmt_delta(0.05, "{:.2f}", 0.1, 0.2)
        self.assertIn("+", result)

    def test_negative_has_minus_sign(self):
        """负值 → 带 - 号"""
        result = fmt_delta(-0.05, "{:.2f}", 0.1, 0.2)
        self.assertIn("-", result)

    def test_critical_level_red(self):
        """超 crit → 红色"""
        result = fmt_delta(0.25, "{:.2f}", 0.1, 0.2)
        self.assertIn(C.RED, result)

    def test_warn_level_yellow(self):
        """超 warn → 黄色"""
        result = fmt_delta(0.15, "{:.2f}", 0.1, 0.2)
        self.assertIn(C.YELLOW, result)

    def test_normal_level_green(self):
        """正常 → 绿色"""
        result = fmt_delta(0.05, "{:.2f}", 0.1, 0.2)
        self.assertIn(C.GREEN, result)

    def test_is_pct_multiplies_by_100(self):
        """is_pct=True → ×100 显示"""
        result = fmt_delta(0.05, "{:.1f}%", 0.1, 0.2, is_pct=True)
        self.assertIn("5.0%", result)  # 0.05 × 100 = 5.0%

    def test_contains_ansi_reset(self):
        """包含 ANSI 重置码"""
        result = fmt_delta(0.05, "{:.2f}", 0.1, 0.2)
        self.assertIn(C.RESET, result)

    def test_zero_is_green(self):
        """零 → 绿色（正常）"""
        result = fmt_delta(0.0, "{:.2f}", 0.1, 0.2)
        self.assertIn(C.GREEN, result)
        self.assertNotIn(C.YELLOW, result)
        self.assertNotIn(C.RED, result)

    def test_at_warn_boundary_is_green(self):
        """刚好 = warn 阈值 → 绿色（> 才变色）"""
        result = fmt_delta(0.1, "{:.2f}", 0.1, 0.2)
        # abs_val > warn_thresh? 0.1 > 0.1? False → 绿色
        self.assertIn(C.GREEN, result)

    def test_at_crit_boundary_is_yellow(self):
        """刚好 = crit 阈值 → 黄色（> 才变红）"""
        result = fmt_delta(0.2, "{:.2f}", 0.1, 0.2)
        # 0.2 > 0.2? False → 黄色（因为 > warn 但 <= crit）
        self.assertIn(C.YELLOW, result)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  回归测试工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
