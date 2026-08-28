#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
5m 出场模拟 — 单元测试
=========================

测试 _sim_exit_5m — 逐 bar 模拟 stop / t2 / 尾仓出场。

1. 止损出场
   - 多单：low <= stop → 止损离场
   - 空单：high >= stop → 止损离场

2. 止盈出场（无尾仓）
   - 多单：high >= t2 → 止盈 2R
   - 空单：low <= t2 → 止盈 2R

3. 尾仓出场（有尾仓）
   - 先触 t2 → 激活尾仓，尾仓止损 = t2 - tail_stop_dist
   - 然后回撤触尾仓止损 → 尾仓离场
   - 尾仓止损 ratchet（只向有利方向移动）

4. 期末平仓
   - 所有 bar 走完未触发 → 最后一根收盘价平仓

5. 返回值结构
   - (exit_price, reason, exit_idx) 三元组
   - exit_idx 是退出 bar 的索引（从 0 开始）

历史背景：
  P-G 尾仓机制 — 触 T2 不全平，留尾仓用移动止损跟趋势，
  日线回测看不到盘中回撤，必须用 5m 细粒度验证。
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import _sim_exit_5m


def _make_df5(bars):
    """从 (open, high, low, close) 列表构造 5m DataFrame。"""
    idx = pd.date_range("2026-06-01 09:00", periods=len(bars), freq="5min")
    return pd.DataFrame(
        [{"open": b[0], "high": b[1], "low": b[2], "close": b[3]} for b in bars],
        index=idx,
    )


def _make_ep(stop=95, t1=105, t2=110, tail_enabled=False, tail_stop_dist=10):
    """构造 exit_plan 风格的 dict。"""
    stop_dist = 5.0  # entry(100) - stop(95) = 5
    return {
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "stop_dist": stop_dist,
        "trailing": tail_enabled,
        "tail_enabled": tail_enabled,
        "tail_stop_dist": tail_stop_dist,
        "tail_pct": 0.25,
        "sr_note": "",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  1. 止损出场
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mStopLoss(unittest.TestCase):
    """止损出场。"""

    def test_long_stop_loss_first_bar(self):
        """多单：第一根就触止损"""
        # 开盘直接跌破 stop
        bars = [(100, 101, 90, 92)]  # low=90 < stop=95
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(exit_price, 95)
        self.assertEqual(reason, "止损")
        self.assertEqual(exit_idx, 0)

    def test_short_stop_loss_first_bar(self):
        """空单：第一根就触止损"""
        bars = [(100, 110, 98, 108)]  # high=110 > stop=105
        df = _make_df5(bars)
        ep = _make_ep(stop=105, t2=90)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=-1, entry=100, ep=ep, sd=0)
        self.assertEqual(exit_price, 105)
        self.assertEqual(reason, "止损")
        self.assertEqual(exit_idx, 0)

    def test_long_stop_loss_third_bar(self):
        """多单：第三根触止损"""
        bars = [
            (100, 102, 98, 101),  # 正常
            (101, 103, 99, 102),  # 正常
            (102, 103, 94, 95),  # low=94 < stop=95 → 止损
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "止损")
        self.assertEqual(exit_idx, 2)

    def test_long_no_stop_when_price_above_stop(self):
        """多单：价格一直在止损上方 → 不止损"""
        bars = [
            (100, 102, 98, 101),  # low=98 > stop=95
            (101, 103, 99, 102),
            (102, 104, 100, 103),
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=120)  # t2 也没到
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        # 没触止损也没触止盈 → 期末平
        self.assertEqual(reason, "期末平")
        self.assertEqual(exit_idx, 2)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 止盈出场（无尾仓）
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mTakeProfit(unittest.TestCase):
    """止盈出场（无尾仓）。"""

    def test_long_t2_take_profit(self):
        """多单：触 t2 → 止盈 2R"""
        bars = [
            (100, 105, 99, 104),  # 接近但没到
            (104, 112, 103, 111),  # high=112 >= t2=110 → 止盈
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=False)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(exit_price, 110)
        self.assertEqual(reason, "止盈2R")
        self.assertEqual(exit_idx, 1)

    def test_short_t2_take_profit(self):
        """空单：触 t2 → 止盈 2R"""
        bars = [
            (100, 102, 96, 97),  # 接近
            (97, 98, 88, 89),  # low=88 <= t2=90 → 止盈
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=105, t2=90, tail_enabled=False)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=-1, entry=100, ep=ep, sd=0)
        self.assertEqual(exit_price, 90)
        self.assertEqual(reason, "止盈2R")
        self.assertEqual(exit_idx, 1)

    def test_stop_before_t2(self):
        """止损比止盈先到 → 止损出场（不是止盈）"""
        bars = [
            (100, 101, 90, 92),  # 先触止损（low=90 < stop=95）
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=False)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "止损")
        self.assertNotEqual(reason, "止盈2R")


