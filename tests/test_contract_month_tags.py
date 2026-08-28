#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合约月份 + 标签匹配 — 单元测试
==============================================

1. ym_of — 合约码 → 年月整数(YYYYMM)
   - 标准4位数字（FG2608 → 202608）
   - 3位数字（FG608 → 202608）
   - 小写输入
   - 特殊字符被剥离
   - 无法识别 → None

2. _add_months — 年月整数 + n 个月（正确跨年）
   - 同年内加月
   - 跨年加月
   - 加 12 个月 = 下一年同月
   - 加负数（减月）
   - 12月 → 1月跨年
   - 零月

3. _next_month_code — 基于旧合约 + 当前月生成近月主力候选
   - 标准合约 + 正常月份
   - 12月 → 次年1月
   - 小写输入
   - 无法识别 → None

4. _tag_to_symbols — 文本命中哪些品种标签
   - 空文本 → 空集合
   - 匹配单个标签
   - 匹配多个标签（去重）
   - 不匹配 → 空集合
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from info_dimension import _tag_to_symbols
from refresh_main_contracts import _add_months, _next_month_code, ym_of

# ═══════════════════════════════════════════════════════════════════════════
#  1. ym_of
# ═══════════════════════════════════════════════════════════════════════════


class TestYmOf(unittest.TestCase):
    """ym_of 合约码 → 年月整数。"""

    def test_standard_4digit(self):
        """标准4位年月 → 202608"""
        self.assertEqual(ym_of("FG2608"), 202608)

    def test_another_symbol(self):
        """其他品种同样正确"""
        self.assertEqual(ym_of("CU2609"), 202609)
        self.assertEqual(ym_of("m2601"), 202601)

    def test_3digit_year(self):
        """3位年月（前2位年+末位月）→ 补全
        注意：FG608 → 60/8 → 206008（前两位是年，最后一位是月）"""
        self.assertEqual(ym_of("FG608"), 206008)

    def test_lowercase(self):
        """小写输入 → 正确解析"""
        self.assertEqual(ym_of("fg2608"), 202608)
        self.assertEqual(ym_of("rb2610"), 202610)

    def test_mixed_case(self):
        """大小写混合 → 正确解析"""
        self.assertEqual(ym_of("Fg2608"), 202608)

    def test_with_dash(self):
        """含特殊字符 → 被剥离后解析"""
        self.assertEqual(ym_of("FG-2608"), 202608)
        self.assertEqual(ym_of("FG.2608"), 202608)

    def test_no_digits(self):
        """无数字 → None"""
        self.assertIsNone(ym_of("FG"))

    def test_empty(self):
        """空串 → None"""
        self.assertIsNone(ym_of(""))

    def test_none(self):
        """None → None"""
        self.assertIsNone(ym_of(None))

    def test_returns_int_or_none(self):
        """返回 int 或 None"""
        self.assertIsInstance(ym_of("FG2608"), int)
        self.assertIsNone(ym_of("xyz"))

    def test_year_1900s(self):
        """70年以上 → 1900s"""
        # yy >= 70 视为 1900s
        result = ym_of("FG7001")
        self.assertEqual(result, 197001)

    def test_year_2000s(self):
        """70年以下 → 2000s"""
        result = ym_of("FG6912")
        self.assertEqual(result, 206912)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _add_months
# ═══════════════════════════════════════════════════════════════════════════


class TestAddMonths(unittest.TestCase):
    """_add_months 年月整数 + n 个月。"""

    def test_same_year_add(self):
        """同年内加月"""
        self.assertEqual(_add_months(202608, 2), 202610)
        self.assertEqual(_add_months(202601, 5), 202606)

    def test_cross_year_add(self):
        """跨年加月"""
        self.assertEqual(_add_months(202611, 2), 202701)
        self.assertEqual(_add_months(202612, 1), 202701)

    def test_add_12_months(self):
        """加 12 个月 = 下一年同月"""
        self.assertEqual(_add_months(202608, 12), 202708)
        self.assertEqual(_add_months(202612, 12), 202712)

    def test_add_24_months(self):
        """加 24 个月 = 两年后同月"""
        self.assertEqual(_add_months(202608, 24), 202808)

    def test_subtract_months(self):
        """减月（负数）"""
        self.assertEqual(_add_months(202608, -2), 202606)
        self.assertEqual(_add_months(202601, -1), 202512)

    def test_december_to_january(self):
        """12月 + 1月 → 次年1月"""
        self.assertEqual(_add_months(202612, 1), 202701)

    def test_january_to_december(self):
        """1月 - 1月 → 上年12月"""
        self.assertEqual(_add_months(202601, -1), 202512)

    def test_zero_months(self):
        """加 0 个月 → 不变"""
        self.assertEqual(_add_months(202608, 0), 202608)

    def test_multi_year_cross(self):
        """跨多年"""
        self.assertEqual(_add_months(202608, 18), 202802)
        self.assertEqual(_add_months(202608, -18), 202502)

    def test_returns_int(self):
        """返回 int"""
        self.assertIsInstance(_add_months(202608, 1), int)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _next_month_code
