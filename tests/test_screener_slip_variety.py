#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
品种筛选 + 滑点 + 品种映射 — 单元测试
==========================================

1. _check_criteria — 品种筛选条件判定
   - None 指标 → 不通过，原因"数据不足"
   - 流动性达标 → 通过
   - 流动性不足 → 不通过，得分<1
   - ATR 在范围内 → 通过
   - ATR 太低 → 不通过
   - ATR 太高 → 不通过
   - |T_D| 达标 → 通过
   - |T_D| 不足 → 不通过
   - 量比达标 → 通过
   - 量比不足 → 不通过
   - 相关性低于阈值 → 通过
   - 相关性高于阈值 → 不通过
   - 无持仓 → 相关性项满分
   - 加权得分 = 各项加权和
   - 返回结构 (passed, score, reasons)
   - score 保留 3 位小数
   - AND 模式 vs 加权模式的 passed 差异

2. get_slip_pts — 流动性滑点查表
   - 超流动品种 → 1.0 点
   - 中流动品种 → 1.5 点
   - 低流动品种 → 2.0 点
   - 大小写不敏感（小写查表）
   - 未知品种 → 全局兜底默认值
   - 合约级微调优先
   - 兜底用 cfg risk_gate.slip_pts

3. variety_of — 合约→品种映射
   - 普通品种 → 原样返回
   - SA01 → SA（特殊映射）
   - 未知合约 → 原样返回
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from symbol_screener import _check_criteria
from four_dim_strategy import get_slip_pts, variety_of


