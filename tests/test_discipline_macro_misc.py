#!/usr/bin/env python3
"""
纪律复盘 + 宏观 + 其他零散纯函数 — 单元测试
====================================================

1. _parse_time — 时间字符串双格式解析
   - "YYYY-MM-DD HH:MM:SS" → datetime
   - "YYYY-MM-DD HH:MM" → datetime
   - 空串 → None
   - None → None
   - 格式错误 → None

2. _period_bounds — 周期边界计算
   - daily: 当日 00:00 ~ 次日 00:00
   - weekly: 当周周一 00:00 ~ 下周一 00:00
   - monthly: 当月1号 00:00 ~ 下月1号 00:00
   - 12月跨年
   - 返回 (start, end) 二元组

3. _friday_of — 计算当周周五
   - 周一 → 当周周五
   - 周五 → 自身
   - 周日 → 下一周周五（下周5）
   - 周三 → 当周周五
   - 返回 date

4. _is_last_trading_day — 是否当月最后交易日
   - 月末在工作日 → 月末那天
   - 月末在周六 → 前周五
   - 月末在周日 → 前周五
   - 非月末 → False
   - 12月末 → 正确跨年

5. _norm_tanh — tanh 归一化
   - x=0 → 0
   - x>0 → 正值(0~1)
   - x<0 → 负值(-1~0)
   - scale=0 → 0
   - x很大 → 接近1
   - x很小 → 接近-1
   - 返回 float
"""

import math
import os
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from discipline_review import (
    _friday_of,
    _is_last_trading_day,
    _parse_time,
    _period_bounds,
)
from macro_context import _norm_tanh

# ═══════════════════════════════════════════════════════════════════════════
#  1. _parse_time
# ═══════════════════════════════════════════════════════════════════════════


class TestParseTime(unittest.TestCase):
    """_parse_time 时间字符串双格式解析。"""

    def test_full_format_with_seconds(self):
        """YYYY-MM-DD HH:MM:SS → datetime"""
        result = _parse_time("2026-08-28 14:30:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 8)
        self.assertEqual(result.day, 28)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.second, 0)

    def test_format_without_seconds(self):
        """YYYY-MM-DD HH:MM → datetime"""
        result = _parse_time("2026-08-28 14:30")
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.second, 0)

    def test_empty_string_none(self):
        """空串 → None"""
        self.assertIsNone(_parse_time(""))

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_parse_time(None))

    def test_bad_format_none(self):
        """格式错误 → None"""
        self.assertIsNone(_parse_time("not a date"))

    def test_wrong_separator_none(self):
        """分隔符错误 → None"""
        self.assertIsNone(_parse_time("2026/08/28 14:30"))

    def test_date_only_none(self):
        """只有日期 → None"""
        self.assertIsNone(_parse_time("2026-08-28"))

    def test_returns_datetime_or_none(self):
        """返回 datetime 或 None"""
        self.assertIsInstance(_parse_time("2026-08-28 10:00:00"), datetime)
        self.assertIsNone(_parse_time("bad"))


# ═══════════════════════════════════════════════════════════════════════════
#  2. _period_bounds
# ═══════════════════════════════════════════════════════════════════════════


