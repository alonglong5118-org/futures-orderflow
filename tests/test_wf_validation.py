#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Walk-Forward 验证工具 — 单元测试
=====================================

1. _calc_max_drawdown — 最大回撤计算
   - 空列表 → 0
   - 全赚 → 0 回撤（持续创新高）
   - 全亏 → 总亏损 = 最大回撤
   - 先赚后亏 → 回撤 = 峰值到谷底的差
   - V 型反弹 → 回撤是中间的最大跌幅
   - 多峰多谷 → 取最大的那个回撤
   - 单笔交易 → 0（亏的话就是那笔亏损额？不对，cumsum 只有一个点，peak=cumulative，dd=0）

2. rolling_windows — 滚动窗口切片
   - 数据正好 = 窗口 → 1 个窗口
   - 数据 > 窗口 → 多个窗口
   - 步长控制窗口重叠度
   - 数据 < 窗口 → 0 个窗口
   - 每个窗口长度 = window_bars
   - 窗口按步长递进
   - 返回 (start, end, df_slice) 三元组
   - 步长 = 窗口大小 → 不重叠
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wf_validation import _calc_max_drawdown, rolling_windows

# ═══════════════════════════════════════════════════════════════════════════
#  1. _calc_max_drawdown
# ═══════════════════════════════════════════════════════════════════════════

class TestCalcMaxDrawdown(unittest.TestCase):
    """_calc_max_drawdown 最大回撤计算。"""

    def test_empty_list_zero(self):
        """空列表 → 0"""
        self.assertEqual(_calc_max_drawdown([]), 0.0)

    def test_all_wins_zero_drawdown(self):
        """全赚 → 0 回撤（持续创新高，peak 永远 = cumulative）"""
        self.assertEqual(_calc_max_drawdown([1.0, 2.0, 0.5, 1.5]), 0.0)

    def test_all_losses_total_loss(self):
        """全亏 → 总亏损 = 最大回撤（peak=0，谷底=总亏损）"""
        # cumsum: [-1, -2, -3]
        # peak: [0, 0, 0] （因为 peak 是 accumulate max，初始 cumsum[0]=-1 < 0 → peak[0]=?）
        # 实际 np.maximum.accumulate 从第一个元素开始
        # cumsum = [-1, -2, -3]
        # peak = [-1, -1, -1] （accumulate max 从 -1 开始，后面更小）
        # drawdown = peak - cumulative = [0, 1, 2]
        # max dd = 2
        result = _calc_max_drawdown([-1.0, -1.0, -1.0])
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_rise_then_fall(self):
        """先赚后亏 → 回撤 = 峰值到谷底的差"""
        # cumsum: [2, 5, 3, 1] （赚2, 赚3→累计5, 亏2→3, 亏2→1）
        # peak:   [2, 5, 5, 5]
        # dd:     [0, 0, 2, 4]
        # max dd = 4
        result = _calc_max_drawdown([2.0, 3.0, -2.0, -2.0])
        self.assertAlmostEqual(result, 4.0, places=6)

    def test_v_shape_rebound(self):
        """V 型反弹 → 回撤是中间的最大跌幅"""
        # cumsum: [3, 1, -1, 1, 3]
        # peak:   [3, 3, 3, 3, 3]
        # dd:     [0, 2, 4, 2, 0]
        # max dd = 4
        result = _calc_max_drawdown([3.0, -2.0, -2.0, 2.0, 2.0])
        self.assertAlmostEqual(result, 4.0, places=6)

    def test_multiple_peaks_valleys(self):
        """多峰多谷 → 取最大的那个回撤"""
        # cumsum: [2, 5, 3, 6, 2]
        # peak:   [2, 5, 5, 6, 6]
        # dd:     [0, 0, 2, 0, 4]
        # max dd = 4（第二个峰 6 → 谷底 2，回撤 4）
        result = _calc_max_drawdown([2.0, 3.0, -2.0, 3.0, -4.0])
        self.assertAlmostEqual(result, 4.0, places=6)

    def test_single_trade_zero_drawdown(self):
        """单笔交易 → 回撤 = 0（peak = cumulative，无落差）"""
        self.assertEqual(_calc_max_drawdown([5.0]), 0.0)
        self.assertEqual(_calc_max_drawdown([-5.0]), 0.0)

    def test_flat_zero(self):
        """全零 → 0 回撤"""
        self.assertEqual(_calc_max_drawdown([0.0, 0.0, 0.0]), 0.0)

    def test_returns_float(self):
        """返回 float"""
        result = _calc_max_drawdown([1.0, -0.5])
        self.assertIsInstance(result, float)

    def test_partial_recovery(self):
        """部分恢复 → 回撤仍是最大跌幅"""
        # cumsum: [5, 2, 3, 1]
        # peak:   [5, 5, 5, 5]
        # dd:     [0, 3, 2, 4]
        # 最大回撤 = 4（从 5 跌到 1）
        result = _calc_max_drawdown([5.0, -3.0, 1.0, -2.0])
        self.assertAlmostEqual(result, 4.0, places=6)


