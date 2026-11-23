#!/usr/bin/env python3
"""rsi 禁用候选的组合层面前提检验（2026-09-16）

对「新增禁用 rsi 的 7 品种」做组合级 OOS 检验：
  基线 A = 当前生产 blacklist
  方案 B = 基线 + b/cs/hc/l/lh/ni/ru 额外禁用 rsi
在「后 1/3（OOS）」口径下对比全市场 pooled expR，确认改动真实改善而非单品种幻觉叠加。
另附后 1/2 口径交叉验证。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

CANDIDATES = ["b", "cs", "hc", "l", "lh", "ni", "ru"]
PROD_BL = fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}


def run(sym, df, blacklist):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    bl = set(blacklist.get(sym, []))
    cfg["strat_blacklist"] = {sym: sorted(bl)} if bl else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=df)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def pooled(syms, ratio, blacklist):
    agg = [0.0, 0]
    detail = {}
    for sym in syms:
        df = fd.load_daily(sym)
        if df is None or len(df) < 300:
            continue
        k = int(len(df) * ratio)
        test = df.iloc[k:]
        e, n = run(sym, test, blacklist)
        agg[0] += e * n
        agg[1] += n
        detail[sym] = {"expR": round(e, 4), "trades": n}
    return (agg[0] / agg[1] if agg[1] else 0.0), agg[1], detail


def main():
    syms = sorted(fd.SYMBOLS)

    # 方案 B blacklist
    bl_b = {s: list(v) for s, v in PROD_BL.items()}
    for s in CANDIDATES:
        bl_b.setdefault(s, [])
        if "rsi" not in bl_b[s]:
            bl_b[s] = bl_b[s] + ["rsi"]

    for ratio, label in ((2.0 / 3.0, "后1/3"), (0.5, "后1/2")):
        a_e, a_n, a_d = pooled(syms, ratio, PROD_BL)
        b_e, b_n, b_d = pooled(syms, ratio, bl_b)
        delta = b_e - a_e
        print(f"=== {label}（OOS）===")
        print(f"  A 基线       pooled expR = {a_e:+.4f}R  n={a_n}")
        print(f"  B +7禁用rsi  pooled expR = {b_e:+.4f}R  n={b_n}")
        print(f"  Δ(B-A) = {delta:+.4f}R")
        # 逐品种 Δ 只列候选 7 品种
        for s in CANDIDATES:
            if s in a_d and s in b_d:
                d = b_d[s]["expR"] - a_d[s]["expR"]
                print(f"    {s:4} A={a_d[s]['expR']:+.4f}(n={a_d[s]['trades']}) B={b_d[s]['expR']:+.4f}(n={b_d[s]['trades']}) Δ={d:+.4f}")
        print()

    out = os.path.join(os.path.dirname(HERE), "rsi_portfolio_check_result.json")
    json.dump(
        {"candidates": CANDIDATES, "blacklist_b": bl_b},
        open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2,
    )
    print(f"方案 B blacklist 落盘 {out}")


if __name__ == "__main__":
    main()
