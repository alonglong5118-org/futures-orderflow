#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用天勤(TqSdk) query_symbol_ranking 回填 DCE/全品种会员持仓排名(龙虎榜)历史，
写入 cpos_cache.json 的 history，使回测能按交易日查到真实 C_score（此前 DCE 因
akshare 历史接口失效而恒为 0）。

设计：
- 每个品种给定候选合约列表（前→后），天勤对每个合约返回 top-20 会员逐日排名；
- 按交易日聚合(sum long_oi/short_oi/change)，交给 long_hu_bang.compute_c_score 合成 C_score；
- 多合约重叠的日期取 total_oi 更大者（≈当日主力），实现换月拼接；
- 只覆盖目标品种，保留 cache 中其它品种(akshare 已填的 CZCE/SHFE 等)不动。

用法：
  python3 backfill_dce_tianqin.py [--days 300] [--start 2025-09-01]
"""
import sys
import os
import json
import datetime
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import long_hu_bang as lhb
from tqsdk import TqApi, TqAuth

CPOS = lhb.CPOS_JSON

# 品种 -> 候选合约(前→后) + 交易所。前主力覆盖近期，旧主力补齐早期。
COMMODITIES = {
    "FG": [("CZCE.FG609", "CZCE")],
    "SA": [("CZCE.SA609", "CZCE")],
    "JM": [("DCE.jm2609", "DCE")],
    "jd": [("DCE.jd2609", "DCE")],
    "lh": [("DCE.lh2609", "DCE")],
    "J":  [("DCE.j2609", "DCE"), ("DCE.j2605", "DCE"), ("DCE.j2601", "DCE")],
}


def _tq_creds():
    cfg = json.load(open(os.path.join(HERE, "tq_config.json"), encoding="utf-8"))
    return cfg["tq_username"], cfg["tq_password"]


def fetch_contract(contract, start_dt, days, user, pw):
    """返回该合约的 ranking DataFrame（含 top-20 会员逐日 long/short）。"""
    api = TqApi(auth=TqAuth(user, pw))
    try:
        df = api.query_symbol_ranking(contract, "LONG", days=days, start_dt=start_dt)
    finally:
        api.close()
    return df


def aggregate(df, sym, exch):
    """按交易日聚合 top-20 会员，合成 C_score。返回 {date: rec}。"""
    out = {}
    if df is None or df.shape[0] == 0:
        return out
    for dt, g in df.groupby("datetime"):
        long_oi = float(g["long_oi"].sum())
        short_oi = float(g["short_oi"].sum())
        long_chg = float(g["long_change"].sum())
        short_chg = float(g["short_change"].sum())
        total_oi = long_oi + short_oi
        rec = {
            "symbol": sym,
            "exchange": exch,
            "long_oi": long_oi,
            "short_oi": short_oi,
            "long_chg": long_chg,
            "short_chg": short_chg,
        }
        cs = lhb.compute_c_score(rec)
        out[dt] = {
            "date": dt,
            "C_score": cs["C_score"],
            "net": cs["net"],
            "net_chg": cs["net_chg"],
            "total_oi": int(total_oi),
        }
    return out


def backfill(days=300, start=None):
    user, pw = _tq_creds()
    if start is None:
        start = datetime.date(2025, 9, 1)
    cache = lhb.load_cache()
    summary = []
    for sym, contracts in COMMODITIES.items():
        merged = {}
        for contract, exch in contracts:
            try:
                df = fetch_contract(contract, start, days, user, pw)
            except Exception as e:
                print(f"  [{sym}] {contract} 拉取异常: {repr(e)[:120]}")
                continue
            if df.shape[0] == 0:
                print(f"  [{sym}] {contract} 无数据，跳过")
                continue
            agg = aggregate(df, sym, exch)
            for dt, rec in agg.items():
                cur = merged.get(dt)
                if cur is None or abs(rec.get("total_oi", 0)) > abs(cur.get("total_oi", 0)):
                    merged[dt] = rec
            print(f"  [{sym}] {contract}: +{len(agg)} 交易日")
        if not merged:
            print(f"[{sym}] 无可用数据，跳过")
            continue
        hist = sorted(merged.values(), key=lambda x: x["date"])
        # 取最近一日作为顶层 C_score（回测 miss 时回落值）
        latest = hist[-1]
        cache[sym] = {
            "date": latest["date"],
            "exchange": COMMODITIES[sym][0][1],
            "C_score": latest["C_score"],
            "net": latest["net"],
            "net_chg": latest["net_chg"],
            "long_oi": None,
            "short_oi": None,
            "long_chg": None,
            "short_chg": None,
            "total_oi": None,
            "history": hist,  # 全量历史（已按日期去重取主力）
        }
        summary.append((sym, len(hist), hist[0]["date"], hist[-1]["date"]))
        print(f"[{sym}] 回填完成: {len(hist)} 天  {hist[0]['date']}..{hist[-1]['date']}")
    cache["_meta"] = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": datetime.date.today().strftime("%Y%m%d"),
        "count": len(cache.get("_meta", {}).get("count", 0)) if False else sum(
            1 for k in cache if k != "_meta"
        ),
        "backfilled": True,
        "source": "backfill_dce_tianqin.py (天勤 query_symbol_ranking)",
    }
    with open(CPOS, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    print("\n=== 回填汇总 ===")
    for s, n, a, b in summary:
        print(f"  {s}: {n} 天  {a}..{b}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--start", type=str, default="2025-09-01")
    args = ap.parse_args()
    sy, sm, sd = map(int, args.start.split("-"))
    backfill(days=args.days, start=datetime.date(sy, sm, sd))
