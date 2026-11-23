#!/usr/bin/env python3
"""
estimate_half_life.py — 跨期套利近远月价差半衰期估算（2026-09-26）

对 data/futures_spread/{品种}_spread.csv 的价差序列做 OU 过程回归，估半衰期，
判定哪些品种价差有均值回归特性（半衰期短 = 适合套利，长 = 无套利价值）。

方法：
  S_{t+1} - S_t = a + b * S_t + eps
  b = -ln(2)/half_life → half_life = -ln(2)/b
  b 显著为负 且 half_life 合理（如 < 30 交易日）→ 价差均值回归，可套利

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python（或任意带 pandas+numpy 的环境）
"""

import os

import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")
SPECS = ["rb", "hc", "cu", "sc", "j", "sa"]


def estimate_half_life(spread: pd.Series):
    """OU 回归估半衰期，返回 dict。"""
    s = spread.dropna().to_numpy()
    if len(s) < 30:
        return {"n": len(s), "half_life": None, "b": None, "note": "样本<30"}
    y = np.diff(s)                      # S_{t+1} - S_t
    x = s[:-1]                          # S_t
    # OLS: y = a + b*x
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    a, b = beta[0], beta[1]
    if b >= 0:
        return {"n": len(s), "half_life": None, "b": round(b, 5), "note": "b>=0 无回归"}
    half_life = -np.log(2) / b
    # R^2
    y_hat = a + b * x
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return {"n": len(s), "half_life": round(half_life, 2), "b": round(b, 5), "r2": round(r2, 4)}


def main():
    print("=" * 72)
    print("跨期套利近远月价差半衰期估算")
    print("=" * 72)
    rows = []
    for code in SPECS:
        path = os.path.join(OUT_DIR, f"{code}_spread.csv")
        if not os.path.exists(path):
            print(f"[{code}] 无数据文件")
            continue
        df = pd.read_csv(path)
        if "spread" not in df.columns:
            continue
        r = estimate_half_life(df["spread"])
        r["品种"] = code
        rows.append(r)
        note = f"  {r.get('note','')}" if r.get("note") else f"  R²={r.get('r2')}"
        print(f"[{code}] 样本{r['n']} 半衰期={r['half_life']} 交易日  b={r['b']}{note}")

    if rows:
        out = pd.DataFrame(rows)
        out = out[["品种", "n", "half_life", "b", "r2"] if "r2" in out.columns else ["品种", "n", "half_life", "b", "note"]]
        print("\n" + "=" * 72)
        print(out.to_string(index=False))

    print("\n判读标准：半衰期 < 10 交易日=强均值回归(可套利)；10~30=中等；>30 或 b>=0=无套利价值(随机游走)")


if __name__ == "__main__":
    main()
