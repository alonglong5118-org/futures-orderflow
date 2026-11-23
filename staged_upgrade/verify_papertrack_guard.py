"""只读验证：papertrack 的 live5m 判定是否被「未来行情」污染。

背景 2026-11-20：
  four_dim_papertrack._load_backtest_bars() 第 0 优先级取 live_bars.json（只含今天最后 ~200 根 5m），
  条件仅为 `index > signal_dt` 且行数 >= 3，没有校验第一根是否紧接信号时间。
  ⇒ 30 天前甚至 2 个月前的信号，被拿今天的 K 线去判 → 第一根就是今天价格 → 多单 low 立刻穿止损 → 一律 -1R。

本脚本不改任何文件、不写报告，只做三件事：
  1. 统计「污染」规模：判定用的第一根 K 线距信号多久
  2. 用「第一根必须紧接信号（<=30 分钟）」的守卫重算，对比新旧胜负
  3. 输出修正后的胜率 / 期望R，判断 9% 是策略问题还是数据问题

用法：
    python3 verify_papertrack_guard.py            # 全量
    python3 verify_papertrack_guard.py --limit 40 # 只跑前 40 笔（快）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pandas as pd  # noqa: E402

import four_dim_papertrack as pt  # noqa: E402

MAX_START_GAP_MIN = 30  # 第一根 K 线必须落在信号后 30 分钟内，否则视为「未来行情」


def guarded_load(symbol: str, signal_dt, _daily_cache: dict):
    """按原始优先级取 K 线，但对 live5m 加「第一根必须紧接信号」守卫。"""
    # 0) runner 实时 5m
    try:
        lb = pt._load_live_bars(symbol)
        if lb is not None and len(lb):
            after = lb[lb.index > signal_dt]
            if len(after) >= 3:
                gap = (after.index[0] - signal_dt).total_seconds() / 60.0
                if 0 <= gap <= MAX_START_GAP_MIN:
                    return after, "live5m", gap
                # 否则拒绝：这批 K 线比信号晚太多，是未来行情
    except Exception:
        pass

    # 1) 本地 5m 缓存
    try:
        import four_dim_strategy as fd

        df5 = fd.load_min5(symbol.upper(), fetch_if_missing=False)
        if df5 is not None and len(df5):
            after = df5[df5.index > signal_dt]
            if len(after) >= 3:
                gap = (after.index[0] - signal_dt).total_seconds() / 60.0
                return after, "5m", gap
    except Exception:
        pass

    # 2) 日线
    try:
        import four_dim_strategy as fd

        if symbol not in _daily_cache:
            _daily_cache[symbol] = fd.load_daily_refreshed(symbol)
        dfd = _daily_cache.get(symbol)
        if dfd is not None and len(dfd):
            sdate = signal_dt.normalize()
            after = dfd[dfd.index >= sdate]
            if len(after) >= 1:
                gap = (after.index[0] - signal_dt).total_seconds() / 60.0
                return after, "daily", gap
    except Exception:
        pass
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default=os.path.join(HERE, "papertrack_report.json"))
    ap.add_argument("--save", default="", help="把逐笔对照结果写成 JSON（默认不写）")
    args = ap.parse_args()

    report = json.load(open(args.json, encoding="utf-8"))
    trades = report.get("trades") or []
    if args.limit:
        trades = trades[: args.limit]

    daily_cache: dict = {}
    rows = []
    for t in trades:
        sdt = pd.to_datetime(t["time"])
        bars, gran, gap = guarded_load(t["symbol"], sdt, daily_cache)
        if bars is None or len(bars) == 0:
            new = {"outcome": None, "R": 0.0, "status": "pending", "gran": gran}
        else:
            p = {
                "symbol": t["symbol"],
                "time": t["time"],
                "direction": t["direction"],
                "entry": t["entry"],
                "stop": t["stop"],
                "target": t["target"],
                "target_R": t.get("target_R", 2.0),
            }
            new = pt.backtest_signal(p, bars=bars, gran=gran)
        rows.append(
            {
                "id": t.get("id"),
                "symbol": t["symbol"],
                "time": t["time"],
                "old_outcome": t.get("outcome"),
                "old_R": t.get("R"),
                "old_gran": t.get("gran"),
                "new_outcome": new.get("outcome"),
                "new_R": new.get("R"),
                "new_gran": gran,
                "start_gap_min": round(gap, 1) if gap is not None else None,
                "status": new.get("status"),
            }
        )

    # ── 污染规模 ──
    live = [r for r in rows if r["old_gran"] == "live5m"]
    polluted = [r for r in live if (r["start_gap_min"] is None) or (r["start_gap_min"] > MAX_START_GAP_MIN)]
    print("=" * 74)
    print(f"样本 {len(rows)} 笔 ｜ 原判定用 live5m 的 {len(live)} 笔")
    print(f"其中第一根 K 线距信号 >{MAX_START_GAP_MIN} 分钟（=用了未来行情）: {len(polluted)} 笔")
    if polluted:
        gaps = sorted(r["start_gap_min"] for r in polluted if r["start_gap_min"] is not None)
        if gaps:
            print(f"  滞后中位数 {gaps[len(gaps) // 2]:.0f} 分钟（≈ {gaps[len(gaps) // 2] / 60 / 24:.1f} 天）")
        for r in polluted[:3]:
            print(f"  例：{r['symbol']} 信号 {r['time']} → 判定首根晚 {r['start_gap_min']} 分钟")

    def stat(key_out, key_r):
        done = [r for r in rows if r[key_out] in ("win", "loss")]
        if not done:
            return 0, 0, 0.0, 0.0
        wins = sum(1 for r in done if r[key_out] == "win")
        return len(done), wins, wins / len(done) * 100, sum(r[key_r] or 0 for r in done) / len(done)

    n0, w0, wr0, er0 = stat("old_outcome", "old_R")
    n1, w1, wr1, er1 = stat("new_outcome", "new_R")
    print("-" * 74)
    print(f"原判定 : 已判定 {n0:3d} 笔 ｜ 胜率 {wr0:5.1f}% ｜ 期望R {er0:+.3f} ｜ 累计R {sum(r['old_R'] or 0 for r in rows):+.1f}")
    print(f"守卫后 : 已判定 {n1:3d} 笔 ｜ 胜率 {wr1:5.1f}% ｜ 期望R {er1:+.3f} ｜ 累计R {sum(r['new_R'] or 0 for r in rows):+.1f}")
    flipped = sum(1 for r in rows if r["old_outcome"] != r["new_outcome"] and r["old_outcome"] in ("win", "loss"))
    print(f"判定翻转的笔数 : {flipped}")
    print("-" * 74)
    from collections import Counter

    print("守卫后数据源分布 :", Counter(r["new_gran"] for r in rows).most_common())
    print("=" * 74)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print(f"逐笔对照已写入 {args.save}")
    print(json.dumps(rows[:3], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
