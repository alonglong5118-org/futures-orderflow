# -*- coding: utf-8 -*-
"""四维策略 · 异动扫描层（从 da龘 的 minishare 全市场扫描适配）
==============================================================
da龘 用 minishare rt_fut_k 全市场快照做「异动扫描」（按涨跌幅排名）。
四维已有 minishare 实时快照（feed.last_snap 含 open/high/low/close/vol），
本模块直接复用四维自己的快照，**无需再依赖 da龘 的 minishare_feed**，
对全部 53 品种算「日内异动评分」，与信号卡片互补（广度选品使命天然契合）。

评分 = 0.7×|日内涨跌幅| + 0.3×振幅
  - 日内涨跌幅 = (close - open) / open × 100   （无昨收时用开盘价作基准）
  - 振幅       = (high - low) / open × 100
可选传入 pre_close 则涨跌幅改用 (close - pre_close)/pre_close。

用法（runner 调用）：
  import anomaly_scan as asc
  snaps = {sym: feed.last_snap[sym] for sym in SYMBOLS if feed.last_snap.get(sym)}
  result = asc.compute(snaps)
  # result = {"ok", "updated", "total", "by_symbol": {...}, "top_up":[...], "top_down":[...]}
"""

from __future__ import annotations

import time

W_PCT = 0.7  # 涨跌幅权重
W_AMP = 0.3  # 振幅权重
TOP_N = 12  # 涨跌榜各取前 N


def compute(snaps, pre_close_map=None, top_n=TOP_N):
    """snaps: {sym: {"close","open","high","low",...}}；pre_close_map 可选。
    返回异动扫描结果 dict。"""
    pre_close_map = pre_close_map or {}
    by_symbol = {}
    rows = []
    for sym, s in snaps.items():
        try:
            close = float(s.get("close"))
            o = float(s.get("open"))
            h = float(s.get("high"))
            l = float(s.get("low"))
        except (TypeError, ValueError):
            continue
        if not (close and o and h and l):
            continue
        # 涨跌幅：优先用昨收，否则用开盘
        pre = pre_close_map.get(sym)
        if pre:
            try:
                pct = (close - float(pre)) / float(pre) * 100
            except (TypeError, ValueError):
                pct = (close - o) / o * 100
        else:
            pct = (close - o) / o * 100 if o else 0.0
        amp = (h - l) / o * 100 if o else 0.0
        score = round(W_PCT * abs(pct) + W_AMP * amp, 2)
        rec = {
            "symbol": sym,
            "name": s.get("name", sym),
            "close": round(close, 2),
            "pct": round(pct, 2),
            "amp": round(amp, 2),
            "score": score,
        }
        by_symbol[sym] = rec
        rows.append(rec)

    if not rows:
        return {"ok": False, "updated": time.time(), "total": 0, "by_symbol": {}, "top_up": [], "top_down": []}

    top_up = sorted(rows, key=lambda x: -x["pct"])[:top_n]
    top_down = sorted(rows, key=lambda x: x["pct"])[:top_n]
    return {
        "ok": True,
        "updated": time.strftime("%H:%M:%S"),
        "total": len(rows),
        "by_symbol": by_symbol,
        "top_up": top_up,
        "top_down": top_down,
    }


if __name__ == "__main__":
    sample = {
        "FG": {"close": 910, "open": 900, "high": 915, "low": 898, "name": "玻璃"},
        "SA": {"close": 980, "open": 1000, "high": 1005, "low": 975, "name": "纯碱"},
        "rb": {"close": 3300, "open": 3300, "high": 3320, "low": 3280, "name": "螺纹"},
    }
    r = compute(sample)
    print("异动:", r["total"], "品种")
    print("领涨:", [(x["name"], x["pct"]) for x in r["top_up"]])
    print("领跌:", [(x["name"], x["pct"]) for x in r["top_down"]])
