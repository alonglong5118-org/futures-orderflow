"""G1 基本面指标（利润 / 比价 / 价差）—— 四维策略盯盘模型

把跨品种基本面关系量化成可监控的指标：
  - 利润类（profit / profit_feed）：加工利润、养殖利润，含固定成本与饲料项
  - 比价类（ratio）：两个品种价格比（如 FG/SA、JM/J）
  - 价差类（spread）：两个品种价格差（如 rb-hc 螺卷差）

数据来源：four_dim_strategy.load_daily_refreshed(sym) 的最新收盘 + 近 60 日序列。
纯只读、不接券商 API、不代下单；数值缺则 latest=None / value_ok=False（前端标注）。
"""

from __future__ import annotations

import datetime
import json
import time

from four_dim_strategy import load_daily_refreshed

try:
    import pandas as pd
except Exception:  # 极端环境兜底
    pd = None


# ---------------------------------------------------------------------------
# 指标定义
# ---------------------------------------------------------------------------
METRIC_DEFS = [
    {
        "id": "fg_sa_profit",
        "cat": "利润",
        "name": "玻璃-纯碱加工利润",
        "unit": "元/吨",
        "kind": "profit",
        "legs": [("FG", 1), ("SA", -0.2)],
        "fixed": -500,
        "note": "每吨玻璃耗约0.2吨纯碱；固定成本约500元/吨(燃料+人工)为经验值，可调",
    },
    {
        "id": "jm_j_profit",
        "cat": "利润",
        "name": "焦煤-焦炭化利润",
        "unit": "元/吨",
        "kind": "profit",
        "legs": [("J", 1), ("JM", -1.33)],
        "fixed": -300,
        "note": "吨焦耗约1.33吨焦煤；固定成本约300为经验值",
    },
    {
        "id": "lh_breed_profit",
        "cat": "利润",
        "name": "生猪自繁自养利润",
        "unit": "元/头",
        "kind": "profit_feed",
        "legs": [("lh", 16)],
        "feed": [("c", 2.6), ("m", 0.9)],
        "note": "头均16kg；饲料=玉米×2.6+豆粕×0.9 为经验估算",
    },
    {
        "id": "jd_layer_profit",
        "cat": "利润",
        "name": "蛋鸡养殖利润",
        "unit": "元/500kg",
        "kind": "profit_feed",
        "legs": [("jd", 1)],
        "feed": [("c", 2.2), ("m", 0.8)],
        "fixed": -300,
        "note": "蛋鸡饲料经验估算，系数可调；固定成本约300元/500kg为经验值",
    },
    {
        "id": "fg_sa_ratio",
        "cat": "比价",
        "name": "玻璃/纯碱比价",
        "unit": "×",
        "kind": "ratio",
        "legs": [("FG", 1), ("SA", -1)],
        "note": "FG/SA 价格比",
    },
    {
        "id": "jm_j_ratio",
        "cat": "比价",
        "name": "焦煤/焦炭比价",
        "unit": "×",
        "kind": "ratio",
        "legs": [("JM", 1), ("J", -1)],
        "note": "JM/J 价格比",
    },
    {
        "id": "rb_hc_spread",
        "cat": "价差",
        "name": "螺卷差",
        "unit": "元/吨",
        "kind": "spread",
        "legs": [("rb", 1), ("hc", -1)],
        "note": "rb-hc 价差",
    },
]


# ---------------------------------------------------------------------------
# 数据取数
# ---------------------------------------------------------------------------
def _last_close(sym):
    """返回 sym 最新收盘价（float），失败/缺数据返回 None。"""
    try:
        df = load_daily_refreshed(sym)
        if df is None or len(df) == 0:
            return None
        return float(df["close"].iloc[-1])
    except Exception:
        return None


def _hist(sym, n=60):
    """返回 sym 最近 n 个收盘价为 list，失败/缺数据返回 None。"""
    try:
        df = load_daily_refreshed(sym)
        if df is None or len(df) == 0:
            return None
        return [float(x) for x in df["close"].iloc[-n:].tolist()]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 单指标计算
