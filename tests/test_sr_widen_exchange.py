#!/usr/bin/env python3
"""
SR 放宽 + 行情时段 + 交易所分类 — 单元测试
======================================================

1. widen_stop_with_sr — 用 SR 位放宽止损
   - 无 SR → 原样返回
   - 多单：支撑位下方且在 max_mult 内 → 放宽到支撑位
   - 多单：支撑位太远(超max_mult) → 不放宽
   - 多单：支撑位更近(小于原止损) → 不放宽
   - 空单：压力位上方且在 max_mult 内 → 放宽到压力位
   - 空单：压力位太远 → 不放宽
   - 方向=0 → 不放宽
   - stop_dist=0 → 不放宽
   - 返回 dict，3 个字段：stop / stop_dist / sr_stop_widen

2. _classify_exchange — 交易所分类
   - 郑商所品种 → CZCE
   - 大商所品种 → DCE
   - 上期所品种 → SHFE
   - 上期能源 → INE
   - 广期所 → GFEX
   - 中金所 → CFFEX
   - 主连 M 后缀 → 正确剥离
   - 未知品种 → 其他
   - 合约数字后缀 → 正确剥离

3. _in_trading_session — 交易时段判断
   - 周一上午 → True
   - 周一夜盘 → True
   - 午休 → False
   - 收盘后 → False
   - 周六 → False
   - 周日 → False
   - 下午盘 → True
   - 夜盘 → True
   - 夜盘后 → False

4. _is_rate_limit — 限流识别
   - 429 错误 → True
   - "调用次数超过限制" → True
   - "超过限制" → True
   - 普通错误 → False
   - None → False
   - 空串 → False
"""

import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from minishare_feed import _classify_exchange, _in_trading_session, _is_rate_limit
from sr_widen_sweep import widen_stop_with_sr

# ═══════════════════════════════════════════════════════════════════════════
#  1. widen_stop_with_sr
# ═══════════════════════════════════════════════════════════════════════════


