#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向源一致性监控 — 单元测试
===================================

1. divergence — 单笔方向一致性
   - 同号正 → True
   - 同号负 → True
   - 一正一负 → False
   - 一方为 0 → None
   - 双方为 0 → None
   - 小数值（接近 0 但非 0）→ 仍按符号判断
   - 大数值 → 只看符号

2. DivergenceTracker — 滚动分歧率追踪
   - 空样本 → rate=None, level=OK
   - 全一致 → rate=0.0, level=OK
   - 全分歧 → rate=1.0, level=HIGH
   - 部分分歧 → rate=分歧比例
   - 窗口滚动：超过 window 后最老样本被挤出
   - SA 样本独立追踪
   - _rate 静态方法：空列表→None，非空→分歧率
   - summary 字段齐全
   - level 分级：OK / WARN / HIGH
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from direction_source_monitor import divergence, DivergenceTracker


# ═══════════════════════════════════════════════════════════════════════════
#  1. divergence
# ═══════════════════════════════════════════════════════════════════════════

class TestDivergence(unittest.TestCase):
    """divergence 单笔方向一致性。"""

    def test_both_positive_agree(self):
        """同号正 → True"""
        self.assertTrue(divergence(50, 30))

    def test_both_negative_agree(self):
        """同号负 → True"""
        self.assertTrue(divergence(-50, -30))

    def test_pos_neg_disagree(self):
        """一正一负 → False"""
        self.assertFalse(divergence(50, -30))

    def test_neg_pos_disagree(self):
        """一负一正 → False"""
        self.assertFalse(divergence(-50, 30))

    def test_one_zero_returns_none(self):
        """一方为 0 → None（无方向，无法判断一致）"""
        self.assertIsNone(divergence(50, 0))
        self.assertIsNone(divergence(0, 30))

    def test_both_zero_returns_none(self):
        """双方为 0 → None"""
        self.assertIsNone(divergence(0, 0))

    def test_small_nonzero_counts(self):
        """小数值（接近 0 但非 0）→ 仍按符号判断"""
        self.assertTrue(divergence(0.1, 0.01))
        self.assertTrue(divergence(-0.1, -0.01))
        self.assertFalse(divergence(0.1, -0.01))

    def test_large_values_only_sign_matters(self):
        """大数值 → 只看符号"""
        self.assertTrue(divergence(10000, 1))
        self.assertFalse(divergence(10000, -1))

    def test_float_zero_exact(self):
        """精确 0.0 → 视为无方向"""
        self.assertIsNone(divergence(0.0, 50))
        self.assertIsNone(divergence(50, 0.0))


# ═══════════════════════════════════════════════════════════════════════════
#  2. DivergenceTracker
# ═══════════════════════════════════════════════════════════════════════════

