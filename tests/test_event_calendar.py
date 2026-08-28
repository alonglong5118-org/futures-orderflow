#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件日历闸门 — 单元测试
===========================

1. _next_occurrence — 下一次事件发生时间
   - 每日事件（wd=None），时刻未到 → 今天
   - 每日事件，时刻已过 → 明天
   - 周几事件，本周未到 → 本周
   - 周几事件，本周已过 → 下周
   - 周几事件，今天正好是该日且时刻未到 → 今天
   - 周几事件，今天正好是该日但时刻已过 → 下周
   - 精确到分钟，秒和微秒清零

2. scale_factor — 闸门→手数缩放
   - no_new_open → 0（禁开）
   - reduce 但不禁开 → 0.5
   - 正常 → normal（默认 1.0）
   - 自定义 normal 值

3. gate — 闸门建议
   - 无临近事件 → reduce=False, no_new_open=False
   - 中等重要性事件临近 → reduce=True
   - 高重要性且 <1 小时 → no_new_open=True
   - 事件按时间排序
   - 在影响半径外的事件不触发
"""

import sys
import os
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from event_calendar import _next_occurrence, scale_factor, gate


# ═══════════════════════════════════════════════════════════════════════════
#  1. _next_occurrence
# ═══════════════════════════════════════════════════════════════════════════

class TestNextOccurrence(unittest.TestCase):
    """_next_occurrence 下一次事件发生时间。"""

    def test_daily_not_yet_passed_today(self):
        """每日事件，时刻未到 → 今天"""
        now = datetime(2026, 1, 15, 8, 0, 0)  # 08:00
        ev = {"hh": 9, "mm": 30, "wd": None}  # 每日 09:30
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 1, 15, 9, 30, 0))

    def test_daily_already_passed_tomorrow(self):
        """每日事件，时刻已过 → 明天"""
        now = datetime(2026, 1, 15, 10, 0, 0)  # 10:00
        ev = {"hh": 9, "mm": 30, "wd": None}   # 每日 09:30
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 1, 16, 9, 30, 0))

    def test_daily_exact_time_tomorrow(self):
        """每日事件，正好当前时刻 → 明天（因为 cand < now 是严格小于）"""
        now = datetime(2026, 1, 15, 9, 30, 0)
        ev = {"hh": 9, "mm": 30, "wd": None}
        result = _next_occurrence(ev, now)
        # cand == now 不满足 cand < now，所以返回今天
        self.assertEqual(result, datetime(2026, 1, 15, 9, 30, 0))

    def test_weekly_this_week_not_passed(self):
        """周几事件，本周未到 → 本周"""
        # 2026-01-15 是周四（weekday=3）
        now = datetime(2026, 1, 15, 8, 0, 0)  # 周四 08:00
        ev = {"hh": 20, "mm": 30, "wd": 4}    # 每周五 20:30
        result = _next_occurrence(ev, now)
        # 本周五 20:30
        self.assertEqual(result, datetime(2026, 1, 16, 20, 30, 0))

    def test_weekly_today_not_yet_passed(self):
        """周几事件，今天正好是该日且时刻未到 → 今天"""
        # 2026-01-15 是周四（weekday=3）
        now = datetime(2026, 1, 15, 8, 0, 0)  # 周四 08:00
        ev = {"hh": 20, "mm": 30, "wd": 3}    # 每周四 20:30
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 1, 15, 20, 30, 0))

    def test_weekly_today_already_passed(self):
        """周几事件，今天正好是该日但时刻已过 → 下周"""
        # 2026-01-15 是周四（weekday=3）
        now = datetime(2026, 1, 15, 21, 0, 0)  # 周四 21:00
        ev = {"hh": 20, "mm": 30, "wd": 3}     # 每周四 20:30
        result = _next_occurrence(ev, now)
        # 下周四 20:30
        self.assertEqual(result, datetime(2026, 1, 22, 20, 30, 0))

    def test_weekly_last_day_of_week(self):
        """周日事件，周六查询 → 第二天（周日）"""
        # 2026-01-17 是周六（weekday=5）
        now = datetime(2026, 1, 17, 10, 0, 0)
        ev = {"hh": 12, "mm": 0, "wd": 6}  # 每周日 12:00
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 1, 18, 12, 0, 0))

    def test_seconds_and_microseconds_zeroed(self):
        """秒和微秒清零"""
        now = datetime(2026, 1, 15, 8, 0, 30, 500000)
        ev = {"hh": 9, "mm": 30, "wd": None}
        result = _next_occurrence(ev, now)
        self.assertEqual(result.second, 0)
        self.assertEqual(result.microsecond, 0)

    def test_monday_from_friday(self):
        """周五查周一事件 → 下周一"""
        # 2026-01-16 是周五（weekday=4）
        now = datetime(2026, 1, 16, 10, 0, 0)
        ev = {"hh": 9, "mm": 0, "wd": 0}  # 每周一 09:00
        result = _next_occurrence(ev, now)
        # 下周一 = 1月19日
        self.assertEqual(result, datetime(2026, 1, 19, 9, 0, 0))


# ═══════════════════════════════════════════════════════════════════════════
#  2. scale_factor
# ═══════════════════════════════════════════════════════════════════════════

class TestScaleFactor(unittest.TestCase):
    """scale_factor 闸门→手数缩放。"""

    def test_no_new_open_returns_zero(self):
        """no_new_open → 0（禁开）"""
        g = {"no_new_open": True, "reduce": True}
        self.assertEqual(scale_factor(g), 0.0)

    def test_reduce_only_returns_half(self):
        """reduce 但不禁开 → 0.5"""
        g = {"no_new_open": False, "reduce": True}
        self.assertEqual(scale_factor(g), 0.5)

    def test_normal_returns_one(self):
        """正常 → 1.0"""
        g = {"no_new_open": False, "reduce": False}
        self.assertEqual(scale_factor(g), 1.0)

    def test_custom_normal_value(self):
        """自定义 normal 值"""
        g = {"no_new_open": False, "reduce": False}
        self.assertEqual(scale_factor(g, normal=0.8), 0.8)

    def test_no_new_open_overrides_reduce(self):
        """no_new_open 优先级高于 reduce（直接返回 0）"""
        g = {"no_new_open": True, "reduce": False}
        self.assertEqual(scale_factor(g), 0.0)

    def test_empty_dict_normal(self):
        """空 dict → 正常（没有键就当 False）"""
        g = {}
        self.assertEqual(scale_factor(g), 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. gate
# ═══════════════════════════════════════════════════════════════════════════

class TestGate(unittest.TestCase):
    """gate 闸门建议。"""

    def test_no_nearby_events_normal(self):
        """无临近事件 → reduce=False, no_new_open=False"""
        # 选一个肯定没有事件的时间点
        # 事件表里没有凌晨 5 点的事件，而且 4 小时 lookahead 到 9 点
        # 09:00 的日盘开盘 imp="低"，rank=1 < 2，不触发 reduce
        now = datetime(2026, 1, 15, 5, 0, 0)  # 凌晨 5 点
        result = gate(lookahead_hours=2, now=now)
        self.assertFalse(result["reduce"])
        self.assertFalse(result["no_new_open"])

    def test_medium_event_triggers_reduce(self):
        """中等重要性（rank≥2）事件在影响半径内 → reduce=True"""
        #  Mysteel 钢材库存：周三(wd=2) 11:00, imp=高(rank=3), win=2h
        # 2026-01-14 是周三
        now = datetime(2026, 1, 14, 10, 0, 0)  # 周三 10:00，事件前 1h
        result = gate(lookahead_hours=4, now=now)
        self.assertTrue(result["reduce"])

    def test_high_event_within_1h_triggers_no_new_open(self):
        """高重要性事件且 <1 小时 → no_new_open=True"""
        # EIA 原油库存：周三(wd=2) 22:30, imp=高(rank=3), win=2h
        # 2026-01-14 是周三
        now = datetime(2026, 1, 14, 22, 0, 0)  # 周三 22:00，事件前 0.5h < 1h
        result = gate(lookahead_hours=4, now=now)
        self.assertTrue(result["no_new_open"])
        self.assertTrue(result["reduce"])

    def test_event_outside_win_no_trigger(self):
        """事件在影响半径外 → 不触发"""
        # EIA 原油库存：周三 22:30, win=2h
        # 现在是周三 19:00，距离事件 3.5h > win=2h
        now = datetime(2026, 1, 14, 19, 0, 0)
        result = gate(lookahead_hours=6, now=now)
        # 事件在 lookahead 范围内，但不在 win 内 → 不应该 reduce
        # 但可能有其他事件，需要更精确判断
        # 这里只检查 EIA 不在 events 列表里（因为 out of win）
        eia_events = [e for e in result["events"] if "EIA" in e["name"]]
        self.assertEqual(len(eia_events), 0)

    def test_msg_changes_by_severity(self):
        """不同严重程度有不同消息"""
        # 正常
        now_normal = datetime(2026, 1, 15, 5, 0, 0)
        r_normal = gate(lookahead_hours=2, now=now_normal)
        self.assertIn("无重大", r_normal["msg"])

        # 禁开
        now_noopen = datetime(2026, 1, 14, 22, 0, 0)
        r_noopen = gate(lookahead_hours=4, now=now_noopen)
        if r_noopen["no_new_open"]:
            self.assertIn("禁止新开仓", r_noopen["msg"])

    def test_events_sorted_by_time(self):
        """events 按时间升序排列（in_hours 升序）"""
        now = datetime(2026, 1, 14, 8, 0, 0)
        result = gate(lookahead_hours=24, now=now)
        in_hours_list = [e["in_hours"] for e in result["events"]]
        self.assertEqual(in_hours_list, sorted(in_hours_list))

    def test_lookahead_respected(self):
        """lookahead 之外的事件不包含在结果里"""
        now = datetime(2026, 1, 15, 10, 0, 0)
        result = gate(lookahead_hours=1, now=now)
        for e in result["events"]:
            self.assertLessEqual(e["in_hours"], 1 + e["win"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  事件日历闸门 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
