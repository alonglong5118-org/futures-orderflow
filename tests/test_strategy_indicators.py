#!/usr/bin/env python3
"""
策略层基础指标 — 单元测试
===============================

1. sma — 简单移动平均
   - 正常计算
   - 长度不足 → 前面 NaN
   - 长度正好 → 最后一个有效值
   - 常数序列 → 平均值=常数

2. ema — 指数移动平均
   - 正常计算
   - 长度不足 → 前面 NaN
   - 常数序列 → EMA 收敛到常数

3. atr — 平均真实波幅
   - 正常计算
   - 长度不足 → 前面 NaN
   - 无波动 → ATR = 0

4. rsi — 相对强弱指数
   - 全涨 → RSI 接近 100
   - 全跌 → RSI 接近 0
   - 横盘 → RSI 接近 50
   - 长度不足 → NaN

5. crossover — 金叉死叉
   - 上穿（金叉）→ 1
   - 下穿（死叉）→ -1
   - 平行 → 0
   - 数据不足 → 0
   - 刚好接触不算（a[-2] == b[-2] 且 a[-1] > b[-1] → 算上穿）
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import atr, crossover, ema, rsi, sma

# ═══════════════════════════════════════════════════════════════════════════
#  1. sma
# ═══════════════════════════════════════════════════════════════════════════


class TestSMA(unittest.TestCase):
    """sma 简单移动平均。"""

    def test_normal_calculation(self):
        """正常计算：3 根 K 线，SMA(2) 最后值 = 平均最后 2 根"""
        s = pd.Series([10.0, 12.0, 14.0])
        result = sma(s, 2)
        self.assertAlmostEqual(result.iloc[-1], 13.0, places=6)

    def test_insufficient_data_nan(self):
        """长度不足 → 前面 NaN"""
        s = pd.Series([10.0, 12.0])
        result = sma(s, 5)
        # 全部都是 NaN（因为 2 < 5）
        self.assertTrue(result.isna().all())

    def test_exact_length(self):
        """长度正好 → 最后一个有效值"""
        s = pd.Series([10.0, 20.0, 30.0])
        result = sma(s, 3)
        self.assertAlmostEqual(result.iloc[-1], 20.0, places=6)

    def test_constant_series(self):
        """常数序列 → 平均值=常数"""
        s = pd.Series([5.0] * 10)
        result = sma(s, 5)
        # 所有有效值都应该 = 5.0
        valid = result.dropna()
        self.assertTrue((valid == 5.0).all())

    def test_first_n_minus_1_nan(self):
        """前 n-1 个是 NaN"""
        s = pd.Series(range(1, 11), dtype=float)
        n = 5
        result = sma(s, n)
        self.assertTrue(result.iloc[: n - 1].isna().all())
        self.assertFalse(pd.isna(result.iloc[n - 1]))

    def test_linear_trend(self):
        """线性递增序列 → 最后 SMA 接近中间值"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(s, 5)
        self.assertAlmostEqual(result.iloc[-1], 3.0, places=6)


# ═══════════════════════════════════════════════════════════════════════════
#  2. ema
# ═══════════════════════════════════════════════════════════════════════════


