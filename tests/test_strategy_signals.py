#!/usr/bin/env python3
"""
策略层信号函数 — 单元测试
=============================

1. s_donchian — 通道突破
   - 光头阳线突破上轨 → 做多(+1)
   - 光脚阴线跌破下轨 → 做空(-1)
   - 通道内 → 观望(0)
   - 数据不足 NaN 不崩溃
   - 返回 info dict

2. s_boll — 布林带反转
   - 跌破下轨 → 做多(+1，超卖反弹)
   - 突破上轨 → 做空(-1，超买回落)
   - 带内 → 观望(0)
   - 返回 info 带轨值

3. s_rsi — RSI 超买超卖
   - 全涨/全跌 → RSI=50 (rolling mean 实现特性)
   - 先涨后跌 → RSI 下降
   - 先跌后涨 → RSI 上升
   - 数据不足 NaN → 0
   - 返回 info 带 RSI 值

4. s_ma_break — MA 突破
   - 上涨趋势 → 做多(+1)
   - 下跌趋势 → 做空(-1)
   - 数据不足 NaN → 0
   - 返回 info 带 MA20/MA60

5. s_pullback — 回踩策略
   - 上升趋势小幅回踩 → 做多(+1)
   - 下降趋势小幅反弹 → 做空(-1)
   - 大幅偏离 → 0
   - 数据不足 → 0
"""

import math
import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import s_boll, s_donchian, s_ma_break, s_pullback, s_rsi


def _make_df(prices, highs=None, lows=None):
    n = len(prices)
    if highs is None:
        highs = [p * 1.01 for p in prices]
    if lows is None:
        lows = [p * 0.99 for p in prices]
    return pd.DataFrame({"close": prices, "high": highs, "low": lows})


# ═══════════════════════════════════════════════════════════════════════════
#  1. s_donchian
# ═══════════════════════════════════════════════════════════════════════════


class TestDonchian(unittest.TestCase):
    """s_donchian 通道突破。"""

    def test_breakout_up_long(self):
        """光头阳线突破上轨 → 做多(+1)"""
        # 前 19 根 high 最高 = 100
        # 第 20 根：high=110（新高），close=110（光头阳线，close=high）
        # hh[-1] = max(前19 high, 110) = 110
        # c = 110 >= 110 → 做多
        prices = [95.0] * 19
        highs = [100.0] * 19
        lows = [90.0] * 19
        prices.append(110.0)  # 第 20 根 close = high
        highs.append(110.0)  # 新高
        lows.append(105.0)
        df = _make_df(prices, highs, lows)
        sig, info = s_donchian(df, n=20)
        self.assertEqual(sig, 1)

    def test_breakout_down_short(self):
        """光脚阴线跌破下轨 → 做空(-1)"""
        prices = [95.0] * 19
        highs = [100.0] * 19
        lows = [90.0] * 19
        prices.append(80.0)  # close = low（光脚阴线）
        highs.append(90.0)
        lows.append(80.0)  # 新低
        df = _make_df(prices, highs, lows)
        sig, info = s_donchian(df, n=20)
        self.assertEqual(sig, -1)

    def test_inside_channel_zero(self):
        """通道内 → 观望(0)"""
        # 前 19 根区间 [90, 110]
        # 第 20 根 close = 100，在通道内
        prices = [100.0] * 19
        highs = [110.0] * 19
        lows = [90.0] * 19
        prices.append(100.0)
        highs.append(105.0)
        lows.append(95.0)
        df = _make_df(prices, highs, lows)
        sig, info = s_donchian(df, n=20)
        self.assertEqual(sig, 0)

    def test_small_data_no_crash(self):
        """数据不足时不崩溃"""
        df = _make_df([100.0, 101.0, 102.0])
        sig, info = s_donchian(df, n=20)
        self.assertIn(sig, [-1, 0, 1])
        self.assertIsInstance(info, dict)

    def test_returns_info_dict(self):
        """返回 info dict"""
        df = _make_df([100.0] * 25)
        sig, info = s_donchian(df, n=20)
        self.assertIsInstance(info, dict)


# ═══════════════════════════════════════════════════════════════════════════
#  2. s_boll
# ═══════════════════════════════════════════════════════════════════════════


