#!/usr/bin/env python3
"""
覆盖率门禁检查脚本

功能：
  1. 读取当前 coverage.json，计算总覆盖率
  2. 与基线（tests/_coverage_baseline.json）对比
  3. 如果覆盖率下降超过容忍度，退出码非零（阻塞 CI）
  4. 输出格式化报告，可直接用于 PR 评论

用法：
  python scripts/check_coverage.py [--tolerance 0.5] [--baseline path] [--json path]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_coverage_json(path: Path) -> dict[str, Any]:
    """读取 coverage.py 生成的 JSON 报告，返回 {文件: {summary}}"""
    with path.open() as f:
        data = json.load(f)

    files = {}
    for filename, info in data.get("files", {}).items():
        summary = info.get("summary", {})
        files[filename] = {
            "covered_lines": summary.get("covered_lines", 0),
            "num_statements": summary.get("num_statements", 0),
            "percent_covered": summary.get("percent_covered", 0.0),
            "missing_lines": summary.get("missing_lines", 0),
            "excluded_lines": summary.get("excluded_lines", 0),
        }

    # 计算总计
    total_covered = sum(f["covered_lines"] for f in files.values())
    total_statements = sum(f["num_statements"] for f in files.values())
    total_percent = (total_covered / total_statements * 100) if total_statements > 0 else 0.0

    return {
        "files": files,
        "totals": {
            "covered_lines": total_covered,
            "num_statements": total_statements,
            "percent_covered": round(total_percent, 2),
        },
    }


def load_baseline(path: Path) -> dict[str, Any] | None:
    """加载覆盖率基线文件"""
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


def format_diff(current: float, baseline: float) -> str:
    """格式化覆盖率差值（带颜色前缀）"""
    diff = current - baseline
    if diff > 0:
        return f"▲ +{diff:.2f}%"
    elif diff < 0:
        return f"▼ {diff:.2f}%"
    else:
        return "— 持平"


def generate_report(
    current_data: dict[str, Any],
    baseline_data: dict[str, Any] | None,
    tolerance: float,
) -> tuple[str, bool]:
    """
    生成覆盖率报告

    Returns:
        (report_text, passed) - 报告文本和是否通过门禁
    """
    current_total = current_data["totals"]["percent_covered"]
    current_files = current_data["files"]

    if baseline_data:
        baseline_total = baseline_data["totals"]["percent_covered"]
        baseline_files = baseline_data.get("files", {})
    else:
        baseline_total = None
        baseline_files = {}

    lines: list[str] = []

    # ── 标题 ──────────────────────────────────────────────────
    lines.append("### 📊 覆盖率报告")
    lines.append("")

    # ── 总体概览 ──────────────────────────────────────────────
    lines.append("#### 总体覆盖率")
    lines.append("")
    lines.append("| 指标 | 当前值 | 基线 | 变化 |")
    lines.append("|---|---|---|---|")

    if baseline_total is not None:
        diff_str = format_diff(current_total, baseline_total)
        baseline_str = f"{baseline_total:.2f}%"
    else:
        diff_str = "— 无基线"
        baseline_str = "—"

    lines.append(f"| **总覆盖率** | **{current_total:.2f}%** | {baseline_str} | {diff_str} |")
    lines.append(f"| 可执行行数 | {current_data['totals']['num_statements']:,} | — | — |")
    lines.append(f"| 已覆盖行数 | {current_data['totals']['covered_lines']:,} | — | — |")
    lines.append("")

    # ── 门禁判断 ──────────────────────────────────────────────
    passed = True
    if baseline_total is not None:
        threshold = baseline_total - tolerance
        if current_total < threshold:
            passed = False
            lines.append("> ❌ **覆盖率门禁不通过**")
            lines.append(f"> 当前 {current_total:.2f}% 低于基线 {baseline_total:.2f}% （容忍度 ±{tolerance:.2f}%）")
            lines.append("> 请为新增代码补充测试，或更新基线（`make coverage-update-baseline`）")
        else:
            lines.append("> ✅ **覆盖率门禁通过**")
            lines.append(f"> 当前 {current_total:.2f}% ≥ 基线 {baseline_total:.2f}% （容忍度 ±{tolerance:.2f}%）")
    else:
        lines.append("> ⚠️ 未找到基线文件，跳过门禁检查")
        lines.append("> 运行 `make coverage-update-baseline` 创建初始基线")

    lines.append("")

    # ── 变化最大的文件（Top 5 上升 / Top 5 下降）──────────────
    if baseline_files:
        file_changes = []
        for filename, current_info in current_files.items():
            baseline_info = baseline_files.get(filename)
            if baseline_info:
                diff = current_info["percent_covered"] - baseline_info["percent_covered"]
            else:
                diff = current_info["percent_covered"]  # 新增文件视为从 0 开始
            file_changes.append((filename, current_info["percent_covered"], diff))

        # 覆盖率下降最多的文件
        declined = sorted([(f, c, d) for f, c, d in file_changes if d < -0.5], key=lambda x: x[2])[:5]

        # 覆盖率上升最多的文件
        improved = sorted([(f, c, d) for f, c, d in file_changes if d > 0.5], key=lambda x: x[2], reverse=True)[:5]

        if declined:
            lines.append("#### ⚠️ 覆盖率下降 Top 5")
            lines.append("")
            lines.append("| 文件 | 当前覆盖率 | 变化 |")
            lines.append("|---|---|---|")
            for filename, coverage, diff in declined:
                lines.append(f"| `{filename}` | {coverage:.2f}% | ▼ {diff:+.2f}% |")
            lines.append("")

        if improved:
            lines.append("#### ✨ 覆盖率提升 Top 5")
            lines.append("")
            lines.append("| 文件 | 当前覆盖率 | 变化 |")
            lines.append("|---|---|---|")
            for filename, coverage, diff in improved:
                lines.append(f"| `{filename}` | {coverage:.2f}% | ▲ {diff:+.2f}% |")
            lines.append("")

    # ── 覆盖率最低的文件（Top 10）────────────────────────────
    low_coverage = sorted(
        [
            (f, info["percent_covered"], info["num_statements"])
            for f, info in current_files.items()
            if info["num_statements"] >= 20
        ],  # 只看有一定规模的文件
        key=lambda x: x[1],
    )[:10]

    if low_coverage:
        lines.append("#### 📉 覆盖率最低的 10 个文件（≥20 行）")
        lines.append("")
        lines.append("| 文件 | 覆盖率 | 可执行行数 |")
        lines.append("|---|---|---|")
        for filename, coverage, statements in low_coverage:
            bar_len = int(coverage / 5)  # 每 5% 一个方块
            bar = "█" * bar_len + "░" * (20 - bar_len)
            lines.append(f"| `{filename}` | {bar} {coverage:.1f}% | {statements} |")
        lines.append("")

    report = "\n".join(lines)
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser(description="覆盖率门禁检查")
    parser.add_argument(
        "--json",
        default="coverage.json",
        help="coverage.json 路径（默认：coverage.json）",
    )
    parser.add_argument(
        "--baseline",
        default="tests/_coverage_baseline.json",
        help="基线文件路径（默认：tests/_coverage_baseline.json）",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="覆盖率下降容忍度（百分点，默认 0.5）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="将报告写入文件（同时输出到 stdout）",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="用当前覆盖率更新基线文件",
    )
    args = parser.parse_args()

    cov_path = Path(args.json)
    baseline_path = Path(args.baseline)

    # 检查 coverage.json 是否存在
    if not cov_path.exists():
        print(f"❌ 找不到覆盖率文件: {cov_path}", file=sys.stderr)
        print("请先运行: python run_tests.py --coverage", file=sys.stderr)
        return 2

    # 加载当前覆盖率
    current_data = load_coverage_json(cov_path)

    # 更新基线模式
    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        with baseline_path.open("w") as f:
            json.dump(current_data, f, indent=2, ensure_ascii=False)
        print(f"✅ 基线已更新: {baseline_path}")
        print(f"   总覆盖率: {current_data['totals']['percent_covered']:.2f}%")
        print(f"   文件数: {len(current_data['files'])}")
        return 0

    # 加载基线
    baseline_data = load_baseline(baseline_path)

    # 生成报告
    report, passed = generate_report(current_data, baseline_data, args.tolerance)

    # 输出报告
    print(report)

    # 写入文件
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report)
        print(f"\n📄 报告已保存到: {out_path}")

    # 退出码
    if passed:
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
