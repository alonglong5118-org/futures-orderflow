#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风控仓位计算 — 单元测试
=========================

覆盖 risk_gate_utils 中的 8 个纯函数 + 1 个集成函数：
  1. calc_risk_lots           — 风险预算手数
  2. calc_min_lot_floor       — 最小 1 手兜底
  3. apply_kelly_scaling      — Kelly 因子缩放
  4. calc_margin_lots          — 保证金约束手数
  5. calc_t_strength_scale    — T 强度缩放系数
  6. deduct_held_lots         — 已有持仓扣减
  7. check_limit_gate         — 涨跌停闸门
  8. calc_position_plan       — 完整仓位计划（集成）

历史 bug / 决策覆盖：
  - P1-4：fractional-Kelly 缩放（0.6~1.2x，原 1.6x 过度杠杆）
  - P2b：同品种持仓扣减（加仓不超配）
  - T 强度随动：弱过阈降仓，|T|≥1.5×阈值满仓
  - 分品种保证金上限收紧
  - 涨跌停闸门（第三道防线）
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
#  1. 风险预算手数
# ═══════════════════════════════════════════════════════════════════════════

class TestCalcRiskLots(unittest.TestCase):
    """风险预算手数计算。"""

    def test_basic_calculation(self):
        """基础计算：权益 10 万，风险 1.5%，止损 15 点，乘数 10 → 10 手"""
        # 每手风险 = 15 * 10 = 150 元
        # 风险预算 = 100000 * 1.5% = 1500 元
        # 手数 = 1500 // 150 = 10 手
        N = calc_risk_lots(equity=100000, risk_pct=1.5, stop_pts=15, multiplier=10)
        self.assertEqual(N, 10)

    def test_rounds_down(self):
        """向下取整：风险预算不够整数手时舍去"""
        # 每手风险 = 15 * 10 = 150
        # 风险预算 = 100000 * 1.6% = 1600
        # 1600 // 150 = 10.666 → 10 手
        N = calc_risk_lots(equity=100000, risk_pct=1.6, stop_pts=15, multiplier=10)
        self.assertEqual(N, 10)

    def test_zero_risk_pct(self):
        """风险比例为 0 → 0 手"""
        N = calc_risk_lots(equity=100000, risk_pct=0, stop_pts=15, multiplier=10)
        self.assertEqual(N, 0)

    def test_zero_stop_pts(self):
        """止损为 0 → 0 手（防止除零）"""
        N = calc_risk_lots(equity=100000, risk_pct=1.5, stop_pts=0, multiplier=10)
        self.assertEqual(N, 0)

    def test_zero_multiplier(self):
        """乘数为 0 → 0 手"""
        N = calc_risk_lots(equity=100000, risk_pct=1.5, stop_pts=15, multiplier=0)
        self.assertEqual(N, 0)

    def test_small_budget_less_than_one(self):
        """风险预算不够 1 手 → 0 手"""
        # 每手风险 = 100 * 10 = 1000
        # 风险预算 = 50000 * 1% = 500
        # 500 // 1000 = 0
        N = calc_risk_lots(equity=50000, risk_pct=1.0, stop_pts=100, multiplier=10)
        self.assertEqual(N, 0)

    def test_large_equity_many_lots(self):
        """大权益 → 多手"""
        # 每手风险 = 10 * 10 = 100
        # 风险预算 = 1000000 * 2% = 20000
        # 20000 // 100 = 200 手
        N = calc_risk_lots(equity=1_000_000, risk_pct=2.0, stop_pts=10, multiplier=10)
        self.assertEqual(N, 200)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 最小 1 手兜底
# ═══════════════════════════════════════════════════════════════════════════