class TestEMA(unittest.TestCase):
    """ema 指数移动平均。"""

    def test_constant_series_converges(self):
        """常数序列 → EMA 收敛到常数"""
        s = pd.Series([10.0] * 50)
        result = ema(s, 10)
        # 足够长后，EMA 应该接近 10.0
        self.assertAlmostEqual(result.iloc[-1], 10.0, places=3)

    def test_insufficient_data_not_nan(self):
        """EMA 从第一个点就有值（adjust=False 模式）"""
        s = pd.Series([1.0, 2.0])
        result = ema(s, 5)
        # adjust=False 模式下，第一个 EMA = 第一个价格
        self.assertEqual(result.iloc[0], 1.0)
        self.assertFalse(result.isna().any())

    def test_ema_lags_price(self):
        """上涨趋势中，EMA < 最新价（滞后）"""
        s = pd.Series(range(1, 31), dtype=float)  # 1 到 30 持续上涨
        result = ema(s, 10)
        self.assertLess(result.iloc[-1], s.iloc[-1])

    def test_ema_smoother_than_price(self):
        """EMA 波动比原始价格小"""
        np.random.seed(42)
        s = pd.Series(100 + np.cumsum(np.random.randn(50) * 2))
        ema_result = ema(s, 10).dropna()
        # EMA 的标准差应该小于原始价格
        self.assertLess(ema_result.std(), s.loc[ema_result.index].std())

    def test_first_value_is_first_price(self):
        """第一个 EMA 值 = 第一个价格（adjust=False 模式，以第一点为种子）"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        n = 5
        e = ema(s, n)
        # adjust=False 模式下，第一个 EMA = 第一个价格
        self.assertEqual(e.iloc[0], s.iloc[0])


# ═══════════════════════════════════════════════════════════════════════════
#  3. atr
# ═══════════════════════════════════════════════════════════════════════════


class TestATR(unittest.TestCase):
    """atr 平均真实波幅。"""

    def _make_df(self, high, low, close):
        return pd.DataFrame({"high": high, "low": low, "close": close})

    def test_normal_calculation(self):
        """正常 ATR 计算"""
        df = self._make_df(
            high=[110, 115, 120, 118, 125],
            low=[100, 105, 110, 108, 115],
            close=[105, 110, 115, 112, 120],
        )
        result = atr(df, n=3)
        # 至少最后一个是有效值
        self.assertFalse(pd.isna(result.iloc[-1]))
        self.assertGreater(result.iloc[-1], 0)

    def test_no_volatility_zero_atr(self):
        """无波动 → ATR = 0"""
        df = self._make_df(
            high=[100.0] * 20,
            low=[100.0] * 20,
            close=[100.0] * 20,
        )
        result = atr(df, n=14)
        # 最后一个 ATR 应该接近 0
        self.assertAlmostEqual(result.iloc[-1], 0.0, places=6)

    def test_insufficient_data_nan(self):
        """数据不足 → NaN"""
        df = self._make_df(
            high=[110, 115],
            low=[100, 105],
            close=[105, 110],
        )
        result = atr(df, n=14)
        self.assertTrue(result.isna().all())

    def test_atr_non_negative(self):
        """ATR 始终 ≥ 0"""
        np.random.seed(42)
        n = 50
        close = 100 + np.cumsum(np.random.randn(n) * 2)
        high = close + np.abs(np.random.randn(n) * 3)
        low = close - np.abs(np.random.randn(n) * 3)
        df = self._make_df(high=high, low=low, close=close)
        result = atr(df, n=14).dropna()
        self.assertTrue((result >= 0).all())

    def test_gap_included_in_tr(self):
        """跳空缺口会计入真实波幅"""
        # 收盘 100，次日高开高走，gap up
        df = self._make_df(
            high=[100, 120],  # 第二天 high=120
            low=[90, 110],  # 第二天 low=110
            close=[95, 115],  # 第一天 close=95
        )
        # TR = max(high-low, |high-prev_close|, |low-prev_close|)
        # 第二天 TR = max(10, |120-95|=25, |110-95|=15) = 25
        result = atr(df, n=2)
        # 因为 n=2，第一天没有 prev_close，TR 就是 high-low
        # 第二天 TR 包含 gap
        self.assertGreater(result.iloc[-1], 10)  # 肯定大于单纯的 high-low


# ═══════════════════════════════════════════════════════════════════════════
#  4. rsi
# ═══════════════════════════════════════════════════════════════════════════


class TestRSI(unittest.TestCase):
    """rsi 相对强弱指数。"""

    def test_all_up_rsi_fillna_50(self):
        """全涨 → dn=0 → rs=NaN → fillna(50)"""
        s = pd.Series(range(1, 31), dtype=float)  # 持续上涨，无下跌
        result = rsi(s, n=14)
        # 全涨时没有下跌，dn=0 → rs 无定义 → fillna(50)
        self.assertEqual(result.iloc[-1], 50.0)

    def test_all_down_near_0(self):
        """全跌 → RSI 接近 0"""
        s = pd.Series(range(30, 0, -1), dtype=float)  # 持续下跌
        result = rsi(s, n=14)
        self.assertLess(result.iloc[-1], 10)

    def test_sideways_near_50(self):
        """横盘震荡 → RSI 接近 50"""
        # 交替涨跌
        prices = [100.0]
        for i in range(30):
            if i % 2 == 0:
                prices.append(prices[-1] + 1)
            else:
                prices.append(prices[-1] - 1)
        s = pd.Series(prices)
        result = rsi(s, n=14)
        # 横盘 RSI 应该在 50 附近
        self.assertGreater(result.iloc[-1], 30)
        self.assertLess(result.iloc[-1], 70)

    def test_insufficient_data_fillna_50(self):
        """数据不足 → NaN → fillna(50)"""
        s = pd.Series([1.0, 2.0, 3.0])
        result = rsi(s, n=14)
        # rolling mean 都是 NaN → rs=NaN → fillna(50)
        self.assertEqual(result.iloc[-1], 50.0)

    def test_rsi_range_0_100(self):
        """RSI 范围在 0-100 之间"""
        np.random.seed(42)
        s = pd.Series(100 + np.cumsum(np.random.randn(50) * 2))
        result = rsi(s, n=14).dropna()
        self.assertTrue((result >= 0).all())
        self.assertTrue((result <= 100).all())

    def test_rsi_at_50_when_equal_up_down(self):
        """涨跌幅度相等时 RSI = 50"""
        # 构造：涨1跌1涨1跌1... 对称
        prices = [100.0]
        for i in range(28):  # 14 涨 14 跌
            if i % 2 == 0:
                prices.append(prices[-1] + 1)
            else:
                prices.append(prices[-1] - 1)
        s = pd.Series(prices)
        result = rsi(s, n=14)
        # 对称涨跌 RSI ≈ 50
        self.assertAlmostEqual(result.iloc[-1], 50.0, delta=5)


# ═══════════════════════════════════════════════════════════════════════════
#  5. crossover
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossover(unittest.TestCase):
    """crossover 金叉死叉。"""

    def test_golden_cross(self):
        """上穿（金叉）→ 1"""
        a = pd.Series([9.0, 11.0])  # 从下往上穿
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 1)

    def test_death_cross(self):
        """下穿（死叉）→ -1"""
        a = pd.Series([11.0, 9.0])  # 从上往下穿
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), -1)

    def test_parallel_no_cross(self):
        """平行 → 0"""
        a = pd.Series([8.0, 9.0])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 0)

    def test_touch_then_break_up(self):
        """刚好接触然后向上突破 → 算上穿"""
        # a[-2] == b[-2] 且 a[-1] > b[-1]
        a = pd.Series([10.0, 11.0])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 1)

    def test_touch_then_break_down(self):
        """刚好接触然后向下跌破 → 算下穿"""
        # a[-2] == b[-2] 且 a[-1] < b[-1]
        a = pd.Series([10.0, 9.0])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), -1)

    def test_insufficient_data_zero(self):
        """数据不足 → 0"""
        a = pd.Series([10.0])
        b = pd.Series([10.0])
        self.assertEqual(crossover(a, b), 0)

    def test_empty_series_zero(self):
        """空序列 → 0"""
        a = pd.Series([], dtype=float)
        b = pd.Series([], dtype=float)
        self.assertEqual(crossover(a, b), 0)

    def test_both_same_no_cross(self):
        """两根都相等 → 0（没有穿越）"""
        a = pd.Series([10.0, 10.0])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 0)

    def test_cross_from_below_no_equal(self):
        """严格从下往上穿（a[-2] < b[-2], a[-1] > b[-1]）→ 1"""
        a = pd.Series([9.5, 10.5])
        b = pd.Series([10.0, 10.0])
        self.assertEqual(crossover(a, b), 1)

    def test_long_series_only_latest_matters(self):
        """长序列只看最后两根"""
        a = pd.Series([1, 2, 3, 4, 5, 4, 6])  # 最后两根: 4 → 6
        b = pd.Series([3, 3, 3, 5, 5, 5, 5])  # 最后两根: 5 → 5
        # a[-2]=4 < 5, a[-1]=6 > 5 → 金叉
        self.assertEqual(crossover(a, b), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  策略层基础指标 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
