#!/usr/bin/env python3
"""
龙虎榜工具 — 单元测试
===========================

1. to_num — 数值转换（去千分位逗号）
   - 普通数字字符串 → float
   - 带千分位逗号 → 去掉逗号后转 float
   - 空串 → 0.0
   - "nan" / "None" → 0.0
   - 纯数字 → 直接转 float
   - 非法字符串 → 0.0
   - pandas Series → 求和

2. compute_c_score — 龙虎榜 C 分计算
   - 净多增仓 → 正 C 分
   - 净空增仓 → 负 C 分
   - 净变化权重 75%，绝对净持仓权重 25%
   - 净变化按总持仓 2% 封顶（最低 300）
   - 绝对净持仓按总持仓 10% 封顶（最低 1000）
   - C 分范围 [-100, 100]
   - 零变化零净持仓 → 0 分
   - 极端情况 clamp 到 ±100
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from long_hu_bang import compute_c_score, to_num

# ═══════════════════════════════════════════════════════════════════════════
#  1. to_num
# ═══════════════════════════════════════════════════════════════════════════


class TestToNum(unittest.TestCase):
    """to_num 数值转换（去千分位逗号）。"""

    def test_plain_number_string(self):
        """普通数字字符串 → float"""
        self.assertEqual(to_num("12345"), 12345.0)
        self.assertEqual(to_num("3.14"), 3.14)

    def test_thousands_separator(self):
        """带千分位逗号 → 去掉逗号后转 float"""
        self.assertEqual(to_num("157,909"), 157909.0)
        self.assertEqual(to_num("1,234,567.89"), 1234567.89)

    def test_empty_string_returns_zero(self):
        """空串 → 0.0"""
        self.assertEqual(to_num(""), 0.0)

    def test_nan_returns_zero(self):
        """ "nan" → 0.0（小写）"""
        self.assertEqual(to_num("nan"), 0.0)

    def test_none_string_returns_zero(self):
        """ "None" → 0.0"""
        self.assertEqual(to_num("None"), 0.0)

    def test_already_number(self):
        """已经是数字 → 直接转 float"""
        self.assertEqual(to_num(123), 123.0)
        self.assertEqual(to_num(3.14), 3.14)
        self.assertEqual(to_num(0), 0.0)

    def test_invalid_string_returns_zero(self):
        """非法字符串 → 0.0（不崩溃）"""
        self.assertEqual(to_num("abc"), 0.0)
        self.assertEqual(to_num("--"), 0.0)

    def test_whitespace_stripped(self):
        """前后空白被去掉"""
        self.assertEqual(to_num("  123  "), 123.0)
        self.assertEqual(to_num(" 1,000.5 "), 1000.5)

    def test_negative_number(self):
        """负数也能转"""
        self.assertEqual(to_num("-123.45"), -123.45)
        self.assertEqual(to_num("-1,234"), -1234.0)

    def test_pandas_series_sum(self):
        """pandas Series → 逐元素转数后求和"""
        import pandas as pd

        s = pd.Series(["1,000", "2,000", "3,000"])
        self.assertEqual(to_num(s), 6000.0)

    def test_pandas_series_with_invalid(self):
        """Series 里有非法值 → 按 0 算"""
        import pandas as pd

        s = pd.Series(["1,000", "abc", "2,000", "nan"])
        self.assertEqual(to_num(s), 3000.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. compute_c_score
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeCScore(unittest.TestCase):
    """compute_c_score 龙虎榜 C 分计算。"""

    def test_net_long_increase_positive_score(self):
        """净多增仓 → 正 C 分"""
        rec = {
            "long_oi": 10000,
            "short_oi": 8000,  # 净多 2000
            "long_chg": 500,
            "short_chg": 100,  # 净增 400
        }
        result = compute_c_score(rec)
        self.assertGreater(result["C_score"], 0)

    def test_net_short_increase_negative_score(self):
        """净空增仓 → 负 C 分"""
        rec = {
            "long_oi": 8000,
            "short_oi": 10000,  # 净空 2000
            "long_chg": 100,
            "short_chg": 500,  # 净空增 400
        }
        result = compute_c_score(rec)
        self.assertLess(result["C_score"], 0)

    def test_weight_ratio_75_25(self):
        """权重：净变化 75% + 绝对净持仓 25%"""
        # 构造：净变化得分 = 100，绝对净持仓得分 = 0
        # 总 C 分应该 = 0.75 * 100 + 0.25 * 0 = 75
        rec = {
            "long_oi": 10000,
            "short_oi": 10000,  # 净持仓 = 0 → net_score = 0
            "long_chg": 500,
            "short_chg": 100,  # 净增 400
        }
        # total_oi = 20000, chg_ref = max(300, 20000*0.02) = 400
        # net_chg = 400, net_chg_score = 400/400 * 100 = 100
        # net = 0, net_ref = max(1000, 20000*0.10) = 2000, net_score = 0
        # C = 0.75*100 + 0.25*0 = 75
        result = compute_c_score(rec)
        self.assertAlmostEqual(result["C_score"], 75.0, places=1)

    def test_chg_cap_at_2_percent(self):
        """净变化按总持仓 2% 封顶 → 超过 2% 也只给 100 分"""
        rec = {
            "long_oi": 10000,
            "short_oi": 10000,
            "long_chg": 2000,
            "short_chg": 0,  # 净增 2000，远超 2%
        }
        # total_oi = 20000, chg_ref = 400
        # net_chg = 2000, net_chg_score = min(100, 2000/400*100) = 100
        result = compute_c_score(rec)
        self.assertAlmostEqual(result["C_score"], 75.0, places=1)
        # 即使再加大变化，分数也不会再涨
        rec2 = {**rec, "long_chg": 5000}
        result2 = compute_c_score(rec2)
        self.assertEqual(result["C_score"], result2["C_score"])

    def test_net_position_cap_at_10_percent(self):
        """绝对净持仓按总持仓 10% 封顶"""
        # 构造：净变化 = 0，只有绝对净持仓贡献
        # 净持仓远大于 10% → net_score = 100（封顶）
        rec = {
            "long_oi": 15000,
            "short_oi": 5000,  # 净多 10000 = 50%，远超 10%
            "long_chg": 0,
            "short_chg": 0,
        }
        # total_oi = 20000, net_ref = max(1000, 20000*0.10) = 2000
        # net = 10000, net_score = min(100, 10000/2000*100) = 100
        # net_chg = 0, net_chg_score = 0
        # C = 0.75*0 + 0.25*100 = 25
        result = compute_c_score(rec)
        self.assertAlmostEqual(result["C_score"], 25.0, places=1)

    def test_score_range_minus_100_to_100(self):
        """C 分范围 [-100, 100]"""
        # 极端看多
        rec_bull = {
            "long_oi": 20000,
            "short_oi": 0,
            "long_chg": 10000,
            "short_chg": 0,
        }
        result_bull = compute_c_score(rec_bull)
        self.assertLessEqual(result_bull["C_score"], 100.0)
        self.assertGreater(result_bull["C_score"], 0)

        # 极端看空
        rec_bear = {
            "long_oi": 0,
            "short_oi": 20000,
            "long_chg": 0,
            "short_chg": 10000,
        }
        result_bear = compute_c_score(rec_bear)
        self.assertGreaterEqual(result_bear["C_score"], -100.0)
        self.assertLess(result_bear["C_score"], 0)

    def test_zero_change_zero_net(self):
        """零变化 + 零净持仓 → 0 分"""
        rec = {
            "long_oi": 10000,
            "short_oi": 10000,
            "long_chg": 0,
            "short_chg": 0,
        }
        result = compute_c_score(rec)
        self.assertEqual(result["C_score"], 0.0)
        self.assertEqual(result["net"], 0)
        self.assertEqual(result["net_chg"], 0)

    def test_return_fields(self):
        """返回字段齐全"""
        rec = {
            "long_oi": 10000,
            "short_oi": 8000,
            "long_chg": 500,
            "short_chg": 100,
        }
        result = compute_c_score(rec)
        self.assertIn("C_score", result)
        self.assertIn("net", result)
        self.assertIn("net_chg", result)
        self.assertIn("long_oi", result)
        self.assertIn("short_oi", result)
        self.assertIn("long_chg", result)
        self.assertIn("short_chg", result)
        self.assertIn("total_oi", result)

    def test_total_oi_correct(self):
        """total_oi = long_oi + short_oi"""
        rec = {
            "long_oi": 12345,
            "short_oi": 6789,
            "long_chg": 100,
            "short_chg": 50,
        }
        result = compute_c_score(rec)
        self.assertEqual(result["total_oi"], 12345 + 6789)

    def test_minimum_reference_values(self):
        """极小持仓时，参考值有最低保障（300 / 1000）"""
        rec = {
            "long_oi": 100,
            "short_oi": 100,  # 总持仓 200
            "long_chg": 50,
            "short_chg": 10,  # 净增 40
        }
        # total_oi = 200
        # chg_ref = max(300, 200*0.02) = max(300, 4) = 300
        # net_ref = max(1000, 200*0.10) = max(1000, 20) = 1000
        result = compute_c_score(rec)
        # net_chg_score = 40/300 * 100 ≈ 13.33
        # net_score = 0 (net=0)
        # C ≈ 0.75 * 13.33 + 0 ≈ 10
        self.assertGreater(result["C_score"], 0)
        self.assertLess(result["C_score"], 20)

    def test_symmetry_bull_bear(self):
        """对称性：多空镜像的 C 分绝对值相等"""
        rec_bull = {
            "long_oi": 10000,
            "short_oi": 8000,
            "long_chg": 500,
            "short_chg": 100,
        }
        rec_bear = {
            "long_oi": 8000,
            "short_oi": 10000,
            "long_chg": 100,
            "short_chg": 500,
        }
        result_bull = compute_c_score(rec_bull)
        result_bear = compute_c_score(rec_bear)
        self.assertAlmostEqual(result_bull["C_score"], -result_bear["C_score"], places=5)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  龙虎榜工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
