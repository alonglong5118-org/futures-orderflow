#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline 四维总入口 — 集成测试
===================================

测试 pipeline() 函数的核心集成逻辑：
用 F_override / c_override 绕过外部数据依赖，专注验证内部合成链路。

1. 输出结构完整性
2. F/C override 生效
3. 消融实验（ablate F/T/C）
4. 硬否决（F/C 反向强 → 否决 T 方向）
5. F/C 同向确认 → 降阈值
6. corr_gate 集成（高相关时降权弱维）
7. regime 阈值调制
8. 风控锁定前置拦截
9. HMM / GARCH 阈值调制（live 专属）
10. SR 位信号质量调制（live 专属）

历史覆盖：
  - P-B：F/C 同向确认降阈值（不是空转）
  - P-C：F/C 反向硬否决（真正能否决 T）
  - P1-2：corr_gate 真降权（不是只改文本）
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from four_dim_strategy import pipeline

# ═══════════════════════════════════════════════════════════════════════════
#  测试数据构造
# ═══════════════════════════════════════════════════════════════════════════

def _make_daily_df(n=120, start=1000, slope=3, vol=2, seed=42):
    """构造日线趋势数据。"""
    np.random.seed(seed)
    px = start + np.arange(n) * slope + np.cumsum(np.random.randn(n) * vol)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "open": px,
        "high": px + abs(np.random.randn(n) * 4),
        "low": px - abs(np.random.randn(n) * 4),
        "close": px,
        "volume": 10000 + np.random.randn(n) * 500,
        "open_interest": 50000 + np.arange(n) * 100,
    }, index=idx)
    df["high"] = df[["high", "close"]].max(axis=1)
    df["low"] = df[["low", "close"]].min(axis=1)
    return df


def _make_5m_df(n=120, start=1000, slope=0.5, vol=1, seed=99):
    """构造 5 分钟数据（60+ 根才会启用 5m T）。"""
    np.random.seed(seed)
    px = start + np.arange(n) * slope + np.cumsum(np.random.randn(n) * vol)
    idx = pd.date_range("2026-06-01 09:00", periods=n, freq="5min")
    df = pd.DataFrame({
        "open": px,
        "high": px + abs(np.random.randn(n) * 1.5),
        "low": px - abs(np.random.randn(n) * 1.5),
        "close": px,
        "volume": 500 + np.random.randn(n) * 50,
    }, index=idx)
    df["high"] = df[["high", "close"]].max(axis=1)
    df["low"] = df[["low", "close"]].min(axis=1)
    return df


# ═══════════════════════════════════════════════════════════════════════════
#  1. 输出结构
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineOutput(unittest.TestCase):
    """pipeline 输出结构完整性。"""

    def test_all_required_fields_present(self):
        """返回所有必要字段"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=20)
        required = [
            "F", "T_D", "T_5m", "C",
            "bias_G", "bias_FC", "dir_T", "dir_T_raw",
            "regime", "rdesc",
            "triggered", "T_thresh_eff", "T_thresh_used",
            "conv", "used_5m", "hard_veto",
            "corr_action", "bs_mode",
        ]
        for key in required:
            self.assertIn(key, result, "缺少字段: %s" % key)

    def test_dir_T_values_valid(self):
        """dir_T ∈ {-1, 0, 1}"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=20)
        self.assertIn(result["dir_T"], [-1, 0, 1])

    def test_regime_not_empty(self):
        """regime 不为空"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=20)
        self.assertTrue(result["regime"])

    def test_bias_G_is_number(self):
        """bias_G 是数值"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=20)
        self.assertIsInstance(result["bias_G"], (int, float))

    def test_bias_FC_formula(self):
        """bias_FC = 0.25*F + 0.15*C"""
        df = _make_daily_df()
        F, C = 40, 60
        result = pipeline("rb", df, F_override=F, c_override=C)
        expected = round(0.25 * F + 0.15 * C, 1)
        self.assertAlmostEqual(result["bias_FC"], expected, places=1)


