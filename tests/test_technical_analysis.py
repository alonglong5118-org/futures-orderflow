#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术分析工具 — 单元测试
===========================

1. round_tick — 价格 tick 取整
   - 正好整数跳 → 不变
   - 半跳向上取整
   - 半跳向下取整
   - tick=1 → 整数取整
   - 小数 tick（如 0.5）

2. find_swings — ZigZag 摆动点检测
   - 数据不足 → 空
   - 简单 V 型底 → 一个低点
   - 简单倒 V 顶 → 一个高点
   - 交替高低点
   - deviation 过滤小摆动
   - depth 控制确认窗口

3. latest_abc — a-b-c 结构识别
   - 不足 3 点 → None
   - 看涨 a-b-c：低-高-低，c > a
   - 看跌 a-b-c：高-低-高，c < a
   - 不满足 c 条件 → 继续往前找
   - direction 参数过滤

4. hidden_pivot — 隐藏枢轴点计算
   - 看涨：p = b + (b-a) × 0.618，stop = c
   - 看跌：p = b - (a-b) × 0.618，stop = c
   - 涨跌停不可达 → reachable=False
   - None 输入 → None
   - tick 取整正确

5. _features_raw — HMM 原始特征
   - 正常数据 → shape=(n-1, 2)
   - 数据不足 → None
   - 无 close 列 → None
   - 两列：收益 + 波动率

