#!/usr/bin/env python3
"""
rescreen_all.py — 对已采集的固定月差序列用修正判据重跑初筛（2026-09-26）

背景：collect_all_spread.py 初筛原用「裸自相关 AC>0.9 剔除」，把几乎所有品种
误判为强趋势（固定月差天然高自相关）。修正为「去趋势负相关」后，直接对已落盘
的 *_fixed.csv 重算，无需重新采集。

判据（对齐 j/rb/hc 四重验证）：偏离 60 日均线 vs 未来 5 日变化相关性 ≤ -0.20。
"""
import os
import glob

import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

ROUGH_COST = {
    "j": 2.5, "jm": 2.5, "rb": 2.7, "hc": 2.7, "i": 1.5, "sf": 7.0, "sm": 7.0,
    "cu": 21.0, "al": 13.0, "zn": 13.0, "ni": 23.0, "pb": 13.0, "sn": 23.0,
    "sc": 20.2, "fu": 4.0, "bu": 6.0, "ru": 13.0, "ta": 7.0, "ma": 4.0, "pp": 4.0,
    "l": 4.0, "v": 4.0, "eg": 4.0, "sa": 5.0, "fg": 5.0,
    "p": 6.5, "y": 6.5, "m": 3.5, "rm": 3.5, "oi": 4.0, "cf": 14.0, "sr": 5.0,
    "ap": 7.0, "c": 3.2, "cs": 3.5, "a": 4.0, "sp": 7.0,
    "ss": 13.0, "eb": 3.0, "ur": 5.0, "cj": 13.0, "lh": 13.0, "pg": 8.0,
    "au": 10.04, "ag": 3.5,
}
SECTOR = {
    "j": "黑色系", "jm": "黑色系", "rb": "黑色系", "hc": "黑色系", "i": "黑色系",
    "sf": "黑色系", "sm": "黑色系", "ss": "黑色系",
    "cu": "有色", "al": "有色", "zn": "有色", "ni": "有色", "pb": "有色", "sn": "有色",
    "au": "贵金属", "ag": "贵金属",
    "sc": "能化", "fu": "能化", "bu": "能化", "ru": "能化", "ta": "能化", "ma": "能化",
    "pp": "能化", "l": "能化", "v": "能化", "eg": "能化", "sa": "能化", "fg": "能化",
    "ur": "能化", "eb": "能化", "pg": "能化",
    "p": "农产品", "y": "农产品", "m": "农产品", "rm": "农产品", "oi": "农产品",
    "cf": "农产品", "sr": "农产品", "ap": "农产品", "c": "农产品", "cs": "农产品",
    "a": "农产品", "sp": "农产品", "cj": "农产品", "lh": "农产品",
}


def screen(s, cost_per_ton):
    s = s.dropna().to_numpy(float)
    if len(s) < 60:
        return None
    ma = pd.Series(s).rolling(60).mean().to_numpy()
    dev = s - ma
    fut5 = pd.Series(s).shift(-5).to_numpy() - s
    mask = ~(np.isnan(dev) | np.isnan(fut5))
    d, f = dev[mask], fut5[mask]
    corr = np.corrcoef(d, f)[0, 1] if len(d) > 30 else 0.0
    daily_vol = np.std(np.diff(s))
    vol_cost_ratio = daily_vol / cost_per_ton if cost_per_ton > 0 else np.inf
    flags = []
    if corr > -0.20:
        flags.append("回归弱")
    if vol_cost_ratio < 1.0:
        flags.append("波动<成本")
    return {
        "去趋势负相关": round(corr, 3),
        "日波动": round(daily_vol, 2),
        "波动/成本": round(vol_cost_ratio, 2),
        "判定": "✅" if not flags else "❌",
        "备注": "; ".join(flags),
    }


def main():
    rows = []
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*_fixed.csv")))
    for fp in files:
        base = os.path.basename(fp)
        code = base.split("_")[0]
        # 解析月差：code_01vs05_fixed.csv → 01vs05
        rest = base[len(code) + 1:]
        month = rest.replace("_fixed.csv", "")
        if "vs" not in month:
            continue
        df = pd.read_csv(fp)
        if "spread" not in df.columns or len(df) < 60:
            continue
        r = screen(df["spread"], ROUGH_COST.get(code, 5.0))
        if r is None:
            continue
        r["品种"] = code
        r["板块"] = SECTOR.get(code, "")
        r["月差"] = month
        r["样本日"] = len(df)
        r["价差std"] = round(df["spread"].std(), 2)
        rows.append(r)

    df = pd.DataFrame(rows).sort_values("去趋势负相关")
    cols = ["品种", "板块", "月差", "样本日", "价差std", "去趋势负相关",
            "日波动", "波动/成本", "判定", "备注"]
    df = df[[c for c in cols if c in df.columns]]
    out = os.path.join(OUT_DIR, "screen_all_fixed.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    passed = df[df["判定"] == "✅"]
    print("=" * 90)
    print(f"修正判据重筛：去趋势负相关 ≤ -0.20 通过 {len(passed)} 组 / 共 {len(df)} 组")
    print("=" * 90)
    print(passed.to_string(index=False))
    print(f"\n完整表: {out}")

    # 按品种聚合：哪些品种至少有一组月差通过
    prod_pass = passed.groupby("品种").agg(月差数=("月差", "count"),
                                           最强负相关=("去趋势负相关", "min")).reset_index()
    prod_pass = prod_pass.sort_values("最强负相关")
    print("\n" + "=" * 90)
    print(f"通过初筛的品种（{len(prod_pass)} 个，按最强负相关排序）:")
    print(prod_pass.to_string(index=False))


if __name__ == "__main__":
    main()
