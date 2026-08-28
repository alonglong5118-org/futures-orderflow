"""
全市场 GA 权重批量优化脚本 v3（串行加速版）
- 串行执行，绝对不卡
- tail 参数加速回测（默认 600 根，速度提升 5-7 倍）
- 断点续跑，每完成一个立即保存
- 失败自动跳过并记录

用法:
  python3 ga_batch_optimize.py --tail 600 --pop 25 --gen 10
  python3 ga_batch_optimize.py --only-group 黑色 --redo
  python3 ga_batch_optimize.py --dry-run
"""

import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ga_factor_miner as gfm
from four_dim_strategy import SYMBOLS, load_daily


def check_data_quality(symbol, min_bars=200):
    """检查数据量。"""
    try:
        df = load_daily(symbol)
        if df is None or len(df) < min_bars:
            return False, len(df) if df is not None else 0
        return True, len(df)
    except Exception:
        return False, 0


def optimize_one(symbol, pop_size, n_gen, tail):
    """跑单个品种的 GA 优化，带异常捕获。"""
    try:
        t0 = time.time()
        result = gfm.optimize_weights(symbol, pop_size=pop_size, n_gen=n_gen,
                                      verbose=False, tail=tail)
        elapsed = time.time() - t0
        return symbol, result, None, elapsed
    except Exception as e:
        tb = traceback.format_exc()
        return symbol, None, f"{e}\n{tb[:800]}", 0


def main():
    parser = argparse.ArgumentParser(description="全市场 GA 权重批量优化 v3（串行加速版）")
    parser.add_argument("--pop", type=int, default=25, help="种群大小")
    parser.add_argument("--gen", type=int, default=10, help="进化代数")
    parser.add_argument("--tail", type=int, default=600, help="回测使用尾部 N 根日线（0=全量）")
    parser.add_argument("--min-bars", type=int, default=200, help="最少日线数")
    parser.add_argument("--only-group", type=str, default="", help="只优化某个板块")
    parser.add_argument("--redo", action="store_true", help="重新优化所有（覆盖已有结果）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待优化品种")
    args = parser.parse_args()

    tail = args.tail if args.tail > 0 else None
    tail_str = f"tail={args.tail}" if tail else "全量数据"

    print("=" * 70)
    print("全市场 GA 权重批量优化 v3（串行加速版）")
    print(f"  pop={args.pop}  gen={args.gen}  {tail_str}")
    print("=" * 70)

    # 1. 筛选品种
    all_syms = sorted(SYMBOLS.keys())
    if args.only_group:
        all_syms = [s for s in all_syms if SYMBOLS.get(s, {}).get("group") == args.only_group]
        print(f"\n筛选板块: {args.only_group}, 候选 {len(all_syms)} 个")

    print(f"\n[1/4] 数据质量检查 ({len(all_syms)} 个候选)...")
    valid_syms = []
    skipped_data = []
    for i, sym in enumerate(all_syms):
        ok, n = check_data_quality(sym, args.min_bars)
        status = "✓" if ok else "✗"
        print(f"  [{i+1}/{len(all_syms)}] {sym}: {n} 根 {status}", end="\r", flush=True)
        if ok:
            valid_syms.append((sym, n))
        else:
            skipped_data.append((sym, n))
    print()

    if skipped_data:
        print(f"  数据不足跳过: {len(skipped_data)} 个 ({', '.join(s for s,_ in skipped_data[:10])})")

    # 2. 跳过已优化
    cache = {}
    cache_file = gfm.WEIGHTS_FILE
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            cache = json.load(f)

    if args.redo:
        todo = [s for s, _ in valid_syms]
        print(f"\n[2/4] 重新优化全部 {len(todo)} 个品种")
    else:
        done = [s for s, _ in valid_syms if s in cache]
        todo = [s for s, _ in valid_syms if s not in cache]
        print(f"\n[2/4] 已优化 {len(done)} 个，待优化 {len(todo)} 个")
        if done:
            print(f"  已完成: {', '.join(done[:10])}{'...' if len(done)>10 else ''}")

    if not todo:
        print("\n全部完成！")
        print_summary(cache)
        return

    if args.dry_run:
        print(f"\n待优化品种 ({len(todo)} 个):")
        print(f"  {', '.join(todo)}")
        # 预估时间
        est_per = 1.2  # 分钟/品种（tail=600, pop=25, gen=10 的经验值）
        est_total = len(todo) * est_per
        print(f"\n预计耗时: 约 {est_total:.0f} 分钟 ({est_total/60:.1f} 小时)")
        return

    # 3. 串行优化
    print(f"\n[3/4] 开始串行优化 ({len(todo)} 个品种)...")
    t_start = time.time()
    success_count = 0
    failed = []

    for idx, sym in enumerate(todo):
        print(f"\n[{idx+1}/{len(todo)}] {sym} 开始...", flush=True)
        sym, result, err, elapsed = optimize_one(sym, args.pop, args.gen, tail)

        if err:
            failed.append((sym, err[:200]))
            print(f"  ✗ 失败 ({elapsed:.0f}s): {err[:100]}", flush=True)
        else:
            cache[sym] = result
            gfm._save_weights(result)
            success_count += 1
            expR = result.get("best_expR", 0)
            wr = result.get("best_winrate", 0)
            rob = result.get("robust_score", 0)
            print(f"  ✓ 完成 ({elapsed:.0f}s): "
                  f"expR={expR:.4f}  win={wr*100:.1f}%  robust={rob:.3f}",
                  flush=True)

        # 进度
        elapsed_total = time.time() - t_start
        done_count = idx + 1
        remaining = len(todo) - done_count
        if done_count > 0:
            avg_time = elapsed_total / done_count
            eta = remaining * avg_time
            print(f"  进度: {done_count}/{len(todo)} ({done_count/len(todo)*100:.1f}%) "
                  f"已用 {elapsed_total/60:.1f}min  预计剩余 {eta/60:.1f}min",
                  flush=True)

    # 4. 汇总
    total_time = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"全部完成！总用时 {total_time/60:.1f} 分钟")
    print(f"  成功: {success_count}/{len(todo)}")
    if failed:
        print(f"  失败: {len(failed)}")
        for sym, err in failed:
            print(f"    - {sym}: {err[:100]}")

    print_summary(cache)