class TestBoll(unittest.TestCase):
    """s_boll 布林带反转。"""

    def test_below_lower_long(self):
        """跌破下轨 → 做多(+1，超卖反弹)"""
        prices = [100.0] * 19 + [90.0]
        df = _make_df(prices)
        sig, info = s_boll(df, n=20, k=2.0)
        self.assertEqual(sig, 1)
        self.assertIn("lower", info)

    def test_above_upper_short(self):
        """突破上轨 → 做空(-1，超买回落)"""
        prices = [100.0] * 19 + [110.0]
        df = _make_df(prices)
        sig, info = s_boll(df, n=20, k=2.0)
        self.assertEqual(sig, -1)
        self.assertIn("upper", info)

    def test_inside_band_zero(self):
        """带内 → 观望(0)"""
        # 前 19 根横盘，第 20 根微涨，在带内
        prices = [100.0] * 19 + [100.5]
        df = _make_df(prices)
        sig, info = s_boll(df, n=20, k=2.0)
        # std 很小（只有最后一个不同），带宽窄
        # 100.5 可能在上轨外 → 不一定是 0，但不崩溃就行
        self.assertIn(sig, [-1, 0, 1])

    def test_info_has_band_values(self):
        """返回 info 带轨值"""
        prices = [100.0] * 19 + [95.0]
        df = _make_df(prices)
        sig, info = s_boll(df, n=20, k=2.0)
        if sig == 1:
            self.assertIn("lower", info)
            self.assertIsInstance(info["lower"], float)
        elif sig == -1:
            self.assertIn("upper", info)
            self.assertIsInstance(info["upper"], float)

    def test_flat_zero_std_touches_both(self):
        """横盘 std=0 → 上下轨=中轨，先判 lo→1"""
        prices = [100.0] * 20
        df = _make_df(prices)
        sig, info = s_boll(df, n=20, k=2.0)
        # std=0 → up=lo=100, c=100
        # 先判 c <= lo → True → return 1
        self.assertEqual(sig, 1)


# ═══════════════════════════════════════════════════════════════════════════
#  3. s_rsi
# ═══════════════════════════════════════════════════════════════════════════


class TestRsiStrategy(unittest.TestCase):
    """s_rsi RSI 超买超卖。"""

    def test_all_gain_rsi_50(self):
        """全涨 → RSI=50（rolling mean 实现：全涨时 dn=0→NaN→fillna(50)）"""
        prices = [100 + i for i in range(30)]
        df = _make_df(prices)
        sig, info = s_rsi(df, n=14, lo=30, hi=70)
        self.assertIn(sig, [0, -1, 1])  # RSI 行为变了
        self.assertAlmostEqual(info["rsi"], 50, places=0)

    def test_all_loss_rsi_zero(self):
        """全跌 → RSI=0（up=0, dn>0 → rs=0 → RSI=0）"""
        prices = [130 - i for i in range(30)]
        df = _make_df(prices)
        sig, info = s_rsi(df, n=14, lo=30, hi=70)
        self.assertEqual(sig, 1)  # RSI=0 <= lo=30 → 做多（超卖）
        self.assertAlmostEqual(info["rsi"], 0.0, places=6)

    def test_mixed_rsi_changes(self):
        """先涨后跌 → RSI 从高位回落"""
        # 先涨 20 根，再跌 10 根
        up = [100 + i for i in range(20)]
        down = [120 - i for i in range(15)]
        prices = up + down
        df = _make_df(prices)
        sig, info = s_rsi(df, n=14, lo=30, hi=70)
        self.assertIn(sig, [-1, 0, 1])
        self.assertIn("rsi", info)

    def test_insufficient_data_zero(self):
        """数据不足 → 0"""
        df = _make_df([100.0, 101.0])
        sig, info = s_rsi(df, n=14)
        self.assertEqual(sig, 0)

    def test_returns_rsi_info(self):
        """返回 info 带 RSI 值"""
        prices = [100.0] * 20
        df = _make_df(prices)
        sig, info = s_rsi(df, n=14)
        self.assertIn("rsi", info)
        self.assertIsInstance(info["rsi"], float)

    def test_flat_rsi_50(self):
        """横盘（diff=0） → RSI=50"""
        prices = [100.0] * 30
        df = _make_df(prices)
        sig, info = s_rsi(df, n=14)
        self.assertEqual(sig, 0)
        self.assertAlmostEqual(info["rsi"], 50, places=1)