class TestMinLotFloor(unittest.TestCase):
    """最小 1 手兜底（超风险标注）。"""

    def test_zero_raw_floors_to_1(self):
        """N_risk_raw=0 + 有风险 → 兜底 1 手，标注超风险"""
        N, over = calc_min_lot_floor(0, risk_per_hand=150)
        self.assertEqual(N, 1)
        self.assertTrue(over)

    def test_negative_raw_floors_to_1(self):
        """N_risk_raw 为负 → 兜底 1 手（理论上不会有负的，但防御性处理）"""
        N, over = calc_min_lot_floor(-5, risk_per_hand=150)
        self.assertEqual(N, 1)
        self.assertTrue(over)

    def test_positive_raw_stays(self):
        """N_risk_raw >= 1 → 不变，不超风险"""
        N, over = calc_min_lot_floor(5, risk_per_hand=150)
        self.assertEqual(N, 5)
        self.assertFalse(over)

    def test_exactly_1_stays(self):
        """N_risk_raw = 1 → 不变，不超风险"""
        N, over = calc_min_lot_floor(1, risk_per_hand=150)
        self.assertEqual(N, 1)
        self.assertFalse(over)

    def test_zero_risk_per_hand_no_floor(self):
        """risk_per_hand = 0 → 不兜底（无风险意义）"""
        N, over = calc_min_lot_floor(0, risk_per_hand=0)
        self.assertEqual(N, 0)
        self.assertFalse(over)


# ═══════════════════════════════════════════════════════════════════════════
#  3. Kelly 缩放
# ═══════════════════════════════════════════════════════════════════════════

class TestKellyScaling(unittest.TestCase):
    """Kelly 因子缩放。"""

    def test_mult_1_no_change(self):
        """kelly_mult = 1.0 → 手数不变"""
        self.assertEqual(apply_kelly_scaling(10, 1.0), 10)

    def test_mult_above_1_increases(self):
        """kelly_mult > 1 → 手数增加"""
        # 10 * 1.2 = 12
        self.assertEqual(apply_kelly_scaling(10, 1.2), 12)

    def test_mult_below_1_decreases(self):
        """kelly_mult < 1 → 手数减少"""
        # 10 * 0.6 = 6
        self.assertEqual(apply_kelly_scaling(10, 0.6), 6)

    def test_min_1_hand_guarantee(self):
        """N_risk >= 1 → 缩放后至少 1 手（不把正数缩成 0）"""
        # 1 * 0.6 = 0.6 → round=1 → max(1, 1) = 1
        result = apply_kelly_scaling(1, 0.6)
        self.assertGreaterEqual(result, 1)
        self.assertEqual(result, 1)

    def test_zero_stays_zero(self):
        """N_risk = 0 → 保持 0（没预算就不开仓）"""
        self.assertEqual(apply_kelly_scaling(0, 1.2), 0)

    def test_negative_stays_zero(self):
        """N_risk < 0 → 返回 0（防御性）"""
        self.assertEqual(apply_kelly_scaling(-3, 1.0), 0)

    def test_rounds_to_nearest_integer(self):
        """缩放后四舍五入"""
        # 10 * 0.75 = 7.5 → round = 8
        self.assertEqual(apply_kelly_scaling(10, 0.75), 8)

    def test_kelly_p14_regression_max_1_2(self):
        """回归（P1-4）：kelly_max=1.2 时，最高只加 20% 杠杆

        历史 bug：原公式 kelly 可达 1.6x，弱/中置信品种过度杠杆。
        修复后封顶 1.2x。
        """
        # 10 手 × 1.2 = 12 手（不是 16 手）
        result = apply_kelly_scaling(10, 1.2)
        self.assertEqual(result, 12)
        # 验证确实不是旧公式的 1.6x
        self.assertLess(result, 16)


# ═══════════════════════════════════════════════════════════════════════════
#  4. 保证金约束
# ═══════════════════════════════════════════════════════════════════════════

