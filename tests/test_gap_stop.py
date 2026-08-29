#!/usr/bin/env python3
"""
gap_stop 缺口击穿告警 — 单元测试
================================

覆盖场景：
1. 多单：有利方向 / 不利方向 / 边界值
2. 空单：有利方向 / 不利方向 / 边界值
3. 异常输入：None / 零值 / 非法类型
4. 回归验证：修复前的假阳性场景（价格在有利方向但穿透 > 0.5R 不应触发）

对应历史 bug：gap_stop 假阳性（2026-08-28 修复）
  - 原逻辑只检查穿透距离，不检查方向
  - 修复后增加 _gap_adverse 方向检查
  - 本测试确保此 bug 不再复发
"""

import os
import sys
import unittest

# ── 路径 ──────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 从独立工具模块导入纯函数，避免加载整个 runner（导入时有全局副作用）
from gap_stop_utils import check_gap_stop_triggered as _check_gap_stop_triggered

# ═══════════════════════════════════════════════════════════════════════════
#  多单场景
# ═══════════════════════════════════════════════════════════════════════════


class TestLongPosition(unittest.TestCase):
    """多单（ds=1）场景测试。"""

    def setUp(self):
        # 典型多单：入场 26000，止损 25000 → oneR = 1000
        self.ds = 1
        self.entry = 26000.0
        self.stop = 25000.0
        self.oneR = 1000.0  # 预期 1R = 1000

    def test_oneR_calculation(self):
        """1R 计算正确：入场 26000，止损 25000 → oneR = 1000"""
        r = _check_gap_stop_triggered(self.ds, 26000, self.stop, self.entry)
        self.assertAlmostEqual(r["oneR"], self.oneR, places=2)

    def test_favorable_direction_no_trigger(self):
        """多单 + 价格在有利方向（px > stop）→ 不触发，即使穿透 > 0.5R"""
        # 价格 27000，远在止损上方（有利方向），但距离止损 2000 > 0.5R
        # 这就是之前的假阳性 bug：只看距离不看方向
        r = _check_gap_stop_triggered(self.ds, 27000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "多单价格在有利方向时不应触发 gap_stop")
        self.assertFalse(r["is_adverse"], "多单价格 > 止损应判定为非不利方向")

    def test_favorable_direction_deep_in_profit(self):
        """多单 + 大幅盈利（价格远高于止损）→ 不触发"""
        r = _check_gap_stop_triggered(self.ds, 30000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["is_adverse"])

    def test_price_at_stop_no_trigger(self):
        """多单 + 价格恰好在止损位 → pen = 0 → 不触发"""
        r = _check_gap_stop_triggered(self.ds, 25000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"])
        self.assertAlmostEqual(r["pen"], 0.0, places=2)

    def test_adverse_but_below_threshold(self):
        """多单 + 不利方向但穿透 < 0.5R → 不触发"""
        # 价格 24700，距离止损 300（0.3R）< 0.5R
        r = _check_gap_stop_triggered(self.ds, 24700.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "穿透不足 0.5R 不应触发")
        self.assertTrue(r["is_adverse"], "多单价格 < 止损应判定为不利方向")
        self.assertLess(r["pen_ratio"], 0.5)

    def test_adverse_at_threshold_boundary(self):
        """多单 + 不利方向且穿透 = 0.5R → 不触发（边界值，严格大于才触发）"""
        # 价格 24500，距离止损 500（恰好 0.5R）
        r = _check_gap_stop_triggered(self.ds, 24500.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "穿透恰好等于 0.5R 不应触发（严格大于）")
        self.assertAlmostEqual(r["pen_ratio"], 0.5, places=2)

    def test_adverse_above_threshold_triggers(self):
        """多单 + 不利方向且穿透 > 0.5R → 触发"""
        # 价格 24000，距离止损 1000（1.0R）> 0.5R
        r = _check_gap_stop_triggered(self.ds, 24000.0, self.stop, self.entry)
        self.assertTrue(r["triggered"], "多单价格跌破止损且穿透 > 0.5R 应触发")
        self.assertTrue(r["is_adverse"])
        self.assertGreater(r["pen_ratio"], 0.5)

    def test_adverse_just_above_threshold(self):
        """多单 + 不利方向且穿透略大于 0.5R → 触发"""
        # 价格 24499，距离止损 501（0.501R）> 0.5R
        r = _check_gap_stop_triggered(self.ds, 24499.0, self.stop, self.entry)
        self.assertTrue(r["triggered"], "穿透略大于 0.5R 应触发")


# ═══════════════════════════════════════════════════════════════════════════
#  空单场景
# ═══════════════════════════════════════════════════════════════════════════


class TestShortPosition(unittest.TestCase):
    """空单（ds=-1）场景测试。"""

    def setUp(self):
        # 典型空单：入场 14000，止损 15000 → oneR = 1000
        self.ds = -1
        self.entry = 14000.0
        self.stop = 15000.0
        self.oneR = 1000.0

    def test_oneR_calculation(self):
        """1R 计算正确：入场 14000，止损 15000 → oneR = 1000"""
        r = _check_gap_stop_triggered(self.ds, 14000, self.stop, self.entry)
        self.assertAlmostEqual(r["oneR"], self.oneR, places=2)

    def test_favorable_direction_no_trigger(self):
        """空单 + 价格在有利方向（px < stop）→ 不触发，即使穿透 > 0.5R"""
        # 价格 13000，远在止损下方（有利方向），但距离止损 2000 > 0.5R
        # 这也是假阳性 bug 的一种场景
        r = _check_gap_stop_triggered(self.ds, 13000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "空单价格在有利方向时不应触发 gap_stop")
        self.assertFalse(r["is_adverse"], "空单价格 < 止损应判定为非不利方向")

    def test_favorable_direction_deep_in_profit(self):
        """空单 + 大幅盈利（价格远低于止损）→ 不触发"""
        r = _check_gap_stop_triggered(self.ds, 10000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["is_adverse"])

    def test_price_at_stop_no_trigger(self):
        """空单 + 价格恰好在止损位 → pen = 0 → 不触发"""
        r = _check_gap_stop_triggered(self.ds, 15000.0, self.stop, self.entry)
        self.assertFalse(r["triggered"])
        self.assertAlmostEqual(r["pen"], 0.0, places=2)

    def test_adverse_but_below_threshold(self):
        """空单 + 不利方向但穿透 < 0.5R → 不触发"""
        # 价格 15300，距离止损 300（0.3R）< 0.5R
        r = _check_gap_stop_triggered(self.ds, 15300.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "穿透不足 0.5R 不应触发")
        self.assertTrue(r["is_adverse"], "空单价格 > 止损应判定为不利方向")
        self.assertLess(r["pen_ratio"], 0.5)

    def test_adverse_at_threshold_boundary(self):
        """空单 + 不利方向且穿透 = 0.5R → 不触发（边界值）"""
        # 价格 15500，距离止损 500（恰好 0.5R）
        r = _check_gap_stop_triggered(self.ds, 15500.0, self.stop, self.entry)
        self.assertFalse(r["triggered"], "穿透恰好等于 0.5R 不应触发")
        self.assertAlmostEqual(r["pen_ratio"], 0.5, places=2)

    def test_adverse_above_threshold_triggers(self):
        """空单 + 不利方向且穿透 > 0.5R → 触发"""
        # 价格 16000，距离止损 1000（1.0R）> 0.5R
        r = _check_gap_stop_triggered(self.ds, 16000.0, self.stop, self.entry)
        self.assertTrue(r["triggered"], "空单价格涨破止损且穿透 > 0.5R 应触发")
        self.assertTrue(r["is_adverse"])
        self.assertGreater(r["pen_ratio"], 0.5)

    def test_adverse_just_above_threshold(self):
        """空单 + 不利方向且穿透略大于 0.5R → 触发"""
        # 价格 15501，距离止损 501（0.501R）> 0.5R
        r = _check_gap_stop_triggered(self.ds, 15501.0, self.stop, self.entry)
        self.assertTrue(r["triggered"], "穿透略大于 0.5R 应触发")


# ═══════════════════════════════════════════════════════════════════════════
#  真实持仓案例回归测试
#  （来自 2026-08-28 gap_stop 假阳性修复时的真实持仓数据）
# ═══════════════════════════════════════════════════════════════════════════


class TestRealHoldingsRegression(unittest.TestCase):
    """
    真实持仓数据回归测试 —— 确保修复后的假阳性场景不再复发。

    数据来源：项目档案 01-项目摘要.md → 当前持仓（2026-08-28）
    所有持仓当时价格都在有利方向，但被错误判定为"缺口击穿"。
    """

    def test_zn_long_favorable(self):
        """沪锌多单：入场 26020，止损 25663.21，现价 26285（有利方向）→ 不触发"""
        r = _check_gap_stop_triggered(1, 26285.0, 25663.21, 26020.0)
        self.assertFalse(r["triggered"], "沪锌多单价格在有利方向，不应触发 gap_stop")
        self.assertFalse(r["is_adverse"])

    def test_ss_short_favorable(self):
        """不锈钢空单：入场 14150，止损 14453.75，现价 13995（有利方向）→ 不触发"""
        r = _check_gap_stop_triggered(-1, 13995.0, 14453.75, 14150.0)
        self.assertFalse(r["triggered"], "不锈钢空单价格在有利方向，不应触发 gap_stop")
        self.assertFalse(r["is_adverse"])

    def test_fu_short_favorable(self):
        """燃油空单：入场 3740，止损 3798，现价 3729（有利方向）→ 不触发"""
        r = _check_gap_stop_triggered(-1, 3729.0, 3798.0, 3740.0)
        self.assertFalse(r["triggered"], "燃油空单价格在有利方向，不应触发 gap_stop")
        self.assertFalse(r["is_adverse"])

    def test_J_long_favorable(self):
        """焦炭多单：入场 2119.5，止损 2087，现价 2140.5（有利方向）→ 不触发"""
        r = _check_gap_stop_triggered(1, 2140.5, 2087.0, 2119.5)
        self.assertFalse(r["triggered"], "焦炭多单价格在有利方向，不应触发 gap_stop")
        self.assertFalse(r["is_adverse"])

    def test_all_7_holdings_favorable(self):
        """全部 7 个持仓均在有利方向 → 全部不触发"""
        holdings = [
            # (ds, px, stop, entry, name)
            (1, 26285.0, 25663.21, 26020.0, "zn 沪锌 多"),
            (-1, 13995.0, 14453.75, 14150.0, "ss 不锈钢 空"),
            (-1, 3729.0, 3798.0, 3740.0, "fu 燃油 空"),
            (1, 4784.0, 4780.0, 4830.0, "sp 纸浆 多"),
            (1, 2140.5, 2087.0, 2119.5, "J 焦炭 多"),
            (1, 8631.0, 8592.0, 8732.3, "eb 苯乙烯 多"),
            (-1, 5800.0, 5920.0, 5833.0, "pg 液化气 空"),
        ]
        for ds, px, stop, entry, name in holdings:
            r = _check_gap_stop_triggered(ds, px, stop, entry)
            self.assertFalse(r["triggered"], f"{name}: 价格在有利方向但被误判为缺口击穿 → 假阳性 bug 复发！")
            self.assertFalse(r["is_adverse"], f"{name}: 方向判定错误")


# ═══════════════════════════════════════════════════════════════════════════
#  边界 & 异常输入
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases(unittest.TestCase):
    """边界情况和异常输入测试。"""

    def test_direction_zero_no_trigger(self):
        """方向未知（ds=0）→ 不触发"""
        r = _check_gap_stop_triggered(0, 100.0, 90.0, 100.0)
        self.assertFalse(r["triggered"])
        self.assertFalse(r["is_adverse"])

    def test_stop_none_no_trigger(self):
        """止损价为 None → 不触发"""
        r = _check_gap_stop_triggered(1, 100.0, None, 100.0)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["oneR"], 0.0)
        self.assertEqual(r["pen"], 0.0)

    def test_entry_none_no_trigger(self):
        """入场价为 None → 不触发"""
        r = _check_gap_stop_triggered(1, 100.0, 90.0, None)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["oneR"], 0.0)

    def test_zero_oneR_no_trigger(self):
        """入场价等于止损价（oneR = 0）→ 不触发（除零保护）"""
        r = _check_gap_stop_triggered(1, 100.0, 100.0, 100.0)
        self.assertFalse(r["triggered"])
        self.assertAlmostEqual(r["oneR"], 0.0, places=2)

    def test_negative_oneR_no_trigger(self):
        """oneR 为负（abs 后为正，但方向逻辑应独立）→ 验证绝对值处理"""
        # 多单：入场 < 止损 → 这是异常配置，但 oneR 用 abs 计算
        # 此时价格低于止损仍应判定为不利方向
        r = _check_gap_stop_triggered(1, 90.0, 100.0, 95.0)
        self.assertTrue(r["is_adverse"], "多单价格 < 止损，无论止损在哪一侧，都算不利方向")
        # oneR = |95 - 100| = 5
        # pen = |90 - 100| = 10
        # pen_ratio = 2.0 > 0.5 → triggered
        self.assertTrue(r["triggered"])

    def test_string_price_no_crash(self):
        """价格为字符串 → 类型转换失败，不触发，不崩溃"""
        r = _check_gap_stop_triggered(1, "abc", 90.0, 100.0)
        self.assertFalse(r["triggered"])

    def test_string_stop_no_crash(self):
        """止损价为字符串 → 类型转换失败，不触发，不崩溃"""
        r = _check_gap_stop_triggered(1, 100.0, "xyz", 100.0)
        self.assertFalse(r["triggered"])

    def test_empty_string_no_crash(self):
        """空字符串输入 → 不崩溃"""
        r = _check_gap_stop_triggered(1, "", 90.0, 100.0)
        self.assertFalse(r["triggered"])

    def test_integer_inputs(self):
        """整数输入 → 正常工作"""
        r = _check_gap_stop_triggered(1, 90, 95, 100)
        self.assertTrue(r["triggered"])
        self.assertTrue(isinstance(r["oneR"], float))
        self.assertTrue(isinstance(r["pen"], float))