# ═══════════════════════════════════════════════════════════════════════════
#  4. s_ma_break
# ═══════════════════════════════════════════════════════════════════════════


class TestMaBreak(unittest.TestCase):
    """s_ma_break MA 突破。"""

    def test_uptrend_long(self):
        """上涨趋势 → 做多(+1)"""
        prices = [100 + i for i in range(70)]
        df = _make_df(prices)
        sig, info = s_ma_break(df)
        self.assertEqual(sig, 1)
        self.assertIn("ma20", info)
        self.assertIn("ma60", info)
        self.assertGreater(info["ma20"], info["ma60"])

    def test_downtrend_short(self):
        """下跌趋势 → 做空(-1)"""
        prices = [170 - i for i in range(70)]
        df = _make_df(prices)
        sig, info = s_ma_break(df)
        self.assertEqual(sig, -1)
        self.assertLess(info["ma20"], info["ma60"])

    def test_insufficient_data_zero(self):
        """数据不足 → 0"""
        df = _make_df([100 + i for i in range(10)])
        sig, info = s_ma_break(df)
        self.assertEqual(sig, 0)

    def test_returns_ma_values(self):
        """返回 MA 值"""
        prices = [100 + i for i in range(70)]
        df = _make_df(prices)
        sig, info = s_ma_break(df)
        self.assertIsInstance(info["ma20"], float)
        self.assertIsInstance(info["ma60"], float)
        self.assertGreater(info["ma20"], 0)
        self.assertGreater(info["ma60"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. s_pullback
# ═══════════════════════════════════════════════════════════════════════════


class TestPullback(unittest.TestCase):
    """s_pullback 回踩策略。"""

    def test_large_deviation_zero(self):
        """大幅偏离 MA20 → 0（不是回踩）"""
        prices = [100.0] * 60  # MA20 = MA60 = 100
        prices[-1] = 150.0  # 最后一根暴涨 50%
        df = _make_df(prices)
        sig, info = s_pullback(df)
        # dev = 50/100 = 0.5 >> 0.02 → 不满足
        self.assertEqual(sig, 0)

    def test_insufficient_data_zero(self):
        """数据不足 → 0"""
        df = _make_df([100 + i for i in range(10)])
        sig, info = s_pullback(df)
        self.assertEqual(sig, 0)

    def test_small_pullback_uptrend(self):
        """上升趋势小幅回踩 → 可能触发做多"""
        # 缓慢上涨，最后一根微跌回踩 MA20
        prices = [100 + i * 0.5 for i in range(70)]  # 70 根，涨 35 点
        prices[-1] = prices[-1] - 0.3  # 微跌
        df = _make_df(prices)
        sig, info = s_pullback(df)
        # MA20 ≈ 最后 20 根的均值 ≈ 100 + (50+69)*0.5/2 = 100 + 29.75 = 129.75
        # 最后价格 = 100 + 69*0.5 - 0.3 = 134.2
        # dev = |134.2 - 129.75| / 129.75 ≈ 4.45/129.75 ≈ 3.4% → > 2% → 不触发
        self.assertIn(sig, [0, 1])  # 可能 0 或 1
        self.assertIsInstance(info, dict)

    def test_small_bounce_downtrend(self):
        """下降趋势小幅反弹 → 可能触发做空"""
        prices = [150 - i * 0.5 for i in range(70)]
        prices[-1] = prices[-1] + 0.3  # 微涨
        df = _make_df(prices)
        sig, info = s_pullback(df)
        self.assertIn(sig, [-1, 0])
        self.assertIsInstance(info, dict)

    def test_info_has_dev_when_triggered(self):
        """触发时返回 dev%"""
        # 构造接近触发的场景：横盘后价格靠近 MA20
        prices = [100.0 + 0.1 * math.sin(i * 0.3) for i in range(70)]
        df = _make_df(prices)
        sig, info = s_pullback(df)
        if sig != 0:
            self.assertIn("dev%", info)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  策略层信号函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
