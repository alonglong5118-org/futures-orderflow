"""
P1 退出机制参数扫描：stop_atr_mult + rr_ratio + tail_trail_R
测试不同退出参数组合对 expR / 胜率 / PF / 最大回撤的影响
"""

import copy
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)


def run_bt(sym, df, stop_mult=None, rr=None, tail_trail=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    # 逐品种止损/止盈
    if stop_mult is not None or rr is not None:
        psr = cfg.setdefault("per_symbol_risk", {})
        sym_cfg = psr.get(sym, {})
        if stop_mult is not None:
            sym_cfg["stop_atr_mult"] = stop_mult
        if rr is not None:
            sym_cfg["rr_ratio"] = rr
        psr[sym] = sym_cfg

    # 尾仓参数
    if tail_trail is not None:
        cfg["trailing_tail"]["tail_trail_R"] = tail_trail

    r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=200)
    trades = r.get("trades_detail", [])

    if not trades:
        return {"expR": 0, "win_rate": 0, "trades": 0, "total_R": 0, "max_dd": 0, "pf": 0}

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r < 0]

    cum = np.cumsum(Rs)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.max(peak - cum))
    total_R = float(np.sum(Rs))
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 99.9

    # 退出原因
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    return {
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades": len(trades),
        "total_R": round(total_R, 2),
        "max_dd": round(max_dd, 3),
        "pf": round(pf, 2),
        "reasons": reasons,
    }


def sweep_stop_rr(symbol, df):
    """扫描 stop_atr_mult 和 rr_ratio 组合。"""
    stop_mults = [1.0, 1.5, 2.0, 2.5, 3.0]
    rr_ratios = [1.5, 2.0, 2.5, 3.0, 4.0]

    # 当前值（基准）
    psr = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(symbol, {})
    base_stop = psr.get("stop_atr_mult", 1.5)
    base_rr = psr.get("rr_ratio", 2.0)
    baseline = run_bt(symbol, df)

    results = []
    for sm in stop_mults:
        for rr in rr_ratios:
            r = run_bt(symbol, df, stop_mult=sm, rr=rr)
            if r["trades"] < max(10, baseline["trades"] * 0.3):
                continue
            delta_expr = r["expR"] - baseline["expR"]
            # 综合得分：expR 提升 - 回撤惩罚
            dd_penalty = max(0, r["max_dd"] - baseline["max_dd"]) * 0.5
            score = delta_expr * 100 - dd_penalty
            results.append(
                {
                    "stop": sm,
                    "rr": rr,
                    **r,
                    "delta_expr": round(delta_expr, 4),
                    "score": round(score, 2),
                }
            )

    results.sort(key=lambda x: x["score"], reverse=True)
    return baseline, base_stop, base_rr, results


def main():
    # 选几个代表品种：强 + 中 + 弱
    test_syms = ["ru", "rb", "au", "CF", "ss", "FG"]

    print("=" * 80)
    print("P1 退出机制参数扫描：stop_atr_mult + rr_ratio")
    print("=" * 80)

    all_results = {}

    for sym in test_syms:
        df = load_daily(sym)
        if df is None:
            continue

        print(f"\n{'─' * 70}")
        print(f"  {sym}")
        print(f"{'─' * 70}")

        baseline, base_stop, base_rr, results = sweep_stop_rr(sym, df)
        all_results[sym] = {
            "baseline": baseline,
            "base_stop": base_stop,
            "base_rr": base_rr,
            "top": results[:10],
        }

        print(
            f"  基准: stop={base_stop}R  rr={base_rr}R  "
            f"expR={baseline['expR']:.3f}  胜率={baseline['win_rate'] * 100:.1f}%  "
            f"笔数={baseline['trades']}  DD={baseline['max_dd']:.3f}  PF={baseline['pf']:.2f}"
        )
        print()
        print(
            f"  {'stop':>5}  {'rr':>5}  {'expR':>7}  {'Δ':>7}  {'胜率':>6}  "
            f"{'笔':>4}  {'DD':>6}  {'PF':>5}  {'score':>7}"
        )

        for r in results[:8]:
            marker = " ★" if r["delta_expr"] > 0 else ""
            print(
                f"  {r['stop']:>5.1f}  {r['rr']:>5.1f}  "
                f"{r['expR']:>7.3f}  {r['delta_expr']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['trades']:>4}  {r['max_dd']:>6.3f}  {r['pf']:>5.2f}  "
                f"{r['score']:>7.2f}{marker}"
            )

    # 汇总
    print(f"\n{'=' * 80}")
    print("  各品种最佳参数（Top 1 by score）")
    print(f"{'=' * 80}")
    print(
        f"  {'品种':>5}  {'基准stop':>8}  {'最优stop':>8}  {'基准rr':>7}  {'最优rr':>7}  "
        f"{'基准expR':>9}  {'最优expR':>9}  {'Δ':>7}"
    )
    for sym, data in all_results.items():
        b = data["baseline"]
        t = data["top"][0] if data["top"] else b
        print(
            f"  {sym:>5}  {data['base_stop']:>8.1f}  {t['stop']:>8.1f}  {data['base_rr']:>7.1f}  {t['rr']:>7.1f}  "
            f"{b['expR']:>9.3f}  {t['expR']:>9.3f}  {t['delta_expr']:>+7.3f}"
        )

    # 保存
    os.makedirs("logs", exist_ok=True)
    with open("logs/exit_param_sweep.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/exit_param_sweep.json")


if __name__ == "__main__":
    main()
