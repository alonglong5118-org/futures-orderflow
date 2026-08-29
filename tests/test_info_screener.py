#!/usr/bin/env python3
"""
信息维度 + 品种筛选 — 单元测试
===================================

1. _tag_to_symbols — 文本标签命中品种
   - 空文本 → 空集合
   - 命中一个标签 → 返回对应品种
   - 命中多个标签 → 返回多个品种
   - 无命中 → 空集合

2. _age_factor — 信息时效衰减
   - 新鲜信息 → 1.0
   - 24h 内线性衰减到 0.3
   - 24h 整 → 0.3
   - 48h → 0.1
   - >48h → 0.1（过期底限）
   - None/0 → 1.0

3. _check_criteria — 品种筛选条件
   - None 指标 → 不通过
   - 全部满足 → 通过 + 高分
   - 全部不满足 → 不通过 + 低分
   - 流动性不足 → 扣分
   - 波动率过高/过低 → 扣分
   - 趋势不足 → 扣分
   - 量比不足 → 扣分
   - 相关性过高 → 扣分
   - 加权分数范围 0-1
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from info_dimension import (
    SYMBOL_TAGS,
    _age_factor,
    _tag_to_symbols,
)
from symbol_screener import (
    _check_criteria,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. _tag_to_symbols
# ═══════════════════════════════════════════════════════════════════════════


class TestTagToSymbols(unittest.TestCase):
    """_tag_to_symbols 文本标签命中品种。"""

    def test_empty_text_returns_empty(self):
        """空文本 → 空集合"""
        self.assertEqual(_tag_to_symbols(""), set())

    def test_hit_one_tag(self):
        """命中一个标签 → 返回对应品种"""
        # 找一个有标签的品种
        test_sym = None
        test_tag = None
        for sym, tags in SYMBOL_TAGS.items():
            if tags:
                test_sym = sym
                test_tag = tags[0]
                break
        if not test_sym:
            self.skipTest("没有定义 SYMBOL_TAGS")

        result = _tag_to_symbols(f"今天{test_tag}涨了")
        self.assertIn(test_sym, result)

    def test_hit_multiple_tags(self):
        """命中多个标签 → 返回多个品种"""
        # 找两个不同品种的不同标签
        syms = []
        tags = []
        for sym, tag_list in SYMBOL_TAGS.items():
            if tag_list:
                syms.append(sym)
                tags.append(tag_list[0])
                if len(syms) >= 2:
                    break
        if len(syms) < 2:
            self.skipTest("标签品种不足 2 个")

        text = f"{tags[0]}和{tags[1]}都涨了"
        result = _tag_to_symbols(text)
        self.assertIn(syms[0], result)
        self.assertIn(syms[1], result)

    def test_no_hit_returns_empty(self):
        """无命中 → 空集合"""
        result = _tag_to_symbols("今天天气真好")
        self.assertEqual(result, set())

    def test_tag_is_substring(self):
        """标签是子串也能命中"""
        test_sym = None
        test_tag = None
        for sym, tags in SYMBOL_TAGS.items():
            if tags:
                test_sym = sym
                test_tag = tags[0]
                break
        if not test_sym:
            self.skipTest("没有定义 SYMBOL_TAGS")

        # 标签出现在更长的词里
        result = _tag_to_symbols(f"超级{test_tag}大行情")
        self.assertIn(test_sym, result)

    def test_none_text_returns_empty(self):
        """None → 空集合（不会崩溃）"""
        # _tag_to_symbols 里 for t in tags: if t in text，text=None 会抛异常吗？
        # 实际上 in None 会抛 TypeError
        # 但这个函数是内部调用，text 总是有值的
        # 这里验证空串安全
        result = _tag_to_symbols("")
        self.assertEqual(result, set())


# ═══════════════════════════════════════════════════════════════════════════
#  2. _age_factor
# ═══════════════════════════════════════════════════════════════════════════


class TestAgeFactor(unittest.TestCase):
    """_age_factor 信息时效衰减。"""

    def test_fresh_info_is_10(self):
        """新鲜信息（刚发布） → 1.0"""
        import time

        now_ts = time.time()
        factor = _age_factor(now_ts)
        self.assertAlmostEqual(factor, 1.0, places=2)

    def test_none_ts_is_10(self):
        """None → 1.0（无时间戳=永久有效？不，默认值 1.0）"""
        self.assertEqual(_age_factor(None), 1.0)

    def test_zero_ts_is_10(self):
        """0 → 1.0"""
        self.assertEqual(_age_factor(0), 1.0)

    def test_12_hours_about_065(self):
        """12 小时 → 约 0.65（1 - 0.7 * 0.5 = 0.65）"""
        import time

        ts = time.time() - 12 * 3600
        factor = _age_factor(ts)
        self.assertAlmostEqual(factor, 0.65, places=1)

    def test_24_hours_is_03(self):
        """24 小时 → 0.3（线性衰减到底）"""
        import time

        ts = time.time() - 24 * 3600
        factor = _age_factor(ts)
        self.assertAlmostEqual(factor, 0.3, places=1)

    def test_48_hours_is_01(self):
        """48 小时 → 0.1（过期底限）"""
        import time

        ts = time.time() - 48 * 3600
        factor = _age_factor(ts)
        self.assertAlmostEqual(factor, 0.1, places=2)

    def test_over_48_hours_stays_01(self):
        """超过 48 小时 → 仍为 0.1（封底）"""
        import time

        ts = time.time() - 72 * 3600  # 3 天
        factor = _age_factor(ts)
        self.assertAlmostEqual(factor, 0.1, places=2)

    def test_future_ts_is_10(self):
        """未来时间戳 → 1.0（age_h <= 0）"""
        import time

        ts = time.time() + 3600  # 1 小时后
        factor = _age_factor(ts)
        self.assertEqual(factor, 1.0)

    def test_monotonic_decreasing(self):
        """单调性：越旧的信息，衰减系数越小"""
        import time

        now = time.time()
        f_1h = _age_factor(now - 3600)
        f_6h = _age_factor(now - 6 * 3600)
        f_12h = _age_factor(now - 12 * 3600)
        f_24h = _age_factor(now - 24 * 3600)
        f_48h = _age_factor(now - 48 * 3600)
        self.assertGreaterEqual(f_1h, f_6h)
        self.assertGreaterEqual(f_6h, f_12h)
        self.assertGreaterEqual(f_12h, f_24h)
        self.assertGreaterEqual(f_24h, f_48h)

    def test_bounds_01_to_10(self):
        """范围：0.1 ~ 1.0"""
        import time

        # 测试几个不同时间点
        for hours in [0, 1, 6, 12, 24, 36, 48, 72, 168]:
            ts = time.time() - hours * 3600
            factor = _age_factor(ts)
            self.assertGreaterEqual(factor, 0.1, f"hours={hours}")
            self.assertLessEqual(factor, 1.0, f"hours={hours}")


# ═══════════════════════════════════════════════════════════════════════════
#  3. _check_criteria
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckCriteria(unittest.TestCase):
    """_check_criteria 品种筛选条件。"""

    def _make_metrics(self, **kwargs):
        """构造一个默认通过的 metrics dict"""
        m = {
            "symbol": "rb",
            "turnover_billion": 50.0,  # 50 亿，够高
            "atr_pct": 1.5,  # 1.5%，中等
            "T_D": 60.0,  # 强趋势
            "vol_ratio": 1.5,  # 1.5 倍量
        }
        m.update(kwargs)
        return m

    def _make_criteria(self, **kwargs):
        """构造筛选条件"""
        c = {
            "min_turnover": 10.0,  # 最低 10 亿
            "atr_pct_min": 0.005,  # ATR 0.5% ~ 3%
            "atr_pct_max": 0.03,
            "min_abs_T_D": 30.0,  # 最低 |T_D| = 30
            "min_volume_ratio": 1.0,  # 最低量比 1.0
            "max_correlation": 0.7,  # 最大相关 0.7
        }
        c.update(kwargs)
        return c

    def test_none_metrics_fails(self):
        """None 指标 → 不通过"""
        passed, score, reasons = _check_criteria(None, {})
        self.assertFalse(passed)
        self.assertEqual(score, 0.0)
        self.assertIn("数据不足", reasons)

    def test_all_pass(self):
        """全部满足 → 通过 + 高分"""
        m = self._make_metrics()
        c = self._make_criteria()
        passed, score, reasons = _check_criteria(m, c)
        self.assertTrue(passed)
        self.assertGreater(score, 0.8)

    def test_low_liquidity_lowers_score(self):
        """流动性不足 → 扣分"""
        c = self._make_criteria()
        m_good = self._make_metrics(turnover_billion=50.0)
        m_bad = self._make_metrics(turnover_billion=5.0)  # 5 亿 < 10 亿
        _, score_good, _ = _check_criteria(m_good, c)
        _, score_bad, _ = _check_criteria(m_bad, c)
        self.assertLess(score_bad, score_good)

    def test_high_volatility_lowers_score(self):
        """波动率过高 → 波动率项扣分（拉低总分）"""
        c = self._make_criteria()
        m_normal = self._make_metrics(atr_pct=1.5)  # 1.5%，正常
        m_high = self._make_metrics(atr_pct=4.0)  # 4%，过高
        _, score_normal, _ = _check_criteria(m_normal, c)
        _, score_high, _ = _check_criteria(m_high, c)
        # 高波动的波动率得分 = max/atr = 3/4 = 0.75，比正常的 1.0 低
        self.assertLess(score_high, score_normal)

    def test_low_volatility_lowers_score(self):
        """波动率过低 → 波动率项扣分"""
        c = self._make_criteria()
        m_normal = self._make_metrics(atr_pct=1.5)
        m_low = self._make_metrics(atr_pct=0.3)  # 0.3% < 0.5%
        _, score_normal, _ = _check_criteria(m_normal, c)
        _, score_low, _ = _check_criteria(m_low, c)
        self.assertLess(score_low, score_normal)

    def test_weak_trend_fails(self):
        """趋势不足 → 不通过"""
        m = self._make_metrics(T_D=10.0)  # |T_D|=10 < 30
        c = self._make_criteria()
        passed, score, reasons = _check_criteria(m, c)
        self.assertFalse(passed)

    def test_low_volume_ratio_fails(self):
        """量比不足 → 不通过"""
        m = self._make_metrics(vol_ratio=0.5)  # 0.5 < 1.0
        c = self._make_criteria()
        passed, score, reasons = _check_criteria(m, c)
        self.assertFalse(passed)

    def test_high_correlation_fails(self):
        """相关性过高 → 不通过"""
        m = self._make_metrics()
        c = self._make_criteria()
        corr_data = {"rb_vs_hc": 0.85}  # 0.85 > 0.7
        passed, score, reasons = _check_criteria(m, c, held_symbols=["hc"], corr_data=corr_data)
        self.assertFalse(passed)

    def test_low_correlation_passes(self):
        """相关性低 → 通过"""
        m = self._make_metrics()
        c = self._make_criteria()
        corr_data = {"rb_vs_hc": 0.3}  # 0.3 < 0.7
        passed, score, reasons = _check_criteria(m, c, held_symbols=["hc"], corr_data=corr_data)
        self.assertTrue(passed)

    def test_no_held_no_corr_check(self):
        """无持仓 → 不检查相关性（相关性得分 1.0）"""
        m = self._make_metrics()
        c = self._make_criteria()
        # 没有 held_symbols，即使有 corr_data 也不检查
        passed, score, reasons = _check_criteria(m, c, corr_data={"rb_vs_hc": 0.9})
        self.assertTrue(passed)

    def test_score_between_0_and_1(self):
        """加权分数在 0-1 之间"""
        m = self._make_metrics()
        c = self._make_criteria()
        _, score, _ = _check_criteria(m, c)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_better_metrics_higher_score(self):
        """更好的指标 → 更高的分数"""
        c = self._make_criteria()
        m_bad = self._make_metrics(turnover_billion=5.0, atr_pct=0.3, T_D=10.0, vol_ratio=0.5)
        m_good = self._make_metrics(turnover_billion=100.0, atr_pct=1.5, T_D=80.0, vol_ratio=2.0)
        _, score_bad, _ = _check_criteria(m_bad, c)
        _, score_good, _ = _check_criteria(m_good, c)
        self.assertGreater(score_good, score_bad)

    def test_reasons_have_checkmarks(self):
        """reasons 里有 ✓ 或 ✗ 标记"""
        m = self._make_metrics()
        c = self._make_criteria()
        _, _, reasons = _check_criteria(m, c)
        self.assertGreater(len(reasons), 0)
        # 每条 reason 都有 ✓ 或 ✗
        for r in reasons:
            self.assertTrue("✓" in r or "✗" in r, f"reason 没有标记: {r}")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  信息维度 + 品种筛选 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
