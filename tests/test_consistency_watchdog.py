#!/usr/bin/env python3
"""
consistency_watchdog — 单元测试
================================

覆盖场景：
1. 训练/服务偏离检测（divergence）
   - 偏离 > 35% → 标记 needs_revalidation
   - 偏离 <= 35% → 不报警
   - 近期重校（7 天内）→ 即使偏离也不报（grace period）
   - base_T 为 0 → 不除零报错
   - base_T 为 None → 跳过
2. 未校验检测（unvalidated）
   - calib 有条目但缺 mean_oos → 报警
   - 不在 calib 中（纯默认）→ 不报警
3. 漂移失效检测（broken_serving / broken_gated）
   - broken + 未禁用 + 未门控 → broken_serving（计入 ok=false）
   - broken + 未禁用 + 已门控 → broken_gated（不计入 ok=false）
   - broken + 已禁用 → 不报警
   - 非 broken → 不报警
4. 陈旧重校检测（stale）
   - recalibrated_at 超过 30 天 → 标记 stale
   - 30 天内 → 不报警
   - 日期格式非法 → 不崩溃
5. 综合场景
   - 全部正常 → ok=True
   - 有问题 → ok=False
   - focus_symbols / disabled_set 参数生效
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import consistency_watchdog as cw

# ═══════════════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════════════


def _days_ago_str(days):
    """返回 N 天前的日期字符串（格式匹配模块期望）。"""
    dt = datetime.now() - timedelta(days=days)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _make_fd_mock(thresholds):
    """构造 mock 的 four_dim_strategy 模块。"""
    mock_fd = MagicMock()
    mock_fd.DEFAULT_CONFIG = {"thresholds_by_symbol": thresholds}
    return mock_fd


def _run_check(thresholds, calib, drift_items=None, focus=None, disabled=None):
    """
    辅助：用 mock 运行 check_consistency。
    通过 patch sys.modules 注入 four_dim_strategy，同时 patch 两个 load 函数。
    """
    mock_fd = _make_fd_mock(thresholds)
    drift_data = {"items": drift_items or []}

    with (
        patch.dict(sys.modules, {"four_dim_strategy": mock_fd}),
        patch.object(cw, "_load_calib", return_value=calib),
        patch.object(cw, "_load_drift", return_value=drift_data),
    ):
        return cw.check_consistency(
            focus_symbols=focus or list(thresholds.keys()),
            disabled_set=disabled or set(),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  偏离检测
# ═══════════════════════════════════════════════════════════════════════════


class TestDivergenceDetection(unittest.TestCase):
    """训练/服务偏离检测测试。"""

    def test_no_divergence_within_threshold(self):
        """偏离 <= 35% → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.5, "mean_oos": 1.5}},  # 偏离 25%
        )
        self.assertEqual(len(result["divergences"]), 0)
        self.assertTrue(result["ok"])

    def test_divergence_exceeds_threshold(self):
        """偏离 > 35% → 标记 needs_revalidation"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 3.0, "mean_oos": 1.5}},  # 偏离 50%
        )
        self.assertEqual(len(result["divergences"]), 1)
        self.assertTrue(result["divergences"][0]["needs_revalidation"])
        self.assertAlmostEqual(result["divergences"][0]["baseline_T"], 2.0)
        self.assertAlmostEqual(result["divergences"][0]["served_T"], 3.0)
        self.assertGreater(result["divergences"][0]["deviation_pct"], 35)
        self.assertFalse(result["ok"])

    def test_recently_recalibrated_skip_divergence(self):
        """近期重校（7 天内）→ 即使偏离也不报（grace period）"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {
                "RB": {
                    "T_thresh": 3.0,  # 偏离 50%
                    "mean_oos": 1.5,
                    "recalibrated_at": _days_ago_str(3),  # 3 天前重校
                }
            },
        )
        self.assertEqual(len(result["divergences"]), 0)

    def test_old_recalibration_still_reports_divergence(self):
        """重校超过 7 天 → 正常报偏离"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {
                "RB": {
                    "T_thresh": 3.0,
                    "mean_oos": 1.5,
                    "recalibrated_at": _days_ago_str(10),  # 10 天前
                }
            },
        )
        self.assertEqual(len(result["divergences"]), 1)

    def test_base_T_zero_no_crash(self):
        """base_T 为 0 → 不除零报错，跳过偏离计算"""
        result = _run_check(
            {"RB": {"T_thresh": 0.0}},
            {"RB": {"T_thresh": 1.0, "mean_oos": 1.5}},
        )
        self.assertEqual(len(result["divergences"]), 0)

    def test_base_T_none_skip_divergence(self):
        """base_T 为 None → 跳过偏离计算"""
        result = _run_check(
            {"RB": {}},  # 无 T_thresh
            {"RB": {"T_thresh": 1.0, "mean_oos": 1.5}},
        )
        self.assertEqual(len(result["divergences"]), 0)

    def test_served_T_falls_back_to_base_T(self):
        """served 无 T_thresh 时 fallback 到 base_T → 无偏离"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"mean_oos": 1.5}},  # 无 T_thresh
        )
        self.assertEqual(len(result["divergences"]), 0)

    def test_multiple_symbols_divergence(self):
        """多品种混合：部分偏离、部分正常"""
        result = _run_check(
            {
                "RB": {"T_thresh": 2.0},
                "MA": {"T_thresh": 1.5},
                "SA": {"T_thresh": 3.0},
            },
            {
                "RB": {"T_thresh": 3.0, "mean_oos": 1.5},  # 偏离 50% → 报警
                "MA": {"T_thresh": 1.6, "mean_oos": 1.2},  # 偏离 ~7% → 不报警
                "SA": {"T_thresh": 4.5, "mean_oos": 2.0},  # 偏离 50% → 报警
            },
        )
        self.assertEqual(len(result["divergences"]), 2)
        syms = {d["symbol"] for d in result["divergences"]}
        self.assertEqual(syms, {"RB", "SA"})


