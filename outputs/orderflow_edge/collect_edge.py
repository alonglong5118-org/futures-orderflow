#!/usr/bin/env python3
"""
collect_edge.py — 订单流 edge 验证 采集期每日计数 harness (Vanta 三感参谋 · 订单流分层面板)

🔴 铁律（来自 docs/orderflow-edge-preregistration-20261004.md）：
  本脚本**只数信号个数，绝不计算命中率**（§7.2）。任何前向 H 秒命中逻辑一律不存在。
  阈值 / 冷却 / 窗口 全部预注册写死（§3 / §13.3），此处不提供任何可调参数绕过。

数据来源（均在 M4）：
  源1 = tick_stream.jsonl (tqsdk 成交流)  → 主终点 FG/SA
  源2 = ~/.local/share/intraday-report/work/data/ticks/*.parquet (CTP 1s 快照) → 次要终点 S1 (18 品种纯 imbalance)

分数算法直接 import **冻结**的 tick_orderflow.py（§9 一行不改），本脚本只持有预注册的
阈值/冷却/窗口（属预注册，非算法）—— 二者严格分离。

用法：
  python3 collect_edge.py --date 2026-10-08            # 正式采集该交易日
  python3 collect_edge.py --date 2026-09-22 --test     # 校准模式（写独立 test tally，不污染正式累计）
  python3 collect_edge.py --status                     # 打印累计 tally

正式数据从 2026-10-08 起；date < START_DATE 时跳过（launchd 即便提前加载也不会污染）。
"""

import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, date

import pandas as pd
import warnings

warnings.filterwarnings("ignore")

# ───────── 预注册常量（写死，禁止命令行覆盖） ─────────
START_DATE = "2026-10-08"
THR = 30  # §3 主档 |score|≥30 ；S1 |imbalance|≥30
COOLDOWN = 900  # §3 同品种同方向 900s
WIN1 = 600  # §3 源1 滚动窗口 600 笔 (≈300s @0.5s)
WIN2 = 300  # §13.3(3) 源2 滚动窗口 300 笔 (≈300s @1.0s)
SYMBOLS_SRC1 = ["FG", "SA"]
SYMBOLS_SRC2 = ["al", "cu", "hc", "rb", "sc", "zn", "i", "m", "p", "y", "MA", "TA", "jd", "lh", "FG", "SA", "jm", "j"]

# M4 默认路径
DEF_SRC1 = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run/tick_stream.jsonl"
DEF_SRC2_DIR = os.path.expanduser("~/.local/share/intraday-report/work/data/ticks")
DEF_FROZEN = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
DEF_TALLY = "/Users/a123/WorkBuddy/2026-10-02-23-00-14/outputs/orderflow_edge/tally.csv"
DEF_TEST_TALLY = "/Users/a123/WorkBuddy/2026-10-02-23-00-14/outputs/orderflow_edge/tally_test.csv"


def shanghai_date(ts):
    """epoch 秒 → 上海日期字符串 (ts 视为 UTC epoch，+8h 对齐源2文件名)。"""
    return datetime.utcfromtimestamp(ts + 8 * 3600).strftime("%Y-%m-%d")


def load_frozen(frozen_dir):
    sys.path.insert(0, frozen_dir)
    import tick_orderflow

    return tick_orderflow


def count_src1(path, date_str, tof):
    """源1：FG/SA 成交流，综合 score 阈值30，900s 同向冷却。返回 (counts, meta)。"""
    rows = {s: [] for s in SYMBOLS_SRC1}
    akshare = {s: 0 for s in SYMBOLS_SRC1}
    if not os.path.exists(path):
        return {s: 0 for s in SYMBOLS_SRC1}, {"rows": {}, "akshare": akshare, "start": {}, "end": {}, "missing": True}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            sym = t.get("symbol")
            if sym not in SYMBOLS_SRC1:
                continue
            if shanghai_date(t.get("ts", 0)) != date_str:
                continue
            if t.get("data_mode") != "tqsdk":
                akshare[sym] += 1
                continue
            rows[sym].append(t)
    counts = {}
    start = {}
    end = {}
    for s in SYMBOLS_SRC1:
        acc = tof.TickOrderflow(s, window=WIN1)
        last = {1: -1e18, -1: -1e18}
        cnt = 0
        st = None
        en = None
        for t in rows[s]:
            acc.push(t.get("price", 0), t.get("vol", 0), t.get("side"), t.get("bid_vol"), t.get("ask_vol"), t.get("ts"))
            sc = acc.score()
            if abs(sc) >= THR:
                d = 1 if sc > 0 else -1
                if t["ts"] - last[d] >= COOLDOWN:
                    cnt += 1
                    last[d] = t["ts"]
            if st is None:
                st = t["ts"]
            en = t["ts"]
        counts[s] = cnt
        start[s] = shanghai_date(st) + " " + datetime.utcfromtimestamp(st + 8 * 3600).strftime("%H:%M:%S") if st else ""
        end[s] = shanghai_date(en) + " " + datetime.utcfromtimestamp(en + 8 * 3600).strftime("%H:%M:%S") if en else ""
    meta = {
        "rows": {s: len(rows[s]) for s in SYMBOLS_SRC1},
        "akshare": akshare,
        "start": start,
        "end": end,
        "missing": False,
    }
    return counts, meta


