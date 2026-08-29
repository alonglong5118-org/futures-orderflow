#!/usr/bin/env python3
"""
SR分析 + HMM状态 + 校准 — 单元测试
==========================================

1. _finalize_cluster — 聚类位最终化
   - 成交量加权均价
   - 无成交量 → 简单均价
   - 单次触及 → touches=1
   - 多次触及 → touches=N
   - 只有 high → dual_sided=False
   - 只有 low → dual_sided=False
   - high+low → dual_sided=True
   - avg_volume = 成交量均值
   - last_date 透传
   - strength 初始化为 0
   - price 保留 2 位小数
   - 返回 6 字段 dict

2. _classify — 支撑压力位分类
   - 价格在上方 → resistance
   - 价格在下方 → support
   - 等于当前价 → support
   - distance_pct 公式
   - 多个位全部正确分类
   - 原地修改 + 返回

3. _empty_result — 空结果构造
   - levels 为空列表
   - nearest_support/resistance 为 None
   - at_support/at_resistance 为 False
   - zone="far", zone_label="无数据"
   - nearest_dist_pct=99.0
   - current_price 透传
   - 返回 8 字段

4. _rule_label — HMM退化规则标签
   - 高波动 → high_vol
   - 趋势向上 → trend_up
   - 趋势向下 → trend_down
   - 震荡 → choppy
   - 边界：恰好 0.15 → choppy（不满足>0.15）
   - 边界：恰好 -0.15 → choppy（不满足<-0.15）
   - vol 75分位=0 → 跳过高波动判断

5. thr_mult — HMM态阈值乘数
   - trend_up → >1（顺势降低门槛）
   - trend_down → >1（顺势降低门槛）
   - choppy → <1（震荡提高门槛）
   - high_vol → <1（高波动提高门槛）
   - 未知 → 默认值
   - 返回 float
"""

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from regime_hmm import _rule_label, thr_mult
from sr_analyzer import _classify, _empty_result, _finalize_cluster

# ═══════════════════════════════════════════════════════════════════════════
#  1. _finalize_cluster
# ═══════════════════════════════════════════════════════════════════════════


