#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蒙特卡洛模拟 — 单元测试
===========================

1. _risk_amount — 单笔风险金额计算
   - 有 stop_dist 时用 stop_dist × 乘数 × 手数
   - 没有 stop_dist 时用 |entry - stop| × 乘数 × 手数
   - 都没有时用权益 × 风险百分比兜底
   - 非法数据不崩溃
   - 风险金额必须 > 0

2. simulate — 蒙特卡洛 bootstrap 模拟
   - 样本不足 → ok=False，返回原因
   - 样本足够 → ok=True，返回完整统计
   - seed 固定 → 结果可复现
   - 全赚路径 → 终值 > 初始
   - 全亏路径 → 终值 < 初始
   - f=0 → 权益不变（无仓位）
   - horizon 参数控制模拟长度
   - bands 长度 = horizon + 1（含起点）
   - bands 分位数有序：p5 ≤ p25 ≤ p50 ≤ p75 ≤ p95
   - 胜率统计正确
   - prob_profit + prob_ruin 范围合理
   - maxdd 始终 ≥ 0
"""

import sys
import os
import unittest
import math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from montecarlo import _risk_amount, simulate


# ═══════════════════════════════════════════════════════════════════════════
#  1. _risk_amount
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskAmount(unittest.TestCase):
    """_risk_amount 单笔风险金额计算。"""

    def test_stop_dist_path(self):
        """有 stop_dist → 用 stop_dist × 乘数 × 手数"""
        t = {"symbol": "rb", "stop_dist": 20, "lots": 3}
        # rb 乘数 = 10（默认）
        # 风险 = 20 × 10 × 3 = 600
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 600.0)

    def test_entry_stop_path(self):
        """没有 stop_dist 但有 entry+stop → 用价差 × 乘数 × 手数"""
        t = {"symbol": "rb", "entry_price": 3300, "stop": 3280, "lots": 2}
        # 价差 = 20，乘数 = 10，手数 = 2
        # 风险 = 20 × 10 × 2 = 400
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 400.0)

    def test_fallback_equity_risk_pct(self):
        """都没有 → 用权益 × 风险百分比兜底"""
        t = {"symbol": "rb", "lots": 1}
        # 兜底 = max(1.0, 100000 × 2% / 100) = max(1, 2000) = 2000
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 2000.0)

    def test_fallback_minimum_1(self):
        """兜底值不能小于 1.0"""
        t = {"symbol": "rb", "lots": 1}
        r = _risk_amount(t, equity=10, risk_pct=0.1)
        # 10 × 0.1% = 0.01 < 1 → 取 1.0
        self.assertGreaterEqual(r, 1.0)

    def test_stop_dist_zero_uses_fallback(self):
        """stop_dist = 0 → 走兜底（因为 r > 0 检查不过）"""
        t = {"symbol": "rb", "stop_dist": 0, "lots": 3}
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        # 0 不满足 r > 0 → 走兜底
        self.assertEqual(r, 2000.0)

    def test_invalid_input_no_crash(self):
        """非法输入不崩溃"""
        t = {"symbol": "rb", "stop_dist": "abc", "entry_price": None, "stop": "xyz"}
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertGreater(r, 0)

    def test_stop_dist_prefers_over_entry_stop(self):
        """stop_dist 和 entry+stop 都有时 → 优先用 stop_dist"""
        t = {
            "symbol": "rb",
            "stop_dist": 50,       # 50 × 10 × 2 = 1000
            "entry_price": 3300,
            "stop": 3280,          # 20 × 10 × 2 = 400
            "lots": 2,
        }
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 1000.0)  # 优先 stop_dist

    def test_negative_stop_dist_takes_abs(self):
        """stop_dist 为负 → 取绝对值"""
        t = {"symbol": "rb", "stop_dist": -30, "lots": 1}
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 300.0)  # abs(-30) * 10 * 1 = 300

    def test_lots_defaults_to_1(self):
        """lots 缺失 → 默认 1 手"""
        t = {"symbol": "rb", "stop_dist": 20}
        r = _risk_amount(t, equity=100000, risk_pct=2.0)
        self.assertEqual(r, 200.0)  # 20 × 10 × 1 = 200


# ═══════════════════════════════════════════════════════════════════════════
#  2. simulate
# ═══════════════════════════════════════════════════════════════════════════

class TestSimulate(unittest.TestCase):
    """simulate 蒙特卡洛 bootstrap 模拟。"""

    def test_insufficient_samples_not_ok(self):
        """样本不足 → ok=False"""
        r_series = [0.5, -0.3]  # 只有 2 笔，不够
        result = simulate(r_series=r_series)
        self.assertFalse(result["ok"])
        self.assertIn("样本", result["reason"])
        self.assertEqual(result["n_trades"], 2)

    def test_sufficient_samples_ok(self):
        """样本足够 → ok=True"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, seed=42)
        self.assertTrue(result["ok"])
        self.assertEqual(result["n_trades"], 10)

    def test_seed_reproducible(self):
        """seed 固定 → 结果可复现"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        r1 = simulate(r_series=r_series, seed=42, n_paths=100)
        r2 = simulate(r_series=r_series, seed=42, n_paths=100)
        self.assertEqual(r1["terminal"]["p50"], r2["terminal"]["p50"])
        self.assertEqual(r1["maxdd"]["p50"], r2["maxdd"]["p50"])

    def test_all_winning_paths_grow(self):
        """全赚 R 序列 → 终值中位数 > 初始"""
        r_series = [0.5, 1.0, 0.3, 0.8, 0.2, 0.6, 0.4, 0.9, 0.1, 0.7]
        result = simulate(r_series=r_series, seed=42, n_paths=200)
        self.assertGreater(result["terminal"]["p50"], 100.0)

    def test_all_losing_paths_shrink(self):
        """全亏 R 序列 → 终值中位数 < 初始"""
        r_series = [-0.5, -1.0, -0.3, -0.8, -0.2, -0.6, -0.4, -0.9, -0.1, -0.7]
        result = simulate(r_series=r_series, seed=42, n_paths=200)
        self.assertLess(result["terminal"]["p50"], 100.0)

    def test_f_zero_no_change(self):
        """f=0 → 权益不变（不加仓）"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, f=0.0, seed=42, n_paths=100)
        self.assertEqual(result["terminal"]["p50"], 100.0)
        self.assertEqual(result["terminal"]["p5"], 100.0)
        self.assertEqual(result["terminal"]["p95"], 100.0)

    def test_horizon_length(self):
        """horizon 参数控制模拟长度"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, horizon=50, seed=42, n_paths=100)
        self.assertEqual(result["horizon"], 50)
        # bands 长度 = horizon + 1（含起点）
        self.assertEqual(len(result["bands"]), 51)

    def test_bands_quantiles_ordered(self):
        """bands 分位数有序：p5 ≤ p25 ≤ p50 ≤ p75 ≤ p95"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, seed=42, n_paths=500)
        for b in result["bands"]:
            self.assertLessEqual(b["p5"], b["p25"])
            self.assertLessEqual(b["p25"], b["p50"])
            self.assertLessEqual(b["p50"], b["p75"])
            self.assertLessEqual(b["p75"], b["p95"])

    def test_first_band_equals_start_eq(self):
        """第一步（step=0）所有分位都等于初始权益"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, start_eq=1000.0, seed=42, n_paths=100)
        first = result["bands"][0]
        self.assertEqual(first["p5"], 1000.0)
        self.assertEqual(first["p50"], 1000.0)
        self.assertEqual(first["p95"], 1000.0)

    def test_win_rate_correct(self):
        """胜率统计正确（正 R 占比）"""
        # 5 正 5 负 → 胜率 0.5
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, seed=42, n_paths=100)
        self.assertEqual(result["win_rate"], 0.5)

    def test_maxdd_non_negative(self):
        """最大回撤始终 ≥ 0"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, seed=42, n_paths=100)
        self.assertGreaterEqual(result["maxdd"]["p50"], 0.0)
        self.assertGreaterEqual(result["maxdd"]["p95"], 0.0)
        self.assertGreaterEqual(result["maxdd"]["mean"], 0.0)

    def test_prob_profit_range(self):
        """prob_profit 在 [0, 1] 范围内"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, seed=42, n_paths=200)
        self.assertGreaterEqual(result["terminal"]["prob_profit"], 0.0)
        self.assertLessEqual(result["terminal"]["prob_profit"], 1.0)

    def test_custom_start_equity(self):
        """自定义初始权益"""
        r_series = [0.5, -0.3, 1.0, -0.5, 0.2, -0.1, 0.8, -0.4, 0.3, -0.2]
        result = simulate(r_series=r_series, start_eq=50000.0, seed=42, n_paths=100)
        self.assertEqual(result["bands"][0]["p50"], 50000.0)

    def test_avg_R_matches_series_mean(self):
        """avg_R 等于 R 序列均值"""
        r_series = [1.0, -0.5, 0.3, -0.2, 0.8, -0.4, 0.6, -0.3, 0.1, -0.1]
        result = simulate(r_series=r_series, seed=42, n_paths=100)
        expected_avg = sum(r_series) / len(r_series)
        self.assertAlmostEqual(result["avg_R"], expected_avg, places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  蒙特卡洛模拟 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