# ---------------------------------------------------------------------------
def _value_at(hist_map, kind, legs, fixed, feed):
    """给定各 leg/feed 的历史序列 dict，计算单日 metric 值。缺数据返回 None。"""
    try:
        if kind == "profit":
            s = 0.0
            for sym, coef in legs:
                h = hist_map.get(sym)
                if h is None or len(h) == 0:
                    return None
                s += coef * h[-1]
            if fixed is not None:
                s += fixed
            return s
        if kind == "profit_feed":
            s = 0.0
            for sym, coef in legs:
                h = hist_map.get(sym)
                if h is None or len(h) == 0:
                    return None
                s += coef * h[-1]
            for sym, coef in feed or []:
                h = hist_map.get(sym)
                if h is None or len(h) == 0:
                    return None
                s -= coef * h[-1]
            return s
        if kind == "ratio":
            # legs[0]/legs[1]（取绝对值比；coef 仅标记符号方向）
            a, b = legs[0][0], legs[1][0]
            ha, hb = hist_map.get(a), hist_map.get(b)
            if ha is None or hb is None or len(ha) == 0 or len(hb) == 0:
                return None
            if hb[-1] == 0:
                return None
            return ha[-1] / hb[-1]
        if kind == "spread":
            s = 0.0
            for sym, coef in legs:
                h = hist_map.get(sym)
                if h is None or len(h) == 0:
                    return None
                s += coef * h[-1]
            return s
    except Exception:
        return None
    return None


def compute_metric(d):
    """计算单条指标定义 d 的最新值 / 均值 / z-score / 趋势。

    返回 (latest, mean30, zscore, trend, value_ok)。
    任一 leg 数据缺失 → (None, None, None, None, False)。
    """
    all_syms = [s for s, _ in d.get("legs", [])]
    all_syms += [s for s, _ in (d.get("feed") or [])]

    # 最新值
    latest = _value_at(
        {s: [_last_close(s)] for s in all_syms}, d["kind"], d.get("legs", []), d.get("fixed"), d.get("feed")
    )
    if latest is None:
        return (None, None, None, None, False)

    # 历史序列（逐日对齐后逐日算 metric 值）
    hists = {}
    for s in all_syms:
        h = _hist(s, 60)
        if h is None:
            return (None, None, None, None, False)
        hists[s] = h
    n = min(len(hists[s]) for s in all_syms)
    series = []
    for i in range(n):
        # _value_at 用 [-1]，需传“到当日为止”的序列：截断到 i+1 长度
        day_hist = {s: hists[s][: i + 1] for s in all_syms}
        v = _value_at(day_hist, d["kind"], d.get("legs", []), d.get("fixed"), d.get("feed"))
        if v is None:
            return (None, None, None, None, False)
        series.append(v)
    if len(series) < 30:
        return (round(latest, 2) if latest is not None else None, None, None, None, True)
    mean30 = sum(series[-30:]) / 30.0
    std30 = (sum((x - mean30) ** 2 for x in series[-30:]) / 30.0) ** 0.5
    zscore = round((latest - mean30) / std30, 2) if std30 and std30 > 0 else None
    # 趋势：末5日均 vs 前5日均
    last5 = sum(series[-5:]) / 5.0
    prev5 = sum(series[-10:-5]) / 5.0 if len(series) >= 10 else last5
    if prev5 == 0:
        trend = "持平"
    elif last5 > prev5 * 1.001:
        trend = "扩大"
    elif last5 < prev5 * 0.999:
        trend = "收窄"
    else:
        trend = "持平"
    return (round(latest, 2), round(mean30, 2), zscore, trend, True)


# ---------------------------------------------------------------------------
# 批量入口（带缓存）
# ---------------------------------------------------------------------------
_FM_CACHE = {"t": 0.0, "v": None}


def fund_metrics(force=False, cache_sec=300):
    """返回全部指标的最新值。带进程内缓存（cache_sec 秒）。"""
    global _FM_CACHE
    _now = time.time()
    if not force and _FM_CACHE["v"] is not None and (_now - _FM_CACHE["t"]) < cache_sec:
        return _FM_CACHE["v"]
    metrics = []
    for d in METRIC_DEFS:
        latest, mean30, zscore, trend, ok = compute_metric(d)
        metrics.append(
            {
                "id": d["id"],
                "cat": d["cat"],
                "name": d["name"],
                "unit": d["unit"],
                "latest": latest,
                "mean30": mean30,
                "zscore": zscore,
                "trend": trend,
                "value_ok": ok,
                "note": d.get("note", ""),
            }
        )
    out = {
        "ok": True,
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
    }
    _FM_CACHE = {"t": _now, "v": out}
    return out


if __name__ == "__main__":
    print(json.dumps(fund_metrics(force=True), ensure_ascii=False, indent=2, default=str))
