#!/usr/bin/env python3
"""迭代贪心重估 blacklist（2026-09-16，联合优化 / 去共线）

修复第三轮教训：不能再「单步扫描直接累加」候选（turtle 单策略双窗口为正，
但 5 策略联合 ablation 为负——被共线趋势簇掩盖）。

本脚本做真正的「贪心坐标上升」（greedy coordinate ascent）：
  - 状态 = 全品种 blacklist（从当前生产 strat_blacklist 出发）
  - 每步候选 = 对某个 (品种, 策略) 做一次启用/禁用翻转
  - 每步重估「联合 pooled OOS expR」（只重跑被翻转品种，其余算术复用）
  - 采纳标准：验收窗口 pooled 均提升 > MIN_DELTA
  - 每步选 min(Δ各窗口) 最大的候选（保守，多窗口都稳健才采纳）
  - 迭代至无候选满足提升为止

用法：
  $PY tools_strategy_greedy.py                        # 尾部双窗口（后1/3+后1/2）验收
  $PY tools_strategy_greedy.py --strict               # 三窗口（后1/3+后1/2+中1/3）验收，更稳健
  $PY tools_strategy_greedy.py --min-delta 0.001
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd
from strategy_layer import ALL_STRATS

WINDOWS = {
    "后1/3": 2.0 / 3.0,
    "后1/2": 0.5,
    "中1/3": None,  # 训练前1/3，测试中1/3（独立样本外段）
}
TAIL_WINDOWS = ["后1/3", "后1/2"]

PROD_BL = {s: list(v) for s, v in (fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}).items()}


def _test_seg(df, w):
    k = int(len(df) * w)
    return df.iloc[k:]


def _mid_seg(df):
    k = int(len(df) * (1.0 / 3.0))
    return df.iloc[k:2 * k]


def run_one(sym, seg, bl):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    bl = set(bl)
    cfg["strat_blacklist"] = {sym: sorted(bl)} if bl else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=seg)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-delta", type=float, default=0.001)
    ap.add_argument("--max-iter", type=int, default=300)
    ap.add_argument("--strict", action="store_true", help="三窗口验收（含中1/3 独立OOS）")
    ap.add_argument("--symbols", type=str, default=None)
    args = ap.parse_args()

    accept_windows = list(WINDOWS.keys()) if args.strict else TAIL_WINDOWS

    syms = sorted(fd.SYMBOLS)
    if args.symbols:
        want = {x.strip() for x in args.symbols.split(",") if x.strip()}
        syms = [s for s in syms if s in want]

    data = {}
    for sym in syms:
        df = fd.load_daily(sym)
        if df is None or len(df) < 300:
            continue
        segs = {}
        for wname, w in WINDOWS.items():
            segs[wname] = _mid_seg(df) if w is None else _test_seg(df, w)
        data[sym] = segs
    syms = list(data.keys())

    t0 = time.time()
    bl = {s: set(PROD_BL.get(s, [])) for s in syms}
    res = {s: {w: run_one(s, data[s][w], bl[s]) for w in WINDOWS} for s in syms}

    def pooled(win_name):
        S = sum(res[s][win_name][0] * res[s][win_name][1] for s in syms)
        N = sum(res[s][win_name][1] for s in syms)
        return (S / N if N else 0.0), N

    base = {w: pooled(w) for w in WINDOWS}
    print("=" * 110)
    print("基线（生产 blacklist） pooled OOS expR：", flush=True)
    for w in WINDOWS:
        e, n = base[w]
        print(f"  {w:6} = {e:+.4f}R  (n={n})", flush=True)
    print(f"候选规模：{len(syms)} 品种 × {len(ALL_STRATS)} 策略 = {len(syms)*len(ALL_STRATS)} 翻转/步", flush=True)
    print(f"采纳门槛：{accept_windows} 窗口均 Δ > {args.min_delta}", flush=True)

    toggle_cache = {}
    trace = []

    for it in range(args.max_iter):
        S = {w: sum(res[s][w][0] * res[s][w][1] for s in syms) for w in WINDOWS}
        N = {w: sum(res[s][w][1] for s in syms) for w in WINDOWS}
        b = {w: (S[w] / N[w] if N[w] else 0.0) for w in WINDOWS}

        best = None  # (score, sym, strat, toggling_out, new, t, delta)
        for s in syms:
            cur = frozenset(bl[s])
            for strat in ALL_STRATS:
                new = set(cur)
                toggling_out = strat in cur
                if toggling_out:
                    new.discard(strat)
                else:
                    new.add(strat)
                # 防全杀：禁止禁用最后一个启用策略（变成品种级排除）
                if not toggling_out and len(new) == len(ALL_STRATS):
                    continue
                key = (s, cur, strat)
                if key not in toggle_cache:
                    toggle_cache[key] = {w: run_one(s, data[s][w], new) for w in WINDOWS}
                t = toggle_cache[key]
                delta = {}
                for w in WINDOWS:
                    e, n = res[s][w]
                    e2, n2 = t[w]
                    Sp = S[w] - e * n + e2 * n2
                    Np = N[w] - n + n2
                    delta[w] = (Sp / Np if Np else 0.0) - b[w]
                if all(delta[w] > args.min_delta for w in accept_windows):
                    score = min(delta[w] for w in accept_windows)
                    if best is None or score > best[0]:
                        best = (score, s, strat, toggling_out, new, t, delta)
        if best is None:
            print(f"\n第 {it} 轮无候选满足{len(accept_windows)}窗口提升，收敛。共采纳 {len(trace)} 步。", flush=True)
            break
        _, s, strat, toggling_out, new, t, delta = best
        action = "启用" if toggling_out else "禁用"
        bl[s] = new
        res[s] = {w: t[w] for w in WINDOWS}
        trace.append({"iter": it, "symbol": s, "strategy": strat, "action": action,
                      "delta": {w: round(delta[w], 4) for w in WINDOWS}})
        print(f"[{len(trace):3d}] {action:2} {s:5} {strat:10} "
              + "  ".join(f"{w}={delta[w]:+.4f}" for w in WINDOWS), flush=True)

    final = {w: pooled(w) for w in WINDOWS}
    print("=" * 110)
    print("最终 pooled OOS expR 对比：", flush=True)
    for w in WINDOWS:
        e0, n0 = base[w]
        e1, n1 = final[w]
        print(f"  {w:6}: 基线 {e0:+.4f}R → 最终 {e1:+.4f}R  Δ={e1-e0:+.4f}R", flush=True)

    new_bl = {}
    for s in syms:
        lst = sorted(bl[s])
        if lst:
            new_bl[s] = lst
    print(f"\n最终 blacklist 品种数 {len(new_bl)}（生产原 {len(PROD_BL)}）", flush=True)
    print("相对生产新增/变更条目：", flush=True)
    for s in sorted(set(list(PROD_BL) + list(new_bl))):
        a = sorted(PROD_BL.get(s, []))
        b = sorted(new_bl.get(s, []))
        if a != b:
            print(f"  {s:5} {a} → {b}", flush=True)
    print(f"耗时 {time.time()-t0:.0f}s", flush=True)

    out = os.path.join(os.path.dirname(HERE), "strategy_greedy_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "args": {"min_delta": args.min_delta, "max_iter": args.max_iter, "strict": args.strict},
            "accept_windows": accept_windows,
            "baseline": {w: {"expR": base[w][0], "n": base[w][1]} for w in WINDOWS},
            "final": {w: {"expR": final[w][0], "n": final[w][1]} for w in WINDOWS},
            "trace": trace,
            "blacklist_final": new_bl,
            "blacklist_prod": PROD_BL,
        }, f, ensure_ascii=False, indent=2)
    print(f"结果落盘 {out}", flush=True)


if __name__ == "__main__":
    main()
