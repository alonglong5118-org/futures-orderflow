#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OOS 验证工具 — 单元测试
===========================

1. _deep_merge — 递归深度合并 dict
   - 空 patch → 返回 base 副本
   - 空 base → 返回 patch 副本
   - 单层键覆盖
   - 嵌套 dict 递归合并
   - 非 dict 值直接覆盖
   - 新增键
   - 不修改原 base（返回副本）
   - 不修改原 patch
   - 深拷贝：修改返回值不影响原数据
   - 多层嵌套

2. split_is_oos — 时间序列 IS/OOS 切分
   - 默认 60/40 切分 + 20 根 embargo
   - 自定义比例
   - 自定义 embargo
   - embargo = 0 → 直接切，无间隙
   - IS 在前，OOS 在后
   - IS 和 OOS 不重叠（中间有 embargo 间隙）
   - 数据不足 → 可能有空 DataFrame
   - 行顺序保持不变
   - embargo_bars 过大时 IS 可能为空
   - 返回两个 DataFrame

3. _metric — 从回测结果提取 (expR, trades)
   - 正常提取
   - 缺 expR → 默认 0.0
   - 缺 trades → 默认 0
   - 都缺 → (0.0, 0)
   - expR 转 float
   - trades 转 int
   - 字符串数字也能转
