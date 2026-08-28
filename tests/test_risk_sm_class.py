#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RiskStateMachine 状态机 — 单元测试
=========================================

状态流转：
  NORMAL → WARNING → LOCKED → WARNING → NORMAL

触发条件：
  - risk_guard status = LOCK/WARN/OK
  - 连续止损 → 警告/冻结
  - 冷却时间 → 自动降级
  - 日亏锁 → 跨日才解除

scale():
  - NORMAL: 1.0
  - WARNING: 0.5
  - LOCKED: 0
  - 连续止损: × 0.8^n（封底 0.2）
"""

import sys
import os
import unittest
from unittest.mock import patch
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from risk_state_machine import (
    RiskStateMachine,
    RED_LINE,
    WARN_LINE,
    DAILY_LOSS_STOP,
    LOSS_DECAY,
    LOSS_FLOOR,
    CONSEC_LOCK,
    CONSEC_WARN,
    LOCK_RELEASE_SEC,
    WARN_RELEASE_SEC,
)


# ═══════════════════════════════════════════════════════════════════════════
#  1. 初始状态
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskSMInit(unittest.TestCase):
    """初始状态。"""

    def test_initial_state_is_normal(self):
        """初始状态 = NORMAL"""
        sm = RiskStateMachine()
        self.assertEqual(sm.state, "NORMAL")

    def test_initial_scale_is_one(self):
        """初始 scale = 1.0"""
        sm = RiskStateMachine()
        self.assertEqual(sm.scale(), 1.0)

    def test_initial_consec_losses_zero(self):
        """初始连续止损 = 0"""
        sm = RiskStateMachine()
        self.assertEqual(sm.consec_losses, 0)

    def test_initial_no_peak(self):
        """初始 peak_equity = None"""
        sm = RiskStateMachine()
        self.assertIsNone(sm.peak_equity)

    def test_summary_has_all_fields(self):
        """summary 返回所有必要字段"""
        sm = RiskStateMachine()
        s = sm.summary()
        for key in ["state", "scale", "consec_losses", "peak_equity",
                    "lock_reason", "daily_loss_pct", "daily_loss_stop"]:
            self.assertIn(key, s)


# ═══════════════════════════════════════════════════════════════════════════
#  2. 状态流转：NORMAL → WARN → LOCKED
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskSMStateTransitions(unittest.TestCase):
    """状态流转。"""

    def setUp(self):
        self.sm = RiskStateMachine()
        self._t = 1000000.0

    def _tick(self, delta=1.0):
        """推进模拟时间。"""
        self._t += delta
        return self._t

    def test_warn_status_moves_to_warning(self):
        """WARN → NORMAL → WARNING"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "WARN", "usage": 0.42, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")
        self.assertAlmostEqual(self.sm.scale(), 0.5, places=3)

    def test_lock_status_moves_to_locked(self):
        """LOCK → NORMAL → LOCKED"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.50,
                            "daily_loss_pct": 0, "reasons": ["保证金红线"]})
        self.assertEqual(self.sm.state, "LOCKED")
        self.assertEqual(self.sm.scale(), 0.0)
        self.assertIn("红线", self.sm.lock_reason)

    def test_ok_stays_normal(self):
        """OK → NORMAL 保持 NORMAL"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "NORMAL")
        self.assertEqual(self.sm.scale(), 1.0)

    def test_warn_then_ok_still_warning_no_cooldown(self):
        """WARN → OK，但冷却时间不够 → 仍 WARNING"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "WARN", "usage": 0.42, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")
        # 只过了 10 秒，不够 WARN_RELEASE_SEC
        self._tick(10)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.30, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")

    def test_warn_then_ok_after_cooldown_recovers(self):
        """WARN → OK，冷却足够 → 恢复 NORMAL"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "WARN", "usage": 0.42, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")
        # 过了 WARN_RELEASE_SEC + 10 秒
        self._tick(WARN_RELEASE_SEC + 10)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.30, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "NORMAL")

    def test_locked_then_ok_still_locked_no_cooldown(self):
        """LOCK → OK，但冷却不够 + 红线已解除 → 仍 LOCKED"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.50,
                            "daily_loss_pct": 0, "reasons": ["保证金红线"]})
        self.assertEqual(self.sm.state, "LOCKED")
        # 只过了 10 秒
        self._tick(10)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.30, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "LOCKED")

    def test_locked_releases_to_warning_after_cooldown(self):
        """LOCK → OK，冷却足够 + 红线解除 → 降级到 WARNING"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.50,
                            "daily_loss_pct": 0, "reasons": ["保证金红线"]})
        self.assertEqual(self.sm.state, "LOCKED")
        # 过了 LOCK_RELEASE_SEC + 10 秒
        self._tick(LOCK_RELEASE_SEC + 10)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.30, "daily_loss_pct": 0})
        # 释放后先到 WARNING（不直接回 NORMAL）
        self.assertEqual(self.sm.state, "WARNING")
        self.assertAlmostEqual(self.sm.scale(), 0.5, places=3)


