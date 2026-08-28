#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GA 质量过滤工具 — 单元测试
=================================

1. check_quality — GA 结果质量检查（6 道门槛）
   - 全通过 → passed=True, reasons=[]
   - expR ≤ 0 → 失败
   - expR = 0 → 失败（边界）
   - expR 微正 → 通过
   - robust < 0.5 → 失败
   - robust = 0.5 → 通过（边界：< 才失败）
   - T < 0.01 → 失败
   - |T| > 1.5 → 失败
   - |F| > 1.5 → 失败
   - |C| > 1.5 → 失败
   - 权重和 < 0.3 → 失败
   - 权重和 > 3.0 → 失败
   - 多项同时失败 → reasons 多条
   - 缺字段用默认值（expR=-999, robust=-999, T/F/C=0）
   - reasons 是字符串列表

2. _value_at — 单日基本面指标计算
   - profit: 线性加权和
   - profit_feed: legs加权和 - feed加权和
   - ratio: legs[0] / legs[1]（绝对值比）
   - spread: 线性加权和（同 profit 公式）
   - 缺数据 → None
   - 空序列 → None
   - ratio 分母为 0 → None
   - 未知 kind → None
   - fixed 偏移量（profit 类型）
   - 多 leg 加权

3. _to_ts — 时间字符串转时间戳
   - 正常格式 → 正确时间戳
   - 空串 → 0
   - 格式错误 → 0
   - None → 0
   - 非法字符串 → 0
   - 日期部分正确但时间错 → 0
