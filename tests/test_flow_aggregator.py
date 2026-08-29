#!/usr/bin/env python3
"""
C 维度流量聚合 — 单元测试
=============================

测试 FlowAggregator 类的核心计算逻辑：

1. push_minishare — 快照累积 + 净流计算
   - dP × dOI（双增=资金流入，看多）
   - dOI=0 时回退 dP × dVol

2. c_flow_score — C_flow 评分 ∈ [-100, 100]
   - 近 window 个净流分量的累计方向
   - 归一化：sum / (mag * window) * 100
   - 数据不足（< 3 个 delta）→ 0

3. tick 订单流叠加
   - 同向增强 / 反向制衡
   - 权重 70% 基础 + 30% tick

历史背景：
  C 维度（资金流/订单流）是四维策略的第三维，
  与 T（技术面）、F（基本面）、C（基本面/资金面）共同构成背景偏置和信号触发。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import FlowAggregator, compute_C_flow

# ═══════════════════════════════════════════════════════════════════════════
#  1. 基础累积与净流方向
# ═══════════════════════════════════════════════════════════════════════════


class TestFlowAggregatorBasic(unittest.TestCase):
    """FlowAggregator 基础累积与净流方向。"""

    def test_insufficient_data_returns_zero(self):
        """delta 少于 3 个 → 返回 0"""
        agg = FlowAggregator("rb")
        # 只 push 2 个快照 → 只有 1 个 delta
        agg.push_minishare(last=100, oi=1000, vol=500)
        agg.push_minishare(last=101, oi=1100, vol=600)
        self.assertEqual(agg.c_flow_score(), 0.0)

    def test_three_snaps_two_deltas_still_zero(self):
        """3 个快照 = 2 个 delta → 还是 0（需要 ≥ 3 个 delta）"""
        agg = FlowAggregator("rb")
        for i in range(3):
            agg.push_minishare(last=100 + i, oi=1000 + i * 10, vol=500 + i * 5)
        # 3 个快照 → 2 个 delta → 不够 3 个
        self.assertEqual(agg.c_flow_score(), 0.0)

    def test_four_snaps_three_deltas_nonzero(self):
        """4 个快照 = 3 个 delta → 开始有分数"""
        agg = FlowAggregator("rb")
        # 持续价涨仓增 → 净流入 → 正分数
        for i in range(4):
            agg.push_minishare(last=100 + i, oi=1000 + i * 10, vol=500 + i * 5)
        score = agg.c_flow_score()
        self.assertNotEqual(score, 0.0)
        self.assertGreater(score, 0)  # 价涨仓增 → 看多

    def test_price_up_oi_up_positive_flow(self):
        """价涨 + 仓增 → 资金流入（accumulation）→ 正分数"""
        agg = FlowAggregator("rb")
        # 5 个快照，价涨仓增
        for i in range(5):
            agg.push_minishare(last=100 + i * 2, oi=1000 + i * 20, vol=500)
        score = agg.c_flow_score()
        self.assertGreater(score, 0)

    def test_price_down_oi_up_negative_flow(self):
        """价跌 + 仓增 → 资金流出（distribution）→ 负分数"""
        agg = FlowAggregator("rb")
        for i in range(5):
            agg.push_minishare(last=100 - i * 2, oi=1000 + i * 20, vol=500)
        score = agg.c_flow_score()
        self.assertLess(score, 0)

    def test_price_up_oi_down_negative_flow(self):
        """价涨 + 仓减 → 资金流出（多头平仓）→ 负分数"""
        agg = FlowAggregator("rb")
        for i in range(5):
            agg.push_minishare(last=100 + i * 2, oi=1000 - i * 20, vol=500)
        score = agg.c_flow_score()
        # dP>0, dOI<0 → flow = dP*dOI < 0 → 负
        self.assertLess(score, 0)

    def test_price_down_oi_down_positive_flow(self):
        """价跌 + 仓减 → 空头平仓 → 正分数（资金流入？取决于符号）"""
        agg = FlowAggregator("rb")
        for i in range(5):
            agg.push_minishare(last=100 - i * 2, oi=1000 - i * 20, vol=500)
        score = agg.c_flow_score()
        # dP<0, dOI<0 → flow = dP*dOI > 0 → 正
        self.assertGreater(score, 0)

    def test_score_bounded_within_100(self):
        """分数 ∈ [-100, 100]"""
        agg = FlowAggregator("rb")
        # 极强的单向流
        for i in range(30):
            agg.push_minishare(last=100 + i * 10, oi=1000 + i * 100, vol=500)
        score = agg.c_flow_score()
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, -100.0)

    def test_flat_price_zero_score(self):
        """价格不动 → 净流为 0 → 分数 ≈ 0"""
        agg = FlowAggregator("rb")
        for i in range(10):
            agg.push_minishare(last=100, oi=1000 + i * 10, vol=500)
        score = agg.c_flow_score()
        # dP=0 → flow=0 → 分数 0
        self.assertEqual(score, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. dVol 回退（无 OI 数据时）
# ═══════════════════════════════════════════════════════════════════════════


class TestFlowAggregatorVolFallback(unittest.TestCase):
    """无 OI 数据时回退到成交量。"""

    def test_no_oi_uses_volume(self):
        """oi 全为 None → 用 vol 计算净流"""
        agg = FlowAggregator("rb")
        # 价涨 + 量增 → 正
        for i in range(5):
            agg.push_minishare(last=100 + i * 2, oi=None, vol=500 + i * 20)
        score = agg.c_flow_score()
        self.assertGreater(score, 0)

    def test_no_oi_price_down_vol_up_negative(self):
        """价跌 + 量增 → 负分数（放量下跌）"""
        agg = FlowAggregator("rb")
        for i in range(5):
            agg.push_minishare(last=100 - i * 2, oi=None, vol=500 + i * 20)
        score = agg.c_flow_score()
        self.assertLess(score, 0)

    def test_no_oi_no_vol_zero(self):
        """oi 和 vol 都没有 → flow = 0 → 分数 0"""
        agg = FlowAggregator("rb")
        for i in range(5):
            agg.push_minishare(last=100 + i * 2, oi=None, vol=None)
        score = agg.c_flow_score()
        self.assertEqual(score, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  3. tick 订单流叠加
# ═══════════════════════════════════════════════════════════════════════════


class TestFlowAggregatorTick(unittest.TestCase):
    """tick 订单流叠加（70% 基础 + 30% tick）。"""

    def _make_bullish_agg(self):
        """构造一个看多的基础聚合器。"""
        agg = FlowAggregator("rb")
        for i in range(10):
            agg.push_minishare(last=100 + i * 2, oi=1000 + i * 20, vol=500)
        return agg

    def test_tick_zero_no_change(self):
        """tick_delta = 0 → 不影响分数"""
        agg = self._make_bullish_agg()
        base = agg.c_flow_score()
        agg.push_tick(0)
        with_tick = agg.c_flow_score()
        self.assertAlmostEqual(base, with_tick, places=3)

    def test_tick_same_direction_boosts(self):
        """同向 tick → 分数增强（更极端）"""
        agg = self._make_bullish_agg()
        base = agg.c_flow_score()
        # 正的基础 + 正的 tick → 更
        agg.push_tick(100)
        boosted = agg.c_flow_score()
        self.assertGreater(boosted, base, "同向 tick 应该增强（分数更极端）")

    def test_tick_opposite_direction_reduces(self):
        """反向 tick → 分数减弱"""
        agg = self._make_bullish_agg()
        base = agg.c_flow_score()  # 正的
        # 正的基础 + 负的 tick → 减弱
        agg.push_tick(-100)
        reduced = agg.c_flow_score()
        self.assertLess(reduced, base, "反向 tick 应该减弱分数")

    def test_tick_capped_at_100(self):
        """tick 叠加后仍不超过 100"""
        agg = self._make_bullish_agg()
        agg.push_tick(1000)  # 极端大的 tick
        score = agg.c_flow_score()
        self.assertLessEqual(score, 100.0)

    def test_tick_floored_at_minus_100(self):
        """tick 叠加后不低于 -100"""
        agg = FlowAggregator("rb")
        for i in range(10):
            agg.push_minishare(last=100 - i * 2, oi=1000 + i * 20, vol=500)
        agg.push_tick(-1000)
        score = agg.c_flow_score()
        self.assertGreaterEqual(score, -100.0)

    def test_tick_weight_ratio(self):
        """tick 权重 = 30%，基础权重 = 70%"""
        # 构造：基础正好 50，tick 正好 100
        # 加权后 = 0.7 * 50 + 0.3 * 100 = 35 + 30 = 65
        agg = FlowAggregator("rb")
        # 我们需要基础分 = 50
        # 用 push_tick 验证比例
        # 先构造一个基础分接近 100 的
        for i in range(30):
            agg.push_minishare(last=100 + i * 2, oi=1000 + i * 20, vol=500)
        base = agg.c_flow_score()
        agg.push_tick(100)
        boosted = agg.c_flow_score()
        # 0.7*base + 0.3*100 = boosted
        # 验证比例大约是 70/30
        expected = 0.7 * base + 0.3 * 100
        expected = min(100.0, max(-100.0, expected))
        self.assertAlmostEqual(boosted, expected, places=0)


# ═══════════════════════════════════════════════════════════════════════════
#  4. compute_C_flow 快捷函数
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeCFlow(unittest.TestCase):
    """compute_C_flow — 快捷计算函数。"""

    def test_bullish_snapshots_positive(self):
        """看多快照序列 → 正分数"""
        # [(last, oi, vol), ...]
        snapshots = [(100 + i * 2, 1000 + i * 20, 500) for i in range(10)]
        score = compute_C_flow("rb", snapshots)
        self.assertGreater(score, 0)

    def test_bearish_snapshots_negative(self):
        """看空快照序列 → 负分数"""
        snapshots = [(100 - i * 2, 1000 + i * 20, 500) for i in range(10)]
        score = compute_C_flow("rb", snapshots)
        self.assertLess(score, 0)

    def test_too_few_snapshots_zero(self):
        """快照太少 → 0"""
        snapshots = [(100, 1000, 500), (101, 1100, 600)]
        score = compute_C_flow("rb", snapshots)
        self.assertEqual(score, 0.0)

    def test_empty_snapshots_zero(self):
        """空列表 → 0"""
        score = compute_C_flow("rb", [])
        self.assertEqual(score, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. 滑动窗口与边界
# ═══════════════════════════════════════════════════════════════════════════


class TestFlowAggregatorWindow(unittest.TestCase):
    """滑动窗口行为与边界。"""

    def test_large_window_smoother(self):
        """大窗口 → 分数更平滑（对短期波动不敏感）"""
        # 构造：大部分正，最后 1 根负
        snaps = [(100 + i * 2, 1000 + i * 20, 500) for i in range(29)]
        # 最后一根大跌
        snaps.append((50, 2000, 1000))

        agg_small = FlowAggregator("rb", window=5)
        for s in snaps:
            agg_small.push_minishare(*s)

        agg_large = FlowAggregator("rb", window=30)
        for s in snaps:
            agg_large.push_minishare(*s)

        # 小窗口更容易被最后一根影响
        # 大窗口有更多正数据缓冲，分数应该更高（更正）
        self.assertGreater(agg_large.c_flow_score(), agg_small.c_flow_score(), "大窗口应该更平滑，受单根影响更小")

    def test_default_window_30(self):
        """默认窗口 = 30"""
        agg = FlowAggregator("rb")
        self.assertEqual(agg.window, 30)

    def test_snaps_truncated_at_2x_window(self):
        """快照数超过 2×window 时会截断"""
        agg = FlowAggregator("rb", window=10)
        # push 30 个 → 2*10 = 20，超过的会被 pop
        for i in range(30):
            agg.push_minishare(last=100 + i, oi=1000 + i * 10, vol=500)
        # 应该 <= 2*window
        self.assertLessEqual(len(agg.snaps), 2 * agg.window + 1)
        self.assertEqual(len(agg.deltas), len(agg.snaps) - 1)

    def test_alternating_flow_milder_than_unidirectional(self):
        """交替正负流 → 分数绝对值 < 纯单向流"""
        # 构造真正的交替流：一轮价涨仓增(正)，下一轮价涨仓减(负)
        # 正：dP>0, dOI>0 → flow>0
        # 负：dP>0, dOI<0 → flow<0（多头平仓，资金流出）
        agg_alt = FlowAggregator("rb")
        last, oi = 100, 2000
        for i in range(20):
            last += 2  # 价格一直涨
            if i % 2 == 0:
                oi += 30  # 仓增 → 正
            else:
                oi -= 30  # 仓减 → 负
            agg_alt.push_minishare(last=last, oi=oi, vol=500)
        alt_score = abs(agg_alt.c_flow_score())

        # 纯单向流（价涨仓增，全正）
        agg_uni = FlowAggregator("rb")
        last, oi = 100, 2000
        for i in range(20):
            last += 2
            oi += 30
            agg_uni.push_minishare(last=last, oi=oi, vol=500)
        uni_score = abs(agg_uni.c_flow_score())

        self.assertLess(alt_score, uni_score, "交替流的分数绝对值应该小于纯单向流（因为正负抵消）")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  C 维度流量聚合 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
