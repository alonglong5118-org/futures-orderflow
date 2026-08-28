#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GBM/GARCH 波动率工具 — 单元测试
===================================

1. _log_returns — 对数收益计算
   - 正常数据 → 返回正确长度
   - 数据不足 → None
   - 无 close 列 → None
   - 常数列 → 接近 0
   - 恒涨 → 收益恒定

2. _ewma_vol — EWMA 波动率
   - 零收益 → 零波动
   - 常波动 → 收敛到该水平
   - lam=1 → 等于初始方差开方
   - lam=0 → 等于最后一个收益绝对值
   - 默认 lam=0.94

3. _rolling_std — 滑动标准差
   - 正常计算
   - 数据不足 → 空数组
   - 常数列 → 标准差 = 0
   - 长度 = n - window + 1
   - 与 numpy rolling 对比验证

4. _garch_nll — GARCH 负对数似然
   - 非法参数 → 大惩罚（1e10）
   - omega <= 0 → 惩罚
   - alpha + beta >= 0.999 → 惩罚
   - 零收益 → 有限值
   - 正常参数 → 有限值

5. thr_mult / risk_scale — 波动率状态映射
   - 4 种状态的正确值
   - 未知状态 → 默认值
   - 单调性验证：low ≤ normal ≤ high ≤ extreme（阈值）
   - 单调性验证：low ≥ normal ≥ high ≥ extreme（仓位）
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gbm_garch import (
    DEFAULT_RISK_SCALE,
    DEFAULT_THR_MULT,
    RISK_SCALE,
    THR_MULT,
    _ewma_vol,
    _garch_nll,
    _log_returns,
    _rolling_std,
    risk_scale,
    thr_mult,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. _log_returns
# ═══════════════════════════════════════════════════════════════════════════

class TestLogReturns(unittest.TestCase):
    """_log_returns 对数收益计算。"""

    def test_normal_data_returns_correct_length(self):
        """正常数据 → 返回 n-1 个收益"""
        df = pd.DataFrame({"close": np.exp(np.arange(100, dtype=float))})
        r = _log_returns(df)
        self.assertIsNotNone(r)
        self.assertEqual(len(r), 99)  # n-1

    def test_insufficient_data_returns_none(self):
        """数据不足（<60）→ None"""
        df = pd.DataFrame({"close": np.arange(30, dtype=float)})
        r = _log_returns(df)
        self.assertIsNone(r)

    def test_exactly_60_returns_59(self):
        """刚好 60 根 → 返回 59 个收益（>= 50，够用）"""
        df = pd.DataFrame({"close": np.exp(np.arange(60, dtype=float) * 0.01)})
        r = _log_returns(df)
        self.assertIsNotNone(r)
        self.assertEqual(len(r), 59)

    def test_no_close_column_returns_none(self):
        """无 close 列 → None"""
        df = pd.DataFrame({"open": np.arange(100, dtype=float)})
        r = _log_returns(df)
        self.assertIsNone(r)

    def test_constant_prices_zero_returns(self):
        """价格不变 → 收益 ≈ 0"""
        df = pd.DataFrame({"close": [100.0] * 100})
        r = _log_returns(df)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(np.mean(r), 0.0, places=6)
        self.assertAlmostEqual(np.std(r), 0.0, places=6)

    def test_constant_growth_constant_return(self):
        """恒比例增长 → 收益恒定"""
        # 每天涨 1%
        prices = 100 * np.exp(np.arange(100) * 0.01)
        df = pd.DataFrame({"close": prices})
        r = _log_returns(df)
        self.assertIsNotNone(r)
        # 所有收益都 ≈ 0.01
        self.assertTrue(np.allclose(r, 0.01, atol=1e-10))

    def test_returns_are_floats(self):
        """返回值是 float 数组"""
        df = pd.DataFrame({"close": np.exp(np.arange(100, dtype=float) * 0.01)})
        r = _log_returns(df)
        self.assertEqual(r.dtype, np.float64)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _ewma_vol
# ═══════════════════════════════════════════════════════════════════════════

class TestEwmaVol(unittest.TestCase):
    """_ewma_vol EWMA 波动率。"""

    def test_zero_returns_zero_vol(self):
        """全零收益 → 零波动"""
        r = np.zeros(100)
        vol = _ewma_vol(r)
        self.assertAlmostEqual(vol, 0.0, places=6)

    def test_positive_lambda_converges(self):
        """正常 lambda → 有限正值"""
        np.random.seed(42)
        r = np.random.randn(500) * 0.02  # 2% 波动
        vol = _ewma_vol(r)
        self.assertGreater(vol, 0)
        # 应该接近 2%（但因为 EWMA 有滞后，大致范围即可）
        self.assertGreater(vol, 0.005)
        self.assertLess(vol, 0.05)

    def test_lambda_one_constant_var(self):
        """lam=1 → 方差不变（等于初始方差的 sqrt）"""
        r = np.array([0.02, -0.01, 0.015, -0.005, 0.01])
        vol = _ewma_vol(r, lam=1.0)
        # lam=1 时：var = lam * var + (1-lam) * x^2 = var（不变）
        # 初始 var = np.var(r)，每步都不变
        expected = np.sqrt(np.var(r))
        self.assertAlmostEqual(vol, expected, places=10)

    def test_lambda_zero_last_abs(self):
        """lam=0 → 波动率 ≈ |最后一个收益|"""
        r = np.array([0.02, -0.01, 0.015, -0.005, 0.01])
        vol = _ewma_vol(r, lam=0.0)
        # lam=0 时：var = 0 * var + 1 * x^2 = x^2（完全跟随最新）
        # 注意：初始 var = np.var(r)，然后逐步被覆盖
        # 最后一步：var = r[-1]^2
        expected = abs(r[-1])
        self.assertAlmostEqual(vol, expected, places=10)

    def test_default_lambda_094(self):
        """默认 lambda = 0.94"""
        np.random.seed(42)
        r = np.random.randn(200) * 0.02
        vol_default = _ewma_vol(r)
        vol_explicit = _ewma_vol(r, lam=0.94)
        self.assertAlmostEqual(vol_default, vol_explicit, places=10)

    def test_higher_vol_higher_ewma(self):
        """高波动数据 → EWMA 更大"""
        np.random.seed(42)
        r_low = np.random.randn(500) * 0.01
        r_high = np.random.randn(500) * 0.05
        vol_low = _ewma_vol(r_low)
        vol_high = _ewma_vol(r_high)
        self.assertGreater(vol_high, vol_low)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _rolling_std
# ═══════════════════════════════════════════════════════════════════════════

class TestRollingStd(unittest.TestCase):
    """_rolling_std 滑动标准差。"""

    def test_normal_length(self):
        """长度 = n - window + 1"""
        arr = np.random.randn(100)
        result = _rolling_std(arr, window=20)
        self.assertEqual(len(result), 100 - 20 + 1)

    def test_insufficient_data_empty(self):
        """数据不足 → 空数组"""
        arr = np.random.randn(10)
        result = _rolling_std(arr, window=20)
        self.assertEqual(len(result), 0)

    def test_constant_array_zero_std(self):
        """常数列 → 标准差 = 0"""
        arr = np.ones(100) * 5.0
        result = _rolling_std(arr, window=20)
        self.assertTrue(np.allclose(result, 0.0, atol=1e-10))

    def test_matches_pandas_rolling(self):
        """与 pandas rolling std 一致（ddof=0）"""
        np.random.seed(42)
        arr = np.random.randn(100)
        window = 20
        result = _rolling_std(arr, window=window)
        # pandas 默认 ddof=1，我们的实现是总体标准差（ddof=0）
        expected_pd = pd.Series(arr).rolling(window).std(ddof=0).dropna().values
        np.testing.assert_allclose(result, expected_pd, atol=1e-10)

    def test_window_one_each_element(self):
        """window=1 → 每个元素都是 0（单点标准差 = 0）"""
        arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _rolling_std(arr, window=1)
        self.assertEqual(len(result), 5)
        self.assertTrue(np.allclose(result, 0.0, atol=1e-10))

    def test_non_negative(self):
        """标准差恒非负"""
        np.random.seed(42)
        arr = np.random.randn(200)
        result = _rolling_std(arr, window=10)
        self.assertTrue(np.all(result >= 0))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _garch_nll
# ═══════════════════════════════════════════════════════════════════════════

class TestGarchNll(unittest.TestCase):
    """_garch_nll GARCH 负对数似然。"""

    def test_invalid_omega_returns_penalty(self):
        """omega <= 0 → 大惩罚 1e10"""
        r = np.random.randn(100) * 0.02
        nll = _garch_nll((0.0, 0.1, 0.85), r)
        self.assertEqual(nll, 1e10)
        nll2 = _garch_nll((-0.001, 0.1, 0.85), r)
        self.assertEqual(nll2, 1e10)

    def test_negative_alpha_returns_penalty(self):
        """alpha < 0 → 大惩罚"""
        r = np.random.randn(100) * 0.02
        nll = _garch_nll((1e-6, -0.1, 0.85), r)
        self.assertEqual(nll, 1e10)

    def test_too_high_persistence_returns_penalty(self):
        """alpha + beta >= 0.999 → 大惩罚"""
        r = np.random.randn(100) * 0.02
        nll = _garch_nll((1e-6, 0.5, 0.5), r)  # 1.0 >= 0.999
        self.assertEqual(nll, 1e10)

    def test_valid_params_finite(self):
        """合法参数 → 有限的 NLL"""
        np.random.seed(42)
        r = np.random.randn(500) * 0.02
        nll = _garch_nll((1e-6, 0.10, 0.85), r)
        self.assertTrue(np.isfinite(nll))
        # 比非法参数的惩罚值小得多
        self.assertLess(nll, 1e10)

    def test_zero_returns_finite(self):
        """零收益 → 仍然有限"""
        r = np.zeros(100)
        nll = _garch_nll((1e-6, 0.10, 0.85), r)
        self.assertTrue(np.isfinite(nll))

    def test_better_params_lower_nll(self):
        """更接近真实参数 → NLL 更低"""
        np.random.seed(42)
        # 用已知参数生成数据
        true_omega, true_alpha, true_beta = 1e-5, 0.10, 0.85
        n = 1000
        sigma2 = np.empty(n)
        r = np.empty(n)
        sigma2[0] = true_omega / (1 - true_alpha - true_beta)
        r[0] = np.sqrt(sigma2[0]) * np.random.randn()
        for t in range(1, n):
            sigma2[t] = true_omega + true_alpha * r[t-1]**2 + true_beta * sigma2[t-1]
            r[t] = np.sqrt(sigma2[t]) * np.random.randn()

        # 真实参数附近的 NLL 应该比随机参数低
        nll_true = _garch_nll((true_omega, true_alpha, true_beta), r)
        nll_wrong = _garch_nll((1e-4, 0.01, 0.99), r)
        self.assertGreater(nll_wrong, nll_true)


# ═══════════════════════════════════════════════════════════════════════════
#  5. thr_mult / risk_scale
# ═══════════════════════════════════════════════════════════════════════════

class TestVolStateMapping(unittest.TestCase):
    """波动率状态 → 阈值乘数 / 仓位系数。"""

    def test_thr_mult_all_states(self):
        """4 种状态都有定义"""
        for state in ["low", "normal", "high", "extreme"]:
            val = thr_mult(state)
            self.assertIn(state, THR_MULT)
            self.assertEqual(val, THR_MULT[state])

    def test_thr_mult_unknown_default(self):
        """未知状态 → 默认值 1.0"""
        self.assertEqual(thr_mult("invalid"), DEFAULT_THR_MULT)
        self.assertEqual(thr_mult(""), DEFAULT_THR_MULT)
        self.assertEqual(thr_mult(None), DEFAULT_THR_MULT)

    def test_thr_mult_monotonic_increasing(self):
        """阈值乘数单调性：low <= normal <= high <= extreme"""
        self.assertLessEqual(thr_mult("low"), thr_mult("normal"))
        self.assertLessEqual(thr_mult("normal"), thr_mult("high"))
        self.assertLessEqual(thr_mult("high"), thr_mult("extreme"))

    def test_risk_scale_all_states(self):
        """4 种仓位系数都有定义"""
        for state in ["low", "normal", "high", "extreme"]:
            val = risk_scale(state)
            self.assertIn(state, RISK_SCALE)
            self.assertEqual(val, RISK_SCALE[state])

    def test_risk_scale_unknown_default(self):
        """未知状态 → 默认值 1.0"""
        self.assertEqual(risk_scale("invalid"), DEFAULT_RISK_SCALE)
        self.assertEqual(risk_scale(""), DEFAULT_RISK_SCALE)

    def test_risk_scale_monotonic_decreasing(self):
        """仓位系数单调性：low >= normal >= high >= extreme（波动越大仓位越小）"""
        self.assertGreaterEqual(risk_scale("low"), risk_scale("normal"))
        self.assertGreaterEqual(risk_scale("normal"), risk_scale("high"))
        self.assertGreaterEqual(risk_scale("high"), risk_scale("extreme"))

    def test_high_vol_reduces_position(self):
        """高波动 → 仓位 < 1.0"""
        self.assertLess(risk_scale("high"), 1.0)
        self.assertLess(risk_scale("extreme"), 1.0)

    def test_low_vol_normal_position(self):
        """低/正常波动 → 仓位 = 1.0"""
        self.assertEqual(risk_scale("low"), 1.0)
        self.assertEqual(risk_scale("normal"), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  GBM/GARCH 波动率工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
