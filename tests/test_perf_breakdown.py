#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绩效拆解工具 — 单元测试
===========================

1. _hold_minutes — 持仓时长（分钟）
   - 正常进出 → 返回分钟数
   - 平仓早于开仓 → 0（不返回负数）
   - 时间格式错误 → None
   - 缺少字段 → None

2. _hold_bucket — 持仓时长分桶
   - None → "未知"
   - <30 分 → "<30分(抢反弹)"
   - 30~120 分 → "30分~2时"
   - 120~480 分 → "2~8时(日内)"
   - 480~1440 分 → "8~24时"
   - ≥1440 分 → ">1天(过夜)"
   - 边界值精确

3. _weekday — 交易日是周几
   - 周一 → "周一"
   - 周日 → "周日"
   - 格式错误 → "未知"
   - 空字符串 → "未知"

4. _stat — 交易统计指标
   - 空列表 → None
   - 全赚 → 胜率 100%, pf=99
   - 全亏 → 胜率 0%, pf=0
   - 有赚有亏 → 胜率/pnl/盈亏比/期望R 正确
   - best/worst 取 R 的极值
   - reliable 标志（n ≥ _MIN_N）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from perf_breakdown import _MIN_N, _hold_bucket, _hold_minutes, _stat, _weekday

# ═══════════════════════════════════════════════════════════════════════════
#  1. _hold_minutes
# ═══════════════════════════════════════════════════════════════════════════

class TestHoldMinutes(unittest.TestCase):
    """_hold_minutes 持仓时长（分钟）。"""

    def test_normal_hold(self):
        """正常进出 → 返回分钟数"""
        t = {
            "time": "2026-01-15 09:30:00",
            "exit_time": "2026-01-15 10:30:00",
        }
        self.assertEqual(_hold_minutes(t), 60.0)

    def test_hold_partial_minute(self):
        """不到 1 分钟也能正确计算"""
        t = {
            "time": "2026-01-15 09:30:00",
            "exit_time": "2026-01-15 09:30:30",
        }
        self.assertAlmostEqual(_hold_minutes(t), 0.5, places=1)

    def test_exit_before_entry_returns_zero(self):
        """平仓早于开仓 → 0（不返回负数）"""
        t = {
            "time": "2026-01-15 10:30:00",
            "exit_time": "2026-01-15 09:30:00",
        }
        self.assertEqual(_hold_minutes(t), 0.0)

    def test_same_time_zero(self):
        """开仓平仓同时 → 0"""
        t = {
            "time": "2026-01-15 09:30:00",
            "exit_time": "2026-01-15 09:30:00",
        }
        self.assertEqual(_hold_minutes(t), 0.0)

    def test_overnight_hold(self):
        """隔夜持仓 → 跨天计算"""
        t = {
            "time": "2026-01-15 22:00:00",
            "exit_time": "2026-01-16 10:00:00",
        }
        # 22:00 到次日 10:00 = 12 小时 = 720 分钟
        self.assertEqual(_hold_minutes(t), 720.0)

    def test_bad_time_format_returns_none(self):
        """时间格式错误 → None"""
        t = {
            "time": "2026/01/15",
            "exit_time": "2026-01-15 10:00:00",
        }
        self.assertIsNone(_hold_minutes(t))

    def test_missing_fields_returns_none(self):
        """缺少字段 → None"""
        t = {"time": "2026-01-15 09:30:00"}
        self.assertIsNone(_hold_minutes(t))

    def test_empty_dict_returns_none(self):
        """空 dict → None"""
        self.assertIsNone(_hold_minutes({}))


# ═══════════════════════════════════════════════════════════════════════════
#  2. _hold_bucket
# ═══════════════════════════════════════════════════════════════════════════

class TestHoldBucket(unittest.TestCase):
    """_hold_bucket 持仓时长分桶。"""

    def test_none_returns_unknown(self):
        """None → "未知" """
        self.assertEqual(_hold_bucket(None), "未知")

    def test_less_than_30(self):
        """<30 分 → "<30分(抢反弹)" """
        self.assertEqual(_hold_bucket(0), "<30分(抢反弹)")
        self.assertEqual(_hold_bucket(29), "<30分(抢反弹)")

    def test_30_to_120(self):
        """30~120 分 → "30分~2时" """
        self.assertEqual(_hold_bucket(30), "30分~2时")
        self.assertEqual(_hold_bucket(60), "30分~2时")
        self.assertEqual(_hold_bucket(119), "30分~2时")

    def test_120_to_480(self):
        """120~480 分 → "2~8时(日内)" """
        self.assertEqual(_hold_bucket(120), "2~8时(日内)")
        self.assertEqual(_hold_bucket(240), "2~8时(日内)")
        self.assertEqual(_hold_bucket(479), "2~8时(日内)")

    def test_480_to_1440(self):
        """480~1440 分 → "8~24时" """
        self.assertEqual(_hold_bucket(480), "8~24时")
        self.assertEqual(_hold_bucket(1000), "8~24时")
        self.assertEqual(_hold_bucket(1439), "8~24时")

    def test_over_1440(self):
        """≥1440 分 → ">1天(过夜)" """
        self.assertEqual(_hold_bucket(1440), ">1天(过夜)")
        self.assertEqual(_hold_bucket(2880), ">1天(过夜)")

    def test_boundary_values(self):
        """边界值精确（左闭右开）"""
        # 刚好 30 → 第二个桶
        self.assertEqual(_hold_bucket(30), "30分~2时")
        # 刚好 120 → 第三个桶
        self.assertEqual(_hold_bucket(120), "2~8时(日内)")
        # 刚好 480 → 第四个桶
        self.assertEqual(_hold_bucket(480), "8~24时")
        # 刚好 1440 → 第五个桶
        self.assertEqual(_hold_bucket(1440), ">1天(过夜)")


