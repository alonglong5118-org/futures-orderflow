#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绩效分桶 + 统计 + 违规检查 — 单元测试
==============================================

1. _hold_bucket — 持仓时长分桶
   - None → "未知"
   - <30分 → "<30分(抢反弹)"
   - 30~120分 → "30分~2时"
   - 120~480分 → "2~8时(日内)"
   - 480~1440分 → "8~24时"
   - >=1440分 → ">1天(过夜)"
   - 边界值验证（30/120/480/1440）
   - 0 分钟 → <30分

2. _stat — 交易切片统计
   - 空列表 → None
   - 全盈利 → win_rate=100%, gl=0 → pf=99.0
   - 全亏损 → win_rate=0%, gp=0 → pf=0.0
   - 混合 → pf=gp/gl
   - 各字段正确：n/win_rate/pnl/avg_pnl/expR/avg_win_R/avg_loss_R/pf/best/worst/reliable
   - reliable: n >= MIN_N
   - 胜率=盈利数/总数
   - 平均盈亏 = 总盈亏/总数

3. _to_ts — 时间字符串转时间戳
   - 正常格式 → 有效时间戳(>0)
   - 格式错误 → 0.0
   - 空串 → 0.0
   - None → 0.0
   - 返回 float
   - 同一时间的时间戳固定

4. _b — 违规记录构造
   - 6 字段完整
   - 各字段值正确传递
   - 返回 dict
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from perf_breakdown import _hold_bucket, _stat
from blunder_check import _to_ts, _b


# ═══════════════════════════════════════════════════════════════════════════
#  1. _hold_bucket
# ═══════════════════════════════════════════════════════════════════════════

class TestHoldBucket(unittest.TestCase):
    """_hold_bucket 持仓时长分桶。"""

    def test_none_unknown(self):
        """None → '未知'"""
        self.assertEqual(_hold_bucket(None), "未知")

    def test_zero_less_than_30(self):
        """0 分钟 → <30分"""
        self.assertEqual(_hold_bucket(0), "<30分(抢反弹)")

    def test_29_minutes(self):
        """29 分钟 → <30分"""
        self.assertEqual(_hold_bucket(29), "<30分(抢反弹)")

    def test_at_30_boundary(self):
        """恰好 30 分钟 → 30分~2时（>=30 且 <120）"""
        # 30 < 30? No → 进入下一档
        self.assertEqual(_hold_bucket(30), "30分~2时")

    def test_60_minutes(self):
        """60 分钟 → 30分~2时"""
        self.assertEqual(_hold_bucket(60), "30分~2时")

    def test_at_120_boundary(self):
        """恰好 120 分钟 → 2~8时(日内)"""
        # 120 < 120? No → 下一档
        self.assertEqual(_hold_bucket(120), "2~8时(日内)")

    def test_240_minutes(self):
        """240 分钟 → 2~8时(日内)"""
        self.assertEqual(_hold_bucket(240), "2~8时(日内)")

    def test_at_480_boundary(self):
        """恰好 480 分钟 → 8~24时"""
        self.assertEqual(_hold_bucket(480), "8~24时")

    def test_600_minutes(self):
        """600 分钟 → 8~24时"""
        self.assertEqual(_hold_bucket(600), "8~24时")

    def test_at_1440_boundary(self):
        """恰好 1440 分钟 → >1天(过夜)"""
        # 1440 < 1440? No → 最后一档
        self.assertEqual(_hold_bucket(1440), ">1天(过夜)")

    def test_2000_minutes(self):
        """2000 分钟 → >1天(过夜)"""
        self.assertEqual(_hold_bucket(2000), ">1天(过夜)")

    def test_negative_treated_as_less_30(self):
        """负数 → <30分（<30 成立）"""
        # m < 30 → 第一档
        self.assertEqual(_hold_bucket(-10), "<30分(抢反弹)")

    def test_returns_string(self):
        """返回字符串"""
        self.assertIsInstance(_hold_bucket(60), str)

    def test_five_buckets_plus_unknown(self):
        """5 个分桶 + 未知 = 6 种标签"""
        labels = set()
        for m in [None, 10, 60, 240, 600, 2000]:
            labels.add(_hold_bucket(m))
        self.assertEqual(len(labels), 6)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _stat
