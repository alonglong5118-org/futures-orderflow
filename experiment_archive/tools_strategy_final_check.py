#!/usr/bin/env python3
"""5 策略（donchian 已排除）联合组合检验（2026-09-16）

将 ma_break/dma/turtle/pullback/seasonal 的双窗口一致禁用候选合并落地，
对比 A 基线（当前生产）的 OOS 双窗口 pooled，验证跨策略共线下仍是净改善。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

# 逐策略双窗口一致候选（donchian 已排除）
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


def build_b():
    bl_b = {s: list(v) for s, v in PROD_BL.items()}
    for strat, cands in ADD.items():
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
    bl_b = build_b()

    print("联合方案 B 相对 A 的差异条目：")
    for s in sorted(set(list(PROD_BL) + list(bl_b))):
        a = PROD_BL.get(s, [])
        b = bl_b.get(s, [])
        if a != b:
            print(f"  {s:4} A={a} → B={b}")
    print()

    for ratio, label in ((2.0 / 3.0, "后1/3"), (0.5, "后1/2")):
        a_e, a_n = pooled(syms, ratio, PROD_BL)
        b_e, b_n = pooled(syms, ratio, bl_b)
        print(f"=== {label}（OOS）===")
        print(f"  A 基线     pooled expR = {a_e:+.4f}R  n={a_n}")
        print(f"  B 5策略联合 pooled expR = {b_e:+.4f}R  n={b_n}")
        print(f"  Δ(B-A) = {b_e - a_e:+.4f}R")
        print()

    json.dump({"add": ADD, "blacklist_b": bl_b},
              open(os.path.join(os.path.dirname(HERE), "strategy_final_check_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("联合方案落盘 strategy_final_check_result.json")


if __name__ == "__main__":
    main()
