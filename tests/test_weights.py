#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权重计算 — 单元测试
=========================

测试 regime_weights 和 cluster_weights 两个纯函数：

1. regime_weights(regime)
   - 趋势 regime：趋势策略权重 1.0，均值 0.3，季节性 0.2
   - 震荡 regime：趋势 0.3，均值 1.0，季节性 0.3
   - 波动 regime：趋势 0.5，均值 0.2，季节性 0.1
   - 未知 regime：所有策略 0.5

2. cluster_weights(regime, cfg, group)
   - 簇权重 = 簇内成员权重的均值（不是总和！P-A 核心）
   - P-D 季节性分组加权：seasonal_boost 开启时按分组放大 seasonal 簇
   - 分品种 regime 权重随动

历史覆盖：
  - P-A 去相关：簇权重是均值不是总和（消除共线放大）
  - P-D 季节性分组加权：农产品/化工等季节性品种 seasonal 权重更高
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    STRAT_CLUSTERS,
    cluster_weights,
    regime_weights,
)
from strategy_layer import ALL_STRATS, MEAN_STRATS, TREND_STRATS

# ═══════════════════════════════════════════════════════════════════════════
#  1. regime_weights — regime 级策略权重
# ═══════════════════════════════════════════════════════════════════════════

class TestRegimeWeights(unittest.TestCase):
    """regime_weights — 按 regime 返回各策略权重。"""

    def test_trend_regime_trend_dominant(self):
        """趋势 regime → 趋势策略权重最高（1.0）"""
        w = regime_weights("趋势")
        # 所有趋势策略权重 = 1.0
        for s in TREND_STRATS:
            self.assertEqual(w[s], 1.0)
        # 均值策略权重 = 0.3
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 0.3)
        # 季节性 = 0.2
        self.assertEqual(w["seasonal"], 0.2)

    def test_range_regime_mean_dominant(self):
        """震荡 regime → 均值策略权重最高（1.0）"""
        w = regime_weights("震荡")
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 1.0)
        for s in TREND_STRATS:
            self.assertEqual(w[s], 0.3)
        self.assertEqual(w["seasonal"], 0.3)

    def test_volatile_regime_moderate(self):
        """波动 regime → 趋势 0.5，均值 0.2，季节性 0.1"""
        w = regime_weights("波动")
        for s in TREND_STRATS:
            self.assertEqual(w[s], 0.5)
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 0.2)
        self.assertEqual(w["seasonal"], 0.1)

    def test_unknown_regime_uniform(self):
        """未知 regime → 所有策略 0.5（等权中性）"""
        w = regime_weights("未知")
        for s in ALL_STRATS:
            self.assertEqual(w[s], 0.5)

    def test_all_strategies_covered(self):
        """所有 8 个策略都在权重 dict 里"""
        for regime in ["趋势", "震荡", "波动", "未知"]:
            w = regime_weights(regime)
            for s in ALL_STRATS:
                self.assertIn(s, w, f"{regime} regime 缺少策略 {s}")
            self.assertEqual(len(w), len(ALL_STRATS))

    def test_all_weights_non_negative(self):
        """所有权重 >= 0"""
        for regime in ["趋势", "震荡", "波动", "未知"]:
            w = regime_weights(regime)
            for s, val in w.items():
                self.assertGreaterEqual(val, 0,
                    f"{regime} regime 策略 {s} 权重 = {val} < 0")

    def test_trend_regime_trend_gt_mean(self):
        """趋势 regime：趋势权重 > 均值权重（符合直觉）"""
        w = regime_weights("趋势")
        trend_avg = sum(w[s] for s in TREND_STRATS) / len(TREND_STRATS)
        mean_avg = sum(w[s] for s in MEAN_STRATS) / len(MEAN_STRATS)
        self.assertGreater(trend_avg, mean_avg)

    def test_range_regime_mean_gt_trend(self):
        """震荡 regime：均值权重 > 趋势权重（符合直觉）"""
        w = regime_weights("震荡")
        trend_avg = sum(w[s] for s in TREND_STRATS) / len(TREND_STRATS)
        mean_avg = sum(w[s] for s in MEAN_STRATS) / len(MEAN_STRATS)
        self.assertGreater(mean_avg, trend_avg)


# ═══════════════════════════════════════════════════════════════════════════
#  2. cluster_weights — 簇权重（P-A 去相关核心）
# ═══════════════════════════════════════════════════════════════════════════