# ═══════════════════════════════════════════════════════════════════════════
#  未校验检测
# ═══════════════════════════════════════════════════════════════════════════


class TestUnvalidatedDetection(unittest.TestCase):
    """未校验（缺 mean_oos）检测测试。"""

    def test_missing_mean_oos_reports_unvalidated(self):
        """calib 有条目但缺 mean_oos → 报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0}},  # 缺 mean_oos
        )
        self.assertEqual(len(result["unvalidated"]), 1)
        self.assertEqual(result["unvalidated"][0]["symbol"], "RB")
        self.assertFalse(result["ok"])

    def test_has_mean_oos_no_unvalidated(self):
        """有 mean_oos → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
        )
        self.assertEqual(len(result["unvalidated"]), 0)

    def test_not_in_calib_no_unvalidated(self):
        """不在 calib 中（纯默认占位）→ 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {},  # RB 根本不在 calib 里
        )
        self.assertEqual(len(result["unvalidated"]), 0)

    def test_note_only_symbol_skipped(self):
        """__note_only__ 中的品种 → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {
                "RB": {"T_thresh": 2.0},  # 缺 mean_oos
                "__note_only__": {"RB": "placeholder"},
            },
        )
        self.assertEqual(len(result["unvalidated"]), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  漂移失效检测
# ═══════════════════════════════════════════════════════════════════════════


class TestBrokenDetection(unittest.TestCase):
    """漂移失效检测测试。"""

    def test_broken_ungated_reports_broken_serving(self):
        """broken + 未禁用 + 未门控 → broken_serving（计入 ok=false）"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
            drift_items=[{"symbol": "RB", "status": "broken", "current_expR": -0.8, "evidence": "oos drop"}],
        )
        self.assertEqual(len(result["broken_serving"]), 1)
        self.assertEqual(len(result["broken_gated"]), 0)
        self.assertEqual(result["broken_serving"][0]["symbol"], "RB")
        self.assertFalse(result["broken_serving"][0]["papertrack_gated"])
        self.assertFalse(result["ok"])

    def test_broken_gated_reports_broken_gated(self):
        """broken + 未禁用 + 已门控 → broken_gated（不计入 ok=false）"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
            drift_items=[
                {
                    "symbol": "RB",
                    "status": "broken",
                    "current_expR": -0.8,
                    "evidence": "oos drop",
                    "papertrack_gated": True,
                }
            ],
        )
        self.assertEqual(len(result["broken_serving"]), 0)
        self.assertEqual(len(result["broken_gated"]), 1)
        self.assertTrue(result["broken_gated"][0]["papertrack_gated"])
        # broken_gated 不计入 ok=false
        self.assertTrue(result["ok"])

    def test_broken_disabled_no_alert(self):
        """broken + 已禁用 → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
            drift_items=[{"symbol": "RB", "status": "broken", "current_expR": -0.8, "evidence": "oos drop"}],
            disabled={"RB"},
        )
        self.assertEqual(len(result["broken_serving"]), 0)
        self.assertEqual(len(result["broken_gated"]), 0)
        self.assertTrue(result["ok"])

    def test_non_broken_status_no_alert(self):
        """非 broken 状态 → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
            drift_items=[{"symbol": "RB", "status": "healthy", "current_expR": 0.5, "evidence": "stable"}],
        )
        self.assertEqual(len(result["broken_serving"]), 0)
        self.assertEqual(len(result["broken_gated"]), 0)

    def test_no_drift_data_no_alert(self):
        """无漂移数据 → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
            drift_items=[],
        )
        self.assertEqual(len(result["broken_serving"]), 0)
        self.assertEqual(len(result["broken_gated"]), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  陈旧重校检测
# ═══════════════════════════════════════════════════════════════════════════


class TestStaleDetection(unittest.TestCase):
    """陈旧重校检测测试。"""

    def test_stale_calibration_reports(self):
        """recalibrated_at 超过 30 天 → 标记 stale"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5, "recalibrated_at": _days_ago_str(45)}},
        )
        self.assertEqual(len(result["stale"]), 1)
        self.assertEqual(result["stale"][0]["symbol"], "RB")
        self.assertGreaterEqual(result["stale"][0]["days_ago"], 30)
        self.assertFalse(result["ok"])

    def test_recent_calibration_no_stale(self):
        """30 天内 → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5, "recalibrated_at": _days_ago_str(10)}},
        )
        self.assertEqual(len(result["stale"]), 0)

    def test_boundary_31_days_stale(self):
        """31 天 → 超过阈值算 stale"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5, "recalibrated_at": _days_ago_str(31)}},
        )
        self.assertEqual(len(result["stale"]), 1)

    def test_no_recalibrated_at_no_stale(self):
        """无 recalibrated_at → 不报警"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
        )
        self.assertEqual(len(result["stale"]), 0)

    def test_invalid_date_format_no_crash(self):
        """日期格式非法 → 不崩溃，跳过"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5, "recalibrated_at": "invalid-date"}},
        )
        self.assertEqual(len(result["stale"]), 0)
        self.assertTrue(result["ok"])


