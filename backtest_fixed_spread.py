#!/usr/bin/env python3
"""
backtest_fixed_spread.py — j 焦炭主力月「固定月份差」跨期套利验证（2026-09-26）

核心：避开主力切换跳变，用「固定月份差」的两合约构造价差。
焦炭 j 主力月 = 1/5/9。取「5月 vs 9月」月差（间隔4个月，最活跃）。

对每个日历年度，用同一年度的近月合约(5月) vs 远月合约(9月)：
  - 合约存续期内两者同时有行情 → 算价差 spread = 近月close − 远月close
  - 拼接各年度 → 得到连续、无切换跳变的价差序列

然后跑无前视均值回归回测（expanding 均值锚）。

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""

import os

import numpy as np
import pandas as pd

RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread", "raw")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 焦炭 j 主力月对（近月, 远月），月差固定
MONTH_PAIRS = [("05", "09"), ("09", "01"), ("01", "05")]  # 09vs01 的01是次年

# 成本（价差点，元/吨）：j tick=0.5，往返滑点1tick*2=1元 + 手续费双边约2元
COST = 3.0


def load_contract(code, sym):
    p = os.path.join(RAW, f"{code}_{sym}.csv")
    if os.path.exists(p):
        return pd.read_csv(p)
    return None


def build_fixed_spread(code, near_m, far_m):
    """构造固定月差价差序列（近月=near_m月，远月=far_m月，可能跨年）。"""
    rows = []
    # 近月合约从 2019 到 2026
    for y in range(19, 27):
        near_sym = f"{code.upper()}{y}{near_m}"
        near_df = load_contract(code, near_sym)
        if near_df is None:
            continue
        # 远月：若 far_m < near_m 则次年，否则同年
        far_y = y if far_m > near_m else y + 1
        if far_y > 26:
            continue
        far_sym = f"{code.upper()}{far_y}{far_m}"
        far_df = load_contract(code, far_sym)
        if far_df is None:
            continue
        # 内连接
        n = near_df.set_index("date")["close"].rename("near")
        f = far_df.set_index("date")["close"].rename("far")
        merged = pd.concat([n, f], axis=1).dropna()
        for d, r in merged.iterrows():
            rows.append({"date": d, "near_sym": near_sym, "far_sym": far_sym,
                         "spread": r["near"] - r["far"]})
    return pd.DataFrame(rows)


def backtest(s, cost=COST, warmup=40, entry_z=1.5, exit_z=0.3):
    """无前视均值回归回测（expanding 均值锚）。"""
    s = s.dropna().to_numpy()
    n = len(s)
    pos = 0
    entry = 0.0
    trades = []
    i = warmup
    while i < n:
        hist = s[:i]
        mu = hist.mean()
        sd = hist.std()
        if sd == 0 or np.isnan(sd):
            i += 1
            continue
        z = (s[i] - mu) / sd
        if pos == 0:
            if z >= entry_z:
                pos = -1; entry = s[i]  # 价差偏高→空价差
            elif z <= -entry_z:
                pos = 1; entry = s[i]   # 价差偏低→多价差
        else:
            if (pos == -1 and s[i] <= mu) or (pos == 1 and s[i] >= mu):
                pnl = (s[i] - entry) * pos - cost
                trades.append(pnl)
                pos = 0
        i += 1
    if not trades:
        return None
    t = np.array(trades)
    return {"n": len(t), "total": t.sum(), "win_rate": (t > 0).mean(),
            "avg": t.mean(), "min": t.min(), "max": t.max()}


def main():
    code = "j"
    print("=" * 72)
    print("j 焦炭 主力月固定月差 跨期套利验证（无前视 expanding 均值锚）")
    print("=" * 72)

    for near_m, far_m in MONTH_PAIRS:
        df = build_fixed_spread(code, near_m, far_m)
        if df.empty:
            print(f"\n[月差 {near_m}vs{far_m}] 无数据")
            continue
        s = df["spread"].to_numpy()
        print(f"\n[月差 {near_m} vs {far_m}] {len(df)}日 区间[{df['date'].iloc[0]}~{df['date'].iloc[-1]}]")
        print(f"  价差: mean={s.mean():+.2f} std={s.std():.2f} min={s.min():.1f} max={s.max():.1f}")

        # 参数扫描
        best = None
        for ez in [1.0, 1.5, 2.0]:
            for xz in [0.2, 0.3, 0.5]:
                r = backtest(df["spread"], cost=COST, entry_z=ez, exit_z=xz)
                if r and r["n"] >= 3:
                    tag = f"entry_z={ez} exit_z={xz} -> {r['n']}笔 净={r['total']:+.1f} 胜率={r['win_rate']:.0%}"
                    print(f"    {tag}")
                    if best is None or r["total"] > best["total"]:
                        best = {**r, "ez": ez, "xz": xz}
        if best:
            print(f"  ★ 最优: entry_z={best['ez']} exit_z={best['xz']} 净PnL={best['total']:+.1f} "
                  f"({best['n']}笔, 胜率{best['win_rate']:.0%})")

        # 保存该月差价差
        out = os.path.join(OUT, f"j_{near_m}vs{far_m}_fixed.csv")
        df.to_csv(out, index=False)
        print(f"  已落盘: {out}")


if __name__ == "__main__":
    main()
