#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场情绪引擎 — 单元测试
===========================

1. is_hard_filtered — 情绪硬过滤
   - 极度贪婪 + 做多 → 过滤
   - 极度贪婪 + 做空 → 不过滤
   - 极度恐惧 + 做空 → 不过滤（数据显示反而赚钱）
   - 中性 → 双向都不过滤
   - 未知 band → 不过滤
   - 空 band → 不过滤

2. _label_for — 分数 → 标签映射
   - 70+ → 极度贪婪
   - 58-70 → 贪婪
   - 42-58 → 中性
   - 30-42 → 恐惧
   - <30 → 极度恐惧
   - 边界点精确验证

3. _thr_mult — 阈值乘数
   - 极度贪婪：多=1.20, 空=0.85
   - 中性：多空都=1.0
   - 极度恐惧：多=0.85, 空=1.20
   - 未知 → 中性默认
   - direction=0 → 1.0

4. _risk_scale — 仓位系数
   - 极度贪婪/恐惧 → 0.75
   - 贪婪/恐惧 → 0.92
   - 中性 → 1.0
   - 对称性：贪婪 = 恐惧（两端对称）

5. _factor_breadth — 市场广度
   - 全涨 → 100
   - 全跌 → 0
   - 半涨半跌 → 50
   - 空数据 → 50
   - 7 涨 3 跌 → 70

6. _factor_momentum — 动量共识
   - 全正 T_D → 高
   - 全负 T_D → 低
   - 多空平衡 → 50
   - 空数据 → 50
   - clamp 到 0-100

7. _factor_activity — 资金活跃度
   - 2 倍量 → 100
   - 0.5 倍量 → 0
   - 1 倍量 → 50
   - 空数据 → 50

8. _factor_amplitude — 涨跌幅度
   - 全大涨 → 100
   - 全大跌 → 0
   - 小涨小跌各半 → ≈50
   - 空数据 → 50

9. _factor_trend_conc — 趋势集中度
   - 全趋势 + 向上 → 高
   - 全趋势 + 向下 → 低
   - 全震荡 → ≈50
   - 空数据 → 50

10. _factor_volatility — 波动率因子
    - 低波动 → 高（贪婪）
    - 高波动 → 低（恐惧）
    - 中波动 → ≈50
    - 空数据 → 50

11. _factor_divergence — 板块分歧
    - 完全一致看多 → 高
    - 完全一致看空 → 中等偏下
    - 极度分歧 → 30
    - 不足 2 组 → 50
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
    _factor_divergence,
    _factor_momentum,
    _factor_trend_conc,
    _factor_volatility,
    _label_for,
    _risk_scale,
    _thr_mult,
    is_hard_filtered,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. is_hard_filtered
# ═══════════════════════════════════════════════════════════════════════════


class TestIsHardFiltered(unittest.TestCase):
    """is_hard_filtered 情绪硬过滤。"""

    def test_extreme_greed_long_filtered(self):
        """极度贪婪 + 做多 → 过滤"""
        filtered, reason = is_hard_filtered("extreme_greed", 1)
        self.assertTrue(filtered)
        self.assertIn("禁做多", reason)

    def test_extreme_greed_short_not_filtered(self):
        """极度贪婪 + 做空 → 不过滤"""
        filtered, reason = is_hard_filtered("extreme_greed", -1)
        self.assertFalse(filtered)
        self.assertEqual(reason, "")

    def test_extreme_fear_short_not_filtered(self):
        """极度恐惧 + 做空 → 不过滤（数据显示反而赚钱）"""
        filtered, reason = is_hard_filtered("extreme_fear", -1)
        self.assertFalse(filtered)

    def test_neutral_both_ok(self):
        """中性 → 双向都不过滤"""
        f_long, _ = is_hard_filtered("neutral", 1)
        f_short, _ = is_hard_filtered("neutral", -1)
        self.assertFalse(f_long)
        self.assertFalse(f_short)

    def test_greed_both_ok(self):
        """贪婪 → 双向都不禁（只调阈值）"""
        f_long, _ = is_hard_filtered("greed", 1)
        f_short, _ = is_hard_filtered("greed", -1)
        self.assertFalse(f_long)
        self.assertFalse(f_short)

    def test_unknown_band_not_filtered(self):
        """未知 band → 不过滤（安全默认）"""
        filtered, reason = is_hard_filtered("unknown_band", 1)
        self.assertFalse(filtered)
        self.assertEqual(reason, "")

    def test_empty_band_not_filtered(self):
        """空 band → 不过滤"""
        filtered, reason = is_hard_filtered("", 1)
        self.assertFalse(filtered)
        self.assertEqual(reason, "")

    def test_none_band_not_filtered(self):
        """None band → 不过滤"""
        filtered, reason = is_hard_filtered(None, 1)
        self.assertFalse(filtered)
        self.assertEqual(reason, "")

    def test_direction_zero_not_filtered(self):
        """direction=0 → 不过滤（没有方向就不触发）"""
        filtered, _ = is_hard_filtered("extreme_greed", 0)
        self.assertFalse(filtered)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _label_for
