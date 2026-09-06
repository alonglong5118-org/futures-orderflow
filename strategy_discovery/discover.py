#!/usr/bin/env python3
"""策略发现 Agent — CLI 入口。

用法:
  # 验证一个假设（从 JSON 文件）
  python strategy_discovery/discover.py validate my_hypothesis.json

  # 快速验证（只跑 150 根）
  python strategy_discovery/discover.py validate my_hypothesis.json --tail 150

  # 指定品种
  python strategy_discovery/discover.py validate my_hypothesis.json --symbols rb,M,i

  # 验证后自动加入候选池
  python strategy_discovery/discover.py validate my_hypothesis.json --add-to-pool

  # 查看候选池
  python strategy_discovery/discover.py pool

  # 查看候选池（只看通过的）
  python strategy_discovery/discover.py pool --verdict pass

  # 导出假设为 Markdown
  python strategy_discovery/discover.py export <hypo_id> output.md
"""

import argparse
import json
import os
import sys

# 确保能 import 项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_discovery.hypothesis import Hypothesis, load_hypothesis_from_json
from strategy_discovery.validator import validate_hypothesis, print_validation_result, load_baseline
from strategy_discovery.candidate_pool import CandidatePool
from strategy_discovery.knowledge_miner import KnowledgeMiner


def cmd_validate(args):
    """验证一个策略假设。"""
    hypo = load_hypothesis_from_json(args.hypothesis)
    print(f"📋 假设: {hypo.name}")
    print(f"   描述: {hypo.description}")
    print(f"   来源: {hypo.source}")
    if hypo.target_symbols:
        print(f"   目标品种: {', '.join(hypo.target_symbols)}")
    print()

    # 加载基准
    baseline = load_baseline(args.baseline) if not args.no_baseline else None
    if baseline:
        print(f"📊 基准版本: {baseline.get('version', 'unknown')}")
    else:
        print("⚠️  无基准数据（跳过基准对比）")

    symbols = args.symbols.split(",") if args.symbols else None
    tail = args.tail if args.tail else None

    print(f"🔬 开始验证... 品种={symbols or '默认8个'}, tail={tail or '全部'}")
    print()

    result = validate_hypothesis(
        hypo,
        baseline=baseline,
        symbols=symbols,
        tail=tail,
        oos_ratio=args.oos_ratio,
    )

    print_validation_result(result, verbose=not args.quiet)

    if args.add_to_pool:
        pool = CandidatePool(args.pool)
        hypo_id = pool.add(result, status="candidate")
        print(f"✅ 已加入候选池: {hypo_id}")

    # 失败时返回非零退出码
    if result["verdict"] == "fail":
        sys.exit(1)


def cmd_pool(args):
    """查看候选池。"""
    pool = CandidatePool(args.pool)

    if args.stats:
        pool.print_summary()
        return

    items = pool.list(
        verdict=args.verdict if args.verdict != "all" else None,
        status=args.status if args.status != "all" else None,
        limit=args.limit,
    )

    if not items:
        print("候选池为空。")
        return

    print()
    print("=" * 80)
    print(f"  策略候选池  ({len(items)} 个)")
    print("=" * 80)
    print(f"  {'状态':<5}{'判定':<6}{'名称':<30}{'expR':>8}{'胜率':>8}"
          f"{'交易数':>7}{'来源':<15}")
    print("  " + "─" * 76)

    for item in items:
        hypo = item["hypothesis"]
        result = item["validation_result"]
        summary = result.get("summary", {})

        st_icon = {"candidate": "🟡", "accepted": "✅", "rejected": "❌",
                   "testing": "🔬"}.get(item.get("status", ""), "❓")
        v_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(result.get("verdict", ""), "?")

        name = hypo.get("name", "")[:28]
        avg_exp = f"{summary.get('avg_expR', 0):+.3f}" if summary.get("avg_expR") is not None else "  N/A"
        avg_wr = f"{summary.get('avg_win_rate', 0)*100:.1f}%" if summary.get("avg_win_rate") else " N/A"
        total_tr = summary.get("total_trades", 0)
        source = hypo.get("source", "")[:13]

        print(f"  {st_icon}   {v_icon}   {name:<30}{avg_exp:>8}{avg_wr:>8}"
              f"{total_tr:>7}{source:<15}")

    print("=" * 80)
    print()


def cmd_export(args):
    """导出单个假设为 Markdown。"""
    pool = CandidatePool(args.pool)
    md = pool.export_markdown(args.hypo_id)

    if not md:
        print(f"❌ 找不到假设: {args.hypo_id}")
        sys.exit(1)

    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(f"✅ 已导出到: {args.output}")
    else:
        print(md)


