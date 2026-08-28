# -*- coding: utf-8 -*-
"""四维策略 · 概率校准 + 置信度分层命中率（#120）
========================================================
信号生成时模型对每个方向有「自信程度」（|bias_G| 越大越自信）。
本模块回答：**越自信的信号，真的越准吗？** —— 这是模型最该被持续监控、
却一直没人追的指标。

做法：
  1) 取信号日志里每条「多/空」信号的方向(pred_dir)与自信度(|bias_G|)与
     触发时价格(ref_price)与时间(t)。
  2) 在 live_bars(5m) 里找 signal.t + WINDOW_H 小时后的真实价格，
     判方向是否吻合 → hit/miss（纯方向命中率，过滤噪声用最小跳动）。
  3) 按 |bias_G| 分桶（低/中/高/极高自信），统计各桶命中率 + 整体命中率。
  4) 概率校准：每桶给一个「名义置信度」(nominal，模型自以为的胜率)，
     与「经验命中率」(empirical) 对比 → 可靠性图；
     整体 Brier 分数 = mean((nominal - hit)^2)，越接近 0 越准。

数据来源全部是系统已有的（信号日志 + 实时5m合成K线），不新增外部依赖。
结果带 300s 缓存（实时面板轮询不反复重算 1.2MB 行情）。

用法（runner 调用）：
  import calibration as cal
  r = cal.evaluate(window_h=4)        # 返回 buckets + overall + reliability
  # /api/calibration 直接返回 r
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNAL_LOG = os.path.join(HERE, "four_dim_signals.json")
LIVEBARS = os.path.join(HERE, "live_bars.json")

WINDOW_H = 4  # 评估窗口：信号后 4 小时看方向
_CACHE = {"ts": 0, "data": None, "lock": threading.Lock()}
_CACHE_TTL = 300  # 缓存 300s

# |bias_G| 分桶（基于实测分布 q25≈8 / 中位≈14 / q75≈19 / q90≈29 划定）
# 每桶给一个「名义置信度」= 模型自以为的胜率（用于可靠性图对比）
TIERS = [
    {"key": "low", "label": "低自信", "lo": 0.0, "hi": 8.0, "nominal": 0.55},
    {"key": "mid", "label": "中自信", "lo": 8.0, "hi": 14.0, "nominal": 0.62},
    {"key": "high", "label": "高自信", "lo": 14.0, "hi": 20.0, "nominal": 0.70},
    {"key": "vhigh", "label": "极高自信", "lo": 20.0, "hi": 1e9, "nominal": 0.78},
]


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _load_signals():
    try:
        d = json.load(open(SIGNAL_LOG, encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _load_bars(symbol):
    """读 live_bars 中某品种的全部 5m 合成K线，按时间升序返回 [(dt, close)]。"""
    try:
        lb = json.load(open(LIVEBARS, encoding="utf-8"))
        rows = lb.get(symbol)
        if not rows:
            return []
        out = []
        for r in rows:
            dt = _parse(r.get("date"))
            if dt is None:
                continue
            c = r.get("close")
            if c is None:
                continue
            out.append((dt, float(c)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception:
        return []


def _future_close(bars, t_window_end):
    """返回 t_window_end 之后第一根K线的收盘价（用于判定窗口末方向）。"""
    for dt, c in bars:
        if dt >= t_window_end:
            return c
    return None


def evaluate(window_h=WINDOW_H, force=False):
    """评估信号方向命中率并按自信度分桶。返回 {updated, window_h, total, evaluated,
    pending, overall:{n,hits,miss,hit_rate,brier}, buckets:[...], reliability:[...]}。"""
    with _CACHE["lock"]:
        now = time.time()
        if not force and _CACHE["data"] and (now - _CACHE["ts"]) < _CACHE_TTL:
            return _CACHE["data"]

    sigs = _load_signals()
    bars_cache = {}

    overall_n = 0
    overall_hits = 0
    overall_brier = 0.0
    buckets = {
        t["key"]: {
            "label": t["label"],
            "lo": t["lo"],
            "hi": t["hi"],
            "nominal": t["nominal"],
            "n": 0,
            "hits": 0,
            "miss": 0,
            "hit_rate": 0.0,
            "avg_move": 0.0,
        }
        for t in TIERS
    }
    pending = 0

    for s in sigs:
        direction = s.get("direction")
        if direction not in ("多", "空"):
            continue
        pred_dir = 1 if direction == "多" else -1
        bg = abs(float((s.get("pipeline") or {}).get("bias_G") or 0.0))
        # 触发时价格：优先 entry_ref，回退 price
        ref = s.get("entry_ref")
        if ref in (None, ""):
            ref = s.get("price")
        try:
            ref = float(ref)
        except Exception:
            ref = None
        t = _parse(s.get("time"))
        if ref is None or t is None:
            continue
        sym = s.get("symbol")
        if sym not in bars_cache:
            bars_cache[sym] = _load_bars(sym)
        bars = bars_cache[sym]
        if not bars:
            pending += 1
            continue
        t_end = t + __import__("datetime").timedelta(hours=window_h)
        fprice = _future_close(bars, t_end)
        if fprice is None:
            pending += 1  # 信号太新，窗口尚未走完
            continue
        move = fprice - ref
        real_dir = 1 if move > 0 else (-1 if move < 0 else 0)
        hit = 1 if real_dir == pred_dir else 0
        # 归属桶
        tier = None
        for t in TIERS:
            if t["lo"] <= bg < t["hi"]:
                tier = t["key"]
                break
        if tier is None:
            tier = TIERS[-1]["key"]
        b = buckets[tier]
        b["n"] += 1
        b["hits"] += hit
        b["miss"] += 1 - hit
        b["avg_move"] += move
        overall_n += 1
        overall_hits += hit
        overall_brier += (buckets[tier]["nominal"] - hit) ** 2

    # 收尾统计
    for k, b in buckets.items():
        if b["n"]:
            b["hit_rate"] = round(b["hits"] / b["n"], 4)
            b["avg_move"] = round(b["avg_move"] / b["n"], 2)
        b["n"] = int(b["n"])
        b["hits"] = int(b["hits"])
        b["miss"] = int(b["miss"])
    reliability = [
        {"nominal": round(b["nominal"], 3), "empirical": b["hit_rate"], "n": b["n"], "label": b["label"]}
        for b in buckets.values()
    ]
    overall = {
        "n": overall_n,
        "hits": overall_hits,
        "miss": overall_n - overall_hits,
        "hit_rate": round(overall_hits / overall_n, 4) if overall_n else 0.0,
        "brier": round(overall_brier / overall_n, 4) if overall_n else 0.0,
    }
    result = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_h": window_h,
        "total_signals": len(sigs),
        "evaluated": overall_n,
        "pending": pending,
        "overall": overall,
        "buckets": [buckets[k] for k in ("low", "mid", "high", "vhigh")],
        "reliability": reliability,
    }
    with _CACHE["lock"]:
        _CACHE["data"] = result
        _CACHE["ts"] = time.time()
    return result


if __name__ == "__main__":
    r = evaluate(force=True)
    print(f"信号总数={r['total_signals']} 已评估={r['evaluated']} 待窗口={r['pending']}")
    print(f"整体命中率={r['overall']['hit_rate'] * 100:.1f}%  Brier={r['overall']['brier']}")
    for b in r["buckets"]:
        print(
            f"  {b['label']:>4}(|bg|{b['lo']:.0f}-{b['hi']:.0f}) "
            f"名义{b['nominal'] * 100:.0f}% → 实际{b['hit_rate'] * 100:.1f}% "
            f"n={b['n']} avg_move={b['avg_move']}"
        )
