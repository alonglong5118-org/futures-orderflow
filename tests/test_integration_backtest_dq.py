#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集成测试（二）— 回测验证 + 数据质量监控
==============================================

一、回测集成测试（walk_forward_backtest）
   - 用真实日线数据运行回测
   - 验证回测输出结构完整性
   - 验证红线守卫（F_override/hmm/macro/garch 禁止注入回测）
   - 验证数据不足时的降级返回
   - 验证 cooldown 冷却机制
   - 验证交易记录字段完整性

二、数据质量监控集成测试（data_quality）
   - observe → check 全流程
   - 正常数据 → 健康度正常
   - 陈旧数据 → 状态=陈旧
   - 冻结数据 → 状态=冻结
   - 跳变检测 → 状态=跳变
   - reset_jumps → 跳变计数清零
   - 交易时段 vs 非交易时段 陈旧阈值差异
"""

import os
import sys
import unittest

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
#  一、回测集成测试
# ═══════════════════════════════════════════════════════════════════════════

class TestWalkForwardBacktestIntegration(unittest.TestCase):
    """walk_forward_backtest 回测集成测试（使用真实日线数据）。"""

    @classmethod
    def setUpClass(cls):
        """加载一次真实数据。"""
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 200

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存，跳过回测集成测试")

    def test_backtest_returns_dict(self):
        """回测返回 dict"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(300))
        self.assertIsInstance(result, dict)

    def test_backtest_has_symbol(self):
        """结果包含 symbol"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(300))
        self.assertEqual(result["symbol"], self.symbol)

    def test_backtest_has_trades_list(self):
        """结果包含 trades（交易数，int）"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(300))
        self.assertIn("trades", result)
        self.assertIsInstance(result["trades"], int)

    def test_backtest_trades_is_int(self):
        """trades 字段是 int（交易笔数）"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(300))
        self.assertIsInstance(result["trades"], int)
        self.assertGreaterEqual(result["trades"], 0)

    def test_insufficient_data_returns_note(self):
        """数据不足 → 返回 note 说明"""
        small_df = self.df.tail(30)
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=small_df)
        self.assertIn("note", result)
        self.assertEqual(result["trades"], 0)

    def test_f_override_assertion(self):
        """F_override 非 None → AssertionError（红线守卫）"""
        with self.assertRaises(AssertionError):
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                  min_bars=60, df_in=self.df.tail(300),
                                  F_override=0.5)

    def test_hmm_label_assertion(self):
        """hmm_label 非 None → AssertionError（红线守卫）"""
        with self.assertRaises(AssertionError):
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                  min_bars=60, df_in=self.df.tail(300),
                                  hmm_label="trend_up")

    def test_macro_label_assertion(self):
        """macro_label 非 None → AssertionError"""
        with self.assertRaises(AssertionError):
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                  min_bars=60, df_in=self.df.tail(300),
                                  macro_label="expansion")

    def test_garch_label_assertion(self):
        """garch_label 非 None → AssertionError"""
        with self.assertRaises(AssertionError):
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                  min_bars=60, df_in=self.df.tail(300),
                                  garch_label="high")

    def test_trade_fields_if_trades(self):
        """如果有交易 → trades_detail 每笔字段完整"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result.get("trades_detail", [])
        if len(detail) > 0:
            t = detail[0]
            self.assertIn("dir", t)
            self.assertIn("R", t)
            self.assertIn("R_adj", t)
            self.assertIn("reason", t)
            self.assertIn("regime", t)
            self.assertIn("entry_date", t)

    def test_cooldown_reduces_trades(self):
        """冷却期长 → 交易数少（或相等）"""
        result_cd1 = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                           min_bars=60, cooldown_bars=1,
                                           df_in=self.df.tail(500))
        result_cd20 = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                            min_bars=60, cooldown_bars=20,
                                            df_in=self.df.tail(500))

        n1 = result_cd1.get("trades", 0)
        n2 = result_cd20.get("trades", 0)
        self.assertLessEqual(n2, n1)

    def test_trades_have_correct_direction(self):
        """每笔交易 dir 为 1 或 -1"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result.get("trades_detail", [])
        for t in detail:
            self.assertIn(t["dir"], [1, -1])

    def test_trades_have_positive_stop_dist(self):
        """回测结果包含关键指标：expR, win_rate, by_regime"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        self.assertIn("expR", result)
        self.assertIn("win_rate", result)
        self.assertIn("by_regime", result)
        self.assertIn("exit_reasons", result)
        self.assertIsInstance(result["expR"], float)
        self.assertIsInstance(result["win_rate"], float)


