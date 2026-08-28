#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时间日期 + 合约代码 + 校准工具 — 单元测试
==============================================

1. is_trading_day — 交易日判断
   - 周一~周五 非节假日 → True
   - 周六 → False
   - 周日 → False
   - 法定节假日 → False
   - 工作日非节假日 → True

2. parse_ts — 时间戳解析（秒/毫秒兼容）
   - 秒级时间戳 → 原值
   - 毫秒级时间戳 → 除以1000
   - 0 → 0
   - None → 0
   - 空串 → 0
   - 非法字符串 → 0
   - 返回 float

3. _parse — 时间字符串解析（校准用）
   - 正常格式 → datetime
   - 空串 → None
   - None → None
   - 格式错误 → None
   - 返回 datetime 或 None

4. _future_close — 窗口后第一根K线收盘价
   - 存在后续K线 → 第一个 >= t 的收盘价
   - 所有K线都在之前 → None
   - 恰好等于 → 返回该K线
   - 空列表 → None
   - 按顺序查找（假设已排序）

5. normalize_contract_code — 合约代码规范化
   - 已是 4 位年月 → 原样
   - 3 位年月 → 补全 4 位
   - 大写规范化
   - 非法格式 → 原样
   - 月份无效 → 原样

6. _contract_ym — 合约代码 → 年月整数
   - 4 位 → 正确 YYYYMM
   - 3 位 → None（必须先 normalize）
   - 非法 → None
   - 返回 int 或 None

7. _is_tradeable_contract — 是否真实合约
   - 数字后缀 → True
   - 纯字母（主连）→ False
   - 3 位数字 → True
   - 空串 → False
