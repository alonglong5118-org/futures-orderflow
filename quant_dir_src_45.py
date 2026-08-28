#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase C1：全市场 45+ 品种 方向源分歧量化 T_D(日线) vs T_5m(近期5m窗口)。

口径（与 6 核心基线 38.9% 一致，扩展到全市场）：
  对每根日线 bar i，复算 sign(T_D) 与 sign(T_5m)：
    · T_D  = compute_T(日线截至 i)              —— 回测决策方向
    · T_5m = compute_T(截至 i 的近 W 根 5m)     —— 实盘 5m 代理（滚动近期窗口，贴近 live）
  W=2000 5m ≈ 7 交易日；早期样本不足 W 则用全部。统计 sign 不一致率。

输出：_dir_src_45.json + 终端汇总（按分歧率排序）。
"""
import json
import os

import numpy as np

import four_dim_strategy as fd

HERE = os.path.dirname(os.path.abspath(__file__))
W = 2000  # 近期5m窗口(根)
cfg = fd.DEFAULT_CONFIG

rep = json.load(open(f"{HERE}/_convert_min5_report.json"))["report"]
syms = [k for k, v in rep.items() if v.get("ok")]
print(f"=== Phase C1：方向源分歧量化（{len(syms)} 品种, W={W} 5m）===\n")

per = {}
overall_disc = 0
overall_tot = 0
for s in syms:
    df = fd.load_daily(s)
    df5 = fd.load_min5(s, fetch_if_missing=False)
    if df is None or df5 is None or len(df5) < 60:
        per[s] = dict(rate=float("nan"), disc=0, tot=0, note="无5m/日线")
        continue
    group = fd.SYMBOLS.get(s, {}).get("group")
    d5_start = df5.index[0].normalize()
    d5_end = df5.index[-1].normalize()
    n = len(df)
    disc = 0
    tot = 0
    for i in range(60, n):
        d = df.index[i]
        if d.normalize() < d5_start or d.normalize() > d5_end:
            continue
        try:
            TD = fd.compute_T(df.iloc[:i + 1], cfg, group, symbol=s)[0]
        except Exception:
            continue
        seg5 = df5[df5.index.normalize() <= d.normalize()]
        if len(seg5) > W:
            seg5 = seg5.iloc[-W:]
        if len(seg5) < 30:
            continue
        try:
            T5 = fd.compute_T(seg5, cfg, group, symbol=s)[0]
        except Exception:
            continue
        if TD == 0 or T5 == 0:
            continue
        tot += 1
        if np.sign(TD) != np.sign(T5):
            disc += 1
    rate = disc / tot if tot else float("nan")
    per[s] = dict(rate=rate, disc=disc, tot=tot)
    overall_disc += disc
    overall_tot += tot
    print(f"  {s:4} 分歧率={rate:6.1%}  ({disc}/{tot})", flush=True)

overall_rate = overall_disc / overall_tot if overall_tot else float("nan")
print(f"\n全样本分歧率={overall_rate:.1%} ({overall_disc}/{overall_tot})")

# 按分歧率排序输出
ranked = sorted([(k, v) for k, v in per.items() if v.get("tot", 0) > 0],
                key=lambda kv: (kv[1]["rate"] if kv[1]["rate"] == kv[1]["rate"] else -1),
                reverse=True)
out = {
    "W": W,
    "overall": dict(rate=overall_rate, disc=overall_disc, tot=overall_tot),
    "per_symbol": per,
    "ranked_high_to_low": [{"symbol": k, "rate": v["rate"], "disc": v["disc"], "tot": v["tot"]} for k, v in ranked],
}
json.dump(out, open(f"{HERE}/_dir_src_45.json", "w"), ensure_ascii=False, indent=2, default=str)
print(f"\n报告已写 _dir_src_45.json（{len(per)} 品种，全样本分歧率 {overall_rate:.1%}）")
