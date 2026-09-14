#!/usr/bin/env python3
"""
账户跟踪器 · 对账口径回归测试
================================

针对 commit 11a14dc「对账口径修复，防止 mtm/累计浮盈混淆导致面板漂移」的回归测试。
覆盖 4 项断言：

1. set_equity 正确把同步价写回 pos["price"]，保证 float_at_sync 与存储价同口径
2. snapshot 优先使用 CTP 同步的 margin_used；无同步值时才用 margin_rate 估算
3. 口径一致时 self_check.ok=True，msg 为「数据自洽」
4. 故意把 float_at_sync 存成 mtm 口径时，触发「[对账漂移]」告警

用 account_context + 临时账户 _drift_test 构造环境，每个 case 独立，跑完清理残留。
直接运行：python -m unittest tests.test_account_tracker_recon
"""

import glob
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import account_tracker as at

ACCOUNT = "_drift_test"
SYM = "FG"  # 玻璃：multiplier=20, margin_rate=0.15（trade_config.json 实测）
MULT = 20
MRATE = 0.15


def _pos(direction="多", lots=2, avg=1000.0, price=None, margin_used=None):
    """构造单个持仓 dict。margin_used=None 表示未写入（模拟无 CTP 同步值）。"""
    p = {"direction": direction, "lots": lots, "avg": avg, "price": price}
    if margin_used is not None:
        p["margin_used"] = margin_used
    return p


class AccountTrackerReconTest(unittest.TestCase):
    """对账口径回归测试（临时账户，互不污染）。"""

    def _base_state(self, float_at_sync=0.0, **pos_kwargs):
        """构造干净的账户状态，positions 只含一个 FG 持仓。"""
        return {
            "equity": 100000.0,
            "realized_pnl": 0.0,
            "realized_pnl_at_sync": 0.0,
            "float_at_sync": float_at_sync,
            "positions": {SYM: _pos(**pos_kwargs)},
        }

    def _snapshot(self, prices):
        """屏蔽实时行情源，让 snapshot 确定性回退到传入的 prices。"""
        with mock.patch.object(at, "_get_ak_price", return_value=None), \
             mock.patch.object(at, "_get_ms_price", return_value=None):
            return at.snapshot(prices)

    def _find(self, snap, sym):
        """从 snapshot 返回的 positions 列表里按 symbol 找持仓。"""
        for p in snap["positions"]:
            if p["symbol"] == sym:
                return p
        self.fail(f"snapshot positions 未找到 symbol={sym}")

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        """清理临时账户的 state / journal 残留（含 .bak）。"""
        patterns = [
            at.state_file_for(ACCOUNT) + "*",
            os.path.join(ROOT, f"trade_journal_{ACCOUNT}.json*"),
        ]
        for pat in patterns:
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ─────────────────────────────────────────────────────────────────────
    # 1. set_equity 写回 price
    # ─────────────────────────────────────────────────────────────────────
    def test_set_equity_writes_price_back(self):
        """set_equity 把同步价写回 pos["price"]，且 float_at_sync 与存储价同口径。"""
        with at.account_context(ACCOUNT):
            at.save_state(self._base_state(price=None))
            ok, msg, _st = at.set_equity(100000.0, prices={SYM: 1100.0})
            self.assertTrue(ok, f"set_equity 应返回成功: msg={msg}")

            st = at.load_state()
            # 存储价被写回同步价
            self.assertEqual(st["positions"][SYM]["price"], 1100.0)
            # float_at_sync = Σ(price - avg) × mult × lots × 方向 = (1100-1000)*20*2*1
            self.assertEqual(st["float_at_sync"], 4000.0)

    # ─────────────────────────────────────────────────────────────────────
    # 2. snapshot 优先 CTP margin_used，无值才 margin_rate 估算
    # ─────────────────────────────────────────────────────────────────────
    def test_snapshot_prefers_ctp_margin_used(self):
        """有 CTP 同步 margin_used 时优先用；无同步值才回退 margin_rate 估算。"""
        with at.account_context(ACCOUNT):
            # 有同步值：50000（与估算 6000 明显不同）
            at.save_state(self._base_state(price=1000.0, margin_used=50000.0))
            snap = self._snapshot({SYM: 1000.0})
            self.assertEqual(self._find(snap, SYM)["margin_used"], 50000.0)

            # 无同步值：回退 margin_rate 估算 = lots*avg*mult*mrate = 2*1000*20*0.15
            at.save_state(self._base_state(price=1000.0))
            snap2 = self._snapshot({SYM: 1000.0})
            self.assertEqual(self._find(snap2, SYM)["margin_used"], 6000.0)

    # ─────────────────────────────────────────────────────────────────────
    # 3. 口径一致 → self_check.ok=True，msg「数据自洽」
    # ─────────────────────────────────────────────────────────────────────
    def test_self_check_ok_when_consistent(self):
        """float_at_sync 与存储价口径一致时，self_check.ok=True 且 msg 为「数据自洽」。"""
        with at.account_context(ACCOUNT):
            at.save_state(self._base_state(float_at_sync=4000.0, price=1100.0))
            snap = self._snapshot({SYM: 1100.0})
            self.assertTrue(snap["self_check"]["ok"])
            self.assertEqual(snap["self_check"]["msg"], "数据自洽")

    # ─────────────────────────────────────────────────────────────────────
    # 4. 故意存成 mtm 口径 → 触发 [对账漂移]
    # ─────────────────────────────────────────────────────────────────────
    def test_float_at_sync_mtm_triggers_recon_drift(self):
        """float_at_sync 被误存成盯市(mtm)口径时，触发 [对账漂移] 告警。"""
        with at.account_context(ACCOUNT):
            # 存储 price=1100 → 持仓累计浮盈和应为 4000，却故意存成 5300（mtm 口径）
            at.save_state(self._base_state(float_at_sync=5300.0, price=1100.0))
            snap = self._snapshot({SYM: 1100.0})
            self.assertIn("[对账漂移]", snap["self_check"]["msg"])


if __name__ == "__main__":
    print("=" * 60)
    print("  账户跟踪器 · 对账口径回归测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