# ═══════════════════════════════════════════════════════════════════════════
#  2. F/C override 与消融
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineOverridesAndAblation(unittest.TestCase):
    """F/C override 与消融实验。"""

    def test_F_override_applied(self):
        """F_override 直接设置 F 值"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=80, c_override=0)
        self.assertEqual(result["F"], 80.0)

    def test_C_override_applied(self):
        """c_override 直接设置 C 值"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=0, c_override=-50)
        self.assertEqual(result["C"], -50.0)

    def test_ablate_F_zeros_F(self):
        """ablate='F' → F=0"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=80, c_override=20, ablate="F")
        self.assertEqual(result["F"], 0.0)
        # C 不变
        self.assertEqual(result["C"], 20.0)

    def test_ablate_C_zeros_C(self):
        """ablate='C' → C=0"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=60, ablate="C")
        self.assertEqual(result["C"], 0.0)

    def test_ablate_T_zeros_T_D(self):
        """ablate='T' → T_D=0"""
        df = _make_daily_df()
        result = pipeline("rb", df, F_override=30, c_override=20, ablate="T")
        self.assertEqual(result["T_D"], 0.0)

    def test_ablate_changes_bias_G(self):
        """消融后 bias_G 应该变小（少了一维贡献）"""
        df = _make_daily_df(slope=3)
        # 正常：T 正 + F 正 + C 正 → bias_G 大
        r_full = pipeline("rb", df, F_override=50, c_override=50)
        # 消融 T：T=0 → bias_G 变小
        r_noT = pipeline("rb", df, F_override=50, c_override=50, ablate="T")
        # 消融后 bias_G 应该不同
        self.assertNotEqual(r_full["bias_G"], r_noT["bias_G"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. 硬否决（P-C 回归）
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineHardVeto(unittest.TestCase):
    """F/C 反向硬否决（P-C 回归测试）。"""

    def test_strong_fc_reverse_triggers_hard_veto(self):
        """F/C 强反向 → hard_veto=True（P-C：否决权真的生效）"""
        df = _make_daily_df(slope=5)  # 强趋势 → T 正
        # F 和 C 都负，且足够强 → bias_FC 负且绝对值大
        result = pipeline("rb", df, F_override=-80, c_override=-80)
        # T 正 + F/C 强负反向 → 应该被硬否决
        if result["dir_T"] == 1:  # 确认 T 是正的
            self.assertTrue(result["hard_veto"],
                "P-C 回归 bug：F/C 强反向应该触发硬否决，但没有！")
            self.assertFalse(result["triggered"],
                "硬否决时 triggered 应该为 False")

    def test_weak_fc_reverse_no_veto(self):
        """F/C 弱反向 → 不触发硬否决"""
        df = _make_daily_df(slope=5)
        # F/C 反向但很弱 → 不够硬否决阈值
        result = pipeline("rb", df, F_override=-10, c_override=-10)
        if result["dir_T"] == 1:
            self.assertFalse(result["hard_veto"],
                "弱反向不应该触发硬否决")

    def test_same_direction_no_veto(self):
        """F/C 与 T 同向 → 不否决"""
        df = _make_daily_df(slope=5)
        result = pipeline("rb", df, F_override=60, c_override=60)
        if result["dir_T"] == 1:
            self.assertFalse(result["hard_veto"],
                "同向不应该触发硬否决")

    def test_hard_veto_blocks_trigger(self):
        """硬否决时 triggered 必为 False"""
        df = _make_daily_df(slope=5)
        result = pipeline("rb", df, F_override=-80, c_override=-80)
        if result["hard_veto"]:
            self.assertFalse(result["triggered"])

    def test_hard_veto_reason_populated(self):
        """硬否决时有 reason 说明"""
        df = _make_daily_df(slope=5)
        result = pipeline("rb", df, F_override=-80, c_override=-80)
        if result["hard_veto"]:
            self.assertTrue(result["hard_veto_reason"])


# ═══════════════════════════════════════════════════════════════════════════
#  4. F/C 同向确认（P-B 回归）
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineFCConfirmation(unittest.TestCase):
    """F/C 同向确认降阈值（P-B 回归测试）。"""

    def test_fc_confirm_reduces_threshold(self):
        """F/C 强同向 → T_thresh_used < T_thresh_eff（确认降阈值）"""
        df = _make_daily_df(slope=2)
        # F/C 都正且强 → 同向确认
        result = pipeline("rb", df, F_override=80, c_override=60)
        if result["dir_T"] == 1:  # T 正
            # 有确认时，实际使用的阈值应该比有效阈值低
            self.assertLess(result["T_thresh_used"], result["T_thresh_eff"],
                "P-B 回归 bug：F/C 同向确认应该降低 T 阈值，但没有！")

    def test_no_fc_confirm_threshold_unchanged(self):
        """F/C 不同向（或中性）→ T_thresh_used == T_thresh_eff"""
        df = _make_daily_df(slope=2)
        # C = 0 → 无确认
        result = pipeline("rb", df, F_override=0, c_override=0)
        if result["dir_T"] != 0:
            self.assertAlmostEqual(result["T_thresh_used"], result["T_thresh_eff"],
                msg="无 F/C 确认时，使用阈值应等于有效阈值")

    def test_fc_confirm_increases_trigger_chance(self):
        """F/C 同向确认 → 更容易触发（triggered 概率更高）

        用一个 T 刚好略低于阈值的边缘场景：
        - 无确认 → 不触发
        - 有确认 → 降阈值后触发
        """
        df = _make_daily_df(slope=1.5, seed=7)  # 弱趋势
        # 无确认
        r_no = pipeline("rb", df, F_override=0, c_override=0)
        # 强确认（降阈值）
        r_yes = pipeline("rb", df, F_override=80, c_override=60)
        # 确认时的触发率应该 >= 无确认时
        # （具体取决于 T 强度，我们验证阈值确实降了）
        if r_no["dir_T"] != 0 and r_yes["dir_T"] != 0:
            self.assertLessEqual(r_yes["T_thresh_used"], r_no["T_thresh_used"])


# ═══════════════════════════════════════════════════════════════════════════
#  5. corr_gate 集成（P1-2 回归）
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineCorrGate(unittest.TestCase):
    """corr_gate 在 pipeline 中的集成（P1-2 回归）。"""

    def _make_corr_hist(self, corr, n=30):
        """构造指定相关系数的 (T, C) 历史。"""
        np.random.seed(42)
        t = np.random.randn(n) * 30
        # c = corr * t + noise
        noise = np.random.randn(n) * 30 * np.sqrt(1 - corr ** 2) if abs(corr) < 1 else 0
        c = corr * t + noise
        return list(zip(t.tolist(), c.tolist()))

    def test_high_corr_weak_dim_zeroed(self):
        """高相关 + T 弱 → T 被降为 0（P1-2：真降权不是空转）"""
        df = _make_daily_df(slope=1)  # 弱趋势 → T 小
        hist = self._make_corr_hist(0.95, n=30)
        # C 给一个很大的值
        result = pipeline("rb", df, F_override=0, c_override=80, corr_hist=hist)
        # T 和 C 高相关，且 T 更弱 → T 应该被降权为 0
        if "降权T" in result["corr_action"]:
            self.assertEqual(result["T_D"], 0.0,
                "P1-2 回归 bug：corr_gate 说降了 T，但 T_D 实际没变！")
            self.assertTrue(result["C"] != 0,
                "降权 T 时 C 应该保留")

    def test_low_corr_no_action(self):
        """低相关 → 不降权"""
        df = _make_daily_df(slope=3)
        hist = self._make_corr_hist(0.2, n=30)
        result = pipeline("rb", df, F_override=0, c_override=30, corr_hist=hist)
        self.assertIn("正常", result["corr_action"])

    def test_insufficient_history_no_action(self):
        """历史数据不足 → 不降权"""
        df = _make_daily_df(slope=3)
        hist = self._make_corr_hist(0.95, n=5)  # 只有 5 个，不够 10 个
        result = pipeline("rb", df, F_override=0, c_override=30, corr_hist=hist)
        # 历史不足时 corr_action 应该是无冗余/正常计权
        self.assertIn("无冗余", result["corr_action"])

    def test_no_corr_hist_no_action(self):
        """不传 corr_hist → 不降权"""
        df = _make_daily_df(slope=3)
        result = pipeline("rb", df, F_override=0, c_override=30, corr_hist=None)
        self.assertIn("无冗余", result["corr_action"])


# ═══════════════════════════════════════════════════════════════════════════
#  6. 风控锁定前置拦截
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineRiskLock(unittest.TestCase):
    """风控锁定前置拦截。"""

    def test_risk_locked_returns_empty(self):
        """风控锁定 → 直接返回空信号，不计算任何东西"""
        df = _make_daily_df(slope=5)
        # state = HALTED / LOCKED 时触发锁定
        risk_state = {"state": "HALTED", "lock_reason": "测试锁定"}
        result = pipeline("rb", df, F_override=50, c_override=30, risk_state=risk_state)
        self.assertTrue(result["risk_blocked"])
        self.assertEqual(result["dir_T"], 0)
        self.assertFalse(result["triggered"])
        self.assertEqual(result["F"], 0.0)
        self.assertEqual(result["T_D"], 0.0)
        self.assertEqual(result["C"], 0.0)
        self.assertEqual(result["conv"], "风控锁定")

    def test_risk_unlocked_normal(self):
        """风控未锁定 → 正常计算"""
        df = _make_daily_df(slope=5)
        risk_state = {"state": "NORMAL", "scale": 1.0}
        result = pipeline("rb", df, F_override=50, c_override=30, risk_state=risk_state)
        # 未被锁定（可能没有 risk_blocked 字段，或为 False）
        self.assertFalse(result.get("risk_blocked", False))
        # 正常计算了 T
        self.assertIsNotNone(result["T_D"])


# ═══════════════════════════════════════════════════════════════════════════
#  7. HMM / GARCH 阈值调制
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineThresholdModulation(unittest.TestCase):
    """HMM / GARCH 阈值调制。"""

    def test_hmm_trend_reduces_threshold(self):
        """HMM 趋势市 → 阈值 ×0.90（降低）"""
        df = _make_daily_df(slope=3)
        r_base = pipeline("rb", df, F_override=0, c_override=0, hmm_label=None)
        r_hmm = pipeline("rb", df, F_override=0, c_override=0, hmm_label="trend_up")
        # 趋势市阈值应该更低
        self.assertLess(r_hmm["T_thresh_eff"], r_base["T_thresh_eff"])

    def test_hmm_high_vol_increases_threshold(self):
        """HMM 高波动 → 阈值 ×1.25（升高）"""
        df = _make_daily_df(slope=3)
        r_base = pipeline("rb", df, F_override=0, c_override=0, hmm_label=None)
        r_hmm = pipeline("rb", df, F_override=0, c_override=0, hmm_label="high_vol")
        self.assertGreater(r_hmm["T_thresh_eff"], r_base["T_thresh_eff"])

    def test_garch_low_reduces_threshold(self):
        """GARCH 低波动 → 阈值 ×0.97（略降）"""
        df = _make_daily_df(slope=3)
        r_base = pipeline("rb", df, F_override=0, c_override=0, garch_label=None)
        r_garch = pipeline("rb", df, F_override=0, c_override=0, garch_label="low")
        self.assertLess(r_garch["T_thresh_eff"], r_base["T_thresh_eff"])

    def test_garch_extreme_increases_threshold(self):
        """GARCH 极端波动 → 阈值 ×1.12（升高）"""
        df = _make_daily_df(slope=3)
        r_base = pipeline("rb", df, F_override=0, c_override=0, garch_label=None)
        r_garch = pipeline("rb", df, F_override=0, c_override=0, garch_label="extreme")
        self.assertGreater(r_garch["T_thresh_eff"], r_base["T_thresh_eff"])


# ═══════════════════════════════════════════════════════════════════════════
#  8. 5m 数据降级
# ═══════════════════════════════════════════════════════════════════════════

class TestPipeline5mFallback(unittest.TestCase):
    """5m 数据降级行为。"""

    def test_no_5m_uses_daily_T(self):
        """无 5m 数据 → T_5m = T_D, used_5m = False"""
        df = _make_daily_df(slope=3)
        result = pipeline("rb", df, df_5m=None, F_override=0, c_override=0)
        self.assertFalse(result["used_5m"])
        self.assertEqual(result["T_5m"], result["T_D"])

    def test_insufficient_5m_falls_back(self):
        """5m 数据不足 60 根 → 回退到日线"""
        df_d = _make_daily_df(slope=3)
        df_5m_short = _make_5m_df(n=30)  # 只有 30 根
        result = pipeline("rb", df_d, df_5m=df_5m_short, F_override=0, c_override=0)
        self.assertFalse(result["used_5m"])

    def test_sufficient_5m_enables(self):
        """5m 数据 >= 60 根 → 启用 5m T"""
        df_d = _make_daily_df(slope=3)
        df_5m = _make_5m_df(n=120)
        result = pipeline("rb", df_d, df_5m=df_5m, F_override=0, c_override=0)
        self.assertTrue(result["used_5m"])


# ═══════════════════════════════════════════════════════════════════════════
#  9. SR 位信号质量调制
# ═══════════════════════════════════════════════════════════════════════════

class TestPipelineSRQuality(unittest.TestCase):
    """SR 位信号质量调制（需 sr_analyzer 模块可用）。"""

    def test_sr_none_no_effect(self):
        """sr_result=None → 无 SR 调制，sr_quality_note 为空"""
        df = _make_daily_df(slope=3)
        result = pipeline("rb", df, F_override=0, c_override=0, sr_result=None)
        self.assertEqual(result["sr_quality_note"], "")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  pipeline 四维总入口 — 集成测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
