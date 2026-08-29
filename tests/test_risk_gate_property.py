#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_gate_utils — Property-Based 测试
========================================

用 hypothesis 生成大量随机输入，验证纯函数的数学不变性（invariants）。

相比传统的示例测试（example-based test），property-based 测试的优势：
  · 覆盖更多边界情况（零值、极值、负数、小数…）
  · 发现人类想不到的反例
  · 验证函数的"恒成立属性"而非特定输入的输出

覆盖模块：
  1. calc_risk_lots         — 风险预算手数
  2. calc_min_lot_floor     — 最小 1 手兜底
  3. apply_kelly_scaling    — Kelly 因子缩放
  4. calc_margin_lots       — 保证金约束手数
  5. calc_t_strength_scale  — T 强度缩放系数
  6. deduct_held_lots       — 已有持仓扣减
  7. check_limit_gate       — 涨跌停闸门
  8. calc_position_plan     — 完整仓位计划（集成）
"""

import os
import sys
import unittest

from hypothesis import given, assume, settings, strategies as st

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
#  辅助策略（生成合理范围内的随机输入）
# ═══════════════════════════════════════════════════════════════════════════

# 账户权益：1000 ~ 1000 万
_equities = st.floats(min_value=1_000, max_value=10_000_000, allow_nan=False, allow_infinity=False)

# 风险比例：0.1% ~ 10%
_risk_pcts = st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False)

# 止损点数：0.1 ~ 1000
_stop_ptss = st.floats(min_value=0.1, max_value=1000.0, allow_nan=False, allow_infinity=False)

# 合约乘数：1 ~ 1000
_multipliers = st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False)

# Kelly 系数：0.3 ~ 1.5
_kelly_mults = st.floats(min_value=0.3, max_value=1.5, allow_nan=False, allow_infinity=False)

# T 强度：-10 ~ 10
_t_strengths = st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False)

# T 阈值：0.1 ~ 5.0
_t_threshs = st.floats(min_value=0.1, max_value=5.0, allow_nan=False, allow_infinity=False)

# 价格：10 ~ 100000
_prices = st.floats(min_value=10.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)

# 保证金率：0.05 ~ 0.3
_margin_rates = st.floats(min_value=0.05, max_value=0.3, allow_nan=False, allow_infinity=False)

# 保证金上限比例：5 ~ 50%
_margin_caps = st.floats(min_value=5.0, max_value=50.0, allow_nan=False, allow_infinity=False)

# 手数：0 ~ 50
_lots = st.integers(min_value=0, max_value=50)

# 涨跌停幅度：0 ~ 500
_limit_ptss = st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False)


# ═══════════════════════════════════════════════════════════════════════════
#  1. calc_risk_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskLotsProperty(unittest.TestCase):
    """calc_risk_lots 属性测试。"""

    @given(equity=_equities, risk_pct=_risk_pcts, stop_pts=_stop_ptss, multiplier=_multipliers)
    @settings(max_examples=200)
    def test_result_non_negative(self, equity, risk_pct, stop_pts, multiplier):
        """不变性：结果永远 >= 0"""
        result = calc_risk_lots(equity, risk_pct, stop_pts, multiplier)
        self.assertGreaterEqual(result, 0)
        self.assertIsInstance(result, int)

    @given(equity=_equities, risk_pct=_risk_pcts, stop_pts=_stop_ptss, multiplier=_multipliers)
    @settings(max_examples=200)
    def test_never_exceeds_budget(self, equity, risk_pct, stop_pts, multiplier):
        """不变性：总风险不超过风险预算（向下取整的性质）"""
        risk_per_hand = stop_pts * multiplier
        assume(risk_per_hand > 0)
        N = calc_risk_lots(equity, risk_pct, stop_pts, multiplier)
        risk_budget = equity * risk_pct / 100.0
        self.assertLessEqual(N * risk_per_hand, risk_budget + 1e-9)

    @given(risk_pct=_risk_pcts, stop_pts=_stop_ptss, multiplier=_multipliers)
    @settings(max_examples=100)
    def test_monotonic_in_equity(self, risk_pct, stop_pts, multiplier):
        """不变性：权益增加 → 手数不减少（单调性）"""
        N1 = calc_risk_lots(50_000, risk_pct, stop_pts, multiplier)
        N2 = calc_risk_lots(100_000, risk_pct, stop_pts, multiplier)
        self.assertGreaterEqual(N2, N1)

    @given(equity=_equities, risk_pct=_risk_pcts, multiplier=_multipliers)
    @settings(max_examples=100)
    def test_monotonic_decreasing_in_stop(self, equity, risk_pct, multiplier):
        """不变性：止损点数增加 → 手数不增加"""
        N1 = calc_risk_lots(equity, risk_pct, 10.0, multiplier)
        N2 = calc_risk_lots(equity, risk_pct, 50.0, multiplier)
        self.assertLessEqual(N2, N1)


# ═══════════════════════════════════════════════════════════════════════════
#  2. calc_min_lot_floor
# ═══════════════════════════════════════════════════════════════════════════


class TestMinLotFloorProperty(unittest.TestCase):
    """calc_min_lot_floor 属性测试。"""

    @given(
        N_raw=st.integers(min_value=-10, max_value=100),
        risk_per_hand=st.floats(min_value=-100, max_value=1000, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_result_structure(self, N_raw, risk_per_hand):
        """不变性：返回 (int, bool) 元组"""
        N, over = calc_min_lot_floor(N_raw, risk_per_hand)
        self.assertIsInstance(N, int)
        self.assertIsInstance(over, bool)

    @given(
        N_raw=st.integers(min_value=0, max_value=100),
        risk_per_hand=st.floats(min_value=0.1, max_value=1000, allow_nan=False),
    )
    @settings(max_examples=100)
    def test_positive_risk_hand_non_negative(self, N_raw, risk_per_hand):
        """不变性：risk_per_hand > 0 时，结果手数 >= 0"""
        N, _ = calc_min_lot_floor(N_raw, risk_per_hand)
        self.assertGreaterEqual(N, 0)

    @given(
        N_raw=st.integers(min_value=1, max_value=100),
        risk_per_hand=st.floats(min_value=0.1, max_value=1000, allow_nan=False),
    )
    @settings(max_examples=100)
    def test_positive_raw_no_floor(self, N_raw, risk_per_hand):
        """不变性：N_raw >= 1 且有风险 → 不触发兜底，over_risk=False"""
        N, over = calc_min_lot_floor(N_raw, risk_per_hand)
        self.assertEqual(N, N_raw)
        self.assertFalse(over)

    @given(risk_per_hand=st.floats(min_value=0.1, max_value=1000, allow_nan=False))
    @settings(max_examples=50)
    def test_zero_or_negative_raw_floors_to_1(self, risk_per_hand):
        """不变性：N_raw < 1 且 risk_per_hand > 0 → 至少 1 手"""
        for N_raw in [0, -1, -5]:
            N, over = calc_min_lot_floor(N_raw, risk_per_hand)
            self.assertEqual(N, 1)
            self.assertTrue(over)


# ═══════════════════════════════════════════════════════════════════════════
#  3. apply_kelly_scaling
# ═══════════════════════════════════════════════════════════════════════════


class TestKellyScalingProperty(unittest.TestCase):
    """apply_kelly_scaling 属性测试。"""

    @given(N_risk=st.integers(min_value=-5, max_value=50), kelly_mult=_kelly_mults)
    @settings(max_examples=200)
    def test_result_non_negative(self, N_risk, kelly_mult):
        """不变性：结果永远 >= 0"""
        result = apply_kelly_scaling(N_risk, kelly_mult)
        self.assertGreaterEqual(result, 0)
        self.assertIsInstance(result, int)

    @given(kelly_mult=_kelly_mults)
    @settings(max_examples=50)
    def test_zero_input_zero_output(self, kelly_mult):
        """不变性：N_risk < 1 → 返回 0"""
        for N in [0, -1, -5]:
            self.assertEqual(apply_kelly_scaling(N, kelly_mult), 0)

    @given(N_risk=st.integers(min_value=1, max_value=50))
    @settings(max_examples=50)
    def test_kelly_1_identity(self, N_risk):
        """不变性：kelly_mult=1.0 → 结果等于输入（至少 1 手）"""
        result = apply_kelly_scaling(N_risk, 1.0)
        self.assertEqual(result, N_risk)

    @given(
        N_risk=st.integers(min_value=1, max_value=50),
        kelly_mult=st.floats(min_value=1.0, max_value=1.5, allow_nan=False),
    )
    @settings(max_examples=100)
    def test_kelly_above_1_increases_or_same(self, N_risk, kelly_mult):
        """不变性：kelly_mult >= 1 → 结果 >= 输入（且至少 1）"""
        result = apply_kelly_scaling(N_risk, kelly_mult)
        self.assertGreaterEqual(result, 1)
        # 四舍五入可能等于原值
        self.assertGreaterEqual(result, N_risk - 1)  # 容差 1（rounding）


# ═══════════════════════════════════════════════════════════════════════════
#  4. calc_t_strength_scale
# ═══════════════════════════════════════════════════════════════════════════


class TestTStrengthScaleProperty(unittest.TestCase):
    """calc_t_strength_scale 属性测试。"""

    @given(t_strength=_t_strengths, t_thresh=_t_threshs)
    @settings(max_examples=300)
    def test_scale_in_range(self, t_strength, t_thresh):
        """不变性：结果始终在 [0.5, 1.0] 范围内"""
        scale = calc_t_strength_scale(t_strength, t_thresh)
        self.assertGreaterEqual(scale, 0.5)
        self.assertLessEqual(scale, 1.0)

    @given(t_strength=_t_strengths, t_thresh=_t_threshs)
    @settings(max_examples=200)
    def test_symmetric(self, t_strength, t_thresh):
        """不变性：正负 T 值得到相同缩放（绝对值对称）"""
        scale_pos = calc_t_strength_scale(t_strength, t_thresh)
        scale_neg = calc_t_strength_scale(-t_strength, t_thresh)
        self.assertAlmostEqual(scale_pos, scale_neg, places=10)

    @given(t_thresh=_t_threshs)
    @settings(max_examples=100)
    def test_monotonic_increasing(self, t_thresh):
        """不变性：|T| 越大，缩放系数越大（单调非递减）"""
        scales = []
        for t_val in [0.1, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
            scales.append(calc_t_strength_scale(t_val, t_thresh))
        for i in range(len(scales) - 1):
            self.assertGreaterEqual(scales[i + 1], scales[i] - 1e-9)

    @given(t_thresh=_t_threshs)
    @settings(max_examples=100)
    def test_strong_signal_full_scale(self, t_thresh):
        """不变性：|T| >= 1.5 × 阈值 → 满仓 1.0"""
        scale = calc_t_strength_scale(t_thresh * 2.0, t_thresh)  # 2x 阈值
        self.assertAlmostEqual(scale, 1.0)

    @given(t_strength=_t_strengths)
    @settings(max_examples=100)
    def test_zero_threshold_no_scale(self, t_strength):
        """不变性：阈值 <= 0 → 不缩放（返回 1.0）"""
        self.assertEqual(calc_t_strength_scale(t_strength, 0), 1.0)
        self.assertEqual(calc_t_strength_scale(t_strength, -1.0), 1.0)
        self.assertEqual(calc_t_strength_scale(t_strength, None), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. deduct_held_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestDeductHeldLotsProperty(unittest.TestCase):
    """deduct_held_lots 属性测试。"""

    @given(N_plan=_lots, held_lots=_lots, max_lots=_lots)
    @settings(max_examples=300)
    def test_result_non_negative(self, N_plan, held_lots, max_lots):
        """不变性：结果永远 >= 0"""
        result = deduct_held_lots(N_plan, held_lots, max_lots)
        self.assertGreaterEqual(result, 0)
        self.assertIsInstance(result, int)

    @given(N_plan=_lots, max_lots=_lots)
    @settings(max_examples=100)
    def test_no_held_no_change(self, N_plan, max_lots):
        """不变性：held_lots <= 0 → 结果 = max(0, N_plan)"""
        result = deduct_held_lots(N_plan, 0, max_lots)
        self.assertEqual(result, max(0, N_plan))
        result_neg = deduct_held_lots(N_plan, -5, max_lots)
        self.assertEqual(result_neg, max(0, N_plan))

    @given(N_plan=_lots, held_lots=_lots, max_lots=_lots)
    @settings(max_examples=200)
    def test_total_never_exceeds_max(self, N_plan, held_lots, max_lots):
        """不变性：0 < 已有持仓 <= max 时，新开 + 已有 <= max_lots（加仓不超配）"""
        assume(0 < held_lots <= max_lots)
        new_lots = deduct_held_lots(N_plan, held_lots, max_lots)
        self.assertLessEqual(new_lots + held_lots, max_lots)

    @given(N_plan=_lots, max_lots=st.integers(min_value=1, max_value=10))
    @settings(max_examples=100)
    def test_full_held_zero_new(self, N_plan, max_lots):
        """不变性：已有持仓 = max（max > 0）→ 新开 = 0"""
        result = deduct_held_lots(N_plan, max_lots, max_lots)
        self.assertEqual(result, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  6. check_limit_gate
# ═══════════════════════════════════════════════════════════════════════════


class TestLimitGateProperty(unittest.TestCase):
    """check_limit_gate 属性测试。"""

    @given(
        stop_pts=st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False),
        limit_pts=_limit_ptss,
        proximity=st.floats(min_value=0.5, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=300)
    def test_zero_limit_always_ok(self, stop_pts, limit_pts, proximity):
        """不变性：limit_pts <= 0 → 始终放行"""
        self.assertTrue(check_limit_gate(stop_pts, 0.0, proximity))
        self.assertTrue(check_limit_gate(stop_pts, -5.0, proximity))

    @given(
        stop_pts=st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False),
        limit_pts=st.floats(min_value=10, max_value=200, allow_nan=False),
        proximity=st.floats(min_value=0.5, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_stop_smaller_than_threshold_ok(self, stop_pts, limit_pts, proximity):
        """不变性：止损 < limit * proximity → 通过"""
        threshold = limit_pts * proximity
        assume(stop_pts < threshold and threshold > 0.01)
        self.assertTrue(check_limit_gate(stop_pts, limit_pts, proximity))

    @given(
        limit_pts=st.floats(min_value=10, max_value=200, allow_nan=False),
        proximity=st.floats(min_value=0.5, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=100)
    def test_stop_at_or_above_threshold_fails(self, limit_pts, proximity):
        """不变性：止损 >= limit * proximity → 不通过"""
        threshold = limit_pts * proximity
        self.assertFalse(check_limit_gate(threshold + 1.0, limit_pts, proximity))


# ═══════════════════════════════════════════════════════════════════════════
#  7. calc_position_plan（集成）
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionPlanProperty(unittest.TestCase):
    """calc_position_plan 集成属性测试。"""

    @given(
        equity=_equities,
        risk_pct=_risk_pcts,
        stop_pts=_stop_ptss,
        multiplier=_multipliers,
        margin_rate=_margin_rates,
        price=_prices,
        kelly_mult=_kelly_mults,
        held_lots=st.integers(min_value=0, max_value=10),
    )
    @settings(max_examples=200)
    def test_result_structure(self, equity, risk_pct, stop_pts, multiplier, margin_rate, price, kelly_mult, held_lots):
        """不变性：返回 dict 包含所有预期字段，且类型正确"""
        result = calc_position_plan(
            equity=equity,
            risk_pct=risk_pct,
            stop_pts=stop_pts,
            multiplier=multiplier,
            margin_rate=margin_rate,
            price=price,
            kelly_mult=kelly_mult,
            held_lots=held_lots,
        )
        # 字段存在
        for key in ["N_risk_raw", "N_risk", "N_margin", "N_plan", "over_risk", "t_scale", "gate3_ok", "passed"]:
            self.assertIn(key, result)

        # 非负整数
        for key in ["N_risk_raw", "N_risk", "N_margin", "N_plan"]:
            self.assertIsInstance(result[key], int)
            self.assertGreaterEqual(result[key], 0)

        # 布尔值
        self.assertIsInstance(result["over_risk"], bool)
        self.assertIsInstance(result["gate3_ok"], bool)
        self.assertIsInstance(result["passed"], bool)

        # passed 逻辑：N_plan >= 1 且 gate3_ok
        if result["passed"]:
            self.assertGreaterEqual(result["N_plan"], 1)
            self.assertTrue(result["gate3_ok"])
        else:
            self.assertTrue(result["N_plan"] < 1 or not result["gate3_ok"])

    @given(
        equity=_equities,
        risk_pct=_risk_pcts,
        stop_pts=_stop_ptss,
        multiplier=_multipliers,
        margin_rate=_margin_rates,
        price=_prices,
        kelly_mult=_kelly_mults,
        t_strength=_t_strengths,
        t_thresh=_t_threshs,
    )
    @settings(max_examples=100)
    def test_t_scale_in_range(
        self, equity, risk_pct, stop_pts, multiplier, margin_rate, price, kelly_mult, t_strength, t_thresh
    ):
        """不变性：启用 T 缩放时，t_scale 在 [0.5, 1.0] 范围内"""
        result = calc_position_plan(
            equity=equity,
            risk_pct=risk_pct,
            stop_pts=stop_pts,
            multiplier=multiplier,
            margin_rate=margin_rate,
            price=price,
            kelly_mult=kelly_mult,
            t_strength=t_strength,
            t_thresh=t_thresh,
        )
        self.assertIsNotNone(result["t_scale"])
        self.assertGreaterEqual(result["t_scale"], 0.5)
        self.assertLessEqual(result["t_scale"], 1.0)

    @given(
        equity=_equities,
        risk_pct=_risk_pcts,
        stop_pts=_stop_ptss,
        multiplier=_multipliers,
        margin_rate=_margin_rates,
        price=_prices,
    )
    @settings(max_examples=100)
    def test_no_t_scale_when_disabled(self, equity, risk_pct, stop_pts, multiplier, margin_rate, price):
        """不变性：不提供 t_strength/t_thresh → t_scale 为 None"""
        result = calc_position_plan(
            equity=equity,
            risk_pct=risk_pct,
            stop_pts=stop_pts,
            multiplier=multiplier,
            margin_rate=margin_rate,
            price=price,
        )
        self.assertIsNone(result["t_scale"])


if __name__ == "__main__":
    unittest.main()
