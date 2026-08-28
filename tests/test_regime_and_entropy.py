#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场状态分类 + 权重熵 — 单元测试
=======================================

1. classify_regime — 市场状态分类
   - 数据不足 → "未知"
   - 高波动 → "波动"
   - 低波动+低偏离 → "震荡"
   - 大斜率+大偏离 → "趋势"（上涨）
   - 大斜率+大偏离 → "趋势"（下跌，斜率负，绝对值判断）
   - 中间状态 → "过渡"
   - 返回 (regime, 描述)
   - 自定义 params 生效
   - 4 种状态全覆盖

2. _weight_entropy — 权重归一化熵
   - 均匀分布 → 1.0
   - 单点分布 → ≈ 0
   - 二项 50/50 → 1.0
   - 二项 80/20 → < 1
   - 三项均匀 → 1.0
   - 返回 0-1 之间
   - 极小值用 1e-6 兜底（无零概率）
"""

import sys
import os
import unittest
import math
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import classify_regime
from ga_group_six_factor_robust import _weight_entropy


def _make_df(prices):
    """构造 OHLC DataFrame，high=price*1.01, low=price*0.99"""
    n = len(prices)
    return pd.DataFrame({
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
    })


# ═══════════════════════════════════════════════════════════════════════════
#  1. classify_regime
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyRegime(unittest.TestCase):
    """classify_regime 市场状态分类。"""

    def test_insufficient_data_unknown(self):
        """数据不足 → "未知" + "数据不足" """
        df = _make_df([100, 101, 102])
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "未知")
        self.assertIn("数据不足", desc)

    def test_high_volatility_regime(self):
        """高波动 → "波动" """
        # 构造大波动：价格大幅跳动
        prices = [100.0] * 25
        # 用 high/low 制造大 ATR
        df = pd.DataFrame({
            "close": prices,
            "high": [p * 1.08 for p in prices],  # 8% 的振幅
            "low": [p * 0.92 for p in prices],
        })
        regime, desc = classify_regime(df)
        # ATR 占比 > 默认阈值 → "波动"
        self.assertEqual(regime, "波动")
        self.assertIn("ATR", desc)

    def test_low_vol_flat_range(self):
        """低波动+低偏离 → "震荡" """
        # 横盘，波动小
        prices = [100.0 + 0.1 * math.sin(i * 0.2) for i in range(30)]
        df = _make_df(prices)
        # 用宽松的阈值来确保进入震荡
        params = {
            "atr_thresh": 0.05,    # 5% 才算高波动
            "flat_dev": 0.02,      # 偏离 < 2%
            "flat_atr": 0.03,      # ATR < 3%（1% 价差的 ATR 约 2%）
            "trend_slope": 0.03,   # 斜率 > 3%
            "trend_dev": 0.02,     # 偏离 > 2%
        }
        regime, desc = classify_regime(df, params)
        # 横盘 + 低波动 → "震荡"
        self.assertEqual(regime, "震荡")
        self.assertIn("收敛", desc)

    def test_uptrend_regime(self):
        """上涨趋势 → "趋势" """
        # 持续上涨，斜率大，偏离大
        prices = [100 + i * 0.8 for i in range(30)]  # 30 根涨 23.2 点，斜率大
        df = _make_df(prices)
        params = {
            "atr_thresh": 0.10,
            "flat_dev": 0.01,
            "flat_atr": 0.005,
            "trend_slope": 0.01,  # 1% 就判趋势
            "trend_dev": 0.01,
        }
        regime, desc = classify_regime(df, params)
        self.assertEqual(regime, "趋势")
        self.assertIn("斜率", desc)

    def test_downtrend_regime(self):
        """下跌趋势 → "趋势"（斜率为负，用绝对值判断）"""
        prices = [120 - i * 0.8 for i in range(30)]
        df = _make_df(prices)
        params = {
            "atr_thresh": 0.10,
            "flat_dev": 0.01,
            "flat_atr": 0.005,
            "trend_slope": 0.01,
            "trend_dev": 0.01,
        }
        regime, desc = classify_regime(df, params)
        self.assertEqual(regime, "趋势")
        self.assertIn("斜率", desc)

    def test_transition_regime(self):
        """中间状态 → "过渡" """
        # 小幅波动，斜率不大，偏离中等
        prices = [100 + i * 0.15 for i in range(30)]  # 缓慢上涨
        df = _make_df(prices)
        params = {
            "atr_thresh": 0.05,
            "flat_dev": 0.005,   # 偏离 > 0.5% 就不算震荡
            "flat_atr": 0.005,
            "trend_slope": 0.02,  # 斜率 > 2% 才算趋势
            "trend_dev": 0.02,
        }
        regime, desc = classify_regime(df, params)
        # 斜率和偏离都在中间 → "过渡"
        self.assertEqual(regime, "过渡")
        self.assertIn("斜率", desc)

    def test_returns_tuple(self):
        """返回 (str, str)"""
        df = _make_df([100 + i for i in range(30)])
        result = classify_regime(df)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], str)

    def test_priority_volatility_first(self):
        """优先级：高波动优先于其他状态"""
        # 同时满足趋势和高波动 → 应该判"波动"（先判断）
        prices = [100 + i * 2.0 for i in range(30)]  # 大涨，同时 ATR 也大
        df = pd.DataFrame({
            "close": prices,
            "high": [p * 1.10 for p in prices],  # 10% 振幅 → 高波动
            "low": [p * 0.90 for p in prices],
        })
        params = {
            "atr_thresh": 0.05,  # 5% 阈值
            "flat_dev": 0.02,
            "flat_atr": 0.01,
            "trend_slope": 0.01,
            "trend_dev": 0.01,
        }
        regime, desc = classify_regime(df, params)
        self.assertEqual(regime, "波动")  # 高波动优先

    def test_flat_second_priority(self):
        """优先级：震荡在波动之后、趋势之前"""
        # 低波动 + 低偏离 → 震荡（在趋势判断之前）
        prices = [100.0 + 0.05 * math.sin(i * 0.3) for i in range(30)]
        df = _make_df(prices)
        params = {
            "atr_thresh": 0.05,
            "flat_dev": 0.01,     # 偏离 < 1% → 震荡
            "flat_atr": 0.03,     # ATR < 3%
            "trend_slope": 0.001, # 斜率门槛极低
            "trend_dev": 0.001,
        }
        regime, desc = classify_regime(df, params)
        # 虽然斜率和偏离也满足趋势，但震荡判断在前
        self.assertEqual(regime, "震荡")


# ═══════════════════════════════════════════════════════════════════════════
#  2. _weight_entropy
# ═══════════════════════════════════════════════════════════════════════════

class TestWeightEntropy(unittest.TestCase):
    """_weight_entropy 权重归一化熵。"""

    def test_uniform_distribution_one(self):
        """均匀分布 → 1.0"""
        w = {"a": 1.0, "b": 1.0, "c": 1.0}
        entropy = _weight_entropy(w)
        self.assertAlmostEqual(entropy, 1.0, places=6)

    def test_single_point_near_zero(self):
        """单点分布 → ≈ 0"""
        w = {"a": 1.0, "b": 1e-6, "c": 1e-6}
        entropy = _weight_entropy(w)
        # 几乎全部权重在 a 上 → 熵很低
        self.assertLess(entropy, 0.1)
        self.assertGreater(entropy, 0)  # 因为有 1e-6 兜底，严格 > 0

    def test_balanced_binary_one(self):
        """二项 50/50 → 1.0"""
        w = {"a": 1.0, "b": 1.0}
        entropy = _weight_entropy(w)
        self.assertAlmostEqual(entropy, 1.0, places=6)

    def test_skewed_binary_less_than_one(self):
        """二项 80/20 → < 1"""
        w = {"a": 8.0, "b": 2.0}
        entropy = _weight_entropy(w)
        self.assertLess(entropy, 1.0)
        self.assertGreater(entropy, 0)

    def test_three_uniform_one(self):
        """三项均匀 → 1.0"""
        w = {"a": 2.0, "b": 2.0, "c": 2.0}
        entropy = _weight_entropy(w)
        self.assertAlmostEqual(entropy, 1.0, places=6)

    def test_range_zero_to_one(self):
        """返回值在 0-1 之间（≥2 个因子）"""
        distributions = [
            {"a": 1.0, "b": 9.0},
            {"a": 3.0, "b": 3.0, "c": 3.0},
            {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
            {"a": 5.0, "b": 5.0},
        ]
        for w in distributions:
            entropy = _weight_entropy(w)
            self.assertGreaterEqual(entropy, 0.0, f"entropy={entropy} < 0 for {w}")
            self.assertLessEqual(entropy, 1.0, f"entropy={entropy} > 1 for {w}")

    def test_single_factor_nan(self):
        """单因子 → 0/0 = NaN（max_entropy = ln(1) = 0）"""
        w = {"a": 1.0}
        entropy = _weight_entropy(w)
        self.assertTrue(math.isnan(entropy))

    def test_known_value_80_20(self):
        """80/20 分布的熵值验证"""
        # H = - (0.8*ln(0.8) + 0.2*ln(0.2)) = - (-0.1785 - 0.3219) = 0.5004
        # H_max = ln(2) = 0.6931
        # 归一化熵 = 0.5004 / 0.6931 ≈ 0.722
        w = {"a": 8.0, "b": 2.0}
        entropy = _weight_entropy(w)
        self.assertAlmostEqual(entropy, 0.722, places=3)

    def test_very_skewed_low_entropy(self):
        """极端倾斜 → 低熵"""
        w = {"a": 99.0, "b": 1.0}
        entropy = _weight_entropy(w)
        self.assertLess(entropy, 0.5)  # 应该比较低

    def test_many_factors_uniform(self):
        """多因子均匀分布 → 1.0"""
        w = {f"f{i}": 1.0 for i in range(10)}
        entropy = _weight_entropy(w)
        self.assertAlmostEqual(entropy, 1.0, places=6)

    def test_does_not_mutate_input(self):
        """不修改输入 dict"""
        w = {"a": 1.0, "b": 2.0}
        original = dict(w)
        _weight_entropy(w)
        self.assertEqual(w, original)

    def test_returns_float(self):
        """返回 float"""
        w = {"a": 1.0, "b": 1.0}
        result = _weight_entropy(w)
        self.assertIsInstance(result, float)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  市场状态分类 + 权重熵 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
