#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格保护 — 单元测试
====================

覆盖场景：
1. 价格有效性校验（None/零/负/字符串/合法值）
2. 止损方向校验 & 自动修正（多单/空单/方向错误镜像修正）
3. 用户价格保护（防止 _auto_levels 篡改用户输入价）
4. 止盈方向校验

对应历史 bug（决策 24：价格保护3层防线）：
  - 问题：用户输入 4830 → 被改成 4829.7；输入 8732.3 → 被改成 8732.8
  - 根因：_auto_levels 函数内部修改了 price 变量
  - 修复：3 层防线（Handler 还原 + record_entry 校验 + record_trade 校验）
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from price_protection import (
    validate_price,
    validate_entry_stop,
    protect_user_price,
    validate_take_profit,
)


# ═══════════════════════════════════════════════════════════════════════════
#  价格有效性校验
# ═══════════════════════════════════════════════════════════════════════════

class TestValidatePrice(unittest.TestCase):
    """价格有效性校验测试。"""

    def test_positive_price_valid(self):
        """正数价格 → 合法"""
        r = validate_price(100.0)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["price"], 100.0)
        self.assertEqual(r["reason"], "")

    def test_integer_price_valid(self):
        """整数价格 → 合法"""
        r = validate_price(4830)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["price"], 4830.0)

    def test_string_number_valid(self):
        """字符串数字 → 合法，自动转换"""
        r = validate_price("8732.3")
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["price"], 8732.3)

    def test_zero_price_invalid(self):
        """价格 = 0 → 非法"""
        r = validate_price(0)
        self.assertFalse(r["valid"])
        self.assertIn("必须大于0", r["reason"])

    def test_negative_price_invalid(self):
        """价格 < 0 → 非法"""
        r = validate_price(-10.5)
        self.assertFalse(r["valid"])
        self.assertIn("必须大于0", r["reason"])

    def test_none_price_invalid(self):
        """价格为 None → 非法"""
        r = validate_price(None)
        self.assertFalse(r["valid"])
        self.assertIn("不能为空", r["reason"])

    def test_string_text_invalid(self):
        """非数字字符串 → 非法"""
        r = validate_price("abc")
        self.assertFalse(r["valid"])
        self.assertIn("格式错误", r["reason"])

    def test_empty_string_invalid(self):
        """空字符串 → 非法"""
        r = validate_price("")
        self.assertFalse(r["valid"])

    def test_very_small_positive_valid(self):
        """很小的正数 → 合法"""
        r = validate_price(0.01)
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["price"], 0.01)


