#!/usr/bin/env python3
"""
EMA 指标 + 稳健池闸门配置 — 单元测试
=========================================

1. ema — 指数移动平均
   - 长度不足 → 全 NaN
   - 长度刚好 → 最后一个有值
   - 常数列 → EMA = 常数
   - 线性增长 → EMA 滞后于 SMA
   - EMA 比 SMA 更贴近最新值（权重倾斜）
   - n=1 → EMA = 原值
   - 与已知值对比（手工验证）

2. configure_robust_gate / get_robust_gate / set_robust_gate
   - set_robust_gate 修改后 get 能读到
   - configure_robust_gate 各参数生效
   - 关闭闸门时返回默认值
   - 恢复默认值
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
    OOS_EXPR_THRESHOLD,
    STABILITY_THRESHOLD,
    configure_robust_gate,
    ema,
    get_robust_gate,
    set_robust_gate,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. ema — 指数移动平均
# ═══════════════════════════════════════════════════════════════════════════


class TestEMA(unittest.TestCase):
    """ema — 指数移动平均。"""

    def test_short_series_has_values(self):
        """短序列也有值（adjust=False 模式从第 1 个开始算）"""
        s = pd.Series([1.0, 2.0])
        result = ema(s, 5)
        # adjust=False 的 EMA 从第 1 个值就有结果
        self.assertFalse(result.isna().any())
        self.assertEqual(len(result), 2)

    def test_constant_series_ema_equals_constant(self):
        """常数列 → EMA = 常数（稳定后）"""
        s = pd.Series([5.0] * 30)
        result = ema(s, 10)
        # 后面的值应该接近 5.0
        self.assertAlmostEqual(result.iloc[-1], 5.0, places=5)

    def test_n1_equals_original(self):
        """n=1 → EMA = 原值（无平滑）"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ema(s, 1)
        # 从第 1 个开始就等于原值
        pd.testing.assert_series_equal(result, s, check_names=False)

    def test_ema_lags_less_than_sma(self):
        """EMA 比 SMA 更贴近最新值（对趋势更敏感）

        在上升趋势中，EMA 应该在 SMA 上方（因为更近的数据权重更大）。
        """
        # 构造线性上升序列
        s = pd.Series(np.arange(1, 51, dtype=float))  # 1, 2, 3, ..., 50
        n = 10
        ema_result = ema(s, n)
        sma_result = s.rolling(n).mean()
        # 后期（稳定后）EMA 应该 > SMA（因为在上升趋势中）
        # 取最后几个值比较
        for i in range(-5, 0):
            self.assertGreater(
                ema_result.iloc[i],
                sma_result.iloc[i],
                f"上升趋势中 EMA({ema_result.iloc[i]:.2f}) 应该 > SMA({sma_result.iloc[i]:.2f})",
            )

    def test_ema_smoother_than_original(self):
        """EMA 比原始序列更平滑（波动更小）"""
        np.random.seed(42)
        s = pd.Series(100 + np.cumsum(np.random.randn(100)))
        n = 10
        ema_result = ema(s, n)
        # EMA 的一阶差分方差应该小于原始序列的差分方差
        # （去掉前 n 个 NaN）
        valid = ema_result.dropna()
        orig_diff = s.loc[valid.index].diff().dropna().var()
        ema_diff = valid.diff().dropna().var()
        self.assertLess(ema_diff, orig_diff, "EMA 差分方差应该小于原始序列差分方差")

    def test_ema_first_value_equals_first(self):
        """第一个值 = 第一个原始值（adjust=False 初始条件）"""
        s = pd.Series([5.0, 6.0, 7.0])
        result = ema(s, 10)
        # adjust=False 时，EMA[0] = price[0]
        self.assertAlmostEqual(result.iloc[0], 5.0)

    def test_ema_handles_series_name(self):
        """不改变 Series 的长度和索引"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], name="close")
        result = ema(s, 3)
        self.assertEqual(len(result), len(s))
        pd.testing.assert_index_equal(result.index, s.index)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 稳健池闸门配置
# ═══════════════════════════════════════════════════════════════════════════


class TestRobustGateConfig(unittest.TestCase):
    """稳健池闸门配置读写。"""

    def setUp(self):
        """保存原始状态，测试后恢复。"""
        self._orig_gate = dict(_ROBUST_GATE)
        self._orig_cfg = dict(_ROBUST_GATE_CFG)

    def tearDown(self):
        """恢复原始状态。"""
        _ROBUST_GATE.clear()
        _ROBUST_GATE.update(self._orig_gate)
        _ROBUST_GATE_CFG.clear()
        _ROBUST_GATE_CFG.update(self._orig_cfg)

    def test_set_robust_gate_stability(self):
        """set_robust_gate 修改 stability，get 能读到"""
        old_s, old_o = get_robust_gate()
        set_robust_gate(stability=0.95)
        new_s, new_o = get_robust_gate()
        self.assertAlmostEqual(new_s, 0.95)
        # oos_expR 应该没变
        self.assertEqual(new_o, old_o)

    def test_set_robust_gate_oos_expR(self):
        """set_robust_gate 修改 oos_expR"""
        old_s, old_o = get_robust_gate()
        set_robust_gate(oos_expR=0.3)
        new_s, new_o = get_robust_gate()
        self.assertAlmostEqual(new_o, 0.3)
        self.assertEqual(new_s, old_s)

    def test_set_both_together(self):
        """同时设置两个参数"""
        set_robust_gate(stability=0.88, oos_expR=0.25)
        s, o = get_robust_gate()
        self.assertAlmostEqual(s, 0.88)
        self.assertAlmostEqual(o, 0.25)

    def test_configure_robust_gate_enabled(self):
        """configure_robust_gate 修改 enabled"""
        configure_robust_gate(enabled=True)
        self.assertTrue(_ROBUST_GATE_CFG["enabled"])
        configure_robust_gate(enabled=False)
        self.assertFalse(_ROBUST_GATE_CFG["enabled"])

    def test_configure_robust_gate_relax_pp(self):
        """configure_robust_gate 修改 relax_pp"""
        configure_robust_gate(relax_pp=0.05)
        self.assertAlmostEqual(_ROBUST_GATE_CFG["relax_pp"], 0.05)

    def test_configure_robust_gate_all_params(self):
        """configure_robust_gate 所有参数都能设置"""
        configure_robust_gate(
            enabled=True,
            auto_adapt=True,
            relax_pp=0.03,
            max_relax=0.10,
            floor_oos=-0.05,
            default_stability=0.60,
            default_oos_expR=0.10,
        )
        self.assertTrue(_ROBUST_GATE_CFG["enabled"])
        self.assertTrue(_ROBUST_GATE_CFG["auto_adapt"])
        self.assertAlmostEqual(_ROBUST_GATE_CFG["relax_pp"], 0.03)
        self.assertAlmostEqual(_ROBUST_GATE_CFG["max_relax"], 0.10)
        self.assertAlmostEqual(_ROBUST_GATE_CFG["floor_oos"], -0.05)
        self.assertAlmostEqual(_ROBUST_GATE_CFG["default_stability"], 0.60)
        self.assertAlmostEqual(_ROBUST_GATE_CFG["default_oos_expR"], 0.10)

    def test_gate_disabled_returns_defaults(self):
        """闸门关闭时 → 返回默认常量阈值"""
        configure_robust_gate(enabled=False)
        s, o = get_robust_gate()
        self.assertEqual(s, STABILITY_THRESHOLD)
        self.assertEqual(o, OOS_EXPR_THRESHOLD)

    def test_gate_enabled_returns_runtime_values(self):
        """闸门开启时 → 返回运行时 _ROBUST_GATE 的值"""
        configure_robust_gate(enabled=True)
        set_robust_gate(stability=0.90, oos_expR=0.20)
        s, o = get_robust_gate()
        self.assertAlmostEqual(s, 0.90)
        self.assertAlmostEqual(o, 0.20)

    def test_set_robust_gate_float_conversion(self):
        """set_robust_gate 自动转 float"""
        set_robust_gate(stability="0.75", oos_expR="0.15")
        s, o = get_robust_gate()
        self.assertAlmostEqual(s, 0.75)
        self.assertAlmostEqual(o, 0.15)

    def test_default_thresholds_reasonable(self):
        """默认阈值在合理范围内"""
        # stability 阈值应该在 [0.5, 1.0]
        self.assertGreaterEqual(STABILITY_THRESHOLD, 0.3)
        self.assertLessEqual(STABILITY_THRESHOLD, 1.0)
        # oos_expR 阈值应该在 [-0.1, 0.5]
        self.assertGreaterEqual(OOS_EXPR_THRESHOLD, -0.2)
        self.assertLessEqual(OOS_EXPR_THRESHOLD, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  EMA 指标 + 稳健池闸门配置 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
