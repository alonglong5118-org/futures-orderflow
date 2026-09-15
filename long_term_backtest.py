#!/usr/bin/env python3
"""10年长周期稳健性回测（2026-09-16）

目标：对「日线≥10年 且 长期5m≥10年」的老品种，验证 8 策略 + regime 加权
在长历史（含 2015 股灾/2016 供给侧/2020 疫情/2021 暴涨/2022 暴跌）下的表现。

口径：
  - daily  日线定信号 + 日线出场（walk_forward_backtest，快，全量）
  - 5m     日线定信号 + 5m 精细出场（walk_forward_backtest_5m_exit(long=True)，慢）

产出：
  1) 总览表：每品种 trades / expR / 胜率 / 分 regime expR
  2) 分年矩阵：年份 × 品种 → (交易笔数, 累计R, 平均R)
  3) 极端年份聚合（2015/2016/2020/2021/2022）
  4) JSON 落盘 long_term_backtest_result.json

用法：
  env -u PYTHONHOME -u PYTHONPATH $PY long_term_backtest.py            # 仅日线口径
  env -u PYTHONHOME -u PYTHONPATH $PY long_term_backtest.py --with-5m  # 追加 5m 出场口径
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import four_dim_strategy as fd

CUTOFF = "2016-09-16"  # 10 年前
EXTREME_YEARS = {2015, 2016, 2020, 2021, 2022}


def _year_of(dt):
    if hasattr(dt, "year"):
        return int(dt.year)
    # 兼容字符串 'YYYY-MM-DD'
    try:
        return int(str(dt)[:4])
    except Exception:
        return None


def eligible_symbols():
    """日线起始≤2016-09 且长期5m非空的老品种。"""
    out = []
    for sym in fd.SYMBOLS:
        d = fd.load_daily(sym)
        if d is None or len(d) == 0:
            continue
        start = str(d.index[0].date())
        d5 = fd.load_min5(sym, fetch_if_missing=False, long=True)
        if start <= CUTOFF and d5 is not None and len(d5) > 0:
            out.append((sym, start, len(d)))
    return out


def summarize(sym, r):
    """从回测结果提炼总览 + 分年统计。"""
    detail = r.get("trades_detail", []) or []
    per_year = {}  # year -> dict(trades, sumR, listR)
    per_regime = {}
    for t in detail:
        y = _year_of(t.get("entry_date"))
        reg = t.get("regime", "未知")
        rr = t.get("R_adj", 0.0)
        if y is not None:
            per_year.setdefault(y, {"trades": 0, "sumR": 0.0, "rs": []})
            per_year[y]["trades"] += 1
            per_year[y]["sumR"] += rr
            per_year[y]["rs"].append(rr)
        per_regime.setdefault(reg, {"trades": 0, "sumR": 0.0, "rs": []})
        per_regime[reg]["trades"] += 1
        per_regime[reg]["sumR"] += rr
        per_regime[reg]["rs"].append(rr)
    year_stats = {
        y: {
            "trades": v["trades"],
            "sumR": round(v["sumR"], 3),
            "avgR": round(v["sumR"] / v["trades"], 3) if v["trades"] else 0.0,
        }
        for y, v in sorted(per_year.items())
    }
    regime_stats = {
        reg: {
            "trades": v["trades"],
            "avgR": round(v["sumR"] / v["trades"], 3) if v["trades"] else 0.0,
        }
        for reg, v in sorted(per_regime.items())
    }
    return year_stats, regime_stats


def run(mode="daily", symbols=None):
    symbols = symbols or eligible_symbols()
    rows = []
    t0 = time.time()
    for idx, (sym, start, nd) in enumerate(symbols, 1):
        try:
            if mode == "5m":
                r = fd.walk_forward_backtest_5m_exit(sym, fd.DEFAULT_CONFIG, long=True)
            else:
                r = fd.walk_forward_backtest(sym, fd.DEFAULT_CONFIG)
        except Exception as e:
            r = {"symbol": sym, "trades": 0, "note": f"异常:{repr(e)[:60]}"}
        ystats, rstats = summarize(sym, r)
        rows.append(
            {
                "symbol": sym,
                "name": fd.SYMBOLS.get(sym, {}).get("name", ""),
                "daily_start": start,
                "trades": r.get("trades", 0),
                "expR": r.get("expR", 0.0),
                "win_rate": r.get("win_rate", 0.0),
                "by_regime": r.get("by_regime", {}),
                "regime_stats": rstats,
                "year_stats": ystats,
                "note": r.get("note", ""),
            }
        )
        print(
            f"[{idx}/{len(symbols)}] {sym:4} {rows[-1]['name']:4} "
            f"笔={r.get('trades',0):>3} expR={r.get('expR',0):>7.3f} "
            f"胜率={r.get('win_rate',0)*100:>5.1f}%  {mode}",
            flush=True,
        )
    return rows, time.time() - t0


def extreme_agg(rows):
    """极端年份聚合：这些年份全品种合计交易数 / 累计R / 平均R。"""
    agg = {}
    for row in rows:
        for y, v in row["year_stats"].items():
            if y in EXTREME_YEARS:
                agg.setdefault(y, {"trades": 0, "sumR": 0.0})
                agg[y]["trades"] += v["trades"]
                agg[y]["sumR"] += v["sumR"]
    return {y: {**v, "avgR": round(v["sumR"] / v["trades"], 3) if v["trades"] else 0.0} for y, v in sorted(agg.items())}


def print_matrix(rows, title):
    years = sorted({y for row in rows for y in row["year_stats"]})
    print(f"\n===== {title} 分年矩阵（年份 × 品种，格式: 笔数/累计R）=====")
    header = "年份   " + " ".join(f"{row['symbol']:>6}" for row in rows)
    print(header)
    for y in years:
        cells = []
        for row in rows:
            v = row["year_stats"].get(y)
            if v and v["trades"] > 0:
                cells.append(f"{v['trades']:>3}/{v['sumR']:+.0f}".rjust(6))
            else:
                cells.append("  ·  ".rjust(6))
        print(f"{y:4}  " + " ".join(cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-5m", action="store_true", help="追加 5m 出场口径（慢，每品种约12s）")
    ap.add_argument("--symbols", type=str, default=None, help="逗号分隔品种，默认全部老品种")
    args = ap.parse_args()

    syms = eligible_symbols()
    if args.symbols:
        want = {s.strip() for s in args.symbols.split(",") if s.strip()}
        syms = [s for s in syms if s[0] in want]

    print(f"老品种清单（日线≥10年+5m≥10年）: {len(syms)} 个", flush=True)
    for sym, start, nd in syms:
        print(f"  {sym:4} 日线自 {start}", flush=True)

    result = {"generated": datetime.now().isoformat(), "mode": [], "extreme_years": sorted(EXTREME_YEARS)}

    # 日线口径
    rows_daily, dt_daily = run("daily", syms)
    result["mode"].append("daily")
    result["daily"] = {"rows": rows_daily, "elapsed_sec": round(dt_daily, 1)}

    rows_5m = None
    if args.with_5m:
        rows_5m, dt_5m = run("5m", syms)
        result["mode"].append("5m")
        result["5m"] = {"rows": rows_5m, "elapsed_sec": round(dt_5m, 1)}

    # 落盘
    with open("long_term_backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ── 报告 ──
    for label, rows in (("日线口径", rows_daily), ("5m出场口径", rows_5m)):
        if rows is None:
            continue
        print(f"\n\n########## {label} 总览 ##########")
        print(f"{'品种':4} {'名称':6} {'笔':>3} {'expR':>8} {'胜率':>6}  分regime")
        pos = neg = zero = 0
        for row in rows:
            br = " ".join(f"{k}:{v:+.2f}" for k, v in row["by_regime"].items())
            flag = ""
            if row["trades"] > 0:
                if row["expR"] > 0:
                    pos += 1
                elif row["expR"] < 0:
                    neg += 1
                else:
                    zero += 1
            if row["symbol"] in fd.DISABLED_SYMBOLS:
                flag = " [硬禁]"
            print(f"{row['symbol']:4} {row['name']:6} {row['trades']:>3} {row['expR']:>8.3f} {row['win_rate']*100:>5.1f}%  {br}{flag}")
        total_trades = sum(r["trades"] for r in rows)
        print(f"\n合计: {len(rows)} 品种, {total_trades} 笔 | 正期望 {pos} / 负期望 {neg} / 零 {zero}")
        ea = extreme_agg(rows)
        if ea:
            print("\n极端年份聚合（全品种合计）:")
            for y, v in ea.items():
                print(f"  {y}: {v['trades']} 笔, 累计R={v['sumR']:+.2f}, 平均R={v['avgR']:+.3f}")
        print_matrix(rows, label)

    print(f"\n结果已落盘: long_term_backtest_result.json")


if __name__ == "__main__":
    main()
