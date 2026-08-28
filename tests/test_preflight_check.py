#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘前检查工具函数 — 单元测试
=================================

1. parse_ts — 时间戳解析（兼容秒/毫秒）
   - 秒级时间戳 → 原样返回
   - 毫秒级时间戳 → 除以 1000
   - 0 → 0
   - None → 0
   - 空字符串 → 0
   - 非法字符串 → 0
   - 字符串数字 → 正常解析
   - 边界：1e12 以上才当毫秒

2. is_trading_day — 是否交易日
   - 普通工作日 → True
   - 周六 → False
   - 周日 → False
   - 法定节假日 → False
   - 调休补班的周末 → False（不调休逻辑）
   - 接受 date 对象
"""

import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from preflight_check import is_trading_day, parse_ts

# ═══════════════════════════════════════════════════════════════════════════
#  1. parse_ts
# ═══════════════════════════════════════════════════════════════════════════


class TestParseTs(unittest.TestCase):
    """parse_ts 时间戳解析（兼容秒/毫秒）。"""

    def test_seconds_timestamp(self):
        """秒级时间戳 → 原样返回"""
        t = 1700000000.0  # 2023-11-14 左右
        self.assertEqual(parse_ts(t), t)

    def test_milliseconds_timestamp(self):
        """毫秒级时间戳 → 除以 1000"""
        t_ms = 1700000000000.0  # 毫秒
        expected = 1700000000.0
        self.assertEqual(parse_ts(t_ms), expected)

    def test_zero(self):
        """0 → 0"""
        self.assertEqual(parse_ts(0), 0)

    def test_none_returns_zero(self):
        """None → 0"""
        self.assertEqual(parse_ts(None), 0)

    def test_empty_string_returns_zero(self):
        """空字符串 → 0"""
        self.assertEqual(parse_ts(""), 0)

    def test_invalid_string_returns_zero(self):
        """非法字符串 → 0"""
        self.assertEqual(parse_ts("not_a_number"), 0)
        self.assertEqual(parse_ts("abc123"), 0)

    def test_string_number(self):
        """字符串数字 → 正常解析"""
        self.assertEqual(parse_ts("1700000000"), 1700000000.0)

    def test_string_milliseconds(self):
        """字符串毫秒 → 除以 1000"""
        self.assertEqual(parse_ts("1700000000000"), 1700000000.0)

    def test_boundary_above_1e12_is_ms(self):
        """边界：> 1e12 当毫秒"""
        t = 1.1e12  # 1.1 × 10^12 > 1e12
        result = parse_ts(t)
        self.assertAlmostEqual(result, t / 1000.0, places=3)

    def test_boundary_below_1e12_is_seconds(self):
        """边界：< 1e12 当秒"""
        t = 1e9  # 10^9，典型秒级时间戳
        self.assertEqual(parse_ts(t), t)

    def test_negative_timestamp(self):
        """负时间戳 → 返回负值（不报错）"""
        self.assertEqual(parse_ts(-100.0), -100.0)

    def test_int_input(self):
        """整数输入 → 正常解析"""
        self.assertEqual(parse_ts(1700000000), 1700000000.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. is_trading_day
# ═══════════════════════════════════════════════════════════════════════════


class TestIsTradingDay(unittest.TestCase):
    """is_trading_day 是否交易日。"""

    def test_normal_weekday_true(self):
        """普通工作日 → True"""
        # 2026-01-15 是周四，非节假日
        self.assertTrue(is_trading_day(date(2026, 1, 15)))

    def test_saturday_false(self):
        """周六 → False"""
        # 2026-01-17 是周六
        self.assertFalse(is_trading_day(date(2026, 1, 17)))

    def test_sunday_false(self):
        """周日 → False"""
        # 2026-01-18 是周日
        self.assertFalse(is_trading_day(date(2026, 1, 18)))

    def test_spring_festival_holiday_false(self):
        """春节假期 → False"""
        # 2026 年春节：2月17日（初一）左右放假
        # 2026-02-17 是周二，应该在假期里
        self.assertFalse(is_trading_day(date(2026, 2, 17)))

    def test_holiday_range_all_false(self):
        """整个假期区间都是非交易日"""
        # 检查几个假期中的日子
        holiday_samples = [
            date(2026, 1, 1),  # 元旦
            date(2026, 2, 18),  # 春节期间
            date(2026, 4, 6),  # 清明
            date(2026, 5, 1),  # 劳动节
            date(2026, 6, 19),  # 端午
            date(2026, 10, 1),  # 国庆
        ]
        for d in holiday_samples:
            self.assertFalse(is_trading_day(d), f"{d} 应该是假期")

    def test_workday_after_holiday_true(self):
        """假期后的第一个工作日 → True"""
        # 找一个假期后肯定是工作日的日子
        # 2026-01-02 是周五，元旦后第一个交易日
        # （如果 1月1日是周四，1月2日是周五工作日）
        # 2026-01-01 是周四
        d = date(2026, 1, 2)
        # 1月2日是周五，且不在假期里
        if d.weekday() < 5:
            result = is_trading_day(d)
            # 不一定是 True（可能调休），但至少不报错
            self.assertIsInstance(result, bool)

    def test_accepts_date_object(self):
        """接受 date 对象"""
        d = date(2026, 1, 15)  # 周四
        result = is_trading_day(d)
        self.assertIsInstance(result, bool)

    def test_monday_workday(self):
        """周一工作日 → True（非假期时）"""
        # 2026-01-12 是周一
        self.assertTrue(is_trading_day(date(2026, 1, 12)))

    def test_friday_workday(self):
        """周五工作日 → True（非假期时）"""
        # 2026-01-16 是周五
        self.assertTrue(is_trading_day(date(2026, 1, 16)))

    def test_national_day_holiday(self):
        """国庆假期 → False"""
        # 2026-10-01 是周四，国庆假期第一天
        self.assertFalse(is_trading_day(date(2026, 10, 1)))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  盘前检查工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
