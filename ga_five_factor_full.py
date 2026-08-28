"""5 因子（剔除 T_seasonal）板块级 GA 优化 + OOS 一体化脚本

因子列表：T_trend, T_mean, F_basis, F_seasonal, C
带稳健性约束：单因子上限 0.4，熵惩罚 λ=0.1

一次跑完：训练集 GA → 验证集评估 → 与 6 因子对比
"""
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
from ga_group_six_factor import GROUP_CXPB, GROUP_MUTPB, load_group_data

# ===== 5 因子 =====
SF5_NAMES = ["T_trend", "T_mean", "F_basis", "F_seasonal", "C"]

# ===== 稳健约束 =====
MAX_WEIGHT = 0.40
ENTROPY_LAMBDA = 0.10
MIN_TRADES = 8

# ===== GA 参数 =====
POP = 20
GEN = 12
TRAIN_BARS = 300
TEST_BARS = 200
WINDOW = 200
MIN_BARS = 40

GROUPS = ["化工", "农产品", "有色", "黑系", "能源", "贵金属", "航运"]
OUTFILE = os.path.join(HERE, "logs", "ga_five_factor_oos.json")


def _ind_to_weights5(ind):
    """染色体 → 5 因子权重字典。"""
    s = sum(max(0, x) for x in ind)
    if s < 1e-6:
        return {n: 1.0/len(SF5_NAMES) for n in SF5_NAMES}
    norm = [max(0, x) / s for x in ind]
    return {name: round(w, 4) for name, w in zip(SF5_NAMES, norm)}


def _weight_entropy(weights_dict):
    w = np.array([max(v, 1e-6) for v in weights_dict.values()])
    w = w / w.sum()
    entropy = -np.sum(w * np.log(w))
    max_entropy = np.log(len(w))
    return float(entropy / max_entropy)


