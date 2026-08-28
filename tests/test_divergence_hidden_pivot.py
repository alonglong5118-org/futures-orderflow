#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向源分歧 + 隐藏枢轴 — 单元测试
======================================

1. divergence — 单笔方向一致性
   - 同号正 → True
   - 同号负 → True
   - 异号 → False
   - T_D=0 → None
   - T_5m=0 → None
   - 都为 0 → None
   - 正小数 → 按正号算
   - 负小数 → 按负号算
   - 返回 bool 或 None

2. DivergenceTracker — 滚动分歧率追踪
   - 初始空 → 无数据，level=OK
   - 全部同号 → 分歧率=0，level=OK
   - 全部异号 → 分歧率=1，level=HIGH
   - 部分异号 → 分歧率介于 0 和 1
   - 滑动窗口裁剪（超过 window 弹出最旧）
   - SA 样本独立追踪
   - 非 SA 不影响 sa_samples
   - summary 返回完整字段（6个）
   - level 三档：OK/WARN/HIGH
   - 分歧率保留 3 位小数
   - 无样本时 divergence_rate=None

3. round_tick — tick 取整
   - 正好整除 → 原值
   - 四舍五入
   - 小数 tick 精度
   - 整数 tick
   - 零价格 → 0
   - 保留 6 位小数

4. latest_abc — 最近 a-b-c 结构
   - 不足 3 个 swing → None
   - 多头结构（low-high-low，c>a）→ dir=1
   - 空头结构（high-low-high，c<a）→ dir=-1
   - 多头但 c<=a → 不成立（找前一个）
   - 空头但 c>=a → 不成立（找前一个）
   - direction=1 只找多头
   - direction=-1 只找空头
   - 返回 (a, b, c, dir) 四元组
   - 取最近的合法结构

5. hidden_pivot — 隐藏枢轴目标位
   - abc=None → None
   - 多头结构 → p = b + (b-a)*0.618
   - 空头结构 → p = b - (a-b)*0.618
   - stop = c 价
   - direction_text = 偏多/偏空
   - 超涨停 → p_reachable=False
   - 超跌停 → p_reachable=False
   - 未到停板 → p_reachable=True
   - gain_pts = abs(p - c) / tick × tick
   - 返回 8 个字段
   - a/b/c/p/stop 都按 tick 取整
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from direction_source_monitor import DivergenceTracker, divergence
from hidden_pivot import hidden_pivot, latest_abc, round_tick

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
        """异号 → False"""
        self.assertFalse(divergence(50, -30))

    def test_neg_pos_disagree(self):
        """负正 → False"""
        self.assertFalse(divergence(-50, 30))

    def test_T_D_zero_none(self):
        """T_D=0 → None"""
        self.assertIsNone(divergence(0, 30))

    def test_T_5m_zero_none(self):
        """T_5m=0 → None"""
        self.assertIsNone(divergence(50, 0))

    def test_both_zero_none(self):
        """都为 0 → None"""
        self.assertIsNone(divergence(0, 0))

    def test_small_positive_counts_positive(self):
        """正小数 → 按正号算"""
        self.assertTrue(divergence(0.1, 10))

    def test_small_negative_counts_negative(self):
        """负小数 → 按负号算"""
        self.assertTrue(divergence(-0.1, -10))

    def test_returns_bool_or_none(self):
        """返回 bool 或 None"""
        self.assertIsInstance(divergence(1, 1), bool)
        self.assertIsNone(divergence(0, 1))


# ═══════════════════════════════════════════════════════════════════════════
#  2. DivergenceTracker
# ═══════════════════════════════════════════════════════════════════════════

