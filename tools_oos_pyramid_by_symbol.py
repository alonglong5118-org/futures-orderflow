#!/usr/bin/env python3
"""金字塔 OOS 品种级分解：每品种 5 折验证（与 cluster_w 同款纪律）。

背景：全门控 OOS 通过（3/5 折，+33.80R），但品种分解两极分化——
SR/FG/ru/l/ss 大幅正贡献，cu/AP/p/pp 为负。cu 的反差最关键：
诊断层加仓 EV 第一（+0.214R），模块层最差（-0.348R/笔）。
原因：诊断测的是"加仓单位孤立 EV"，模块还改变了基础仓止损轨迹
（保本止损掐掉本可到 +2R 的赢单）——持仓层与信号层要分开校准的又一实证。

方法：每个白名单品种独立做 5 折时序切分，判据同 cluster_w：
≥3/5 折 Δ 为正 且 品种合计 Δ > 0 → 该品种保留在白名单。
"""

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from four_dim_strategy import (
    DEFAULT_CONFIG, ROLL_GAP_MULT, ROLL_GAP_PCT, _atr_array, exit_plan,
    get_slip_pts, load_daily,
)
from pyramid_addon import PYRAMID_LADDER, PYRAMID_TRAIL_DIST_R, PYRAMID_TRAIL_START_R, PYRAMID_WHITELIST
from tools_oos_pyramid import GATE_LABELS, GATE_REGIME, replay

LABELS_FILE = Path(__file__).parent / "backtest_strategy_labels.json"
N_FOLDS = 5


def main():
    with open(LABELS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    cfg = DEFAULT_CONFIG

    print("金字塔 OOS · 品种级 5 折验证")
    print(f"{'='*70}")

    keep, drop = [], []
    print(f"{'品种':<5} {'n':>4} {'Δ合计':>8} {'Δ均值':>9} {'正折':>5} {'折Δ(1..5)':>34} {'判定':>6}")
    for sym in sorted(PYRAMID_WHITELIST, key=lambda s: -len(data.get(s, {}).get("trades_detail", []))):
        r = data.get(sym, {})
        trades = r.get("trades_detail", [])
        if not trades:
            continue
        df = load_daily(sym)
        if df is None or len(df) < 100:
            continue
        dates = [str(x)[:10] for x in df.index]
        date_idx = {}
        for k, dt in enumerate(dates):
            date_idx.setdefault(dt, k)
        high, low, close, open_ = df["high"].values, df["low"].values, df["close"].values, df["open"].values
        arrays = {"high": high, "low": low, "close": close, "open": open_, "n": len(df), "dates": dates}
        atr_all = _atr_array(high, low, close, 14)

        # 门控子集（该品种内）
        recs = []
        for t in trades:
            if t.get("regime") != GATE_REGIME or t.get("strategy") not in GATE_LABELS:
                continue
            k = date_idx.get(str(t["entry_date"])[:10])
            if k is None or k < 1:
                continue
            entry = float(open_[k])
            atr_val = atr_all[k - 1]
            if atr_val is None or atr_val <= 0 or math.isnan(atr_val):
                continue
            out = replay(sym, arrays, k, t["dir"], entry, atr_val, t.get("regime", "震荡"), cfg, "pyramid")
            if out is None:
                continue
            recs.append({"date": str(t["entry_date"])[:10], "delta": out[0] - t["R_adj"]})

        if len(recs) < 15:
            print(f"{sym:<5} {len(recs):>4} {'样本不足（<15）':>40}")
            drop.append(sym)
            continue

        recs.sort(key=lambda x: x["date"])
        d_arr = np.array([x["delta"] for x in recs])
        n = len(d_arr)
        fold_size = n // N_FOLDS
        fold_sums = []
        for f in range(N_FOLDS):
            s = f * fold_size if f < N_FOLDS - 1 else (N_FOLDS - 1) * fold_size
            e = (f + 1) * fold_size if f < N_FOLDS - 1 else n
            fold_sums.append(float(d_arr[s:e].sum()))
        pos_folds = sum(1 for s in fold_sums if s > 0)
        total = float(d_arr.sum())
        ok = pos_folds >= 3 and total > 0
        (keep if ok else drop).append(sym)
        folds_str = " ".join(f"{s:+7.2f}" for s in fold_sums)
        print(f"{sym:<5} {n:>4} {total:>+8.2f} {d_arr.mean():>+9.4f} {pos_folds:>3}/5 {folds_str:>34} {'保留' if ok else '剔除':>6}")

    print()
    print(f"白名单 v2（品种级 OOS 通过）: {sorted(keep)}")
    print(f"剔除: {sorted(drop)}")
    print()

    # 修正后的全门控验证（仅保留品种）
    print("【白名单 v2 复验】仅保留品种的全门控 5 折")
    all_deltas = []
    for sym in keep:
        r = data.get(sym, {})
        df = load_daily(sym)
        if df is None:
            continue
        dates = [str(x)[:10] for x in df.index]
        date_idx = {}
        for k, dt in enumerate(dates):
            date_idx.setdefault(dt, k)
        high, low, close, open_ = df["high"].values, df["low"].values, df["close"].values, df["open"].values
        arrays = {"high": high, "low": low, "close": close, "open": open_, "n": len(df), "dates": dates}
        atr_all = _atr_array(high, low, close, 14)
        for t in r.get("trades_detail", []):
            if t.get("regime") != GATE_REGIME or t.get("strategy") not in GATE_LABELS:
                continue
            k = date_idx.get(str(t["entry_date"])[:10])
            if k is None or k < 1:
                continue
            entry = float(open_[k])
            atr_val = atr_all[k - 1]
            if atr_val is None or atr_val <= 0 or math.isnan(atr_val):
                continue
            out = replay(sym, arrays, k, t["dir"], entry, atr_val, t.get("regime", "震荡"), cfg, "pyramid")
            if out is None:
                continue
            all_deltas.append({"date": str(t["entry_date"])[:10], "delta": out[0] - t["R_adj"]})

    all_deltas.sort(key=lambda x: x["date"])
    d_arr = np.array([x["delta"] for x in all_deltas])
    n = len(d_arr)
    fold_size = n // N_FOLDS
    fold_sums = []
    for f in range(N_FOLDS):
        s = f * fold_size if f < N_FOLDS - 1 else (N_FOLDS - 1) * fold_size
        e = (f + 1) * fold_size if f < N_FOLDS - 1 else n
        fold_sums.append(float(d_arr[s:e].sum()))
    pos_folds = sum(1 for s in fold_sums if s > 0)
    print(f"  n={n}, Δ合计 {d_arr.sum():+.2f}R, 均值 {d_arr.mean():+.4f}R/笔, 改善比例 {(d_arr>0).mean():.1%}")
    print(f"  折Δ: {' '.join(f'{s:+.2f}' for s in fold_sums)}  → 正折 {pos_folds}/5")
    print(f"  判定: {'PASS ✅' if pos_folds >= 3 and d_arr.sum() > 0 else 'FAIL ❌'}")


if __name__ == "__main__":
    main()
