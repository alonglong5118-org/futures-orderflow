#!/usr/bin/env python3
"""KillSwitch 硬熔断 + 跨日重置 回归测试（P0-1/P0-2/P1-1~P1-4）

覆盖真实写路径（落盘/重载/多账户隔离），防止「只改内存 dict / 只断言纯函数」的假绿。

对应修复：
  P0-1  force_halt 真正置 halted 并落盘 → is_locked/is_halted 拦截新开仓
  P0-2  reset_daily 跨日解除连亏锁/日亏锁；set_opening_equity 重定日初锚点
  P1-1  kill_switch.auto_recover 门控（默认不自动恢复）
  P1-2  check() 触发时写 orig_* 快照
  P1-3  _maybe_recover 冷却日志修复
  P1-4  reset_daily_if_new_day 多账户隔离
"""

import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import risk_state_machine as rsm
from risk_state_machine import KillSwitch, get_kill, is_halted, is_locked


def _tmp_kill():
    """返回指向临时状态文件的独立 KillSwitch（不污染真实熔断态）。"""
    fd, path = tempfile.mkstemp(prefix="killswitch_test_", suffix=".json")
    os.close(fd)
    os.remove(path)
    return KillSwitch(path=path), path


class TestForceHalt(unittest.TestCase):
    def test_force_halt_locks_and_persists(self):
        k, path = _tmp_kill()
        try:
            s = k.force_halt(
                "账户回撤10.5%≥10%，强制全平+休息24小时",
                positions=[{"symbol": "FG", "name": "玻璃", "direction": "多", "lots": 3, "price": 1200}],
                force_rest_until=time.time() + 86400,
            )
            self.assertTrue(s["halted"], "force_halt 应置 halted=True")
            self.assertEqual(len(s["flatten_plan"]), 1)
            self.assertIn("force_rest_until", s)
            # 落盘重载：进程重启后仍熔断
            k2 = KillSwitch(path=path)
            self.assertTrue(k2.halted, "落盘重载后应仍为熔断态")
            self.assertIsNotNone(k2.force_rest_until)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_force_halt_blocks_new_positions_via_module(self):
        aid = "tst_forcehalt_block"
        k = get_kill(aid)
        if os.path.exists(k.path):
            os.remove(k.path)
        k = get_kill(aid)
        try:
            k.force_halt("回撤熔断", force_rest_until=time.time() + 86400)
            self.assertTrue(is_locked(aid), "force_halt 后 is_locked 应 True")
            self.assertTrue(is_halted(aid), "force_halt 后 is_halted 应 True")
        finally:
            if os.path.exists(k.path):
                os.remove(k.path)


class TestCheckOrigMetrics(unittest.TestCase):
    def test_check_records_orig_metrics_on_kill(self):
        k, path = _tmp_kill()
        try:
            r = k.check(840000, peak_equity=1000000, daily_pnl=-20000, consec_losses=0)
            self.assertTrue(r["halted"])
            m = r["metrics"]
            self.assertEqual(m.get("orig_daily_pnl_at_kill"), -20000.0)
            self.assertEqual(m.get("orig_equity_at_kill"), 840000.0)
            self.assertAlmostEqual(m.get("orig_drawdown_at_kill"), 0.16, places=4)
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestAutoRecoverGate(unittest.TestCase):
    def test_no_auto_recover_by_default(self):
        old = rsm.KILL_AUTO_RECOVER
        rsm.KILL_AUTO_RECOVER = False
        k, path = _tmp_kill()
        try:
            k.check(840000, peak_equity=1000000, daily_pnl=-20000, consec_losses=0)
            self.assertTrue(k.halted)
            k.ack = True
            # 条件已解除，但 auto_recover=False → 不应自动恢复
            r2 = k.check(990000, peak_equity=1000000, daily_pnl=0, consec_losses=0)
            self.assertTrue(r2["halted"], "auto_recover=False 时熔断不应自动恢复")
        finally:
            rsm.KILL_AUTO_RECOVER = old
            if os.path.exists(path):
                os.remove(path)

    def test_recover_logs_sane_cooldown(self):
        old = rsm.KILL_AUTO_RECOVER
        rsm.KILL_AUTO_RECOVER = True
        k, path = _tmp_kill()
        try:
            k.check(840000, peak_equity=1000000, daily_pnl=-20000, consec_losses=0)
            k.ack = True
            k.triggered_at = time.time() - 7200
            k._maybe_recover(990000, 1000000, 0, 0)
            self.assertFalse(k.halted)
            note = k.history[-1]["note"]
            self.assertIn("7200", note, "冷却日志应记录合理秒数而非 now-0")
        finally:
            rsm.KILL_AUTO_RECOVER = old
            if os.path.exists(path):
                os.remove(path)


class TestResetDailyIsolation(unittest.TestCase):
    def test_reset_daily_if_new_day_isolates_account(self):
        import account_tracker as at

        aid_a = "tst_resetdaily_a"
        aid_b = "tst_resetdaily_b"
        sf_a = at.state_file_for(aid_a)
        sf_b = at.state_file_for(aid_b)
        ka = get_kill(aid_a)
        kb = get_kill(aid_b)
        try:
            # 账户 A 写 equity=123456，账户 B 保持空
            with at.account_context(aid_a):
                pass
            if os.path.exists(sf_a):
                with open(sf_a, encoding="utf-8") as f:
                    d = json.load(f)
            else:
                d = {}
            d["equity"] = 123456.0
            with open(sf_a, "w", encoding="utf-8") as f:
                json.dump(d, f)
            ka._opening_equity = None
            ka._save()
            kb._opening_equity = None
            kb._save()

            rsm.reset_daily_if_new_day("2020-01-01", account_id=aid_a)

            self.assertEqual(get_kill(aid_a)._opening_equity, 123456.0, "A 账户锚点应更新")
            self.assertIsNone(get_kill(aid_b)._opening_equity, "B 账户锚点不应被污染")
        finally:
            for sf in (sf_a, sf_b):
                if os.path.exists(sf):
                    os.remove(sf)
            for k in (ka, kb):
                if os.path.exists(k.path):
                    os.remove(k.path)


class TestResetDailyClearsLocks(unittest.TestCase):
    def test_reset_daily_clears_both_locks(self):
        aid = "tst_resetdaily_clear"
        f = rsm.get_fsm(aid)
        f.consec_losses = 2
        f.consec_lock = True
        f.daily_loss_locked = True
        f.state = rsm.RiskStateMachine.LOCKED
        f.reset_daily()
        self.assertEqual(f.consec_losses, 0)
        self.assertFalse(f.consec_lock)
        self.assertFalse(f.daily_loss_locked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
