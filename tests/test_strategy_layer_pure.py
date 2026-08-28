#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略层纯函数 — 单元测试
======================================

1. crossover — 金叉/死叉判断
   - 金叉：a从下往上穿b → 1
   - 死叉：a从上往下穿b → -1
   - 数据不足 → 0
   - 平行（都相等）→ 0
   - 同向（都上升但不交叉）→ 0
   - a在b上方且继续上行 → 0
   - a在b下方且继续下行 → 0
   - 返回 int

2. classify_regime — 市场状态分类
   - 数据不足(<25根) → "未知"
   - ATR占比高 → "波动"
   - 偏离小+ATR低 → "震荡"
   - 斜率大+偏离大 → "趋势"
   - 都不满足 → "过渡"
   - 返回 (regime, description) 二元组
   - 5种状态全覆盖
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import classify_regime, crossover

# ═══════════════════════════════════════════════════════════════════════════
#  1. crossover
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossover(unittest.TestCase):
    """crossover 金叉/死叉判断。"""

    def test_golden_cross(self):
        """金叉：a从下往上穿b → 1"""
        # 前一期 a <= b，后一期 a > b
        a = pd.Series([9, 11])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 1)

    def test_death_cross(self):
        """死叉：a从上往下穿b → -1"""
        # 前一期 a >= b，后一期 a < b
        a = pd.Series([11, 9])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), -1)

    def test_insufficient_data(self):
        """数据不足（<2根） → 0"""
        a = pd.Series([10])
        b = pd.Series([10])
        self.assertEqual(crossover(a, b), 0)

    def test_empty_series(self):
        """空序列 → 0"""
        a = pd.Series([])
        b = pd.Series([])
        self.assertEqual(crossover(a, b), 0)

    def test_parallel_equal(self):
        """平行（都相等） → 0"""
        a = pd.Series([10, 10])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 0)

    def test_above_continues_up(self):
        """a在b上方且继续上行 → 0（不交叉）"""
        a = pd.Series([11, 12])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 0)

    def test_below_continues_down(self):
        """a在b下方且继续下行 → 0（不交叉）"""
        a = pd.Series([9, 8])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 0)

    def test_touch_from_below_no_cross(self):
        """从下方触及但不穿越（等于） → 0"""
        # a: 9 → 10, b: 10 → 10
        # a.iloc[-2] = 9 <= 10, a.iloc[-1] = 10 > 10? No → 不满足金叉
        # a.iloc[-2] = 9 >= 10? No → 不满足死叉
        a = pd.Series([9, 10])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 0)

    def test_touch_from_above_no_cross(self):
        """从上方触及但不穿越（等于） → 0"""
        a = pd.Series([11, 10])
        b = pd.Series([10, 10])
        self.assertEqual(crossover(a, b), 0)

    def test_returns_int(self):
        """返回 int"""
        a = pd.Series([9, 11])
        b = pd.Series([10, 10])
        self.assertIsInstance(crossover(a, b), int)

    def test_unequal_lengths(self):
        """长度不一致 → 取较短的判断"""
        # a 有3个，b 有2个 → 只用最后2个
        a = pd.Series([8, 9, 11])
        b = pd.Series([10, 10])
        # 最后2个：a=[9,11], b=[10,10] → 金叉
        self.assertEqual(crossover(a, b), 1)

    def test_long_series_golden(self):
        """长序列金叉（只用最后2个）"""
        a = pd.Series([5, 6, 7, 8, 9, 11])
        b = pd.Series([10, 10, 10, 10, 10, 10])
        self.assertEqual(crossover(a, b), 1)

    def test_cross_with_floats(self):
        """浮点数据交叉"""
        a = pd.Series([9.5, 10.5])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 1)

    def test_same_trend_no_cross(self):
        """同向上升，a始终在b上方 → 0"""
        a = pd.Series([11, 12])
        b = pd.Series([9, 10])
        self.assertEqual(crossover(a, b), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. classify_regime
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyRegime(unittest.TestCase):
    """classify_regime 市场状态分类。"""

    def _make_df(self, n=30, close_start=100, close_step=0, high_premium=2.0, low_premium=2.0, vol=1000000):
        """构造测试用 DataFrame。"""
        closes = [close_start + i * close_step for i in range(n)]
        df = pd.DataFrame(
            {
                "close": closes,
                "high": [c + high_premium for c in closes],
                "low": [c - low_premium for c in closes],
                "volume": [vol] * n,
            },
            index=pd.date_range("2026-01-01", periods=n, freq="D"),
        )
        return df

    def test_insufficient_data_unknown(self):
        """数据不足(<25根) → '未知'"""
        df = self._make_df(n=20)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "未知")
        self.assertIn("数据不足", desc)

    def test_high_atr_is_volatility(self):
        """ATR占比高 → '波动'"""
        # 高ATR（高低价差大）
        df = self._make_df(n=30, high_premium=5.0, low_premium=5.0)
        regime, desc = classify_regime(df)
        # 高ATR = 波动市
        self.assertEqual(regime, "波动")
        self.assertIn("ATR", desc)

    def test_flat_low_atr_is_range(self):
        """偏离小+ATR低 → '震荡'"""
        # 价格横盘，波动极小（high-low=0.5 → ATR≈0.5/100=0.5% < 1.2%）
        # 价格不变 → dev≈0 < 0.8%
        df = self._make_df(n=30, close_step=0.0, high_premium=0.3, low_premium=0.3)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "震荡")
        self.assertIn("收敛", desc)

    def test_strong_trend_is_trend(self):
        """斜率大+偏离大 → '趋势'"""
        # 持续上涨，ATR适中
        # close_step=1.0, n=40: MA20斜率 ≈ 1.0/100 = 1% > 0.3%
        # dev = 价格偏离MA ≈ 10/110 ≈ 9% > 1.0%
        # ATR ≈ 3/110 ≈ 2.7% → 先看ATR是否>2.5% → 是的 → 波动！
        # 所以要让ATR小一点
        df = self._make_df(n=40, close_step=1.0, high_premium=0.8, low_premium=0.8)
        regime, desc = classify_regime(df)
        # ATR ≈ 1.6/120 ≈ 1.3% < 2.5% → 不是波动
        # dev ≈ 10/115 ≈ 8.7% > 0.8% → 不是震荡（ATR 1.3% > 1.2% flat_atr）
        # slope ≈ 1% > 0.3%，dev > 1% → 趋势
        self.assertEqual(regime, "趋势")
        self.assertIn("斜率", desc)

    def test_mild_is_transition(self):
        """都不满足 → '过渡'"""
        # 小斜率（<0.3%）但ATR中等（>1.2% flat_atr）
        # close_step=0.1: MA20变化约0.1 per day → 5天变化0.5/100 = 0.5% > 0.3%
        # 用更小的step: close_step=0.05 → 5天变化0.25/100 = 0.25% < 0.3%
        # 但dev也会很小 → < 1% → 不满足趋势
        # ATR: high-low = 2.0 → ATR≈2.0/100 = 2% < 2.5% → 不是波动
        # dev: 价格贴近MA → dev≈0.3% < 0.8% → 但ATR=2% > 1.2% → 不满足震荡
        # → 过渡
        df = self._make_df(n=30, close_step=0.05, high_premium=1.0, low_premium=1.0)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "过渡")

    def test_returns_tuple(self):
        """返回 (regime, description) 二元组"""
        df = self._make_df(n=30)
        result = classify_regime(df)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], str)

    def test_five_regime_types(self):
        """5种状态全覆盖"""
        regimes = set()
        # 1. 未知
        df = self._make_df(n=10)
        r = classify_regime(df)[0]
        regimes.add(r)
        self.assertEqual(r, "未知")

        # 2. 波动：高ATR（high-low=6 → ATR≈6/100=6% > 2.5%）
        df = self._make_df(n=30, high_premium=3.0, low_premium=3.0)
        r = classify_regime(df)[0]
        regimes.add(r)
        self.assertEqual(r, "波动")

        # 3. 震荡：横盘+低ATR
        df = self._make_df(n=30, close_step=0.0, high_premium=0.3, low_premium=0.3)
        r = classify_regime(df)[0]
        regimes.add(r)
        self.assertEqual(r, "震荡")

        # 4. 趋势：强趋势+中低ATR
        df = self._make_df(n=40, close_step=1.0, high_premium=0.8, low_premium=0.8)
        r = classify_regime(df)[0]
        regimes.add(r)
        self.assertEqual(r, "趋势")

        # 5. 过渡：小斜率+中ATR
        df = self._make_df(n=30, close_step=0.05, high_premium=1.0, low_premium=1.0)
        r = classify_regime(df)[0]
        regimes.add(r)
        self.assertEqual(r, "过渡")

        self.assertEqual(len(regimes), 5)

    def test_custom_params_used(self):
        """自定义参数生效"""
        # 默认：横盘+低ATR → 震荡
        df = self._make_df(n=30, close_step=0.0, high_premium=0.3, low_premium=0.3)
        default_regime, _ = classify_regime(df)
        self.assertEqual(default_regime, "震荡")

        # 把 flat_atr 设得极小 → ATR虽小但仍 > flat_atr → 不满足震荡
        # 同时ATR < atr_thresh，slope < trend_slope → 过渡
        tight_params = {
            "atr_thresh": 0.025,  # 默认
            "flat_dev": 0.008,  # 默认
            "flat_atr": 0.001,  # 极小 → ATR=0.6% > 0.1% → 不满足震荡
            "trend_slope": 0.003,  # 默认
            "trend_dev": 0.010,  # 默认
        }
        custom_regime, _ = classify_regime(df, params=tight_params)
        self.assertEqual(custom_regime, "过渡")

    def test_description_contains_numbers(self):
        """描述中包含数值"""
        df = self._make_df(n=30, high_premium=5.0, low_premium=5.0)
        _, desc = classify_regime(df)
        # 描述中应该有百分比数字
        self.assertIn("%", desc)

    def test_atr_priority_highest(self):
        """ATR优先级最高（先判断波动）"""
        # 同时满足高ATR和强趋势 → 应该是波动（ATR优先）
        df = self._make_df(n=40, close_step=2.0, high_premium=5.0, low_premium=5.0)
        regime, _ = classify_regime(df)
        self.assertEqual(regime, "波动")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  策略层纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
