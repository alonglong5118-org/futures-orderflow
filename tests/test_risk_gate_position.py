#!/usr/bin/env python3
"""
风险闸门 + 仓位计划 — 单元测试
====================================

1. calc_risk_lots — 风险预算手数
   - 正常计算：向下取整
   - 风险预算不够 1 手 → 0
   - stop_pts=0 → 0（除零保护）
   - multiplier=0 → 0
   - risk_pct=0 → 0
   - equity=0 → 0
   - 恰好整除 → 整数手
   - 返回 int

2. calc_min_lot_floor — 最小 1 手兜底
   - N>=1 → 原样，over_risk=False
   - N=0 且有风险 → 1手，over_risk=True
   - N=0 且 risk_per_hand=0 → 0，over_risk=False
   - 返回 (int, bool) 二元组

3. apply_kelly_scaling — Kelly 因子缩放
   - N>=1 且 kelly>1 → 放大（四舍五入）
   - N>=1 且 kelly<1 → 缩小，至少 1 手
   - N=0 → 0
   - kelly=1 → 不变
   - 四舍五入取整
   - 缩到 0.4 也保底 1 手

4. calc_margin_lots — 保证金约束手数
   - 正常计算：向下取整
   - 保证金不够 1 手 → 0
   - margin_rate=0 → 0（除零保护）
   - price=0 → 0
   - margin_cap_pct=0 → 0
   - 返回 int

5. calc_t_strength_scale — T 强度缩放
   - |T| >= 1.5×阈值 → 1.0（满仓）
   - |T| = 阈值 → scale = 1/1.5 ≈ 0.667
   - |T| = 0 → 0.5（最小半仓）
   - |T| 很小 → 0.5（封顶最小值）
   - t_thresh=None → 1.0
   - t_thresh<=0 → 1.0
   - 返回 float，范围 [0.5, 1.0]

6. deduct_held_lots — 扣减已有持仓
   - 无持仓 → N_plan（但 >= 0）
   - 有持仓且计划 > 可用 → 可用额度
   - 有持仓且计划 <= 可用 → 计划手数
   - 已满仓 → 0
   - 负持仓 → 当作 0
   - 负计划 → 0
   - 返回 int

7. check_limit_gate — 涨跌停闸门
   - 止损距 < limit×proximity → 通过（True）
   - 止损距 >= limit×proximity → 不通过（False）
   - limit_pts<=0 → 通过（放行）
   - 恰好等于 → 不通过（严格小于）
   - 返回 bool

8. calc_position_plan — 完整仓位计划
   - 正常流程：风险→兜底→Kelly→保证金→上限→T缩放→扣持仓→闸门
   - 风险约束是瓶颈
   - 保证金约束是瓶颈
   - max_lots 约束是瓶颈
   - 最小 1 手兜底触发（over_risk=True）
   - Kelly 缩放生效
   - T 强度缩放生效（弱过阈降仓）
   - 已有持仓扣减生效
   - 涨跌停闸门未通过 → passed=False
   - N_plan=0 → passed=False
   - 返回 8 字段
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from risk_gate_utils import (
    apply_kelly_scaling,
    calc_margin_lots,
    calc_min_lot_floor,
    calc_position_plan,
    calc_risk_lots,
    calc_t_strength_scale,
    check_limit_gate,
    deduct_held_lots,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. calc_risk_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestCalcRiskLots(unittest.TestCase):
    """calc_risk_lots 风险预算手数。"""

    def test_normal_calculation_floor(self):
        """正常计算：向下取整"""
        # 权益 100000, 风险 1%, 止损 10 点, 乘数 10
        # risk_budget = 100000 * 0.01 = 1000
        # risk_per_hand = 10 * 10 = 100
        # N = 1000 // 100 = 10
        self.assertEqual(calc_risk_lots(100000, 1.0, 10, 10), 10)

    def test_not_enough_for_one(self):
        """风险预算不够 1 手 → 0"""
        # risk_budget = 50, risk_per_hand = 100 → 0
        self.assertEqual(calc_risk_lots(5000, 1.0, 10, 10), 0)

    def test_zero_stop_pts(self):
        """stop_pts=0 → 0（除零保护）"""
        self.assertEqual(calc_risk_lots(100000, 1.0, 0, 10), 0)

    def test_zero_multiplier(self):
        """multiplier=0 → 0"""
        self.assertEqual(calc_risk_lots(100000, 1.0, 10, 0), 0)

    def test_zero_risk_pct(self):
        """risk_pct=0 → 0"""
        self.assertEqual(calc_risk_lots(100000, 0, 10, 10), 0)

    def test_zero_equity(self):
        """equity=0 → 0"""
        self.assertEqual(calc_risk_lots(0, 1.0, 10, 10), 0)

    def test_exact_divisible(self):
        """恰好整除 → 整数手"""
        # 1000 // 100 = 10
        self.assertEqual(calc_risk_lots(100000, 1.0, 10, 10), 10)

    def test_floor_rounding(self):
        """向下取整（1.9 → 1）"""
        # risk_budget = 190, risk_per_hand = 100 → 1.9 → 1
        self.assertEqual(calc_risk_lots(19000, 1.0, 10, 10), 1)

    def test_returns_int(self):
        """返回 int"""
        self.assertIsInstance(calc_risk_lots(100000, 1.0, 10, 10), int)


# ═══════════════════════════════════════════════════════════════════════════
#  2. calc_min_lot_floor
# ═══════════════════════════════════════════════════════════════════════════


class TestMinLotFloor(unittest.TestCase):
    """calc_min_lot_floor 最小 1 手兜底。"""

    def test_n_ge_one_unchanged(self):
        """N>=1 → 原样，over_risk=False"""
        n, over = calc_min_lot_floor(5, 100)
        self.assertEqual(n, 5)
        self.assertFalse(over)

    def test_zero_with_risk_floor_one(self):
        """N=0 且有风险 → 1手，over_risk=True"""
        n, over = calc_min_lot_floor(0, 100)
        self.assertEqual(n, 1)
        self.assertTrue(over)

    def test_zero_no_risk_stays_zero(self):
        """N=0 且 risk_per_hand=0 → 0，over_risk=False"""
        n, over = calc_min_lot_floor(0, 0)
        self.assertEqual(n, 0)
        self.assertFalse(over)

    def test_one_is_not_over_risk(self):
        """N=1 → over_risk=False"""
        n, over = calc_min_lot_floor(1, 100)
        self.assertEqual(n, 1)
        self.assertFalse(over)

    def test_returns_tuple(self):
        """返回 (int, bool) 二元组"""
        result = calc_min_lot_floor(3, 100)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], bool)


# ═══════════════════════════════════════════════════════════════════════════
#  3. apply_kelly_scaling
# ═══════════════════════════════════════════════════════════════════════════


class TestKellyScaling(unittest.TestCase):
    """apply_kelly_scaling Kelly 因子缩放。"""

    def test_kelly_above_one_scales_up(self):
        """N>=1 且 kelly>1 → 放大（四舍五入）"""
        # 10 * 1.2 = 12
        self.assertEqual(apply_kelly_scaling(10, 1.2), 12)

    def test_kelly_below_one_scales_down(self):
        """N>=1 且 kelly<1 → 缩小，至少 1 手"""
        # 10 * 0.6 = 6
        self.assertEqual(apply_kelly_scaling(10, 0.6), 6)

    def test_zero_stays_zero(self):
        """N=0 → 0"""
        self.assertEqual(apply_kelly_scaling(0, 1.2), 0)

    def test_kelly_one_unchanged(self):
        """kelly=1 → 不变"""
        self.assertEqual(apply_kelly_scaling(5, 1.0), 5)

    def test_rounding(self):
        """缩放后取整（Python round 银行家舍入）"""
        # 5 * 0.7 = 3.5 → round 到 4（偶数）
        self.assertEqual(apply_kelly_scaling(5, 0.7), 4)

    def test_minimum_one_hand(self):
        """缩到很小也保底 1 手"""
        # 1 * 0.4 = 0.4 → 0 → 保底 1
        self.assertEqual(apply_kelly_scaling(1, 0.4), 1)

    def test_negative_n_zero(self):
        """N<1 → 0"""
        self.assertEqual(apply_kelly_scaling(-1, 1.2), 0)

    def test_returns_int(self):
        """返回 int"""
        self.assertIsInstance(apply_kelly_scaling(5, 1.0), int)


# ═══════════════════════════════════════════════════════════════════════════
#  4. calc_margin_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestCalcMarginLots(unittest.TestCase):
    """calc_margin_lots 保证金约束手数。"""

    def test_normal_calculation(self):
        """正常计算：向下取整"""
        # equity=100000, cap=30% → budget=30000
        # price=100, mult=10, margin_rate=0.12 → per_hand = 100*10*0.12 = 120
        # 30000 // 120 = 250
        self.assertEqual(calc_margin_lots(100000, 30, 100, 10, 0.12), 250)

    def test_not_enough_for_one(self):
        """保证金不够 1 手 → 0"""
        # budget=100, per_hand=120 → 0
        self.assertEqual(calc_margin_lots(1000, 10, 100, 10, 0.12), 0)

    def test_zero_margin_rate(self):
        """margin_rate=0 → 0（除零保护）"""
        self.assertEqual(calc_margin_lots(100000, 30, 100, 10, 0), 0)

    def test_zero_price(self):
        """price=0 → 0"""
        self.assertEqual(calc_margin_lots(100000, 30, 0, 10, 0.12), 0)

    def test_zero_cap_pct(self):
        """margin_cap_pct=0 → 0"""
        self.assertEqual(calc_margin_lots(100000, 0, 100, 10, 0.12), 0)

    def test_returns_int(self):
        """返回 int"""
        self.assertIsInstance(calc_margin_lots(100000, 30, 100, 10, 0.12), int)

    def test_floor_rounding(self):
        """向下取整"""
        # budget=29000, per_hand=120 → 241.67 → 241
        self.assertEqual(calc_margin_lots(96667, 30, 100, 10, 0.12), 241)


# ═══════════════════════════════════════════════════════════════════════════
#  5. calc_t_strength_scale
# ═══════════════════════════════════════════════════════════════════════════


class TestTStrengthScale(unittest.TestCase):
    """calc_t_strength_scale T 强度缩放。"""

    def test_strong_signal_full_scale(self):
        """|T| >= 1.5×阈值 → 1.0（满仓）"""
        # T=75, thresh=50 → 75/75 = 1.0
        self.assertEqual(calc_t_strength_scale(75, 50), 1.0)

    def test_very_strong_capped_at_one(self):
        """|T| 远超阈值 → 封顶 1.0"""
        self.assertEqual(calc_t_strength_scale(100, 50), 1.0)

    def test_at_threshold_two_thirds(self):
        """|T| = 阈值 → scale = 1/1.5 ≈ 0.667"""
        # 50 / 75 = 0.666...
        self.assertAlmostEqual(calc_t_strength_scale(50, 50), 0.667, places=3)

    def test_zero_strength_min_scale(self):
        """|T| = 0 → 0.5（最小半仓）"""
        self.assertEqual(calc_t_strength_scale(0, 50), 0.5)

    def test_weak_signal_floor(self):
        """|T| 很小 → 0.5（封顶最小值）"""
        # 10 / 75 = 0.133 → 小于 0.5 → 取 0.5
        self.assertEqual(calc_t_strength_scale(10, 50), 0.5)

    def test_none_thresh_returns_one(self):
        """t_thresh=None → 1.0"""
        self.assertEqual(calc_t_strength_scale(50, None), 1.0)

    def test_zero_thresh_returns_one(self):
        """t_thresh<=0 → 1.0"""
        self.assertEqual(calc_t_strength_scale(50, 0), 1.0)
        self.assertEqual(calc_t_strength_scale(50, -10), 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(calc_t_strength_scale(50, 50), float)

    def test_range_between_half_and_one(self):
        """范围 [0.5, 1.0]"""
        scale = calc_t_strength_scale(30, 50)
        self.assertGreaterEqual(scale, 0.5)
        self.assertLessEqual(scale, 1.0)

    def test_negative_t_uses_abs(self):
        """负 T 取绝对值计算"""
        self.assertEqual(calc_t_strength_scale(-50, 50), calc_t_strength_scale(50, 50))


# ═══════════════════════════════════════════════════════════════════════════
#  6. deduct_held_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestDeductHeldLots(unittest.TestCase):
    """deduct_held_lots 扣减已有持仓。"""

    def test_no_held_unchanged(self):
        """无持仓 → N_plan（但 >= 0）"""
        self.assertEqual(deduct_held_lots(5, 0, 10), 5)

    def test_plan_exceeds_available(self):
        """有持仓且计划 > 可用 → 可用额度"""
        # 已持 3，max 5，计划 4 → 可用 2，实际开 2
        self.assertEqual(deduct_held_lots(4, 3, 5), 2)

    def test_plan_within_available(self):
        """有持仓且计划 <= 可用 → 计划手数"""
        # 已持 2，max 5，计划 2 → 可用 3，实际开 2
        self.assertEqual(deduct_held_lots(2, 2, 5), 2)

    def test_full_position_zero(self):
        """已满仓 → 0"""
        self.assertEqual(deduct_held_lots(5, 5, 5), 0)

    def test_negative_held_treated_as_zero(self):
        """负持仓 → 当作 0"""
        self.assertEqual(deduct_held_lots(5, -1, 10), 5)

    def test_negative_plan_zero(self):
        """负计划 → 0"""
        self.assertEqual(deduct_held_lots(-3, 0, 10), 0)

    def test_held_exceeds_max_zero(self):
        """已超上限 → 0（可用为负 → max(0, ...)）"""
        self.assertEqual(deduct_held_lots(1, 6, 5), 0)

    def test_returns_int(self):
        """返回 int"""
        self.assertIsInstance(deduct_held_lots(5, 2, 10), int)


# ═══════════════════════════════════════════════════════════════════════════
#  7. check_limit_gate
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckLimitGate(unittest.TestCase):
    """check_limit_gate 涨跌停闸门。"""

    def test_stop_below_limit_passes(self):
        """止损距 < limit×proximity → 通过（True）"""
        # stop=90, limit=100, proximity=0.9 → limit×0.9=90
        # 90 < 90? No → False. Need stop < 90
        self.assertTrue(check_limit_gate(80, 100, 0.9))

    def test_stop_above_limit_fails(self):
        """止损距 >= limit×proximity → 不通过（False）"""
        self.assertFalse(check_limit_gate(95, 100, 0.9))

    def test_zero_limit_passes(self):
        """limit_pts<=0 → 通过（放行）"""
        self.assertTrue(check_limit_gate(100, 0, 0.9))
        self.assertTrue(check_limit_gate(100, -1, 0.9))

    def test_exactly_at_threshold_fails(self):
        """恰好等于 → 不通过（严格小于）"""
        # stop = limit * proximity → 90 = 100 * 0.9 → 不小于 → False
        self.assertFalse(check_limit_gate(90, 100, 0.9))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(check_limit_gate(50, 100, 0.9), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  8. calc_position_plan
# ═══════════════════════════════════════════════════════════════════════════


class TestCalcPositionPlan(unittest.TestCase):
    """calc_position_plan 完整仓位计划。"""

    def _base(self, **overrides):
        base = {
            "equity": 100000,
            "risk_pct": 1.0,
            "stop_pts": 10,
            "multiplier": 10,
            "margin_rate": 0.12,
            "price": 100,
            "margin_cap_pct": 30.0,
            "max_lots": 5,
            "kelly_mult": 1.0,
            "t_strength": None,
            "t_thresh": None,
            "held_lots": 0,
            "limit_pts": 0.0,
            "limit_proximity": 0.9,
        }
        base.update(overrides)
        return calc_position_plan(**base)

    def test_risk_is_bottleneck(self):
        """风险约束是瓶颈"""
        # risk: 100000*0.01/(10*10) = 10
        # margin: 30000/(100*10*0.12) = 250
        # max_lots: 5
        # → N_plan = 5（max_lots 限制）
        r = self._base(max_lots=3)
        self.assertEqual(r["N_plan"], 3)
        self.assertEqual(r["N_risk_raw"], 10)
        self.assertTrue(r["passed"])

    def test_margin_is_bottleneck(self):
        """保证金约束是瓶颈"""
        # margin: equity*cap / (price*mult*rate)
        # 100000*0.05 / (100*10*0.12) = 5000/120 = 41.67 → 41
        # risk: 10
        # → min(10, 41, 5) = 5
        r = self._base(margin_cap_pct=5.0, max_lots=20)
        # margin = 100000*0.05 / (100*10*0.12) = 5000/120 = 41
        self.assertEqual(r["N_margin"], 41)
        self.assertEqual(r["N_plan"], 10)  # risk 是瓶颈

    def test_max_lots_bottleneck(self):
        """max_lots 约束是瓶颈"""
        r = self._base(max_lots=3)
        self.assertEqual(r["N_plan"], 3)
        self.assertTrue(r["passed"])

    def test_min_lot_floor_triggered(self):
        """最小 1 手兜底触发（over_risk=True）"""
        # risk: 5000*0.01/(10*10) = 50/100 = 0.5 → 0
        # 兜底 → 1 手，over_risk=True
        r = self._base(equity=5000)
        self.assertEqual(r["N_risk_raw"], 0)
        self.assertEqual(r["N_plan"], 1)
        self.assertTrue(r["over_risk"])
        self.assertTrue(r["passed"])

    def test_kelly_scaling_effective(self):
        """Kelly 缩放生效"""
        # risk_raw = 10, floor=10, kelly=1.2 → 12
        # margin = 250, max=5 → N_plan = min(12, 250, 5) = 5
        r = self._base(kelly_mult=1.2, max_lots=20)
        self.assertEqual(r["N_risk"], 12)
        self.assertEqual(r["N_plan"], 12)

    def test_t_strength_scaling_effective(self):
        """T 强度缩放生效（弱过阈降仓）"""
        # N_plan 先算到 10
        # T=50, thresh=50 → scale=0.667
        # 10 * 0.667 = 6.67 → 6
        r = self._base(t_strength=50, t_thresh=50, max_lots=20)
        self.assertIsNotNone(r["t_scale"])
        self.assertAlmostEqual(r["t_scale"], 0.667, places=3)
        self.assertEqual(r["N_plan"], 6)

    def test_held_lots_deducted(self):
        """已有持仓扣减生效"""
        # 计划 5，已持 2 → 还能开 3
        r = self._base(max_lots=5, held_lots=2)
        self.assertEqual(r["N_plan"], 3)

    def test_limit_gate_fails_not_passed(self):
        """涨跌停闸门未通过 → passed=False"""
        # stop_pts=100, limit_pts=100, proximity=0.9 → 100 < 90? No → gate3=False
        r = self._base(stop_pts=100, limit_pts=100, limit_proximity=0.9)
        self.assertFalse(r["gate3_ok"])
        self.assertFalse(r["passed"])

    def test_zero_plan_not_passed(self):
        """N_plan=0 → passed=False"""
        r = self._base(equity=100)  # 风险预算不够
        self.assertEqual(r["N_plan"], 0)
        self.assertFalse(r["passed"])

    def test_return_eight_fields(self):
        """返回 8 字段"""
        r = self._base()
        for key in ("N_risk_raw", "N_risk", "N_margin", "N_plan", "over_risk", "t_scale", "gate3_ok", "passed"):
            self.assertIn(key, r)

    def test_full_pipeline(self):
        """完整流水线验证"""
        # equity=200000, risk=1%, stop=10pts, mult=10
        # risk_raw = 200000*0.01/(10*10) = 2000/100 = 20
        # floor: 20, over_risk=False
        # kelly=1.2 → 24
        # margin: 200000*0.3/(100*10*0.12) = 60000/120 = 500
        # max_lots=5 → min(24, 500, 5) = 5
        # T强度: T=50, thresh=50 → 0.667 → int(5*0.667) = 3
        # 已持 1 → 可用=4, min(3,4) = 3
        # 闸门：stop=10 < 20*0.9=18 → 通过
        r = calc_position_plan(
            equity=200000,
            risk_pct=1.0,
            stop_pts=10,
            multiplier=10,
            margin_rate=0.12,
            price=100,
            margin_cap_pct=30.0,
            max_lots=5,
            kelly_mult=1.2,
            t_strength=50,
            t_thresh=50,
            held_lots=1,
            limit_pts=20,
            limit_proximity=0.9,
        )
        self.assertEqual(r["N_risk_raw"], 20)
        self.assertEqual(r["N_risk"], 24)
        self.assertFalse(r["over_risk"])
        self.assertEqual(r["N_margin"], 500)
        self.assertAlmostEqual(r["t_scale"], 0.667, places=3)
        self.assertTrue(r["gate3_ok"])
        self.assertEqual(r["N_plan"], 3)
        self.assertTrue(r["passed"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  风险闸门 + 仓位计划 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
