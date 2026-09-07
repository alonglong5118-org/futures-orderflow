#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Path B · K 线衍生资金面 C（flow proxy）
========================================
P0-1 实测：该天勤账户**只有历史 K 线回放（get_kline_serial ✅），无历史 tick 回放**。
故真订单流(tick Delta/OFI)只能 live；但 **K 线衍生的 flow proxy** 可作回测侧 C 维度，
用「逐 bar 量×方向」的订单流不平衡（K 线版 Delta）替代零贡献的龙虎榜 C。

本模块：
  · kline_flow_proxy(df)        —— K 线 → 日频 flow proxy ∈ [-100,100]（订单流不平衡）
  · fetch_kline_tianqin(sym,..) —— 天勤回放 K 线（已验证可取，作数据源 A / 自测用）
  · load_kline_local(sym, dir)  —— 读本地 K 线文件（数据源 B：Ken 桌面 17 年数据，待落位）
  · build_kline_cflow_cache(..) —— 造 cpos_cache 兼容的 cflow_kline_cache.json，供 precompute_C_array(c_source="kline") 直接吃

数据源可插拔：天勤回放（现成、短窗口）⇄ 本地 17 年文件（待 Ken 移到可读目录后切换）。
"""
import os
import sys
import json
import time
import signal
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# 交易所映射（同 backend_tqsdk.SYMBOL_MAP）
EX_MAP = {"FG": "CZCE", "SA": "CZCE", "JM": "DCE", "J": "DCE", "jd": "DCE", "lh": "DCE"}


def _load_cfg():
    p = os.path.join(HERE, "tq_config.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {}


# ───────────────────────── K 线 → 日频 flow proxy ─────────────────────────
def kline_flow_proxy(df, window=20):
    """输入 K 线 DataFrame（列需含 open/high/low/close/volume/open_oi/close_oi，index 或 datetime 列为时间）。
    返回 {date_str(YYYYMMDD): score∈[-100,100]}。

    核心 = 订单流不平衡（volume-weighted directional imbalance）：
      每根 bar 的方向 = sign(close - open)；
      当日 = 100 × Σ(sign×volume) / Σ(volume)  → 涨量占比 − 跌量占比，∈[-100,100]。
    再用滚动 z-score 做轻度平滑（window 根bar），避免单日极端值。
    """
    import numpy as np
    import pandas as pd

    d = df.copy()
    # 解析时间
    if "datetime" in d.columns:
        d["dt"] = pd.to_datetime(d["datetime"])
    elif isinstance(d.index, pd.DatetimeIndex):
        d["dt"] = d.index
    else:
        raise ValueError("K线需含 datetime 列或 DatetimeIndex")
    d = d.sort_values("dt")
    d["date"] = d["dt"].dt.strftime("%Y%m%d")

    # 每 bar 方向量
    d["dirvol"] = np.sign((d["close"] - d["open"]).fillna(0).values) * d["volume"].fillna(0).values

    out = {}
    for day, g in d.groupby("date"):
        tot = g["volume"].sum()
        if tot <= 0:
            out[day] = 0.0
            continue
        imbalance = 100.0 * g["dirvol"].sum() / tot  # ∈[-100,100]
        out[day] = round(float(imbalance), 2)
    # 滚动 z-score 平滑（按日期序列）
    days = sorted(out.keys())
    vals = np.array([out[d] for d in days], dtype=float)
    if len(vals) > window:
        mu = pd.Series(vals).rolling(window, min_periods=window // 2).mean()
        sd = pd.Series(vals).rolling(window, min_periods=window // 2).std().replace(0, np.nan)
        z = ((pd.Series(vals) - mu) / sd).fillna(0.0)
        # 把 z 限幅回 [-100,100]
        sm = (np.tanh(z.values / 2.0) * 100)
        for i, d in enumerate(days):
            out[d] = round(float(sm[i]), 2)
    return out


# ───────────────────────── 数据源 A：天勤回放 K 线 ─────────────────────────
def fetch_kline_tianqin(symbol, start_dt, end_dt, duration=60, timeout=180):
    """天勤 TqBacktest 回放 K 线。symbol 如 FG/SA/JM... 自动拼 KQ.m@<EX>.<SYM>。
    返回 pandas DataFrame（open/high/low/close/volume/open_oi/close_oi/datetime）。"""
    import pandas as pd
    from tqsdk import TqApi, TqAuth, TqBacktest

    cfg = _load_cfg()
    U, P = cfg.get("tq_username"), cfg.get("tq_password")
    ex = EX_MAP.get(symbol, "CZCE")
    tqsym = f"KQ.m@{ex}.{symbol}"
    print(f"[kline] 天勤回放 {tqsym} {start_dt}→{end_dt} dur={duration}", flush=True)

    api = TqApi(backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt), auth=TqAuth(U, P))
    k = api.get_kline_serial(tqsym, duration)
    signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(timeout)
    try:
        while api.wait_update():
            pass
    except TimeoutError:
        print("[kline] 超时", flush=True)
    except Exception as e:
        if "BacktestFinished" in repr(e):
            pass
        else:
            print(f"[kline] 回放异常: {repr(e)[:120]}", flush=True)
    finally:
        signal.alarm(0)
        try:
            api.close()
        except Exception:
            pass

    df = pd.DataFrame(k)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
    print(f"[kline] 取到 {len(df)} 根 K", flush=True)
    return df


# ───────────────────────── 数据源 B：本地 K 线文件 ─────────────────────────
def load_kline_local(symbol, data_dir, pattern=None):
    """读本地 K 线文件。支持 CSV / parquet。
    pattern 未给时，按 `<symbol>*.(csv|parquet)` 在 data_dir 下查找（不递归子目录）。
    期望列：datetime(或 date/time/open_time) + open/high/low/close/volume + open_oi/close_oi(可选)。
    返回 pandas DataFrame。格式细节待 Ken 把数据移到可读目录后据实校准。"""
    import pandas as pd
    import glob

    if pattern is None:
        pats = [os.path.join(data_dir, f"{symbol}*.csv"),
                os.path.join(data_dir, f"{symbol}*.parquet"),
                os.path.join(data_dir, f"{symbol.upper()}*.csv"),
                os.path.join(data_dir, f"{symbol.upper()}*.parquet")]
    else:
        pats = [os.path.join(data_dir, pattern)]
    files = []
    for p in pats:
        files += glob.glob(p)
    if not files:
        raise FileNotFoundError(f"本地 K 线未找到 symbol={symbol} 于 {data_dir}（pattern={pattern}）")
    fp = files[0]
    print(f"[kline] 本地读取 {fp}", flush=True)
    if fp.endswith(".parquet"):
        df = pd.read_parquet(fp)
    else:
        df = pd.read_csv(fp)
    # 时间列归一
    for c in ("datetime", "date", "time", "open_time", "timestamp"):
        if c in df.columns:
            df["datetime"] = pd.to_datetime(df[c])
            break
    if "datetime" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={df.index.name or "index": "datetime"})
    return df


# ───────────────────────── 构建 cpos 兼容缓存 ─────────────────────────
def build_kline_cflow_cache(symbols, out_file="cflow_kline_cache.json",
                            source="tianqin", data_dir=None,
                            start=None, end=None, duration=60):
    """对 symbols 算日频 flow proxy，写出 cpos_cache 兼容的 JSON：
       {SYM: {"symbol":SYM, "history":[{"date":YYYYMMDD,"C_score":x}, ...], "C_score":<最新>}}
    precompute_C_array(c_source="kline") 直接吃这个结构。"""
    import pandas as pd

    cache = {}
    for sym in symbols:
        print(f"\n=== {sym} ({source}) ===", flush=True)
        if source == "tianqin":
            sd = start or datetime(2024, 1, 1)
            ed = end or datetime(2024, 3, 5)
            df = fetch_kline_tianqin(sym, sd, ed, duration=duration)
        else:
            if not data_dir:
                raise ValueError("本地源需 data_dir")
            df = load_kline_local(sym, data_dir)
        if df is None or len(df) == 0:
            print(f"[warn] {sym} 无数据，跳过", flush=True)
            continue
        daily = kline_flow_proxy(df)
        if not daily:
            print(f"[warn] {sym} proxy 空，跳过", flush=True)
            continue
        hist = [{"date": d, "C_score": v} for d, v in sorted(daily.items())]
        cache[sym] = {
            "symbol": sym,
            "history": hist,
            "C_score": hist[-1]["C_score"],
        }
        print(f"[ok] {sym}: {len(hist)} 日 proxy，范围 {hist[0]['date']}~{hist[-1]['date']} "
              f"样例 {hist[0]} {hist[len(hist)//2]} {hist[-1]}", flush=True)

    out = os.path.join(HERE, out_file)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 写出 {out}（{len(cache)} 品种）", flush=True)
    return out


if __name__ == "__main__":
    # 自测：用天勤回放 K 线给 FG/SA 造 cache（验证整条 pipeline 端到端）
    build_kline_cflow_cache(
        ["FG", "SA"],
        out_file="cflow_kline_cache.json",
        source="tianqin",
        start=datetime(2024, 1, 1),
        end=datetime(2024, 3, 5),
        duration=60,
    )
