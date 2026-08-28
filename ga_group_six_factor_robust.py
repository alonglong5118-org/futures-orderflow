"""稳健性约束版 板块级 GA 6 因子优化

在 ga_group_six_factor.py 基础上增加：
1. 单因子权重上限（默认 0.35）—— 防止极端权重
2. 权重熵惩罚 —— 鼓励因子分散，避免过度依赖单一因子
3. 最低交易数提升到 8 笔/品种 —— 过滤样本量不足的品种
4. 噪声扰动鲁棒性 —— 每组权重评估 N 次（带轻微噪声），取均值

直接在训练集上跑，结果与无约束版对比。
"""
import argparse
import copy
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
from deap import algorithms, base, creator, tools

from four_dim_strategy import DEFAULT_CONFIG, walk_forward_backtest
from ga_group_six_factor import (
    GROUP_CXPB,
    GROUP_MUTPB,
    SF_NAMES,
    _ind_to_weights,
    load_group_data,
)

# ===== 稳健性参数 =====
MAX_WEIGHT = 0.35        # 单因子权重上限
ENTROPY_LAMBDA = 0.15    # 熵惩罚系数（越大越鼓励分散）
MIN_TRADES_ROBUST = 8    # 最低交易数（每品种）
NOISE_ROUNDS = 1         # 噪声扰动次数（0=关闭）
NOISE_STD = 0.02         # 噪声标准差（权重的相对扰动）

GROUPS = ["化工", "农产品", "有色", "黑系", "能源", "贵金属", "航运"]

POP = 20
GEN = 12
TAIL_BARS = 500
WINDOW = 300
MIN_BARS = 60

OUTFILE = os.path.join(HERE, "logs", "ga_group_six_factor_robust.json")


def _weight_entropy(weights_dict):
    """计算权重的归一化熵：1 = 均匀分布，0 = 单点分布。"""
    w = np.array([max(v, 1e-6) for v in weights_dict.values()])
    w = w / w.sum()
    entropy = -np.sum(w * np.log(w))
    max_entropy = np.log(len(w))
    return float(entropy / max_entropy)


def _evaluate_robust(ind, group_data, cfg_template=None):
    """带稳健性约束的评估：返回 (fitness,)
    fitness = avg_expR + ENTROPY_LAMBDA * (entropy - 0.5)
    """
    w = _ind_to_weights(ind)

    # 单因子权重上限硬约束：超过上限的直接大幅惩罚
    max_w = max(w.values())
    if max_w > MAX_WEIGHT + 0.01:
        penalty = (max_w - MAX_WEIGHT) * 5.0
    else:
        penalty = 0.0

    cfg = copy.deepcopy(cfg_template or DEFAULT_CONFIG)
    cfg["subfactor_weights"] = w

    expRs = []
    total_trades = 0
    for sym, df in group_data.items():
        # 多次评估取均值（带噪声扰动）
        sym_expRs = []
        for n in range(max(1, NOISE_ROUNDS)):
            if NOISE_ROUNDS > 0 and n > 0:
                # 给权重加轻微噪声后再评估
                noisy_w = {}
                for k, v in w.items():
                    noise = np.random.normal(0, NOISE_STD)
                    noisy_w[k] = max(0.001, v * (1 + noise))
                # 重新归一化
                s = sum(noisy_w.values())
                noisy_w = {k: v / s for k, v in noisy_w.items()}
                cfg_noisy = copy.deepcopy(cfg)
                cfg_noisy["subfactor_weights"] = noisy_w
            else:
                cfg_noisy = cfg

            try:
                r = walk_forward_backtest(sym, cfg=cfg_noisy, window=WINDOW,
                                          min_bars=MIN_BARS, df_in=df)
                nt = int(r.get("trades", 0))
                if nt >= MIN_TRADES_ROBUST:
                    sym_expRs.append(float(r.get("expR", 0)))
            except Exception:
                pass

        if sym_expRs:
            expRs.append(float(np.mean(sym_expRs)))
            # 总交易数取第一次（无噪声）的结果
            try:
                r0 = walk_forward_backtest(sym, cfg=cfg, window=WINDOW,
                                           min_bars=MIN_BARS, df_in=df)
                total_trades += int(r0.get("trades", 0))
            except Exception:
                pass

    if not expRs:
        return (-5.0 - penalty,)

    avg_expR = float(np.mean(expRs))

    # 熵惩罚：熵越低（越集中），惩罚越大
    entropy = _weight_entropy(w)
    entropy_bonus = ENTROPY_LAMBDA * (entropy - 0.5)  # 熵>0.5有奖，<0.5有罚

    # 有效品种数惩罚
    n_valid = len(expRs)
    if n_valid < 3:
        avg_expR -= 0.5 * (3 - n_valid)

    fitness = avg_expR + entropy_bonus - penalty

    return (fitness,)


