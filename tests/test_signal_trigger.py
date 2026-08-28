#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号触发判断 — 单元测试
=========================

覆盖 signal_trigger_utils 中的 6 个纯函数：
  1. compute_bias_FC         — F/C 合成背景偏置
  2. check_hard_veto         — F/C 反向硬否决
  3. check_fc_confirmation   — F/C 同向确认
  4. compute_effective_threshold — 有效阈值计算
  5. check_same_direction    — bias_G 与 T 同向判断
  6. signal_trigger_decision — 完整触发决策

历史 bug 回归（P-B / P-C 改造）：
  - P-C：硬否决阈值太高（bias_G≥60 几乎不可达）→ F/C 形同虚设
  - P-B：同向确认没生效（F/C 强同向应该降阈值，但逻辑没接上）
  - T 方向为 0 时误触发
  - bias_G 中性时错误阻挡信号（bias_G≈0 应该放行）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from signal_trigger_utils import (
    check_fc_confirmation,
    check_hard_veto,
    check_same_direction,
    compute_bias_FC,
    compute_effective_threshold,
    signal_trigger_decision,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. bias_FC 计算
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeBiasFC(unittest.TestCase):
    """F/C 合成背景偏置计算。"""

    def test_basic_calculation(self):
        """基础计算：bias_FC = 0.25*F + 0.15*C"""
        # F=100, C=0 → 25.0
        self.assertAlmostEqual(compute_bias_FC(100, 0), 25.0, places=1)

    def test_F_and_C_both_positive(self):
        """F 和 C 都正 → bias_FC 正，F 权重更大"""
        # F=60, C=40 → 0.25*60 + 0.15*40 = 15 + 6 = 21
        result = compute_bias_FC(60, 40)
        self.assertAlmostEqual(result, 21.0, places=1)
        self.assertGreater(result, 0)

    def test_F_and_C_both_negative(self):
        """F 和 C 都负 → bias_FC 负"""
        # F=-60, C=-40 → -15 + (-6) = -21
        result = compute_bias_FC(-60, -40)
        self.assertAlmostEqual(result, -21.0, places=1)
        self.assertLess(result, 0)

    def test_F_pos_C_neg_partial_cancel(self):
        """F 正 C 负 → 部分抵消"""
        # F=80, C=-60 → 20 + (-9) = 11
        result = compute_bias_FC(80, -60)
        self.assertAlmostEqual(result, 11.0, places=1)

    def test_zero_inputs(self):
        """F=0, C=0 → bias_FC=0"""
        self.assertEqual(compute_bias_FC(0, 0), 0.0)

    def test_F_weight_heavier_than_C(self):
        """F 权重(0.25) > C 权重(0.15) — 验证权重比"""
        # F 和 C 绝对值相同，F 的贡献更大
        result_F = compute_bias_FC(100, 0)   # 25
        result_C = compute_bias_FC(0, 100)   # 15
        self.assertGreater(result_F, result_C)
        self.assertAlmostEqual(result_F / result_C, 0.25 / 0.15, places=2)

    def test_rounded_to_1_decimal(self):
        """结果保留 1 位小数"""
        # F=33, C=11 → 0.25*33 + 0.15*11 = 8.25 + 1.65 = 9.9
        result = compute_bias_FC(33, 11)
        # 9.9 恰好是 1 位小数
        self.assertEqual(result, 9.9)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 硬否决
# ═══════════════════════════════════════════════════════════════════════════

class TestHardVeto(unittest.TestCase):
    """F/C 反向硬否决判断。"""

    def test_strong_reverse_triggers_veto(self):
        """强反向 → 触发硬否决"""
        # dir_T=1（多），bias_FC=-30（F/C 偏空，绝对值 30 >= 25）
        vetoed, reason = check_hard_veto(bias_FC=-30, dir_T=1, fc_hard=25)
        self.assertTrue(vetoed)
        self.assertIn("硬否决", reason)

    def test_same_direction_no_veto(self):
        """同向 → 不否决"""
        # dir_T=1（多），bias_FC=30（F/C 偏多）
        vetoed, _ = check_hard_veto(bias_FC=30, dir_T=1, fc_hard=25)
        self.assertFalse(vetoed)

    def test_weak_reverse_no_veto(self):
        """弱反向（低于阈值）→ 不否决"""
        # dir_T=1，bias_FC=-20（绝对值 20 < 25）
        vetoed, _ = check_hard_veto(bias_FC=-20, dir_T=1, fc_hard=25)
        self.assertFalse(vetoed)

    def test_exactly_at_threshold_veto(self):
        """恰好等于阈值 → 触发否决（>= 判断）"""
        vetoed, _ = check_hard_veto(bias_FC=-25, dir_T=1, fc_hard=25)
        self.assertTrue(vetoed, "恰好达到硬否决阈值也应触发")

    def test_bias_FC_zero_no_veto(self):
        """bias_FC=0 → 不否决"""
        vetoed, _ = check_hard_veto(bias_FC=0, dir_T=1, fc_hard=25)
        self.assertFalse(vetoed)

    def test_dir_zero_no_veto(self):
        """dir_T=0 → 不否决（无方向可否决）"""
        vetoed, reason = check_hard_veto(bias_FC=50, dir_T=0, fc_hard=25)
        self.assertFalse(vetoed)
        self.assertEqual(reason, "")

    def test_short_with_positive_biasFC_veto(self):
        """空单 + F/C 偏多 → 硬否决"""
        vetoed, reason = check_hard_veto(bias_FC=30, dir_T=-1, fc_hard=25)
        self.assertTrue(vetoed)
        self.assertIn("反向硬否决", reason)

    def test_short_with_negative_biasFC_no_veto(self):
        """空单 + F/C 偏空 → 不否决（同向）"""
        vetoed, _ = check_hard_veto(bias_FC=-30, dir_T=-1, fc_hard=25)
        self.assertFalse(vetoed)

    def test_custom_higher_threshold_harder_to_veto(self):
        """更高的 fc_hard → 更难触发否决"""
        # bias_FC=-30，阈值 35 → 不否决
        vetoed, _ = check_hard_veto(bias_FC=-30, dir_T=1, fc_hard=35)
        self.assertFalse(vetoed)

    def test_custom_lower_threshold_easier_to_veto(self):
        """更低的 fc_hard → 更容易触发否决"""
        # bias_FC=-15，阈值 10 → 否决
        vetoed, _ = check_hard_veto(bias_FC=-15, dir_T=1, fc_hard=10)
        self.assertTrue(vetoed)

    def test_pc_regression_hard_veto_reachable(self):
        """回归（P-C）：硬否决阈值应该可达，不是虚设

        历史 bug：原硬否决用 bias_G≥60，F/C 合成后几乎达不到，
        导致 F/C 没有实际否决权，属于"空转"。
        修复后用 bias_FC + fc_hard=25，合理的 F/C 值就能达到。
        """
        # 用合理的 F/C 值：F=-70, C=-50 → bias_FC = -17.5 - 7.5 = -25
        # 恰好达到阈值 25 → 应该能否决
        bias_FC = compute_bias_FC(F=-70, C=-50)
        vetoed, _ = check_hard_veto(bias_FC=bias_FC, dir_T=1, fc_hard=25)
        self.assertTrue(vetoed,
            "P-C 回归 bug：硬否决阈值不可达，F/C 否决权是空转的")


# ═══════════════════════════════════════════════════════════════════════════
#  3. 同向确认
# ═══════════════════════════════════════════════════════════════════════════

class TestFCConfirmation(unittest.TestCase):
    """F/C 同向确认判断。"""

    def test_strong_same_dir_confirmed(self):
        """强同向 → 确认成立"""
        confirmed = check_fc_confirmation(bias_FC=30, dir_T=1, fc_confirm=25)
        self.assertTrue(confirmed)

    def test_reverse_not_confirmed(self):
        """反向 → 不确认"""
        confirmed = check_fc_confirmation(bias_FC=-30, dir_T=1, fc_confirm=25)
        self.assertFalse(confirmed)

    def test_weak_same_dir_not_confirmed(self):
        """弱同向（低于阈值）→ 不确认"""
        confirmed = check_fc_confirmation(bias_FC=20, dir_T=1, fc_confirm=25)
        self.assertFalse(confirmed)

    def test_exactly_at_threshold_confirmed(self):
        """恰好等于阈值 → 确认成立（>= 判断）"""
        confirmed = check_fc_confirmation(bias_FC=25, dir_T=1, fc_confirm=25)
        self.assertTrue(confirmed, "恰好达到确认阈值也应成立")

    def test_bias_FC_zero_not_confirmed(self):
        """bias_FC=0 → 不确认"""
        confirmed = check_fc_confirmation(bias_FC=0, dir_T=1, fc_confirm=25)
        self.assertFalse(confirmed)

    def test_dir_zero_not_confirmed(self):
        """dir_T=0 → 不确认"""
        confirmed = check_fc_confirmation(bias_FC=30, dir_T=0, fc_confirm=25)
        self.assertFalse(confirmed)

    def test_short_negative_biasFC_confirmed(self):
        """空单 + F/C 偏空 → 确认成立"""
        confirmed = check_fc_confirmation(bias_FC=-30, dir_T=-1, fc_confirm=25)
        self.assertTrue(confirmed)

    def test_short_positive_biasFC_not_confirmed(self):
        """空单 + F/C 偏多 → 不确认（反向）"""
        confirmed = check_fc_confirmation(bias_FC=30, dir_T=-1, fc_confirm=25)
        self.assertFalse(confirmed)

    def test_custom_lower_threshold_easier_confirm(self):
        """更低的确认阈值 → 更容易确认"""
        # bias_FC=15，阈值 10 → 确认
        confirmed = check_fc_confirmation(bias_FC=15, dir_T=1, fc_confirm=10)
        self.assertTrue(confirmed)

    def test_pb_regression_fc_confirm_reduces_threshold(self):
        """回归（P-B）：同向确认应该真正降低阈值

        历史 bug：F/C 强同向但没有实际降阈值，属于"空转"。
        这里验证确认成立 + 阈值确实降低了。
        """
        # 先用同向确认判断
        confirmed = check_fc_confirmation(bias_FC=30, dir_T=1, fc_confirm=25)
        self.assertTrue(confirmed, "F/C 强同向应该确认")

        # 再验证有效阈值确实降低了
        base_thr = 70.0
        eff_thr = compute_effective_threshold(base_thr, confirmed, confirm_relief=0.85)
        self.assertLess(eff_thr, base_thr,
            "P-B 回归 bug：同向确认没有降低阈值，是空转的")
        self.assertAlmostEqual(eff_thr, base_thr * 0.85, places=4)


# ═══════════════════════════════════════════════════════════════════════════
#  4. 有效阈值
# ═══════════════════════════════════════════════════════════════════════════

class TestEffectiveThreshold(unittest.TestCase):
    """有效阈值计算。"""

    def test_no_confirm_same_as_base(self):
        """无确认 → 阈值不变"""
        thr = compute_effective_threshold(70.0, False, confirm_relief=0.85)
        self.assertEqual(thr, 70.0)

    def test_confirm_reduces_threshold(self):
        """有确认 → 阈值 = base × confirm_relief"""
        thr = compute_effective_threshold(70.0, True, confirm_relief=0.85)
        self.assertAlmostEqual(thr, 59.5, places=4)  # 70 * 0.85 = 59.5

    def test_relief_1_no_change(self):
        """confirm_relief = 1.0 → 确认也不降低阈值"""
        thr = compute_effective_threshold(70.0, True, confirm_relief=1.0)
        self.assertEqual(thr, 70.0)

    def test_relief_0_5_halves_threshold(self):
        """confirm_relief = 0.5 → 阈值减半（极端配置）"""
        thr = compute_effective_threshold(100.0, True, confirm_relief=0.5)
        self.assertEqual(thr, 50.0)

    def test_zero_base_threshold(self):
        """基准阈值为 0 → 始终为 0"""
        thr = compute_effective_threshold(0.0, True, confirm_relief=0.85)
        self.assertEqual(thr, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  5. 同向判断
# ═══════════════════════════════════════════════════════════════════════════

class TestSameDirection(unittest.TestCase):
    """bias_G 与 T 方向同向判断。"""

    def test_both_positive_same_dir(self):
        """都正 → 同向"""
        self.assertTrue(check_same_direction(bias_G=30, dir_T=1))

    def test_both_negative_same_dir(self):
        """都负 → 同向"""
        self.assertTrue(check_same_direction(bias_G=-30, dir_T=-1))

    def test_pos_biasG_neg_dirT_not_same(self):
        """bias_G 正 + dir_T 负 → 不同向"""
        self.assertFalse(check_same_direction(bias_G=30, dir_T=-1))

    def test_neg_biasG_pos_dirT_not_same(self):
        """bias_G 负 + dir_T 正 → 不同向"""
        self.assertFalse(check_same_direction(bias_G=-30, dir_T=1))

    def test_biasG_zero_same_dir(self):
        """bias_G = 0 → 同向（中性背景不阻挡）"""
        self.assertTrue(check_same_direction(bias_G=0, dir_T=1))
        self.assertTrue(check_same_direction(bias_G=0, dir_T=-1))

    def test_biasG_near_zero_same_dir(self):
        """bias_G 接近 0（< 1e-6）→ 同向"""
        self.assertTrue(check_same_direction(bias_G=1e-7, dir_T=1))
        self.assertTrue(check_same_direction(bias_G=-1e-7, dir_T=-1))

    def test_biasG_above_epsilon_still_checks(self):
        """bias_G 略大于 1e-6 → 正常判断"""
        self.assertTrue(check_same_direction(bias_G=1e-5, dir_T=1))
        self.assertFalse(check_same_direction(bias_G=1e-5, dir_T=-1))

    def test_dir_zero_not_same(self):
        """dir_T=0 → 不同向（无方向）"""
        self.assertFalse(check_same_direction(bias_G=30, dir_T=0))
        self.assertFalse(check_same_direction(bias_G=0, dir_T=0))


# ═══════════════════════════════════════════════════════════════════════════
#  6. 完整触发决策
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalTriggerDecision(unittest.TestCase):
    """完整触发决策集成测试。"""

    def test_basic_trigger_long(self):
        """多单：T 强 + 同向 + 无否决 → 触发"""
        result = signal_trigger_decision(
            T_5m=80, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=20,
        )
        self.assertTrue(result["triggered"])
        self.assertFalse(result["hard_veto"])
        self.assertTrue(result["same_dir"])

    def test_basic_trigger_short(self):
        """空单：T 强 + 同向 + 无否决 → 触发"""
        result = signal_trigger_decision(
            T_5m=-80, dir_T=-1, T_thresh_eff=70,
            bias_G=-30, bias_FC=-20,
        )
        self.assertTrue(result["triggered"])
        self.assertTrue(result["same_dir"])

    def test_T_below_threshold_no_trigger(self):
        """T 低于阈值 → 不触发"""
        result = signal_trigger_decision(
            T_5m=60, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=20,
        )
        self.assertFalse(result["triggered"])

    def test_dir_zero_no_trigger(self):
        """dir_T=0 → 不触发（核心安全边界）"""
        result = signal_trigger_decision(
            T_5m=90, dir_T=0, T_thresh_eff=70,
            bias_G=30, bias_FC=30,
        )
        self.assertFalse(result["triggered"])
        self.assertFalse(result["hard_veto"])
        self.assertFalse(result["fc_confirmed"])
        self.assertFalse(result["same_dir"])

    def test_hard_veto_blocks_trigger(self):
        """硬否决 → 不触发，即使 T 很强"""
        result = signal_trigger_decision(
            T_5m=90, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=-30,  # F/C 强反向
            fc_hard=25,
        )
        self.assertFalse(result["triggered"])
        self.assertTrue(result["hard_veto"])
        self.assertIn("硬否决", result["hard_veto_reason"])

    def test_fc_confirmation_lowers_threshold(self):
        """F/C 同向确认 → 降低阈值，原本不够的 T 也能触发"""
        # T_5m=62, base_thr=70 → 不够
        result_no_confirm = signal_trigger_decision(
            T_5m=62, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=10,  # 弱同向，不确认
            fc_confirm=25, confirm_relief=0.85,
        )
        self.assertFalse(result_no_confirm["triggered"],
                         "无确认时 T=62 不应触发（阈值 70）")

        # 同样的 T，F/C 强同向确认 → 阈值降到 59.5，62 >= 59.5 → 触发
        result_with_confirm = signal_trigger_decision(
            T_5m=62, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=30,  # 强同向，确认
            fc_confirm=25, confirm_relief=0.85,
        )
        self.assertTrue(result_with_confirm["triggered"],
                        "P-B 回归 bug：同向确认没有降低阈值，T 还是不够触发")
        self.assertTrue(result_with_confirm["fc_confirmed"])
        self.assertAlmostEqual(result_with_confirm["effective_thr"], 59.5, places=4)

    def test_opposite_biasG_blocks_trigger(self):
        """bias_G 反向 → 不同向 → 不触发，即使 T 很强"""
        result = signal_trigger_decision(
            T_5m=90, dir_T=1, T_thresh_eff=70,
            bias_G=-30, bias_FC=10,  # bias_G 反向
        )
        self.assertFalse(result["triggered"])
        self.assertFalse(result["same_dir"])

    def test_neutral_biasG_allows_trigger(self):
        """bias_G 中性（≈0）→ 放行，T 够就触发"""
        result = signal_trigger_decision(
            T_5m=80, dir_T=1, T_thresh_eff=70,
            bias_G=0, bias_FC=10,
        )
        self.assertTrue(result["triggered"])
        self.assertTrue(result["same_dir"])

    def test_exactly_at_threshold_triggers(self):
        """T 恰好等于阈值 → 触发（>= 判断）"""
        result = signal_trigger_decision(
            T_5m=70, dir_T=1, T_thresh_eff=70,
            bias_G=10, bias_FC=10,
        )
        self.assertTrue(result["triggered"], "T 恰好等于阈值也应触发")

    def test_barely_below_threshold_no_trigger(self):
        """T 略低于阈值 → 不触发"""
        result = signal_trigger_decision(
            T_5m=69.99, dir_T=1, T_thresh_eff=70,
            bias_G=10, bias_FC=10,
        )
        self.assertFalse(result["triggered"])

    def test_T_strong_but_vetoed_no_trigger(self):
        """回归：T 很强但被 F/C 硬否决 → 绝不触发

        历史 bug（P-C）：硬否决阈值太高，F/C 反向也拦不住 T。
        """
        result = signal_trigger_decision(
            T_5m=100, dir_T=1, T_thresh_eff=70,
            bias_G=50, bias_FC=-30,  # F/C 强反向
            fc_hard=25,
        )
        self.assertFalse(result["triggered"],
            "P-C 回归 bug：F/C 强反向应该硬否决，但 T 还是触发了")
        self.assertTrue(result["hard_veto"])

    def test_T_weak_with_confirm_triggers(self):
        """回归：T 不够但 F/C 强同向确认 → 降低阈值后触发

        历史 bug（P-B）：同向确认是空转，没有实际降阈值。
        """
        # T=60, 阈值=70 → 差 10 点
        # F/C 强同向 → 阈值降到 70*0.85=59.5 → 60 >= 59.5 → 触发
        result = signal_trigger_decision(
            T_5m=60, dir_T=1, T_thresh_eff=70,
            bias_G=20, bias_FC=30,
            fc_confirm=25, confirm_relief=0.85,
        )
        self.assertTrue(result["triggered"],
            "P-B 回归 bug：F/C 同向确认没有降低阈值，T 本该触发但没触发")
        self.assertTrue(result["fc_confirmed"])


class TestDecisionEdgeCases(unittest.TestCase):
    """触发决策的边界情况。"""

    def test_zero_T_zero_dir_no_trigger(self):
        """T=0, dir_T=0 → 不触发"""
        result = signal_trigger_decision(
            T_5m=0, dir_T=0, T_thresh_eff=70,
            bias_G=0, bias_FC=0,
        )
        self.assertFalse(result["triggered"])

    def test_negative_T_with_pos_dir_mismatch(self):
        """T 值为负但 dir_T 为正（理论上不应出现，但要安全）

        注意：abs(T_5m) 判断，所以 T=-80, dir_T=1 时 abs=80 >= 70
        但 same_dir 检查：bias_G 正 + dir_T 正 → 同向
        所以会触发。这是符合设计的（abs 判断 + 方向独立判断）。
        """
        result = signal_trigger_decision(
            T_5m=-80, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=20,
        )
        # abs(-80) = 80 >= 70，同向 → 触发
        self.assertTrue(result["triggered"])
        self.assertTrue(result["same_dir"])

    def test_very_high_fc_hard_never_vetoes(self):
        """fc_hard 非常大 → 几乎不会否决"""
        result = signal_trigger_decision(
            T_5m=80, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=-90,  # 强反向
            fc_hard=100,  # 阈值极高
        )
        self.assertFalse(result["hard_veto"])
        # bias_G 正 + dir_T 正 → 同向 → T 够 → 触发
        self.assertTrue(result["triggered"])

    def test_fc_confirm_zero_always_confirmed(self):
        """fc_confirm = 0 → 任何同向都算确认（极端配置）"""
        result = signal_trigger_decision(
            T_5m=65, dir_T=1, T_thresh_eff=70,
            bias_G=30, bias_FC=1,  # 极微弱同向
            fc_confirm=0, confirm_relief=0.85,
        )
        self.assertTrue(result["fc_confirmed"])
        # 阈值降到 59.5，T=65 够 → 触发
        self.assertTrue(result["triggered"])


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  信号触发判断 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
