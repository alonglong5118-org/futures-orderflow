"""
P0 参数扫描：F 维度权重 + fc_confirm + fc_hard 优化
核心假设：当前 fc_confirm=25 太高，bias_FC 几乎从未达阈值
测试不同参数组合对 expR / 胜率 / 交易数 / 最大回撤的影响
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


def run_bt(sym, df, f_weight, fc_confirm, fc_hard):
    """跑一组参数的回测。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    # F 权重（T 权重相应调整，C 不变）
    c_weight = cfg["combine_weights"]["C"]
    t_weight = 1.0 - f_weight - c_weight
    if t_weight < 0.1:
        t_weight = 0.1
        f_weight = 1.0 - t_weight - c_weight
    cfg["combine_weights"] = {"T": round(t_weight, 4), "F": round(f_weight, 4), "C": round(c_weight, 4)}

    # fc_confirm / fc_hard
    cfg["bias_synthesis"]["fc_confirm"] = fc_confirm
    cfg["bias_synthesis"]["fc_hard"] = fc_hard

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

    return {
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades": len(trades),
        "total_R": round(total_R, 2),
        "max_dd": round(max_dd, 3),
        "pf": round(pf, 2),
    }


def param_sweep(symbol, df):
    """扫描 F 权重 + fc_confirm + fc_hard 组合。"""
    f_weights = [0.15, 0.25, 0.35, 0.45, 0.55, 0.65]
    fc_confirms = [5, 10, 15, 20, 25, 30]
    fc_hards = [15, 25, 35, 45]

    baseline = run_bt(symbol, df, 0.25, 25, 25)

    results = []
    for fw in f_weights:
        for fcc in fc_confirms:
            for fch in fc_hards:
                r = run_bt(symbol, df, fw, fcc, fch)
                # 过滤：交易数不能太少
                if r["trades"] < max(10, baseline["trades"] * 0.3):
                    continue
                delta_expr = r["expR"] - baseline["expR"]
                results.append(
                    {
                        "f_weight": fw,
                        "fc_confirm": fcc,
                        "fc_hard": fch,
                        **r,
                        "delta_expr": round(delta_expr, 4),
                        "score": round(delta_expr * 100 - abs(r["max_dd"] - baseline["max_dd"]) * 0.1, 3),
                    }
                )

    # 按综合得分排序
    results.sort(key=lambda x: x["score"], reverse=True)
    return baseline, results


def main():
    # 选 5 个代表品种：2 强 + 1 中 + 2 弱
    test_syms = ["sn", "cu", "rb", "FG", "au"]

    print("=" * 80)
    print("P0 参数扫描：F 权重 + fc_confirm + fc_hard")
    print("=" * 80)

    all_results = {}

    for sym in test_syms:
        df = load_daily(sym)
        if df is None:
            continue

        print(f"\n{'─' * 60}")
        print(f"  {sym}")
        print(f"{'─' * 60}")

        baseline, results = param_sweep(sym, df)
        all_results[sym] = {"baseline": baseline, "top": results[:20]}

        print(
            f"  基准: expR={baseline['expR']:.3f}  胜率={baseline['win_rate'] * 100:.1f}%  "
            f"笔数={baseline['trades']}  R={baseline['total_R']:.2f}  DD={baseline['max_dd']:.3f}  PF={baseline['pf']:.2f}"
        )
        print()
        print(
            f"  {'F_w':>5}  {'fcc':>4}  {'fch':>4}  {'expR':>7}  {'ΔexpR':>7}  {'胜率':>6}  "
            f"{'笔':>4}  {'总R':>7}  {'DD':>6}  {'PF':>5}  {'score':>7}"
        )

        for r in results[:10]:
            marker = " ★" if r["delta_expr"] > 0 else ""
            print(
                f"  {r['f_weight']:>5.2f}  {r['fc_confirm']:>4}  {r['fc_hard']:>4}  "
                f"{r['expR']:>7.3f}  {r['delta_expr']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['trades']:>4}  {r['total_R']:>7.2f}  {r['max_dd']:>6.3f}  {r['pf']:>5.2f}  "
                f"{r['score']:>7.2f}{marker}"
            )

    # 汇总：每个品种最佳参数
    print(f"\n{'=' * 80}")
    print("  各品种最佳参数（Top 1 by score）")
    print(f"{'=' * 80}")
    print(
        f"  {'品种':>5}  {'F_w':>5}  {'fcc':>4}  {'fch':>4}  {'expR_base':>9}  {'expR_opt':>9}  {'Δ':>7}  {'笔_base':>6}  {'笔_opt':>6}"
    )
    for sym, data in all_results.items():
        b = data["baseline"]
        t = data["top"][0] if data["top"] else b
        print(
            f"  {sym:>5}  {t['f_weight']:>5.2f}  {t['fc_confirm']:>4}  {t['fc_hard']:>4}  "
            f"{b['expR']:>9.3f}  {t['expR']:>9.3f}  {t['delta_expr']:>+7.3f}  "
            f"{b['trades']:>6}  {t['trades']:>6}"
        )

    # 保存
    os.makedirs("logs", exist_ok=True)
    with open("logs/fc_param_sweep.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/fc_param_sweep.json")


if __name__ == "__main__":
    main()
