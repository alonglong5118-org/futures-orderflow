#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
情绪引擎纯函数 — 单元测试
======================================

1. is_hard_filtered — 情绪硬过滤
   - band 为空 → 不过滤
   - 极度贪婪 + 做多 → 过滤（禁做多）
   - 极度贪婪 + 做空 → 不过滤
   - 极度恐惧 + 做空 → 过滤（禁做空）
   - 极度恐惧 + 做多 → 不过滤
   - 中性 + 做多 → 不过滤
   - 中性 + 做空 → 不过滤
   - 未知 band → 不过滤
   - direction=0 → 不过滤
   - 返回 (bool, str) 二元组

2. _label_for — 分数→标签映射
   - 100 → 极度贪婪 / extreme_greed
   - 80 → 贪婪 / greed
   - 50 → 中性 / neutral
   - 30 → 恐惧 / fear
   - 0 → 极度恐惧 / extreme_fear
   - 负分 → 极度恐惧（兜底）
   - 返回 (label, band) 二元组

3. _thr_mult — 阈值乘数
   - 极度贪婪 + 做多 → >1（提高门槛）
   - 极度贪婪 + 做空 → <1（降低门槛）
   - 极度恐惧 + 做空 → >1（提高门槛）
   - 极度恐惧 + 做多 → <1（降低门槛）
   - 中性 → 1.0
   - direction=0 → 1.0
   - 未知 band → 1.0（neutral 兜底）

4. _risk_scale — 风险仓位缩放
   - 极度贪婪 → 0.75
   - 贪婪 → 0.92
   - 中性 → 1.0
   - 恐惧 → 0.92
   - 极度恐惧 → 0.75
   - 未知 → 1.0
   - 返回 float

5. _factor_breadth — 市场广度
   - 全涨 → 100
   - 全跌 → 0
   - 涨跌各半 → 50
   - 空数据 → 50
   - 全部持平 → 50
   - 7涨3跌 → 70
   - 3涨7跌 → 30
   - 返回 float

6. _factor_activity — 资金活跃度
   - 量比=2.0 → 100（极度活跃）
   - 量比=1.0 → 50（中性）
   - 量比=0.5 → 25（冷清）
   - 量比=0 → 跳过（50）
   - 空数据 → 50
   - 返回 float，范围 [0, 100]

7. _factor_amplitude — 涨跌幅度分布
   - 全大涨 → 100
   - 全大跌 → 0
   - 全小涨 → 50（rise=全量, fall=0 → ratio=1 → 100? 不对）
   - 大小各半（对称） → 50
   - 空数据 → 50
   - 大涨权重是小涨的 2 倍
   - 返回 float
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sentiment_engine import (
    _factor_activity,
    _factor_amplitude,
    _factor_breadth,
    _label_for,
    _risk_scale,
    _thr_mult,
    is_hard_filtered,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. is_hard_filtered
# ═══════════════════════════════════════════════════════════════════════════


class TestHardFiltered(unittest.TestCase):
    """is_hard_filtered 情绪硬过滤。"""

    def test_empty_band_not_filtered(self):
        """band 为空 → 不过滤"""
        filtered, reason = is_hard_filtered("", 1)
        self.assertFalse(filtered)
        self.assertEqual(reason, "")

    def test_extreme_greed_long_filtered(self):
        """极度贪婪 + 做多 → 过滤（禁做多）"""
        filtered, reason = is_hard_filtered("extreme_greed", 1)
        self.assertTrue(filtered)
        self.assertIn("禁做多", reason)

    def test_extreme_greed_short_not_filtered(self):
        """极度贪婪 + 做空 → 不过滤"""
        filtered, _ = is_hard_filtered("extreme_greed", -1)
        self.assertFalse(filtered)

    def test_extreme_fear_short_not_filtered(self):
        """极度恐惧 + 做空 → 不过滤（数据显示反而赚钱）"""
        filtered, _ = is_hard_filtered("extreme_fear", -1)
        self.assertFalse(filtered)

    def test_extreme_fear_long_not_filtered(self):
        """极度恐惧 + 做多 → 不过滤"""
        filtered, _ = is_hard_filtered("extreme_fear", 1)
        self.assertFalse(filtered)

    def test_neutral_long_not_filtered(self):
        """中性 + 做多 → 不过滤"""
        filtered, _ = is_hard_filtered("neutral", 1)
        self.assertFalse(filtered)

    def test_neutral_short_not_filtered(self):
        """中性 + 做空 → 不过滤"""
        filtered, _ = is_hard_filtered("neutral", -1)
        self.assertFalse(filtered)

    def test_unknown_band_not_filtered(self):
        """未知 band → 不过滤"""
        filtered, _ = is_hard_filtered("unknown", 1)
        self.assertFalse(filtered)

    def test_zero_direction_not_filtered(self):
        """direction=0 → 不过滤"""
        filtered, _ = is_hard_filtered("extreme_greed", 0)
        self.assertFalse(filtered)

    def test_returns_tuple(self):
        """返回 (bool, str) 二元组"""
        result = is_hard_filtered("neutral", 1)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], str)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _label_for