# ═══════════════════════════════════════════════════════════════════════════
#  综合场景
# ═══════════════════════════════════════════════════════════════════════════


class TestOverallScenarios(unittest.TestCase):
    """综合场景测试。"""

    def test_all_clean_ok_true(self):
        """全部正常 → ok=True"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}, "MA": {"T_thresh": 1.5}},
            {
                "RB": {"T_thresh": 2.1, "mean_oos": 1.5},
                "MA": {"T_thresh": 1.5, "mean_oos": 1.2},
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["divergences"], 0)
        self.assertEqual(result["summary"]["unvalidated"], 0)
        self.assertEqual(result["summary"]["broken_serving"], 0)
        self.assertEqual(result["summary"]["broken_gated"], 0)
        self.assertEqual(result["summary"]["stale"], 0)
        self.assertEqual(result["summary"]["focus_count"], 2)

    def test_focus_symbols_filter(self):
        """focus_symbols 限制检查范围"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}, "MA": {"T_thresh": 1.5}, "SA": {"T_thresh": 3.0}},
            {
                "RB": {"T_thresh": 3.0, "mean_oos": 1.5},  # 偏离
                "MA": {"T_thresh": 1.5, "mean_oos": 1.2},  # 正常
                "SA": {"T_thresh": 4.5, "mean_oos": 2.0},  # 偏离
            },
            focus=["RB"],  # 只查 RB
        )
        self.assertEqual(result["summary"]["focus_count"], 1)
        self.assertEqual(len(result["divergences"]), 1)
        self.assertEqual(result["divergences"][0]["symbol"], "RB")

    def test_disabled_set_affects_broken_only(self):
        """disabled_set 只影响 broken 检测，不影响其他"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 3.0}},  # 偏离 + 未校验
            drift_items=[{"symbol": "RB", "status": "broken", "current_expR": -0.8, "evidence": "drop"}],
            disabled={"RB"},
        )
        # broken 被禁用 → 不报警
        self.assertEqual(len(result["broken_serving"]), 0)
        # 但偏离和未校验仍然报警（disabled 只管 broken）
        self.assertEqual(len(result["divergences"]), 1)
        self.assertEqual(len(result["unvalidated"]), 1)

    def test_summary_counts_match(self):
        """summary 计数与实际列表长度一致"""
        result = _run_check(
            {
                "RB": {"T_thresh": 2.0},
                "MA": {"T_thresh": 1.5},
                "SA": {"T_thresh": 3.0},
                "PP": {"T_thresh": 1.0},
            },
            {
                "RB": {"T_thresh": 3.0, "mean_oos": 1.5},  # 偏离
                "MA": {"T_thresh": 1.6},  # 未校验
                "SA": {"T_thresh": 3.1, "mean_oos": 2.0, "recalibrated_at": _days_ago_str(60)},  # 陈旧
                "PP": {"T_thresh": 1.0, "mean_oos": 0.8},  # 正常
            },
            drift_items=[
                {
                    "symbol": "RB",
                    "status": "broken",
                    "current_expR": -0.5,
                    "evidence": "drop",
                    "papertrack_gated": False,
                },  # broken_serving
                {
                    "symbol": "SA",
                    "status": "broken",
                    "current_expR": -0.3,
                    "evidence": "drop",
                    "papertrack_gated": True,
                },  # broken_gated
            ],
        )
        self.assertEqual(result["summary"]["divergences"], len(result["divergences"]))
        self.assertEqual(result["summary"]["unvalidated"], len(result["unvalidated"]))
        self.assertEqual(result["summary"]["broken_serving"], len(result["broken_serving"]))
        self.assertEqual(result["summary"]["broken_gated"], len(result["broken_gated"]))
        self.assertEqual(result["summary"]["stale"], len(result["stale"]))
        self.assertEqual(result["summary"]["focus_count"], 4)

    def test_result_has_metadata(self):
        """结果包含 generated_at 和 params"""
        result = _run_check(
            {"RB": {"T_thresh": 2.0}},
            {"RB": {"T_thresh": 2.0, "mean_oos": 1.5}},
        )
        self.assertIn("generated_at", result)
        self.assertIn("params", result)
        self.assertEqual(result["params"]["deviate_pct"], 0.35)
        self.assertEqual(result["params"]["stale_days"], 30)


# ═══════════════════════════════════════════════════════════════════════════
#  辅助函数测试（I/O 异常路径）
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadFunctions(unittest.TestCase):
    """文件加载辅助函数测试（异常路径）。"""

    def test_load_calib_missing_file(self):
        """calib 文件不存在 → 返回空 dict"""
        import tempfile

        tmpdir = tempfile.mkdtemp()
        old_path = cw.CALIB_FILE
        cw.CALIB_FILE = os.path.join(tmpdir, "nonexistent.json")
        try:
            result = cw._load_calib()
            self.assertEqual(result, {})
        finally:
            cw.CALIB_FILE = old_path

    def test_load_drift_missing_file(self):
        """drift 文件不存在 → 返回空 dict"""
        import tempfile

        tmpdir = tempfile.mkdtemp()
        old_path = cw.DRIFT_FILE
        cw.DRIFT_FILE = os.path.join(tmpdir, "nonexistent.json")
        try:
            result = cw._load_drift()
            self.assertEqual(result, {})
        finally:
            cw.DRIFT_FILE = old_path

    def test_load_calib_corrupt_json(self):
        """calib 文件内容损坏 → 不崩溃，返回空 dict"""
        import tempfile

        tmpdir = tempfile.mkdtemp()
        corrupt_file = os.path.join(tmpdir, "corrupt.json")
        with open(corrupt_file, "w") as f:
            f.write("{invalid json!!!")
        old_path = cw.CALIB_FILE
        cw.CALIB_FILE = corrupt_file
        try:
            result = cw._load_calib()
            self.assertEqual(result, {})
        finally:
            cw.CALIB_FILE = old_path


if __name__ == "__main__":
    unittest.main()
