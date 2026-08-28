#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
止盈止损计划 + 持仓时长 + 信号来源 — 单元测试
====================================================

1. calc_exit_plan — 止损/止盈/尾仓参数计算
   - 多单：止损在下方，t1/t2 在上方
   - 空单：止损在上方，t1/t2 在下方
   - stop_dist = stop_atr_mult × regime_coef × ATR
   - t1 = entry ± 1R（平半仓）
   - t2 = entry ± rr_ratio × R（全平/尾仓）
   - regime 系数影响止损距离（1.2 → 更远）
   - 尾仓跟踪距离 = tail_trail_R × stop_dist
   - 尾仓未启用 → tail_enabled=False
   - 尾仓启用 → tail_enabled=True
   - 价格保留 2 位小数
   - 返回 7 字段
   - dir_T 为负 → 空单
   - ATR 为 0 → 止损=入场价

2. _duration — 持仓时长格式化
   - 不到 1 小时 → X分钟
   - 整小时 → X小时
   - 小时+分钟 → X小时Y分
   - 时间倒序（t2 < t1）→ 空串
   - 格式错误 → 空串
   - 0 分钟 → "0分钟"
   - 恰好 1 小时 → "1小时"
   - 恰好 1 分钟 → "1分钟"

3. _source_label — 信号来源标签
   - 空字符串 → ""
   - None → ""
   - "账户同步..." → "账户同步"
   - "历史持仓..." → "账户同步"
   - "手动" → "手动"
   - "manual_xxx" → "手动"（大小写不敏感）
   - 时间格式（yyyy-MM-dd HH:mm:ss）→ "信号"
   - 其他 → "其他"
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from discipline_review import _duration, _source_label
from take_profit_utils import calc_exit_plan

# ═══════════════════════════════════════════════════════════════════════════
#  1. calc_exit_plan
# ═══════════════════════════════════════════════════════════════════════════