def count_src2(src2_dir, date_str, tof):
    """源2：18 品种 1s 快照，纯 imbalance 阈值30，900s 同向冷却。返回 (per_symbol, meta)。"""
    ymd = date_str.replace("-", "")
    files = glob.glob(os.path.join(src2_dir, f"*_{ymd}*.parquet"))
    per_sym = {s: 0 for s in SYMBOLS_SRC2}
    bid1_nn = 0
    bid1_tot = 0
    ask1_nn = 0
    ask1_tot = 0
    if not files:
        return per_sym, {"files": 0, "symbols": 0, "bid1_nn": 0.0, "ask1_nn": 0.0, "missing": True}
    # 按品种分组
    groups = {}
    for fp in files:
        base = os.path.basename(fp)
        sym = base.split("_")[0]
        groups.setdefault(sym, []).append(fp)
    for sym in SYMBOLS_SRC2:
        if sym not in groups:
            continue
        acc = tof.TickOrderflow(sym, window=WIN2)
        last = {1: -1e18, -1: -1e18}
        cnt = 0
        for fp in sorted(groups[sym]):
            try:
                df = pd.read_parquet(fp)
            except Exception:
                continue
            for _, r in df.iterrows():
                ts = r["ts"].timestamp() if hasattr(r["ts"], "timestamp") else float(r["ts"])
                bv = r.get("bvol1", 0) or 0
                av = r.get("avol1", 0) or 0
                acc.push(r.get("last", 0), r.get("volume", 0), None, bv, av, ts)
                imb = acc.imbalance_score()
                if abs(imb) >= THR:
                    d = 1 if imb > 0 else -1
                    if ts - last[d] >= COOLDOWN:
                        cnt += 1
                        last[d] = ts
                # 校验累计
                if not pd_isna(r.get("bid1")):
                    bid1_tot += 1
                    if r.get("bid1") != 0:
                        bid1_nn += 1
                if not pd_isna(r.get("ask1")):
                    ask1_tot += 1
                    if r.get("ask1") != 0:
                        ask1_nn += 1
        per_sym[sym] = cnt
    meta = {
        "files": len(files),
        "symbols": len(groups),
        "bid1_nn": (bid1_nn / bid1_tot) if bid1_tot else 0.0,
        "ask1_nn": (ask1_nn / ask1_tot) if ask1_tot else 0.0,
        "missing": False,
    }
    return per_sym, meta


def pd_isna(v):
    try:
        return v is None or (isinstance(v, float) and v != v)
    except Exception:
        return False