# ═══════════════════════════════════════════════════════════════════════════
#  3. _weekday
# ═══════════════════════════════════════════════════════════════════════════

class TestWeekday(unittest.TestCase):
    """_weekday 交易日是周几。"""

    def test_monday(self):
        """周一 → "周一" """
        # 2026-01-12 是周一
        t = {"time": "2026-01-12 09:30:00"}
        self.assertEqual(_weekday(t), "周一")

    def test_friday(self):
        """周五 → "周五" """
        # 2026-01-16 是周五
        t = {"time": "2026-01-16 15:00:00"}
        self.assertEqual(_weekday(t), "周五")

    def test_sunday(self):
        """周日 → "周日" """
        # 2026-01-18 是周日
        t = {"time": "2026-01-18 21:00:00"}
        self.assertEqual(_weekday(t), "周日")

    def test_bad_format_returns_unknown(self):
        """格式错误 → "未知" """
        t = {"time": "2026/01/15"}
        self.assertEqual(_weekday(t), "未知")

    def test_empty_string_returns_unknown(self):
        """空字符串 → "未知" """
        t = {"time": ""}
        self.assertEqual(_weekday(t), "未知")

    def test_missing_time_returns_unknown(self):
        """缺少 time 字段 → "未知" """
        t = {"symbol": "rb"}
        self.assertEqual(_weekday(t), "未知")

    def test_all_weekdays(self):
        """一周七天全部正确映射"""
        # 2026-01-12 是周一，连续 7 天
        expected = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for i in range(7):
            day = 12 + i
            t = {"time": f"2026-01-{day:02d} 09:30:00"}
            self.assertEqual(_weekday(t), expected[i])


# ═══════════════════════════════════════════════════════════════════════════
#  4. _stat
# ═══════════════════════════════════════════════════════════════════════════

class TestStat(unittest.TestCase):
    """_stat 交易统计指标。"""

    def _make_trade(self, pnl, R):
        """构造一个带 _pnl 和 _R 字段的交易 dict"""
        return {"_pnl": pnl, "_R": R}

    def test_empty_list_returns_none(self):
        """空列表 → None"""
        self.assertIsNone(_stat([]))

    def test_all_wins(self):
        """全赚 → 胜率 100%, pf=99"""
        trades = [
            self._make_trade(1000, 2.0),
            self._make_trade(500, 1.0),
            self._make_trade(200, 0.5),
        ]
        s = _stat(trades)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["win_rate"], 100.0)
        self.assertEqual(s["pnl"], 1700.0)
        self.assertEqual(s["pf"], 99.0)  # 全赚时 pf=99
        self.assertAlmostEqual(s["expR"], (2.0 + 1.0 + 0.5) / 3, places=3)

    def test_all_losses(self):
        """全亏 → 胜率 0%, pf=0"""
        trades = [
            self._make_trade(-500, -1.0),
            self._make_trade(-300, -0.6),
        ]
        s = _stat(trades)
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["pf"], 0.0)

    def test_mixed_win_rate(self):
        """有赚有亏 → 胜率计算正确"""
        trades = [
            self._make_trade(1000, 2.0),   # win
            self._make_trade(-500, -1.0),  # loss
            self._make_trade(300, 0.5),    # win
            self._make_trade(-200, -0.4),  # loss
            self._make_trade(100, 0.2),    # win
        ]
        s = _stat(trades)
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["win_rate"], 60.0)  # 3/5

    def test_profit_factor(self):
        """盈亏比 = 总盈利 / 总亏损"""
        trades = [
            self._make_trade(2000, 4.0),   # win: 2000
            self._make_trade(-1000, -2.0), # loss: 1000
        ]
        s = _stat(trades)
        self.assertEqual(s["pf"], 2.0)  # 2000/1000 = 2.0

    def test_best_worst_R(self):
        """best/worst 取 R 的极值"""
        trades = [
            self._make_trade(500, 1.0),
            self._make_trade(-800, -2.5),
            self._make_trade(200, 0.5),
        ]
        s = _stat(trades)
        self.assertEqual(s["best"], 1.0)
        self.assertEqual(s["worst"], -2.5)

    def test_avg_win_loss_R(self):
        """平均赚 R / 平均亏 R"""
        trades = [
            self._make_trade(1000, 2.0),
            self._make_trade(500, 1.0),
            self._make_trade(-400, -0.8),
            self._make_trade(-600, -1.2),
        ]
        s = _stat(trades)
        # avg_win_R = (2.0 + 1.0) / 2 = 1.5
        self.assertEqual(s["avg_win_R"], 1.5)
        # avg_loss_R = (-0.8 + -1.2) / 2 = -1.0
        self.assertEqual(s["avg_loss_R"], -1.0)

    def test_reliable_flag_false_for_few(self):
        """交易数少 → reliable=False"""
        trades = [self._make_trade(100, 0.2) for _ in range(_MIN_N - 1)]
        s = _stat(trades)
        self.assertFalse(s["reliable"])

    def test_reliable_flag_true_for_enough(self):
        """交易数 ≥ _MIN_N → reliable=True"""
        trades = [self._make_trade(100, 0.2) for _ in range(_MIN_N)]
        s = _stat(trades)
        self.assertTrue(s["reliable"])

    def test_avg_pnl(self):
        """平均盈亏 = 总盈亏 / 笔数（保留 2 位小数）"""
        trades = [
            self._make_trade(1000, 2.0),
            self._make_trade(-500, -1.0),
            self._make_trade(300, 0.5),
        ]
        s = _stat(trades)
        self.assertEqual(s["avg_pnl"], 266.67)  # 800/3 ≈ 266.67


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  绩效拆解工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
