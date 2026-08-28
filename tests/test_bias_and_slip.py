#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流动性滑点 + 背景偏置合成 — 单元测试
=========================================

两个轻量但关键的纯函数模块：

1. 流动性滑点分级（get_slip_pts）
   - 逐合约微调优先（contract_specs[sym]["slip"]）
   - 流动性分级表（LIQUIDITY_SLIP，大小写不敏感）
   - 全局兜底（risk_gate.slip_pts）
   - 历史：P2 流动性敏感滑点（从全局固定 1 点改为分级）

2. 背景偏置合成（combine_bias）
   - T/F/C 加权求和
   - 权重读 cfg["combine_weights"]
   - 历史：P2-④ 权重配置化（从硬编码改为可扫参）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    combine_bias,
)


class TestCombineBias(unittest.TestCase):
    """combine_bias — T/F/C 背景偏置加权合成。"""

    def test_default_weights_all_positive(self):
        """默认权重 + 都正 → 正值"""
        # 默认权重：T=0.6, F=0.25, C=0.15
        # bias = 0.6*60 + 0.25*40 + 0.15*30 = 36 + 10 + 4.5 = 50.5
        result = combine_bias(F=40, T=60, C=30)
        self.assertAlmostEqual(result, 50.5, places=1)

    def test_T_dominates(self):
        """T 权重最大（0.6），对结果影响最大"""
        # T 从 0 变到 100 → 变化 60 点
        r_low = combine_bias(F=0, T=0, C=0)
        r_high = combine_bias(F=0, T=100, C=0)
        self.assertAlmostEqual(r_high - r_low, 60.0, places=1)

    def test_F_second_dominant(self):
        """F 权重第二（0.25）"""
        r_low = combine_bias(F=0, T=0, C=0)
        r_high = combine_bias(F=100, T=0, C=0)
        self.assertAlmostEqual(r_high - r_low, 25.0, places=1)

    def test_C_smallest_weight(self):
        """C 权重最小（0.15）"""
        r_low = combine_bias(F=0, T=0, C=0)
        r_high = combine_bias(F=0, T=0, C=100)
        self.assertAlmostEqual(r_high - r_low, 15.0, places=1)

    def test_weights_sum_to_1(self):
        """默认权重之和 = 1.0（归一化验证）"""
        w = DEFAULT_CONFIG.get("combine_weights", {})
        self.assertAlmostEqual(w["T"] + w["F"] + w["C"], 1.0, places=4)

    def test_negative_values(self):
        """负值输入 → 负的 bias"""
        # 0.6*(-50) + 0.25*(-30) + 0.15*(-20) = -30 - 7.5 - 3 = -40.5
        result = combine_bias(F=-30, T=-50, C=-20)
        self.assertAlmostEqual(result, -40.5, places=1)

    def test_mixed_signs(self):
        """正负混合 → 互相抵消"""
        # T 正，F 负，C 正
        # 0.6*50 + 0.25*(-40) + 0.15*30 = 30 - 10 + 4.5 = 24.5
        result = combine_bias(F=-40, T=50, C=30)
        self.assertAlmostEqual(result, 24.5, places=1)

    def test_all_zero_zero_result(self):
        """都为 0 → 结果 0"""
        self.assertEqual(combine_bias(F=0, T=0, C=0), 0.0)

    def test_custom_weights_from_cfg(self):
        """自定义权重（通过 cfg 传入）"""
        custom_cfg = {"combine_weights": {"T": 0.5, "F": 0.3, "C": 0.2}}
        # 0.5*60 + 0.3*40 + 0.2*30 = 30 + 12 + 6 = 48
        result = combine_bias(F=40, T=60, C=30, cfg=custom_cfg)
        self.assertAlmostEqual(result, 48.0, places=1)

    def test_custom_weights_different_from_default(self):
        """自定义权重结果与默认不同"""
        default_result = combine_bias(F=100, T=0, C=0)
        custom_cfg = {"combine_weights": {"T": 0.2, "F": 0.6, "C": 0.2}}
        custom_result = combine_bias(F=100, T=0, C=0, cfg=custom_cfg)
        # F 权重从 0.25 提到 0.6 → 结果应该更大
        self.assertGreater(custom_result, default_result)

    def test_p2_regression_weights_configurable(self):
        """回归（P2-④）：权重可配置，不是硬编码

        历史：权重曾经是硬编码的，OOS 扫参时很难调。
        现在通过 cfg["combine_weights"] 可覆盖。
        """
        # 验证：极端权重（T=1.0, F=0, C=0）能生效
        extreme_cfg = {"combine_weights": {"T": 1.0, "F": 0, "C": 0}}
        result = combine_bias(F=100, T=50, C=100, cfg=extreme_cfg)
        # 只有 T 有贡献 → 50
        self.assertAlmostEqual(result, 50.0, places=1, msg="P2-④ 回归：combine_weights 配置没有生效，还是用的硬编码值")

    def test_result_rounded_to_1_decimal(self):
        """结果保留 1 位小数"""
        # 0.6 * 33 + 0.25 * 11 + 0.15 * 7 = 19.8 + 2.75 + 1.05 = 23.6
        result = combine_bias(F=11, T=33, C=7)
        # 23.6 恰好是 1 位小数
        self.assertEqual(result, 23.6)


