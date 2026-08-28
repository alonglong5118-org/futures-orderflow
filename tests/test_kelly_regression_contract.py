#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kelly因子 + 回归状态 + 合约工具 — 单元测试
==============================================

1. compute_kelly_factor — Kelly 仓位缩放系数
   - 无校准数据 → 1.0
   - edge=0 → kelly_min
   - edge=target_edge → kelly_max
   - edge>target_edge → 封顶 kelly_max
   - 负 edge → kelly_min（按 0 处理）
   - 近景门槛：近景负时封顶 1.0
   - 参数防御：非法输入 → 1.0
   - kelly_min > kelly_max → 自动交换
   - target_edge<=0 → 拉满

2. classify_status — 回归测试状态分类
   - 全 ok → ok
   - 单个 warn → warn
   - 单个 critical → critical
   - 多个 critical → critical（计数）
   - None 维度跳过
   - sig_agree 方向：越低越差

3. calc_signal_agreement — 信号一致率
   - 都为空 → 1.0
   - 一个为空 → 0.0
   - 完全一致 → 1.0
   - 部分一致 → 交集 / max(基线大小, 1)
   - 完全不同 → 0.0

4. _is_tradeable_contract — 可交易合约判断
   - 标准合约 → True
   - 主连（无数字尾）→ False
   - 3位数字 → True
   - 空串 → False

5. _contract_ym — 合约年月解析
   - 4位数字 → YYYYMM
   - 3位数字 → YYYYMM
   - 无法识别 → None
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from kelly_utils import compute_kelly_factor
from minishare_live import _contract_ym, _is_tradeable_contract
from regression_test import calc_signal_agreement, classify_status

# ═══════════════════════════════════════════════════════════════════════════
#  1. compute_kelly_factor
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeKellyFactor(unittest.TestCase):
    """compute_kelly_factor Kelly 仓位缩放系数。"""

    def test_none_edge_returns_one(self):
        """无校准数据(None) → 1.0"""
        self.assertEqual(compute_kelly_factor(None), 1.0)

    def test_zero_edge_returns_min(self):
        """edge=0 → kelly_min(0.6)"""
        result = compute_kelly_factor(0.0)
        self.assertAlmostEqual(result, 0.6)

    def test_full_edge_returns_max(self):
        """edge=target_edge → kelly_max(1.2)"""
        result = compute_kelly_factor(0.5, target_edge=0.5)
        self.assertAlmostEqual(result, 1.2)

    def test_above_target_capped(self):
        """edge > target_edge → 封顶 k_max"""
        result = compute_kelly_factor(1.0, target_edge=0.5)
        self.assertAlmostEqual(result, 1.2)

    def test_negative_edge_returns_min(self):
        """负 edge → kelly_min（按 0 处理）"""
        result = compute_kelly_factor(-0.2)
        self.assertAlmostEqual(result, 0.6)

    def test_half_edge_linear(self):
        """edge = target_edge/2 → 中间值 (0.6+1.2)/2 = 0.9"""
        result = compute_kelly_factor(0.25, target_edge=0.5)
        self.assertAlmostEqual(result, 0.9)

    def test_near_term_negative_cap_1(self):
        """近景负期望 → 封顶 1.0"""
        # edge=0.5 本应得到 1.2，但近景负 → 封顶 1.0
        result = compute_kelly_factor(0.5, cur_full_expR=-0.1)
        self.assertAlmostEqual(result, 1.0)

    def test_near_term_positive_no_cap(self):
        """近景正期望 → 不封顶，正常 1.2"""
        result = compute_kelly_factor(0.5, cur_full_expR=0.3)
        self.assertAlmostEqual(result, 1.2)

    def test_near_term_none_fallback_edge(self):
        """近景为 None → 退回远 edge 符号（正 → 不封顶）"""
        result = compute_kelly_factor(0.5, cur_full_expR=None)
        self.assertAlmostEqual(result, 1.2)

    def test_near_term_none_negative_edge(self):
        """近景 None + 负 edge → 封顶 1.0（但 min=0.6 本就 < 1）"""
        result = compute_kelly_factor(-0.2, cur_full_expR=None)
        self.assertAlmostEqual(result, 0.6)

    def test_invalid_edge_returns_one(self):
        """非法 edge → 1.0"""
        self.assertEqual(compute_kelly_factor("abc"), 1.0)
        self.assertEqual(compute_kelly_factor([]), 1.0)

    def test_invalid_params_returns_one(self):
        """非法参数 → 1.0"""
        self.assertEqual(compute_kelly_factor(0.5, kelly_min="bad"), 1.0)

    def test_min_greater_than_max_swapped(self):
        """kelly_min > kelly_max → 自动交换"""
        result = compute_kelly_factor(0.5, kelly_min=1.2, kelly_max=0.6)
        self.assertAlmostEqual(result, 1.2)  # edge满时取max

    def test_zero_target_edge_pulls_max(self):
        """target_edge=0 → 拉满 kelly_max"""
        result = compute_kelly_factor(0.5, target_edge=0.0)
        self.assertAlmostEqual(result, 1.2)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(compute_kelly_factor(0.5), float)
        self.assertIsInstance(compute_kelly_factor(None), float)

    def test_custom_params(self):
        """自定义参数范围"""
        result = compute_kelly_factor(1.0, kelly_min=0.5, kelly_max=2.0, target_edge=2.0)
        # edge=1.0, target=2.0 → ratio=0.5 → 0.5 + (2.0-0.5)*0.5 = 0.5 + 0.75 = 1.25
        self.assertAlmostEqual(result, 1.25)


