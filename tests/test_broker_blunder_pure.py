#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
broker_import + blunder 纯函数 — 单元测试
==============================================

1. _norm_key — 表头键规范化
   - 去空格、冒号、括号、下划线、横杠
   - 转小写
   - None → 空串
   - 数字原样

2. _map_columns — 表头 → 标准字段位置
   - 中文表头映射
   - 英文表头映射
   - 部分匹配
   - 未知字段跳过
   - 返回 {field: index}

3. contract_to_symbol — 合约代码 → 品种主键
   - FG2608 → FG
   - rb2610 → rb
   - 3位数字 → 正确提取字母
   - 纯字母 → 原样返回
   - None → None
   - 空串 → None

4. _parse_side — 买卖方向解析
   - 买/buy/b/多 → "买"
   - 卖/sell/s/空 → "卖"
   - 大小写不敏感
   - 未知 → ""
   - None → ""

5. _parse_offset — 开平标志解析
   - 开/open → "开"
   - 平/close → "平"
   - 开平 → "平"（先匹配平）
   - 未知 → ""
   - None → ""

6. _to_num — 数值解析
   - 整数 → float
   - 小数 → float
   - 带逗号 → 去逗号后解析
   - 带 ¥ → 去符号后解析
   - 空串 → None
   - None → None
   - 非法 → None

7. _to_ts — 时间字符串 → 时间戳
   - 正常格式 → 正确时间戳
   - 空串 → 0.0
   - None → 0.0
   - 格式错误 → 0.0
   - 返回 float
