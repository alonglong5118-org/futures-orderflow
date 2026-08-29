#!/usr/bin/env python3
"""
HMM 市场状态识别 — 单元测试
===================================

1. _features_raw — 从日线数据构造特征
   - 正常数据 → 返回二维数组 [log_ret, rolling_vol]
   - 数据不足 → None
   - 不含 close 列 → None
   - 返回行数 = len(close) - 1（对数收益少一根）
   - 去除 NaN 行

2. _rule_label — 规则分桶（无 hmmlearn 退化路径）
   - 高波动（vol 在 75% 分位以上）→ high_vol
   - 高正收益 + 非高波动 → trend_up
   - 高负收益 + 非高波动 → trend_down
   - 收益近零 + 非高波动 → choppy

3. thr_mult — 阈值乘数查询
   - trend_up / trend_down → 0.90
   - choppy → 1.15
   - high_vol → 1.25
   - 未知标签 → 默认 1.0
"""

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from regime_hmm import DEFAULT_THR_MULT, _features_raw, _rule_label, thr_mult

# ═══════════════════════════════════════════════════════════════════════════
#  1. _features_raw
# ═══════════════════════════════════════════════════════════════════════════


class TestFeaturesRaw(unittest.TestCase):
    """_features_raw 从日线数据构造特征。"""

    def test_normal_data_returns_2d_array(self):
        """正常数据 → 返回二维数组 [log_ret, rolling_vol]"""
        import pandas as pd

        # 构造 50 根上升趋势的日线
        close = np.linspace(100, 120, 50)
        df = pd.DataFrame({"close": close})
        X = _features_raw(df)
        self.assertIsNotNone(X)
        self.assertEqual(X.shape[1], 2)  # 两列：ret, vol
        self.assertGreater(X.shape[0], 30)  # 至少 30 行有效数据

    def test_insufficient_data_returns_none(self):
        """数据不足（<40 根）→ None"""
        import pandas as pd

        close = np.linspace(100, 105, 20)
        df = pd.DataFrame({"close": close})
        self.assertIsNone(_features_raw(df))

    def test_no_close_column_returns_none(self):
        """不含 close 列 → None"""
        import pandas as pd

        df = pd.DataFrame({"open": [100, 101, 102], "high": [101, 102, 103]})
        self.assertIsNone(_features_raw(df))

    def test_log_return_correctness(self):
        """对数收益计算正确：ret[i] = log(close[i+1]) - log(close[i])"""
        import pandas as pd

        close = np.array(
            [
                100.0,
                105.0,
                110.0,
                115.0,
                120.0,
                118.0,
                122.0,
                125.0,
                123.0,
                126.0,
                128.0,
                130.0,
                129.0,
                131.0,
                133.0,
                135.0,
                134.0,
                136.0,
                138.0,
                140.0,
                139.0,
                141.0,
                143.0,
                145.0,
                144.0,
                146.0,
                148.0,
                150.0,
                149.0,
                151.0,
                153.0,
                155.0,
                154.0,
                156.0,
                158.0,
                160.0,
                159.0,
                161.0,
                163.0,
                165.0,
            ]
        )
        df = pd.DataFrame({"close": close})
        X = _features_raw(df)
        # 第一列是对数收益
        expected_first_ret = np.log(close[1]) - np.log(close[0])
        self.assertAlmostEqual(X[0, 0], expected_first_ret, places=8)

    def test_volatility_non_negative(self):
        """波动率始终 ≥ 0"""
        import pandas as pd

        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(60) * 2)
        df = pd.DataFrame({"close": close})
        X = _features_raw(df)
        self.assertTrue(np.all(X[:, 1] >= 0))

    def test_no_nan_in_output(self):
        """输出中不含 NaN"""
        import pandas as pd

        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(60) * 2)
        df = pd.DataFrame({"close": close})
        X = _features_raw(df)
        self.assertFalse(np.isnan(X).any())


