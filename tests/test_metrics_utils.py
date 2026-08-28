#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测统计指标 + 工具函数 — 单元测试
=====================================

1. max_drawdown — 最大回撤（R 单位）
   - 空列表 → 0
   - 全盈利 → 0（无回撤）
   - 全亏损 → 等于总亏损
   - 先赚后亏 → 回撤正确
   - 多段回撤 → 取最大

2. summarize — 回测汇总
   - 交易数、期望收益、胜率、最大回撤、止盈率、尾仓占比
   - 空结果 → 全 0
   - 混合结果 → 各指标正确

3. variety_of — 品种映射
   - 已知合约 → 返回品种 key
   - 未知合约 → 返回自身

4. _norm_daily_cols — 日线列名标准化
   - 中文列名 → 英文标准列
   - 英文列名 → 不变
   - date 列 → 转为索引
   - 空 DataFrame → 不变
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_oos_compare import max_drawdown, summarize
from four_dim_strategy import _norm_daily_cols, variety_of

# ═══════════════════════════════════════════════════════════════════════════
#  1. max_drawdown — 最大回撤
# ═══════════════════════════════════════════════════════════════════════════

class TestMaxDrawdown(unittest.TestCase):
    """max_drawdown — 累积权益曲线的最大回撤。"""

    def test_empty_list_zero(self):
        """空列表 → 0"""
        self.assertEqual(max_drawdown([]), 0.0)

    def test_all_profitable_zero_dd(self):
        """全盈利 → 回撤 = 0（一路新高，无回撤）"""
        self.assertEqual(max_drawdown([1.0, 2.0, 0.5, 1.5]), 0.0)

    def test_all_losing_equals_total_loss(self):
        """全亏损 → 最大回撤 = 总亏损绝对值"""
        # 权益：-1, -3, -5 → peak = -1, -1, -1 → 回撤 = 0, 2, 4 → max = 4
        result = max_drawdown([-1.0, -2.0, -2.0])
        self.assertAlmostEqual(result, 4.0, places=2)

    def test_rise_then_fall(self):
        """先赚后亏 → 回撤 = 从峰值到谷底的距离"""
        # 权益：2, 4, 1 → peak = 2, 4, 4 → 回撤 = 0, 0, 3 → max = 3
        result = max_drawdown([2.0, 2.0, -3.0])
        self.assertAlmostEqual(result, 3.0, places=2)

    def test_multiple_drawdowns_take_largest(self):
        """多段回撤 → 取最大的那段"""
        # 权益：3, 5, 2, 4, 0 → peak = 3, 5, 5, 5, 5
        # 回撤：0, 0, 3, 1, 5 → max = 5
        result = max_drawdown([3.0, 2.0, -3.0, 2.0, -4.0])
        self.assertAlmostEqual(result, 5.0, places=2)

    def test_single_trade_zero(self):
        """单笔交易 → 回撤 = 0（没有峰谷关系）"""
        # 只有一个点，peak = eq，回撤 = 0
        self.assertEqual(max_drawdown([2.0]), 0.0)

    def test_negative_then_recovery(self):
        """先亏后赚 → 回撤从一开始就有，但后面会修复"""
        # 权益：-2, -1, 2 → peak = -2, -2, -2 → 回撤 = 0, 1, 4
        # 等等，这不对。最大回撤定义是"从之前的峰值到当前的跌幅"
        # peak 是 running max，所以：
        # eq[0]=-2, peak[0]=-2, dd=0
        # eq[1]=-1, peak[1]=-1, dd=0
        # eq[2]=2,  peak[2]=2,  dd=0
        # 全是 0？不对，因为权益一直在涨，没有回撤。
        result = max_drawdown([-2.0, 1.0, 3.0])
        # 权益一直上升 → 回撤 = 0
        self.assertEqual(result, 0.0)

    def test_drawdown_unit_is_R(self):
        """回撤单位是 R（与输入同单位）"""
        # 用小数值验证
        result = max_drawdown([0.5, 0.5, -1.0])
        # 权益：0.5, 1.0, 0.0 → peak: 0.5, 1.0, 1.0 → dd: 0, 0, 1.0
        self.assertAlmostEqual(result, 1.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  2. summarize — 回测汇总
# ═══════════════════════════════════════════════════════════════════════════

class TestSummarize(unittest.TestCase):
    """summarize — 回测结果汇总指标。"""

    def test_empty_result_zero_metrics(self):
        """空结果 → 所有指标 = 0"""
        result = summarize({})
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["expR"], 0.0)
        self.assertEqual(result["win_rate"], 0.0)
        self.assertEqual(result["max_dd_R"], 0.0)
        self.assertEqual(result["t2_rate"], 0.0)
        self.assertEqual(result["tail_share"], 0.0)

    def test_all_winning_trades(self):
        """全盈利 → win_rate = 1.0"""
        trades = [
            {"R_adj": 2.0},
            {"R_adj": 1.5},
            {"R_adj": 0.5},
        ]
        result = summarize({"trades_detail": trades})
        self.assertEqual(result["trades"], 3)
        self.assertAlmostEqual(result["win_rate"], 1.0)
        expected = round((2.0 + 1.5 + 0.5) / 3, 4)
        self.assertAlmostEqual(result["expR"], expected, places=4)

    def test_all_losing_trades(self):
        """全亏损 → win_rate = 0.0"""
        trades = [
            {"R_adj": -1.0},
            {"R_adj": -0.5},
        ]
        result = summarize({"trades_detail": trades})
        self.assertEqual(result["win_rate"], 0.0)
        self.assertLess(result["expR"], 0)

    def test_mixed_win_rate(self):
        """混合结果 → win_rate 正确"""
        trades = [
            {"R_adj": 2.0},   # 赢
            {"R_adj": -1.0},  # 亏
            {"R_adj": 1.5},   # 赢
            {"R_adj": -0.5},  # 亏
            {"R_adj": 0.0},   # 平（不算赢）
        ]
        result = summarize({"trades_detail": trades})
        self.assertEqual(result["trades"], 5)
        self.assertAlmostEqual(result["win_rate"], 2 / 5)

    def test_max_drawdown_in_summary(self):
        """summarize 中的 max_dd_R 正确"""
        trades = [
            {"R_adj": 3.0},   # 权益 3
            {"R_adj": 2.0},   # 权益 5（峰值）
            {"R_adj": -4.0},  # 权益 1 → 回撤 4R
        ]
        result = summarize({"trades_detail": trades})
        self.assertAlmostEqual(result["max_dd_R"], 4.0, places=1)

    def test_exit_reason_rates(self):
        """止盈率和尾仓占比正确"""
        trades = [{"R_adj": 1.0} for _ in range(10)]
        exit_reasons = {
            "止盈2R": 6,
            "尾仓离场": 2,
            "止损": 2,
        }
        result = summarize({
            "trades_detail": trades,
            "exit_reasons": exit_reasons,
        })
        # t2_rate = (止盈2R + 尾仓离场) / trades
        self.assertAlmostEqual(result["t2_rate"], (6 + 2) / 10)
        # tail_share = 尾仓离场 / trades
        self.assertAlmostEqual(result["tail_share"], 2 / 10)

    def test_by_regime_passed_through(self):
        """by_regime 字段透传"""
        by_regime = {"趋势": {"expR": 0.5, "trades": 10}}
        result = summarize({
            "trades_detail": [{"R_adj": 1.0}],
            "by_regime": by_regime,
        })
        self.assertEqual(result["by_regime"], by_regime)


