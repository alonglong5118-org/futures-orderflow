#!/usr/bin/env python3
"""
异动扫描 + 事件闸门 — 单元测试
====================================

1. anomaly_scan.compute — 异动评分
   - 空快照 → ok=False, total=0
   - 单品种 → 计算 pct/amp/score
   - 涨跌幅 = (close - open) / open × 100
   - 振幅 = (high - low) / open × 100
   - score = 0.7×|pct| + 0.3×amp
   - pre_close 优先（昨收计算涨跌幅）
   - pre_close 无效时回退到 open
   - 多品种 top_up 按 pct 降序
   - 多品种 top_down 按 pct 升序
   - top_n 限制数量
   - 数据缺失（无 close）→ 跳过
   - 零值价格 → 跳过
   - by_symbol 按品种索引
   - 每条记录 6 字段

2. _next_occurrence — 事件下一次发生时间
   - 每日事件（wd=None）：今日未到 → 今日
   - 每日事件：今日已过 → 明天
   - 每周事件（指定 weekday）：本周未到 → 本周
   - 每周事件：本周已过 → 下周
   - 每周事件恰好在当天但时刻已过 → 下周同一天
   - 时分秒归零（second=0, microsecond=0）
   - 返回 datetime

3. scale_factor — 闸门缩放系数
   - no_new_open=True → 0.0
   - reduce=True → 0.5
   - 都为 False → normal
   - normal 默认 1.0
   - normal 自定义 → 正常时返回自定义值
"""

import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import anomaly_scan as asc
from event_calendar import _next_occurrence, scale_factor

# ═══════════════════════════════════════════════════════════════════════════
#  1. anomaly_scan.compute
# ═══════════════════════════════════════════════════════════════════════════


