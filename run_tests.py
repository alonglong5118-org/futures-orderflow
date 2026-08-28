#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维策略 — 单元测试总入口
==========================

用法:
  python run_tests.py                  # 跑全部单元测试（Python + JS）
  python run_tests.py gap_stop         # 只跑某个模块
  python run_tests.py smoke            # 冒烟测试（<10s，核心功能快速验证）
  python run_tests.py unit             # 只跑单元测试（不含集成/高级）
  python run_tests.py integration      # 只跑集成测试
  python run_tests.py advanced         # 属性+基准+性能
  python run_tests.py all              # 全部（含性能测试）
  python run_tests.py -v               # 详细输出
  python run_tests.py -f               # 快速失败（第一个失败就停止）
  python run_tests.py -c               # 生成覆盖率报告
  python run_tests.py -r               # 随机测试顺序（发现测试间依赖）
  python run_tests.py --slow 500       # 标记耗时超过 500ms 的慢测试
  python run_tests.py --junit report.xml  # 生成 JUnit XML 报告（CI 用）
  python run_tests.py --retry 3          # 失败的测试最多重跑 3 次（检测不稳定测试）
  python run_tests.py --list             # 列出所有可用测试模块
  python run_tests.py --py-only          # 只跑 Python 测试
  python run_tests.py --js-only          # 只跑 JS 测试