# ═══════════════════════════════════════════════════════════════════════════
#  3. 尾仓出场
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mTrailingTail(unittest.TestCase):
    """尾仓移动止损出场。"""

    def test_long_tail_activated_then_stopped(self):
        """多单：先触 t2 激活尾仓，然后回撤触尾仓止损

        流程：
        - Bar 1 触 t2 → 激活尾仓，尾仓止损 = t2 - tail_stop_dist = 110 - 10 = 100
        - Bar 2 进入尾仓模式：low=98 <= 100 → 尾仓离场
        """
        bars = [
            (100, 105, 99, 104),  # 正常
            (104, 115, 103, 114),  # 触 t2 → 激活尾仓，尾仓止损 = 100
            (114, 116, 98, 105),  # 回撤，low=98 <= 尾仓止损100 → 尾仓离场
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=True, tail_stop_dist=10)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "尾仓离场")
        self.assertEqual(exit_idx, 2)
        self.assertEqual(exit_price, 100)  # 尾仓止损价

    def test_long_tail_stop_ratchets_up(self):
        """多单：尾仓止损只向上移动（ratchet 机制）

        价格创新高 → 尾仓止损上移；价格回落 → 尾仓止损不下移。
        """
        bars = [
            (100, 112, 99, 111),  # 触 t2，激活尾仓，尾仓止损 = 112-10=102
            (111, 120, 110, 119),  # 创新高，尾仓止损 = max(102, 120-10=110) = 110
            (119, 118, 112, 115),  # 回落，但 low=112 > 110 → 不触发
            # 尾仓止损 = min? 不，多单是 max，所以保持 110
            (115, 116, 109, 110),  # low=109 <= 110 → 尾仓离场（110 价）
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=True, tail_stop_dist=10)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "尾仓离场")
        # 尾仓止损应该是 110（被 ratchet 上去了）
        self.assertAlmostEqual(exit_price, 110, places=1)

    def test_short_tail_activated_then_stopped(self):
        """空单：先触 t2 激活尾仓，然后反弹触尾仓止损

        流程：
        - Bar 1 触 t2 → 激活尾仓，尾仓止损 = t2 + tail_stop_dist = 90 + 10 = 100
        - Bar 2 进入尾仓模式：high=102 >= 100 → 尾仓离场
        """
        bars = [
            (100, 102, 95, 96),  # 下跌
            (96, 97, 85, 86),  # 触 t2=90，激活尾仓，尾仓止损 = 100
            (86, 102, 84, 90),  # 反弹，high=102 >= 尾仓止损100 → 尾仓离场
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=105, t2=90, tail_enabled=True, tail_stop_dist=10)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=-1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "尾仓离场")
        self.assertEqual(exit_idx, 2)
        self.assertEqual(exit_price, 100)  # 尾仓止损价

    def test_short_tail_stop_ratchets_down(self):
        """空单：尾仓止损只向下移动（ratchet 机制）

        价格创新低 → 尾仓止损下移；价格反弹 → 尾仓止损不上移。

        流程：
        - Bar 0 触 t2 → 尾仓止损 = 100
        - Bar 1 创新低 → 尾仓止损 = min(100, 78+10=88) = 88
        - Bar 2 继续创新低 → 尾仓止损 = min(88, 77+10=87) = 87
        - Bar 3 反弹，high=89 >= 87 → 尾仓离场（价格 87）
        """
        bars = [
            (100, 102, 88, 89),  # 触 t2=90，激活尾仓，尾仓止损 = 100
            (89, 90, 78, 79),  # 创新低，尾仓止损 = 88
            (79, 87, 77, 85),  # 继续创新低，尾仓止损 = 87
            (85, 89, 84, 86),  # 反弹触尾仓止损 87 → 离场
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=105, t2=90, tail_enabled=True, tail_stop_dist=10)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=-1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(exit_price, 87, places=1)

    def test_tail_disabled_no_tail_exit(self):
        """尾仓关闭 → 触 t2 直接止盈，不激活尾仓"""
        bars = [
            (100, 105, 99, 104),
            (104, 112, 103, 111),  # 触 t2
            (111, 115, 100, 102),  # 就算回撤也跟尾仓没关系（已经平掉了）
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=False)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "止盈2R")
        self.assertEqual(exit_idx, 1)  # 第 1 根就出场了


