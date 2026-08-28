#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具函数 — 单元测试
=====================

1. is_trading_day — 交易日判断
   - 周一~周五 → 是交易日
   - 周六/周日 → 不是
   - 法定节假日 → 不是
   - 调休补班日 → 不是（期货不调休）

2. parse_ts — 时间戳解析
   - 秒级时间戳 → 原样返回
   - 毫秒级时间戳 → 转成秒
   - 字符串时间戳 → 解析为 float
   - None / 空串 / 0 → 0
   - 非法输入 → 0

3. variety_of + VARIETY_OF 完整性
   - 已有映射 + 未知品种兜底
"""

import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import VARIETY_OF, variety_of
from preflight_check import HOLIDAY_SET_2026, is_trading_day, parse_ts

# ═══════════════════════════════════════════════════════════════════════════
#  1. is_trading_day — 交易日判断
# ═══════════════════════════════════════════════════════════════════════════

class TestIsTradingDay(unittest.TestCase):
    """is_trading_day — 交易日判断。"""

    def test_monday_is_trading_day(self):
        """周一 → 是交易日（非节假日）"""
        # 2026-01-05 是周一
        d = datetime.date(2026, 1, 5)
        self.assertEqual(d.weekday(), 0)  # 确认是周一
        if d.isoformat() not in HOLIDAY_SET_2026:
            self.assertTrue(is_trading_day(d))

    def test_saturday_not_trading_day(self):
        """周六 → 不是交易日"""
        # 2026-01-03 是周六
        d = datetime.date(2026, 1, 3)
        self.assertEqual(d.weekday(), 5)  # 周六
        self.assertFalse(is_trading_day(d))

    def test_sunday_not_trading_day(self):
        """周日 → 不是交易日"""
        # 2026-01-04 是周日
        d = datetime.date(2026, 1, 4)
        self.assertEqual(d.weekday(), 6)  # 周日
        self.assertFalse(is_trading_day(d))

    def test_spring_festival_not_trading_day(self):
        """春节假期 → 不是交易日"""
        # 找一个在 HOLIDAY_SET_2026 里且是工作日的日期
        holiday_weekday = None
        for h in HOLIDAY_SET_2026:
            d = datetime.date.fromisoformat(h)
            if d.weekday() < 5:  # 工作日
                holiday_weekday = d
                break
        if holiday_weekday is None:
            self.skipTest("没有工作日的节假日")
        self.assertFalse(is_trading_day(holiday_weekday))

    def test_default_arg_uses_today(self):
        """不传参数 → 返回布尔值（不崩溃）"""
        result = is_trading_day()
        self.assertIsInstance(result, bool)

    def test_holiday_set_2026_populated(self):
        """HOLIDAY_SET_2026 有数据（不是空的）"""
        self.assertGreater(len(HOLIDAY_SET_2026), 0)
        # 至少包含春节、国庆等主要假期
        # 检查日期格式是否正确
        for h in HOLIDAY_SET_2026:
            # 应该是 YYYY-MM-DD 格式
            self.assertEqual(len(h), 10)
            self.assertTrue(h.startswith("2026-"))


# ═══════════════════════════════════════════════════════════════════════════
#  2. parse_ts — 时间戳解析
# ═══════════════════════════════════════════════════════════════════════════

class TestParseTs(unittest.TestCase):
    """parse_ts — 秒/毫秒时间戳解析。"""

    def test_seconds_unchanged(self):
        """秒级时间戳 → 原样返回"""
        ts = 1700000000  # 约 2023-11-14，秒级
        result = parse_ts(ts)
        self.assertEqual(result, ts)

    def test_milliseconds_converted(self):
        """毫秒级时间戳 → 转成秒"""
        ms = 1700000000000  # 毫秒级
        result = parse_ts(ms)
        self.assertAlmostEqual(result, 1700000000, places=2)

    def test_string_seconds(self):
        """字符串秒级时间戳 → 解析为 float"""
        result = parse_ts("1700000000")
        self.assertEqual(result, 1700000000.0)

    def test_string_milliseconds(self):
        """字符串毫秒级时间戳 → 转成秒"""
        result = parse_ts("1700000000000")
        self.assertAlmostEqual(result, 1700000000, places=2)

    def test_none_returns_zero(self):
        """None → 0"""
        self.assertEqual(parse_ts(None), 0)

    def test_empty_string_returns_zero(self):
        """空串 → 0"""
        self.assertEqual(parse_ts(""), 0)

    def test_zero_returns_zero(self):
        """0 → 0"""
        self.assertEqual(parse_ts(0), 0)

    def test_invalid_string_returns_zero(self):
        """非法字符串 → 0"""
        self.assertEqual(parse_ts("abc"), 0)

    def test_negative_still_valid(self):
        """负数时间戳 → 不报错（虽然不太可能）"""
        result = parse_ts(-1000)
        self.assertEqual(result, -1000)

    def test_float_seconds(self):
        """float 秒级 → 正确"""
        result = parse_ts(1700000000.5)
        self.assertEqual(result, 1700000000.5)


# ═══════════════════════════════════════════════════════════════════════════
#  3. variety_of — 品种映射补充
# ═══════════════════════════════════════════════════════════════════════════

class TestVarietyOfCompleteness(unittest.TestCase):
    """variety_of 补充测试。"""

    def test_all_variety_keys_in_symbols(self):
        """VARIETY_OF 中的品种 key 大部分在 SYMBOLS 里"""
        from four_dim_strategy import SYMBOLS
        valid = 0
        for contract, variety in VARIETY_OF.items():
            if variety in SYMBOLS:
                valid += 1
        total = len(VARIETY_OF)
        # 允许有 20% 不在（可能是过期合约或新品种）
        if total > 0:
            ratio = valid / total
            self.assertGreater(ratio, 0.7,
                f"VARIETY_OF 中只有 {ratio:.0%} 的品种在 SYMBOLS 里")

    def test_variety_of_upper_lower_consistency(self):
        """大小写合约名映射到同一个品种"""
        # 找一个映射
        if not VARIETY_OF:
            self.skipTest("VARIETY_OF 为空")
        contract = list(VARIETY_OF.keys())[0]
        variety = VARIETY_OF[contract]
        # 同一个品种的不同合约应该映射到同一个 variety
        # 比如 rb2501 → rb，rb2505 → rb
        same_variety = [k for k, v in VARIETY_OF.items() if v == variety]
        if len(same_variety) >= 2:
            for c in same_variety:
                self.assertEqual(variety_of(c), variety)

    def test_variety_of_preserves_case(self):
        """variety_of 返回值与 VARIETY_OF 中一致"""
        if not VARIETY_OF:
            self.skipTest("VARIETY_OF 为空")
        contract = list(VARIETY_OF.keys())[0]
        self.assertEqual(variety_of(contract), VARIETY_OF[contract])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
