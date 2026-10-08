#!/usr/bin/env python3
"""第二轮 ablation：排除 turtle 后，对 4 策略方案逐策略检验（2026-09-16）"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

ADD = {
    "ma_break": ["ag", "cs", "l", "lc", "sp"],
    "dma": ["AP", "PF", "SA", "cs", "fu"],
    "pullback": ["OI", "a", "jd", "lh"],
    "seasonal": ["JM", "OI", "SR", "jd", "l", "lc", "rb", "rr", "sp", "v"],
}
PROD_BL = fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}


def run(sym, df, blacklist):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    bl = set(blacklist.get(sym, []))
    cfg["strat_blacklist"] = {sym: sorted(bl)} if bl else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=df)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def build(subset):
    bl = {s: list(v) for s, v in PROD_BL.items()}
    for strat, cands in ADD.items():
        if strat not in subset:
            continue
        for s in cands:
            bl.setdefault(s, [])
            if strat not in bl[s]:
                bl[s] = bl[s] + [strat]
    return bl


def pooled(syms, ratio, blacklist):
    agg = [0.0, 0]
    for sym in syms:
        df = fd.load_daily(sym)
        if df is None or len(df) < 300:
            continue
        k = int(len(df) * ratio)
        test = df.iloc[k:]
        e, n = run(sym, test, blacklist)
        agg[0] += e * n
        agg[1] += n
    return (agg[0] / agg[1] if agg[1] else 0.0), agg[1]


def main():
    syms = sorted(fd.SYMBOLS)
    all4 = set(ADD.keys())

    def evalset(subset):
        bl = build(subset)
        return {label: pooled(syms, ratio, bl)[0] for ratio, label in ((2.0/3.0, "后1/3"), (0.5, "后1/2"))}

    full = evalset(all4)
    print(f"4策略方案 pooled: 后1/3={full['后1/3']:+.4f}R  后1/2={full['后1/2']:+.4f}R\n")
    print(f"{'移除策略':10} {'后1/3':>10} {'Δ(后1/3)':>10} {'后1/2':>10} {'Δ(后1/2)':>10}  判断")

    for s in sorted(all4):
        e = evalset(all4 - {s})
        d13 = e["后1/3"] - full["后1/3"]
        d12 = e["后1/2"] - full["后1/2"]
        verdict = "负贡献?" if (d13 > 0 or d12 > 0) else "正贡献"
        print(f"{s:10} {e['后1/3']:+.4f}R {d13:+10.4f} {e['后1/2']:+.4f}R {d12:+10.4f}  {verdict}")


if __name__ == "__main__":
    main()