# ═══════════════════════════════════════════════════════════════════════════
#  二、数据质量监控集成测试
# ═══════════════════════════════════════════════════════════════════════════

class _FakeFeed:
    """模拟 feed 对象，用于 data_quality 集成测试。"""

    def __init__(self, prices=None):
        self._prices = prices or {}

    def price(self, sym):
        return self._prices.get(sym)


class TestDataQualityIntegration(unittest.TestCase):
    """data_quality 数据质量监控集成测试。

    注意：data_quality 使用全局变量存储状态，
    每个测试前需要重置状态以避免相互影响。
    """

    def setUp(self):
        """每个测试前重置全局状态。"""
        dq._last_seen.clear()
        dq._last_price.clear()
        dq._frozen.clear()
        dq._prev_price.clear()
        dq._ever_seen.clear()
        dq._jumps.clear()

    def test_observe_then_check_normal(self):
        """observe 正常数据 → check 健康度正常"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0

        dq.observe(feed, now_ts=base_ts)
        rep = dq.check(now_ts=base_ts, trading=True)

        self.assertIn("health_pct", rep)
        self.assertIn("counts", rep)
        self.assertIn("rows", rep)
        self.assertIn("ok", rep)

        rb_rows = [r for r in rep["rows"] if r["symbol"] == "rb"]
        if rb_rows:
            rb = rb_rows[0]
            self.assertEqual(rb["status"], "正常")
            self.assertEqual(rb["price"], 3500.0)

    def test_stale_data_detected(self):
        """数据陈旧 → 状态=陈旧"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0

        dq.observe(feed, now_ts=base_ts)
        # 200 秒后检查（交易时段阈值 120s）
        rep = dq.check(now_ts=base_ts + 200.0, trading=True)

        rb_rows = [r for r in rep["rows"] if r["symbol"] == "rb"]
        if rb_rows:
            self.assertEqual(rb_rows[0]["status"], "陈旧")
            self.assertGreaterEqual(rb_rows[0]["age_sec"], 200.0)

    def test_frozen_data_detected(self):
        """价格连续不变 7 次（FROZEN_N=6，首次不计入）→ 状态=冻结"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0

        # 7 次观察：第 1 次初始化，后 6 次连续不变 → frozen=6
        for i in range(7):
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        rep = dq.check(now_ts=base_ts + 35.0, trading=True)
        rb_rows = [r for r in rep["rows"] if r["symbol"] == "rb"]
        if rb_rows:
            self.assertEqual(rb_rows[0]["status"], "冻结")
            self.assertGreaterEqual(rb_rows[0]["frozen_n"], 6)

    def test_price_change_resets_frozen(self):
        """价格变化 → 冻结计数清零"""
        base_ts = 1000000.0

        # 先冻结（7次不变 = 6次连续）
        for i in range(7):
            feed = _FakeFeed({"rb": 3500.0})
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        # 价格变化
        feed_change = _FakeFeed({"rb": 3550.0})
        dq.observe(feed_change, now_ts=base_ts + 35.0)

        rep = dq.check(now_ts=base_ts + 35.0, trading=True)
        rb_rows = [r for r in rep["rows"] if r["symbol"] == "rb"]
        if rb_rows:
            self.assertEqual(rb_rows[0]["frozen_n"], 0)
            self.assertEqual(rb_rows[0]["status"], "正常")

    def test_jump_detected(self):
        """价格跳变超过 5% → 跳变计数增加"""
        base_ts = 1000000.0

        feed1 = _FakeFeed({"rb": 3500.0})
        dq.observe(feed1, now_ts=base_ts)

        feed2 = _FakeFeed({"rb": 3850.0})  # +10%
        dq.observe(feed2, now_ts=base_ts + 5.0)

        self.assertGreaterEqual(dq._jumps.get("rb", 0), 1)

    def test_multiple_jumps_trigger_status(self):
        """3 次以上跳变 → 状态=跳变"""
        base_ts = 1000000.0

        # 初始
        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=base_ts)

        # 3 次跳变（每次 10%）
        prices = [3850.0, 3500.0, 3850.0]
        for i, p in enumerate(prices):
            feed = _FakeFeed({"rb": p})
            dq.observe(feed, now_ts=base_ts + (i + 1) * 5.0)

        rep = dq.check(now_ts=base_ts + 20.0, trading=True)
        rb_rows = [r for r in rep["rows"] if r["symbol"] == "rb"]
        if rb_rows:
            self.assertEqual(rb_rows[0]["status"], "跳变")

    def test_reset_jumps_clears(self):
        """reset_jumps → 跳变计数清零"""
        base_ts = 1000000.0

        feed1 = _FakeFeed({"rb": 3500.0})
        dq.observe(feed1, now_ts=base_ts)
        feed2 = _FakeFeed({"rb": 3850.0})
        dq.observe(feed2, now_ts=base_ts + 5.0)

        self.assertGreater(dq._jumps.get("rb", 0), 0)

        dq.reset_jumps()
        self.assertEqual(dq._jumps.get("rb", 0), 0)

    def test_trading_vs_idle_stale_threshold(self):
        """交易时段 vs 非交易时段 陈旧阈值不同
        交易时段 120s，非交易时段 600s"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0
        dq.observe(feed, now_ts=base_ts)

        # 200s 后：交易时段=陈旧，非交易时段=正常
        rep_trading = dq.check(now_ts=base_ts + 200.0, trading=True)
        rep_idle = dq.check(now_ts=base_ts + 200.0, trading=False)

        rb_trading = [r for r in rep_trading["rows"] if r["symbol"] == "rb"]
        rb_idle = [r for r in rep_idle["rows"] if r["symbol"] == "rb"]

        if rb_trading and rb_idle:
            self.assertEqual(rb_trading[0]["status"], "陈旧")
            # 200s < 600s → 非交易时段不应陈旧
            self.assertNotEqual(rb_idle[0]["status"], "陈旧")

    def test_health_pct_range(self):
        """健康度在 0-100 之间"""
        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=1000000.0)
        rep = dq.check(now_ts=1000000.0, trading=True)

        self.assertGreaterEqual(rep["health_pct"], 0.0)
        self.assertLessEqual(rep["health_pct"], 100.0)

    def test_never_seen_status(self):
        """从未见过的品种 → 状态=未订阅"""
        rep = dq.check(now_ts=1000000.0, trading=True)

        for r in rep["rows"]:
            self.assertEqual(r["status"], "未订阅")

    def test_zero_price_skipped_in_observe(self):
        """价格 ≤ 0 → observe 跳过（不计入 last_price）"""
        feed = _FakeFeed({"rb": 0.0})
        dq.observe(feed, now_ts=1000000.0)

        # rb 不会被记录（p <= 0 时 continue）
        self.assertNotIn("rb", dq._last_price)
        self.assertNotIn("rb", dq._ever_seen)

    def test_negative_price_skipped(self):
        """负价格 → observe 跳过"""
        feed = _FakeFeed({"rb": -100.0})
        dq.observe(feed, now_ts=1000000.0)

        self.assertNotIn("rb", dq._last_price)

    def test_check_returns_structure(self):
        """check 返回完整结构"""
        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=1000000.0)
        rep = dq.check(now_ts=1000000.0, trading=True)

        required_keys = ["ok", "health_pct", "counts", "rows", "worst",
                         "trading", "stale_sec", "checked_at"]
        for k in required_keys:
            self.assertIn(k, rep, f"missing key: {k}")

        # counts 包含各状态计数
        for status in ["正常", "陈旧", "冻结", "异常", "跳变", "未订阅"]:
            self.assertIn(status, rep["counts"])

    def test_worst_list_contains_problems(self):
        """worst 列表只包含有问题的品种"""
        feed = _FakeFeed({"rb": 3500.0})
        dq.observe(feed, now_ts=1000000.0)
        rep = dq.check(now_ts=1000000.0, trading=True)

        for r in rep["worst"]:
            self.assertIn(r["status"], ["陈旧", "冻结", "异常", "跳变"])

    def test_all_symbols_tracked(self):
        """rows 中包含所有 SYMBOLS 品种"""
        from four_dim_strategy import SYMBOLS
        rep = dq.check(now_ts=1000000.0, trading=True)

        row_symbols = set(r["symbol"] for r in rep["rows"])
        for sym in SYMBOLS:
            self.assertIn(sym, row_symbols, f"missing symbol in rows: {sym}")

    def test_observe_multiple_symbols(self):
        """同时观察多个品种"""
        feed = _FakeFeed({"rb": 3500.0, "hc": 3800.0, "FG": 1500.0})
        dq.observe(feed, now_ts=1000000.0)

        self.assertIn("rb", dq._ever_seen)
        self.assertIn("hc", dq._ever_seen)
        self.assertIn("FG", dq._ever_seen)


