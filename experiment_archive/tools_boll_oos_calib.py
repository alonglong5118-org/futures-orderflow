#!/usr/bin/env python3
"""boll 策略品种级 OOS 校准（2026-09-16）

背景：rsi 已验证「品种级 OOS 校准」方法论；boll 同理。
      当前 blacklist 里 boll 禁用 4 品种（y/sp/zn/PK），均来自全样本贪心搜索 v2/未验证，
      未做近期 OOS 复核。本脚本专门对 boll 做品种级 OOS 校准。

方法（干净 OOS，复用 tools_rsi_oos_calib.py 方法论）：
  - 前 2/3 训练 → 后 1/3 测试（OOS）
  - 每品种保持「除 boll 外」其他策略 blacklist 现状不变，仅切换 boll 启用/禁用
  - delta = expR(boll禁用) - expR(boll启用)；正值 = 禁用更优
  - 判定以 test 段（OOS）为准；train 段仅作一致性参考
  - 跨品种 pooled 符号检验（单品种成交少，禁止逐品种下结论）

用法：
  env -u PYTHONHOME -u PYTHONPATH $PY tools_boll_oos_calib.py                # 全市场
  env -u PYTHONHOME -u PYTHONPATH $PY tools_boll_oos_calib.py --symbols y,sp,zn,PK
  env -u PYTHONHOME -u PYTHONPATH $PY tools_boll_oos_calib.py --ratio 0.5    # 后1/2交叉验证
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
sys.path.insert(0, os.path.dirname(HERE))  # 项目根（four_dim_strategy 所在）

import four_dim_strategy as fd
from strategy_layer import ALL_STRATS

TRAIN_RATIO = 2.0 / 3.0
MIN_TEST_TRADES = 3
MIN_DELTA = 0.03

PROD_BL = fd.DEFAULT_CONFIG.get("strat_blacklist", {}) or {}


def run(sym, df, boll_disabled):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    base = set(PROD_BL.get(sym, []))
    if boll_disabled:
        base.add("boll")
    else:
        base.discard("boll")
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
    global TRAIN_RATIO
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default=None, help="逗号分隔品种，默认全市场")
    ap.add_argument("--ratio", type=float, default=2.0/3.0, help="训练占比，默认2/3；0.5=后1/2交叉验证")
    args = ap.parse_args()

    syms = sorted(fd.SYMBOLS)
    if args.symbols:
        want = {x.strip() for x in args.symbols.split(",") if x.strip()}
        syms = [s for s in syms if s in want]

    TRAIN_RATIO = args.ratio
    t0 = time.time()
    rows = []
    agg_on = [0.0, 0]
    agg_off = [0.0, 0]
    n_disable_win = n_enable_win = 0
    print(f"boll 品种级 OOS 校准（{len(syms)} 品种，训练 {TRAIN_RATIO:.0%} → 测试 {1-TRAIN_RATIO:.0%}）", flush=True)
    print(f"{'sym':6} {'train_on':>9} {'train_off':>9} {'Δtrain':>8} {'test_on':>9} {'test_off':>9} {'Δtest':>8} {'tn':>4} {'tn_off':>4} 当前   建议", flush=True)

    for sym in syms:
        df = fd.load_daily(sym)
        if df is None or len(df) < 300:
            rows.append({"symbol": sym, "note": "数据不足"})
            print(f"{sym:6} 数据不足，跳过", flush=True)
            continue
        k = int(len(df) * TRAIN_RATIO)
        train, test = df.iloc[:k], df.iloc[k:]
        cur_bl = PROD_BL.get(sym, [])
        cur_disabled = "boll" in cur_bl

        on_tr, on_tr_n = run(sym, train, False)
        off_tr, off_tr_n = run(sym, train, True)
        on_te, on_te_n = run(sym, test, False)
        off_te, off_te_n = run(sym, test, True)

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
            rec = "禁用boll" if not cur_disabled else "维持禁用"
            n_disable_win += 1
        else:
            rec = "启用boll" if cur_disabled else "维持启用"
            n_enable_win += 1

        rows.append(
            {
                "symbol": sym,
                "cur_disabled": cur_disabled,
                "train_on": round(on_tr, 4), "train_off": round(off_tr, 4), "delta_train": round(d_tr, 4),
                "test_on": round(on_te, 4), "test_off": round(off_te, 4), "delta_test": round(d_te, 4),
                "test_trades_on": on_te_n, "test_trades_off": off_te_n,
                "recommend": rec,
            }
        )
        print(
            f"{sym:6} {on_tr:+9.4f} {off_tr:+9.4f} {d_tr:+8.4f} {on_te:+9.4f} {off_te:+9.4f} {d_te:+8.4f} "
            f"{on_te_n:4d} {off_te_n:4d} {'禁用' if cur_disabled else '启用':5} {rec}",
            flush=True,
        )

    pooled_on = agg_on[0] / agg_on[1] if agg_on[1] else 0.0
    pooled_off = agg_off[0] / agg_off[1] if agg_off[1] else 0.0
    p = _sign_test(n_disable_win, n_enable_win)

    print("=" * 100, flush=True)
    print(f"test 段 pooled：boll 启用 = {pooled_on:+.4f}R (n={agg_on[1]}) | boll 禁用 = {pooled_off:+.4f}R (n={agg_off[1]})", flush=True)
    print(f"test 段 Δ(pooled) = {pooled_off - pooled_on:+.4f}R", flush=True)
    print(f"逐品种方向：禁用更优 {n_disable_win} / 启用更优 {n_enable_win}", flush=True)
    if p is not None:
        print(f"符号检验 p ≈ {p:.3f}" + ("  → 无统计证据，不建议据此改动生产配置" if p > 0.05 else "  → 方向显著"), flush=True)

    disable_candidates = [r["symbol"] for r in rows if r.get("recommend") == "禁用boll"]
    enable_candidates = [r["symbol"] for r in rows if r.get("recommend") == "启用boll"]
    print(f"\n建议新增禁用 boll：{disable_candidates}", flush=True)
    print(f"建议恢复 boll：{enable_candidates}", flush=True)
    print(f"耗时 {time.time() - t0:.0f}s", flush=True)

    out = os.path.join(os.path.dirname(HERE), "boll_oos_calib_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "pooled": {"on": pooled_on, "off": pooled_off, "delta": pooled_off - pooled_on, "n_on": agg_on[1], "n_off": agg_off[1]},
                "sign_test_p": p,
                "disable_win": n_disable_win,
                "enable_win": n_enable_win,
                "disable_candidates": disable_candidates,
                "enable_candidates": enable_candidates,
                "rows": rows,
            },
            f, ensure_ascii=False, indent=2,
        )
    print(f"结果写入 {out}", flush=True)


if __name__ == "__main__":
    main()
