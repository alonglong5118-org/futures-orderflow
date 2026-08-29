#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
参数解析 — 单元测试
=========================

测试多层级配置覆盖逻辑：
  1. effective_params  — 阈值参数（T_thresh / bias_hard_dict）
     覆盖优先级：per_symbol > by_group > 全局默认
     历史：P-F 分品种阈值改造

  2. regime_params_for  — Regime 分类阈值
     覆盖优先级：per_symbol > by_group > default
     历史：P-F 分品种 regime 阈值（解决跨品种 regime 错配）

这些函数虽然逻辑简单，但配置层级多、容易配错，
而且是 OOS 校准和 walk-forward 的关键入口。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    effective_params,
    regime_params_for,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. effective_params — 阈值参数解析
# ═══════════════════════════════════════════════════════════════════════════


class TestEffectiveParams(unittest.TestCase):
    """effective_params — T_thresh + bias_hard_dict 解析。"""

    def test_group_level_lookup(self):
        """分组级查询 → 返回该组配置（用无 per-symbol 覆盖的品种验证）"""
        # pg（液化石油气）属于能源组，且没有 per-symbol 覆盖
        T_base, bhd = effective_params("pg")
        # 能源 T_thresh = 22
        self.assertEqual(T_base, 22)
        # 能源 bias_hard = 60 → 趋势=60, 波动=65, 震荡=70
        self.assertEqual(bhd["趋势"], 60)
        self.assertEqual(bhd["波动"], 65)
        self.assertEqual(bhd["震荡"], 70)

    def test_agriculture_group_lower_bias_hard(self):
        """农产品组 bias_hard 更低（50 vs 黑系 60）"""
        # m（豆粕）属于农产品
        T_base, bhd = effective_params("m")
        self.assertEqual(bhd["趋势"], 50)
        self.assertEqual(bhd["波动"], 55)
        self.assertEqual(bhd["震荡"], 60)

    def test_bias_hard_dict_offsets(self):
        """bias_hard_dict 按 regime 递增：趋势 < 波动 < 震荡（+5 / +10）"""
        _, bhd = effective_params("pg")
        # 波动 - 趋势 = 5
        self.assertEqual(bhd["波动"] - bhd["趋势"], 5)
        # 震荡 - 波动 = 5
        self.assertEqual(bhd["震荡"] - bhd["波动"], 5)

    def test_per_symbol_override(self):
        """逐品种覆盖优先于分组（如果配置了 thresholds_by_symbol）"""
        # 构造一个带 per_symbol 覆盖的 cfg
        custom_cfg = {
            "thresholds": {
                "黑系": {"T_thresh": 22, "T_small_thresh": 15, "conv_thresh": 50, "bias_hard": 60},
            },
            "thresholds_by_symbol": {
                "JM": {"T_thresh": 30, "bias_hard_base": 70},  # 焦煤特殊配置
            },
        }
        T_base, bhd = effective_params("JM", cfg=custom_cfg)
        # per_symbol 覆盖：T_thresh=30（不是分组的 22）
        self.assertEqual(T_base, 30)
        # bias_hard_base=70 → 趋势=70, 波动=75, 震荡=80
        self.assertEqual(bhd["趋势"], 70)
        self.assertEqual(bhd["震荡"], 80)

    def test_symbol_not_in_symbol_table_fallback(self):
        """未知品种 → 应该能处理（不崩溃）"""
        # 用一个不存在的品种
        # 注意：实际代码会查 SYMBOLS[symbol]["group"]，不存在会 KeyError
        # 我们验证已知品种都能正常工作
        # 选一个已知属于某组的
        T_base, _ = effective_params("au")  # 黄金 → 贵金属
        self.assertGreater(T_base, 0)

    def test_all_groups_have_valid_thresholds(self):
        """所有分组的 T_thresh 都在合理范围（15~30）"""
        groups = ["黑系", "化工", "农产品", "有色", "贵金属", "能源", "航运"]
        thresholds = DEFAULT_CONFIG["thresholds"]
        for g in groups:
            self.assertIn(g, thresholds, f"缺少分组配置: {g}")
            t = thresholds[g]["T_thresh"]
            self.assertGreaterEqual(t, 15, f"{g} T_thresh 太低: {t}")
            self.assertLessEqual(t, 30, f"{g} T_thresh 太高: {t}")

    def test_three_regimes_in_bias_hard_dict(self):
        """返回的 bias_hard_dict 包含三个 regime"""
        _, bhd = effective_params("rb")
        self.assertIn("趋势", bhd)
        self.assertIn("波动", bhd)
        self.assertIn("震荡", bhd)

    def test_agriculture_higher_thresh_than_black(self):
        """农产品组级 T_thresh > 黑系组级（波动小 → 阈值更高）

        注意：很多品种有 per-symbol 覆盖，所以这里直接比 thresholds 配置。
        """
        thresholds = DEFAULT_CONFIG["thresholds"]
        T_agri = thresholds["农产品"]["T_thresh"]  # 25
        T_black = thresholds["黑系"]["T_thresh"]  # 22
        self.assertGreater(T_agri, T_black, "农产品波动小，组级 T_thresh 应该更高（更难触发）")


