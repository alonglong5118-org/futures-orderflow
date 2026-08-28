#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能测试（Performance Benchmarks）
==============================================

测量关键函数的执行时间，设置性能基线，防止性能退化。

一、策略核心函数性能
   - risk_gate：单次调用耗时
   - compute_kelly_factor：单次调用耗时
   - _norm_tanh：单次调用耗时

二、回测性能
   - walk_forward_backtest 500 根 K 线耗时
   - 每笔交易平均耗时

三、数据质量性能
   - dq.observe 单品种耗时
   - dq.observe 全品种耗时
   - dq.check 全品种耗时

四、解析函数性能
   - _parse_side / _parse_offset / _to_num / _norm_key

五、性能回归检查
   - 与基线对比，退化超过 50% 告警
   - （通过环境变量 PERF_CHECK=1 启用严格检查）

用法：
  python tests/test_performance.py           # 运行基准，输出耗时
  PERF_CHECK=1 python tests/test_performance.py  # 严格模式，退化超阈值失败
"""

import os
import statistics
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import data_quality as dq
from broker_import import _norm_key, _parse_offset, _parse_side, _to_num
from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    risk_gate,
    walk_forward_backtest,
)
from kelly_utils import compute_kelly_factor
from macro_context import _norm_tanh

# 是否启用严格性能检查
PERF_CHECK = os.environ.get("PERF_CHECK", "0") == "1"

# 性能退化阈值（相对，0.5 = 慢 50% 则告警）
PERF_REGRESSION_THRESHOLD = 0.5

# 基线文件
PERF_BASELINE_FILE = os.path.join(HERE, "_perf_baseline.json")


def _timeit(func, *args, iterations=1000, warmup=10, **kwargs):
    """测量函数执行时间，返回 (avg_ms, min_ms, max_ms)。"""
    # warmup
    for _ in range(warmup):
        func(*args, **kwargs)

    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        func(*args, **kwargs)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)  # ms

    return (
        statistics.mean(times),
        min(times),
        max(times),
        statistics.median(times),
    )


def _load_perf_baselines():
    import json

    if os.path.exists(PERF_BASELINE_FILE):
        with open(PERF_BASELINE_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_perf_baselines(data):
    import json

    with open(PERF_BASELINE_FILE, "w") as f:
        json.dump(data, f, indent=2)


_perf_baselines = _load_perf_baselines()
_perf_results = {}


def _check_perf(name, avg_ms, threshold_ms=None):
    """检查性能是否在可接受范围内。"""
    _perf_results[name] = avg_ms

    if not PERF_CHECK:
        return  # 非严格模式只记录不检查

    # 检查绝对阈值
    if threshold_ms is not None and avg_ms > threshold_ms:
        raise AssertionError(f"性能超出绝对阈值: {name} = {avg_ms:.3f}ms > {threshold_ms:.3f}ms")

    # 检查相对基线
    if name in _perf_baselines:
        baseline = _perf_baselines[name]
        if avg_ms > baseline * (1 + PERF_REGRESSION_THRESHOLD):
            raise AssertionError(
                f"性能退化: {name} = {avg_ms:.3f}ms, "
                f"基线 = {baseline:.3f}ms, "
                f"退化 {(avg_ms / baseline - 1) * 100:.1f}% "
                f"(阈值 {PERF_REGRESSION_THRESHOLD * 100:.0f}%)"
            )


# ═══════════════════════════════════════════════════════════════════════════
#  一、策略核心函数性能
# ═══════════════════════════════════════════════════════════════════════════


class TestCoreFunctionPerformance(unittest.TestCase):
    """策略核心函数性能基准。"""

    def test_risk_gate_perf(self):
        """risk_gate 单次调用 < 0.5ms"""

        def run():
            risk_gate("rb", 3500.0, 80.0, cfg=DEFAULT_CONFIG)

        avg, mn, mx, med = _timeit(run, iterations=500)
        _check_perf("risk_gate", avg, threshold_ms=0.5)
        print(f"  risk_gate: avg={avg:.3f}ms, min={mn:.3f}ms, max={mx:.3f}ms, median={med:.3f}ms")

    def test_kelly_factor_perf(self):
        """compute_kelly_factor 单次调用 < 0.01ms"""

        def run():
            compute_kelly_factor(0.5, 0.5, 1.5, 0.5, 0.3)

        avg, mn, mx, med = _timeit(run, iterations=10000)
        _check_perf("compute_kelly_factor", avg, threshold_ms=0.01)
        print(f"  compute_kelly_factor: avg={avg:.4f}ms, min={mn:.4f}ms, max={mx:.4f}ms")

    def test_norm_tanh_perf(self):
        """_norm_tanh 单次调用 < 0.005ms"""

        def run():
            _norm_tanh(1.5, 2.0)

        avg, mn, mx, med = _timeit(run, iterations=20000)
        _check_perf("norm_tanh", avg, threshold_ms=0.005)
        print(f"  _norm_tanh: avg={avg:.5f}ms, min={mn:.5f}ms, max={mx:.5f}ms")


# ═══════════════════════════════════════════════════════════════════════════
#  二、回测性能
# ═══════════════════════════════════════════════════════════════════════════


class TestBacktestPerformance(unittest.TestCase):
    """回测性能基准。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 500

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存")

    def test_backtest_300bars_perf(self):
        """walk_forward_backtest 300 根 K 线 < 500ms"""
        df_slice = self.df.tail(300)

        def run():
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_slice)

        avg, mn, mx, med = _timeit(run, iterations=10, warmup=2)
        _check_perf("backtest_300bars", avg, threshold_ms=500)
        print(f"  backtest 300 bars: avg={avg:.1f}ms, min={mn:.1f}ms, max={mx:.1f}ms")

    def test_backtest_500bars_perf(self):
        """walk_forward_backtest 500 根 K 线 < 1500ms"""
        df_slice = self.df.tail(500)

        def run():
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_slice)

        avg, mn, mx, med = _timeit(run, iterations=5, warmup=1)
        _check_perf("backtest_500bars", avg, threshold_ms=1500)
        print(f"  backtest 500 bars: avg={avg:.1f}ms, min={mn:.1f}ms, max={mx:.1f}ms")

    def test_backtest_per_bar_avg(self):
        """每根 K 线平均处理时间 < 2ms（衡量核心计算成本）"""
        df_slice = self.df.tail(500)
        n_bars = len(df_slice)

        def run():
            walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_slice)

        avg_total, _, _, _ = _timeit(run, iterations=5, warmup=1)
        per_bar = avg_total / n_bars

        _check_perf("backtest_per_bar", per_bar, threshold_ms=2.0)
        print(f"  backtest per bar: {per_bar:.3f}ms ({n_bars} bars in {avg_total:.1f}ms)")

    def test_backtest_scaling(self):
        """数据量翻倍，耗时增长 < 3.5 倍（近似线性+初始化开销）"""
        df_200 = self.df.tail(200)
        df_400 = self.df.tail(400)

        t_200, _, _, _ = _timeit(
            lambda: walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_200),
            iterations=10,
            warmup=2,
        )
        t_400, _, _, _ = _timeit(
            lambda: walk_forward_backtest(self.symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=df_400),
            iterations=5,
            warmup=1,
        )

        ratio = t_400 / t_200 if t_200 > 0 else 0
        print(f"  scaling 200→400 bars: {ratio:.2f}x (200={t_200:.1f}ms, 400={t_400:.1f}ms)")

        # 近似线性，允许到 3.5 倍（初始化开销在小数据量占比高）
        self.assertLess(ratio, 3.5, f"回测扩展比过高: {ratio:.2f}x")


