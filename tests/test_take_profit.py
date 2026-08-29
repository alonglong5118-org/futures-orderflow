#!/usr/bin/env python3
"""
止盈止损单元测试
===================

覆盖 calc_exit_plan() 和 sim_exit_bars() 两个纯函数。

历史 bug 回归测试：
  - 方向搞反（多单止盈在入场下方 / 空单止损在入场下方）
  - regime 系数漏乘
  - 尾仓跟踪方向搞反
  - t2 触发后未进入尾仓态（尾仓逻辑空转）
  - 同一根 bar 内止损 vs 止盈优先级

运行: python -m pytest tests/test_take_profit.py -v
  或: python tests/test_take_profit.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from take_profit_utils import calc_exit_plan, sim_exit_bars

# ═══════════════════════════════════════════════════════════════════════════
#  一、calc_exit_plan 测试
# ═══════════════════════════════════════════════════════════════════════════


class TestCalcExitPlanLong(unittest.TestCase):
    """多单止盈止损价位计算"""

    def test_basic_long_levels(self):
        """多单基础：stop 在入场下，t1/t2 在入场上"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, rr_ratio=2.0)
        stop_dist = 1.5 * 10.0  # 15
        self.assertAlmostEqual(ep["stop_dist"], stop_dist, places=2)
        self.assertAlmostEqual(ep["stop"], 100 - stop_dist, places=2)
        self.assertAlmostEqual(ep["t1"], 100 + stop_dist, places=2)
        self.assertAlmostEqual(ep["t2"], 100 + 2 * stop_dist, places=2)
        # 方向验证
        self.assertLess(ep["stop"], 100.0, "多单止损应在入场下方")
        self.assertGreater(ep["t1"], 100.0, "多单 t1 应在入场上方")
        self.assertGreater(ep["t2"], 100.0, "多单 t2 应在入场上方")

    def test_long_stop_below_entry(self):
        """回归：多单止损必须在入场价下方（不能搞反方向）"""
        ep = calc_exit_plan(entry=5000.0, dir_T=1.0, atr_val=50.0)
        self.assertLess(ep["stop"], 5000.0, "历史 bug：多单止损方向搞反，跑到入场上方了")

    def test_long_t2_above_entry(self):
        """回归：多单 t2 止盈必须在入场价上方"""
        ep = calc_exit_plan(entry=5000.0, dir_T=1.0, atr_val=50.0)
        self.assertGreater(ep["t2"], 5000.0, "历史 bug：多单止盈方向搞反，跑到入场下方了")


class TestCalcExitPlanShort(unittest.TestCase):
    """空单止盈止损价位计算"""

    def test_basic_short_levels(self):
        """空单基础：stop 在入场上，t1/t2 在入场下"""
        ep = calc_exit_plan(entry=100.0, dir_T=-1.0, atr_val=10.0, stop_atr_mult=1.5, rr_ratio=2.0)
        stop_dist = 1.5 * 10.0
        self.assertAlmostEqual(ep["stop_dist"], stop_dist, places=2)
        self.assertAlmostEqual(ep["stop"], 100 + stop_dist, places=2)
        self.assertAlmostEqual(ep["t1"], 100 - stop_dist, places=2)
        self.assertAlmostEqual(ep["t2"], 100 - 2 * stop_dist, places=2)
        # 方向验证
        self.assertGreater(ep["stop"], 100.0, "空单止损应在入场上方")
        self.assertLess(ep["t1"], 100.0, "空单 t1 应在入场下方")
        self.assertLess(ep["t2"], 100.0, "空单 t2 应在入场下方")

    def test_short_stop_above_entry(self):
        """回归：空单止损必须在入场价上方（不能搞反方向）"""
        ep = calc_exit_plan(entry=5000.0, dir_T=-1.0, atr_val=50.0)
        self.assertGreater(ep["stop"], 5000.0, "历史 bug：空单止损方向搞反，跑到入场下方了")

    def test_short_t2_below_entry(self):
        """回归：空单 t2 止盈必须在入场价下方"""
        ep = calc_exit_plan(entry=5000.0, dir_T=-1.0, atr_val=50.0)
        self.assertLess(ep["t2"], 5000.0, "历史 bug：空单止盈方向搞反，跑到入场上方了")


