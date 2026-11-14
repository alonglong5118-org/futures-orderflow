#!/usr/bin/env python3
"""
build_continuous_spread.py — 构造连续近远月价差序列（跨期套利长历史验证用）

思路：枚举某品种全部历史交割月合约（新浪已能回溯到 2019 年），
每个交易日取「近月=最近到期合约」「远月=次近到期合约」的收盘价，
算价差 spread = near_close − far_close，拼成连续序列，覆盖多年。

输出：data/futures_spread/{品种}_cont_spread.csv（date, near, far, near_sym, far_sym, spread）
      同时存各腿原始数据 data/futures_spread/raw/{品种}_{合约}.csv

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""

import os
from datetime import datetime

import akshare as ak
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")
RAW_DIR = os.path.join(OUT_DIR, "raw")

# 品种代码 + 枚举的年份范围（新浪可回溯约 2019 起）
SPECS = {
    "j":  (20, 27),   # 焦炭
}


def fetch_all_contracts(code: str, year_start: int, year_end: int):
    """拉取品种全部交割月合约日线，返回 {sym: df}。"""
    contracts = {}
    for y in range(year_start, year_end + 1):
        for m in ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]:
            sym = f"{code.upper()}{y}{m}"
            try:
                df = ak.futures_zh_daily_sina(symbol=sym)
                if df is not None and len(df) > 50:
                    df = df.sort_values("date").reset_index(drop=True)
                    contracts[sym] = df
            except Exception:
                pass
    return contracts


def build_continuous_spread(contracts: dict):
    """构造连续近远月价差：每日取最近到期(近月)与次近到期(远月)收盘。"""
    # 解析每个合约的交割月份
    sym_month = {}
    for sym in contracts:
        # 如 J2601 -> 2026-01
        y = 2000 + int(sym[-4:-2])
        m = int(sym[-2:])
        sym_month[sym] = (y, m)

    # 收集所有交易日的近月/远月
    # 近月 = 当日之后最近到期的合约（含当月仍在交易的）
    # 远月 = 次近到期
    all_dates = sorted(set().union(*[set(df["date"]) for df in contracts.values()]))
    rows = []
    for d in all_dates:
        # 当日有收盘价的合约，按到期月排序
        active = []
        for sym, df in contracts.items():
            sub = df[df["date"] == d]
            if not sub.empty:
                active.append((sym_month[sym], sym, float(sub.iloc[0]["close"])))
        if len(active) < 2:
            continue
        active.sort()  # 按到期月排序
        near_month, near_sym, near_close = active[0]
        far_month, far_sym, far_close = active[1]
        rows.append({
            "date": d, "near_sym": near_sym, "far_sym": far_sym,
            "near": near_close, "far": far_close,
            "spread": near_close - far_close,
        })
    return pd.DataFrame(rows)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    for code, (ys, ye) in SPECS.items():
        print(f"\n[{code}] 拉取 {20 if ys<20 else ys}{ye if ye<27 else ''} 年份全部合约...")
        contracts = fetch_all_contracts(code, ys, ye)
        print(f"  共 {len(contracts)} 个合约月份")

        # 存原始
        for sym, df in contracts.items():
            df.to_csv(os.path.join(RAW_DIR, f"{code}_{sym}.csv"), index=False)

        cont = build_continuous_spread(contracts)
        print(f"  连续价差序列: {len(cont)} 个交易日")
        if len(cont):
            print(f"  区间: [{cont['date'].iloc[0]} ~ {cont['date'].iloc[-1]}]")
            print(f"  价差: mean={cont['spread'].mean():.2f} std={cont['spread'].std():.2f}")
            out = os.path.join(OUT_DIR, f"{code}_cont_spread.csv")
            cont.to_csv(out, index=False)
            print(f"  已落盘: {out}")


if __name__ == "__main__":
    main()
