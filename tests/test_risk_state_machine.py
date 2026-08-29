#!/usr/bin/env python3
"""
风控状态机 — 单元测试
=======================

1. risk_guard — 风控闸门
   - 正常状态 → OK
   - 保证金破红线 → LOCK
   - 日亏达停机线 → LOCK
   - 保证金接近红线 → WARN
   - 单笔超上限 → WARN
   - 同时触发多个 → 取最严重状态
   - 分品种收紧（低胜率品种）
   - zero equity → 安全处理
   - proposed_margin 计入评估

2. build_flatten_plan — 平仓计划
   - 空持仓 → 空计划
   - 多单 → 卖出平仓
   - 空单 → 买入平仓
   - 混合持仓 → 分别处理
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from risk_state_machine import (
    DAILY_LOSS_STOP,
    RED_LINE,
    SINGLE_LEG,
    WARN_LINE,
    build_flatten_plan,
    risk_guard,
)

# ═══════════════════════════════════════════════════════════════════════════
#  1. risk_guard — 正常状态
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGuardNormal(unittest.TestCase):
    """risk_guard 正常状态。"""

    def test_no_risk_ok_status(self):
        """无风险 → status = OK"""
        result = risk_guard(equity=100000, used_margin=10000, daily_pnl=0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["reasons"]), 0)

    def test_usage_calculation(self):
        """usage = (used + proposed) / equity"""
        result = risk_guard(equity=100000, used_margin=20000, proposed_margin=10000)
        # usage = 30000 / 100000 = 0.30
        self.assertAlmostEqual(result["usage"], 0.30, places=3)

    def test_daily_loss_zero_when_profit(self):
        """盈利时 daily_loss_pct = 0"""
        result = risk_guard(equity=100000, used_margin=10000, daily_pnl=5000)
        self.assertEqual(result["daily_loss_pct"], 0.0)

    def test_daily_loss_pct_calculation(self):
        """日亏比例计算正确"""
        # 日亏 3000，权益 100000 → 3%
        result = risk_guard(equity=100000, used_margin=10000, daily_pnl=-3000)
        self.assertAlmostEqual(result["daily_loss_pct"], 0.03, places=3)

    def test_zero_equity_no_crash(self):
        """equity = 0 → 不崩溃"""
        result = risk_guard(equity=0, used_margin=0, daily_pnl=0)
        self.assertEqual(result["usage"], 0.0)
        self.assertEqual(result["daily_loss_pct"], 0.0)

    def test_negative_equity_no_crash(self):
        """equity < 0 → 不崩溃"""
        result = risk_guard(equity=-1000, used_margin=5000, daily_pnl=-2000)
        # usage 应该是 0（分母 <= 0）
        self.assertEqual(result["usage"], 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. risk_guard — 红线锁定
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGuardRedLine(unittest.TestCase):
    """risk_guard 保证金红线锁定。"""

    def test_above_red_line_locks(self):
        """保证金使用率 >= 红线 → LOCK"""
        # used_margin = 50000, equity = 100000 → 50% > 45% 红线
        result = risk_guard(equity=100000, used_margin=50000)
        self.assertEqual(result["status"], "LOCK")
        self.assertTrue(any("红线" in r for r in result["reasons"]))

    def test_exactly_red_line_locks(self):
        """刚好 = 红线 → LOCK"""
        eq = 100000
        used = eq * RED_LINE  # 刚好 45%
        result = risk_guard(equity=eq, used_margin=used)
        self.assertEqual(result["status"], "LOCK")

    def test_below_red_line_not_locked(self):
        """略低于红线 → 不锁定"""
        eq = 100000
        used = eq * (RED_LINE - 0.01)  # 44%
        result = risk_guard(equity=eq, used_margin=used)
        self.assertNotEqual(result["status"], "LOCK")

    def test_proposed_margin_counts_to_red_line(self):
        """proposed_margin 计入红线评估"""
        eq = 100000
        used = eq * 0.40  # 40% 已用
        proposed = eq * 0.10  # 再加 10% → 50% > 45% 红线
        result = risk_guard(equity=eq, used_margin=used, proposed_margin=proposed)
        self.assertEqual(result["status"], "LOCK")


# ═══════════════════════════════════════════════════════════════════════════
#  3. risk_guard — 日亏停机
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGuardDailyLoss(unittest.TestCase):
    """risk_guard 日亏停机线。"""

    def test_daily_loss_hits_stop(self):
        """日亏 >= 停机线 → LOCK"""
        eq = 100000
        loss = eq * DAILY_LOSS_STOP  # 5% = 5000
        result = risk_guard(equity=eq, used_margin=10000, daily_pnl=-loss)
        self.assertEqual(result["status"], "LOCK")
        self.assertTrue(any("日亏" in r or "亏损" in r for r in result["reasons"]))

    def test_daily_loss_below_stop(self):
        """日亏 < 停机线 → 不锁定"""
        eq = 100000
        loss = eq * (DAILY_LOSS_STOP * 0.5)  # 2.5%
        result = risk_guard(equity=eq, used_margin=10000, daily_pnl=-loss)
        self.assertNotEqual(result["status"], "LOCK")

    def test_daily_profit_no_lock(self):
        """盈利 → 不触发日亏停机"""
        result = risk_guard(equity=100000, used_margin=10000, daily_pnl=10000)
        self.assertNotEqual(result["status"], "LOCK")
        self.assertEqual(result["daily_loss_pct"], 0.0)

    def test_opening_equity_used_for_loss_pct(self):
        """opening_equity 存在时用它做日亏基数"""
        opening = 100000
        current = 95000  # 已经亏了 5%
        loss = 5000  # 日亏 5000
        # 用 opening_equity = 100000 → 日亏 = 5000/100000 = 5% = 停机线
        result = risk_guard(equity=current, used_margin=10000, daily_pnl=-loss, opening_equity=opening)
        self.assertEqual(result["status"], "LOCK")


# ═══════════════════════════════════════════════════════════════════════════
#  4. risk_guard — 预警状态
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGuardWarning(unittest.TestCase):
    """risk_guard 预警状态。"""

    def test_near_red_line_warns(self):
        """保证金 >= 预警线 → WARN"""
        eq = 100000
        used = eq * WARN_LINE  # 40%
        result = risk_guard(equity=eq, used_margin=used)
        self.assertEqual(result["status"], "WARN")

    def test_below_warn_line_ok(self):
        """低于预警线 → OK"""
        eq = 100000
        used = eq * (WARN_LINE - 0.05)  # 35%
        result = risk_guard(equity=eq, used_margin=used)
        self.assertEqual(result["status"], "OK")

    def test_single_leg_over_limit_warns(self):
        """单笔保证金超上限 → WARN"""
        eq = 100000
        proposed = eq * (SINGLE_LEG + 0.05)  # 35% > 30%
        result = risk_guard(equity=eq, used_margin=0, proposed_margin=proposed)
        self.assertEqual(result["status"], "WARN")
        self.assertTrue(any("单笔" in r for r in result["reasons"]))

    def test_single_leg_below_limit_ok(self):
        """单笔在限额内 → 不触发单笔预警"""
        eq = 100000
        proposed = eq * (SINGLE_LEG - 0.05)  # 25% < 30%
        result = risk_guard(equity=eq, used_margin=0, proposed_margin=proposed)
        # 只要不是因为单笔而 WARN
        if result["status"] == "WARN":
            self.assertFalse(any("单笔" in r for r in result["reasons"]))

    def test_lock_takes_priority_over_warn(self):
        """同时触发 LOCK 和 WARN 条件 → LOCK 优先（status 取最严重）"""
        eq = 100000
        # 保证金破红线 → LOCK
        used = eq * 0.50  # 50% > 45% 红线
        proposed = eq * 0.05  # 很小的单笔，不触发单笔警告
        result = risk_guard(equity=eq, used_margin=used, proposed_margin=proposed)
        self.assertEqual(result["status"], "LOCK")
        self.assertTrue(any("红线" in r for r in result["reasons"]))


# ═══════════════════════════════════════════════════════════════════════════
#  5. risk_guard — 分品种覆盖
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGuardPerSymbol(unittest.TestCase):
    """risk_guard 分品种风险覆盖。"""

    def test_no_symbol_no_override(self):
        """不传 symbol → symbol_override = None"""
        result = risk_guard(equity=100000, used_margin=10000)
        self.assertIsNone(result["symbol_override"])

    def test_jm_tightens_single_leg(self):
        """JM（焦煤）→ 收紧单笔上限（低胜率品种）"""
        from risk_state_machine import PER_SYMBOL_RISK

        if "JM" not in PER_SYMBOL_RISK:
            self.skipTest("JM 不在分品种风险表中")

        result = risk_guard(equity=100000, used_margin=10000, symbol="JM")
        ov = result["symbol_override"]
        self.assertIsNotNone(ov)
        # 收紧后的单笔上限应该 <= 默认
        self.assertLessEqual(ov.get("single_leg", 1.0), SINGLE_LEG)

    def test_low_win_symbol_strict_stop(self):
        """低胜率品种 → strict_stop = True"""
        from risk_state_machine import PER_SYMBOL_RISK

        # 找一个有 strict_stop 的品种
        strict_sym = None
        for sym, cfg in PER_SYMBOL_RISK.items():
            if cfg.get("strict_stop"):
                strict_sym = sym
                break
        if not strict_sym:
            self.skipTest("没有 strict_stop 的品种")

        # 构造一个刚好触发单笔警告的场景（需要 OK 状态下才会加 reason）
        eq = 100000
        single_leg = PER_SYMBOL_RISK[strict_sym]["single_leg"]
        # 让 proposed 略超 single_leg，但 used+proposed 还在 warn_line 以下
        proposed = eq * (single_leg + 0.02)
        used = eq * 0.10  # 10% 已用，远低于预警线
        result = risk_guard(equity=eq, used_margin=used, proposed_margin=proposed, symbol=strict_sym)
        self.assertTrue(any("强制止损" in r for r in result["reasons"]))


# ═══════════════════════════════════════════════════════════════════════════
#  6. build_flatten_plan — 平仓计划
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildFlattenPlan(unittest.TestCase):
    """build_flatten_plan — 全平仓计划。"""

    def test_empty_positions_empty_plan(self):
        """空持仓 → 空计划"""
        plan = build_flatten_plan([])
        self.assertEqual(len(plan), 0)

    def test_long_position_sell_close(self):
        """多单 → 卖出平仓"""
        positions = [{"symbol": "rb", "direction": "多", "lots": 5}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["symbol"], "rb")
        self.assertEqual(plan[0]["action"], "平多（卖平）")
        self.assertEqual(plan[0]["side"], 1)
        self.assertEqual(plan[0]["lots"], 5)

    def test_short_position_buy_close(self):
        """空单 → 买入平仓"""
        positions = [{"symbol": "rb", "direction": "空", "lots": 3}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["symbol"], "rb")
        self.assertEqual(plan[0]["action"], "平空（买平）")
        self.assertEqual(plan[0]["side"], -1)
        self.assertEqual(plan[0]["lots"], 3)

    def test_mixed_positions_both_sides(self):
        """混合持仓 → 分别平多和平空"""
        positions = [
            {"symbol": "rb", "direction": "多", "lots": 5},
            {"symbol": "i", "direction": "空", "lots": 2},
        ]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 2)
        # 找 rb 多单 → 卖平
        rb_plan = [p for p in plan if p["symbol"] == "rb"][0]
        self.assertEqual(rb_plan["action"], "平多（卖平）")
        self.assertEqual(rb_plan["lots"], 5)
        # 找 i 空单 → 买平
        i_plan = [p for p in plan if p["symbol"] == "i"][0]
        self.assertEqual(i_plan["action"], "平空（买平）")
        self.assertEqual(i_plan["lots"], 2)

    def test_zero_volume_skipped(self):
        """零持仓 → 跳过"""
        positions = [{"symbol": "rb", "direction": "多", "lots": 0}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  风控状态机 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
