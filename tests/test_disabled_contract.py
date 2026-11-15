#!/usr/bin/env python3
"""短路原因契约 — 单元测试
================================

对应 `disabled_contract.py`。校验「DISABLED_SYMBOLS 的每个条目都有**可核查原因**」这条契约。

1. 正向：真实配置下 validate() 无违规；登记与 DISABLED_SYMBOLS / AUTO_RECOVER_SYMBOLS 双向一致。
2. 反测（**保证校验器不是空壳**）：注入「缺登记 / 非法码 / 禁止措辞 / 恢复标志不一致」四类
   假输入，断言校验器**确实能抓到**。
"""

import contextlib
import copy
import io
import os
import sys
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import disabled_contract as dc  # noqa: E402

# 生产配置可能因**本地环境缺依赖**（如裸 python3 无 numpy）而无法导入。
# 这与 disabled_contract.py 的「降级档」同理：环境不全时**显式跳过**（skip），
# 而不是让整个测试模块 import 失败 —— 那会把本地提交钩子无端卡红（= 狼来了）。
# CI 侧依赖齐全 → LIVE=True → 下列需要生产配置的用例**全部真跑**。
try:
    from four_dim_strategy import AUTO_RECOVER_SYMBOLS, DISABLED_SYMBOLS  # noqa: E402

    LIVE = True
    LIVE_ERR = ""
except Exception as _exc:  # pragma: no cover - 取决于运行环境
    AUTO_RECOVER_SYMBOLS = set()
    DISABLED_SYMBOLS = set()
    LIVE = False
    LIVE_ERR = f"{type(_exc).__name__}: {_exc}"

_SKIP_LIVE = f"four_dim_strategy 不可导入（本地缺依赖；CI 有依赖会真跑）：{LIVE_ERR}"


def _self_consistent_sets():
    """用登记表**自身**构造「内部自洽」的（被禁集, 可恢复集）。

    反测只需要一个「基线不产生违规」的输入来注入坏值，**不需要**生产配置 ——
    这样反测在任何环境（含本地无依赖）都能真跑，不会退化成空壳。
    """
    disabled = set(dc.DISABLED_REASONS)
    auto = {s for s, r in dc.DISABLED_REASONS.items() if r.get("auto_recover")}
    return disabled, auto


class TestContractPositive(unittest.TestCase):
    """① 正向：真实配置应合规。"""

    def test_real_config_passes(self):
        self.assertEqual(dc.validate(), [], "真实配置下契约应无违规")

    @unittest.skipUnless(LIVE, _SKIP_LIVE)
    def test_every_disabled_symbol_has_reason(self):
        missing = set(DISABLED_SYMBOLS) - set(dc.DISABLED_REASONS)
        self.assertEqual(missing, set(), f"这些被禁品种缺原因登记：{sorted(missing)}")

    @unittest.skipUnless(LIVE, _SKIP_LIVE)
    def test_no_orphan_registry_entries(self):
        orphan = set(dc.DISABLED_REASONS) - set(DISABLED_SYMBOLS)
        self.assertEqual(orphan, set(), f"登记了但并未被禁（孤儿）：{sorted(orphan)}")

    def test_all_codes_allowed(self):
        for sym, rec in dc.DISABLED_REASONS.items():
            self.assertIn(rec["code"], dc.ALLOWED_REASON_CODES, f"{sym} 原因码非法")

    def test_no_forbidden_wording_in_evidence(self):
        for sym, rec in dc.DISABLED_REASONS.items():
            for pat in dc.FORBIDDEN_REASON_PATTERNS:
                self.assertNotIn(pat, rec["evidence"], f"{sym} 证据含禁止措辞「{pat}」")


