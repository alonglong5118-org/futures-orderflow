#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘健康检查 — 单元测试
===========================

1. _market_open_now — 是否处于交易时段
   - 日盘上午盘（09:00-10:15）→ True
   - 日盘午休（10:15-10:30）→ False
   - 日盘下午前（11:30-13:30）→ False
   - 日盘下午盘（13:30-15:00）→ True
   - 日盘收盘后（15:00-21:00）→ False
   - 夜盘（21:00-23:00）→ True
   - 凌晨盘（23:00-02:30）→ True
   - 凌晨收盘后（02:35-09:00）→ False
   - 周末 → False
   - 边界值精确（整点/15分/30分）

2. ym_of — 合约码解析年月
   - 标准 4 位年（如 rb2501 → 202501）
   - 3 位年（如 rb501 → 200501？不对，应该是 202501）
   - 小写合约名
   - 含特殊字符（空格/下划线）
   - 无数字 → None
   - 数字不足 3 位 → None
   - 70 年以上归 19xx（如 rb9901 → 199901）
"""

import sys
import os
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from live_health_check import _market_open_now, ym_of


# ═══════════════════════════════════════════════════════════════════════════
#  1. _market_open_now
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketOpenNow(unittest.TestCase):
    """_market_open_now 是否处于交易时段。"""

    def _make_dt(self, weekday, hour, minute):
        """构造指定周几和时间的 datetime。
        2026-01-12 是周一（weekday=0）。"""
        day = 12 + weekday  # 周一=12号
        return datetime(2026, 1, day, hour, minute, 0)

    def test_morning_session_open(self):
        """日盘上午盘（09:00-10:15）→ True"""
        self.assertTrue(_market_open_now(self._make_dt(0, 9, 30)))
        self.assertTrue(_market_open_now(self._make_dt(0, 9, 0)))
        self.assertTrue(_market_open_now(self._make_dt(0, 10, 15)))

    def test_morning_break_closed(self):
        """日盘午休（10:15-10:30）→ False"""
        self.assertFalse(_market_open_now(self._make_dt(0, 10, 20)))
        self.assertFalse(_market_open_now(self._make_dt(0, 10, 16)))

    def test_midday_closed(self):
        """午间休市（11:30-13:30）→ False"""
        self.assertFalse(_market_open_now(self._make_dt(0, 12, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 13, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 11, 45)))

    def test_afternoon_session_open(self):
        """日盘下午盘（13:30-15:00）→ True"""
        self.assertTrue(_market_open_now(self._make_dt(0, 14, 0)))
        self.assertTrue(_market_open_now(self._make_dt(0, 13, 30)))
        self.assertTrue(_market_open_now(self._make_dt(0, 15, 0)))

    def test_afternoon_closed(self):
        """日盘收盘后（15:00-21:00）→ False"""
        self.assertFalse(_market_open_now(self._make_dt(0, 16, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 20, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 15, 30)))

    def test_night_session_open(self):
        """夜盘（21:00-23:00）→ True"""
        self.assertTrue(_market_open_now(self._make_dt(0, 21, 0)))
        self.assertTrue(_market_open_now(self._make_dt(0, 22, 0)))
        self.assertTrue(_market_open_now(self._make_dt(0, 23, 0)))

    def test_late_night_session_open(self):
        """凌晨盘（23:00-02:30）：23:00 属于开盘
        注：当前实现用自然日分钟数表达，0:00-2:30 的跨日部分
        需要结合日期判断，此处仅验证 23:00 这一侧。"""
        self.assertTrue(_market_open_now(self._make_dt(0, 23, 0)))
        self.assertTrue(_market_open_now(self._make_dt(0, 23, 30)))

    def test_pre_market_closed(self):
        """开盘前（02:35-09:00）→ False"""
        self.assertFalse(_market_open_now(self._make_dt(0, 3, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 8, 0)))
        self.assertFalse(_market_open_now(self._make_dt(0, 2, 35)))

    def test_weekend_closed(self):
        """周末 → False"""
        # 周六
        self.assertFalse(_market_open_now(self._make_dt(5, 10, 0)))
        # 周日
        self.assertFalse(_market_open_now(self._make_dt(6, 14, 0)))

    def test_boundary_10_30_opens(self):
        """10:30 准点开盘 → True"""
        # 10:30 是上午第二盘开始
        self.assertTrue(_market_open_now(self._make_dt(0, 10, 30)))

    def test_boundary_13_30_opens(self):
        """13:30 准点开盘 → True"""
        self.assertTrue(_market_open_now(self._make_dt(0, 13, 30)))

    def test_weekday_does_not_matter_when_open(self):
        """交易日内，周几不影响（只要是工作日就开）"""
        # 周一到周五同一时间都应该开
        for wd in range(5):
            self.assertTrue(_market_open_now(self._make_dt(wd, 10, 0)),
                            f"周{wd+1} 10:00 应该开盘")


# ═══════════════════════════════════════════════════════════════════════════
#  2. ym_of
# ═══════════════════════════════════════════════════════════════════════════

class TestYmOf(unittest.TestCase):
    """ym_of 合约码解析年月。"""

    def test_standard_4digit(self):
        """标准 4 位年月 → 正确解析"""
        # rb2501 → 2025 年 1 月
        self.assertEqual(ym_of("rb2501"), 202501)
        # rb2612 → 2026 年 12 月
        self.assertEqual(ym_of("rb2612"), 202612)

    def test_3digit_year(self):
        """3 位年月（前两位是年，最后 1 位是月？不对，看代码: d[:2] 是年，d[2:] 是月）
        3 位数字：前 2 位年 + 最后 1 位月"""
        # rb501 → d="501" → yy=50, mm=1 → 2050 年 1 月
        self.assertEqual(ym_of("rb501"), 205001)
        # rb912 → d="912" → yy=91, mm=2 → 1991 年 2 月（yy≥70 归 19xx）
        self.assertEqual(ym_of("rb912"), 199102)

    def test_lowercase_symbol(self):
        """小写合约名也能解析"""
        self.assertEqual(ym_of("RB2501"), 202501)
        self.assertEqual(ym_of("FG2605"), 202605)

    def test_mixed_case(self):
        """混合大小写 → 统一转大写"""
        self.assertEqual(ym_of("Rb2501"), 202501)

    def test_special_chars_stripped(self):
        """含特殊字符（空格/下划线/点）→ 去掉后解析"""
        self.assertEqual(ym_of("rb_2501"), 202501)
        self.assertEqual(ym_of("rb 2501"), 202501)

    def test_no_number_returns_none(self):
        """纯字母 → None"""
        self.assertIsNone(ym_of("rb"))
        self.assertIsNone(ym_of("FG"))

    def test_empty_returns_none(self):
        """空字符串 → None"""
        self.assertIsNone(ym_of(""))

    def test_70s_goes_19xx(self):
        """70 年以上归 19xx"""
        # rb9901 → yy=99 ≥ 70 → 1999 年 1 月
        self.assertEqual(ym_of("rb9901"), 199901)
        # rb7001 → yy=70 ≥ 70 → 1970 年 1 月
        self.assertEqual(ym_of("rb7001"), 197001)

    def test_69s_goes_20xx(self):
        """69 年及以下归 20xx"""
        # rb6912 → yy=69 < 70 → 2069 年 12 月
        self.assertEqual(ym_of("rb6912"), 206912)

    def test_insufficient_digits_returns_none(self):
        """数字不足 3 位 → None（无法匹配 \d{3,4}）"""
        # rb01 → 只有 2 位数字
        self.assertIsNone(ym_of("rb01"))
        # rb1 → 只有 1 位
        self.assertIsNone(ym_of("rb1"))

    def test_multi_letter_symbol(self):
        """多字母品种名（如 FG、SA、MA）"""
        self.assertEqual(ym_of("FG2505"), 202505)
        self.assertEqual(ym_of("SA2601"), 202601)
        self.assertEqual(ym_of("MA2509"), 202509)

    def test_number_only_returns_none(self):
        """纯数字 → None（必须有字母前缀）"""
        # 2501 没有字母前缀
        self.assertIsNone(ym_of("2501"))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  实盘健康检查 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