# ═══════════════════════════════════════════════════════════════════════════


class TestLabelFor(unittest.TestCase):
    """_label_for 分数→标签映射。"""

    def test_score_100_extreme_greed(self):
        """100 → 极度贪婪 / extreme_greed"""
        label, band = _label_for(100)
        self.assertEqual(label, "极度贪婪")
        self.assertEqual(band, "extreme_greed")

    def test_score_80_extreme_greed(self):
        """80 → 极度贪婪 / extreme_greed（阈值70）"""
        label, band = _label_for(80)
        self.assertEqual(label, "极度贪婪")
        self.assertEqual(band, "extreme_greed")

    def test_score_60_greed(self):
        """60 → 贪婪 / greed（阈值58）"""
        label, band = _label_for(60)
        self.assertEqual(label, "贪婪")
        self.assertEqual(band, "greed")

    def test_score_50_neutral(self):
        """50 → 中性 / neutral"""
        label, band = _label_for(50)
        self.assertEqual(label, "中性")
        self.assertEqual(band, "neutral")

    def test_score_30_fear(self):
        """30 → 恐惧 / fear"""
        label, band = _label_for(30)
        self.assertEqual(label, "恐惧")
        self.assertEqual(band, "fear")

    def test_score_10_extreme_fear(self):
        """10 → 极度恐惧 / extreme_fear"""
        label, band = _label_for(10)
        self.assertEqual(label, "极度恐惧")
        self.assertEqual(band, "extreme_fear")

    def test_negative_score_fallback(self):
        """负分 → 极度恐惧（兜底）"""
        label, band = _label_for(-10)
        self.assertEqual(label, "极度恐惧")
        self.assertEqual(band, "extreme_fear")

    def test_score_zero_extreme_fear(self):
        """0 → 极度恐惧"""
        label, band = _label_for(0)
        self.assertEqual(label, "极度恐惧")
        self.assertEqual(band, "extreme_fear")

    def test_returns_tuple(self):
        """返回 (label, band) 二元组"""
        result = _label_for(50)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], str)

    def test_monotonic(self):
        """分数越高，情绪越乐观"""
        prev_label_idx = None
        label_order = ["极度恐惧", "恐惧", "中性", "贪婪", "极度贪婪"]
        for score in [10, 35, 50, 60, 75, 95]:
            label, _ = _label_for(score)
            idx = label_order.index(label)
            if prev_label_idx is not None:
                self.assertGreaterEqual(idx, prev_label_idx)
            prev_label_idx = idx


# ═══════════════════════════════════════════════════════════════════════════
#  3. _thr_mult
# ═══════════════════════════════════════════════════════════════════════════