以后新增的测试模块，在 TEST_MODULES（Python）或 JS_TESTS（JS）里加一行。
或者运行 python scripts/discover_tests.py --update 自动发现。
"""

import sys
import os
import unittest
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ── Python 测试模块清单（新增测试在这里注册） ─────────────────────────────
TEST_MODULES = {
    "gap_stop": "tests.test_gap_stop",
    "kelly_factor": "tests.test_kelly_factor",
    "price_protection": "tests.test_price_protection",
    "corr_gate": "tests.test_corr_gate",
    "take_profit": "tests.test_take_profit",
    "signal_trigger": "tests.test_signal_trigger",
    "risk_gate": "tests.test_risk_gate",
    "regime": "tests.test_regime",
    "t_score": "tests.test_t_score",
    "sr_analyzer": "tests.test_sr_analyzer",
    "bias_and_slip": "tests.test_bias_and_slip",
    "params": "tests.test_params",
    "strategies": "tests.test_strategies",
    "config": "tests.test_config",
    "weights": "tests.test_weights",
    "flow_aggregator": "tests.test_flow_aggregator",
    "compute_strategy": "tests.test_compute_strategy",
    "pipeline": "tests.test_pipeline",
    "risk_exit_main": "tests.test_risk_exit_main",
    "subfactors_buildsignal": "tests.test_subfactors_buildsignal",
    "sim_exit_5m": "tests.test_sim_exit_5m",
    "metrics_utils": "tests.test_metrics_utils",
    "risk_lock_wf_gate": "tests.test_risk_lock_wf_gate",
    "ema_robust_gate": "tests.test_ema_robust_gate",
    "risk_state_machine": "tests.test_risk_state_machine",
    "util_functions": "tests.test_util_functions",
    "risk_sm_class": "tests.test_risk_sm_class",
    "montecarlo": "tests.test_montecarlo",
    "discipline_utils": "tests.test_discipline_utils",
    "trade_journal_utils": "tests.test_trade_journal_utils",
    "gbm_garch": "tests.test_gbm_garch",
    "technical_analysis": "tests.test_technical_analysis",
    "sentiment_engine": "tests.test_sentiment_engine",
    "info_screener": "tests.test_info_screener",
    "account_tracker_utils": "tests.test_account_tracker_utils",
    "anomaly_calibration": "tests.test_anomaly_calibration",
    "long_hu_bang": "tests.test_long_hu_bang",
    "hidden_pivot": "tests.test_hidden_pivot",
    "regime_hmm": "tests.test_regime_hmm",
    "event_calendar": "tests.test_event_calendar",
    "perf_breakdown": "tests.test_perf_breakdown",
    "live_health_check": "tests.test_live_health_check",
    "discipline_utils": "tests.test_discipline_utils",
    "strategy_indicators": "tests.test_strategy_indicators",
    "sr_threshold_validation": "tests.test_sr_threshold_validation",
    "preflight_check": "tests.test_preflight_check",
    "direction_source_monitor": "tests.test_direction_source_monitor",
    "wf_validation": "tests.test_wf_validation",
    "calibration_utils": "tests.test_calibration_utils",
    "t_score_utils": "tests.test_t_score_utils",
    "ga_quality": "tests.test_ga_quality",
    "oos_weight_validation": "tests.test_oos_weight_validation",
    "sr_widen_filter": "tests.test_sr_widen_filter",
    "strategy_signals": "tests.test_strategy_signals",
    "regime_and_entropy": "tests.test_regime_and_entropy",
    "regression_utils": "tests.test_regression_utils",
    "four_dim_core": "tests.test_four_dim_core",
    "orderflow_sentiment_llm": "tests.test_orderflow_sentiment_llm",
    "screener_slip_variety": "tests.test_screener_slip_variety",
    "position_flatten": "tests.test_position_flatten",
    "divergence_hidden_pivot": "tests.test_divergence_hidden_pivot",
    "anomaly_event_gate": "tests.test_anomaly_event_gate",
    "sr_quality_fundamental": "tests.test_sr_quality_fundamental",
    "price_protection_corr_gate": "tests.test_price_protection_corr_gate",
    "kelly_gap_signal": "tests.test_kelly_gap_signal",
    "exit_plan_duration_source": "tests.test_exit_plan_duration_source",
    "risk_gate_position": "tests.test_risk_gate_position",
    "perf_blunder_stats": "tests.test_perf_blunder_stats",
    "four_dim_pure": "tests.test_four_dim_pure",
    "sentiment_pure_factors": "tests.test_sentiment_pure_factors",
    "sr_hmm_pure": "tests.test_sr_hmm_pure",
    "strategy_layer_pure": "tests.test_strategy_layer_pure",
    "papertrack_pure": "tests.test_papertrack_pure",
    "time_contract_utils": "tests.test_time_contract_utils",
    "discipline_macro_misc": "tests.test_discipline_macro_misc",
    "broker_blunder_pure": "tests.test_broker_blunder_pure",
    "remaining_pure": "tests.test_remaining_pure",
    "calibration_cache_pure": "tests.test_calibration_cache_pure",
    "sr_widen_exchange": "tests.test_sr_widen_exchange",
    "seasonal_final": "tests.test_seasonal_final",
    "contract_month_tags": "tests.test_contract_month_tags",
    "kelly_regression_contract": "tests.test_kelly_regression_contract",
    "discipline_cache_risk": "tests.test_discipline_cache_risk",
    "broker_macro_tqsdk": "tests.test_broker_macro_tqsdk",
    "integration_pipeline": "tests.test_integration_pipeline",
    "integration_backtest_dq": "tests.test_integration_backtest_dq",
    "integration_deep": "tests.test_integration_deep",
    "property_fuzz": "tests.test_property_fuzz",
    "baseline_regression": "tests.test_baseline_regression",
    "performance": "tests.test_performance",
}

# 默认全量测试时跳过的模块（性能测试等耗时/波动大的）
SKIP_BY_DEFAULT = {
    "performance",  # 性能测试：耗时 + 环境波动大，需显式运行
}

# ── 测试分类套件 ──────────────────────────────────────────────────────────
# 冒烟测试：核心功能快速验证，目标 <10s
SMOKE_TESTS = {
    "kelly_factor",       # Kelly 因子核心
    "risk_gate",          # 风险闸门
    "take_profit",        # 止盈
    "regime",             # 市场状态
    "property_fuzz",      # 属性测试（快速验证数学属性）
    "four_dim_pure",      # 四维纯函数
}

# 单元测试（不含集成/属性/基准/性能）
UNIT_TESTS = None  # None = 全部减去集成类

# 集成测试套件
INTEGRATION_TESTS = {
    "integration_pipeline",
    "integration_backtest_dq",
    "integration_deep",
}

# 高级测试套件（属性+基准+性能）
ADVANCED_TESTS = {
    "property_fuzz",
    "baseline_regression",
    "performance",
}

# 分类别名映射
CATEGORY_ALIASES = {
    "smoke": SMOKE_TESTS,
    "unit": None,  # 动态计算
    "integration": INTEGRATION_TESTS,
    "advanced": ADVANCED_TESTS,
    "all": None,   # 全部（含默认跳过的）
}

# ── JavaScript 测试模块清单（新增测试在这里注册） ────────────────────────
JS_TESTS = {
    "user_action_lock": "tests/test_user_action_lock.js",
}

# ── 颜色 ──────────────────────────────────────────────────────────────────
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    DIM = "\033[2m"


def color(text, *codes):
    return "".join(codes) + text + C.RESET


def list_modules():
    """列出所有可用测试模块。"""
    print()
    print(color("=" * 60, C.BOLD, C.CYAN))
    print(color("  可用测试模块", C.BOLD, C.CYAN))
    print(color("=" * 60, C.BOLD, C.CYAN))
    print()
    print(color("  分类套件:", C.BOLD, C.YELLOW))
    for name in sorted(CATEGORY_ALIASES.keys()):
        modules = CATEGORY_ALIASES[name]
        if modules is None:
            if name == "all":
                desc = "全部测试（含性能）"
            elif name == "unit":
                desc = "单元测试（不含集成/高级）"
            else:
                desc = ""
        else:
            desc = f"{len(modules)} 个模块"
        print(f"    {color(name, C.BOLD, C.MAGENTA):<20} → {desc}")
    print()
    print(color("  Python 模块:", C.BOLD, C.YELLOW))
    for name, mod in TEST_MODULES.items():
        skip_tag = "  (默认跳过)" if name in SKIP_BY_DEFAULT else ""
        cat_tag = ""
        if name in INTEGRATION_TESTS:
            cat_tag = "  [集成]"
        elif name in ADVANCED_TESTS:
            cat_tag = "  [高级]"
        print(f"    {color(name, C.BOLD, C.GREEN):<20} → {mod}{skip_tag}{cat_tag}")
    print()
    print(color("  JavaScript 模块:", C.BOLD, C.YELLOW))
    for name, path in JS_TESTS.items():
        print(f"    {color(name, C.BOLD, C.GREEN):<20} → {path}")
    print()
    total = len(TEST_MODULES) + len(JS_TESTS)
    print(f"  共 {total} 个模块 + {len(CATEGORY_ALIASES)} 个分类套件")
    print()
    print(color("  用法: python run_tests.py [模块名|分类名] [-v] [-c] [--list] [--py-only]", C.DIM))
    print()


def _make_runner(verbosity=1, failfast=False, slow_threshold_ms=0,
                 junit_output=None):
    """创建 unittest 测试运行器（支持慢测试标记和 JUnit XML）。"""
    resultclass = None
    if slow_threshold_ms > 0 or junit_output:
        # 自定义 result 类，记录每个测试的耗时
        class _TimedResult(unittest.TextTestResult):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._test_times = {}  # test_id -> elapsed_ms
                self._current_start = None

            def startTest(self, test):
                self._current_start = time.time()
                super().startTest(test)

            def stopTest(self, test):
                super().stopTest(test)
                if self._current_start is not None:
                    elapsed = (time.time() - self._current_start) * 1000
                    self._test_times[str(test)] = elapsed

        resultclass = _TimedResult

    runner = unittest.TextTestRunner(
        verbosity=verbosity, failfast=failfast, resultclass=resultclass
    )

    # 附加属性供调用方读取
    runner._slow_threshold = slow_threshold_ms
    runner._junit_output = junit_output
    return runner


def _report_slow_tests(result, threshold_ms):
    """输出慢测试列表。"""
    if not hasattr(result, "_test_times"):
        return []

    times = result._test_times
    slow = [(name, t) for name, t in times.items() if t > threshold_ms]
    slow.sort(key=lambda x: -x[1])

    if slow:
        print()
        print(color(f"  🐢 慢测试（>{threshold_ms}ms，共 {len(slow)} 个）",
                    C.BOLD, C.YELLOW))
        print(color("  " + "-" * 40, C.DIM))
        for name, t in slow[:20]:
            short = name[:55]
            print(color(f"  {t:>8.1f}ms  {short}", C.YELLOW))
        if len(slow) > 20:
            print(color(f"  ... 还有 {len(slow) - 20} 个", C.DIM))
        print()

    return slow


def _write_junit_xml(result, output_path):
    """生成 JUnit XML 格式报告（CI 友好）。"""
    if not hasattr(result, "_test_times"):
        return

    times = result._test_times
    total_tests = result.testsRun
    total_failures = len(result.failures)
    total_errors = len(result.errors)
    total_skipped = len(result.skipped)
    total_time = sum(times.values()) / 1000.0

    from xml.sax.saxutils import escape

    # 按类分组
    suites = {}
    # 简单起见，所有测试放一个 suite
    test_cases = []

    # 收集所有测试名（从 times 里拿不到全部，因为失败的也有记录）
    all_test_ids = set(times.keys())
    for test, _ in result.failures + result.errors + result.skipped:
        all_test_ids.add(str(test))

    for test_id in sorted(all_test_ids):
        t = times.get(test_id, 0.0) / 1000.0
        classname = "tests"
        name = escape(test_id)

        status_elem = ""
        for test_obj, trace in result.failures:
            if str(test_obj) == test_id:
                status_elem = f'<failure message="assertion failed">{escape(trace[-200:])}</failure>'
                break
        if not status_elem:
            for test_obj, trace in result.errors:
                if str(test_obj) == test_id:
                    status_elem = f'<error message="error">{escape(trace[-200:])}</error>'
                    break
        if not status_elem:
            for test_obj, reason in result.skipped:
                if str(test_obj) == test_id:
                    status_elem = f'<skipped message="{escape(str(reason)[:200])}"/>'
                    break

        test_cases.append(
            f'    <testcase classname="{classname}" name="{name}" time="{t:.6f}">'
            f'{status_elem}</testcase>'
        )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="{total_tests}" failures="{total_failures}" errors="{total_errors}" skipped="{total_skipped}" time="{total_time:.3f}">
  <testsuite name="all_tests" tests="{total_tests}" failures="{total_failures}" errors="{total_errors}" skipped="{total_skipped}" time="{total_time:.3f}">
{chr(10).join(test_cases)}
  </testsuite>
</testsuites>
"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml)
    print(color(f"  📄 JUnit XML 报告已生成: {output_path}", C.DIM))


def run_py_tests(module_name=None, verbose=False, module_set=None,
                  coverage=False, failfast=False, random_order=False,
                  slow_threshold_ms=0, junit_output=None):
    """运行 Python 测试。

    Args:
        module_name: 单个模块名
        verbose: 详细输出
        module_set: 模块名集合（用于分类套件），None 表示默认全部
        coverage: 是否启用覆盖率统计
        failfast: 第一个失败就停止
        random_order: 随机测试顺序（发现测试间隐式依赖）
        slow_threshold_ms: 慢测试阈值（ms），0 表示不检查
        junit_output: JUnit XML 输出路径，None 表示不生成
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    def _safe_load(name, mod_path):
        """安全加载测试模块，失败时打印详细错误。"""
        try:
            sub = loader.loadTestsFromName(mod_path)
            suite.addTests(sub)
        except Exception as e:
            print(color(f"  ❌ 加载失败: {name} ({mod_path})", C.RED))
            print(color(f"     错误: {e}", C.RED))
            import traceback
            # 只打最内层的几帧，避免输出过长
            tb_lines = traceback.format_exc().strip().split('\n')
            for line in tb_lines[-8:]:
                print(color(f"     {line}", C.DIM))

    if module_name:
        if module_name not in TEST_MODULES:
            return None  # 不是 Python 模块，返回 None 让调用者处理
        mod_path = TEST_MODULES[module_name]
        _safe_load(module_name, mod_path)
    elif module_set is not None:
        # 指定模块集合
        for name in sorted(module_set):
            if name in TEST_MODULES:
                _safe_load(name, TEST_MODULES[name])
    else:
        # 默认全部（跳过 SKIP_BY_DEFAULT）
        for name, mod_path in TEST_MODULES.items():
            if name in SKIP_BY_DEFAULT:
                continue
            _safe_load(name, mod_path)

    # 随机打乱测试顺序
    if random_order:
        import random
        tests = list(suite)
        random.shuffle(tests)
        suite = unittest.TestSuite(tests)
        print(color("  🔀 随机测试顺序已启用", C.DIM))

    verbosity = 2 if verbose else 1
    runner = _make_runner(verbosity, failfast, slow_threshold_ms, junit_output)

    if coverage:
        try:
            import coverage
        except ImportError:
            print(color("  ⚠️  未安装 coverage，跳过覆盖率统计", C.YELLOW))
            print(color("     安装: pip install coverage", C.DIM))
            result = runner.run(suite)
            if slow_threshold_ms > 0:
                _report_slow_tests(result, slow_threshold_ms)
            if junit_output:
                _write_junit_xml(result, junit_output)
            return result

        cov = coverage.Coverage()
        cov.start()
        result = runner.run(suite)
        cov.stop()
        cov.save()

        print()
        print(color("  覆盖率报告", C.BOLD, C.CYAN))
        print(color("  " + "-" * 40, C.DIM))
        cov.report(show_missing=False)
        print()
    else:
        result = runner.run(suite)

    # 后处理：慢测试报告 + JUnit XML
    if slow_threshold_ms > 0:
        _report_slow_tests(result, slow_threshold_ms)
    if junit_output:
        _write_junit_xml(result, junit_output)

    return result