def print_summary(cache):
    """打印结果汇总表。"""
    print(f"\n{'='*70}")
    print(f"GA 优化结果汇总 (共 {len(cache)} 个品种)")
    print(f"{'='*70}")
    print(f"{'品种':<6} {'expR':>8} {'胜率':>8} {'稳健性':>8} {'T':>6} {'F':>6} {'C':>6} {'板块':<6}")
    print("-" * 62)

    sorted_syms = sorted(cache.items(), key=lambda x: -x[1].get("best_expR", 0))
    for sym, data in sorted_syms:
        bw = data.get("best_weights", {}).get("base", {})
        expR = data.get("best_expR", 0)
        wr = data.get("best_winrate", 0)
        rob = data.get("robust_score", 0)
        grp = SYMBOLS.get(sym, {}).get("group", "?")
        print(f"{sym:<6} {expR:>8.4f} {wr*100:>7.1f}% {rob:>8.3f} "
              f"{bw.get('T',0):>6.3f} {bw.get('F',0):>6.3f} {bw.get('C',0):>6.3f} {grp:<6}")

    print()
    # 按板块统计
    from collections import defaultdict
    groups = defaultdict(list)
    for sym, data in cache.items():
        grp = SYMBOLS.get(sym, {}).get("group", "其他")
        groups[grp].append(data.get("best_expR", 0))

    print("板块平均 expR:")
    for grp, expRs in sorted(groups.items(), key=lambda x: -sum(x[1])/len(x[1])):
        avg = sum(expRs) / len(expRs)
        print(f"  {grp}: {avg:.4f} (n={len(expRs)})")


if __name__ == "__main__":
    main()
