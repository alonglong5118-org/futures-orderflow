#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T 评分合成（compute_T_score）— 单元测试
===========================================

完整 T 评分合成链路（簇投票 → 拥挤降权 → 反向阻尼 → 归一化）。

注：底层三个工具函数（cluster_vote_and_consensus / crowd_penalty_factor /
contrarian_damping_factor）的单元测试见 test_t_score_utils.py。
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from t_score_utils import compute_T_score


class TestComputeTScore(unittest.TestCase):
    """compute_T_score 完整 T 评分合成。"""

    def _cfg(self):
        return {
            "clusters": {
                "trend": ["ma_break", "dma", "turtle"],
                "mean": ["boll", "rsi"],
                "seasonal": ["carry"],
            },
            "cluster_weights": {"trend": 0.6, "mean": 0.25, "seasonal": 0.15},
            "base_cluster_weights": {"trend": 0.6, "mean": 0.25, "seasonal": 0.15},
        }

    def test_all_zero_signals_zero_T(self):
        """全零信号 → T=0"""
        cfg = self._cfg()
        sig = {"ma_break": 0, "dma": 0, "turtle": 0, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertEqual(result["T_score"], 0.0)

    def test_trend_all_long_positive_T(self):
        """趋势簇全做多 → T>0"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertGreater(result["T_score"], 0)

    def test_trend_all_short_negative_T(self):
        """趋势簇全做空 → T<0"""
        cfg = self._cfg()
        sig = {"ma_break": -1, "dma": -1, "turtle": -1, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertLess(result["T_score"], 0)

    def test_crowd_penalty_reduces_T(self):
        """拥挤降权生效 → T 比无降权时小"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 0, "rsi": 0, "carry": 0}
        r_pen = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"],
                                 crowd_thresh=0.8, crowd_pen=0.35)
        r_nopen = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"],
                                   crowd_thresh=0.8, crowd_pen=0.0)
        self.assertLess(abs(r_pen["T_score"]), abs(r_nopen["T_score"]))
        self.assertLess(r_pen["crowd_factor"], 1.0)
        self.assertEqual(r_nopen["crowd_factor"], 1.0)

    def test_contrarian_damping_reduces_amplitude(self):
        """反向阻尼生效 → T 幅度比无阻尼时小"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": -1, "rsi": -1, "carry": 0}
        r_damp = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"],
                                  contr_damp=0.25)
        r_nodamp = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"],
                                    contr_damp=0.0)
        self.assertLess(abs(r_damp["T_score"]), abs(r_nodamp["T_score"]))
        self.assertLess(r_damp["contr_factor"], 1.0)
        self.assertEqual(r_nodamp["contr_factor"], 1.0)

    def test_T_score_bounded_100(self):
        """T 范围 [-100, 100]（极端输入也钳位）"""
        cfg = self._cfg()
        sig_extreme = {"ma_break": 10, "dma": 10, "turtle": 10, "boll": 10, "rsi": 10, "carry": 10}
        result = compute_T_score(sig_extreme, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertLessEqual(result["T_score"], 100.0)
        self.assertGreaterEqual(result["T_score"], -100.0)

    def test_one_decimal_precision(self):
        """T_score 保留 1 位小数"""
        cfg = self._cfg()
        sig = {"ma_break": 0.5, "dma": 0.3, "turtle": 0.1, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertEqual(result["T_score"], round(result["T_score"], 1))

    def test_return_structure_complete(self):
        """返回结构完整（6 个字段）"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        for key in ("T_score", "cluster_vote", "cluster_consensus",
                    "crowd_factor", "contr_factor", "raw_score"):
            self.assertIn(key, result, f"missing key: {key}")

    def test_same_direction_no_contr_damping(self):
        """趋势均值同向 → contr_factor=1.0"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 1, "rsi": 1, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertEqual(result["contr_factor"], 1.0)

    def test_opposite_direction_has_contr_damping(self):
        """趋势均值反向 → contr_factor < 1.0"""
        cfg = self._cfg()
        sig = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": -1, "rsi": -1, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertLess(result["contr_factor"], 1.0)

    def test_seasonal_contributes_positive(self):
        """季节性维度做多 → T 增大"""
        cfg = self._cfg()
        sig_trend = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 0, "rsi": 0, "carry": 0}
        sig_both = {"ma_break": 1, "dma": 1, "turtle": 1, "boll": 0, "rsi": 0, "carry": 1}
        r1 = compute_T_score(sig_trend, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        r2 = compute_T_score(sig_both, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertGreater(r2["T_score"], r1["T_score"])

    def test_raw_score_rounded(self):
        """raw_score 保留 4 位小数"""
        cfg = self._cfg()
        sig = {"ma_break": 0.7, "dma": 0.3, "turtle": 0.5, "boll": 0, "rsi": 0, "carry": 0}
        result = compute_T_score(sig, cfg["clusters"], cfg["cluster_weights"], cfg["base_cluster_weights"])
        self.assertEqual(result["raw_score"], round(result["raw_score"], 4))


if __name__ == "__main__":
    print("=" * 60)
    print("  T 评分合成 (compute_T_score) — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
