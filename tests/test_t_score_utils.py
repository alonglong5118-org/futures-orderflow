#!/usr/bin/env python3
"""
T 分计算工具 — 单元测试
===========================

1. cluster_vote_and_consensus — 簇投票与一致度
   - 单策略簇：投票=信号值，一致度=1
   - 多策略全同向：投票=均值，一致度=1
   - 多策略全反向抵消：投票=0，一致度=0
   - 多数同向少数反向：投票=加权均值，一致度=多数比例
   - 空簇：投票=0，一致度=0
   - 缺策略默认 0：未在 sig 中的策略计为 0
   - 多簇独立计算：互不影响
   - 返回两个 dict，key 对齐

2. crowd_penalty_factor — 拥挤降权系数
   - 一致度 <= 阈值 → 不降权（1.0）
   - 一致度 = 阈值 → 不降权（边界）
   - 一致度 > 阈值 → 线性降权
   - 一致度 = 1.0 → 最大降权 = 1 - crowd_pen
   - crowd_pen = 0 → 永远不降权
   - 阈值 = 0 → 任何一致度都降权
   - 阈值 = 1 → 永远不降权（除零保护）
   - factor 范围：[1 - crowd_pen, 1.0]
   - 负一致度 → 不降权（按 <= thresh 处理）

3. contrarian_damping_factor — 反向阻尼系数
   - 同向 → 不阻尼（1.0）
   - 一正一负 → 阻尼
   - 一方为 0 → 不阻尼
   - 双方为 0 → 不阻尼
   - 阻尼幅度 = min(|trend|, |mean|) / |trend| × contr_damp
   - |mean| >= |trend| → 最大阻尼 = 1 - contr_damp
   - contr_damp = 0 → 永远不阻尼
   - trend 接近 0 → 不阻尼（除零保护）
   - factor 范围：[1 - contr_damp, 1.0]
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from t_score_utils import (
    cluster_vote_and_consensus,
    contrarian_damping_factor,
    crowd_penalty_factor,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. cluster_vote_and_consensus
# ═══════════════════════════════════════════════════════════════════════════


class TestClusterVoteAndConsensus(unittest.TestCase):
    """cluster_vote_and_consensus 簇投票与一致度。"""

    def test_single_strategy_cluster(self):
        """单策略簇：投票=信号值，一致度=1"""
        sig = {"ma_break": 1.0}
        clusters = {"trend": ["ma_break"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 1.0)
        self.assertEqual(consensus["trend"], 1.0)

    def test_multi_all_same_direction(self):
        """多策略全同向：投票=均值，一致度=1"""
        sig = {"ma": 1.0, "boll": 1.0, "macd": 1.0}
        clusters = {"trend": ["ma", "boll", "macd"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 1.0)
        self.assertEqual(consensus["trend"], 1.0)

    def test_multi_all_opposite_cancel(self):
        """多策略全反向抵消：投票=0，一致度=0"""
        sig = {"ma": 1.0, "boll": -1.0}
        clusters = {"trend": ["ma", "boll"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 0.0)
        self.assertEqual(consensus["trend"], 0.0)

    def test_majority_same_minority_opposite(self):
        """多数同向少数反向：投票=加权均值，一致度=多数比例"""
        # 2 个 +1，1 个 -1 → 均值 = (2-1)/3 = 1/3
        # 均值 > 0 → sgn=1，同向的有 2 个 → 一致度 = 2/3
        sig = {"a": 1.0, "b": 1.0, "c": -1.0}
        clusters = {"trend": ["a", "b", "c"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertAlmostEqual(vote["trend"], 1 / 3, places=6)
        self.assertAlmostEqual(consensus["trend"], 2 / 3, places=6)

    def test_empty_cluster(self):
        """空簇：投票=0，一致度=0"""
        sig = {}
        clusters = {"empty": []}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["empty"], 0.0)
        self.assertEqual(consensus["empty"], 0.0)

    def test_missing_strategy_defaults_zero(self):
        """缺策略默认 0：未在 sig 中的策略计为 0"""
        sig = {"ma": 1.0}
        clusters = {"trend": ["ma", "missing_one"]}
        # 1.0 + 0 → 均值 = 0.5
        # 均值 > 0 → sgn=1，同向的有 1 个（ma），1 个=0 不算同向
        # 一致度 = 1/2 = 0.5
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 0.5)
        self.assertEqual(consensus["trend"], 0.5)

    def test_multiple_clusters_independent(self):
        """多簇独立计算：互不影响"""
        sig = {"ma": 1.0, "boll": -1.0, "rsi": 0.5}
        clusters = {
            "trend": ["ma"],
            "mean": ["boll"],
            "momentum": ["rsi"],
        }
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 1.0)
        self.assertEqual(vote["mean"], -1.0)
        self.assertEqual(vote["momentum"], 0.5)
        # 精确等于 ±1 的策略才算完全同向
        self.assertEqual(consensus["trend"], 1.0)  # 1.0 == +1 → 同向
        self.assertEqual(consensus["mean"], 1.0)  # -1.0 == -1 → 同向
        self.assertEqual(consensus["momentum"], 0.0)  # 0.5 != +1 → 不算完全同向

    def test_return_keys_aligned(self):
        """返回两个 dict，key 对齐"""
        clusters = {"a": ["x"], "b": ["y"], "c": []}
        sig = {"x": 1, "y": -1}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(set(vote.keys()), set(clusters.keys()))
        self.assertEqual(set(consensus.keys()), set(clusters.keys()))

    def test_consensus_requires_exact_match(self):
        """一致度语义：只有精确等于 ±1 的策略才算完全同向"""
        # 2 个满仓同向 (+1) + 1 个半仓同向 (0.5) + 1 个反向 (-1)
        sig = {"a": 1.0, "b": 1.0, "c": 0.5, "d": -1.0}
        clusters = {"trend": ["a", "b", "c", "d"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        # 投票均值 = (1+1+0.5-1)/4 = 1.5/4 = 0.375
        self.assertAlmostEqual(vote["trend"], 0.375, places=6)
        # 均值 > 0 → sgn = 1，精确等于 1 的只有 a 和 b → 2/4 = 0.5
        self.assertEqual(consensus["trend"], 0.5)

    def test_all_neutral_zero_consensus(self):
        """所有策略都是 0 → 投票=0，一致度=0"""
        sig = {"a": 0, "b": 0, "c": 0}
        clusters = {"trend": ["a", "b", "c"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        self.assertEqual(vote["trend"], 0.0)
        self.assertEqual(consensus["trend"], 0.0)

    def test_partial_values_not_binary(self):
        """非二元信号（0.5, 0.8 等）也能正确计算投票均值"""
        sig = {"a": 0.5, "b": 0.8, "c": -0.3}
        clusters = {"trend": ["a", "b", "c"]}
        vote, consensus = cluster_vote_and_consensus(sig, clusters)
        expected_mean = (0.5 + 0.8 - 0.3) / 3  # = 1.0/3 ≈ 0.333
        self.assertAlmostEqual(vote["trend"], expected_mean, places=6)
        # 一致度：必须精确等于 sgn（+1 或 -1）才算完全同向
        # 均值 > 0 → sgn = 1，但 0.5≠1, 0.8≠1, -0.3≠1 → 一致度 = 0
        self.assertEqual(consensus["trend"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. crowd_penalty_factor
# ═══════════════════════════════════════════════════════════════════════════


class TestCrowdPenaltyFactor(unittest.TestCase):
    """crowd_penalty_factor 拥挤降权系数。"""

    def test_below_threshold_no_penalty(self):
        """一致度 < 阈值 → 不降权（1.0）"""
        self.assertEqual(crowd_penalty_factor(0.5, crowd_thresh=0.8), 1.0)

    def test_at_threshold_no_penalty(self):
        """一致度 = 阈值 → 不降权（边界）"""
        self.assertEqual(crowd_penalty_factor(0.8, crowd_thresh=0.8), 1.0)

    def test_above_threshold_partial_penalty(self):
        """一致度 > 阈值 → 线性降权"""
        # thresh=0.8, consensus=0.9 → over = (0.9-0.8)/(1-0.8) = 0.5
        # factor = 1 - 0.35 × 0.5 = 0.825
        result = crowd_penalty_factor(0.9, crowd_thresh=0.8, crowd_pen=0.35)
        self.assertAlmostEqual(result, 0.825, places=6)

    def test_full_consensus_max_penalty(self):
        """一致度 = 1.0 → 最大降权 = 1 - crowd_pen"""
        result = crowd_penalty_factor(1.0, crowd_thresh=0.8, crowd_pen=0.35)
        self.assertAlmostEqual(result, 1.0 - 0.35, places=6)  # 0.65

    def test_zero_pen_always_one(self):
        """crowd_pen = 0 → 永远不降权"""
        self.assertEqual(crowd_penalty_factor(1.0, crowd_pen=0.0), 1.0)
        self.assertEqual(crowd_penalty_factor(0.5, crowd_pen=0.0), 1.0)

    def test_zero_threshold_all_penalized(self):
        """阈值 = 0 → 任何一致度>0 都降权"""
        # consensus=0.5, thresh=0 → over = min(1, 0.5/1) = 0.5
        # factor = 1 - 0.35 × 0.5 = 0.825
        result = crowd_penalty_factor(0.5, crowd_thresh=0.0, crowd_pen=0.35)
        self.assertAlmostEqual(result, 0.825, places=6)

    def test_threshold_one_no_penalty(self):
        """阈值 = 1 → 永远不降权（除零保护走 denom=1 路径，但 consensus<=thresh 直接返回 1）"""
        self.assertEqual(crowd_penalty_factor(1.0, crowd_thresh=1.0), 1.0)
        self.assertEqual(crowd_penalty_factor(0.5, crowd_thresh=1.0), 1.0)

    def test_factor_within_range(self):
        """factor 范围：[1 - crowd_pen, 1.0]"""
        pen = 0.35
        lo, hi = 1.0 - pen, 1.0
        for c in [0.0, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0]:
            f = crowd_penalty_factor(c, crowd_thresh=0.6, crowd_pen=pen)
            self.assertGreaterEqual(f, lo - 1e-12)
            self.assertLessEqual(f, hi + 1e-12)

    def test_negative_consensus_no_penalty(self):
        """负一致度 → 不降权（按 <= thresh 处理）"""
        self.assertEqual(crowd_penalty_factor(-0.5), 1.0)

    def test_consensus_above_one_clamped(self):
        """一致度 > 1 → over 被 clamp 到 1.0，即最大降权"""
        result = crowd_penalty_factor(2.0, crowd_thresh=0.8, crowd_pen=0.35)
        # over = min(1.0, (2.0-0.8)/0.2) = min(1, 6) = 1.0
        # factor = 1 - 0.35 = 0.65
        self.assertAlmostEqual(result, 0.65, places=6)

    def test_linear_decrease(self):
        """超过阈值后线性下降"""
        thresh, pen = 0.6, 0.4
        # 在 thresh 和 1.0 之间取两个点
        f1 = crowd_penalty_factor(0.7, thresh, pen)
        f2 = crowd_penalty_factor(0.8, thresh, pen)
        f3 = crowd_penalty_factor(0.9, thresh, pen)
        # 每增加 0.1 的一致度，factor 应该减少相同的量
        drop1 = f1 - f2
        drop2 = f2 - f3
        self.assertAlmostEqual(drop1, drop2, places=10)


# ═══════════════════════════════════════════════════════════════════════════
#  3. contrarian_damping_factor
# ═══════════════════════════════════════════════════════════════════════════


class TestContrarianDampingFactor(unittest.TestCase):
    """contrarian_damping_factor 反向阻尼系数。"""

    def test_same_direction_no_damping(self):
        """同向 → 不阻尼（1.0）"""
        self.assertEqual(contrarian_damping_factor(0.5, 0.3), 1.0)
        self.assertEqual(contrarian_damping_factor(-0.5, -0.3), 1.0)

    def test_opposite_direction_damped(self):
        """一正一负 → 阻尼（< 1.0）"""
        result = contrarian_damping_factor(0.5, -0.3, contr_damp=0.25)
        self.assertLess(result, 1.0)
        self.assertGreater(result, 1.0 - 0.25)  # 没到最大阻尼

    def test_one_zero_no_damping(self):
        """一方为 0 → 不阻尼"""
        self.assertEqual(contrarian_damping_factor(0.5, 0.0), 1.0)
        self.assertEqual(contrarian_damping_factor(0.0, 0.5), 1.0)

    def test_both_zero_no_damping(self):
        """双方为 0 → 不阻尼"""
        self.assertEqual(contrarian_damping_factor(0.0, 0.0), 1.0)

    def test_damping_formula(self):
        """阻尼幅度 = min(|trend|, |mean|) / |trend| × contr_damp"""
        # trend=0.5, mean=-0.5 → |mean|=|trend| → div = 1 → factor = 1 - 0.25×1 = 0.75
        result = contrarian_damping_factor(0.5, -0.5, contr_damp=0.25)
        self.assertAlmostEqual(result, 0.75, places=6)

    def test_mean_bigger_than_trend_max_damping(self):
        """|mean| >= |trend| → 最大阻尼 = 1 - contr_damp"""
        # trend=0.3, mean=-0.8 → min=0.3, div=0.3/0.3=1 → factor = 1 - 0.25 = 0.75
        result = contrarian_damping_factor(0.3, -0.8, contr_damp=0.25)
        self.assertAlmostEqual(result, 1.0 - 0.25, places=6)

    def test_mean_smaller_partial_damping(self):
        """|mean| < |trend| → 部分阻尼"""
        # trend=0.8, mean=-0.2 → min=0.2, div=0.2/0.8=0.25
        # factor = 1 - 0.25 × 0.25 = 1 - 0.0625 = 0.9375
        result = contrarian_damping_factor(0.8, -0.2, contr_damp=0.25)
        self.assertAlmostEqual(result, 0.9375, places=6)

    def test_zero_damp_always_one(self):
        """contr_damp = 0 → 永远不阻尼"""
        self.assertEqual(contrarian_damping_factor(0.5, -0.5, contr_damp=0.0), 1.0)
        self.assertEqual(contrarian_damping_factor(0.3, -0.8, contr_damp=0.0), 1.0)

    def test_trend_near_zero_no_damping(self):
        """trend 接近 0 → 不阻尼（除零保护）"""
        result = contrarian_damping_factor(1e-15, -0.5, contr_damp=0.25)
        self.assertEqual(result, 1.0)

    def test_factor_within_range(self):
        """factor 范围：[1 - contr_damp, 1.0]"""
        damp = 0.25
        lo, hi = 1.0 - damp, 1.0
        test_cases = [
            (0.5, 0.3),
            (0.5, -0.3),
            (0.1, -0.9),
            (-0.5, 0.3),
            (-0.5, -0.3),
            (0.0, 0.0),
        ]
        for t, m in test_cases:
            f = contrarian_damping_factor(t, m, contr_damp=damp)
            self.assertGreaterEqual(f, lo - 1e-12)
            self.assertLessEqual(f, hi + 1e-12)

    def test_symmetry_pos_neg_trend(self):
        """trend 正负对称：反向阻尼幅度只和绝对值有关"""
        f_pos = contrarian_damping_factor(0.5, -0.3, contr_damp=0.25)
        f_neg = contrarian_damping_factor(-0.5, 0.3, contr_damp=0.25)
        self.assertAlmostEqual(f_pos, f_neg, places=10)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  T 分计算工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
