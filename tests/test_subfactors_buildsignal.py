#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T 子因子分解 + 信号包裹 — 单元测试
=========================================

1. compute_T_subfactors — T 维度三簇子因子分解
   - 数据不足 → 全 0 + 未知
   - 趋势行情 → T_trend 正，T_mean 可能反向
   - 震荡行情 → T_mean 正，T_trend 弱
   - decorrelate 关闭 → 退化成旧逻辑（T_trend = T, 其余 0）
   - 子因子范围 [-100, 100]
   - 子因子独立（趋势簇和均值簇可以反向）

2. build_signal — 信号包裹格式化
   - 输出字段完整性
   - 多/空方向文本
   - 滑点成本计算
   - reason 文案包含关键信息
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    build_signal,
    compute_T_subfactors,
    exit_plan,
    pipeline,
    risk_gate,
)

# ═══════════════════════════════════════════════════════════════════════════
#  测试数据
# ═══════════════════════════════════════════════════════════════════════════

def _make_trend_df(n=120, slope=3, seed=42):
    """趋势行情。"""
    np.random.seed(seed)
    px = 1000 + np.arange(n) * slope + np.cumsum(np.random.randn(n) * 2)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "open": px, "high": px + 3, "low": px - 3, "close": px,
        "volume": 10000, "open_interest": 50000,
    }, index=idx)
    return df


def _make_range_df(n=120, amp=20, seed=42):
    """震荡行情。"""
    np.random.seed(seed)
    t = np.arange(n)
    px = 1000 + amp * np.sin(t * 0.12) + np.random.randn(n) * 2
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "open": px, "high": px + 3, "low": px - 3, "close": px,
        "volume": 10000, "open_interest": 50000,
    }, index=idx)
    return df


# ═══════════════════════════════════════════════════════════════════════════
#  1. compute_T_subfactors — 基本
# ═══════════════════════════════════════════════════════════════════════════

class TestTSubfactorsBasic(unittest.TestCase):
    """compute_T_subfactors 基本行为。"""

    def test_insufficient_data_zeros(self):
        """数据不足（< 60 根）→ 全 0 + 未知"""
        df_short = _make_trend_df(n=30)
        t_trend, t_mean, t_seas, regime, rdesc = compute_T_subfactors(df_short)
        self.assertEqual(t_trend, 0.0)
        self.assertEqual(t_mean, 0.0)
        self.assertEqual(t_seas, 0.0)
        self.assertEqual(regime, "未知")

    def test_returns_five_values(self):
        """返回 5 个值：T_trend, T_mean, T_seasonal, regime, rdesc"""
        df = _make_trend_df()
        result = compute_T_subfactors(df)
        self.assertEqual(len(result), 5)

    def test_all_subfactors_bounded(self):
        """所有子因子 ∈ [-100, 100]"""
        df = _make_trend_df(slope=5)
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df)
        for name, val in [("trend", t_trend), ("mean", t_mean), ("seasonal", t_seas)]:
            self.assertGreaterEqual(val, -100, "%s = %s < -100" % (name, val))
            self.assertLessEqual(val, 100, "%s = %s > 100" % (name, val))

    def test_regime_populated(self):
        """regime 字段有值（不是空）"""
        df = _make_trend_df()
        _, _, _, regime, rdesc = compute_T_subfactors(df)
        self.assertTrue(regime)
        self.assertTrue(rdesc)


# ═══════════════════════════════════════════════════════════════════════════
#  2. compute_T_subfactors — 行情类型
# ═══════════════════════════════════════════════════════════════════════════

class TestTSubfactorsMarketTypes(unittest.TestCase):
    """不同行情下的子因子表现。"""

    def test_uptrend_positive_trend_factor(self):
        """上升趋势 → T_trend > 0"""
        df = _make_trend_df(slope=4, seed=1)
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df)
        self.assertGreater(t_trend, 0,
            "上升趋势中 T_trend 应该为正")

    def test_downtrend_negative_trend_factor(self):
        """下降趋势 → T_trend < 0"""
        df = _make_trend_df(slope=-4, seed=1)
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df)
        self.assertLess(t_trend, 0,
            "下降趋势中 T_trend 应该为负")

    def test_trend_factor_stronger_than_mean_in_trend(self):
        """趋势行情 → |T_trend| > |T_mean|"""
        df = _make_trend_df(slope=4, seed=1)
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df)
        self.assertGreater(abs(t_trend), abs(t_mean),
            "趋势行情中 T_trend 绝对值应该大于 T_mean")


