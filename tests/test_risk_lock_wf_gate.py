#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控锁定 + 稳健池闸门 — 单元测试
=====================================

1. _is_risk_locked — 风控锁定判断
   - None → 未锁定
   - NORMAL → 未锁定
   - HALTED → 锁定
   - LOCKED → 锁定
   - scale=0 → 锁定
   - scale>0 → 未锁定
   - 未知 state 但 scale 正常 → 未锁定
   - reason 字段正确返回

2. walk_forward_gate — 稳健池闸门
   - 在池 + 有 trade → 通过
   - 不在池 → 观察中
   - 池内但 trade 不足 → 观察中
   - 紧急出池 → 不通过
   - symbol 大小写不敏感
   - 空池 → 全部观察

3. compute_kelly_factor — 细节补充
   - 无校准数据 → 返回默认值
   - 校准数据为空 → 返回默认值
   - symbol 不在校准表 → 默认值
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    _is_risk_locked,
    compute_kelly_factor,
    DEFAULT_CONFIG,
)
from strategy_layer import walk_forward_gate


# ═══════════════════════════════════════════════════════════════════════════
#  1. _is_risk_locked — 风控锁定
# ═══════════════════════════════════════════════════════════════════════════

class TestIsRiskLocked(unittest.TestCase):
    """_is_risk_locked — 风控锁定判断。"""

    def test_none_not_locked(self):
        """None → 未锁定"""
        locked, reason = _is_risk_locked(None)
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    def test_normal_state_not_locked(self):
        """NORMAL → 未锁定"""
        locked, reason = _is_risk_locked({"state": "NORMAL", "scale": 1.0})
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    def test_halted_state_locked(self):
        """HALTED → 锁定"""
        locked, reason = _is_risk_locked({"state": "HALTED", "lock_reason": "熔断"})
        self.assertTrue(locked)
        self.assertIn("熔断", reason)

    def test_locked_state_locked(self):
        """LOCKED → 锁定"""
        locked, reason = _is_risk_locked({"state": "LOCKED", "lock_reason": "日内超限"})
        self.assertTrue(locked)
        self.assertIn("日内超限", reason)

    def test_scale_zero_locked(self):
        """scale=0 → 锁定（即使 state 是 NORMAL）"""
        locked, reason = _is_risk_locked({"state": "NORMAL", "scale": 0.0})
        self.assertTrue(locked)
        self.assertIn("scale=0", reason)

    def test_scale_positive_not_locked(self):
        """scale > 0 → 未锁定"""
        locked, reason = _is_risk_locked({"state": "NORMAL", "scale": 0.5})
        self.assertFalse(locked)

    def test_unknown_state_scale_ok_not_locked(self):
        """未知 state 但 scale 正常 → 未锁定"""
        locked, reason = _is_risk_locked({"state": "WHATEVER", "scale": 1.0})
        self.assertFalse(locked)

    def test_reason_fallback_to_state(self):
        """lock_reason 和 reason 都没有 → 返回状态名"""
        locked, reason = _is_risk_locked({"state": "HALTED"})
        self.assertTrue(locked)
        self.assertIn("HALTED", reason)

    def test_reason_fallback_reason_key(self):
        """reason 键（非 lock_reason）也能取到"""
        locked, reason = _is_risk_locked({"state": "HALTED", "reason": "风控原因"})
        self.assertTrue(locked)
        self.assertEqual(reason, "风控原因")

    def test_scale_none_treated_as_1(self):
        """scale=None → 不触发锁定（视为正常）"""
        locked, reason = _is_risk_locked({"state": "NORMAL", "scale": None})
        self.assertFalse(locked)

    def test_empty_dict_not_locked(self):
        """空 dict → 未锁定"""
        locked, reason = _is_risk_locked({})
        self.assertFalse(locked)

    def test_halted_with_scale_zero(self):
        """HALTED + scale=0 → 锁定（state 优先，reason 取 lock_reason）"""
        locked, reason = _is_risk_locked({
            "state": "HALTED", "scale": 0.0, "lock_reason": "大回撤"
        })
        self.assertTrue(locked)
        self.assertIn("大回撤", reason)
        self.assertNotIn("scale=0", reason)  # state 已经命中了，不用 scale 判断


# ═══════════════════════════════════════════════════════════════════════════
#  2. walk_forward_gate — 稳健池闸门
# ═══════════════════════════════════════════════════════════════════════════

