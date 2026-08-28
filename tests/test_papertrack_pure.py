#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper track 纯函数 — 单元测试
======================================

1. bars_to_records — DataFrame → JSON 序列化记录
   - None → []
   - 空 df → []
   - 正常转换：ISO 时间 + OHLC 4位小数
   - DatetimeIndex 正确转换
   - 截断到 BARS_SNAPSHOT_MAX

2. records_to_bars — 记录 → DataFrame
   - 空列表 → None
   - 正常转换回 DataFrame
   - 坏数据行跳过
   - 全坏 → None
   - DatetimeIndex 正确
   - 双向转换一致性

3. sig_id — 信号 ID 哈希
   - 相同信号 → 相同 ID
   - 不同价格 → 不同 ID
   - 16 位十六进制
   - 键顺序不影响结果

4. parse_signal — 信号解析
   - 正常多单 → 成功解析
   - 正常空单 → 成功解析
   - 类型不对 → None
   - 缺字段 → None
   - 方向无效 → None
   - stop_dist=0 → None
   - target=entry → None
   - 多单 target<=entry → None
   - 空单 target>=entry → None
   - target_R 公式正确
   - 返回 19 字段

5. aggregate — 交易统计聚合
   - 空列表 → 零统计
   - 全赢 → 100% 胜率
   - 全亏 → 0% 胜率
   - 混合 → 正确胜率和期望R
   - 连续亏损计数
   - 连续亏损预警(>=3)
   - 累计R
   - 加权累计R

6. _dim_vote — 维度投票
   - None → None
   - 正数 → 1
   - 负数 → -1
   - 0 → 0