class TestCalcMarginLots(unittest.TestCase):
    """保证金约束手数计算。"""

    def test_basic_calculation(self):
        """基础计算：权益 10 万，保证金上限 30%，价格 1000，乘数 10，保证金率 12%"""
        # 每手保证金 = 1000 * 10 * 0.12 = 1200 元
        # 保证金预算 = 100000 * 30% = 30000 元
        # 手数 = 30000 // 1200 = 25 手
        N = calc_margin_lots(equity=100000, margin_cap_pct=30,
                             price=1000, multiplier=10, margin_rate=0.12)
        self.assertEqual(N, 25)

    def test_rounds_down(self):
        """向下取整"""
        # 每手保证金 = 1000 * 10 * 0.12 = 1200
        # 预算 = 100000 * 30.5% = 30500
        # 30500 // 1200 = 25.416 → 25
        N = calc_margin_lots(equity=100000, margin_cap_pct=30.5,
                             price=1000, multiplier=10, margin_rate=0.12)
        self.assertEqual(N, 25)

    def test_zero_price(self):
        """价格为 0 → 0 手"""
        N = calc_margin_lots(equity=100000, margin_cap_pct=30,
                             price=0, multiplier=10, margin_rate=0.12)
        self.assertEqual(N, 0)

    def test_zero_margin_rate(self):
        """保证金率为 0 → 0 手"""
        N = calc_margin_lots(equity=100000, margin_cap_pct=30,
                             price=1000, multiplier=10, margin_rate=0)
        self.assertEqual(N, 0)

    def test_zero_multiplier(self):
        """乘数为 0 → 0 手"""
        N = calc_margin_lots(equity=100000, margin_cap_pct=30,
                             price=1000, multiplier=0, margin_rate=0.12)
        self.assertEqual(N, 0)

    def test_tight_margin_cap_fewer_lots(self):
        """更紧的保证金上限 → 手数更少"""
        # 30% → 25 手；15% → 12 手（减半）
        N_tight = calc_margin_lots(equity=100000, margin_cap_pct=15,
                                   price=1000, multiplier=10, margin_rate=0.12)
        N_normal = calc_margin_lots(equity=100000, margin_cap_pct=30,
                                    price=1000, multiplier=10, margin_rate=0.12)
        self.assertLess(N_tight, N_normal)
        self.assertEqual(N_tight, 12)

    def test_per_symbol_margin_cap_jm(self):
        """回归：JM 焦煤低胜率品种保证金收紧到 18% → 手数更少"""
        # 默认 30% vs 收紧 18%
        N_default = calc_margin_lots(equity=100000, margin_cap_pct=30,
                                     price=2000, multiplier=60, margin_rate=0.12)
        N_tight = calc_margin_lots(equity=100000, margin_cap_pct=18,
                                   price=2000, multiplier=60, margin_rate=0.12)
        self.assertLess(N_tight, N_default,
                        "低胜率品种保证金收紧后，手数应该更少")
        # 比例应该大约是 18/30 = 0.6（向下取整后近似）
        self.assertAlmostEqual(N_tight / N_default, 18/30, places=0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. T 强度缩放
# ═══════════════════════════════════════════════════════════════════════════

class TestTStrengthScale(unittest.TestCase):
    """T 强度缩放系数（弱过阈降仓）。"""

    def test_at_threshold_gives_2_3(self):
        """|T| = 阈值 → scale = 1/1.5 ≈ 0.667"""
        scale = calc_t_strength_scale(t_strength=70, t_thresh=70)
        # 70 / (70 * 1.5) = 1/1.5 ≈ 0.667
        self.assertAlmostEqual(scale, 1 / 1.5, places=3)
        self.assertGreater(scale, 0.5)
        self.assertLess(scale, 1.0)

    def test_1_5x_threshold_gives_full(self):
        """|T| = 1.5 × 阈值 → scale = 1.0（满仓）"""
        scale = calc_t_strength_scale(t_strength=105, t_thresh=70)
        # 105 / (70 * 1.5) = 105 / 105 = 1.0
        self.assertAlmostEqual(scale, 1.0, places=3)

    def test_above_1_5x_capped_at_1(self):
        """|T| > 1.5 × 阈值 → 封顶 1.0"""
        scale = calc_t_strength_scale(t_strength=150, t_thresh=70)
        self.assertEqual(scale, 1.0)

    def test_very_small_t_floored_at_0_5(self):
        """|T| 很小 → 保底 0.5（半仓）"""
        scale = calc_t_strength_scale(t_strength=10, t_thresh=70)
        # 10 / 105 ≈ 0.095 → 低于 0.5 → 保底 0.5
        self.assertEqual(scale, 0.5)

    def test_at_0_75_threshold(self):
        """|T| = 0.75 × 阈值 → scale = 0.5"""
        # 0.75 / 1.5 = 0.5 → 恰好等于下限
        scale = calc_t_strength_scale(t_strength=52.5, t_thresh=70)
        # 52.5 / 105 = 0.5
        self.assertAlmostEqual(scale, 0.5, places=3)

    def test_zero_threshold_returns_1(self):
        """阈值为 0 → 返回 1.0（不缩放，异常保护）"""
        scale = calc_t_strength_scale(t_strength=50, t_thresh=0)
        self.assertEqual(scale, 1.0)

    def test_negative_threshold_returns_1(self):
        """阈值为负 → 返回 1.0"""
        scale = calc_t_strength_scale(t_strength=50, t_thresh=-10)
        self.assertEqual(scale, 1.0)

    def test_negative_T_same_as_positive(self):
        """T 为负 → 取绝对值，结果相同"""
        scale_pos = calc_t_strength_scale(t_strength=80, t_thresh=70)
        scale_neg = calc_t_strength_scale(t_strength=-80, t_thresh=70)
        self.assertAlmostEqual(scale_pos, scale_neg, places=10)

    def test_zero_T_floored_at_0_5(self):
        """T = 0 → 保底 0.5"""
        scale = calc_t_strength_scale(t_strength=0, t_thresh=70)
        self.assertEqual(scale, 0.5)

    def test_monotonically_increasing(self):
        """缩放系数随 |T| 增大而非递减"""
        prev = 0.0
        for t in [10, 30, 50, 70, 90, 110, 130, 150]:
            scale = calc_t_strength_scale(t_strength=t, t_thresh=70)
            self.assertGreaterEqual(scale, prev, f"t={t} 时 scale 下降了")
            prev = scale


# ═══════════════════════════════════════════════════════════════════════════
#  6. 持仓扣减
# ═══════════════════════════════════════════════════════════════════════════

class TestDeductHeldLots(unittest.TestCase):
    """已有持仓扣减（加仓不超配）。"""

    def test_no_held_no_change(self):
        """无持仓 → 不变"""
        N = deduct_held_lots(N_plan=5, held_lots=0, max_lots=10)
        self.assertEqual(N, 5)

    def test_partial_held_reduces_plan(self):
        """有部分持仓 → 扣减"""
        # max=10, 已有 3 → 还能加 7 → 计划 5 在范围内 → 5
        N = deduct_held_lots(N_plan=5, held_lots=3, max_lots=10)
        self.assertEqual(N, 5)

    def test_held_near_max_caps_plan(self):
        """持仓接近上限 → 计划被限制"""
        # max=10, 已有 8 → 还能加 2 → 计划 5 被压到 2
        N = deduct_held_lots(N_plan=5, held_lots=8, max_lots=10)
        self.assertEqual(N, 2)

    def test_held_at_max_zero_plan(self):
        """已满仓 → 计划为 0"""
        N = deduct_held_lots(N_plan=5, held_lots=10, max_lots=10)
        self.assertEqual(N, 0)

    def test_held_above_max_zero_plan(self):
        """持仓超限（可能是手动加的）→ 计划为 0"""
        N = deduct_held_lots(N_plan=5, held_lots=12, max_lots=10)
        self.assertEqual(N, 0)

    def test_negative_plan_returns_zero(self):
        """N_plan 为负 → 返回 0（防御性）"""
        N = deduct_held_lots(N_plan=-3, held_lots=0, max_lots=10)
        self.assertEqual(N, 0)

    def test_negative_held_ignored(self):
        """held_lots 为负 → 当作 0（防御性）"""
        N = deduct_held_lots(N_plan=5, held_lots=-2, max_lots=10)
        self.assertEqual(N, 5)

    def test_p2b_regression_no_double_counting(self):
        """回归（P2b）：加仓时必须扣减已有持仓，不能重复计算

        历史问题：加仓时忘记扣减已有持仓，导致单品种总持仓超过 max_lots。
        """
        max_lots = 5
        held = 3
        plan = 5  # 想加 5 手
        result = deduct_held_lots(plan, held, max_lots)
        # 总持仓不能超过 5，已有 3 → 最多加 2
        self.assertLessEqual(result + held, max_lots,
            "P2b 回归 bug：加仓后总持仓超过了 max_lots")
        self.assertEqual(result, 2)


# ═══════════════════════════════════════════════════════════════════════════
#  7. 涨跌停闸门
# ═══════════════════════════════════════════════════════════════════════════

class TestLimitGate(unittest.TestCase):
    """涨跌停闸门（第三道防线）。"""

    def test_stop_much_smaller_than_limit_ok(self):
        """止损远小于涨跌停 → 通过"""
        ok = check_limit_gate(stop_pts=10, limit_pts=100, limit_proximity=0.9)
        self.assertTrue(ok)

    def test_stop_above_limit_fails(self):
        """止损超过涨跌停 → 不通过"""
        ok = check_limit_gate(stop_pts=95, limit_pts=100, limit_proximity=0.9)
        # 95 >= 100 * 0.9 = 90 → 不通过
        self.assertFalse(ok)

    def test_stop_at_proximity_boundary_fails(self):
        """止损恰好等于 limit × proximity → 不通过（严格小于）"""
        ok = check_limit_gate(stop_pts=90, limit_pts=100, limit_proximity=0.9)
        # 90 < 90? 不 → 不通过
        self.assertFalse(ok, "必须严格小于，等于也不通过")

    def test_stop_just_below_proximity_ok(self):
        """止损略小于 proximity 边界 → 通过"""
        ok = check_limit_gate(stop_pts=89.9, limit_pts=100, limit_proximity=0.9)
        self.assertTrue(ok)

    def test_zero_limit_always_ok(self):
        """涨跌停为 0 → 放行（无数据不卡）"""
        ok = check_limit_gate(stop_pts=1000, limit_pts=0)
        self.assertTrue(ok)

    def test_negative_limit_always_ok(self):
        """涨跌停为负 → 放行（异常数据）"""
        ok = check_limit_gate(stop_pts=100, limit_pts=-50)
        self.assertTrue(ok)

    def test_proximity_1_means_stop_must_be_smaller(self):
        """proximity = 1.0 → 止损必须严格小于涨跌停"""
        ok = check_limit_gate(stop_pts=99, limit_pts=100, limit_proximity=1.0)
        self.assertTrue(ok)
        ok2 = check_limit_gate(stop_pts=100, limit_pts=100, limit_proximity=1.0)
        self.assertFalse(ok2)

    def test_proximity_0_5_strict(self):
        """proximity = 0.5 → 止损不能超过涨跌停的一半"""
        # stop=60, limit=100, prox=0.5 → 60 < 50? 不 → 不通过
        ok = check_limit_gate(stop_pts=60, limit_pts=100, limit_proximity=0.5)
        self.assertFalse(ok)
        # stop=40 < 50 → 通过
        ok2 = check_limit_gate(stop_pts=40, limit_pts=100, limit_proximity=0.5)
        self.assertTrue(ok2)


# ═══════════════════════════════════════════════════════════════════════════
#  8. 完整仓位计划（集成）
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionPlan(unittest.TestCase):
    """完整仓位计划计算（集成测试）。"""

    def _default_params(self):
        """默认参数（典型品种配置）"""
        return dict(
            equity=100000,
            risk_pct=1.5,
            stop_pts=15,
            multiplier=10,
            margin_rate=0.12,
            price=1000,
            margin_cap_pct=30.0,
            max_lots=5,
            kelly_mult=1.0,
        )

    def test_basic_risk_driven(self):
        """风险预算是约束瓶颈 → N_plan 由风险决定"""
        # 风险：100000*1.5% / (15*10) = 1500/150 = 10 手
        # 保证金：100000*30% / (1000*10*0.12) = 30000/1200 = 25 手
        # max_lots = 5
        # → 受 max_lots 限制，最终 5 手
        result = calc_position_plan(**self._default_params())
        self.assertEqual(result["N_plan"], 5)
        self.assertEqual(result["N_risk"], 10)
        self.assertEqual(result["N_margin"], 25)
        self.assertTrue(result["passed"])

    def test_margin_tight_constraint(self):
        """保证金是约束瓶颈 → N_plan 由保证金决定"""
        params = self._default_params()
        params["margin_cap_pct"] = 1.0  # 极紧的保证金约束
        # 保证金预算 = 1000 元，每手 1200 → 0 手
        # 但最小 1 手兜底 → over_risk=True
        # 等等，0 手 + 兜底 → 1 手，但保证金只够 0 手
        # 实际逻辑：N_risk 经过兜底，N_margin 是保证金约束，取 min
        result = calc_position_plan(**params)
        # N_margin = int(100000 * 0.01 // 1200) = int(1000 // 1200) = 0
        # min(1, 0, 5) = 0
        self.assertEqual(result["N_plan"], 0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["N_margin"], 0)

    def test_kelly_scaling_reduces_lots(self):
        """Kelly 因子 < 1 → 降低风险手数"""
        params = self._default_params()
        params["kelly_mult"] = 0.6
        result = calc_position_plan(**params)
        # 风险原始 10 手 × 0.6 = 6 → 但 max_lots=5 → 还是 5
        # 等一下，10 * 0.6 = 6，min(6, 25, 5) = 5，还是 5
        # 调整一下参数让风险是瓶颈
        params["max_lots"] = 20
        result = calc_position_plan(**params)
        # 10 * 0.6 = 6 手
        self.assertEqual(result["N_risk"], 6)
        self.assertEqual(result["N_plan"], 6)

    def test_kelly_scaling_increases_lots(self):
        """Kelly 因子 > 1 → 增加风险手数"""
        params = self._default_params()
        params["kelly_mult"] = 1.2
        params["max_lots"] = 20
        result = calc_position_plan(**params)
        # 10 * 1.2 = 12 手
        self.assertEqual(result["N_risk"], 12)
        self.assertEqual(result["N_plan"], 12)

    def test_t_strength_weak_reduces_lots(self):
        """T 弱（刚过阈值）→ 降仓"""
        params = self._default_params()
        params["t_strength"] = 70  # 刚过阈值
        params["t_thresh"] = 70
        params["max_lots"] = 10
        result = calc_position_plan(**params)
        # scale = 70 / (70*1.5) = 0.667
        # N_plan = int(10 * 0.667) = 6
        self.assertIsNotNone(result["t_scale"])
        self.assertAlmostEqual(result["t_scale"], 0.667, places=2)
        self.assertLess(result["N_plan"], 10)
        self.assertEqual(result["N_plan"], 6)

    def test_t_strength_strong_full_lots(self):
        """T 强（1.5×阈值以上）→ 满仓"""
        params = self._default_params()
        params["t_strength"] = 120
        params["t_thresh"] = 70
        params["max_lots"] = 10
        result = calc_position_plan(**params)
        self.assertEqual(result["t_scale"], 1.0)
        self.assertEqual(result["N_plan"], 10)

    def test_held_lots_reduce_new_position(self):
        """已有持仓 → 新开仓减少"""
        params = self._default_params()
        params["held_lots"] = 3
        params["max_lots"] = 5
        result = calc_position_plan(**params)
        # 原计划 5 手，已有 3 → 还能加 2
        self.assertEqual(result["N_plan"], 2)

    def test_full_held_zero_new(self):
        """已满仓 → 不加仓"""
        params = self._default_params()
        params["held_lots"] = 5
        params["max_lots"] = 5
        result = calc_position_plan(**params)
        self.assertEqual(result["N_plan"], 0)
        self.assertFalse(result["passed"])

    def test_gate3_failure_blocks_trade(self):
        """涨跌停闸门不通过 → passed=False"""
        params = self._default_params()
        params["limit_pts"] = 15  # 涨跌停 = 止损 → 不通过
        params["limit_proximity"] = 0.9
        result = calc_position_plan(**params)
        # 15 < 15*0.9 = 13.5? 不 → gate3_ok=False
        self.assertFalse(result["gate3_ok"])
        self.assertFalse(result["passed"])

    def test_gate3_ok_passes(self):
        """涨跌停闸门通过 → 不影响"""
        params = self._default_params()
        params["limit_pts"] = 50  # 涨跌停 50，止损 15 → 15 < 45 → 通过
        params["limit_proximity"] = 0.9
        result = calc_position_plan(**params)
        self.assertTrue(result["gate3_ok"])

    def test_over_risk_flag(self):
        """超风险预算（最小 1 手兜底）→ over_risk=True"""
        params = self._default_params()
        params["equity"] = 5000  # 小账户
        params["stop_pts"] = 50   # 大止损
        params["max_lots"] = 10
        # 每手风险 = 50*10 = 500
        # 风险预算 = 5000 * 1.5% = 75
        # 75 // 500 = 0 → 兜底 1 手
        result = calc_position_plan(**params)
        self.assertTrue(result["over_risk"])
        self.assertEqual(result["N_risk_raw"], 0)
        self.assertEqual(result["N_risk"], 1)

    def test_zero_plan_not_passed(self):
        """计划 0 手 → passed=False"""
        params = self._default_params()
        params["held_lots"] = 100  # 持仓远超上限
        result = calc_position_plan(**params)
        self.assertEqual(result["N_plan"], 0)
        self.assertFalse(result["passed"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  风控仓位计算 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
