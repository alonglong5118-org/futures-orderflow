#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kline_cflow_from_local.py —— 用 Ken 桌面 17 年 1 分钟 K 线造 Path B 的 cflow_kline_cache.json
================================================================================================
数据源：/Users/a123/Desktop/期货数据/分钟线数据/1分钟/YYYY/EXCHANGE/期货1分钟历史行情_YYYY_EXCHANGE.zip
每个 zip 内含按合约拆分的 parquet：FG2405.ZCE.parquet（列 ts_code/trade_time/open/close/high/low/vol/amount/oi）。

方法（资金面 C flow proxy，intraday 版）：
  · 每根 1 分钟 bar：dirvol = sign(close - open) × vol（订单流方向量）
  · 每个「交易日」：imbalance = 100 × Σ(dirvol) / Σ(vol) ∈ [-100,100]（全合约成交量加权 → 真实聚合资金方向）
  · 交易日定义：夜盘尾段（00:00–07:59）归入前一自然日，避免一个交易日被拆到两天
  · 滚动 z-score（window=20 交易日）轻度平滑，限幅回 [-100,100]

输出：cflow_kline_cache.json，结构 {SYM: {symbol, history:[{date,C_score}], C_score}}，
      键用大写（FG/SA/JM/J/JD/LH）以匹配 four_dim_strategy.score_C 的 _CONTRACT_CPOS_KEY.upper() 查找。
      precompute_C_array(c_source="kline") 直接消费；回溯用 date_ints 走前向填充，对 1 日约定偏差鲁棒。
"""
import os
import re
import json
import zipfile
import tempfile
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = "/Users/a123/Desktop/期货数据/分钟线数据/1分钟"
OUT = os.path.join(HERE, "cflow_kline_cache.json")

# symbol -> (exchange_dir, filename_exchange_suffix, listing_year, file_prefix)
# 注意：DCE 鸡蛋/生猪的 parquet 合约前缀是大写 JD / LH（非 jd/lh）
SYMS = {
    "FG": ("CZCE", "ZCE", 2012, "FG"),
    "SA": ("CZCE", "ZCE", 2019, "SA"),
    "JM": ("DCE",  "DCE", 2013, "JM"),
    "J":  ("DCE",  "DCE", 2011, "J"),
    "jd": ("DCE",  "DCE", 2013, "JD"),
    "lh": ("DCE",  "DCE", 2021, "LH"),
}

END_YEAR = 2026
SMOOTH_WINDOW = 20


def symbol_members(zip_path, prefix, exsuf):
    """返回 zip 中匹配 file_prefix + 3~4 位数字 + '.' + exsuf + '.parquet' 的成员名列表。"""
    pat = re.compile(rf"^{re.escape(prefix)}[0-9]{{3,4}}\.{exsuf}\.parquet$")
    try:
        with zipfile.ZipFile(zip_path) as z:
            return [n for n in z.namelist() if pat.match(n)]
    except (zipfile.BadZipFile, OSError):
        return []


def read_symbol_year(prefix, ex, exsuf, year, tmp):
    """抽取并读取该年该品种所有合约的 1 分钟 parquet，返回仅含所需列的 DataFrame。"""
    zip_path = os.path.join(DATA_ROOT, str(year), ex, f"期货1分钟历史行情_{year}_{ex}.zip")
    if not os.path.exists(zip_path):
        return None
    members = symbol_members(zip_path, prefix, exsuf)
    if not members:
        return None
    parts = []
    with zipfile.ZipFile(zip_path) as z:
        for m in members:
            tgt = os.path.join(tmp, os.path.basename(m))
            z.extract(m, tmp)
            try:
                df = pd.read_parquet(tgt, columns=["trade_time", "open", "close", "vol"])
                if len(df):
                    parts.append(df)
            except Exception:
                pass
            finally:
                try:
                    os.remove(tgt)
                except OSError:
                    pass
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True)


def daily_flow(df):
    """1 分钟 bars → {trading_date(YYYYMMDD): imbalance∈[-100,100]}（全合约成交量加权）。"""
    dt = pd.to_datetime(df["trade_time"])
    cal = dt.dt.strftime("%Y%m%d")
    cal_dt = pd.to_datetime(cal, format="%Y%m%d")
    prev = (cal_dt - pd.Timedelta(days=1)).dt.strftime("%Y%m%d")
    tdate = np.where(dt.dt.hour < 8, prev, cal)

    dirvol = np.sign((df["close"] - df["open"]).fillna(0).values) * df["vol"].fillna(0).values
    s = pd.DataFrame({"tdate": tdate, "vol": df["vol"].fillna(0).values, "dv": dirvol})
    g = s.groupby("tdate").agg(tot=("vol", "sum"), dv=("dv", "sum"))
    g = g[g["tot"] > 0]
    g["imb"] = 100.0 * g["dv"] / g["tot"]
    return g["imb"].to_dict()


def smooth(daily_dict, window=SMOOTH_WINDOW):
    days = sorted(daily_dict.keys())
    if not days:
        return {}
    vals = np.array([float(daily_dict[d]) for d in days], dtype=float)
    out = {}
    if len(vals) > window:
        s = pd.Series(vals)
        mu = s.rolling(window, min_periods=max(2, window // 2)).mean()
        sd = s.rolling(window, min_periods=max(2, window // 2)).std().replace(0, np.nan)
        z = ((s - mu) / sd).fillna(0.0)
        sm = np.tanh(z.values / 2.0) * 100.0
        for i, d in enumerate(days):
            out[d] = round(float(sm[i]), 2)
    else:
        for d in days:
            out[d] = round(float(daily_dict[d]), 2)
    return out


def build_symbol(sym, ex, exsuf, listed, prefix):
    print(f"\n=== {sym} ({ex}, 文件前缀 {prefix}) 年份 {listed}–{END_YEAR} ===", flush=True)
    tmp = tempfile.mkdtemp(prefix=f"kline_{sym}_")
    try:
        all_parts = []
        for year in range(listed, END_YEAR + 1):
            dfy = read_symbol_year(prefix, ex, exsuf, year, tmp)
            if dfy is None or len(dfy) == 0:
                continue
            all_parts.append(dfy)
            print(f"  {year}: {len(dfy):>8,} 行", flush=True)
        if not all_parts:
            print(f"  [warn] {sym} 无任何数据，跳过", flush=True)
            return None
        df = pd.concat(all_parts, ignore_index=True)
        print(f"  合并 {len(df):,} 行，计算日频 flow proxy…", flush=True)
        raw = daily_flow(df)
        sm = smooth(raw)
        if not sm:
            print(f"  [warn] {sym} proxy 空，跳过", flush=True)
            return None
        hist = [{"date": d, "C_score": v} for d, v in sorted(sm.items())]
        print(f"  [ok] {sym}: {len(hist)} 交易日，{hist[0]['date']}~{hist[-1]['date']} "
              f"范围 [{min(v for _,v in sm.items()):.1f}, {max(v for _,v in sm.items()):.1f}]", flush=True)
        cache_key = sym.upper()
        return cache_key, {
            "symbol": cache_key,
            "history": hist,
            "C_score": hist[-1]["C_score"],
        }
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    cache = {}
    for sym, (ex, exsuf, listed, prefix) in SYMS.items():
        res = build_symbol(sym, ex, exsuf, listed, prefix)
        if res:
            key, rec = res
            cache[key] = rec
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 写出 {OUT}（{len(cache)} 品种：{', '.join(sorted(cache.keys()))}）", flush=True)


if __name__ == "__main__":
    main()