"""

import os
import sys
import unittest

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from oos_weight_validation import _deep_merge, _metric, split_is_oos

# ═══════════════════════════════════════════════════════════════════════════
#  1. _deep_merge
# ═══════════════════════════════════════════════════════════════════════════

class TestDeepMerge(unittest.TestCase):
    """_deep_merge 递归深度合并 dict。"""

    def test_empty_patch_returns_base_copy(self):
        """空 patch → 返回 base 副本"""
        base = {"a": 1, "b": 2}
        result = _deep_merge(base, {})
        self.assertEqual(result, base)
        # 是副本，不是同一个对象
        result["a"] = 999
        self.assertEqual(base["a"], 1)

    def test_empty_base_returns_patch_copy(self):
        """空 base → 返回 patch 副本"""
        patch = {"a": 1, "b": 2}
        result = _deep_merge({}, patch)
        self.assertEqual(result, patch)

    def test_single_level_override(self):
        """单层键覆盖"""
        base = {"a": 1, "b": 2}
        patch = {"b": 3, "c": 4}
        result = _deep_merge(base, patch)
        self.assertEqual(result["a"], 1)  # 保留
        self.assertEqual(result["b"], 3)  # 覆盖
        self.assertEqual(result["c"], 4)  # 新增

    def test_nested_dict_recursive_merge(self):
        """嵌套 dict 递归合并"""
        base = {"outer": {"a": 1, "b": 2}}
        patch = {"outer": {"b": 3, "c": 4}}
        result = _deep_merge(base, patch)
        self.assertEqual(result["outer"]["a"], 1)  # 保留
        self.assertEqual(result["outer"]["b"], 3)  # 覆盖
        self.assertEqual(result["outer"]["c"], 4)  # 新增

    def test_non_dict_overrides_dict(self):
        """patch 值非 dict → 直接覆盖 base 的 dict"""
        base = {"key": {"a": 1, "b": 2}}
        patch = {"key": "scalar"}
        result = _deep_merge(base, patch)
        self.assertEqual(result["key"], "scalar")

    def test_new_keys_added(self):
        """新增键"""
        base = {"a": 1}
        patch = {"b": 2, "c": 3}
        result = _deep_merge(base, patch)
        self.assertEqual(result["a"], 1)
        self.assertEqual(result["b"], 2)
        self.assertEqual(result["c"], 3)

    def test_does_not_mutate_original_base(self):
        """不修改原 base"""
        base = {"a": 1, "nested": {"x": 10}}
        patch = {"a": 2, "nested": {"x": 20, "y": 30}}
        base_copy = {"a": 1, "nested": {"x": 10}}
        _deep_merge(base, patch)
        self.assertEqual(base, base_copy)

    def test_does_not_mutate_original_patch(self):
        """不修改原 patch"""
        base = {"a": 1}
        patch = {"b": {"x": 10}}
        patch_copy = {"b": {"x": 10}}
        result = _deep_merge(base, patch)
        # 修改结果中的嵌套 dict
        result["b"]["x"] = 999
        self.assertEqual(patch, patch_copy)

    def test_deep_copy_independent(self):
        """深拷贝：修改返回值不影响原数据"""
        base = {"nested": {"inner": [1, 2, 3]}}
        result = _deep_merge(base, {})
        result["nested"]["inner"].append(4)
        self.assertEqual(base["nested"]["inner"], [1, 2, 3])

    def test_multi_level_nesting(self):
        """多层嵌套"""
        base = {"l1": {"l2": {"l3": {"val": 1}}}}
        patch = {"l1": {"l2": {"l3": {"val": 2, "new": 3}}}}
        result = _deep_merge(base, patch)
        self.assertEqual(result["l1"]["l2"]["l3"]["val"], 2)
        self.assertEqual(result["l1"]["l2"]["l3"]["new"], 3)


# ═══════════════════════════════════════════════════════════════════════════
#  2. split_is_oos
# ═══════════════════════════════════════════════════════════════════════════

class TestSplitIsOos(unittest.TestCase):
    """split_is_oos 时间序列 IS/OOS 切分。"""

    def _make_df(self, n):
        return pd.DataFrame({"close": range(n), "volume": range(100, 100 + n)})

    def test_default_split_ratio(self):
        """默认 60/40 切分 + 20 根 embargo"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df)
        # cut = int(100 * 0.6) = 60
        # IS = [0 : 60-20] = [0 : 40] → 40 行
        # OOS = [60+20 : ] = [80 : ] → 20 行
        self.assertEqual(len(is_df), 40)
        self.assertEqual(len(oos_df), 20)

    def test_custom_ratio(self):
        """自定义比例"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df, is_ratio=0.8, embargo_bars=10)
        # cut = int(100 * 0.8) = 80
        # IS = [0 : 80-10] = [0 : 70] → 70 行
        # OOS = [80+10 : ] = [90 : ] → 10 行
        self.assertEqual(len(is_df), 70)
        self.assertEqual(len(oos_df), 10)

    def test_zero_embargo_no_gap(self):
        """embargo = 0 → 直接切，无间隙"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df, is_ratio=0.6, embargo_bars=0)
        self.assertEqual(len(is_df), 60)
        self.assertEqual(len(oos_df), 40)
        # IS 最后一行和 OOS 第一行相邻
        self.assertEqual(is_df.iloc[-1]["close"] + 1, oos_df.iloc[0]["close"])

    def test_is_before_oos(self):
        """IS 在前，OOS 在后"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df)
        # IS 的最后一个 close < OOS 的第一个 close
        self.assertLess(is_df.iloc[-1]["close"], oos_df.iloc[0]["close"])

    def test_no_overlap_with_embargo(self):
        """IS 和 OOS 不重叠（中间有 embargo 间隙）"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df, is_ratio=0.6, embargo_bars=20)
        # IS 最后索引 + 1 应该 < OOS 起始索引（有间隙）
        is_last_idx = is_df.index[-1]
        oos_first_idx = oos_df.index[0]
        self.assertGreater(oos_first_idx - is_last_idx, 1)  # 至少差 2（有间隙）

    def test_returns_dataframes(self):
        """返回两个 DataFrame"""
        df = self._make_df(100)
        is_df, oos_df = split_is_oos(df)
        self.assertIsInstance(is_df, pd.DataFrame)
        self.assertIsInstance(oos_df, pd.DataFrame)

    def test_preserves_order(self):
        """行顺序保持不变"""
        df = self._make_df(50)
        is_df, oos_df = split_is_oos(df, is_ratio=0.6, embargo_bars=5)
        # IS 的 close 应该是连续递增
        self.assertTrue((is_df["close"].diff().dropna() > 0).all())
        self.assertTrue((oos_df["close"].diff().dropna() > 0).all())

    def test_small_data_may_be_empty(self):
        """数据很少时 OOS 可能为空"""
        df = self._make_df(30)
        is_df, oos_df = split_is_oos(df, is_ratio=0.6, embargo_bars=20)
        # cut = 18, OOS 从 18+20=38 开始，只有 30 行 → 空
        self.assertEqual(len(oos_df), 0)

    def test_large_embargo_is_not_empty(self):
        """embargo > cut 时，IS = df[:负数] → 相当于去掉最后 N 行，不为空"""
        df = self._make_df(30)
        is_df, oos_df = split_is_oos(df, is_ratio=0.5, embargo_bars=20)
        # cut = 15, IS = df[:15-20] = df[:-5] → 25 行（去掉最后 5 行）
        # 这是 pandas iloc 的行为：[:负数] 表示到倒数第 N 个之前
        self.assertEqual(len(is_df), 25)
        self.assertEqual(len(oos_df), 0)  # OOS 从 35 开始，超出范围 → 空

    def test_very_large_data(self):
        """大数据量也正确切分"""
        df = self._make_df(10000)
        is_df, oos_df = split_is_oos(df, is_ratio=0.7, embargo_bars=50)
        # cut = int(10000 * 0.7) = 7000
        # IS = 7000 - 50 = 6950
        # OOS = 10000 - 7050 = 2950
        self.assertEqual(len(is_df), 6950)
        self.assertEqual(len(oos_df), 2950)