# ═══════════════════════════════════════════════════════════════════════════
#  pen_ratio 计算验证
# ═══════════════════════════════════════════════════════════════════════════


class TestPenRatio(unittest.TestCase):
    """pen_ratio（穿透比例）计算准确性测试。"""

    def test_ratio_at_1R(self):
        """穿透 1R → pen_ratio = 1.0"""
        r = _check_gap_stop_triggered(1, 900.0, 950.0, 1000.0)
        self.assertAlmostEqual(r["pen_ratio"], 1.0, places=3)

    def test_ratio_at_half_R(self):
        """穿透 0.5R → pen_ratio = 0.5"""
        r = _check_gap_stop_triggered(1, 925.0, 950.0, 1000.0)
        self.assertAlmostEqual(r["pen_ratio"], 0.5, places=3)

    def test_ratio_at_2R(self):
        """穿透 2R → pen_ratio = 2.0"""
        r = _check_gap_stop_triggered(1, 850.0, 950.0, 1000.0)
        self.assertAlmostEqual(r["pen_ratio"], 2.0, places=3)

    def test_ratio_zero_when_zero_oneR(self):
        """oneR = 0 时 pen_ratio = 0（除零保护）"""
        r = _check_gap_stop_triggered(1, 100.0, 100.0, 100.0)
        self.assertEqual(r["pen_ratio"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