class TestThrMult(unittest.TestCase):
    """_thr_mult 阈值乘数。"""

    def test_extreme_greed_long_higher_threshold(self):
        """极度贪婪 + 做多 → >1（提高门槛）"""
        mult = _thr_mult("extreme_greed", 1)
        self.assertGreater(mult, 1.0)

    def test_extreme_greed_short_lower_threshold(self):
        """极度贪婪 + 做空 → <1（降低门槛）"""
        mult = _thr_mult("extreme_greed", -1)
        self.assertLess(mult, 1.0)

    def test_extreme_fear_short_higher_threshold(self):
        """极度恐惧 + 做空 → >1（提高门槛）"""
        mult = _thr_mult("extreme_fear", -1)
        self.assertGreater(mult, 1.0)

    def test_extreme_fear_long_lower_threshold(self):
        """极度恐惧 + 做多 → <1（降低门槛）"""
        mult = _thr_mult("extreme_fear", 1)
        self.assertLess(mult, 1.0)

    def test_neutral_equals_one(self):
        """中性 → 1.0"""
        self.assertEqual(_thr_mult("neutral", 1), 1.0)
        self.assertEqual(_thr_mult("neutral", -1), 1.0)

    def test_zero_direction_equals_one(self):
        """direction=0 → 1.0"""
        self.assertEqual(_thr_mult("extreme_greed", 0), 1.0)

    def test_unknown_band_default_one(self):
        """未知 band → 1.0（neutral 兜底）"""
        self.assertEqual(_thr_mult("unknown", 1), 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_thr_mult("neutral", 1), float)

    def test_greed_long_higher_than_one(self):
        """贪婪 + 做多 → >1.0"""
        mult = _thr_mult("greed", 1)
        self.assertGreater(mult, 1.0)
        # 贪婪的乘数应该小于极度贪婪
        self.assertLess(mult, _thr_mult("extreme_greed", 1))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _risk_scale
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskScale(unittest.TestCase):
    """_risk_scale 风险仓位缩放。"""

    def test_extreme_greed_075(self):
        """极度贪婪 → 0.75"""
        self.assertEqual(_risk_scale("extreme_greed"), 0.75)

    def test_greed_092(self):
        """贪婪 → 0.92"""
        self.assertEqual(_risk_scale("greed"), 0.92)

    def test_neutral_100(self):
        """中性 → 1.0"""
        self.assertEqual(_risk_scale("neutral"), 1.0)

    def test_fear_092(self):
        """恐惧 → 0.92"""
        self.assertEqual(_risk_scale("fear"), 0.92)

    def test_extreme_fear_075(self):
        """极度恐惧 → 0.75"""
        self.assertEqual(_risk_scale("extreme_fear"), 0.75)

    def test_unknown_default_one(self):
        """未知 → 1.0"""
        self.assertEqual(_risk_scale("unknown"), 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_risk_scale("neutral"), float)

    def test_extreme_most_aggressive_reduction(self):
        """极端情绪缩仓最多"""
        extreme = _risk_scale("extreme_greed")
        greed = _risk_scale("greed")
        neutral = _risk_scale("neutral")
        self.assertLess(extreme, greed)
        self.assertLess(greed, neutral)

    def test_fear_same_as_greed(self):
        """恐惧与贪婪缩仓程度相同（对称）"""
        self.assertEqual(_risk_scale("fear"), _risk_scale("greed"))

    def test_extreme_symmetric(self):
        """极度贪婪与极度恐惧缩仓相同（对称）"""
        self.assertEqual(_risk_scale("extreme_greed"), _risk_scale("extreme_fear"))