# ═══════════════════════════════════════════════════════════════════════════
#  3. _metric
# ═══════════════════════════════════════════════════════════════════════════

class TestMetric(unittest.TestCase):
    """_metric 从回测结果提取 (expR, trades)。"""

    def test_normal_extraction(self):
        """正常提取"""
        r = {"expR": 0.85, "trades": 120}
        expR, trades = _metric(r)
        self.assertAlmostEqual(expR, 0.85, places=6)
        self.assertEqual(trades, 120)

    def test_missing_expR_default_zero(self):
        """缺 expR → 默认 0.0"""
        r = {"trades": 50}
        expR, trades = _metric(r)
        self.assertEqual(expR, 0.0)
        self.assertEqual(trades, 50)

    def test_missing_trades_default_zero(self):
        """缺 trades → 默认 0"""
        r = {"expR": 0.5}
        expR, trades = _metric(r)
        self.assertAlmostEqual(expR, 0.5, places=6)
        self.assertEqual(trades, 0)

    def test_both_missing_zero(self):
        """都缺 → (0.0, 0)"""
        expR, trades = _metric({})
        self.assertEqual(expR, 0.0)
        self.assertEqual(trades, 0)

    def test_expR_converted_to_float(self):
        """expR 转 float"""
        r = {"expR": "0.75", "trades": 100}
        expR, trades = _metric(r)
        self.assertIsInstance(expR, float)
        self.assertAlmostEqual(expR, 0.75, places=6)

    def test_trades_converted_to_int(self):
        """trades 转 int"""
        r = {"expR": 0.5, "trades": "50"}
        expR, trades = _metric(r)
        self.assertIsInstance(trades, int)
        self.assertEqual(trades, 50)

    def test_string_numbers_convert(self):
        """字符串数字也能转"""
        r = {"expR": "1.25", "trades": "200"}
        expR, trades = _metric(r)
        self.assertAlmostEqual(expR, 1.25, places=6)
        self.assertEqual(trades, 200)

    def test_negative_expR(self):
        """负 expR 正常转换"""
        r = {"expR": -0.3, "trades": 80}
        expR, trades = _metric(r)
        self.assertAlmostEqual(expR, -0.3, places=6)
        self.assertEqual(trades, 80)

    def test_zero_values(self):
        """零值"""
        r = {"expR": 0.0, "trades": 0}
        expR, trades = _metric(r)
        self.assertEqual(expR, 0.0)
        self.assertEqual(trades, 0)

    def test_extra_keys_ignored(self):
        """额外的 key 不影响"""
        r = {"expR": 0.8, "trades": 100, "win_rate": 0.55, "sharpe": 1.2}
        expR, trades = _metric(r)
        self.assertAlmostEqual(expR, 0.8, places=6)
        self.assertEqual(trades, 100)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  OOS 验证工具 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
