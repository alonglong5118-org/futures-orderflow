"""
固定权重验证：不跑 GA，直接试几套简单权重，看多窗口下是否稳健
- 6 套基础权重方案（6因子）
- 3 套加新因子的方案（7因子）
- 4 个时间窗口
- 5 个板块
- 统计：各方案在各板块的 OOS 表现、稳定性
"""

import argparse
import copy
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest

MIN_TRADES = 5

# 板块列表
SECTORS = ["化工", "农产品", "有色", "黑系", "能源"]

# 窗口
WINDOWS = [500, 700, 900, 1100]
WINDOW_LABELS = ["W1(最近)", "W2", "W3", "W4(最早)"]

# 固定权重方案（6因子）
BASE_SCHEMES = {
    "默认权重": {"T_trend": 0.20, "T_mean": 0.15, "T_seasonal": 0.05, "F_basis": 0.20, "F_seasonal": 0.10, "C": 0.30},
    "等权": {"T_trend": 1 / 6, "T_mean": 1 / 6, "T_seasonal": 1 / 6, "F_basis": 1 / 6, "F_seasonal": 1 / 6, "C": 1 / 6},
    "T主导 (趋势型)": {
        "T_trend": 0.35,
        "T_mean": 0.25,
        "T_seasonal": 0.05,
        "F_basis": 0.10,
        "F_seasonal": 0.05,
        "C": 0.20,
    },
    "F主导 (基本面型)": {
        "T_trend": 0.10,
        "T_mean": 0.10,
        "T_seasonal": 0.05,
        "F_basis": 0.35,
        "F_seasonal": 0.20,
        "C": 0.20,
    },
    "C主导 (情绪型)": {
        "T_trend": 0.10,
        "T_mean": 0.10,
        "T_seasonal": 0.05,
        "F_basis": 0.10,
        "F_seasonal": 0.05,
        "C": 0.60,
    },
    "保守分散型": {"T_trend": 0.18, "T_mean": 0.17, "T_seasonal": 0.08, "F_basis": 0.18, "F_seasonal": 0.14, "C": 0.25},
}

# 加新因子的方案（7因子，新因子给一个固定权重，从原因子里等比例扣）
# 全板块 × 5新因子 × 2权重（10%/20%）
NEW_FACTOR_SCHEMES = {}
for _sector in SECTORS:
    for _factor in ["V_vol", "Vol_vol", "SR_breakout", "OI_int", "Inv_stock"]:
        for _fw in [0.10, 0.20]:
            _name = f"{_sector}+{_factor}({int(_fw * 100)}%)"
            NEW_FACTOR_SCHEMES[_name] = {
                "sector": _sector,
                "factor": _factor,
                "factor_weight": _fw,
                "base_scheme": "默认权重",
            }


def load_group_data(group, tail=0):
    syms = []
    for sym, info in SYMBOLS.items():
        if info.get("group") == group:
            syms.append(sym)
    data = {}
    for sym in syms:
        try:
            df = load_daily(sym)
            if df is None or len(df) < 200:
                continue
            if tail and len(df) > tail:
                df = df.tail(tail)
            data[sym] = df
        except Exception:
            continue
    return data