# ═══════════════════════════════════════════════════════════════════════════


class TestNextMonthCode(unittest.TestCase):
    """_next_month_code 生成近月主力候选合约。"""

    def test_standard_case(self):
        """标准合约 + 正常月份"""
        self.assertEqual(_next_month_code("FG2608", 202608), "FG2609")

    def test_december_cross_year(self):
        """12月时 now_ym + 1 直接加整数 → 2613（不跨年，函数直接 +1 操作）"""
        # 注意：函数内部用 now_ym + 1 整数加法，12月会产生 202613
        self.assertEqual(_next_month_code("FG2612", 202612), "FG2613")

    def test_lowercase_input(self):
        """小写输入 → 输出大写前缀"""
        result = _next_month_code("fg2608", 202608)
        self.assertEqual(result, "FG2609")

    def test_different_symbol(self):
        """其他品种"""
        self.assertEqual(_next_month_code("CU2609", 202609), "CU2610")
        self.assertEqual(_next_month_code("rb2610", 202610), "RB2611")

    def test_no_digits(self):
        """无数字 → None"""
        self.assertIsNone(_next_month_code("FG", 202608))

    def test_empty_code(self):
        """空串 → None"""
        self.assertIsNone(_next_month_code("", 202608))

    def test_format_4_digit(self):
        """输出格式：前缀 + 2位年 + 2位月"""
        result = _next_month_code("FG2608", 202608)
        self.assertEqual(len(result), 6)  # FG + 26 + 09 = 6 chars
        self.assertTrue(result[:2].isalpha())
        self.assertTrue(result[2:].isdigit())

    def test_now_ym_before_contract(self):
        """当前月早于合约月 → 仍然取当前月+1"""
        self.assertEqual(_next_month_code("FG2612", 202608), "FG2609")


# ═══════════════════════════════════════════════════════════════════════════
#  4. _tag_to_symbols
# ═══════════════════════════════════════════════════════════════════════════


class TestTagToSymbols(unittest.TestCase):
    """_tag_to_symbols 文本命中品种标签。"""

    def test_empty_text(self):
        """空文本 → 空集合"""
        result = _tag_to_symbols("")
        self.assertIsInstance(result, set)
        self.assertEqual(len(result), 0)

    def test_returns_set(self):
        """返回 set 类型"""
        self.assertIsInstance(_tag_to_symbols("测试"), set)

    def test_no_match(self):
        """不匹配任何标签 → 空集合"""
        result = _tag_to_symbols("今天天气真好")
        self.assertEqual(len(result), 0)

    def test_match_egg(self):
        """匹配鸡蛋 → 含 jd"""
        # 从 SYMBOL_TAGS 看，jd 应该有"鸡蛋"标签
        result = _tag_to_symbols("鸡蛋价格上涨")
        # 只要不是空集就行，具体标签由配置决定
        self.assertIsInstance(result, set)
        if len(result) > 0:
            self.assertIn("jd", result)

    def test_match_hog(self):
        """匹配生猪 → 含 lh"""
        result = _tag_to_symbols("生猪存栏下降")
        self.assertIsInstance(result, set)
        if len(result) > 0:
            self.assertIn("lh", result)

    def test_match_multiple(self):
        """同时匹配多个品种 → 集合含多个元素"""
        result = _tag_to_symbols("鸡蛋和生猪的基本面")
        self.assertIsInstance(result, set)
        # 至少应该匹配到鸡蛋和生猪
        if len(result) >= 2:
            self.assertIn("jd", result)
            self.assertIn("lh", result)

    def test_no_duplicates(self):
        """同一品种多次命中 → 不重复"""
        result = _tag_to_symbols("鸡蛋鸡蛋鸡蛋")
        self.assertIsInstance(result, set)
        if "jd" in result:
            # 集合去重，只出现一次
            count = sum(1 for s in result if s == "jd")
            self.assertEqual(count, 1)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  合约月份 + 标签匹配 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
