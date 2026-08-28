#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维策略核心函数 — 单元测试
=================================

1. combine_bias — 三维偏置合成
   - 全零 → 0
   - T 主导 → T 权重最大
   - F 正贡献 → 偏多
   - C 负贡献 → 偏空
   - 三维同向 → 放大
   - 三维反向 → 抵消
   - 自定义权重生效
   - 保留 1 位小数
   - F/C 为零时退化为 T 主导
   - 权重和不影响（各自加权）

2. regime_weights — 市场状态策略权重
   - 趋势态：趋势策略 1.0，均值策略 0.3，seasonal 0.2
   - 震荡态：趋势策略 0.3，均值策略 1.0，seasonal 0.3
   - 波动态：趋势策略 0.5，均值策略 0.2，seasonal 0.1
   - 过渡/未知/其他：全部 0.5
   - 返回 dict，key 齐全
   - 趋势态趋势权重 > 震荡态趋势权重
   - 震荡态均值权重 > 趋势态均值权重
   - seasonal 永远在

3. _norm_daily_cols — 日线列名标准化
   - 英文列名（hold/settle/open_interest）
   - 中文列名（日期/开盘/最高/最低/收盘/成交量/持仓量）
   - date 列转 datetime 并设为索引
   - 按日期排序
   - 空 DataFrame → 原样返回
   - None → 返回 None
   - 无 date 列 → 不设索引
   - 中英文混合也能识别
