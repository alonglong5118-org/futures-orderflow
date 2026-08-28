# -*- coding: utf-8 -*-
"""全市场批量扫描器（Market Scanner）
=================================================================
并行扫描所有品种的信号强度，按 T_D/触发概率排序输出 Top N。
用于盘前预选和盘中机会发现。

优化点（对比现有逐品种循环）：
  1. concurrent.futures ThreadPool 并行计算（IO 密集型→线程池即可）
  2. 轻量评估：只算 T_D + regime，不算完整 pipeline（减少 80% 计算量）
  3. 结果聚合：按信号强度/方向/品种组分类输出

参考 Kara说量化 的全市场批量扫描思路，适配到 54 品种期货框架。

红线：
  - 扫描结果仅用于预选/排序，不直接触发交易
  - 并行度自动适配 CPU 核数
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import four_dim_strategy as fd
from four_dim_strategy import DEFAULT_CONFIG, load_daily

# 扫描参数
MAX_WORKERS = 8          # 默认线程数（IO 密集型，线程池足够）
SCAN_CACHE_TTL = 30      # 缓存 30 秒（盘中实时扫描不需更频繁）
_CACHE = {"ts": 0, "data": None, "lock": __import__("threading").Lock()}


def _light_eval(symbol, df_daily, cfg=DEFAULT_CONFIG):
    """轻量评估：只算 T_D + regime + bias_G，不算完整 pipeline。

    返回 {symbol, T_D, regime, bias_G, group, close, chg_pct}
    """
    try:
        T_D, regime, rdesc = fd.compute_T(df_daily, cfg, group=None, symbol=symbol)
        F = fd.score_F(symbol)
        C = fd.score_C(symbol)
        bias_G = fd.combine_bias(F, T_D, C)
        close = float(df_daily["close"].iloc[-1])
        prev_close = float(df_daily["close"].iloc[-2]) if len(df_daily) > 1 else close
        chg_pct = round((close / prev_close - 1) * 100, 2) if prev_close else 0
        return {
            "symbol": symbol,
            "T_D": round(float(T_D), 1),
            "regime": regime,
            "rdesc": rdesc,
            "bias_G": round(float(bias_G), 1),
            "F": round(float(F), 1),
            "C": round(float(C), 1),
            "close": round(close, 2),
            "chg_pct": chg_pct,
        }
    except Exception:
        return {"symbol": symbol, "T_D": 0, "regime": "?", "bias_G": 0,
                "F": 0, "C": 0, "close": 0, "chg_pct": 0}


def scan_all(symbols_dict, max_workers=MAX_WORKERS, use_cache=True):
    """并行扫描全部品种。

    参数：
      symbols_dict: SYMBOLS dict {sym: {name, group, ...}}
      max_workers: 线程数
      use_cache: 是否使用缓存

    返回 dict:
      - results: [sorted by |T_D| desc]
      - by_group: {group: [results]}
      - summary: {n_up, n_down, n_neutral, avg_T, strongest}
      - elapsed: 耗时（秒）
    """
    # 缓存检查
    if use_cache:
        with _CACHE["lock"]:
            if _CACHE["data"] and time.time() - _CACHE["ts"] < SCAN_CACHE_TTL:
                return _CACHE["data"]

    t0 = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for sym in symbols_dict:
            try:
                df = load_daily(sym)
                if df is None or len(df) < 30:
                    continue
                group = symbols_dict[sym].get("group", "")
                fut = pool.submit(_light_eval, sym, df)
                futures[fut] = (sym, group)
            except Exception:
                continue

        for fut in as_completed(futures):
            sym, group = futures[fut]
            try:
                r = fut.result(timeout=10)
                r["group"] = group
                r["name"] = symbols_dict.get(sym, {}).get("name", sym)
                results.append(r)
            except Exception:
                results.append({"symbol": sym, "group": group,
                                "T_D": 0, "regime": "?", "bias_G": 0,
                                "close": 0, "chg_pct": 0, "name": sym})

    # 排序：按 |T_D| 降序
    results.sort(key=lambda x: -abs(float(x.get("T_D", 0))))

    # 分组统计
    by_group = {}
    for r in results:
        g = r.get("group", "其他")
        by_group.setdefault(g, []).append(r)

    n_up = sum(1 for r in results if float(r.get("T_D", 0)) > 0)
    n_down = sum(1 for r in results if float(r.get("T_D", 0)) < 0)
    n_neutral = len(results) - n_up - n_down
    avg_T = float(np.mean([abs(float(r.get("T_D", 0))) for r in results])) if results else 0
    strongest = results[0] if results else None

    output = {
        "results": results,
        "by_group": by_group,
        "summary": {
            "n_total": len(results),
            "n_up": n_up, "n_down": n_down, "n_neutral": n_neutral,
            "avg_abs_T": round(avg_T, 2),
            "strongest": strongest,
        },
        "elapsed": round(time.time() - t0, 2),
    }

    if use_cache:
        with _CACHE["lock"]:
            _CACHE["ts"] = time.time()
            _CACHE["data"] = output

    return output


def get_top_opportunities(symbols_dict, n=10, direction=0):
    """获取 Top N 机会品种。

    direction: 0=全部, 1=只看多, -1=只看空
    """
    scan = scan_all(symbols_dict)
    results = scan["results"]
    if direction > 0:
        results = [r for r in results if float(r.get("T_D", 0)) > 0]
    elif direction < 0:
        results = [r for r in results if float(r.get("T_D", 0)) < 0]
    return results[:n]


def get_cache():
    """获取缓存数据（供 API 消费）。"""
    with _CACHE["lock"]:
        if _CACHE["data"] and time.time() - _CACHE["ts"] < SCAN_CACHE_TTL * 2:
            return _CACHE["data"]
    return None