def run_py_tests_suite(suite, verbose=False, coverage=False, failfast=False,
                       slow_threshold_ms=0, junit_output=None):
    """运行一个已组装好的测试套件。"""
    verbosity = 2 if verbose else 1
    runner = _make_runner(verbosity, failfast, slow_threshold_ms, junit_output)

    if coverage:
        try:
            import coverage
        except ImportError:
            print(color("  ⚠️  未安装 coverage，跳过覆盖率统计", C.YELLOW))
            print(color("     安装: pip install coverage", C.DIM))
            result = runner.run(suite)
            if slow_threshold_ms > 0:
                _report_slow_tests(result, slow_threshold_ms)
            if junit_output:
                _write_junit_xml(result, junit_output)
            return result

        cov = coverage.Coverage()
        cov.start()
        result = runner.run(suite)
        cov.stop()
        cov.save()

        print()
        print(color("  覆盖率报告", C.BOLD, C.CYAN))
        print(color("  " + "-" * 40, C.DIM))
        cov.report(show_missing=False)
        print()
    else:
        result = runner.run(suite)

    if slow_threshold_ms > 0:
        _report_slow_tests(result, slow_threshold_ms)
    if junit_output:
        _write_junit_xml(result, junit_output)

    return result


def run_js_tests(module_name=None, verbose=False):
    """运行 JavaScript 测试。返回 (success: bool, test_count: int)"""
    import shutil

    if not shutil.which("node"):
        print(color("  ⚠️  未检测到 Node.js，跳过 JS 测试", C.YELLOW))
        return True, 0

    if module_name:
        if module_name not in JS_TESTS:
            return None  # 不是 JS 模块
        js_file = JS_TESTS[module_name]
        print(color(f"\n  运行 JS 测试: {module_name}", C.BOLD, C.CYAN))
    else:
        # 全部 JS 测试 — 一个个跑
        all_ok = True
        total = 0
        for name, js_file in JS_TESTS.items():
            print(color(f"\n  运行 JS 测试: {name}", C.BOLD, C.CYAN))
            ok, count = _run_single_js_test(js_file, verbose)
            all_ok = all_ok and ok
            total += count
        return all_ok, total

    return _run_single_js_test(js_file, verbose)


