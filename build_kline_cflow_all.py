#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kline_cflow_all.py —— 用 17 年本地 1 分钟 K 线，为 fd.SYMBOLS 全部真实品种造 Path B 的 cflow_kline_cache.json
============================================================================================================================
相比 build_kline_cflow_from_local.py（仅 6 品种手填映射），本脚本：
  · 直接从 four_dim_strategy.SYMBOLS 派生全部品种（跳过合成品种 SA01），自动推断 exchange / 文件前缀；
  · 文件前缀规则 = symbol.upper()（DCE 鸡蛋 JD / 生猪 LH 也是大写，统一成立）；
  · exsuf 映射：SHFE→SHF, CZCE→ZCE, DCE→DCE, INE→INE, GFEX→GFE；
  · 按 (exchange, year) 并行（multiprocessing）扫描各年 zip，只抽取命中前缀的合约 parquet，按交易日聚合 vol/dirvol；
  · 主进程合并全部年份后算 imbalance + 滚动 z 平滑，写出 cflow_kline_cache.json（键 = symbol.upper()）。

资金面 C flow proxy（intraday 版）：
  · 每根 1 分钟 bar：dirvol = sign(close - open) × vol
  · 每个「交易日」：imbalance = 100 × Σ(dirvol) / Σ(vol)（全合约成交量加权 → 真实聚合资金方向）
  · 交易日定义：夜盘尾段（00:00–07:59）归入前一自然日
  · 滚动 z-score（window=20）轻度平滑，限幅回 [-100,100]

用法：
  python3 build_kline_cflow_all.py            # 全品种
  python3 build_kline_cflow_all.py FG SA JM J  # 仅指定品种（调试）
"""
import os
import re
import sys
import json
import zipfile
import tempfile
import shutil
from collections import defaultdict
import multiprocessing as mp

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import four_dim_strategy as fd  # noqa: E402

DATA_ROOT = "/Users/a123/Desktop/期货数据/分钟线数据/1分钟"
OUT = os.path.join(HERE, "cflow_kline_cache.json")
SMOOTH_WINDOW = 20

EX_SUF = {"SHFE": "SHF", "CZCE": "ZCE", "DCE": "DCE", "INE": "INE", "GFEX": "GFE"}
EXCHANGES = ["CZCE", "DCE", "SHFE", "INE", "GFEX"]
YEARS = list(range(2010, 2027))

# 从 fd.SYMBOLS 派生：prefix(upper) -> symbol key（跳过合成 SA01）
TARGETS = {}
for _sym in fd.SYMBOLS:
    if _sym == "SA01":
        continue
    _p = _sym.upper()
    if _p in TARGETS and TARGETS[_p] != _sym:
        raise SystemExit(f"[fatal] prefix 冲突: {_p} -> {TARGETS[_p]} / {_sym}")
    TARGETS[_p] = _sym
SYM_EX = {_sym: fd.SYMBOLS[_sym]["exchange"] for _sym in TARGETS.values()}

# 受限品种（调试用，argv 传入）
_LIMIT = set(sys.argv[1:]) if len(sys.argv) > 1 else None
if _LIMIT:
    TARGETS = {p: s for p, s in TARGETS.items() if s in _LIMIT or p in _LIMIT}
    print(f"[limit] 仅构建: {sorted(TARGETS.values())}", flush=True)


def _work(args):
    ex, year = args
    exsuf = EX_SUF[ex]
    prefs = [p for p, s in TARGETS.items() if SYM_EX[s] == ex]
    if not prefs:
        return {}
    pat = re.compile(rf"^({'|'.join(re.escape(p) for p in prefs)})[0-9]{{3,4}}\.{exsuf}\.parquet$")
    zip_path = os.path.join(DATA_ROOT, str(year), ex, f"期货1分钟历史行情_{year}_{ex}.zip")
    acc = {p: defaultdict(lambda: [0.0, 0.0]) for p in prefs}  # prefix -> {date:[vol,dv]}
    if not os.path.exists(zip_path):
        return {}
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if pat.match(n)]
            if not names:
                return {}
            tmp = tempfile.mkdtemp(prefix=f"kl_{ex}_{year}_")
            try:
                for n in names:
                    tgt = os.path.join(tmp, os.path.basename(n))
                    z.extract(n, tmp)
                    try:
                        df = pd.read_parquet(tgt, columns=["trade_time", "open", "close", "vol"])
                    except Exception:
                        continue
                    if len(df) == 0:
                        continue
                    dt = pd.to_datetime(df["trade_time"])
                    cal = dt.dt.strftime("%Y%m%d")
                    cal_dt = pd.to_datetime(cal, format="%Y%m%d")
                    prev = (cal_dt - pd.Timedelta(days=1)).dt.strftime("%Y%m%d")
                    tdate = np.where(dt.dt.hour < 8, prev, cal)
                    m = pat.match(n)
                    pref = m.group(1)
                    vol = df["vol"].fillna(0).values.astype(float)
                    dv = np.sign((df["close"] - df["open"]).fillna(0).values).astype(float) * vol
                    s = pd.DataFrame({"tdate": tdate, "vol": vol, "dv": dv})
                    g = s.groupby("tdate").agg(tot=("vol", "sum"), dv=("dv", "sum"))
                    a = acc[pref]
                    for d, row in g.iterrows():
                        a[d][0] += float(row["tot"])
                        a[d][1] += float(row["dv"])
                    del df
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{ex}/{year}: {e}"}
    out = {}
    for p, a in acc.items():
        if a:
            out[p] = {d: [v0, v1] for d, (v0, v1) in a.items()}
    return out


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


def main():
    tasks = [(ex, y) for ex in EXCHANGES for y in YEARS]
    sym_data = {p: defaultdict(lambda: [0.0, 0.0]) for p in TARGETS}  # prefix -> {date:[vol,dv]}
    ncpus = min(12, mp.cpu_count())
    print(f"[start] {len(TARGETS)} 品种, {len(tasks)} (exchange,year) 任务, {ncpus} 进程", flush=True)
    with mp.Pool(processes=ncpus) as pool:
        done = 0
        for res in pool.imap_unordered(_work, tasks, chunksize=1):
            done += 1
            if "__error__" in res:
                print("  ERR", res["__error__"], flush=True)
                continue
            for p, dmap in res.items():
                a = sym_data[p]
                for d, (v0, v1) in dmap.items():
                    a[d][0] += v0
                    a[d][1] += v1
            if done % 10 == 0:
                print(f"  ... {done}/{len(tasks)} 年-所完成", flush=True)

    cache = {}
    skipped = []
    for p, s in TARGETS.items():
        a = sym_data.get(p)
        if not a:
            skipped.append(s)
            continue
        days = sorted(a.keys())
        raw = {d: (100.0 * a[d][1] / a[d][0]) for d in days if a[d][0] > 0}
        sm = smooth(raw)
        if not sm:
            skipped.append(s)
            continue
        hist = [{"date": d, "C_score": v} for d, v in sorted(sm.items())]
        cache[p] = {
            "symbol": p,
            "history": hist,
            "C_score": hist[-1]["C_score"],
        }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 写出 {OUT}：成功 {len(cache)} 品种，跳过 {len(skipped)} 品种：{skipped}", flush=True)
    print("  品种列表：" + ", ".join(sorted(cache.keys())), flush=True)


if __name__ == "__main__":
    main()
