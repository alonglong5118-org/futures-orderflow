#!/usr/bin/env python3
"""
校准 + 缓存 + 文件名解析 纯函数 — 单元测试
======================================================

1. sym_from_cache — 缓存文件名 → 品种名
   - 标准格式：rb_5min.csv → rb
   - 大写：FG_5min.csv → FG
   - 路径前缀：/path/rb_5min.csv → rb

2. sym_from_std — 标准文件名 → 品种名
   - 主连格式：_FG0_min5.csv → FG
   - 小写主连：_jd0_min5.csv → jd
   - 不带 0 后缀的（理论上不会出现）

3. papertrack_recent — 最近 window 笔交易统计
   - 空列表 → None
   - 无匹配品种 → None
   - 不足 window 笔 → 全部计算
   - 超过 window 笔 → 取最近 window 笔
   - 按时间排序
   - 计算 expR / win_rate / cum_R / n
   - R 为 None → 按 0 处理

4. _status_of — 模型状态判定
   - None → insufficient
   - 负收益 → broken
   - 正收益+高于漂移阈值 → healthy
   - 低于漂移阈值 → drift
   - mean_oos <= 0 + 低于 → drift
   - gated=True → drift
   - 有收益但 mean_oos 为负且 cur>mean → healthy

5. best_stop_rr — 最优止损止盈选择
   - 空结果 → None
   - 全部不达标 → 回退到只看交易数
   - 达标组选 expR 最高的
   - 达标组为空但有交易 → 回退选择
   - 返回字段完整
   - win_rate 过滤阈值
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from convert_min5_cache import sym_from_cache, sym_from_std
from four_dim_calibrate import best_stop_rr
from four_dim_recalibrate import _status_of, papertrack_recent

# ═══════════════════════════════════════════════════════════════════════════
#  1. sym_from_cache
# ═══════════════════════════════════════════════════════════════════════════


class TestSymFromCache(unittest.TestCase):
    """sym_from_cache 缓存文件名 → 品种名。"""

    def test_standard_lowercase(self):
        """标准格式：rb_5min.csv → rb"""
        self.assertEqual(sym_from_cache("rb_5min.csv"), "rb")

    def test_uppercase(self):
        """大写：FG_5min.csv → FG"""
        self.assertEqual(sym_from_cache("FG_5min.csv"), "FG")

    def test_with_path_prefix(self):
        """路径前缀：/path/rb_5min.csv → rb"""
        self.assertEqual(sym_from_cache("/data/cache/rb_5min.csv"), "rb")

    def test_two_letter_uppercase(self):
        """双字母大写：SA_5min.csv → SA"""
        self.assertEqual(sym_from_cache("SA_5min.csv"), "SA")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(sym_from_cache("rb_5min.csv"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  2. sym_from_std
# ═══════════════════════════════════════════════════════════════════════════


class TestSymFromStd(unittest.TestCase):
    """sym_from_std 标准文件名 → 品种名。"""

    def test_main_uppercase(self):
        """主连格式：_FG0_min5.csv → FG"""
        self.assertEqual(sym_from_std("_FG0_min5.csv"), "FG")

    def test_main_lowercase(self):
        """小写主连：_jd0_min5.csv → jd"""
        self.assertEqual(sym_from_std("_jd0_min5.csv"), "jd")

    def test_sa_main(self):
        """SA 主连：_SA0_min5.csv → SA"""
        self.assertEqual(sym_from_std("_SA0_min5.csv"), "SA")

    def test_single_letter(self):
        """单字母品种：_V0_min5.csv → V"""
        self.assertEqual(sym_from_std("_V0_min5.csv"), "V")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(sym_from_std("_FG0_min5.csv"), str)

    def test_full_path(self):
        """带路径"""
        self.assertEqual(sym_from_std("/data/min5/_rb0_min5.csv"), "rb")


# ═══════════════════════════════════════════════════════════════════════════
#  3. papertrack_recent
# ═══════════════════════════════════════════════════════════════════════════


class TestPapertrackRecent(unittest.TestCase):
    """papertrack_recent 最近 window 笔交易统计。"""

    def test_empty_list_none(self):
        """空列表 → None"""
        self.assertIsNone(papertrack_recent([], "rb"))

    def test_no_matching_symbol_none(self):
        """无匹配品种 → None"""
        trades = [
            {"symbol": "FG", "time": "2026-08-28", "R": 1.0},
        ]
        self.assertIsNone(papertrack_recent(trades, "rb"))

    def test_fewer_than_window_all_computed(self):
        """不足 window 笔 → 全部计算"""
        trades = [
            {"symbol": "rb", "time": "2026-08-28", "R": 1.0},
            {"symbol": "rb", "time": "2026-08-29", "R": -1.0},
        ]
        result = papertrack_recent(trades, "rb", window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["expR"], 0.0)  # (1 + (-1)) / 2
        self.assertEqual(result["win_rate"], 0.5)
        self.assertEqual(result["cum_R"], 0.0)

    def test_more_than_window_takes_recent(self):
        """超过 window 笔 → 取最近 window 笔"""
        trades = []
        for i in range(20):
            trades.append(
                {
                    "symbol": "rb",
                    "time": f"2026-08-{i + 1:02d}",
                    "R": float(i),  # 0, 1, 2, ..., 19
                }
            )
        result = papertrack_recent(trades, "rb", window=5)
        self.assertEqual(result["n"], 5)
        # 最近 5 笔 R = 15, 16, 17, 18, 19
        self.assertEqual(result["cum_R"], 15 + 16 + 17 + 18 + 19)

    def test_sorted_by_time(self):
        """按时间排序"""
        trades = [
            {"symbol": "rb", "time": "2026-08-29", "R": 1.0},  # 后
            {"symbol": "rb", "time": "2026-08-28", "R": 2.0},  # 先
        ]
        result = papertrack_recent(trades, "rb", window=1)
        # 取最近 1 笔 → 8月29日 → R=1.0
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["expR"], 1.0)

    def test_none_R_treated_as_zero(self):
        """R 为 None → 按 0 处理"""
        trades = [
            {"symbol": "rb", "time": "2026-08-28", "R": None},
        ]
        result = papertrack_recent(trades, "rb")
        self.assertEqual(result["expR"], 0.0)
        self.assertEqual(result["win_rate"], 0.0)  # 0 不算赢

    def test_all_wins(self):
        """全赢 → 胜率 1.0"""
        trades = [
            {"symbol": "rb", "time": "2026-08-28", "R": 1.5},
            {"symbol": "rb", "time": "2026-08-29", "R": 0.5},
        ]
        result = papertrack_recent(trades, "rb")
        self.assertEqual(result["win_rate"], 1.0)

    def test_all_losses(self):
        """全亏 → 胜率 0"""
        trades = [
            {"symbol": "rb", "time": "2026-08-28", "R": -1.0},
            {"symbol": "rb", "time": "2026-08-29", "R": -0.5},
        ]
        result = papertrack_recent(trades, "rb")
        self.assertEqual(result["win_rate"], 0.0)

    def test_returns_dict_or_none(self):
        """返回 dict 或 None"""
        self.assertIsNone(papertrack_recent([], "rb"))
        self.assertIsInstance(papertrack_recent([{"symbol": "rb", "time": "2026-08-28", "R": 1.0}], "rb"), dict)


# ═══════════════════════════════════════════════════════════════════════════
#  4. _status_of
# ═══════════════════════════════════════════════════════════════════════════


class TestStatusOf(unittest.TestCase):
    """_status_of 模型状态判定。"""

    def test_none_expR_insufficient(self):
        """None → insufficient"""
        self.assertEqual(_status_of(None, 1.0, False), "insufficient")

    def test_negative_expR_broken(self):
        """负收益 → broken"""
        self.assertEqual(_status_of(-0.5, 1.0, False), "broken")

    def test_positive_above_threshold_healthy(self):
        """正收益+高于漂移阈值 → healthy"""
        # DRIFT_FACTOR = 0.7（默认）
        # cur = 0.8, mean_oos = 1.0 → 0.8 > 0.7*1.0 = 0.7 → healthy
        self.assertEqual(_status_of(0.8, 1.0, False), "healthy")

    def test_below_drift_threshold_drift(self):
        """低于漂移阈值 → drift（DRIFT_FACTOR=0.5）"""
        # cur = 0.3, mean_oos = 1.0 → 0.3 < 0.5*1.0 = 0.5 → drift
        self.assertEqual(_status_of(0.3, 1.0, False), "drift")

    def test_mean_oos_negative_and_cur_lower_drift(self):
        """mean_oos <= 0 + cur < mean_oos → 先触发 broken（cur<0 优先级更高）"""
        # cur = -0.3 < 0 → 先触发 broken，不走 mean_oos <= 0 的 drift 分支
        self.assertEqual(_status_of(-0.3, -0.2, False), "broken")

    def test_mean_oos_negative_cur_positive_but_below_zero_drift(self):
        """mean_oos 为负，cur 也为负 → broken"""
        self.assertEqual(_status_of(-0.1, -0.2, False), "broken")

    def test_gated_true_drift(self):
        """gated=True → drift"""
        self.assertEqual(_status_of(1.5, 1.0, True), "drift")

    def test_positive_but_mean_negative_cur_higher_healthy(self):
        """mean_oos 为负但 cur > mean 且 cur > 0 → healthy"""
        # mean_oos = -0.5, cur = 0.3
        # mean_oos <= 0 and cur_expR < mean_oos → 0.3 < -0.5? No
        # gated = False
        # → healthy
        self.assertEqual(_status_of(0.3, -0.5, False), "healthy")

    def test_zero_expR_drift(self):
        """cur=0 + mean>0 → drift（0 < 0.5*mean）"""
        # 0 < 0.5*1.0 → drift
        self.assertEqual(_status_of(0.0, 1.0, False), "drift")

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_status_of(1.0, 1.0, False), str)

    def test_exactly_at_drift_threshold(self):
        """恰好在漂移因子处 → healthy（严格小于才算drift）"""
        # cur = 0.5, mean = 1.0 → 0.5 < 0.5? No → healthy
        self.assertEqual(_status_of(0.5, 1.0, False), "healthy")


# ═══════════════════════════════════════════════════════════════════════════
#  5. best_stop_rr
# ═══════════════════════════════════════════════════════════════════════════


class TestBestStopRr(unittest.TestCase):
    """best_stop_rr 最优止损止盈选择。"""

    def test_empty_sweep_none(self):
        """空结果 → None"""
        self.assertIsNone(best_stop_rr({"all": []}))

    def test_all_below_min_trades_none(self):
        """全部不达标（交易数不够） → None"""
        sweep = {
            "all": [
                (1.0, 2.0, {"trades": 5, "win_rate": 0.6, "expR": 0.8}),
            ]
        }
        # min_trades=10（默认）, 5 < 10 → 不达标
        self.assertIsNone(best_stop_rr(sweep))

    def test_fallback_to_trades_only(self):
        """达标组为空但有交易 → 回退选择（只看交易数）"""
        sweep = {
            "all": [
                (1.0, 2.0, {"trades": 15, "win_rate": 0.3, "expR": 0.5}),
                # 胜率 0.3 < 0.4 → 不达标
                # 但 trades 15 >= 10 → 回退组
            ]
        }
        result = best_stop_rr(sweep)
        self.assertIsNotNone(result)
        self.assertEqual(result["stop_atr_mult"], 1.0)
        self.assertEqual(result["rr_ratio"], 2.0)

    def test_picks_highest_expR_in_valid(self):
        """达标组选 expR 最高的"""
        sweep = {
            "all": [
                (1.0, 2.0, {"trades": 20, "win_rate": 0.5, "expR": 0.5}),
                (1.5, 2.5, {"trades": 20, "win_rate": 0.55, "expR": 0.8}),
                (2.0, 3.0, {"trades": 20, "win_rate": 0.45, "expR": 0.6}),
            ]
        }
        result = best_stop_rr(sweep)
        # expR 最高的是第二个 (1.5, 2.5) → 0.8
        self.assertEqual(result["stop_atr_mult"], 1.5)
        self.assertEqual(result["rr_ratio"], 2.5)
        self.assertEqual(result["expR"], 0.8)

    def test_win_rate_filter_threshold(self):
        """胜率过滤阈值 0.4"""
        sweep = {
            "all": [
                (1.0, 2.0, {"trades": 20, "win_rate": 0.39, "expR": 1.0}),
                # 胜率 0.39 < 0.4 → 不达标（但交易数够，进回退组）
                (1.5, 2.5, {"trades": 20, "win_rate": 0.4, "expR": 0.5}),
                # 胜率 = 0.4 → 不达标（>=0.4? 用 >= 才达标）
            ]
        }
        # 要看函数中是 >= 0.4 还是 > 0.4
        result = best_stop_rr(sweep)
        # 如果 win_rate >= 0.4 才算达标：第二个达标，选 expR=0.5 的
        # 如果 win_rate > 0.4：都不达标，回退组选 expR=1.0 的
        # 实际代码是 >= 0.4
        self.assertEqual(result["stop_atr_mult"], 1.5)
        self.assertEqual(result["expR"], 0.5)

    def test_return_fields_complete(self):
        """返回字段完整"""
        sweep = {
            "all": [
                (1.5, 2.0, {"trades": 15, "win_rate": 0.55, "expR": 0.7}),
            ]
        }
        result = best_stop_rr(sweep)
        for key in ("stop_atr_mult", "rr_ratio", "expR", "win_rate", "trades"):
            self.assertIn(key, result)

    def test_custom_min_trades(self):
        """自定义 min_trades"""
        sweep = {
            "all": [
                (1.0, 2.0, {"trades": 8, "win_rate": 0.5, "expR": 0.5}),
            ]
        }
        # 默认 min_trades=10 → 8 < 10 → None
        self.assertIsNone(best_stop_rr(sweep))
        # min_trades=5 → 8 >= 5 → 有结果
        result = best_stop_rr(sweep, min_trades=5)
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  校准 + 缓存 + 文件名解析 纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
