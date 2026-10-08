#!/usr/bin/env python3
"""5 策略联合方案的逐策略 ablation（2026-09-16）

从「5 策略全落地」出发，逐个移除单策略候选，看 pooled 变化。
若移除某策略后 pooled 反而上升 → 该策略在联合中为负贡献，应排除。
"""
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
    "turtle": ["AP", "PF", "RM", "c", "cs", "eb", "lc", "y", "zn"],
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
    all5 = set(ADD.keys())

    def evalset(subset):
        bl = build(subset)
        r = {}
        for ratio, label in ((2.0/3.0, "后1/3"), (0.5, "后1/2")):
            r[label] = pooled(syms, ratio, bl)[0]
        return r

    full = evalset(all5)
    print(f"全方案(5策略) pooled: 后1/3={full['后1/3']:+.4f}R  后1/2={full['后1/2']:+.4f}R\n")
    print(f"{'移除策略':10} {'后1/3':>10} {'Δ(后1/3)':>10} {'后1/2':>10} {'Δ(后1/2)':>10}  判断")

    report = {"full": full, "ablation": {}}
    for s in sorted(all5):
        sub = all5 - {s}
        e = evalset(sub)
        d13 = e["后1/3"] - full["后1/3"]
        d12 = e["后1/2"] - full["后1/2"]
        # 移除后 pooled 上升 = 该策略负贡献
        verdict = "负贡献?" if (d13 > 0 or d12 > 0) else "正贡献"
        report["ablation"][s] = {"pooled": e, "delta": {"后1/3": round(d13,4), "后1/2": round(d12,4)}}
        print(f"{s:10} {e['后1/3']:+.4f}R {d13:+10.4f} {e['后1/2']:+.4f}R {d12:+10.4f}  {verdict}")

    json.dump(report, open(os.path.join(os.path.dirname(HERE), "strategy_ablation_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n结果落盘 strategy_ablation_result.json")
    print("注：移除后 Δ>0 表示该策略在联合中拖后腿（负贡献），应排除")


if __name__ == "__main__":
    main()
