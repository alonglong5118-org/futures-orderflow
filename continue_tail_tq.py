#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase B：用天勤免费网关给止于 2026-07-24 的品种续接 07-24 之后的 5m 尾部。

- 仅对合并后尾日期 < 2026-08-10 的品种（约 43 个）fetch TqSdk 近期 8000 根，
  截取 date > 现有最大日期 的部分追加，使覆盖延展到 ~08-14。
- 写回 data_5m/_XX0_min5.csv 与 BACKTEST_DIR/_XX0_min5.csv 两处（与 Phase A 一致）。
- bz 不在 SYMBOLS（无交易所映射），跳过，保留 07-24 版。
- 带 45s 硬超时防卡死；可重入（按日期去重，重跑安全）。
"""
import json
import os
import threading
import time

import pandas as pd

import four_dim_strategy as fd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_5M = os.path.join(HERE, "data_5m")
BACKTEST_DIR = fd.BACKTEST_DIR
COUNT = 8000
COLS = ["date", "open", "high", "low", "close", "volume", "oi"]

def tq_code(sym):
    ex = fd.SYMBOLS[sym]["exchange"]
    code = sym.lower() if ex == "DCE" else sym
    return f"KQ.m@{ex}.{code}"

def _fetch_df(code):
    from tqsdk import TqApi, TqAuth
    # P0-17 fix: 优先使用环境变量，回退到配置文件
    _tq_user = os.environ.get("TQ_USERNAME", "")
    _tq_pass = os.environ.get("TQ_PASSWORD", "")
    if not _tq_user or not _tq_pass:
        _cfg = json.load(open(os.path.join(HERE, "tq_config.json")))
        _tq_user = _tq_user or _cfg.get("tq_username", "")
        _tq_pass = _tq_pass or _cfg.get("tq_password", "")
    api = TqApi(auth=TqAuth(_tq_user, _tq_pass))
    k = api.get_kline_serial(code, 300, COUNT)
    for _ in range(10):
        api.wait_update(deadline=time.time() + 3)
    df = k.copy()
    # 保持 date 为 datetime（便于与现有文件 Timestamp 比较；写盘时 pandas 自动转 ISO 字符串）
    df["date"] = pd.to_datetime(df["datetime"], unit="ns")
    out = df[["date", "open", "high", "low", "close", "volume", "close_oi"]].rename(columns={"close_oi": "oi"})
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    api.close()
    return out

def fetch_with_timeout(sym):
    """返回 (ok, df_or_err)。45s 卡死返回 (False, 'timeout')。"""
    holder = {}
    def _run():
        try:
            holder["df"] = _fetch_df(tq_code(sym))
            holder["ok"] = True
        except Exception as e:
            holder["ok"] = False
            holder["err"] = f"{type(e).__name__}: {e}"
    th = threading.Thread(target=_run, daemon=True)
    th.start(); th.join(timeout=45)
    if th.is_alive():
        return False, "45s 卡死"
    if not holder.get("ok"):
        return False, holder.get("err", "未知")
    return True, holder["df"]

def load_existing(sym):
    for base in (DATA_5M, BACKTEST_DIR):
        p = os.path.join(base, f"_{sym}0_min5.csv")
        if os.path.exists(p):
            df = pd.read_csv(p)
            for s in ("日期", "时间", "datetime", "Datetime"):
                if s in df.columns and "date" not in df.columns:
                    df = df.rename(columns={s: "date"}); break
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            return df.dropna(subset=["date"]).sort_values("date")[COLS]
    return None

def main():
    # 直接扫实际文件尾日期，精准续接未完成品种（不依赖陈旧报告，重跑幂等安全）
    need = []
    for sym in fd.SYMBOLS:
        ex = load_existing(sym)
        if ex is None:
            continue
        if str(ex["date"].max()) < "2026-08-10":
            need.append(sym)
    print(f"[PhaseB] 需续接尾部 {len(need)} 个", flush=True)
    res = {}
    for sym in need:
        print(f"[PhaseB] -> {sym} ({tq_code(sym)})", flush=True)
        ok, payload = fetch_with_timeout(sym)
        if not ok:
            res[sym] = dict(ok=False, err=str(payload)); print(f"    FAIL {payload}", flush=True); continue
        fresh = payload
        ex = load_existing(sym)
        if ex is None:
            res[sym] = dict(ok=False, err="无现有合并文件"); print("    WARN 跳过", flush=True); continue
        ex_max = ex["date"].max()
        tail = fresh[fresh["date"] > ex_max]
        if len(tail) == 0:
            res[sym] = dict(ok=True, appended=0, last=str(ex["date"].iloc[-1]), note="天勤尾部未超出现有")
            print("    无新增(尾部未超出)", flush=True); continue
        merged = pd.concat([ex, tail[COLS]]).drop_duplicates(subset=["date"]).sort_values("date")[COLS]
        for p in (os.path.join(DATA_5M, f"_{sym}0_min5.csv"),
                  os.path.join(BACKTEST_DIR, f"_{sym}0_min5.csv")):
            merged.to_csv(p, index=False)
        res[sym] = dict(ok=True, appended=int(len(tail)),
                        new_last=str(merged["date"].iloc[-1]),
                        span_days=int((merged["date"].iloc[-1]-merged["date"].iloc[0]).days))
        print(f"    追加 {len(tail)} 根 -> 尾 {merged['date'].iloc[-1]}", flush=True)
    # 汇总
    ok_n = sum(1 for v in res.values() if v.get("ok"))
    full = sum(1 for v in res.values() if v.get("ok") and str(v.get("new_last","")) >= "2026-08-14")
    print(f"[PhaseB] 完成：{ok_n}/{len(need)} 成功，其中续到 08-14 的 {full} 个", flush=True)
    json.dump(res, open(os.path.join(HERE, "_tail_report.json"), "w"), ensure_ascii=False, indent=2, default=str)
    import os as _os
    _os._exit(0)

if __name__ == "__main__":
    main()