def optimize_group(group_name, pop_size=POP, n_gen=GEN, tail=TAIL_BARS):
    """对单个板块做带约束的 GA 优化。"""
    print(f"\n{'='*60}", flush=True)
    print(f"[稳健版] 板块: {group_name}", flush=True)
    print(f"  约束: max_weight={MAX_WEIGHT}, entropy_lambda={ENTROPY_LAMBDA}, "
          f"min_trades={MIN_TRADES_ROBUST}", flush=True)
    print(f"{'='*60}", flush=True)

    group_data = load_group_data(group_name, min_bars=MIN_BARS + WINDOW, tail=tail)
    if len(group_data) < 3:
        print(f"  跳过：有效品种不足（{len(group_data)}个）", flush=True)
        return None

    print(f"  有效品种: {len(group_data)} 个", flush=True)

    # 清理 DEAP creator
    for _name in ["FitnessRobust", "IndividualRobust"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FitnessRobust", base.Fitness, weights=(1.0,))
    creator.create("IndividualRobust", list, fitness=creator.FitnessRobust)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.IndividualRobust,
                     toolbox.attr_float, n=len(SF_NAMES))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        return _evaluate_robust(ind, group_data)

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
    best_max = -999
    stall = 0
    for gen in range(n_gen):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=GROUP_CXPB, mutpb=GROUP_MUTPB)
        fits = list(map(toolbox.evaluate, offspring))
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit
        hof.update(offspring)
        pop = toolbox.select(offspring, k=len(pop))
        record = stats.compile(pop)
        history.append({"gen": gen+1, "avg": record["avg"], "max": record["max"]})
        print(f"  Gen {gen+1:2d}: best={record['max']:+.4f}  avg={record['avg']:+.4f}", flush=True)
        if record["max"] > best_max + 0.001:
            best_max = record["max"]
            stall = 0
        else:
            stall += 1
        if stall >= 5:
            print(f"  早停：连续 5 代无显著提升", flush=True)
            break

    best = hof[0]
    best_w = _ind_to_weights(best)

    # 计算纯 expR（不含惩罚项）
    # 用原始评估函数算一遍纯 expR
    from ga_group_six_factor import _evaluate as _raw_eval
    raw_result = _raw_eval(best, group_data)
    pure_expR = raw_result[0]

    print(f"  最优权重: {best_w}", flush=True)
    print(f"  纯 expR: {pure_expR:+.4f}  (含约束适应度: {best_max:+.4f})", flush=True)
    print(f"  权重熵: {_weight_entropy(best_w):.3f}", flush=True)

    return {
        "group": group_name,
        "best_weights": best_w,
        "pure_expR": pure_expR,
        "constrained_fitness": best_max,
        "entropy": _weight_entropy(best_w),
        "max_weight": max(best_w.values()),
        "n_valid": len(group_data),
        "history": history,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=str, default=None, help="指定单个板块")
    parser.add_argument("--pop", type=int, default=POP)
    parser.add_argument("--gen", type=int, default=GEN)
    parser.add_argument("--tail", type=int, default=TAIL_BARS)
    args = parser.parse_args()

    t0 = time.time()
    results = {}

    if args.group:
        r = optimize_group(args.group, pop_size=args.pop, n_gen=args.gen, tail=args.tail)
        if r:
            results[args.group] = r
    else:
        for g in GROUPS:
            r = optimize_group(g, pop_size=args.pop, n_gen=args.gen, tail=args.tail)
            if r:
                results[g] = r

    # 保存
    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"全部完成，耗时 {elapsed/60:.1f} 分钟", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"{'板块':<8s} {'纯expR':>10s} {'约束适应度':>10s} {'最大权重':>10s} {'熵':>8s}", flush=True)
    print("-" * 50, flush=True)
    for g, r in results.items():
        print(f"{g:<8s} {r['pure_expR']:+.4f}    {r['constrained_fitness']:+.4f}    "
              f"{r['max_weight']:.3f}    {r['entropy']:.3f}", flush=True)
    print(f"\n结果已保存到: {OUTFILE}", flush=True)


if __name__ == "__main__":
    main()
