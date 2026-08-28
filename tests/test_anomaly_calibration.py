#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异动扫描 + 校准工具 — 单元测试
===================================

1. anomaly_scan.compute — 异动扫描
   - 空数据 → ok=False
   - 正常数据 → 计算涨跌幅、振幅、异动分
   - 领涨榜按 pct 降序
   - 领跌榜按 pct 升序
   - top_n 参数限制数量
   - pre_close 优先于 open 计算涨跌幅
   - 无效数据被过滤（None/0）
   - 异常数据不崩溃

2. _future_close — 未来收盘价
   - 找到第一根 >= 窗口结束时间的 K 线收盘价
   - 所有 K 线都在窗口前 → None
   - 第一根就满足 → 返回第一根
   - 精确匹配时间点
   - 空列表 → None
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from anomaly_scan import W_AMP, W_PCT
from anomaly_scan import compute as anomaly_compute
from calibration import _future_close

# ═══════════════════════════════════════════════════════════════════════════
#  1. anomaly_scan.compute
# ═══════════════════════════════════════════════════════════════════════════


class TestAnomalyScan(unittest.TestCase):
    """anomaly_scan.compute 异动扫描。"""

    def test_empty_data_not_ok(self):
        """空数据 → ok=False"""
        result = anomaly_compute({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["top_up"], [])
        self.assertEqual(result["top_down"], [])

    def test_normal_data_computes_correctly(self):
        """正常数据 → 正确计算涨跌幅、振幅、异动分"""
        snaps = {
            "rb": {"close": 3300, "open": 3200, "high": 3350, "low": 3180, "name": "螺纹"},
        }
        result = anomaly_compute(snaps)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)

        rec = result["by_symbol"]["rb"]
        # 涨跌幅 = (3300-3200)/3200 * 100 = 3.125%
        expected_pct = (3300 - 3200) / 3200 * 100
        self.assertAlmostEqual(rec["pct"], expected_pct, places=1)
        # 振幅 = (3350-3180)/3200 * 100 = 5.3125%
        expected_amp = (3350 - 3180) / 3200 * 100
        self.assertAlmostEqual(rec["amp"], expected_amp, places=1)
        # 异动分 = W_PCT * |pct| + W_AMP * amp
        expected_score = round(W_PCT * abs(expected_pct) + W_AMP * expected_amp, 2)
        self.assertEqual(rec["score"], expected_score)

    def test_top_up_sorted_descending(self):
        """领涨榜按 pct 降序"""
        snaps = {
            "up_big": {"close": 110, "open": 100, "high": 112, "low": 98},  # +10%
            "up_small": {"close": 103, "open": 100, "high": 105, "low": 99},  # +3%
            "down": {"close": 95, "open": 100, "high": 101, "low": 94},  # -5%
        }
        result = anomaly_compute(snaps)
        top_up = result["top_up"]
        self.assertGreater(top_up[0]["pct"], top_up[1]["pct"])
        self.assertGreater(top_up[1]["pct"], top_up[2]["pct"])

    def test_top_down_sorted_ascending(self):
        """领跌榜按 pct 升序（最跌的在前）"""
        snaps = {
            "up": {"close": 110, "open": 100, "high": 112, "low": 98},  # +10%
            "down_small": {"close": 97, "open": 100, "high": 101, "low": 96},  # -3%
            "down_big": {"close": 90, "open": 100, "high": 101, "low": 89},  # -10%
        }
        result = anomaly_compute(snaps)
        top_down = result["top_down"]
        # 最跌的在前
        self.assertLess(top_down[0]["pct"], top_down[1]["pct"])

    def test_top_n_limits_count(self):
        """top_n 参数限制上榜数量"""
        snaps = {}
        for i in range(20):
            snaps[f"sym{i}"] = {
                "close": 100 + i,
                "open": 100,
                "high": 100 + i + 2,
                "low": 98 + i,
            }
        result = anomaly_compute(snaps, top_n=5)
        self.assertEqual(len(result["top_up"]), 5)
        self.assertEqual(len(result["top_down"]), 5)

    def test_pre_close_overrides_open(self):
        """pre_close 优先于 open 计算涨跌幅"""
        snaps = {
            "rb": {"close": 3300, "open": 3200, "high": 3350, "low": 3180},
        }
        pre_close = {"rb": 3100}  # 昨收 3100
        result = anomaly_compute(snaps, pre_close_map=pre_close)
        rec = result["by_symbol"]["rb"]
        # 用昨收算：(3300-3100)/3100 * 100 ≈ 6.45%
        # 用开盘算：3.125%
        expected_with_pre = (3300 - 3100) / 3100 * 100
        self.assertAlmostEqual(rec["pct"], expected_with_pre, places=1)

    def test_invalid_data_filtered(self):
        """无效数据被过滤（close=0 或 None）"""
        snaps = {
            "valid": {"close": 105, "open": 100, "high": 106, "low": 99},
            "zero_close": {"close": 0, "open": 100, "high": 101, "low": 99},
            "none_close": {"close": None, "open": 100, "high": 101, "low": 99},
            "bad_string": {"close": "abc", "open": 100, "high": 101, "low": 99},
        }
        result = anomaly_compute(snaps)
        self.assertEqual(result["total"], 1)  # 只有 valid
        self.assertIn("valid", result["by_symbol"])

    def test_flat_market_zero_score(self):
        """平开平收 → 涨跌幅=0，振幅=0，异动分=0"""
        snaps = {
            "flat": {"close": 100, "open": 100, "high": 100, "low": 100},
        }
        result = anomaly_compute(snaps)
        rec = result["by_symbol"]["flat"]
        self.assertEqual(rec["pct"], 0.0)
        self.assertEqual(rec["amp"], 0.0)
        self.assertEqual(rec["score"], 0.0)

    def test_big_move_high_score(self):
        """大波动 → 异动分高"""
        snaps = {
            "calm": {"close": 101, "open": 100, "high": 101.5, "low": 99.5},
            "wild": {"close": 110, "open": 100, "high": 115, "low": 90},
        }
        result = anomaly_compute(snaps)
        self.assertGreater(result["by_symbol"]["wild"]["score"], result["by_symbol"]["calm"]["score"])

    def test_by_symbol_dict_access(self):
        """by_symbol 可以按品种名访问"""
        snaps = {
            "rb": {"close": 3300, "open": 3200, "high": 3350, "low": 3180, "name": "螺纹"},
        }
        result = anomaly_compute(snaps)
        self.assertIn("rb", result["by_symbol"])
        self.assertEqual(result["by_symbol"]["rb"]["symbol"], "rb")
        self.assertEqual(result["by_symbol"]["rb"]["name"], "螺纹")


