#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regime 分类 — 单元测试
=========================

测试 strategy_layer.classify_regime 的分类逻辑。

决策树（优先级从高到低）：
  1. 数据不足（< 25 bar） → 未知
  2. ATR占比 > atr_thresh  → 波动
  3. MA偏离 < flat_dev 且 ATR < flat_atr → 震荡
  4. |MA斜率| > trend_slope 且 MA偏离 > trend_dev → 趋势
  5. 否则 → 过渡

历史覆盖：
  - P-F：分品种 regime 阈值（波动大的品种放大阈值，避免长期被分错 regime）
  - 优先级顺序：波动 > 震荡 > 趋势 > 过渡（容易踩坑的地方）
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from strategy_layer import REGIME_THRESHOLDS, classify_regime


def make_price_df(prices):
    """从收盘价数组构造一个简单的 OHLC DataFrame（用于 regime 分类）。
    用 close 填充高/低，确保 ATR 计算不报错。"""
    n = len(prices)
    df = pd.DataFrame(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1000] * n,
        }
    )
    return df


def make_trend_df(start=100, slope_pct=0.005, n=60):
    """构造趋势行情：价格按固定斜率上涨/下跌。

    slope_pct: 每根 bar 的涨跌幅（小数），正=上涨，负=下跌
    """
    prices = [start * (1 + slope_pct) ** i for i in range(n)]
    return make_price_df(np.array(prices))


def make_flat_df(base=100, noise_pct=0.002, n=60, seed=42):
    """构造震荡行情：价格在基准附近小幅波动。

    noise_pct: 噪声幅度（相对基准的比例）
    """
    rng = np.random.RandomState(seed)
    noise = rng.normal(0, noise_pct * base, n)
    prices = base + noise
    # 确保 MA20 斜率接近 0（整体水平）
    prices = prices - np.linspace(0, prices[-1] - prices[0], n)
    return make_price_df(prices)


def make_volatile_df(base=100, atr_pct=0.04, n=60, seed=42):
    """构造高波动行情：ATR 占比高。

    atr_pct: 目标 ATR / 价格 比例
    """
    rng = np.random.RandomState(seed)
    # 大幅上下波动
    swings = rng.choice([-1, 1], n) * base * atr_pct * 2
    prices = base + np.cumsum(swings) * 0.5
    # 回拉到基准附近，避免变成趋势
    prices = prices - np.linspace(0, prices[-1] - prices[0], n)
    return make_price_df(prices)


# ═══════════════════════════════════════════════════════════════════════════
#  基本分类正确性
# ═══════════════════════════════════════════════════════════════════════════


class TestRegimeClassification(unittest.TestCase):
    """regime 分类基本正确性。"""

    def test_insufficient_data_unknown(self):
        """数据不足（< 25 bar）→ 未知"""
        df = make_trend_df(n=20)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "未知")
        self.assertIn("数据不足", desc)

    def test_just_enough_data_not_unknown(self):
        """恰好 25 bar → 不再是"未知"（至少进入判断）"""
        df = make_trend_df(n=25, slope_pct=0.01)
        regime, _ = classify_regime(df)
        self.assertNotEqual(regime, "未知")

    def test_strong_trend_up_classified_as_trend(self):
        """强上涨趋势 → 趋势"""
        df = make_trend_df(start=100, slope_pct=0.008, n=60)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "趋势")
        self.assertIn("斜率", desc)

    def test_strong_trend_down_classified_as_trend(self):
        """强下跌趋势 → 趋势"""
        df = make_trend_df(start=100, slope_pct=-0.008, n=60)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "趋势")
        self.assertIn("斜率", desc)

    def test_flat_market_classified_as_flat(self):
        """窄幅震荡 → 震荡"""
        df = make_flat_df(base=100, noise_pct=0.003, n=60)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "震荡")
        self.assertIn("收敛", desc)

    def test_high_volatility_classified_as_volatile(self):
        """高波动 → 波动"""
        df = make_volatile_df(base=100, atr_pct=0.05, n=60)
        regime, desc = classify_regime(df)
        self.assertEqual(regime, "波动")
        self.assertIn("ATR", desc)


# ═══════════════════════════════════════════════════════════════════════════
#  优先级测试（最容易出 bug 的地方）
# ═══════════════════════════════════════════════════════════════════════════