def _run_single_js_test(js_file, verbose=False):
    """运行单个 JS 测试文件。返回 (success, test_count)"""
    cmd = ["node", js_file]
    try:
        result = subprocess.run(
            cmd, cwd=HERE, capture_output=True, text=True, timeout=30
        )
        # 输出结果（测试脚本自己会打印）
        if result.stdout:
            # 去掉首行标题（避免重复打印）
            lines = result.stdout.split("\n")
            # 只打印非标题行
            for line in lines:
                if "═" in line and ("用户交互锁" in line or "单元测试" in line):
                    continue
                print("  " + line)

        if result.returncode != 0:
            if result.stderr:
                print(color(f"  错误输出:\n{result.stderr}", C.RED))
            return False, 0

        # 从输出中提取测试数量
        # 输出格式："结果：X 通过，Y 失败"
        import re
        m = re.search(r"结果：(\d+) 通过", result.stdout)
        count = int(m.group(1)) if m else 0
        return True, count

    except subprocess.TimeoutExpired:
        print(color("  ❌ JS 测试超时 (30s)", C.RED))
        return False, 0
    except Exception as e:
        print(color(f"  ❌ JS 测试执行失败: {e}", C.RED))
        return False, 0


def run_tests(module_name=None, verbose=False, py_only=False, js_only=False,
              coverage=False, failfast=False, random_order=False,
              slow_threshold_ms=0, junit_output=None, max_retries=0):
    """运行测试（Python + JS）。"""
    all_ok = True
    total_tests = 0
    py_result = None
    module_timings = []  # [(module_name, elapsed_ms, test_count, passed)]
    failed_tests = []    # [(module_name, test_name, error_msg)]

    # 处理分类别名
    py_module_set = None
    category_label = None
    if module_name and module_name in CATEGORY_ALIASES:
        category_label = module_name
        if module_name == "all":
            py_module_set = set(TEST_MODULES.keys())  # 全部，包括默认跳过的
        elif module_name == "unit":
            # 单元测试 = 全部 - 集成 - 高级
            py_module_set = (set(TEST_MODULES.keys())
                             - INTEGRATION_TESTS
                             - ADVANCED_TESTS
                             - SKIP_BY_DEFAULT)
        else:
            py_module_set = CATEGORY_ALIASES[module_name]
        module_name = None  # 重置，走批量逻辑

    # 如果指定了单个模块名，先看看是 Python 还是 JS 的
    if module_name:
        if module_name in TEST_MODULES:
            t0 = time.time()
            result = run_py_tests(module_name, verbose, coverage=coverage,
                                  failfast=failfast, random_order=random_order,
                                  slow_threshold_ms=slow_threshold_ms,
                                  junit_output=junit_output)
            elapsed = (time.time() - t0) * 1000
            if result:
                total_tests += result.testsRun
                all_ok = all_ok and result.wasSuccessful()
                module_timings.append((module_name, elapsed, result.testsRun,
                                       result.wasSuccessful()))
                # 收集失败
                for test, trace in result.failures + result.errors:
                    failed_tests.append((module_name, str(test), trace))
        elif module_name in JS_TESTS:
            t0 = time.time()
            ok, count = run_js_tests(module_name, verbose)
            elapsed = (time.time() - t0) * 1000
            total_tests += count
            all_ok = all_ok and ok
            module_timings.append((module_name, elapsed, count, ok))
        else:
            print(color(f"  ❌ 未知模块: {module_name}", C.RED))
            all_names = list(TEST_MODULES.keys()) + list(JS_TESTS.keys()) + list(CATEGORY_ALIASES.keys())
            print(f"  可用模块: {', '.join(sorted(all_names))}")
            return False
    else:
        # 跑全部（或按分类/--py-only/--js-only 过滤）
        # 逐个模块运行以便计时
        if not js_only:
            if category_label:
                label = f"Python 测试 [{category_label}]"
            else:
                label = "Python 测试"
            print(color(f"\n  {label}", C.BOLD, C.YELLOW))
            print(color("  " + "-" * 40, C.DIM))

            # 确定要跑的模块
            if py_module_set is not None:
                modules_to_run = [(n, TEST_MODULES[n])
                                  for n in sorted(py_module_set)
                                  if n in TEST_MODULES]
            else:
                modules_to_run = [(n, p) for n, p in TEST_MODULES.items()
                                  if n not in SKIP_BY_DEFAULT]

            # 逐个运行
            combined_suite = unittest.TestSuite()
            loader = unittest.TestLoader()
            for name, mod_path in modules_to_run:
                try:
                    combined_suite.addTests(loader.loadTestsFromName(mod_path))
                except Exception as e:
                    print(color(f"  ⚠️  加载模块失败 {name}: {e}", C.YELLOW))

            t0 = time.time()
            result = run_py_tests_suite(combined_suite, verbose, coverage,
                                        failfast, slow_threshold_ms,
                                        junit_output)
            elapsed = (time.time() - t0) * 1000

            # 失败重试（检测不稳定测试）
            if max_retries > 0 and not result.wasSuccessful():
                result, flaky, n_retry = _rerun_failed_tests(
                    result, combined_suite, max_retries, verbose,
                    slow_threshold_ms, junit_output
                )
                elapsed = (time.time() - t0) * 1000

            py_result = result
            total_tests += result.testsRun
            all_ok = all_ok and result.wasSuccessful()

            # 记录总计时（不拆分到模块，因为是一起跑的）
            module_timings.append(("python_total", elapsed, result.testsRun,
                                   result.wasSuccessful()))

            # 收集失败
            for test, trace in result.failures + result.errors:
                test_id = str(test)
                failed_tests.append(("python", test_id, trace))

        if not py_only:
            print(color("\n  JavaScript 测试", C.BOLD, C.YELLOW))
            print(color("  " + "-" * 40, C.DIM))
            t0 = time.time()
            ok, count = run_js_tests(None, verbose)
            elapsed = (time.time() - t0) * 1000
            total_tests += count
            all_ok = all_ok and ok
            module_timings.append(("javascript", elapsed, count, ok))

    # 汇总输出
    print()
    print(color("=" * 60, C.BOLD))
    print(color("  测试结果汇总", C.BOLD, C.CYAN))
    print(color("=" * 60, C.BOLD))

    # 失败汇总
    if failed_tests:
        print()
        print(color(f"  ❌ 失败的测试 ({len(failed_tests)} 个)", C.BOLD, C.RED))
        print(color("  " + "-" * 40, C.DIM))
        for i, (mod, test_name, trace) in enumerate(failed_tests[:20], 1):
            short_name = test_name.split("\n")[0][:60]
            print(color(f"  {i}. [{mod}] {short_name}", C.RED))
        if len(failed_tests) > 20:
            print(color(f"  ... 还有 {len(failed_tests) - 20} 个失败", C.DIM))

        # 打印第一个失败的详细错误（方便 CI 定位问题）
        print()
        print(color("  🔍 第一个失败详情:", C.BOLD, C.YELLOW))
        print(color("  " + "-" * 40, C.DIM))
        first_mod, first_name, first_trace = failed_tests[0]
        print(color(f"  模块: {first_mod}", C.YELLOW))
        print(color(f"  测试: {first_name.split(chr(10))[0]}", C.YELLOW))
        print(color("  错误:", C.YELLOW))
        # trace 可能很长，截取最后部分
        trace_lines = first_trace.strip().split(chr(10))
        for line in trace_lines[-12:]:
            print(color(f"    {line}", C.DIM))
        print()

    # 总耗时
    total_elapsed = sum(t for _, t, _, _ in module_timings)
    print(color(f"  总测试数: {total_tests}", C.BOLD))
    print(color(f"  总耗时: {total_elapsed/1000:.2f}s", C.BOLD))

    if all_ok:
        print(color(f"  状态: ✅ 全部通过", C.BOLD, C.GREEN))
    else:
        print(color(f"  状态: ❌ 有失败", C.BOLD, C.RED))
    print()

    return all_ok