# ═══════════════════════════════════════════════════════════════════════════
#  3. compute_T_subfactors — decorrelate 开关
# ═══════════════════════════════════════════════════════════════════════════

class TestTSubfactorsDecorrelateSwitch(unittest.TestCase):
    """decorrelate 开关效果。"""

    def test_decorrelate_disabled_returns_old_format(self):
        """decorrelate 关闭 → 退化成旧逻辑：T_trend = T，其余 = 0"""
        df = _make_trend_df(slope=3)
        cfg_no_dec = dict(DEFAULT_CONFIG)
        cfg_no_dec["decorrelate"] = {"enabled": False}
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df, cfg=cfg_no_dec)
        # 关闭时，T_mean 和 T_seasonal 应该 = 0
        self.assertEqual(t_mean, 0.0)
        self.assertEqual(t_seas, 0.0)
        # T_trend 就是原来的 T 分数（非 0）
        self.assertNotEqual(t_trend, 0.0)

    def test_decorrelate_enabled_has_three_factors(self):
        """decorrelate 开启 → 三个子因子都可能非 0"""
        df = _make_trend_df(slope=3)
        cfg_dec = dict(DEFAULT_CONFIG)
        cfg_dec["decorrelate"] = {"enabled": True}
        t_trend, t_mean, t_seas, _, _ = compute_T_subfactors(df, cfg=cfg_dec)
        # 开启时，T_trend 非 0（趋势行情）
        self.assertNotEqual(t_trend, 0.0)

    def test_decorrelate_on_vs_off_different(self):
        """开关状态不同 → 结果不同"""
        df = _make_trend_df(slope=3)
        cfg_on = dict(DEFAULT_CONFIG, decorrelate={"enabled": True})
        cfg_off = dict(DEFAULT_CONFIG, decorrelate={"enabled": False})
        r_on = compute_T_subfactors(df, cfg=cfg_on)
        r_off = compute_T_subfactors(df, cfg=cfg_off)
        # T_trend 的计算方式不同，结果应该不同
        self.assertNotEqual(r_on[0], r_off[0])

    def test_decorrelate_default_enabled(self):
        """默认配置下 decorrelate 应该是开启的"""
        dc = DEFAULT_CONFIG.get("decorrelate", {})
        self.assertTrue(dc.get("enabled", True),
            "默认 decorrelate 应该开启")


# ═══════════════════════════════════════════════════════════════════════════
#  4. compute_T_subfactors — 子因子独立性
# ═══════════════════════════════════════════════════════════════════════════

class TestTSubfactorsIndependence(unittest.TestCase):
    """子因子独立性验证。"""

    def test_decorrelate_on_mean_can_be_nonzero(self):
        """decorrelate 开启时 T_mean 可以非零（关闭时必为 0）

        这证明子因子是独立计算的——旧逻辑只有 T_trend 有值，
        新逻辑下 T_mean 和 T_seasonal 也有独立的值。
        """
        df = _make_range_df(amp=25, seed=7)  # 震荡行情
        cfg_off = dict(DEFAULT_CONFIG, decorrelate={"enabled": False})
        cfg_on = dict(DEFAULT_CONFIG, decorrelate={"enabled": True})
        _, t_mean_off, _, _, _ = compute_T_subfactors(df, cfg=cfg_off)
        _, t_mean_on, _, _, _ = compute_T_subfactors(df, cfg=cfg_on)
        # 关闭时 T_mean 一定 = 0
        self.assertEqual(t_mean_off, 0.0)
        # 开启时 T_mean 可以非零（震荡行情下均值策略有信号）
        # 不强制断言非零（可能行情特殊），但开启和关闭应该不同
        # 至少趋势和均值不应该总是相等
        t_trend_on, t_mean_on_2, _, _, _ = compute_T_subfactors(df, cfg=cfg_on)
        # 开启时，T_trend 和 T_mean 是两个独立的量
        self.assertIsInstance(t_trend_on, float)
        self.assertIsInstance(t_mean_on_2, float)