def _eval_on_data(weights, data_dict):
    """在给定数据集上评估 5 因子权重，返回 (avg_expR, total_trades, n_valid)"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    # 构造 6 因子权重（T_seasonal 填 0），因为策略层用 6 因子格式
    sf6_w = {
        "T_trend": weights.get("T_trend", 0),
        "T_mean": weights.get("T_mean", 0),
        "T_seasonal": 0.0,
        "F_basis": weights.get("F_basis", 0),
        "F_seasonal": weights.get("F_seasonal", 0),
        "C": weights.get("C", 0),
    }
    cfg["subfactor_weights"] = sf6_w

    expRs = []
    total_trades = 0
    for sym, df in data_dict.items():
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW,
                                      min_bars=MIN_BARS, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass

    if not expRs:
        return -5.0, 0, 0
    avg = float(np.mean(expRs))
    # 有效品种数惩罚
    if len(expRs) < 3:
        avg -= 0.5 * (3 - len(expRs))
    return avg, total_trades, len(expRs)


def _ga_evaluate(ind, train_data):
    """GA 适应度：expR + 熵惩罚 + 权重上限惩罚"""
    w = _ind_to_weights5(ind)

    # 单因子权重上限硬惩罚
    max_w = max(w.values())
    if max_w > MAX_WEIGHT + 0.01:
        penalty = (max_w - MAX_WEIGHT) * 5.0
    else:
        penalty = 0.0

    avg_expR, _, _ = _eval_on_data(w, train_data)

    # 熵奖励
    entropy = _weight_entropy(w)
    entropy_bonus = ENTROPY_LAMBDA * (entropy - 0.5)

    fitness = avg_expR + entropy_bonus - penalty
    return (fitness,)


def optimize_and_validate(group_name):
    """对单个板块做 5 因子 GA 优化 + OOS 验证。"""
    print(f"\n{'='*60}", flush=True)
    print(f"[5因子] 板块: {group_name}", flush=True)
    print(f"{'='*60}", flush=True)

    # 加载全量数据
    group_data = load_group_data(group_name, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
    if len(group_data) < 3:
        print(f"  跳过：有效品种不足（{len(group_data)}个）", flush=True)
        return None

    print(f"  有效品种: {len(group_data)} 个", flush=True)

    # 切分
    train_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        total = len(df)
        train_end = total - TEST_BARS
        train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS):train_end]
        test_data[sym] = df.iloc[train_end:]

    # ========== GA 优化（训练集） ==========
    print("  [训练集] GA 优化中...", flush=True)

    for _name in ["Fitness5", "Individual5"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("Fitness5", base.Fitness, weights=(1.0,))
    creator.create("Individual5", list, fitness=creator.Fitness5)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.Individual5,
                     toolbox.attr_float, n=len(SF5_NAMES))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        return _ga_evaluate(ind, train_data)

    toolbox.register("evaluate", _eval_ind)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=POP)
    hof = tools.HallOfFame(5)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", lambda x: float(np.mean(x)))
    stats.register("max", lambda x: float(np.max(x)))

    history = []
    best_max = -999
    stall = 0
    for gen in range(GEN):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=GROUP_CXPB, mutpb=GROUP_MUTPB)
        fits = list(map(toolbox.evaluate, offspring))
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit
        hof.update(offspring)
        pop = toolbox.select(offspring, k=len(pop))
        record = stats.compile(pop)
        history.append({"gen": gen+1, "avg": record["avg"], "max": record["max"]})
        print(f"    Gen {gen+1:2d}: best={record['max']:+.4f}  avg={record['avg']:+.4f}", flush=True)
        if record["max"] > best_max + 0.001:
            best_max = record["max"]
            stall = 0
        else:
            stall += 1
        if stall >= 5:
            print(f"    早停：连续 5 代无显著提升", flush=True)
            break

    best = hof[0]
    best_w = _ind_to_weights5(best)
    print(f"  最优 5 因子权重: {best_w}", flush=True)
    print(f"  权重熵: {_weight_entropy(best_w):.3f}", flush=True)
    print(f"  最大单因子: {max(best_w.values()):.3f}", flush=True)

    # ========== OOS 验证 ==========
    train_expR, train_trades, train_valid = _eval_on_data(best_w, train_data)
    test_expR, test_trades, test_valid = _eval_on_data(best_w, test_data)

    # 基准（默认 3 因子，无 subfactor_weights）
    base_cfg = copy.deepcopy(DEFAULT_CONFIG)
    base_train_expR, _, _ = _eval_on_data({}, train_data) if False else (0, 0, 0)
    # 直接用无 subfactor_weights 的配置跑基准
    base_train_expR, base_train_trades, base_train_valid = _eval_base(train_data)
    base_test_expR, base_test_trades, base_test_valid = _eval_base(test_data)

    train_gain = train_expR - base_train_expR
    test_gain = test_expR - base_test_expR
    overfit = test_gain / train_gain if abs(train_gain) > 0.001 else 0.0

    print(flush=True)
    print(f"  {'':12s} {'基准':>10s} {'5因子':>10s} {'提升':>10s}", flush=True)
    print(f"  {'训练集':12s} {base_train_expR:+.4f}    {train_expR:+.4f}    {train_gain:+.4f}", flush=True)
    print(f"  {'验证集':12s} {base_test_expR:+.4f}    {test_expR:+.4f}    {test_gain:+.4f}", flush=True)
    print(f"  过拟合系数: {overfit:+.2f}", flush=True)

    return {
        "group": group_name,
        "best_weights": best_w,
        "entropy": _weight_entropy(best_w),
        "max_weight": max(best_w.values()),
        "train": {
            "base_expR": base_train_expR,
            "sf5_expR": train_expR,
            "gain": train_gain,
            "trades": train_trades,
            "n_valid": train_valid,
        },
        "test": {
            "base_expR": base_test_expR,
            "sf5_expR": test_expR,
            "gain": test_gain,
            "trades": test_trades,
            "n_valid": test_valid,
        },
        "overfit_coef": overfit,
        "history": history,
    }


def _eval_base(data_dict):
    """基准：默认权重（无 subfactor_weights）"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    expRs = []
    total_trades = 0
    for sym, df in data_dict.items():
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW,
                                      min_bars=MIN_BARS, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass
    if not expRs:
        return -5.0, 0, 0
    avg = float(np.mean(expRs))
    if len(expRs) < 3:
        avg -= 0.5 * (3 - len(expRs))
    return avg, total_trades, len(expRs)


def main():
    t0 = time.time()
    results = {}

    for g in GROUPS:
        r = optimize_and_validate(g)
        if r:
            results[g] = r

    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f"\n{'='*70}", flush=True)
    print(f"全部完成，耗时 {elapsed/60:.1f} 分钟", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"{'板块':<8s} {'训练基准':>10s} {'训练5因子':>10s} {'验证基准':>10s} {'验证5因子':>10s} {'过拟合':>8s}", flush=True)
    print("-" * 60, flush=True)
    for g, r in results.items():
        print(f"{g:<8s} {r['train']['base_expR']:+.4f}    {r['train']['sf5_expR']:+.4f}    "
              f"{r['test']['base_expR']:+.4f}    {r['test']['sf5_expR']:+.4f}    "
              f"{r['overfit_coef']:+.2f}", flush=True)
    print(f"\n结果已保存到: {OUTFILE}", flush=True)


if __name__ == "__main__":
    main()
