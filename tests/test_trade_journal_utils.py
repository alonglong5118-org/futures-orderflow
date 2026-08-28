#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易日记工具函数 — 单元测试
=================================

1. _dir_sign — 方向符号
   - 多 → +1
   - 空 → -1
   - 其他 → 0

2. _validate_entry_stop — 止损方向校验 + 镜像修正
   - 多单 + 止损低于入场 → OK
   - 多单 + 止损高于入场 → 镜像修正到下方
   - 空单 + 止损高于入场 → OK
   - 空单 + 止损低于入场 → 镜像修正到上方
   - None → None
   - 方向不明 → 不处理
   - 止损=入场 → 微调 0.01

3. _normalize_sym — 品种名标准化
   - 已在表里 → 原样返回
   - 小写输入 → 匹配小写 key
   - 大写输入 → 匹配大写 key
   - 不在表里 → 原样返回
   - 空串 → 原样返回

4. _leg_fee — 单边手续费
   - fixed 模式开仓
   - fixed 模式平仓
   - fixed 平今免收（close_today=0）
   - pct 模式开仓
   - 回退模式（未知品种）
   - 非法输入 → 0

5. _session_of — 交易时段判断
   - 日盘（09:00 - 15:00）
   - 夜盘（21:00 - 02:30）
   - 其他时段
   - 边界：09:00 / 15:00 / 21:00 / 02:30
   - 非法输入 → 其他