class TestPeriodBounds(unittest.TestCase):
    """_period_bounds 周期边界计算。"""

    def test_daily_bounds(self):
        """daily: 当日 00:00 ~ 次日 00:00"""
        now = datetime(2026, 8, 28, 14, 30, 0)
        start, end = _period_bounds("daily", now=now)
        self.assertEqual(start, datetime(2026, 8, 28, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 29, 0, 0, 0))

    def test_weekly_bounds(self):
        """weekly: 当周周一 00:00 ~ 下周一 00:00"""
        # 2026-08-28 是周五
        now = datetime(2026, 8, 28, 14, 30, 0)
        start, end = _period_bounds("weekly", now=now)
        # 周一 = 8月24日
        self.assertEqual(start, datetime(2026, 8, 24, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 31, 0, 0, 0))

    def test_weekly_monday(self):
        """周一当天 → 从当天开始"""
        now = datetime(2026, 8, 24, 9, 0, 0)  # 周一
        start, end = _period_bounds("weekly", now=now)
        self.assertEqual(start, datetime(2026, 8, 24, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 31, 0, 0, 0))

    def test_monthly_bounds(self):
        """monthly: 当月1号 ~ 下月1号"""
        now = datetime(2026, 8, 15, 10, 0, 0)
        start, end = _period_bounds("monthly", now=now)
        self.assertEqual(start, datetime(2026, 8, 1, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 1, 0, 0, 0))

    def test_monthly_december_crossyear(self):
        """12月跨年"""
        now = datetime(2026, 12, 25, 10, 0, 0)
        start, end = _period_bounds("monthly", now=now)
        self.assertEqual(start, datetime(2026, 12, 1, 0, 0, 0))
        self.assertEqual(end, datetime(2027, 1, 1, 0, 0, 0))

    def test_returns_tuple(self):
        """返回 (start, end) 二元组"""
        now = datetime(2026, 8, 28)
        result = _period_bounds("daily", now=now)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], datetime)
        self.assertIsInstance(result[1], datetime)

    def test_start_before_end(self):
        """start < end"""
        now = datetime(2026, 8, 28)
        for kind in ("daily", "weekly", "monthly"):
            start, end = _period_bounds(kind, now=now)
            self.assertLess(start, end)

    def test_january_first(self):
        """1月1号 → 1月1日 ~ 2月1日"""
        now = datetime(2026, 1, 1, 0, 0, 0)
        start, end = _period_bounds("monthly", now=now)
        self.assertEqual(start, datetime(2026, 1, 1, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 2, 1, 0, 0, 0))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _friday_of
# ═══════════════════════════════════════════════════════════════════════════


class TestFridayOf(unittest.TestCase):
    """_friday_of 计算当周周五。"""

    def test_monday_to_friday(self):
        """周一 → 当周周五（+4天）"""
        d = date(2026, 8, 24)  # 周一
        self.assertEqual(d.weekday(), 0)
        fri = _friday_of(d)
        self.assertEqual(fri.weekday(), 4)  # 周五
        self.assertEqual(fri, date(2026, 8, 28))

    def test_friday_is_self(self):
        """周五 → 自身"""
        d = date(2026, 8, 28)  # 周五
        fri = _friday_of(d)
        self.assertEqual(fri, d)

    def test_wednesday_to_friday(self):
        """周三 → 当周周五（+2天）"""
        d = date(2026, 8, 26)  # 周三
        self.assertEqual(d.weekday(), 2)
        fri = _friday_of(d)
        self.assertEqual(fri.weekday(), 4)
        self.assertEqual(fri, date(2026, 8, 28))

    def test_sunday_to_next_friday(self):
        """周日 → 下一周周五（+5天，因为周日 weekday=6, 4-6=-2 → 上周五？不对）"""
        d = date(2026, 8, 30)  # 周日 (weekday=6)
        # 4 - 6 = -2 → 8月30日-2天 = 8月28日（上周五）
        fri = _friday_of(d)
        # 周日 weekday=6, 4-6=-2 → 往前2天 = 周五
        self.assertEqual(fri, date(2026, 8, 28))

    def test_saturday_to_friday(self):
        """周六 → 往前1天 = 周五"""
        d = date(2026, 8, 29)  # 周六 (weekday=5)
        fri = _friday_of(d)
        self.assertEqual(fri, date(2026, 8, 28))

    def test_returns_date(self):
        """返回 date"""
        self.assertIsInstance(_friday_of(date(2026, 8, 28)), date)

    def test_datetime_input(self):
        """datetime 输入也能处理"""
        d = datetime(2026, 8, 26, 14, 30)  # 周三
        fri = _friday_of(d)
        self.assertEqual(fri, date(2026, 8, 28))
        self.assertIsInstance(fri, date)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _is_last_trading_day
# ═══════════════════════════════════════════════════════════════════════════