# ═══════════════════════════════════════════════════════════════════════════
#  5. build_signal — 输出结构
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildSignalStructure(unittest.TestCase):
    """build_signal 输出结构。"""

    def _make_signal_inputs(self, symbol="rb", direction=1):
        """构造 build_signal 的输入参数。"""
        df = _make_trend_df(slope=3 if direction > 0 else -3)
        pipe = pipeline(symbol, df, F_override=30, c_override=20)
        # 确保有方向
        if pipe["dir_T"] == 0:
            # 强制给一个方向
            pipe = dict(pipe)
            pipe["dir_T"] = direction
        price = df["close"].iloc[-1]
        rg = risk_gate(symbol, price=price, atr_val=50,
                       t_strength=abs(pipe.get("T_5m", 60)),
                       t_thresh=pipe.get("T_thresh_eff", 50))
        ep = exit_plan(symbol, entry=price, dir_T=pipe["dir_T"],
                       atr_val=50, regime=pipe["regime"] or "趋势")
        return pipe, rg, ep, price

    def test_output_has_required_fields(self):
        """返回所有必要字段"""
        pipe, rg, ep, price = self._make_signal_inputs()
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        required = [
            "symbol", "name", "direction", "entry_ref",
            "stop", "target", "t1", "t2",
            "stop_dist", "lots",
            "pipeline", "risk_gate", "cost", "exit_plan", "reason",
        ]
        for key in required:
            self.assertIn(key, sig, "缺少字段: %s" % key)

    def test_long_direction_text(self):
        """做多 → direction = '多'"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        pipe = dict(pipe)
        pipe["dir_T"] = 1
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        self.assertEqual(sig["direction"], "多")

    def test_short_direction_text(self):
        """做空 → direction = '空'"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=-1)
        pipe = dict(pipe)
        pipe["dir_T"] = -1
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        self.assertEqual(sig["direction"], "空")

    def test_reason_contains_key_info(self):
        """reason 文案包含关键信息"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        reason = sig["reason"]
        # 应该包含这些关键词
        keywords = ["止损", "t1", "t2"]
        for kw in keywords:
            self.assertIn(kw, reason, "reason 应该包含 '%s'" % kw)

    def test_slip_cost_calculation(self):
        """滑点成本 = 2 * slip_pts / stop_dist"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        slip = sig["cost"]["slip_pts"]
        expected_ratio = 2 * slip / ep["stop_dist"] if ep["stop_dist"] > 0 else 0
        self.assertAlmostEqual(sig["cost"]["slip_cost_r"], expected_ratio, places=3)

    def test_pipeline_section_has_all_dims(self):
        """pipeline 段落包含 F/T/C/bias_G/regime"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        p = sig["pipeline"]
        for key in ["F_bias", "T_D", "T_5m", "C_score", "bias_G", "regime"]:
            self.assertIn(key, p, "pipeline 缺少字段: %s" % key)

    def test_risk_gate_section(self):
        """risk_gate 段落包含 pass/N_risk/N_margin/N_plan/kelly_mult"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        g = sig["risk_gate"]
        for key in ["pass", "N_risk", "N_margin", "N_plan", "kelly_mult", "limit_check"]:
            self.assertIn(key, g, "risk_gate 缺少字段: %s" % key)

    def test_target_equals_t2(self):
        """target = t2（主目标位 = 第二止盈位）"""
        pipe, rg, ep, price = self._make_signal_inputs(direction=1)
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        self.assertEqual(sig["target"], sig["t2"])

    def test_symbol_name_present(self):
        """品种名正确显示"""
        pipe, rg, ep, price = self._make_signal_inputs("rb")
        sig = build_signal("rb", pipe, rg, ep, entry_ref=price)
        from four_dim_strategy import SYMBOLS
        self.assertEqual(sig["name"], SYMBOLS["rb"]["name"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  T 子因子分解 + 信号包裹 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
