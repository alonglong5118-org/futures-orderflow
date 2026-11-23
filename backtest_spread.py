#!/usr/bin/env python3
"""
backtest_spread.py — 跨期套利（近远月价差）均值回归回测 v2（2026-09-26）

v2 修正：
  - 用「长窗口均值」作锚（expanding 均值，随样本累积，不追价差），而非短期滚动均值
  - 出场：价差回到均值附近（|z| < exit_z）即平，不再拖到 MAX_HOLD
  - 信号 bar t 产生，bar t+1 成交（滞后一根）
  - 方向：z>0 价差偏高→空价差(空近多远，spread 下降赚)；z<0→多价差

成本口径：每边滑点 SLIP_TICKS tick + 手续费，双边合计（价差点数）
"""
import os
import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")
SPECS = ["j", "hc", "sa"]

# 成本（价差点数，元/吨）
TICK = {"j": 0.5, "hc": 1.0, "sa": 1.0}
SLIP_TICKS = 1
FEE_SIDE = {"j": 1.0, "hc": 1.0, "sa": 1.5}


def backtest(s, code, lookback=40, entry_z=1.5, exit_z=0.3, max_hold=20):
    """expanding 均值锚定 + 均值回归。返回统计 dict。"""
    s = s.dropna().to_numpy()
    n = len(s)
    cost = SLIP_TICKS * TICK[code] * 2 + FEE_SIDE[code] * 2

    pos = 0
    entry_price = entry_day = 0
    trades = []
    equity = 0.0
    eq_curve = []

    i = lookback
    while i < n:
        window = s[:i]
        mu = window.mean()
        sd = window.std()
        if sd == 0 or np.isnan(sd):
            i += 1
            continue
        z = (s[i] - mu) / sd

        if pos == 0:
            if z >= entry_z:
                if i + 1 < n:
                    pos = 1; entry_price = s[i + 1]; entry_day = i + 1; i += 2; continue
            elif z <= -entry_z:
                if i + 1 < n:
                    pos = -1; entry_price = s[i + 1]; entry_day = i + 1; i += 2; continue
        else:
            exit_now = (pos == 1 and z <= exit_z) or (pos == -1 and z >= -exit_z)
            if i - entry_day >= max_hold:
                exit_now = True
            if exit_now:
                exit_price = s[i]
                pnl = (exit_price - entry_price) * pos
                net = pnl - cost
                trades.append({"entry_day": entry_day, "exit_day": i, "entry": round(entry_price, 2),
                               "exit": round(exit_price, 2), "pos": pos, "gross": round(pnl, 2),
                               "net": round(net, 2), "hold": i - entry_day})
                equity += net
                eq_curve.append(equity)
                pos = 0
        i += 1

    if not trades:
        return None
    tdf = pd.DataFrame(trades)
    total_gross = tdf["gross"].sum()
    total_net = tdf["net"].sum()
    wins = (tdf["net"] > 0).sum()
    wr = wins / len(tdf)
    gw = tdf.loc[tdf["gross"] > 0, "gross"].sum()
    gl = -tdf.loc[tdf["gross"] < 0, "gross"].sum()
    pf = gw / gl if gl > 0 else float("inf")
    eq = np.array(eq_curve)
    dd = (np.maximum.accumulate(eq) - eq).max() if len(eq) > 1 else 0
    return {"code": code, "n": len(tdf), "gross": round(total_gross, 2), "net": round(total_net, 2),
            "wr": round(wr, 4), "pf": round(pf, 3) if pf != float("inf") else "inf",
            "dd": round(dd, 2), "avg_hold": round(tdf["hold"].mean(), 1)}


def main():
    print("跨期套利均值回归回测 v2（expanding 均值锚定）")
    print("参数扫描：entry_z × exit_z × lookback\n")

    # 参数网格
    grid = []
    for code in SPECS:
        df = pd.read_csv(os.path.join(OUT_DIR, f"{code}_spread.csv"))
        best = None
        for lb in [30, 40, 60]:
            for ez in [1.0, 1.5, 2.0]:
                for xz in [0.2, 0.3, 0.5]:
                    r = backtest(df["spread"], code, lookback=lb, entry_z=ez, exit_z=xz)
                    if r and r["n"] >= 3:
                        row = {**r, "lookback": lb, "entry_z": ez, "exit_z": xz}
                        grid.append(row)
                        if best is None or r["net"] > best["net"]:
                            best = row
        if best:
            print(f"[{code}] 最优: 交易{best['n']}笔 毛PnL={best['gross']:+.2f} 净PnL={best['net']:+.2f} "
                  f"胜率={best['wr']:.1%} PF={best['pf']} 回撤={best['dd']:.2f} "
                  f"(lookback={best['lookback']} entry_z={best['entry_z']} exit_z={best['exit_z']})")

    if grid:
        gdf = pd.DataFrame(grid)
        gdf = gdf.sort_values("net", ascending=False)
        print("\n=== 净PnL TOP 15（全参数组合）===")
        cols = ["code", "n", "gross", "net", "wr", "pf", "dd", "avg_hold", "lookback", "entry_z", "exit_z"]
        print(gdf[cols].head(15).to_string(index=False))

        # 稳健性：看每个品种所有参数组合的净PnL分布
        print("\n=== 每品种净PnL分布（稳健性检验）===")
        for code in SPECS:
            sub = gdf[gdf["code"] == code]["net"]
            if len(sub):
                print(f"[{code}] 组合数={len(sub)} 净PnL: 中位={sub.median():+.2f} 均值={sub.mean():+.2f} "
                      f"正值占比={(sub>0).mean():.0%} min={sub.min():+.2f} max={sub.max():+.2f}")


if __name__ == "__main__":
    main()
