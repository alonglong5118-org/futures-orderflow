"""
GA 权重 vs 默认权重 — 全市场回测对比

对每个品种分别跑两套权重的 walk-forward 回测，对比 expR/胜率/交易笔数。
结果保存为 JSON 并打印汇总表。

用法:
  python3 ga_vs_default_compare.py --tail 600
  python3 ga_vs_default_compare.py --only-group 化工
"""

import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ga_factor_miner as gfm
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)


def run_with_weights(symbol, weights_label, weights_dict, tail=None):
    """用指定权重跑回测。

    weights_dict: {"T": ..., "F": ..., "C": ...}
    返回 dict 结果
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["combine_weights"] = weights_dict

    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=300, tail=tail)
        return {
            "label": weights_label,
            "expR": round(float(r.get("expR", 0)), 4),
            "win_rate": round(float(r.get("win_rate", 0)), 4),
            "trades": int(r.get("trades", 0)),
            "total_R": round(float(r.get("total_R", 0)), 2),
            "max_dd": round(float(r.get("max_drawdown", 0)), 4),
        }
    except Exception as e:
        return {
            "label": weights_label,
            "expR": 0,
            "win_rate": 0,
            "trades": 0,
            "total_R": 0,
            "max_dd": 0,
            "error": str(e),
        }


def main():
    parser = argparse.ArgumentParser(description="GA权重 vs 默认权重 全市场回测对比")
    parser.add_argument("--tail", type=int, default=None, help="仅用尾部 N 根日线（加速）")
    parser.add_argument("--min-bars", type=int, default=200, help="最少日线数")
    parser.add_argument("--only-group", type=str, default="", help="只对比某个板块")
    parser.add_argument("--only-ga", action="store_true", help="只对比有 GA 权重的品种")
    parser.add_argument("--output", type=str, default="logs/ga_vs_default.json", help="结果输出文件")
    args = parser.parse_args()

    # 加载 GA 权重缓存
    ga_cache = {}
    ga_file = gfm.WEIGHTS_FILE
    if os.path.exists(ga_file):
        with open(ga_file, encoding="utf-8") as f:
            ga_cache = json.load(f)

    print("=" * 80)
    print("GA 权重 vs 默认权重 — 全市场回测对比")
    print(f"  GA 缓存品种: {len(ga_cache)}")
    if args.tail:
        print(f"  回测范围: 最近 {args.tail} 根日线")
    print("=" * 80)

    # 筛选品种
    all_syms = sorted(SYMBOLS.keys())
    if args.only_group:
        all_syms = [s for s in all_syms if SYMBOLS.get(s, {}).get("group") == args.only_group]
        print(f"\n筛选板块: {args.only_group}, 候选 {len(all_syms)} 个")

    if args.only_ga:
        all_syms = [s for s in all_syms if s in ga_cache]
        print(f"\n只对比有 GA 权重的品种: {len(all_syms)} 个")

    # 数据质量检查
    print(f"\n[1/3] 数据质量检查 ({len(all_syms)} 个候选)...")
    valid_syms = []
    for i, sym in enumerate(all_syms):
        try:
            df = load_daily(sym)
            if df is not None and len(df) >= args.min_bars:
                valid_syms.append((sym, len(df)))
                status = "✓"
            else:
                status = "✗"
        except Exception:
            status = "✗"
        print(f"  [{i + 1}/{len(all_syms)}] {sym} {status}", end="\r", flush=True)
    print()

    if not valid_syms:
        print("没有有效品种")
        return

    # 跑回测
    print(f"\n[2/3] 回测对比 ({len(valid_syms)} 个品种)...")
    results = {}
    t_start = time.time()

    for idx, (sym, n_bars) in enumerate(valid_syms):
        group = SYMBOLS.get(sym, {}).get("group", "?")
        has_ga = sym in ga_cache

        # 默认权重
        default_w = {"T": 0.6, "F": 0.25, "C": 0.15}
        r_default = run_with_weights(sym, "default", default_w, tail=args.tail)

        # GA 权重（如果有）
        if has_ga:
            ga_w = ga_cache[sym].get("best_weights", {}).get("base", default_w)
            r_ga = run_with_weights(sym, "ga", ga_w, tail=args.tail)
        else:
            r_ga = None

        results[sym] = {
            "group": group,
            "bars": n_bars,
            "has_ga": has_ga,
            "default": r_default,
            "ga": r_ga,
        }

        # 打印进度
        expR_d = r_default["expR"]
        if r_ga:
            expR_g = r_ga["expR"]
            delta = expR_g - expR_d
            delta_pct = (delta / abs(expR_d) * 100) if expR_d != 0 else float("inf")
            arrow = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "→")
            ga_tag = "GA"
        else:
            expR_g = 0
            delta = 0
            delta_pct = 0
            arrow = "—"
            ga_tag = "  "

        print(
            f"  [{idx + 1}/{len(valid_syms)}] {sym:<6} {group:<4} "
            f"默认={expR_d:>7.4f} {ga_tag}={expR_g:>7.4f} {arrow} "
            f"{delta:+.4f} ({delta_pct:+.0f}%)",
            flush=True,
        )

    total_time = time.time() - t_start

    # 汇总
    print(f"\n{'=' * 80}")
    print("[3/3] 汇总")
    print(f"{'=' * 80}")
    print(
        f"总品种: {len(valid_syms)} | 有GA权重: {sum(1 for r in results.values() if r['has_ga'])} | "
        f"总耗时: {total_time / 60:.1f}min"
    )

    # 有 GA 权重的品种对比
    ga_syms = {s: r for s, r in results.items() if r["has_ga"] and r["ga"]}
    if ga_syms:
        print(f"\n--- 有 GA 权重的品种对比 ({len(ga_syms)} 个) ---")
        print(
            f"{'品种':<6} {'板块':<5} {'默认expR':>9} {'GA expR':>9} {'差值':>8} {'变化%':>8} "
            f"{'默认胜率':>8} {'GA胜率':>8} {'默认笔数':>8} {'GA笔数':>8}"
        )
        print("-" * 82)

        improved = 0
        worsened = 0
        unchanged = 0
        total_delta = 0
        total_default_expR = 0
        total_ga_expR = 0

        for sym in sorted(ga_syms.keys(), key=lambda s: -(ga_syms[s]["ga"]["expR"] - ga_syms[s]["default"]["expR"])):
            r = ga_syms[sym]
            d = r["default"]
            g = r["ga"]
            delta = g["expR"] - d["expR"]
            delta_pct = (delta / abs(d["expR"]) * 100) if d["expR"] != 0 else float("inf")

            if delta > 0.01:
                improved += 1
            elif delta < -0.01:
                worsened += 1
            else:
                unchanged += 1

            total_delta += delta
            total_default_expR += d["expR"]
            total_ga_expR += g["expR"]

            print(
                f"{sym:<6} {r['group']:<5} {d['expR']:>9.4f} {g['expR']:>9.4f} "
                f"{delta:>+8.4f} {delta_pct:>+7.0f}% "
                f"{d['win_rate'] * 100:>7.1f}% {g['win_rate'] * 100:>7.1f}% "
                f"{d['trades']:>8} {g['trades']:>8}"
            )

        avg_default = total_default_expR / len(ga_syms)
        avg_ga = total_ga_expR / len(ga_syms)
        avg_delta = avg_ga - avg_default
        avg_delta_pct = (avg_delta / abs(avg_default) * 100) if avg_default != 0 else 0

        print("-" * 82)
        print(f"{'平均':<6} {'':<5} {avg_default:>9.4f} {avg_ga:>9.4f} {avg_delta:>+8.4f} {avg_delta_pct:>+7.0f}%")
        print()
        print(f"  提升: {improved} 个 | 下降: {worsened} 个 | 持平: {unchanged} 个")
        print(f"  平均 expR 变化: {avg_delta:+.4f} ({avg_delta_pct:+.1f}%)")

    # 无 GA 权重的品种（用默认权重的表现）
    no_ga_syms = {s: r for s, r in results.items() if not r["has_ga"]}
    if no_ga_syms:
        print(f"\n--- 无 GA 权重的品种（用默认权重）({len(no_ga_syms)} 个) ---")
        pos = sum(1 for r in no_ga_syms.values() if r["default"]["expR"] > 0)
        neg = sum(1 for r in no_ga_syms.values() if r["default"]["expR"] < 0)
        avg_expR = sum(r["default"]["expR"] for r in no_ga_syms.values()) / len(no_ga_syms)
        print(f"  expR > 0: {pos} 个 | expR < 0: {neg} 个")
        print(f"  平均 expR: {avg_expR:.4f}")

    # 按板块汇总（有 GA 的）
    if ga_syms:
        from collections import defaultdict

        groups = defaultdict(lambda: {"default": [], "ga": []})
        for sym, r in ga_syms.items():
            groups[r["group"]]["default"].append(r["default"]["expR"])
            groups[r["group"]]["ga"].append(r["ga"]["expR"])

        print("\n--- 板块平均 expR 对比（有 GA 权重的品种） ---")
        print(f"{'板块':<6} {'品种数':>6} {'默认avg':>9} {'GA avg':>9} {'变化':>8} {'变化%':>8}")
        print("-" * 55)
        for grp in sorted(groups.keys(), key=lambda g: -(sum(groups[g]["ga"]) / len(groups[g]["ga"]))):
            data = groups[grp]
            n = len(data["default"])
            d_avg = sum(data["default"]) / n
            g_avg = sum(data["ga"]) / n
            delta = g_avg - d_avg
            delta_pct = (delta / abs(d_avg) * 100) if d_avg != 0 else 0
            print(f"{grp:<6} {n:>6} {d_avg:>9.4f} {g_avg:>9.4f} {delta:>+8.4f} {delta_pct:>+7.0f}%")

    # 保存结果
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": {
                    "total": len(valid_syms),
                    "with_ga": len(ga_syms),
                    "without_ga": len(no_ga_syms),
                    "improved": improved if ga_syms else 0,
                    "worsened": worsened if ga_syms else 0,
                    "avg_default_expR": avg_default if ga_syms else 0,
                    "avg_ga_expR": avg_ga if ga_syms else 0,
                    "avg_delta": avg_delta if ga_syms else 0,
                    "avg_delta_pct": avg_delta_pct if ga_syms else 0,
                },
                "results": results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\n详细结果已保存到: {args.output}")


if __name__ == "__main__":
    main()