# ═══════════════════════════════════════════════════════════════════════════
#  3. 日亏锁 — 跨日才解除
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskSMDailyLossLock(unittest.TestCase):
    """日亏锁：当日不自动解锁，需 reset_daily。"""

    def setUp(self):
        self.sm = RiskStateMachine()
        self._t = 1000000.0

    def _tick(self, delta=1.0):
        self._t += delta
        return self._t

    def test_daily_loss_marks_daily_loss_locked(self):
        """日亏触发锁定 → daily_loss_locked = True"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.30,
                            "daily_loss_pct": DAILY_LOSS_STOP,
                            "reasons": ["日亏达停机线"]})
        self.assertTrue(self.sm.daily_loss_locked)
        self.assertEqual(self.sm.state, "LOCKED")

    def test_daily_loss_lock_not_released_same_day(self):
        """日亏锁：当日即使浮亏回吐 + 冷却够 → 也不解锁"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.30,
                            "daily_loss_pct": DAILY_LOSS_STOP,
                            "reasons": ["日亏达停机线"]})
        self.assertTrue(self.sm.daily_loss_locked)

        # 过了很久，usage 也下来了
        self._tick(LOCK_RELEASE_SEC + 100)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0.01})
        # 还是 LOCKED（日亏锁当日不解除）
        self.assertEqual(self.sm.state, "LOCKED")

    def test_daily_loss_lock_released_after_reset(self):
        """日亏锁：reset_daily 后 + 冷却足够 → 可以解锁"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "LOCK", "usage": 0.30,
                            "daily_loss_pct": DAILY_LOSS_STOP,
                            "reasons": ["日亏达停机线"]})
        self.assertTrue(self.sm.daily_loss_locked)

        # 跨日重置
        self.sm.reset_daily()
        self.assertFalse(self.sm.daily_loss_locked)

        # 冷却够了 → 降级到 WARNING
        self._tick(LOCK_RELEASE_SEC + 10)
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")


# ═══════════════════════════════════════════════════════════════════════════
#  4. 连续止损
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskSMConsecutiveLosses(unittest.TestCase):
    """连续止损：2 笔警告，3 笔冻结。"""

    def setUp(self):
        self.sm = RiskStateMachine()
        self._t = 1000000.0

    def test_consec_warn_triggers_warning(self):
        """连续止损 ≥ CONSEC_WARN → WARNING"""
        for _ in range(CONSEC_WARN):
            self.sm.mark_loss()
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")
        self.assertIn("连续止损", self.sm.lock_reason)

    def test_consec_lock_triggers_locked(self):
        """连续止损 ≥ CONSEC_LOCK → LOCKED"""
        for _ in range(CONSEC_LOCK):
            self.sm.mark_loss()
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "LOCKED")
        self.assertEqual(self.sm.scale(), 0.0)
        self.assertTrue(self.sm.consec_lock)

    def test_consec_lock_not_released_same_day(self):
        """连续止损冻结：当日不自动解锁"""
        for _ in range(CONSEC_LOCK):
            self.sm.mark_loss()
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "LOCKED")

        # 过了很久也不解锁
        self._t += LOCK_RELEASE_SEC + 100
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.10, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "LOCKED")

    def test_consec_lock_released_after_reset_daily(self):
        """连续止损冻结：reset_daily 后 → 解除"""
        for _ in range(CONSEC_LOCK):
            self.sm.mark_loss()
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0})
        self.assertTrue(self.sm.consec_lock)

        # 跨日重置
        self.sm.reset_daily()
        self.assertFalse(self.sm.consec_lock)
        self.assertEqual(self.sm.consec_losses, 0)

        # 现在可以正常恢复了
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.10, "daily_loss_pct": 0})
        # 注意：state 可能还是 LOCKED 因为 entered_at 没更新，但 consec_lock=False
        # 再等冷却够了应该能释放
        self._t += LOCK_RELEASE_SEC + 10
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.10, "daily_loss_pct": 0})
        self.assertEqual(self.sm.state, "WARNING")

    def test_loss_factor_decay(self):
        """scale() 中连续止损衰减因子：0.8^n，封底 0.2"""
        # NORMAL 状态下
        for i in range(1, 6):
            self.sm.mark_loss()
            expected_base = 1.0
            expected_factor = max(LOSS_FLOOR, LOSS_DECAY ** i)
            expected_scale = round(expected_base * expected_factor, 3)
            actual = self.sm.scale()
            self.assertAlmostEqual(actual, expected_scale, places=3,
                msg=f"第 {i} 次止损后 scale 不对")

    def test_loss_factor_floor(self):
        """连续止损很多次 → 封底在 LOSS_FLOOR"""
        for _ in range(20):  # 0.8^20 ≈ 0.01，远低于 0.2
            self.sm.mark_loss()
        self.assertGreaterEqual(self.sm.scale(), LOSS_FLOOR)
        # 实际应该等于 LOSS_FLOOR（因为 0.8^20 < 0.2）
        self.assertAlmostEqual(self.sm.scale(), LOSS_FLOOR, places=3)


# ═══════════════════════════════════════════════════════════════════════════
#  5. peak_equity 追踪
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskSMPeakEquity(unittest.TestCase):
    """peak_equity 追踪。"""

    def setUp(self):
        self.sm = RiskStateMachine()
        self._t = 1000000.0

    def test_peak_equity_first_update(self):
        """首次 update → 设置 peak_equity"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20, "daily_loss_pct": 0},
                           equity=100000)
        self.assertEqual(self.sm.peak_equity, 100000)

    def test_peak_equity_only_goes_up(self):
        """peak_equity 只升不降（ratchet）"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20}, equity=100000)
        self._t += 1
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20}, equity=110000)
        self.assertEqual(self.sm.peak_equity, 110000)
        self._t += 1
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20}, equity=90000)
        # 还是 110000（不下降）
        self.assertEqual(self.sm.peak_equity, 110000)

    def test_no_equity_no_change(self):
        """不传 equity → peak_equity 不变"""
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20}, equity=100000)
        self._t += 1
        with patch("risk_state_machine.time.time", return_value=self._t):
            self.sm.update({"status": "OK", "usage": 0.20})  # 不传 equity
        self.assertEqual(self.sm.peak_equity, 100000)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  RiskStateMachine 状态机 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