class TestWidenStopWithSr(unittest.TestCase):
    """widen_stop_with_sr 用 SR 位放宽止损。"""

    def _base_exit(self, stop=95.0, stop_dist=5.0):
        return {"stop": stop, "stop_dist": stop_dist, "target": 110.0}

    def _sr_support(self, price=93.0):
        return {
            "levels": [{"price": 93.0, "kind": "support"}],
            "nearest_support": {"price": price, "distance_pct": 2.0},
            "nearest_resistance": {"price": 105.0, "distance_pct": 3.0},
        }

    def _sr_resistance(self, price=107.0):
        return {
            "levels": [{"price": 107.0, "kind": "resistance"}],
            "nearest_support": {"price": 93.0, "distance_pct": 2.0},
            "nearest_resistance": {"price": price, "distance_pct": 3.0},
        }

    def test_no_sr_unchanged(self):
        """无 SR → 原样返回"""
        exit_d = self._base_exit()
        result = widen_stop_with_sr(exit_d, None, 1, 100.0)
        self.assertEqual(result, exit_d)
        self.assertNotIn("sr_stop_widen", result)

    def test_no_levels_unchanged(self):
        """levels 为空 → 原样返回"""
        exit_d = self._base_exit()
        sr = {"levels": []}
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0)
        self.assertEqual(result["stop"], 95.0)

    def test_long_widen_to_support(self):
        """多单：支撑位下方且在 max_mult 内 → 放宽到支撑位"""
        # entry=100, stop=95 (stop_dist=5)
        # nearest_support=93 → sr_dist=7
        # 7 > 5 且 7 <= 10 (max_mult=2.0, max_widen=10) → 放宽
        exit_d = self._base_exit()
        sr = self._sr_support(price=93.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(result["stop"], 93.0)
        self.assertEqual(result["stop_dist"], 7.0)
        self.assertTrue(result["sr_stop_widen"])

    def test_long_support_too_far(self):
        """多单：支撑位太远(超max_mult) → 不放宽"""
        # stop_dist=5, max_widen=10
        # support=88 → sr_dist=12 > 10 → 不放宽
        exit_d = self._base_exit()
        sr = self._sr_support(price=88.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(result["stop"], 95.0)
        self.assertNotIn("sr_stop_widen", result)

    def test_long_support_closer(self):
        """多单：支撑位更近(小于原止损) → 不放宽"""
        # stop=95 (dist=5), support=97 → dist=3 < 5 → 不放宽
        # 不对，支撑位应该在入场下方
        # 支撑位 98 也在入场下方，但 dist=2 < 5
        exit_d = self._base_exit()
        sr = self._sr_support(price=98.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        # sr_dist = 100-98 = 2 < 5 → 不放宽（因为条件是 sr_dist > stop_dist）
        self.assertEqual(result["stop"], 95.0)

    def test_short_widen_to_resistance(self):
        """空单：压力位上方且在 max_mult 内 → 放宽到压力位"""
        # entry=100, stop=105 (stop_dist=5)
        # resistance=107 → sr_dist=7
        # 7 > 5 且 7 <= 10 → 放宽
        exit_d = {"stop": 105.0, "stop_dist": 5.0, "target": 90.0}
        sr = self._sr_resistance(price=107.0)
        result = widen_stop_with_sr(exit_d, sr, -1, 100.0, max_mult=2.0)
        self.assertEqual(result["stop"], 107.0)
        self.assertEqual(result["stop_dist"], 7.0)
        self.assertTrue(result["sr_stop_widen"])

    def test_short_resistance_too_far(self):
        """空单：压力位太远 → 不放宽"""
        exit_d = {"stop": 105.0, "stop_dist": 5.0, "target": 90.0}
        sr = self._sr_resistance(price=115.0)  # dist=15 > 10
        result = widen_stop_with_sr(exit_d, sr, -1, 100.0, max_mult=2.0)
        self.assertEqual(result["stop"], 105.0)

    def test_direction_zero_no_change(self):
        """方向=0 → 不放宽"""
        exit_d = self._base_exit()
        sr = self._sr_support()
        result = widen_stop_with_sr(exit_d, sr, 0, 100.0)
        self.assertEqual(result["stop"], 95.0)

    def test_stop_dist_zero_no_change(self):
        """stop_dist=0 → 不放宽"""
        exit_d = {"stop": 100.0, "stop_dist": 0.0}
        sr = self._sr_support()
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0)
        self.assertEqual(result["stop"], 100.0)

    def test_returns_dict(self):
        """返回 dict"""
        exit_d = self._base_exit()
        self.assertIsInstance(widen_stop_with_sr(exit_d, None, 1, 100.0), dict)

    def test_original_not_mutated(self):
        """原 exit_dict 不被修改"""
        exit_d = self._base_exit()
        original_stop = exit_d["stop"]
        sr = self._sr_support(price=93.0)
        widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        # 函数内部用 dict() 复制，原 dict 不变
        self.assertEqual(exit_d["stop"], original_stop)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _classify_exchange
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyExchange(unittest.TestCase):
    """_classify_exchange 交易所分类。"""

    def test_czce_variety(self):
        """郑商所品种 → CZCE"""
        self.assertEqual(_classify_exchange("FG2608"), "CZCE")
        self.assertEqual(_classify_exchange("SA2609"), "CZCE")
        self.assertEqual(_classify_exchange("MA301"), "CZCE")

    def test_dce_variety(self):
        """大商所品种 → DCE"""
        self.assertEqual(_classify_exchange("m2609"), "DCE")
        self.assertEqual(_classify_exchange("j2609"), "DCE")

    def test_shfe_variety(self):
        """上期所品种 → SHFE"""
        self.assertEqual(_classify_exchange("CU2609"), "SHFE")
        self.assertEqual(_classify_exchange("RU2609"), "SHFE")

    def test_ine_variety(self):
        """上期能源 → INE"""
        self.assertEqual(_classify_exchange("sc2609"), "INE")

    def test_cffex_variety(self):
        """中金所 → CFFEX"""
        self.assertEqual(_classify_exchange("IF2609"), "CFFEX")
        self.assertEqual(_classify_exchange("IM2609"), "CFFEX")

    def test_gfex_variety(self):
        """广期所 → GFEX"""
        self.assertEqual(_classify_exchange("SI2609"), "GFEX")
        self.assertEqual(_classify_exchange("LC2609"), "GFEX")

    def test_main_contract_m_stripped(self):
        """主连 M 后缀 → 正确剥离"""
        # JMM → JM → DCE
        result = _classify_exchange("JMM")
        self.assertEqual(result, "DCE")

    def test_unknown_returns_other(self):
        """未知品种 → 其他"""
        self.assertEqual(_classify_exchange("XYZ2609"), "其他")

    def test_contract_digits_stripped(self):
        """合约数字后缀 → 正确剥离"""
        self.assertEqual(_classify_exchange("FG608"), "CZCE")
        self.assertEqual(_classify_exchange("CU610"), "SHFE")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_classify_exchange("FG2608"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _in_trading_session
# ═══════════════════════════════════════════════════════════════════════════


class TestInTradingSession(unittest.TestCase):
    """_in_trading_session 交易时段判断。"""

    def test_monday_morning_true(self):
        """周一上午 → True"""
        dt = datetime(2026, 8, 31, 10, 0, 0)  # 周一
        self.assertEqual(dt.weekday(), 0)
        self.assertTrue(_in_trading_session(dt))

    def test_monday_lunch_false(self):
        """午休 → False"""
        dt = datetime(2026, 8, 31, 12, 0, 0)  # 周一 12:00
        self.assertFalse(_in_trading_session(dt))

    def test_monday_afternoon_true(self):
        """下午盘 → True"""
        dt = datetime(2026, 8, 31, 14, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_monday_night_true(self):
        """夜盘 → True"""
        dt = datetime(2026, 8, 31, 22, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_after_close_false(self):
        """收盘后 → False"""
        dt = datetime(2026, 8, 31, 16, 0, 0)
        self.assertFalse(_in_trading_session(dt))

    def test_saturday_false(self):
        """周六 → False"""
        dt = datetime(2026, 8, 29, 10, 0, 0)  # 周六
        self.assertEqual(dt.weekday(), 5)
        self.assertFalse(_in_trading_session(dt))

    def test_sunday_false(self):
        """周日 → False"""
        dt = datetime(2026, 8, 30, 10, 0, 0)  # 周日
        self.assertEqual(dt.weekday(), 6)
        self.assertFalse(_in_trading_session(dt))

    def test_night_after_close_false(self):
        """夜盘后 → False"""
        dt = datetime(2026, 8, 31, 23, 30, 0)
        self.assertFalse(_in_trading_session(dt))

    def test_boundary_9am_true(self):
        """9:00 整 → True"""
        dt = datetime(2026, 8, 31, 9, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_boundary_1130_true(self):
        """11:30 整 → True"""
        dt = datetime(2026, 8, 31, 11, 30, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_boundary_1330_true(self):
        """13:30 整 → True"""
        dt = datetime(2026, 8, 31, 13, 30, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_boundary_1500_true(self):
        """15:00 整 → True"""
        dt = datetime(2026, 8, 31, 15, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_boundary_2100_true(self):
        """21:00 整 → True"""
        dt = datetime(2026, 8, 31, 21, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_boundary_2300_true(self):
        """23:00 整 → True"""
        dt = datetime(2026, 8, 31, 23, 0, 0)
        self.assertTrue(_in_trading_session(dt))

    def test_friday_night_session(self):
        """周五夜盘 → True"""
        dt = datetime(2026, 9, 4, 22, 0, 0)  # 周五
        self.assertEqual(dt.weekday(), 4)
        self.assertTrue(_in_trading_session(dt))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _is_rate_limit
# ═══════════════════════════════════════════════════════════════════════════


class TestIsRateLimit(unittest.TestCase):
    """_is_rate_limit 限流识别。"""

    def test_code_429_true(self):
        """429 错误 → True"""
        self.assertTrue(_is_rate_limit("HTTP 429 Too Many Requests"))

    def test_chinese_limit_true(self):
        """ "调用次数超过限制" → True"""
        self.assertTrue(_is_rate_limit("调用次数超过限制"))

    def test_short_limit_true(self):
        """ "超过限制" → True"""
        self.assertTrue(_is_rate_limit("超过限制，请明日再试"))

    def test_normal_error_false(self):
        """普通错误 → False"""
        self.assertFalse(_is_rate_limit("连接超时"))
        self.assertFalse(_is_rate_limit("500 Internal Server Error"))

    def test_none_false(self):
        """None → False"""
        self.assertFalse(_is_rate_limit(None))

    def test_empty_false(self):
        """空串 → False"""
        self.assertFalse(_is_rate_limit(""))

    def test_returns_bool(self):
        """返回 bool"""
        self.assertIsInstance(_is_rate_limit("429"), bool)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR 放宽 + 行情时段 + 交易所分类 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