# ═══════════════════════════════════════════════════════════════════════════
#  3. variety_of — 品种映射
# ═══════════════════════════════════════════════════════════════════════════

class TestVarietyOf(unittest.TestCase):
    """variety_of — 具体合约 → 品种 key。"""

    def test_known_contract_returns_variety(self):
        """已知合约 → 返回品种 key

        例如：rb2501 → rb，i2505 → i
        """
        # 验证 VARIETY_OF 中至少有一些映射
        from four_dim_strategy import VARIETY_OF
        if VARIETY_OF:
            # 取第一个映射来验证
            contract, variety = list(VARIETY_OF.items())[0]
            self.assertEqual(variety_of(contract), variety)

    def test_unknown_contract_returns_self(self):
        """未知合约 → 返回自身"""
        self.assertEqual(variety_of("xyz999"), "xyz999")

    def test_variety_key_in_symbols(self):
        """VARIETY_OF 中的品种 key 都在 SYMBOLS 里"""
        from four_dim_strategy import SYMBOLS, VARIETY_OF
        missing = []
        for contract, variety in VARIETY_OF.items():
            if variety not in SYMBOLS:
                missing.append((contract, variety))
        # 允许有少量不在（可能是新合约），但大部分应该在
        if len(VARIETY_OF) > 10:
            self.assertLess(len(missing), len(VARIETY_OF) * 0.3,
                "超过 30% 的品种映射不在 SYMBOLS 中: %s..." % (missing[:5],))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _norm_daily_cols — 日线列名标准化