# ═══════════════════════════════════════════════════════════════════════════
#  2. rolling_windows
# ═══════════════════════════════════════════════════════════════════════════

class TestRollingWindows(unittest.TestCase):
    """rolling_windows 滚动窗口切片。"""

    def _make_df(self, n):
        """构造 n 根 K 线的 DataFrame"""
        return pd.DataFrame({
            "close": range(100, 100 + n),
            "high": range(101, 101 + n),
            "low": range(99, 99 + n),
        })

    def test_exact_one_window(self):
        """数据正好 = 窗口 → 1 个窗口"""
        df = self._make_df(10)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][0], 0)  # start
        self.assertEqual(windows[0][1], 10)  # end
        self.assertEqual(len(windows[0][2]), 10)

    def test_multiple_windows(self):
        """数据 > 窗口 → 多个窗口"""
        df = self._make_df(25)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        # 窗口: [0:10], [5:15], [10:20], [15:25] → 4 个
        self.assertEqual(len(windows), 4)

    def test_step_size(self):
        """步长控制窗口重叠度"""
        df = self._make_df(20)
        # 步长 = 窗口大小 → 不重叠
        windows_no_overlap = rolling_windows(df, window_bars=10, step_bars=10)
        self.assertEqual(len(windows_no_overlap), 2)
        self.assertEqual(windows_no_overlap[0][1], windows_no_overlap[1][0])

    def test_data_smaller_than_window(self):
        """数据 < 窗口 → 0 个窗口"""
        df = self._make_df(5)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        self.assertEqual(len(windows), 0)

    def test_each_window_correct_length(self):
        """每个窗口长度 = window_bars"""
        df = self._make_df(30)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        for start, end, slc in windows:
            self.assertEqual(len(slc), 10)
            self.assertEqual(end - start, 10)

    def test_windows_progress_by_step(self):
        """窗口按步长递进"""
        df = self._make_df(30)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        for i in range(len(windows) - 1):
            self.assertEqual(windows[i+1][0] - windows[i][0], 5)

    def test_returns_triples(self):
        """返回 (start, end, df_slice) 三元组"""
        df = self._make_df(15)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        for w in windows:
            self.assertEqual(len(w), 3)
            self.assertIsInstance(w[0], int)
            self.assertIsInstance(w[1], int)
            self.assertIsInstance(w[2], pd.DataFrame)

    def test_no_overlap_windows(self):
        """步长 = 窗口大小 → 不重叠"""
        df = self._make_df(30)
        windows = rolling_windows(df, window_bars=10, step_bars=10)
        # 窗口 [0:10], [10:20], [20:30]
        self.assertEqual(len(windows), 3)
        for i in range(len(windows) - 1):
            self.assertEqual(windows[i][1], windows[i+1][0])

    def test_first_window_starts_at_zero(self):
        """第一个窗口从 0 开始"""
        df = self._make_df(20)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        self.assertEqual(windows[0][0], 0)

    def test_slice_is_copy(self):
        """切片是 copy（修改不影响原数据）"""
        df = self._make_df(10)
        windows = rolling_windows(df, window_bars=10, step_bars=5)
        slc = windows[0][2]
        slc.iloc[0, 0] = 9999
        # 原数据不变
        self.assertEqual(df.iloc[0, 0], 100)

    def test_large_step_few_windows(self):
        """大步长 → 窗口少"""
        df = self._make_df(100)
        windows_small_step = rolling_windows(df, window_bars=10, step_bars=1)
        windows_large_step = rolling_windows(df, window_bars=10, step_bars=10)
        self.assertGreater(len(windows_small_step), len(windows_large_step))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Walk-Forward 验证工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