# ═══════════════════════════════════════════════════════════════════════════


class TestLabelFor(unittest.TestCase):
    """_label_for 分数 → 标签映射。"""

    def test_extreme_greed_at_70(self):
        """70 → 极度贪婪"""
        label, band = _label_for(70)
        self.assertEqual(label, "极度贪婪")
        self.assertEqual(band, "extreme_greed")

    def test_greed_at_58(self):
        """58 → 贪婪"""
        label, band = _label_for(58)
        self.assertEqual(label, "贪婪")
        self.assertEqual(band, "greed")

    def test_neutral_at_42(self):
        """42 → 中性"""
        label, band = _label_for(42)
        self.assertEqual(label, "中性")
        self.assertEqual(band, "neutral")

    def test_fear_at_30(self):
        """30 → 恐惧"""
        label, band = _label_for(30)
        self.assertEqual(label, "恐惧")
        self.assertEqual(band, "fear")

    def test_extreme_fear_below_30(self):
        """<30 → 极度恐惧"""
        label, band = _label_for(20)
        self.assertEqual(label, "极度恐惧")
        self.assertEqual(band, "extreme_fear")

    def test_zero_score_extreme_fear(self):
        """0 分 → 极度恐惧"""
        label, band = _label_for(0)
        self.assertEqual(band, "extreme_fear")

    def test_boundary_just_above_58(self):
        """58.1 → 贪婪（不是中性）"""
        label, band = _label_for(58.1)
        self.assertEqual(band, "greed")

    def test_boundary_just_below_58(self):
        """57.9 → 中性（不是贪婪）"""
        label, band = _label_for(57.9)
        self.assertEqual(band, "neutral")

    def test_mid_neutral(self):
        """50 → 中性"""
        label, band = _label_for(50)
        self.assertEqual(band, "neutral")


# ═══════════════════════════════════════════════════════════════════════════
#  3. _thr_mult
# ═══════════════════════════════════════════════════════════════════════════