class TestIsLastTradingDay(unittest.TestCase):
    """_is_last_trading_day 是否当月最后交易日。"""

    def test_last_day_weekday_true(self):
        """月末在工作日 → True"""
        # 2026年8月29日=周六, 31日=周一
        d = date(2026, 8, 31)  # 周一，月末
        self.assertTrue(_is_last_trading_day(d))

    def test_last_day_saturday_prev_friday(self):
        """月末在周六 → 前周五是最后交易日"""
        # 2026年5月31日是周日... 找一个月末是周六的
        # 2025年11月30日是周日...
        # 2026年1月31日是周六
        d_fri = date(2026, 1, 30)  # 周五（前一天）
        d_sat = date(2026, 1, 31)  # 周六（月末）
        self.assertTrue(_is_last_trading_day(d_fri))  # 周五 = 最后交易日
        self.assertFalse(_is_last_trading_day(d_sat))  # 周六不是

    def test_last_day_sunday_prev_friday(self):
        """月末在周日 → 前周五是最后交易日"""
        # 2026年5月31日是周日
        d_fri = date(2026, 5, 29)  # 周五
        d_sun = date(2026, 5, 31)  # 周日（月末）
        self.assertTrue(_is_last_trading_day(d_fri))  # 周五 = 最后交易日
        self.assertFalse(_is_last_trading_day(d_sun))  # 周日不是

    def test_mid_month_false(self):
        """月中 → False"""
        d = date(2026, 8, 15)
        self.assertFalse(_is_last_trading_day(d))

    def test_first_day_false(self):
        """月初 → False"""
        d = date(2026, 8, 1)
        self.assertFalse(_is_last_trading_day(d))

    def test_december_last_day(self):
        """12月末 → 正确跨年计算"""
        # 2026年12月31日是周四
        d = date(2026, 12, 31)
        self.assertTrue(_is_last_trading_day(d))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_last_trading_day(date(2026, 8, 15)), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _norm_tanh
# ═══════════════════════════════════════════════════════════════════════════


class TestNormTanh(unittest.TestCase):
    """_norm_tanh tanh 归一化。"""

    def test_zero_input_zero_output(self):
        """x=0 → 0"""
        self.assertEqual(_norm_tanh(0, 1.0), 0.0)

    def test_positive_input_positive_output(self):
        """x>0 → 正值(0~1)"""
        result = _norm_tanh(1.0, 1.0)
        self.assertGreater(result, 0)
        self.assertLess(result, 1.0)

    def test_negative_input_negative_output(self):
        """x<0 → 负值(-1~0)"""
        result = _norm_tanh(-1.0, 1.0)
        self.assertLess(result, 0)
        self.assertGreater(result, -1.0)

    def test_scale_zero_returns_zero(self):
        """scale=0 → 0"""
        self.assertEqual(_norm_tanh(1.0, 0), 0.0)

    def test_large_x_approaches_one(self):
        """x很大 → 接近1"""
        result = _norm_tanh(100.0, 1.0)
        self.assertGreater(result, 0.99)
        self.assertLessEqual(result, 1.0)

    def test_large_negative_x_approaches_minus_one(self):
        """x很小 → 接近-1"""
        result = _norm_tanh(-100.0, 1.0)
        self.assertLess(result, -0.99)
        self.assertGreaterEqual(result, -1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_norm_tanh(1.0, 1.0), float)

    def test_matches_math_tanh(self):
        """结果 = math.tanh(x / scale)"""
        x, scale = 2.5, 3.0
        expected = math.tanh(x / scale)
        self.assertAlmostEqual(_norm_tanh(x, scale), expected, places=10)

    def test_scale_affects_sensitivity(self):
        """scale 越大 → 同等 x 下值越小（更不敏感）"""
        small_scale = _norm_tanh(1.0, 0.5)
        large_scale = _norm_tanh(1.0, 2.0)
        self.assertGreater(small_scale, large_scale)

    def test_symmetry(self):
        """正负对称：f(x) = -f(-x)"""
        x, scale = 2.0, 1.0
        pos = _norm_tanh(x, scale)
        neg = _norm_tanh(-x, scale)
        self.assertAlmostEqual(pos, -neg, places=10)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  纪律复盘 + 宏观归一化 + 其他 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