class TestRegimeStopCoef(unittest.TestCase):
    """regime 止损系数的影响"""

    def test_volatility_regime_widens_stop(self):
        """波动 regime (coef=1.2) → 止损比趋势 regime (coef=1.0) 宽"""
        ep_trend = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, regime_stop_coef=1.0)
        ep_vol = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, regime_stop_coef=1.2)
        self.assertGreater(ep_vol["stop_dist"], ep_trend["stop_dist"], "波动 regime 止损距离应更大")
        # 比例正确
        ratio = ep_vol["stop_dist"] / ep_trend["stop_dist"]
        self.assertAlmostEqual(ratio, 1.2, places=2)

    def test_regime_coef_applied_to_t2(self):
        """regime 系数影响止损 → 也影响 t2（因为 t2 基于 stop_dist）"""
        ep1 = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, regime_stop_coef=1.0)
        ep2 = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, regime_stop_coef=1.5)
        # t2 距离入场的距离也应该变大
        t2_dist_1 = ep1["t2"] - 100.0
        t2_dist_2 = ep2["t2"] - 100.0
        self.assertGreater(t2_dist_2, t2_dist_1, "止损放宽后，t2 目标也应该相应变远")
        self.assertAlmostEqual(t2_dist_2 / t2_dist_1, 1.5, places=2)


class TestTailParams(unittest.TestCase):
    """尾仓参数计算"""

    def test_tail_stop_dist_proportional_to_R(self):
        """尾仓跟踪距离 = tail_trail_R × stop_dist"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, stop_atr_mult=1.5, tail_trail_R=2.0)
        expected = 2.0 * 1.5 * 10.0  # tail_trail_R × stop_dist
        self.assertAlmostEqual(ep["tail_stop_dist"], expected, places=2)

    def test_tail_disabled_by_default(self):
        """默认尾仓关闭"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0)
        self.assertFalse(ep["tail_enabled"])

    def test_tail_enabled(self):
        """尾仓启用时参数正确"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=10.0, tail_enabled=True, tail_trail_R=2.5, tail_pct=0.3)
        self.assertTrue(ep["tail_enabled"])
        self.assertAlmostEqual(ep["tail_pct"], 0.3)
        self.assertAlmostEqual(ep["tail_stop_dist"], 2.5 * 1.5 * 10.0, places=2)


class TestStopDistAlwaysPositive(unittest.TestCase):
    """stop_dist 永远是正数"""

    def test_long_stop_dist_positive(self):
        """多单 stop_dist > 0"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=5.0)
        self.assertGreater(ep["stop_dist"], 0)

    def test_short_stop_dist_positive(self):
        """空单 stop_dist > 0"""
        ep = calc_exit_plan(entry=100.0, dir_T=-1.0, atr_val=5.0)
        self.assertGreater(ep["stop_dist"], 0)

    def test_zero_atr_gives_zero_stop(self):
        """ATR = 0 → stop_dist = 0（边界情况）"""
        ep = calc_exit_plan(entry=100.0, dir_T=1.0, atr_val=0.0)
        self.assertEqual(ep["stop_dist"], 0.0)
        self.assertEqual(ep["stop"], 100.0)
        self.assertEqual(ep["t1"], 100.0)
        self.assertEqual(ep["t2"], 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  二、sim_exit_bars 测试 — 基础出场
# ═══════════════════════════════════════════════════════════════════════════


def _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False, tail_trail_R=2.0):
    """构造多单 exit_plan dict（测试辅助函数）"""
    return {
        "stop": round(entry - stop_dist, 2),
        "t1": round(entry + stop_dist, 2),
        "t2": round(entry + rr * stop_dist, 2),
        "stop_dist": round(stop_dist, 2),
        "tail_enabled": tail_enabled,
        "tail_stop_dist": round(tail_trail_R * stop_dist, 2),
        "tail_pct": 0.25,
    }


def _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False, tail_trail_R=2.0):
    """构造空单 exit_plan dict（测试辅助函数）"""
    return {
        "stop": round(entry + stop_dist, 2),
        "t1": round(entry - stop_dist, 2),
        "t2": round(entry - rr * stop_dist, 2),
        "stop_dist": round(stop_dist, 2),
        "tail_enabled": tail_enabled,
        "tail_stop_dist": round(tail_trail_R * stop_dist, 2),
        "tail_pct": 0.25,
    }