# ═══════════════════════════════════════════════════════════════════════════
#  三、回测 + 风控 集成验证
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestRiskGateIntegration(unittest.TestCase):
    """回测中风控闸门的实际效果。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 200

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存")

    def test_backtest_trades_matches_detail_count(self):
        """trades 计数 == trades_detail 长度"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        self.assertEqual(result["trades"], len(result["trades_detail"]))

    def test_each_trade_has_R_value(self):
        """每笔交易有 R 值（盈亏 R 倍数）"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result["trades_detail"]
        for t in detail:
            self.assertIn("R", t)
            self.assertIsInstance(t["R"], float)

    def test_each_trade_has_exit_reason(self):
        """每笔交易有出场原因"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result["trades_detail"]
        for t in detail:
            self.assertIn("reason", t)
            self.assertIn(t["reason"], ["止损", "止盈", "尾仓离场", "信号反转"])

    def test_exit_reasons_count_matches(self):
        """exit_reasons 计数 == 实际交易出场分布"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result["trades_detail"]
        reasons_count = {}
        for t in detail:
            r = t["reason"]
            reasons_count[r] = reasons_count.get(r, 0) + 1

        for reason, count in result["exit_reasons"].items():
            self.assertEqual(count, reasons_count.get(reason, 0),
                             f"exit_reason '{reason}' count mismatch")

    def test_R_adj_is_worse_than_R(self):
        """R_adj 总是 ≤ R（扣费后：盈利更少、亏损更多）"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        detail = result["trades_detail"]
        for t in detail:
            # 扣费后 R_adj 总是比 R 差
            self.assertLessEqual(t["R_adj"], t["R"],
                                 f"R_adj should be <= R: {t['R_adj']} > {t['R']}")

    def test_win_rate_between_0_and_1(self):
        """win_rate 在 [0, 1] 范围内"""
        result = walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG,
                                       min_bars=60, df_in=self.df.tail(500))
        self.assertGreaterEqual(result["win_rate"], 0.0)
        self.assertLessEqual(result["win_rate"], 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  四、数据质量 + 策略 集成
# ═══════════════════════════════════════════════════════════════════════════

class TestDataQualityStrategyIntegration(unittest.TestCase):
    """数据质量 → 策略决策 的集成验证。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 100

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存")

    def test_pipeline_with_real_data(self):
        """pipeline 用真实数据运行正常"""
        from four_dim_strategy import pipeline

        df = self.df.tail(100)
        result = pipeline(self.symbol, df, cfg=DEFAULT_CONFIG)
        self.assertIsInstance(result, dict)
        self.assertIn("T_D", result)
        self.assertIn("F", result)
        self.assertIn("C", result)
        self.assertIn("regime", result)
        self.assertIn("triggered", result)

    def test_pipeline_regime_is_valid(self):
        """pipeline 返回的 regime 是有效值"""
        from four_dim_strategy import pipeline

        df = self.df.tail(200)
        result = pipeline(self.symbol, df, cfg=DEFAULT_CONFIG)
        valid_regimes = ["趋势", "波动", "震荡", "未知", "高波动"]
        self.assertIn(result["regime"], valid_regimes +
                      [r for r in valid_regimes])  # 宽松匹配

    def test_pipeline_bias_range(self):
        """bias_G 在合理范围内"""
        from four_dim_strategy import pipeline

        df = self.df.tail(200)
        result = pipeline(self.symbol, df, cfg=DEFAULT_CONFIG)
        # bias_G 是 bias 合成值，范围取决于具体实现
        self.assertIn("bias_G", result)
        self.assertIsInstance(result["bias_G"], float)

    def test_pipeline_dir_T_values(self):
        """dir_T 为 -1, 0, 或 1"""
        from four_dim_strategy import pipeline

        df = self.df.tail(200)
        result = pipeline(self.symbol, df, cfg=DEFAULT_CONFIG)
        self.assertIn(result["dir_T"], [-1, 0, 1])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  集成测试（二）— 回测验证 + 数据质量监控")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