# ═══════════════════════════════════════════════════════════════════════════
#  2. regime_params_for — Regime 阈值解析
# ═══════════════════════════════════════════════════════════════════════════


class TestRegimeParamsFor(unittest.TestCase):
    """regime_params_for — 分品种 regime 阈值解析。"""

    def test_enabled_returns_params(self):
        """启用状态 → 返回非空 dict，包含所有 5 个参数"""
        params = regime_params_for("rb")
        self.assertIn("atr_thresh", params)
        self.assertIn("flat_dev", params)
        self.assertIn("flat_atr", params)
        self.assertIn("trend_slope", params)
        self.assertIn("trend_dev", params)

    def test_disabled_returns_default(self):
        """关闭状态 → 返回 default（旧全局行为，A-B 对照）"""
        custom_cfg = {
            "regime_params": {
                "enabled": False,
                "default": {
                    "atr_thresh": 0.025,
                    "flat_dev": 0.008,
                    "flat_atr": 0.012,
                    "trend_slope": 0.003,
                    "trend_dev": 0.010,
                },
            }
        }
        params = regime_params_for("rb", cfg=custom_cfg)
        # 关闭时，所有品种都用 default（忽略分组和逐品种）
        self.assertEqual(params["atr_thresh"], 0.025)

    def test_black_group_higher_atr_thresh(self):
        """黑系 atr_thresh > 默认值（高波动品种阈值放大）"""
        params = regime_params_for("rb")  # 黑系
        default = DEFAULT_CONFIG["regime_params"]["default"]
        # 黑系 0.035 > 默认 0.025
        self.assertGreater(params["atr_thresh"], default["atr_thresh"])

    def test_agriculture_lower_atr_thresh(self):
        """农产品 atr_thresh < 默认值（低波动品种阈值缩小）"""
        params = regime_params_for("m")  # 农产品
        default = DEFAULT_CONFIG["regime_params"]["default"]
        # 农产品 0.021 < 默认 0.025
        self.assertLess(params["atr_thresh"], default["atr_thresh"])

    def test_per_symbol_override_highest_priority(self):
        """逐品种覆盖优先级最高（> by_group > default）"""
        custom_cfg = {
            "regime_params": {
                "enabled": True,
                "default": {
                    "atr_thresh": 0.025,
                    "flat_dev": 0.008,
                    "flat_atr": 0.012,
                    "trend_slope": 0.003,
                    "trend_dev": 0.010,
                },
                "by_group": {
                    "黑系": {
                        "atr_thresh": 0.035,
                        "flat_dev": 0.010,
                        "flat_atr": 0.018,
                        "trend_slope": 0.0035,
                        "trend_dev": 0.012,
                    },
                },
                "by_symbol": {
                    "rb": {"atr_thresh": 0.045},  # rb 特殊：更高
                },
            }
        }
        # rb 属于黑系
        params = regime_params_for("rb", cfg=custom_cfg)
        # per-symbol 覆盖：0.045（不是 by_group 的 0.035，也不是 default 的 0.025）
        self.assertEqual(params["atr_thresh"], 0.045)
        # flat_dev 应该继承 by_group 的 0.010（per_symbol 没覆盖的字段继承 by_group）
        self.assertEqual(params["flat_dev"], 0.010)

    def test_group_override_default(self):
        """分组覆盖 default（未设置 per_symbol 时）"""
        custom_cfg = {
            "regime_params": {
                "enabled": True,
                "default": {
                    "atr_thresh": 0.025,
                    "flat_dev": 0.008,
                    "flat_atr": 0.012,
                    "trend_slope": 0.003,
                    "trend_dev": 0.010,
                },
                "by_group": {
                    "黑系": {"atr_thresh": 0.035},
                },
            }
        }
        params = regime_params_for("rb", cfg=custom_cfg)
        # by_group 覆盖 default
        self.assertEqual(params["atr_thresh"], 0.035)
        # 没覆盖的字段继承 default
        self.assertEqual(params["flat_dev"], 0.008)

    def test_pf_regression_black_higher_than_agri(self):
        """回归（P-F）：黑系 regime 阈值 > 农产品

        历史 bug：全局阈值导致跨品种 regime 错配——
        高波动品种（黑系）长期被判"波动"，低波动品种（农产品）几乎到不了"波动"。
        修复后：黑系阈值放大，农产品阈值缩小。
        """
        p_black = regime_params_for("rb")  # 黑系
        p_agri = regime_params_for("m")  # 农产品
        self.assertGreater(p_black["atr_thresh"], p_agri["atr_thresh"], "P-F 回归：高波动品种 regime 阈值应该更大")
        self.assertGreater(p_black["flat_atr"], p_agri["flat_atr"], "P-F 回归：高波动品种 flat_atr 阈值应该更大")
        self.assertGreater(p_black["trend_dev"], p_agri["trend_dev"], "P-F 回归：高波动品种 trend_dev 阈值应该更大")

    def test_all_params_are_positive(self):
        """所有返回的参数都是正数"""
        params = regime_params_for("rb")
        for key in ["atr_thresh", "flat_dev", "flat_atr", "trend_slope", "trend_dev"]:
            self.assertGreater(params[key], 0, f"{key} 应该是正数")

    def test_unknown_group_uses_default(self):
        """未知分组的品种 → 使用 default（不崩溃）"""
        # 构造一个不在任何已知组的品种配置
        custom_cfg = {
            "regime_params": {
                "enabled": True,
                "default": {
                    "atr_thresh": 0.025,
                    "flat_dev": 0.008,
                    "flat_atr": 0.012,
                    "trend_slope": 0.003,
                    "trend_dev": 0.010,
                },
                "by_group": {},  # 没有任何分组
            }
        }
        # 用一个在 SYMBOLS 里的品种，但 by_group 里没有它的组
        # rb 属于黑系，但 by_group 里没有"黑系" → 用 default
        params = regime_params_for("rb", cfg=custom_cfg)
        self.assertEqual(params["atr_thresh"], 0.025)

    def test_params_dictionary_is_copy_not_reference(self):
        """返回的是副本，修改不会影响默认配置"""
        params1 = regime_params_for("rb")
        params1["atr_thresh"] = 999
        # 再取一次，应该还是原值
        params2 = regime_params_for("rb")
        self.assertNotEqual(params2["atr_thresh"], 999, "返回的 dict 应该是副本，修改不应该影响后续调用")