def _rerun_failed_tests(result, original_suite, max_retries, verbose,
                        slow_threshold_ms, junit_output):
    """重跑失败的测试，检测不稳定（flake）测试。

    返回 (最终结果, flaky_tests列表, 总重试次数)
    """
    if not result.failures and not result.errors:
        return result, [], 0

    # 收集失败的测试 id
    failed_ids = set()
    for test, _ in result.failures + result.errors:
        failed_ids.add(str(test))

    if not failed_ids:
        return result, [], 0

    flaky = {}  # test_id -> [pass_count, fail_count]
    total_retries = 0
    last_result = result
    verbosity = 2 if verbose else 1

    for attempt in range(1, max_retries + 1):
        # 构建只有失败测试的 suite
        retry_suite = unittest.TestSuite()
        for test in _iter_tests(original_suite):
            if str(test) in failed_ids:
                retry_suite.addTest(test)

        if retry_suite.countTestCases() == 0:
            break

        print()
        print(color(f"  🔄 第 {attempt} 次重跑失败测试 "
                    f"({retry_suite.countTestCases()} 个)", C.YELLOW))
        print(color("  " + "-" * 40, C.DIM))

        runner = _make_runner(verbosity, failfast=False,
                              slow_threshold_ms=slow_threshold_ms,
                              junit_output=None)  # 重试不生成 junit
        retry_result = runner.run(retry_suite)
        total_retries += 1
        last_result = retry_result

        # 更新 flaky 状态
        still_failed = set()
        for test, _ in retry_result.failures + retry_result.errors:
            still_failed.add(str(test))

        # 这次通过了的 → 标记为 flaky
        for tid in failed_ids:
            if tid not in still_failed:
                if tid not in flaky:
                    flaky[tid] = [0, 0]
                flaky[tid][0] += 1  # pass
            else:
                if tid not in flaky:
                    flaky[tid] = [0, 0]
                flaky[tid][1] += 1  # fail

        # 更新失败列表
        failed_ids = still_failed
        if not failed_ids:
            break

    # 输出 flaky 报告
    flaky_tests = [(tid, p, f) for tid, (p, f) in flaky.items() if p > 0]
    if flaky_tests:
        print()
        print(color(f"  ⚠️  不稳定测试（{len(flaky_tests)} 个）",
                    C.BOLD, C.YELLOW))
        print(color("  " + "-" * 40, C.DIM))
        for tid, p, f in sorted(flaky_tests, key=lambda x: -x[1]):
            short = tid[:55]
            print(color(f"  {p}胜{f}败  {short}", C.YELLOW))
        print()

    return last_result, flaky_tests, total_retries


