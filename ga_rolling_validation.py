"""
多窗口滚动验证：检验新因子的稳健性
- 4 个时间窗口（tail=500/700/900/1100）
- 3 个重点组合：化工+V_vol, 农产品+SR_breakout, 黑系+Inv_stock
- 每个组合：6因子GA 和 7因子GA，然后交叉验证（训练权重在所有窗口上评估）
- 统计：OOS 平均增量、胜率
"""

import argparse
import copy
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest

try:
    from deap import algorithms, base, creator, tools

    _HAVE_DEAP = True
except ImportError:
    _HAVE_DEAP = False

MIN_TRADES_PER_SYMBOL = 5
GA_CXPB = 0.7
GA_MUTPB = 0.3

BASE_FACTORS = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]

TARGET_COMBOS = [
    {"sector": "化工", "factor": "V_vol"},
    {"sector": "农产品", "factor": "SR_breakout"},
    {"sector": "黑系", "factor": "Inv_stock"},
]

WINDOWS = [500, 700, 900, 1100]
WINDOW_LABELS = ["窗口1(最近)", "窗口2", "窗口3", "窗口4(最早)"]


def load_group_data(group, tail=0):
    """加载板块数据，返回 {sym: df}"""
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


def _normalize_weights(ind):
    vals = [max(0.0, float(v)) for v in ind]
    total = sum(vals)
    if total <= 0:
        return [1.0 / len(vals)] * len(vals)
    return [v / total for v in vals]


def _ind_to_weights(ind, factor_names):
    norm = _normalize_weights(ind)
    return {name: round(w, 4) for name, w in zip(factor_names, norm)}


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
            if nt >= MIN_TRADES_PER_SYMBOL:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass
    if not expRs:
        return -5.0, 0, 0
    avg = float(np.mean(expRs))
    n_valid = len(expRs)
    if n_valid < 3:
        avg -= 0.3 * (3 - n_valid)
    return avg, n_valid, total_trades


