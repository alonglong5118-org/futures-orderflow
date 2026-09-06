#!/usr/bin/env python3
"""批量回测策略标签生成器（策略标签体系 P0，2026-08-31）。

对全部 54 品种跑 walk_forward_backtest(export_evidence=True)，
每笔虚拟成交携带完整 strat_evidence + strategy 标签，
输出 backtest_strategy_labels.json 供品种×簇→期望R 分布分析。

用法：
    python tools_backtest_strategy_labels.py              # 全品种
    python tools_backtest_strategy_labels.py jd cu i      # 指定品种
    python tools_backtest_strategy_labels.py --tail 500   # 只看最近500根
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from four_dim_strategy import SYMBOLS, walk_forward_backtest

OUT_FILE = Path(__file__).parent / "backtest_strategy_labels.json"


def run_backtest(symbols, tail=None):
    results = {}
    total_trades = 0
    t0 = time.time()

    for idx, sym in enumerate(symbols, 1):
        name = SYMBOLS.get(sym, {}).get("name", sym)
        print(f"[{idx}/{len(symbols)}] {sym} ({name}) ...", end=" ", flush=True)
        try:
            r = walk_forward_backtest(sym, tail=tail, export_evidence=True)
        except Exception as e:
            print(f"ERROR: {e}")
            results[sym] = {"symbol": sym, "name": name, "trades": 0, "note": f"error: {e}"}
            continue

        n = r.get("trades", 0)
        total_trades += n
        exp_r = r.get("expR", 0)
        wr = r.get("win_rate", 0)
        print(f"{n} 笔, expR={exp_r:.3f}, 胜率={wr:.1%}")

        # trades_detail 保留完整 strat_evidence
        results[sym] = r

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"总计: {len(symbols)} 品种, {total_trades} 笔成交, 耗时 {elapsed:.1f}s")
    return results


def summarize(results):
    """快速汇总：品种×策略标签→期望R/胜率 透视表。"""
    rows = []
    for sym, r in results.items():
        trades_detail = r.get("trades_detail", [])
        if not trades_detail:
            continue
        by_strat = {}
        for t in trades_detail:
            strat = t.get("strategy", "无标签")
            by_strat.setdefault(strat, []).append(t["R_adj"])
        for strat, rs in by_strat.items():
            rows.append({
                "symbol": sym,
                "name": r.get("name", sym),
                "strategy": strat,
                "trades": len(rs),
                "expR": round(float(np.mean(rs)), 4),
                "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3),
            })
    return rows


def print_summary(rows):
    """打印透视表（按品种+策略排序）。"""
    if not rows:
        print("无数据")
        return
    # 按品种排序，同品种按期望R降序
    rows.sort(key=lambda x: (x["symbol"], -x["expR"]))
    cur_sym = None
    print(f"\n{'品种':<8} {'名称':<6} {'策略':<10} {'笔数':>4} {'期望R':>8} {'胜率':>7}")
    print("-" * 50)
    for r in rows:
        if r["symbol"] != cur_sym:
            cur_sym = r["symbol"]
            print()
        print(
            f"{r['symbol']:<8} {r['name']:<6} {r['strategy']:<10} "
            f"{r['trades']:>4} {r['expR']:>8.3f} {r['win_rate']:>7.1%}"
        )


def print_cluster_analysis(results):
    """簇贡献维度分析：各品种 trend/mean/seasonal 贡献占比与期望R的关系。"""
    print(f"\n{'='*60}")
    print("簇贡献分析（品种 × 主导簇 → 期望R）")
    print(f"{'='*60}")
    for sym, r in sorted(results.items()):
        trades_detail = r.get("trades_detail", [])
        if not trades_detail or len(trades_detail) < 5:
            continue
        # 按主导簇分组
        by_cluster = {}
        for t in trades_detail:
            ev = t.get("strat_evidence")
            if not ev:
                continue
            contrib = ev["cluster_contrib"]
            dominant = max(contrib, key=lambda k: abs(contrib[k]))
            by_cluster.setdefault(dominant, []).append(t["R_adj"])
        if len(by_cluster) < 2:
            continue
        name = r.get("name", sym)
        parts = []
        for cluster in ("trend", "mean", "seasonal"):
            rs = by_cluster.get(cluster, [])
            if rs:
                exp_r = float(np.mean(rs))
                parts.append(f"{cluster}({len(rs)}笔,R={exp_r:+.3f})")
        if len(parts) >= 2:
            print(f"  {sym:<6} {name:<6} {' | '.join(parts)}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tail = None
    if "--tail" in sys.argv:
        idx = sys.argv.index("--tail")
        tail = int(sys.argv[idx + 1])
        args = [a for a in args if a != str(tail)]

    if args:
        symbols = [s for s in args if s in SYMBOLS]
        if not symbols:
            print(f"未知品种: {args}")
            print(f"可用: {', '.join(sorted(SYMBOLS.keys()))}")
            sys.exit(1)
    else:
        symbols = sorted(SYMBOLS.keys())

    print(f"策略标签回测: {len(symbols)} 品种" + (f", tail={tail}" if tail else ""))
    print()

    results = run_backtest(symbols, tail=tail)

    # 保存完整结果
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, default=str)
    print(f"\n完整结果已保存: {OUT_FILE}")

    # 汇总透视表
    rows = summarize(results)
    print_summary(rows)

    # 簇贡献分析
    print_cluster_analysis(results)


if __name__ == "__main__":
    main()