def evaluate_weights(weights, group_data):
    """评估一组权重，返回 (avg_expR, n_valid, total_trades)"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = weights

    expRs = []
    total_trades = 0
    for sym, df in group_data.items():
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=300, min_bars=60, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass

    if not expRs:
        return -5.0, 0, 0
    return float(np.mean(expRs)), len(expRs), total_trades


def add_factor_to_weights(base_weights, factor_name, factor_weight):
    """把新因子加入权重，从原因子等比例扣除"""
    new_w = {}
    for k, v in base_weights.items():
        new_w[k] = v * (1 - factor_weight)
    new_w[factor_name] = factor_weight
    return new_w


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    results = {}

    # === 第一部分：6 因子基础方案 × 5 板块 × 4 窗口 ===
    print("=" * 80)
    print("第一部分：6 因子基础方案对比")
    print("=" * 80)

    base_results = {}
    for sector in SECTORS:
        print(f"\n【{sector}】")
        sector_data = {}
        for scheme_name, weights in BASE_SCHEMES.items():
            expR_by_window = []
            for wi, tail in enumerate(WINDOWS):
                gd = load_group_data(sector, tail=tail)
                if len(gd) < 3:
                    expR_by_window.append(None)
                    continue
                expR, n_valid, _ = evaluate_weights(weights, gd)
                expR_by_window.append(expR)

            valid = [v for v in expR_by_window if v is not None]
            avg = float(np.mean(valid)) if valid else 0.0
            std = float(np.std(valid)) if len(valid) > 1 else 0.0
            win_rate = float(np.mean([1 if v > 0 else 0 for v in valid])) if valid else 0.0

            sector_data[scheme_name] = {
                "expR_by_window": expR_by_window,
                "avg_expR": avg,
                "std_expR": std,
                "win_rate": win_rate,
                "n_valid_windows": len(valid),
            }

            # 打印
            vals = "  ".join([f"{v:>+6.3f}" if v is not None else "  N/A " for v in expR_by_window])
            print(f"  {scheme_name:<18} {vals}  | avg={avg:+.4f} σ={std:.4f}")

        base_results[sector] = sector_data

    # 打印横向对比（每个板块找最优方案）
    print("\n\n各板块最优基础方案：")
    print(f"{'板块':<8}{'最优方案':<20}{'平均expR':>12}{'稳定性σ':>10}{'胜率':>8}")
    for sector in SECTORS:
        best_scheme = max(BASE_SCHEMES.keys(), key=lambda s: base_results[sector][s]["avg_expR"])
        r = base_results[sector][best_scheme]
        print(f"{sector:<8}{best_scheme:<20}{r['avg_expR']:>+12.4f}{r['std_expR']:>10.4f}{r['win_rate']:>8.0%}")

    results["base_schemes"] = base_results

    # === 第二部分：加新因子的方案 ===
    print(f"\n\n{'=' * 80}")
    print("第二部分：新因子固定权重验证")
    print("=" * 80)

    new_factor_results = {}
    for scheme_name, cfg in NEW_FACTOR_SCHEMES.items():
        sector = cfg["sector"]
        factor = cfg["factor"]
        fw = cfg["factor_weight"]
        base_name = cfg["base_scheme"]
        base_weights = BASE_SCHEMES[base_name]

        print(f"\n【{scheme_name}】")
        new_weights = add_factor_to_weights(base_weights, factor, fw)

        expR_base = []  # 基准（6因子）
        expR_new = []  # 加新因子后
        deltas = []  # 增量

        for wi, tail in enumerate(WINDOWS):
            gd = load_group_data(sector, tail=tail)
            if len(gd) < 3:
                expR_base.append(None)
                expR_new.append(None)
                deltas.append(None)
                continue

            e_base, _, _ = evaluate_weights(base_weights, gd)
            e_new, _, _ = evaluate_weights(new_weights, gd)
            delta = e_new - e_base

            expR_base.append(e_base)
            expR_new.append(e_new)
            deltas.append(delta)

        valid_deltas = [d for d in deltas if d is not None]
        avg_delta = float(np.mean(valid_deltas)) if valid_deltas else 0.0
        std_delta = float(np.std(valid_deltas)) if len(valid_deltas) > 1 else 0.0
        win_rate = float(np.mean([1 if d > 0 else 0 for d in valid_deltas])) if valid_deltas else 0.0

        vals = "  ".join([f"{d:>+6.3f}" if d is not None else "  N/A " for d in deltas])
        print(f"  增量(delta): {vals}")
        print(
            f"  平均增量: {avg_delta:+.4f}  σ={std_delta:.4f}  胜率: {win_rate:.0%} ({sum(1 for d in valid_deltas if d > 0)}/{len(valid_deltas)})"
        )

        if win_rate >= 0.75 and avg_delta > 0:
            print("  ✅ 稳健正向")
        elif win_rate >= 0.5 and avg_delta > 0:
            print("  ⚠️ 微弱正向")
        else:
            print("  ❌ 不正向 / 不稳定")

        new_factor_results[scheme_name] = {
            "sector": sector,
            "factor": factor,
            "factor_weight": fw,
            "expR_base": expR_base,
            "expR_new": expR_new,
            "deltas": deltas,
            "avg_delta": avg_delta,
            "std_delta": std_delta,
            "win_rate": win_rate,
        }

    results["new_factor_schemes"] = new_factor_results

    # === 汇总 ===
    print(f"\n\n{'=' * 80}")
    print("新因子方案汇总")
    print(f"{'=' * 80}")
    print(f"{'方案':<22}{'平均增量':>10}{'σ':>8}{'胜率':>8}{'结论':>14}")
    for name, r in new_factor_results.items():
        d = r["avg_delta"]
        wr = r["win_rate"]
        if wr >= 0.75 and d > 0:
            v = "✅ 稳健正向"
        elif wr >= 0.5 and d > 0:
            v = "⚠️ 微弱正向"
        else:
            v = "❌ 不稳健"
        print(f"{name:<22}{d:>+10.4f}{r['std_delta']:>8.4f}{wr:>8.0%}{v:>14}")

    if args.save:
        out = {"windows": WINDOWS, "window_labels": WINDOW_LABELS, "base_schemes": BASE_SCHEMES, "results": results}
        out_path = "logs/ga_fixed_weights.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=float)
        print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