# ═══════════════════════════════════════════════════════════════════════════

class TestStat(unittest.TestCase):
    """_stat 交易切片统计。"""

    def _trades(self, pnl_r_pairs):
        """构造测试交易数据。"""
        return [{"_pnl": p, "_R": r} for p, r in pnl_r_pairs]

    def test_empty_returns_none(self):
        """空列表 → None"""
        self.assertIsNone(_stat([]))

    def test_all_wins(self):
        """全盈利 → win_rate=100%, pf=99.0"""
        trades = self._trades([(1000, 2.0), (500, 1.0), (200, 0.5)])
        s = _stat(trades)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["win_rate"], 100.0)
        self.assertEqual(s["pnl"], 1700)
        self.assertEqual(s["avg_pnl"], round(1700/3, 2))
        self.assertEqual(s["pf"], 99.0)  # 全赢 → pf=99.0
        self.assertEqual(s["avg_loss_R"], 0.0)

    def test_all_losses(self):
        """全亏损 → win_rate=0%, pf=0.0"""
        trades = self._trades([(-500, -1.0), (-300, -0.5)])
        s = _stat(trades)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["pf"], 0.0)  # 全亏 → pf=0.0
        self.assertEqual(s["avg_win_R"], 0.0)

    def test_mixed_profit_factor(self):
        """混合 → pf = gp / gl"""
        # gp = 1000, gl = 500 → pf = 2.0
        trades = self._trades([(1000, 2.0), (-500, -1.0)])
        s = _stat(trades)
        self.assertEqual(s["pf"], 2.0)
        self.assertEqual(s["win_rate"], 50.0)

    def test_expR_average(self):
        """expR = 平均 R"""
        trades = self._trades([(100, 1.0), (-50, -0.5), (200, 2.0)])
        s = _stat(trades)
        # (1.0 + (-0.5) + 2.0) / 3 = 2.5 / 3 ≈ 0.833
        self.assertAlmostEqual(s["expR"], 0.833, places=3)

    def test_avg_win_R(self):
        """avg_win_R = 盈利交易平均 R"""
        trades = self._trades([(100, 2.0), (-50, -1.0), (300, 3.0)])
        s = _stat(trades)
        # (2.0 + 3.0) / 2 = 2.5
        self.assertEqual(s["avg_win_R"], 2.5)

    def test_avg_loss_R(self):
        """avg_loss_R = 亏损交易平均 R"""
        trades = self._trades([(100, 2.0), (-50, -1.0), (-30, -0.5)])
        s = _stat(trades)
        # (-1.0 + -0.5) / 2 = -0.75
        self.assertEqual(s["avg_loss_R"], -0.75)

    def test_best_worst(self):
        """best/worst = 最大/最小 R"""
        trades = self._trades([(100, 2.0), (-50, -1.5), (200, 3.0)])
        s = _stat(trades)
        self.assertEqual(s["best"], 3.0)
        self.assertEqual(s["worst"], -1.5)

    def test_reliable_flag(self):
        """reliable: n >= MIN_N"""
        from perf_breakdown import _MIN_N
        # 不足 MIN_N
        trades = self._trades([(100, 1.0)] * 2)
        s = _stat(trades)
        self.assertEqual(s["reliable"], 2 >= _MIN_N)
        # 超过 MIN_N
        trades_many = self._trades([(100, 1.0)] * (_MIN_N + 5))
        s2 = _stat(trades_many)
        self.assertTrue(s2["reliable"])

    def test_win_rate_formula(self):
        """胜率 = 盈利数 / 总数 × 100"""
        trades = self._trades([(100, 1.0), (-50, -1.0), (200, 2.0), (-30, -0.5)])
        s = _stat(trades)
        self.assertEqual(s["win_rate"], 50.0)  # 2/4

    def test_avg_pnl_formula(self):
        """平均盈亏 = 总盈亏 / 总数"""
        trades = self._trades([(100, 1.0), (-50, -1.0)])
        s = _stat(trades)
        self.assertEqual(s["avg_pnl"], 25.0)  # (100-50)/2 = 25

    def test_return_fields(self):
        """返回 11 字段"""
        trades = self._trades([(100, 1.0)])
        s = _stat(trades)
        for key in ("n", "win_rate", "pnl", "avg_pnl", "expR",
                     "avg_win_R", "avg_loss_R", "pf", "best", "worst", "reliable"):
            self.assertIn(key, s)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _to_ts