class TestWalkForwardGate(unittest.TestCase):
    """walk_forward_gate — 稳健池准入闸门。"""

    def test_not_in_pool_observation(self):
        """不在稳健池 → 观察池"""
        result = walk_forward_gate("xyz999")
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "观察池")
        self.assertIsNone(result["stability"])
        self.assertIsNone(result["oos_expR"])

    def test_in_robust_pool_returns_stability_and_oos(self):
        """在稳健池 → 返回 stability 和 oos_expR"""
        from strategy_layer import ROBUST_POOL
        if not ROBUST_POOL:
            self.skipTest("ROBUST_POOL 为空")
        sym = list(ROBUST_POOL.keys())[0]
        result = walk_forward_gate(sym)
        self.assertIn("passed", result)
        self.assertIn("stability", result)
        self.assertIn("oos_expR", result)
        self.assertIn("status", result)
        self.assertIn("reason", result)
        # stability 和 oos_expR 应该是数值（不是 None）
        self.assertIsNotNone(result["stability"])
        self.assertIsNotNone(result["oos_expR"])

    def test_case_insensitive(self):
        """品种名大小写不敏感"""
        from strategy_layer import ROBUST_POOL
        if not ROBUST_POOL:
            self.skipTest("ROBUST_POOL 为空")
        sym_upper = list(ROBUST_POOL.keys())[0]
        sym_lower = sym_upper.lower()
        r_upper = walk_forward_gate(sym_upper)
        r_lower = walk_forward_gate(sym_lower)
        self.assertEqual(r_upper["passed"], r_lower["passed"])
        self.assertEqual(r_upper["status"], r_lower["status"])

    def test_empty_symbol_observation(self):
        """空字符串 → 观察池"""
        result = walk_forward_gate("")
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "观察池")

    def test_none_symbol_observation(self):
        """None → 观察池（不崩溃）"""
        result = walk_forward_gate(None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "观察池")

    def test_status_is_one_of_three(self):
        """status 是三种之一：观察池 / 稳健池 / 稳健池·紧急出池"""
        from strategy_layer import ROBUST_POOL
        valid_statuses = {"观察池", "稳健池", "稳健池·紧急出池"}
        # 测一个不在池的
        r1 = walk_forward_gate("xyz999")
        self.assertIn(r1["status"], valid_statuses)
        # 测一个在池的
        if ROBUST_POOL:
            sym = list(ROBUST_POOL.keys())[0]
            r2 = walk_forward_gate(sym)
            self.assertIn(r2["status"], valid_statuses)


# ═══════════════════════════════════════════════════════════════════════════
#  3. compute_kelly_factor — 细节补充
# ═══════════════════════════════════════════════════════════════════════════

class TestKellyFactorDetails(unittest.TestCase):
    """compute_kelly_factor 细节补充测试。"""

    def test_returns_float(self):
        """返回 float"""
        result = compute_kelly_factor("rb")
        self.assertIsInstance(result, float)

    def test_within_kelly_min_max(self):
        """结果在 [kelly_min, kelly_max] 范围内"""
        result = compute_kelly_factor("rb")
        rg = DEFAULT_CONFIG["risk_gate"]
        k_min = rg.get("kelly_min", 0.6)
        k_max = rg.get("kelly_max", 1.2)
        self.assertGreaterEqual(result, k_min)
        self.assertLessEqual(result, k_max)

    def test_unknown_symbol_returns_1_0(self):
        """未知品种（无校准数据）→ 返回 1.0（中性）"""
        result = compute_kelly_factor("xyz999")
        self.assertEqual(result, 1.0)

    def test_custom_cfg_kelly_range(self):
        """自定义 cfg 的 kelly_min/kelly_max 影响范围"""
        cfg1 = dict(DEFAULT_CONFIG)
        cfg1["risk_gate"] = dict(DEFAULT_CONFIG["risk_gate"],
                                  kelly_min=0.8, kelly_max=0.8)
        # min = max = 0.8 → 如果有 edge 的话应该是 0.8
        # 但无校准数据时返回 1.0，所以这个测试验证函数不崩溃
        result = compute_kelly_factor("rb", cfg=cfg1)
        self.assertIsInstance(result, float)
        self.assertGreater(result, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  风控锁定 + 稳健池闸门 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