class TestRegimePriority(unittest.TestCase):
    """regime 判断的优先级：波动 > 震荡 > 趋势 > 过渡。

    历史踩坑：同时满足多个 regime 条件时，优先级决定结果。
    比如"高波动 + 强趋势" → 应该判为波动（因为波动优先级最高）。
    """

    def test_volatility_beats_trend(self):
        """高波动 + 有趋势 → 波动（波动优先级更高）"""
        # 构造：整体上涨，但中间剧烈波动
        base = 100
        n = 60
        rng = np.random.RandomState(123)
        # 趋势 + 大波动
        trend = np.array([base * (1 + 0.006) ** i for i in range(n)])
        noise = rng.normal(0, base * 0.03, n)  # 3% 噪声 → ATR 会很高
        prices = trend + noise
        df = make_price_df(prices)
        regime, _ = classify_regime(df)
        # ATR 高 → 波动（即使有趋势）
        self.assertEqual(regime, "波动", "高波动 + 趋势 → 应该判为波动（波动优先级更高）")

    def test_flat_beats_trend(self):
        """震荡（低波动+低偏离）+ 微趋势 → 震荡（震荡优先级更高）"""
        # 价格很平，偏离极小，ATR 也小 → 震荡
        df = make_flat_df(base=100, noise_pct=0.002, n=60, seed=7)
        regime, _ = classify_regime(df)
        self.assertEqual(regime, "震荡")

    def test_volatility_beats_flat(self):
        """高波动 + 低偏离 → 波动（波动比震荡优先级高）"""
        # 价格整体在中间，但波动很大 → ATR 高 → 判波动
        df = make_volatile_df(base=100, atr_pct=0.04, n=60, seed=99)
        regime, _ = classify_regime(df)
        self.assertEqual(regime, "波动", "高波动即使偏离不大，也应该判波动（优先级更高）")


# ═══════════════════════════════════════════════════════════════════════════
#  边界条件
# ═══════════════════════════════════════════════════════════════════════════


class TestRegimeBoundaries(unittest.TestCase):
    """regime 分类的边界条件。"""

    def test_transition_regime_exists(self):
        """存在"过渡"状态（既不满足趋势也不满足震荡也不是高波动）"""
        # 构造一个中等波动、中等斜率的行情 → 应该落到过渡
        base = 100
        n = 60
        rng = np.random.RandomState(42)
        # 温和上涨 + 中等波动
        trend = np.array([base * (1 + 0.002) ** i for i in range(n)])
        noise = rng.normal(0, base * 0.008, n)
        prices = trend + noise
        df = make_price_df(prices)
        regime, _ = classify_regime(df)
        # 可能是趋势或过渡，取决于参数。我们用自定义参数来确保落到过渡
        custom_params = {
            "atr_thresh": 0.05,  # 提高波动门槛 → 不容易判波动
            "flat_dev": 0.002,  # 降低震荡门槛？不，提高门槛让震荡更难
            "flat_atr": 0.003,
            "trend_slope": 0.01,  # 提高趋势斜率门槛 → 不容易判趋势
            "trend_dev": 0.05,  # 提高趋势偏离门槛
        }
        regime, desc = classify_regime(df, params=custom_params)
        self.assertEqual(regime, "过渡")
        self.assertIn("斜率", desc)

    def test_custom_params_change_result(self):
        """自定义参数可以改变分类结果（P-F 分品种阈值的基础）"""
        # 用默认参数判为趋势
        df = make_trend_df(start=100, slope_pct=0.005, n=60)
        regime_default, _ = classify_regime(df)
        self.assertEqual(regime_default, "趋势")

        # 把趋势斜率和偏离门槛设得很高 → 同样的数据不再是趋势
        custom = dict(REGIME_THRESHOLDS)
        custom["trend_slope"] = 0.03  # 3%，高于实际 ~2%
        custom["trend_dev"] = 0.06  # 6%，高于实际 ~4.8%
        custom["atr_thresh"] = 0.05  # 波动门槛也提高，避免落到波动
        regime_custom, _ = classify_regime(df, params=custom)

        self.assertNotEqual(regime_default, regime_custom, "P-F：调整参数应该能改变 regime 分类结果")

    def test_default_params_match_constant(self):
        """不传 params 时使用 REGIME_THRESHOLDS 默认值"""
        df = make_flat_df(base=100, noise_pct=0.003, n=60)
        r1, d1 = classify_regime(df)
        r2, d2 = classify_regime(df, params=REGIME_THRESHOLDS)
        self.assertEqual(r1, r2)
        self.assertEqual(d1, d2)

    def test_24_bars_unknown_25_bars_not(self):
        """24 bar → 未知；25 bar → 非未知（边界精确）"""
        df_24 = make_trend_df(n=24, slope_pct=0.01)
        df_25 = make_trend_df(n=25, slope_pct=0.01)
        r24, _ = classify_regime(df_24)
        r25, _ = classify_regime(df_25)
        self.assertEqual(r24, "未知")
        self.assertNotEqual(r25, "未知")


# ═══════════════════════════════════════════════════════════════════════════
#  P-F 分品种阈值回归测试
# ═══════════════════════════════════════════════════════════════════════════


