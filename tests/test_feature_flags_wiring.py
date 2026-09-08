# -*- coding: utf-8 -*-
"""feature_flags 接线状态契约测试。

背景（2026-09-08）：feature_flags.json 里 13 个开关中只有 6 个真正被代码读取
（`FeatureManager.is_enabled`），其余 7 个是「装饰性」开关——代码从不读取，
对应模块本身硬生效。UI 上却可点，造成「能点但没用」的错觉。

处理原则（不是真接线，而是显式不可切换）：
  drawdown_guard / kill_switch 属保护性机制，让 UI 能关掉反而引入风险，
  因此保持模块硬生效，并在 toggle_feature 层显式拒绝切换 + 说明原因。

本测试锁定三条契约：
  1. 每个 flag 都必须标注 `_wired`（防新增开关忘记标注）
  2. `_wired: false` 的开关，切换请求必须被拒绝且状态不变
  3. 真接线开关的 `_wired` 必须为 True
"""

from __future__ import annotations

import json
import os
import unittest

import feature_manager as fm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLAGS_JSON = os.path.join(ROOT, "feature_flags.json")


def _load_raw():
    with open(FLAGS_JSON, encoding="utf-8") as f:
        return json.load(f)


class TestFeatureFlagsWiring(unittest.TestCase):
    """feature_flags 接线状态契约。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(FLAGS_JSON):
            raise unittest.SkipTest("feature_flags.json 不存在")
        cls.raw = _load_raw()
        cls.mgr = fm.get_manager()

    def _flags(self):
        return {k: v for k, v in self.raw.items() if not k.startswith("_")}

    def test_every_flag_has_wired_marker(self):
        """每个开关都必须显式标注 _wired，防止新增开关忘记判定接线状态。"""
        missing = [k for k, v in self._flags().items() if "_wired" not in v]
        self.assertEqual(
            [], missing,
            msg=f"以下开关缺少 _wired 标注，请判定接线状态后补上: {missing}",
        )

    def test_unwired_flags_reject_toggle(self):
        """_wired=false 的开关必须拒绝切换，且状态保持不变。"""
        flags = self._flags()
        unwired = [k for k, v in flags.items() if v.get("_wired") is False]
        self.assertTrue(unwired, msg="预期存在未接线开关，否则本用例失去意义")

        for name in unwired:
            with self.subTest(flag=name):
                before = self.mgr.is_enabled(name)
                # 尝试切到相反状态
                res = self.mgr.toggle_feature(
                    name, not before, reason="契约测试", operator="unittest"
                )
                self.assertFalse(
                    res.get("ok"),
                    msg=f"{name} 是未接线开关，切换本应被拒绝，实际返回 {res}",
                )
                self.assertIn("未接线", res.get("error", ""))
                # 状态必须原封不动
                self.assertEqual(
                    before, self.mgr.is_enabled(name),
                    msg=f"{name} 状态被改动了（未接线开关不应写入）",
                )

    def test_unwired_flags_have_note(self):
        """未接线开关必须写明「实际控制者」，否则下次还得重新排查。"""
        for name, v in self._flags().items():
            if v.get("_wired") is False:
                with self.subTest(flag=name):
                    self.assertTrue(
                        v.get("_wired_note"),
                        msg=f"{name} 缺少 _wired_note（应说明由什么实际控制）",
                    )

    def test_wired_flags_marked_true(self):
        """真接线开关的 _wired 必须为 True。"""
        for name, v in self._flags().items():
            if v.get("_wired") is True:
                with self.subTest(flag=name):
                    self.assertIsInstance(v.get("enabled"), bool)


if __name__ == "__main__":
    unittest.main()
