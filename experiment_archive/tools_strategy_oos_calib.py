#!/usr/bin/env python3
"""通用策略品种级 OOS 校准（2026-09-16）

对任意单策略做品种级 OOS 校准（rsi/boll 已单独完成，本脚本覆盖其余 6 个：
ma_break / dma / turtle / donchian / pullback / seasonal）。

方法（与 tools_boll_oos_calib.py 完全一致）：
  - 前 ratio 训练 → 后 (1-ratio) 测试（OOS）
  - 每品种保持「除目标策略外」blacklist 现状不变，仅切换目标策略启用/禁用
  - delta = expR(禁用) - expR(启用)；正值 = 禁用更优
  - 判定以 test 段为准；逐品种 pooled 符号检验

用法：
  $PY tools_strategy_oos_calib.py --strategy ma_break                 # 2/3 窗口
  $PY tools_strategy_oos_calib.py --strategy ma_break --ratio 0.5     # 1/2 窗口
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import four_dim_strategy as fd
from strategy_layer import ALL_STRATS

MIN_TEST_TRADES = 3
MIN_DELTA = 0.03

PROD_BL = fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}


def run(sym, df, strat, disabled):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    base = set(PROD_BL.get(sym, []))
    if disabled:
        base.add(strat)
    else:
        base.discard(strat)
    cfg["strat_blacklist"] = {sym: sorted(base)} if base else {}
    r = fd.walk_forward_backtest(sym, cfg=cfg, df_in=df)
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def _sign_test(w_a, w_b):
    m = w_a + w_b
    if m == 0:
        return None
    k = min(w_a, w_b)
    return min(2 * sum(comb(m, i) for i in range(k + 1)) / (2**m), 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", type=str, required=True, choices=ALL_STRATS)
    ap.add_argument("--symbols", type=str, default=None)
    ap.add_argument("--ratio", type=float, default=2.0/3.0)
    args = ap.parse_args()

    strat = args.strategy
    syms = sorted(fd.SYMBOLS)
    if args.symbols:
        want = {x.strip() for x in args.symbols.split(",") if x.strip()}
        syms = [s for s in syms if s in want]

    t0 = time.time()
    rows = []
    agg_on = [0.0, 0]
    agg_off = [0.0, 0]
    n_disable_win = n_enable_win = 0
    print(f"{strat} 品种级 OOS 校准（{len(syms)} 品种，训练 {args.ratio:.0%}）", flush=True)
    print(f"{'sym':6} {'Δtrain':>8} {'test_on':>9} {'test_off':>9} {'Δtest':>8} {'tn':>4} {'tn_off':>4} 当前   建议", flush=True)

    for sym in syms:
        df = fd.load_daily(sym)
        if df is None or len(df) < 300:
            rows.append({"symbol": sym, "note": "数据不足"})
            continue
        k = int(len(df) * args.ratio)
        train, test = df.iloc[:k], df.iloc[k:]
        cur_bl = PROD_BL.get(sym, [])
        cur_disabled = strat in cur_bl

        on_tr, on_tr_n = run(sym, train, strat, False)
        off_tr, off_tr_n = run(sym, train, strat, True)
        on_te, on_te_n = run(sym, test, strat, False)
        off_te, off_te_n = run(sym, test, strat, True)

        d_tr = off_tr - on_tr
        d_te = off_te - on_te

        agg_on[0] += on_te * on_te_n
        agg_on[1] += on_te_n
        agg_off[0] += off_te * off_te_n
        agg_off[1] += off_te_n

        if on_te_n < MIN_TEST_TRADES or off_te_n < MIN_TEST_TRADES:
            rec = "维持(样本不足)"
        elif abs(d_te) < MIN_DELTA:
            rec = "维持(噪声)"
        elif d_te > 0:
            rec = f"禁用{strat}" if not cur_disabled else "维持禁用"
            n_disable_win += 1
        else:
            rec = f"启用{strat}" if cur_disabled else "维持启用"
            n_enable_win += 1

        rows.append(
            {
                "symbol": sym, "cur_disabled": cur_disabled,
                "delta_train": round(d_tr, 4),
                "test_on": round(on_te, 4), "test_off": round(off_te, 4), "delta_test": round(d_te, 4),
                "test_trades_on": on_te_n, "test_trades_off": off_te_n,
                "recommend": rec,
            }
        )
        print(
            f"{sym:6} {d_tr:+8.4f} {on_te:+9.4f} {off_te:+9.4f} {d_te:+8.4f} "
            f"{on_te_n:4d} {off_te_n:4d} {'禁用' if cur_disabled else '启用':5} {rec}",
            flush=True,
        )

    pooled_on = agg_on[0] / agg_on[1] if agg_on[1] else 0.0
    pooled_off = agg_off[0] / agg_off[1] if agg_off[1] else 0.0
    p = _sign_test(n_disable_win, n_enable_win)

    print("=" * 100, flush=True)
    print(f"{strat} test 段 pooled：启用 = {pooled_on:+.4f}R (n={agg_on[1]}) | 禁用 = {pooled_off:+.4f}R (n={agg_off[1]})", flush=True)
    print(f"{strat} test 段 Δ(pooled) = {pooled_off - pooled_on:+.4f}R", flush=True)
    print(f"逐品种方向：禁用更优 {n_disable_win} / 启用更优 {n_enable_win}", flush=True)
    if p is not None:
        print(f"符号检验 p ≈ {p:.3f}" + ("  → 无统计证据" if p > 0.05 else "  → 方向显著"), flush=True)

    disable_candidates = [r["symbol"] for r in rows if r.get("recommend") == f"禁用{strat}"]
    enable_candidates = [r["symbol"] for r in rows if r.get("recommend") == f"启用{strat}"]
    print(f"\n建议新增禁用 {strat}：{disable_candidates}", flush=True)
    print(f"建议恢复 {strat}：{enable_candidates}", flush=True)
    print(f"耗时 {time.time() - t0:.0f}s", flush=True)

    out = os.path.join(os.path.dirname(HERE), f"{strat}_oos_calib_{int(args.ratio*100)}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "strategy": strat, "ratio": args.ratio,
                "pooled": {"on": pooled_on, "off": pooled_off, "delta": pooled_off - pooled_on, "n_on": agg_on[1], "n_off": agg_off[1]},
                "sign_test_p": p,
                "disable_win": n_disable_win, "enable_win": n_enable_win,
                "disable_candidates": disable_candidates, "enable_candidates": enable_candidates,
                "rows": rows,
            },
            f, ensure_ascii=False, indent=2,
        )
    print(f"结果写入 {out}", flush=True)


if __name__ == "__main__":
    main()
