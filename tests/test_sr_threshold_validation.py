#!/usr/bin/env python3
"""
SR 阈值验证 — 单元测试
===========================

1. analyze_by_zone — 按距离分档统计
   - 近位区 / 灰色地带 / 远位区 三档分类
   - near_pct 和 grey_pct 边界值（左闭右开）
   - 空交易列表 → 每档都是 0
   - mode 参数：nearest / friendly / hostile
   - expR 和 win_rate 计算正确
   - 全赚 → win_rate = 1.0
   - 全亏 → win_rate = 0.0

2. fine_grained_bins — 细粒度分档
   - 9 个分档全部存在
   - 交易正确落入对应档位
   - 边界值精确（左闭右开）
   - 空档 trades=0, expR=0, win_rate=0
   - mode 参数切换距离字段
   - 多档混合统计正确
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sr_threshold_validation import analyze_by_zone, fine_grained_bins

# ═══════════════════════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════════════════════


def _make_trade(nearest_dist, R_adj, friendly_dist=None, hostile_dist=None):
    """构造一笔带距离字段的交易"""
    return {
        "nearest_dist": nearest_dist,
        "R_adj": R_adj,
        "friendly_dist": friendly_dist if friendly_dist is not None else nearest_dist,
        "hostile_dist": hostile_dist if hostile_dist is not None else nearest_dist * 2,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  1. analyze_by_zone
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalyzeByZone(unittest.TestCase):
    """analyze_by_zone 按距离分档统计。"""

    def test_three_zones_exist(self):
        """三档分区都存在"""
        trades = [
            _make_trade(0.5, 1.0),  # 近位区
            _make_trade(1.2, -0.5),  # 灰色地带
            _make_trade(2.0, 2.0),  # 远位区
        ]
        result = analyze_by_zone(trades)
        self.assertEqual(len(result), 3)
        # 检查三个 key
        keys = list(result.keys())
        self.assertTrue(any("近位" in k for k in keys))
        self.assertTrue(any("灰色" in k for k in keys))
        self.assertTrue(any("远位" in k for k in keys))

    def test_empty_trades_all_zero(self):
        """空交易列表 → 每档都是 0"""
        result = analyze_by_zone([])
        for zone, stats in result.items():
            self.assertEqual(stats["trades"], 0)
            self.assertEqual(stats["expR"], 0.0)
            self.assertEqual(stats["win_rate"], 0.0)

    def test_near_zone_correct(self):
        """近位区交易统计正确"""
        trades = [
            _make_trade(0.3, 2.0),  # 近位区，赚
            _make_trade(0.7, -1.0),  # 近位区，亏
            _make_trade(0.5, 0.5),  # 近位区，赚
        ]
        result = analyze_by_zone(trades, near_pct=0.8, grey_pct=1.6)
        near_key = [k for k in result if "近位" in k][0]
        near = result[near_key]
        self.assertEqual(near["trades"], 3)
        self.assertAlmostEqual(near["expR"], (2.0 - 1.0 + 0.5) / 3, places=4)
        self.assertAlmostEqual(near["win_rate"], 2 / 3, places=4)

    def test_boundary_near_pct_exclusive(self):
        """near_pct 边界：d == near_pct → 灰色地带（左闭右开）"""
        # d = 0.8 正好等于 near_pct=0.8 → 落入灰色地带
        trades = [_make_trade(0.8, 1.0)]
        result = analyze_by_zone(trades, near_pct=0.8, grey_pct=1.6)
        near_key = [k for k in result if "近位" in k][0]
        grey_key = [k for k in result if "灰色" in k][0]
        self.assertEqual(result[near_key]["trades"], 0)
        self.assertEqual(result[grey_key]["trades"], 1)

    def test_boundary_grey_pct_exclusive(self):
        """grey_pct 边界：d == grey_pct → 远位区"""
        trades = [_make_trade(1.6, 1.0)]
        result = analyze_by_zone(trades, near_pct=0.8, grey_pct=1.6)
        grey_key = [k for k in result if "灰色" in k][0]
        far_key = [k for k in result if "远位" in k][0]
        self.assertEqual(result[grey_key]["trades"], 0)
        self.assertEqual(result[far_key]["trades"], 1)

    def test_all_wins_win_rate_1(self):
        """全赚 → win_rate = 1.0"""
        trades = [
            _make_trade(0.5, 2.0),
            _make_trade(0.3, 1.0),
            _make_trade(0.7, 0.5),
        ]
        result = analyze_by_zone(trades, near_pct=1.0, grey_pct=2.0)
        near_key = [k for k in result if "近位" in k][0]
        self.assertEqual(result[near_key]["win_rate"], 1.0)

    def test_all_losses_win_rate_0(self):
        """全亏 → win_rate = 0.0"""
        trades = [
            _make_trade(0.5, -2.0),
            _make_trade(0.3, -1.0),
        ]
        result = analyze_by_zone(trades, near_pct=1.0, grey_pct=2.0)
        near_key = [k for k in result if "近位" in k][0]
        self.assertEqual(result[near_key]["win_rate"], 0.0)

    def test_mode_nearest_default(self):
        """默认 mode = nearest"""
        trades = [_make_trade(0.5, 1.0)]
        result = analyze_by_zone(trades, near_pct=1.0, grey_pct=2.0)
        # 默认用 nearest_dist = 0.5 → 近位区
        near_key = [k for k in result if "近位" in k][0]
        self.assertEqual(result[near_key]["trades"], 1)

    def test_mode_friendly(self):
        """mode = friendly 使用 friendly_dist"""
        trades = [
            # nearest_dist=0.5（近位）, friendly_dist=1.2（灰色）
            _make_trade(0.5, 1.0, friendly_dist=1.2)
        ]
        result = analyze_by_zone(trades, near_pct=0.8, grey_pct=1.6, mode="friendly")
        # friendly_dist = 1.2 → 灰色地带
        grey_key = [k for k in result if "灰色" in k][0]
        self.assertEqual(result[grey_key]["trades"], 1)

    def test_mode_hostile(self):
        """mode = hostile 使用 hostile_dist"""
        trades = [
            # nearest_dist=0.5, hostile_dist=2.5（远位）
            _make_trade(0.5, 1.0, hostile_dist=2.5)
        ]
        result = analyze_by_zone(trades, near_pct=0.8, grey_pct=2.0, mode="hostile")
        # hostile_dist = 2.5 ≥ 2.0 → 远位区
        far_key = [k for k in result if "远位" in k][0]
        self.assertEqual(result[far_key]["trades"], 1)

    def test_exr_negative_for_all_losses(self):
        """全亏 → expR 为负"""
        trades = [_make_trade(0.5, -1.5), _make_trade(0.3, -0.5)]
        result = analyze_by_zone(trades, near_pct=1.0, grey_pct=2.0)
        near_key = [k for k in result if "近位" in k][0]
        self.assertLess(result[near_key]["expR"], 0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. fine_grained_bins
# ═══════════════════════════════════════════════════════════════════════════


class TestFineGrainedBins(unittest.TestCase):
    """fine_grained_bins 细粒度分档。"""

    def test_nine_bins(self):
        """9 个分档全部存在"""
        stats = fine_grained_bins([])
        self.assertEqual(len(stats), 9)

    def test_empty_bins_zero_stats(self):
        """空档 trades=0, expR=0, win_rate=0"""
        stats = fine_grained_bins([])
        for label, n, expR, wr in stats:
            self.assertEqual(n, 0)
            self.assertEqual(expR, 0.0)
            self.assertEqual(wr, 0.0)

    def test_trade_falls_in_correct_bin(self):
        """交易正确落入对应档位"""
        trades = [_make_trade(0.4, 1.0)]  # 0.3-0.5%
        stats = fine_grained_bins(trades)
        # 找 0.3-0.5% 档
        bin_03_05 = [s for s in stats if s[0] == "0.3-0.5%"][0]
        self.assertEqual(bin_03_05[1], 1)  # n=1
        self.assertAlmostEqual(bin_03_05[2], 1.0, places=4)  # expR=1.0
        self.assertEqual(bin_03_05[3], 1.0)  # win_rate=1.0

    def test_boundary_left_inclusive(self):
        """左闭：d == lo → 落入该档"""
        # 0.3 正好是 0.3-0.5% 档的左边界
        trades = [_make_trade(0.3, 1.0)]
        stats = fine_grained_bins(trades)
        bin_03_05 = [s for s in stats if s[0] == "0.3-0.5%"][0]
        self.assertEqual(bin_03_05[1], 1)

    def test_boundary_right_exclusive(self):
        """右开：d == hi → 落入下一档"""
        # 0.5 正好是 0.3-0.5% 档的右边界 → 落入 0.5-0.8%
        trades = [_make_trade(0.5, 1.0)]
        stats = fine_grained_bins(trades)
        bin_03_05 = [s for s in stats if s[0] == "0.3-0.5%"][0]
        bin_05_08 = [s for s in stats if s[0] == "0.5-0.8%"][0]
        self.assertEqual(bin_03_05[1], 0)
        self.assertEqual(bin_05_08[1], 1)

    def test_multiple_bins_mixed(self):
        """多档混合统计正确"""
        trades = [
            _make_trade(0.2, 2.0),  # 0-0.3%，赚
            _make_trade(0.4, -1.0),  # 0.3-0.5%，亏
            _make_trade(0.6, 1.5),  # 0.5-0.8%，赚
            _make_trade(1.0, 0.5),  # 0.8-1.2%，赚
            _make_trade(2.5, -2.0),  # 2.0-3.0%，亏
            _make_trade(6.0, 3.0),  # >=5.0%，赚
        ]
        stats = fine_grained_bins(trades)
        stats_dict = {label: (n, expR, wr) for label, n, expR, wr in stats}
        self.assertEqual(stats_dict["0-0.3%"][0], 1)
        self.assertEqual(stats_dict["0.3-0.5%"][0], 1)
        self.assertEqual(stats_dict["0.5-0.8%"][0], 1)
        self.assertEqual(stats_dict["0.8-1.2%"][0], 1)
        self.assertEqual(stats_dict["2.0-3.0%"][0], 1)
        self.assertEqual(stats_dict[">=5.0%"][0], 1)

    def test_mode_nearest_default(self):
        """默认 mode = nearest"""
        trades = [_make_trade(0.5, 1.0, friendly_dist=2.0)]
        stats = fine_grained_bins(trades)
        # nearest_dist = 0.5 → 0.5-0.8% 档
        bin_05_08 = [s for s in stats if s[0] == "0.5-0.8%"][0]
        self.assertEqual(bin_05_08[1], 1)

    def test_mode_friendly(self):
        """mode = friendly 使用 friendly_dist"""
        trades = [_make_trade(0.2, 1.0, friendly_dist=1.0)]
        stats = fine_grained_bins(trades, mode="friendly")
        # friendly_dist = 1.0 → 0.8-1.2% 档
        bin_08_12 = [s for s in stats if s[0] == "0.8-1.2%"][0]
        self.assertEqual(bin_08_12[1], 1)

    def test_bin_labels_ordered(self):
        """分档按距离升序排列"""
        stats = fine_grained_bins([])
        labels = [s[0] for s in stats]
        expected = [
            "0-0.3%",
            "0.3-0.5%",
            "0.5-0.8%",
            "0.8-1.2%",
            "1.2-1.6%",
            "1.6-2.0%",
            "2.0-3.0%",
            "3.0-5.0%",
            ">=5.0%",
        ]
        self.assertEqual(labels, expected)

    def test_last_bin_includes_high_values(self):
        """最后一档 >=5.0% 包含很大的值"""
        trades = [_make_trade(10.0, 5.0), _make_trade(50.0, -3.0)]
        stats = fine_grained_bins(trades)
        last_bin = [s for s in stats if s[0] == ">=5.0%"][0]
        self.assertEqual(last_bin[1], 2)
        # expR = (5.0 + (-3.0)) / 2 = 1.0
        self.assertAlmostEqual(last_bin[2], 1.0, places=4)
        # win_rate = 1/2 = 0.5
        self.assertEqual(last_bin[3], 0.5)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SR 阈值验证 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