"""

import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from blunder_check import _to_ts
from fundamental_metrics import _value_at
from ga_quality_filter import check_quality

# ═══════════════════════════════════════════════════════════════════════════
#  1. check_quality
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckQuality(unittest.TestCase):
    """check_quality GA 结果质量检查。"""

    def _make_data(self, **overrides):
        """构造一份合格的 data，再用 overrides 覆盖"""
        data = {
            "best_weights": {"base": {"T": 0.5, "F": 0.3, "C": 0.2}},
            "best_expR": 0.5,
            "robust_score": 0.8,
        }
        # 深度合并
        if "best_weights" in overrides:
            data["best_weights"]["base"].update(overrides["best_weights"].get("base", {}))
            overrides.pop("best_weights")
        data.update(overrides)
        return data

    def test_all_pass(self):
        """全通过 → passed=True, reasons=[]"""
        d = self._make_data()
        passed, reasons = check_quality("rb", d)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_expR_zero_fails(self):
        """expR ≤ 0 → 失败（边界：=0 也失败）"""
        d = self._make_data(best_expR=0.0)
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("expR" in r for r in reasons))

    def test_expR_negative_fails(self):
        """expR 负 → 失败"""
        d = self._make_data(best_expR=-0.1)
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("expR" in r for r in reasons))

    def test_expR_slightly_positive_passes(self):
        """expR 微正 → 通过"""
        d = self._make_data(best_expR=0.001)
        passed, reasons = check_quality("rb", d)
        # expR > 0 → 通过
        expR_reasons = [r for r in reasons if "expR" in r]
        self.assertEqual(len(expR_reasons), 0)

    def test_robust_below_min_fails(self):
        """robust < 0.5 → 失败"""
        d = self._make_data(robust_score=0.4)
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("robust" in r for r in reasons))

    def test_robust_at_min_passes(self):
        """robust = 0.5 → 通过（边界：< 才失败）"""
        d = self._make_data(robust_score=0.5)
        passed, reasons = check_quality("rb", d)
        robust_reasons = [r for r in reasons if "robust" in r]
        self.assertEqual(len(robust_reasons), 0)

    def test_T_below_min_fails(self):
        """T < 0.01 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 0.001, "F": 0.3, "C": 0.2}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("T=" in r and "<" in r for r in reasons))

    def test_T_above_max_fails(self):
        """|T| > 1.5 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 2.0, "F": 0.3, "C": 0.2}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("|T|" in r for r in reasons))

    def test_F_above_max_fails(self):
        """|F| > 1.5 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 0.5, "F": 2.0, "C": 0.2}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("|F|" in r for r in reasons))

    def test_C_above_max_fails(self):
        """|C| > 1.5 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 0.5, "F": 0.3, "C": 2.0}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("|C|" in r for r in reasons))

    def test_total_weight_below_min_fails(self):
        """权重和 < 0.3 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 0.1, "F": 0.05, "C": 0.05}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("权重和" in r and "<" in r for r in reasons))

    def test_total_weight_above_max_fails(self):
        """权重和 > 3.0 → 失败"""
        d = self._make_data(best_weights={"base": {"T": 1.2, "F": 1.0, "C": 1.0}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("权重和" in r and ">" in r for r in reasons))

    def test_multiple_failures_multiple_reasons(self):
        """多项同时失败 → reasons 多条"""
        d = self._make_data(
            best_expR=-0.5,
            robust_score=0.3,
            best_weights={"base": {"T": 2.0, "F": 2.0, "C": 2.0}},
        )
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        # expR + robust + |T| + |F| + |C| + 权重和 = 至少 6 条
        self.assertGreaterEqual(len(reasons), 6)

    def test_missing_fields_defaults(self):
        """缺字段用默认值（全不合格）"""
        d = {}
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        # expR 默认 -999 → 失败
        # robust 默认 -999 → 失败
        # T/F/C 默认 0 → T<0.01 失败，权重和=0<0.3 失败
        self.assertGreater(len(reasons), 0)

    def test_returns_tuple_bool_list(self):
        """返回 (bool, list)"""
        d = self._make_data()
        result = check_quality("rb", d)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], list)

    def test_negative_T_abs_checked(self):
        """T 为负时，绝对值也检查上限"""
        # T = -2.0 → |T| = 2.0 > 1.5 → 失败
        # 同时 T < 0.01 → 也失败
        d = self._make_data(best_weights={"base": {"T": -2.0, "F": 0.3, "C": 0.2}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("|T|" in r for r in reasons))

    def test_negative_F_abs_checked(self):
        """F 为负时，绝对值检查上限"""
        d = self._make_data(best_weights={"base": {"T": 0.5, "F": -2.0, "C": 0.2}})
        passed, reasons = check_quality("rb", d)
        self.assertFalse(passed)
        self.assertTrue(any("|F|" in r for r in reasons))


# ═══════════════════════════════════════════════════════════════════════════
#  2. _value_at
# ═══════════════════════════════════════════════════════════════════════════


class TestValueAt(unittest.TestCase):
    """_value_at 单日基本面指标计算。"""

    def test_profit_linear_sum(self):
        """profit: 线性加权和"""
        hist = {"A": [100.0], "B": [50.0]}
        legs = [("A", 1.0), ("B", -2.0)]
        result = _value_at(hist, "profit", legs, None, None)
        # 100 × 1 + 50 × (-2) = 100 - 100 = 0
        self.assertAlmostEqual(result, 0.0, places=6)

    def test_profit_with_fixed(self):
        """profit + fixed 偏移量"""
        hist = {"A": [100.0]}
        legs = [("A", 1.0)]
        result = _value_at(hist, "profit", legs, 50.0, None)
        self.assertAlmostEqual(result, 150.0, places=6)

    def test_profit_feed_subtracts(self):
        """profit_feed: legs加权和 - feed加权和"""
        hist = {"A": [100.0], "B": [50.0]}
        legs = [("A", 1.0)]
        feed = [("B", 2.0)]
        result = _value_at(hist, "profit_feed", legs, None, feed)
        # 100 × 1 - 50 × 2 = 100 - 100 = 0
        self.assertAlmostEqual(result, 0.0, places=6)

    def test_ratio_division(self):
        """ratio: legs[0] / legs[1]"""
        hist = {"A": [100.0], "B": [50.0]}
        legs = [("A", 1.0), ("B", 1.0)]
        result = _value_at(hist, "ratio", legs, None, None)
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_ratio_zero_denominator_none(self):
        """ratio 分母为 0 → None"""
        hist = {"A": [100.0], "B": [0.0]}
        legs = [("A", 1.0), ("B", 1.0)]
        result = _value_at(hist, "ratio", legs, None, None)
        self.assertIsNone(result)

    def test_spread_linear_sum(self):
        """spread: 线性加权和（同 profit）"""
        hist = {"A": [100.0], "B": [80.0]}
        legs = [("A", 1.0), ("B", -1.0)]
        result = _value_at(hist, "spread", legs, None, None)
        # 100 - 80 = 20
        self.assertAlmostEqual(result, 20.0, places=6)

    def test_missing_data_returns_none(self):
        """缺数据 → None"""
        hist = {"A": [100.0]}
        legs = [("A", 1.0), ("B", 1.0)]  # B 不在 hist 里
        result = _value_at(hist, "profit", legs, None, None)
        self.assertIsNone(result)

    def test_empty_sequence_returns_none(self):
        """空序列 → None"""
        hist = {"A": []}
        legs = [("A", 1.0)]
        result = _value_at(hist, "profit", legs, None, None)
        self.assertIsNone(result)

    def test_unknown_kind_returns_none(self):
        """未知 kind → None"""
        hist = {"A": [100.0]}
        legs = [("A", 1.0)]
        result = _value_at(hist, "unknown_kind", legs, None, None)
        self.assertIsNone(result)

    def test_uses_last_value(self):
        """用序列最后一个值"""
        hist = {"A": [50.0, 80.0, 100.0]}  # 最后 = 100
        legs = [("A", 1.0)]
        result = _value_at(hist, "profit", legs, None, None)
        self.assertAlmostEqual(result, 100.0, places=6)

    def test_multi_leg_weighted(self):
        """多 leg 加权"""
        hist = {"A": [10.0], "B": [20.0], "C": [30.0]}
        legs = [("A", 2.0), ("B", -1.0), ("C", 0.5)]
        result = _value_at(hist, "profit", legs, None, None)
        # 20 - 20 + 15 = 15
        self.assertAlmostEqual(result, 15.0, places=6)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _to_ts
# ═══════════════════════════════════════════════════════════════════════════


class TestToTs(unittest.TestCase):
    """_to_ts 时间字符串转时间戳。"""

    def test_normal_format(self):
        """正常格式 → 正确时间戳"""
        s = "2026-01-15 10:30:00"
        result = _to_ts(s)
        expected = datetime(2026, 1, 15, 10, 30, 0).timestamp()
        self.assertAlmostEqual(result, expected, places=3)

    def test_empty_string_zero(self):
        """空串 → 0"""
        self.assertEqual(_to_ts(""), 0.0)

    def test_invalid_format_zero(self):
        """格式错误 → 0"""
        self.assertEqual(_to_ts("not a date"), 0.0)
        self.assertEqual(_to_ts("2026/01/15"), 0.0)  # 斜杠
        self.assertEqual(_to_ts("01-15-2026"), 0.0)  # 月日前

    def test_none_zero(self):
        """None → 0"""
        self.assertEqual(_to_ts(None), 0.0)

    def test_garbage_string_zero(self):
        """非法字符串 → 0"""
        self.assertEqual(_to_ts("abc123"), 0.0)
        self.assertEqual(_to_ts("2026-13-01 00:00:00"), 0.0)  # 13 月

    def test_partial_date_invalid(self):
        """只有日期没有时间 → 0"""
        self.assertEqual(_to_ts("2026-01-15"), 0.0)

    def test_midnight(self):
        """午夜 00:00:00 → 正确"""
        s = "2026-01-01 00:00:00"
        result = _to_ts(s)
        expected = datetime(2026, 1, 1, 0, 0, 0).timestamp()
        self.assertAlmostEqual(result, expected, places=3)

    def test_end_of_day(self):
        """23:59:59 → 正确"""
        s = "2026-12-31 23:59:59"
        result = _to_ts(s)
        expected = datetime(2026, 12, 31, 23, 59, 59).timestamp()
        self.assertAlmostEqual(result, expected, places=3)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  GA 质量过滤工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