def run_ga(group_data, factor_names, pop_size=15, n_gen=8):
    """带任意因子集合的 GA 优化。返回 {weights, fitness, n_valid}"""
    if not _HAVE_DEAP:
        return None

    n_genes = len(factor_names)

    # 清理旧 creator
    for _name in ["FitnessRoll", "IndividualRoll"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FitnessRoll", base.Fitness, weights=(1.0,))
    creator.create("IndividualRoll", list, fitness=creator.FitnessRoll)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.IndividualRoll, toolbox.attr_float, n=n_genes)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        w = _ind_to_weights(ind, factor_names)
        expR, _, _ = evaluate_weights(w, group_data)
        return (expR,)

    toolbox.register("evaluate", _eval_ind)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(3)

    history = []
    for gen in range(n_gen):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=GA_CXPB, mutpb=GA_MUTPB)
        fits = list(map(toolbox.evaluate, offspring))
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit
        hof.update(offspring)
        pop = toolbox.select(offspring, k=len(pop))

        record = {"max": float(max(f[0] for f in fits))}
        history.append(record)

        # 早停
        if gen >= 4 and all(h["max"] >= record["max"] - 0.002 for h in history[-4:]):
            break

    best = hof[0]
    best_w = _ind_to_weights(best, factor_names)
    best_fitness, n_valid, _ = evaluate_weights(best_w, group_data)

    return {
        "weights": best_w,
        "fitness": best_fitness,
        "n_valid": n_valid,
        "generations": len(history),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pop", type=int, default=15)
    parser.add_argument("--gen", type=int, default=8)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    results = {}

    for combo in TARGET_COMBOS:
        sector = combo["sector"]
        factor = combo["factor"]
        print(f"\n{'=' * 80}")
        print(f"【{sector} + {factor}】多窗口滚动验证")
        print(f"{'=' * 80}")

        factor_names_6 = BASE_FACTORS
        factor_names_7 = BASE_FACTORS + [factor]

        # 4 个窗口分别跑 6因子 GA 和 7因子 GA
        weights_6_by_window = {}
        weights_7_by_window = {}
        train_expR_6 = []
        train_expR_7 = []

        for wi, tail in enumerate(WINDOWS):
            print(f"\n--- 窗口 {wi + 1}/4: tail={tail} ({WINDOW_LABELS[wi]}) ---")
            group_data = load_group_data(sector, tail=tail)
            if len(group_data) < 3:
                print(f"  有效品种不足（{len(group_data)}个），跳过")
                train_expR_6.append(None)
                train_expR_7.append(None)
                continue

            print(f"  有效品种: {len(group_data)} 个")

            # 6 因子 GA
            print("  6因子 GA...", end=" ", flush=True)
            r6 = run_ga(group_data, factor_names_6, pop_size=args.pop, n_gen=args.gen)
            if r6:
                weights_6_by_window[wi] = r6["weights"]
                train_expR_6.append(r6["fitness"])
                print(f"best={r6['fitness']:+.4f} ({r6['generations']}代)")
            else:
                train_expR_6.append(None)
                print("失败")

            # 7 因子 GA
            print("  7因子 GA...", end=" ", flush=True)
            r7 = run_ga(group_data, factor_names_7, pop_size=args.pop, n_gen=args.gen)
            if r7:
                weights_7_by_window[wi] = r7["weights"]
                train_expR_7.append(r7["fitness"])
                print(f"best={r7['fitness']:+.4f} ({r7['generations']}代)")
            else:
                train_expR_7.append(None)
                print("失败")

        # 交叉验证矩阵
        print("\n--- 交叉验证 ---")
        cross_6 = []  # [train_w][test_w] = expR
        cross_7 = []

        for wi in range(len(WINDOWS)):
            row6, row7 = [], []
            for wj in range(len(WINDOWS)):
                tail_test = WINDOWS[wj]
                test_data = load_group_data(sector, tail=tail_test)

                # 6 因子
                if wi in weights_6_by_window:
                    e6, _, _ = evaluate_weights(weights_6_by_window[wi], test_data)
                    row6.append(e6)
                else:
                    row6.append(None)

                # 7 因子
                if wi in weights_7_by_window:
                    e7, _, _ = evaluate_weights(weights_7_by_window[wi], test_data)
                    row7.append(e7)
                else:
                    row7.append(None)

            cross_6.append(row6)
            cross_7.append(row7)

        # 打印矩阵
        header = "训练\\测试  " + "  ".join([f"W{i + 1:>5}" for i in range(len(WINDOWS))])
        print("\n6因子 expR 矩阵：")
        print(header)
        for wi, row in enumerate(cross_6):
            vals = "  ".join([f"{v:>+6.3f}" if v is not None else "  N/A " for v in row])
            print(f"  W{wi + 1}      {vals}")

        print(f"\n7因子(+{factor}) expR 矩阵：")
        print(header)
        for wi, row in enumerate(cross_7):
            vals = "  ".join([f"{v:>+6.3f}" if v is not None else "  N/A " for v in row])
            print(f"  W{wi + 1}      {vals}")

        # OOS 增量：训练 W_i，测试 W_j (j!=i) 时的 7因子 - 6因子
        oos_deltas = []
        for wi in range(len(WINDOWS)):
            for wj in range(len(WINDOWS)):
                if wi == wj:
                    continue
                if cross_6[wi][wj] is not None and cross_7[wi][wj] is not None:
                    d = cross_7[wi][wj] - cross_6[wi][wj]
                    oos_deltas.append(d)

        avg_delta = float(np.mean(oos_deltas)) if oos_deltas else 0.0
        win_rate = float(np.mean([1 if d > 0 else 0 for d in oos_deltas])) if oos_deltas else 0.0
        pos_count = sum(1 for d in oos_deltas if d > 0)
        total_count = len(oos_deltas)

        print(f"\nOOS 平均增量: {avg_delta:+.4f}")
        print(f"OOS 胜率: {win_rate:.0%} ({pos_count}/{total_count})")

        if win_rate >= 0.7 and avg_delta > 0:
            print("  ✅ 稳健正向")
        elif win_rate >= 0.5 and avg_delta > 0:
            print("  ⚠️ 微弱正向，有争议")
        else:
            print("  ❌ 不稳健 / 负向")

        results[sector] = {
            "factor": factor,
            "train_expR_6": train_expR_6,
            "train_expR_7": train_expR_7,
            "cross_matrix_6": cross_6,
            "cross_matrix_7": cross_7,
            "oos_deltas": oos_deltas,
            "avg_oos_delta": avg_delta,
            "oos_win_rate": win_rate,
            "oos_pos_count": pos_count,
            "oos_total_count": total_count,
        }

    # 汇总
    print(f"\n\n{'=' * 80}")
    print("汇总：三组合多窗口滚动验证")
    print(f"{'=' * 80}")
    print(f"{'板块':<8}{'因子':<14}{'OOS增量':>10}{'胜率':>8}{'结论':>14}")
    for sector, r in results.items():
        delta = r["avg_oos_delta"]
        wr = r["oos_win_rate"]
        if wr >= 0.7 and delta > 0:
            v = "✅ 稳健正向"
        elif wr >= 0.5 and delta > 0:
            v = "⚠️ 微弱正向"
        else:
            v = "❌ 不稳健"
        print(f"{sector:<8}{r['factor']:<14}{delta:>+10.4f}{wr:>8.0%}{v:>14}")

    if args.save:
        out = {"windows": WINDOWS, "window_labels": WINDOW_LABELS, "results": results}
        out_path = "logs/ga_rolling_validation.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=float)
        print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