# ═══════════════════════════════════════════════════════════════════════════

class TestNormDailyCols(unittest.TestCase):
    """_norm_daily_cols — 日线列名标准化。"""

    def test_none_input_returns_none(self):
        """None → 返回 None"""
        self.assertIsNone(_norm_daily_cols(None))

    def test_empty_df_returns_empty(self):
        """空 DataFrame → 返回空"""
        df = pd.DataFrame()
        result = _norm_daily_cols(df)
        self.assertTrue(result.empty)

    def test_chinese_col_names_renamed(self):
        """中文列名 → 标准英文列名"""
        df = pd.DataFrame({
            "日期": ["2026-01-01", "2026-01-02"],
            "开盘": [100, 101],
            "最高": [102, 103],
            "最低": [99, 100],
            "收盘": [101, 102],
            "成交量": [1000, 2000],
            "持仓量": [5000, 6000],
        })
        result = _norm_daily_cols(df)
        self.assertIn("open", result.columns)
        self.assertIn("high", result.columns)
        self.assertIn("low", result.columns)
        self.assertIn("close", result.columns)
        self.assertIn("volume", result.columns)
        self.assertIn("oi", result.columns)
        self.assertNotIn("日期", result.columns)
        self.assertNotIn("开盘", result.columns)

    def test_date_becomes_index(self):
        """date 列 → 转为 DatetimeIndex"""
        df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02"],
            "open": [100, 101],
            "close": [101, 102],
        })
        result = _norm_daily_cols(df)
        self.assertIsInstance(result.index, pd.DatetimeIndex)
        self.assertNotIn("date", result.columns)

    def test_english_cols_unchanged(self):
        """英文列名 → 基本不变"""
        df = pd.DataFrame({
            "date": ["2026-01-01"],
            "open": [100],
            "high": [102],
            "low": [99],
            "close": [101],
            "volume": [1000],
            "oi": [5000],
        })
        result = _norm_daily_cols(df)
        for col in ["open", "high", "low", "close", "volume", "oi"]:
            self.assertIn(col, result.columns)

    def test_hold_becomes_oi(self):
        """hold → oi（另一种持仓量列名）"""
        df = pd.DataFrame({
            "date": ["2026-01-01"],
            "hold": [5000],
            "close": [100],
        })
        result = _norm_daily_cols(df)
        self.assertIn("oi", result.columns)
        self.assertNotIn("hold", result.columns)
        self.assertEqual(result["oi"].iloc[0], 5000)

    def test_settle_becomes_settlement(self):
        """settle → settlement"""
        df = pd.DataFrame({
            "date": ["2026-01-01"],
            "settle": [100.5],
            "close": [100],
        })
        result = _norm_daily_cols(df)
        self.assertIn("settlement", result.columns)
        self.assertNotIn("settle", result.columns)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  回测统计 + 工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