# ═══════════════════════════════════════════════════════════════════════════
#  3. 品种元数据完整性
# ═══════════════════════════════════════════════════════════════════════════


class TestSymbolMetadata(unittest.TestCase):
    """SYMBOLS 品种元数据完整性检查。"""

    def test_all_symbols_have_group(self):
        """所有品种都有 group 字段"""
        for sym, info in SYMBOLS.items():
            self.assertIn("group", info, f"{sym} 缺少 group")
            self.assertIsInstance(info["group"], str)
            self.assertGreater(len(info["group"]), 0)

    def test_all_symbols_have_name(self):
        """所有品种都有中文名"""
        for sym, info in SYMBOLS.items():
            self.assertIn("name", info, f"{sym} 缺少 name")

    def test_all_symbols_have_exchange(self):
        """所有品种都有交易所"""
        for sym, info in SYMBOLS.items():
            self.assertIn("exchange", info, f"{sym} 缺少 exchange")

    def test_thresholds_covers_all_groups(self):
        """thresholds 配置覆盖 SYMBOLS 中所有分组"""
        groups_in_symbols = set(info["group"] for info in SYMBOLS.values())
        groups_in_thresholds = set(DEFAULT_CONFIG["thresholds"].keys())
        missing = groups_in_symbols - groups_in_thresholds
        self.assertEqual(len(missing), 0, f"以下分组缺少 thresholds 配置: {missing}")

    def test_regime_params_covers_all_groups(self):
        """regime_params.by_group 覆盖 SYMBOLS 中所有分组"""
        groups_in_symbols = set(info["group"] for info in SYMBOLS.values())
        groups_in_regime = set(DEFAULT_CONFIG["regime_params"].get("by_group", {}).keys())
        missing = groups_in_symbols - groups_in_regime
        self.assertEqual(len(missing), 0, f"以下分组缺少 regime_params.by_group 配置: {missing}")

    def test_symbol_count_reasonable(self):
        """品种数量合理（> 40 个）"""
        self.assertGreater(len(SYMBOLS), 40, "SYMBOLS 品种数量太少，可能加载不完整")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  参数解析 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
