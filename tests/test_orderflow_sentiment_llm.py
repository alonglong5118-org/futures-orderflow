#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单流 + 情绪日度 + LLM prompt — 单元测试
=================================================

1. _side_from_tick — 主动买卖方向判定
   - 显式买(B/b/buy/1) → 1
   - 显式卖(S/s/sell/-1) → -1
   - 价格高于昨收 → 主动买 1
   - 价格低于昨收 → 主动卖 -1
   - 价格不变且无方向 → 0
   - last=None 且 side 不明 → 0
   - side 是整数 1/-1
   - side 大小写不敏感

2. build_sentiment_daily — 每日情绪快照
   - 数据不足 60 根 → 空 dict
   - 上涨趋势 → 贪婪/极度贪婪
   - 下跌趋势 → 恐惧/极度恐惧
   - 横盘 → 中性
   - 分数钳位 [0, 100]
   - 每一项含 score/band/label
   - key 是 YYYYMMDD 字符串
   - 5 档分类全覆盖

3. _build_llm_prompt — LLM 提示词构造
   - 包含 bullets 内容
   - 包含 signal reason
   - 包含角色描述
   - 包含"情景分析"提示
   - 空 bullets 也能生成
   - 空 reason 也能生成
   - 返回非空字符串
"""

import sys
import os
import unittest
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tick_orderflow import _side_from_tick
from sentiment_sr_combined_bt import build_sentiment_daily
from signal_explain import _build_llm_prompt


# ═══════════════════════════════════════════════════════════════════════════
#  1. _side_from_tick
# ═══════════════════════════════════════════════════════════════════════════

class TestSideFromTick(unittest.TestCase):
    """_side_from_tick 主动买卖方向判定。"""

    def test_explicit_buy_B(self):
        """显式买 B → 1"""
        self.assertEqual(_side_from_tick(100.0, 99.0, "B"), 1)

    def test_explicit_buy_lowercase(self):
        """显式买 b → 1（大小写不敏感）"""
        self.assertEqual(_side_from_tick(100.0, 99.0, "b"), 1)

    def test_explicit_buy_string(self):
        """显式买 buy → 1"""
        self.assertEqual(_side_from_tick(100.0, 99.0, "buy"), 1)

    def test_explicit_buy_int_one(self):
        """显式买 1 → 1"""
        self.assertEqual(_side_from_tick(100.0, 99.0, 1), 1)

    def test_explicit_sell_S(self):
        """显式卖 S → -1"""
        self.assertEqual(_side_from_tick(100.0, 101.0, "S"), -1)

    def test_explicit_sell_lowercase(self):
        """显式卖 s → -1"""
        self.assertEqual(_side_from_tick(100.0, 101.0, "s"), -1)

    def test_explicit_sell_string(self):
        """显式卖 sell → -1"""
        self.assertEqual(_side_from_tick(100.0, 101.0, "sell"), -1)

    def test_explicit_sell_int_minus_one(self):
        """显式卖 -1 → -1"""
        self.assertEqual(_side_from_tick(100.0, 101.0, -1), -1)

    def test_price_up_inferred_buy(self):
        """价格高于昨收 → 主动买 1"""
        self.assertEqual(_side_from_tick(101.0, 100.0, None), 1)

    def test_price_down_inferred_sell(self):
        """价格低于昨收 → 主动卖 -1"""
        self.assertEqual(_side_from_tick(99.0, 100.0, None), -1)

    def test_price_unchanged_zero(self):
        """价格不变且无方向 → 0"""
        self.assertEqual(_side_from_tick(100.0, 100.0, None), 0)

    def test_last_none_unknown_zero(self):
        """last=None 且 side 不明 → 0"""
        self.assertEqual(_side_from_tick(100.0, None, "unknown"), 0)

    def test_explicit_overrides_price(self):
        """显式方向优先于价格推断"""
        # 价格上涨但 side=S → 仍然是卖
        self.assertEqual(_side_from_tick(101.0, 100.0, "S"), -1)
        # 价格下跌但 side=B → 仍然是买
        self.assertEqual(_side_from_tick(99.0, 100.0, "B"), 1)

    def test_returns_int(self):
        """返回 int"""
        result = _side_from_tick(100.0, 99.0, "B")
        self.assertIsInstance(result, int)


# ═══════════════════════════════════════════════════════════════════════════
#  2. build_sentiment_daily
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildSentimentDaily(unittest.TestCase):
    """build_sentiment_daily 每日情绪快照。"""

    def _make_df(self, prices, start_date="2026-01-01"):
        dates = pd.date_range(start=start_date, periods=len(prices), freq="D")
        return pd.DataFrame({"close": prices}, index=dates)

    def test_insufficient_data_empty(self):
        """数据不足 60 根 → 空 dict"""
        df = self._make_df([100 + i for i in range(30)])
        result = build_sentiment_daily(df)
        self.assertEqual(result, {})

    def test_uptrend_extreme_greed(self):
        """强上涨 → 极度贪婪"""
        # 60 根 + 后续大涨
        prices = [100.0] * 60 + [130.0]  # 第 61 天涨幅 30% (相对 20 天前)
        # 不对，20 日涨跌幅是相对 i-20 的
        # 让我构造：前 60 根 100，第 61 根 = 120
        # chg_20d = (120/100 - 1)*100 = 20%
        # score = 50 + 20*3 = 110 → clamp to 100 → extreme_greed
        df = self._make_df([100.0] * 60 + [120.0])
        result = build_sentiment_daily(df)
        self.assertGreater(len(result), 0)
        # 取最后一天
        last_key = sorted(result.keys())[-1]
        last = result[last_key]
        self.assertEqual(last["band"], "extreme_greed")
        self.assertEqual(last["label"], "极度贪婪")
        self.assertEqual(last["score"], 100.0)

    def test_downtrend_extreme_fear(self):
        """强下跌 → 极度恐惧"""
        # 前 60 根 100，第 61 根 = 80
        # chg_20d = (80/100 - 1)*100 = -20%
        # score = 50 + (-20)*3 = -10 → clamp to 0 → extreme_fear
        df = self._make_df([100.0] * 60 + [80.0])
        result = build_sentiment_daily(df)
        last_key = sorted(result.keys())[-1]
        last = result[last_key]
        self.assertEqual(last["band"], "extreme_fear")
        self.assertEqual(last["label"], "极度恐惧")
        self.assertEqual(last["score"], 0.0)

    def test_flat_neutral(self):
        """横盘 → 中性"""
        # 全部 100 → chg_20d = 0 → score = 50 → neutral
        df = self._make_df([100.0] * 80)
        result = build_sentiment_daily(df)
        last_key = sorted(result.keys())[-1]
        last = result[last_key]
        self.assertEqual(last["band"], "neutral")
        self.assertEqual(last["label"], "中性")
        self.assertEqual(last["score"], 50.0)

    def test_score_clamped_zero_to_hundred(self):
        """分数钳位 [0, 100]"""
        df = self._make_df([100.0] * 60 + [50.0])  # 暴跌 50%
        result = build_sentiment_daily(df)
        last_key = sorted(result.keys())[-1]
        last = result[last_key]
        self.assertGreaterEqual(last["score"], 0.0)
        self.assertLessEqual(last["score"], 100.0)

    def test_each_entry_has_three_fields(self):
        """每一项含 score/band/label"""
        df = self._make_df([100.0 + i * 0.5 for i in range(80)])
        result = build_sentiment_daily(df)
        for key, val in result.items():
            self.assertIn("score", val)
            self.assertIn("band", val)
            self.assertIn("label", val)
            self.assertIsInstance(val["score"], float)

    def test_keys_are_date_strings(self):
        """key 是 YYYYMMDD 字符串"""
        df = self._make_df([100.0] * 70)
        result = build_sentiment_daily(df)
        for key in result.keys():
            self.assertEqual(len(key), 8)
            self.assertTrue(key.isdigit())

    def test_five_bands_coverage(self):
        """5 档分类全覆盖（通过不同涨跌幅构造）"""
        # score = 50 + chg_20d * 3
        # extreme_greed: >=80 → chg >= 10%
        # greed: 65-80 → chg 5% ~ 10%
        # neutral: 35-65 → chg -5% ~ 5%
        # fear: 20-35 → chg -10% ~ -5%
        # extreme_fear: <=20 → chg <= -10%
        bands_seen = set()
        for chg_pct in [20, 7, 2, -3, -8, -25]:
            prices = [100.0] * 60 + [100.0 * (1 + chg_pct / 100)]
            df = self._make_df(prices)
            result = build_sentiment_daily(df)
            if result:
                last_key = sorted(result.keys())[-1]
                bands_seen.add(result[last_key]["band"])
        self.assertIn("extreme_greed", bands_seen)
        self.assertIn("greed", bands_seen)
        self.assertIn("neutral", bands_seen)
        self.assertIn("fear", bands_seen)
        self.assertIn("extreme_fear", bands_seen)

    def test_score_rounded_one_decimal(self):
        """score 保留 1 位小数"""
        df = self._make_df([100.0 + i * 0.3 for i in range(80)])
        result = build_sentiment_daily(df)
        last_key = sorted(result.keys())[-1]
        score = result[last_key]["score"]
        # 检查是 round 到 1 位小数
        self.assertEqual(score, round(score, 1))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _build_llm_prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildLLMPrompt(unittest.TestCase):
    """_build_llm_prompt LLM 提示词构造。"""

    def test_contains_bullets(self):
        """包含 bullets 内容"""
        bullets = ["技术面做多", "基本面偏多", "资金流正向"]
        sig = {"reason": "测试信号"}
        p = {}
        result = _build_llm_prompt(sig, p, bullets)
        for b in bullets:
            self.assertIn(b, result)

    def test_contains_signal_reason(self):
        """包含 signal reason"""
        bullets = ["a", "b"]
        sig = {"reason": "MA突破+基本面共振"}
        p = {}
        result = _build_llm_prompt(sig, p, bullets)
        self.assertIn("MA突破+基本面共振", result)

    def test_contains_role_description(self):
        """包含角色描述（期货风控教练）"""
        result = _build_llm_prompt({"reason": "x"}, {}, ["y"])
        self.assertIn("期货风控教练", result)

    def test_contains_scenario_disclaimer(self):
        """包含"情景分析"提示"""
        result = _build_llm_prompt({"reason": "x"}, {}, ["y"])
        self.assertIn("情景分析", result)

    def test_empty_bullets_still_works(self):
        """空 bullets 也能生成"""
        result = _build_llm_prompt({"reason": "test"}, {}, [])
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_empty_reason_still_works(self):
        """空 reason 也能生成"""
        result = _build_llm_prompt({}, {}, ["bullet1"])
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_returns_nonempty_string(self):
        """返回非空字符串"""
        result = _build_llm_prompt(
            {"reason": "测试"},
            {"T_5m": 30, "regime": "趋势"},
            ["技术面做多", "基本面偏多"]
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 50)

    def test_concise_limit_mentioned(self):
        """提到字数限制（不超过120字）"""
        result = _build_llm_prompt({"reason": "x"}, {}, ["y"])
        self.assertIn("120", result)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  订单流 + 情绪日度 + LLM prompt — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
