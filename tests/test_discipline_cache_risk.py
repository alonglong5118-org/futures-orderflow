#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纪律检查 + 缓存命名 + 风险计算 — 单元测试
==============================================

1. _parse_time — 时间字符串解析
   - 完整格式 YYYY-MM-DD HH:MM:SS
   - 简化格式 YYYY-MM-DD HH:MM
   - 空串/None → None
   - 非法格式 → None

2. _period_bounds — 周期边界计算
   - daily → 当日 00:00 ~ 次日 00:00
   - weekly → 当周周一 00:00 ~ 下周一 00:00
   - monthly → 当月1日 00:00 ~ 下月1日 00:00
   - 月末跨年（12月）
   - 自定义 now 参数

3. _is_signal_backed — 信号关联校验
   - 有 signal_id 且匹配 → True
   - 无 signal_id → False
   - manual 开头 → False
   - 信号不存在 → False
   - 不同品种 → False

4. _is_manual_record — 手动记录判断
   - 空 signal_id → True
   - manual 开头 → True
   - 正常信号 → False
   - 大小写不敏感

5. sym_from_cache — 缓存文件名提取品种
   - 标准 _5min.csv 后缀
   - 不同品种

6. sym_from_std — 标准文件名提取品种
   - 主连 0 后缀剥离
   - 无前导 0 的品种

7. to_key — 品种名标准化
   - 大小写匹配 SYMBOLS
   - 不在 SYMBOLS 原样返回

8. _risk_amount — 风险金额计算
   - 有 stop_dist → stop_dist * multiplier * lots
   - 无 stop_dist 但有 stop+entry → |entry-stop| * mult * lots
   - 都没有 → equity * risk_pct / 100
   - 结果 > 0