# ═══════════════════════════════════════════════════════════════════════════
#  2. classify_status
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyStatus(unittest.TestCase):
    """classify_status 回归测试状态分类。

    阈值:
    - expr_delta: warn=0.015, crit=0.030
    - win_delta: warn=0.03, crit=0.06
    - trades_pct_delta: warn=0.15, crit=0.30
    - sig_agree: warn=0.95, crit=0.90 (越低越差)
    """

    def test_all_ok(self):
        """全 ok → ok, 0, 0"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 1.0)
        self.assertEqual(status, "ok")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 0)

    def test_single_warn_expr(self):
        """单个 warn（expr）→ warn, 0, 1"""
        status, crits, warns = classify_status(0.02, 0.0, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 1)

    def test_single_critical_expr(self):
        """单个 critical（expr）→ critical, 1, 0"""
        status, crits, warns = classify_status(0.04, 0.0, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)
        self.assertEqual(warns, 0)

    def test_win_delta_warn(self):
        """win_delta warn → warn"""
        status, crits, warns = classify_status(0.0, 0.04, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(warns, 1)

    def test_win_delta_critical(self):
        """win_delta critical → critical"""
        status, crits, warns = classify_status(0.0, 0.08, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)

    def test_trades_pct_warn(self):
        """trades_pct warn → warn"""
        status, crits, warns = classify_status(0.0, 0.0, 0.2, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(warns, 1)

    def test_trades_pct_critical(self):
        """trades_pct critical → critical"""
        status, crits, warns = classify_status(0.0, 0.0, 0.4, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)

    def test_sig_agree_warn(self):
        """sig_agree warn（0.90 < x < 0.95）→ warn"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.93)
        self.assertEqual(status, "warn")
        self.assertEqual(warns, 1)

    def test_sig_agree_critical(self):
        """sig_agree critical（< 0.90）→ critical"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.85)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)

    def test_sig_agree_boundary_warn(self):
        """sig_agree = 0.95 边界 → ok（刚好等于不触发）"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.95)
        self.assertEqual(status, "ok")

    def test_sig_agree_boundary_crit(self):
        """sig_agree = 0.90 边界 → warn（刚好等于不触发 critical）"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.90)
        self.assertEqual(status, "warn")
        self.assertEqual(warns, 1)

    def test_none_dimensions_skipped(self):
        """None 维度跳过 → ok"""
        status, crits, warns = classify_status(None, None, None, None)
        self.assertEqual(status, "ok")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 0)

    def test_multiple_criticals(self):
        """多个 critical → critical + 计数"""
        # expr critical + win critical = 2 criticals
        status, crits, warns = classify_status(0.04, 0.08, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 2)
        self.assertEqual(warns, 0)

    def test_critical_priority_over_warn(self):
        """critical 优先级高于 warn"""
        # 1 critical + 1 warn → critical
        status, crits, warns = classify_status(0.04, 0.04, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)
        self.assertEqual(warns, 1)

    def test_negative_values_abs(self):
        """负值取绝对值"""
        status, crits, warns = classify_status(-0.04, -0.08, -0.4, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 3)

    def test_boundary_expr_warn(self):
        """expr_delta = 0.015 边界 → ok（刚好等于不触发）"""
        status, _, _ = classify_status(0.015, 0.0, 0.0, 1.0)
        self.assertEqual(status, "ok")

    def test_boundary_expr_crit(self):
        """expr_delta = 0.030 边界 → warn（刚好等于不触发 crit）"""
        status, crits, warns = classify_status(0.030, 0.0, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 1)

    def test_returns_tuple(self):
        """返回 (status, crits, warns) 三元组"""
        result = classify_status(0.0, 0.0, 0.0, 1.0)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], int)
        self.assertIsInstance(result[2], int)


# ═══════════════════════════════════════════════════════════════════════════
#  3. calc_signal_agreement
# ═══════════════════════════════════════════════════════════════════════════


class TestCalcSignalAgreement(unittest.TestCase):
    """calc_signal_agreement 信号一致率。"""

    def test_both_empty(self):
        """都为空 → 1.0"""
        self.assertEqual(calc_signal_agreement([], []), 1.0)

    def test_one_empty(self):
        """一个为空 → 0.0"""
        self.assertEqual(calc_signal_agreement(["a"], []), 0.0)
        self.assertEqual(calc_signal_agreement([], ["a"]), 0.0)

    def test_identical(self):
        """完全一致 → 1.0"""
        self.assertEqual(calc_signal_agreement(["a", "b"], ["a", "b"]), 1.0)

    def test_partial_overlap(self):
        """部分重叠 → 交集 / max(基线大小, 1)"""
        # 交集=1, 基线大小=2 → 0.5
        result = calc_signal_agreement(["a", "c"], ["a", "b"])
        self.assertEqual(result, 0.5)

    def test_no_overlap(self):
        """完全不重叠 → 0.0"""
        self.assertEqual(calc_signal_agreement(["a"], ["b"]), 0.0)

    def test_current_superset(self):
        """当前是基线超集 → 交集=基线 → 1.0"""
        # 交集=基线大小 → 1.0
        result = calc_signal_agreement(["a", "b", "c"], ["a", "b"])
        self.assertEqual(result, 1.0)

    def test_current_subset(self):
        """当前是基线子集 → 交集/基线"""
        result = calc_signal_agreement(["a"], ["a", "b"])
        self.assertEqual(result, 0.5)

    def test_single_both(self):
        """双方各一个且相同 → 1.0"""
        self.assertEqual(calc_signal_agreement(["a"], ["a"]), 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(calc_signal_agreement([], []), float)
        self.assertIsInstance(calc_signal_agreement(["a"], ["a"]), float)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _is_tradeable_contract
# ═══════════════════════════════════════════════════════════════════════════


class TestIsTradeableContract(unittest.TestCase):
    """_is_tradeable_contract 可交易合约判断。"""

    def test_standard_contract_true(self):
        """标准合约 → True"""
        self.assertTrue(_is_tradeable_contract("FG2608"))
        self.assertTrue(_is_tradeable_contract("CU2609"))
        self.assertTrue(_is_tradeable_contract("rb2610"))

    def test_main_contract_false(self):
        """主连（无数字尾）→ False"""
        self.assertFalse(_is_tradeable_contract("FGM"))
        self.assertFalse(_is_tradeable_contract("JMM"))
        self.assertFalse(_is_tradeable_contract("RBM"))

    def test_3digit_contract_true(self):
        """3位数字合约 → True"""
        self.assertTrue(_is_tradeable_contract("FG608"))

    def test_empty_false(self):
        """空串 → False"""
        self.assertFalse(_is_tradeable_contract(""))

    def test_only_letters_false(self):
        """只有字母 → False"""
        self.assertFalse(_is_tradeable_contract("FG"))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_tradeable_contract("FG2608"), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _contract_ym
# ═══════════════════════════════════════════════════════════════════════════


class TestContractYm(unittest.TestCase):
    """_contract_ym 合约年月解析。"""

    def test_4digit_standard(self):
        """4位数字 → YYYYMM"""
        self.assertEqual(_contract_ym("FG2608"), 202608)
        self.assertEqual(_contract_ym("CU2609"), 202609)

    def test_3digit(self):
        """3位数字 → YYYYMM"""
        # FG608 → yy=60, mm=8 → 206008
        self.assertEqual(_contract_ym("FG608"), 206008)

    def test_lowercase(self):
        """小写 → 正确解析"""
        self.assertEqual(_contract_ym("fg2608"), 202608)

    def test_no_digits_none(self):
        """无数字 → None"""
        self.assertIsNone(_contract_ym("FGM"))
        self.assertIsNone(_contract_ym("FG"))

    def test_empty_none(self):
        """空串 → None"""
        self.assertIsNone(_contract_ym(""))

    def test_returns_int_or_none(self):
        """返回 int 或 None"""
        self.assertIsInstance(_contract_ym("FG2608"), int)
        self.assertIsNone(_contract_ym("FG"))

    def test_year_1900s(self):
        """70年以上 → 1900s"""
        self.assertEqual(_contract_ym("FG7001"), 197001)

    def test_year_2000s(self):
        """70年以下 → 2000s"""
        self.assertEqual(_contract_ym("FG6912"), 206912)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Kelly因子 + 回归状态 + 合约工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
