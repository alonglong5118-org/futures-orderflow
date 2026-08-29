#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能趋势追踪工具。

收集历史性能基准数据，生成趋势报告和简单的趋势图（ASCII）。
用于追踪项目性能随时间的变化，发现长期性能退化趋势。

数据格式（trend.json）：
{
  "history": [
    {
      "timestamp": "2026-08-29T10:00:00Z",
      "commit": "abc123def",
      "version": "v1.0.0",  // 可选
      "results": {"bench1": 1.23, "bench2": 4.56, ...}
    },
    ...
  ],
  "latest": { ... }  // 最新一次的结果
}

用法：
  # 将当前结果添加到趋势数据
  python scripts/perf_trend.py add --results results.json --commit abc123

  # 生成趋势报告
  python scripts/perf_trend.py report --trend trend.json --output trend.md

  # 生成 ASCII 趋势图
  python scripts/perf_trend.py chart --trend trend.json --benchmark "risk_gate"
"""

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_entry(trend_file, results_file, commit=None, version=None):
    """添加一条基准记录到趋势数据。"""
    results = load_json(results_file)
    if not results:
        print(f"❌ 无法加载结果文件: {results_file}", file=sys.stderr)
        sys.exit(2)

    trend = load_json(trend_file) or {"history": [], "latest": None}

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "commit": commit or "unknown",
        "results": results,
    }
    if version:
        entry["version"] = version

    trend["history"].append(entry)
    trend["latest"] = entry

    # 保留最近 50 条记录
    if len(trend["history"]) > 50:
        trend["history"] = trend["history"][-50:]

    save_json(trend_file, trend)
    print(f"✅ 已添加记录（共 {len(trend['history'])} 条历史记录）")
    return trend


def generate_report(trend_file, output_file=None):
    """生成趋势报告（Markdown）。"""
    trend = load_json(trend_file)
    if not trend or not trend["history"]:
        print("❌ 无趋势数据", file=sys.stderr)
        sys.exit(2)

    history = trend["history"]
    latest = history[-1]
    first = history[0]

    # 获取所有基准项名称
    all_benches = set()
    for entry in history:
        all_benches.update(entry["results"].keys())
    all_benches = sorted(all_benches)

    lines = []
    lines.append("# 性能趋势报告")
    lines.append("")
    lines.append(f"- **记录数**: {len(history)} 条")
    lines.append(f"- **时间范围**: {first['timestamp'][:10]} ~ {latest['timestamp'][:10]}")
    lines.append(f"- **基准项数**: {len(all_benches)} 项")
    lines.append("")

    # 总体变化（首 vs 尾）
    lines.append("## 📈 总体变化（首次 vs 最新）")
    lines.append("")
    lines.append("| 基准项 | 首次 (ms) | 最新 (ms) | 总变化 | 趋势 |")
    lines.append("|---|---|---|---|---|")

    for name in all_benches:
        first_val = first["results"].get(name)
        latest_val = latest["results"].get(name)

        if first_val is None or latest_val is None or first_val == 0:
            lines.append(f"| {name} | — | — | — | — |")
            continue

        delta_pct = (latest_val / first_val - 1) * 100
        delta_str = f"{delta_pct:+.1f}%"

        if delta_pct > 20:
            trend_icon = "🔴 退化"
        elif delta_pct > 5:
            trend_icon = "🟡 微降"
        elif delta_pct < -20:
            trend_icon = "🟢 提升"
        elif delta_pct < -5:
            trend_icon = "🔵 微升"
        else:
            trend_icon = "➖ 稳定"

        lines.append(f"| {name} | {first_val:.4f} | {latest_val:.4f} | {delta_str} | {trend_icon} |")

    lines.append("")

    # 最近 5 次变化
    lines.append("## 🔄 最近 5 次变化")
    lines.append("")
    recent = history[-5:] if len(history) >= 5 else history
    lines.append("| 日期 | Commit |")
    lines.append("|---|---|")
    for entry in recent:
        date = entry["timestamp"][:10]
        commit_short = entry["commit"][:7] if entry["commit"] != "unknown" else "—"
        lines.append(f"| {date} | `{commit_short}` |")
    lines.append("")

    # ASCII 趋势图（每个基准项一个）
    lines.append("## 📊 趋势图")
    lines.append("")
    lines.append("> 每行代表一次基准测试，从左到右 = 从旧到新")
    lines.append("")

    for name in all_benches:
        values = [entry["results"].get(name) for entry in history if name in entry["results"]]
        if len(values) < 2:
            continue

        chart = ascii_chart(values, width=40, height=8)
        lines.append(f"### {name}")
        lines.append("")
        lines.append("```")
        lines.extend(chart)
        lines.append("```")
        lines.append("")

    report = "\n".join(lines)

    if output_file:
        with open(output_file, "w") as f:
            f.write(report)
        print(f"✅ 趋势报告已保存到: {output_file}")
    else:
        print(report)

    return report


def ascii_chart(values, width=40, height=8):
    """生成简单的 ASCII 折线图。

    返回行列表。
    """
    n = len(values)
    if n == 0:
        return ["(no data)"]

    min_v = min(values)
    max_v = max(values)
    range_v = max_v - min_v if max_v != min_v else 1

    # 采样到指定宽度
    if n > width:
        step = n / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
        # 不足则用最后一个值填充
        while len(sampled) < width:
            sampled.append(sampled[-1])

    # 映射到行号（0 = 顶部 = 最大值）
    rows = []
    for row_idx in range(height):
        threshold_pct = 1 - (row_idx / (height - 1))  # 行 0 = 100% (最高)
        threshold = min_v + range_v * threshold_pct

        line_chars = []
        for i, v in enumerate(sampled):
            if v >= threshold:
                line_chars.append("█")
            else:
                # 检查是否接近阈值（斜线效果）
                next_v = sampled[i + 1] if i + 1 < len(sampled) else v
                avg = (v + next_v) / 2
                if avg >= threshold:
                    line_chars.append("▄")
                else:
                    line_chars.append(" ")

        rows.append("".join(line_chars))

    # 添加 y 轴标签
    labeled = []
    for i, row in enumerate(rows):
        if i == 0:
            label = f"{max_v:.2f}ms "
        elif i == height - 1:
            label = f"{min_v:.2f}ms "
        else:
            label = " " * 8
        labeled.append(f"{label}| {row}")

    labeled.append(" " * 8 + "+" + "-" * (len(sampled) + 1))
    labeled.append(" " * 8 + f"  共 {n} 次测试")

    return labeled


def print_chart(trend_file, benchmark_name):
    """打印单个基准项的趋势图。"""
    trend = load_json(trend_file)
    if not trend or not trend["history"]:
        print("❌ 无趋势数据", file=sys.stderr)
        sys.exit(2)

    values = [entry["results"][benchmark_name] for entry in trend["history"] if benchmark_name in entry["results"]]

    if not values:
        print(f"❌ 未找到基准项: {benchmark_name}", file=sys.stderr)
        print(f"可用项: {', '.join(sorted(set().union(*(e['results'].keys() for e in trend['history']))))}")
        sys.exit(2)

    print(f"\n📊 {benchmark_name} — 性能趋势")
    print(f"   共 {len(values)} 次测试")
    print(f"   范围: {min(values):.4f}ms ~ {max(values):.4f}ms")
    print()

    chart = ascii_chart(values, width=50, height=10)
    for line in chart:
        print(f"   {line}")
    print()


def main():
    parser = argparse.ArgumentParser(description="性能趋势追踪工具")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # add 命令
    add_p = subparsers.add_parser("add", help="添加基准记录")
    add_p.add_argument("--results", required=True, help="基准结果 JSON 文件")
    add_p.add_argument("--trend", default="benchmark-results/trend.json", help="趋势数据文件")
    add_p.add_argument("--commit", help="commit hash")
    add_p.add_argument("--version", help="版本号")

    # report 命令
    rep_p = subparsers.add_parser("report", help="生成趋势报告")
    rep_p.add_argument("--trend", default="benchmark-results/trend.json", help="趋势数据文件")
    rep_p.add_argument("--output", "-o", help="输出文件")

    # chart 命令
    chart_p = subparsers.add_parser("chart", help="打印单基准趋势图")
    chart_p.add_argument("--trend", default="benchmark-results/trend.json", help="趋势数据文件")
    chart_p.add_argument("--benchmark", required=True, help="基准项名称")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "add":
        add_entry(args.trend, args.results, args.commit, args.version)
    elif args.command == "report":
        generate_report(args.trend, args.output)
    elif args.command == "chart":
        print_chart(args.trend, args.benchmark)


if __name__ == "__main__":
    main()