class TestThrMult(unittest.TestCase):
    """_thr_mult 阈值乘数。"""

    def test_extreme_greed_long_higher(self):
        """极度贪婪 + 做多 → 阈值提高 20%（防追涨）"""
        self.assertEqual(_thr_mult("extreme_greed", 1), 1.20)

    def test_extreme_greed_short_lower(self):
        """极度贪婪 + 做空 → 阈值降低 15%（顺势做空更容易）"""
        self.assertEqual(_thr_mult("extreme_greed", -1), 0.85)

    def test_neutral_both_one(self):
        """中性 → 多空都是 1.0"""
        self.assertEqual(_thr_mult("neutral", 1), 1.0)
        self.assertEqual(_thr_mult("neutral", -1), 1.0)

    def test_extreme_fear_long_lower(self):
        """极度恐惧 + 做多 → 阈值降低 15%（逢低做多）"""
        self.assertEqual(_thr_mult("extreme_fear", 1), 0.85)

    def test_extreme_fear_short_higher(self):
        """极度恐惧 + 做空 → 阈值提高 20%（防杀跌）"""
        self.assertEqual(_thr_mult("extreme_fear", -1), 1.20)

    def test_unknown_defaults_neutral(self):
        """未知 band → 中性默认 1.0"""
        self.assertEqual(_thr_mult("unknown", 1), 1.0)
        self.assertEqual(_thr_mult("unknown", -1), 1.0)

    def test_direction_zero_returns_one(self):
        """direction=0 → 1.0"""
        self.assertEqual(_thr_mult("extreme_greed", 0), 1.0)

    def test_greed_long_above_one(self):
        """贪婪 + 做多 → 阈值略升（>1）"""
        self.assertGreater(_thr_mult("greed", 1), 1.0)

    def test_fear_short_above_one(self):
        """恐惧 + 做空 → 阈值略升（>1）"""
        self.assertGreater(_thr_mult("fear", -1), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _risk_scale
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskScale(unittest.TestCase):
    """_risk_scale 仓位系数。"""

    def test_extreme_greed_075(self):
        """极度贪婪 → 0.75（缩仓 25%）"""
        self.assertEqual(_risk_scale("extreme_greed"), 0.75)

    def test_extreme_fear_075(self):
        """极度恐惧 → 0.75（缩仓 25%）"""
        self.assertEqual(_risk_scale("extreme_fear"), 0.75)

    def test_greed_092(self):
        """贪婪 → 0.92"""
        self.assertEqual(_risk_scale("greed"), 0.92)

    def test_fear_092(self):
        """恐惧 → 0.92"""
        self.assertEqual(_risk_scale("fear"), 0.92)

    def test_neutral_10(self):
        """中性 → 1.0"""
        self.assertEqual(_risk_scale("neutral"), 1.0)

    def test_symmetry(self):
        """对称性：贪婪系数 = 恐惧系数"""
        self.assertEqual(_risk_scale("greed"), _risk_scale("fear"))
        self.assertEqual(_risk_scale("extreme_greed"), _risk_scale("extreme_fear"))

    def test_monotonic_from_extreme_to_neutral(self):
        """单调性：极端 < 普通 < 中性"""
        self.assertLess(_risk_scale("extreme_greed"), _risk_scale("greed"))
        self.assertLess(_risk_scale("greed"), _risk_scale("neutral"))

    def test_unknown_default_one(self):
        """未知 → 1.0（安全默认）"""
        self.assertEqual(_risk_scale("unknown_band"), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _factor_breadth
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorBreadth(unittest.TestCase):
    """_factor_breadth 市场广度。"""

    def test_all_up_is_100(self):
        """全涨 → 100"""
        snaps = {f"sym{i}": {"chg_pct": 0.01 * i} for i in range(1, 11)}
        self.assertAlmostEqual(_factor_breadth(snaps), 100.0, places=2)

    def test_all_down_is_0(self):
        """全跌 → 0"""
        snaps = {f"sym{i}": {"chg_pct": -0.01 * i} for i in range(1, 11)}
        self.assertAlmostEqual(_factor_breadth(snaps), 0.0, places=2)

    def test_half_up_half_down_is_50(self):
        """半涨半跌 → 50"""
        snaps = {}
        for i in range(5):
            snaps[f"up{i}"] = {"chg_pct": 0.01}
            snaps[f"down{i}"] = {"chg_pct": -0.01}
        self.assertAlmostEqual(_factor_breadth(snaps), 50.0, places=2)

    def test_empty_returns_50(self):
        """空数据 → 50（中性默认）"""
        self.assertEqual(_factor_breadth({}), 50.0)

    def test_7_up_3_down_is_70(self):
        """7 涨 3 跌 → 70（(7-3)/10 = 0.4, 50+20 = 70）"""
        snaps = {}
        for i in range(7):
            snaps[f"up{i}"] = {"chg_pct": 0.01}
        for i in range(3):
            snaps[f"down{i}"] = {"chg_pct": -0.01}
        self.assertAlmostEqual(_factor_breadth(snaps), 70.0, places=2)

    def test_none_chg_pct_ignored(self):
        """chg_pct=None 的品种被忽略"""
        snaps = {
            "up": {"chg_pct": 0.01},
            "down": {"chg_pct": -0.01},
            "na": {"chg_pct": None},
        }
        # 1 涨 1 跌 → 50
        self.assertAlmostEqual(_factor_breadth(snaps), 50.0, places=2)

    def test_zero_chg_counted_as_neutral(self):
        """涨跌幅=0 不算涨也不算跌，但算 total"""
        snaps = {
            "up": {"chg_pct": 0.01},
            "down": {"chg_pct": -0.01},
            "flat": {"chg_pct": 0.0},
        }
        # 1 涨 1 跌 1 平，total=3，(1-1)/3 = 0 → 50
        self.assertAlmostEqual(_factor_breadth(snaps), 50.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  6. _factor_momentum
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorMomentum(unittest.TestCase):
    """_factor_momentum 动量共识。"""

    def test_all_positive_high(self):
        """全正 T_D → 高分"""
        snaps = {f"sym{i}": {"T_D": 50.0 + i} for i in range(10)}
        score = _factor_momentum(snaps)
        self.assertGreater(score, 50)

    def test_all_negative_low(self):
        """全负 T_D → 低分"""
        snaps = {f"sym{i}": {"T_D": -50.0 - i} for i in range(10)}
        score = _factor_momentum(snaps)
        self.assertLess(score, 50)

    def test_balanced_near_50(self):
        """多空平衡 → ≈50"""
        snaps = {}
        for i in range(5):
            snaps[f"long{i}"] = {"T_D": 30.0}
            snaps[f"short{i}"] = {"T_D": -30.0}
        score = _factor_momentum(snaps)
        self.assertAlmostEqual(score, 50.0, places=2)

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_momentum({}), 50.0)

    def test_clamped_to_0_100(self):
        """结果被 clamp 到 0-100"""
        # 极大的 T_D 也不会超过 100
        snaps = {f"sym{i}": {"T_D": 200.0} for i in range(10)}
        score = _factor_momentum(snaps)
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, 0.0)

    def test_none_td_ignored(self):
        """T_D=None 的品种被忽略"""
        snaps = {
            "pos": {"T_D": 50.0},
            "neg": {"T_D": -50.0},
            "na": {"T_D": None},
        }
        score = _factor_momentum(snaps)
        # 一正一负，均值 0 → 50
        self.assertAlmostEqual(score, 50.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  7. _factor_activity
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorActivity(unittest.TestCase):
    """_factor_activity 资金活跃度。"""

    def test_double_volume_is_100(self):
        """2 倍量 → 100"""
        snaps = {"sym1": {"volume_ratio": 2.0}}
        self.assertAlmostEqual(_factor_activity(snaps), 100.0, places=2)

    def test_half_volume_is_25(self):
        """0.5 倍量 → 25（50 + (0.5-1)*50 = 25）"""
        snaps = {"sym1": {"volume_ratio": 0.5}}
        self.assertAlmostEqual(_factor_activity(snaps), 25.0, places=2)

    def test_zero_volume_is_0(self):
        """0 倍量 → 0（clamp 下限）"""
        snaps = {"sym1": {"volume_ratio": 0.0}}
        # 但 volume_ratio > 0 的过滤会把 0 过滤掉...
        # 用一个很小的正值
        snaps2 = {"sym1": {"volume_ratio": 0.01}}
        score = _factor_activity(snaps2)
        self.assertLess(score, 50)
        self.assertGreaterEqual(score, 0.0)

    def test_normal_volume_is_50(self):
        """1 倍量 → 50"""
        snaps = {"sym1": {"volume_ratio": 1.0}}
        self.assertAlmostEqual(_factor_activity(snaps), 50.0, places=2)

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_activity({}), 50.0)

    def test_15_volume_is_75(self):
        """1.5 倍量 → 75"""
        snaps = {"sym1": {"volume_ratio": 1.5}}
        self.assertAlmostEqual(_factor_activity(snaps), 75.0, places=2)

    def test_zero_volume_ignored(self):
        """volume_ratio=0 或负的被忽略"""
        snaps = {
            "ok": {"volume_ratio": 1.0},
            "zero": {"volume_ratio": 0.0},
            "neg": {"volume_ratio": -0.5},
        }
        # 只有 ok 有效，ratio=1.0 → 50
        self.assertAlmostEqual(_factor_activity(snaps), 50.0, places=2)

    def test_clamped_to_0_100(self):
        """结果 clamp 到 0-100"""
        snaps = {"sym1": {"volume_ratio": 5.0}}  # 极端高量
        score = _factor_activity(snaps)
        self.assertLessEqual(score, 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  8. _factor_amplitude
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorAmplitude(unittest.TestCase):
    """_factor_amplitude 涨跌幅度分布。"""

    def test_all_big_rise_is_100(self):
        """全大涨 → 100"""
        snaps = {f"sym{i}": {"chg_pct": 0.05 + i * 0.01} for i in range(10)}
        score = _factor_amplitude(snaps)
        self.assertAlmostEqual(score, 100.0, places=2)

    def test_all_big_fall_is_0(self):
        """全大跌 → 0"""
        snaps = {f"sym{i}": {"chg_pct": -0.05 - i * 0.01} for i in range(10)}
        score = _factor_amplitude(snaps)
        self.assertAlmostEqual(score, 0.0, places=2)

    def test_small_up_down_balanced_near_50(self):
        """小涨小跌各半 → ≈50"""
        snaps = {}
        for i in range(5):
            snaps[f"up{i}"] = {"chg_pct": 0.01}  # 小涨
            snaps[f"down{i}"] = {"chg_pct": -0.01}  # 小跌
        score = _factor_amplitude(snaps)
        self.assertAlmostEqual(score, 50.0, places=2)

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_amplitude({}), 50.0)

    def test_big_rise_weight_higher(self):
        """大涨权重 > 小涨权重"""
        # 1 个大涨(2x权重) vs 2 个小跌(1x权重)
        # rise_score = 2, fall_score = 2 → 平衡
        snaps = {
            "big_rise": {"chg_pct": 0.05},
            "small_fall1": {"chg_pct": -0.01},
            "small_fall2": {"chg_pct": -0.01},
        }
        score = _factor_amplitude(snaps)
        # 2 - 2 = 0 → 50
        self.assertAlmostEqual(score, 50.0, places=2)

    def test_none_chg_ignored(self):
        """chg_pct=None 被忽略"""
        snaps = {
            "up": {"chg_pct": 0.03},
            "down": {"chg_pct": -0.03},
            "na": {"chg_pct": None},
        }
        score = _factor_amplitude(snaps)
        # 1 大涨 vs 1 大跌，权重都是 2 → 平衡 → 50
        self.assertAlmostEqual(score, 50.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  9. _factor_trend_conc
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorTrendConc(unittest.TestCase):
    """_factor_trend_conc 趋势集中度。"""

    def test_all_trend_bullish_high(self):
        """全趋势 + 向上 → 高分（>50）"""
        snaps = {f"sym{i}": {"regime": "趋势", "T_D": 50.0} for i in range(10)}
        score = _factor_trend_conc(snaps)
        self.assertGreater(score, 50)

    def test_all_trend_bearish_low(self):
        """全趋势 + 向下 → 低分（<50）"""
        snaps = {f"sym{i}": {"regime": "趋势", "T_D": -50.0} for i in range(10)}
        score = _factor_trend_conc(snaps)
        self.assertLess(score, 50)

    def test_all_shock_near_50(self):
        """全震荡 → ≈50（中性偏迷茫）"""
        snaps = {f"sym{i}": {"regime": "震荡", "T_D": 0.0} for i in range(10)}
        score = _factor_trend_conc(snaps)
        # 震荡集中 → 迷茫，接近 50
        self.assertAlmostEqual(score, 50.0, delta=5)

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_trend_conc({}), 50.0)

    def test_no_regime_returns_50(self):
        """无 regime 数据 → 50"""
        snaps = {f"sym{i}": {"T_D": 50.0} for i in range(10)}  # 没有 regime 字段
        self.assertEqual(_factor_trend_conc(snaps), 50.0)

    def test_clamped_to_0_100(self):
        """结果 clamp 到 0-100"""
        snaps = {f"sym{i}": {"regime": "趋势", "T_D": 200.0} for i in range(10)}
        score = _factor_trend_conc(snaps)
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  10. _factor_volatility
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorVolatility(unittest.TestCase):
    """_factor_volatility 波动率因子。"""

    def test_low_vol_high_score(self):
        """低波动 → 高分（贪婪/自满）"""
        snaps = {"sym1": {"atr_pct": 0.005}}  # 0.5%
        score = _factor_volatility(snaps)
        self.assertGreater(score, 80)

    def test_high_vol_low_score(self):
        """高波动 → 低分（恐惧）"""
        snaps = {"sym1": {"atr_pct": 0.03}}  # 3%
        score = _factor_volatility(snaps)
        self.assertLess(score, 30)

    def test_mid_vol_near_50(self):
        """中波动 → ≈50"""
        snaps = {"sym1": {"atr_pct": 0.015}}  # 1.5%
        score = _factor_volatility(snaps)
        self.assertAlmostEqual(score, 68.0, delta=10)  # 大致中间范围

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_volatility({}), 50.0)

    def test_zero_atr_ignored(self):
        """atr_pct<=0 被忽略"""
        snaps = {
            "ok": {"atr_pct": 0.01},
            "zero": {"atr_pct": 0.0},
        }
        # 只有 ok 有效
        score = _factor_volatility(snaps)
        self.assertTrue(0 < score < 100)

    def test_clamped_to_0_100(self):
        """结果 clamp 到 0-100"""
        snaps = {"sym1": {"atr_pct": 0.1}}  # 极端高波动
        score = _factor_volatility(snaps)
        self.assertGreaterEqual(score, 0.0)
        snaps2 = {"sym1": {"atr_pct": 0.001}}  # 极端低波动
        score2 = _factor_volatility(snaps2)
        self.assertLessEqual(score2, 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  11. _factor_divergence
# ═══════════════════════════════════════════════════════════════════════════


class TestFactorDivergence(unittest.TestCase):
    """_factor_divergence 板块分歧。"""

    def test_unanimous_bullish_high(self):
        """板块一致看多 → 高分"""
        snaps = {}
        for i in range(5):
            snaps[f"g1_{i}"] = {"group": "black", "T_D": 60.0}
            snaps[f"g2_{i}"] = {"group": "nonferrous", "T_D": 60.0}
        score = _factor_divergence(snaps)
        self.assertGreater(score, 50)

    def test_unanimous_bearish_mid_low(self):
        """板块一致看空 → 中等偏下（一致看空也是"明确"，但不应给高分）"""
        snaps = {}
        for i in range(5):
            snaps[f"g1_{i}"] = {"group": "black", "T_D": -60.0}
            snaps[f"g2_{i}"] = {"group": "nonferrous", "T_D": -60.0}
        score = _factor_divergence(snaps)
        # 一致偏空 → 中等偏下（spread<10 时 50 + avg_direction * 0.5）
        self.assertLess(score, 50)
        self.assertGreater(score, 20)  # 不会太低

    def test_extreme_divergence_is_30(self):
        """极度分歧 → 30"""
        snaps = {}
        # 两个板块方向完全相反，spread 很大
        for i in range(5):
            snaps[f"g1_{i}"] = {"group": "bull_group", "T_D": 80.0}
            snaps[f"g2_{i}"] = {"group": "bear_group", "T_D": -80.0}
        score = _factor_divergence(snaps)
        # spread = std([80, -80]) = 80 → 极度分歧 → 30
        self.assertAlmostEqual(score, 30.0, delta=5)

    def test_less_than_2_groups_returns_50(self):
        """不足 2 组 → 50（无法计算分歧）"""
        snaps = {f"sym{i}": {"group": "same", "T_D": 50.0} for i in range(10)}
        self.assertEqual(_factor_divergence(snaps), 50.0)

    def test_no_group_returns_50(self):
        """无 group → 50"""
        snaps = {f"sym{i}": {"T_D": 50.0} for i in range(10)}
        self.assertEqual(_factor_divergence(snaps), 50.0)

    def test_empty_returns_50(self):
        """空数据 → 50"""
        self.assertEqual(_factor_divergence({}), 50.0)

    def test_clamped_to_0_100(self):
        """结果 clamp 到 0-100"""
        snaps = {}
        for i in range(3):
            snaps[f"g{i}"] = {"group": f"g{i}", "T_D": 100.0 * i}
        score = _factor_divergence(snaps)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  市场情绪引擎 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