class TestDivergenceTracker(unittest.TestCase):
    """DivergenceTracker 滚动分歧率追踪。"""

    def test_empty_rate_none(self):
        """空样本 → rate=None, level=OK"""
        t = DivergenceTracker()
        s = t.summary()
        self.assertIsNone(s["divergence_rate"])
        self.assertEqual(s["level"], "OK")
        self.assertEqual(s["n"], 0)

    def test_all_agree_rate_zero(self):
        """全一致 → rate=0.0, level=OK"""
        t = DivergenceTracker()
        for _ in range(10):
            t.update("rb", 50, 30)  # 同正
        s = t.summary()
        self.assertEqual(s["divergence_rate"], 0.0)
        self.assertEqual(s["level"], "OK")
        self.assertEqual(s["n"], 10)

    def test_all_disagree_rate_one(self):
        """全分歧 → rate=1.0, level=HIGH"""
        t = DivergenceTracker(window=10)
        for _ in range(10):
            t.update("rb", 50, -30)  # 一正一负
        s = t.summary()
        self.assertEqual(s["divergence_rate"], 1.0)
        self.assertEqual(s["level"], "HIGH")

    def test_partial_divergence(self):
        """部分分歧 → rate=分歧比例"""
        t = DivergenceTracker(window=10)
        # 7 次一致，3 次分歧 → 分歧率 = 0.3
        for _ in range(7):
            t.update("rb", 50, 30)   # 一致
        for _ in range(3):
            t.update("rb", 50, -30)  # 分歧
        s = t.summary()
        self.assertAlmostEqual(s["divergence_rate"], 0.3, places=3)

    def test_window_rolling(self):
        """窗口滚动：超过 window 后最老样本被挤出"""
        t = DivergenceTracker(window=5)
        # 前 5 个：全分歧 → rate=1.0
        for _ in range(5):
            t.update("rb", 50, -30)
        self.assertEqual(t.summary()["divergence_rate"], 1.0)
        # 再加 5 个全一致 → 老的分歧被挤出，rate 应该降为 0
        for _ in range(5):
            t.update("rb", 50, 30)
        s = t.summary()
        self.assertEqual(s["divergence_rate"], 0.0)
        self.assertEqual(s["n"], 5)  # 窗口大小不变

    def test_sa_samples_tracked_separately(self):
        """SA 样本独立追踪"""
        t = DivergenceTracker(window=20)
        # 非 SA 品种：全一致（2 个）
        t.update("rb", 50, 30)
        t.update("FG", 40, 20)
        # SA 品种：全分歧（2 个）
        t.update("SA", 50, -30)
        t.update("SA", 40, -20)
        s = t.summary()
        # 总体：4 个样本（2 一致 + 2 分歧）→ 分歧率 = 0.5
        self.assertEqual(s["divergence_rate"], 0.5)
        self.assertEqual(s["n"], 4)
        # SA 专项：2 个全分歧 → 1.0
        self.assertEqual(s["sa_divergence_rate"], 1.0)

    def test_rate_static_method(self):
        """_rate 静态方法"""
        # 空列表 → None
        self.assertIsNone(DivergenceTracker._rate([]))
        # 全 True（全一致）→ 分歧率 = 0
        self.assertEqual(DivergenceTracker._rate([True, True, True]), 0.0)
        # 全 False（全分歧）→ 分歧率 = 1
        self.assertEqual(DivergenceTracker._rate([False, False]), 1.0)
        # 部分分歧
        self.assertAlmostEqual(DivergenceTracker._rate([True, True, False]), 1/3, places=10)

    def test_summary_fields_complete(self):
        """summary 字段齐全"""
        t = DivergenceTracker()
        t.update("rb", 50, 30)
        s = t.summary()
        self.assertIn("divergence_rate", s)
        self.assertIn("baseline", s)
        self.assertIn("level", s)
        self.assertIn("sa_divergence_rate", s)
        self.assertIn("sa_sensitive", s)
        self.assertIn("n", s)

    def test_level_ok_when_low_divergence(self):
        """低分歧 → OK"""
        t = DivergenceTracker(window=10)
        # 9 一致，1 分歧 → 0.1，应该是 OK
        for _ in range(9):
            t.update("rb", 50, 30)
        t.update("rb", 50, -30)
        s = t.summary()
        self.assertEqual(s["level"], "OK")

    def test_zero_size_directionless_samples_ignored(self):
        """无方向的样本（divergence 返回 None）不计数"""
        t = DivergenceTracker(window=10)
        t.update("rb", 0, 50)   # T_D=0 → None，不计入
        t.update("rb", 50, 0)   # T_5m=0 → None，不计入
        s = t.summary()
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["divergence_rate"])

    def test_update_returns_summary(self):
        """update 返回 summary dict"""
        t = DivergenceTracker()
        result = t.update("rb", 50, 30)
        self.assertIsInstance(result, dict)
        self.assertIn("level", result)
        self.assertIn("divergence_rate", result)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  方向源一致性监控 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