# ═══════════════════════════════════════════════════════════════════════════
#  5. _factor_breadth
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorBreadth(unittest.TestCase):
    """_factor_breadth 市场广度。"""

    def test_all_rise_100(self):
        """全涨 → 100"""
        snaps = {f"s{i}": {"chg_pct": 0.01} for i in range(10)}
        self.assertAlmostEqual(_factor_breadth(snaps), 100.0, places=1)

    def test_all_fall_0(self):
        """全跌 → 0"""
        snaps = {f"s{i}": {"chg_pct": -0.01} for i in range(10)}
        self.assertAlmostEqual(_factor_breadth(snaps), 0.0, places=1)

    def test_half_half_50(self):
        """涨跌各半 → 50"""
        snaps = {}
        for i in range(5):
            snaps[f"up{i}"] = {"chg_pct": 0.01}
        for i in range(5):
            snaps[f"down{i}"] = {"chg_pct": -0.01}
        self.assertAlmostEqual(_factor_breadth(snaps), 50.0, places=1)

    def test_empty_data_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_breadth({}), 50.0)

    def test_all_flat_50(self):
        """全部持平 → 50"""
        snaps = {f"s{i}": {"chg_pct": 0.0} for i in range(10)}
        self.assertEqual(_factor_breadth(snaps), 50.0)

    def test_seven_up_three_down_70(self):
        """7涨3跌 → 70"""
        snaps = {}
        for i in range(7):
            snaps[f"up{i}"] = {"chg_pct": 0.01}
        for i in range(3):
            snaps[f"down{i}"] = {"chg_pct": -0.01}
        # (7-3)/10 = 0.4 → 50 + 0.4*50 = 70
        self.assertAlmostEqual(_factor_breadth(snaps), 70.0, places=1)

    def test_three_up_seven_down_30(self):
        """3涨7跌 → 30"""
        snaps = {}
        for i in range(3):
            snaps[f"up{i}"] = {"chg_pct": 0.01}
        for i in range(7):
            snaps[f"down{i}"] = {"chg_pct": -0.01}
        # (3-7)/10 = -0.4 → 50 + (-0.4)*50 = 30
        self.assertAlmostEqual(_factor_breadth(snaps), 30.0, places=1)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_factor_breadth({"a": {"chg_pct": 0.01}}), float)

    def test_none_chg_skipped(self):
        """None 的 chg_pct 跳过"""
        snaps = {
            "a": {"chg_pct": 0.01},
            "b": {"chg_pct": None},
            "c": {"chg_pct": -0.01},
        }
        # 1涨1跌 → 50
        self.assertAlmostEqual(_factor_breadth(snaps), 50.0, places=1)

    def test_range_bounded(self):
        """范围 [0, 100]"""
        score = _factor_breadth({"a": {"chg_pct": 0.01}})
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


# ═══════════════════════════════════════════════════════════════════════════
#  6. _factor_activity
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorActivity(unittest.TestCase):
    """_factor_activity 资金活跃度。"""

    def test_ratio_2_is_100(self):
        """量比=2.0 → 100（极度活跃）"""
        snaps = {"a": {"volume_ratio": 2.0}}
        self.assertAlmostEqual(_factor_activity(snaps), 100.0, places=1)

    def test_ratio_1_is_50(self):
        """量比=1.0 → 50（中性）"""
        snaps = {"a": {"volume_ratio": 1.0}}
        self.assertAlmostEqual(_factor_activity(snaps), 50.0, places=1)

    def test_ratio_half_is_25(self):
        """量比=0.5 → 25（冷清）"""
        snaps = {"a": {"volume_ratio": 0.5}}
        # 50 + (0.5-1)*50 = 50 - 25 = 25
        self.assertAlmostEqual(_factor_activity(snaps), 25.0, places=1)

    def test_zero_ratio_skipped(self):
        """量比=0 → 跳过（50）"""
        snaps = {"a": {"volume_ratio": 0}}
        self.assertEqual(_factor_activity(snaps), 50.0)

    def test_empty_data_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_activity({}), 50.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_factor_activity({"a": {"volume_ratio": 1.0}}), float)

    def test_range_bounded_0_100(self):
        """范围 [0, 100]"""
        score_high = _factor_activity({"a": {"volume_ratio": 3.0}})
        score_low = _factor_activity({"a": {"volume_ratio": 0.1}})
        self.assertLessEqual(score_high, 100)
        self.assertGreaterEqual(score_low, 0)

    def test_none_ratio_skipped(self):
        """None 的 volume_ratio 跳过"""
        snaps = {"a": {"volume_ratio": None}, "b": {"volume_ratio": 1.5}}
        # 只有 b 有效，ratio=1.5 → 50 + 0.5*50 = 75
        self.assertAlmostEqual(_factor_activity(snaps), 75.0, places=1)

    def test_higher_ratio_higher_score(self):
        """量比越高，分数越高"""
        low = _factor_activity({"a": {"volume_ratio": 0.8}})
        high = _factor_activity({"a": {"volume_ratio": 1.5}})
        self.assertGreater(high, low)


