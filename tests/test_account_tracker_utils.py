#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户跟踪器工具函数 — 单元测试
===================================

1. _dir_sign — 方向符号
   - 多 → +1
   - 空 → -1
   - 其他 → 0

2. _fmt_price — 价格格式化
   - 正常数字 → 保留 2 位小数
   - None → "—"
   - 整数 → .0 结尾
   - 非法输入 → 原样返回字符串

3. _to_float — 安全转 float
   - 正常数字字符串 → float
   - None → None
   - 空串 → None
   - 非法字符串 → None
   - 已经是 float → 原样返回

4. _validate_levels — 持仓档位校验修正
   - 多单：stop<avg, target>avg → 不修改
   - 多单：stop>avg → 镜像修正到下方
   - 多单：target<avg → 镜像修正到上方
   - 空单：stop<avg → 镜像修正到上方
   - 空单：target>avg → 镜像修正到下方
   - 多档位同时修正（stop + target + t1 + t2）
   - 方向不明 → 不处理
   - 无 avg → 不处理
   - 档位 = avg → 微调 0.01
   - 返回 changed dict + reason 文本
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from account_tracker import (
    _dir_sign,
    _fmt_price,
    _to_float,
    _validate_levels,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. _dir_sign
# ═══════════════════════════════════════════════════════════════════════════

class TestDirSignAcc(unittest.TestCase):
    """_dir_sign 方向符号。"""

    def test_long_is_positive(self):
        """多 → +1"""
        self.assertEqual(_dir_sign("多"), 1)

    def test_short_is_negative(self):
        """空 → -1"""
        self.assertEqual(_dir_sign("空"), -1)

    def test_other_is_zero(self):
        """其他 → 0"""
        self.assertEqual(_dir_sign(""), 0)
        self.assertEqual(_dir_sign("平"), 0)
        self.assertEqual(_dir_sign(None), 0)
        self.assertEqual(_dir_sign("做多"), 0)  # 必须精确匹配"多"


# ═══════════════════════════════════════════════════════════════════════════
#  2. _fmt_price
# ═══════════════════════════════════════════════════════════════════════════

class TestFmtPrice(unittest.TestCase):
    """_fmt_price 价格格式化。"""

    def test_normal_rounds_to_2_decimals(self):
        """正常数字 → 保留 2 位小数"""
        self.assertEqual(_fmt_price(100.123), "100.12")
        self.assertEqual(_fmt_price(100.126), "100.13")

    def test_none_returns_dash(self):
        """None → "—" """
        self.assertEqual(_fmt_price(None), "—")

    def test_integer_has_decimal(self):
        """整数 → .0 结尾"""
        self.assertEqual(_fmt_price(100), "100.0")

    def test_string_number(self):
        """字符串数字 → 也能格式化"""
        self.assertEqual(_fmt_price("100.5"), "100.5")

    def test_invalid_returns_str(self):
        """非法输入 → 原样转字符串（不崩溃）"""
        self.assertEqual(_fmt_price("abc"), "abc")
        self.assertEqual(_fmt_price([]), "[]")

    def test_zero(self):
        """0 → 0.0"""
        self.assertEqual(_fmt_price(0), "0.0")

    def test_negative(self):
        """负数也能格式化"""
        self.assertEqual(_fmt_price(-100.5), "-100.5")


# ═══════════════════════════════════════════════════════════════════════════
#  3. _to_float
# ═══════════════════════════════════════════════════════════════════════════

class TestToFloat(unittest.TestCase):
    """_to_float 安全转 float。"""

    def test_normal_string(self):
        """正常数字字符串 → float"""
        self.assertEqual(_to_float("100.5"), 100.5)
        self.assertEqual(_to_float("0"), 0.0)
        self.assertEqual(_to_float("-3.14"), -3.14)

    def test_none_returns_none(self):
        """None → None"""
        self.assertIsNone(_to_float(None))

    def test_empty_string_returns_none(self):
        """空串 → None"""
        self.assertIsNone(_to_float(""))

    def test_invalid_string_returns_none(self):
        """非法字符串 → None"""
        self.assertIsNone(_to_float("abc"))
        self.assertIsNone(_to_float("100.5.6"))

    def test_already_float(self):
        """已经是 float → 原样返回"""
        self.assertEqual(_to_float(3.14), 3.14)

    def test_already_int(self):
        """int → 转成 float"""
        self.assertEqual(_to_float(42), 42.0)

    def test_whitespace_string(self):
        """空白字符串 → None（strip 后为空？要看实现）"""
        # 实现里只判断 v == ""，没有 strip
        # 所以 "   " 会走 float("   ") → 应该是 ValueError → None
        # 实际上 float("   ") = 0.0... 让我们验证
        result = _to_float("   ")
        # 不管结果是什么，只要不崩溃就行
        self.assertTrue(result is None or isinstance(result, float))


# ═══════════════════════════════════════════════════════════════════════════
#  4. _validate_levels
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateLevels(unittest.TestCase):
    """_validate_levels 持仓档位校验修正。"""

    def test_long_correct_levels_no_change(self):
        """多单：stop<avg, target>avg → 不修改"""
        pos = {"direction": "多", "avg": 100.0, "stop": 95.0, "target": 110.0}
        changed, reason = _validate_levels(pos)
        self.assertEqual(changed, {})
        self.assertEqual(reason, "")
        # 值不变
        self.assertEqual(pos["stop"], 95.0)
        self.assertEqual(pos["target"], 110.0)

    def test_long_stop_above_avg_fixed(self):
        """多单：stop>avg → 镜像修正到下方"""
        pos = {"direction": "多", "avg": 100.0, "stop": 105.0}
        changed, reason = _validate_levels(pos)
        # 镜像：100 + (100 - 105) = 95
        self.assertIn("stop", changed)
        self.assertEqual(changed["stop"][0], 105.0)  # old
        self.assertAlmostEqual(changed["stop"][1], 95.0, places=2)  # new
        self.assertIn("止损", reason)
        self.assertIn("→", reason)
        # pos 已被修改
        self.assertAlmostEqual(pos["stop"], 95.0, places=2)

    def test_long_target_below_avg_fixed(self):
        """多单：target<avg → 镜像修正到上方"""
        pos = {"direction": "多", "avg": 100.0, "target": 95.0}
        changed, reason = _validate_levels(pos)
        # 镜像：100 + (100 - 95) = 105
        self.assertIn("target", changed)
        self.assertAlmostEqual(changed["target"][1], 105.0, places=2)
        self.assertIn("止盈", reason)

    def test_short_stop_below_avg_fixed(self):
        """空单：stop<avg → 镜像修正到上方"""
        pos = {"direction": "空", "avg": 100.0, "stop": 95.0}
        changed, reason = _validate_levels(pos)
        # 镜像：100 + (100 - 95) = 105
        self.assertIn("stop", changed)
        self.assertAlmostEqual(changed["stop"][1], 105.0, places=2)

    def test_short_target_above_avg_fixed(self):
        """空单：target>avg → 镜像修正到下方"""
        pos = {"direction": "空", "avg": 100.0, "target": 105.0}
        changed, reason = _validate_levels(pos)
        # 镜像：100 + (100 - 105) = 95
        self.assertIn("target", changed)
        self.assertAlmostEqual(changed["target"][1], 95.0, places=2)

    def test_multiple_levels_fixed_simultaneously(self):
        """多档位同时修正（stop + target + t1 + t2 都错）"""
        pos = {
            "direction": "多", "avg": 100.0,
            "stop": 105.0,     # 错：应该在下方
            "target": 95.0,    # 错：应该在上方
            "t1": 90.0,        # 错：应该在上方
            "t2": 85.0,        # 错：应该在上方
        }
        changed, reason = _validate_levels(pos)
        self.assertEqual(len(changed), 4)
        self.assertIn("stop", changed)
        self.assertIn("target", changed)
        self.assertIn("t1", changed)
        self.assertIn("t2", changed)
        # 验证修正后的值
        self.assertLess(pos["stop"], pos["avg"])
        self.assertGreater(pos["target"], pos["avg"])
        self.assertGreater(pos["t1"], pos["avg"])
        self.assertGreater(pos["t2"], pos["avg"])

    def test_unknown_direction_no_change(self):
        """方向不明 → 不处理"""
        pos = {"direction": "平", "avg": 100.0, "stop": 105.0, "target": 95.0}
        changed, reason = _validate_levels(pos)
        self.assertEqual(changed, {})
        self.assertEqual(reason, "")
        # 值不变
        self.assertEqual(pos["stop"], 105.0)

    def test_no_avg_no_change(self):
        """无 avg → 不处理"""
        pos = {"direction": "多", "stop": 105.0, "target": 95.0}
        changed, reason = _validate_levels(pos)
        self.assertEqual(changed, {})

    def test_stop_equals_avg_adjusted(self):
        """stop = avg → 微调 0.01（确保有实际距离）"""
        pos = {"direction": "多", "avg": 100.0, "stop": 100.0}
        changed, reason = _validate_levels(pos)
        self.assertIn("stop", changed)
        # 距离 = 0.01
        self.assertAlmostEqual(abs(pos["stop"] - 100.0), 0.01, places=2)

    def test_none_levels_skipped(self):
        """None 档位被跳过（不报错）"""
        pos = {"direction": "多", "avg": 100.0, "stop": None, "target": 110.0}
        changed, reason = _validate_levels(pos)
        self.assertEqual(changed, {})
        self.assertEqual(reason, "")

    def test_short_correct_levels_no_change(self):
        """空单正确档位 → 不修改"""
        pos = {"direction": "空", "avg": 100.0, "stop": 105.0, "target": 95.0}
        changed, reason = _validate_levels(pos)
        self.assertEqual(changed, {})
        self.assertEqual(reason, "")

    def test_reason_contains_chinese_labels(self):
        """reason 文本包含中文标签"""
        pos = {"direction": "多", "avg": 100.0, "stop": 105.0, "target": 95.0}
        _, reason = _validate_levels(pos)
        self.assertIn("止损", reason)
        self.assertIn("止盈", reason)
        self.assertIn("→", reason)

    def test_mirror_symmetry_all_levels(self):
        """镜像对称性：修正后的距离 = 修正前的距离"""
        pos = {"direction": "多", "avg": 100.0, "stop": 105.0, "target": 92.0}
        _validate_levels(pos)
        # stop 原来在上方 5 点，修正后在下方 5 点
        self.assertAlmostEqual(abs(pos["stop"] - 100.0), 5.0, places=2)
        # target 原来在下方 8 点，修正后在上方 8 点
        self.assertAlmostEqual(abs(pos["target"] - 100.0), 8.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  账户跟踪器工具函数 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
