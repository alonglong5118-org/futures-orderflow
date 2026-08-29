"""
板块级 GA 6 因子权重优化

同一板块的所有品种共享一套权重，适应度 = 板块内所有品种的平均 expR。
大幅降低过拟合风险，输出 7 套板块权重。

用法:
  python3 ga_group_six_factor.py --group 化工 --pop 30 --gen 15
  python3 ga_group_six_factor.py --all --pop 25 --gen 12
"""

import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

try:
    import random

    import numpy as np
    from deap import algorithms, base, creator, tools

    _HAVE_DEAP = True
except ImportError:
    _HAVE_DEAP = False

# 6 个子因子名称
SF_NAMES = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]

# GA 参数
GROUP_POP = 30
GROUP_GEN = 15
GROUP_CXPB = 0.7
GROUP_MUTPB = 0.3

# 最少单品种交易数（低于则不计入平均）
MIN_TRADES_PER_SYMBOL = 5


def get_group_symbols(group):
    """获取某板块的品种列表（只取有日线数据的）。"""
    syms = []
    for sym, info in SYMBOLS.items():
        if info.get("group") == group:
            syms.append(sym)
    return syms


def load_group_data(group, min_bars=200, tail=0):
    """加载板块内所有品种的数据，返回 {symbol: df}。"""
    data = {}
    for sym in get_group_symbols(group):
        try:
            df = load_daily(sym)
            if df is None or len(df) < min_bars:
                continue
            if tail and len(df) > tail:
                df = df.tail(tail)
            data[sym] = df
        except Exception:
            continue
    return data


def _normalize_weights(ind):
    """权重归一化到和为 1。"""
    vals = [max(0.0, float(v)) for v in ind]
    total = sum(vals)
    if total <= 0:
        return [1.0 / len(vals)] * len(vals)
    return [v / total for v in vals]


def _ind_to_weights(ind):
    """染色体 → 权重字典。"""
    norm = _normalize_weights(ind)
    return {name: round(w, 4) for name, w in zip(SF_NAMES, norm)}


def _evaluate(ind, group_data, cfg_template=None):
    """评估一组权重：板块内所有品种的平均 expR。"""
    w = _ind_to_weights(ind)
    cfg = copy.deepcopy(cfg_template or DEFAULT_CONFIG)
    cfg["subfactor_weights"] = w

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
        return (-5.0,)

    avg_expR = float(np.mean(expRs))
    # 惩罚有效品种太少（至少 3 个才有统计意义）
    n_valid = len(expRs)
    if n_valid < 3:
        avg_expR -= 0.5 * (3 - n_valid)

    return (avg_expR,)