class TestAnomalyScan(unittest.TestCase):
    """anomaly_scan.compute 异动评分。"""

    def test_empty_snaps_not_ok(self):
        """空快照 → ok=False, total=0"""
        r = asc.compute({})
        self.assertFalse(r["ok"])
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["top_up"], [])
        self.assertEqual(r["top_down"], [])

    def test_single_symbol_pct_amp(self):
        """单品种 → 计算 pct/amp/score"""
        snaps = {"FG": {"close": 910, "open": 900, "high": 915, "low": 898}}
        r = asc.compute(snaps)
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 1)
        rec = r["by_symbol"]["FG"]
        # pct = (910-900)/900*100 = 1.111...
        self.assertAlmostEqual(rec["pct"], 1.11, places=2)
        # amp = (915-898)/900*100 = 1.888...
        self.assertAlmostEqual(rec["amp"], 1.89, places=1)

    def test_pct_formula_open_basis(self):
        """涨跌幅 = (close - open) / open × 100"""
        # close > open → 正
        r = asc.compute({"X": {"close": 110, "open": 100, "high": 110, "low": 100}})
        self.assertAlmostEqual(r["by_symbol"]["X"]["pct"], 10.0, places=2)
        # close < open → 负
        r2 = asc.compute({"X": {"close": 90, "open": 100, "high": 100, "low": 90}})
        self.assertAlmostEqual(r2["by_symbol"]["X"]["pct"], -10.0, places=2)

    def test_amp_formula(self):
        """振幅 = (high - low) / open × 100"""
        r = asc.compute({"X": {"close": 105, "open": 100, "high": 110, "low": 95}})
        # amp = (110-95)/100*100 = 15
        self.assertAlmostEqual(r["by_symbol"]["X"]["amp"], 15.0, places=2)

    def test_score_weighted(self):
        """score = 0.7×|pct| + 0.3×amp"""
        # pct=10, amp=15 → 0.7*10 + 0.3*15 = 7 + 4.5 = 11.5
        r = asc.compute({"X": {"close": 110, "open": 100, "high": 115, "low": 100}})
        # pct=10, amp=(115-100)/100*100=15
        # score = 0.7*10 + 0.3*15 = 11.5
        self.assertAlmostEqual(r["by_symbol"]["X"]["score"], 11.5, places=2)

    def test_pre_close_used_when_available(self):
        """pre_close 优先（昨收计算涨跌幅）"""
        snaps = {"X": {"close": 110, "open": 105, "high": 112, "low": 104}}
        pre = {"X": 100}
        r = asc.compute(snaps, pre_close_map=pre)
        # pct = (110-100)/100*100 = 10
        self.assertAlmostEqual(r["by_symbol"]["X"]["pct"], 10.0, places=2)

    def test_pre_close_invalid_falls_back(self):
        """pre_close 无效时回退到 open"""
        snaps = {"X": {"close": 110, "open": 100, "high": 112, "low": 98}}
        pre = {"X": "invalid"}
        r = asc.compute(snaps, pre_close_map=pre)
        # 回退到 open → pct = (110-100)/100*100 = 10
        self.assertAlmostEqual(r["by_symbol"]["X"]["pct"], 10.0, places=2)

    def test_top_up_descending(self):
        """多品种 top_up 按 pct 降序"""
        snaps = {
            "A": {"close": 120, "open": 100, "high": 120, "low": 100},  # +20%
            "B": {"close": 110, "open": 100, "high": 110, "low": 100},  # +10%
            "C": {"close": 90, "open": 100, "high": 100, "low": 90},  # -10%
        }
        r = asc.compute(snaps)
        self.assertEqual(r["top_up"][0]["symbol"], "A")
        self.assertEqual(r["top_up"][1]["symbol"], "B")
        self.assertEqual(r["top_up"][2]["symbol"], "C")

    def test_top_down_ascending(self):
        """多品种 top_down 按 pct 升序"""
        snaps = {
            "A": {"close": 120, "open": 100, "high": 120, "low": 100},  # +20%
            "B": {"close": 110, "open": 100, "high": 110, "low": 100},  # +10%
            "C": {"close": 90, "open": 100, "high": 100, "low": 90},  # -10%
        }
        r = asc.compute(snaps)
        self.assertEqual(r["top_down"][0]["symbol"], "C")
        self.assertEqual(r["top_down"][1]["symbol"], "B")
        self.assertEqual(r["top_down"][2]["symbol"], "A")

    def test_top_n_limit(self):
        """top_n 限制数量"""
        snaps = {}
        for i in range(20):
            sym = chr(ord("A") + i)
            snaps[sym] = {"close": 100 + i, "open": 100, "high": 100 + i, "low": 100}
        r = asc.compute(snaps, top_n=5)
        self.assertEqual(len(r["top_up"]), 5)
        self.assertEqual(len(r["top_down"]), 5)

    def test_missing_data_skipped(self):
        """数据缺失（无 close）→ 跳过"""
        snaps = {
            "A": {"close": 110, "open": 100, "high": 110, "low": 100},
            "B": {"open": 100, "high": 110, "low": 100},  # 缺 close
        }
        r = asc.compute(snaps)
        self.assertEqual(r["total"], 1)
        self.assertIn("A", r["by_symbol"])
        self.assertNotIn("B", r["by_symbol"])

    def test_zero_price_skipped(self):
        """零值价格 → 跳过"""
        snaps = {"X": {"close": 0, "open": 100, "high": 100, "low": 90}}
        r = asc.compute(snaps)
        self.assertEqual(r["total"], 0)

    def test_by_symbol_indexed(self):
        """by_symbol 按品种索引"""
        snaps = {"FG": {"close": 910, "open": 900, "high": 915, "low": 898}}
        r = asc.compute(snaps)
        self.assertIn("FG", r["by_symbol"])
        self.assertEqual(r["by_symbol"]["FG"]["symbol"], "FG")

    def test_record_fields_complete(self):
        """每条记录 6 字段"""
        snaps = {"X": {"close": 105, "open": 100, "high": 108, "low": 98}}
        r = asc.compute(snaps)
        rec = r["by_symbol"]["X"]
        for key in ("symbol", "name", "close", "pct", "amp", "score"):
            self.assertIn(key, rec, f"missing key: {key}")

    def test_name_falls_back_to_symbol(self):
        """无 name → 用 symbol 兜底"""
        snaps = {"X": {"close": 105, "open": 100, "high": 108, "low": 98}}
        r = asc.compute(snaps)
        self.assertEqual(r["by_symbol"]["X"]["name"], "X")

    def test_score_rounded_two_decimals(self):
        """score 保留 2 位小数"""
        snaps = {"X": {"close": 101, "open": 100, "high": 102, "low": 99}}
        r = asc.compute(snaps)
        score = r["by_symbol"]["X"]["score"]
        self.assertEqual(score, round(score, 2))

    def test_negative_pct_uses_abs_in_score(self):
        """负 pct 取绝对值计入 score"""
        # 涨跌幅 -5%，振幅 5% → score = 0.7*5 + 0.3*5 = 5.0
        snaps = {"X": {"close": 95, "open": 100, "high": 100, "low": 95}}
        r = asc.compute(snaps)
        self.assertAlmostEqual(r["by_symbol"]["X"]["score"], 5.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _next_occurrence
# ═══════════════════════════════════════════════════════════════════════════


class TestNextOccurrence(unittest.TestCase):
    """_next_occurrence 事件下一次发生时间。"""

    def test_daily_not_yet_today(self):
        """每日事件：今日未到 → 今日"""
        now = datetime(2026, 8, 28, 8, 0, 0)  # 早上 8 点
        ev = {"hh": 9, "mm": 30, "wd": None}  # 9:30 每日
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 8, 28, 9, 30, 0))

    def test_daily_already_passed(self):
        """每日事件：今日已过 → 明天"""
        now = datetime(2026, 8, 28, 10, 0, 0)  # 早上 10 点
        ev = {"hh": 9, "mm": 30, "wd": None}  # 9:30 每日
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 8, 29, 9, 30, 0))

    def test_weekly_this_week_not_yet(self):
        """每周事件：本周未到 → 本周"""
        # 2026-08-28 是周五（weekday=4）
        now = datetime(2026, 8, 28, 8, 0, 0)
        ev = {"hh": 9, "mm": 0, "wd": 4}  # 每周五 9:00
        result = _next_occurrence(ev, now)
        self.assertEqual(result, datetime(2026, 8, 28, 9, 0, 0))

    def test_weekly_this_week_passed(self):
        """每周事件：本周已过 → 下周"""
        # 2026-08-28 是周五（weekday=4），已过 9:00
        now = datetime(2026, 8, 28, 10, 0, 0)
        ev = {"hh": 9, "mm": 0, "wd": 4}  # 每周五 9:00
        result = _next_occurrence(ev, now)
        # 下周五 = 2026-09-04
        self.assertEqual(result, datetime(2026, 9, 4, 9, 0, 0))

    def test_weekly_same_day_time_passed(self):
        """每周事件恰好在当天但时刻已过 → 下周同一天"""
        # 2026-08-28 周五 10:00，事件 9:00 周五
        now = datetime(2026, 8, 28, 10, 0, 0)
        ev = {"hh": 9, "mm": 0, "wd": 4}
        result = _next_occurrence(ev, now)
        self.assertEqual(result.weekday(), 4)  # 仍是周五
        self.assertGreater(result, now)

    def test_weekly_different_weekday_ahead(self):
        """每周事件：目标 weekday 在未来几天"""
        # 2026-08-28 周五（weekday=4）
        now = datetime(2026, 8, 28, 8, 0, 0)
        ev = {"hh": 10, "mm": 0, "wd": 6}  # 每周日 10:00
        result = _next_occurrence(ev, now)
        # 周日 = 8/30
        self.assertEqual(result, datetime(2026, 8, 30, 10, 0, 0))

    def test_seconds_and_microseconds_zero(self):
        """时分秒归零（second=0, microsecond=0）"""
        now = datetime(2026, 8, 28, 8, 15, 30, 123456)
        ev = {"hh": 9, "mm": 30, "wd": None}
        result = _next_occurrence(ev, now)
        self.assertEqual(result.second, 0)
        self.assertEqual(result.microsecond, 0)

    def test_returns_datetime(self):
        """返回 datetime"""
        now = datetime(2026, 8, 28, 8, 0, 0)
        ev = {"hh": 9, "mm": 0, "wd": None}
        self.assertIsInstance(_next_occurrence(ev, now), datetime)