class TestCalcExitPlan(unittest.TestCase):
    """calc_exit_plan 止损/止盈/尾仓参数计算。"""

    def test_long_stop_below_entry(self):
        """多单：止损在下方"""
        # entry=100, atr=10, stop_atr_mult=1.5 → stop_dist=15
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        self.assertEqual(r["stop"], 85.0)   # 100 - 15
        self.assertLess(r["stop"], 100)

    def test_long_t1_t2_above_entry(self):
        """多单：t1/t2 在上方"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        self.assertEqual(r["t1"], 115.0)   # 100 + 15
        self.assertEqual(r["t2"], 130.0)   # 100 + 30
        self.assertGreater(r["t1"], 100)
        self.assertGreater(r["t2"], r["t1"])

    def test_short_stop_above_entry(self):
        """空单：止损在上方"""
        r = calc_exit_plan(entry=100, dir_T=-1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        self.assertEqual(r["stop"], 115.0)  # 100 + 15
        self.assertGreater(r["stop"], 100)

    def test_short_t1_t2_below_entry(self):
        """空单：t1/t2 在下方"""
        r = calc_exit_plan(entry=100, dir_T=-1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        self.assertEqual(r["t1"], 85.0)    # 100 - 15
        self.assertEqual(r["t2"], 70.0)    # 100 - 30
        self.assertLess(r["t1"], 100)
        self.assertLess(r["t2"], r["t1"])

    def test_stop_dist_formula(self):
        """stop_dist = stop_atr_mult × regime_coef × ATR"""
        # 1.5 * 1.0 * 10 = 15
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5,
                           regime_stop_coef=1.0, rr_ratio=2.0)
        self.assertEqual(r["stop_dist"], 15.0)

    def test_regime_coef_affects_stop(self):
        """regime 系数影响止损距离（1.2 → 更远）"""
        r1 = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5,
                            regime_stop_coef=1.0, rr_ratio=2.0)
        r2 = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5,
                            regime_stop_coef=1.2, rr_ratio=2.0)
        # 1.2 系数 → 止损更远
        self.assertGreater(r2["stop_dist"], r1["stop_dist"])
        # 1.5 * 1.2 * 10 = 18
        self.assertEqual(r2["stop_dist"], 18.0)

    def test_t1_is_one_R(self):
        """t1 = entry ± 1R（平半仓）"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        # 1R = stop_dist = 15
        self.assertEqual(r["t1"] - 100, r["stop_dist"])

    def test_t2_is_rr_ratio_R(self):
        """t2 = entry ± rr_ratio × R"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.5)
        expected = 100 + 2.5 * 15  # entry + rr_ratio * stop_dist
        self.assertEqual(r["t2"], expected)

    def test_tail_stop_dist_formula(self):
        """尾仓跟踪距离 = tail_trail_R × stop_dist"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, stop_atr_mult=1.5,
                           rr_ratio=2.0, tail_enabled=True, tail_trail_R=2.0)
        # stop_dist=15, tail_trail_R=2 → 30
        self.assertEqual(r["tail_stop_dist"], 30.0)

    def test_tail_disabled_default(self):
        """尾仓未启用 → tail_enabled=False"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10)
        self.assertFalse(r["tail_enabled"])

    def test_tail_enabled_flag(self):
        """尾仓启用 → tail_enabled=True"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, tail_enabled=True)
        self.assertTrue(r["tail_enabled"])

    def test_prices_two_decimals(self):
        """价格保留 2 位小数"""
        r = calc_exit_plan(entry=100.123, dir_T=1, atr_val=10.345, stop_atr_mult=1.5, rr_ratio=2.0)
        for key in ("stop", "t1", "t2", "stop_dist", "tail_stop_dist"):
            self.assertEqual(r[key], round(r[key], 2))

    def test_return_seven_fields(self):
        """返回 7 字段"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10)
        for key in ("stop", "t1", "t2", "stop_dist",
                     "tail_enabled", "tail_stop_dist", "tail_pct"):
            self.assertIn(key, r)

    def test_negative_dir_is_short(self):
        """dir_T 为负 → 空单"""
        r = calc_exit_plan(entry=100, dir_T=-5, atr_val=10, stop_atr_mult=1.5, rr_ratio=2.0)
        # 空单特征：止损在上方
        self.assertGreater(r["stop"], 100)
        self.assertLess(r["t1"], 100)

    def test_zero_atr_stop_equals_entry(self):
        """ATR 为 0 → 止损=入场价"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=0, stop_atr_mult=1.5, rr_ratio=2.0)
        self.assertEqual(r["stop_dist"], 0.0)
        self.assertEqual(r["stop"], 100.0)
        self.assertEqual(r["t1"], 100.0)

    def test_tail_pct_passed_through(self):
        """tail_pct 原样返回"""
        r = calc_exit_plan(entry=100, dir_T=1, atr_val=10, tail_pct=0.3)
        self.assertEqual(r["tail_pct"], 0.3)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _duration
# ═══════════════════════════════════════════════════════════════════════════