class TestValidatorCatchesErrors(unittest.TestCase):
    """② 反测：校验器必须真的能抓到违规（否则是空壳）。"""

    def setUp(self):
        self._backup = copy.deepcopy(dc.DISABLED_REASONS)
        # 用登记表自身构造自洽基线（不依赖生产配置），供反测注入坏值
        self._self = _self_consistent_sets()

    def tearDown(self):
        dc.DISABLED_REASONS.clear()
        dc.DISABLED_REASONS.update(self._backup)

    def test_catches_missing_reason(self):
        v = dc.validate({"zzz_fake"}, set())
        self.assertTrue(any("缺登记" in x for x in v), f"应抓到缺登记，实际={v}")

    def test_catches_illegal_code(self):
        dc.DISABLED_REASONS["a"]["code"] = "FEELS_BAD"
        v = dc.validate(*self._self)
        self.assertTrue(any("非法原因码" in x for x in v), f"应抓到非法码，实际={v}")

    def test_catches_forbidden_wording(self):
        dc.DISABLED_REASONS["a"]["evidence"] = "结果不好"
        v = dc.validate(*self._self)
        self.assertTrue(any("禁止措辞" in x for x in v), f"应抓到禁止措辞，实际={v}")

    def test_catches_auto_recover_mismatch(self):
        # hc 真实可自适应恢复；把它标成不可恢复 → 应报不一致
        dc.DISABLED_REASONS["hc"]["auto_recover"] = False
        v = dc.validate(*self._self)
        self.assertTrue(any("恢复标志不一致" in x for x in v), f"应抓到恢复标志不一致，实际={v}")

    def test_catches_orphan(self):
        dc.DISABLED_REASONS["ghost"] = {
            "code": "BLACKLIST",
            "evidence": "测试用",
            "since": "2026-01-01",
            "auto_recover": False,
        }
        v = dc.validate(*self._self)
        self.assertTrue(any("孤儿" in x for x in v), f"应抓到孤儿，实际={v}")


class TestDegradeAndStrict(unittest.TestCase):
    """③ 降级 / 严格模式：治理门不许「静默降级成绿」，也不许在降级时假阳性。"""

    def test_live_import_available(self):
        """**有依赖的环境（CI）必须能导入** four_dim_strategy —— 缺依赖即失败，不许静默成绿。

        本地无依赖（裸 python3）时 **显式 skip**：这不是「代码坏」而是「环境不全」，
        与 disabled_contract.py 的降级档同理 —— 显式声明，不伪装成通过。
        """
        if not LIVE:
            self.skipTest(f"本地环境缺依赖，跳过（CI 侧有依赖会真验证）：{LIVE_ERR}")
        d, a = dc.load_live_sets()
        self.assertIsNotNone(d, "依赖齐全却仍无法导入 four_dim_strategy：模块被改名或路径错")

    def test_degraded_does_not_false_positive(self):
        """降级（读不到生产配置）时不得产生假阳性违规。"""
        with mock.patch.object(dc, "load_live_sets", return_value=(None, None)):
            v = dc.validate()
        self.assertEqual(v, [], f"降级档应只做内部一致性校验、不得假阳性，实际={v}")
        # 关键回归：hc 登记 auto_recover=True，若把「空集合」当真会误报「恢复标志不一致」
        self.assertFalse(any("恢复标志不一致" in x for x in v), "降级时不得判恢复标志不一致（那是对空集合比对）")

    def test_degraded_still_catches_internal_error(self):
        """降级仍是**真**校验：登记表内部一致性坏了必须能抓到。"""
        backup = copy.deepcopy(dc.DISABLED_REASONS)
        try:
            dc.DISABLED_REASONS["a"]["code"] = "FEELS_BAD"
            with mock.patch.object(dc, "load_live_sets", return_value=(None, None)):
                v = dc.validate()
            self.assertTrue(any("非法原因码" in x for x in v), f"降级档也要抓内部错误，实际={v}")
        finally:
            dc.DISABLED_REASONS.clear()
            dc.DISABLED_REASONS.update(backup)

    def test_require_live_fails_when_degraded(self):
        """--require-live + 降级 → exit 非零（治理门不许静默跳过对账）。"""
        with (
            mock.patch.object(dc, "load_live_sets", return_value=(None, None)),
            mock.patch.object(sys, "argv", ["disabled_contract.py", "--require-live"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = dc.main()
        self.assertEqual(rc, 1, "--require-live 下降级应判违规")

    @unittest.skipUnless(LIVE, _SKIP_LIVE)
    def test_require_live_passes_when_available(self):
        """--require-live + 能对账 → exit 0。"""
        with (
            mock.patch.object(sys, "argv", ["disabled_contract.py", "--require-live"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = dc.main()
        self.assertEqual(rc, 0)

    def test_plain_degrade_exits_zero(self):
        """不带 --require-live 时，降级 + 内部一致 → exit 0（本地缺依赖时友好，不误伤）。"""
        with (
            mock.patch.object(dc, "load_live_sets", return_value=(None, None)),
            mock.patch.object(sys, "argv", ["disabled_contract.py"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = dc.main()
        self.assertEqual(rc, 0, "普通降级（内部一致）应 exit 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
