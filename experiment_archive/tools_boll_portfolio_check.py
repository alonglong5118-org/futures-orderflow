#!/usr/bin/env python3
"""boll 禁用候选 + y 恢复 的组合层面前提检验（2026-09-16）

候选来自 tools_boll_oos_calib.py 双窗口（后1/3 + 后1/2）交叉验证，只保留两窗口一致的：
  新增禁用 boll：UR/ag/al/ao/eg/ni（6 个）
  恢复 boll：    y（1 个）
注意：boll 整体 pooled 是「启用更优」（与 rsi 相反），故本检验必须确认
      这 6 品种禁用 boll 的组合动作是净改善而非逆势损害。

方案：
  A 基线 = 当前生产 blacklist
  B 方案 = 基线 + 6 品种禁用 boll + y 恢复 boll
在双 OOS 窗口下对比全市场 pooled expR。
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd

ADD_BOLL = ["UR", "ag", "al", "ao", "eg", "ni"]
REMOVE_BOLL = ["y"]
PROD_BL = fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}


def run(sym, df, blacklist):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    bl = set(blacklist.get(sym, []))
    cfg["strat_blacklist"] = {sym: sorted(bl)} if bl else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=df)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def build_b():
    bl_b = {s: list(v) for s, v in PROD_BL.items()}
    for s in ADD_BOLL:
        bl_b.setdefault(s, [])
        if "boll" not in bl_b[s]:
            bl_b[s] = bl_b[s] + ["boll"]
    for s in REMOVE_BOLL:
        if s in bl_b and "boll" in bl_b[s]:
            bl_b[s] = [x for x in bl_b[s] if x != "boll"]
            if not bl_b[s]:
                del bl_b[s]
    return bl_b


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
    bl_b = build_b()

    print("方案 B 的 blacklist 变化：")
    print(f"  新增禁用 boll: {ADD_BOLL}")
    print(f"  恢复 boll:     {REMOVE_BOLL}")
    print("  B 相对 A 的差异条目：")
    for s in sorted(set(list(PROD_BL) + list(bl_b))):
        a = PROD_BL.get(s, [])
        b = bl_b.get(s, [])
        if a != b:
            print(f"    {s:4} A={a} → B={b}")
    print()

    for ratio, label in ((2.0 / 3.0, "后1/3"), (0.5, "后1/2")):
        a_e, a_n, a_d = pooled(syms, ratio, PROD_BL)
        b_e, b_n, b_d = pooled(syms, ratio, bl_b)
        delta = b_e - a_e
        print(f"=== {label}（OOS）===")
        print(f"  A 基线       pooled expR = {a_e:+.4f}R  n={a_n}")
        print(f"  B +6禁boll-y恢复 pooled expR = {b_e:+.4f}R  n={b_n}")
        print(f"  Δ(B-A) = {delta:+.4f}R")
        for s in ADD_BOLL + REMOVE_BOLL:
            if s in a_d and s in b_d:
                d = b_d[s]["expR"] - a_d[s]["expR"]
                print(f"    {s:4} A={a_d[s]['expR']:+.4f}(n={a_d[s]['trades']}) B={b_d[s]['expR']:+.4f}(n={b_d[s]['trades']}) Δ={d:+.4f}")
        print()

    json.dump(
        {"add_boll": ADD_BOLL, "remove_boll": REMOVE_BOLL, "blacklist_b": bl_b},
        open(os.path.join(os.path.dirname(HERE), "boll_portfolio_check_result.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )
    print(f"方案 B blacklist 落盘 boll_portfolio_check_result.json")


if __name__ == "__main__":
    main()
