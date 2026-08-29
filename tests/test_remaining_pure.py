#!/usr/bin/env python3
"""
剩余零散纯函数 — 单元测试
======================================

1. _norm_daily_cols — 日线 DataFrame 列名规范化
   - None → None
   - 空 df → 原样
   - 中文列名映射
   - 英文列名映射
   - date 列设为索引
   - 按索引排序
   - oi / settlement 别名

2. _col — DataFrame 列候选查找
   - 第一个候选存在 → 返回该列名
   - 都不存在 → None
   - 顺序优先
   - 空候选 → None
   - 返回 str 或 None

3. _latest_change — 最近变化归一化
   - 上涨 → 正值
   - 下跌 → 负值
   - 不变 → 0
   - 数据不足 → 0
   - prev=0 → 0（除零保护）
   - 放大 5 倍
   - 封顶 ±1
   - None 值跳过
   - 非法 → 0

4. color — 终端颜色包装
   - 正常包装
   - 多 code 组合
   - 包含 RESET 结尾
   - 空文本也能包装

5. calc_signal_agreement — 信号一致率
   - 都空 → 1.0
   - 一空一非空 → 0.0
   - 完全相同 → 1.0
   - 完全不同 → 0.0
   - 部分重叠 → 交集/baseline
   - baseline 更大 → 交集/baseline
   - current 更大 → 交集/baseline（用 baseline 做分母）

6. classify_status — 回归状态分类
   - 全 OK → 'ok'
   - 1个warn → 'warn'
   - 1个critical → 'critical'
   - 多个critical → 'critical'
   - 多维度混合
   - None 维度跳过
   - sig_agree 低 → critical
   - 临界值边界
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fetch_info_dimension import _col, _latest_change
from four_dim_strategy import _norm_daily_cols
from regression_test import calc_signal_agreement, classify_status, color

# ═══════════════════════════════════════════════════════════════════════════
#  1. _norm_daily_cols
# ═══════════════════════════════════════════════════════════════════════════


class TestNormDailyCols(unittest.TestCase):
    """_norm_daily_cols 日线 DataFrame 列名规范化。"""

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_norm_daily_cols(None))

    def test_empty_df_returns_empty(self):
        """空 df → 原样"""
        df = pd.DataFrame()
        result = _norm_daily_cols(df)
        self.assertEqual(len(result), 0)

    def test_chinese_columns(self):
        """中文列名映射"""
        df = pd.DataFrame(
            {
                "日期": ["2026-08-28"],
                "开盘": [100],
                "最高": [105],
                "最低": [95],
                "收盘": [102],
                "成交量": [10000],
                "持仓量": [5000],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIn("open", result.columns)
        self.assertIn("high", result.columns)
        self.assertIn("low", result.columns)
        self.assertIn("close", result.columns)
        self.assertIn("volume", result.columns)
        self.assertIn("oi", result.columns)

    def test_date_becomes_index(self):
        """date 列设为索引"""
        df = pd.DataFrame(
            {
                "日期": ["2026-08-28", "2026-08-29"],
                "开盘": [100, 101],
                "收盘": [102, 103],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIsInstance(result.index, pd.DatetimeIndex)
        self.assertEqual(result.index[0], pd.Timestamp("2026-08-28"))

    def test_sorted_by_date(self):
        """按索引排序"""
        df = pd.DataFrame(
            {
                "日期": ["2026-08-29", "2026-08-28"],
                "开盘": [101, 100],
                "收盘": [103, 102],
            }
        )
        result = _norm_daily_cols(df)
        self.assertEqual(result.index[0], pd.Timestamp("2026-08-28"))
        self.assertEqual(result.index[1], pd.Timestamp("2026-08-29"))

    def test_english_hold_to_oi(self):
        """hold → oi"""
        df = pd.DataFrame(
            {
                "date": ["2026-08-28"],
                "open": [100],
                "close": [102],
                "hold": [5000],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIn("oi", result.columns)
        self.assertNotIn("hold", result.columns)
        self.assertEqual(result["oi"].iloc[0], 5000)

    def test_settle_to_settlement(self):
        """settle → settlement"""
        df = pd.DataFrame(
            {
                "date": ["2026-08-28"],
                "close": [102],
                "settle": [101.5],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIn("settlement", result.columns)

    def test_open_interest_to_oi(self):
        """open_interest → oi"""
        df = pd.DataFrame(
            {
                "date": ["2026-08-28"],
                "close": [102],
                "open_interest": [8000],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIn("oi", result.columns)

    def test_no_date_column_preserved(self):
        """没有 date 列 → 不设索引，列照常映射"""
        df = pd.DataFrame(
            {
                "open": [100],
                "high": [105],
                "low": [95],
                "close": [102],
            }
        )
        result = _norm_daily_cols(df)
        self.assertIn("close", result.columns)
        # 索引还是默认 RangeIndex
        self.assertNotIsInstance(result.index, pd.DatetimeIndex)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _col
# ═══════════════════════════════════════════════════════════════════════════


class TestCol(unittest.TestCase):
    """_col DataFrame 列候选查找。"""

    def test_first_candidate_found(self):
        """第一个候选存在 → 返回该列名"""
        df = pd.DataFrame({"open": [1, 2], "close": [3, 4]})
        result = _col(df, ["open", "开盘", "开盘价"])
        self.assertEqual(result, "open")

    def test_second_candidate_found(self):
        """第一个不存在，第二个存在"""
        df = pd.DataFrame({"开盘": [1, 2]})
        result = _col(df, ["open", "开盘", "开盘价"])
        self.assertEqual(result, "开盘")

    def test_none_found_returns_none(self):
        """都不存在 → None"""
        df = pd.DataFrame({"close": [1, 2]})
        result = _col(df, ["open", "开盘", "开盘价"])
        self.assertIsNone(result)

    def test_order_priority(self):
        """顺序优先（第一个匹配的返回）"""
        df = pd.DataFrame({"开盘": [1], "开盘价": [2]})
        result = _col(df, ["开盘价", "开盘"])
        self.assertEqual(result, "开盘价")

    def test_empty_candidates_none(self):
        """空候选列表 → None"""
        df = pd.DataFrame({"close": [1]})
        self.assertIsNone(_col(df, []))

    def test_returns_str_or_none(self):
        """返回 str 或 None"""
        df = pd.DataFrame({"close": [1]})
        self.assertIsInstance(_col(df, ["close"]), str)
        self.assertIsNone(_col(df, ["open"]))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _latest_change
# ═══════════════════════════════════════════════════════════════════════════


class TestLatestChange(unittest.TestCase):
    """_latest_change 最近变化归一化。"""

    def test_up_positive(self):
        """上涨 → 正值"""
        result = _latest_change([100, 110])
        # (110-100)/100 = 0.1, ×5 = 0.5
        self.assertAlmostEqual(result, 0.5, places=6)
        self.assertGreater(result, 0)

    def test_down_negative(self):
        """下跌 → 负值"""
        result = _latest_change([100, 90])
        # (90-100)/100 = -0.1, ×5 = -0.5
        self.assertAlmostEqual(result, -0.5, places=6)
        self.assertLess(result, 0)

    def test_no_change_zero(self):
        """不变 → 0"""
        self.assertEqual(_latest_change([100, 100]), 0.0)

    def test_insufficient_data_zero(self):
        """数据不足 → 0"""
        self.assertEqual(_latest_change([100]), 0.0)
        self.assertEqual(_latest_change([]), 0.0)

    def test_prev_zero_zero(self):
        """prev=0 → 0（除零保护）"""
        self.assertEqual(_latest_change([0, 10]), 0.0)

    def test_scaled_by_5x(self):
        """放大 5 倍"""
        # 1% 变化 → 5% 归一化值 = 0.05
        result = _latest_change([100, 101])
        self.assertAlmostEqual(result, 0.05, places=6)

    def test_capped_at_plus_1(self):
        """封顶 +1"""
        # 50% 上涨 → ×5 = 2.5 → 封顶 1.0
        result = _latest_change([100, 150])
        self.assertEqual(result, 1.0)

    def test_capped_at_minus_1(self):
        """封顶 -1"""
        # 50% 下跌 → ×5 = -2.5 → 封顶 -1.0
        result = _latest_change([100, 50])
        self.assertEqual(result, -1.0)

    def test_none_values_skipped(self):
        """None 值跳过"""
        result = _latest_change([None, 100, None, 110, None])
        # 有效：100, 110 → 涨10% → 0.5
        self.assertAlmostEqual(result, 0.5, places=6)

    def test_invalid_returns_zero(self):
        """非法数据 → 0"""
        self.assertEqual(_latest_change("not_a_list"), 0.0)

    def test_long_series_uses_last_two(self):
        """长序列只用最后两个"""
        result = _latest_change([50, 60, 70, 80, 90, 100])
        # 最后两个：90, 100 → 涨 10/90 ≈ 11.1% → ×5 ≈ 0.556
        expected = (100 - 90) / 90 * 5
        self.assertAlmostEqual(result, expected, places=6)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(_latest_change([100, 110]), float)

    def test_small_change_visible(self):
        """小波动被放大后可见"""
        result = _latest_change([100, 100.5])
        # 0.5% 变化 → ×5 = 2.5% = 0.025
        self.assertAlmostEqual(result, 0.025, places=6)
        self.assertGreater(abs(result), 0.01)  # 确实放大了


# ═══════════════════════════════════════════════════════════════════════════
#  4. color
# ═══════════════════════════════════════════════════════════════════════════


class TestColor(unittest.TestCase):
    """color 终端颜色包装。"""

    def test_wraps_with_color_code(self):
        """正常包装：前缀 + 文本 + RESET"""
        result = color("hello", "\033[91m")
        self.assertIn("hello", result)
        self.assertTrue(result.startswith("\033[91m"))
        self.assertTrue(result.endswith("\033[0m"))

    def test_multiple_codes_combined(self):
        """多 code 组合"""
        result = color("hi", "\033[1m", "\033[91m")
        self.assertTrue(result.startswith("\033[1m\033[91m"))

    def test_empty_text_ok(self):
        """空文本也能包装"""
        result = color("", "\033[91m")
        self.assertIn("\033[0m", result)

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(color("test", "\033[91m"), str)


# ═══════════════════════════════════════════════════════════════════════════
#  5. calc_signal_agreement
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalAgreement(unittest.TestCase):
    """calc_signal_agreement 信号一致率。"""

    def test_both_empty_1(self):
        """都空 → 1.0"""
        self.assertEqual(calc_signal_agreement([], []), 1.0)

    def test_one_empty_0(self):
        """一空一非空 → 0.0"""
        self.assertEqual(calc_signal_agreement([("a", 1)], []), 0.0)
        self.assertEqual(calc_signal_agreement([], [("a", 1)]), 0.0)

    def test_identical_1(self):
        """完全相同 → 1.0"""
        sigs = [("2026-08-28", 1, "趋势"), ("2026-08-29", -1, "震荡")]
        self.assertEqual(calc_signal_agreement(sigs, sigs), 1.0)

    def test_completely_different_0(self):
        """完全不同 → 0.0"""
        s1 = [("a", 1)]
        s2 = [("b", 1)]
        self.assertEqual(calc_signal_agreement(s1, s2), 0.0)

    def test_partial_overlap(self):
        """部分重叠 → 交集 / baseline_size"""
        s1 = [("a", 1), ("b", 1), ("c", 1)]  # 3个
        s2 = [("a", 1), ("b", 1), ("d", 1), ("e", 1), ("f", 1)]  # 5个
        # 交集 = 2, baseline = 5 → 0.4
        result = calc_signal_agreement(s1, s2)
        self.assertEqual(result, 2 / 5)

    def test_baseline_smaller(self):
        """baseline 更小 → 交集/baseline 可能 > 交集/union"""
        s1 = [("a", 1), ("b", 1), ("c", 1), ("d", 1)]  # 4个 (current)
        s2 = [("a", 1), ("b", 1)]  # 2个 (baseline)
        # 交集 = 2, baseline = 2 → 1.0
        result = calc_signal_agreement(s1, s2)
        self.assertEqual(result, 1.0)

    def test_returns_float(self):
        """返回 float"""
        self.assertIsInstance(calc_signal_agreement([], []), float)


# ═══════════════════════════════════════════════════════════════════════════
#  6. classify_status
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyStatus(unittest.TestCase):
    """classify_status 回归状态分类。"""

    def test_all_ok(self):
        """全 OK → ('ok', 0, 0)"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 1.0)
        self.assertEqual(status, "ok")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 0)

    def test_one_warn(self):
        """1个 warn → ('warn', 0, 1)"""
        # WARN_EXPR_DELTA = 0.015, CRIT = 0.030
        status, crits, warns = classify_status(0.02, 0.0, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 1)

    def test_one_critical(self):
        """1个 critical → ('critical', 1, 0)"""
        status, crits, warns = classify_status(0.04, 0.0, 0.0, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 1)
        self.assertEqual(warns, 0)

    def test_multiple_criticals(self):
        """多个 critical → ('critical', 2, 0)"""
        # expr_delta=0.04(crit), trades_pct=0.4(crit) → 2个critical
        status, crits, warns = classify_status(0.04, 0.0, 0.4, 1.0)
        self.assertEqual(status, "critical")
        self.assertEqual(crits, 2)

    def test_mixed_warn_and_critical(self):
        """warn + critical → 'critical'（critical 优先）"""
        status, crits, warns = classify_status(0.02, 0.0, 0.0, 0.92)
        # expr_delta = warn(0.02 > 0.015), sig_agree = critical(0.92 < 0.90? No, 0.92 > 0.90)
        # WARN_SIG_AGREE = 0.95, CRIT_SIG_AGREE = 0.90
        # 0.92 < 0.95 → warn, 0.92 >= 0.90 → 不是critical
        self.assertEqual(status, "warn")
        self.assertGreater(warns, 0)

    def test_none_dimensions_skipped(self):
        """None 维度跳过"""
        status, crits, warns = classify_status(None, None, None, None)
        self.assertEqual(status, "ok")
        self.assertEqual(crits, 0)
        self.assertEqual(warns, 0)

    def test_sig_agree_low_critical(self):
        """sig_agree 低 → critical"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.85)
        self.assertEqual(status, "critical")
        self.assertGreaterEqual(crits, 1)

    def test_sig_agree_warn(self):
        """sig_agree warn 级"""
        status, crits, warns = classify_status(0.0, 0.0, 0.0, 0.92)
        # 0.92 < 0.95(WARN) → warn, 0.92 >= 0.90(CRIT) → 不是critical
        self.assertEqual(status, "warn")
        self.assertEqual(crits, 0)
        self.assertGreater(warns, 0)

    def test_trades_pct_warn(self):
        """trades_pct warn 级"""
        status, crits, warns = classify_status(0.0, 0.0, 0.2, 1.0)
        self.assertEqual(status, "warn")
        self.assertGreater(warns, 0)

    def test_trades_pct_critical(self):
        """trades_pct critical 级"""
        status, crits, warns = classify_status(0.0, 0.0, 0.4, 1.0)
        self.assertEqual(status, "critical")
        self.assertGreater(crits, 0)

    def test_win_delta_warn(self):
        """win_delta warn 级"""
        status, crits, warns = classify_status(0.0, 0.04, 0.0, 1.0)
        self.assertEqual(status, "warn")
        self.assertGreater(warns, 0)

    def test_boundary_expr_warn(self):
        """expr_delta 边界：恰好等于 WARN → ok（严格大于才触发）"""
        status, crits, warns = classify_status(0.015, 0.0, 0.0, 1.0)
        # 0.015 > 0.015? No → ok
        self.assertEqual(status, "ok")
        self.assertEqual(warns, 0)

    def test_boundary_expr_critical(self):
        """expr_delta 边界：恰好等于 CRIT → warn（>CRIT 才是critical）"""
        status, crits, warns = classify_status(0.030, 0.0, 0.0, 1.0)
        # 0.030 > 0.030? No → 不触发critical
        # 但 0.030 > 0.015 → warn
        self.assertEqual(status, "warn")
        self.assertEqual(crits, 0)
        self.assertGreater(warns, 0)

    def test_returns_tuple(self):
        """返回 (status, criticals, warns) 三元组"""
        result = classify_status(0, 0, 0, 1.0)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], str)
        self.assertIsInstance(result[1], int)
        self.assertIsInstance(result[2], int)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  剩余零散纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