# ═══════════════════════════════════════════════════════════════════════════
#  止损方向校验 & 自动修正
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateEntryStop(unittest.TestCase):
    """止损方向校验 & 自动修正测试。"""

    # ── 多单场景 ──────────────────────────────────────────────────────────

    def test_long_stop_below_entry_correct(self):
        """多单 + 止损在入场价下方 → 正确，不修正"""
        r = validate_entry_stop("多", 100.0, 95.0)
        self.assertFalse(r["fixed"])
        self.assertAlmostEqual(r["stop"], 95.0)
        self.assertEqual(r["fix_note"], "")
        self.assertTrue(r["direction_valid"])

    def test_long_stop_above_entry_fixed(self):
        """多单 + 止损在入场价上方 → 错误，镜像修正到下方"""
        # 入场 100，止损 105（错的，应该在下方）
        # 镜像修正：100 + (100 - 105) = 95
        r = validate_entry_stop("多", 100.0, 105.0)
        self.assertTrue(r["fixed"])
        self.assertAlmostEqual(r["stop"], 95.0)
        self.assertIn("止损方向已自动修正", r["fix_note"])

    def test_long_stop_equals_entry_fixed(self):
        """多单 + 止损等于入场价 → 触发修正（镜像后仍等于，偏移+0.01）"""
        # 注意：原代码在止损=入场价时，偏移方向是 +0.01（多单往上）
        # 这与"多单止损应在下方"的直觉相反，
        # 但此处测试是为了锁定现有行为，防止意外变更
        r = validate_entry_stop("多", 100.0, 100.0)
        self.assertTrue(r["fixed"])
        self.assertAlmostEqual(r["stop"], 100.01, places=2)

    # ── 空单场景 ──────────────────────────────────────────────────────────

    def test_short_stop_above_entry_correct(self):
        """空单 + 止损在入场价上方 → 正确，不修正"""
        r = validate_entry_stop("空", 100.0, 105.0)
        self.assertFalse(r["fixed"])
        self.assertAlmostEqual(r["stop"], 105.0)

    def test_short_stop_below_entry_fixed(self):
        """空单 + 止损在入场价下方 → 错误，镜像修正到上方"""
        # 入场 100，止损 95（错的，应该在上方）
        # 镜像修正：100 + (100 - 95) = 105
        r = validate_entry_stop("空", 100.0, 95.0)
        self.assertTrue(r["fixed"])
        self.assertAlmostEqual(r["stop"], 105.0)

    def test_short_stop_equals_entry_fixed(self):
        """空单 + 止损等于入场价 → 触发修正（镜像后仍等于，偏移-0.01）"""
        # 注意：原代码在止损=入场价时，偏移方向是 -0.01（空单往下）
        # 这与"空单止损应在上方"的直觉相反，
        # 但此处测试是为了锁定现有行为，防止意外变更
        r = validate_entry_stop("空", 100.0, 100.0)
        self.assertTrue(r["fixed"])
        self.assertAlmostEqual(r["stop"], 99.99, places=2)

    # ── 方向表示方式兼容 ──────────────────────────────────────────────────

    def test_direction_long_variants(self):
        """多种"多"的表示方式都能正确识别"""
        for d in ["多", "多 ", "long", "Long", "LONG", 1, 1.0]:
            r = validate_entry_stop(d, 100.0, 95.0)
            self.assertTrue(r["direction_valid"], f"方向 {d} 应被识别为有效")
            self.assertFalse(r["fixed"], f"方向 {d}: 止损在下方应正确")

    def test_direction_short_variants(self):
        """多种"空"的表示方式都能正确识别"""
        for d in ["空", "空 ", "short", "Short", "SHORT", -1, -1.0]:
            r = validate_entry_stop(d, 100.0, 105.0)
            self.assertTrue(r["direction_valid"], f"方向 {d} 应被识别为有效")
            self.assertFalse(r["fixed"], f"方向 {d}: 止损在上方应正确")

    def test_direction_invalid(self):
        """无效方向 → direction_valid = False，不修正"""
        r = validate_entry_stop("unknown", 100.0, 105.0)
        self.assertFalse(r["direction_valid"])
        self.assertFalse(r["fixed"])
        self.assertAlmostEqual(r["stop"], 105.0)  # 原样返回

    # ── None / 异常输入 ──────────────────────────────────────────────────

    def test_stop_none_no_action(self):
        """止损为 None → 不修正，返回 None"""
        r = validate_entry_stop("多", 100.0, None)
        self.assertFalse(r["fixed"])
        self.assertIsNone(r["stop"])

    def test_stop_string_number_ok(self):
        """止损为字符串数字 → 正常校验"""
        r = validate_entry_stop("多", 100.0, "95.0")
        self.assertFalse(r["fixed"])
        self.assertAlmostEqual(r["stop"], "95.0")  # 注意：不修正时原样返回

    def test_stop_invalid_string_no_crash(self):
        """止损为非数字字符串 → 不崩溃，原样返回"""
        r = validate_entry_stop("多", 100.0, "abc")
        self.assertFalse(r["fixed"])  # 不修正
        self.assertEqual(r["stop"], "abc")  # 原样返回

    # ── 镜像修正对称性验证 ────────────────────────────────────────────────

    def test_mirror_fix_symmetry_long(self):
        """多单：止损在上方 X 点 → 修正到下方 X 点（对称）"""
        entry = 100.0
        wrong_stop = 105.0  # 上方 5 点
        r = validate_entry_stop("多", entry, wrong_stop)
        expected = entry - (wrong_stop - entry)  # = 95.0
        self.assertAlmostEqual(r["stop"], expected, places=2)

    def test_mirror_fix_symmetry_short(self):
        """空单：止损在下方 X 点 → 修正到上方 X 点（对称）"""
        entry = 100.0
        wrong_stop = 92.0  # 下方 8 点
        r = validate_entry_stop("空", entry, wrong_stop)
        expected = entry + (entry - wrong_stop)  # = 108.0
        self.assertAlmostEqual(r["stop"], expected, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  用户价格保护（核心历史 bug 回归）
# ═══════════════════════════════════════════════════════════════════════════

class TestProtectUserPrice(unittest.TestCase):
    """
    用户价格保护测试 —— 确保用户输入的价格不被自动计算篡改。

    对应历史 bug（决策 24）：
      - 纸浆多单：用户输入 4830 → 被 _auto_levels 改成 4829.7
      - 苯乙烯多单：用户输入 8732.3 → 被改成 8732.8
    """

    def test_user_price_preserved_when_computed_differs(self):
        """用户提供了价格，且计算价不同 → 强制还原用户价"""
        # 模拟：用户输入 4830，_auto_levels 算出 4829.7
        r = protect_user_price(original_price=4830, computed_price=4829.7,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 4830.0)
        self.assertTrue(r["was_protected"])
        self.assertTrue(r["price_changed"])

    def test_user_price_preserved_small_difference(self):
        """计算价与用户价差异很小（浮点精度）→ 仍保护，还原用户价"""
        # 模拟：用户输入 8732.3，_auto_levels 改成 8732.8
        r = protect_user_price(original_price=8732.3, computed_price=8732.8,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 8732.3)
        self.assertTrue(r["was_protected"])

    def test_user_price_same_as_computed_no_protection_needed(self):
        """用户价 = 计算价 → 不需要保护（但 final_price 仍是用户价）"""
        r = protect_user_price(original_price=100.0, computed_price=100.0,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 100.0)
        self.assertFalse(r["was_protected"])
        self.assertFalse(r["price_changed"])

    def test_user_not_provided_price_uses_computed(self):
        """用户没提供价格 → 使用计算价，不触发保护"""
        r = protect_user_price(original_price=0, computed_price=100.0,
                               user_provided_price=False)
        self.assertAlmostEqual(r["final_price"], 100.0)
        self.assertFalse(r["was_protected"])

    def test_paper_example_4830_regression(self):
        """回归测试：纸浆 4830 → 4829.7 的 bug 必须被修复"""
        # 这是真实发生过的 bug：用户输入 4830，系统记录成 4829.7
        r = protect_user_price(original_price=4830, computed_price=4829.7,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 4830.0,
                               msg="纸浆价格 4830 被改成 4829.7 的 bug 复发了！")

    def test_paper_example_8732_regression(self):
        """回归测试：苯乙烯 8732.3 → 8732.8 的 bug 必须被修复"""
        # 真实 bug：用户输入 8732.3，系统记录成 8732.8
        r = protect_user_price(original_price=8732.3, computed_price=8732.8,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 8732.3,
                               msg="苯乙烯价格 8732.3 被改成 8732.8 的 bug 复发了！")

    def test_string_prices_converted(self):
        """字符串价格 → 自动转换，正常保护"""
        r = protect_user_price(original_price="4830", computed_price="4829.7",
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 4830.0)
        self.assertTrue(r["was_protected"])

    def test_none_computed_price(self):
        """计算价为 None → 使用用户价（0 或用户提供的）"""
        r = protect_user_price(original_price=4830, computed_price=None,
                               user_provided_price=True)
        self.assertAlmostEqual(r["final_price"], 4830.0)

    def test_none_original_price(self):
        """用户价为 None → 视为 0，计算价有效则用计算价"""
        r = protect_user_price(original_price=None, computed_price=100.0,
                               user_provided_price=True)
        # original_price=None → 转换为 0.0，计算价 100 不同 → 触发保护
        # 但用户价是 0，final_price 会是 0... 这是边界情况
        # 实际场景中 None 不应该出现（validate_price 会拦住）
        self.assertTrue(r["was_protected"])  # 价格变了，所以触发保护


# ═══════════════════════════════════════════════════════════════════════════
#  止盈方向校验
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateTakeProfit(unittest.TestCase):
    """止盈方向校验测试。"""

    def test_long_tp_above_entry_valid(self):
        """多单 + 止盈在入场价上方 → 正确"""
        r = validate_take_profit("多", 100.0, 110.0)
        self.assertTrue(r["valid"])

    def test_long_tp_below_entry_invalid(self):
        """多单 + 止盈在入场价下方 → 错误"""
        r = validate_take_profit("多", 100.0, 90.0)
        self.assertFalse(r["valid"])
        self.assertIn("方向错误", r["reason"])

    def test_short_tp_below_entry_valid(self):
        """空单 + 止盈在入场价下方 → 正确"""
        r = validate_take_profit("空", 100.0, 90.0)
        self.assertTrue(r["valid"])

    def test_short_tp_above_entry_invalid(self):
        """空单 + 止盈在入场价上方 → 错误"""
        r = validate_take_profit("空", 100.0, 110.0)
        self.assertFalse(r["valid"])

    def test_tp_none_valid(self):
        """未设止盈 → 合法（不校验）"""
        r = validate_take_profit("多", 100.0, None)
        self.assertTrue(r["valid"])
        self.assertIsNone(r["tp_price"])

    def test_tp_equals_entry_invalid(self):
        """止盈等于入场价 → 无效（不在有利方向）"""
        r = validate_take_profit("多", 100.0, 100.0)
        self.assertFalse(r["valid"])

    def test_invalid_direction(self):
        """方向无效 → 校验失败"""
        r = validate_take_profit("unknown", 100.0, 110.0)
        self.assertFalse(r["valid"])
        self.assertIn("方向无效", r["reason"])


# ═══════════════════════════════════════════════════════════════════════════
#  综合场景：3 层防线模拟
# ═══════════════════════════════════════════════════════════════════════════

class TestThreeLayerDefense(unittest.TestCase):
    """
    价格保护 3 层防线的综合模拟测试。

    模拟完整流程：
    1. 用户输入价格 → validate_price 校验（第 2、3 层）
    2. 调用 _auto_levels 计算止损止盈 → 价格可能被篡改
    3. protect_user_price 还原用户价（第 1 层）
    4. validate_entry_stop 校验止损方向
    """

    def test_full_flow_paper_pulp_example(self):
        """完整流程模拟：纸浆开仓 4830"""
        # 用户输入
        user_price = 4830
        direction = "多"

        # 第 2/3 层：价格有效性校验
        pv = validate_price(user_price)
        self.assertTrue(pv["valid"], "用户价格应合法")

        # 模拟 _auto_levels 计算后价格被改成 4829.7
        computed_price = 4829.7
        computed_stop = 4780.0  # 假设算出来的止损

        # 第 1 层：保护用户价格
        pp = protect_user_price(user_price, computed_price, user_provided_price=True)
        self.assertAlmostEqual(pp["final_price"], 4830.0,
                               msg="第 1 层防线失效：用户价格被篡改")
        self.assertTrue(pp["was_protected"], "应该触发价格保护")

        # 止损方向校验
        sv = validate_entry_stop(direction, pp["final_price"], computed_stop)
        self.assertFalse(sv["fixed"], "止损方向应该正确，不需要修正")
        self.assertLess(sv["stop"], pp["final_price"], "多单止损应在入场价下方")

    def test_full_flow_styrene_example(self):
        """完整流程模拟：苯乙烯开仓 8732.3"""
        user_price = 8732.3
        direction = "多"

        pv = validate_price(user_price)
        self.assertTrue(pv["valid"])

        # 模拟 _auto_levels 改成 8732.8
        computed_price = 8732.8
        computed_stop = 8592.0

        pp = protect_user_price(user_price, computed_price, user_provided_price=True)
        self.assertAlmostEqual(pp["final_price"], 8732.3,
                               msg="第 1 层防线失效：苯乙烯价格被篡改")

        sv = validate_entry_stop(direction, pp["final_price"], computed_stop)
        self.assertFalse(sv["fixed"])

    def test_full_flow_stop_direction_wrong_then_fixed(self):
        """完整流程：止损方向错了 → 被修正"""
        user_price = 100.0
        direction = "多"
        wrong_stop = 105.0  # 多单止损在上方，错的

        sv = validate_entry_stop(direction, user_price, wrong_stop)
        self.assertTrue(sv["fixed"], "止损方向错误应该被修正")
        self.assertAlmostEqual(sv["stop"], 95.0)  # 镜像修正到下方


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
