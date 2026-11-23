#!/usr/bin/env python3
"""43 实盘品种口径的反向 ablation（2026-09-16）

验证最终方案（清理死品种 + v禁用ma_break + 保留lc/y改动）中每条「有效改动」都是正贡献。
死品种条目（a/b/eg/hc/m/rr/RM）不进 43 品种 pooled，清理后 pooled 不变，仅做结构清理。
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

DEAD = {"a", "b", "eg", "hc", "m", "rr", "RM"}  # 死品种条目，本轮清理
# 有效改动（相对生产的净变更，死品种清理除外）
EFFECTIVE = [
    ("v", "ma_break", "add"),      # 本轮贪心新增
    ("lc", "seasonal", "remove"),  # 上轮改动（43口径需复验）
    ("y", "ma_break", "add"),      # 上轮改动
    ("y", "dma", "add"),           # 上轮改动
    ("y", "seasonal", "add"),      # 上轮改动
]


def final_bl():
    bl = {s: set(v) for s, v in PROD_BL.items()}
    for sym in DEAD:
        bl.pop(sym, None)  # 清理死品种
    for sym, strat, kind in EFFECTIVE:
        bl.setdefault(sym, set())
        if kind == "add":
            bl[sym].add(strat)
        else:
            bl[sym].discard(strat)
    return bl


def live_syms():
    return [s for s in sorted(fd.SYMBOLS) if s not in fd.DISABLED_SYMBOLS]


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
    syms = live_syms()
    fin = pooled(final_bl(), syms)
    prod = pooled(PROD_BL, syms)  # 生产 blacklist（含死品种条目，但死品种不进 pooled）

    print("43 实盘品种口径 pooled：", flush=True)
    for w in WINDOWS:
        print(f"  {w:6} 生产 {prod[w][0]:+.4f}R → 最终 {fin[w][0]:+.4f}R  Δ={fin[w][0]-prod[w][0]:+.4f}R", flush=True)
    print()

    print("逐条回退（43口径，回退后 pooled 应 < 最终，Δ<0=正贡献）：", flush=True)
    print(f"{'回退条目':24} {'后1/3 Δ':>10} {'后1/2 Δ':>10} {'中1/3 Δ':>10}  判定", flush=True)
    for sym, strat, kind in EFFECTIVE:
        bl = final_bl()
        bl.setdefault(sym, set())
        if kind == "add":
            bl[sym].discard(strat)
        else:
            bl[sym].add(strat)
        e = pooled(bl, syms)
        d = {w: e[w][0] - fin[w][0] for w in WINDOWS}
        verdict = "正贡献" if all(v < 0 for v in d.values()) else "存疑/负贡献"
        print(f"{f'{sym} {strat} ({kind})':24} {d['后1/3']:+10.4f} {d['后1/2']:+10.4f} {d['中1/3']:+10.4f}  {verdict}", flush=True)

    json.dump({"final": {w: fin[w][0] for w in WINDOWS}, "prod": {w: prod[w][0] for w in WINDOWS}},
              open(os.path.join(os.path.dirname(HERE), "strategy_greedy_live_ablation_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n结果落盘 strategy_greedy_live_ablation_result.json", flush=True)


if __name__ == "__main__":
    main()