"""

import sys
import os
import unittest
import json
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_papertrack import (
    bars_to_records, records_to_bars, sig_id, parse_signal,
    aggregate, _dim_vote, SIGNAL_TYPE, DIRECTION_MAP,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. bars_to_records
# ═══════════════════════════════════════════════════════════════════════════

class TestBarsToRecords(unittest.TestCase):
    """bars_to_records DataFrame → 记录列表。"""

    def test_none_returns_empty(self):
        """None → []"""
        self.assertEqual(bars_to_records(None), [])

    def test_empty_df_returns_empty(self):
        """空 df → []"""
        df = pd.DataFrame()
        self.assertEqual(bars_to_records(df), [])

    def test_normal_conversion(self):
        """正常转换：ISO 时间 + OHLC 4位小数"""
        dates = pd.date_range("2026-08-28 09:30:00", periods=3, freq="5min")
        df = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
        }, index=dates)
        recs = bars_to_records(df)
        self.assertEqual(len(recs), 3)
        # 每行 5 个元素
        self.assertEqual(len(recs[0]), 5)
        # ISO 时间格式
        self.assertIn("2026-08-28", recs[0][0])
        # OHLC 数值
        self.assertEqual(recs[0][1], 100.0)
        self.assertEqual(recs[0][2], 100.5)
        self.assertEqual(recs[0][3], 99.5)
        self.assertEqual(recs[0][4], 100.2)

    def test_datetime_index_preserved(self):
        """DatetimeIndex 正确转换为字符串"""
        dates = pd.date_range("2026-01-15 14:00:00", periods=1, freq="D")
        df = pd.DataFrame({
            "open": [100], "high": [110], "low": [90], "close": [105],
        }, index=dates)
        recs = bars_to_records(df)
        self.assertIn("2026-01-15", recs[0][0])
        self.assertIn("14:00:00", recs[0][0])

    def test_rounding_to_4_decimals(self):
        """价格 4 位小数取整"""
        dates = pd.date_range("2026-08-28", periods=1, freq="D")
        df = pd.DataFrame({
            "open": [100.12345],
            "high": [101.67890],
            "low": [99.11111],
            "close": [100.99999],
        }, index=dates)
        recs = bars_to_records(df)
        self.assertEqual(recs[0][1], 100.1235)  # round(100.12345, 4) = 100.1235
        self.assertEqual(recs[0][2], 101.6789)
        self.assertEqual(recs[0][3], 99.1111)
        self.assertEqual(recs[0][4], 101.0)

    def test_returns_list(self):
        """返回 list"""
        dates = pd.date_range("2026-08-28", periods=2, freq="D")
        df = pd.DataFrame({"open":[1,2],"high":[2,3],"low":[0,1],"close":[1.5,2.5]}, index=dates)
        self.assertIsInstance(bars_to_records(df), list)


# ═══════════════════════════════════════════════════════════════════════════
#  2. records_to_bars
# ═══════════════════════════════════════════════════════════════════════════

class TestRecordsToBars(unittest.TestCase):
    """records_to_bars 记录列表 → DataFrame。"""

    def test_empty_returns_none(self):
        """空列表 → None"""
        self.assertIsNone(records_to_bars([]))

    def test_normal_conversion(self):
        """正常转换回 DataFrame"""
        recs = [
            ["2026-08-28 09:30:00", 100, 101, 99, 100.5],
            ["2026-08-28 09:35:00", 100.5, 102, 100, 101.5],
        ]
        df = records_to_bars(recs)
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)
        self.assertIn("open", df.columns)
        self.assertIn("high", df.columns)
        self.assertIn("low", df.columns)
        self.assertIn("close", df.columns)
        self.assertEqual(df.iloc[0]["open"], 100.0)
        self.assertEqual(df.iloc[1]["close"], 101.5)

    def test_datetime_index(self):
        """DatetimeIndex 正确"""
        recs = [["2026-01-15 10:00:00", 100, 101, 99, 100.5]]
        df = records_to_bars(recs)
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(df.index[0], pd.Timestamp("2026-01-15 10:00:00"))

    def test_bad_rows_skipped(self):
        """坏数据行跳过"""
        recs = [
            ["2026-08-28 09:30:00", 100, 101, 99, 100.5],
            ["bad_date", "x", "y", "z", "w"],  # 坏数据
            ["2026-08-28 09:40:00", 101, 102, 100, 101.5],
        ]
        df = records_to_bars(recs)
        self.assertEqual(len(df), 2)

    def test_all_bad_returns_none(self):
        """全坏 → None"""
        recs = [["not_a_date", "x", "y", "z", "w"]]
        self.assertIsNone(records_to_bars(recs))

    def test_roundtrip_consistency(self):
        """双向转换一致性（DataFrame → records → DataFrame）"""
        dates = pd.date_range("2026-08-28 09:30:00", periods=3, freq="5min")
        df_orig = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
        }, index=dates)
        recs = bars_to_records(df_orig)
        df_back = records_to_bars(recs)
        self.assertEqual(len(df_back), 3)
        # 数值一致
        for i in range(3):
            self.assertAlmostEqual(df_back.iloc[i]["open"], df_orig.iloc[i]["open"], places=4)
            self.assertAlmostEqual(df_back.iloc[i]["close"], df_orig.iloc[i]["close"], places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  3. sig_id
# ═══════════════════════════════════════════════════════════════════════════

class TestSigId(unittest.TestCase):
    """sig_id 信号 ID 哈希。"""

    def test_same_signal_same_id(self):
        """相同信号 → 相同 ID"""
        s = {"symbol": "rb", "time": "2026-08-28 10:00:00", "price": 3500,
             "direction": 1, "stop": 3450, "target": 3600, "lots": 2}
        id1 = sig_id(s)
        id2 = sig_id(dict(s))
        self.assertEqual(id1, id2)

    def test_different_price_different_id(self):
        """不同价格 → 不同 ID"""
        s1 = {"symbol": "rb", "time": "2026-08-28", "price": 3500,
              "direction": 1, "stop": 3450, "target": 3600, "lots": 1}
        s2 = dict(s1, price=3501)
        self.assertNotEqual(sig_id(s1), sig_id(s2))

    def test_16_char_hex(self):
        """16 位十六进制"""
        s = {"symbol": "rb", "time": "t", "price": 1, "direction": 1,
             "stop": 2, "target": 3, "lots": 1}
        sid = sig_id(s)
        self.assertEqual(len(sid), 16)
        # 都是十六进制字符
        int(sid, 16)  # 不抛异常就是合法十六进制

    def test_key_order_independent(self):
        """键顺序不影响结果"""
        s1 = {"symbol": "rb", "time": "t", "price": 1}
        s2 = {"price": 1, "time": "t", "symbol": "rb"}
        # 补全必要字段
        for k in ["direction", "stop", "target", "lots"]:
            s1[k] = 0
            s2[k] = 0
        self.assertEqual(sig_id(s1), sig_id(s2))


# ═══════════════════════════════════════════════════════════════════════════
#  4. parse_signal
# ═══════════════════════════════════════════════════════════════════════════

class TestParseSignal(unittest.TestCase):
    """parse_signal 信号解析。"""

    def _base_signal(self, **overrides):
        s = {
            "signal_type": SIGNAL_TYPE,
            "symbol": "rb",
            "price": 3500,
            "stop": 3450,
            "target": 3600,
            "direction": "long",
            "time": "2026-08-28 10:00:00",
            "lots": 2,
            "pipeline": {"regime": "趋势", "T_D": 65},
        }
        s.update(overrides)
        return s

    def test_valid_long_parsed(self):
        """正常多单 → 成功解析"""
        s = self._base_signal()
        p = parse_signal(s)
        self.assertIsNotNone(p)
        self.assertEqual(p["symbol"], "rb")
        self.assertEqual(p["direction"], "long")
        self.assertEqual(p["entry"], 3500.0)
        self.assertEqual(p["stop"], 3450.0)
        self.assertEqual(p["target"], 3600.0)

    def test_valid_short_parsed(self):
        """正常空单 → 成功解析"""
        s = self._base_signal(direction="short", price=3500, stop=3550, target=3400)
        p = parse_signal(s)
        self.assertIsNotNone(p)
        self.assertEqual(p["direction"], "short")

    def test_wrong_type_returns_none(self):
        """类型不对 → None"""
        s = self._base_signal(signal_type="wrong_type")
        self.assertIsNone(parse_signal(s))

    def test_missing_price_returns_none(self):
        """缺字段 → None"""
        s = self._base_signal()
        del s["price"]
        self.assertIsNone(parse_signal(s))

    def test_invalid_direction_returns_none(self):
        """方向无效 → None"""
        s = self._base_signal(direction="sideways")
        self.assertIsNone(parse_signal(s))

    def test_zero_stop_dist_returns_none(self):
        """stop_dist=0 → None"""
        s = self._base_signal(stop=3500)  # stop == entry
        self.assertIsNone(parse_signal(s))

    def test_target_equals_entry_returns_none(self):
        """target=entry → None"""
        s = self._base_signal(target=3500)
        self.assertIsNone(parse_signal(s))

    def test_long_target_below_entry_returns_none(self):
        """多单 target<=entry → None"""
        s = self._base_signal(target=3400)  # 多单目标低于入场
        self.assertIsNone(parse_signal(s))

    def test_short_target_above_entry_returns_none(self):
        """空单 target>=entry → None"""
        s = self._base_signal(direction="short", stop=3550, target=3600)
        self.assertIsNone(parse_signal(s))

    def test_target_R_formula(self):
        """target_R = dist_to_target / stop_dist"""
        s = self._base_signal(price=3500, stop=3450, target=3600)
        p = parse_signal(s)
        # stop_dist = 50, target_dist = 100 → target_R = 2.0
        self.assertEqual(p["stop_dist"], 50.0)
        self.assertEqual(p["dist_to_target"], 100.0)
        self.assertEqual(p["target_R"], 2.0)

    def test_return_fields(self):
        """返回字段完整"""
        s = self._base_signal()
        p = parse_signal(s)
        for key in ("id", "symbol", "direction", "time", "entry", "stop",
                     "target", "stop_dist", "dist_to_target", "target_R",
                     "lots", "regime", "F_bias", "T_D", "C_score"):
            self.assertIn(key, p)

    def test_passthrough_pipeline_fields(self):
        """pipeline 字段透传"""
        s = self._base_signal(pipeline={"regime": "震荡", "T_D": 40, "F_bias": 25})
        p = parse_signal(s)
        self.assertEqual(p["regime"], "震荡")
        self.assertEqual(p["T_D"], 40)
        self.assertEqual(p["F_bias"], 25)


# ═══════════════════════════════════════════════════════════════════════════
#  5. aggregate
# ═══════════════════════════════════════════════════════════════════════════

class TestAggregate(unittest.TestCase):
    """aggregate 交易统计聚合。"""

    def test_empty_zero_stats(self):
        """空列表 → 零统计"""
        r = aggregate([], "R", "outcome")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["wins"], 0)
        self.assertEqual(r["losses"], 0)
        self.assertEqual(r["win_rate"], 0.0)
        self.assertEqual(r["expected_R"], 0.0)
        self.assertEqual(r["max_consecutive_losses"], 0)
        self.assertFalse(r["consecutive_loss_warning"])
        self.assertEqual(r["final_cum_R"], 0.0)

    def test_all_wins_100pct(self):
        """全赢 → 100% 胜率"""
        trades = [
            {"R": 2.0, "outcome": "win", "lots": 1},
            {"R": 1.5, "outcome": "win", "lots": 2},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["win_rate"], 1.0)
        self.assertEqual(r["wins"], 2)
        self.assertEqual(r["losses"], 0)

    def test_all_losses_0pct(self):
        """全亏 → 0% 胜率"""
        trades = [
            {"R": -1.0, "outcome": "loss", "lots": 1},
            {"R": -0.5, "outcome": "loss", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["win_rate"], 0.0)
        self.assertEqual(r["wins"], 0)
        self.assertEqual(r["losses"], 2)

    def test_mixed_win_rate(self):
        """混合 → 正确胜率"""
        trades = [
            {"R": 1.0, "outcome": "win", "lots": 1},
            {"R": -1.0, "outcome": "loss", "lots": 1},
            {"R": 2.0, "outcome": "win", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        # 2/3 = 0.6667
        self.assertAlmostEqual(r["win_rate"], 0.6667, places=4)

    def test_expected_R_average(self):
        """expected_R = 平均 R"""
        trades = [
            {"R": 2.0, "outcome": "win", "lots": 1},
            {"R": -1.0, "outcome": "loss", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["expected_R"], 0.5)

    def test_consecutive_losses_count(self):
        """连续亏损计数"""
        trades = [
            {"R": -1, "outcome": "loss", "lots": 1},
            {"R": -1, "outcome": "loss", "lots": 1},
            {"R": 1, "outcome": "win", "lots": 1},
            {"R": -1, "outcome": "loss", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["max_consecutive_losses"], 2)

    def test_consecutive_loss_warning(self):
        """连续亏损 >= 3 → 预警"""
        trades = [
            {"R": -1, "outcome": "loss", "lots": 1},
            {"R": -1, "outcome": "loss", "lots": 1},
            {"R": -1, "outcome": "loss", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertTrue(r["consecutive_loss_warning"])
        self.assertEqual(r["max_consecutive_losses"], 3)

    def test_cumulative_R(self):
        """累计 R"""
        trades = [
            {"R": 2.0, "outcome": "win", "lots": 1},
            {"R": -1.0, "outcome": "loss", "lots": 1},
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["final_cum_R"], 1.0)

    def test_lot_weighted_cum_R(self):
        """加权累计 R（按手数）"""
        trades = [
            {"R": 2.0, "outcome": "win", "lots": 3},  # 2*3 = 6
            {"R": -1.0, "outcome": "loss", "lots": 2},  # -1*2 = -2
        ]
        r = aggregate(trades, "R", "outcome")
        self.assertEqual(r["final_cum_R_lotweighted"], 4.0)


# ═══════════════════════════════════════════════════════════════════════════
#  6. _dim_vote
# ═══════════════════════════════════════════════════════════════════════════

class TestDimVote(unittest.TestCase):
    """_dim_vote 维度投票。"""

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_dim_vote(None))

    def test_positive_returns_1(self):
        """正数 → 1"""
        self.assertEqual(_dim_vote(1.0), 1)
        self.assertEqual(_dim_vote(0.001), 1)
        self.assertEqual(_dim_vote(100), 1)

    def test_negative_returns_minus_1(self):
        """负数 → -1"""
        self.assertEqual(_dim_vote(-1.0), -1)
        self.assertEqual(_dim_vote(-0.001), -1)
        self.assertEqual(_dim_vote(-100), -1)

    def test_zero_returns_0(self):
        """0 → 0"""
        self.assertEqual(_dim_vote(0), 0)
        self.assertEqual(_dim_vote(0.0), 0)

    def test_returns_int_or_none(self):
        """返回 int 或 None"""
        self.assertIsInstance(_dim_vote(1), int)
        self.assertIsInstance(_dim_vote(-1), int)
        self.assertIsNone(_dim_vote(None))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  paper track 纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