class TestDuration(unittest.TestCase):
    """_duration 持仓时长格式化。"""

    def test_less_than_hour_minutes_only(self):
        """不到 1 小时 → X分钟"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 10:35:00"), "35分钟")

    def test_exact_hours(self):
        """整小时 → X小时"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 12:00:00"), "2小时")

    def test_hours_and_minutes(self):
        """小时+分钟 → X小时Y分"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 12:35:00"), "2小时35分")

    def test_negative_duration_empty(self):
        """时间倒序（t2 < t1）→ 空串"""
        self.assertEqual(_duration("2026-01-01 12:00:00", "2026-01-01 10:00:00"), "")

    def test_bad_format_empty(self):
        """格式错误 → 空串"""
        self.assertEqual(_duration("not a date", "2026-01-01 10:00:00"), "")
        self.assertEqual(_duration("2026-01-01 10:00:00", "not a date"), "")

    def test_zero_minutes(self):
        """0 分钟 → 0分钟"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 10:00:00"), "0分钟")

    def test_one_hour_exact(self):
        """恰好 1 小时 → 1小时"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 11:00:00"), "1小时")

    def test_one_minute(self):
        """恰好 1 分钟 → 1分钟"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 10:01:00"), "1分钟")

    def test_across_midnight(self):
        """跨天计算"""
        # 23:00 → 次日 01:30 = 2小时30分
        self.assertEqual(_duration("2026-01-01 23:00:00", "2026-01-02 01:30:00"), "2小时30分")

    def test_59_minutes(self):
        """59 分钟 → 59分钟"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 10:59:00"), "59分钟")

    def test_60_minutes_is_one_hour(self):
        """60 分钟 = 1 小时 → 1小时"""
        self.assertEqual(_duration("2026-01-01 10:00:00", "2026-01-01 11:00:00"), "1小时")

    def test_returns_string(self):
        """返回字符串"""
        self.assertIsInstance(_duration("2026-01-01 10:00:00", "2026-01-01 10:05:00"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _source_label
# ═══════════════════════════════════════════════════════════════════════════

class TestSourceLabel(unittest.TestCase):
    """_source_label 信号来源标签。"""

    def test_empty_string_empty(self):
        """空字符串 → "" """
        self.assertEqual(_source_label(""), "")

    def test_none_empty(self):
        """None → "" """
        self.assertEqual(_source_label(None), "")

    def test_account_sync_prefix(self):
        """'账户同步...' → '账户同步'"""
        self.assertEqual(_source_label("账户同步_xxx"), "账户同步")
        self.assertEqual(_source_label("账户同步"), "账户同步")

    def test_history_position_prefix(self):
        """'历史持仓...' → '账户同步'"""
        self.assertEqual(_source_label("历史持仓_xxx"), "账户同步")
        self.assertEqual(_source_label("历史持仓"), "账户同步")

    def test_manual_exact(self):
        """'手动' → '手动'"""
        self.assertEqual(_source_label("手动"), "手动")

    def test_manual_prefix_case_insensitive(self):
        """'manual_xxx' → '手动'（大小写不敏感）"""
        self.assertEqual(_source_label("manual_entry"), "手动")
        self.assertEqual(_source_label("MANUAL_test"), "手动")
        self.assertEqual(_source_label("Manual_abc"), "手动")

    def test_time_format_signal(self):
        """时间格式（yyyy-MM-dd HH:mm:ss）→ '信号'"""
        self.assertEqual(_source_label("2026-08-28 14:30:00"), "信号")

    def test_other_label(self):
        """其他 → '其他'"""
        self.assertEqual(_source_label("随便什么"), "其他")
        self.assertEqual(_source_label("unknown"), "其他")

    def test_short_string_other(self):
        """短字符串（<19字符且非前缀）→ 其他"""
        self.assertEqual(_source_label("abc"), "其他")

    def test_long_non_time_other(self):
        """长字符串但非时间格式 → 其他"""
        self.assertEqual(_source_label("abcdefghijklmnopqrs"), "其他")

    def test_time_format_checks_positions(self):
        """时间格式校验位置：第4位'-'、第10位' '、第13位':'"""
        # 正确格式
        self.assertEqual(_source_label("2026-08-28 14:30:00"), "信号")
        # 第4位不是'-'
        self.assertEqual(_source_label("2026x08-28 14:30:00"), "其他")
        # 第10位不是' '
        self.assertEqual(_source_label("2026-08-28x14:30:00"), "其他")
        # 第13位不是':'
        self.assertEqual(_source_label("2026-08-28 14x30:00"), "其他")

    def test_returns_string(self):
        """返回字符串"""
        self.assertIsInstance(_source_label("手动"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  止盈止损计划 + 持仓时长 + 信号来源 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
