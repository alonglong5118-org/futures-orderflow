"""A/B：回测手续费统一到 live 新 schema（按铁律：任何策略数值改动前必跑全品种 A/B）。

OLD：当前 DEFAULT_CONFIG 旧固定 fee（元/手/单边，引擎按 2*fee 算往返）。
NEW：trade_config.json 新 schema 折算成固定 元/手/单边：
     - fixed  : fee = fee_exchange + broker_fee
     - ratio  : fee = (fee_exchange + broker_fee)/10000 * 代表价 * multiplier
     代表价取该品种日线 median close（ratio 随价浮动，用代表价近似均值水平）。
费用只缩放每笔 R_adj，不改成交集合 → OLD/NEW 逐笔一一对应，delta 可配对。
输出：全品种 pooled expR / total_R、Δ、逐品种 Δ 表、配对 bootstrap CI 与 P(Δ<0)。
"""

import copy
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import four_dim_strategy as S

live = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
live_cs = live.get("contract_specs", {})


def new_fee_onside(symbol, rep_price, multiplier):
    sp = live_cs.get(symbol)
    if not sp:
        return None
    ft = sp.get("fee_type")
    fe = sp.get("fee_exchange", 0) or 0
    bf = sp.get("broker_fee", 0) or 0
    if ft == "ratio":
        return (fe + bf) / 10000.0 * rep_price * multiplier
    return fe + bf  # fixed: 元/手/单边


def build_cfg_new():
    OLD = S.DEFAULT_CONFIG
    cfg_new = copy.deepcopy(OLD)
    changes = {}
    for sym in S.SYMBOLS:
        if sym in S.DISABLED_SYMBOLS:
            continue
        df = S.load_daily(sym)
        if df is None or len(df) < 80:
            continue
        rep = float(np.median(df["close"].values))
        old_entry = OLD["contract_specs"].get(sym, S._FALLBACK_SPEC)
        mult = old_entry["multiplier"]
        nf = new_fee_onside(sym, rep, mult)
        if nf is None:
            continue
        old_fee = old_entry["fee"]
        if abs(nf - old_fee) < 1e-9:
            continue
        entry = dict(old_entry)
        entry["fee"] = nf
        cfg_new["contract_specs"][sym] = entry
        changes[sym] = {
            "old_fee": round(old_fee, 4),
            "new_fee": round(nf, 4),
            "rep_price": round(rep, 2),
            "mult": mult,
            "src": live_cs.get(sym, {}).get("broker_fee_source", "?"),
        }
    return cfg_new, changes


def run_all(cfg):
    per_sym = {}
    for sym in S.SYMBOLS:
        if sym in S.DISABLED_SYMBOLS:
            continue
        try:
            r = S.walk_forward_backtest(sym, cfg=cfg)
        except Exception as e:
            per_sym[sym] = {"err": repr(e)[:120]}
            continue
        td = r.get("trades_detail") or []
        rs = [float(t["R_adj"]) for t in td]
        per_sym[sym] = {
            "trades": len(rs),
            "expR": float(np.mean(rs)) if rs else 0.0,
            "total_R": float(np.sum(rs)),
            "rs": rs,
        }
    return per_sym


def pooled(per_sym):
    all_rs = [r for v in per_sym.values() if "rs" in v for r in v["rs"]]
    n = len(all_rs)
    tot = float(np.sum(all_rs))
    return n, tot, (tot / n if n else 0.0)


def paired_bootstrap(per_sym_old, per_sym_new, n_boot=4000, seed=20260909):
    rng = np.random.default_rng(seed)
    syms = [s for s in per_sym_old if "rs" in per_sym_old[s] and "rs" in per_sym_new[s]]
    deltas = []
    for s in syms:
        o = per_sym_old[s]["rs"]
        nw = per_sym_new[s]["rs"]
        m = min(len(o), len(nw))
        deltas.append(np.array(nw[:m]) - np.array(o[:m]))
    total_delta = float(sum(d.sum() for d in deltas))
    B = np.empty(n_boot)
    k = len(deltas)
    for i in range(n_boot):
        idx = rng.integers(0, k, k)
        B[i] = sum(deltas[j].sum() for j in idx)
    ci = np.percentile(B, [2.5, 97.5])
    p_neg = float((B < 0).mean())
    return total_delta, ci, p_neg, syms


def main():
    t0 = time.time()
    cfg_new, changes = build_cfg_new()
    print(f"[build] {len(changes)} 个品种费率有差异:")
    for s, c in sorted(changes.items(), key=lambda kv: abs(kv[1]["new_fee"] - kv[1]["old_fee"]), reverse=True):
        print(
            f"   {s:4s} 旧 {c['old_fee']:>8.3f} → 新 {c['new_fee']:>8.3f}  "
            f"(代表价 {c['rep_price']:>8.1f} ×{c['mult']}, src={c['src']})"
        )

    print("\n[run] OLD (当前 DEFAULT fee) …")
    po = run_all(S.DEFAULT_CONFIG)
    print(f"   OLD 完成 {time.time() - t0:.1f}s")

    print("[run] NEW (live schema 折算) …")
    pn = run_all(cfg_new)
    print(f"   NEW 完成 {time.time() - t0:.1f}s")

    n_o, tot_o, exp_o = pooled(po)
    n_n, tot_n, exp_n = pooled(pn)
    print("\n========== 全品种汇总 ==========")
    print(f"OLD : n={n_o}  expR={exp_o:.4f}  total_R={tot_o:.2f}")
    print(f"NEW : n={n_n}  expR={exp_n:.4f}  total_R={tot_n:.2f}")
    print(f"Δ   : n={n_n - n_o}  ΔexpR={exp_n - exp_o:+.4f}  Δtotal_R={tot_n - tot_o:+.2f}")

    td, ci, p_neg, syms = paired_bootstrap(po, pn)
    print(f"\n配对 bootstrap (n={len(syms)} 品种, 4000 次):")
    print(f"  total_R Δ = {td:+.2f}  R  [95% CI {ci[0]:+.2f}, {ci[1]:+.2f}]  P(Δ<0)={p_neg:.3f}")

    # 逐品种 Δ 表（按 |Δtotal_R| 降序）
    rows = []
    for s in syms:
        o = po[s]
        nw = pn[s]
        rows.append((s, o["trades"], nw["total_R"] - o["total_R"], nw["expR"] - o["expR"]))
    rows.sort(key=lambda r: abs(r[2]), reverse=True)
    print("\n逐品种 Δtotal_R (top 20):")
    print(f"  {'sym':4s} {'n':>5s} {'Δtotal_R':>10s} {'ΔexpR':>9s}")
    for s, n, dtr, de in rows[:20]:
        print(f"  {s:4s} {n:5d} {dtr:+10.3f} {de:+9.4f}")

    print(f"\n[done] {time.time() - t0:.1f}s")
    # 落盘供复核
    out = {
        "changes": changes,
        "OLD": {"n": n_o, "expR": exp_o, "total_R": tot_o},
        "NEW": {"n": n_n, "expR": exp_n, "total_R": tot_n},
        "delta_total_R": td,
        "ci95": [float(x) for x in ci],
        "P_delta_neg": p_neg,
        "per_symbol": {
            s: {
                "n": po[s]["trades"],
                "d_total_R": pn[s]["total_R"] - po[s]["total_R"],
                "d_expR": pn[s]["expR"] - po[s]["expR"],
            }
            for s in syms
        },
    }
    with open(os.path.join(HERE, "ab_fee_unify_result.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("结果已落盘 ab_fee_unify_result.json")


if __name__ == "__main__":
    main()
