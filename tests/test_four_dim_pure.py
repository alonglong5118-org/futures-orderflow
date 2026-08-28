#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维策略核心纯函数 — 单元测试
======================================

1. regime_weights — 市场状态权重映射
   - "趋势"：趋势策略=1.0, 均值=0.3, seasonal=0.2
   - "震荡"：趋势=0.3, 均值=1.0, seasonal=0.3
   - "波动"：趋势=0.5, 均值=0.2, seasonal=0.1
   - 未知/其他 → 全部 0.5
   - 返回 dict，键包含所有策略
   - 趋势策略在趋势市权重 > 均值策略
   - 均值策略在震荡市权重 > 趋势策略

2. _is_risk_locked — 风控锁定检查
   - None → 不锁定
   - state="HALTED" → 锁定
   - state="LOCKED" → 锁定
   - state="NORMAL" → 不锁定
   - scale=0 → 锁定
   - scale<0 → 锁定
   - scale=0.5 → 不锁定
   - scale=None → 不锁定（默认 1.0）
   - 锁定时有 reason
   - 返回 (bool, str) 二元组

3. combine_bias — 背景偏置合成
   - 默认权重：T=0.6, F=0.25, C=0.15
   - 全正 → 正结果
   - 全负 → 负结果
   - 全零 → 0
   - 自定义权重生效
   - T 主导（权重最大）
   - F 和 C 贡献较小
   - 保留 1 位小数
   - cfg=None → 使用默认权重
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    regime_weights, _is_risk_locked, combine_bias,
    TREND_STRATS, MEAN_STRATS, STRATS,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. regime_weights
# ═══════════════════════════════════════════════════════════════════════════

class TestRegimeWeights(unittest.TestCase):
    """regime_weights 市场状态权重映射。"""

    def test_trend_regime_trend_high(self):
        """'趋势'：趋势策略权重 = 1.0"""
        w = regime_weights("趋势")
        for s in TREND_STRATS:
            self.assertEqual(w[s], 1.0)

    def test_trend_regime_mean_low(self):
        """'趋势'：均值策略权重 = 0.3"""
        w = regime_weights("趋势")
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 0.3)

    def test_trend_regime_seasonal(self):
        """'趋势'：seasonal = 0.2"""
        w = regime_weights("趋势")
        self.assertEqual(w["seasonal"], 0.2)

    def test_range_regime_mean_high(self):
        """'震荡'：均值策略权重 = 1.0"""
        w = regime_weights("震荡")
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 1.0)

    def test_range_regime_trend_low(self):
        """'震荡'：趋势策略权重 = 0.3"""
        w = regime_weights("震荡")
        for s in TREND_STRATS:
            self.assertEqual(w[s], 0.3)

    def test_range_regime_seasonal(self):
        """'震荡'：seasonal = 0.3"""
        w = regime_weights("震荡")
        self.assertEqual(w["seasonal"], 0.3)

    def test_volatility_regime_mixed(self):
        """'波动'：趋势=0.5, 均值=0.2, seasonal=0.1"""
        w = regime_weights("波动")
        for s in TREND_STRATS:
            self.assertEqual(w[s], 0.5)
        for s in MEAN_STRATS:
            self.assertEqual(w[s], 0.2)
        self.assertEqual(w["seasonal"], 0.1)

    def test_unknown_regime_all_half(self):
        """未知/其他 → 全部 0.5"""
        w = regime_weights("未知")
        for s in STRATS:
            self.assertEqual(w[s], 0.5)

    def test_returns_dict(self):
        """返回 dict"""
        self.assertIsInstance(regime_weights("趋势"), dict)

    def test_contains_all_strats(self):
        """键包含所有策略"""
        w = regime_weights("趋势")
        for s in STRATS:
            self.assertIn(s, w)

    def test_trend_city_trend_gt_mean(self):
        """趋势市：趋势策略权重 > 均值策略"""
        w = regime_weights("趋势")
        trend_w = w[list(TREND_STRATS)[0]]
        mean_w = w[list(MEAN_STRATS)[0]]
        self.assertGreater(trend_w, mean_w)

    def test_range_city_mean_gt_trend(self):
        """震荡市：均值策略权重 > 趋势策略"""
        w = regime_weights("震荡")
        trend_w = w[list(TREND_STRATS)[0]]
        mean_w = w[list(MEAN_STRATS)[0]]
        self.assertGreater(mean_w, trend_w)

    def test_empty_string_unknown(self):
        """空串 → 全部 0.5（未知分支）"""
        w = regime_weights("")
        for s in STRATS:
            self.assertEqual(w[s], 0.5)

    def test_none_unknown(self):
        """None → 全部 0.5（未知分支）"""
        w = regime_weights(None)
        for s in STRATS:
            self.assertEqual(w[s], 0.5)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _is_risk_locked
# ═══════════════════════════════════════════════════════════════════════════

