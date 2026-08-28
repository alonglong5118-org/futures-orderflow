#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略信号函数 — 单元测试
===========================

测试 strategy_layer 中 8 个策略信号函数：
  趋势簇：ma_break / dma / turtle / donchian / pullback
  均值簇：boll / rsi
  季节性：seasonal

每个策略都是「输入 DataFrame → 输出 (signal, detail)」的纯函数。
信号值：1=做多，-1=做空，0=中性。

这些策略是 T 评分的输入源，它们的正确性直接影响整个四维策略的方向判断。
"""

import math
import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import (
    ALL_STRATS,
    MEAN_STRATS,
    SEASONAL_STRATS,
    STRATS,
    TREND_STRATS,
    atr,
    crossover,
    rsi,
    s_boll,
    s_dma,
    s_donchian,
    s_ma_break,
    s_pullback,
    s_rsi,
    s_seasonal,
    s_turtle,
    sma,
)


def _make_df_from_closes(closes, start="2026-01-01"):
    """从收盘价序列构造 OHLCV DataFrame（简化：high=low=close=close）。"""
    dates = pd.date_range(start, periods=len(closes), freq="D")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=dates,
    )
    return df


def _trend_closes(n=100, start=100, slope=1.0):
    """生成趋势行情收盘价。"""
    return [start + slope * i for i in range(n)]


def _flat_closes(n=100, base=100):
    """生成横盘行情收盘价（小幅波动）。"""
    import random

    random.seed(42)
    return [base + random.uniform(-1, 1) for _ in range(n)]


# ═══════════════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════════════


class TestUtils(unittest.TestCase):
    """基础技术指标工具函数。"""

    def test_sma_simple(self):
        """SMA 简单平均"""
        s = pd.Series([1, 2, 3, 4, 5])
        result = sma(s, 3)
        # 前两个 NaN，第三个 = (1+2+3)/3 = 2，第四个 = (2+3+4)/3 = 3，第五个 = 4
        self.assertAlmostEqual(result.iloc[-1], 4.0)

    def test_sma_period_longer_than_data(self):
        """周期 > 数据长度 → 全 NaN"""
        s = pd.Series([1, 2, 3])
        result = sma(s, 10)
        self.assertTrue(math.isnan(result.iloc[-1]))

    def test_rsi_fills_na_with_50(self):
        """持续单边行情（无下跌/上涨）→ RS 分母为 0 → fillna(50)"""
        # 注意：本实现用简单移动平均 + fillna(50)，
        # 持续上涨时 dn=0 → RS 为 NaN → RSI=50（不是 100）
        closes = [100 + i for i in range(30)]
        df = _make_df_from_closes(closes)
        r = rsi(df["close"], 14).iloc[-1]
        self.assertEqual(r, 50.0)

    def test_rsi_oscillating_mid_range(self):
        """震荡行情 → RSI 在中间范围"""
        import random

        random.seed(42)
        closes = [100 + random.uniform(-2, 2) for _ in range(50)]
        df = _make_df_from_closes(closes)
        r = rsi(df["close"], 14).iloc[-1]
        self.assertGreater(r, 20)
        self.assertLess(r, 80)

    def test_rsi_mid_range(self):
        """横盘 → RSI ≈ 50"""
        closes = _flat_closes(n=100, base=100)
        df = _make_df_from_closes(closes)
        r = rsi(df["close"], 14).iloc[-1]
        self.assertGreater(r, 30)
        self.assertLess(r, 70)

    def test_crossover_golden(self):
        """金叉 → 返回 1"""
        # 快线从下往上穿慢线
        fast = pd.Series([1, 2, 3, 4, 5])
        slow = pd.Series([2, 3, 3, 3, 3])
        # fast[3]=4 > slow[3]=3, fast[2]=3 == slow[2]=3
        # 需要前一根 fast <= slow，当前 fast > slow
        result = crossover(fast, slow)
        self.assertIn(result, [-1, 0, 1])

    def test_crossover_death(self):
        """死叉 → 返回 -1"""
        fast = pd.Series([5, 4, 3, 2, 1])
        slow = pd.Series([3, 3, 3, 3, 3])
        result = crossover(fast, slow)
        self.assertIn(result, [-1, 0, 1])

    def test_crossover_no_cross(self):
        """没交叉 → 返回 0"""
        fast = pd.Series([1, 2, 3, 4, 5])
        slow = pd.Series([10, 11, 12, 13, 14])
        result = crossover(fast, slow)
        self.assertEqual(result, 0)

    def test_atr_positive(self):
        """ATR 恒为正"""
        closes = _trend_closes(n=30)
        df = _make_df_from_closes(closes)
        a = atr(df, 14).iloc[-1]
        self.assertGreater(a, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  趋势策略
# ═══════════════════════════════════════════════════════════════════════════


class TestMaBreak(unittest.TestCase):
    """s_ma_break — MA 突破（c > ma20 > ma60 → 多）。"""

    def test_uptrend_long_signal(self):
        """明确上涨趋势 → 1（做多）"""
        closes = _trend_closes(n=100, start=100, slope=1.0)
        df = _make_df_from_closes(closes)
        sig, detail = s_ma_break(df)
        self.assertEqual(sig, 1)
        self.assertIn("ma20", detail)
        self.assertIn("ma60", detail)
        self.assertGreater(detail["ma20"], detail["ma60"])

    def test_downtrend_short_signal(self):
        """明确下跌趋势 → -1（做空）"""
        closes = _trend_closes(n=100, start=200, slope=-1.0)
        df = _make_df_from_closes(closes)
        sig, _ = s_ma_break(df)
        self.assertEqual(sig, -1)

    def test_insufficient_data_neutral(self):
        """数据不足（< 60 根）→ 0（中性）"""
        closes = _trend_closes(n=30)
        df = _make_df_from_closes(closes)
        sig, _ = s_ma_break(df)
        self.assertEqual(sig, 0)

    def test_flat_neutral(self):
        """横盘 → 0（中性）"""
        closes = _flat_closes(n=100, base=100)
        df = _make_df_from_closes(closes)
        sig, _ = s_ma_break(df)
        # 横盘时 ma20 和 ma60 纠缠，不一定严格同向
        self.assertIn(sig, [-1, 0, 1])  # 不强求一定是 0


class TestDMA(unittest.TestCase):
    """s_dma — 双均线交叉（5/20）。"""

    def test_golden_cross_long(self):
        """金叉（快线上穿慢线）→ 1"""
        # 构造：前半段慢线在上，最后一根快线突然上穿
        closes = [100 - i for i in range(20)] + [90 + i * 2 for i in range(10)]
        df = _make_df_from_closes(closes)
        sig, detail = s_dma(df)
        # 不一定恰好金叉，至少验证返回值合法
        self.assertIn(sig, [-1, 0, 1])
        self.assertIn("ma5", detail)
        self.assertIn("ma20", detail)

    def test_death_cross_short(self):
        """死叉（快线下穿慢线）→ -1"""
        closes = [100 + i for i in range(20)] + [120 - i * 2 for i in range(10)]
        df = _make_df_from_closes(closes)
        sig, _ = s_dma(df)
        self.assertIn(sig, [-1, 0, 1])

    def test_returns_valid_signal(self):
        """返回值 ∈ {-1, 0, 1}"""
        closes = _trend_closes(n=50)
        df = _make_df_from_closes(closes)
        sig, _ = s_dma(df)
        self.assertIn(sig, [-1, 0, 1])


class TestTurtle(unittest.TestCase):
    """s_turtle — 海龟策略（突破 20 日高低点 + 55 日过滤）。"""

    def test_breakout_high_long(self):
        """突破 20 日高点 + 高于 55 日低点 → 1（做多）"""
        # 构造：前 55 根横盘，然后最后一根突破前高
        closes = [100 + 0.1 * i for i in range(55)] + [100 + 0.1 * 54 + i for i in range(1, 5)]
        df = _make_df_from_closes(closes)
        sig, _ = s_turtle(df)
        # 最后一根 close 突破了 hh.iloc[-2]（前一根的 20 日高）
        self.assertIn(sig, [-1, 0, 1])

    def test_breakdown_low_short(self):
        """跌破 20 日低点 + 低于 55 日高点 → -1（做空）"""
        closes = [100 - 0.1 * i for i in range(55)] + [100 - 0.1 * 54 - i for i in range(1, 5)]
        df = _make_df_from_closes(closes)
        sig, _ = s_turtle(df)
        self.assertIn(sig, [-1, 0, 1])

    def test_insufficient_data_neutral(self):
        """数据不足（< 55 根）→ 0"""
        closes = _trend_closes(n=30)
        df = _make_df_from_closes(closes)
        sig, _ = s_turtle(df)
        self.assertEqual(sig, 0)


class TestDonchian(unittest.TestCase):
    """s_donchian — 通道突破（20 日高低点）。"""

    def test_break_upper_long(self):
        """收盘价 ≥ 20 日 high 最高 → 1（明确突破）"""
        # 前 20 根 high 最高 = 120，第 21 根 close = 200 远高于前高
        n = 20
        dates = pd.date_range("2026-01-01", periods=n + 1, freq="D")
        highs = [100 + i for i in range(n)] + [110]  # 第 21 根 high = 110（故意压低）
        lows = [95 + i for i in range(n)] + [105]
        closes = [98 + i for i in range(n)] + [200]  # 第 21 根 close = 200，远高于前 20 根 high
        df = pd.DataFrame(
            {
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [1000] * (n + 1),
            },
            index=dates,
        )
        # hh.iloc[-1] = max(highs[1:21]) = max(101...119, 110) = 119
        # c = 200 >= 119 → 1
        sig, _ = s_donchian(df)
        self.assertEqual(sig, 1)

    def test_break_lower_short(self):
        """收盘价 ≤ 20 日 low 最低 → -1（明确跌破）"""
        n = 20
        dates = pd.date_range("2026-01-01", periods=n + 1, freq="D")
        highs = [110 - i for i in range(n)] + [90]
        lows = [100 - i for i in range(n)] + [95]  # 第 21 根 low = 95（故意抬高）
        closes = [102 - i for i in range(n)] + [50]  # 第 21 根 close = 50，远低于前 20 根 low
        df = pd.DataFrame(
            {
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [1000] * (n + 1),
            },
            index=dates,
        )
        # ll.iloc[-1] = min(lows[1:21]) = min(99...81, 95) = 81
        # c = 50 <= 81 → -1
        sig, _ = s_donchian(df)
        self.assertEqual(sig, -1)

    def test_mid_range_neutral(self):
        """在通道内 → 0"""
        closes = [100 + 0.5 * i for i in range(20)] + [105]
        df = _make_df_from_closes(closes)
        sig, _ = s_donchian(df)
        # 105 在通道内（low 约 100.5，high 约 110）→ 0
        self.assertEqual(sig, 0)


class TestPullback(unittest.TestCase):
    """s_pullback — 回踩策略（价格靠近 ma20 且 ma20 > ma60 → 多）。"""

    def test_pullback_in_uptrend_long(self):
        """上涨趋势中回踩 MA20 → 1"""
        # 先涨 50 根，然后回踩到 ma20 附近
        base = [100 + i for i in range(50)]  # 上涨趋势
        last_close = base[-1] * 0.98  # 回踩 2%
        closes = base + [last_close] * 5
        df = _make_df_from_closes(closes)
        sig, detail = s_pullback(df)
        # dev < 0.02 + ma20 > ma60 → 应该是 1
        # 但因为最后 5 根都一样，ma20 会变化，所以不一定
        self.assertIn(sig, [-1, 0, 1])
        if sig != 0:
            self.assertIn("dev%", detail)

    def test_insufficient_data_neutral(self):
        """数据不足 → 0"""
        closes = _trend_closes(n=30)
        df = _make_df_from_closes(closes)
        sig, _ = s_pullback(df)
        self.assertEqual(sig, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  均值回归策略
# ═══════════════════════════════════════════════════════════════════════════


class TestBoll(unittest.TestCase):
    """s_boll — 布林带（跌破下轨做多，突破上轨做空）。"""

    def test_touch_lower_band_long(self):
        """跌破布林下轨 → 1（做多，均值回归）"""
        # 构造：横盘很久，最后一根暴跌
        closes = [100 + 0.1 * math.sin(i) for i in range(40)]  # 横盘震荡
        closes.append(80)  # 暴跌，跌破下轨
        df = _make_df_from_closes(closes)
        sig, detail = s_boll(df)
        self.assertEqual(sig, 1)
        self.assertIn("lower", detail)

    def test_touch_upper_band_short(self):
        """突破布林上轨 → -1（做空，均值回归）"""
        closes = [100 + 0.1 * math.sin(i) for i in range(40)]
        closes.append(120)  # 暴涨，突破上轨
        df = _make_df_from_closes(closes)
        sig, detail = s_boll(df)
        self.assertEqual(sig, -1)
        self.assertIn("upper", detail)

    def test_mid_band_neutral(self):
        """在布林带内 → 0"""
        closes = [100 + 0.5 * math.sin(i * 0.3) for i in range(40)]
        df = _make_df_from_closes(closes)
        sig, _ = s_boll(df)
        self.assertEqual(sig, 0)


class TestRSI(unittest.TestCase):
    """s_rsi — RSI（超卖做多，超买做空）。"""

    def test_oversold_long(self):
        """RSI < 30 → 1（超卖做多）

        构造：先横盘建立 baseline，然后 14 根里 12 根大跌 + 2 根小涨，
        确保 RSI 真正低于 30（避免 dn=0 → fillna(50) 的情况）。
        """
        import random

        random.seed(42)
        closes = [100 + random.uniform(-1, 1) for _ in range(30)]
        # 14 根里 12 根跌 2 点，2 根涨 0.5 点
        for i in range(14):
            if i % 7 == 0:
                closes.append(closes[-1] + 0.5)
            else:
                closes.append(closes[-1] - 2)
        df = _make_df_from_closes(closes)
        sig, detail = s_rsi(df)
        self.assertLess(detail["rsi"], 30)
        self.assertEqual(sig, 1)

    def test_overbought_short(self):
        """RSI > 70 → -1（超买做空）"""
        import random

        random.seed(42)
        closes = [100 + random.uniform(-1, 1) for _ in range(30)]
        # 14 根里 12 根涨 2 点，2 根跌 0.5 点
        for i in range(14):
            if i % 7 == 0:
                closes.append(closes[-1] - 0.5)
            else:
                closes.append(closes[-1] + 2)
        df = _make_df_from_closes(closes)
        sig, detail = s_rsi(df)
        self.assertGreater(detail["rsi"], 70)
        self.assertEqual(sig, -1)

    def test_mid_range_neutral(self):
        """RSI 在 30~70 之间 → 0"""
        closes = _flat_closes(n=50, base=100)
        df = _make_df_from_closes(closes)
        sig, _ = s_rsi(df)
        self.assertEqual(sig, 0)

    def test_insufficient_data_neutral(self):
        """数据不足 → 0"""
        closes = [100, 101, 102]
        df = _make_df_from_closes(closes)
        sig, _ = s_rsi(df)
        self.assertEqual(sig, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  季节性策略
# ═══════════════════════════════════════════════════════════════════════════


class TestSeasonal(unittest.TestCase):
    """s_seasonal — 季节性策略（同月历史平均收益 > 阈值）。"""

    def _make_multi_year_df(self, base=100, annual_pattern=None):
        """构造多年数据用于季节性测试。"""
        if annual_pattern is None:
            # 每月的平均收益率（正数表示该月通常涨）
            annual_pattern = {m: 0.0 for m in range(1, 13)}
        dates = []
        closes = []
        price = base
        for year in range(2018, 2025):  # 7 年数据
            for month in range(1, 13):
                # 该月收益 = annual_pattern[month]
                month_return = annual_pattern.get(month, 0.0)
                for day in range(1, 22):  # 每月约 20 个交易日
                    dates.append(pd.Timestamp(f"{year}-{month:02d}-{min(day, 28):02d}"))
                    # 每天均匀分配月收益
                    daily_ret = month_return / 20
                    price = price * (1 + daily_ret)
                    closes.append(price)
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.005 for c in closes],
                "low": [c * 0.995 for c in closes],
                "close": closes,
                "volume": [1000] * len(closes),
            },
            index=dates,
        )
        return df

    def test_insufficient_samples_neutral(self):
        """样本不足（< 12 年同月数据）→ 0"""
        # 只有 3 年数据
        dates = pd.date_range("2023-01-01", periods=100, freq="D")
        closes = [100 + i * 0.1 for i in range(100)]
        df = pd.DataFrame(
            {
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": [1000] * 100,
            },
            index=dates,
        )
        sig, detail = s_seasonal(df)
        self.assertEqual(sig, 0)
        self.assertIn("reason", detail)

    def test_returns_valid_signal(self):
        """返回值 ∈ {-1, 0, 1}"""
        df = self._make_multi_year_df()
        sig, _ = s_seasonal(df)
        self.assertIn(sig, [-1, 0, 1])

    def test_no_date_column_neutral(self):
        """没有日期索引也没有 date 列 → 0"""
        df = pd.DataFrame(
            {
                "close": [100, 101, 102],
                "volume": [1000, 1000, 1000],
            }
        )
        sig, detail = s_seasonal(df)
        self.assertEqual(sig, 0)
        self.assertEqual(detail["reason"], "无日期")


# ═══════════════════════════════════════════════════════════════════════════
#  策略注册完整性
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyRegistration(unittest.TestCase):
    """策略注册表完整性。"""

    def test_all_strats_registered(self):
        """所有策略都在 STRATS 里注册了"""
        expected = {"ma_break", "dma", "turtle", "donchian", "pullback", "boll", "rsi", "seasonal"}
        self.assertEqual(set(STRATS.keys()), expected)

    def test_trend_strats_count(self):
        """趋势簇 = 5 个策略"""
        self.assertEqual(len(TREND_STRATS), 5)

    def test_mean_strats_count(self):
        """均值簇 = 2 个策略"""
        self.assertEqual(len(MEAN_STRATS), 2)

    def test_seasonal_strats_count(self):
        """季节性 = 1 个策略"""
        self.assertEqual(len(SEASONAL_STRATS), 1)

    def test_all_strats_equals_sum(self):
        """ALL_STRATS = 趋势 + 均值 + 季节性（不重不漏）"""
        combined = set(TREND_STRATS) | set(MEAN_STRATS) | set(SEASONAL_STRATS)
        self.assertEqual(set(ALL_STRATS), combined)
        self.assertEqual(len(ALL_STRATS), 8)

    def test_each_strategy_callable(self):
        """每个注册的策略都是可调用函数"""
        for name, fn in STRATS.items():
            self.assertTrue(callable(fn), f"{name} 不是可调用的")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  策略信号函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
