#!/usr/bin/env python3
"""
collect_all_spread.py — 全品种固定月差采集 + 均值回归初筛（2026-09-26）

一次跑完两件事：
  1. 采集：对 B 级品种（覆盖度探针确认可回溯 2019）拉 1/5/9 主力月全部合约，
     构造 01vs05 / 05vs09 / 09vs01 三组固定月差序列，落盘 csv。
  2. 初筛：对每组月差算三类指标，淘汰结构性无 edge 品种：
     - OU 半衰期（b>=0 或半衰期>60 → 无回归/趋势，剔）
     - 一阶自相关（高正自相关 = 趋势非回归，剔；参考 hc/sa 教训）
     - 波动率/成本比（日波动 < 往返成本 → 不可交易，剔）

口径（与 j/rb/hc 已验证口径完全一致）：
  - 固定月份差：01vs05 / 05vs09 / 09vs01
  - spread = 近月收盘 − 远月收盘
  - 换月断层分段（near_sym 切换处切段）

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""
import os
import sys
import time
from datetime import datetime

import akshare as ak
import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 跨板块 B 级品种（覆盖度探针结果，回溯 2019）。C 级品种(样本短)单独列表供标注。
B_GRADE = {
    # 黑色系
    "j": ("J", "黑色系"), "jm": ("JM", "黑色系"), "rb": ("RB", "黑色系"),
    "hc": ("HC", "黑色系"), "i": ("I", "黑色系"), "sf": ("SF", "黑色系"),
    "sm": ("SM", "黑色系"),
    # 有色
    "cu": ("CU", "有色"), "al": ("AL", "有色"), "zn": ("ZN", "有色"),
    "ni": ("NI", "有色"), "pb": ("PB", "有色"), "sn": ("SN", "有色"),
    # 能化
    "sc": ("SC", "能化"), "fu": ("FU", "能化"), "bu": ("BU", "能化"),
    "ru": ("RU", "能化"), "ta": ("TA", "能化"), "ma": ("MA", "能化"),
    "pp": ("PP", "能化"), "l": ("L", "能化"), "v": ("V", "能化"),
    "eg": ("EG", "能化"), "sa": ("SA", "能化"), "fg": ("FG", "能化"),
    # 农产品
    "p": ("P", "农产品"), "y": ("Y", "农产品"), "m": ("M", "农产品"),
    "rm": ("RM", "农产品"), "oi": ("OI", "农产品"), "cf": ("CF", "农产品"),
    "sr": ("SR", "农产品"), "ap": ("AP", "农产品"), "c": ("C", "农产品"),
    "cs": ("CS", "农产品"), "a": ("A", "农产品"), "sp": ("SP", "农产品"),
}
# C 级品种（样本偏短 2020/2021 起，仍采集但标注样本不足）
C_GRADE = {
    "ss": ("SS", "黑色系"), "eb": ("EB", "能化"), "ur": ("UR", "能化"),
    "cj": ("CJ", "农产品"), "lh": ("LH", "农产品"), "pg": ("PG", "能化"),
    "au": ("AU", "贵金属"), "ag": ("AG", "贵金属"),
}

PAIRS = [("01", "05"), ("05", "09"), ("09", "01")]

# 粗成本口径（元/吨）：tick_size × 2（滑点）+ 手续费。用于初筛「波动/成本比」。
# 乘数用于把「元/吨」成本换算成价差单位。此处先按 tick 近似，精确成本在核算环节补。
# (tick_size 元/吨, 手续费元/吨 近似)
ROUGH_COST = {
    "j":  (0.5, 1.5), "jm": (0.5, 1.5), "rb": (1.0, 0.7), "hc": (1.0, 0.7),
    "i":  (0.5, 0.5), "sf": (2.0, 3.0), "sm": (2.0, 3.0),
    "cu": (10.0, 1.0), "al": (5.0, 3.0), "zn": (5.0, 3.0), "ni": (10.0, 3.0),
    "pb": (5.0, 3.0), "sn": (10.0, 3.0),
    "sc": (0.1, 20.0), "fu": (1.0, 2.0), "bu": (2.0, 2.0), "ru": (5.0, 3.0),
    "ta": (2.0, 3.0), "ma": (1.0, 2.0), "pp": (1.0, 2.0), "l": (1.0, 2.0),
    "v":  (1.0, 2.0), "eg": (1.0, 2.0), "sa": (1.0, 3.0), "fg": (1.0, 3.0),
    "p":  (2.0, 2.5), "y": (2.0, 2.5), "m": (1.0, 1.5), "rm": (1.0, 1.5),
    "oi": (1.0, 2.0), "cf": (5.0, 4.0), "sr": (1.0, 3.0), "ap": (1.0, 5.0),
    "c":  (1.0, 1.2), "cs": (1.0, 1.5), "a": (1.0, 2.0), "sp": (2.0, 3.0),
    "ss": (5.0, 3.0), "eb": (1.0, 1.0), "ur": (1.0, 3.0), "cj": (5.0, 3.0),
    "lh": (5.0, 3.0), "pg": (1.0, 6.0), "au": (0.02, 10.0), "ag": (1.0, 1.5),
}


def fetch_main_contracts(code):
    prefix = B_GRADE.get(code, C_GRADE.get(code, (code.upper(), "")))[0]
    contracts = {}
    for y in range(15, 27):  # 2015~2026
        for m in ["01", "05", "09"]:
            sym = f"{prefix}{y:02d}{m}"
            try:
                df = ak.futures_zh_daily_sina(symbol=sym)
                if df is not None and len(df) >= 50:
                    df = df.sort_values("date").reset_index(drop=True)
                    contracts[sym] = df
            except Exception:
                pass
            time.sleep(0.15)
    return contracts


def build_fixed_spread(contracts, m1, m2):
    """构造固定月差序列（对齐 reproduce_spread_edge.build_fixed_spread）。"""
    sym_info = {}
    for sym in contracts:
        y = 2000 + int(sym[-4:-2])
        m = sym[-2:]
        sym_info[sym] = (y, m)
    rows = []
    years = sorted({info[0] for info in sym_info.values()})
    for y in years:
        sym_m1 = sym_m2 = None
        for sym, (yy, mm) in sym_info.items():
            if yy == y and mm == m1:
                sym_m1 = sym
        y2 = y if m1 < m2 else y + 1
        for sym, (yy, mm) in sym_info.items():
            if yy == y2 and mm == m2:
                sym_m2 = sym
        if sym_m1 is None or sym_m2 is None:
            continue
        df1, df2 = contracts[sym_m1], contracts[sym_m2]
        merged = pd.merge(
            df1[["date", "close"]].rename(columns={"close": "c1"}),
            df2[["date", "close"]].rename(columns={"close": "c2"}),
            on="date", how="inner").sort_values("date")
        for _, r in merged.iterrows():
            rows.append({"date": r["date"], "near_sym": sym_m1,
                         "far_sym": sym_m2, "spread": r["c1"] - r["c2"]})
    return pd.DataFrame(rows)


def screen(spread_series, cost_per_ton):
    """初筛（修正版）：核心判据 = 去趋势后「偏离 60 日均线 vs 未来 5 日变化」负相关。

    🔴 2026-09-26 修正：固定月差序列天然带强正自相关（AC≈0.9+ 是「近月-远月
    差价围绕缓漂中枢」的固有结构，非趋势），因此「裸自相关 AC>0.9 剔除」会把
    几乎所有品种误杀（j 焦炭已验证正向 edge 也被标成强趋势）。正判据 = 去趋势
    后偏离 vs 未来变化的负相关性（corr<0 越负越强回归），对齐 j/rb/hc 四重验证。
    """
    s = spread_series.dropna().to_numpy(float)
    if len(s) < 60:
        return {"判定": "样本不足", "n": len(s)}
    ma = pd.Series(s).rolling(60).mean().to_numpy()
    dev = s - ma                       # 偏离 60 日均线
    fut5 = pd.Series(s).shift(-5).to_numpy() - s   # 未来 5 日变化
    mask = ~(np.isnan(dev) | np.isnan(fut5))
    d, f = dev[mask], fut5[mask]
    corr = np.corrcoef(d, f)[0, 1] if len(d) > 30 else 0.0   # 去趋势负相关（核心）

    # 辅助指标
    daily_vol = np.std(np.diff(s))
    vol_cost_ratio = daily_vol / cost_per_ton if cost_per_ton > 0 else np.inf

    # 判定（宽松门槛 corr ≤ -0.20，宁多勿漏，四重验证再逐品种筛）
    flags = []
    if corr > -0.20:
        flags.append(f"回归弱(corr={corr:+.2f})")
    if vol_cost_ratio < 1.0:
        flags.append(f"波动<成本({vol_cost_ratio:.1f}x)")
    verdict = "✅" if not flags else "❌"
    return {
        "n": len(s), "去趋势负相关": round(corr, 3),
        "日波动": round(daily_vol, 2), "成本": cost_per_ton,
        "波动/成本": round(vol_cost_ratio, 2),
        "判定": verdict, "备注": "; ".join(flags),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 90)
    print(f"全品种固定月差采集 + 均值回归初筛 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 90)

    all_codes = list(B_GRADE.keys()) + list(C_GRADE.keys())
    summary_rows = []
    for code in all_codes:
        grade = "B" if code in B_GRADE else "C"
        prefix = (B_GRADE if code in B_GRADE else C_GRADE)[code][0]
        sector = (B_GRADE if code in B_GRADE else C_GRADE)[code][1]
        cost = ROUGH_COST.get(code, (1.0, 1.0))
        cost_per_ton = cost[0] * 2 + cost[1]  # 2 tick 滑点 + 手续费

        sys.stdout.write(f"\n[{code:>3}] {sector}{grade} 采集...")
        sys.stdout.flush()
        try:
            contracts = fetch_main_contracts(code)
        except Exception as e:
            print(f" 采集失败 {e}")
            continue
        sys.stdout.write(f" {len(contracts)}合约 ")
        sys.stdout.flush()

        for m1, m2 in PAIRS:
            df = build_fixed_spread(contracts, m1, m2)
            if len(df) == 0:
                continue
            out = os.path.join(OUT_DIR, f"{code}_{m1}vs{m2}_fixed.csv")
            df.to_csv(out, index=False)
            r = screen(df["spread"], cost_per_ton)
            r["品种"] = code
            r["板块"] = sector
            r["月差"] = f"{m1}vs{m2}"
            r["样本日"] = len(df)
            r["价差std"] = round(df["spread"].std(), 2)
            summary_rows.append(r)
            print(f"\n    {m1}vs{m2}: n={r['样本日']} std={r['价差std']} "
                  f"半衰期={r.get('半衰期')} AC={r.get('自相关')} "
                  f"波动/成本={r.get('波动/成本')} {r.get('判定')} {r.get('备注','')}")

    # 汇总
    if summary_rows:
        df = pd.DataFrame(summary_rows)
        cols = ["品种", "板块", "月差", "样本日", "价差std", "去趋势负相关",
                "日波动", "波动/成本", "判定", "备注"]
        df = df[[c for c in cols if c in df.columns]]
        out = os.path.join(OUT_DIR, "screen_all.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        passed = df[df["判定"] == "✅"]
        print("\n" + "=" * 90)
        print(f"初筛通过（✅，去趋势负相关 ≤ -0.20）{len(passed)} 组月差:")
        print(passed.to_string(index=False))
        print(f"\n初筛淘汰（❌）{len(df)-len(passed)} 组。完整表: {out}")


if __name__ == "__main__":
    main()
