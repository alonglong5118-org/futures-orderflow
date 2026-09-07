# -*- coding: utf-8 -*-
"""oppose 门阈值扫描：thr ∈ {30,40,50} 的 OOS 均值期望R + 二项显著性。
复用 oos_c_gate 的 run_backtest / IS-OOS 切分逻辑。"""
import os, json, copy
import numpy as np
import pandas as pd
import four_dim_strategy as fd
from oos_c_gate import run_backtest, split_is_oos, subset_stats, _load_kline_cache, GATE_THRESHOLD

HERE = os.path.dirname(os.path.abspath(__file__))
SPLIT = 0.6
cache = _load_kline_cache()
targets = [s for s in fd.SYMBOLS if s.upper() in cache and s not in ("SA01",)
           and s not in set(getattr(fd, "DISABLED_SYMBOLS", []) or [])]
targets.sort()

def oos_mean(sym, thr):
    r_base = run_backtest(sym, "dragon", None)
    r_ot = run_backtest(sym, "kline", "oppose_threshold", thr)
    bt = r_base.get("trades_detail") or []
    ot = r_ot.get("trades_detail") or []
    if not bt:
        return None
    dates = [pd.to_datetime(t["entry_date"]) for t in bt]
    lo, hi = min(dates), max(dates)
    cut = lo + SPLIT * (hi - lo)
    _, base_oos = split_is_oos(bt, cut)
    _, ot_oos = split_is_oos(ot, cut)
    bs = subset_stats(base_oos); os_ = subset_stats(ot_oos)
    if os_["expR"] is None or bs["expR"] is None:
        return None
    return {"base": bs["expR"], "ot": os_["expR"], "delta": round(os_["expR"] - bs["expR"], 4),
            "ot_n": os_["n"], "base_n": bs["n"]}

def binom_p(wins, losses):
    from math import comb
    n = wins + losses
    if n == 0:
        return None
    p = 0.5
    def pmf(x):
        return comb(n, x) * (p ** x) * ((1 - p) ** (n - x))
    pk = pmf(wins)
    return min(1.0, sum(pmf(x) for x in range(n + 1) if pmf(x) <= pk + 1e-18))

rows = []
for thr in (30, 40, 50):
    deltas, wins, losses, flats = [], 0, 0, 0
    base_means, ot_means = [], []
    for s in targets:
        r = oos_mean(s, thr)
        if r is None:
            continue
        d = r["delta"]
        deltas.append(d)
        base_means.append(r["base"]); ot_means.append(r["ot"])
        if d > 0: wins += 1
        elif d < 0: losses += 1
        else: flats += 1
    rows.append({
        "thr": thr,
        "n": len(deltas),
        "mean_delta": round(float(np.mean(deltas)), 4),
        "median_delta": round(float(np.median(deltas)), 4),
        "mean_base_oos": round(float(np.mean(base_means)), 4),
        "mean_ot_oos": round(float(np.mean(ot_means)), 4),
        "wins": wins, "losses": losses, "flats": flats,
        "win_ratio": round(wins / (wins + losses), 3) if (wins + losses) else None,
        "binom_p": binom_p(wins, losses),
    })

out = {"rows": rows, "targets_n": len(targets)}
with open(os.path.join(HERE, "oos_threshold_sweep.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

print(f"门可用品种 {len(targets)} 个\n")
print(f"{'thr':>4} {'n':>4} {'Δ均值':>8} {'Δ中位':>8} {'基线OOS':>9} {'opposeOOS':>10} {'增益':>4} {'有损':>4} {'持平':>4} {'占比':>6} {'p':>8}")
for r in rows:
    print(f"{r['thr']:>4} {r['n']:>4} {r['mean_delta']:>8} {r['median_delta']:>8} "
          f"{r['mean_base_oos']:>9} {r['mean_ot_oos']:>10} {r['wins']:>4} {r['losses']:>4} {r['flats']:>4} "
          f"{str(r['win_ratio']):>6} {str(r['binom_p']):>8}")
print("\n[已写] oos_threshold_sweep.json")