class TestPerSymbolRegimeParams(unittest.TestCase):
    """P-F：分品种 regime 阈值 — 验证参数调整能改变分类结果。

    历史问题：全局阈值对波动大的品种（如纯碱 SA）太敏感，
    长期被判为"波动"导致趋势策略权重被压低。
    分品种阈值让高波动品种用更高的门槛。
    """

    def test_higher_atr_thresh_reduces_volatile_regime(self):
        """提高 atr_thresh → 更少被判为波动

        模拟 P-F 改造：对高波动品种提高 ATR 门槛，
        避免长期被错判为"波动"。
        """
        df = make_volatile_df(base=100, atr_pct=0.03, n=60, seed=7)

        # 默认阈值 → 可能判波动
        regime_default, _ = classify_regime(df)

        # 提高 ATR 门槛（模拟高波动品种配置）
        custom = dict(REGIME_THRESHOLDS)
        custom["atr_thresh"] = 0.05  # 从 2.5% 提高到 5%
        regime_higher, _ = classify_regime(df, params=custom)

        # 提高门槛后，不应该还是波动（除非波动确实很大）
        # 至少结果可能从"波动"变成其他
        if regime_default == "波动":
            self.assertNotEqual(regime_higher, "波动", "P-F 回归：提高 atr_thresh 应该减少'波动'判定")

    def test_lower_flat_dev_reduces_flat_regime(self):
        """降低 flat_dev 门槛 → 更少被判为震荡（偏离容忍度更低）

        flat_dev 是"偏离小于此值才算震荡"的上限，
        降低它意味着只有更小的偏离才算震荡 → 震荡判定更少。
        """
        df = make_flat_df(base=100, noise_pct=0.005, n=60, seed=3)

        # 默认参数 → 震荡（dev=0.49% < 0.8%）
        regime_default, _ = classify_regime(df)
        self.assertEqual(regime_default, "震荡")

        # 降低 flat_dev 到 0.3%（比实际 0.49% 小）→ 不再满足震荡条件
        custom = dict(REGIME_THRESHOLDS)
        custom["flat_dev"] = 0.003  # 从 0.8% 降到 0.3%
        custom["flat_atr"] = 0.005  # 同步降低 flat_atr 到 0.5%（实际 0.62%）
        regime_custom, _ = classify_regime(df, params=custom)

        self.assertNotEqual(regime_custom, "震荡", "降低 flat_dev 应该减少'震荡'判定")

    def test_higher_trend_slope_makes_trend_harder(self):
        """提高 trend_slope 门槛 → 更难被判为趋势"""
        df = make_trend_df(start=100, slope_pct=0.005, n=60)

        # 默认 → 趋势（斜率 ~2% > 0.3% 门槛）
        regime_default, _ = classify_regime(df)
        self.assertEqual(regime_default, "趋势")

        # 提高趋势斜率门槛到 3%（高于实际 ~2%）+ 提高偏离门槛
        custom = dict(REGIME_THRESHOLDS)
        custom["trend_slope"] = 0.03  # 从 0.3% 提到 3%
        custom["trend_dev"] = 0.06  # 从 1% 提到 6%
        custom["atr_thresh"] = 0.05  # 波动门槛也提高
        regime_custom, _ = classify_regime(df, params=custom)

        self.assertNotEqual(regime_custom, "趋势", "提高 trend_slope 应该减少'趋势'判定")


# ═══════════════════════════════════════════════════════════════════════════
#  回归：数值稳定性
# ═══════════════════════════════════════════════════════════════════════════


class TestRegimeNumericalStability(unittest.TestCase):
    """数值稳定性测试。"""

    def test_constant_price_flat(self):
        """价格完全不变 → 震荡（0 偏离 + 0 ATR）"""
        prices = np.full(60, 100.0)
        df = make_price_df(prices)
        regime, _ = classify_regime(df)
        # dev = 0 < flat_dev, atr = 0 < flat_atr → 震荡
        self.assertEqual(regime, "震荡")

    def test_very_small_data_range(self):
        """价格极小（如 0.001）→ 不除零，能正常分类"""
        prices = np.full(60, 0.001) + np.random.RandomState(1).normal(0, 0.00001, 60)
        df = make_price_df(prices)
        regime, desc = classify_regime(df)
        # 不崩溃即可，结果不重要
        self.assertIsInstance(regime, str)
        self.assertIn(regime, ["趋势", "震荡", "波动", "过渡", "未知"])

    def test_large_price_numbers(self):
        """价格很大（如黄金 1000+）→ 比例计算正确，不受绝对值影响"""
        df_small = make_trend_df(start=10, slope_pct=0.005, n=60)
        df_large = make_trend_df(start=10000, slope_pct=0.005, n=60)
        r_small, _ = classify_regime(df_small)
        r_large, _ = classify_regime(df_large)
        # 相同斜率比例，分类结果应该一致
        self.assertEqual(r_small, r_large, "regime 是比例计算，不受价格绝对值影响")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Regime 分类 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