class TestClusterWeights(unittest.TestCase):
    """cluster_weights — 簇级权重（P-A 去相关 + P-D 季节性加权）。"""

    def test_trend_regime_cluster_values(self):
        """趋势 regime 下各簇权重值"""
        cw = cluster_weights("趋势")
        # 趋势簇：5 个策略各 1.0 → 均值 = 1.0
        self.assertAlmostEqual(cw["trend"], 1.0)
        # 均值簇：2 个策略各 0.3 → 均值 = 0.3
        self.assertAlmostEqual(cw["mean"], 0.3)
        # 季节性：1 个策略 0.2 → 均值 = 0.2
        self.assertAlmostEqual(cw["seasonal"], 0.2)

    def test_range_regime_cluster_values(self):
        """震荡 regime 下各簇权重值"""
        cw = cluster_weights("震荡")
        # 趋势簇：5 个策略各 0.3 → 均值 = 0.3
        self.assertAlmostEqual(cw["trend"], 0.3)
        # 均值簇：2 个策略各 1.0 → 均值 = 1.0
        self.assertAlmostEqual(cw["mean"], 1.0)
        # 季节性：1 个策略 0.3 → 均值 = 0.3
        self.assertAlmostEqual(cw["seasonal"], 0.3)

    def test_volatile_regime_cluster_values(self):
        """波动 regime 下各簇权重值"""
        cw = cluster_weights("波动")
        self.assertAlmostEqual(cw["trend"], 0.5)
        self.assertAlmostEqual(cw["mean"], 0.2)
        self.assertAlmostEqual(cw["seasonal"], 0.1)

    def test_pa_regression_cluster_weight_is_mean_not_sum(self):
        """回归（P-A）：簇权重是均值，不是总和

        历史 bug：旧逻辑逐策略加权累加，趋势簇 5 个策略各 1.0 →
        趋势贡献 = 5.0（被共线放大 5 倍）。
        修复后：簇权重 = 均值 = 1.0，趋势簇只有 1 票。
        """
        cw = cluster_weights("趋势")
        # 趋势簇权重应该 = 1.0（均值），不是 5.0（总和）
        self.assertAlmostEqual(cw["trend"], 1.0,
            msg="P-A 回归 bug：簇权重应该是均值，不是总和（共线放大）")
        self.assertLess(cw["trend"], 2.0,
            msg="P-A 回归 bug：趋势簇权重不可能超过 1.0")

    def test_three_clusters_always_present(self):
        """始终返回 3 个簇：trend / mean / seasonal"""
        for regime in ["趋势", "震荡", "波动", "未知"]:
            cw = cluster_weights(regime)
            self.assertIn("trend", cw)
            self.assertIn("mean", cw)
            self.assertIn("seasonal", cw)

    def test_pd_seasonal_boost_disabled_by_default(self):
        """默认 cfg：seasonal_boost 关闭 → seasonal 权重不变"""
        cw_trend = cluster_weights("趋势")
        cw_with_group = cluster_weights("趋势", group="农产品")
        # 没开 seasonal_boost，传了 group 也不影响
        self.assertAlmostEqual(cw_trend["seasonal"], cw_with_group["seasonal"])

    def test_pd_seasonal_boost_enabled(self):
        """P-D：seasonal_boost 开启 + 指定分组 → seasonal 权重放大"""
        custom_cfg = {
            "seasonal_boost": {
                "enabled": True,
                "global_mult": 1.5,
                "by_group": {
                    "农产品": 2.0,  # 农产品季节性特别强
                    "化工": 1.5,
                },
            }
        }
        # 趋势 regime 下 base seasonal 权重 = 0.2
        # 农产品：0.2 * 1.5 (global) * 2.0 (group) = 0.6
        cw_agri = cluster_weights("趋势", cfg=custom_cfg, group="农产品")
        self.assertAlmostEqual(cw_agri["seasonal"], 0.6, places=4)

    def test_pd_seasonal_boost_only_affects_seasonal(self):
        """seasonal_boost 只影响 seasonal 簇，不影响 trend/mean"""
        custom_cfg = {
            "seasonal_boost": {
                "enabled": True,
                "global_mult": 3.0,
                "by_group": {"农产品": 2.0},
            }
        }
        cw_boosted = cluster_weights("趋势", cfg=custom_cfg, group="农产品")
        cw_normal = cluster_weights("趋势")
        # trend 和 mean 不变
        self.assertAlmostEqual(cw_boosted["trend"], cw_normal["trend"])
        self.assertAlmostEqual(cw_boosted["mean"], cw_normal["mean"])
        # seasonal 变大了
        self.assertGreater(cw_boosted["seasonal"], cw_normal["seasonal"])

    def test_pd_seasonal_boost_group_not_in_by_group(self):
        """分组不在 by_group 里 → 只乘 global_mult"""
        custom_cfg = {
            "seasonal_boost": {
                "enabled": True,
                "global_mult": 1.5,
                "by_group": {"农产品": 2.0},  # 只有农产品
            }
        }
        # 黑系不在 by_group 里 → by_group 倍率 = 1.0
        cw = cluster_weights("趋势", cfg=custom_cfg, group="黑系")
        # base = 0.2 * 1.5 * 1.0 = 0.3
        self.assertAlmostEqual(cw["seasonal"], 0.3, places=4)

    def test_pd_seasonal_boost_no_group_no_effect(self):
        """开启了 boost 但没传 group → 不生效"""
        custom_cfg = {
            "seasonal_boost": {
                "enabled": True,
                "global_mult": 3.0,
                "by_group": {"农产品": 2.0},
            }
        }
        cw_no_group = cluster_weights("趋势", cfg=custom_cfg, group=None)
        cw_normal = cluster_weights("趋势")
        self.assertAlmostEqual(cw_no_group["seasonal"], cw_normal["seasonal"],
            msg="没传 group 时 seasonal_boost 不应该生效")

    def test_unknown_regime_cluster_weights(self):
        """未知 regime → 所有簇权重 = 0.5（因为所有策略都是 0.5）"""
        cw = cluster_weights("未知")
        self.assertAlmostEqual(cw["trend"], 0.5)
        self.assertAlmostEqual(cw["mean"], 0.5)
        self.assertAlmostEqual(cw["seasonal"], 0.5)

    def test_cluster_weights_always_positive(self):
        """所有簇权重 >= 0"""
        for regime in ["趋势", "震荡", "波动", "未知"]:
            cw = cluster_weights(regime)
            for cname, val in cw.items():
                self.assertGreaterEqual(val, 0,
                    f"{regime} regime 簇 {cname} 权重 = {val} < 0")

    def test_strat_clusters_definition_matches(self):
        """STRAT_CLUSTERS 定义与策略模块的簇划分一致"""
        from strategy_layer import MEAN_STRATS, TREND_STRATS
        self.assertEqual(set(STRAT_CLUSTERS["trend"]), set(TREND_STRATS))
        self.assertEqual(set(STRAT_CLUSTERS["mean"]), set(MEAN_STRATS))
        self.assertEqual(STRAT_CLUSTERS["seasonal"], ["seasonal"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. 权重单调性与对称性
# ═══════════════════════════════════════════════════════════════════════════

class TestWeightMonotonicity(unittest.TestCase):
    """权重的单调性与对称性验证。"""

    def test_trend_cluster_weight_order(self):
        """趋势簇权重：趋势 regime > 波动 regime > 震荡 regime"""
        trend = cluster_weights("趋势")["trend"]
        volatile = cluster_weights("波动")["trend"]
        ranging = cluster_weights("震荡")["trend"]
        self.assertGreater(trend, volatile)
        self.assertGreater(volatile, ranging)

    def test_mean_cluster_weight_order(self):
        """均值簇权重：震荡 regime > 趋势 regime > 波动 regime"""
        ranging = cluster_weights("震荡")["mean"]
        trend = cluster_weights("趋势")["mean"]
        volatile = cluster_weights("波动")["mean"]
        self.assertGreater(ranging, trend)
        self.assertGreater(trend, volatile)

    def test_seasonal_weight_order(self):
        """季节性权重：震荡 > 趋势 > 波动（大致）"""
        ranging = cluster_weights("震荡")["seasonal"]
        trend = cluster_weights("趋势")["seasonal"]
        volatile = cluster_weights("波动")["seasonal"]
        self.assertGreater(ranging, trend)
        self.assertGreater(trend, volatile)

    def test_regime_switch_preserves_sum_order(self):
        """不同 regime 下，总权重（3 簇之和）的相对大小合理"""
        sums = {}
        for regime in ["趋势", "震荡", "波动", "未知"]:
            cw = cluster_weights(regime)
            sums[regime] = sum(cw.values())
        # 趋势 regime 总权重应该最大（1.0 + 0.3 + 0.2 = 1.5）
        self.assertAlmostEqual(sums["趋势"], 1.5, places=4)
        # 震荡 regime 总权重（0.3 + 1.0 + 0.3 = 1.6）
        self.assertAlmostEqual(sums["震荡"], 1.6, places=4)
        # 波动 regime 总权重（0.5 + 0.2 + 0.1 = 0.8）
        self.assertAlmostEqual(sums["波动"], 0.8, places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  权重计算 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
