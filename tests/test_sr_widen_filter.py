#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SR 止损放宽与过滤模拟 — 单元测试
=====================================

1. widen_stop_with_sr — 用 SR 位放宽止损
   - 无 SR 数据 → 不修改
   - levels 为空 → 不修改
   - 多单：最近支撑位在止损外且在 max_mult 内 → 放宽止损
   - 多单：支撑位太近（比原止损还近）→ 不修改
   - 多单：支撑位太远（超 max_mult）→ 不修改
   - 空单：最近阻力位在止损外且在 max_mult 内 → 放宽止损
   - 空单：阻力位太近 → 不修改
   - 空单：阻力位太远 → 不修改
   - direction = 0 → 不修改
   - stop_dist = 0 → 不修改（不除以零）
   - 不修改原 exit_dict（返回副本）
   - 放宽后设置 sr_stop_widen=True

2. simulate_filter — 模拟过滤效果
   - 全保留 → expR 不变，filtered=0
   - 全过滤 → trades=0, expR=0, win_rate=0
   - 部分过滤 → 只统计保留的
   - 空列表 → trades=0
   - expR 保留 4 位小数
   - win_rate 保留 4 位小数
   - win_rate = 盈利笔数 / 总笔数
   - filtered = 过滤掉的数量
   - name 字段正确传递
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sr_filter_simulate import simulate_filter
from sr_widen_sweep import widen_stop_with_sr

# ═══════════════════════════════════════════════════════════════════════════
#  1. widen_stop_with_sr
# ═══════════════════════════════════════════════════════════════════════════

