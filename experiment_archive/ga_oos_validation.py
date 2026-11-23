"""
GA 权重样本外验证（Walk-Forward OOS）

方案：
  - 总数据：取 600 根日线
  - 训练集：前 400 根 → 跑 GA 优化权重
  - 验证集：后 200 根 → 用训练出的权重跑回测，对比默认权重
  - walk-forward window = 200（验证集用 200 根 warmup + 200 根 OOS）

用法:
  python3 ga_oos_validation.py --pop 25 --gen 10
  python3 ga_oos_validation.py --only-ga  # 只跑当前缓存里有的品种
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
from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest


def run_backtest_on_slice(df_slice, symbol, weights_dict, window=200):
    """在指定数据切片上跑回测。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["combine_weights"] = weights_dict

    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=window, df_in=df_slice)
        return {
            "expR": round(float(r.get("expR", 0)), 4),
            "win_rate": round(float(r.get("win_rate", 0)), 4),
            "trades": int(r.get("trades", 0)),
            "total_R": round(float(r.get("total_R", 0)), 2),
            "max_drawdown": round(float(r.get("max_drawdown", 0)), 4),
        }
    except Exception as e:
        return {"expR": 0, "win_rate": 0, "trades": 0, "total_R": 0, "max_drawdown": 0, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="GA权重样本外验证（前400训练/后200验证）")
    parser.add_argument("--pop", type=int, default=25, help="GA种群大小")
    parser.add_argument("--gen", type=int, default=10, help="GA进化代数")
    parser.add_argument("--train-bars", type=int, default=400, help="训练集长度")
    parser.add_argument("--oos-bars", type=int, default=200, help="验证集长度")
    parser.add_argument("--window", type=int, default=200, help="walk-forward窗口")
    parser.add_argument("--min-bars", type=int, default=600, help="最少总日线数")
    parser.add_argument("--only-ga", action="store_true", help="只跑当前缓存里有的品种")
    parser.add_argument("--output", type=str, default="logs/ga_oos_validation.json", help="结果输出文件")
    args = parser.parse_args()

    total_bars = args.train_bars + args.oos_bars

    print("=" * 80)
    print("GA 权重样本外验证（Walk-Forward OOS）")
    print(f"  训练: 前 {args.train_bars} 根 | 验证: 后 {args.oos_bars} 根")
    print(f"  GA: pop={args.pop} gen={args.gen} | window={args.window}")
    print("=" * 80)

    # 加载当前 GA 缓存（用于 --only-ga 筛选）
    ga_cache = {}
    ga_file = gfm.WEIGHTS_FILE
    if os.path.exists(ga_file):
        with open(ga_file, encoding="utf-8") as f:
            ga_cache = json.load(f)

    # 筛选品种
    all_syms = sorted(SYMBOLS.keys())
    if args.only_ga:
        all_syms = [s for s in all_syms if s in ga_cache]
        print(f"\n只跑有 GA 缓存的品种: {len(all_syms)} 个")

    # 数据质量检查
    print(f"\n[1/4] 数据质量检查 ({len(all_syms)} 个候选, 需≥{args.min_bars}根)...")
    valid_syms = []
    for i, sym in enumerate(all_syms):
        try:
            df = load_daily(sym)
            if df is not None and len(df) >= args.min_bars:
                valid_syms.append((sym, df))
                status = "✓"
            else:
                status = "✗"
        except Exception:
            status = "✗"
        print(f"  [{i + 1}/{len(all_syms)}] {sym} {status}", end="\r", flush=True)
    print()

    if not valid_syms:
        print("没有足够数据的品种")
        return

    # 跑 OOS 验证
    print(f"\n[2/4] GA 训练 + OOS 验证 ({len(valid_syms)} 个品种)...")
    results = {}
    t_start = time.time()

    default_w = {"T": 0.6, "F": 0.25, "C": 0.15}

    for idx, (sym, df_full) in enumerate(valid_syms):
        group = SYMBOLS.get(sym, {}).get("group", "?")

        # 取最近 total_bars 根（最新的在末尾）
        df = df_full.tail(total_bars).copy()

        # 训练集：前 train_bars 根
        df_train = df.iloc[: args.train_bars].copy()
        # 验证集：从 train_bars - window 开始（给 walk-forward 留 warmup），到末尾
        # 即 window 根 warmup + oos_bars 根 OOS
        oos_start = args.train_bars - args.window
        df_oos = df.iloc[oos_start:].copy()

        # 1. 在训练集上跑 GA 优化
        try:
            ga_result = gfm.optimize_weights(
                sym,
                df_daily=df_train,
                pop_size=args.pop,
                n_gen=args.gen,
                verbose=False,
                tail=None,  # tail=None 因为 df_train 已经是训练集
            )
            ga_w = ga_result.get("best_weights", {}).get("base", default_w)
            train_expR = ga_result.get("best_expR", 0)
            train_robust = ga_result.get("robust_score", 0)
            ga_ok = True
        except Exception as e:
            ga_w = default_w
            train_expR = 0
            train_robust = 0
            ga_ok = False
            print(f"  [{idx + 1}/{len(valid_syms)}] {sym}: GA训练失败: {e}", flush=True)

        # 2. 在训练集上跑默认权重（样本内对比）
        r_train_default = run_backtest_on_slice(df_train, sym, default_w, window=args.window)

        # 3. 在 OOS 验证集上跑 GA 权重
        r_oos_ga = run_backtest_on_slice(df_oos, sym, ga_w, window=args.window)

        # 4. 在 OOS 验证集上跑默认权重
        r_oos_default = run_backtest_on_slice(df_oos, sym, default_w, window=args.window)

        results[sym] = {
            "group": group,
            "total_bars": len(df),
            "ga_ok": ga_ok,
            "ga_weights": ga_w,
            "train": {
                "default": r_train_default,
                "ga_expR": train_expR,
                "ga_robust": train_robust,
            },
            "oos": {
                "default": r_oos_default,
                "ga": r_oos_ga,
            },
        }

        # 打印
        oos_d = r_oos_default["expR"]
        oos_g = r_oos_ga["expR"]
        delta = oos_g - oos_d
        if oos_d != 0:
            delta_pct = delta / abs(oos_d) * 100
        elif oos_g > 0:
            delta_pct = float("inf")
        else:
            delta_pct = 0

        arrow = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "→")

        print(
            f"  [{idx + 1}/{len(valid_syms)}] {sym:<6} {group:<4} "
            f"训练:默认={r_train_default['expR']:>7.4f}/GA={train_expR:>7.4f} | "
            f"OOS:默认={oos_d:>7.4f}/GA={oos_g:>7.4f} {arrow}{delta:+.4f} ({delta_pct:+.0f}%)",
            flush=True,
        )

    total_time = time.time() - t_start

    # 汇总
    print(f"\n{'=' * 80}")
    print("[3/4] OOS 汇总")
    print(f"{'=' * 80}")
    print(f"总品种: {len(valid_syms)} | 总耗时: {total_time / 60:.1f}min")

    # OOS 对比
    print("\n--- 样本外（OOS）对比 ---")
    print(
        f"{'品种':<6} {'板块':<5} {'OOS默认':>9} {'OOS_GA':>9} {'差值':>8} {'变化%':>8} "
        f"{'IS默认':>9} {'IS_GA':>9} {'过拟合':>8}"
    )
    print("-" * 85)

    improved_oos = 0
    worsened_oos = 0
    unchanged_oos = 0
    total_oos_default = 0
    total_oos_ga = 0
    overfit_count = 0  # IS提升但OOS下降

    for sym in sorted(
        results.keys(), key=lambda s: -(results[s]["oos"]["ga"]["expR"] - results[s]["oos"]["default"]["expR"])
    ):
        r = results[sym]
        oos_d = r["oos"]["default"]["expR"]
        oos_g = r["oos"]["ga"]["expR"]
        is_d = r["train"]["default"]["expR"]
        is_g = r["train"]["ga_expR"]

        delta_oos = oos_g - oos_d
        delta_is = is_g - is_d
        if oos_d != 0:
            delta_pct = delta_oos / abs(oos_d) * 100
        else:
            delta_pct = 0

        if delta_oos > 0.01:
            improved_oos += 1
        elif delta_oos < -0.01:
            worsened_oos += 1
        else:
            unchanged_oos += 1

        # 过拟合判断：样本内提升 > 0.05 但样本外下降
        if delta_is > 0.05 and delta_oos < -0.01:
            overfit_count += 1
            of_tag = "⚠是"
        else:
            of_tag = "否"

        total_oos_default += oos_d
        total_oos_ga += oos_g

        print(
            f"{sym:<6} {r['group']:<5} {oos_d:>9.4f} {oos_g:>9.4f} "
            f"{delta_oos:>+8.4f} {delta_pct:>+7.0f}% "
            f"{is_d:>9.4f} {is_g:>9.4f} {of_tag:>8}"
        )

    n = len(results)
    avg_oos_default = total_oos_default / n
    avg_oos_ga = total_oos_ga / n
    avg_delta = avg_oos_ga - avg_oos_default
    avg_delta_pct = (avg_delta / abs(avg_oos_default) * 100) if avg_oos_default != 0 else 0

    print("-" * 85)
    print(f"{'平均':<6} {'':<5} {avg_oos_default:>9.4f} {avg_oos_ga:>9.4f} {avg_delta:>+8.4f} {avg_delta_pct:>+7.0f}%")
    print()
    print(f"  OOS 提升: {improved_oos} 个 | 下降: {worsened_oos} 个 | 持平: {unchanged_oos} 个")
    print(f"  OOS 平均 expR 变化: {avg_delta:+.4f} ({avg_delta_pct:+.1f}%)")
    print(f"  过拟合迹象(IS↑OOS↓): {overfit_count} 个")

    # 按板块 OOS 对比
    from collections import defaultdict

    groups = defaultdict(lambda: {"default": [], "ga": []})
    for sym, r in results.items():
        groups[r["group"]]["default"].append(r["oos"]["default"]["expR"])
        groups[r["group"]]["ga"].append(r["oos"]["ga"]["expR"])

    print("\n--- 板块 OOS 平均 expR ---")
    print(f"{'板块':<6} {'品种数':>6} {'默认avg':>9} {'GA avg':>9} {'变化':>8} {'变化%':>8}")
    print("-" * 55)
    for grp in sorted(groups.keys(), key=lambda g: -(sum(groups[g]["ga"]) / max(len(groups[g]["ga"]), 1))):
        data = groups[grp]
        n_g = len(data["default"])
        d_avg = sum(data["default"]) / n_g
        g_avg = sum(data["ga"]) / n_g
        delta = g_avg - d_avg
        delta_pct = (delta / abs(d_avg) * 100) if d_avg != 0 else 0
        print(f"{grp:<6} {n_g:>6} {d_avg:>9.4f} {g_avg:>9.4f} {delta:>+8.4f} {delta_pct:>+7.0f}%")

    # 保存
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "config": {
                    "train_bars": args.train_bars,
                    "oos_bars": args.oos_bars,
                    "window": args.window,
                    "pop": args.pop,
                    "gen": args.gen,
                },
                "summary": {
                    "total": n,
                    "improved_oos": improved_oos,
                    "worsened_oos": worsened_oos,
                    "unchanged_oos": unchanged_oos,
                    "overfit_count": overfit_count,
                    "avg_oos_default": round(avg_oos_default, 4),
                    "avg_oos_ga": round(avg_oos_ga, 4),
                    "avg_delta": round(avg_delta, 4),
                    "avg_delta_pct": round(avg_delta_pct, 2),
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