def optimize_group(group, pop_size=GROUP_POP, n_gen=GROUP_GEN, verbose=True, tail=600, min_bars=200):
    """对一个板块运行 GA 6 因子优化。

    返回 dict:
      - group, best_weights, best_avg_expR, n_valid_symbols, total_trades
      - per_symbol: 各品种 expR
      - history: 每代最优
    """
    if not _HAVE_DEAP:
        return {"error": "DEAP not installed", "group": group}

    # 加载数据
    group_data = load_group_data(group, min_bars=min_bars, tail=tail)
    if len(group_data) < 2:
        return {"error": f"有效品种不足（{len(group_data)}个）", "group": group}

    if verbose:
        print(f"[板块GA] {group}: {len(group_data)} 个有效品种, pop={pop_size}, gen={n_gen}, tail={tail}")
        for sym in sorted(group_data.keys()):
            print(f"  - {sym}: {len(group_data[sym])} 根")

    n_genes = len(SF_NAMES)

    # 清理旧 creator
    for _name in ["FitnessGroup", "IndividualGroup"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FitnessGroup", base.Fitness, weights=(1.0,))
    creator.create("IndividualGroup", list, fitness=creator.FitnessGroup)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.IndividualGroup, toolbox.attr_float, n=n_genes)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        return _evaluate(ind, group_data)

    toolbox.register("evaluate", _eval_ind)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(5)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", lambda x: float(np.mean(x)))
    stats.register("max", lambda x: float(np.max(x)))

    history = []

    for gen in range(n_gen):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=GROUP_CXPB, mutpb=GROUP_MUTPB)
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
        if gen >= 5 and all(h["max"] >= record["max"] - 0.001 for h in history[-5:]):
            if verbose:
                print("  早停：连续 5 代无显著提升")
            break

    best = hof[0]
    best_w = _ind_to_weights(best)

    # 计算各品种详情
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = best_w
    per_symbol = {}
    expRs = []
    total_trades = 0
    for sym, df in group_data.items():
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=300, min_bars=60, df_in=df)
            nt = int(r.get("trades", 0))
            per_symbol[sym] = {
                "expR": round(float(r.get("expR", 0)), 4),
                "win_rate": round(float(r.get("win_rate", 0)), 3),
                "trades": nt,
            }
            if nt >= MIN_TRADES_PER_SYMBOL:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception as e:
            per_symbol[sym] = {"error": str(e)[:50]}

    avg_expR = float(np.mean(expRs)) if expRs else 0.0

    result = {
        "group": group,
        "best_weights": best_w,
        "best_avg_expR": round(avg_expR, 4),
        "n_valid_symbols": len(expRs),
        "total_trades": total_trades,
        "per_symbol": per_symbol,
        "history": history,
        "pop_size": pop_size,
        "n_gen": len(history),
        "n_symbols_with_data": len(group_data),
    }

    if verbose:
        print(f"\n  结果: avg_expR={avg_expR:+.4f}")
        print(f"  有效品种: {len(expRs)}/{len(group_data)}")
        print(f"  总交易数: {total_trades}")
        print(f"  最优权重: {best_w}")
        for sym in sorted(per_symbol.keys()):
            v = per_symbol[sym]
            if "expR" in v:
                print(f"    {sym}: expR={v['expR']:+.4f} wr={v['win_rate']:.1%} trades={v['trades']}")
            else:
                print(f"    {sym}: 失败 {v.get('error', '')}")

    return result


def main():
    parser = argparse.ArgumentParser(description="板块级 GA 6 因子权重优化")
    parser.add_argument("--group", type=str, default="", help="优化单个板块")
    parser.add_argument("--all", action="store_true", help="优化所有板块")
    parser.add_argument("--pop", type=int, default=GROUP_POP, help="种群大小")
    parser.add_argument("--gen", type=int, default=GROUP_GEN, help="进化代数")
    parser.add_argument("--tail", type=int, default=600, help="回测使用尾部 N 根日线")
    parser.add_argument("--min-bars", type=int, default=200, help="最少日线数")
    parser.add_argument("--save", action="store_true", help="保存结果到 logs/ga_group_six_factor.json")
    args = parser.parse_args()

    groups = []
    if args.group:
        groups = [args.group]
    elif args.all:
        groups = ["化工", "农产品", "有色", "黑系", "能源", "贵金属", "航运"]
    else:
        print("请指定 --group 板块名 或 --all")
        return

    all_results = {}
    t0 = time.time()

    for i, g in enumerate(groups):
        print(f"\n{'=' * 60}")
        print(f"[{i + 1}/{len(groups)}] 优化板块: {g}")
        print(f"{'=' * 60}")
        result = optimize_group(
            g, pop_size=args.pop, n_gen=args.gen, verbose=True, tail=args.tail, min_bars=args.min_bars
        )
        all_results[g] = result

        if args.save:
            out_path = os.path.join(HERE, "logs", "ga_group_six_factor.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)
            print(f"  已保存到 {out_path}")

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"全部完成，耗时 {elapsed / 60:.1f} 分钟")
    print(f"{'=' * 60}")
    for g, r in all_results.items():
        if "best_weights" in r:
            print(f"  {g}: avg_expR={r['best_avg_expR']:+.4f} ({r['n_valid_symbols']}品种/{r['total_trades']}笔)")
        else:
            print(f"  {g}: 失败 - {r.get('error', '')}")


if __name__ == "__main__":
    main()