# ═══════════════════════════════════════════════════════════════════════════
#  2. _future_close
# ═══════════════════════════════════════════════════════════════════════════


class TestFutureClose(unittest.TestCase):
    """_future_close 未来收盘价。"""

    def test_finds_first_after_window(self):
        """找到第一根 >= 窗口结束时间的 K 线收盘价"""
        bars = [
            (10, 100),
            (20, 105),
            (30, 110),
            (40, 115),
        ]
        # 窗口结束在 25，第一根 >= 25 的是第 3 根（时间 30，价 110）
        self.assertEqual(_future_close(bars, 25), 110)

    def test_all_before_window_returns_none(self):
        """所有 K 线都在窗口前 → None"""
        bars = [
            (10, 100),
            (20, 105),
            (30, 110),
        ]
        self.assertIsNone(_future_close(bars, 50))

    def test_first_bar_satisfies(self):
        """第一根就满足 → 返回第一根"""
        bars = [
            (10, 100),
            (20, 105),
        ]
        self.assertEqual(_future_close(bars, 5), 100)

    def test_exact_match(self):
        """精确匹配时间点"""
        bars = [
            (10, 100),
            (20, 105),
            (30, 110),
        ]
        self.assertEqual(_future_close(bars, 20), 105)

    def test_empty_list_returns_none(self):
        """空列表 → None"""
        self.assertIsNone(_future_close([], 10))

    def test_single_bar_before(self):
        """单根在窗口前 → None"""
        self.assertIsNone(_future_close([(5, 100)], 10))

    def test_single_bar_after(self):
        """单根在窗口后 → 返回它"""
        self.assertEqual(_future_close([(15, 100)], 10), 100)

    def test_datetime_objects(self):
        """也可以用 datetime 对象（只要支持 >= 比较）"""
        from datetime import datetime

        t1 = datetime(2026, 1, 15, 9, 30)
        t2 = datetime(2026, 1, 15, 10, 30)
        t3 = datetime(2026, 1, 15, 11, 30)
        bars = [(t1, 100), (t2, 105), (t3, 110)]
        window_end = datetime(2026, 1, 15, 10, 0)
        self.assertEqual(_future_close(bars, window_end), 105)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  异动扫描 + 校准工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