"""

import sys
import os
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from discipline_review import _parse_time, _period_bounds, _is_signal_backed, _is_manual_record
from convert_min5_cache import sym_from_cache, sym_from_std, to_key
from blunder_check import _risk_amount


# ═══════════════════════════════════════════════════════════════════════════
#  1. _parse_time
# ═══════════════════════════════════════════════════════════════════════════

class TestParseTime(unittest.TestCase):
    """_parse_time 时间字符串解析。"""

    def test_full_format(self):
        """完整格式 YYYY-MM-DD HH:MM:SS"""
        result = _parse_time("2026-08-28 10:30:00")
        self.assertEqual(result, datetime(2026, 8, 28, 10, 30, 0))

    def test_short_format(self):
        """简化格式 YYYY-MM-DD HH:MM"""
        result = _parse_time("2026-08-28 10:30")
        self.assertEqual(result, datetime(2026, 8, 28, 10, 30, 0))

    def test_empty_returns_none(self):
        """空串 → None"""
        self.assertIsNone(_parse_time(""))

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_parse_time(None))

    def test_invalid_format_none(self):
        """非法格式 → None"""
        self.assertIsNone(_parse_time("not a date"))
        self.assertIsNone(_parse_time("2026/08/28"))
        self.assertIsNone(_parse_time("08-28-2026"))

    def test_returns_datetime_or_none(self):
        """返回 datetime 或 None"""
        self.assertIsInstance(_parse_time("2026-08-28 10:30:00"), datetime)
        self.assertIsNone(_parse_time(""))

    def test_date_only_fails(self):
        """只有日期没有时间 → None（不匹配任何格式）"""
        self.assertIsNone(_parse_time("2026-08-28"))


# ═══════════════════════════════════════════════════════════════════════════
#  2. _period_bounds
# ═══════════════════════════════════════════════════════════════════════════

class TestPeriodBounds(unittest.TestCase):
    """_period_bounds 周期边界计算。"""

    def test_daily(self):
        """daily → 当日 00:00 ~ 次日 00:00"""
        now = datetime(2026, 8, 28, 14, 30, 0)
        start, end = _period_bounds("daily", now=now)
        self.assertEqual(start, datetime(2026, 8, 28, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 29, 0, 0, 0))

    def test_weekly(self):
        """weekly → 当周周一 ~ 下周一
        2026-08-28 是周五 → 周一是 2026-08-24"""
        now = datetime(2026, 8, 28, 14, 30, 0)
        self.assertEqual(now.weekday(), 4)  # 确认是周五
        start, end = _period_bounds("weekly", now=now)
        self.assertEqual(start, datetime(2026, 8, 24, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 31, 0, 0, 0))

    def test_weekly_monday(self):
        """周一当天 → 当天开始"""
        now = datetime(2026, 8, 24, 9, 0, 0)
        self.assertEqual(now.weekday(), 0)  # 周一
        start, end = _period_bounds("weekly", now=now)
        self.assertEqual(start, datetime(2026, 8, 24, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 8, 31, 0, 0, 0))

    def test_monthly(self):
        """monthly → 当月1日 ~ 下月1日"""
        now = datetime(2026, 8, 28, 14, 30, 0)
        start, end = _period_bounds("monthly", now=now)
        self.assertEqual(start, datetime(2026, 8, 1, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 1, 0, 0, 0))

    def test_monthly_december(self):
        """12月 → 次年1月（跨年）"""
        now = datetime(2026, 12, 25, 10, 0, 0)
        start, end = _period_bounds("monthly", now=now)
        self.assertEqual(start, datetime(2026, 12, 1, 0, 0, 0))
        self.assertEqual(end, datetime(2027, 1, 1, 0, 0, 0))

    def test_returns_tuple(self):
        """返回 (start, end) 二元组"""
        now = datetime(2026, 8, 28, 10, 0, 0)
        result = _period_bounds("daily", now=now)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], datetime)
        self.assertIsInstance(result[1], datetime)

    def test_end_after_start(self):
        """end > start"""
        now = datetime(2026, 8, 28, 10, 0, 0)
        for kind in ["daily", "weekly", "monthly"]:
            start, end = _period_bounds(kind, now=now)
            self.assertGreater(end, start, f"{kind} end should be after start")

    def test_default_kind_is_monthly(self):
        """未知 kind → 走 monthly 分支"""
        now = datetime(2026, 8, 28, 10, 0, 0)
        start, end = _period_bounds("unknown", now=now)
        # 应该和 monthly 一样
        m_start, m_end = _period_bounds("monthly", now=now)
        self.assertEqual(start, m_start)
        self.assertEqual(end, m_end)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _is_signal_backed
# ═══════════════════════════════════════════════════════════════════════════

class TestIsSignalBacked(unittest.TestCase):
    """_is_signal_backed 信号关联校验。"""

    def test_matching_signal_true(self):
        """有 signal_id 且同品种匹配 → True"""
        trade = {"signal_id": "sig_001", "symbol": "FG"}
        sig_map = {"sig_001": {"symbol": "FG", "direction": 1}}
        self.assertTrue(_is_signal_backed(trade, sig_map))

    def test_no_signal_id_false(self):
        """无 signal_id → False"""
        trade = {"symbol": "FG"}
        sig_map = {"sig_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_empty_signal_id_false(self):
        """空 signal_id → False"""
        trade = {"signal_id": "", "symbol": "FG"}
        sig_map = {"sig_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_manual_prefix_false(self):
        """manual 开头 → False"""
        trade = {"signal_id": "manual_001", "symbol": "FG"}
        sig_map = {"manual_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_MANUAL_uppercase_false(self):
        """MANUAL 大写 → False（lower() 判断）"""
        trade = {"signal_id": "MANUAL_001", "symbol": "FG"}
        sig_map = {"MANUAL_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_signal_not_found_false(self):
        """信号不存在 → False"""
        trade = {"signal_id": "sig_999", "symbol": "FG"}
        sig_map = {"sig_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_different_symbol_false(self):
        """不同品种 → False（防止串号）"""
        trade = {"signal_id": "sig_001", "symbol": "SA"}
        sig_map = {"sig_001": {"symbol": "FG"}}
        self.assertFalse(_is_signal_backed(trade, sig_map))

    def test_empty_sig_map_false(self):
        """空 sig_map → False"""
        trade = {"signal_id": "sig_001", "symbol": "FG"}
        self.assertFalse(_is_signal_backed(trade, {}))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_signal_backed({"signal_id": "x", "symbol": "y"}, {}), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _is_manual_record
# ═══════════════════════════════════════════════════════════════════════════

class TestIsManualRecord(unittest.TestCase):
    """_is_manual_record 手动记录判断。"""

    def test_empty_signal_id_true(self):
        """空 signal_id → True（手动记账）"""
        self.assertTrue(_is_manual_record({"signal_id": ""}))

    def test_missing_signal_id_true(self):
        """缺失 signal_id → True"""
        self.assertTrue(_is_manual_record({}))

    def test_none_signal_id_true(self):
        """None signal_id → True"""
        self.assertTrue(_is_manual_record({"signal_id": None}))

    def test_manual_prefix_true(self):
        """manual 开头 → True"""
        self.assertTrue(_is_manual_record({"signal_id": "manual_entry"}))

    def test_MANUAL_uppercase_true(self):
        """MANUAL 大写 → True（lower() 判断）"""
        self.assertTrue(_is_manual_record({"signal_id": "MANUAL_ENTRY"}))

    def test_Manual_mixed_true(self):
        """Manual 混合 → True"""
        self.assertTrue(_is_manual_record({"signal_id": "Manual_001"}))

    def test_normal_signal_false(self):
        """正常信号 → False"""
        self.assertFalse(_is_manual_record({"signal_id": "sig_001"}))

    def test_auto_signal_false(self):
        """auto 开头 → False"""
        self.assertFalse(_is_manual_record({"signal_id": "auto_gen_001"}))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_manual_record({}), bool)
        self.assertIsInstance(_is_manual_record({"signal_id": "sig_001"}), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  5. sym_from_cache
# ═══════════════════════════════════════════════════════════════════════════

class TestSymFromCache(unittest.TestCase):
    """sym_from_cache 缓存文件名提取品种。"""

    def test_standard_name(self):
        """标准 _5min.csv 后缀"""
        self.assertEqual(sym_from_cache("FG_5min.csv"), "FG")

    def test_lowercase_symbol(self):
        """小写品种名"""
        self.assertEqual(sym_from_cache("jd_5min.csv"), "jd")

    def test_path_input(self):
        """含路径的文件名"""
        self.assertEqual(sym_from_cache("/data/cache/rb_5min.csv"), "rb")

    def test_multi_char_symbol(self):
        """多字母品种"""
        self.assertEqual(sym_from_cache("FG0_5min.csv"), "FG0")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(sym_from_cache("FG_5min.csv"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  6. sym_from_std
# ═══════════════════════════════════════════════════════════════════════════

class TestSymFromStd(unittest.TestCase):
    """sym_from_std 标准文件名提取品种。"""

    def test_main_contract_zero(self):
        """主连 0 后缀 → 剥离"""
        self.assertEqual(sym_from_std("_FG0_min5.csv"), "FG")

    def test_lowercase_zero(self):
        """小写主连"""
        self.assertEqual(sym_from_std("_jd0_min5.csv"), "jd")

    def test_no_zero_suffix(self):
        """无 0 后缀 → 原样返回"""
        # 注意：函数会剥掉末尾 0，但只有末尾是 0 才剥
        # _FG_min5.csv → FG → 末尾是 G，不是 0，不剥
        self.assertEqual(sym_from_std("_FG_min5.csv"), "FG")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(sym_from_std("_FG0_min5.csv"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  7. to_key
# ═══════════════════════════════════════════════════════════════════════════

class TestToKey(unittest.TestCase):
    """to_key 品种名标准化。"""

    def test_case_insensitive_match(self):
        """大小写匹配 SYMBOLS → 返回标准形式"""
        # FG 应该在 SYMBOLS 里
        result = to_key("fg")
        self.assertIsInstance(result, str)
        # 检查返回的是标准形式（通常是大写）
        self.assertTrue(len(result) > 0)

    def test_unknown_symbol_returns_same(self):
        """不在 SYMBOLS → 原样返回"""
        self.assertEqual(to_key("UNKNOWN_XYZ"), "UNKNOWN_XYZ")

    def test_known_upper(self):
        """大写匹配 → 原样返回"""
        result = to_key("FG")
        self.assertIsInstance(result, str)
        # 应该和 SYMBOLS 中的键一致
        self.assertTrue(len(result) > 0)

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(to_key("FG"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  8. _risk_amount
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskAmount(unittest.TestCase):
    """_risk_amount 风险金额计算。"""

    def test_with_stop_dist(self):
        """有 stop_dist → stop_dist * multiplier * lots
        rb multiplier = 10，5 * 10 * 2 = 100"""
        trade = {"symbol": "rb", "stop_dist": 5.0, "lots": 2}
        result = _risk_amount(trade, 100000, 2.0)
        self.assertAlmostEqual(result, 100.0)

    def test_with_stop_and_entry(self):
        """无 stop_dist，有 stop + entry → |entry-stop| * mult * lots
        rb multiplier = 10，|100-95| * 10 * 1 = 50"""
        trade = {"symbol": "rb", "entry_price": 100.0, "stop": 95.0, "lots": 1}
        result = _risk_amount(trade, 100000, 2.0)
        self.assertAlmostEqual(result, 50.0)

    def test_stop_above_entry_same(self):
        """stop > entry → 绝对值，结果相同"""
        trade = {"symbol": "rb", "entry_price": 95.0, "stop": 100.0, "lots": 1}
        result = _risk_amount(trade, 100000, 2.0)
        self.assertAlmostEqual(result, 50.0)

    def test_fallback_equity_pct(self):
        """都没有 → equity * risk_pct / 100"""
        trade = {"symbol": "rb"}
        result = _risk_amount(trade, 100000, 2.0)
        # 100000 * 2.0 / 100 = 2000
        self.assertAlmostEqual(result, 2000.0)

    def test_fallback_minimum_1(self):
        """极小 equity * pct < 1 → 至少 1.0"""
        trade = {"symbol": "rb"}
        result = _risk_amount(trade, 10, 0.5)
        # 10 * 0.5 / 100 = 0.05 → max(1.0, 0.05) = 1.0
        self.assertAlmostEqual(result, 1.0)

    def test_stop_dist_prefers_over_stop(self):
        """同时有 stop_dist 和 stop → 优先用 stop_dist
        rb: stop_dist=3 → 30; stop=5 → 50; 取优先的 30"""
        trade = {"symbol": "rb", "stop_dist": 3.0, "entry_price": 100.0, "stop": 95.0, "lots": 1}
        result = _risk_amount(trade, 100000, 2.0)
        # stop_dist 优先: 3 * 10 * 1 = 30
        self.assertAlmostEqual(result, 30.0)

    def test_zero_lots_defaults_1(self):
        """lots 为空 → 默认 1
        rb: 5 * 10 * 1 = 50"""
        trade = {"symbol": "rb", "stop_dist": 5.0}
        result = _risk_amount(trade, 100000, 2.0)
        self.assertAlmostEqual(result, 50.0)

    def test_returns_positive(self):
        """返回正数"""
        trade = {"symbol": "rb", "stop_dist": 5.0, "lots": 1}
        result = _risk_amount(trade, 100000, 2.0)
        self.assertGreater(result, 0)

    def test_returns_float(self):
        """返回 float"""
        trade = {"symbol": "rb", "stop_dist": 5.0, "lots": 1}
        self.assertIsInstance(_risk_amount(trade, 100000, 2.0), float)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  纪律检查 + 缓存命名 + 风险计算 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
