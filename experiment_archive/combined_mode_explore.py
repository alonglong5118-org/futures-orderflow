"""
Combined 模式探索：
1. threshold vs combined baseline 对比（各板块）
2. combined 模式下 T/F/C 权重 GA 优化 + OOS 验证
3. 回答：combined 模式下权重优化空间是否更大？
"""

import copy
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest

try:
    from deap import base, creator, tools

    _HAVE_DEAP = True
except Exception:
    _HAVE_DEAP = False

# 板块定义
SECTORS = {
    "化工": ["PF", "PX", "SA", "SH", "TA", "eb", "l", "pp", "ru", "sp", "v", "ma", "fg", "UR", "nr", "bu"],
    "农产品": ["AP", "CF", "CJ", "CS", "SR", "a", "c", "i", "jd", "lh", "m", "p", "y", "rm", "OI", "PK"],
    "有色": ["al", "ao", "cu", "ni", "pb", "ss", "zn", "si", "bc", "br"],
    "黑系": ["i", "j", "jm", "rb", "hc", "sf", "sm", "ss", "fg"],
    "能源": ["sc", "lu", "bu", "fu", "pg", "eb", "l", "pp", "v", "ru", "nr"],
    "贵金属": ["au", "ag"],
    "航运": ["ec", "bc", "br"],
}

WINDOWS = [500, 700, 900, 1100]
WINDOW_LABELS = ["W1(最近)", "W2", "W3", "W4(最早)"]
DEFAULT_W = {"T": 0.6, "F": 0.25, "C": 0.15}

# GA 参数
POP_SIZE = 30
N_GEN = 12
MIN_TRADES = 8


def get_sector_symbols(sector):
    """获取板块内有效品种列表"""
    syms = []
    for s in SECTORS.get(sector, []):
        if s in SYMBOLS:
            syms.append(s)
    return syms


def evaluate_config(symbol, cfg, df):
    """评估配置在给定数据上的 expR"""
    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=300, min_bars=60, df_in=df)
        nt = int(r.get("trades", 0))
        expR = float(r.get("expR", 0))
        if nt >= MIN_TRADES:
            return expR, nt
    except Exception:
        pass
    return -10.0, 0


