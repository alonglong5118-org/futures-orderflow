#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
季节性 + 其他收尾纯函数 — 单元测试
==============================================

1. seasonal_f — 品种季节性打分
   - 鸡蛋 7-9月 → +35（中秋备货）
   - 鸡蛋 10-11月 → -20（节后偏弱）
   - 鸡蛋 其他月 → 0
   - 生猪 11-12-1月 → +30（腌腊/春节前）
   - 生猪 3-4月 → -15（节后淡季）
   - 生猪 其他月 → 0
   - 未知品种 → 0
   - 非法日期 → 0
   - 返回 float

2. _today_str — 今日日期字符串
   - 格式 YYYYMMDD
   - 8 位数字
   - 返回 str

3. _safe_load — 安全加载 JSON
   - 文件不存在 → (None, "文件不存在")
   - 正常 JSON → (dict, None)
   - 损坏 JSON → (None, 错误信息)
   - 返回二元组

4. _is_rate_limit 已经在另一个文件测了，此处不重复
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fundamental_feed import _today_str, seasonal_f
from self_check import _safe_load

# ═══════════════════════════════════════════════════════════════════════════
#  1. seasonal_f
# ═══════════════════════════════════════════════════════════════════════════

class TestSeasonalF(unittest.TestCase):
    """seasonal_f 品种季节性打分。"""

    def test_egg_july_positive(self):
        """鸡蛋 7月 → +35（中秋备货）"""
        self.assertEqual(seasonal_f("jd", "2026-07-15"), 35)

    def test_egg_august_positive(self):
        """鸡蛋 8月 → +35"""
        self.assertEqual(seasonal_f("jd", "2026-08-15"), 35)

    def test_egg_september_positive(self):
        """鸡蛋 9月 → +35"""
        self.assertEqual(seasonal_f("jd", "2026-09-15"), 35)

    def test_egg_october_negative(self):
        """鸡蛋 10月 → -20（节后偏弱）"""
        self.assertEqual(seasonal_f("jd", "2026-10-15"), -20)

    def test_egg_november_negative(self):
        """鸡蛋 11月 → -20"""
        self.assertEqual(seasonal_f("jd", "2026-11-15"), -20)

    def test_egg_other_months_zero(self):
        """鸡蛋 其他月 → 0"""
        self.assertEqual(seasonal_f("jd", "2026-01-15"), 0.0)
        self.assertEqual(seasonal_f("jd", "2026-06-15"), 0.0)
        self.assertEqual(seasonal_f("jd", "2026-12-15"), 0.0)

    def test_hog_november_positive(self):
        """生猪 11月 → +30（腌腊）"""
        self.assertEqual(seasonal_f("lh", "2026-11-15"), 30)

    def test_hog_december_positive(self):
        """生猪 12月 → +30"""
        self.assertEqual(seasonal_f("lh", "2026-12-15"), 30)

    def test_hog_january_positive(self):
        """生猪 1月 → +30（春节前）"""
        self.assertEqual(seasonal_f("lh", "2026-01-15"), 30)

    def test_hog_march_negative(self):
        """生猪 3月 → -15（节后淡季）"""
        self.assertEqual(seasonal_f("lh", "2026-03-15"), -15)

    def test_hog_april_negative(self):
        """生猪 4月 → -15"""
        self.assertEqual(seasonal_f("lh", "2026-04-15"), -15)

    def test_hog_other_months_zero(self):
        """生猪 其他月 → 0"""
        self.assertEqual(seasonal_f("lh", "2026-05-15"), 0.0)
        self.assertEqual(seasonal_f("lh", "2026-07-15"), 0.0)
        self.assertEqual(seasonal_f("lh", "2026-09-15"), 0.0)

    def test_unknown_symbol_zero(self):
        """未知品种 → 0"""
        self.assertEqual(seasonal_f("rb", "2026-08-15"), 0.0)
        self.assertEqual(seasonal_f("FG", "2026-08-15"), 0.0)
        self.assertEqual(seasonal_f("XYZ", "2026-08-15"), 0.0)

    def test_invalid_date_zero(self):
        """非法日期 → 0"""
        self.assertEqual(seasonal_f("jd", "not-a-date"), 0.0)
        self.assertEqual(seasonal_f("jd", ""), 0.0)

    def test_returns_float_or_int(self):
        """返回数值型"""
        self.assertIsInstance(seasonal_f("jd", "2026-08-15"), (int, float))
        self.assertIsInstance(seasonal_f("rb", "2026-08-15"), float)

    def test_boundary_july(self):
        """7月1日 → 正值"""
        self.assertEqual(seasonal_f("jd", "2026-07-01"), 35)

    def test_boundary_september(self):
        """9月30日 → 正值"""
        self.assertEqual(seasonal_f("jd", "2026-09-30"), 35)

    def test_boundary_october(self):
        """10月1日 → 负值"""
        self.assertEqual(seasonal_f("jd", "2026-10-01"), -20)

    def test_boundary_december(self):
        """12月31日 → 正值（生猪）"""
        self.assertEqual(seasonal_f("lh", "2026-12-31"), 30)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _today_str
# ═══════════════════════════════════════════════════════════════════════════

class TestTodayStr(unittest.TestCase):
    """_today_str 今日日期字符串。"""

    def test_format_yyyymmdd(self):
        """格式 YYYYMMDD，8 位数字"""
        s = _today_str()
        self.assertEqual(len(s), 8)
        self.assertTrue(s.isdigit())

    def test_returns_string(self):
        """返回 str"""
        self.assertIsInstance(_today_str(), str)

    def test_starts_with_20(self):
        """以 20 开头（21 世纪）"""
        self.assertTrue(_today_str().startswith("20"))


# ═══════════════════════════════════════════════════════════════════════════
#  3. _safe_load
# ═══════════════════════════════════════════════════════════════════════════

class TestSafeLoad(unittest.TestCase):
    """_safe_load 安全加载 JSON。"""

    def test_file_not_found(self):
        """文件不存在 → (None, "文件不存在")"""
        data, err = _safe_load("/nonexistent/path/file.json")
        self.assertIsNone(data)
        self.assertIsNotNone(err)

    def test_valid_json(self):
        """正常 JSON → (dict, None)"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump({"key": "value", "num": 42}, f)
            path = f.name
        try:
            data, err = _safe_load(path)
            self.assertIsNotNone(data)
            self.assertEqual(data["key"], "value")
            self.assertEqual(data["num"], 42)
            self.assertIsNone(err)
        finally:
            os.unlink(path)

    def test_corrupted_json(self):
        """损坏 JSON → (None, 错误信息)"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write("{not valid json}")
            path = f.name
        try:
            data, err = _safe_load(path)
            self.assertIsNone(data)
            self.assertIsNotNone(err)
        finally:
            os.unlink(path)

    def test_returns_tuple(self):
        """返回 (data, error) 二元组"""
        result = _safe_load("/nonexistent")
        self.assertEqual(len(result), 2)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  季节性 + 收尾纯函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