"""

import sys
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from trade_journal import (
    _dir_sign,
    _validate_entry_stop,
    _normalize_sym,
    _leg_fee,
    _session_of,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. _dir_sign
# ═══════════════════════════════════════════════════════════════════════════

class TestDirSign(unittest.TestCase):
    """_dir_sign 方向符号。"""

    def test_long_is_positive(self):
        """多 → +1"""
        self.assertEqual(_dir_sign("多"), 1)

    def test_short_is_negative(self):
        """空 → -1"""
        self.assertEqual(_dir_sign("空"), -1)

    def test_other_is_zero(self):
        """其他 → 0"""
        self.assertEqual(_dir_sign(""), 0)
        self.assertEqual(_dir_sign("平"), 0)
        self.assertEqual(_dir_sign(None), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _validate_entry_stop
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateEntryStop(unittest.TestCase):
    """_validate_entry_stop 止损方向校验 + 镜像修正。"""

    def test_long_stop_below_entry_ok(self):
        """多单 + 止损低于入场 → OK，不修正"""
        stop, note = _validate_entry_stop("多", 100.0, 95.0)
        self.assertEqual(stop, 95.0)
        self.assertEqual(note, "")

    def test_long_stop_above_entry_fixed(self):
        """多单 + 止损高于入场 → 镜像修正到下方"""
        stop, note = _validate_entry_stop("多", 100.0, 105.0)
        # 镜像：100 + (100 - 105) = 95
        self.assertAlmostEqual(stop, 95.0, places=2)
        self.assertIn("止损方向已自动修正", note)
        self.assertIn("95", note)

    def test_short_stop_above_entry_ok(self):
        """空单 + 止损高于入场 → OK"""
        stop, note = _validate_entry_stop("空", 100.0, 105.0)
        self.assertEqual(stop, 105.0)
        self.assertEqual(note, "")

    def test_short_stop_below_entry_fixed(self):
        """空单 + 止损低于入场 → 镜像修正到上方"""
        stop, note = _validate_entry_stop("空", 100.0, 95.0)
        # 镜像：100 + (100 - 95) = 105
        self.assertAlmostEqual(stop, 105.0, places=2)
        self.assertIn("止损方向已自动修正", note)

    def test_none_stop_returns_none(self):
        """stop=None → (None, "")"""
        stop, note = _validate_entry_stop("多", 100.0, None)
        self.assertIsNone(stop)
        self.assertEqual(note, "")

    def test_unknown_direction_no_fix(self):
        """方向不明 → 不处理"""
        stop, note = _validate_entry_stop("平", 100.0, 105.0)
        self.assertEqual(stop, 105.0)
        self.assertEqual(note, "")

    def test_stop_equals_entry_adjusted(self):
        """止损 = 入场 → 微调（方向由实现决定，确保有非零距离）"""
        stop, note = _validate_entry_stop("多", 100.0, 100.0)
        # 关键断言：止损价 ≠ 入场价（有实际距离）
        self.assertNotEqual(float(stop), 100.0)
        self.assertIn("止损方向已自动修正", note)
        # 距离 = 0.01
        self.assertAlmostEqual(abs(float(stop) - 100.0), 0.01, places=2)

    def test_stop_equals_entry_short(self):
        """空单止损 = 入场 → 微调（确保有非零距离）"""
        stop, note = _validate_entry_stop("空", 100.0, 100.0)
        self.assertNotEqual(float(stop), 100.0)
        self.assertIn("止损方向已自动修正", note)
        self.assertAlmostEqual(abs(float(stop) - 100.0), 0.01, places=2)

    def test_invalid_price_no_crash(self):
        """非法价格 → 不崩溃"""
        stop, note = _validate_entry_stop("多", "abc", 95.0)
        self.assertEqual(stop, 95.0)  # 原样返回
        self.assertEqual(note, "")

    def test_mirror_symmetry(self):
        """镜像修正对称：多单 105 修正到 95，空单 95 修正到 105"""
        stop_long, _ = _validate_entry_stop("多", 100.0, 105.0)
        stop_short, _ = _validate_entry_stop("空", 100.0, 95.0)
        # 到入场价的距离应该相等
        dist_long = abs(float(stop_long) - 100.0)
        dist_short = abs(float(stop_short) - 100.0)
        self.assertAlmostEqual(dist_long, dist_short, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _normalize_sym
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizeSym(unittest.TestCase):
    """_normalize_sym 品种名标准化。"""

    def test_exact_match_returns_as_is(self):
        """已在表里（小写）→ 原样返回"""
        from trade_journal import _MULTIPLIERS
        # 找一个小写 key
        lower_keys = [k for k in _MULTIPLIERS if k.islower()]
        if not lower_keys:
            self.skipTest("没有小写品种名")
        sym = lower_keys[0]
        self.assertEqual(_normalize_sym(sym), sym)

    def test_upper_input_matches_lower_key(self):
        """大写输入 → 匹配小写 key"""
        from trade_journal import _MULTIPLIERS
        lower_keys = [k for k in _MULTIPLIERS if k.islower()]
        if not lower_keys:
            self.skipTest("没有小写品种名")
        sym = lower_keys[0]
        upper_sym = sym.upper()
        self.assertEqual(_normalize_sym(upper_sym), sym)

    def test_lower_input_matches_upper_key(self):
        """小写输入 → 匹配大写 key"""
        from trade_journal import _MULTIPLIERS
        upper_keys = [k for k in _MULTIPLIERS if k.isupper()]
        if not upper_keys:
            self.skipTest("没有大写品种名")
        sym = upper_keys[0]
        lower_sym = sym.lower()
        self.assertEqual(_normalize_sym(lower_sym), sym)

    def test_unknown_returns_as_is(self):
        """不在表里 → 原样返回"""
        self.assertEqual(_normalize_sym("XYZ"), "XYZ")
        self.assertEqual(_normalize_sym("unknown"), "unknown")

    def test_empty_string(self):
        """空串 → 原样返回"""
        self.assertEqual(_normalize_sym(""), "")


# ═══════════════════════════════════════════════════════════════════════════
#  4. _leg_fee
# ═══════════════════════════════════════════════════════════════════════════

class TestLegFee(unittest.TestCase):
    """_leg_fee 单边手续费。"""

    def test_fixed_mode_open(self):
        """fixed 模式开仓"""
        from trade_journal import _FEE_SCHEDULE
        # 找一个 fixed 模式的品种
        fixed_sym = None
        for sym, cfg in _FEE_SCHEDULE.items():
            if cfg.get("mode") == "fixed":
                fixed_sym = sym
                break
        if not fixed_sym:
            self.skipTest("没有 fixed 模式的品种")

        fee = _leg_fee(fixed_sym, 1000, 2, side="open")
        expected = round(_FEE_SCHEDULE[fixed_sym]["open"] * 2, 2)
        self.assertEqual(fee, expected)

    def test_fixed_mode_close(self):
        """fixed 模式平仓（非平今）"""
        from trade_journal import _FEE_SCHEDULE
        fixed_sym = None
        for sym, cfg in _FEE_SCHEDULE.items():
            if cfg.get("mode") == "fixed" and "close" in cfg:
                fixed_sym = sym
                break
        if not fixed_sym:
            self.skipTest("没有 fixed 模式平仓的品种")

        fee = _leg_fee(fixed_sym, 1000, 2, side="close")
        expected = round(_FEE_SCHEDULE[fixed_sym]["close"] * 2, 2)
        self.assertEqual(fee, expected)

    def test_fixed_close_today_free(self):
        """平今免收：close_today=0 → 0 元"""
        from trade_journal import _FEE_SCHEDULE
        free_sym = None
        for sym, cfg in _FEE_SCHEDULE.items():
            if cfg.get("close_today") == 0:
                free_sym = sym
                break
        if not free_sym:
            self.skipTest("没有平今免收的品种")

        fee = _leg_fee(free_sym, 1000, 5, side="close", same_day=True)
        self.assertEqual(fee, 0.0)

    def test_pct_mode_open(self):
        """pct 模式开仓：fee = price × mult × lots × rate"""
        from trade_journal import _FEE_SCHEDULE, _MULTIPLIERS
        pct_sym = None
        for sym, cfg in _FEE_SCHEDULE.items():
            if cfg.get("mode") != "fixed" and "open" in cfg:
                pct_sym = sym
                break
        if not pct_sym:
            self.skipTest("没有 pct 模式的品种")

        price = 3000.0
        lots = 2
        mult = _MULTIPLIERS.get(pct_sym, 10)
        rate = float(_FEE_SCHEDULE[pct_sym]["open"])
        expected = round(price * mult * lots * rate, 2)
        fee = _leg_fee(pct_sym, price, lots, side="open")
        self.assertEqual(fee, expected)

    def test_unknown_symbol_fallback(self):
        """未知品种 → 回退默认费率"""
        from trade_journal import _FEE_DEFAULT, _MULTIPLIERS
        fee = _leg_fee("UNKNOWN_XYZ", 1000, 1, side="open")
        # 回退模式：price * mult * lots * _FEE_DEFAULT
        expected = round(1000 * 10 * 1 * _FEE_DEFAULT, 2)
        self.assertEqual(fee, expected)

    def test_invalid_input_returns_zero(self):
        """非法输入 → 0"""
        self.assertEqual(_leg_fee("rb", "abc", 1), 0.0)
        self.assertEqual(_leg_fee("rb", 1000, "xyz"), 0.0)

    def test_zero_lots_fee_zero(self):
        """0 手 → 0 手续费"""
        fee = _leg_fee("rb", 3000, 0, side="open")
        self.assertEqual(fee, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. _session_of
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionOf(unittest.TestCase):
    """_session_of 交易时段判断。"""

    def test_morning_day_session(self):
        """上午 → 日盘"""
        self.assertEqual(_session_of("2026-01-15 09:30:00"), "日盘")

    def test_afternoon_day_session(self):
        """下午 → 日盘"""
        self.assertEqual(_session_of("2026-01-15 14:30:00"), "日盘")

    def test_night_session_evening(self):
        """晚上 21 点后 → 夜盘"""
        self.assertEqual(_session_of("2026-01-15 22:00:00"), "夜盘")

    def test_night_session_after_midnight(self):
        """凌晨 → 夜盘"""
        self.assertEqual(_session_of("2026-01-16 01:30:00"), "夜盘")

    def test_other_session_morning_early(self):
        """清晨 8 点 → 其他"""
        self.assertEqual(_session_of("2026-01-15 08:00:00"), "其他")

    def test_other_session_evening_early(self):
        """傍晚 18 点 → 其他"""
        self.assertEqual(_session_of("2026-01-15 18:00:00"), "其他")

    def test_boundary_0900_is_day(self):
        """边界：09:00 → 日盘"""
        self.assertEqual(_session_of("2026-01-15 09:00:00"), "日盘")

    def test_boundary_1500_is_day(self):
        """边界：15:00 → 日盘"""
        self.assertEqual(_session_of("2026-01-15 15:00:00"), "日盘")

    def test_boundary_2100_is_night(self):
        """边界：21:00 → 夜盘"""
        self.assertEqual(_session_of("2026-01-15 21:00:00"), "夜盘")

    def test_boundary_0230_is_night(self):
        """边界：02:30 → 夜盘"""
        self.assertEqual(_session_of("2026-01-16 02:30:00"), "夜盘")

    def test_empty_string(self):
        """空串 → 其他"""
        self.assertEqual(_session_of(""), "其他")

    def test_none(self):
        """None → 其他"""
        self.assertEqual(_session_of(None), "其他")

    def test_short_string(self):
        """短字符串 → 其他"""
        self.assertEqual(_session_of("09:30"), "其他")

    def test_invalid_time(self):
        """非法时间 → 其他"""
        self.assertEqual(_session_of("2026-01-15 xx:yy:zz"), "其他")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  交易日记工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