# ═══════════════════════════════════════════════════════════════════════════
#  2. _rule_label
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleLabel(unittest.TestCase):
    """_rule_label 规则分桶（无 hmmlearn 退化路径）。"""

    def _make_Xz(self, ret_val, vol_val, n_samples=20):
        """构造标准化后的特征矩阵，最后一个样本为指定值。
        调整分位使 vol_val 不在 75% 分位以上（除非特意设高）。"""
        Xz = np.zeros((n_samples, 2))
        # ret 列：大部分接近 0
        Xz[:, 0] = np.random.RandomState(42).randn(n_samples) * 0.05
        Xz[-1, 0] = ret_val
        # vol 列：大部分在低位
        Xz[:, 1] = np.abs(np.random.RandomState(42).randn(n_samples)) * 0.3
        Xz[-1, 1] = vol_val
        return Xz

    def test_high_vol_label(self):
        """高波动（vol 在 75% 分位以上）→ high_vol"""
        # vol 设成极高，确保在 75% 分位以上
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.1  # 收益不重要
        Xz[:, 1] = np.linspace(0.1, 0.5, 20)  # vol 从低到高
        # 最后一个 vol = 0.5，在 75% 分位以上（因为 75% 分位 ≈ 0.4）
        label = _rule_label(Xz)
        self.assertEqual(label, "high_vol")

    def test_trend_up_label(self):
        """高正收益 + 非高波动 → trend_up"""
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.05  # 普通收益
        Xz[-1, 0] = 0.3  # 最新一根高正收益（>0.15）
        # vol 大部分很高，但最新一根 vol 很低（明显低于 75% 分位）
        Xz[:, 1] = 0.8  # 大部分样本 vol=0.8
        Xz[-1, 1] = 0.2  # 最新 vol=0.2，远低于 75% 分位（≈0.8）
        label = _rule_label(Xz)
        self.assertEqual(label, "trend_up")

    def test_trend_down_label(self):
        """高负收益 + 非高波动 → trend_down"""
        Xz = np.zeros((20, 2))
        Xz[:, 0] = -0.05
        Xz[-1, 0] = -0.3  # 最新一根高负收益（<-0.15）
        Xz[:, 1] = 0.8
        Xz[-1, 1] = 0.2
        label = _rule_label(Xz)
        self.assertEqual(label, "trend_down")

    def test_choppy_label(self):
        """收益近零 + 非高波动 → choppy"""
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.05  # 普通小波动
        Xz[-1, 0] = 0.05  # 最新收益接近 0（在 -0.15 ~ 0.15 之间）
        Xz[:, 1] = 0.8  # 大部分 vol 高
        Xz[-1, 1] = 0.2  # 最新 vol 低（远低于 75% 分位）
        label = _rule_label(Xz)
        self.assertEqual(label, "choppy")

    def test_high_vol_overrides_direction(self):
        """高波动优先于方向判断（即使是趋势）"""
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.0
        Xz[-1, 0] = 0.3  # 高正收益（本应 trend_up）
        Xz[:, 1] = np.linspace(0.1, 0.8, 20)  # 但 vol 极高
        label = _rule_label(Xz)
        self.assertEqual(label, "high_vol")

    def test_label_is_string(self):
        """返回值是字符串"""
        Xz = np.zeros((20, 2))
        Xz[:, 0] = 0.0
        Xz[:, 1] = 0.2
        label = _rule_label(Xz)
        self.assertIsInstance(label, str)

    def test_valid_labels_only(self):
        """返回值一定是四种标签之一"""
        valid = {"trend_up", "trend_down", "choppy", "high_vol"}
        # 多种不同输入
        for ret_val in [-0.5, -0.2, -0.05, 0, 0.05, 0.2, 0.5]:
            for vol_val in [0.1, 0.5, 1.0]:
                Xz = np.zeros((20, 2))
                Xz[:, 0] = np.linspace(-0.1, 0.1, 20)
                Xz[-1, 0] = ret_val
                Xz[:, 1] = np.linspace(0.05, vol_val, 20)
                label = _rule_label(Xz)
                self.assertIn(label, valid, f"ret={ret_val}, vol={vol_val} → {label}")


# ═══════════════════════════════════════════════════════════════════════════
#  3. thr_mult
# ═══════════════════════════════════════════════════════════════════════════


class TestThrMult(unittest.TestCase):
    """thr_mult 阈值乘数查询。"""

    def test_trend_up_09(self):
        """trend_up → 0.90（顺势降阈值）"""
        self.assertEqual(thr_mult("trend_up"), 0.90)

    def test_trend_down_09(self):
        """trend_down → 0.90（顺势降阈值）"""
        self.assertEqual(thr_mult("trend_down"), 0.90)

    def test_choppy_115(self):
        """choppy → 1.15（震荡抬阈值，抑制假突破）"""
        self.assertEqual(thr_mult("choppy"), 1.15)

    def test_high_vol_125(self):
        """high_vol → 1.25（高波动抬阈值，控风险）"""
        self.assertEqual(thr_mult("high_vol"), 1.25)

    def test_unknown_label_default(self):
        """未知标签 → 默认 1.0"""
        self.assertEqual(thr_mult("unknown"), DEFAULT_THR_MULT)
        self.assertEqual(thr_mult(None), DEFAULT_THR_MULT)
        self.assertEqual(thr_mult(""), DEFAULT_THR_MULT)

    def test_volatility_higher_than_choppy(self):
        """high_vol 阈值 > choppy 阈值（风险控制更严）"""
        self.assertGreater(thr_mult("high_vol"), thr_mult("choppy"))

    def test_trend_lower_than_choppy(self):
        """趋势阈值 < 震荡阈值（顺势更易触发）"""
        self.assertLess(thr_mult("trend_up"), thr_mult("choppy"))
        self.assertLess(thr_mult("trend_down"), thr_mult("choppy"))


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  HMM 市场状态识别 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
