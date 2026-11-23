"""account_ssot 单元测试 —— 双向故障注入版

判据与被测对象同源：所有断言都直接算期望值，不引用模块内部常量。
运行：python3 -m unittest test_account_ssot -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_ssot as ssot  # noqa: E402

SPECS = {"PK": {"multiplier": 5}, "al": {"multiplier": 5}, "au": {"multiplier": 1000},
         "ni": {"multiplier": 1}}


def _state(cash, positions):
    return {"cash": cash, "positions": positions, "last_updated": "2026-11-06 10:00:00"}


def _pos(sym, direction, lots, entry, last=None):
    d = {"symbol": sym, "direction": direction, "lots": lots, "entry_price": entry}
    if last is not None:
        d["last_price"] = last
    return d


class TestPaperEquityMath(unittest.TestCase):
    def test_short_gains_when_price_falls(self):
        # PK 空 2 手 @7770，乘数 5，现价 7000 → (7000-7770)*-1*2*5 = +7700
        st = _state(1_000_000, {"PK": _pos("PK", "空", 2, 7770.0, 7000.0)})
        eq, legs, _ = ssot.paper_equity(st, SPECS)
        self.assertAlmostEqual(legs[0].mtm, 7700.0)
        self.assertAlmostEqual(eq, 1_007_700.0)

    def test_short_loses_when_price_rises(self):
        st = _state(1_000_000, {"PK": _pos("PK", "空", 2, 7770.0, 7913.16)})
        eq, legs, _ = ssot.paper_equity(st, SPECS)
        self.assertAlmostEqual(legs[0].mtm, -1431.6, places=2)

    def test_long_gains_when_price_rises(self):
        st = _state(500_000, {"al": _pos("al", "多", 1, 23000.0, 23200.0)})
        eq, _, _ = ssot.paper_equity(st, SPECS)
        self.assertAlmostEqual(eq, 501_000.0)

    def test_multiplier_matters_100x(self):
        # au 乘数 1000，若被默认成 10 就会错 100 倍 —— 这条就是防它
        st = _state(1_000_000, {"au": _pos("au", "多", 1, 600.0, 601.0)})
        eq, _, _ = ssot.paper_equity(st, SPECS)
        self.assertAlmostEqual(eq, 1_001_000.0)

    def test_unknown_multiplier_raises_not_defaults(self):
        st = _state(1_000_000, {"zz": _pos("zz", "多", 1, 100.0, 101.0)})
        with self.assertRaises(ssot.UnknownMultiplier):
            ssot.paper_equity(st, SPECS, journal_mult={"PK": 5})

    def test_journal_multiplier_fallback(self):
        st = _state(1_000_000, {"PK": _pos("PK", "空", 1, 7770.0, 7000.0)})
        eq, legs, _ = ssot.paper_equity(st, {}, journal_mult={"PK": 5.0})
        self.assertAlmostEqual(legs[0].mtm, 3850.0)

    def test_remaining_lots_preferred_over_lots(self):
        st = _state(1_000_000, {"PK": {"symbol": "PK", "direction": "空", "lots": 2,
                                       "remaining_lots": 1, "entry_price": 7770.0,
                                       "last_price": 7000.0}})
        _, legs, _ = ssot.paper_equity(st, SPECS)
        self.assertAlmostEqual(legs[0].lots, 1.0)
        self.assertAlmostEqual(legs[0].mtm, 3850.0)

    def test_zero_lot_skipped(self):
        st = _state(1_000_000, {"PK": _pos("PK", "空", 0, 7770.0, 7000.0)})
        eq, legs, _ = ssot.paper_equity(st, SPECS)
        self.assertEqual(len(legs), 0)
        self.assertAlmostEqual(eq, 1_000_000.0)


class TestPaperEquityFailureModes(unittest.TestCase):
    def test_unpriced_leg_returns_none(self):
        st = _state(1_000_000, {"PK": _pos("PK", "空", 2, 7770.0)})  # 无现价无 last
        eq, legs, warns = ssot.paper_equity(st, SPECS)
        self.assertIsNone(eq)
        self.assertTrue(any("取不到现价" in w for w in warns))

    def test_missing_cash_returns_none(self):
        eq, _, warns = ssot.paper_equity({"positions": {}}, SPECS)
        self.assertIsNone(eq)
        self.assertTrue(warns)

    def test_bad_direction_skipped_with_warning(self):
        st = _state(1_000_000, {"PK": _pos("PK", "?", 2, 7770.0, 7000.0)})
        eq, legs, warns = ssot.paper_equity(st, SPECS)
        self.assertEqual(len(legs), 0)
        self.assertAlmostEqual(eq, 1_000_000.0)
        self.assertTrue(any("方向无法识别" in w for w in warns))

    def test_price_fn_overrides_stale_last_price(self):
        st = _state(1_000_000, {"PK": _pos("PK", "空", 1, 7770.0, 7000.0)})
        eq, _, warns = ssot.paper_equity(st, SPECS, price_fn=lambda s: 7000.0)
        self.assertAlmostEqual(eq, 1_003_850.0)
        self.assertFalse(any("退回落盘" in w for w in warns))


class TestResolvePaper(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "trade_config.json"), "w", encoding="utf-8") as f:
            json.dump({"contract_specs": SPECS}, f)

    def _write_state(self, cash=1_000_000, positions=None, age=0.0):
        p = os.path.join(self.d, "paper_trading_state.json")
        if positions is None:
            positions = {"PK": _pos("PK", "空", 2, 7770.0, 7000.0)}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_state(cash, positions), f)
        t = time.time() - age
        os.utime(p, (t, t))

    def test_ok_path(self):
        self._write_state()
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600,
                         price_fn=lambda s: 7000.0)
        self.assertTrue(r.ok)
        self.assertEqual(r.source, ssot.SOURCE_PAPER_LEDGER)
        self.assertAlmostEqual(r.equity, 1_007_700.0)

    def test_no_price_source_fails(self):
        """没给现价源 ⇒ 不许用 last_price 冒充活权益。"""
        self._write_state()
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600)
        self.assertFalse(r.ok)
        self.assertIn("未提供实时价源", r.detail)

    def test_allow_stale_prices_optin(self):
        self._write_state()
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600,
                         allow_stale_prices=True)
        self.assertTrue(r.ok)
        self.assertTrue(any("退回落盘" in w for w in r.warnings))

    def test_missing_file_fails(self):
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600,
                         price_fn=lambda s: 7000.0)
        self.assertFalse(r.ok)
        self.assertIsNone(r.equity)

    def test_stale_file_reported_not_fatal(self):
        """账本 mtime 只代表「上次成交」，不代表价格新鲜 —— 不该硬失败。"""
        self._write_state(age=900)
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=180,
                         price_fn=lambda s: 7000.0)
        self.assertTrue(r.ok)
        self.assertGreater(r.age_sec or 0, 800)
        self.assertIn("落盘", r.detail)

    def test_corrupt_json_fails(self):
        with open(os.path.join(self.d, "paper_trading_state.json"), "w") as f:
            f.write("{not json")
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600, allow_stale_prices=True)
        self.assertFalse(r.ok)

    def test_zero_equity_fails(self):
        self._write_state(cash=0.0, positions={})
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600, allow_stale_prices=True)
        self.assertFalse(r.ok)
        self.assertIn("≤ 0", r.detail)

    def test_below_floor_fails(self):
        self._write_state(cash=500.0, positions={})
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600, floor=10_000,
                         allow_stale_prices=True)
        self.assertFalse(r.ok)
        self.assertIn("下限", r.detail)

    def test_cap_truncates(self):
        self._write_state(cash=9_000_000, positions={})
        r = ssot.resolve(mode="paper", base_dir=self.d, max_age=600, cap=2_000_000,
                         allow_stale_prices=True)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.equity, 2_000_000)


class TestResolveLive(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_no_source_never_returns_static(self):
        """核心断言：三级全空时绝不能返回 1000000 之类的常量。"""
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600)
        self.assertFalse(r.ok)
        self.assertEqual(r.source, "no_source")
        self.assertIsNone(r.equity)

    def test_zeroed_account_state_still_fails(self):
        """复刻现状：equity_live=0, equity=0（正是今天 account_state.json 的样子）。"""
        with open(os.path.join(self.d, "account_state.json"), "w") as f:
            json.dump({"equity": 0.0, "equity_live": 0.0, "positions": {}}, f)
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600)
        self.assertFalse(r.ok)
        self.assertIsNone(r.equity)

    def test_equity_live_preferred(self):
        with open(os.path.join(self.d, "account_state.json"), "w") as f:
            json.dump({"equity": 111.0, "equity_live": 993_685.0}, f)
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600)
        self.assertTrue(r.ok)
        self.assertEqual(r.source, ssot.SOURCE_LIVE_SNAPSHOT)
        self.assertAlmostEqual(r.equity, 993_685.0)

    def test_manual_anchor_fallback(self):
        with open(os.path.join(self.d, "account_state.json"), "w") as f:
            json.dump({"equity": 984_415.0, "equity_live": 0.0}, f)
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600)
        self.assertTrue(r.ok)
        self.assertEqual(r.source, ssot.SOURCE_MANUAL_ANCHOR)

    def test_broker_reader_wins(self):
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600,
                         live_reader=lambda: {"balance": 1_234_567.0})
        self.assertTrue(r.ok)
        self.assertEqual(r.source, ssot.SOURCE_LIVE_BROKER)
        self.assertAlmostEqual(r.equity, 1_234_567.0)

    def test_broker_reader_raises_falls_through(self):
        def boom():
            raise RuntimeError("ctp 未连接")
        r = ssot.resolve(mode="live", base_dir=self.d, max_age=600, live_reader=boom)
        self.assertFalse(r.ok)

    def test_static_not_in_valid_sources(self):
        """source 枚举里不许出现 static —— 从类型层面封死静态兜底。"""
        self.assertNotIn("static", " ".join(ssot.VALID_SOURCES).lower())


class TestDirSign(unittest.TestCase):
    def test_map(self):
        for v in ("多", "long", "buy", 1):
            self.assertEqual(ssot.dir_sign(v), 1, v)
        for v in ("空", "short", "sell", -1):
            self.assertEqual(ssot.dir_sign(v), -1, v)
        for v in (None, "", "?", 0):
            self.assertEqual(ssot.dir_sign(v), 0, v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
