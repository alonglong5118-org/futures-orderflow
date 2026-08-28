#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SR分类 + 信号质量 + 基本面指标计算 — 单元测试
========================================================

1. _classify — 支撑/压力位分类
   - 价格在上方 → resistance
   - 价格在下方 → support
   - 价格恰好等于 → support（因为不是 >）
   - distance_pct 公式 = |price - current| / current × 100
   - distance_pct 保留 2 位小数
   - 多个位都正确分类
   - 返回修改后的 levels 列表

2. signal_quality_boost — 信号质量调整
   - 空 sr_result → boost=0, reason=""
   - 无 levels → boost=0, reason=""
   - 做多：极近压力位（<0.3%）→ boost=0
   - 做多：危险区（0.3%~1.0%）→ boost=-0.3
   - 做多：安全区（>=1.0%）→ boost=0
   - 做空：极近支撑位（<0.3%）→ boost=0
   - 做空：危险区（0.3%~1.0%）→ boost=-0.3
   - 做空：安全区（>=1.0%）→ boost=0
   - 做多无压力位 → 安全区
   - 做空无支撑位 → 安全区
   - reason 包含中文描述
   - 返回 (boost, reason) 二元组

3. _value_at — 基本面指标单日计算
   - profit: 线性组合 Σ coef × price
   - profit_feed: 线性组合 - feed 项
   - ratio: legs[0] / legs[1]（绝对值比）
   - spread: 同 profit（无 fixed）
   - 缺数据 → None
   - 分母为 0 → None
   - 空序列 → None
   - 未知 kind → None
   - fixed 叠加（profit 模式）
   - 多 leg 正确累加
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fundamental_metrics import _value_at
from sr_analyzer import _classify, signal_quality_boost

# ═══════════════════════════════════════════════════════════════════════════
#  1. _classify
# ═══════════════════════════════════════════════════════════════════════════