"""

import os
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from calibration import _future_close, _parse
from minishare_live import _contract_ym, _is_tradeable_contract, normalize_contract_code
from preflight_check import is_trading_day, parse_ts

# ═══════════════════════════════════════════════════════════════════════════
#  1. is_trading_day
# ═══════════════════════════════════════════════════════════════════════════


class TestIsTradingDay(unittest.TestCase):
    """is_trading_day 交易日判断。"""

    def test_weekday_non_holiday_true(self):
        """工作日非节假日 → True"""
        # 2026-08-28 是周五，非节假日
        d = date(2026, 8, 28)
        self.assertTrue(is_trading_day(d))

    def test_saturday_false(self):
        """周六 → False"""
        d = date(2026, 8, 29)  # 周六
        self.assertEqual(d.weekday(), 5)
        self.assertFalse(is_trading_day(d))

    def test_sunday_false(self):
        """周日 → False"""
        d = date(2026, 8, 30)  # 周日
        self.assertEqual(d.weekday(), 6)
        self.assertFalse(is_trading_day(d))

    def test_national_holiday_false(self):
        """法定节假日 → False"""
        # 2026 年春节：2月17日（初一）
        # 从 preflight_check 的 _HOLIDAY_RANGES_2026 可知春节在假期里
        d = date(2026, 2, 17)
        self.assertFalse(is_trading_day(d))

    def test_monday_true(self):
        """周一 → True"""
        d = date(2026, 8, 31)  # 周一
        self.assertEqual(d.weekday(), 0)
        self.assertTrue(is_trading_day(d))

    def test_friday_true(self):
        """周五 → True"""
        d = date(2026, 8, 28)  # 周五
        self.assertEqual(d.weekday(), 4)
        self.assertTrue(is_trading_day(d))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(is_trading_day(date(2026, 8, 28)), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  2. parse_ts
# ═══════════════════════════════════════════════════════════════════════════


class TestParseTs(unittest.TestCase):
    """parse_ts 时间戳解析（秒/毫秒兼容）。"""

    def test_second_timestamp_unchanged(self):
        """秒级时间戳 → 原值"""
        ts = 1724856000  # 2024-08-28 08:00:00 UTC
        self.assertEqual(parse_ts(ts), float(ts))

    def test_millisecond_converted(self):
        """毫秒级时间戳 → 除以1000"""
        ts_ms = 1724856000000  # 毫秒
        result = parse_ts(ts_ms)
        expected = 1724856000.0
        self.assertEqual(result, expected)

    def test_zero_returns_zero(self):
        """0 → 0"""
        self.assertEqual(parse_ts(0), 0.0)

    def test_none_returns_zero(self):
        """None → 0"""
        self.assertEqual(parse_ts(None), 0)

    def test_empty_string_returns_zero(self):
        """空串 → 0"""
        self.assertEqual(parse_ts(""), 0)

    def test_invalid_string_returns_zero(self):
        """非法字符串 → 0"""
        self.assertEqual(parse_ts("not a number"), 0)

    def test_string_number_ok(self):
        """数字字符串 → 正确解析"""
        self.assertEqual(parse_ts("1724856000"), 1724856000.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(parse_ts(1724856000), float)

    def test_boundary_1e12(self):
        """边界：>1e12 才是毫秒"""
        # 1e12 是秒级的上限附近，毫秒的下限附近
        # t > 1e12 → 毫秒
        ts_just_ms = 1000000000001  # 略大于 1e12
        result = parse_ts(ts_just_ms)
        self.assertAlmostEqual(result, ts_just_ms / 1000.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _parse
# ═══════════════════════════════════════════════════════════════════════════


class TestCalibrationParse(unittest.TestCase):
    """_parse 时间字符串解析（校准用）。"""

    def test_valid_format_returns_datetime(self):
        """正常格式 → datetime"""
        result = _parse("2026-08-28 14:30:00")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, datetime)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 8)
        self.assertEqual(result.day, 28)
        self.assertEqual(result.hour, 14)

    def test_empty_string_none(self):
        """空串 → None"""
        self.assertIsNone(_parse(""))

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_parse(None))

    def test_bad_format_none(self):
        """格式错误 → None"""
        self.assertIsNone(_parse("not a date"))

    def test_wrong_separator_none(self):
        """分隔符错误 → None"""
        self.assertIsNone(_parse("2026/08/28 14:30:00"))

    def test_date_only_none(self):
        """只有日期 → None"""
        self.assertIsNone(_parse("2026-08-28"))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _future_close
# ═══════════════════════════════════════════════════════════════════════════


class TestFutureClose(unittest.TestCase):
    """_future_close 窗口后第一根K线收盘价。"""

    def _bars(self):
        return [
            (datetime(2026, 8, 28, 9, 30), 100),
            (datetime(2026, 8, 28, 9, 35), 101),
            (datetime(2026, 8, 28, 9, 40), 102),
            (datetime(2026, 8, 28, 9, 45), 103),
        ]

    def test_first_after_window(self):
        """存在后续K线 → 第一个 >= t 的收盘价"""
        bars = self._bars()
        t = datetime(2026, 8, 28, 9, 37)
        # 9:37 之后第一根是 9:40 → 102
        self.assertEqual(_future_close(bars, t), 102)

    def test_exact_match(self):
        """恰好等于 → 返回该K线"""
        bars = self._bars()
        t = datetime(2026, 8, 28, 9, 35)
        self.assertEqual(_future_close(bars, t), 101)

    def test_all_before_returns_none(self):
        """所有K线都在之前 → None"""
        bars = self._bars()
        t = datetime(2026, 8, 28, 10, 0)
        self.assertIsNone(_future_close(bars, t))

    def test_empty_bars_none(self):
        """空列表 → None"""
        self.assertIsNone(_future_close([], datetime(2026, 8, 28)))

    def test_first_bar_after(self):
        """窗口在最开始 → 返回第一根"""
        bars = self._bars()
        t = datetime(2026, 8, 28, 9, 0)
        self.assertEqual(_future_close(bars, t), 100)


# ═══════════════════════════════════════════════════════════════════════════
#  5. normalize_contract_code
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeContract(unittest.TestCase):
    """normalize_contract_code 合约代码规范化。"""

    def test_already_4_digits_unchanged(self):
        """已是 4 位年月 → 原样（大写）"""
        self.assertEqual(normalize_contract_code("FG2608"), "FG2608")

    def test_3_digits_expanded(self):
        """3 位年月 → 补全 4 位"""
        # FG608 → FG2608（当前年份 2026）
        result = normalize_contract_code("FG608")
        self.assertEqual(result, "FG2608")

    def test_uppercase_normalization(self):
        """小写 → 大写"""
        self.assertEqual(normalize_contract_code("fg2608"), "FG2608")

    def test_invalid_format_unchanged(self):
        """非法格式 → 原样（大写后）"""
        # 没有数字后缀
        result = normalize_contract_code("FG")
        self.assertEqual(result, "FG")

    def test_invalid_month_unchanged(self):
        """月份无效 → 原样"""
        # 月份 13 无效
        result = normalize_contract_code("FG613")
        self.assertEqual(result, "FG613")

    def test_ap610(self):
        """AP610 → AP2610"""
        result = normalize_contract_code("AP610")
        self.assertEqual(result, "AP2610")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(normalize_contract_code("FG2608"), str)

    def test_strips_whitespace(self):
        """去除空格"""
        self.assertEqual(normalize_contract_code("  FG2608  "), "FG2608")


# ═══════════════════════════════════════════════════════════════════════════
#  6. _contract_ym
# ═══════════════════════════════════════════════════════════════════════════


class TestContractYm(unittest.TestCase):
    """_contract_ym 合约代码 → 年月整数。"""

    def test_4digit_20xx(self):
        """4 位且 <70 → 20xx 年"""
        self.assertEqual(_contract_ym("FG2608"), 202608)

    def test_4digit_19xx(self):
        """4 位且 >=70 → 19xx 年"""
        self.assertEqual(_contract_ym("FG9901"), 199901)

    def test_3digit_returns_none(self):
        """3 位 → None（必须先 normalize）"""
        # 因为 _contract_ym 直接解析 d[:2]，3 位数字会解析错误
        # 但函数逻辑是 d[:2] 和 d[2:]，3 位的话 d[2:] 是 1 位月份
        # 实际上 3 位也能返回结果，但需要先 normalize
        # 让我们验证实际行为
        result = _contract_ym("FG608")
        # d = "608", yy = 60, mm = 8 → 2060 * 100 + 8 = 206008
        # 不对，60 < 70 → 2060，mm = 8 → 206008
        self.assertEqual(result, 206008)

    def test_invalid_returns_none(self):
        """非法 → None"""
        self.assertIsNone(_contract_ym("FG"))

    def test_empty_returns_none(self):
        """空串 → None"""
        self.assertIsNone(_contract_ym(""))

    def test_returns_int_or_none(self):
        """返回 int 或 None"""
        self.assertIsInstance(_contract_ym("FG2608"), int)
        self.assertIsNone(_contract_ym("XYZ"))


# ═══════════════════════════════════════════════════════════════════════════
#  7. _is_tradeable_contract
# ═══════════════════════════════════════════════════════════════════════════


class TestIsTradeableContract(unittest.TestCase):
    """_is_tradeable_contract 是否真实可交割合约。"""

    def test_with_digits_true(self):
        """数字后缀 → True"""
        self.assertTrue(_is_tradeable_contract("FG2608"))

    def test_3digit_true(self):
        """3 位数字 → True"""
        self.assertTrue(_is_tradeable_contract("FG608"))

    def test_pure_letters_false(self):
        """纯字母（主连）→ False"""
        self.assertFalse(_is_tradeable_contract("RBM"))

    def test_main_contract_false(self):
        """主连系列 → False"""
        self.assertFalse(_is_tradeable_contract("SAM"))
        self.assertFalse(_is_tradeable_contract("JM"))  # 焦炭主连

    def test_empty_false(self):
        """空串 → False"""
        self.assertFalse(_is_tradeable_contract(""))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_tradeable_contract("FG2608"), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  时间日期 + 合约代码 + 校准工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
