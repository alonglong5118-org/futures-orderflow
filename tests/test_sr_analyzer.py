#!/usr/bin/env python3
"""
SR 位分析 — 单元测试
=========================

测试 sr_analyzer 中的核心纯函数：
  1. signal_quality_boost  — 逆向位方向感知信号质量调整
  2. _cluster_levels        — 极值聚类（相近价位合并）
  3. _score_strength        — 结构位强度评分
  4. _classify              — 支撑/压力分类
  5. analyze                — 完整 SR 分析（集成）
  6. adjust_exit_plan       — SR 位微调止盈止损

历史覆盖：
  - 逆向位 v2：方向感知过滤（做多看压力、做空看支撑）
  - 极近位突破区（<0.3%）vs 危险区（0.3%~1.0%）vs 安全区（>=1.0%）
  - 危险区 T 阈值 ×1.3（惩罚）
  - 双面验证加分（既是支撑又是压力的结构位更强）
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sr_analyzer import (
    HOSTILE_DANGER_PENALTY,
    _classify,
    _cluster_levels,
    _score_strength,
    adjust_exit_plan,
    analyze,
    signal_quality_boost,
)


def _make_sr_result(nearest_support=None, nearest_resistance=None, levels=None):
    """构造一个简化的 sr_result dict。"""
    if levels is None:
        # 确保 levels 非空（signal_quality_boost / adjust_exit_plan 都检查 levels）
        levels = [nearest_support] if nearest_support else []
        if nearest_resistance:
            levels.append(nearest_resistance)
    return {
        "levels": levels,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "at_support": False,
        "at_resistance": False,
        "zone": "far",
        "current_price": 100.0,
    }


def _make_level(price, distance_pct=1.0, strength=50, role=None):
    return {
        "price": price,
        "distance_pct": distance_pct,
        "strength": strength,
        "touches": 3,
        "dual_sided": False,
        "role": role,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  1. 逆向位信号质量调整
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalQualityBoost(unittest.TestCase):
    """signal_quality_boost — 逆向位方向感知过滤。"""

    def test_long_safe_zone_no_penalty(self):
        """做多 + 离压力位很远（>1.0%）→ 安全区，无惩罚"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(105, distance_pct=5.0),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_short_safe_zone_no_penalty(self):
        """做空 + 离支撑位很远 → 安全区，无惩罚"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(105, distance_pct=5.0),
        )
        boost, reason = signal_quality_boost(sr, direction=-1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_long_danger_zone_penalty(self):
        """做多 + 压力位在危险区（0.3%~1.0%）→ 惩罚 -0.30"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(100.5, distance_pct=0.5),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertAlmostEqual(boost, -HOSTILE_DANGER_PENALTY, places=4)
        self.assertIn("危险区", reason)
        self.assertIn("压力", reason)

    def test_short_danger_zone_penalty(self):
        """做空 + 支撑位在危险区 → 惩罚 -0.30"""
        sr = _make_sr_result(
            nearest_support=_make_level(99.5, distance_pct=0.5),
            nearest_resistance=_make_level(105, distance_pct=5.0),
        )
        boost, reason = signal_quality_boost(sr, direction=-1)
        self.assertAlmostEqual(boost, -HOSTILE_DANGER_PENALTY, places=4)
        self.assertIn("危险区", reason)
        self.assertIn("支撑", reason)

    def test_long_very_tight_breakout_zone(self):
        """做多 + 压力位极近（<0.3%）→ 极近位突破区，不惩罚"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(100.2, distance_pct=0.2),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0)  # HOSTILE_TIGHT_BOOST = 0
        self.assertIn("突破区", reason)

    def test_short_very_tight_breakout_zone(self):
        """做空 + 支撑位极近 → 极近位突破区，不惩罚"""
        sr = _make_sr_result(
            nearest_support=_make_level(99.8, distance_pct=0.2),
            nearest_resistance=_make_level(105, distance_pct=5.0),
        )
        boost, reason = signal_quality_boost(sr, direction=-1)
        self.assertEqual(boost, 0.0)
        self.assertIn("突破区", reason)

    def test_no_sr_data_no_penalty(self):
        """无 SR 数据 → 不惩罚（安全通过）"""
        boost, reason = signal_quality_boost(None, direction=1)
        self.assertEqual(boost, 0.0)
        self.assertEqual(reason, "")

    def test_empty_levels_no_penalty(self):
        """空 levels → 不惩罚"""
        sr = _make_sr_result(levels=[])
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0)

    def test_no_resistance_long_safe(self):
        """做多但没有压力位 → 视作安全（99% 距离）"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=None,
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_no_support_short_safe(self):
        """做空但没有支撑位 → 视作安全"""
        sr = _make_sr_result(
            nearest_support=None,
            nearest_resistance=_make_level(105, distance_pct=5.0),
        )
        boost, reason = signal_quality_boost(sr, direction=-1)
        self.assertEqual(boost, 0.0)

    def test_danger_zone_exactly_at_boundary(self):
        """恰好 = 危险区上限（1.0%）→ 危险区？不，< 才是，等于 → 安全区"""
        # hostile_frac = 0.01 < 0.01? 不 → 安全区
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(101.0, distance_pct=1.0),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0)
        self.assertIn("安全", reason)

    def test_danger_zone_just_below_boundary(self):
        """略低于危险区上限（0.99%）→ 危险区"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(100.99, distance_pct=0.99),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertAlmostEqual(boost, -HOSTILE_DANGER_PENALTY, places=4)

    def test_tight_zone_exactly_at_boundary(self):
        """恰好 = 极近位阈值（0.3%）→ 不满足 <0.3% → 危险区"""
        sr = _make_sr_result(
            nearest_support=_make_level(95, distance_pct=5.0),
            nearest_resistance=_make_level(100.3, distance_pct=0.3),
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        # 0.3% < 0.3%? 不 → 进入危险区判断
        # 0.3% < 1.0%? 是 → 危险区
        self.assertAlmostEqual(boost, -HOSTILE_DANGER_PENALTY, places=4)
        self.assertIn("危险区", reason)

    def test_direction_aware_checks_right_side(self):
        """方向感知：做多只关心压力位，不关心支撑位远近"""
        # 支撑位极近（危险区），但做多只看压力位 → 安全
        sr = _make_sr_result(
            nearest_support=_make_level(99.5, distance_pct=0.5),  # 很近（危险区距离）
            nearest_resistance=_make_level(110, distance_pct=10.0),  # 很远
        )
        boost, reason = signal_quality_boost(sr, direction=1)
        self.assertEqual(boost, 0.0, "做多时只关心压力位，支撑位近不应该惩罚")
        self.assertIn("压力", reason)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 极值聚类
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterLevels(unittest.TestCase):
    """_cluster_levels — 相近极值合并为结构位。"""

    def test_single_extremum_one_cluster(self):
        """单个极值 → 1 个聚类"""
        extrema = [(0, 100.0, "high", 1000)]
        clusters = _cluster_levels(extrema)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["price"], 100.0)
        self.assertEqual(clusters[0]["touches"], 1)

    def test_close_prices_merge(self):
        """相近价格（<0.3%）→ 合并"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 100.2, "high", 800),  # 0.2% < 0.3% → 合并
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.003)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["touches"], 2)

    def test_far_prices_dont_merge(self):
        """相差较远（>0.3%）→ 不合并"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 101.0, "high", 800),  # 1.0% > 0.3% → 不合并
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.003)
        self.assertEqual(len(clusters), 2)

    def test_three_close_levels_merge(self):
        """三个相近价格 → 合并为 1 个"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 100.1, "low", 800),
            (2, 100.2, "high", 1200),
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.003)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["touches"], 3)

    def test_dual_sided_detection(self):
        """既有 high 又有 low → dual_sided=True"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 100.1, "low", 800),
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.005)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0]["dual_sided"])

    def test_only_high_not_dual(self):
        """只有 high → dual_sided=False"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 100.1, "high", 800),
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.005)
        self.assertFalse(clusters[0]["dual_sided"])

    def test_empty_input_empty_output(self):
        """空输入 → 空输出"""
        self.assertEqual(_cluster_levels([]), [])

    def test_volume_weighted_price(self):
        """聚类价格是成交量加权平均"""
        # 价 100 量 1000 + 价 102 量 3000
        # 加权平均 = (100*1000 + 102*3000) / 4000 = (100000 + 306000) / 4000 = 406000/4000 = 101.5
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 102.0, "high", 3000),
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.03)
        self.assertEqual(len(clusters), 1)
        self.assertAlmostEqual(clusters[0]["price"], 101.5, places=2)

    def test_multiple_clusters_separated(self):
        """两组相距较远的极值 → 2 个聚类"""
        extrema = [
            (0, 100.0, "high", 1000),
            (1, 100.1, "high", 800),
            (2, 110.0, "low", 500),  # 远
            (3, 110.2, "low", 600),
        ]
        clusters = _cluster_levels(extrema, cluster_pct=0.005)
        self.assertEqual(len(clusters), 2)
        # 按价格排序
        self.assertLess(clusters[0]["price"], clusters[1]["price"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. 强度评分
# ═══════════════════════════════════════════════════════════════════════════


class TestScoreStrength(unittest.TestCase):
    """_score_strength — 结构位强度评分。"""

    def _make_df(self, avg_volume=1000, n=30):
        """构造简单的 DataFrame 用于评分测试。"""
        dates = pd.date_range("2026-01-01", periods=n, freq="D")
        df = pd.DataFrame(
            {
                "high": [100 + i * 0.1 for i in range(n)],
                "low": [99 + i * 0.1 for i in range(n)],
                "close": [99.5 + i * 0.1 for i in range(n)],
                "volume": [avg_volume] * n,
            },
            index=dates,
        )
        return df

    def test_two_touches_base_score(self):
        """2 次触及 → 基础分 = 20 + (2-1)*25 = 45"""
        levels = [
            {
                "price": 100,
                "touches": 2,
                "dual_sided": False,
                "avg_volume": 1000,
                "last_date": pd.Timestamp("2026-01-28"),
            }
        ]
        df = self._make_df(avg_volume=1000, n=30)
        result = _score_strength(levels, df, 100)
        # touch_score = 20 + 1*25 = 45
        # vol_ratio = 1.0, vol_score = min(20, 1.0/1.2*10) = min(20, 8.33) = 8.33
        # time_score = 10（2 天前，<20）
        # dual_bonus = 0
        # total = 45 + 8.33 + 10 + 0 = 63.33
        self.assertAlmostEqual(result[0]["strength"], 63.3, places=1)

    def test_more_touches_higher_score(self):
        """触及次数越多，分数越高"""
        df = self._make_df()
        levels_2 = [
            {
                "price": 100,
                "touches": 2,
                "dual_sided": False,
                "avg_volume": 1000,
                "last_date": pd.Timestamp("2026-01-28"),
            }
        ]
        levels_5 = [
            {
                "price": 100,
                "touches": 5,
                "dual_sided": False,
                "avg_volume": 1000,
                "last_date": pd.Timestamp("2026-01-28"),
            }
        ]
        s2 = _score_strength(list(levels_2), df, 100)[0]["strength"]
        s5 = _score_strength(list(levels_5), df, 100)[0]["strength"]
        self.assertGreater(s5, s2)

    def test_dual_sided_bonus(self):
        """双面验证 → 加 5 分"""
        df = self._make_df()
        date = pd.Timestamp("2026-01-28")
        lv_single = [{"price": 100, "touches": 3, "dual_sided": False, "avg_volume": 1000, "last_date": date}]
        lv_dual = [{"price": 100, "touches": 3, "dual_sided": True, "avg_volume": 1000, "last_date": date}]
        s_single = _score_strength(list(lv_single), df, 100)[0]["strength"]
        s_dual = _score_strength(list(lv_dual), df, 100)[0]["strength"]
        self.assertAlmostEqual(s_dual - s_single, 5.0, places=1)

    def test_high_volume_higher_score(self):
        """高成交量确认 → 分数更高"""
        df = self._make_df(avg_volume=1000)
        date = pd.Timestamp("2026-01-28")
        lv_low_vol = [{"price": 100, "touches": 3, "dual_sided": False, "avg_volume": 500, "last_date": date}]
        lv_high_vol = [{"price": 100, "touches": 3, "dual_sided": False, "avg_volume": 2000, "last_date": date}]
        s_low = _score_strength(list(lv_low_vol), df, 100)[0]["strength"]
        s_high = _score_strength(list(lv_high_vol), df, 100)[0]["strength"]
        self.assertGreater(s_high, s_low)

    def test_old_level_lower_score(self):
        """越旧的结构位，分数越低（时间衰减）"""
        df = self._make_df(n=100)
        recent = pd.Timestamp("2026-04-10")  # 离最后一天近
        old = pd.Timestamp("2026-01-15")  # 很久以前
        lv_recent = [{"price": 100, "touches": 3, "dual_sided": False, "avg_volume": 1000, "last_date": recent}]
        lv_old = [{"price": 100, "touches": 3, "dual_sided": False, "avg_volume": 1000, "last_date": old}]
        s_recent = _score_strength(list(lv_recent), df, 100)[0]["strength"]
        s_old = _score_strength(list(lv_old), df, 100)[0]["strength"]
        self.assertGreater(s_recent, s_old, "时间衰减：越旧的结构位分数应该越低")

    def test_score_capped_at_100(self):
        """分数封顶 100"""
        df = self._make_df(avg_volume=1000)
        # 很多触及 + 高量 + 新 + 双面 → 应该封顶
        lv = [
            {
                "price": 100,
                "touches": 20,
                "dual_sided": True,
                "avg_volume": 5000,
                "last_date": pd.Timestamp("2026-01-29"),
            }
        ]
        result = _score_strength(lv, df, 100)
        self.assertLessEqual(result[0]["strength"], 100)

    def test_empty_df_no_crash(self):
        """空 DataFrame → 不崩溃"""
        levels = [
            {
                "price": 100,
                "touches": 3,
                "dual_sided": False,
                "avg_volume": 1000,
                "last_date": pd.Timestamp("2026-01-28"),
            }
        ]
        result = _score_strength(levels, pd.DataFrame(), 100)
        self.assertEqual(len(result), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  4. 支撑/压力分类
# ═══════════════════════════════════════════════════════════════════════════


class TestClassify(unittest.TestCase):
    """_classify — 按当前价分类支撑/压力。"""

    def test_above_current_is_resistance(self):
        """价格在当前价上方 → 压力位"""
        levels = [{"price": 105}]
        result = _classify(levels, current_price=100)
        self.assertEqual(result[0]["role"], "resistance")

    def test_below_current_is_support(self):
        """价格在当前价下方 → 支撑位"""
        levels = [{"price": 95}]
        result = _classify(levels, current_price=100)
        self.assertEqual(result[0]["role"], "support")

    def test_exactly_at_current_is_support(self):
        """恰好等于当前价 → 支撑位（else 分支，因为不是 >）"""
        levels = [{"price": 100}]
        result = _classify(levels, current_price=100)
        self.assertEqual(result[0]["role"], "support")

    def test_distance_pct_calculated(self):
        """距离百分比计算正确"""
        levels = [{"price": 105}]
        result = _classify(levels, current_price=100)
        # (105-100)/100 * 100 = 5%
        self.assertAlmostEqual(result[0]["distance_pct"], 5.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  5. adjust_exit_plan — SR 微调止盈止损
# ═══════════════════════════════════════════════════════════════════════════


class TestAdjustExitPlan(unittest.TestCase):
    """adjust_exit_plan — SR 位微调止盈止损。"""

    def test_no_sr_returns_original(self):
        """无 SR 数据 → 返回原计划不变"""
        original = {"stop": 95, "t1": 110, "t2": 120, "stop_dist": 5}
        result = adjust_exit_plan(original, None, direction=1, entry_price=100)
        self.assertEqual(result, original)

    def test_long_support_tighter_stop(self):
        """做多 + 支撑位比 ATR 止损更紧（且 > 0.5×ATR）→ 用 SR 止损"""
        original = {"stop": 90, "t1": 120, "t2": 140, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(94, distance_pct=6.0, strength=80),
            nearest_resistance=_make_level(110, distance_pct=10.0),
        )
        result = adjust_exit_plan(original, sr, direction=1, entry_price=100)
        # 原止损 10 点，SR 止损 6 点（0.5*10=5 < 6 < 10 → 有效）
        self.assertTrue(result.get("sr_stop", False))
        self.assertEqual(result["stop"], 94)
        self.assertAlmostEqual(result["stop_dist"], 6, places=2)

    def test_long_support_too_tight_no_change(self):
        """支撑位太近（< 0.5×ATR 止损）→ 不用（太近容易被扫）"""
        original = {"stop": 90, "t1": 120, "t2": 140, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(99, distance_pct=1.0, strength=80),
            nearest_resistance=_make_level(110, distance_pct=10.0),
        )
        result = adjust_exit_plan(original, sr, direction=1, entry_price=100)
        # SR 止损 = 1 点 < 0.5 * 10 = 5 → 太近，不用
        self.assertFalse(result.get("sr_stop", False))
        self.assertEqual(result["stop"], 90)

    def test_long_resistance_t1_in_2r(self):
        """做多 + 压力位在 2R 内 → T1 设在压力位"""
        original = {"stop": 90, "t1": 120, "t2": 140, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(90, distance_pct=10.0),
            nearest_resistance=_make_level(115, distance_pct=15.0, strength=80),
        )
        result = adjust_exit_plan(original, sr, direction=1, entry_price=100)
        # SR 目标距离 = 15 点（1.5R）< 2R → 用 SR 做 T1
        self.assertTrue(result.get("sr_t1", False))
        self.assertEqual(result["t1"], 115)

    def test_long_resistance_beyond_2r_no_change(self):
        """压力位超过 2R → T1 不变（太远了不适合做目标）"""
        original = {"stop": 90, "t1": 120, "t2": 140, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(90, distance_pct=10.0),
            nearest_resistance=_make_level(130, distance_pct=30.0, strength=80),
        )
        result = adjust_exit_plan(original, sr, direction=1, entry_price=100)
        # 30 点 = 3R > 2R → 不改 T1
        self.assertFalse(result.get("sr_t1", False))
        self.assertEqual(result["t1"], 120)

    def test_short_support_as_t1(self):
        """做空 + 支撑位在 2R 内 → T1 设在支撑位"""
        original = {"stop": 110, "t1": 80, "t2": 70, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(88, distance_pct=12.0, strength=80),
            nearest_resistance=_make_level(110, distance_pct=10.0),
        )
        result = adjust_exit_plan(original, sr, direction=-1, entry_price=100)
        # SR 目标距离 = 12 点（1.2R）< 2R → 用 SR 做 T1
        self.assertTrue(result.get("sr_t1", False))
        self.assertEqual(result["t1"], 88)

    def test_t2_not_adjusted(self):
        """T2 保持不变（趋势利润不让 SR 位限制）"""
        original = {"stop": 90, "t1": 120, "t2": 140, "stop_dist": 10}
        sr = _make_sr_result(
            nearest_support=_make_level(94, distance_pct=6.0),
            nearest_resistance=_make_level(108, distance_pct=8.0),
        )
        result = adjust_exit_plan(original, sr, direction=1, entry_price=100)
        self.assertEqual(result["t2"], 140, "T2 不应该被 SR 位调整")


# ═══════════════════════════════════════════════════════════════════════════
#  6. 完整分析（集成）
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalyzeIntegration(unittest.TestCase):
    """analyze — 完整 SR 分析集成测试。"""

    def _make_trend_df(self, start=100, slope=0.5, n=60):
        """构造简单的趋势行情 DataFrame。"""
        dates = pd.date_range("2026-01-01", periods=n, freq="D")
        highs = [start + slope * i + 1 for i in range(n)]
        lows = [start + slope * i - 1 for i in range(n)]
        closes = [start + slope * i for i in range(n)]
        df = pd.DataFrame(
            {
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [1000] * n,
            },
            index=dates,
        )
        return df

    def test_insufficient_data_empty(self):
        """数据不足 → 返回空结果"""
        df = self._make_trend_df(n=10)
        result = analyze(df, current_price=100)
        self.assertEqual(result["levels"], [])
        self.assertIsNone(result["nearest_support"])
        self.assertIsNone(result["nearest_resistance"])

    def test_none_df_empty(self):
        """None → 返回空结果"""
        result = analyze(None)
        self.assertEqual(result["levels"], [])

    def test_trend_market_has_levels(self):
        """趋势行情应该能识别出结构位"""
        df = self._make_trend_df(start=100, slope=0.3, n=60)
        result = analyze(df)
        # 趋势行情中应该能找到一些 swing high/low
        # 但不一定满足 MIN_TOUCHES=2 的聚类条件
        # 至少不崩溃
        self.assertIn("levels", result)
        self.assertIn("nearest_support", result)

    def test_current_price_defaults_to_close(self):
        """不传 current_price → 用收盘价"""
        df = self._make_trend_df(start=100, slope=0.5, n=60)
        result = analyze(df)
        expected_close = 100 + 0.5 * 59  # 最后一根的 close
        self.assertAlmostEqual(result["current_price"], expected_close, places=1)

    def test_result_has_all_keys(self):
        """返回结果包含所有必要字段"""
        df = self._make_trend_df(n=60)
        result = analyze(df, current_price=100)
        required_keys = [
            "levels",
            "nearest_support",
            "nearest_resistance",
            "at_support",
            "at_resistance",
            "zone",
            "zone_label",
            "nearest_dist_pct",
            "current_price",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"缺少字段: {key}")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR 位分析 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