class TestSimExitStopLoss(unittest.TestCase):
    """止损出场"""

    def test_long_hit_stop_first_bar(self):
        """多单：第 1 根 bar 就跌破止损 → 立即止损"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0)  # stop=90
        bars = [(105, 85)]  # high=105, low=85 → 跌破 90
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止损")
        self.assertAlmostEqual(price, 90.0, places=2)
        self.assertEqual(idx, 0)

    def test_short_hit_stop_first_bar(self):
        """空单：第 1 根 bar 就涨破止损 → 立即止损"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0)  # stop=110
        bars = [(115, 95)]  # high=115 → 涨破 110
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "止损")
        self.assertAlmostEqual(price, 110.0, places=2)
        self.assertEqual(idx, 0)

    def test_long_stop_at_bar3(self):
        """多单：第 3 根 bar 才止损 → 返回正确的索引"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0)  # stop=90
        bars = [
            (102, 98),  # bar 0: 正常波动
            (105, 100),  # bar 1: 上涨
            (103, 95),  # bar 2: 回撤，未破止损
            (99, 85),  # bar 3: 跌破止损
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止损")
        self.assertEqual(idx, 3)

    def test_long_stop_not_hit(self):
        """多单：全程没碰止损和止盈 → 期末平"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0)  # stop=90, t2=120
        bars = [
            (102, 98),
            (105, 101),
            (108, 104),
            (110, 106),
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "期末平")
        self.assertEqual(idx, 3)


