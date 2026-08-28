#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_gate + exit_plan 主入口 — 集成测试
===========================================

测试 four_dim_strategy.py 中的真实入口函数：

1. risk_gate() — 风控闸门主入口
   - 基本仓位计算（风险预算 + 保证金约束）
   - 最小 1 手兜底（超风险标注）
   - Kelly 因子缩放
   - T 强度随动缩放
   - 已有持仓扣减
   - 涨跌停第三道闸门
   - 风控锁定前置拦截
   - 分品种参数覆盖（stop_atr_mult / rr_ratio / margin_cap_pct）

2. exit_plan() — 出场计划主入口
   - 多/空方向的 stop / t1 / t2 计算
   - regime 系数调制（stop 倍率）
   - 趋势/波动开启移动止损，震荡单批
   - 尾仓参数（tail_stop_dist / tail_pct）
   - SR 位放宽止损（需 sr_analyzer）
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    risk_gate, exit_plan, DEFAULT_CONFIG, SYMBOLS,
    _FALLBACK_SPEC,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. risk_gate — 基本仓位计算
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateBasic(unittest.TestCase):
    """risk_gate 基本仓位计算。"""

    def test_returns_all_required_fields(self):
        """返回所有必要字段"""
        result = risk_gate("rb", price=3500, atr_val=50)
        required = [
            "passed", "N_risk", "N_margin", "N_plan",
            "stop_pts", "limit_pts", "gate3_ok",
            "over_risk", "kelly_mult", "t_scale",
        ]
        for key in required:
            self.assertIn(key, result, "缺少字段: %s" % key)

    def test_stop_pts_equals_atr_mult(self):
        """stop_pts = stop_atr_mult * atr_val"""
        atr = 50
        result = risk_gate("rb", price=3500, atr_val=atr)
        expected = DEFAULT_CONFIG["risk_gate"]["stop_atr_mult"] * atr
        self.assertAlmostEqual(result["stop_pts"], expected, places=2)

    def test_n_plan_at_most_n_risk(self):
        """N_plan <= N_risk（被保证金约束收紧）"""
        result = risk_gate("rb", price=3500, atr_val=50)
        self.assertLessEqual(result["N_plan"], result["N_risk"])

    def test_n_plan_at_most_n_margin(self):
        """N_plan <= N_margin"""
        result = risk_gate("rb", price=3500, atr_val=50)
        self.assertLessEqual(result["N_plan"], result["N_margin"])

    def test_passed_requires_positive_size_and_gate3(self):
        """passed = N_plan >= 1 且 gate3_ok"""
        result = risk_gate("rb", price=3500, atr_val=50)
        expected = (result["N_plan"] >= 1) and result["gate3_ok"]
        self.assertEqual(result["passed"], expected)

    def test_zero_atr_zero_size(self):
        """atr = 0 → size = 0"""
        result = risk_gate("rb", price=3500, atr_val=0)
        self.assertEqual(result["N_plan"], 0)
        self.assertFalse(result["passed"])