def cmd_mine(args):
    """从知识库扫描并生成假设草稿。"""
    vault = args.vault
    if not vault:
        # 尝试默认路径
        default_vault = os.path.expanduser("~/Documents/Obsidian Vault/知识库")
        if os.path.exists(default_vault):
            vault = default_vault
        else:
            print("❌ 找不到知识库路径，请用 --vault 指定")
            sys.exit(1)

    output_dir = args.output or "strategy_discovery/draft_hypotheses"

    print(f"⛏️  扫描知识库: {vault}")
    miner = KnowledgeMiner(vault)
    notes = miner.scan()
    print(f"   发现 {len(notes)} 篇相关笔记")

    # 统计
    by_type = {}
    for n in notes:
        by_type[n.note_type] = by_type.get(n.note_type, 0) + 1
    for t, c in sorted(by_type.items()):
        print(f"   - {t}: {c} 篇")

    print()
    print("📝 生成假设草稿...")
    generated = miner.generate_hypothesis_templates(output_dir)

    drafts = miner.list_drafts(output_dir)
    print(f"   已生成 {len(generated)} 个草稿 → {output_dir}/")
    print()

    if args.list:
        print_draft_list(drafts)


def print_draft_list(drafts):
    """打印草稿列表。"""
    if not drafts:
        print("  （没有草稿）")
        return

    print()
    print("=" * 80)
    print(f"  假设草稿  ({len(drafts)} 个)")
    print("=" * 80)
    print(f"  {'类型':<6}{'状态':<6}{'名称':<40}{'品种':<15}")
    print("  " + "─" * 76)

    for d in drafts:
        st_str = {"draft": "📝草稿", "ready": "✅就绪", "rejected": "❌废弃"}.get(d["status"], d["status"])
        syms = ",".join(d["symbols"][:3]) if d["symbols"] else "未指定"
        name = d["name"][:38]
        print(f"  {d['type']:<6}{st_str:<8}{name:<40}{syms:<15}")

    print("=" * 80)
    print(f"  下一步: 编辑草稿文件，填好 config_changes，然后 run validate")
    print()


def cmd_drafts(args):
    """列出草稿假设。"""
    from strategy_discovery.knowledge_miner import KnowledgeMiner
    miner = KnowledgeMiner("")
    drafts = miner.list_drafts(args.dir)
    print_draft_list(drafts)


def main():
    parser = argparse.ArgumentParser(description="策略发现 Agent")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # validate
    v = subparsers.add_parser("validate", help="验证一个策略假设")
    v.add_argument("hypothesis", help="假设 JSON 文件路径")
    v.add_argument("--tail", type=int, default=None, help="仅用尾部 N 根 K 线")
    v.add_argument("--symbols", type=str, default=None, help="品种列表，逗号分隔")
    v.add_argument("--oos-ratio", type=float, default=0.3, help="OOS 数据占比 (默认 0.3)")
    v.add_argument("--baseline", type=str, default="regression_baseline.json", help="基准文件路径")
    v.add_argument("--no-baseline", action="store_true", help="不加载基准（跳过对比）")
    v.add_argument("--add-to-pool", action="store_true", help="验证后加入候选池")
    v.add_argument("--pool", type=str, default="strategy_discovery/candidate_pool.json", help="候选池路径")
    v.add_argument("--quiet", action="store_true", help="只看汇总")

    # pool
    p = subparsers.add_parser("pool", help="查看候选池")
    p.add_argument("--verdict", type=str, default="all", choices=["all", "pass", "warn", "fail"],
                   help="按判定筛选")
    p.add_argument("--status", type=str, default="all",
                   choices=["all", "candidate", "accepted", "rejected", "testing"],
                   help="按状态筛选")
    p.add_argument("--limit", type=int, default=20, help="显示数量")
    p.add_argument("--stats", action="store_true", help="只看统计")
    p.add_argument("--pool", type=str, default="strategy_discovery/candidate_pool.json", help="候选池路径")

    # export
    e = subparsers.add_parser("export", help="导出假设为 Markdown")
    e.add_argument("hypo_id", help="假设 ID")
    e.add_argument("--output", "-o", type=str, default=None, help="输出文件路径")
    e.add_argument("--pool", type=str, default="strategy_discovery/candidate_pool.json", help="候选池路径")

    # mine
    m = subparsers.add_parser("mine", help="从知识库扫描生成假设草稿")
    m.add_argument("--vault", type=str, default=None, help="知识库路径")
    m.add_argument("--output", "-o", type=str, default="strategy_discovery/draft_hypotheses",
                   help="草稿输出目录")
    m.add_argument("--list", action="store_true", help="生成后列出草稿")

    # drafts
    d = subparsers.add_parser("drafts", help="列出草稿假设")
    d.add_argument("--dir", type=str, default="strategy_discovery/draft_hypotheses",
                   help="草稿目录")

    args = parser.parse_args()

    if args.command == "validate":
        cmd_validate(args)
    elif args.command == "pool":
        cmd_pool(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "mine":
        cmd_mine(args)
    elif args.command == "drafts":
        cmd_drafts(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