class TestWidenStopWithSr(unittest.TestCase):
    """widen_stop_with_sr 用 SR 位放宽止损。"""

    def _make_exit(self, stop_dist=10.0, stop=90.0):
        return {"stop_dist": stop_dist, "stop": stop, "target": 120.0}

    def _make_sr(self, ns_price=None, nr_price=None):
        """构造 SR 结果"""
        levels = []
        result = {"levels": levels}
        if ns_price is not None:
            result["nearest_support"] = {"price": ns_price, "strength": 3}
            levels.append({"price": ns_price, "type": "support"})
        if nr_price is not None:
            result["nearest_resistance"] = {"price": nr_price, "strength": 3}
            levels.append({"price": nr_price, "type": "resistance"})
        return result

    def test_no_sr_unchanged(self):
        """无 SR 数据 → 不修改"""
        exit_d = self._make_exit()
        result = widen_stop_with_sr(exit_d, None, 1, 100.0)
        self.assertEqual(result, exit_d)

    def test_empty_levels_unchanged(self):
        """levels 为空 → 不修改"""
        exit_d = self._make_exit()
        sr = {"levels": []}
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0)
        self.assertEqual(result, exit_d)

    def test_long_support_widens_stop(self):
        """多单：最近支撑位在止损外且在 max_mult 内 → 放宽止损"""
        # entry=100, 原 stop_dist=10 → 止损 90
        # 最近支撑 = 85 → sr_dist = 15 > 10 且 ≤ 20 (max_mult=2.0)
        exit_d = self._make_exit(stop_dist=10.0, stop=90.0)
        sr = self._make_sr(ns_price=85.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertAlmostEqual(result["stop"], 85.0, places=2)
        self.assertAlmostEqual(result["stop_dist"], 15.0, places=2)
        self.assertTrue(result["sr_stop_widen"])

    def test_long_support_too_close_no_change(self):
        """多单：支撑位太近（比原止损还近）→ 不修改"""
        # entry=100, 原 stop=90 (dist=10)
        # 支撑位 = 95 → sr_dist = 5 < 10 → 不放宽（止损更紧了，不是放宽）
        exit_d = self._make_exit(stop_dist=10.0, stop=90.0)
        sr = self._make_sr(ns_price=95.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(result, exit_d)
        self.assertNotIn("sr_stop_widen", result)

    def test_long_support_too_far_no_change(self):
        """多单：支撑位太远（超 max_mult）→ 不修改"""
        # entry=100, stop_dist=10, max_mult=2 → 最远放宽到 20
        # 支撑位 = 70 → sr_dist = 30 > 20 → 不放宽
        exit_d = self._make_exit(stop_dist=10.0, stop=90.0)
        sr = self._make_sr(ns_price=70.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(result, exit_d)

    def test_short_resistance_widens_stop(self):
        """空单：最近阻力位在止损外且在 max_mult 内 → 放宽止损"""
        # entry=100, 原 stop_dist=10 → 止损 110
        # 最近阻力 = 115 → sr_dist = 15 > 10 且 ≤ 20
        exit_d = self._make_exit(stop_dist=10.0, stop=110.0)
        sr = self._make_sr(nr_price=115.0)
        result = widen_stop_with_sr(exit_d, sr, -1, 100.0, max_mult=2.0)
        self.assertAlmostEqual(result["stop"], 115.0, places=2)
        self.assertAlmostEqual(result["stop_dist"], 15.0, places=2)
        self.assertTrue(result["sr_stop_widen"])

    def test_short_resistance_too_close_no_change(self):
        """空单：阻力位太近 → 不修改"""
        # entry=100, 原 stop=110 (dist=10)
        # 阻力位 = 105 → sr_dist = 5 < 10 → 不放宽
        exit_d = self._make_exit(stop_dist=10.0, stop=110.0)
        sr = self._make_sr(nr_price=105.0)
        result = widen_stop_with_sr(exit_d, sr, -1, 100.0, max_mult=2.0)
        self.assertEqual(result, exit_d)

    def test_short_resistance_too_far_no_change(self):
        """空单：阻力位太远 → 不修改"""
        # entry=100, stop_dist=10, max_mult=2 → 最远放宽到 20
        # 阻力位 = 130 → sr_dist = 30 > 20 → 不放宽
        exit_d = self._make_exit(stop_dist=10.0, stop=110.0)
        sr = self._make_sr(nr_price=130.0)
        result = widen_stop_with_sr(exit_d, sr, -1, 100.0, max_mult=2.0)
        self.assertEqual(result, exit_d)

    def test_direction_zero_unchanged(self):
        """direction = 0 → 不修改"""
        exit_d = self._make_exit()
        sr = self._make_sr(ns_price=85.0)
        result = widen_stop_with_sr(exit_d, sr, 0, 100.0)
        self.assertEqual(result, exit_d)

    def test_zero_stop_dist_unchanged(self):
        """stop_dist = 0 → 不修改"""
        exit_d = self._make_exit(stop_dist=0.0)
        sr = self._make_sr(ns_price=95.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0)
        self.assertEqual(result, exit_d)

    def test_does_not_mutate_original(self):
        """不修改原 exit_dict（返回副本）"""
        exit_d = self._make_exit(stop_dist=10.0, stop=90.0)
        original = dict(exit_d)
        sr = self._make_sr(ns_price=85.0)
        widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(exit_d, original)

    def test_at_max_mult_boundary(self):
        """刚好 = max_mult → 放宽（<= 边界）"""
        # stop_dist=10, max_mult=2 → max_widen_dist=20
        # 支撑位 sr_dist=20 → 刚好等于上限 → 应该放宽
        exit_d = self._make_exit(stop_dist=10.0, stop=90.0)
        sr = self._make_sr(ns_price=80.0)  # 100-80 = 20
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertAlmostEqual(result["stop_dist"], 20.0, places=2)
        self.assertTrue(result["sr_stop_widen"])

    def test_other_keys_preserved(self):
        """其他字段保留"""
        exit_d = {"stop_dist": 10.0, "stop": 90.0, "target": 120.0, "rr": 2.0}
        sr = self._make_sr(ns_price=85.0)
        result = widen_stop_with_sr(exit_d, sr, 1, 100.0, max_mult=2.0)
        self.assertEqual(result["target"], 120.0)
        self.assertEqual(result["rr"], 2.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. simulate_filter
# ═══════════════════════════════════════════════════════════════════════════

class TestSimulateFilter(unittest.TestCase):
    """simulate_filter 模拟过滤效果。"""

    def _make_trades(self, rs):
        """构造交易列表，rs 是 R_adj 列表"""
        return [{"R_adj": r, "symbol": "rb"} for r in rs]

    def test_all_kept(self):
        """全保留 → expR 不变，filtered=0"""
        trades = self._make_trades([1.0, 2.0, -0.5])
        result = simulate_filter(trades, "test_filter", lambda t: False)
        self.assertEqual(result["trades"], 3)
        self.assertEqual(result["filtered"], 0)
        # expR = (1+2-0.5)/3 = 2.5/3 = 0.8333
        self.assertAlmostEqual(result["expR"], 0.8333, places=4)

    def test_all_filtered(self):
        """全过滤 → trades=0, expR=0, win_rate=0"""
        trades = self._make_trades([1.0, 2.0, -0.5])
        result = simulate_filter(trades, "test_filter", lambda t: True)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["filtered"], 3)
        self.assertEqual(result["expR"], 0)
        self.assertEqual(result["win_rate"], 0)

    def test_partial_filter(self):
        """部分过滤 → 只统计保留的"""
        trades = self._make_trades([2.0, -1.0, 1.5, -0.5, 3.0])
        # 过滤掉亏损的（R_adj < 0）
        result = simulate_filter(trades, "no_loss", lambda t: t["R_adj"] < 0)
        # 保留：2.0, 1.5, 3.0 → 3 笔
        # 过滤：-1.0, -0.5 → 2 笔
        self.assertEqual(result["trades"], 3)
        self.assertEqual(result["filtered"], 2)
        # expR = (2+1.5+3)/3 = 6.5/3 = 2.1667
        self.assertAlmostEqual(result["expR"], 2.1667, places=4)
        # win_rate = 3/3 = 1.0
        self.assertAlmostEqual(result["win_rate"], 1.0, places=4)

    def test_empty_list(self):
        """空列表 → trades=0"""
        result = simulate_filter([], "empty", lambda t: True)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["filtered"], 0)
        self.assertEqual(result["expR"], 0)
        self.assertEqual(result["win_rate"], 0)

    def test_expR_rounds_4_decimals(self):
        """expR 保留 4 位小数"""
        trades = self._make_trades([1.0, 1.0, 1.0, -1.0])
        result = simulate_filter(trades, "test", lambda t: False)
        # expR = 2/4 = 0.5 → 精确
        self.assertEqual(result["expR"], 0.5)
        # 检查是 float 且有合理精度
        self.assertIsInstance(result["expR"], float)

    def test_win_rate_correct(self):
        """win_rate = 盈利笔数 / 总笔数"""
        trades = self._make_trades([1.0, -1.0, 0.5, -0.3, 2.0])
        result = simulate_filter(trades, "test", lambda t: False)
        # 盈利：1.0, 0.5, 2.0 → 3 笔
        # 总笔数：5
        self.assertAlmostEqual(result["win_rate"], 0.6, places=4)

    def test_filtered_count(self):
        """filtered = 过滤掉的数量"""
        trades = self._make_trades(range(10))
        # 过滤掉偶数
        result = simulate_filter(trades, "even", lambda t: t["R_adj"] % 2 == 0)
        # 0,2,4,6,8 → 5 个被过滤
        self.assertEqual(result["filtered"], 5)
        self.assertEqual(result["trades"], 5)

    def test_name_passed_through(self):
        """name 字段正确传递"""
        trades = self._make_trades([1.0])
        result = simulate_filter(trades, "my_special_filter", lambda t: False)
        self.assertEqual(result["name"], "my_special_filter")

    def test_all_winning_win_rate_one(self):
        """全赚 → win_rate = 1.0"""
        trades = self._make_trades([0.5, 1.0, 2.0])
        result = simulate_filter(trades, "test", lambda t: False)
        self.assertEqual(result["win_rate"], 1.0)

    def test_all_losing_win_rate_zero(self):
        """全亏 → win_rate = 0"""
        trades = self._make_trades([-0.5, -1.0, -2.0])
        result = simulate_filter(trades, "test", lambda t: False)
        self.assertEqual(result["win_rate"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR 止损放宽与过滤模拟 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
