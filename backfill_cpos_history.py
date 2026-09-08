#!/usr/bin/env python3
"""
backfill_cpos_history.py — 龙虎榜历史回填 → cpos_cache.json
=================================================================
让 OOS / walk-forward 回测能按「每根 bar 的交易日」取到真实资金面 C_score，
而不是因为 cpos_cache.json 缺历史而恒为中性 0。

原理：复用 long_hu_bang 的抓取函数（fetch_czce/shfe/dce/gfex/ine）与
compute_c_score，对每个历史交易日抓取前 20 会员持仓并合并进各品种的
history 数组（按 date 去重、排序、不设 30 天截断）。四个维度管线
score_C(symbol, date) 本就支持按回测日期查 history（见 four_dim_strategy.py）。

与 long_hu_bang.run() 的区别：
- run() 每次只抓「今天或回溯几天」并 hist[-30:] 截断；
- 本脚本批量回填一段连续交易日，且不截断（配合 long_hu_bang.py 已上调的 600 上限）。

用法：
    python backfill_cpos_history.py --days 250      # 回填最近 250 个交易日
    python backfill_cpos_history.py --days 12       # 小批量测试
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import long_hu_bang as lhb  # noqa: E402

EXCHANGE_FETCHERS = [
    (lhb.fetch_czce, "ak", 40),
    (lhb.fetch_shfe, "requests", 40),
    (lhb.fetch_dce, "ak", 35),
    (lhb.fetch_gfex, "ak", 35),
    (lhb.fetch_ine, "requests", 30),
]


def get_trade_dates(n):
    """返回最近 n 个交易日（升序），优先 akshare 交易日历，失败回退工作日枚举。"""
    try:
        ak = lhb.ensure("akshare")
        df = ak.tool_trade_date_hist_sina()
        col = [c for c in df.columns if "date" in str(c).lower()][0]
        ds = [str(d).replace("-", "")[:8] for d in df[col].tolist()]
        ds = sorted({d for d in ds if len(d) == 8 and d.isdigit()})
        today = datetime.date.today().strftime("%Y%m%d")
        ds = [d for d in ds if d <= today]
        return ds[-n:]
    except Exception as e:
        print(f"[warn] 交易日历获取失败，回退工作日枚举: {e}")
        out, d = [], datetime.date.today()
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.strftime("%Y%m%d"))
            d -= datetime.timedelta(days=1)
        return list(reversed(out))


def merge_day(cache, recs, date):
    for rec in recs:
        sym = rec["symbol"].upper()
        cs = lhb.compute_c_score(rec)
        cur = cache.setdefault(sym, {})
        hist = [h for h in cur.get("history", []) if h.get("date") != date]
        hist.append({"date": date, "C_score": cs["C_score"], "net": cs["net"], "net_chg": cs["net_chg"]})
        hist.sort(key=lambda x: x["date"])
        hist = hist[-600:]
        cur["history"] = hist
        if cur.get("date") is None or date >= cur["date"]:
            cur.update(
                date=date,
                exchange=rec["exchange"],
                C_score=cs["C_score"],
                net=cs["net"],
                net_chg=cs["net_chg"],
                long_oi=cs["long_oi"],
                short_oi=cs["short_oi"],
                long_chg=cs["long_chg"],
                short_chg=cs["short_chg"],
                total_oi=cs["total_oi"],
            )
        cache[sym] = cur


def backfill(dates, sleep=0.4):
    ak = lhb.ensure("akshare")
    requests = lhb.ensure("requests")
    cache = lhb.load_cache()
    filled = 0
    newest = None
    for D in dates:
        recs = []
        for fn, mod_name, timeout in EXCHANGE_FETCHERS:
            mod = ak if mod_name == "ak" else requests
            try:
                recs += lhb.timed(fn, timeout, mod, D)
            except Exception as e:
                print(f"  [{D}] {fn.__name__} 异常: {e}")
        if not recs:
            print(f"  {D} 无数据，跳过")
            continue
        merge_day(cache, recs, D)
        newest = D
        filled += 1
        print(f"  {D}: {len(recs)} 品种 ✓")
        if sleep:
            time.sleep(sleep)

    cache["_meta"] = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": newest or cache.get("_meta", {}).get("trade_date", ""),
        "count": len(cache.get("_meta", {}).get("symbols", [])) if isinstance(cache.get("_meta"), dict) else 0,
        "backfilled": True,
        "backfill_days": filled,
        "source": "backfill_cpos_history.py",
    }
    with open(lhb.CPOS_JSON, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 {lhb.CPOS_JSON}（实际回填 {filled}/{len(dates)} 个交易日）")
    print("各品种 history 长度：")
    for sym in sorted(s for s in cache if s != "_meta"):
        print(f"  {sym}: {len(cache[sym].get('history', []))} 天")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="龙虎榜历史回填 → cpos_cache.json")
    p.add_argument("--days", type=int, default=250, help="回填最近 N 个交易日（默认 250）")
    p.add_argument("--sleep", type=float, default=0.4, help="每交易日间隔秒数（防限流）")
    a = p.parse_args()
    dates = get_trade_dates(a.days)
    if not dates:
        print("无可用交易日")
        sys.exit(1)
    print(f"待回填 {len(dates)} 个交易日，区间 {dates[0]}..{dates[-1]}")
    backfill(dates, a.sleep)