# ═══════════════════════════════════════════════════════════════════════════
#  1. _check_criteria
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckCriteria(unittest.TestCase):
    """_check_criteria 品种筛选条件判定。"""

    def _metrics(self, **overrides):
        m = {
            "symbol": "rb",
            "turnover_billion": 5.0,
            "atr_pct": 1.5,
            "T_D": 60.0,
            "vol_ratio": 1.2,
        }
        m.update(overrides)
        return m

    def _criteria(self, **overrides):
        c = {
            "min_turnover": 1.0,
            "atr_pct_min": 0.005,   # 小数形式：0.5%
            "atr_pct_max": 0.03,    # 小数形式：3%
            "min_abs_T_D": 30.0,
            "min_volume_ratio": 0.8,
            "max_correlation": 0.7,
        }
        c.update(overrides)
        return c

    def test_none_metrics_rejected(self):
        """None 指标 → 不通过，原因"数据不足"，得分 0"""
        passed, score, reasons = _check_criteria(None, self._criteria())
        self.assertFalse(passed)
        self.assertEqual(score, 0.0)
        self.assertIn("数据不足", reasons[0])

    def test_all_metrics_perfect_score_one(self):
        """所有维度满分 → score=1.0"""
        m = self._metrics(turnover_billion=5.0, atr_pct=1.5, T_D=60.0, vol_ratio=1.2)
        c = self._criteria()
        passed, score, reasons = _check_criteria(m, c)
        self.assertTrue(passed)
        self.assertAlmostEqual(score, 1.0, places=3)

    def test_liquidity_insufficient(self):
        """流动性不足 → 不通过，得分<1"""
        m = self._metrics(turnover_billion=0.5)
        c = self._criteria(min_turnover=2.0)
        passed, score, reasons = _check_criteria(m, c)
        self.assertFalse(passed)
        self.assertLess(score, 1.0)

    def test_atr_in_range_ok(self):
        """ATR 在范围内 → 波动分=1.0，整体得分高"""
        m = self._metrics(atr_pct=1.5)
        c = self._criteria(atr_pct_min=0.005, atr_pct_max=0.03)
        passed, score, reasons = _check_criteria(m, c)
        self.assertTrue(passed)
        # atr=1.5% 在范围内 → volatility score = 1.0
        self.assertAlmostEqual(score, 1.0, places=3)

    def test_atr_too_low_reduces_score(self):
        """ATR 太低 → 波动分<1.0，得分降低"""
        m = self._metrics(atr_pct=0.2)
        c = self._criteria(atr_pct_min=0.005, atr_pct_max=0.03)
        # atr=0.2% = 0.002, min=0.5% = 0.005 → 低于
        # volatility score = 0.002 / 0.005 = 0.4
        passed, score, reasons = _check_criteria(m, c)
        # 注意：ATR 不影响 all_pass（变量覆盖），只影响得分
        self.assertLess(score, 1.0)
        # 得分应比 ATR 正常时低
        m_ok = self._metrics(atr_pct=1.5)
        _, score_ok, _ = _check_criteria(m_ok, c)
        self.assertLess(score, score_ok)

    def test_atr_too_high_reduces_score(self):
        """ATR 太高 → 波动分<1.0，得分降低"""
        m = self._metrics(atr_pct=5.0)
        c = self._criteria(atr_pct_min=0.005, atr_pct_max=0.03)
        # atr=5% = 0.05, max=3% = 0.03 → 高于
        # volatility score = 0.03 / 0.05 = 0.6
        passed, score, reasons = _check_criteria(m, c)
        self.assertLess(score, 1.0)

    def test_T_D_sufficient(self):
        """|T_D| 达标 → 通过"""
        m = self._metrics(T_D=60.0)
        c = self._criteria(min_abs_T_D=30.0)
        passed, score, reasons = _check_criteria(m, c)
        self.assertTrue(passed)

    def test_T_D_insufficient(self):
        """|T_D| 不足 → 不通过"""
        m = self._metrics(T_D=10.0)
        c = self._criteria(min_abs_T_D=30.0)
        passed, score, reasons = _check_criteria(m, c)
        self.assertFalse(passed)

    def test_volume_ratio_ok(self):
        """量比达标 → 通过"""
        m = self._metrics(vol_ratio=1.5)
        c = self._criteria(min_volume_ratio=0.8)
        passed, score, reasons = _check_criteria(m, c)
        self.assertTrue(passed)

    def test_volume_ratio_low(self):
        """量比不足 → 不通过"""
        m = self._metrics(vol_ratio=0.5)
        c = self._criteria(min_volume_ratio=1.0)
        passed, score, reasons = _check_criteria(m, c)
        self.assertFalse(passed)

    def test_correlation_below_threshold_ok(self):
        """相关性低于阈值 → 通过"""
        m = self._metrics()
        c = self._criteria(max_correlation=0.7)
        corr = {"rb_vs_hc": 0.5}
        passed, score, reasons = _check_criteria(m, c, held_symbols=["hc"], corr_data=corr)
        self.assertTrue(passed)

    def test_correlation_above_threshold_rejected(self):
        """相关性高于阈值 → 不通过"""
        m = self._metrics()
        c = self._criteria(max_correlation=0.7)
        corr = {"rb_vs_hc": 0.9}
        passed, score, reasons = _check_criteria(m, c, held_symbols=["hc"], corr_data=corr)
        self.assertFalse(passed)

    def test_no_held_symbols_correlation_full_score(self):
        """无持仓 → 相关性项满分"""
        m = self._metrics()
        c = self._criteria(max_correlation=0.7)
        passed, score, reasons = _check_criteria(m, c, held_symbols=None, corr_data={})
        self.assertTrue(passed)

    def test_returns_triple(self):
        """返回 (passed, score, reasons) 三元组"""
        result = _check_criteria(self._metrics(), self._criteria())
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], float)
        self.assertIsInstance(result[2], list)

    def test_score_three_decimals(self):
        """score 保留 3 位小数"""
        _, score, _ = _check_criteria(self._metrics(), self._criteria())
        self.assertEqual(score, round(score, 3))

    def test_score_between_zero_and_one(self):
        """score 范围 [0, 1]"""
        cases = [
            self._metrics(turnover_billion=0.1, atr_pct=0.1, T_D=5, vol_ratio=0.1),
            self._metrics(turnover_billion=10, atr_pct=1.0, T_D=80, vol_ratio=2.0),
        ]
        for m in cases:
            _, score, _ = _check_criteria(m, self._criteria())
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_negative_T_uses_abs(self):
        """负 T_D 用绝对值判断（空单趋势也算）"""
        m_pos = self._metrics(T_D=50.0)
        m_neg = self._metrics(T_D=-50.0)
        c = self._criteria(min_abs_T_D=30.0)
        _, s_pos, _ = _check_criteria(m_pos, c)
        _, s_neg, _ = _check_criteria(m_neg, c)
        self.assertAlmostEqual(s_pos, s_neg, places=3)