# ═══════════════════════════════════════════════════════════════════════════
#  4. 期末平仓
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mEndOfPeriod(unittest.TestCase):
    """期末平仓。"""

    def test_no_trigger_end_of_period(self):
        """没触止损也没触止盈 → 期末平仓"""
        bars = [
            (100, 102, 98, 101),
            (101, 103, 99, 102),
            (102, 104, 100, 103),
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=90, t2=120)  # 都很远
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "期末平")
        self.assertEqual(exit_price, 103)  # 最后一根 close
        self.assertEqual(exit_idx, 2)  # 最后一根索引

    def test_single_bar_end_of_period(self):
        """只有 1 根 bar 且没触发 → 期末平"""
        bars = [(100, 102, 98, 101)]
        df = _make_df5(bars)
        ep = _make_ep(stop=90, t2=120)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(reason, "期末平")
        self.assertEqual(exit_idx, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. 优先级：止损 > 止盈（同一根 bar 内）
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mPriority(unittest.TestCase):
    """同一根 bar 内的触发优先级。"""

    def test_long_same_bar_both_hit_stop_first(self):
        """多单：同一根 bar 同时触止损和止盈 → 止损优先？

        实际逻辑：先检查 low（止损），再检查 high（止盈）。
        如果 low <= stop 且 high >= t2 → 止损先触发。
        （这是"先看最坏情况"的保守逻辑）
        """
        # 极端波动：low 触止损，high 触止盈
        bars = [(100, 120, 90, 105)]  # low=90 < stop=95, high=120 > t2=110
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110, tail_enabled=False)
        exit_price, reason, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        # 代码先检查止损，所以止损优先
        self.assertEqual(reason, "止损")


# ═══════════════════════════════════════════════════════════════════════════
#  6. 返回值
# ═══════════════════════════════════════════════════════════════════════════


class TestSimExit5mReturnValue(unittest.TestCase):
    """返回值结构。"""

    def test_returns_three_values(self):
        """返回 (exit_price, reason, exit_idx) 三元组"""
        bars = [(100, 101, 99, 100)]
        df = _make_df5(bars)
        ep = _make_ep(stop=90, t2=120)
        result = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(len(result), 3)

    def test_exit_idx_within_bars(self):
        """exit_idx 在 [0, len(bars)-1] 范围内"""
        n = 10
        bars = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(n)]
        df = _make_df5(bars)
        ep = _make_ep(stop=80, t2=150)
        _, _, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertGreaterEqual(exit_idx, 0)
        self.assertLess(exit_idx, n)

    def test_exit_idx_matches_stop_bar(self):
        """exit_idx 正确指向触发止损的 bar"""
        bars = [
            (100, 102, 98, 101),  # 0: 正常
            (101, 103, 99, 102),  # 1: 正常
            (102, 104, 94, 96),  # 2: 止损
            (96, 97, 90, 92),  # 3: 不会走到这里
        ]
        df = _make_df5(bars)
        ep = _make_ep(stop=95, t2=110)
        _, _, exit_idx = _sim_exit_5m(df, dir_T=1, entry=100, ep=ep, sd=0)
        self.assertEqual(exit_idx, 2)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  5m 出场模拟 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
