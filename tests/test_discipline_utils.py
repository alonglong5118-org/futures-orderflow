#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纪律复盘工具函数 — 单元测试
=================================

1. _duration — 持仓时长格式化
   - 不足 1 小时 → "X分钟"
   - 整小时 → "X小时"
   - 小时+分钟 → "X小时Y分"
   - 0 分钟 → "0分钟"
   - 结束早于开始 → ""
   - 格式错误 → ""
   - 跨天计算

2. _friday_of — 所在周的周五
   - 周一 → 当周周五
   - 周三 → 当周周五
   - 周五 → 当天
   - 周六 → 当周周五（已过）
   - 周日 → 当周周五（已过）
   - 接受 date 和 datetime

3. _is_last_trading_day — 当月最后交易日
   - 月末工作日 → True
   - 月末周六 → 前一个周五 True
   - 月末周日 → 前一个周五 True
   - 月中 → False
   - 2 月闰年/平年
"""

import os
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from discipline_review import _duration, _friday_of, _is_last_trading_day

# ═══════════════════════════════════════════════════════════════════════════
#  1. _duration
# ═══════════════════════════════════════════════════════════════════════════


class TestDuration(unittest.TestCase):
    """_duration 持仓时长格式化。"""

    def test_less_than_one_hour(self):
        """不足 1 小时 → "X分钟" """
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-15 09:45:00"), "15分钟")

    def test_exact_hours(self):
        """整小时 → "X小时" """
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-15 11:30:00"), "2小时")

    def test_hours_and_minutes(self):
        """小时+分钟 → "X小时Y分" """
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-15 12:45:00"), "3小时15分")

    def test_zero_minutes(self):
        """0 分钟 → "0分钟" """
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-15 09:30:00"), "0分钟")

    def test_one_minute(self):
        """1 分钟 → "1分钟" """
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-15 09:31:00"), "1分钟")

    def test_negative_duration_empty(self):
        """结束早于开始 → "" """
        self.assertEqual(_duration("2026-01-15 10:30:00", "2026-01-15 09:30:00"), "")

    def test_bad_format_empty(self):
        """格式错误 → "" """
        self.assertEqual(_duration("bad", "2026-01-15 09:30:00"), "")
        self.assertEqual(_duration("2026-01-15 09:30:00", "bad"), "")

    def test_overnight_duration(self):
        """跨天计算"""
        self.assertEqual(_duration("2026-01-15 22:00:00", "2026-01-16 10:30:00"), "12小时30分")

    def test_multi_day_duration(self):
        """多日持仓"""
        self.assertEqual(_duration("2026-01-15 09:30:00", "2026-01-17 15:00:00"), "53小时30分")

    def test_fifty_nine_minutes(self):
        """59 分钟 → "59分钟"（边界）"""
        self.assertEqual(_duration("2026-01-15 09:00:00", "2026-01-15 09:59:00"), "59分钟")

    def test_sixty_minutes_is_one_hour(self):
        """60 分钟 → "1小时"（边界）"""
        self.assertEqual(_duration("2026-01-15 09:00:00", "2026-01-15 10:00:00"), "1小时")


# ═══════════════════════════════════════════════════════════════════════════
#  2. _friday_of
# ═══════════════════════════════════════════════════════════════════════════


class TestFridayOf(unittest.TestCase):
    """_friday_of 所在周的周五。"""

    def test_monday_to_friday(self):
        """周一 → 当周周五"""
        # 2026-01-12 是周一
        d = date(2026, 1, 12)
        result = _friday_of(d)
        # 当周周五 = 1月16日
        self.assertEqual(result, date(2026, 1, 16))

    def test_wednesday_to_friday(self):
        """周三 → 当周周五"""
        d = date(2026, 1, 14)  # 周三
        result = _friday_of(d)
        self.assertEqual(result, date(2026, 1, 16))

    def test_friday_is_same_day(self):
        """周五 → 当天"""
        d = date(2026, 1, 16)  # 周五
        result = _friday_of(d)
        self.assertEqual(result, d)

    def test_saturday_to_friday(self):
        """周六 → 当周周五（已过，往前推）"""
        d = date(2026, 1, 17)  # 周六
        result = _friday_of(d)
        # 4 - 5 = -1 → 往前 1 天 = 周五
        self.assertEqual(result, date(2026, 1, 16))

    def test_sunday_to_friday(self):
        """周日 → 当周周五（已过，往前推）"""
        d = date(2026, 1, 18)  # 周日
        result = _friday_of(d)
        # 4 - 6 = -2 → 往前 2 天 = 周五
        self.assertEqual(result, date(2026, 1, 16))

    def test_accepts_datetime(self):
        """接受 datetime 输入"""
        dt = datetime(2026, 1, 13, 10, 30, 0)  # 周二
        result = _friday_of(dt)
        self.assertEqual(result, date(2026, 1, 16))
        self.assertIsInstance(result, date)

    def test_accepts_date(self):
        """接受 date 输入"""
        d = date(2026, 1, 15)  # 周四
        result = _friday_of(d)
        self.assertEqual(result, date(2026, 1, 16))

    def test_monday_first_week(self):
        """年初第一周周一 → 当周周五"""
        # 2026-01-05 是周一
        d = date(2026, 1, 5)
        result = _friday_of(d)
        self.assertEqual(result, date(2026, 1, 9))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _is_last_trading_day
# ═══════════════════════════════════════════════════════════════════════════


class TestIsLastTradingDay(unittest.TestCase):
    """_is_last_trading_day 当月最后交易日。"""

    def test_last_day_weekday_true(self):
        """月末是工作日 → True"""
        # 2026-01-30 是周五，也是 1 月最后一个工作日
        # 1 月有 31 天，31 号是周六 → 最后交易日 = 30 号（周五）
        d = date(2026, 1, 30)
        self.assertTrue(_is_last_trading_day(d))

    def test_last_day_saturday_friday_true(self):
        """月末周六 → 前一个周五 True"""
        # 2026-01-31 是周六 → 最后交易日 = 30 号（周五）
        d_friday = date(2026, 1, 30)
        d_saturday = date(2026, 1, 31)
        self.assertTrue(_is_last_trading_day(d_friday))
        self.assertFalse(_is_last_trading_day(d_saturday))

    def test_last_day_sunday_friday_true(self):
        """月末周日 → 前一个周五 True"""
        # 2026-03-31 是周二 → 不是周末，最后交易日就是 31 号
        # 找一个月末是周日的月份
        # 2026-05-31 是周日 → 最后交易日 = 29 号（周五）
        d_friday = date(2026, 5, 29)
        d_sunday = date(2026, 5, 31)
        self.assertTrue(_is_last_trading_day(d_friday))
        self.assertFalse(_is_last_trading_day(d_sunday))

    def test_mid_month_false(self):
        """月中 → False"""
        d = date(2026, 1, 15)
        self.assertFalse(_is_last_trading_day(d))

    def test_february_leap_year(self):
        """2 月闰年（2028 是闰年）"""
        # 2028-02-29 是周二 → 最后交易日 = 29 号
        d = date(2028, 2, 29)
        self.assertTrue(_is_last_trading_day(d))

    def test_february_non_leap(self):
        """2 月平年"""
        # 2026-02-28 是周六 → 最后交易日 = 27 号（周五）
        d = date(2026, 2, 27)
        self.assertTrue(_is_last_trading_day(d))
        # 28 号是周六，不是最后交易日
        self.assertFalse(_is_last_trading_day(date(2026, 2, 28)))

    def test_december_year_end(self):
        """12 月末（跨年）"""
        # 2026-12-31 是周四 → 最后交易日 = 31 号
        d = date(2026, 12, 31)
        self.assertTrue(_is_last_trading_day(d))

    def test_day_before_last_false(self):
        """倒数第二个工作日 → False"""
        # 2026-01-29 是周四，倒数第二个工作日
        d = date(2026, 1, 29)
        self.assertFalse(_is_last_trading_day(d))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  纪律复盘工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
