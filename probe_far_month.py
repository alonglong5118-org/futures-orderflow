#!/usr/bin/env python3
"""
probe_far_month.py — 跨期套利远月历史数据可行性探针（2026-09-26 修正版）

修正记录：
  1. 原脚本日线用 futures_hist_em 报 KeyError('RB') —— 东财接口只认中文合约名(如"螺纹钢2701")，
     且 push2his.eastmoney.com 在本机被限流/拒绝连接(RemoteDisconnected)，弃用。
  2. 改用新浪通道：
     - 日线 futures_zh_daily_sina(symbol="RB2701") —— 已验证可取远月历史(169~233行，约1年)
     - 分钟 futures_zh_minute_sina(symbol="RB2701", period="5") —— 已验证可取远月(约21交易日)
  3. 分钟线真实列名是 datetime(不是 date)，已修正。
  4. 运行环境必须是 ~/.workbuddy/venvs/akshare39/bin/python（akshare 1.18.88 装在这里）。

用法：
  /Users/a123/.workbuddy/venvs/akshare39/bin/python probe_far_month.py [--near RB2610] [--far RB2701]

本脚本只读，不落盘，仅打印探针结果 + 近远月价差统计。
"""

import argparse
from datetime import datetime

import akshare as ak
import pandas as pd


def probe_daily_sina(symbol: str):
    """新浪日线 futures_zh_daily_sina，返回标准化 df（date, close, settle, hold）。"""
    df = ak.futures_zh_daily_sina(symbol=symbol)
    if df is None or df.empty:
        return None, "空数据"
    df = df.sort_values("date").reset_index(drop=True)
    return df, None


def probe_minute_sina(symbol: str, period="5"):
    """新浪分钟 futures_zh_minute_sina，返回标准化 df（datetime, close, hold）。"""
    df = ak.futures_zh_minute_sina(symbol=symbol, period=period)
    if df is None or df.empty:
        return None, "空数据"
    df = df.sort_values("datetime").reset_index(drop=True)
    return df, None


def main():
    ap = argparse.ArgumentParser(description="跨期套利远月历史数据探针")
    ap.add_argument("--near", default="RB2610", help="近月合约代码")
    ap.add_argument("--far", default="RB2701", help="远月合约代码")
    args = ap.parse_args()

    print("=" * 72)
    print(f"跨期套利远月历史数据探针 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"近月={args.near}  远月={args.far}  (akshare 新浪通道)")
    print("=" * 72)

    # [1] 日线
    print("\n[1] 日线历史（新浪 futures_zh_daily_sina）")
    near_d, far_d = None, None
    for label, sym in (("近月", args.near), ("远月", args.far)):
        try:
            df, err = probe_daily_sina(sym)
            if err:
                print(f"  {label} {sym}: 失败 {err}")
            else:
                print(f"  {label} {sym}: {len(df)} 行  [{df.iloc[0]['date']} ~ {df.iloc[-1]['date']}]  close末={df.iloc[-1]['close']}")
                if label == "近月":
                    near_d = df
                else:
                    far_d = df
        except Exception as e:
            print(f"  {label} {sym}: 异常 {type(e).__name__} {str(e)[:100]}")

    # [2] 分钟线
    print("\n[2] 5m 分钟线（新浪 futures_zh_minute_sina）")
    for label, sym in (("近月", args.near), ("远月", args.far)):
        try:
            df, err = probe_minute_sina(sym, "5")
            if err:
                print(f"  {label} {sym}: 失败 {err}")
            else:
                print(f"  {label} {sym}: {len(df)} 行  [{df.iloc[0]['datetime']} ~ {df.iloc[-1]['datetime']}]")
        except Exception as e:
            print(f"  {label} {sym}: 异常 {type(e).__name__} {str(e)[:100]}")

    # [3] 近远月价差（日线对齐）
    print("\n[3] 近远月价差（日线，按 date 内连接对齐）")
    if near_d is not None and far_d is not None:
        near_c = near_d.set_index("date")["close"].rename("near")
        far_c = far_d.set_index("date")["close"].rename("far")
        spread = pd.concat([near_c, far_c], axis=1).dropna()
        spread["spread"] = spread["near"] - spread["far"]
        if len(spread) > 0:
            print(f"  对齐交易日数: {len(spread)}")
            print(f"  价差(spread=near-far): mean={spread['spread'].mean():.2f}  std={spread['spread'].std():.2f}  min={spread['spread'].min():.2f}  max={spread['spread'].max():.2f}")
            print(f"  最新价差: {spread['spread'].iloc[-1]:.2f}")
            print(f"  价差前5行:\n{spread.head().to_string()}")
        else:
            print("  ⚠️ 近远月日期无重叠，价差算不出（可能合约代码对应月份不对）")
    else:
        print("  ⚠️ 近月或远月日线未取到，无法算价差")

    print("\n[4] 结论")
    print("  - 远月日线可回溯约1年(合约上市日起)，分钟线约21交易日 —— 够做近远月价差序列与半衰期初估")
    print("  - 若要更长历史(多年)，日线需换东财源(本机被限流)或自建tick/日线落盘积累")


if __name__ == "__main__":
    main()
