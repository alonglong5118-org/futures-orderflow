"""
权重融合比例扫描（OOS 验证）

对不同 alpha（GA 权重占比）：w = alpha * GA + (1-alpha) * 默认
跑 OOS 回测，找 OOS 平均 expR 最高的 alpha。

用法:
  python3 ga_blend_sweep.py --oos-file logs/ga_oos_validation.json
"""

import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest


def run_backtest(df_slice, symbol, weights_dict, window=150):
    """在指定数据切片上跑回测。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["combine_weights"] = weights_dict
    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=window, df_in=df_slice)
        return float(r.get("expR", 0)), int(r.get("trades", 0))
    except Exception:
        return 0.0, 0


def main():
    parser = argparse.ArgumentParser(description="权重融合比例 OOS 扫描")
    parser.add_argument("--oos-file", type=str, default="logs/ga_oos_validation.json",
                        help="OOS 验证结果文件（取 GA 权重和数据范围）")
    parser.add_argument("--alphas", type=str, default="0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0",
                        help="扫描的 alpha 值（逗号分隔）")
    parser.add_argument("--output", type=str, default="logs/ga_blend_sweep.json",
                        help="结果输出")
    args = parser.parse_args()

    alphas = [float(a) for a in args.alphas.split(",")]

    # 加载 OOS 结果（取每个品种的 GA 权重）
    with open(args.oos_file, encoding="utf-8") as f:
        oos_data = json.load(f)

    cfg = oos_data["config"]
    results = oos_data["results"]
    train_bars = cfg["train_bars"]
    oos_bars = cfg["oos_bars"]
    window = cfg["window"]
    total_bars = train_bars + oos_bars

    print("=" * 70)
    print("权重融合比例 OOS 扫描")
    print(f"  训练: {train_bars}根 | OOS: {oos_bars}根 | window: {window}")
    print(f"  品种数: {len(results)}")
    print(f"  扫描 alpha: {alphas}")
    print("=" * 70)

    default_w = {"T": 0.6, "F": 0.25, "C": 0.15}

    # 预加载所有数据
    print(f"\n[1/3] 加载数据...")
    sym_data = {}
    for sym in results:
        df_full = load_daily(sym)
        if df_full is None or len(df_full) < total_bars:
            continue
        df = df_full.tail(total_bars).copy()
        oos_start = train_bars - window
        df_oos = df.iloc[oos_start:].copy()
        ga_w = results[sym].get("ga_weights", default_w)
        sym_data[sym] = {"df_oos": df_oos, "ga_w": ga_w, "group": results[sym]["group"]}

    print(f"  有效品种: {len(sym_data)} 个")

    # 扫每个 alpha
    print(f"\n[2/3] 扫描 alpha...")
    sweep_results = {}
    t_start = time.time()

    for alpha in alphas:
        alpha_expRs = {}
        alpha_trades = {}
        total_expR = 0

        for sym, data in sym_data.items():
            ga_w = data["ga_w"]
            # 融合权重
            blend_w = {
                "T": round(alpha * ga_w["T"] + (1 - alpha) * default_w["T"], 6),
                "F": round(alpha * ga_w["F"] + (1 - alpha) * default_w["F"], 6),
                "C": round(alpha * ga_w["C"] + (1 - alpha) * default_w["C"], 6),
            }
            expR, trades = run_backtest(data["df_oos"], sym, blend_w, window=window)
            alpha_expRs[sym] = expR
            alpha_trades[sym] = trades
            total_expR += expR

        avg_expR = total_expR / len(sym_data)
        n_pos = sum(1 for v in alpha_expRs.values() if v > 0)
        n_neg = sum(1 for v in alpha_expRs.values() if v < 0)

        sweep_results[alpha] = {
            "avg_expR": round(avg_expR, 4),
            "n_pos": n_pos,
            "n_neg": n_neg,
            "per_symbol": alpha_expRs,
        }

        elapsed = time.time() - t_start
        print(f"  α={alpha:.1f}: avg_expR={avg_expR:+.4f} pos={n_pos} neg={n_neg} ({elapsed:.0f}s)",
              flush=True)

    # 汇总
    print(f"\n{'='*70}")
    print(f"[3/3] 汇总")
    print(f"{'='*70}")
    print(f"{'alpha':>6} {'avg_expR':>10} {'变化(vs默认)':>14} {'正收益':>6} {'负收益':>6}")
    print("-" * 50)

    baseline = sweep_results[0.0]["avg_expR"]
    best_alpha = None
    best_expR = -999

    for alpha in sorted(sweep_results.keys()):
        r = sweep_results[alpha]
        delta = r["avg_expR"] - baseline
        delta_pct = (delta / abs(baseline) * 100) if baseline != 0 else 0
        marker = " ← 最优" if r["avg_expR"] > best_expR else ""
        if r["avg_expR"] > best_expR:
            best_expR = r["avg_expR"]
            best_alpha = alpha
        print(f"{alpha:>6.1f} {r['avg_expR']:>+10.4f} {delta:>+9.4f} ({delta_pct:>+6.1f}%) "
              f"{r['n_pos']:>6} {r['n_neg']:>6}{marker}")

    print()
    print(f"最优 alpha: {best_alpha} (avg_expR={best_expR:.4f})")
    print(f"相比纯默认: {best_expR - baseline:+.4f} ({(best_expR-baseline)/abs(baseline)*100 if baseline!=0 else 0:+.1f}%)")
    print(f"相比纯GA:  {best_expR - sweep_results[1.0]['avg_expR']:+.4f}")

    # 按板块最优 alpha
    from collections import defaultdict
    print(f"\n--- 各板块最优 alpha ---")
    groups = defaultdict(list)
    for sym, data in sym_data.items():
        groups[data["group"]].append(sym)

    print(f"{'板块':<6} {'品种数':>5} {'默认avg':>9} {'纯GA avg':>9} {'最优α':>6} {'最优avg':>9} {'提升':>8}")
    print("-" * 60)
    for grp in sorted(groups.keys(), key=lambda g: -sweep_results[best_alpha]["avg_expR"]):
        syms = groups[grp]
        d_avg = sum(sweep_results[0.0]["per_symbol"][s] for s in syms) / len(syms)
        g_avg = sum(sweep_results[1.0]["per_symbol"][s] for s in syms) / len(syms)

        best_g_alpha = 0
        best_g_expR = -999
        for alpha in alphas:
            avg = sum(sweep_results[alpha]["per_symbol"][s] for s in syms) / len(syms)
            if avg > best_g_expR:
                best_g_expR = avg
                best_g_alpha = alpha

        delta = best_g_expR - d_avg
        print(f"{grp:<6} {len(syms):>5} {d_avg:>+9.4f} {g_avg:>+9.4f} "
              f"{best_g_alpha:>6.1f} {best_g_expR:>+9.4f} {delta:>+8.4f}")

    # 保存
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "alphas": alphas,
            "best_alpha": best_alpha,
            "best_avg_expR": best_expR,
            "baseline_avg_expR": baseline,
            "sweep": {str(k): v for k, v in sweep_results.items()},
        }, f, ensure_ascii=False, indent=2)

    print(f"\n详细结果已保存到: {args.output}")


if __name__ == "__main__":
    main()
