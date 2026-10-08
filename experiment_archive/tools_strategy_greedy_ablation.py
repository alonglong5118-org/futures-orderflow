#!/usr/bin/env python3
"""贪心最终方案的逐条反向 ablation（2026-09-16）

从 5 步最终方案出发，逐条回退，确认每条在联合语境下仍是正贡献
（回退后 pooled 应下降，即最终方案 pooled > 回退方案 pooled）。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

WINDOWS = {"后1/3": 2.0 / 3.0, "后1/2": 0.5, "中1/3": None}
PROD_BL = {s: list(v) for s, v in (fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}).items()}

# 5 步最终方案（相对生产的增量）
MOVES = [
    ("lc", "seasonal", "remove"),   # 启用 seasonal（从黑名单移除）
    ("y", "ma_break", "add"),        # 禁用
    ("y", "dma", "add"),
    ("y", "seasonal", "add"),
    ("RM", "turtle", "add"),
]


def final_bl():
    bl = {s: set(v) for s, v in PROD_BL.items()}
    for sym, strat, kind in MOVES:
        bl.setdefault(sym, set())
        if kind == "add":
            bl[sym].add(strat)
        else:
            bl[sym].discard(strat)
    return bl


def _seg(df, w):
    if w is None:
        k = int(len(df) * (1.0 / 3.0))
        return df.iloc[k:2 * k]
    k = int(len(df) * w)
    return df.iloc[k:]


def run_one(sym, seg, bl):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    blset = set(bl)
    cfg["strat_blacklist"] = {sym: sorted(blset)} if blset else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=seg)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def pooled(bl, syms):
    out = {}
    for wname, w in WINDOWS.items():
        S = N = 0
        for s in syms:
            df = fd.load_daily(s)
            if df is None or len(df) < 300:
                continue
            e, n = run_one(s, _seg(df, w), bl.get(s, []))
            S += e * n
            N += n
        out[wname] = (S / N if N else 0.0), N
    return out


def main():
    syms = sorted(fd.SYMBOLS)
    fin = pooled(final_bl(), syms)
    base = pooled(PROD_BL, syms)

    print("基线 vs 最终方案 pooled：", flush=True)
    for w in WINDOWS:
        print(f"  {w:6} 基线 {base[w][0]:+.4f}R → 最终 {fin[w][0]:+.4f}R  Δ={fin[w][0]-base[w][0]:+.4f}R", flush=True)
    print()

    print("逐条回退 ablation（回退后 pooled 应 < 最终，即 Δ<0 表示该条正贡献）：", flush=True)
    print(f"{'回退条目':24} {'后1/3 Δ':>10} {'后1/2 Δ':>10} {'中1/3 Δ':>10}  判定", flush=True)
    report = {}
    for i, (sym, strat, kind) in enumerate(MOVES):
        bl = final_bl()
        bl.setdefault(sym, set())
        if kind == "add":
            bl[sym].discard(strat)  # 回退 = 恢复该策略
        else:
            bl[sym].add(strat)
        e = pooled(bl, syms)
        d = {w: e[w][0] - fin[w][0] for w in WINDOWS}
        # 全部窗口 Δ<0 = 正贡献（回退导致 pooled 下降）
        verdict = "正贡献" if all(v < 0 for v in d.values()) else "存疑/负贡献"
        report[i] = {"move": [sym, strat, kind], "delta": {w: round(d[w], 4) for w in WINDOWS}}
        print(f"{f'{sym} {strat} ({kind})':24} {d['后1/3']:+10.4f} {d['后1/2']:+10.4f} {d['中1/3']:+10.4f}  {verdict}", flush=True)

    json.dump({"final": {w: fin[w][0] for w in WINDOWS}, "ablation": report},
              open(os.path.join(os.path.dirname(HERE), "strategy_greedy_ablation_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n结果落盘 strategy_greedy_ablation_result.json", flush=True)


if __name__ == "__main__":
    main()
