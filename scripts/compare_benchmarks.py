#!/usr/bin/env python3
"""性能基准对比工具。

对比两次性能基准测试结果，输出详细的差异报告，支持：
  · 逐项对比（当前 vs 基线 / A vs B）
  · 统计显著退化/提升项
  · 按变化幅度排序
  · 输出 Markdown / JSON 格式报告
  · 支持 CI 中调用（退出码表示是否有退化）

用法：
  # 对比当前结果与基线
  python scripts/compare_benchmarks.py --results benchmark-results/results.json --baseline tests/_perf_baseline.json

  # 对比两个结果文件
  python scripts/compare_benchmarks.py --a results_a.json --b results_b.json --name-a "v1.0" --name-b "v2.0"

  # 严格模式（有退化则退出码 1）
  python scripts/compare_benchmarks.py --results results.json --baseline baseline.json --strict

  # 输出到文件
  python scripts/compare_benchmarks.py ... --output report.md --format markdown
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 默认退化阈值（和 test_performance.py 保持一致）
DEFAULT_THRESHOLD = 0.5  # 50%


def load_json(path):
    """加载 JSON 文件。"""
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}", file=sys.stderr)
        sys.exit(2)
    with open(path) as f:
        return json.load(f)


def compare(a_data, b_data, name_a="A", name_b="B", threshold=DEFAULT_THRESHOLD):
    """对比两组基准数据。

    返回 dict:
      {
        "summary": {total, regressions, improvements, unchanged, new_in_a, new_in_b, only_in_a, only_in_b},
        "items": [
          {name, a_ms, b_ms, delta_pct, abs_delta_ms, status, category},
          ...
        ]
      }
    """
    all_names = sorted(set(a_data.keys()) | set(b_data.keys()))

    items = []
    regressions = 0
    improvements = 0
    unchanged = 0
    new_in_a = 0  # a 有 b 没有
    new_in_b = 0  # b 有 a 没有

    for name in all_names:
        a_ms = a_data.get(name)
        b_ms = b_data.get(name)

        if a_ms is not None and b_ms is not None and b_ms > 0:
            delta_pct = (a_ms / b_ms - 1) * 100
            abs_delta_ms = a_ms - b_ms

            if delta_pct > threshold * 100:
                status = "regression"  # 退化：a 比 b 慢
                regressions += 1
            elif delta_pct < -threshold * 100:
                status = "improvement"  # 提升：a 比 b 快
                improvements += 1
            else:
                status = "unchanged"
                unchanged += 1

            items.append(
                {
                    "name": name,
                    "a_ms": a_ms,
                    "b_ms": b_ms,
                    "delta_pct": delta_pct,
                    "abs_delta_ms": abs_delta_ms,
                    "status": status,
                    "category": "both",
                }
            )
        elif a_ms is not None and b_ms is None:
            items.append(
                {
                    "name": name,
                    "a_ms": a_ms,
                    "b_ms": None,
                    "delta_pct": None,
                    "abs_delta_ms": None,
                    "status": "new_in_a",
                    "category": "only_a",
                }
            )
            new_in_a += 1
        elif a_ms is None and b_ms is not None:
            items.append(
                {
                    "name": name,
                    "a_ms": None,
                    "b_ms": b_ms,
                    "delta_pct": None,
                    "abs_delta_ms": None,
                    "status": "new_in_b",
                    "category": "only_b",
                }
            )
            new_in_b += 1

    # 按变化幅度排序（退化在前，提升在后，按 |delta_pct| 降序）
    def sort_key(item):
        if item["status"] == "regression":
            return (0, -item["delta_pct"])
        elif item["status"] == "improvement":
            return (1, item["delta_pct"])
        elif item["status"] == "unchanged":
            return (2, -abs(item.get("delta_pct", 0) or 0))
        else:
            return (3, 0)

    items.sort(key=sort_key)

    summary = {
        "total": len(all_names),
        "regressions": regressions,
        "improvements": improvements,
        "unchanged": unchanged,
        "only_in_a": new_in_a,
        "only_in_b": new_in_b,
        "name_a": name_a,
        "name_b": name_b,
        "threshold": threshold,
    }

    return {"summary": summary, "items": items}


def format_ms(ms):
    if ms is None:
        return "—"
    return f"{ms:.4f}"


def status_icon(status):
    return {
        "regression": "🔴",
        "improvement": "🟢",
        "unchanged": "✅",
        "new_in_a": "🆕",
        "new_in_b": "⬜",
    }.get(status, "❓")


def status_label(status, name_a="A", name_b="B"):
    return {
        "regression": "退化",
        "improvement": "提升",
        "unchanged": "正常",
        "new_in_a": f"仅 {name_a}",
        "new_in_b": f"仅 {name_b}",
    }.get(status, status)


def generate_markdown(result, title="性能基准对比"):
    """生成 Markdown 格式对比报告。"""
    s = result["summary"]
    name_a = s["name_a"]
    name_b = s["name_b"]
    threshold = s["threshold"]

    lines = []
    lines.append(f"## {title}")
    lines.append("")

    # 概览
    lines.append("### 📊 概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 对比 | **{name_a}** vs **{name_b}** |")
    lines.append(f"| 总项数 | {s['total']} |")
    lines.append(f"| 🔴 退化 | {s['regressions']} 项 |")
    lines.append(f"| 🟢 提升 | {s['improvements']} 项 |")
    lines.append(f"| ✅ 正常 | {s['unchanged']} 项 |")
    if s["only_in_a"]:
        lines.append(f"| 🆕 仅 {name_a} | {s['only_in_a']} 项 |")
    if s["only_in_b"]:
        lines.append(f"| ⬜ 仅 {name_b} | {s['only_in_b']} 项 |")
    lines.append(f"| 退化阈值 | {threshold * 100:.0f}% |")
    lines.append("")

    # 退化项（如果有）
    regressions = [i for i in result["items"] if i["status"] == "regression"]
    if regressions:
        lines.append(f"### 🔴 性能退化（{len(regressions)} 项）")
        lines.append("")
        lines.append(f"| 基准项 | {name_a} (ms) | {name_b} (ms) | 变化 | 绝对变化 (ms) |")
        lines.append("|---|---|---|---|---|")
        for item in regressions:
            delta_str = f"{item['delta_pct']:+.1f}%"
            abs_str = f"{item['abs_delta_ms']:+.4f}"
            lines.append(
                f"| {item['name']} | {format_ms(item['a_ms'])} | {format_ms(item['b_ms'])} | {delta_str} | {abs_str} |"
            )
        lines.append("")

    # 提升项（如果有）
    improvements = [i for i in result["items"] if i["status"] == "improvement"]
    if improvements:
        lines.append(f"### 🟢 性能提升（{len(improvements)} 项）")
        lines.append("")
        lines.append(f"| 基准项 | {name_a} (ms) | {name_b} (ms) | 变化 | 绝对变化 (ms) |")
        lines.append("|---|---|---|---|---|")
        for item in improvements:
            delta_str = f"{item['delta_pct']:+.1f}%"
            abs_str = f"{item['abs_delta_ms']:+.4f}"
            lines.append(
                f"| {item['name']} | {format_ms(item['a_ms'])} | {format_ms(item['b_ms'])} | {delta_str} | {abs_str} |"
            )
        lines.append("")

    # 全部对比
    lines.append("### 📋 完整对比")
    lines.append("")
    lines.append(f"| 状态 | 基准项 | {name_a} (ms) | {name_b} (ms) | 变化 |")
    lines.append("|---|---|---|---|---|")
    for item in result["items"]:
        icon = status_icon(item["status"])
        label = status_label(item["status"], name_a, name_b)
        delta_str = f"{item['delta_pct']:+.1f}%" if item["delta_pct"] is not None else "—"
        lines.append(
            f"| {icon} {label} | {item['name']} | {format_ms(item['a_ms'])} | {format_ms(item['b_ms'])} | {delta_str} |"
        )
    lines.append("")

    # 说明
    lines.append("---")
    lines.append("")
    lines.append(f"> 退化阈值：{threshold * 100:.0f}%（变化超过此比例才标记为退化/提升）")
    lines.append(f"> 正值表示 {name_a} 比 {name_b} 慢（退化），负值表示更快（提升）")

    return "\n".join(lines)


def generate_json(result):
    """生成 JSON 格式对比结果。"""
    return json.dumps(result, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="性能基准对比工具")
    parser.add_argument("--a", help="A 组结果文件（当前/新版本）")
    parser.add_argument("--b", help="B 组结果文件（基线/旧版本）")
    parser.add_argument("--results", help="当前结果文件（等同 --a）")
    parser.add_argument("--baseline", help="基线文件（等同 --b）")
    parser.add_argument("--name-a", default="当前", help="A 组显示名称")
    parser.add_argument("--name-b", default="基线", help="B 组显示名称")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help=f"退化阈值（默认 {DEFAULT_THRESHOLD}，即 50%）"
    )
    parser.add_argument(
        "--format", choices=["markdown", "json", "both"], default="markdown", help="输出格式（默认 markdown）"
    )
    parser.add_argument("--output", "-o", help="输出文件路径（不指定则输出到 stdout）")
    parser.add_argument("--strict", action="store_true", help="严格模式：有退化则退出码 1")
    parser.add_argument("--summary-only", action="store_true", help="只输出概览，不输出完整表格")

    args = parser.parse_args()

    # 解析输入文件
    file_a = args.a or args.results
    file_b = args.b or args.baseline

    if not file_a:
        print("❌ 请指定 --a/--results", file=sys.stderr)
        sys.exit(2)
    if not file_b:
        print("❌ 请指定 --b/--baseline", file=sys.stderr)
        sys.exit(2)

    # 加载数据
    data_a = load_json(file_a)
    data_b = load_json(file_b)

    # 对比
    result = compare(data_a, data_b, args.name_a, args.name_b, args.threshold)
    s = result["summary"]

    # 生成输出
    if args.format in ("markdown", "both"):
        md = generate_markdown(result)
        if args.output:
            md_path = args.output
            if args.format == "both":
                md_path = args.output.rsplit(".", 1)[0] + ".md"
            with open(md_path, "w") as f:
                f.write(md)
        if args.format == "markdown":
            print(md)

    if args.format in ("json", "both"):
        js = generate_json(result)
        if args.output:
            json_path = args.output
            if args.format == "both":
                json_path = args.output.rsplit(".", 1)[0] + ".json"
            with open(json_path, "w") as f:
                f.write(js)
        if args.format == "json":
            print(js)

    # 严格模式
    if args.strict and s["regressions"] > 0:
        print(f"\n❌ 严格模式：{s['regressions']} 项性能退化", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