# ═══════════════════════════════════════════════════════════════════════════

class TestToTs(unittest.TestCase):
    """_to_ts 时间字符串转时间戳。"""

    def test_valid_format_positive(self):
        """正常格式 → 有效时间戳(>0)"""
        ts = _to_ts("2026-08-28 14:30:00")
        self.assertGreater(ts, 0)
        self.assertIsInstance(ts, float)

    def test_invalid_format_zero(self):
        """格式错误 → 0.0"""
        self.assertEqual(_to_ts("not a date"), 0.0)

    def test_empty_string_zero(self):
        """空串 → 0.0"""
        self.assertEqual(_to_ts(""), 0.0)

    def test_none_zero(self):
        """None → 0.0"""
        self.assertEqual(_to_ts(None), 0.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_to_ts("2026-08-28 14:30:00"), float)

    def test_same_time_same_ts(self):
        """同一时间的时间戳固定"""
        ts1 = _to_ts("2026-01-01 10:00:00")
        ts2 = _to_ts("2026-01-01 10:00:00")
        self.assertEqual(ts1, ts2)

    def test_later_time_greater(self):
        """更晚的时间 → 更大的时间戳"""
        ts_early = _to_ts("2026-01-01 10:00:00")
        ts_late = _to_ts("2026-01-01 12:00:00")
        self.assertGreater(ts_late, ts_early)

    def test_wrong_separator(self):
        """分隔符错误 → 0.0"""
        self.assertEqual(_to_ts("2026/08/28 14:30:00"), 0.0)

    def test_partial_format(self):
        """只有日期 → 0.0"""
        self.assertEqual(_to_ts("2026-08-28"), 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _b
# ═══════════════════════════════════════════════════════════════════════════

class TestBlunderRecord(unittest.TestCase):
    """_b 违规记录构造。"""

    def test_six_fields(self):
        """6 字段完整"""
        r = _b("类型", "H", "rb", "2026-08-28 10:00:00", "详情", "建议")
        for key in ("type", "sev", "symbol", "time", "detail", "suggestion"):
            self.assertIn(key, r)

    def test_values_passed_through(self):
        """各字段值正确传递"""
        r = _b("无止损", "H", "IF", "2026-01-01 09:00:00", "未设止损", "带止损")
        self.assertEqual(r["type"], "无止损")
        self.assertEqual(r["sev"], "H")
        self.assertEqual(r["symbol"], "IF")
        self.assertEqual(r["time"], "2026-01-01 09:00:00")
        self.assertEqual(r["detail"], "未设止损")
        self.assertEqual(r["suggestion"], "带止损")

    def test_returns_dict(self):
        """返回 dict"""
        self.assertIsInstance(_b("t", "H", "s", "t", "d", "s"), dict)

    def test_empty_strings(self):
        """空字符串也正常传递"""
        r = _b("", "", "", "", "", "")
        self.assertEqual(r["type"], "")
        self.assertEqual(r["sev"], "")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  绩效分桶 + 统计 + 违规检查 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