def _iter_tests(suite):
    """递归遍历 suite 中的所有测试用例。"""
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_tests(test)
        else:
            yield test


def main():
    args = sys.argv[1:]

    if "--list" in args or "-l" in args:
        list_modules()
        return

    verbose = "-v" in args or "--verbose" in args
    py_only = "--py-only" in args
    js_only = "--js-only" in args
    coverage = "--coverage" in args or "-c" in args
    failfast = "-f" in args or "--failfast" in args
    random_order = "-r" in args or "--random" in args

    # --slow <ms>
    slow_threshold_ms = 0
    if "--slow" in args:
        idx = args.index("--slow")
        if idx + 1 < len(args):
            try:
                slow_threshold_ms = float(args[idx + 1])
            except ValueError:
                print(color(f"  ⚠️  --slow 参数无效: {args[idx+1]}", C.YELLOW))

    # --junit <path>
    junit_output = None
    if "--junit" in args:
        idx = args.index("--junit")
        if idx + 1 < len(args):
            junit_output = args[idx + 1]

    # --retry <n>  失败重试次数（检测不稳定测试）
    max_retries = 0
    if "--retry" in args:
        idx = args.index("--retry")
        if idx + 1 < len(args):
            try:
                max_retries = int(args[idx + 1])
            except ValueError:
                print(color(f"  ⚠️  --retry 参数无效: {args[idx+1]}", C.YELLOW))

    # 找出模块名（第一个非参数项）
    known_flags = {
        "-v", "--verbose", "--list", "-l",
        "--py-only", "--js-only",
        "--coverage", "-c",
        "-f", "--failfast",
        "-r", "--random",
        "--slow", "--junit", "--retry",
    }
    module_name = None
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in known_flags:
            if a in ("--slow", "--junit", "--retry"):
                skip_next = True  # 下一个是参数值
            continue
        module_name = a
        break

    ok = run_tests(module_name, verbose, py_only, js_only,
                   coverage, failfast, random_order,
                   slow_threshold_ms, junit_output, max_retries)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