def read_tally(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_row(path, row, fieldnames):
    exist = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exist:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD")
    ap.add_argument("--src1", default=DEF_SRC1)
    ap.add_argument("--src2-dir", default=DEF_SRC2_DIR)
    ap.add_argument("--frozen-dir", default=DEF_FROZEN)
    ap.add_argument("--tally", default=DEF_TALLY)
    ap.add_argument("--test", action="store_true", help="校准模式：写独立 test tally，不污染正式累计")
    ap.add_argument("--status", action="store_true", help="打印累计 tally 后退出")
    ap.add_argument("--no-append", action="store_true", help="只计算打印，不写 tally")
    ap.add_argument("--ignore-start", action="store_true", help="忽略起始日守卫（仅校准/测试用）")
    args = ap.parse_args()

    if args.status:
        rows = read_tally(args.tally)
        for r in rows:
            print(
                f"{r['date']}  src1={r['src1_merged']}  src2={r['src2_merged']}  "
                f"cum1={r.get('cum_src1', '')}  cum2={r.get('cum_src2', '')}  flags={r.get('flags', '')}"
            )
        if not rows:
            print("(空)")
        return

    if not args.date:
        ap.error("--date 必填（或 --status）")
    date_str = args.date
    if date_str < START_DATE and not args.ignore_start:
        print(f"[SKIP] {date_str} < 起始日 {START_DATE}，不采集（launchd 提前加载时静默跳过）")
        return

    tof = load_frozen(args.frozen_dir)

    # ── 源1 ──
    s1_counts, s1_meta = count_src1(args.src1, date_str, tof)
    s1_merged = sum(s1_counts.values())
    # ── 源2 ──
    s2_per, s2_meta = count_src2(args.src2_dir, date_str, tof)
    s2_merged = sum(s2_per.values())

    # ── §13.4 校验 ──
    flags = []
    if s1_meta.get("missing"):
        flags.append("SRC1_FILE_MISSING")
    for s in SYMBOLS_SRC1:
        if s1_meta["rows"].get(s, 0) < 10000:
            flags.append(f"SRC1_{s}_LOWROWS({s1_meta['rows'].get(s, 0)})")
        if s1_meta["akshare"].get(s, 0) > 0:
            flags.append(f"SRC1_{s}_AKSHARE({s1_meta['akshare'][s]})")
    if s2_meta.get("missing"):
        flags.append("SRC2_FILE_MISSING")
    else:
        if s2_meta["symbols"] < len(SYMBOLS_SRC2):
            flags.append(f"SRC2_SYMBOLS_LOW({s2_meta['symbols']}/{len(SYMBOLS_SRC2)})")
        if s2_meta["bid1_nn"] < 0.80:
            flags.append(f"SRC2_BID1_LOW({s2_meta['bid1_nn']:.2f})")
        if s2_meta["ask1_nn"] < 0.80:
            flags.append(f"SRC2_ASK1_LOW({s2_meta['ask1_nn']:.2f})")

    # ── 累计 ──
    tally_path = args.tally
    if args.test and args.tally == DEF_TALLY:
        tally_path = DEF_TEST_TALLY
    prev = read_tally(tally_path)
    cum1 = s1_merged + sum(int(r.get("src1_merged", 0) or 0) for r in prev)
    cum2 = s2_merged + sum(int(r.get("src2_merged", 0) or 0) for r in prev)

    row = {
        "date": date_str,
        "src1_FG": s1_counts.get("FG", 0),
        "src1_SA": s1_counts.get("SA", 0),
        "src1_merged": s1_merged,
        "src1_FG_rows": s1_meta["rows"].get("FG", 0),
        "src1_SA_rows": s1_meta["rows"].get("SA", 0),
        "src1_FG_akshare": s1_meta["akshare"].get("FG", 0),
        "src1_SA_akshare": s1_meta["akshare"].get("SA", 0),
        "src1_FG_start": s1_meta["start"].get("FG", ""),
        "src1_FG_end": s1_meta["end"].get("FG", ""),
        "src1_SA_start": s1_meta["start"].get("SA", ""),
        "src1_SA_end": s1_meta["end"].get("SA", ""),
        "src2_files": s2_meta.get("files", 0),
        "src2_symbols": s2_meta.get("symbols", 0),
        "src2_bid1_nn": round(s2_meta.get("bid1_nn", 0.0), 4),
        "src2_ask1_nn": round(s2_meta.get("ask1_nn", 0.0), 4),
        "src2_merged": s2_merged,
        "src2_per_symbol": json.dumps(s2_per, ensure_ascii=False),
        "cum_src1": cum1,
        "cum_src2": cum2,
        "flags": ";".join(flags) if flags else "OK",
    }
    fields = list(row.keys())

    # 报告
    print(f"===== 订单流 edge 采集 {date_str} ({'TEST' if args.test else 'PROD'}) =====")
    print(f"源1(FG+SA 综合score≥30): FG={s1_counts.get('FG', 0)} SA={s1_counts.get('SA', 0)} 合并={s1_merged}")
    print(
        f"    FG 行={s1_meta['rows'].get('FG', 0)} 起={s1_meta['start'].get('FG', '')} 终={s1_meta['end'].get('FG', '')}"
    )
    print(
        f"    SA 行={s1_meta['rows'].get('SA', 0)} 起={s1_meta['start'].get('SA', '')} 终={s1_meta['end'].get('SA', '')}"
    )
    print(f"    akshare_min(剔除不计数): FG={s1_meta['akshare'].get('FG', 0)} SA={s1_meta['akshare'].get('SA', 0)}")
    print(
        f"源2(18品种 纯imbalance≥30): 合并={s2_merged}  文件={s2_meta.get('files', 0)} 品种={s2_meta.get('symbols', 0)}"
    )
    print(f"    bid1非空率={s2_meta.get('bid1_nn', 0):.3f} ask1非空率={s2_meta.get('ask1_nn', 0):.3f}")
    print(f"    分品种: {s2_per}")
    print(f"累计: src1={cum1}  src2={cum2}")
    print(f"校验flags: {row['flags']}")
    print(f"⚠️ 本脚本只数信号，未计算任何命中率（§7.2）。TICK_FEED_ENABLED 请人工确认仍为 0。")

    if not args.no_append:
        write_row(tally_path, row, fields)
        print(f"[OK] 已写入 {tally_path}")


if __name__ == "__main__":
    main()