# ═══════════════════════════════════════════════════════════════════════════
#  三、数据质量性能
# ═══════════════════════════════════════════════════════════════════════════


class _FakeFeed:
    def __init__(self, prices=None):
        self._prices = prices or {}

    def price(self, sym):
        return self._prices.get(sym)


class TestDataQualityPerformance(unittest.TestCase):
    """数据质量性能基准。"""

    def setUp(self):
        dq._last_seen.clear()
        dq._last_price.clear()
        dq._frozen.clear()
        dq._prev_price.clear()
        dq._ever_seen.clear()
        dq._jumps.clear()

    def test_observe_single_symbol_perf(self):
        """dq.observe 单品种 < 0.01ms"""
        feed = _FakeFeed({"rb": 3500.0})
        base_ts = 1000000.0

        def run():
            dq.observe(feed, now_ts=base_ts)

        avg, mn, mx, med = _timeit(run, iterations=5000)
        _check_perf("dq_observe_single", avg, threshold_ms=0.01)
        print(f"  dq.observe (1 symbol): avg={avg:.4f}ms, min={mn:.4f}ms, max={mx:.4f}ms")

    def test_observe_multi_symbol_perf(self):
        """dq.observe 多品种(5个) < 0.05ms"""
        feed = _FakeFeed(
            {
                "rb": 3500.0,
                "hc": 3800.0,
                "FG": 1500.0,
                "SA": 1800.0,
                "MA": 2500.0,
            }
        )
        base_ts = 1000000.0

        def run():
            dq.observe(feed, now_ts=base_ts)

        avg, mn, mx, med = _timeit(run, iterations=2000)
        _check_perf("dq_observe_multi5", avg, threshold_ms=0.05)
        print(f"  dq.observe (5 symbols): avg={avg:.4f}ms, min={mn:.4f}ms, max={mx:.4f}ms")

    def test_check_perf(self):
        """dq.check 全品种 < 0.1ms"""
        feed = _FakeFeed({"rb": 3500.0, "hc": 3800.0, "FG": 1500.0})
        dq.observe(feed, now_ts=1000000.0)

        def run():
            dq.check(now_ts=1000000.0, trading=True)

        avg, mn, mx, med = _timeit(run, iterations=2000)
        _check_perf("dq_check", avg, threshold_ms=0.1)
        print(f"  dq.check: avg={avg:.4f}ms, min={mn:.4f}ms, max={mx:.4f}ms")

    def test_observe_1000_ticks_perf(self):
        """1000 次 tick observe 总耗时 < 50ms"""
        base_ts = 1000000.0

        def run_1000():
            for i in range(1000):
                feed = _FakeFeed({"rb": 3500.0 + i * 0.01})
                dq.observe(feed, now_ts=base_ts + i * 0.5)

        # 单次测试（1000次已经够多了）
        t0 = time.perf_counter()
        run_1000()
        t1 = time.perf_counter()
        total_ms = (t1 - t0) * 1000

        _check_perf("dq_observe_1000ticks", total_ms, threshold_ms=50)
        print(f"  dq.observe 1000 ticks: total={total_ms:.1f}ms, per_tick={total_ms / 1000:.4f}ms")