class TestDivergenceTracker(unittest.TestCase):
    """DivergenceTracker 滚动分歧率追踪。"""

    def test_initial_empty(self):
        """初始空 → 无数据，level=OK"""
        t = DivergenceTracker()
        s = t.summary()
        self.assertIsNone(s["divergence_rate"])
        self.assertEqual(s["level"], "OK")
        self.assertEqual(s["n"], 0)

    def test_all_agree_zero_rate(self):
        """全部同号 → 分歧率=0，level=OK"""
        t = DivergenceTracker()
        for i in range(10):
            t.update("rb", 50, 30)
        s = t.summary()
        self.assertEqual(s["divergence_rate"], 0.0)
        self.assertEqual(s["level"], "OK")
        self.assertEqual(s["n"], 10)

    def test_all_disagree_full_rate(self):
        """全部异号 → 分歧率=1，level=HIGH"""
        t = DivergenceTracker()
        for i in range(10):
            t.update("rb", 50, -30)
        s = t.summary()
        self.assertEqual(s["divergence_rate"], 1.0)
        self.assertEqual(s["level"], "HIGH")

    def test_partial_disagree_mid_rate(self):
        """部分异号 → 分歧率介于 0 和 1"""
        t = DivergenceTracker()
        # 6 同号 + 4 异号 → 分歧率=0.4
        for i in range(6):
            t.update("rb", 50, 30)
        for i in range(4):
            t.update("rb", 50, -30)
        s = t.summary()
        self.assertAlmostEqual(s["divergence_rate"], 0.4, places=3)
        self.assertEqual(s["level"], "OK")  # 0.4 < 0.55

    def test_warn_level(self):
        """分歧率 >= 0.55 → WARN"""
        t = DivergenceTracker()
        # 4 同 + 6 异 → 分歧率=0.6 → WARN
        for i in range(4):
            t.update("rb", 50, 30)
        for i in range(6):
            t.update("rb", 50, -30)
        s = t.summary()
        self.assertEqual(s["level"], "WARN")

    def test_high_level(self):
        """分歧率 >= 0.65 → HIGH"""
        t = DivergenceTracker()
        # 3 同 + 7 异 → 分歧率=0.7 → HIGH
        for i in range(3):
            t.update("rb", 50, 30)
        for i in range(7):
            t.update("rb", 50, -30)
        s = t.summary()
        self.assertEqual(s["level"], "HIGH")

    def test_sliding_window(self):
        """滑动窗口裁剪（超过 window 弹出最旧）"""
        t = DivergenceTracker(window=10)
        # 先 10 个同号（分歧率 0）
        for i in range(10):
            t.update("rb", 50, 30)
        self.assertEqual(t.summary()["divergence_rate"], 0.0)
        # 再加 10 个异号 → 窗口内 10 个全是异号
        for i in range(10):
            t.update("rb", 50, -30)
        self.assertEqual(t.summary()["n"], 10)
        self.assertEqual(t.summary()["divergence_rate"], 1.0)

    def test_sa_samples_tracked_separately(self):
        """SA 样本独立追踪"""
        t = DivergenceTracker()
        t.update("SA", 50, -30)  # SA 异号
        t.update("rb", 50, 30)   # 非 SA 同号
        s = t.summary()
        self.assertEqual(s["n"], 2)
        # SA 只有 1 个样本，全异号 → 1.0
        self.assertEqual(s["sa_divergence_rate"], 1.0)

    def test_non_sa_not_in_sa_samples(self):
        """非 SA 不影响 sa_samples"""
        t = DivergenceTracker()
        for i in range(5):
            t.update("rb", 50, -30)  # 都是非 SA
        s = t.summary()
        self.assertIsNone(s["sa_divergence_rate"])

    def test_summary_fields_complete(self):
        """summary 返回完整字段（6个）"""
        t = DivergenceTracker()
        t.update("rb", 50, 30)
        s = t.summary()
        for key in ("divergence_rate", "baseline", "level",
                     "sa_divergence_rate", "sa_sensitive", "n"):
            self.assertIn(key, s, f"missing key: {key}")

    def test_rate_three_decimals(self):
        """分歧率保留 3 位小数"""
        t = DivergenceTracker()
        for i in range(7):
            t.update("rb", 50, 30)
        for i in range(3):
            t.update("rb", 50, -30)
        s = t.summary()
        self.assertEqual(s["divergence_rate"], round(s["divergence_rate"], 3))

    def test_none_divergence_skipped(self):
        """divergence 返回 None 的样本不记录"""
        t = DivergenceTracker()
        t.update("rb", 0, 50)   # T_D=0 → None
        t.update("rb", 50, 0)   # T_5m=0 → None
        s = t.summary()
        self.assertEqual(s["n"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. round_tick
# ═══════════════════════════════════════════════════════════════════════════

class TestRoundTick(unittest.TestCase):
    """round_tick tick 取整。"""

    def test_exact_multiple_unchanged(self):
        """正好整除 → 原值"""
        self.assertEqual(round_tick(10.0, 1.0), 10.0)

    def test_rounds_up(self):
        """四舍五入向上"""
        self.assertEqual(round_tick(10.6, 1.0), 11.0)

    def test_rounds_down(self):
        """四舍五入向下"""
        self.assertEqual(round_tick(10.4, 1.0), 10.0)

    def test_small_tick(self):
        """小数 tick 精度（0.1）"""
        self.assertAlmostEqual(round_tick(10.36, 0.1), 10.4, places=6)

    def test_fractional_tick(self):
        """分数 tick（0.2）"""
        self.assertAlmostEqual(round_tick(10.25, 0.2), 10.2, places=6)

    def test_integer_tick(self):
        """整数 tick"""
        self.assertEqual(round_tick(1234, 10), 1230.0)

    def test_zero_price(self):
        """零价格 → 0"""
        self.assertEqual(round_tick(0, 1.0), 0.0)

    def test_six_decimal_precision(self):
        """保留 6 位小数"""
        result = round_tick(0.0001234, 0.00001)
        # round 到 6 位 → 0.00012
        self.assertEqual(result, round(result, 6))

    def test_returns_numeric(self):
        """返回数值类型（int 或 float）"""
        self.assertTrue(isinstance(round_tick(10, 1), (int, float)))


# ═══════════════════════════════════════════════════════════════════════════
#  4. latest_abc
# ═══════════════════════════════════════════════════════════════════════════

class TestLatestAbc(unittest.TestCase):
    """latest_abc 最近 a-b-c 结构。"""

    def test_fewer_than_three_swings_none(self):
        """不足 3 个 swing → None"""
        self.assertIsNone(latest_abc([]))
        self.assertIsNone(latest_abc([(0, "low", 100)]))
        self.assertIsNone(latest_abc([(0, "low", 100), (1, "high", 110)]))

    def test_bullish_structure(self):
        """多头结构（low-high-low，c>a）→ dir=1"""
        swings = [
            (0, "low", 100),
            (1, "high", 120),
            (2, "low", 105),  # c > a → 多头
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, d = result
        self.assertEqual(d, 1)
        self.assertEqual(a[2], 100)
        self.assertEqual(b[2], 120)
        self.assertEqual(c[2], 105)

    def test_bearish_structure(self):
        """空头结构（high-low-high，c<a）→ dir=-1"""
        swings = [
            (0, "high", 120),
            (1, "low", 100),
            (2, "high", 115),  # c < a → 空头
        ]
        result = latest_abc(swings)
        self.assertIsNotNone(result)
        a, b, c, d = result
        self.assertEqual(d, -1)
        self.assertEqual(a[2], 120)
        self.assertEqual(b[2], 100)
        self.assertEqual(c[2], 115)

    def test_bullish_c_below_a_not_valid(self):
        """多头但 c<=a → 不成立（找前一个）"""
        swings = [
            (0, "low", 100),
            (1, "high", 120),
            (2, "low", 95),   # c < a → 不构成多头
        ]
        # 只有 3 个，且不构成 → None
        self.assertIsNone(latest_abc(swings))

    def test_bearish_c_above_a_not_valid(self):
        """空头但 c>=a → 不成立"""
        swings = [
            (0, "high", 120),
            (1, "low", 100),
            (2, "high", 125),  # c > a → 不构成空头
        ]
        self.assertIsNone(latest_abc(swings))

    def test_direction_filter_bullish_only(self):
        """direction=1 只找多头"""
        swings = [
            (0, "high", 120),
            (1, "low", 100),
            (2, "high", 115),  # 空头结构
        ]
        # 有空头结构，但 direction=1 只找多头 → None
        self.assertIsNone(latest_abc(swings, direction=1))

    def test_direction_filter_bearish_only(self):
        """direction=-1 只找空头"""
        swings = [
            (0, "low", 100),
            (1, "high", 120),
            (2, "low", 105),  # 多头结构
        ]
        self.assertIsNone(latest_abc(swings, direction=-1))

    def test_returns_four_tuple(self):
        """返回 (a, b, c, dir) 四元组"""
        swings = [
            (0, "low", 100),
            (1, "high", 120),
            (2, "low", 105),
        ]
        result = latest_abc(swings)
        self.assertEqual(len(result), 4)
        self.assertIsInstance(result[3], int)

    def test_picks_latest_valid(self):
        """取最近的合法结构"""
        swings = [
            (0, "low", 100),
            (1, "high", 120),
            (2, "low", 105),   # 第1个多头
            (3, "high", 130),
            (4, "low", 115),   # 第2个多头（更近）
        ]
        result = latest_abc(swings)
        a, b, c, d = result
        self.assertEqual(d, 1)
        self.assertEqual(a[2], 105)  # 第2个的 a 是第2个 swing
        self.assertEqual(b[2], 130)
        self.assertEqual(c[2], 115)


# ═══════════════════════════════════════════════════════════════════════════
#  5. hidden_pivot
# ═══════════════════════════════════════════════════════════════════════════

class TestHiddenPivot(unittest.TestCase):
    """hidden_pivot 隐藏枢轴目标位。"""

    def _bull_abc(self):
        a = (0, "low", 100.0)
        b = (1, "high", 120.0)
        c = (2, "low", 105.0)
        return (a, b, c, 1)

    def _bear_abc(self):
        a = (0, "high", 120.0)
        b = (1, "low", 100.0)
        c = (2, "high", 115.0)
        return (a, b, c, -1)

    def test_none_abc_returns_none(self):
        """abc=None → None"""
        self.assertIsNone(hidden_pivot(None, 1.0))

    def test_bull_p_calculation(self):
        """多头结构 → p = b + (b-a)*0.618"""
        # b=120, a=100 → b-a=20 → 20*0.618=12.36 → p=132.36
        result = hidden_pivot(self._bull_abc(), 0.01)
        self.assertAlmostEqual(result["p"], 132.36, places=2)

    def test_bear_p_calculation(self):
        """空头结构 → p = b - (a-b)*0.618"""
        # a=120, b=100 → a-b=20 → 20*0.618=12.36 → p=100-12.36=87.64
        result = hidden_pivot(self._bear_abc(), 0.01)
        self.assertAlmostEqual(result["p"], 87.64, places=2)

    def test_stop_is_c_price(self):
        """stop = c 价"""
        result = hidden_pivot(self._bull_abc(), 0.01)
        self.assertEqual(result["stop"], 105.0)

    def test_direction_text_bull(self):
        """多头 → 偏多"""
        result = hidden_pivot(self._bull_abc(), 1.0)
        self.assertEqual(result["direction_text"], "偏多")

    def test_direction_text_bear(self):
        """空头 → 偏空"""
        result = hidden_pivot(self._bear_abc(), 1.0)
        self.assertEqual(result["direction_text"], "偏空")

    def test_above_limit_up_not_reachable(self):
        """超涨停 → p_reachable=False"""
        result = hidden_pivot(self._bull_abc(), 0.01, limit_up=130.0)
        # p=132.36 > limit_up=130 → 不可达
        self.assertFalse(result["p_reachable"])

    def test_below_limit_down_not_reachable(self):
        """超跌停 → p_reachable=False"""
        result = hidden_pivot(self._bear_abc(), 0.01, limit_down=90.0)
        # p=87.64 < limit_down=90 → 不可达
        self.assertFalse(result["p_reachable"])

    def test_within_limits_reachable(self):
        """未到停板 → p_reachable=True"""
        result = hidden_pivot(self._bull_abc(), 0.01, limit_up=200.0)
        self.assertTrue(result["p_reachable"])

    def test_gain_pts_calculation(self):
        """gain_pts = abs(p - c) 按 tick 取整"""
        # 多头：p=132.36, c=105 → gain=27.36
        result = hidden_pivot(self._bull_abc(), 0.01)
        self.assertAlmostEqual(result["gain_pts"], 27.36, places=2)

    def test_return_fields_complete(self):
        """返回 8 个字段"""
        result = hidden_pivot(self._bull_abc(), 1.0)
        for key in ("direction", "direction_text", "a", "b", "c",
                     "p", "stop", "p_reachable", "gain_pts"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_all_prices_tick_rounded(self):
        """a/b/c/p/stop 都按 tick 取整"""
        # tick=1 → 全部取整
        result = hidden_pivot(self._bull_abc(), 1.0)
        for key in ("a", "b", "c", "p", "stop"):
            self.assertEqual(result[key], round_tick(result[key], 1.0))

    def test_no_limit_default_reachable(self):
        """无停板限制 → p_reachable=True"""
        result = hidden_pivot(self._bull_abc(), 0.01)
        self.assertTrue(result["p_reachable"])
        result2 = hidden_pivot(self._bear_abc(), 0.01)
        self.assertTrue(result2["p_reachable"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  方向源分歧 + 隐藏枢轴 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