def make_config(mode="threshold", weights=None, direction_alpha=0.5):
    """生成配置"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["bias_synthesis"]["direction_mode"] = mode
    cfg["bias_synthesis"]["direction_alpha"] = direction_alpha
    if weights:
        cfg["combine_weights"] = weights
    return cfg


def run_ga_combined(symbol, df_train, direction_alpha=0.5):
    """combined 模式下 GA 优化 T/F/C 权重"""
    if not _HAVE_DEAP:
        return None

    if not hasattr(creator, "CombinedWeight"):
        creator.create("CombinedWeight", base.Fitness, weights=(1.0,))
        creator.create("IndCombined", list, fitness=creator.CombinedWeight)

    toolbox = base.Toolbox()
    toolbox.register("attr_T", random.uniform, 0.20, 0.90)
    toolbox.register("attr_F", random.uniform, 0.00, 0.60)
    toolbox.register("attr_C", random.uniform, 0.00, 0.50)
    toolbox.register(
        "individual", tools.initCycle, creator.IndCombined, (toolbox.attr_T, toolbox.attr_F, toolbox.attr_C), n=1
    )
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        t, f, c = ind[0], ind[1], ind[2]
        total = t + f + c
        if total <= 0:
            return (-10.0,)
        t_n, f_n, c_n = t / total, f / total, c / total
        w = {"T": t_n, "F": f_n, "C": c_n}
        cfg = make_config("combined", w, direction_alpha)
        expR, nt = evaluate_config(symbol, cfg, df_train)
        return (expR,)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.5)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.05, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=POP_SIZE)
    hof = tools.HallOfFame(5)

    fits = list(map(toolbox.evaluate, pop))
    for ind, fit in zip(pop, fits):
        ind.fitness.values = fit
    hof.update(pop)

    best_fitness = max(f[0] for f in fits) if fits else -10
    stagnation = 0

    for gen in range(N_GEN):
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        for i in range(1, len(offspring), 2):
            if random.random() < 0.8:
                offspring[i - 1], offspring[i] = toolbox.mate(offspring[i - 1], offspring[i])
                del offspring[i - 1].fitness.values, offspring[i].fitness.values

        for ind in offspring:
            if random.random() < 0.3:
                toolbox.mutate(ind)
                if ind.fitness.valid:
                    del ind.fitness.values

        invalid = [ind for ind in offspring if not ind.fitness.valid]
        fits = list(map(toolbox.evaluate, invalid))
        for ind, fit in zip(invalid, fits):
            ind.fitness.values = fit

        hof.update(offspring)
        pop[:] = offspring

        current_best = max(ind.fitness.values[0] for ind in pop)
        if current_best > best_fitness + 0.001:
            best_fitness = current_best
            stagnation = 0
        else:
            stagnation += 1
        if stagnation >= 5:
            break

    best = hof[0]
    t, f, c = best[0], best[1], best[2]
    total = t + f + c
    if total <= 0:
        t_n, f_n, c_n = 0.6, 0.25, 0.15
    else:
        t_n, f_n, c_n = t / total, f / total, c / total

    return {
        "T": round(t_n, 4),
        "F": round(f_n, 4),
        "C": round(c_n, 4),
        "train_expR": best_fitness,
    }


def sector_baseline_test(sector):
    """板块级 baseline 测试：threshold vs combined"""
    syms = get_sector_symbols(sector)
    if not syms:
        return None

    results = []
    for sym in syms:
        df = load_daily(sym)
        if df is None or len(df) < 200:
            continue

        # threshold 模式（默认权重）
        cfg_th = make_config("threshold", DEFAULT_W)
        th_expR, th_nt = evaluate_config(sym, cfg_th, df)

        # combined 模式（默认权重 + 默认 alpha）
        cfg_cb = make_config("combined", DEFAULT_W, 0.5)
        cb_expR, cb_nt = evaluate_config(sym, cfg_cb, df)

        results.append(
            {
                "symbol": sym,
                "threshold_expR": th_expR,
                "threshold_nt": th_nt,
                "combined_expR": cb_expR,
                "combined_nt": cb_nt,
                "delta_expR": cb_expR - th_expR if th_nt >= MIN_TRADES and cb_nt >= MIN_TRADES else None,
                "delta_nt": cb_nt - th_nt,
            }
        )

    return results


def sector_ga_combined_oos(sector):
    """板块级 combined 模式 GA 优化 + 单窗口 OOS 验证"""
    syms = get_sector_symbols(sector)
    if not syms:
        return None

    results = []
    for sym in syms:
        df = load_daily(sym)
        if df is None or len(df) < 400:
            continue

        # 切分
        n = len(df)
        split = int(n * 0.75)
        df_train = df.iloc[:split]
        df_oos = df.iloc[split:]

        if len(df_train) < 200 or len(df_oos) < 100:
            continue

        # threshold 默认
        cfg_th = make_config("threshold", DEFAULT_W)
        th_train_expR, th_train_nt = evaluate_config(sym, cfg_th, df_train)
        th_oos_expR, th_oos_nt = evaluate_config(sym, cfg_th, df_oos)

        # combined 默认
        cfg_cb_def = make_config("combined", DEFAULT_W, 0.5)
        cb_def_train_expR, cb_def_train_nt = evaluate_config(sym, cfg_cb_def, df_train)
        cb_def_oos_expR, cb_def_oos_nt = evaluate_config(sym, cfg_cb_def, df_oos)

        # combined + GA 权重
        ga_result = run_ga_combined(sym, df_train, 0.5)
        if ga_result is None:
            continue
        cfg_cb_ga = make_config("combined", {"T": ga_result["T"], "F": ga_result["F"], "C": ga_result["C"]}, 0.5)
        cb_ga_train_expR = ga_result["train_expR"]
        cb_ga_oos_expR, cb_ga_oos_nt = evaluate_config(sym, cfg_cb_ga, df_oos)

        results.append(
            {
                "symbol": sym,
                "threshold": {
                    "train_expR": th_train_expR,
                    "train_nt": th_train_nt,
                    "oos_expR": th_oos_expR,
                    "oos_nt": th_oos_nt,
                },
                "combined_default": {
                    "train_expR": cb_def_train_expR,
                    "train_nt": cb_def_train_nt,
                    "oos_expR": cb_def_oos_expR,
                    "oos_nt": cb_def_oos_nt,
                },
                "combined_ga": {
                    "weights": {"T": ga_result["T"], "F": ga_result["F"], "C": ga_result["C"]},
                    "train_expR": cb_ga_train_expR,
                    "oos_expR": cb_ga_oos_expR,
                    "oos_nt": cb_ga_oos_nt,
                },
            }
        )

    return results


def main():
    sectors_to_test = ["化工", "农产品", "有色", "黑系", "能源"]

    all_baseline = {}
    all_ga_oos = {}

    for sector in sectors_to_test:
        print(f"\n{'=' * 60}")
        print(f"【{sector}】 Baseline 测试 (threshold vs combined)")
        print(f"{'=' * 60}")

        bl = sector_baseline_test(sector)
        all_baseline[sector] = bl

        if not bl:
            print("  无有效品种")
            continue

        valid = [r for r in bl if r["delta_expR"] is not None]
        if valid:
            avg_delta = np.mean([r["delta_expR"] for r in valid])
            avg_th = np.mean([r["threshold_expR"] for r in valid])
            avg_cb = np.mean([r["combined_expR"] for r in valid])
            n_better = sum(1 for r in valid if r["delta_expR"] > 0)

            print(f"  有效品种: {len(valid)}/{len(bl)}")
            print(f"  平均 threshold expR: {avg_th:+.4f}")
            print(f"  平均 combined expR: {avg_cb:+.4f}")
            print(f"  平均 ΔexpR: {avg_delta:+.4f}")
            print(f"  combined 更好: {n_better}/{len(valid)} ({n_better / len(valid) * 100:.0f}%)")
            print()
            print(f"  {'品种':<8}{'th_expR':>10}{'th_nt':>8}{'cb_expR':>10}{'cb_nt':>8}{'ΔexpR':>10}")
            print(f"  {'-' * 60}")
            for r in sorted(valid, key=lambda x: -x["delta_expR"]):
                print(
                    f"  {r['symbol']:<8}{r['threshold_expR']:>+10.3f}{r['threshold_nt']:>8}"
                    f"{r['combined_expR']:>+10.3f}{r['combined_nt']:>8}{r['delta_expR']:>+10.3f}"
                )
        else:
            print("  无有效对比数据")

        # GA OOS 测试
        print(f"\n【{sector}】 Combined GA OOS 验证")
        print(f"{'-' * 60}")
        ga = sector_ga_combined_oos(sector)
        all_ga_oos[sector] = ga

        if not ga:
            print("  无有效品种")
            continue

        valid_ga = [
            r for r in ga if r["combined_default"]["oos_nt"] >= MIN_TRADES and r["combined_ga"]["oos_nt"] >= MIN_TRADES
        ]

        if valid_ga:
            avg_cb_def_oos = np.mean([r["combined_default"]["oos_expR"] for r in valid_ga])
            avg_cb_ga_oos = np.mean([r["combined_ga"]["oos_expR"] for r in valid_ga])
            avg_gain = avg_cb_ga_oos - avg_cb_def_oos
            n_better_ga = sum(1 for r in valid_ga if r["combined_ga"]["oos_expR"] > r["combined_default"]["oos_expR"])

            print(f"  有效品种: {len(valid_ga)}/{len(ga)}")
            print(f"  平均 combined 默认 OOS: {avg_cb_def_oos:+.4f}")
            print(f"  平均 combined GA OOS: {avg_cb_ga_oos:+.4f}")
            print(f"  平均 GA 提升: {avg_gain:+.4f}")
            print(f"  GA 胜率: {n_better_ga}/{len(valid_ga)} ({n_better_ga / len(valid_ga) * 100:.0f}%)")
            print()
            print(f"  {'品种':<8}{'cb_def_OOS':>12}{'cb_ga_OOS':>12}{'ΔOOS':>10}{'GA权重(T/F/C)':>25}")
            print(f"  {'-' * 70}")
            for r in sorted(
                valid_ga, key=lambda x: -(x["combined_ga"]["oos_expR"] - x["combined_default"]["oos_expR"])
            ):
                w = r["combined_ga"]["weights"]
                delta = r["combined_ga"]["oos_expR"] - r["combined_default"]["oos_expR"]
                print(
                    f"  {r['symbol']:<8}{r['combined_default']['oos_expR']:>+12.3f}"
                    f"{r['combined_ga']['oos_expR']:>+12.3f}{delta:>+10.3f}"
                    f"  {w['T']:.2f}/{w['F']:.2f}/{w['C']:.2f}"
                )
        else:
            print("  无有效对比数据")

    # 保存结果
    out = {
        "baseline": all_baseline,
        "ga_oos": all_ga_oos,
    }
    out_path = "logs/combined_mode_exploration.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