# ═══════════════════════════════════════════════════════════════════════════
#  四、解析函数性能
# ═══════════════════════════════════════════════════════════════════════════


class TestParsePerformance(unittest.TestCase):
    """解析函数性能基准。"""

    def test_parse_side_perf(self):
        """_parse_side < 0.005ms"""

        def run():
            _parse_side("BUY")

        avg, mn, mx, med = _timeit(run, iterations=20000)
        _check_perf("parse_side", avg, threshold_ms=0.005)
        print(f"  _parse_side: avg={avg:.5f}ms, min={mn:.5f}ms, max={mx:.5f}ms")

    def test_parse_offset_perf(self):
        """_parse_offset < 0.005ms"""

        def run():
            _parse_offset("平仓")

        avg, mn, mx, med = _timeit(run, iterations=20000)
        _check_perf("parse_offset", avg, threshold_ms=0.005)
        print(f"  _parse_offset: avg={avg:.5f}ms, min={mn:.5f}ms, max={mx:.5f}ms")

    def test_to_num_perf(self):
        """_to_num < 0.01ms"""

        def run():
            _to_num("1,234.56")

        avg, mn, mx, med = _timeit(run, iterations=10000)
        _check_perf("to_num", avg, threshold_ms=0.01)
        print(f"  _to_num: avg={avg:.5f}ms, min={mn:.5f}ms, max={mx:.5f}ms")

    def test_norm_key_perf(self):
        """_norm_key < 0.01ms"""

        def run():
            _norm_key("Signal_Order_123 : 螺纹钢")

        avg, mn, mx, med = _timeit(run, iterations=10000)
        _check_perf("norm_key", avg, threshold_ms=0.01)
        print(f"  _norm_key: avg={avg:.5f}ms, min={mn:.5f}ms, max={mx:.5f}ms")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════


def run_all_perf_tests():
    """运行所有性能测试并输出汇总。"""
    print("=" * 60)
    print("  性能基准测试")
    if PERF_CHECK:
        print(f"  ⚠️  严格模式：退化超 {PERF_REGRESSION_THRESHOLD * 100:.0f}% 则失败")
    print("=" * 60)
    print()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    for cls in [
        TestCoreFunctionPerformance,
        TestBacktestPerformance,
        TestDataQualityPerformance,
        TestParsePerformance,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 输出汇总
    print()
    print("=" * 60)
    print("  性能汇总")
    print("=" * 60)
    for name, avg_ms in sorted(_perf_results.items()):
        baseline = _perf_baselines.get(name)
        if baseline:
            delta = (avg_ms / baseline - 1) * 100
            marker = " ⚠️" if delta > PERF_REGRESSION_THRESHOLD * 100 else ""
            print(f"  {name:30s} {avg_ms:8.3f}ms  (基线 {baseline:8.3f}ms, {delta:+.1f}%){marker}")
        else:
            print(f"  {name:30s} {avg_ms:8.3f}ms  (无基线)")

    print()
    print(f"  共 {len(_perf_results)} 项性能基准")
    print()

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all_perf_tests()
    sys.exit(0 if success else 1)
