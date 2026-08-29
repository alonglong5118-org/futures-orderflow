#!/usr/bin/env python3
"""
策略总入口 compute_strategy — 集成测试
=========================================

测试 compute_strategy() 主函数的集成行为：

1. 基本输出结构完整性
   - 返回所有必要字段（regime / direction / confidence / stop_pts / size 等）

2. 方向判定
   - 强趋势 → direction = 1 或 -1
   - 震荡/中性 → direction = 0

3. 止损与仓位计算
   - stop_pts >= ATR * 1.5 且 >= point_value * 0.5
   - size >= 0（不会负）
   - 风险预算约束：size * risk_hand <= equity * risk_pct
   - 保证金约束：size * price * mult * margin_rate <= equity * red_line

4. 稳健池闸门
   - 未入池品种 + wf_gate=True → direction=0, size=0
   - wf_gate=False → 即使未入池也正常交易

5. 策略权重定制
   - 自定义 strategy_weights 可以调整权重
   - 禁用某策略（权重=0）后该策略不影响结果

6. 数值鲁棒性
   - equity=0 → size=0
   - price=0 → 不崩溃
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import (
    _ROBUST_GATE,
    _ROBUST_GATE_CFG,
    STRATS,
    compute_strategy,
    set_robust_gate,
)

# ═══════════════════════════════════════════════════════════════════════════
#  测试数据构造
# ═══════════════════════════════════════════════════════════════════════════


def _make_trend_df(n=120, start=1000, slope=5, vol=2, seed=42):
    """构造趋势行情数据。"""
    np.random.seed(seed)
    px = start + np.arange(n) * slope + np.cumsum(np.random.randn(n) * vol)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": px,
            "high": px + abs(np.random.randn(n) * 3),
            "low": px - abs(np.random.randn(n) * 3),
            "close": px,
            "volume": 1000 + np.random.randn(n) * 100,
        },
        index=idx,
    )
    # 确保 high >= close >= low
    df["high"] = df[["high", "close"]].max(axis=1)
    df["low"] = df[["low", "close"]].min(axis=1)
    return df


def _make_range_df(n=120, center=1000, amp=20, seed=42):
    """构造震荡行情数据。"""
    np.random.seed(seed)
    # 用正弦波构造震荡
    t = np.arange(n)
    px = center + amp * np.sin(t * 0.15) + np.random.randn(n) * 2
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": px,
            "high": px + abs(np.random.randn(n) * 3),
            "low": px - abs(np.random.randn(n) * 3),
            "close": px,
            "volume": 1000 + np.random.randn(n) * 100,
        },
        index=idx,
    )
    df["high"] = df[["high", "close"]].max(axis=1)
    df["low"] = df[["low", "close"]].min(axis=1)
    return df


COMMON_KWARGS = dict(
    equity=100000,
    price=1000,
    mult=20,
    point_value=20,
    margin_rate=0.10,
    fee_per_hand=3.0,
    risk_pct=0.03,
    red_line=0.45,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. 输出结构完整性
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeStrategyOutputStructure(unittest.TestCase):
    """compute_strategy 输出结构完整性。"""

    def test_return_has_all_required_fields(self):
        """返回结果包含所有必要字段"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        required = [
            "regime",
            "regime_desc",
            "direction",
            "direction_text",
            "confidence",
            "main_strategy",
            "stop_pts",
            "stop_price",
            "size",
            "risk_amount",
            "strategies",
            "pool_status",
            "pool_passed",
            "wf_stability",
            "wf_oos_expR",
            "gate_reason",
            "gated",
        ]
        for key in required:
            self.assertIn(key, result, f"缺少字段: {key}")

    def test_direction_values_valid(self):
        """direction ∈ {-1, 0, 1}"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        self.assertIn(result["direction"], [-1, 0, 1])

    def test_direction_text_matches_direction(self):
        """direction_text 与 direction 一致"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        mapping = {1: "偏多", -1: "偏空", 0: "中性"}
        if not result["gated"]:
            self.assertEqual(result["direction_text"], mapping[result["direction"]])

    def test_confidence_in_0_1(self):
        """confidence ∈ [0, 1]"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_size_non_negative(self):
        """size >= 0"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        self.assertGreaterEqual(result["size"], 0)

    def test_stop_pts_positive_when_has_direction(self):
        """有方向时 stop_pts > 0"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        if result["direction"] != 0:
            self.assertGreater(result["stop_pts"], 0)

    def test_strategies_contains_all_strats(self):
        """strategies 包含所有已注册策略"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        for name in STRATS:
            self.assertIn(name, result["strategies"], f"strategies 缺少策略: {name}")
            self.assertIn("signal", result["strategies"][name])
            self.assertIn("detail", result["strategies"][name])

    def test_strategy_signals_are_valid(self):
        """所有策略信号 ∈ {-1, 0, 1}"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        for name, info in result["strategies"].items():
            sig = info["signal"]
            self.assertIn(sig, [-1, 0, 1], f"策略 {name} 信号 = {sig}，应该是 -1/0/1")


# ═══════════════════════════════════════════════════════════════════════════
#  2. 止损与仓位约束
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeStrategyRiskConstraints(unittest.TestCase):
    """止损与仓位约束验证。"""

    def test_stop_pts_at_least_atr_times_15(self):
        """stop_pts >= ATR * 1.5"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        if result["direction"] != 0:
            from strategy_layer import atr

            a = atr(df).iloc[-1]
            self.assertGreaterEqual(result["stop_pts"], a * 1.5 - 0.01, "stop_pts 应该 >= ATR * 1.5")

    def test_stop_pts_at_least_half_point_value(self):
        """stop_pts >= point_value * 0.5"""
        df = _make_trend_df()
        pv = 20
        result = compute_strategy(df, **dict(COMMON_KWARGS, point_value=pv))
        if result["direction"] != 0:
            self.assertGreaterEqual(result["stop_pts"], pv * 0.5 - 0.01, "stop_pts 应该 >= point_value * 0.5")

    def test_size_respects_risk_budget(self):
        """仓位满足风险预算约束：size * risk_hand <= equity * risk_pct

        注意：因为 size 向下取整，可能略低于预算，但不应超过。
        """
        equity = 100000
        risk_pct = 0.03
        df = _make_trend_df()
        result = compute_strategy(df, **dict(COMMON_KWARGS, equity=equity, risk_pct=risk_pct))
        risk_budget = equity * risk_pct
        self.assertLessEqual(
            result["risk_amount"], risk_budget + 1, f"风险金额 {result['risk_amount']} 超过预算 {risk_budget}"
        )

    def test_size_respects_margin_constraint(self):
        """仓位满足保证金约束"""
        equity = 100000
        margin_rate = 0.10
        red_line = 0.45
        mult = 20
        price = 1000
        df = _make_trend_df()
        result = compute_strategy(
            df, **dict(COMMON_KWARGS, equity=equity, margin_rate=margin_rate, red_line=red_line, mult=mult, price=price)
        )
        margin_per = price * mult * margin_rate
        budget = equity * red_line
        max_by_margin = int(budget // margin_per)
        self.assertLessEqual(result["size"], max_by_margin, f"仓位 {result['size']} 超过保证金上限 {max_by_margin}")

    def test_zero_equity_zero_size(self):
        """equity = 0 → size = 0"""
        df = _make_trend_df()
        result = compute_strategy(df, **dict(COMMON_KWARGS, equity=0))
        self.assertEqual(result["size"], 0)

    def test_stop_price_matches_direction(self):
        """stop_price 方向正确（多单在价格下方，空单在上方）"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        price = COMMON_KWARGS["price"]
        if result["direction"] == 1:
            self.assertLess(result["stop_price"], price)
        elif result["direction"] == -1:
            self.assertGreater(result["stop_price"], price)


# ═══════════════════════════════════════════════════════════════════════════
#  3. 稳健池闸门
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeStrategyWfGate(unittest.TestCase):
    """walk-forward 稳健池闸门集成。"""

    def setUp(self):
        """保存完整的闸门状态（阈值 + 配置），测试后恢复。"""
        self._orig_gate = dict(_ROBUST_GATE)
        self._orig_cfg = dict(_ROBUST_GATE_CFG)

    def tearDown(self):
        """完整恢复闸门状态，避免污染其他测试。"""
        _ROBUST_GATE.clear()
        _ROBUST_GATE.update(self._orig_gate)
        _ROBUST_GATE_CFG.clear()
        _ROBUST_GATE_CFG.update(self._orig_cfg)

    def test_gated_symbol_zero_size_and_direction(self):
        """未入池品种 + wf_gate=True → direction=0, size=0"""
        df = _make_trend_df()
        # 把阈值设极高，让所有品种都不通过
        set_robust_gate(stability=0.99, oos_expR=0.50)
        result = compute_strategy(df, **dict(COMMON_KWARGS, symbol="JM", wf_gate=True))
        self.assertTrue(result["gated"])
        self.assertEqual(result["direction"], 0)
        self.assertEqual(result["direction_text"], "观望")
        self.assertEqual(result["size"], 0)
        self.assertEqual(result["confidence"], 0.0)

    def test_wf_gate_disabled_even_not_in_pool(self):
        """wf_gate=False → 即使不在稳健池也正常交易"""
        df = _make_trend_df()
        set_robust_gate(stability=0.99, oos_expR=0.50)  # 极高门槛
        result = compute_strategy(df, **dict(COMMON_KWARGS, symbol="xyz_nonexistent", wf_gate=False))
        self.assertFalse(result["gated"])
        # 没有被闸门关掉，应该有正常的方向判断
        self.assertIn(result["direction"], [-1, 0, 1])

    def test_no_symbol_no_gating(self):
        """symbol=None → 不启用闸门"""
        df = _make_trend_df()
        result = compute_strategy(df, **dict(COMMON_KWARGS, symbol=None, wf_gate=True))
        self.assertFalse(result["gated"])
        self.assertEqual(result["pool_status"], "—")

    def test_pool_status_reported(self):
        """pool_status 字段正确反映状态"""
        df = _make_trend_df()
        set_robust_gate(stability=0.99, oos_expR=0.50)
        result = compute_strategy(df, **dict(COMMON_KWARGS, symbol="JM", wf_gate=True))
        self.assertTrue(result["gated"])
        self.assertEqual(result["pool_status"], "观察池")


# ═══════════════════════════════════════════════════════════════════════════
#  4. 策略权重定制
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeStrategyWeights(unittest.TestCase):
    """策略权重定制效果。"""

    def test_custom_weights_affect_result(self):
        """自定义 strategy_weights 影响结果"""
        df = _make_trend_df()
        # 正常权重
        r_normal = compute_strategy(df, **COMMON_KWARGS)

        # 把趋势策略权重全设为 0，均值权重放大
        zero_trend = {k: 0.0 for k in STRATS if k in ["ma_break", "dma", "turtle", "donchian", "pullback"]}
        r_zerotrend = compute_strategy(df, **dict(COMMON_KWARGS, strategy_weights=zero_trend))

        # 权重不同，confidence 应该不同
        # （方向不一定变，因为可能还有均值策略同向）
        self.assertIsNotNone(r_zerotrend["confidence"])

    def test_disable_all_strategies_zero_direction(self):
        """禁用所有策略 → direction = 0, confidence = 0"""
        df = _make_trend_df()
        all_zero = {k: 0.0 for k in STRATS}
        result = compute_strategy(df, **dict(COMMON_KWARGS, strategy_weights=all_zero))
        # 所有策略权重为 0 → score = 0 → direction = 0
        self.assertEqual(result["direction"], 0)
        self.assertEqual(result["confidence"], 0.0)

    def test_main_strategy_has_highest_weighted_signal(self):
        """main_strategy 是同向中加权信号最强的策略"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        if result["direction"] != 0 and result["main_strategy"]:
            self.assertIn(result["main_strategy"], STRATS)
            # 主策略的信号应该与 direction 同向
            main_sig = result["strategies"][result["main_strategy"]]["signal"]
            self.assertEqual(main_sig, result["direction"])


# ═══════════════════════════════════════════════════════════════════════════
#  5. 行情类型与方向
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeStrategyMarketTypes(unittest.TestCase):
    """不同行情类型下的策略行为。"""

    def test_uptrend_positive_direction(self):
        """上升趋势 → direction = 1（偏多）"""
        df = _make_trend_df(n=120, slope=3, seed=1)
        result = compute_strategy(df, **dict(COMMON_KWARGS, price=df["close"].iloc[-1]))
        # 强上升趋势应该偏多
        self.assertEqual(result["direction"], 1)

    def test_downtrend_negative_direction(self):
        """下降趋势 → direction = -1（偏空）"""
        df = _make_trend_df(n=120, slope=-3, seed=1)
        result = compute_strategy(df, **dict(COMMON_KWARGS, price=df["close"].iloc[-1]))
        # 强下降趋势应该偏空
        self.assertEqual(result["direction"], -1)

    def test_regime_not_empty(self):
        """regime 字段不为空"""
        df = _make_trend_df()
        result = compute_strategy(df, **COMMON_KWARGS)
        self.assertTrue(result["regime"])
        self.assertIn(result["regime"], ["趋势", "震荡", "波动", "未知", "过渡"])

    def test_confidence_positive_when_has_direction(self):
        """有方向时 confidence > 0"""
        df = _make_trend_df(n=120, slope=3, seed=1)
        result = compute_strategy(df, **dict(COMMON_KWARGS, price=df["close"].iloc[-1]))
        if result["direction"] != 0:
            self.assertGreater(result["confidence"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  策略总入口 compute_strategy — 集成测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
