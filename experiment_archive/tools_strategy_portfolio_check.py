#!/usr/bin/env python3
"""6 策略品种级校准的逐策略组合检验（2026-09-16）

对 ma_break/dma/turtle/donchian/pullback/seasonal 各自的双窗口一致禁用候选，
做单策略组合层面前提检验：A=当前基线 vs B=基线+候选禁用该策略，双窗口 pooled 对比。
注意：pooled 整体多为「启用更优」，候选是逆势动作，须确认净改善才采纳。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

# 双窗口一致禁用候选（2/3 与 1/2 窗口建议禁用该策略的交集）
CANDIDATES = {
    "ma_break": ["ag", "cs", "l", "lc", "sp"],
    "dma": ["AP", "PF", "SA", "cs", "fu"],
    "turtle": ["AP", "PF", "RM", "c", "cs", "eb", "lc", "y", "zn"],
    "donchian": ["AP", "PF", "cs", "eb", "y"],
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


def build_b(strat, cands):
    bl_b = {s: list(v) for s, v in PROD_BL.items()}
    for s in cands:
        bl_b.setdefault(s, [])
        if strat not in bl_b[s]:
            bl_b[s] = bl_b[s] + [strat]
    return bl_b


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
    report = {}
    for strat, cands in CANDIDATES.items():
        bl_b = build_b(strat, cands)
        report[strat] = {"candidates": cands, "windows": {}}
        print(f"########## {strat}（候选 {len(cands)} 个）##########")
        for ratio, label in ((2.0 / 3.0, "后1/3"), (0.5, "后1/2")):
            a_e, a_n = pooled(syms, ratio, PROD_BL)
            b_e, b_n = pooled(syms, ratio, bl_b)
            d = b_e - a_e
            report[strat]["windows"][label] = {"A": round(a_e, 4), "B": round(b_e, 4), "delta": round(d, 4)}
            print(f"  {label}: A={a_e:+.4f}R → B={b_e:+.4f}R  Δ={d:+.4f}R")
        print()

    json.dump(report, open(os.path.join(os.path.dirname(HERE), "strategy_portfolio_check_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("结果落盘 strategy_portfolio_check_result.json")


if __name__ == "__main__":
    main()