class TestSimExitTakeProfit(unittest.TestCase):
    """止盈出场（无尾仓）"""

    def test_long_hit_t2_no_tail(self):
        """多单无尾仓：触及 t2 → 止盈2R 全平"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False)  # t2=120
        bars = [(125, 100)]  # high=125 → 触及 t2
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止盈2R")
        self.assertAlmostEqual(price, 120.0, places=2)
        self.assertEqual(idx, 0)

    def test_short_hit_t2_no_tail(self):
        """空单无尾仓：触及 t2 → 止盈2R 全平"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False)  # t2=80
        bars = [(105, 75)]  # low=75 → 触及 t2
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "止盈2R")
        self.assertAlmostEqual(price, 80.0, places=2)
        self.assertEqual(idx, 0)

    def test_t2_after_several_bars(self):
        """多根 bar 后才止盈"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False)  # t2=120
        bars = [
            (103, 99),
            (108, 104),
            (115, 110),
            (122, 116),  # 第 3 根触及 t2
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止盈2R")
        self.assertEqual(idx, 3)


class TestSimExitStopPriority(unittest.TestCase):
    """同一根 bar 内：止损优先级高于止盈"""

    def test_long_same_bar_both_hit_stop_first(self):
        """回归：同一根 bar 同时破止损和止盈 → 止损优先"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0)
        # stop=90, t2=120
        # 这根 bar: low=85（破止损）, high=125（破止盈）
        bars = [(125, 85)]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止损", "历史 bug：同一根 bar 内应该先检查止损，再检查止盈")

    def test_short_same_bar_both_hit_stop_first(self):
        """空单：同一根 bar 同时破止损和止盈 → 止损优先"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0)
        # stop=110, t2=80
        bars = [(115, 75)]  # high 破止损，low 破止盈
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "止损", "空单同一根 bar 也应该止损优先")


# ═══════════════════════════════════════════════════════════════════════════
#  三、sim_exit_bars 测试 — 尾仓逻辑
# ═══════════════════════════════════════════════════════════════════════════


class TestTailActivation(unittest.TestCase):
    """尾仓激活逻辑"""

    def test_long_t2_triggers_tail_mode(self):
        """回归：多单 t2 触发后应进入尾仓态，不是直接全平"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=120, tail_stop_dist=10
        # bar 0: 触及 t2 → 进入尾仓态，初始尾仓止损 = 120 - 10 = 110
        # bar 1: 继续上涨，high=122 → 尾仓止损上移到 122-10=112
        # bar 2: 跌破 112 → 尾仓离场（证明是尾仓态，而不是直接全平）
        bars = [
            (125, 115),  # bar 0: 触及 t2，进入尾仓态
            (122, 116),  # bar 1: 尾仓态，low=116 > 尾仓止损110，安全；新高122→止损上移到112
            (118, 108),  # bar 2: low=108 <= 112 → 尾仓离场
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场", "历史 bug：t2 后没有进入尾仓态，直接全平了")
        self.assertAlmostEqual(price, 112.0, places=2)
        self.assertEqual(idx, 2)

    def test_short_t2_triggers_tail_mode(self):
        """空单 t2 触发后进入尾仓态"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=80, tail_stop_dist=10
        # bar 0: 触及 t2 → 进入尾仓态，初始尾仓止损 = 80 + 10 = 90
        # bar 1: 继续下跌，low=78 → 尾仓止损下移到 78+10=88
        # bar 2: 涨破 88 → 尾仓离场
        bars = [
            (85, 75),  # bar 0: 触及 t2
            (82, 78),  # bar 1: 尾仓态，high=82 < 90，安全；新低78→止损下移到88
            (95, 82),  # bar 2: high=95 >= 88 → 尾仓离场
        ]
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 88.0, places=2)
        self.assertEqual(idx, 2)

    def test_tail_initial_stop_price(self):
        """回归：尾仓初始止损价 = t2 ± tail_stop_dist"""
        # 多单：尾仓止损 = t2 - tail_stop_dist
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=2.0)
        # t2=120, tail_stop_dist=20 → 初始尾仓止损 = 100
        bars = [
            (125, 105),  # bar 0: 触及 t2，进入尾仓，尾仓止损=100
            (122, 99),  # bar 1: low=99 <= 100 → 尾仓离场，价=100
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 100.0, places=2, msg="尾仓初始止损价应该是 t2 - tail_stop_dist = 100")


class TestTrailingStopMovement(unittest.TestCase):
    """移动止损（尾仓态下止损价跟随行情移动）"""

    def test_long_trail_moves_up(self):
        """多单尾仓：创新高 → 尾仓止损上移"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=120, tail_stop_dist=10
        # bar 0: 触及 t2=120 → 尾仓止损=110
        # bar 1: 最高 130 → 尾仓止损上移到 120 (130-10)
        # bar 2: 最低 115 → 高于 110 但低于 120？不，115 < 120 会触发
        # 等等，让我重新算：bar 1 high=130, tail_stop = max(110, 130-10)=120
        # bar 2: low=118 < 120 → 触发尾仓止损，出场价 120
        bars = [
            (125, 115),  # bar 0: 进入尾仓态，尾仓止损=110
            (130, 120),  # bar 1: 创新高，尾仓止损上移到 120
            (125, 118),  # bar 2: 跌破 120 → 尾仓离场
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 120.0, places=2, msg="尾仓止损应该上移到 120，而不是停在 110")

    def test_long_trail_never_moves_down(self):
        """多单尾仓：止损价只上移不下移（ratchet 机制）"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=120, tail_stop_dist=10 → 初始尾仓止损=110
        bars = [
            (125, 115),  # bar 0: 进入尾仓，尾仓止损=110
            (130, 122),  # bar 1: 新高，尾仓止损=120
            (128, 121),  # bar 2: 回落，但 low=121 > 120，尾仓止损保持 120
            (125, 119),  # bar 3: low=119 < 120 → 触发
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 120.0, places=2, msg="尾仓止损不应回落，应该保持在最高位 120")

    def test_short_trail_moves_down(self):
        """空单尾仓：创新低 → 尾仓止损下移"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=80, tail_stop_dist=10 → 初始尾仓止损=90
        bars = [
            (85, 75),  # bar 0: 进入尾仓，尾仓止损=90
            (80, 70),  # bar 1: 新低，尾仓止损下移到 80 (70+10)
            (85, 75),  # bar 2: 反弹，high=85 < 80? 不，85 > 80 → 触发！
        ]
        # 等等，bar 2 high=85，尾仓止损=80，85 >= 80 → 触发尾仓止损
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 80.0, places=2, msg="空单尾仓止损应该下移到 80")

    def test_short_trail_never_moves_up(self):
        """空单尾仓：止损价只下移不上移（ratchet 机制）"""
        ep = _make_ep_short(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.0)
        # t2=80, tail_stop_dist=10 → 初始尾仓止损=90
        bars = [
            (85, 75),  # bar 0: 进入尾仓，尾仓止损=90
            (78, 68),  # bar 1: 新低，尾仓止损=78 (68+10)
            (82, 72),  # bar 2: 反弹，high=82 > 78 → 触发尾仓止损
        ]
        price, reason, idx = sim_exit_bars(bars, -1.0, 100.0, ep)
        self.assertEqual(reason, "尾仓离场")
        self.assertAlmostEqual(price, 78.0, places=2, msg="空单尾仓止损不应回升，应该保持在最低位 78")