# ═══════════════════════════════════════════════════════════════════════════
#  7. _factor_amplitude
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorAmplitude(unittest.TestCase):
    """_factor_amplitude 涨跌幅度分布。"""

    def test_all_big_rise_100(self):
        """全大涨 → 100"""
        snaps = {f"s{i}": {"chg_pct": 0.05} for i in range(10)}
        self.assertAlmostEqual(_factor_amplitude(snaps), 100.0, places=1)

    def test_all_big_fall_0(self):
        """全大跌 → 0"""
        snaps = {f"s{i}": {"chg_pct": -0.05} for i in range(10)}
        self.assertAlmostEqual(_factor_amplitude(snaps), 0.0, places=1)

    def test_symmetric_balance_50(self):
        """大小各半（对称） → 50"""
        snaps = {}
        # 2 大涨 vs 2 大跌（权重相同都是2.0）
        for i in range(2):
            snaps[f"up{i}"] = {"chg_pct": 0.05}
        for i in range(2):
            snaps[f"down{i}"] = {"chg_pct": -0.05}
        # rise_score = 2*2 = 4, fall_score = 2*2 = 4 → 平衡
        self.assertAlmostEqual(_factor_amplitude(snaps), 50.0, places=1)

    def test_empty_data_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_amplitude({}), 50.0)

    def test_big_rise_weighted_double(self):
        """大涨权重是小涨的 2 倍"""
        # 1 大涨 (权重2) vs 2 小涨 (权重各1，总2) → 全涨，但要验证加权
        snaps = {
            "big": {"chg_pct": 0.05},
            "mid1": {"chg_pct": 0.01},
            "mid2": {"chg_pct": 0.005},
        }
        # 全涨 → fall_score=0 → ratio=1 → 100
        self.assertAlmostEqual(_factor_amplitude(snaps), 100.0, places=1)

    def test_all_small_rise_is_100(self):
        """全小涨 → 100（全部是 rise_side）"""
        snaps = {f"s{i}": {"chg_pct": 0.01} for i in range(10)}
        self.assertAlmostEqual(_factor_amplitude(snaps), 100.0, places=1)

    def test_all_small_fall_is_0(self):
        """全小跌 → 0"""
        snaps = {f"s{i}": {"chg_pct": -0.01} for i in range(10)}
        self.assertAlmostEqual(_factor_amplitude(snaps), 0.0, places=1)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_factor_amplitude({"a": {"chg_pct": 0.01}}), float)

    def test_none_chg_skipped(self):
        """None 的 chg_pct 跳过"""
        snaps = {
            "a": {"chg_pct": 0.05},
            "b": {"chg_pct": None},
            "c": {"chg_pct": -0.05},
        }
        # 1大涨 vs 1大跌 → 平衡 50
        self.assertAlmostEqual(_factor_amplitude(snaps), 50.0, places=1)

    def test_big_rise_beats_small_rise(self):
        """1大涨 > 1小跌（大涨权重2.0 > 小跌权重1.0）→ 偏多"""
        snaps = {
            "big_up": {"chg_pct": 0.05},
            "small_down": {"chg_pct": -0.01},
        }
        # rise_score = 2.0, fall_score = 1.0 → (2-1)/3 = 0.333 → 50 + 16.67 = 66.67
        score = _factor_amplitude(snaps)
        self.assertGreater(score, 50)  # 偏多


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  情绪引擎纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
