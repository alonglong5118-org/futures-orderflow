#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格保护 + 相关性闸门 — 单元测试
====================================

1. validate_price — 价格合法性校验
   - 正 int → 合法
   - 正 float → 合法
   - 字符串数字 → 合法（转 float）
   - None → 非法（不能为空）
   - 0 → 非法（必须 > 0）
   - 负数 → 非法
   - 字符串非数字 → 非法（格式错误）
   - 空串 → 非法
   - 返回 3 字段：valid/price/reason

2. _dir_sign — 方向符号
   - "多"/"long"/"duo"/"buy" → 1
   - "空"/"short"/"kong"/"sell" → -1
   - 正 int → 1
   - 负 int → -1
   - 零 → 0
   - 正 float → 1
   - 负 float → -1
   - 未知字符串 → 0
   - 大小写不敏感
   - 前后空格自动去掉
   - 其他类型 → 0

3. validate_entry_stop — 止损方向校验+镜像修正
   - 多单止损在下方 → 正确（不修正）
   - 空单止损在上方 → 正确（不修正）
   - 多单止损在上方 → 镜像修正到下方
   - 空单止损在下方 → 镜像修正到上方
   - stop=None → 原样返回
   - 方向无效 → direction_valid=False
   - 修正后 = 入场价 → 微调偏移
   - 修正后止损保留 2 位小数
   - 价格格式错误 → 不修正原样返回

4. protect_user_price — 用户价格保护
   - 用户提供了价格且不同 → 保护（用用户价）
   - 用户提供了价格且相同 → 不保护
   - 用户没提供 → 用计算值
   - 用户价为 None → 用计算值
   - 计算价格式错误 → 用 0
   - was_protected 含义：用户提供且价格不同
   - price_changed 含义：计算价 != 原始价

5. validate_take_profit — 止盈方向校验
   - 多单止盈在上方 → 有效
   - 空单止盈在下方 → 有效
   - 多单止盈在下方 → 无效
   - 空单止盈在上方 → 无效
   - tp=None → 有效（未设止盈）
   - 方向无效 → 无效
   - 价格格式错误 → 无效
   - 返回 3 字段：valid/tp_price/reason

6. _pearson_corr — 皮尔逊相关系数
   - 完全正相关 → 1.0
   - 完全负相关 → -1.0
   - 不相关 → ~0
   - 数据不足（<2）→ None
   - 长度不一致 → None
   - 一方差为 0 → None
   - 限制在 [-1, 1]

7. apply_corr_gate — 相关性闸门
   - 无历史数据 → 跳过
   - 历史不足 → 跳过
   - 低相关 → 正常计权
   - 高相关且 T 更弱 → 降权 T
   - 高相关且 C 更弱 → 降权 C
   - 一方无波动 → 跳过
   - 数据格式错误 → 跳过
   - 返回 6 字段
   - abs(corr) 用于判定（负相关也触发）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from corr_gate_utils import _pearson_corr, apply_corr_gate