# ═══════════════════════════════════════════════════════════════════════════
#  流动性滑点分级
# ═══════════════════════════════════════════════════════════════════════════


class TestSlipPts(unittest.TestCase):
    """get_slip_pts — 流动性敏感滑点分级。"""

    def _import_slip_fn(self):
        """从 four_dim_strategy 导入 get_slip_pts。"""
        sys.path.insert(0, ROOT)
        from four_dim_strategy import get_slip_pts

        return get_slip_pts

    def test_a_tier_super_liquid(self):
        """A 档超流动品种 → 1.0 点滑点"""
        get_slip_pts = self._import_slip_fn()
        # rb（螺纹钢）是 A 档
        self.assertEqual(get_slip_pts("rb"), 1.0)

    def test_a_tier_gold(self):
        """黄金（au）→ A 档 1.0"""
        get_slip_pts = self._import_slip_fn()
        self.assertEqual(get_slip_pts("au"), 1.0)

    def test_b_tier_mid_liquid(self):
        """B 档中流动品种 → 1.5 点滑点"""
        get_slip_pts = self._import_slip_fn()
        # fg（玻璃）是 B 档
        self.assertEqual(get_slip_pts("fg"), 1.5)

    def test_c_tier_low_liquid(self):
        """C 档低流动品种 → 2.0 点滑点"""
        get_slip_pts = self._import_slip_fn()
        # ap（苹果）是 C 档
        self.assertEqual(get_slip_pts("ap"), 2.0)

    def test_case_insensitive_lookup(self):
        """大小写不敏感（大写品种名也能查到）"""
        get_slip_pts = self._import_slip_fn()
        # J/JM 等是大写命名的
        # jm 小写也能查到（因为 lower() 了）
        self.assertEqual(get_slip_pts("jm"), 1.0)
        # JM 大写也能查到（有兜底的原大小写查找）
        self.assertEqual(get_slip_pts("JM"), 1.0)

    def test_unknown_symbol_fallback_to_default(self):
        """未知品种 → 回退全局默认值（1.0）"""
        get_slip_pts = self._import_slip_fn()
        # 随便一个不存在的品种
        result = get_slip_pts("xyz_nonexistent")
        # 全局默认 1.0
        self.assertEqual(result, 1.0)

    def test_contract_specs_override(self):
        """逐合约微调（contract_specs[sym]["slip"]）优先级最高"""
        get_slip_pts = self._import_slip_fn()
        # 构造一个带 contract_specs 的 cfg
        custom_cfg = {
            "contract_specs": {
                "rb": {"slip": 0.5},  # 螺纹钢特殊设置 0.5 点
            },
            "risk_gate": {"slip_pts": 1.0},
        }
        result = get_slip_pts("rb", cfg=custom_cfg)
        # 逐合约覆盖优先
        self.assertEqual(result, 0.5)

    def test_custom_default_via_cfg(self):
        """通过 cfg 修改全局默认滑点"""
        get_slip_pts = self._import_slip_fn()
        custom_cfg = {
            "risk_gate": {"slip_pts": 2.5},
        }
        # 未知品种 + 自定义默认 → 2.5
        result = get_slip_pts("xyz_unknown", cfg=custom_cfg)
        self.assertEqual(result, 2.5)

    def test_p2_regression_not_uniform(self):
        """回归（P2）：不同品种滑点不同，不是全局统一 1 点

        历史：原滑点 slip_pts=1 全局固定，对低流动品种严重低估冲击成本。
        修复后：按流动性分级，A/B/C 三档不同。
        """
        get_slip_pts = self._import_slip_fn()
        # 超流动品种（rb）和低流动品种（ap）滑点不同
        slip_high = get_slip_pts("rb")  # A 档 1.0
        slip_low = get_slip_pts("ap")  # C 档 2.0
        self.assertGreater(slip_low, slip_high, "P2 回归：低流动品种滑点应该更高，不是全局统一的")
        # 验证确实是分级的（C 档是 A 档的 2 倍）
        self.assertAlmostEqual(slip_low / slip_high, 2.0, places=1)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  流动性滑点 + 背景偏置合成 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