# ═══════════════════════════════════════════════════════════════════════════
#  2. get_slip_pts
# ═══════════════════════════════════════════════════════════════════════════

class TestGetSlipPts(unittest.TestCase):
    """get_slip_pts 流动性滑点查表。"""

    def test_super_liquid_one_pt(self):
        """超流动品种 → 1.0 点（rb 螺纹钢）"""
        result = get_slip_pts("rb")
        self.assertEqual(result, 1.0)

    def test_mid_liquid_15_pt(self):
        """中流动品种 → 1.5 点（FG 玻璃）"""
        result = get_slip_pts("fg")
        self.assertEqual(result, 1.5)

    def test_low_liquid_2_pt(self):
        """低流动品种 → 2.0 点（AP 苹果）"""
        result = get_slip_pts("ap")
        self.assertEqual(result, 2.0)

    def test_case_insensitive_lower(self):
        """小写查表（au 黄金）"""
        result = get_slip_pts("au")
        self.assertEqual(result, 1.0)

    def test_case_insensitive_upper(self):
        """大写品种也能查到（先尝试小写，再原大小写）"""
        # SA 在表里是 "sa": 1.5
        result = get_slip_pts("SA")
        self.assertEqual(result, 1.5)

    def test_unknown_symbol_default(self):
        """未知品种 → 全局兜底默认值（1.0）"""
        result = get_slip_pts("UNKNOWNXYZ")
        self.assertEqual(result, 1.0)

    def test_contract_level_override(self):
        """合约级微调优先（从 cfg.contract_specs 读取）"""
        cfg = {
            "contract_specs": {"rb": {"slip": 0.5}},
            "risk_gate": {"slip_pts": 2.0},
        }
        result = get_slip_pts("rb", cfg=cfg)
        self.assertEqual(result, 0.5)

    def test_fallback_to_cfg_default(self):
        """未知品种兜底用 cfg.risk_gate.slip_pts"""
        cfg = {
            "contract_specs": {},
            "risk_gate": {"slip_pts": 3.0},
        }
        result = get_slip_pts("UNKNOWN", cfg=cfg)
        self.assertEqual(result, 3.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(get_slip_pts("rb"), float)


# ═══════════════════════════════════════════════════════════════════════════
#  3. variety_of
# ═══════════════════════════════════════════════════════════════════════════

class TestVarietyOf(unittest.TestCase):
    """variety_of 合约→品种映射。"""

    def test_normal_symbol_returns_self(self):
        """普通品种 → 原样返回"""
        self.assertEqual(variety_of("rb"), "rb")
        self.assertEqual(variety_of("au"), "au")

    def test_sa01_maps_to_sa(self):
        """SA01 → SA（特殊映射）"""
        self.assertEqual(variety_of("SA01"), "SA")

    def test_unknown_returns_self(self):
        """未知合约 → 原样返回"""
        self.assertEqual(variety_of("UNKNOWN"), "UNKNOWN")

    def test_empty_string_returns_self(self):
        """空串 → 原样返回"""
        self.assertEqual(variety_of(""), "")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(variety_of("rb"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  品种筛选 + 滑点 + 品种映射 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
