#!/usr/bin/env python3
"""
基准回归测试（Baseline Regression Tests）
==============================================

用固定输入验证关键输出的稳定性，防止代码改动导致意外退化。

一、策略管线基准回归
   - 固定 DataFrame 切片 → build_signal 输出完全一致
   - risk_gate 关键指标在容差内
   - exit_plan 结果确定性

二、回测基准回归
   - 固定数据窗口 → 回测指标（trades/expR/win_rate）稳定
   - 各品种回测结果在容差范围内
   - 退出原因分布稳定

三、数据质量基准回归
   - 固定 observe 序列 → check 结果完全一致
   - 健康度计算精确一致

四、Kelly 基准回归
   - 典型 edge 值 → kelly 因子精确一致
   - 边界值精确一致

五、数学函数基准回归
   - _norm_tanh 典型值精确一致
   - 月份运算典型值精确一致
   - 季节性典型值精确一致

用法：
  首次运行或代码重大调整后，可用 --update-baseline 更新基准值。
  （通过环境变量 UPDATE_BASELINE=1 触发）
"""

import json
import os
import sys
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
from fundamental_feed import seasonal_f
from kelly_utils import compute_kelly_factor
from macro_context import _norm_tanh
from refresh_main_contracts import _add_months

# 基准文件路径
BASELINE_FILE = os.path.join(HERE, "_baseline_values.json")

# 是否更新基准
UPDATE_BASELINE = os.environ.get("UPDATE_BASELINE", "0") == "1"

# 浮点容差（相对）
REL_TOL = 1e-6
ABS_TOL = 1e-8


def _assert_almost_equal(test_obj, actual, expected, name, places=6):
    """近似相等断言，支持更新模式。"""
    if UPDATE_BASELINE:
        return  # 更新模式下不校验
    test_obj.assertAlmostEqual(actual, expected, places=places, msg=f"{name}: got {actual}, expected {expected}")


def _load_baselines():
    """加载基准值。"""
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_baselines(data):
    """保存基准值。"""
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# 全局基准字典（运行时填充）
_baselines = _load_baselines()


def _baseline(key, value):
    """获取或设置基准值。"""
    if UPDATE_BASELINE:
        _baselines[key] = value
        return value
    if key not in _baselines:
        raise KeyError(f"基准值缺失: {key} (运行 UPDATE_BASELINE=1 初始化)")
    return _baselines[key]


