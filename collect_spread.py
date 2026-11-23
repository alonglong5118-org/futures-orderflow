#!/usr/bin/env python3
"""
collect_spread.py — 跨期套利远月历史数据批量采集（2026-09-26）

采集候选品种（趋势腿白名单 6 品种）的近月+远月历史日线，落盘 parquet + 算近远月价差。

代码格式（新浪 futures_zh_daily_sina，已验证）：
  rb RB2610/RB2701   hc HC2610/HC2701   cu CU2610/CU2701
  sc SC2610/SC2701   j  J2610/J2701     sa SA2610/SA2701（纯碱用4位年份，非郑商所3位610）

输出：
  data/futures_spread/{品种}_near.csv  {品种}_far.csv  {品种}_spread.csv
  data/futures_spread/summary.csv     各品种价差统计汇总

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""

import os
from datetime import datetime

import akshare as ak
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 品种 -> (代码前缀, 近月, 远月)
CANDIDATES = {
    "rb": ("RB", "RB2610", "RB2701"),
    "hc": ("HC", "HC2610", "HC2701"),
    "cu": ("CU", "CU2610", "CU2701"),
    "sc": ("SC", "SC2610", "SC2701"),
    "j":  ("J",  "J2610",  "J2701"),
    "sa": ("SA", "SA2610", "SA2701"),
}


def fetch_daily(symbol: str):
    """拉新浪日线，返回标准化 df（date, open, high, low, close, volume, hold, settle）。"""
    df = ak.futures_zh_daily_sina(symbol=symbol)
    if df is None or df.empty:
        raise ValueError(f"{symbol} 空数据")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    summary_rows = []

    print("=" * 72)
    print(f"跨期套利远月历史批量采集 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"落盘目录: {OUT_DIR}")
    print("=" * 72)

    for code, (prefix, near_sym, far_sym) in CANDIDATES.items():
        print(f"\n[{code}] 近月={near_sym} 远月={far_sym}")
        try:
            near = fetch_daily(near_sym)
            far = fetch_daily(far_sym)

            # 对齐价差
            n = near.set_index("date")["close"].rename("near")
            f = far.set_index("date")["close"].rename("far")
            spread = pd.concat([n, f], axis=1).dropna()
            spread["spread"] = spread["near"] - spread["far"]

            # 落盘（csv，避免依赖 pyarrow/fastparquet，此 venv 仅装 akshare）
            near.to_csv(os.path.join(OUT_DIR, f"{code}_near.csv"))
            far.to_csv(os.path.join(OUT_DIR, f"{code}_far.csv"))
            spread.to_csv(os.path.join(OUT_DIR, f"{code}_spread.csv"))

            row = {
                "品种": code,
                "近月": near_sym,
                "远月": far_sym,
                "近月行数": len(near),
                "远月行数": len(far),
                "对齐交易日": len(spread),
                "价差均值": round(spread["spread"].mean(), 2),
                "价差std": round(spread["spread"].std(), 2),
                "价差min": round(spread["spread"].min(), 2),
                "价差max": round(spread["spread"].max(), 2),
                "最新价差": round(spread["spread"].iloc[-1], 2),
                "近月区间": f"{near['date'].iloc[0]}~{near['date'].iloc[-1]}",
                "远月区间": f"{far['date'].iloc[0]}~{far['date'].iloc[-1]}",
            }
            summary_rows.append(row)
            print(f"  ✅ 近月{len(near)}行 远月{len(far)}行 对齐{len(spread)}日 "
                  f"价差mean={spread['spread'].mean():.2f} std={spread['spread'].std():.2f}")
        except Exception as e:
            print(f"  ❌ 失败: {type(e).__name__} {str(e)[:100]}")
            summary_rows.append({"品种": code, "近月": near_sym, "远月": far_sym, "错误": str(e)[:100]})

    # 汇总落盘
    if summary_rows:
        summary = pd.DataFrame(summary_rows)
        summary_path = os.path.join(OUT_DIR, "summary.csv")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        print("\n" + "=" * 72)
        print("采集汇总:")
        print(summary.to_string(index=False))
        print(f"\n汇总已落盘: {summary_path}")

    print("\n采集完成。下一步：对每个品种价差序列估半衰期（OU/DF），判定哪些品种价差有均值回归特性可做套利。")


if __name__ == "__main__":
    main()