class TestIsRiskLocked(unittest.TestCase):
    """_is_risk_locked 风控锁定检查。"""

    def test_none_not_locked(self):
        """None → 不锁定"""
        locked, reason = _is_risk_locked(None)
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    def test_halted_locked(self):
        """state='HALTED' → 锁定"""
        rs = {"state": "HALTED", "lock_reason": "熔断"}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertEqual(reason, "熔断")

    def test_locked_state_locked(self):
        """state='LOCKED' → 锁定"""
        rs = {"state": "LOCKED", "reason": "超限"}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertIn("超限", reason)

    def test_normal_not_locked(self):
        """state='NORMAL' → 不锁定"""
        rs = {"state": "NORMAL", "scale": 1.0}
        locked, reason = _is_risk_locked(rs)
        self.assertFalse(locked)

    def test_scale_zero_locked(self):
        """scale=0 → 锁定"""
        rs = {"state": "NORMAL", "scale": 0.0}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertIn("scale=0", reason)

    def test_scale_negative_locked(self):
        """scale<0 → 锁定"""
        rs = {"state": "NORMAL", "scale": -0.5}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)

    def test_scale_half_not_locked(self):
        """scale=0.5 → 不锁定"""
        rs = {"state": "NORMAL", "scale": 0.5}
        locked, _ = _is_risk_locked(rs)
        self.assertFalse(locked)

    def test_scale_none_not_locked(self):
        """scale=None → 不锁定（默认 1.0）"""
        rs = {"state": "NORMAL"}
        locked, _ = _is_risk_locked(rs)
        self.assertFalse(locked)

    def test_locked_has_reason(self):
        """锁定时有 reason"""
        rs = {"state": "HALTED", "lock_reason": "爆仓熔断"}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertTrue(len(reason) > 0)

    def test_returns_tuple(self):
        """返回 (bool, str) 二元组"""
        result = _is_risk_locked(None)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], str)

    def test_empty_dict_not_locked(self):
        """空 dict → 不锁定"""
        locked, _ = _is_risk_locked({})
        self.assertFalse(locked)

    def test_halted_without_reason_fallback(self):
        """HALTED 无 lock_reason → 回退到 reason 字段"""
        rs = {"state": "HALTED", "reason": "手动暂停"}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertEqual(reason, "手动暂停")

    def test_halted_no_reason_fallback_state(self):
        """HALTED 无任何原因 → 状态=xxx"""
        rs = {"state": "HALTED"}
        locked, reason = _is_risk_locked(rs)
        self.assertTrue(locked)
        self.assertIn("状态=HALTED", reason)


# ═══════════════════════════════════════════════════════════════════════════
#  3. combine_bias
# ═══════════════════════════════════════════════════════════════════════════

class TestCombineBias(unittest.TestCase):
    """combine_bias 背景偏置合成。"""

    def test_default_weights_sum_positive(self):
        """全正 → 正结果"""
        # 参数顺序: F, T, C
        result = combine_bias(50, 50, 50)
        self.assertGreater(result, 0)

    def test_all_negative(self):
        """全负 → 负结果"""
        result = combine_bias(-50, -50, -50)
        self.assertLess(result, 0)

    def test_all_zero(self):
        """全零 → 0"""
        self.assertEqual(combine_bias(0, 0, 0), 0.0)

    def test_custom_weights_effective(self):
        """自定义权重生效"""
        cfg = {"combine_weights": {"T": 1.0, "F": 0.0, "C": 0.0}}
        # 只有 T 有贡献（参数顺序 F, T, C）
        result = combine_bias(100, 0, 0, cfg=cfg)  # F=100, T=0, C=0
        self.assertEqual(result, 0.0)  # T=0, F权重=0
        result2 = combine_bias(0, 100, 0, cfg=cfg)  # F=0, T=100, C=0
        self.assertEqual(result2, 100.0)  # T=100 * 1.0 = 100

    def test_t_is_dominant(self):
        """T 权重最大（默认 0.6 > 0.25/0.15）"""
        # 参数顺序: F, T, C
        # F=-100, T=100, C=-100
        # 0.25*(-100) + 0.6*100 + 0.15*(-100) = -25 + 60 - 15 = 20
        result = combine_bias(-100, 100, -100)
        # T 主导 → 结果应为正
        self.assertGreater(result, 0)
        self.assertAlmostEqual(result, 20.0, places=1)

    def test_f_contribution_smaller_than_t(self):
        """F 贡献 < T 贡献"""
        # 默认权重 T=0.6, F=0.25 → T 贡献更大
        # F=50, T=50, C=0 → 0.25*50 + 0.6*50 = 12.5 + 30 = 42.5
        default = combine_bias(50, 50, 0)
        self.assertAlmostEqual(default, 42.5, places=1)

    def test_one_decimal_precision(self):
        """保留 1 位小数"""
        # 0.6*10.333 = 6.2 → 1位小数
        result = combine_bias(10.333, 0, 0)
        # 验证只有 1 位小数
        self.assertEqual(result, round(result, 1))

    def test_none_cfg_uses_default(self):
        """cfg=None → 使用默认权重"""
        # 不传 cfg 与传 None 结果一致
        r1 = combine_bias(50, 30, 20)
        r2 = combine_bias(50, 30, 20, cfg=None)
        self.assertEqual(r1, r2)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(combine_bias(50, 30, 20), float)

    def test_weighted_sum_formula(self):
        """公式：wF*F + wT*T + wC*C（参数顺序 F, T, C）"""
        # F=20, T=40, C=10
        # 0.25*20 + 0.6*40 + 0.15*10 = 5 + 24 + 1.5 = 30.5
        result = combine_bias(20, 40, 10)
        self.assertAlmostEqual(result, 30.5, places=1)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  四维策略核心纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