"""

import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from blunder_check import _to_ts
from broker_import import (
    _map_columns,
    _norm_key,
    _parse_offset,
    _parse_side,
    _to_num,
    contract_to_symbol,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. _norm_key
# ═══════════════════════════════════════════════════════════════════════════


class TestNormKey(unittest.TestCase):
    """_norm_key 表头键规范化。"""

    def test_removes_spaces(self):
        """去除空格"""
        self.assertEqual(_norm_key("成交 日期"), "成交日期")

    def test_removes_colons(self):
        """去除冒号"""
        self.assertEqual(_norm_key("日期："), "日期")

    def test_removes_parentheses(self):
        """去除括号"""
        self.assertEqual(_norm_key("价格(元)"), "价格元")

    def test_removes_underscores(self):
        """去除下划线"""
        self.assertEqual(_norm_key("trade_date"), "tradedate")

    def test_removes_dashes(self):
        """去除横杠"""
        self.assertEqual(_norm_key("buy-sell"), "buysell")

    def test_lowercase(self):
        """转小写"""
        self.assertEqual(_norm_key("TradeDate"), "tradedate")

    def test_none_becomes_empty(self):
        """None → 空串"""
        self.assertEqual(_norm_key(None), "")

    def test_digits_preserved(self):
        """数字原样保留"""
        self.assertEqual(_norm_key("price_2"), "price2")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_norm_key("test"), str)

    def test_full_messy_header(self):
        """完整脏乱表头 → 干净小写"""
        messy = " 成交日期（日期）：Date_D "
        result = _norm_key(messy)
        self.assertEqual(result, "成交日期日期dated")


# ═══════════════════════════════════════════════════════════════════════════
#  2. _map_columns
# ═══════════════════════════════════════════════════════════════════════════


class TestMapColumns(unittest.TestCase):
    """_map_columns 表头 → 标准字段位置。"""

    def test_chinese_header(self):
        """中文表头映射"""
        header = ["成交日期", "合约", "买卖", "开平", "成交价", "手数"]
        idx = _map_columns(header)
        self.assertEqual(idx["date"], 0)
        self.assertEqual(idx["contract"], 1)
        self.assertEqual(idx["side"], 2)
        self.assertEqual(idx["offset"], 3)
        self.assertEqual(idx["price"], 4)
        self.assertEqual(idx["lots"], 5)

    def test_english_header(self):
        """英文表头映射"""
        header = ["date", "instrument", "direction", "offset", "price", "volume"]
        idx = _map_columns(header)
        self.assertEqual(idx["date"], 0)
        self.assertEqual(idx["contract"], 1)
        self.assertEqual(idx["side"], 2)
        self.assertEqual(idx["offset"], 3)
        self.assertEqual(idx["price"], 4)
        self.assertEqual(idx["lots"], 5)

    def test_unknown_field_skipped(self):
        """未知字段跳过"""
        header = ["成交日期", "备注", "价格"]
        idx = _map_columns(header)
        self.assertEqual(idx["date"], 0)
        self.assertEqual(idx["price"], 2)
        self.assertNotIn("备注", idx)

    def test_returns_dict(self):
        """返回 dict"""
        self.assertIsInstance(_map_columns(["日期"]), dict)

    def test_partial_match(self):
        """部分匹配（别名包含在表头中）"""
        header = ["成交编号ID", "成交价格(元)"]
        idx = _map_columns(header)
        self.assertIn("tid", idx)
        self.assertIn("price", idx)

    def test_first_occurrence_taken(self):
        """第一次出现的位置生效"""
        header = ["成交价", "成交均价", "结算价"]
        idx = _map_columns(header)
        self.assertEqual(idx["price"], 0)  # 第一个价格字段

    def test_fee_mapping(self):
        """手续费字段映射"""
        header = ["手续费", "佣金"]
        idx = _map_columns(header)
        self.assertEqual(idx["fee"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. contract_to_symbol
# ═══════════════════════════════════════════════════════════════════════════


class TestContractToSymbol(unittest.TestCase):
    """contract_to_symbol 合约代码 → 品种主键。"""

    def test_fg_uppercase(self):
        """FG2608 → FG"""
        self.assertEqual(contract_to_symbol("FG2608"), "FG")

    def test_rb_lowercase(self):
        """rb2610 → rb"""
        self.assertEqual(contract_to_symbol("rb2610"), "rb")

    def test_3digit_contract(self):
        """3位数字合约 → 正确提取字母"""
        # FG608 → FG（字母部分）
        result = contract_to_symbol("FG608")
        self.assertEqual(result, "FG")

    def test_pure_letters(self):
        """纯字母 → 原样返回（主连）"""
        result = contract_to_symbol("RBM")
        self.assertEqual(result, "RBM")

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(contract_to_symbol(None))

    def test_empty_string_none(self):
        """空串 → None"""
        self.assertIsNone(contract_to_symbol(""))

    def test_lowercase_contract(self):
        """小写合约代码 → 正确映射"""
        # fg2608 → FG（通过 SYMBOLS 匹配）
        result = contract_to_symbol("fg2608")
        self.assertEqual(result, "FG")

    def test_returns_string_or_none(self):
        """返回 str 或 None"""
        self.assertIsInstance(contract_to_symbol("FG2608"), str)
        self.assertIsNone(contract_to_symbol(""))

    def test_sa_contract(self):
        """SA01 → SA"""
        self.assertEqual(contract_to_symbol("SA2601"), "SA")


# ═══════════════════════════════════════════════════════════════════════════
#  4. _parse_side
# ═══════════════════════════════════════════════════════════════════════════


class TestParseSide(unittest.TestCase):
    """_parse_side 买卖方向解析。"""

    def test_chinese_buy(self):
        """买 → '买'"""
        self.assertEqual(_parse_side("买"), "买")

    def test_english_buy(self):
        """buy → '买'"""
        self.assertEqual(_parse_side("buy"), "买")

    def test_b_letter(self):
        """b → '买'"""
        self.assertEqual(_parse_side("B"), "买")

    def test_duo_word(self):
        """多 → '买'"""
        self.assertEqual(_parse_side("多头"), "买")

    def test_chinese_sell(self):
        """卖 → '卖'"""
        self.assertEqual(_parse_side("卖"), "卖")

    def test_english_sell(self):
        """sell → '卖'"""
        self.assertEqual(_parse_side("SELL"), "卖")

    def test_s_letter(self):
        """s → '卖'"""
        self.assertEqual(_parse_side("s"), "卖")

    def test_kong_word(self):
        """空 → '卖'"""
        self.assertEqual(_parse_side("空头"), "卖")

    def test_unknown_empty(self):
        """未知 → ''"""
        self.assertEqual(_parse_side("flat"), "")

    def test_none_empty(self):
        """None → ''"""
        self.assertEqual(_parse_side(None), "")

    def test_empty_string_empty(self):
        """空串 → ''"""
        self.assertEqual(_parse_side(""), "")

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_parse_side("BUY"), "买")
        self.assertEqual(_parse_side("Sell"), "卖")


# ═══════════════════════════════════════════════════════════════════════════
#  5. _parse_offset
# ═══════════════════════════════════════════════════════════════════════════


class TestParseOffset(unittest.TestCase):
    """_parse_offset 开平标志解析。"""

    def test_open_chinese(self):
        """开 → '开'"""
        self.assertEqual(_parse_offset("开仓"), "开")

    def test_open_english(self):
        """open → '开'"""
        self.assertEqual(_parse_offset("OPEN"), "开")

    def test_close_chinese(self):
        """平 → '平'"""
        self.assertEqual(_parse_offset("平仓"), "平")

    def test_close_english(self):
        """close → '平'"""
        self.assertEqual(_parse_offset("close"), "平")

    def test_openclose_prioritizes_close(self):
        """开平 → '平'（先匹配平）"""
        # 函数逻辑是先判断平再判断开
        self.assertEqual(_parse_offset("开平"), "平")

    def test_unknown_empty(self):
        """未知 → ''"""
        self.assertEqual(_parse_offset("unknown"), "")

    def test_none_empty(self):
        """None → ''"""
        self.assertEqual(_parse_offset(None), "")

    def test_empty_string_empty(self):
        """空串 → ''"""
        self.assertEqual(_parse_offset(""), "")

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_parse_offset("CLOSE"), "平")
        self.assertEqual(_parse_offset("Open"), "开")

    def test_offset_word(self):
        """offset → '平'"""
        self.assertEqual(_parse_offset("offset"), "平")


# ═══════════════════════════════════════════════════════════════════════════
#  6. _to_num
# ═══════════════════════════════════════════════════════════════════════════


class TestToNum(unittest.TestCase):
    """_to_num 数值解析。"""

    def test_integer(self):
        """整数 → float"""
        self.assertEqual(_to_num("100"), 100.0)

    def test_decimal(self):
        """小数 → float"""
        self.assertEqual(_to_num("100.5"), 100.5)

    def test_with_commas(self):
        """带逗号 → 去逗号后解析"""
        self.assertEqual(_to_num("1,234.56"), 1234.56)

    def test_with_yen_symbol(self):
        """带 ¥ → 去符号后解析"""
        self.assertEqual(_to_num("¥99.9"), 99.9)

    def test_empty_string_none(self):
        """空串 → None"""
        self.assertIsNone(_to_num(""))

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_to_num(None))

    def test_invalid_returns_none(self):
        """非法 → None"""
        self.assertIsNone(_to_num("abc"))

    def test_negative(self):
        """负数"""
        self.assertEqual(_to_num("-100"), -100.0)

    def test_returns_float_or_none(self):
        """返回 float 或 None"""
        self.assertIsInstance(_to_num("1.0"), float)
        self.assertIsNone(_to_num("bad"))

    def test_int_input(self):
        """int 输入也能解析"""
        self.assertEqual(_to_num(42), 42.0)


# ═══════════════════════════════════════════════════════════════════════════
#  7. _to_ts
# ═══════════════════════════════════════════════════════════════════════════


class TestToTs(unittest.TestCase):
    """_to_ts 时间字符串 → 时间戳。"""

    def test_valid_format(self):
        """正常格式 → 正确时间戳"""
        s = "2026-08-28 10:00:00"
        ts = _to_ts(s)
        expected = datetime(2026, 8, 28, 10, 0, 0).timestamp()
        self.assertEqual(ts, expected)

    def test_empty_string_zero(self):
        """空串 → 0.0"""
        self.assertEqual(_to_ts(""), 0.0)

    def test_none_zero(self):
        """None → 0.0"""
        self.assertEqual(_to_ts(None), 0.0)

    def test_bad_format_zero(self):
        """格式错误 → 0.0"""
        self.assertEqual(_to_ts("not a date"), 0.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_to_ts("2026-08-28 10:00:00"), float)

    def test_different_times(self):
        """不同时间不同戳"""
        t1 = _to_ts("2026-08-28 09:00:00")
        t2 = _to_ts("2026-08-28 10:00:00")
        self.assertGreater(t2, t1)

    def test_midnight(self):
        """午夜 00:00:00"""
        ts = _to_ts("2026-08-28 00:00:00")
        expected = datetime(2026, 8, 28, 0, 0, 0).timestamp()
        self.assertEqual(ts, expected)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  broker_import + blunder 纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
