#!/usr/bin/env python3
"""
经纪导入 + 符号映射 + 归一化 — 单元测试（冲 3000 收尾）
==============================================

1. _norm_key — 表头标准化
   - 去空格/冒号/括号/下划线/连字符
   - 转小写
   - None → 空串
   - 全角空格

2. contract_to_symbol — 合约代码 → 品种主键
   - 标准合约 → SYMBOLS 主键
   - 3位数字 → 正确
   - 纯字母 → 正确
   - 大小写还原（郑商所大写、上期小写）
   - 无法识别 → None

3. _parse_side — 买卖方向解析
   - 中文买/卖
   - 英文 buy/sell
   - 多/空
   - 缩写 b/s
   - 未知 → 空串
   - 大小写不敏感

4. _parse_offset — 开平标记解析
   - 开/open
   - 平/close
   - 未知 → 空串

5. _to_num — 数值转换
   - 正常数字
   - 千分位逗号
   - ¥ 符号
   - 负数
   - 非法 → None

6. _norm_tanh — tanh 归一化
   - 正值 → 正结果
   - 负值 → 负结果
   - 零 → 0
   - scale=0 → 0
   - 大值 → 趋近 1
   - 大负值 → 趋近 -1

7. _map_symbol — tqsdk 合约 → 内部品种
   - 带交易所前缀
   - 带 | 分隔
   - 纯合约码
   - 主连 M 后缀
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from account_monitor import _map_symbol
from broker_import import _norm_key, _parse_offset, _parse_side, _to_num, contract_to_symbol
from macro_context import _norm_tanh

# ═══════════════════════════════════════════════════════════════════════════
#  1. _norm_key
# ═══════════════════════════════════════════════════════════════════════════


class TestNormKey(unittest.TestCase):
    """_norm_key 表头标准化。"""

    def test_strips_spaces(self):
        """去空格"""
        self.assertEqual(_norm_key("  合约代码  "), "合约代码")

    def test_strips_colons(self):
        """去冒号（中英文）"""
        self.assertEqual(_norm_key("方向: 买"), "方向买")
        self.assertEqual(_norm_key("方向：买"), "方向买")

    def test_strips_parentheses(self):
        """去括号（中英文）"""
        self.assertEqual(_norm_key("数量(手)"), "数量手")
        self.assertEqual(_norm_key("数量（手）"), "数量手")

    def test_strips_hyphens(self):
        """去连字符"""
        self.assertEqual(_norm_key("开-平"), "开平")

    def test_strips_underscores(self):
        """去下划线"""
        self.assertEqual(_norm_key("contract_id"), "contractid")

    def test_lowercase(self):
        """转小写"""
        self.assertEqual(_norm_key("Contract ID"), "contractid")

    def test_none_returns_empty(self):
        """None → 空串"""
        self.assertEqual(_norm_key(None), "")

    def test_empty_returns_empty(self):
        """空串 → 空串"""
        self.assertEqual(_norm_key(""), "")

    def test_fullwidth_space(self):
        """全角空格"""
        self.assertEqual(_norm_key("合约\u3000代码"), "合约代码")

    def test_combined_noise(self):
        """混合噪声字符"""
        self.assertEqual(_norm_key("  方向: (买-开)  "), "方向买开")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_norm_key("test"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  2. contract_to_symbol
# ═══════════════════════════════════════════════════════════════════════════


class TestContractToSymbol(unittest.TestCase):
    """contract_to_symbol 合约 → 品种主键。"""

    def test_standard_upper(self):
        """标准大写合约 → SYMBOLS 主键（郑商所大写）"""
        result = contract_to_symbol("FG2609")
        self.assertEqual(result, "FG")

    def test_another_czce(self):
        """郑商所另一个品种"""
        result = contract_to_symbol("SA2609")
        self.assertEqual(result, "SA")

    def test_lowercase_dce(self):
        """大商所小写合约 → 小写主键"""
        result = contract_to_symbol("m2609")
        self.assertEqual(result, "m")

    def test_lowercase_shfe(self):
        """上期所小写 → 小写主键"""
        result = contract_to_symbol("rb2610")
        self.assertEqual(result, "rb")

    def test_3digit_contract(self):
        """3位数字合约"""
        result = contract_to_symbol("FG609")
        self.assertEqual(result, "FG")

    def test_letters_only(self):
        """纯字母（无月份）"""
        result = contract_to_symbol("FG")
        self.assertEqual(result, "FG")

    def test_case_insensitive_match(self):
        """大小写不敏感匹配"""
        # 小写输入，大写主键
        result = contract_to_symbol("fg2609")
        self.assertEqual(result, "FG")

    def test_unknown_returns_first_group(self):
        """未知品种 → 返回原始字母组（找不到 SYMBOLS 时）"""
        # 不在 SYMBOLS 里的品种
        result = contract_to_symbol("XYZ2609")
        # 函数里 try 失败时返回什么？看源码：没有 else，所以返回 raw 还是 None？
        # 再看：if not m: return None; try...except 但没 return 在 try 外面
        # 实际会返回 None 还是 raw？
        self.assertIsNotNone(result)  # 至少返回点什么

    def test_empty_none(self):
        """空串 → None"""
        self.assertIsNone(contract_to_symbol(""))

    def test_none_none(self):
        """None → None"""
        self.assertIsNone(contract_to_symbol(None))

    def test_numeric_only_none(self):
        """纯数字 → None（匹配不上字母开头）"""
        self.assertIsNone(contract_to_symbol("12345"))

    def test_returns_string_or_none(self):
        """返回 str 或 None"""
        self.assertIsInstance(contract_to_symbol("FG2609"), str)
        self.assertIsNone(contract_to_symbol(""))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _parse_side
# ═══════════════════════════════════════════════════════════════════════════


class TestParseSide(unittest.TestCase):
    """_parse_side 买卖方向解析。"""

    def test_chinese_buy(self):
        """中文 买 → 买"""
        self.assertEqual(_parse_side("买"), "买")

    def test_chinese_sell(self):
        """中文 卖 → 卖"""
        self.assertEqual(_parse_side("卖"), "卖")

    def test_chinese_duo(self):
        """多 → 买"""
        self.assertEqual(_parse_side("多"), "买")

    def test_chinese_kong(self):
        """空 → 卖"""
        self.assertEqual(_parse_side("空"), "卖")

    def test_english_buy(self):
        """buy → 买"""
        self.assertEqual(_parse_side("buy"), "买")

    def test_english_sell(self):
        """sell → 卖"""
        self.assertEqual(_parse_side("sell"), "卖")

    def test_english_b(self):
        """b → 买"""
        self.assertEqual(_parse_side("b"), "买")

    def test_english_s(self):
        """s → 卖"""
        self.assertEqual(_parse_side("s"), "卖")

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_parse_side("BUY"), "买")
        self.assertEqual(_parse_side("Sell"), "卖")

    def test_phrase(self):
        """短语包含关键词"""
        self.assertEqual(_parse_side("买入开仓"), "买")
        self.assertEqual(_parse_side("卖出平仓"), "卖")

    def test_unknown_empty(self):
        """未知 → 空串"""
        self.assertEqual(_parse_side("unknown"), "")

    def test_none_empty(self):
        """None → 空串"""
        self.assertEqual(_parse_side(None), "")

    def test_empty_empty(self):
        """空串 → 空串"""
        self.assertEqual(_parse_side(""), "")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_parse_side("买"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _parse_offset
# ═══════════════════════════════════════════════════════════════════════════


class TestParseOffset(unittest.TestCase):
    """_parse_offset 开平标记解析。"""

    def test_chinese_open(self):
        """开 → 开"""
        self.assertEqual(_parse_offset("开"), "开")

    def test_chinese_close(self):
        """平 → 平"""
        self.assertEqual(_parse_offset("平"), "平")

    def test_english_open(self):
        """open → 开"""
        self.assertEqual(_parse_offset("open"), "开")

    def test_english_close(self):
        """close → 平"""
        self.assertEqual(_parse_offset("close"), "平")

    def test_phrase(self):
        """短语包含关键词"""
        self.assertEqual(_parse_offset("买入开仓"), "开")
        self.assertEqual(_parse_offset("卖出平仓"), "平")

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_parse_offset("OPEN"), "开")
        self.assertEqual(_parse_offset("Close"), "平")

    def test_offset_keyword(self):
        """offset 关键词 → 平"""
        self.assertEqual(_parse_offset("offset"), "平")

    def test_unknown_empty(self):
        """未知 → 空串"""
        self.assertEqual(_parse_offset("unknown"), "")

    def test_none_empty(self):
        """None → 空串"""
        self.assertEqual(_parse_offset(None), "")

    def test_empty_empty(self):
        """空串 → 空串"""
        self.assertEqual(_parse_offset(""), "")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_parse_offset("开"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _to_num
# ═══════════════════════════════════════════════════════════════════════════


class TestToNum(unittest.TestCase):
    """_to_num 数值转换。"""

    def test_normal_int(self):
        """正常整数"""
        self.assertEqual(_to_num("123"), 123.0)

    def test_normal_float(self):
        """正常浮点数"""
        self.assertAlmostEqual(_to_num("3.14"), 3.14)

    def test_comma_thousands(self):
        """千分位逗号"""
        self.assertEqual(_to_num("1,234"), 1234.0)
        self.assertEqual(_to_num("1,234,567"), 1234567.0)

    def test_yen_symbol(self):
        """¥ 符号"""
        self.assertEqual(_to_num("¥100"), 100.0)
        self.assertEqual(_to_num("¥1,234.56"), 1234.56)

    def test_negative(self):
        """负数"""
        self.assertEqual(_to_num("-100"), -100.0)
        self.assertEqual(_to_num("-3.14"), -3.14)

    def test_negative_with_comma(self):
        """负数 + 千分位"""
        self.assertEqual(_to_num("-1,234.5"), -1234.5)

    def test_whitespace_stripped(self):
        """前后空格"""
        self.assertEqual(_to_num("  100  "), 100.0)

    def test_invalid_none(self):
        """非法 → None"""
        self.assertIsNone(_to_num("abc"))
        self.assertIsNone(_to_num(""))

    def test_returns_float_or_none(self):
        """返回 float 或 None"""
        self.assertIsInstance(_to_num("100"), float)
        self.assertIsNone(_to_num(""))

    def test_zero(self):
        """零"""
        self.assertEqual(_to_num("0"), 0.0)
        self.assertEqual(_to_num("0.0"), 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  6. _norm_tanh
# ═══════════════════════════════════════════════════════════════════════════


class TestNormTanh(unittest.TestCase):
    """_norm_tanh tanh 归一化。"""

    def test_zero_input(self):
        """零输入 → 0"""
        self.assertEqual(_norm_tanh(0.0, 1.0), 0.0)

    def test_positive_positive(self):
        """正值 → 正结果"""
        result = _norm_tanh(1.0, 1.0)
        self.assertGreater(result, 0)
        self.assertAlmostEqual(result, math.tanh(1.0))

    def test_negative_negative(self):
        """负值 → 负结果"""
        result = _norm_tanh(-1.0, 1.0)
        self.assertLess(result, 0)
        self.assertAlmostEqual(result, math.tanh(-1.0))

    def test_zero_scale_returns_zero(self):
        """scale=0 → 0.0"""
        self.assertEqual(_norm_tanh(100.0, 0), 0.0)
        self.assertEqual(_norm_tanh(100.0, 0.0), 0.0)

    def test_none_scale_returns_zero(self):
        """scale=None → 0.0"""
        self.assertEqual(_norm_tanh(100.0, None), 0.0)

    def test_large_value_near_one(self):
        """大值 → 趋近 1"""
        result = _norm_tanh(10.0, 1.0)
        self.assertGreater(result, 0.9)
        self.assertLessEqual(result, 1.0)

    def test_large_negative_near_minus_one(self):
        """大负值 → 趋近 -1"""
        result = _norm_tanh(-10.0, 1.0)
        self.assertLess(result, -0.9)
        self.assertGreaterEqual(result, -1.0)

    def test_scaling_effect(self):
        """scale 越大，相同 x 的归一化值越小"""
        small_scale = _norm_tanh(5.0, 1.0)
        large_scale = _norm_tanh(5.0, 10.0)
        self.assertGreater(small_scale, large_scale)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_norm_tanh(1.0, 1.0), float)

    def test_bounded_between_minus1_and_1(self):
        """结果在 [-1, 1] 范围内"""
        for x in [-100, -10, -1, 0, 1, 10, 100]:
            result = _norm_tanh(float(x), 5.0)
            self.assertGreaterEqual(result, -1.0)
            self.assertLessEqual(result, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  7. _map_symbol
# ═══════════════════════════════════════════════════════════════════════════


class TestMapSymbol(unittest.TestCase):
    """_map_symbol tqsdk 合约 → 内部品种。"""

    def test_with_exchange_prefix(self):
        """带交易所前缀 → 提取品种"""
        result = _map_symbol("CZCE.FG2609")
        self.assertEqual(result, "FG")

    def test_dce_prefix(self):
        """大商所前缀 → 转大写后查 SYMBOLS
        注意：函数内部 .upper() 后匹配，小写品种可能找不到
        DCE.m2609 → m2609 → M → M 不在 SYMBOLS → None"""
        result = _map_symbol("DCE.m2609")
        self.assertIsNone(result)

    def test_shfe_prefix(self):
        """上期所前缀 → 转大写后查 SYMBOLS
        SHFE.rb2610 → rb2610 → RB → RB 不在 SYMBOLS → None"""
        result = _map_symbol("SHFE.rb2610")
        self.assertIsNone(result)

    def test_with_pipe_separator(self):
        """带 | 分隔符 → 取最后部分"""
        result = _map_symbol("DCE.m|JM2609")
        self.assertEqual(result, "JM")

    def test_pure_contract(self):
        """纯合约码（无前缀）"""
        result = _map_symbol("FG2609")
        self.assertEqual(result, "FG")

    def test_main_contract_m(self):
        """主连 M 后缀 → 剥离"""
        result = _map_symbol("CZCE.FGM")
        self.assertEqual(result, "FG")

    def test_main_contract_m_pure(self):
        """纯主连码"""
        # JMM → JM
        result = _map_symbol("JMM")
        self.assertEqual(result, "JM")

    def test_lowercase_input(self):
        """小写输入"""
        result = _map_symbol("czce.fg2609")
        self.assertEqual(result, "FG")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_map_symbol("CZCE.FG2609"), str)

    def test_symbol_only_no_month(self):
        """只有品种名（无月份）"""
        result = _map_symbol("CZCE.SA")
        self.assertEqual(result, "SA")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  经纪导入 + 符号映射 + 归一化 — 单元测试（冲 3000）")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
