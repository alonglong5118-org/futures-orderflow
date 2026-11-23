#!/usr/bin/env python3
"""
reproduce_spread_edge.py — hc/rb 跨期套利 edge 复现验证（对齐 j 焦炭四重验证口径）

目的：验证 j 焦炭固定月差套利的 edge 是否可迁移到同属黑色系、同为 1/5/9 主力月的
hc（热卷）、rb（螺纹）。用与 j 完全一致的策略口径：

  - 固定月份差：01vs05 / 05vs09 / 09vs01（避免主力切换跳变）
  - 信号：价差偏离滚动 60 日均线 z-score，|z|>=k 反向开仓，回归到均线平仓
  - 成交：滞后一根（信号 bar t → t+1 收盘成交），无前视
  - 成本：3 元/吨（与 j 对齐，先保守；hc/rb 实际成本待后算）
  - 关键：分段回测（剔除换月断层，near_sym 切换处强制平仓）

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""

import os

import akshare as ak
import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 主力月固定月差配对
PAIRS = [("01", "05"), ("05", "09"), ("09", "01")]


def fetch_main_contracts(code: str):
    """拉取品种 1/5/9 主力月全部历史合约日线，返回 {sym: df}。"""
    contracts = {}
    for y in range(18, 27):  # 2018~2026（新浪对 hc/rb 可回溯 2018）
        for m in ["01", "05", "09"]:
            sym = f"{code.upper()}{y}{m}"
            try:
                df = ak.futures_zh_daily_sina(symbol=sym)
                if df is not None and len(df) > 50:
                    df = df.sort_values("date").reset_index(drop=True)
                    contracts[sym] = df
            except Exception:
                pass
    return contracts


def build_fixed_spread(contracts: dict, m1: str, m2: str):
    """
    构造固定月差序列：每月取 m1 月合约与 m2 月合约（同年优先，跨年按 09vs01 规则）。
    返回 DataFrame(date, near_sym, far_sym, spread)。
    """
    # 按 (年份, 月份) 建立索引
    sym_info = {}
    for sym in contracts:
        y = 2000 + int(sym[-4:-2])
        m = sym[-2:]
        sym_info[sym] = (y, m)

    rows = []
    # 对每个"年度对"，找到 m1 与 m2 的合约
    # 规则：m1 < m2 时同年配对；09vs01 时 m1=09 用当年，m2=01 用次年
    years = sorted({info[0] for info in sym_info.values()})
    for y in years:
        # 找该年度 m1 月合约
        sym_m1 = None
        for sym, (yy, mm) in sym_info.items():
            if yy == y and mm == m1:
                sym_m1 = sym
        # 找 m2 月合约：m1<m2 同年，m1>m2（09vs01）次年
        y2 = y if m1 < m2 else y + 1
        sym_m2 = None
        for sym, (yy, mm) in sym_info.items():
            if yy == y2 and mm == m2:
                sym_m2 = sym
        if sym_m1 is None or sym_m2 is None:
            continue
        df1 = contracts[sym_m1]
        df2 = contracts[sym_m2]
        # 对齐日期
        merged = pd.merge(
            df1[["date", "close"]].rename(columns={"close": "c1"}),
            df2[["date", "close"]].rename(columns={"close": "c2"}),
            on="date", how="inner",
        )
        merged = merged.sort_values("date")
        for _, r in merged.iterrows():
            rows.append({
                "date": r["date"],
                "near_sym": sym_m1,
                "far_sym": sym_m2,
                "spread": r["c1"] - r["c2"],
            })
    return pd.DataFrame(rows)


def backtest(s, cost, k, warmup=60):
    """滚动 60 日均线 z-score 均值回归，滞后一根成交。"""
    n = len(s)
    ma = pd.Series(s).rolling(60).mean().to_numpy()
    sd = pd.Series(s).rolling(60).std().to_numpy()
    pos = 0
    entry = 0.0
    trades = []
    i = warmup
    while i < n - 1:
        if np.isnan(ma[i]) or np.isnan(sd[i]) or sd[i] == 0:
            i += 1
            continue
        z = (s[i] - ma[i]) / sd[i]
        if pos == 0:
            if z >= k:
                pos = -1
                entry = s[i + 1]
                i += 1
            elif z <= -k:
                pos = 1
                entry = s[i + 1]
                i += 1
        else:
            if (pos == -1 and s[i] <= ma[i]) or (pos == 1 and s[i] >= ma[i]):
                trades.append((s[i] - entry) * pos - cost)
                pos = 0
        i += 1
    return trades


def segment_backtest(df: pd.DataFrame, cost, k):
    """分段回测：near_sym 切换处（换月）强制平仓重置，剔除换月断层。"""
    all_trades = []
    seg_start = 0
    n = len(df)
    for i in range(1, n + 1):
        if i == n or df.iloc[i]["near_sym"] != df.iloc[seg_start]["near_sym"]:
            seg = df.iloc[seg_start:i]["spread"].to_numpy()
            if len(seg) > 60:
                all_trades.extend(backtest(seg, cost, k))
            seg_start = i
    return all_trades


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for code in ["hc", "rb"]:
        print(f"\n{'='*60}")
        print(f"[{code}] 拉取 1/5/9 主力月历史合约...")
        contracts = fetch_main_contracts(code)
        print(f"  共 {len(contracts)} 个合约")
        for m1, m2 in PAIRS:
            df = build_fixed_spread(contracts, m1, m2)
            if len(df) == 0:
                print(f"  [{m1}vs{m2}] 无数据")
                continue
            # 落盘固定月差序列
            out = os.path.join(OUT_DIR, f"{code}_{m1}vs{m2}_fixed.csv")
            df.to_csv(out, index=False)
            s = df["spread"].to_numpy()
            n_switch = (df["near_sym"] != df["near_sym"].shift(1)).sum()
            print(f"\n  [{m1}vs{m2}] {len(df)}日 价差 mean={s.mean():+.2f} std={s.std():.2f} 换月次数={n_switch}")
            # 固定 k 稳健性 + 分段剔除换月
            for k in [1.0, 1.5, 2.0]:
                trades = segment_backtest(df, 3.0, k)
                if trades:
                    t = np.array(trades)
                    print(f"    k={k}: {len(t)}笔 净PnL={t.sum():+8.1f} 胜率={(t>0).mean():.0%} 均值={t.mean():+.1f}")
                else:
                    print(f"    k={k}: 0笔")


if __name__ == "__main__":
    main()