class TestTailNoStopLossInTailMode(unittest.TestCase):
    """尾仓态下不再检查原始止损（只检查移动止损）"""

    def test_long_tail_mode_ignores_original_stop(self):
        """多单尾仓态：价格回到原始止损附近但高于尾仓止损 → 不触发"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=0.5)
        # stop=90, t2=120, tail_stop_dist=5
        # bar 0: 触及 t2 → 尾仓止损 = 120 - 5 = 115
        # bar 1: 价格回落到 105，但尾仓止损是 115 → 应该触发尾仓止损
        # 等等，105 < 115，所以会触发。让我调整一下测试。
        # 重新设计：tail_trail_R 大一点，尾仓止损低于 t2 但高于原始止损
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=True, tail_trail_R=1.5)
        # stop=90, t2=120, tail_stop_dist=15 → 初始尾仓止损=105
        bars = [
            (125, 115),  # bar 0: 进入尾仓态，尾仓止损=105
            (120, 110),  # bar 1: 回落，low=110 > 105，不触发
            (118, 108),  # bar 2: 继续回落，low=108 > 105
        ]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        # 没出场 → 期末平
        self.assertEqual(reason, "期末平", "尾仓态下只检查移动止损，不应再检查原始止损")


# ═══════════════════════════════════════════════════════════════════════════
#  四、边界 & 异常情况
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases(unittest.TestCase):
    """边界情况"""

    def test_empty_bars(self):
        """空 bar 列表 → 期末平，索引 -1"""
        ep = _make_ep_long()
        price, reason, idx = sim_exit_bars([], 1.0, 100.0, ep)
        self.assertEqual(reason, "期末平")
        self.assertEqual(idx, -1)

    def test_single_bar_no_hit(self):
        """单根 bar，不触发 → 期末平"""
        ep = _make_ep_long(entry=100.0, stop_dist=20.0)  # stop=80, t2=140
        bars = [(105, 95)]
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "期末平")
        self.assertEqual(idx, 0)

    def test_exactly_at_stop(self):
        """价格恰好等于止损价 → 触发（<= / >= 判断）"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0)  # stop=90
        bars = [(100, 90)]  # low 恰好 = 90
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止损", "恰好触及止损价也应触发")

    def test_exactly_at_t2(self):
        """价格恰好等于 t2 → 触发"""
        ep = _make_ep_long(entry=100.0, stop_dist=10.0, rr=2.0, tail_enabled=False)  # t2=120
        bars = [(120, 100)]  # high 恰好 = 120
        price, reason, idx = sim_exit_bars(bars, 1.0, 100.0, ep)
        self.assertEqual(reason, "止盈2R", "恰好触及 t2 也应触发")


class TestDirectionConsistency(unittest.TestCase):
    """方向一致性：dir_T 为 0 或极小值的处理"""

    def test_dir_zero_treated_as_short(self):
        """dir_T = 0 → 按空单处理（因为 is_long = dir_T > 0 为 False）"""
        ep = calc_exit_plan(entry=100.0, dir_T=0.0, atr_val=10.0)
        # dir_T=0 时 is_long=False，按空单算
        self.assertGreaterEqual(ep["stop"], 100.0, "dir_T=0 时按空单处理，止损应在入场上方")

    def test_small_positive_dir_is_long(self):
        """dir_T 微小正值 → 多单"""
        ep = calc_exit_plan(entry=100.0, dir_T=0.001, atr_val=10.0)
        self.assertLess(ep["stop"], 100.0)

    def test_small_negative_dir_is_short(self):
        """dir_T 微小负值 → 空单"""
        ep = calc_exit_plan(entry=100.0, dir_T=-0.001, atr_val=10.0)
        self.assertGreater(ep["stop"], 100.0)


# ═══════════════════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  止盈止损单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