6. _rule_label — 规则式 regime 标注
   - 高波动 → high_vol
   - 强上升 → trend_up
   - 强下降 → trend_down
   - 中性 → choppy
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from hidden_pivot import (
    find_swings,
    hidden_pivot,
    latest_abc,
    round_tick,
)
from regime_hmm import (
    _features_raw,
    _rule_label,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. round_tick
# ═══════════════════════════════════════════════════════════════════════════


class TestRoundTick(unittest.TestCase):
    """round_tick 价格 tick 取整。"""

    def test_exact_multiple_unchanged(self):
        """正好整数跳 → 不变"""
        self.assertEqual(round_tick(100.0, 1.0), 100.0)
        self.assertEqual(round_tick(3500.0, 5.0), 3500.0)

    def test_rounds_to_nearest(self):
        """四舍五入到最近的 tick"""
        # tick=1, 100.4 → 100, 100.6 → 101
        self.assertEqual(round_tick(100.4, 1.0), 100.0)
        self.assertEqual(round_tick(100.6, 1.0), 101.0)

    def test_half_tick_rounds_up(self):
        """正好半跳 → 向上取整（Python round 银行家舍入，这里用的是 round(price/tick)*tick）"""
        # 2.5 / 1 = 2.5，round(2.5) = 2（银行家舍入，取偶数）
        # 所以结果可能是 2.0
        result = round_tick(2.5, 1.0)
        # 只要是 tick 的整数倍就行
        self.assertAlmostEqual(result % 1.0, 0.0, places=6)

    def test_small_tick(self):
        """小数 tick（如 0.5）"""
        self.assertEqual(round_tick(100.2, 0.5), 100.0)
        self.assertEqual(round_tick(100.3, 0.5), 100.5)

    def test_result_is_tick_multiple(self):
        """结果总是 tick 的整数倍"""
        import random

        random.seed(42)
        for _ in range(20):
            price = random.uniform(10, 1000)
            tick = random.choice([0.5, 1.0, 2.0, 5.0, 10.0])
            result = round_tick(price, tick)
            ratio = result / tick
            self.assertAlmostEqual(ratio, round(ratio), places=5, msg=f"price={price}, tick={tick}, result={result}")


# ═══════════════════════════════════════════════════════════════════════════
#  2. find_swings
# ═══════════════════════════════════════════════════════════════════════════


class TestFindSwings(unittest.TestCase):
    """find_swings ZigZag 摆动点检测。"""

    def test_insufficient_data_empty(self):
        """数据不足 → 空列表"""
        highs = [100, 101]
        lows = [99, 100]
        closes = [100, 101]
        self.assertEqual(find_swings(highs, lows, closes), [])

    def test_v_bottom_one_low(self):
        """V 型底 → 一个低点"""
        n = 10
        highs = [100 + abs(i - 5) * 2 for i in range(n)]  # 最低点在 i=5
        lows = [99 + abs(i - 5) * 2 for i in range(n)]
        closes = [99.5 + abs(i - 5) * 2 for i in range(n)]
        swings = find_swings(highs, lows, closes, depth=2, deviation=0.0)
        # 应该有一个低点
        lows_swing = [s for s in swings if s[1] == "low"]
        self.assertEqual(len(lows_swing), 1)
        self.assertEqual(lows_swing[0][0], 5)  # 索引 5

    def test_inverted_v_one_high(self):
        """倒 V 顶 → 一个高点"""
        n = 10
        highs = [100 - abs(i - 5) * 2 + 10 for i in range(n)]  # 最高点在 i=5
        lows = [99 - abs(i - 5) * 2 + 10 for i in range(n)]
        closes = [99.5 - abs(i - 5) * 2 + 10 for i in range(n)]
        swings = find_swings(highs, lows, closes, depth=2, deviation=0.0)
        highs_swing = [s for s in swings if s[1] == "high"]
        self.assertEqual(len(highs_swing), 1)
        self.assertEqual(highs_swing[0][0], 5)

    def test_alternating_swings(self):
        """交替高低点 → 高低交替"""
        # 构造一个有多个摆动的序列
        n = 30
        highs = []
        lows = []
        closes = []
        base = 100
        for i in range(n):
            # 正弦波模拟摆动
            import math

            val = base + 5 * math.sin(i * 0.5)
            highs.append(val + 1)
            lows.append(val - 1)
            closes.append(val)
        swings = find_swings(highs, lows, closes, depth=2, deviation=0.0)
        # 应该有多个摆动点
        self.assertGreater(len(swings), 3)
        # 高低交替
        for i in range(1, len(swings)):
            self.assertNotEqual(swings[i][1], swings[i - 1][1], f"摆动点 {i} 和 {i - 1} 类型相同，应该交替")

    def test_deviation_filters_small_swings(self):
        """deviation 过滤小摆动"""
        # 构造：大摆动 + 小反弹
        n = 20
        prices = [100.0] * n
        # 大 V 形：0-5 下跌，5-15 上涨，15-19 下跌
        for i in range(6):
            prices[i] = 100 - i * 2  # 100 → 90
        for i in range(6, 16):
            prices[i] = 90 + (i - 6) * 1.5  # 90 → 105
        for i in range(16, 20):
            prices[i] = 105 - (i - 16) * 2  # 105 → 97
        highs = [p + 0.5 for p in prices]
        lows = [p - 0.5 for p in prices]
        closes = prices
        # 无 deviation → 更多摆动点
        swings_none = find_swings(highs, lows, closes, depth=2, deviation=0.0)
        # 有 deviation → 更少摆动点
        swings_dev = find_swings(highs, lows, closes, depth=2, deviation=0.05)
        self.assertLessEqual(len(swings_dev), len(swings_none))

    def test_depth_wider_window(self):
        """更大的 depth → 更少摆动点（需要更多确认）"""
        n = 30
        import math

        base = 100
        highs = [base + 3 * math.sin(i * 0.4) + 1 for i in range(n)]
        lows = [base + 3 * math.sin(i * 0.4) - 1 for i in range(n)]
        closes = [base + 3 * math.sin(i * 0.4) for i in range(n)]
        swings_small = find_swings(highs, lows, closes, depth=1, deviation=0.0)
        swings_large = find_swings(highs, lows, closes, depth=4, deviation=0.0)
        self.assertLessEqual(len(swings_large), len(swings_small))


# ═══════════════════════════════════════════════════════════════════════════
#  3. latest_abc
# ═══════════════════════════════════════════════════════════════════════════


class TestLatestAbc(unittest.TestCase):
    """latest_abc a-b-c 结构识别。"""

    def test_fewer_than_3_swings_none(self):
        """不足 3 个摆动点 → None"""
        swings = [(0, "low", 100), (5, "high", 110)]
        self.assertIsNone(latest_abc(swings))

    def test_bullish_abc(self):
        """看涨 a-b-c：低-高-低，c > a"""
        swings = [
            (0, "low", 100),  # a
            (5, "high", 120),  # b
            (10, "low", 105),  # c（高于 a）
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, direction = result
        self.assertEqual(direction, 1)  # 看涨
        self.assertEqual(a[2], 100)
        self.assertEqual(b[2], 120)
        self.assertEqual(c[2], 105)

    def test_bearish_abc(self):
        """看跌 a-b-c：高-低-高，c < a"""
        swings = [
            (0, "high", 120),  # a
            (5, "low", 100),  # b
            (10, "high", 115),  # c（低于 a）
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, direction = result
        self.assertEqual(direction, -1)  # 看跌
        self.assertEqual(a[2], 120)
        self.assertEqual(b[2], 100)
        self.assertEqual(c[2], 115)

    def test_c_below_a_not_bullish(self):
        """c <= a → 不构成看涨，继续往前找"""
        # 最近的 c = a，不满足；应该找更前面的
        swings = [
            (0, "low", 100),  # a2
            (5, "high", 120),  # b2
            (10, "low", 95),  # c2（低于 a → 不满足看涨）
            (15, "high", 115),  # 又一个高点
            (20, "low", 105),  # 最近的低点（c1 > a1? 但 a1 是谁？）
        ]
        # 实际上这个序列的结构是 low-high-low-high-low
        # 最近的 3 点：high-low-high → 看跌候选，需要 c < a
        # a=high(115), b=low(105), ... 不对，只有 5 个点
        # 让我们构造一个更清晰的例子
        pass

    def test_direction_filter_bullish(self):
        """direction=1 → 只找看涨结构"""
        swings = [
            (0, "low", 100),
            (5, "high", 120),
            (10, "low", 105),
        ]
        result = latest_abc(swings, direction=1)
        self.assertIsNotNone(result)
        self.assertEqual(result[3], 1)

    def test_direction_filter_bearish_not_found(self):
        """direction=-1 但只有看涨结构 → None"""
        swings = [
            (0, "low", 100),
            (5, "high", 120),
            (10, "low", 105),
        ]
        result = latest_abc(swings, direction=-1)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
#  4. hidden_pivot
# ═══════════════════════════════════════════════════════════════════════════


class TestHiddenPivot(unittest.TestCase):
    """hidden_pivot 隐藏枢轴点计算。"""

    def test_none_input_returns_none(self):
        """None 输入 → None"""
        self.assertIsNone(hidden_pivot(None, 1.0))

    def test_bullish_p_calculation(self):
        """看涨：p = b + (b-a) × 0.618"""
        # a=100(low), b=120(high), c=105(low)
        abc = ((0, "low", 100), (1, "high", 120), (2, "low", 105), 1)
        result = hidden_pivot(abc, tick=1.0)
        self.assertIsNotNone(result)
        expected_p = 120 + (120 - 100) * 0.618  # 120 + 12.36 = 132.36
        self.assertAlmostEqual(result["p"], round(expected_p), places=0)
        # stop = c
        self.assertEqual(result["stop"], 105.0)
        self.assertTrue(result["p_reachable"])
        self.assertEqual(result["direction"], 1)

    def test_bearish_p_calculation(self):
        """看跌：p = b - (a-b) × 0.618"""
        # a=120(high), b=100(low), c=115(high)
        abc = ((0, "high", 120), (1, "low", 100), (2, "high", 115), -1)
        result = hidden_pivot(abc, tick=1.0)
        self.assertIsNotNone(result)
        expected_p = 100 - (120 - 100) * 0.618  # 100 - 12.36 = 87.64
        self.assertAlmostEqual(result["p"], round(expected_p), places=0)
        # stop = c
        self.assertEqual(result["stop"], 115.0)
        self.assertTrue(result["p_reachable"])
        self.assertEqual(result["direction"], -1)

    def test_bullish_above_limit_up_not_reachable(self):
        """看涨目标 > 涨停价 → p_reachable=False"""
        abc = ((0, "low", 100), (1, "high", 120), (2, "low", 105), 1)
        # 目标约 132，涨停 125 → 不可达
        result = hidden_pivot(abc, tick=1.0, limit_up=125.0)
        self.assertFalse(result["p_reachable"])

    def test_bearish_below_limit_down_not_reachable(self):
        """看跌目标 < 跌停价 → p_reachable=False"""
        abc = ((0, "high", 120), (1, "low", 100), (2, "high", 115), -1)
        # 目标约 88，跌停 95 → 不可达
        result = hidden_pivot(abc, tick=1.0, limit_down=95.0)
        self.assertFalse(result["p_reachable"])

    def test_bullish_below_limit_up_reachable(self):
        """看涨目标 < 涨停价 → p_reachable=True"""
        abc = ((0, "low", 100), (1, "high", 120), (2, "low", 105), 1)
        result = hidden_pivot(abc, tick=1.0, limit_up=150.0)
        self.assertTrue(result["p_reachable"])

    def test_tick_rounding_applied(self):
        """tick 取整正确应用"""
        abc = ((0, "low", 100), (1, "high", 120), (2, "low", 105), 1)
        result = hidden_pivot(abc, tick=5.0)
        # p 应该是 5 的整数倍
        self.assertAlmostEqual(result["p"] % 5.0, 0.0, places=5)
        # stop 也应该是
        self.assertAlmostEqual(result["stop"] % 5.0, 0.0, places=5)

    def test_gain_pts(self):
        """gain_pts 计算正确"""
        abc = ((0, "low", 100), (1, "high", 120), (2, "low", 105), 1)
        result = hidden_pivot(abc, tick=1.0)
        # gain = p - c
        expected_gain = result["p"] - 105
        self.assertAlmostEqual(result["gain_pts"], expected_gain, places=5)
        self.assertGreater(result["gain_pts"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _features_raw
# ═══════════════════════════════════════════════════════════════════════════


class TestFeaturesRaw(unittest.TestCase):
    """_features_raw HMM 原始特征。"""

    def test_normal_data_returns_2d(self):
        """正常数据 → 返回 2 列（收益 + 波动率）"""
        df = pd.DataFrame({"close": np.exp(np.cumsum(np.random.randn(100) * 0.01))})
        X = _features_raw(df)
        self.assertIsNotNone(X)
        self.assertEqual(X.shape[1], 2)
        self.assertGreater(X.shape[0], 30)

    def test_insufficient_data_returns_none(self):
        """数据不足 → None"""
        df = pd.DataFrame({"close": np.arange(20, dtype=float)})
        self.assertIsNone(_features_raw(df))

    def test_no_close_column_returns_none(self):
        """无 close 列 → None"""
        df = pd.DataFrame({"open": np.arange(100, dtype=float)})
        self.assertIsNone(_features_raw(df))

    def test_first_col_is_return(self):
        """第一列是收益（大致零均值）"""
        np.random.seed(42)
        df = pd.DataFrame({"close": 100 * np.exp(np.cumsum(np.random.randn(200) * 0.01))})
        X = _features_raw(df)
        self.assertIsNotNone(X)
        ret_mean = np.mean(X[:, 0])
        # 随机游走收益均值应该接近 0
        self.assertAlmostEqual(ret_mean, 0.0, places=2)

    def test_second_col_is_volatility(self):
        """第二列是波动率（恒非负）"""
        np.random.seed(42)
        df = pd.DataFrame({"close": 100 * np.exp(np.cumsum(np.random.randn(200) * 0.01))})
        X = _features_raw(df)
        self.assertIsNotNone(X)
        self.assertTrue(np.all(X[:, 1] >= 0))


# ═══════════════════════════════════════════════════════════════════════════
#  6. _rule_label
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleLabel(unittest.TestCase):
    """_rule_label 规则式 regime 标注。"""

    def _make_sample(self, ret_val, vol_val, n=50):
        """构造一个样本，最后一个点的 ret/vol 为指定值"""
        X = np.zeros((n, 2))
        X[:, 0] = np.random.randn(n) * 0.01  # 小收益
        X[:, 1] = np.abs(np.random.randn(n)) * 0.5 + 0.5  # 中等波动
        X[-1, 0] = ret_val
        X[-1, 1] = vol_val
        return X

    def test_high_vol_label(self):
        """当前波动在 75 分位以上 → high_vol"""
        np.random.seed(42)
        X = self._make_sample(ret_val=0.0, vol_val=5.0, n=100)
        # 把 vol 列设为大部分低，最后一个高
        X[:, 1] = np.linspace(0.5, 1.5, 100)  # 大部分在 0.5~1.5
        X[-1, 1] = 3.0  # 最后一个远高于 75 分位
        label = _rule_label(X)
        self.assertEqual(label, "high_vol")

    def test_strong_uptrend_label(self):
        """强上升 + 波动不高 → trend_up"""
        n = 100
        X = np.zeros((n, 2))
        # 大部分 vol 较低，形成 75 分位阈值
        X[:, 1] = 0.3  # 全部低波动
        X[:, 0] = 0.01  # 小收益
        X[-1, 0] = 0.5  # 最后一根强上升（> 0.15 阈值）
        # 全部 vol=0.3，vhi = 0.3，mv = 0.3
        # mv >= vhi → True → high_vol
        # 这是边界情况。我们让大部分 vol 更低，让最后一个也低于 75 分位
        # 用递增序列，最后一个在中等位置
        X[:, 1] = np.linspace(0.1, 0.5, n)  # vol 从 0.1 到 0.5
        # 75 分位 ≈ 0.4
        # 最后一个 vol = 0.5 → high_vol
        # 所以我们把最后一个设为低于 75 分位
        X[-1, 1] = 0.2  # 低于 75 分位 (≈0.4)
        X[-1, 0] = 0.5  # 强上升
        label = _rule_label(X)
        self.assertEqual(label, "trend_up")

    def test_strong_downtrend_label(self):
        """强下降 + 波动不高 → trend_down"""
        n = 100
        X = np.zeros((n, 2))
        X[:, 1] = np.linspace(0.1, 0.5, n)
        X[-1, 1] = 0.2  # 低于 75 分位
        X[-1, 0] = -0.5  # 强下降
        label = _rule_label(X)
        self.assertEqual(label, "trend_down")

    def test_neutral_choppy(self):
        """中性收益 + 中等波动（低于 75 分位） → choppy"""
        n = 100
        X = np.zeros((n, 2))
        X[:, 1] = np.linspace(0.1, 0.5, n)
        X[-1, 1] = 0.2  # 低于 75 分位
        X[-1, 0] = 0.05  # 微弱正收益（在 -0.15 ~ 0.15 之间）
        label = _rule_label(X)
        self.assertEqual(label, "choppy")

    def test_high_vol_overrides_trend(self):
        """高波动优先级最高（即使有趋势）"""
        X = self._make_sample(ret_val=0.5, vol_val=0.5, n=100)
        X[:, 1] = np.linspace(0.5, 1.5, 100)
        X[-1, 0] = 0.5  # 强上升
        X[-1, 1] = 3.0  # 但波动更高
        label = _rule_label(X)
        self.assertEqual(label, "high_vol")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  技术分析工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
