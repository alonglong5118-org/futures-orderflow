#!/usr/bin/env python3
"""生成性能基准测试报告（Markdown + JSON）。

从 results.json（测试输出）和 _perf_baseline.json（历史基线）读取数据，
生成 Markdown 表格报告。

CI 中使用：先跑 tests.test_performance（设置 PERF_RESULTS_FILE），
再跑本脚本生成报告。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 性能退化阈值（和 test_performance.py 保持一致）
PERF_REGRESSION_THRESHOLD = 0.5

# 文件路径
RESULTS_FILE = os.path.join(ROOT, "benchmark-results", "results.json")
BASELINE_FILE = os.path.join(ROOT, "tests", "_perf_baseline.json")


def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def generate_report(results, baselines):
    """生成 Markdown 报告。"""
    lines = []
    lines.append("# 性能基准测试报告")
    lines.append("")
    lines.append("| 指标 | 当前 (ms) | 基线 (ms) | 变化 | 状态 |")
    lines.append("|------|-----------|-----------|------|------|")

    regressions = 0
    improvements = 0
    new_items = 0
    total = len(results)

    for name in sorted(results.keys()):
        avg_ms = results[name]
        baseline = baselines.get(name)

        if baseline and baseline > 0:
            delta_pct = (avg_ms / baseline - 1) * 100
            delta_str = f"{delta_pct:+.1f}%"
            if delta_pct > PERF_REGRESSION_THRESHOLD * 100:
                status = "🔴 退化"
                regressions += 1
            elif delta_pct < -PERF_REGRESSION_THRESHOLD * 100:
                status = "🟢 提升"
                improvements += 1
            else:
                status = "✅ 正常"
            baseline_str = f"{baseline:.4f}"
        else:
            delta_str = "—"
            status = "🆕 新增"
            baseline_str = "—"
            new_items += 1

        lines.append(f"| {name} | {avg_ms:.4f} | {baseline_str} | {delta_str} | {status} |")

    lines.append("")
    lines.append(
        f"**共 {total} 项基准** · 🔴 退化 {regressions} 项 · 🟢 提升 {improvements} 项 · 🆕 新增 {new_items} 项"
    )

    return "\n".join(lines), regressions


def main():
    results = load_json(RESULTS_FILE)
    baselines = load_json(BASELINE_FILE)

    if not results:
        print("❌ 未找到性能结果文件，跳过报告生成")
        return 0

    out_dir = os.path.join(ROOT, "benchmark-results")
    os.makedirs(out_dir, exist_ok=True)

    # 生成 Markdown 报告
    report_md, regressions = generate_report(results, baselines)
    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w") as f:
        f.write(report_md)

    # 控制台输出
    print()
    print("=" * 60)
    print("  性能基准报告")
    print("=" * 60)
    print(report_md)
    print()
    print(f"报告已保存到: {out_dir}/report.md")

    # 严格模式下有退化则失败
    strict = os.environ.get("PERF_CHECK", "0") == "1"
    if strict and regressions > 0:
        print(f"❌ 严格模式：{regressions} 项性能退化，失败")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