"""

import sys
import os
import unittest
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import combine_bias, regime_weights, _norm_daily_cols


# ═══════════════════════════════════════════════════════════════════════════
#  1. combine_bias
# ═══════════════════════════════════════════════════════════════════════════

class TestCombineBias(unittest.TestCase):
    """combine_bias 三维偏置合成（参数顺序: F, T, C）。"""

    def _cfg(self, fw=0.25, tw=0.6, cw=0.15):
        return {"combine_weights": {"F": fw, "T": tw, "C": cw}}

    def test_all_zero(self):
        """全零 → 0"""
        result = combine_bias(0, 0, 0, cfg=self._cfg())
        self.assertEqual(result, 0.0)

    def test_t_dominant(self):
        """T 主导 → T 权重贡献最大"""
        # F=0, T=100, C=0 → 0.6*100 = 60
        result = combine_bias(0, 100, 0, cfg=self._cfg())
        self.assertAlmostEqual(result, 60.0, places=6)

    def test_f_positive_adds(self):
        """F 正贡献 → 偏多"""
        # F=100, T=0, C=0 → 0.25*100 = 25
        result = combine_bias(100, 0, 0, cfg=self._cfg())
        self.assertAlmostEqual(result, 25.0, places=6)

    def test_c_negative_subtracts(self):
        """C 负贡献 → 偏空"""
        # F=0, T=0, C=-100 → 0.15*(-100) = -15
        result = combine_bias(0, 0, -100, cfg=self._cfg())
        self.assertAlmostEqual(result, -15.0, places=6)

    def test_three_same_direction_amplifies(self):
        """三维同向 → 放大"""
        # F=100, T=100, C=100 → 0.25+0.6+0.15 = 1.0 → 100
        result = combine_bias(100, 100, 100, cfg=self._cfg())
        self.assertAlmostEqual(result, 100.0, places=6)

    def test_three_opposite_cancels(self):
        """三维反向 → 部分抵消"""
        # F=-100 (×0.25=-25), T=100 (×0.6=+60), C=-100 (×0.15=-15)
        # -25 + 60 - 15 = 20
        result = combine_bias(-100, 100, -100, cfg=self._cfg())
        self.assertAlmostEqual(result, 20.0, places=6)

    def test_custom_weights_effective(self):
        """自定义权重生效（改 T 权重影响最大）"""
        cfg1 = self._cfg(tw=0.9, fw=0.05, cw=0.05)  # T 权重高
        cfg2 = self._cfg(tw=0.1, fw=0.8, cw=0.1)   # T 权重低
        r1 = combine_bias(0, 100, 0, cfg=cfg1)  # T 高 → 结果大
        r2 = combine_bias(0, 100, 0, cfg=cfg2)  # T 低 → 结果小
        self.assertGreater(r1, r2)

    def test_rounds_to_one_decimal(self):
        """保留 1 位小数"""
        result = combine_bias(1, 1, 1, cfg=self._cfg(fw=0.333, tw=0.333, cw=0.334))
        self.assertAlmostEqual(result, 1.0, places=1)

    def test_f_c_zero_t_only(self):
        """F/C 为零时退化为 T 主导"""
        result = combine_bias(0, 50, 0, cfg=self._cfg())
        # 0.6 * 50 = 30
        self.assertAlmostEqual(result, 30.0, places=6)

    def test_negative_t(self):
        """T 为负 → 负偏置"""
        result = combine_bias(0, -50, 0, cfg=self._cfg())
        self.assertLess(result, 0)
        self.assertAlmostEqual(result, -30.0, places=6)


# ═══════════════════════════════════════════════════════════════════════════
#  2. regime_weights
# ═══════════════════════════════════════════════════════════════════════════

class TestRegimeWeights(unittest.TestCase):
    """regime_weights 市场状态策略权重。"""

    def test_trend_regime_trend_weight_one(self):
        """趋势态：趋势策略权重 = 1.0"""
        w = regime_weights("趋势")
        # 趋势策略权重应该是 1.0
        trend_strats = ["ma_break", "dma", "turtle", "donchian", "pullback"]
        for s in trend_strats:
            self.assertEqual(w.get(s), 1.0, f"{s} should be 1.0 in trend regime")

    def test_trend_regime_mean_weight_low(self):
        """趋势态：均值策略权重 = 0.3"""
        w = regime_weights("趋势")
        mean_strats = ["boll", "rsi"]
        for s in mean_strats:
            self.assertEqual(w.get(s), 0.3)

    def test_trend_regime_seasonal(self):
        """趋势态：seasonal = 0.2"""
        w = regime_weights("趋势")
        self.assertEqual(w.get("seasonal"), 0.2)

    def test_range_regime_mean_weight_one(self):
        """震荡态：均值策略权重 = 1.0"""
        w = regime_weights("震荡")
        mean_strats = ["boll", "rsi"]
        for s in mean_strats:
            self.assertEqual(w.get(s), 1.0)

    def test_range_regime_trend_weight_low(self):
        """震荡态：趋势策略权重 = 0.3"""
        w = regime_weights("震荡")
        trend_strats = ["ma_break", "dma", "turtle"]
        for s in trend_strats:
            self.assertEqual(w.get(s), 0.3)

    def test_range_regime_seasonal(self):
        """震荡态：seasonal = 0.3"""
        w = regime_weights("震荡")
        self.assertEqual(w.get("seasonal"), 0.3)

    def test_volatility_regime(self):
        """波动态：趋势 0.5，均值 0.2，seasonal 0.1"""
        w = regime_weights("波动")
        self.assertEqual(w.get("ma_break"), 0.5)
        self.assertEqual(w.get("boll"), 0.2)
        self.assertEqual(w.get("seasonal"), 0.1)

    def test_unknown_regime_half(self):
        """过渡/未知 → 全部 0.5"""
        w = regime_weights("过渡")
        for key, val in w.items():
            self.assertEqual(val, 0.5, f"{key} should be 0.5 in unknown regime")

    def test_unknown_string_half(self):
        """任意未知字符串 → 全部 0.5"""
        w = regime_weights("随便写的")
        for key, val in w.items():
            self.assertEqual(val, 0.5)

    def test_returns_dict(self):
        """返回 dict"""
        w = regime_weights("趋势")
        self.assertIsInstance(w, dict)
        self.assertGreater(len(w), 5)  # 至少 5 个策略

    def test_trend_trend_gt_range_trend(self):
        """趋势态趋势权重 > 震荡态趋势权重"""
        w_trend = regime_weights("趋势")
        w_range = regime_weights("震荡")
        self.assertGreater(w_trend["ma_break"], w_range["ma_break"])

    def test_range_mean_gt_trend_mean(self):
        """震荡态均值权重 > 趋势态均值权重"""
        w_trend = regime_weights("趋势")
        w_range = regime_weights("震荡")
        self.assertGreater(w_range["boll"], w_trend["boll"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. _norm_daily_cols
# ═══════════════════════════════════════════════════════════════════════════

class TestNormDailyCols(unittest.TestCase):
    """_norm_daily_cols 日线列名标准化。"""

    def test_english_columns(self):
        """英文列名：hold→oi, settle→settlement, open_interest→oi"""
        df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02"],
            "open": [100, 101],
            "high": [102, 103],
            "low": [99, 100],
            "close": [101, 102],
            "volume": [1000, 2000],
            "hold": [500, 600],
            "settle": [100.5, 101.5],
        })
        result = _norm_daily_cols(df)
        self.assertIn("oi", result.columns)
        self.assertIn("settlement", result.columns)
        self.assertNotIn("hold", result.columns)
        self.assertNotIn("settle", result.columns)

    def test_chinese_columns(self):
        """中文列名：日期/开盘/最高/最低/收盘/成交量/持仓量"""
        df = pd.DataFrame({
            "日期": ["2026-01-01", "2026-01-02"],
            "开盘": [100, 101],
            "最高": [102, 103],
            "最低": [99, 100],
            "收盘": [101, 102],
            "成交量": [1000, 2000],
            "持仓量": [500, 600],
        })
        result = _norm_daily_cols(df)
        self.assertIn("open", result.columns)
        self.assertIn("high", result.columns)
        self.assertIn("low", result.columns)
        self.assertIn("close", result.columns)
        self.assertIn("volume", result.columns)
        self.assertIn("oi", result.columns)

    def test_date_becomes_index(self):
        """date 列转 datetime 并设为索引"""
        df = pd.DataFrame({
            "date": ["2026-01-02", "2026-01-01"],  # 乱序
            "close": [101, 100],
        })
        result = _norm_daily_cols(df)
        self.assertIsInstance(result.index, pd.DatetimeIndex)
        self.assertEqual(result.index.name, "date")

    def test_sorted_by_date(self):
        """按日期排序"""
        df = pd.DataFrame({
            "date": ["2026-01-03", "2026-01-01", "2026-01-02"],
            "close": [102, 100, 101],
        })
        result = _norm_daily_cols(df)
        # 索引应该是升序
        self.assertTrue(result.index.is_monotonic_increasing)

    def test_empty_dataframe_returns_as_is(self):
        """空 DataFrame → 原样返回"""
        df = pd.DataFrame()
        result = _norm_daily_cols(df)
        self.assertEqual(len(result), 0)

    def test_none_returns_none(self):
        """None → 返回 None"""
        result = _norm_daily_cols(None)
        self.assertIsNone(result)

    def test_no_date_column_no_index(self):
        """无 date 列 → 不设索引"""
        df = pd.DataFrame({
            "close": [100, 101, 102],
            "volume": [1000, 2000, 3000],
        })
        result = _norm_daily_cols(df)
        self.assertNotIsInstance(result.index, pd.DatetimeIndex)

    def test_mixed_columns(self):
        """中英文混合也能识别"""
        df = pd.DataFrame({
            "date": ["2026-01-01"],
            "close": [100],
            "成交量": [1000],
            "hold": [500],
        })
        result = _norm_daily_cols(df)
        self.assertIn("volume", result.columns)
        self.assertIn("oi", result.columns)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  四维策略核心函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