class TestSrClassify(unittest.TestCase):
    """_classify 支撑/压力位分类。"""

    def test_above_price_is_resistance(self):
        """价格在上方 → resistance"""
        levels = [{"price": 110}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "resistance")

    def test_below_price_is_support(self):
        """价格在下方 → support"""
        levels = [{"price": 90}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "support")

    def test_equal_price_is_support(self):
        """价格恰好等于 → support（因为不是 >）"""
        levels = [{"price": 100}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "support")

    def test_distance_pct_formula(self):
        """distance_pct 公式 = |price - current| / current × 100"""
        # 110 vs 100 → |110-100|/100*100 = 10%
        levels = [{"price": 110}]
        result = _classify(levels, 100)
        self.assertAlmostEqual(result[0]["distance_pct"], 10.0, places=2)

    def test_distance_pct_below(self):
        """下方距离也用绝对值"""
        # 90 vs 100 → 10%
        levels = [{"price": 90}]
        result = _classify(levels, 100)
        self.assertAlmostEqual(result[0]["distance_pct"], 10.0, places=2)

    def test_two_decimals(self):
        """distance_pct 保留 2 位小数"""
        levels = [{"price": 100.123}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["distance_pct"], round(result[0]["distance_pct"], 2))

    def test_multiple_levels(self):
        """多个位都正确分类"""
        levels = [
            {"price": 90},  # support
            {"price": 95},  # support
            {"price": 105},  # resistance
            {"price": 110},  # resistance
        ]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "support")
        self.assertEqual(result[1]["role"], "support")
        self.assertEqual(result[2]["role"], "resistance")
        self.assertEqual(result[3]["role"], "resistance")

    def test_returns_same_list(self):
        """返回修改后的 levels 列表（原地修改）"""
        levels = [{"price": 90}, {"price": 110}]
        result = _classify(levels, 100)
        self.assertIs(result, levels)

    def test_zero_distance(self):
        """价格完全一致 → distance_pct = 0"""
        levels = [{"price": 100.0}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["distance_pct"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. signal_quality_boost
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalQualityBoost(unittest.TestCase):
    """signal_quality_boost 信号质量调整。"""

    def _sr(self, support_dist=None, resist_dist=None):
        """构造 sr_result 字典。"""
        ns = {"distance_pct": support_dist, "price": 1000} if support_dist is not None else None
        nr = {"distance_pct": resist_dist, "price": 1100} if resist_dist is not None else None
        return {
            "levels": [{"price": 1000, "role": "support"}, {"price": 1100, "role": "resistance"}],
            "nearest_support": ns,
            "nearest_resistance": nr,
        }

    def test_none_sr_zero_boost(self):
        """空 sr_result → boost=0, reason=''"""
        boost, reason = signal_quality_boost(None, 1)
        self.assertEqual(boost, 0.0)
        self.assertEqual(reason, "")

    def test_no_levels_zero_boost(self):
        """无 levels → boost=0, reason=''"""
        boost, reason = signal_quality_boost({"levels": []}, 1)
        self.assertEqual(boost, 0.0)
        self.assertEqual(reason, "")

    def test_long_tight_resistance(self):
        """做多：极近压力位（<0.3%）→ boost=0"""
        sr = self._sr(support_dist=5.0, resist_dist=0.2)
        boost, reason = signal_quality_boost(sr, 1)
        self.assertEqual(boost, 0.0)
        self.assertIn("压力", reason)

    def test_long_danger_resistance(self):
        """做多：危险区（0.3%~1.0%）→ boost=-0.3"""
        sr = self._sr(support_dist=5.0, resist_dist=0.5)
        boost, reason = signal_quality_boost(sr, 1)
        self.assertEqual(boost, -0.3)
        self.assertIn("危险区", reason)

    def test_long_safe_resistance(self):
        """做多：安全区（>=1.0%）→ boost=0"""
        sr = self._sr(support_dist=5.0, resist_dist=2.0)
        boost, reason = signal_quality_boost(sr, 1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_short_tight_support(self):
        """做空：极近支撑位（<0.3%）→ boost=0"""
        sr = self._sr(support_dist=0.2, resist_dist=5.0)
        boost, reason = signal_quality_boost(sr, -1)
        self.assertEqual(boost, 0.0)
        self.assertIn("支撑", reason)

    def test_short_danger_support(self):
        """做空：危险区（0.3%~1.0%）→ boost=-0.3"""
        sr = self._sr(support_dist=0.5, resist_dist=5.0)
        boost, reason = signal_quality_boost(sr, -1)
        self.assertEqual(boost, -0.3)
        self.assertIn("危险区", reason)

    def test_short_safe_support(self):
        """做空：安全区（>=1.0%）→ boost=0"""
        sr = self._sr(support_dist=2.0, resist_dist=5.0)
        boost, reason = signal_quality_boost(sr, -1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_long_no_resistance_safe(self):
        """做多无压力位 → 安全区"""
        sr = self._sr(support_dist=1.0, resist_dist=None)
        boost, reason = signal_quality_boost(sr, 1)
        self.assertEqual(boost, 0.0)

    def test_short_no_support_safe(self):
        """做空无支撑位 → 安全区"""
        sr = self._sr(support_dist=None, resist_dist=1.0)
        boost, reason = signal_quality_boost(sr, -1)
        self.assertEqual(boost, 0.0)

    def test_returns_tuple(self):
        """返回 (boost, reason) 二元组"""
        sr = self._sr(support_dist=2.0, resist_dist=2.0)
        result = signal_quality_boost(sr, 1)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], str)

    def test_boundary_tight_at_03(self):
        """边界：0.3% 恰好等于 tight 上限 → 进入危险区"""
        # hostile_frac < 0.003 → tight；0.3% = 0.003 不满足 <，进入下一区间
        sr = self._sr(support_dist=5.0, resist_dist=0.3)
        boost, _ = signal_quality_boost(sr, 1)
        # 0.3% = 0.003 不在 tight 区（<0.003 才是），进入 danger 区
        self.assertEqual(boost, -0.3)

    def test_boundary_danger_at_1pct(self):
        """边界：1.0% 恰好等于 danger 上限 → 进入安全区"""
        # hostile_frac < 0.01 → danger；1.0% = 0.01 不满足 <，进入安全区
        sr = self._sr(support_dist=5.0, resist_dist=1.0)
        boost, _ = signal_quality_boost(sr, 1)
        # 1.0% = 0.01 不在 danger 区（<0.01 才是），进入安全区
        self.assertEqual(boost, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _value_at
# ═══════════════════════════════════════════════════════════════════════════


class TestValueAt(unittest.TestCase):
    """_value_at 基本面指标单日计算。"""

    def test_profit_linear_combination(self):
        """profit: 线性组合 Σ coef × price"""
        # 2×100 + 3×200 = 200 + 600 = 800
        hist = {"A": [100], "B": [200]}
        legs = [("A", 2), ("B", 3)]
        result = _value_at(hist, "profit", legs, None, None)
        self.assertEqual(result, 800.0)

    def test_profit_with_fixed(self):
        """profit + fixed: 线性组合 + fixed"""
        hist = {"A": [100]}
        legs = [("A", 2)]
        result = _value_at(hist, "profit", legs, 50, None)
        self.assertEqual(result, 250.0)

    def test_profit_feed_subtracts_feed(self):
        """profit_feed: 线性组合 - feed 项"""
        # 2×100 - 1×50 = 200 - 50 = 150
        hist = {"A": [100], "F": [50]}
        legs = [("A", 2)]
        feed = [("F", 1)]
        result = _value_at(hist, "profit_feed", legs, None, feed)
        self.assertEqual(result, 150.0)

    def test_ratio_division(self):
        """ratio: legs[0] / legs[1]"""
        hist = {"A": [120], "B": [100]}
        legs = [("A", 1), ("B", 1)]
        result = _value_at(hist, "ratio", legs, None, None)
        self.assertEqual(result, 1.2)

    def test_spread_linear(self):
        """spread: 同 profit（无 fixed）"""
        # 1×100 + (-1)×80 = 20
        hist = {"A": [100], "B": [80]}
        legs = [("A", 1), ("B", -1)]
        result = _value_at(hist, "spread", legs, None, None)
        self.assertEqual(result, 20.0)

    def test_missing_data_returns_none(self):
        """缺数据 → None"""
        hist = {"A": [100]}
        legs = [("A", 1), ("B", 1)]  # B 缺失
        result = _value_at(hist, "profit", legs, None, None)
        self.assertIsNone(result)

    def test_zero_denominator_returns_none(self):
        """ratio 分母为 0 → None"""
        hist = {"A": [100], "B": [0]}
        legs = [("A", 1), ("B", 1)]
        result = _value_at(hist, "ratio", legs, None, None)
        self.assertIsNone(result)

    def test_empty_sequence_returns_none(self):
        """空序列 → None"""
        hist = {"A": []}
        legs = [("A", 1)]
        result = _value_at(hist, "profit", legs, None, None)
        self.assertIsNone(result)

    def test_unknown_kind_returns_none(self):
        """未知 kind → None"""
        hist = {"A": [100]}
        legs = [("A", 1)]
        result = _value_at(hist, "unknown_kind", legs, None, None)
        self.assertIsNone(result)

    def test_uses_last_value(self):
        """取序列最后一个值（h[-1]）"""
        hist = {"A": [50, 60, 70, 80, 90, 100]}
        legs = [("A", 1)]
        result = _value_at(hist, "profit", legs, None, None)
        self.assertEqual(result, 100.0)

    def test_negative_coefficient(self):
        """负系数正确处理"""
        hist = {"A": [100], "B": [50]}
        legs = [("A", 1), ("B", -2)]
        result = _value_at(hist, "profit", legs, None, None)
        # 100 - 100 = 0
        self.assertEqual(result, 0.0)

    def test_profit_feed_missing_feed(self):
        """profit_feed 缺 feed 数据 → None"""
        hist = {"A": [100]}
        legs = [("A", 2)]
        feed = [("F", 1)]  # F 缺失
        result = _value_at(hist, "profit_feed", legs, None, feed)
        self.assertIsNone(result)

    def test_ratio_missing_leg(self):
        """ratio 缺其中一个 → None"""
        hist = {"A": [100]}
        legs = [("A", 1), ("B", 1)]  # B 缺失
        result = _value_at(hist, "ratio", legs, None, None)
        self.assertIsNone(result)

    def test_spread_missing_leg(self):
        """spread 缺数据 → None"""
        hist = {"A": [100]}
        legs = [("A", 1), ("B", -1)]  # B 缺失
        result = _value_at(hist, "spread", legs, None, None)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR分类 + 信号质量 + 基本面指标计算 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
