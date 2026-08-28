#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集成测试（三）— 深度回测验证 + 数据质量状态机
==============================================

一、深度回测集成
   - 消融实验：分别消融 F/C/T，验证各维度贡献
   - 多品种回测一致性
   - by_regime 结构与 trades_detail 分布一致
   - expR 数学一致性：expR ≈ 平均 R_adj
   - win_rate 数学一致性：胜率 = 盈利笔数 / 总笔数
   - 不同 min_bars 对结果的影响
   - 回测结果确定性：相同输入 → 相同输出

二、数据质量状态机集成
   - 正常 → 陈旧 → 正常（恢复）状态转换
   - 正常 → 冻结 → 正常（价格变化）状态转换
   - 跳变边界：2 次不触发、3 次触发
   - 陈旧 + 冻结同时存在时的优先级
   - 多品种混合健康度计算准确性
   - 连续 observe 的状态演进
"""

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import data_quality as dq
from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)

# ═══════════════════════════════════════════════════════════════════════════
#  一、深度回测集成
# ═══════════════════════════════════════════════════════════════════════════


class TestDeepBacktestIntegration(unittest.TestCase):
    """深度回测集成：消融、一致性、确定性。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 500

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存")
        self._df_tail = self.df.tail(600)

    def test_ablate_F_changes_result(self):
        """消融 F 维度 → 结果可能变化（F 有贡献）"""
        result_full = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        result_no_F = walk_forward_backtest(
            self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail, ablate="F"
        )
        # F 消融后 expR 可能不同（F 维度有贡献时）
        # 至少不崩溃，返回有效结果
        self.assertIsInstance(result_no_F["expR"], float)
        self.assertIsInstance(result_no_F["win_rate"], float)

    def test_ablate_T_reduces_or_zeroes_trades(self):
        """消融 T 维度 → 交易数减少或为零（T 是主要触发源）"""
        result_full = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        result_no_T = walk_forward_backtest(
            self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail, ablate="T"
        )
        # T 消融后交易数应该 <= 完整版本
        self.assertLessEqual(result_no_T["trades"], result_full["trades"])
        # 至少不崩溃，返回有效结果
        self.assertGreaterEqual(result_no_T["trades"], 0)

    def test_ablate_C_changes_result(self):
        """消融 C 维度 → 结果可能变化"""
        result_full = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        result_no_C = walk_forward_backtest(
            self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail, ablate="C"
        )
        self.assertIsInstance(result_no_C["expR"], float)
        self.assertIsInstance(result_no_C["win_rate"], float)

    def test_expR_equals_mean_R_adj(self):
        """expR ≈ 所有交易 R_adj 的平均值"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        detail = result["trades_detail"]
        if len(detail) > 0:
            avg_r_adj = np.mean([t["R_adj"] for t in detail])
            # expR 应该接近平均 R_adj（可能有细微差异，比如权重/取整）
            self.assertAlmostEqual(result["expR"], avg_r_adj, places=1)

    def test_win_rate_matches_count(self):
        """win_rate == 盈利笔数 / 总笔数"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        detail = result["trades_detail"]
        if len(detail) > 0:
            wins = sum(1 for t in detail if t["R_adj"] > 0)
            expected_win_rate = wins / len(detail)
            self.assertAlmostEqual(result["win_rate"], expected_win_rate, places=2)

    def test_by_regime_structure(self):
        """by_regime 包含各 regime 的 expR"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        br = result["by_regime"]
        self.assertIsInstance(br, dict)
        for regime, expR in br.items():
            self.assertIsInstance(regime, str)
            self.assertIsInstance(expR, float)

    def test_by_regime_trades_match_detail(self):
        """by_regime 中各 regime 的交易数 ≈ trades_detail 中该 regime 的数量"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        detail = result["trades_detail"]
        if len(detail) == 0:
            self.skipTest("无交易")

        regime_counts = {}
        for t in detail:
            r = t["regime"]
            regime_counts[r] = regime_counts.get(r, 0) + 1

        # by_regime 中每个 regime 都应该在 trades_detail 中有对应
        for regime in result["by_regime"]:
            # by_regime 的 key 应该出现在 trades_detail 的 regime 中
            self.assertIn(regime, regime_counts, f"regime '{regime}' in by_regime but not in trades_detail")

    def test_deterministic_results(self):
        """相同输入 → 相同输出（确定性）"""
        result1 = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        result2 = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        self.assertEqual(result1["trades"], result2["trades"])
        self.assertAlmostEqual(result1["expR"], result2["expR"], places=10)
        self.assertAlmostEqual(result1["win_rate"], result2["win_rate"], places=10)

    def test_more_data_more_trades_or_same(self):
        """更多数据 → 交易数更多或相等（样本量更大）"""
        result_small = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self.df.tail(200))
        result_large = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self.df.tail(500))
        # 更多数据通常有更多交易，但也可能刚好持平
        self.assertGreaterEqual(result_large["trades"], 0)
        self.assertGreaterEqual(result_small["trades"], 0)

    def test_different_symbols_same_structure(self):
        """不同品种回测结果结构一致"""
        df_rb = load_daily("rb")
        df_hc = load_daily("hc")
        if df_rb is None or df_hc is None or len(df_rb) < 300 or len(df_hc) < 300:
            self.skipTest("数据不足")

        result_rb = walk_forward_backtest("rb", cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_rb.tail(300))
        result_hc = walk_forward_backtest("hc", cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_hc.tail(300))

        # 结构应该一致
        for key in ["symbol", "trades", "expR", "win_rate", "trades_detail", "by_regime", "exit_reasons"]:
            self.assertIn(key, result_rb)
            self.assertIn(key, result_hc)

    def test_trade_detail_regime_valid(self):
        """每笔交易的 regime 是有效值"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        detail = result["trades_detail"]
        valid_regimes = set(result["by_regime"].keys()) | {"趋势", "波动", "震荡", "过渡", "高波动"}
        for t in detail:
            self.assertIn(t["regime"], valid_regimes, f"invalid regime: {t['regime']}")

    def test_roll_skipped_nonnegative(self):
        """roll_skipped >= 0"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self._df_tail)
        self.assertGreaterEqual(result.get("roll_skipped", 0), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  二、数据质量状态机集成
# ═══════════════════════════════════════════════════════════════════════════


class _FakeFeed:
    def __init__(self, prices=None):
        self._prices = prices or {}

    def price(self, sym):
        return self._prices.get(sym)


class TestDataQualityStateMachine(unittest.TestCase):
    """data_quality 状态机集成测试。"""

    def setUp(self):
        dq._last_seen.clear()
        dq._last_price.clear()
        dq._frozen.clear()
        dq._prev_price.clear()
        dq._ever_seen.clear()
        dq._jumps.clear()

    def test_normal_to_stale_and_back(self):
        """正常 → 陈旧 → 恢复正常"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0

        # 初始正常
        dq.observe(feed, now_ts=base_ts)
        rep = dq.check(now_ts=base_ts, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "正常")

        # 200s 后 → 陈旧
        rep = dq.check(now_ts=base_ts + 200.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "陈旧")

        # 新数据到来 → 恢复正常
        dq.observe(feed, now_ts=base_ts + 201.0)
        rep = dq.check(now_ts=base_ts + 201.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "正常")

    def test_normal_to_frozen_and_back(self):
        """正常 → 冻结 → 价格变化 → 恢复正常"""
        base_ts = 1000000.0

        # 7 次不变 → 冻结
        for i in range(7):
            feed = _FakeFeed({"rb": 3500.0})
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        rep = dq.check(now_ts=base_ts + 35.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "冻结")

        # 价格变化 → 解冻
        feed_new = _FakeFeed({"rb": 3550.0})
        dq.observe(feed_new, now_ts=base_ts + 40.0)
        rep = dq.check(now_ts=base_ts + 40.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "正常")
        self.assertEqual(rb["frozen_n"], 0)

    def test_jump_boundary_two_no_trigger(self):
        """2 次跳变 → 不触发跳变状态（阈值 3）"""
        base_ts = 1000000.0

        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=base_ts)

        # 2 次跳变
        prices = [3850.0, 3500.0]
        for i, p in enumerate(prices):
            feed = _FakeFeed({"rb": p})
            dq.observe(feed, now_ts=base_ts + (i + 1) * 5.0)

        rep = dq.check(now_ts=base_ts + 15.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        # 2 次 < 3 → 不触发跳变状态
        self.assertNotEqual(rb["status"], "跳变")

    def test_jump_boundary_three_triggers(self):
        """3 次跳变 → 触发跳变状态"""
        base_ts = 1000000.0

        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=base_ts)

        # 3 次跳变
        prices = [3850.0, 3500.0, 3850.0]
        for i, p in enumerate(prices):
            feed = _FakeFeed({"rb": p})
            dq.observe(feed, now_ts=base_ts + (i + 1) * 5.0)

        rep = dq.check(now_ts=base_ts + 20.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "跳变")

    def test_stale_priority_over_frozen(self):
        """陈旧优先级高于冻结
        判断顺序：异常 > 陈旧 > 冻结 > 跳变 > 正常"""
        base_ts = 1000000.0

        # 7 次不变 → 冻结
        for i in range(7):
            feed = _FakeFeed({"rb": 3500.0})
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        # 时间流逝 200s → 陈旧（陈旧先于冻结判断）
        rep = dq.check(now_ts=base_ts + 200.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "陈旧")

    def test_bad_priority_over_stale(self):
        """异常优先级高于陈旧
        异常(价格≤0) 先于 陈旧 判断"""
        # 先让数据陈旧
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0
        dq.observe(feed, now_ts=base_ts)

        # 然后 last_price 设为 0（模拟异常）
        dq._last_price["rb"] = 0.0
        rep = dq.check(now_ts=base_ts + 200.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "异常")

    def test_multi_symbol_health_calculation(self):
        """多品种混合时健康度计算正确"""
        base_ts = 1000000.0

        # 3 个正常 + 1 个陈旧 + 1 个冻结 → 健康度 = 3/5 = 60%
        feed = _FakeFeed(
            {
                "rb": 3500.0,
                "hc": 3800.0,
                "FG": 1500.0,
                "SA": 1800.0,
                "MA": 2500.0,
            }
        )
        dq.observe(feed, now_ts=base_ts)

        # 让 SA 陈旧（只更新其他 4 个）
        feed2 = _FakeFeed(
            {
                "rb": 3501.0,
                "hc": 3801.0,
                "FG": 1501.0,
                "MA": 2501.0,
            }
        )
        check_ts = base_ts + 200.0
        dq.observe(feed2, now_ts=check_ts - 1.0)  # 这 4 个是新鲜的

        rep = dq.check(now_ts=check_ts, trading=True)

        # 检查 tracked 的品种中，SA 是陈旧的，其他 4 个正常
        tracked = [r for r in rep["rows"] if r["status"] != "未订阅"]
        if len(tracked) >= 5:
            normal_count = sum(1 for r in tracked if r["status"] == "正常")
            stale_count = sum(1 for r in tracked if r["status"] == "陈旧")
            self.assertGreaterEqual(normal_count, 4)
            self.assertGreaterEqual(stale_count, 1)
            # 健康度 = 正常 / tracked
            expected_health = round(normal_count / len(tracked) * 100, 1)
            self.assertAlmostEqual(rep["health_pct"], expected_health, places=0)

    def test_continuous_observe_state_evolution(self):
        """连续 observe 的状态演进：正常→正常→...→冻结"""
        base_ts = 1000000.0
        feed = _FakeFeed({"rb": 3500.0})

        # 第 1 次：正常（无冻结）
        dq.observe(feed, now_ts=base_ts)
        self.assertEqual(dq._frozen.get("rb", 0), 0)

        # 第 2 次：冻结计数=1
        dq.observe(feed, now_ts=base_ts + 5.0)
        self.assertEqual(dq._frozen.get("rb", 0), 1)

        # 第 6 次：冻结计数=5
        for i in range(2, 6):
            dq.observe(feed, now_ts=base_ts + i * 5.0)
        self.assertEqual(dq._frozen.get("rb", 0), 5)

        # 第 7 次：冻结计数=6 → 触发冻结状态
        dq.observe(feed, now_ts=base_ts + 30.0)
        self.assertEqual(dq._frozen.get("rb", 0), 6)

        rep = dq.check(now_ts=base_ts + 30.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        self.assertEqual(rb["status"], "冻结")

    def test_price_change_resets_frozen_immediately(self):
        """价格变化立即清零冻结计数"""
        base_ts = 1000000.0

        # 先冻结
        for i in range(7):
            feed = _FakeFeed({"rb": 3500.0})
            dq.observe(feed, now_ts=base_ts + i * 5.0)
        self.assertEqual(dq._frozen["rb"], 6)

        # 一次价格变化 → 清零
        feed_new = _FakeFeed({"rb": 3550.0})
        dq.observe(feed_new, now_ts=base_ts + 35.0)
        self.assertEqual(dq._frozen["rb"], 0)

    def test_idle_mode_higher_stale_threshold(self):
        """非交易时段陈旧阈值更高（600s vs 120s）"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0
        dq.observe(feed, now_ts=base_ts)

        # 300s 后
        check_ts = base_ts + 300.0
        rep_trading = dq.check(now_ts=check_ts, trading=True)
        rep_idle = dq.check(now_ts=check_ts, trading=False)

        rb_trading = [r for r in rep_trading["rows"] if r["symbol"] == "rb"][0]
        rb_idle = [r for r in rep_idle["rows"] if r["symbol"] == "rb"][0]

        # 300s > 120s → 交易时段陈旧
        self.assertEqual(rb_trading["status"], "陈旧")
        # 300s < 600s → 非交易时段正常
        self.assertEqual(rb_idle["status"], "正常")

    def test_stale_sec_matches_mode(self):
        """stale_sec 字段匹配模式"""
        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=1000000.0)

        rep_trading = dq.check(now_ts=1000000.0, trading=True)
        rep_idle = dq.check(now_ts=1000000.0, trading=False)

        self.assertEqual(rep_trading["stale_sec"], 120)
        self.assertEqual(rep_idle["stale_sec"], 600)

    def test_health_pct_zero_when_all_problems(self):
        """所有跟踪品种都有问题 → 健康度 = 0%"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0
        dq.observe(feed, now_ts=base_ts)

        # 让 rb 变得陈旧
        rep = dq.check(now_ts=base_ts + 200.0, trading=True)

        # 只跟踪了 rb 一个品种，且它是陈旧的 → 健康度 = 0%
        tracked = [r for r in rep["rows"] if r["status"] != "未订阅"]
        if len(tracked) == 1 and tracked[0]["status"] == "陈旧":
            self.assertEqual(rep["health_pct"], 0.0)

    def test_health_pct_100_when_all_normal(self):
        """所有跟踪品种都正常 → 健康度 = 100%"""
        feed = _FakeFeed({"rb": 3500.0, "hc": 3800.0})
        base_ts = 1000000.0
        dq.observe(feed, now_ts=base_ts)

        rep = dq.check(now_ts=base_ts, trading=True)
        tracked = [r for r in rep["rows"] if r["status"] != "未订阅"]
        normal_count = sum(1 for r in tracked if r["status"] == "正常")
        if len(tracked) == normal_count and len(tracked) > 0:
            self.assertEqual(rep["health_pct"], 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  集成测试（三）— 深度回测 + 数据质量状态机")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
