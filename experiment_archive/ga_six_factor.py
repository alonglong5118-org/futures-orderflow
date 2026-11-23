"""
GA 6 因子权重优化（T_trend / T_mean / T_seasonal / F_basis / F_seasonal / C）

从原 3 因子（T/F/C）扩展到 6 子因子，让 GA 搜索最优权重组合。

用法:
  python3 ga_six_factor.py --symbol cu --pop 30 --gen 15
  python3 ga_six_factor.py --symbol rb --tail 600
"""

import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

try:
    import random

    import numpy as np
    from deap import algorithms, base, creator, tools

    _HAVE_DEAP = True
except ImportError:
    _HAVE_DEAP = False

# 6 个子因子名称
SF_NAMES = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]

# GA 默认参数
SF_POP = 30
SF_GEN = 15
SF_CXPB = 0.7
SF_MUTPB = 0.3

# 最少交易数
MIN_TRADES = 8


def _normalize_weights(ind):
    """将权重归一化到和为 1（每个权重 >= 0）。"""
    vals = [max(0.0, float(v)) for v in ind]
    total = sum(vals)
    if total <= 0:
        return [1.0 / len(vals)] * len(vals)
    return [v / total for v in vals]


def _ind_to_weights(ind):
    """染色体 → 权重字典。"""
    norm = _normalize_weights(ind)
    return {name: round(w, 4) for name, w in zip(SF_NAMES, norm)}


def _evaluate(ind, symbol, df_daily, tail=None):
    """评估一组权重：返回 (expR, win_rate, n_trades)"""
    w = _ind_to_weights(ind)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = w

    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=300, min_bars=60, tail=tail, df_in=df_daily)
        expR = float(r.get("expR", 0))
        win_rate = float(r.get("win_rate", 0))
        n_trades = int(r.get("trades", 0))
        if n_trades < MIN_TRADES:
            return -5.0, 0.0, 0
        return expR, win_rate, n_trades
    except Exception:
        return -5.0, 0.0, 0


def optimize_six_factor(symbol, df_daily=None, pop_size=SF_POP, n_gen=SF_GEN, verbose=True, tail=None):
    """6 因子 GA 权重优化。

    返回 dict:
      - best_weights: 最优权重字典
      - best_expR, best_win_rate, n_trades
      - history: 每代最优 expR
    """
    if not _HAVE_DEAP:
        return {"error": "DEAP not installed", "symbol": symbol}

    if df_daily is None:
        df_daily = load_daily(symbol)
    if df_daily is None or len(df_daily) < 120:
        return {"error": "insufficient data", "symbol": symbol}

    if verbose:
        print(f"[6因子GA] {symbol}: pop={pop_size} gen={n_gen} tail={tail}")

    n_genes = len(SF_NAMES)

    # 清理旧的 creator
    for _name in ["FitnessSF", "IndividualSF"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FitnessSF", base.Fitness, weights=(1.0,))
    creator.create("IndividualSF", list, fitness=creator.FitnessSF)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.IndividualSF, toolbox.attr_float, n=n_genes)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        expR, wr, nt = _evaluate(ind, symbol, df_daily, tail=tail)
        return (expR,)

    toolbox.register("evaluate", _eval_ind)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(5)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", lambda x: float(np.mean(x)))
    stats.register("max", lambda x: float(np.max(x)))
    stats.register("min", lambda x: float(np.min(x)))

    history = []

    for gen in range(n_gen):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=SF_CXPB, mutpb=SF_MUTPB)
        fits = list(map(toolbox.evaluate, offspring))
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit

        hof.update(offspring)
        pop = toolbox.select(offspring, k=len(pop))

        record = stats.compile(pop)
        history.append(
            {
                "gen": gen + 1,
                "avg": round(record["avg"], 4),
                "max": round(record["max"], 4),
            }
        )

        if verbose:
            print(f"  Gen {gen + 1:2d}: best={record['max']:+.4f}  avg={record['avg']:+.4f}")

        # 早停：连续 5 代无提升
        if gen >= 5 and history[-5]["max"] >= record["max"] - 0.001:
            if all(h["max"] >= record["max"] - 0.001 for h in history[-5:]):
                if verbose:
                    print("  早停：连续 5 代无显著提升")
                break

    best = hof[0]
    best_w = _ind_to_weights(best)
    best_expR, best_wr, best_nt = _evaluate(best, symbol, df_daily, tail=tail)

    result = {
        "symbol": symbol,
        "best_weights": best_w,
        "best_expR": round(best_expR, 4),
        "best_win_rate": round(best_wr, 4),
        "n_trades": best_nt,
        "history": history,
        "pop_size": pop_size,
        "n_gen": len(history),
    }

    if verbose:
        print(f"  结果: expR={best_expR:+.4f} wr={best_wr:.1%} trades={best_nt}")
        print(f"  权重: {best_w}")

    return result


def main():
    parser = argparse.ArgumentParser(description="GA 6 因子权重优化")
    parser.add_argument("--symbol", type=str, required=True, help="品种代码")
    parser.add_argument("--pop", type=int, default=SF_POP, help="种群大小")
    parser.add_argument("--gen", type=int, default=SF_GEN, help="进化代数")
    parser.add_argument("--tail", type=int, default=600, help="回测使用尾部 N 根日线（0=全量）")
    parser.add_argument("--save", action="store_true", help="保存结果到 logs/ga_six_factor.json")
    args = parser.parse_args()

    result = optimize_six_factor(
        args.symbol, pop_size=args.pop, n_gen=args.gen, verbose=True, tail=args.tail if args.tail > 0 else None
    )

    if args.save and "best_weights" in result:
        out_path = os.path.join(HERE, "logs", "ga_six_factor.json")
        existing = {}
        if os.path.exists(out_path):
            try:
                with open(out_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing[args.symbol] = result
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"\n已保存到 {out_path}")


if __name__ == "__main__":
    main()