class TestFinalizeCluster(unittest.TestCase):
    """_finalize_cluster 聚类位最终化。"""

    def test_volume_weighted_price(self):
        """成交量加权均价"""
        c = {
            "prices": [100, 110],
            "volumes": [100, 300],  # 100权重100, 110权重300
            "touches": 2,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        # (100*100 + 110*300) / 400 = (10000+33000)/400 = 43000/400 = 107.5
        r = _finalize_cluster(c)
        self.assertEqual(r["price"], 107.5)

    def test_no_volume_simple_average(self):
        """无成交量 → 简单均价"""
        c = {
            "prices": [100, 110],
            "volumes": [0, 0],
            "touches": 2,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["price"], 105.0)

    def test_single_touch(self):
        """单次触及 → touches=1"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["touches"], 1)

    def test_multiple_touches(self):
        """多次触及 → touches=N"""
        c = {
            "prices": [100, 102, 101],
            "volumes": [50, 60, 70],
            "touches": 3,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["touches"], 3)

    def test_high_only_not_dual(self):
        """只有 high → dual_sided=False"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertFalse(r["dual_sided"])

    def test_low_only_not_dual(self):
        """只有 low → dual_sided=False"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"low"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertFalse(r["dual_sided"])

    def test_high_and_low_dual_sided(self):
        """high+low → dual_sided=True"""
        c = {
            "prices": [100, 101],
            "volumes": [50, 60],
            "touches": 2,
            "types": {"high", "low"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertTrue(r["dual_sided"])

    def test_avg_volume(self):
        """avg_volume = 成交量均值"""
        c = {
            "prices": [100, 110],
            "volumes": [100, 300],
            "touches": 2,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["avg_volume"], 200.0)

    def test_last_date_passthrough(self):
        """last_date 透传"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"high"},
            "last_date": "2026-01-15",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["last_date"], "2026-01-15")

    def test_strength_init_zero(self):
        """strength 初始化为 0"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["strength"], 0)

    def test_price_two_decimals(self):
        """price 保留 2 位小数"""
        c = {
            "prices": [100.123, 110.456],
            "volumes": [100, 100],
            "touches": 2,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        self.assertEqual(r["price"], round(r["price"], 2))

    def test_return_six_fields(self):
        """返回 6 字段 dict"""
        c = {
            "prices": [100],
            "volumes": [50],
            "touches": 1,
            "types": {"high"},
            "last_date": "2026-08-28",
        }
        r = _finalize_cluster(c)
        for key in ("price", "touches", "dual_sided", "avg_volume", "last_date", "strength"):
            self.assertIn(key, r)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _classify
# ═══════════════════════════════════════════════════════════════════════════


class TestClassify(unittest.TestCase):
    """_classify 支撑压力位分类。"""

    def test_above_is_resistance(self):
        """价格在上方 → resistance"""
        levels = [{"price": 110}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "resistance")

    def test_below_is_support(self):
        """价格在下方 → support"""
        levels = [{"price": 90}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "support")

    def test_equal_is_support(self):
        """等于当前价 → support"""
        levels = [{"price": 100}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "support")

    def test_distance_pct_formula(self):
        """distance_pct 公式"""
        levels = [{"price": 110}]
        result = _classify(levels, 100)
        # |110-100| / 100 * 100 = 10%
        self.assertEqual(result[0]["distance_pct"], 10.0)

    def test_distance_pct_two_decimals(self):
        """distance_pct 保留 2 位小数"""
        levels = [{"price": 103.333}]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["distance_pct"], round(result[0]["distance_pct"], 2))

    def test_multiple_levels_all_classified(self):
        """多个位全部正确分类"""
        levels = [
            {"price": 120},  # 上方 → 压力
            {"price": 105},  # 上方 → 压力
            {"price": 95},  # 下方 → 支撑
            {"price": 80},  # 下方 → 支撑
        ]
        result = _classify(levels, 100)
        self.assertEqual(result[0]["role"], "resistance")
        self.assertEqual(result[1]["role"], "resistance")
        self.assertEqual(result[2]["role"], "support")
        self.assertEqual(result[3]["role"], "support")

    def test_modifies_in_place(self):
        """原地修改 + 返回"""
        levels = [{"price": 110}]
        result = _classify(levels, 100)
        # 同一个对象
        self.assertIs(result, levels)
        self.assertIn("role", levels[0])

    def test_empty_levels(self):
        """空列表 → 空列表"""
        result = _classify([], 100)
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════════════════════
#  3. _empty_result
# ═══════════════════════════════════════════════════════════════════════════


class TestEmptyResult(unittest.TestCase):
    """_empty_result 空结果构造。"""

    def test_levels_empty_list(self):
        """levels 为空列表"""
        r = _empty_result(100)
        self.assertEqual(r["levels"], [])

    def test_nearest_none(self):
        """nearest_support/resistance 为 None"""
        r = _empty_result(100)
        self.assertIsNone(r["nearest_support"])
        self.assertIsNone(r["nearest_resistance"])

    def test_at_flags_false(self):
        """at_support/at_resistance 为 False"""
        r = _empty_result(100)
        self.assertFalse(r["at_support"])
        self.assertFalse(r["at_resistance"])

    def test_zone_defaults(self):
        """zone='far', zone_label='无数据'"""
        r = _empty_result(100)
        self.assertEqual(r["zone"], "far")
        self.assertEqual(r["zone_label"], "无数据")

    def test_nearest_dist_pct_default(self):
        """nearest_dist_pct=99.0"""
        r = _empty_result(100)
        self.assertEqual(r["nearest_dist_pct"], 99.0)

    def test_current_price_passthrough(self):
        """current_price 透传"""
        r = _empty_result(42.5)
        self.assertEqual(r["current_price"], 42.5)

    def test_return_eight_fields(self):
        """返回 8 字段"""
        r = _empty_result(100)
        for key in (
            "levels",
            "nearest_support",
            "nearest_resistance",
            "at_support",
            "at_resistance",
            "zone",
            "zone_label",
            "nearest_dist_pct",
            "current_price",
        ):
            self.assertIn(key, r)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _rule_label
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleLabel(unittest.TestCase):
    """_rule_label HMM退化规则标签。"""

    def _make_Xz(self, ret_val, vol_val, n=20, high_vol_thresh=None):
        """构造测试用 Xz 数组（2列: ret, vol）。

        用 75 分位来控制 high_vol 阈值。
        前 75% 设为低波动，最后 25% 设为高波动 → 75分位 = 低波动的最高值
        """
        if high_vol_thresh is None:
            # 默认：vol 75分位 = 0.5 → 高波动阈值 0.5
            low_vol = 0.3  # 75% 的数据
            high_vol = 1.0  # 25% 的数据
        else:
            low_vol = high_vol_thresh * 0.6
            high_vol = high_vol_thresh * 2.0
        n_low = int(n * 0.75)
        n_high = n - n_low
        rets = np.zeros(n)
        vols = np.concatenate([np.full(n_low, low_vol), np.full(n_high, high_vol)])
        rets[-1] = ret_val
        vols[-1] = vol_val
        return np.column_stack([rets, vols])

    def test_high_vol_label(self):
        """高波动 → high_vol"""
        # vol[-1] >= 75分位 → high_vol
        Xz = self._make_Xz(ret_val=0.1, vol_val=1.0, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "high_vol")

    def test_trend_up_label(self):
        """趋势向上 → trend_up"""
        # vol[-1] < 75分位（低波动），ret[-1] > 0.15
        Xz = self._make_Xz(ret_val=0.3, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "trend_up")

    def test_trend_down_label(self):
        """趋势向下 → trend_down"""
        Xz = self._make_Xz(ret_val=-0.3, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "trend_down")

    def test_choppy_label(self):
        """震荡 → choppy"""
        # 低波动 + ret 在 [-0.15, 0.15] 之间
        Xz = self._make_Xz(ret_val=0.05, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "choppy")

    def test_boundary_positive_15(self):
        """边界：恰好 0.15 → choppy（不满足>0.15）"""
        Xz = self._make_Xz(ret_val=0.15, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "choppy")

    def test_boundary_negative_15(self):
        """边界：恰好 -0.15 → choppy（不满足<-0.15）"""
        Xz = self._make_Xz(ret_val=-0.15, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "choppy")

    def test_vol_75p_zero_skip_high_vol(self):
        """vol 75分位=0 → 跳过高波动判断"""
        # 全零波动率 → vhi=0 → 跳过 high_vol 判断
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.3  # ret > 0.15
        # 因为 vhi=0，不进入 high_vol 分支，ret=0.3 > 0.15 → trend_up
        self.assertEqual(_rule_label(Xz), "trend_up")

    def test_high_vol_takes_priority_over_trend(self):
        """高波动优先于趋势判断"""
        # 高波动 + 趋势向上 → 应该返回 high_vol（高波动优先）
        Xz = self._make_Xz(ret_val=0.5, vol_val=1.0, n=20, high_vol_thresh=0.5)
        self.assertEqual(_rule_label(Xz), "high_vol")

    def test_returns_string(self):
        """返回 str"""
        Xz = self._make_Xz(ret_val=0.05, vol_val=0.2, n=20, high_vol_thresh=0.5)
        self.assertIsInstance(_rule_label(Xz), str)

    def test_four_possible_labels(self):
        """4 种可能标签"""
        labels = set()
        # high_vol
        Xz = self._make_Xz(ret_val=0.1, vol_val=1.0, n=20, high_vol_thresh=0.5)
        labels.add(_rule_label(Xz))
        # trend_up
        Xz = self._make_Xz(ret_val=0.3, vol_val=0.2, n=20, high_vol_thresh=0.5)
        labels.add(_rule_label(Xz))
        # trend_down
        Xz = self._make_Xz(ret_val=-0.3, vol_val=0.2, n=20, high_vol_thresh=0.5)
        labels.add(_rule_label(Xz))
        # choppy
        Xz = self._make_Xz(ret_val=0.05, vol_val=0.2, n=20, high_vol_thresh=0.5)
        labels.add(_rule_label(Xz))
        self.assertEqual(len(labels), 4)


# ═══════════════════════════════════════════════════════════════════════════
#  5. thr_mult
# ═══════════════════════════════════════════════════════════════════════════


class TestThrMult(unittest.TestCase):
    """thr_mult HMM态阈值乘数。"""

    def test_trend_up_below_one(self):
        """trend_up → <1（顺势降低阈值，更易触发）"""
        mult = thr_mult("trend_up")
        self.assertLess(mult, 1.0)

    def test_trend_down_below_one(self):
        """trend_down → <1（顺势降低阈值，更易触发）"""
        mult = thr_mult("trend_down")
        self.assertLess(mult, 1.0)

    def test_choppy_above_one(self):
        """choppy → >1（震荡提高阈值，抑制假突破）"""
        mult = thr_mult("choppy")
        self.assertGreater(mult, 1.0)

    def test_high_vol_above_one(self):
        """high_vol → >1（高波动提高阈值，控风险少出手）"""
        mult = thr_mult("high_vol")
        self.assertGreater(mult, 1.0)

    def test_unknown_default(self):
        """未知 → 默认值"""
        from regime_hmm import DEFAULT_THR_MULT

        self.assertEqual(thr_mult("unknown"), DEFAULT_THR_MULT)

    def test_none_default(self):
        """None → 默认值"""
        from regime_hmm import DEFAULT_THR_MULT

        self.assertEqual(thr_mult(None), DEFAULT_THR_MULT)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(thr_mult("choppy"), float)

    def test_trend_states_same_mult(self):
        """trend_up 和 trend_down 乘数相同（对称）"""
        self.assertEqual(thr_mult("trend_up"), thr_mult("trend_down"))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR分析 + HMM状态 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