# ═══════════════════════════════════════════════════════════════════════════
#  一、策略管线基准回归
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineBaseline(unittest.TestCase):
    """策略管线基准回归测试。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "rb"
        cls.df = load_daily(cls.symbol)
        cls.has_data = cls.df is not None and len(cls.df) > 300

    def setUp(self):
        if not self.has_data:
            self.skipTest("无日线数据缓存")

    def test_risk_gate_baseline(self):
        """risk_gate 典型输入 → 结果与基准一致"""
        price, atr = 3500.0, 80.0
        result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)

        key = f"risk_gate_{self.symbol}_{price}_{atr}"
        expected = _baseline(
            key,
            {
                "passed": result["passed"],
                "N_risk": result["N_risk"],
                "N_margin": result["N_margin"],
                "N_plan": result["N_plan"],
                "stop_pts": result["stop_pts"],
                "kelly_mult": result["kelly_mult"],
            },
        )

        self.assertEqual(result["passed"], expected["passed"])
        _assert_almost_equal(self, result["N_risk"], expected["N_risk"], f"{key}.N_risk")
        _assert_almost_equal(self, result["N_margin"], expected["N_margin"], f"{key}.N_margin")
        _assert_almost_equal(self, result["N_plan"], expected["N_plan"], f"{key}.N_plan")
        _assert_almost_equal(self, result["stop_pts"], expected["stop_pts"], f"{key}.stop_pts")
        _assert_almost_equal(self, result["kelly_mult"], expected["kelly_mult"], f"{key}.kelly_mult")

    def test_risk_gate_low_price_baseline(self):
        """risk_gate 低价品种 → 结果与基准一致"""
        price, atr = 300.0, 10.0
        result = risk_gate("FG", price, atr, cfg=DEFAULT_CONFIG)

        key = f"risk_gate_FG_{price}_{atr}"
        expected = _baseline(
            key,
            {
                "passed": result["passed"],
                "N_plan": result["N_plan"],
                "N_risk": result["N_risk"],
                "N_margin": result["N_margin"],
            },
        )

        self.assertEqual(result["passed"], expected["passed"])
        _assert_almost_equal(self, result["N_plan"], expected["N_plan"], f"{key}.N_plan")

    def test_risk_gate_with_held_baseline(self):
        """risk_gate 带持仓 → 结果与基准一致"""
        price, atr = 3500.0, 80.0
        result = risk_gate("rb", price, atr, cfg=DEFAULT_CONFIG, held_lots=3)

        key = f"risk_gate_rb_{price}_{atr}_held3"
        expected = _baseline(
            key,
            {
                "passed": result["passed"],
                "N_plan": result["N_plan"],
                "N_risk": result["N_risk"],
                "N_margin": result["N_margin"],
                "kelly_mult": result["kelly_mult"],
            },
        )

        self.assertEqual(result["passed"], expected["passed"])
        _assert_almost_equal(self, result["N_plan"], expected["N_plan"], f"{key}.N_plan")


# ═══════════════════════════════════════════════════════════════════════════
#  二、回测基准回归
# ═══════════════════════════════════════════════════════════════════════════


class TestBacktestBaseline(unittest.TestCase):
    """回测基准回归测试。"""

    @classmethod
    def setUpClass(cls):
        cls.symbols = ["rb", "hc", "FG"]
        cls.data = {}
        cls.has_data = True
        for sym in cls.symbols:
            df = load_daily(sym)
            if df is None or len(df) < 500:
                cls.has_data = False
                break
            cls.data[sym] = df.tail(500)

    def setUp(self):
        if not self.has_data:
            self.skipTest("数据不足")

    def test_backtest_rb_baseline(self):
        """rb 回测 → 关键指标与基准一致"""
        self._run_backtest_baseline("rb")

    def test_backtest_hc_baseline(self):
        """hc 回测 → 关键指标与基准一致"""
        self._run_backtest_baseline("hc")

    def test_backtest_FG_baseline(self):
        """FG 回测 → 关键指标与基准一致"""
        self._run_backtest_baseline("FG")

    def _run_backtest_baseline(self, symbol):
        result = walk_forward_backtest(symbol, cfg=DEFAULT_CONFIG, min_bars=60, df_in=self.data[symbol])

        key = f"backtest_{symbol}_tail500"
        baseline_data = {
            "symbol": result["symbol"],
            "trades": result["trades"],
            "expR": result["expR"],
            "win_rate": result["win_rate"],
            "roll_skipped": result.get("roll_skipped", 0),
            "exit_reasons": result.get("exit_reasons", {}),
            "by_regime_keys": sorted(result.get("by_regime", {}).keys()),
        }
        expected = _baseline(key, baseline_data)

        self.assertEqual(result["trades"], expected["trades"], f"{key}: trades 不匹配")
        _assert_almost_equal(self, result["expR"], expected["expR"], f"{key}.expR", places=4)
        _assert_almost_equal(self, result["win_rate"], expected["win_rate"], f"{key}.win_rate", places=4)

        # 退出原因分布（每笔数一致）
        if "exit_reasons" in expected:
            for reason, count in expected["exit_reasons"].items():
                self.assertEqual(result["exit_reasons"].get(reason, 0), count, f"{key}.exit_reasons['{reason}']")

    def test_backtest_cooldown_baseline(self):
        """冷却期=5 的回测结果稳定"""
        result = walk_forward_backtest("rb", cfg=DEFAULT_CONFIG, min_bars=60, cooldown_bars=5, df_in=self.data["rb"])
        key = "backtest_rb_cooldown5"
        baseline_data = {
            "trades": result["trades"],
            "expR": result["expR"],
            "win_rate": result["win_rate"],
        }
        expected = _baseline(key, baseline_data)

        self.assertEqual(result["trades"], expected["trades"])
        _assert_almost_equal(self, result["expR"], expected["expR"], f"{key}.expR", places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  三、数据质量基准回归
# ═══════════════════════════════════════════════════════════════════════════


class TestDataQualityBaseline(unittest.TestCase):
    """数据质量基准回归测试。"""

    def setUp(self):
        dq._last_seen.clear()
        dq._last_price.clear()
        dq._frozen.clear()
        dq._prev_price.clear()
        dq._ever_seen.clear()
        dq._jumps.clear()

    def _feed_sequence(self, prices_seq, base_ts=1000000.0):
        """按序列喂数据。"""
        for i, prices in enumerate(prices_seq):
            feed = _FakeFeed(prices)
            dq.observe(feed, now_ts=base_ts + i * 5.0)

    def test_normal_health_baseline(self):
        """正常数据 → 健康度 100%"""
        feed = _FakeFeed({"rb": 3500.0, "hc": 3800.0, "FG": 1500.0})
        dq.observe(feed, now_ts=1000000.0)
        rep = dq.check(now_ts=1000000.0, trading=True)

        key = "dq_normal_3symbols"
        tracked = [r for r in rep["rows"] if r["status"] != "未订阅"]
        baseline_data = {
            "health_pct": rep["health_pct"],
            "ok": rep["ok"],
            "tracked_count": len(tracked),
            "normal_count": sum(1 for r in tracked if r["status"] == "正常"),
        }
        expected = _baseline(key, baseline_data)

        self.assertEqual(rep["health_pct"], expected["health_pct"])
        self.assertEqual(rep["ok"], expected["ok"])

    def test_frozen_status_baseline(self):
        """冻结状态 → 结果与基准一致"""
        prices_seq = [{"rb": 3500.0}] * 7  # 7 次不变 = 6 次连续冻结
        self._feed_sequence(prices_seq)

        rep = dq.check(now_ts=1000000.0 + 30.0, trading=True)
        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]

        key = "dq_frozen_rb_7obs"
        baseline_data = {
            "status": rb["status"],
            "frozen_n": rb["frozen_n"],
            "health_pct": rep["health_pct"],
        }
        expected = _baseline(key, baseline_data)

        self.assertEqual(rb["status"], expected["status"])
        self.assertEqual(rb["frozen_n"], expected["frozen_n"])

    def test_mixed_health_baseline(self):
        """混合状态（1正常+1陈旧+1冻结）→ 健康度与基准一致"""
        base_ts = 1000000.0

        # 三个品种全部初始正常
        feed_all = _FakeFeed({"rb": 3500.0, "hc": 3800.0, "FG": 1500.0})
        dq.observe(feed_all, now_ts=base_ts)

        # 让 hc 陈旧（只更新 rb 和 FG）
        for i in range(1, 5):
            feed = _FakeFeed({"rb": 3500.0 + i, "FG": 1500.0 + i})
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        # 让 FG 冻结（连续 7 次不变）
        for i in range(5, 12):
            feed = _FakeFeed({"rb": 3505.0 + i, "FG": 1504.0})  # FG 价格不变
            dq.observe(feed, now_ts=base_ts + i * 5.0)

        check_ts = base_ts + 200.0  # hc 已经陈旧（200s > 120s）
        rep = dq.check(now_ts=check_ts, trading=True)

        rb = [r for r in rep["rows"] if r["symbol"] == "rb"][0]
        hc = [r for r in rep["rows"] if r["symbol"] == "hc"][0]
        fg = [r for r in rep["rows"] if r["symbol"] == "FG"][0]

        key = "dq_mixed_3symbols"
        baseline_data = {
            "rb_status": rb["status"],
            "hc_status": hc["status"],
            "fg_status": fg["status"],
            "health_pct": rep["health_pct"],
            "ok": rep["ok"],
        }
        expected = _baseline(key, baseline_data)

        self.assertEqual(rb["status"], expected["rb_status"])
        self.assertEqual(hc["status"], expected["hc_status"])
        self.assertEqual(fg["status"], expected["fg_status"])
        _assert_almost_equal(self, rep["health_pct"], expected["health_pct"], f"{key}.health_pct", places=1)


# ═══════════════════════════════════════════════════════════════════════════
#  四、Kelly 基准回归
# ═══════════════════════════════════════════════════════════════════════════


class TestKellyBaseline(unittest.TestCase):
    """Kelly 因子基准回归测试。"""

    def test_kelly_typical_values(self):
        """典型 edge 值 → 精确结果"""
        cases = [
            (0.0, 0.5, 1.5, 0.5, None, "zero_edge"),
            (0.5, 0.5, 1.5, 0.5, None, "half_target"),
            (1.0, 0.5, 1.5, 0.5, None, "full_target"),
            (2.0, 0.5, 1.5, 0.5, None, "double_target"),
            (0.5, 0.5, 1.5, 0.5, 0.3, "with_near_pos"),
            (0.5, 0.5, 1.5, 0.5, -0.2, "with_near_neg"),
        ]

        for edge, k_min, k_max, target, near, name in cases:
            result = compute_kelly_factor(edge, k_min, k_max, target, near)
            key = f"kelly_{name}"
            expected = _baseline(key, result)
            _assert_almost_equal(self, result, expected, key, places=10)

    def test_kelly_edge_none(self):
        """edge=None → 1.0"""
        result = compute_kelly_factor(None)
        key = "kelly_none_edge"
        expected = _baseline(key, result)
        self.assertEqual(result, expected)

    def test_kelly_boundary_values(self):
        """边界值精确一致"""
        cases = [
            (-1.0, "negative_edge"),
            (100.0, "huge_edge"),
        ]
        for edge, name in cases:
            result = compute_kelly_factor(edge)
            key = f"kelly_boundary_{name}"
            expected = _baseline(key, result)
            _assert_almost_equal(self, result, expected, key, places=10)


# ═══════════════════════════════════════════════════════════════════════════
#  五、数学函数基准回归
# ═══════════════════════════════════════════════════════════════════════════


class TestMathBaseline(unittest.TestCase):
    """数学函数基准回归测试。"""

    def test_norm_tanh_typical(self):
        """_norm_tanh 典型值精确一致"""
        cases = [
            (0.0, 1.0, "zero"),
            (1.0, 1.0, "x1_s1"),
            (-1.0, 1.0, "neg_x1_s1"),
            (2.0, 0.5, "x2_s0.5"),
            (10.0, 5.0, "x10_s5"),
            (100.0, 1.0, "saturation_pos"),
            (-100.0, 1.0, "saturation_neg"),
        ]
        for x, scale, name in cases:
            result = _norm_tanh(x, scale)
            key = f"tanh_{name}"
            expected = _baseline(key, result)
            _assert_almost_equal(self, result, expected, key, places=12)

    def test_add_months_typical(self):
        """月份运算典型值精确一致"""
        cases = [
            (202501, 1, 202502, "jan_to_feb"),
            (202512, 1, 202601, "dec_to_jan"),
            (202501, -1, 202412, "jan_to_prev_dec"),
            (202506, 12, 202606, "plus_12_months"),
            (202506, -12, 202406, "minus_12_months"),
            (202501, 18, 202607, "plus_18_months"),
        ]
        for ym, n, expected_ym, name in cases:
            result = _add_months(ym, n)
            key = f"month_{name}"
            expected = _baseline(key, result)
            self.assertEqual(result, expected, f"{key}: {result} != {expected}")

    def test_seasonal_typical(self):
        """季节性典型值精确一致"""
        cases = [
            ("jd", "2025-01-15", "jd_jan"),
            ("jd", "2025-07-15", "jd_jul"),
            ("lh", "2025-03-15", "lh_mar"),
            ("lh", "2025-09-15", "lh_sep"),
            ("rb", "2025-05-15", "rb_may"),
            ("rb", "2025-11-15", "rb_nov"),
        ]
        for sym, date, name in cases:
            result = seasonal_f(sym, date)
            key = f"seasonal_{name}"
            expected = _baseline(key, result)
            self.assertEqual(result, expected, f"{key}: {result} != {expected}")

    def test_parse_typical(self):
        """解析函数典型值精确一致"""
        cases = [
            ("BUY", "side", "买"),
            ("sell", "side", "卖"),
            ("开仓", "offset", "开"),
            ("平仓", "offset", "平"),
            ("1,234.56", "to_num", 1234.56),
            ("  99.9  ", "to_num", 99.9),
            ("Signal_123", "norm_key", "signal_123"),
        ]
        for value, func, expected in cases:
            if func == "side":
                result = _parse_side(value)
            elif func == "offset":
                result = _parse_offset(value)
            elif func == "to_num":
                result = _to_num(value)
            elif func == "norm_key":
                result = _norm_key(value)
            else:
                continue

            key = f"parse_{func}_{value.replace(' ', '_').replace(',', '')}"
            expected_val = _baseline(key, result)
            if isinstance(expected_val, float):
                _assert_almost_equal(self, result, expected_val, key, places=10)
            else:
                self.assertEqual(result, expected_val, f"{key}: {result} != {expected_val}")


class _FakeFeed:
    def __init__(self, prices=None):
        self._prices = prices or {}

    def price(self, sym):
        return self._prices.get(sym)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if UPDATE_BASELINE:
        print("=" * 60)
        print("  ⚠️  更新基准模式 — 运行测试并保存基准值")
        print("=" * 60)
    else:
        print("=" * 60)
        print("  基准回归测试")
        print("  （运行 UPDATE_BASELINE=1 可更新基准）")
        print("=" * 60)
    print()

    # 先运行测试
    result = unittest.main(verbosity=2, exit=False)

    # 更新模式下保存基准值
    if UPDATE_BASELINE and _baselines:
        _save_baselines(_baselines)
        print(f"\n✅ 基准值已保存到 {BASELINE_FILE}")
        print(f"   共 {len(_baselines)} 个基准项")

    sys.exit(0 if result.result.wasSuccessful() else 1)