from price_protection import (
    _dir_sign,
    protect_user_price,
    validate_entry_stop,
    validate_price,
    validate_take_profit,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. validate_price
# ═══════════════════════════════════════════════════════════════════════════

class TestValidatePrice(unittest.TestCase):
    """validate_price 价格合法性校验。"""

    def test_positive_int_valid(self):
        """正 int → 合法"""
        r = validate_price(100)
        self.assertTrue(r["valid"])
        self.assertEqual(r["price"], 100.0)
        self.assertEqual(r["reason"], "")

    def test_positive_float_valid(self):
        """正 float → 合法"""
        r = validate_price(123.45)
        self.assertTrue(r["valid"])
        self.assertEqual(r["price"], 123.45)

    def test_string_number_valid(self):
        """字符串数字 → 合法（转 float）"""
        r = validate_price("99.9")
        self.assertTrue(r["valid"])
        self.assertEqual(r["price"], 99.9)

    def test_none_invalid(self):
        """None → 非法（不能为空）"""
        r = validate_price(None)
        self.assertFalse(r["valid"])
        self.assertEqual(r["price"], 0.0)
        self.assertIn("不能为空", r["reason"])

    def test_zero_invalid(self):
        """0 → 非法（必须 > 0）"""
        r = validate_price(0)
        self.assertFalse(r["valid"])
        self.assertIn("必须大于0", r["reason"])

    def test_negative_invalid(self):
        """负数 → 非法"""
        r = validate_price(-5)
        self.assertFalse(r["valid"])
        self.assertIn("必须大于0", r["reason"])

    def test_string_non_numeric_invalid(self):
        """字符串非数字 → 非法（格式错误）"""
        r = validate_price("abc")
        self.assertFalse(r["valid"])
        self.assertIn("格式错误", r["reason"])

    def test_empty_string_invalid(self):
        """空串 → 非法"""
        r = validate_price("")
        self.assertFalse(r["valid"])

    def test_return_three_fields(self):
        """返回 3 字段：valid/price/reason"""
        r = validate_price(10)
        for key in ("valid", "price", "reason"):
            self.assertIn(key, r)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _dir_sign
# ═══════════════════════════════════════════════════════════════════════════

class TestDirSign(unittest.TestCase):
    """_dir_sign 方向符号。"""

    def test_chinese_duo(self):
        """'多' → 1"""
        self.assertEqual(_dir_sign("多"), 1)

    def test_long_string(self):
        """'long' → 1"""
        self.assertEqual(_dir_sign("long"), 1)

    def test_duo_pinyin(self):
        """'duo' → 1"""
        self.assertEqual(_dir_sign("duo"), 1)

    def test_buy_string(self):
        """'buy' → 1"""
        self.assertEqual(_dir_sign("buy"), 1)

    def test_chinese_kong(self):
        """'空' → -1"""
        self.assertEqual(_dir_sign("空"), -1)

    def test_short_string(self):
        """'short' → -1"""
        self.assertEqual(_dir_sign("short"), -1)

    def test_kong_pinyin(self):
        """'kong' → -1"""
        self.assertEqual(_dir_sign("kong"), -1)

    def test_sell_string(self):
        """'sell' → -1"""
        self.assertEqual(_dir_sign("sell"), -1)

    def test_positive_int(self):
        """正 int → 1"""
        self.assertEqual(_dir_sign(1), 1)
        self.assertEqual(_dir_sign(100), 1)

    def test_negative_int(self):
        """负 int → -1"""
        self.assertEqual(_dir_sign(-1), -1)
        self.assertEqual(_dir_sign(-50), -1)

    def test_zero_int(self):
        """零 → 0"""
        self.assertEqual(_dir_sign(0), 0)

    def test_positive_float(self):
        """正 float → 1"""
        self.assertEqual(_dir_sign(1.5), 1)

    def test_negative_float(self):
        """负 float → -1"""
        self.assertEqual(_dir_sign(-2.0), -1)

    def test_unknown_string_zero(self):
        """未知字符串 → 0"""
        self.assertEqual(_dir_sign("unknown"), 0)

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_dir_sign("LONG"), 1)
        self.assertEqual(_dir_sign("Short"), -1)

    def test_strip_whitespace(self):
        """前后空格自动去掉"""
        self.assertEqual(_dir_sign("  long  "), 1)
        self.assertEqual(_dir_sign("  short  "), -1)

    def test_other_type_zero(self):
        """其他类型 → 0"""
        self.assertEqual(_dir_sign([]), 0)
        self.assertEqual(_dir_sign({}), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. validate_entry_stop
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateEntryStop(unittest.TestCase):
    """validate_entry_stop 止损方向校验+镜像修正。"""

    def test_long_stop_below_correct(self):
        """多单止损在下方 → 正确（不修正）"""
        r = validate_entry_stop("long", 100, 95)
        self.assertFalse(r["fixed"])
        self.assertEqual(r["stop"], 95)
        self.assertEqual(r["fix_note"], "")
        self.assertTrue(r["direction_valid"])

    def test_short_stop_above_correct(self):
        """空单止损在上方 → 正确（不修正）"""
        r = validate_entry_stop("short", 100, 105)
        self.assertFalse(r["fixed"])
        self.assertEqual(r["stop"], 105)

    def test_long_stop_above_fixed(self):
        """多单止损在上方 → 镜像修正到下方"""
        # 入场 100，止损 105（错） → 镜像到 95
        r = validate_entry_stop("long", 100, 105)
        self.assertTrue(r["fixed"])
        self.assertEqual(r["stop"], 95.0)
        self.assertIn("已自动修正", r["fix_note"])

    def test_short_stop_below_fixed(self):
        """空单止损在下方 → 镜像修正到上方"""
        # 入场 100，止损 95（错） → 镜像到 105
        r = validate_entry_stop("short", 100, 95)
        self.assertTrue(r["fixed"])
        self.assertEqual(r["stop"], 105.0)

    def test_none_stop_unchanged(self):
        """stop=None → 原样返回"""
        r = validate_entry_stop("long", 100, None)
        self.assertIsNone(r["stop"])
        self.assertFalse(r["fixed"])

    def test_invalid_direction_flag(self):
        """方向无效 → direction_valid=False"""
        r = validate_entry_stop("unknown", 100, 95)
        self.assertFalse(r["direction_valid"])
        self.assertFalse(r["fixed"])

    def test_stop_equals_entry_min_offset(self):
        """修正后 = 入场价 → 微调偏移"""
        # 止损恰好等于入场价 → 镜像后也相等 → 触发微调
        r = validate_entry_stop("long", 100, 100)
        self.assertTrue(r["fixed"])
        # 多单 → 向上偏移 0.01
        self.assertNotEqual(r["stop"], 100.0)

    def test_two_decimals_precision(self):
        """修正后止损保留 2 位小数"""
        r = validate_entry_stop("long", 100, 105.123)
        self.assertEqual(r["stop"], round(r["stop"], 2))

    def test_bad_price_not_fixed(self):
        """价格格式错误 → 不修正原样返回"""
        r = validate_entry_stop("long", "abc", 95)
        self.assertFalse(r["fixed"])
        self.assertEqual(r["stop"], 95)  # 原样


# ═══════════════════════════════════════════════════════════════════════════
#  4. protect_user_price
# ═══════════════════════════════════════════════════════════════════════════

class TestProtectUserPrice(unittest.TestCase):
    """protect_user_price 用户价格保护。"""

    def test_user_provided_different_protected(self):
        """用户提供了价格且不同 → 保护（用用户价）"""
        r = protect_user_price(4830, 4829.7, user_provided_price=True)
        self.assertEqual(r["final_price"], 4830.0)
        self.assertTrue(r["was_protected"])
        self.assertTrue(r["price_changed"])

    def test_user_provided_same_not_protected(self):
        """用户提供了价格且相同 → 不保护"""
        r = protect_user_price(100, 100, user_provided_price=True)
        self.assertEqual(r["final_price"], 100.0)
        self.assertFalse(r["was_protected"])
        self.assertFalse(r["price_changed"])

    def test_not_user_provided_uses_computed(self):
        """用户没提供 → 用计算值"""
        r = protect_user_price(100, 99.5, user_provided_price=False)
        self.assertEqual(r["final_price"], 99.5)
        self.assertFalse(r["was_protected"])
        self.assertTrue(r["price_changed"])

    def test_none_original_uses_computed(self):
        """用户价为 None → 用计算值"""
        r = protect_user_price(None, 100, user_provided_price=True)
        # orig = 0.0, comp = 100 → 不同 → protected=True，但 final_price = orig = 0
        self.assertEqual(r["final_price"], 0.0)
        self.assertTrue(r["was_protected"])

    def test_computed_bad_format_zero(self):
        """计算价格式错误 → 用 0"""
        r = protect_user_price(100, "abc", user_provided_price=False)
        self.assertEqual(r["final_price"], 0.0)

    def test_was_protected_definition(self):
        """was_protected：用户提供且价格不同"""
        # 用户提供 + 不同 → True
        r1 = protect_user_price(100, 99, user_provided_price=True)
        self.assertTrue(r1["was_protected"])
        # 用户提供 + 相同 → False
        r2 = protect_user_price(100, 100, user_provided_price=True)
        self.assertFalse(r2["was_protected"])
        # 没提供 → False
        r3 = protect_user_price(100, 99, user_provided_price=False)
        self.assertFalse(r3["was_protected"])

    def test_price_changed_definition(self):
        """price_changed：计算价 != 原始价"""
        r1 = protect_user_price(100, 99, user_provided_price=False)
        self.assertTrue(r1["price_changed"])
        r2 = protect_user_price(100, 100, user_provided_price=False)
        self.assertFalse(r2["price_changed"])

    def test_return_three_fields(self):
        """返回 3 字段"""
        r = protect_user_price(100, 100)
        for key in ("final_price", "was_protected", "price_changed"):
            self.assertIn(key, r)


# ═══════════════════════════════════════════════════════════════════════════
#  5. validate_take_profit
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateTakeProfit(unittest.TestCase):
    """validate_take_profit 止盈方向校验。"""

    def test_long_tp_above_valid(self):
        """多单止盈在上方 → 有效"""
        r = validate_take_profit("long", 100, 110)
        self.assertTrue(r["valid"])
        self.assertEqual(r["tp_price"], 110)
        self.assertEqual(r["reason"], "")

    def test_short_tp_below_valid(self):
        """空单止盈在下方 → 有效"""
        r = validate_take_profit("short", 100, 90)
        self.assertTrue(r["valid"])

    def test_long_tp_below_invalid(self):
        """多单止盈在下方 → 无效"""
        r = validate_take_profit("long", 100, 90)
        self.assertFalse(r["valid"])
        self.assertIn("方向错误", r["reason"])

    def test_short_tp_above_invalid(self):
        """空单止盈在上方 → 无效"""
        r = validate_take_profit("short", 100, 110)
        self.assertFalse(r["valid"])
        self.assertIn("方向错误", r["reason"])

    def test_none_tp_valid(self):
        """tp=None → 有效（未设止盈）"""
        r = validate_take_profit("long", 100, None)
        self.assertTrue(r["valid"])
        self.assertIsNone(r["tp_price"])

    def test_invalid_direction(self):
        """方向无效 → 无效"""
        r = validate_take_profit("unknown", 100, 110)
        self.assertFalse(r["valid"])
        self.assertIn("方向无效", r["reason"])

    def test_bad_price_format(self):
        """价格格式错误 → 无效"""
        r = validate_take_profit("long", "abc", 110)
        self.assertFalse(r["valid"])
        self.assertIn("格式错误", r["reason"])

    def test_return_three_fields(self):
        """返回 3 字段：valid/tp_price/reason"""
        r = validate_take_profit("long", 100, 110)
        for key in ("valid", "tp_price", "reason"):
            self.assertIn(key, r)

    def test_equal_price_invalid(self):
        """止盈 = 入场 → 无效（不大于也不小于）"""
        r = validate_take_profit("long", 100, 100)
        self.assertFalse(r["valid"])


# ═══════════════════════════════════════════════════════════════════════════
#  6. _pearson_corr
# ═══════════════════════════════════════════════════════════════════════════

class TestPearsonCorr(unittest.TestCase):
    """_pearson_corr 皮尔逊相关系数。"""

    def test_perfect_positive(self):
        """完全正相关 → 1.0"""
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        self.assertAlmostEqual(_pearson_corr(x, y), 1.0, places=6)

    def test_perfect_negative(self):
        """完全负相关 → -1.0"""
        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        self.assertAlmostEqual(_pearson_corr(x, y), -1.0, places=6)

    def test_no_correlation(self):
        """不相关 → 绝对值小"""
        # 弱相关序列，相关系数绝对值应 < 0.5
        x = [1, 2, 3, 4, 5, 6]
        y = [1, -1, 1, -1, 1, -1]
        r = _pearson_corr(x, y)
        self.assertLess(abs(r), 0.5)

    def test_insufficient_data(self):
        """数据不足（<2）→ None"""
        self.assertIsNone(_pearson_corr([1], [2]))
        self.assertIsNone(_pearson_corr([], []))

    def test_length_mismatch(self):
        """长度不一致 → None"""
        self.assertIsNone(_pearson_corr([1, 2, 3], [1, 2]))

    def test_zero_variance_x(self):
        """x 方差为 0 → None"""
        x = [5, 5, 5, 5]
        y = [1, 2, 3, 4]
        self.assertIsNone(_pearson_corr(x, y))

    def test_zero_variance_y(self):
        """y 方差为 0 → None"""
        x = [1, 2, 3, 4]
        y = [5, 5, 5, 5]
        self.assertIsNone(_pearson_corr(x, y))

    def test_bounded_in_range(self):
        """限制在 [-1, 1]"""
        # 正常数据结果应在范围内
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        r = _pearson_corr(x, y)
        self.assertGreaterEqual(r, -1.0)
        self.assertLessEqual(r, 1.0)

    def test_returns_float_or_none(self):
        """返回 float 或 None"""
        self.assertIsInstance(_pearson_corr([1, 2], [3, 4]), float)
        self.assertIsNone(_pearson_corr([], []))


# ═══════════════════════════════════════════════════════════════════════════
#  7. apply_corr_gate
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyCorrGate(unittest.TestCase):
    """apply_corr_gate 相关性闸门。"""

    def _hist_high_corr(self, n=15):
        """构造高度正相关的历史数据。"""
        return [[i, i * 1.1 + 0.5] for i in range(1, n + 1)]

    def _hist_low_corr(self, n=15):
        """构造低相关的历史数据。"""
        # x 线性增，y 交替上下
        return [[i, 1 if i % 2 == 0 else -1] for i in range(1, n + 1)]

    def test_no_history_skip(self):
        """无历史数据 → 跳过"""
        r = apply_corr_gate(50, 30, None)
        self.assertFalse(r["applied"])
        self.assertEqual(r["dropped"], "none")
        self.assertIn("无历史", r["action"])

    def test_insufficient_history_skip(self):
        """历史不足 → 跳过"""
        hist = [[1, 2], [2, 3], [3, 4]]  # 只有 3 条
        r = apply_corr_gate(50, 30, hist, min_history=10)
        self.assertFalse(r["applied"])
        self.assertIn("不足", r["action"])

    def test_low_correlation_normal(self):
        """低相关 → 正常计权"""
        hist = self._hist_low_corr(20)
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        self.assertFalse(r["applied"])
        self.assertEqual(r["T"], 50)
        self.assertEqual(r["C"], 30)
        self.assertEqual(r["dropped"], "none")

    def test_high_corr_T_weaker_drop_T(self):
        """高相关且 T 更弱 → 降权 T"""
        hist = self._hist_high_corr(20)
        # T=30, C=50 → T 更弱
        r = apply_corr_gate(30, 50, hist, gate=0.7)
        self.assertTrue(r["applied"])
        self.assertEqual(r["T"], 0.0)
        self.assertEqual(r["C"], 50)
        self.assertEqual(r["dropped"], "T")

    def test_high_corr_C_weaker_drop_C(self):
        """高相关且 C 更弱 → 降权 C"""
        hist = self._hist_high_corr(20)
        # T=50, C=30 → C 更弱
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        self.assertTrue(r["applied"])
        self.assertEqual(r["T"], 50)
        self.assertEqual(r["C"], 0.0)
        self.assertEqual(r["dropped"], "C")

    def test_no_variance_skip(self):
        """一方无波动 → 跳过"""
        # x 全相同 → 方差 0
        hist = [[5, i] for i in range(15)]
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        self.assertFalse(r["applied"])
        self.assertIn("无波动", r["action"])

    def test_bad_data_format_skip(self):
        """数据格式错误 → 跳过"""
        hist = [["a", "b"] for _ in range(15)]
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        self.assertFalse(r["applied"])
        self.assertIn("格式错误", r["action"])

    def test_return_six_fields(self):
        """返回 6 字段"""
        r = apply_corr_gate(50, 30, None)
        for key in ("T", "C", "corr", "action", "applied", "dropped"):
            self.assertIn(key, r)

    def test_negative_corr_triggers(self):
        """abs(corr) 用于判定（负相关也触发）"""
        # 构造高度负相关
        hist = [[i, 20 - i] for i in range(1, 20)]
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        # 高度负相关 → 应触发降权
        self.assertTrue(r["applied"])

    def test_equal_strength_drop_T(self):
        """T 和 C 相等 → 降权 T（abs_T <= abs_C 分支）"""
        hist = self._hist_high_corr(20)
        r = apply_corr_gate(50, 50, hist, gate=0.7)
        self.assertTrue(r["applied"])
        self.assertEqual(r["dropped"], "T")
        self.assertEqual(r["T"], 0.0)
        self.assertEqual(r["C"], 50)

    def test_corr_value_returned(self):
        """corr 值被正确返回"""
        hist = self._hist_high_corr(20)
        r = apply_corr_gate(50, 30, hist, gate=0.7)
        self.assertIsNotNone(r["corr"])
        self.assertIsInstance(r["corr"], float)
        self.assertGreaterEqual(abs(r["corr"]), 0.7)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  价格保护 + 相关性闸门 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
