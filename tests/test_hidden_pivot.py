#!/usr/bin/env python3
"""
隐秘枢轴 Hidden Pivot — 单元测试
=======================================

1. round_tick — tick 取整
   - 正常取整到最近 tick
   - 正好是 tick 整数倍 → 不变
   - 小 tick（0.01）精度
   - 零价格 → 0

2. find_swings — ZigZag 摆动点检测
   - 数据不足 → 空列表
   - 简单 V 型 → 检测到 1 个低点
   - 简单 Λ 型 → 检测到 1 个高点
   - 完整 N 型（低-高-低）→ 检测到 3 个摆动点
   - deviation 过滤小波动
   - depth 参数影响检测密度
   - 高低交替（不会连续两个 high）
   - 按时间升序返回

3. latest_abc — 最近 a-b-c 结构
   - 不足 3 个摆动点 → None
   - 多头结构（low-high-low, c>a）→ 返回 direction=1
   - 空头结构（high-low-high, c<a）→ 返回 direction=-1
   - 方向过滤（direction=1 只找多头）
   - 没有合法结构 → None
   - 找最近的（从后往前找）
   - c = a（equal，不满足 higher low / lower high）→ 不算

4. hidden_pivot — 计算目标位与止损
   - 多头结构：p = b + (b-a) * 0.618，stop = c
   - 空头结构：p = b - (a-b) * 0.618，stop = c
   - abc=None → 返回 None
   - tick 取整生效
   - 涨停板限制：p 超过涨停 → p_reachable=False
   - 跌停板限制：p 低于跌停 → p_reachable=False
   - gain_pts = |p - c|（tick 取整后）
   - 返回字段齐全
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from hidden_pivot import find_swings, hidden_pivot, latest_abc, round_tick

# ═══════════════════════════════════════════════════════════════════════════
#  1. round_tick
# ═══════════════════════════════════════════════════════════════════════════


class TestRoundTick(unittest.TestCase):
    """round_tick tick 取整。"""

    def test_round_to_nearest_tick(self):
        """取整到最近的 tick 倍数"""
        # tick=1，10.6 → 11
        self.assertEqual(round_tick(10.6, 1), 11.0)
        # tick=1，10.4 → 10
        self.assertEqual(round_tick(10.4, 1), 10.0)

    def test_exact_multiple_unchanged(self):
        """正好是 tick 整数倍 → 不变"""
        self.assertEqual(round_tick(10.0, 1), 10.0)
        self.assertEqual(round_tick(10.5, 0.5), 10.5)

    def test_small_tick_precision(self):
        """小 tick（0.01）精度"""
        self.assertAlmostEqual(round_tick(10.126, 0.01), 10.13, places=6)
        self.assertAlmostEqual(round_tick(10.124, 0.01), 10.12, places=6)

    def test_zero_price(self):
        """零价格 → 0"""
        self.assertEqual(round_tick(0.0, 1), 0.0)
        self.assertEqual(round_tick(0.0, 0.01), 0.0)

    def test_half_tick_rounds_up(self):
        """正好一半 → 银行家舍入（round 内置行为）"""
        # Python round 是银行家舍入，2.5 → 2，3.5 → 4
        # tick=1 时 2.5 → 2.0
        self.assertEqual(round_tick(2.5, 1), 2.0)
        self.assertEqual(round_tick(3.5, 1), 4.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. find_swings
# ═══════════════════════════════════════════════════════════════════════════


class TestFindSwings(unittest.TestCase):
    """find_swings ZigZag 摆动点检测。"""

    def test_insufficient_data_empty(self):
        """数据不足 → 空列表"""
        # 需要 depth*2+2 = 8 根，给 5 根不够
        highs = [10, 11, 12, 11, 10]
        lows = [9, 10, 11, 10, 9]
        closes = [9.5, 10.5, 11.5, 10.5, 9.5]
        result = find_swings(highs, lows, closes, depth=3)
        self.assertEqual(result, [])

    def test_v_shape_finds_low(self):
        """V 型 → 检测到 1 个低点"""
        n = 10
        # V 型：中间低，两边高
        highs = [100 + abs(i - 5) * 2 + 1 for i in range(n)]
        lows = [100 + abs(i - 5) * 2 for i in range(n)]
        closes = [100 + abs(i - 5) * 2 + 0.5 for i in range(n)]
        result = find_swings(highs, lows, closes, deviation=0.001, depth=2)
        # 应该有一个低点在索引 5
        low_swings = [s for s in result if s[1] == "low"]
        self.assertGreater(len(low_swings), 0)

    def test_inverted_v_finds_high(self):
        """Λ 型 → 检测到 1 个高点"""
        n = 10
        highs = [100 + (5 - abs(i - 5)) * 2 for i in range(n)]
        lows = [99 + (5 - abs(i - 5)) * 2 for i in range(n)]
        closes = [99.5 + (5 - abs(i - 5)) * 2 for i in range(n)]
        result = find_swings(highs, lows, closes, deviation=0.001, depth=2)
        high_swings = [s for s in result if s[1] == "high"]
        self.assertGreater(len(high_swings), 0)

    def test_swings_alternate(self):
        """摆动点高低交替（不会连续两个 high 或两个 low）"""
        # 构造一个有多个摆动的序列
        n = 30
        import math

        highs = [100 + math.sin(i * 0.5) * 10 + 1 for i in range(n)]
        lows = [100 + math.sin(i * 0.5) * 10 - 1 for i in range(n)]
        closes = [100 + math.sin(i * 0.5) * 10 for i in range(n)]
        result = find_swings(highs, lows, closes, deviation=0.01, depth=2)
        # 检查交替
        for i in range(1, len(result)):
            self.assertNotEqual(result[i][1], result[i - 1][1], f"连续两个 {result[i][1]} 在位置 {i}")

    def test_returns_sorted_by_index(self):
        """返回结果按索引升序排列"""
        n = 30
        import math

        highs = [100 + math.sin(i * 0.5) * 10 + 1 for i in range(n)]
        lows = [100 + math.sin(i * 0.5) * 10 - 1 for i in range(n)]
        closes = [100 + math.sin(i * 0.5) * 10 for i in range(n)]
        result = find_swings(highs, lows, closes, deviation=0.01, depth=2)
        indices = [s[0] for s in result]
        self.assertEqual(indices, sorted(indices))

    def test_deviation_filters_small_moves(self):
        """deviation 过滤掉小于阈值的波动"""
        n = 20
        import math

        # 大波动 + 小毛刺
        highs = [100 + math.sin(i * 0.3) * 15 + 1 for i in range(n)]
        lows = [100 + math.sin(i * 0.3) * 15 - 1 for i in range(n)]
        closes = [100 + math.sin(i * 0.3) * 15 for i in range(n)]
        # 大 deviation → 摆动点少
        big_dev = find_swings(highs, lows, closes, deviation=0.05, depth=2)
        # 小 deviation → 摆动点多
        small_dev = find_swings(highs, lows, closes, deviation=0.001, depth=2)
        self.assertLessEqual(len(big_dev), len(small_dev))

    def test_depth_affects_detection(self):
        """depth 越大，需要两侧更多根确认，摆动点越少"""
        n = 40
        import math

        highs = [100 + math.sin(i * 0.3) * 10 + 1 for i in range(n)]
        lows = [100 + math.sin(i * 0.3) * 10 - 1 for i in range(n)]
        closes = [100 + math.sin(i * 0.3) * 10 for i in range(n)]
        shallow = find_swings(highs, lows, closes, deviation=0.001, depth=1)
        deep = find_swings(highs, lows, closes, deviation=0.001, depth=4)
        self.assertLessEqual(len(deep), len(shallow))


# ═══════════════════════════════════════════════════════════════════════════
#  3. latest_abc
# ═══════════════════════════════════════════════════════════════════════════


class TestLatestAbc(unittest.TestCase):
    """latest_abc 最近 a-b-c 结构。"""

    def test_less_than_3_swings_returns_none(self):
        """不足 3 个摆动点 → None"""
        swings = [(0, "low", 100), (5, "high", 110)]
        self.assertIsNone(latest_abc(swings))

    def test_bullish_structure(self):
        """多头结构：low-high-low 且 c > a → direction=1"""
        swings = [
            (0, "low", 100),  # a
            (5, "high", 120),  # b
            (10, "low", 105),  # c (higher low: 105 > 100)
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, direction = result
        self.assertEqual(direction, 1)
        self.assertEqual(a[2], 100)
        self.assertEqual(b[2], 120)
        self.assertEqual(c[2], 105)

    def test_bearish_structure(self):
        """空头结构：high-low-high 且 c < a → direction=-1"""
        swings = [
            (0, "high", 120),  # a
            (5, "low", 100),  # b
            (10, "high", 110),  # c (lower high: 110 < 120)
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, direction = result
        self.assertEqual(direction, -1)
        self.assertEqual(a[2], 120)
        self.assertEqual(b[2], 100)
        self.assertEqual(c[2], 110)

    def test_direction_filter_bullish(self):
        """direction=1 → 只找多头结构"""
        # 只有空头结构
        swings = [
            (0, "high", 120),
            (5, "low", 100),
            (10, "high", 110),
        ]
        # 用 direction=1 过滤 → 找不到
        self.assertIsNone(latest_abc(swings, direction=1))

    def test_direction_filter_bearish(self):
        """direction=-1 → 只找空头结构"""
        # 只有多头结构
        swings = [
            (0, "low", 100),
            (5, "high", 120),
            (10, "low", 105),
        ]
        self.assertIsNone(latest_abc(swings, direction=-1))

    def test_no_valid_structure_returns_none(self):
        """没有合法结构 → None（c = a 不算 higher low）"""
        swings = [
            (0, "low", 100),
            (5, "high", 120),
            (10, "low", 100),  # c == a，不是 higher low
        ]
        self.assertIsNone(latest_abc(swings))

    def test_finds_latest_structure(self):
        """找最近的结构（从后往前找）"""
        swings = [
            (0, "low", 100),  # 第一个结构 a
            (5, "high", 120),  # 第一个结构 b
            (10, "low", 105),  # 第一个结构 c（valid）
            (15, "high", 130),  # 第二个结构 b
            (20, "low", 115),  # 第二个结构 c（higher low: 115 > 105）
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, direction = result
        # 应该找到第二个结构（更近期的）
        self.assertEqual(c[0], 20)
        self.assertEqual(b[0], 15)
        self.assertEqual(a[0], 10)

    def test_equal_low_not_bullish(self):
        """c 价 = a 价 → 不是 higher low，不算多头"""
        swings = [
            (0, "low", 100),
            (5, "high", 120),
            (10, "low", 100),  # 等于 a，不是 higher low
        ]
        self.assertIsNone(latest_abc(swings, direction=1))

    def test_equal_high_not_bearish(self):
        """c 价 = a 价 → 不是 lower high，不算空头"""
        swings = [
            (0, "high", 120),
            (5, "low", 100),
            (10, "high", 120),  # 等于 a，不是 lower high
        ]
        self.assertIsNone(latest_abc(swings, direction=-1))


# ═══════════════════════════════════════════════════════════════════════════
#  4. hidden_pivot
# ═══════════════════════════════════════════════════════════════════════════


class TestHiddenPivot(unittest.TestCase):
    """hidden_pivot 计算目标位与止损。"""

    def _bull_abc(self):
        a = (0, "low", 100.0)
        b = (5, "high", 120.0)
        c = (10, "low", 105.0)
        return (a, b, c, 1)

    def _bear_abc(self):
        a = (0, "high", 120.0)
        b = (5, "low", 100.0)
        c = (10, "high", 110.0)
        return (a, b, c, -1)

    def test_none_abc_returns_none(self):
        """abc=None → 返回 None"""
        self.assertIsNone(hidden_pivot(None, tick=1))

    def test_bullish_target_formula(self):
        """多头：p = b + (b-a) * 0.618"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=0.01)
        # b=120, a=100, b-a=20
        # p = 120 + 20 * 0.618 = 120 + 12.36 = 132.36
        expected_p = 120.0 + (120.0 - 100.0) * 0.618
        self.assertAlmostEqual(result["p"], expected_p, places=2)

    def test_bearish_target_formula(self):
        """空头：p = b - (a-b) * 0.618"""
        abc = self._bear_abc()
        result = hidden_pivot(abc, tick=0.01)
        # a=120, b=100, a-b=20
        # p = 100 - 20 * 0.618 = 100 - 12.36 = 87.64
        expected_p = 100.0 - (120.0 - 100.0) * 0.618
        self.assertAlmostEqual(result["p"], expected_p, places=2)

    def test_stop_equals_c(self):
        """止损位 = c 点价格"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=0.01)
        self.assertEqual(result["stop"], 105.0)  # c 点

    def test_tick_rounding(self):
        """tick 取整生效"""
        abc = self._bull_abc()
        # tick=1 → p 取整到整数
        result = hidden_pivot(abc, tick=1.0)
        # 132.36 → 132（tick=1 取整）
        self.assertEqual(result["p"], round(result["p"]))
        # a/b/c 也都被取整
        self.assertEqual(result["a"], round(result["a"]))

    def test_direction_text_bull(self):
        """多头 → direction_text = "偏多" """
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=1)
        self.assertEqual(result["direction_text"], "偏多")
        self.assertEqual(result["direction"], 1)

    def test_direction_text_bear(self):
        """空头 → direction_text = "偏空" """
        abc = self._bear_abc()
        result = hidden_pivot(abc, tick=1)
        self.assertEqual(result["direction_text"], "偏空")
        self.assertEqual(result["direction"], -1)

    def test_limit_up_makes_unreachable(self):
        """涨停板限制：p 超过涨停 → p_reachable=False"""
        abc = self._bull_abc()
        # p ≈ 132.36，涨停 130 → 不可达
        result = hidden_pivot(abc, tick=0.01, limit_up=130.0)
        self.assertFalse(result["p_reachable"])

    def test_limit_down_makes_unreachable(self):
        """跌停板限制：p 低于跌停 → p_reachable=False"""
        abc = self._bear_abc()
        # p ≈ 87.64，跌停 90 → 不可达
        result = hidden_pivot(abc, tick=0.01, limit_down=90.0)
        self.assertFalse(result["p_reachable"])

    def test_target_within_limits_reachable(self):
        """目标位在涨跌停内 → p_reachable=True"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=0.01, limit_up=150.0, limit_down=80.0)
        self.assertTrue(result["p_reachable"])

    def test_gain_pts_calculation(self):
        """gain_pts = |p - c|（tick 取整后）"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=1.0)
        expected_gain = abs(result["p"] - 105.0)
        # tick 取整后的距离
        self.assertAlmostEqual(result["gain_pts"], expected_gain, places=4)

    def test_return_fields_complete(self):
        """返回字段齐全"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=1)
        self.assertIn("direction", result)
        self.assertIn("direction_text", result)
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("c", result)
        self.assertIn("p", result)
        self.assertIn("stop", result)
        self.assertIn("p_reachable", result)
        self.assertIn("gain_pts", result)

    def test_bullish_gain_positive(self):
        """多头结构的 gain_pts 是正的（p > c）"""
        abc = self._bull_abc()
        result = hidden_pivot(abc, tick=0.01)
        self.assertGreater(result["gain_pts"], 0)
        self.assertGreater(result["p"], result["c"])

    def test_bearish_gain_positive(self):
        """空头结构的 gain_pts 也是正的（绝对值）"""
        abc = self._bear_abc()
        result = hidden_pivot(abc, tick=0.01)
        self.assertGreater(result["gain_pts"], 0)
        self.assertLess(result["p"], result["c"])


# ═══════════════════════════════════════════════════════════════════════════
#  5. 端到端：find_swings → latest_abc → hidden_pivot
# ═══════════════════════════════════════════════════════════════════════════


class TestHiddenPivotEndToEnd(unittest.TestCase):
    """端到端：摆动点检测 → ABC 结构 → 目标位计算。"""

    def test_clean_trend_produces_valid_structure(self):
        """正弦波动 → 能检测到摆动点 + ABC 结构 + 目标位"""
        import math

        n = 60
        # 正弦波，足够多周期，确保有多个摆动
        closes = [100 + math.sin(i * 0.4) * 15 for i in range(n)]
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]

        swings = find_swings(highs, lows, closes, deviation=0.005, depth=3)
        self.assertGreaterEqual(len(swings), 3)

        abc = latest_abc(swings)
        # 有足够摆动点，应该能找到 ABC 结构
        self.assertIsNotNone(abc)
        result = hidden_pivot(abc, tick=0.01)
        self.assertIsNotNone(result)
        self.assertIn(result["direction"], (1, -1))
        self.assertGreater(result["gain_pts"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  隐秘枢轴 Hidden Pivot — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