# ═══════════════════════════════════════════════════════════════════════════
#  3. scale_factor
# ═══════════════════════════════════════════════════════════════════════════


class TestScaleFactor(unittest.TestCase):
    """scale_factor 闸门缩放系数。"""

    def test_no_new_open_returns_zero(self):
        """no_new_open=True → 0.0"""
        g = {"no_new_open": True, "reduce": True}
        self.assertEqual(scale_factor(g), 0.0)

    def test_reduce_returns_half(self):
        """reduce=True → 0.5"""
        g = {"no_new_open": False, "reduce": True}
        self.assertEqual(scale_factor(g), 0.5)

    def test_all_clear_returns_normal(self):
        """都为 False → normal（默认 1.0）"""
        g = {"no_new_open": False, "reduce": False}
        self.assertEqual(scale_factor(g), 1.0)

    def test_custom_normal(self):
        """normal 自定义 → 正常时返回自定义值"""
        g = {"no_new_open": False, "reduce": False}
        self.assertEqual(scale_factor(g, normal=0.8), 0.8)

    def test_empty_gate_returns_normal(self):
        """空 dict → 返回 normal"""
        self.assertEqual(scale_factor({}), 1.0)

    def test_no_new_open_overrides_reduce(self):
        """no_new_open 优先级高于 reduce → 返回 0"""
        g = {"no_new_open": True, "reduce": False}
        self.assertEqual(scale_factor(g), 0.0)

    def test_reduce_with_custom_normal(self):
        """reduce 时固定返回 0.5（不受 normal 影响）"""
        g = {"reduce": True}
        self.assertEqual(scale_factor(g, normal=0.8), 0.5)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  异动扫描 + 事件闸门 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