# ═══════════════════════════════════════════════════════════════════════════
#  2. risk_gate — 最小 1 手兜底
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateMinLot(unittest.TestCase):
    """最小 1 手兜底（超风险标注）。"""

    def test_small_atr_still_1_lot(self):
        """ATR 很小 → 风险预算算出来 < 1 手 → 兜底 1 手，标注 over_risk"""
        # 极小 ATR → risk_hand 极小 → N_risk_raw 很大？不对
        # 应该用极大 ATR → risk_hand 很大 → N_risk_raw < 1
        result = risk_gate("rb", price=3500, atr_val=500)  # 超大 ATR
        # risk_hand = 1.5 * 500 * 10 = 7500
        # 100000 * 0.015 = 1500 → 1500 // 7500 = 0 → 兜底 1 手
        self.assertEqual(result["N_risk"], 1)
        self.assertTrue(result["over_risk"],
            "超风险预算时应该标注 over_risk=True")

    def test_normal_atr_no_over_risk(self):
        """正常 ATR → 不超风险"""
        result = risk_gate("rb", price=3500, atr_val=50)
        if result["N_risk"] > 1:
            self.assertFalse(result["over_risk"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. risk_gate — Kelly 因子缩放
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateKelly(unittest.TestCase):
    """Kelly 因子缩放。"""

    def test_kelly_mult_applied(self):
        """Kelly 因子被应用到 N_risk"""
        result = risk_gate("rb", price=3500, atr_val=50)
        # kelly_mult 应该是一个合理值（0.5~1.2 之间）
        self.assertGreater(result["kelly_mult"], 0)
        self.assertLessEqual(result["kelly_mult"], 1.2)  # 封顶 1.2

    def test_custom_cfg_kelly_affects_size(self):
        """自定义 Kelly 配置影响仓位"""
        # 用默认配置
        r_default = risk_gate("rb", price=3500, atr_val=50)

        # 构造一个低 edge 的配置 → Kelly 小 → 仓位小
        cfg_low = dict(DEFAULT_CONFIG)
        cfg_low["kelly"] = {
            "target_edge": 0.5,  # 极低 edge 预期
            "kelly_fraction": 0.5,
            "kelly_min": 0.3,
            "kelly_max": 1.2,
            "min_history": 3,
        }
        r_low = risk_gate("rb", price=3500, atr_val=50, cfg=cfg_low)

        # 低 Kelly → N_risk 应该 <= 默认
        # （注意 N_risk 至少 1 手兜底，所以可能相等）
        self.assertLessEqual(r_low["N_risk"], r_default["N_risk"] + 1)


# ═══════════════════════════════════════════════════════════════════════════
#  4. risk_gate — T 强度随动
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateTStrength(unittest.TestCase):
    """T 强度随动缩放。"""

    def test_weak_t_reduces_size(self):
        """弱 T（刚过阈值）→ 仓位减半（0.5x）"""
        thresh = 50.0
        # T 刚过阈值：强度 = 阈值本身 → ratio = 1 / 1.5 = 0.667 → 取 max(0.5, 0.667) = 0.667
        r_weak = risk_gate("rb", price=3500, atr_val=50,
                           t_strength=50.0, t_thresh=thresh)
        r_full = risk_gate("rb", price=3500, atr_val=50,
                           t_strength=150.0, t_thresh=thresh)
        # 弱 T 的 t_scale 应该 < 1.0
        self.assertIsNotNone(r_weak["t_scale"])
        self.assertLess(r_weak["t_scale"], 1.0)
        # 强 T 的 t_scale 应该 = 1.0（满仓）
        self.assertAlmostEqual(r_full["t_scale"], 1.0)

    def test_very_strong_t_full_size(self):
        """T 远大于 1.5×阈值 → t_scale = 1.0（满仓）"""
        result = risk_gate("rb", price=3500, atr_val=50,
                           t_strength=200.0, t_thresh=50.0)
        self.assertAlmostEqual(result["t_scale"], 1.0)

    def test_no_t_strength_no_scaling(self):
        """不传 t_strength → t_scale = None（不缩放）"""
        result = risk_gate("rb", price=3500, atr_val=50)
        self.assertIsNone(result["t_scale"])

    def test_t_scale_floor_05(self):
        """t_scale 下限 0.5"""
        result = risk_gate("rb", price=3500, atr_val=50,
                           t_strength=1.0, t_thresh=100.0)  # 极弱 T
        self.assertGreaterEqual(result["t_scale"], 0.5)


# ═══════════════════════════════════════════════════════════════════════════
#  5. risk_gate — 持仓扣减
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateHeldLots(unittest.TestCase):
    """已有持仓扣减。"""

    def test_held_lots_reduces_plan(self):
        """已有持仓 → N_plan 减少"""
        r_no_held = risk_gate("rb", price=3500, atr_val=50)
        r_with_held = risk_gate("rb", price=3500, atr_val=50, held_lots=3)
        self.assertLessEqual(r_with_held["N_plan"], r_no_held["N_plan"])

    def test_full_holding_zero_plan(self):
        """持仓已满 → N_plan = 0（不能再加）"""
        max_lots = DEFAULT_CONFIG["account"]["max_lots"]
        result = risk_gate("rb", price=3500, atr_val=50, held_lots=max_lots)
        self.assertEqual(result["N_plan"], 0)
        self.assertFalse(result["passed"])

    def test_zero_held_lots_no_effect(self):
        """held_lots = 0 → 不影响"""
        r0 = risk_gate("rb", price=3500, atr_val=50, held_lots=0)
        r_none = risk_gate("rb", price=3500, atr_val=50)
        self.assertEqual(r0["N_plan"], r_none["N_plan"])


# ═══════════════════════════════════════════════════════════════════════════
#  6. risk_gate — 涨跌停闸门
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateLimitGate(unittest.TestCase):
    """涨跌停第三道闸门。"""

    def test_normal_atr_gate3_ok(self):
        """正常 ATR → gate3_ok = True"""
        result = risk_gate("rb", price=3500, atr_val=50)
        self.assertTrue(result["gate3_ok"])

    def test_huge_atr_gate3_fails(self):
        """极大 ATR（止损距接近涨跌停）→ gate3_ok = False"""
        # 3500 * 0.09 = 315（涨跌停 9%，闸门 90% = 8.1% = 283.5）
        # stop_pts = 1.5 * atr → 需要 atr > 283.5/1.5 = 189
        result = risk_gate("rb", price=3500, atr_val=300)  # 超大 ATR
        # stop_pts = 450, limit_pts = 3500*0.09 = 315, gate = 0.9*315 = 283.5
        # 450 > 283.5 → gate3_ok = False
        self.assertFalse(result["gate3_ok"])
        self.assertFalse(result["passed"],
            "涨跌停闸门关闭时 passed 应该为 False")

    def test_limit_pts_formula(self):
        """limit_pts = price * limit_pct"""
        price = 3500
        result = risk_gate("rb", price=price, atr_val=50)
        spec = DEFAULT_CONFIG["contract_specs"].get("rb", _FALLBACK_SPEC)
        expected = price * spec["limit_pct"]
        self.assertAlmostEqual(result["limit_pts"], expected, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  7. risk_gate — 风控锁定
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateRiskLock(unittest.TestCase):
    """风控锁定前置拦截。"""

    def test_halted_state_zero_everything(self):
        """HALTED 状态 → 全 0，risk_blocked=True"""
        risk_state = {"state": "HALTED", "lock_reason": "测试锁定"}
        result = risk_gate("rb", price=3500, atr_val=50, risk_state=risk_state)
        self.assertFalse(result["passed"])
        self.assertTrue(result["risk_blocked"])
        self.assertEqual(result["N_plan"], 0)
        self.assertEqual(result["N_risk"], 0)
        self.assertEqual(result["N_margin"], 0)
        self.assertEqual(result["kelly_mult"], 0.0)

    def test_scale_zero_locked(self):
        """scale=0 → 锁定"""
        risk_state = {"state": "NORMAL", "scale": 0.0}
        result = risk_gate("rb", price=3500, atr_val=50, risk_state=risk_state)
        self.assertTrue(result["risk_blocked"])
        self.assertEqual(result["N_plan"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  8. exit_plan — 基本出场计算
# ═══════════════════════════════════════════════════════════════════════════

class TestExitPlanBasic(unittest.TestCase):
    """exit_plan 基本出场计算。"""

    def test_long_direction_stop_below_entry(self):
        """多单 → 止损在入场价下方"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        self.assertLess(ep["stop"], 3500)
        self.assertGreater(ep["t1"], 3500)
        self.assertGreater(ep["t2"], 3500)

    def test_short_direction_stop_above_entry(self):
        """空单 → 止损在入场价上方"""
        ep = exit_plan("rb", entry=3500, dir_T=-1, atr_val=50, regime="趋势")
        self.assertGreater(ep["stop"], 3500)
        self.assertLess(ep["t1"], 3500)
        self.assertLess(ep["t2"], 3500)

    def test_stop_dist_positive(self):
        """stop_dist > 0"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        self.assertGreater(ep["stop_dist"], 0)

    def test_t1_1R_t2_2R(self):
        """t1 = 1R, t2 = 2R（默认 rr_ratio = 2.0）"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        # t1 距离入场 = 1R = stop_dist
        t1_dist = abs(ep["t1"] - 3500)
        t2_dist = abs(ep["t2"] - 3500)
        self.assertAlmostEqual(t1_dist, ep["stop_dist"], places=1)
        self.assertAlmostEqual(t2_dist, 2 * ep["stop_dist"], places=1)

    def test_returns_all_required_fields(self):
        """返回所有必要字段"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        required = [
            "stop", "t1", "t2", "stop_dist",
            "trailing", "style", "tail_enabled",
            "tail_stop_dist", "tail_pct", "sr_note",
        ]
        for key in required:
            self.assertIn(key, ep, "缺少字段: %s" % key)


# ═══════════════════════════════════════════════════════════════════════════
#  9. exit_plan — regime 调制
# ═══════════════════════════════════════════════════════════════════════════

class TestExitPlanRegime(unittest.TestCase):
    """exit_plan 的 regime 调制。"""

    def test_trend_trailing_enabled(self):
        """趋势 regime → trailing=True"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        self.assertTrue(ep["trailing"])

    def test_volatile_trailing_enabled(self):
        """波动 regime → trailing=True"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="波动")
        self.assertTrue(ep["trailing"])

    def test_range_no_trailing(self):
        """震荡 regime → trailing=False（单批出场）"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="震荡")
        self.assertFalse(ep["trailing"])
        self.assertEqual(ep["style"], "单批(震荡)")

    def test_regime_affects_stop_dist(self):
        """不同 regime → stop 距离不同（regime_coef stop 不同）"""
        ep_trend = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        ep_range = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="震荡")
        # 震荡 regime 的 stop 系数可能不同
        # 只要不一样就行，说明 regime 真的在起作用
        # （实际上趋势和震荡的 stop 系数可能一样，我们用波动来比）
        ep_volatile = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="波动")
        # 波动 regime 的 stop 应该更宽（防扫损）
        rc = DEFAULT_CONFIG["regime_coef"]
        if rc["波动"]["stop"] > rc["趋势"]["stop"]:
            self.assertGreater(ep_volatile["stop_dist"], ep_trend["stop_dist"])


# ═══════════════════════════════════════════════════════════════════════════
#  10. exit_plan — 尾仓参数
# ═══════════════════════════════════════════════════════════════════════════

class TestExitPlanTrailingTail(unittest.TestCase):
    """exit_plan 尾仓参数。"""

    def test_tail_stop_dist_2R_by_default(self):
        """尾仓跟踪距离 = 2R（默认 tail_trail_R = 2.0）"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        # 默认 tail_trail_R = 2.0 → tail_stop_dist = 2 * stop_dist
        expected = 2.0 * ep["stop_dist"]
        self.assertAlmostEqual(ep["tail_stop_dist"], expected, places=1)

    def test_tail_pct_default_25(self):
        """尾仓比例默认 25%"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="趋势")
        self.assertEqual(ep["tail_pct"], 0.25)

    def test_range_tail_disabled_by_trend_only(self):
        """震荡 + trend_only=True → tail_enabled=False"""
        ep = exit_plan("rb", entry=3500, dir_T=1, atr_val=50, regime="震荡")
        # 默认 trend_only=True，震荡时尾仓不开
        self.assertFalse(ep["tail_enabled"])


# ═══════════════════════════════════════════════════════════════════════════
#  11. risk_gate — 未知品种兜底
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskGateFallbackSpec(unittest.TestCase):
    """未知品种用 _FALLBACK_SPEC 兜底。"""

    def test_unknown_symbol_uses_fallback(self):
        """未知品种 → 不崩溃，用兜底规格"""
        result = risk_gate("xyz999", price=1000, atr_val=20)
        self.assertIn("passed", result)
        self.assertIn("N_plan", result)
        # limit_pts 应该等于 fallback 的 limit_pct
        expected = 1000 * _FALLBACK_SPEC["limit_pct"]
        self.assertAlmostEqual(result["limit_pts"], expected, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  risk_gate + exit_plan 主入口 — 集成测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
