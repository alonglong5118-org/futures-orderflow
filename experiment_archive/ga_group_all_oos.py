"""全板块 6 因子样本外验证（前 N 训练 / 后 M 验证）

对所有有效板块逐一做：
1. 训练集 GA 优化（前 TRAIN_BARS 根）
2. 验证集测试（后 TEST_BARS 根）
3. 对比默认权重 vs GA 6 因子权重
4. 计算过拟合系数
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import random

import numpy as np
from deap import algorithms, base, creator, tools

from four_dim_strategy import DEFAULT_CONFIG, walk_forward_backtest
from ga_group_six_factor import (
    GROUP_CXPB,
    GROUP_MUTPB,
    MIN_TRADES_PER_SYMBOL,
    SF_NAMES,
    _evaluate,
    _ind_to_weights,
    load_group_data,
)

GROUPS = ["化工", "农产品", "有色", "黑系", "能源", "贵金属", "航运"]

TRAIN_BARS = 300  # 训练集 N 根
TEST_BARS = 200  # 验证集 M 根
POP = 20
GEN = 10
MIN_TRADES = MIN_TRADES_PER_SYMBOL
OUTFILE = os.path.join(HERE, "logs", "ga_group_six_factor_oos.json")

results = {}


def run_oos_for_group(group_name):
    """对单个板块做 OOS 验证，返回结果字典。"""
    print(f"\n{'=' * 60}", flush=True)
    print(f"[OOS] 板块: {group_name}", flush=True)
    print(f"{'=' * 60}", flush=True)

    # 加载全量数据
    group_data = load_group_data(group_name, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
    if len(group_data) < 3:
        print(f"  跳过：有效品种不足（{len(group_data)}个）", flush=True)
        return None

    print(f"  有效品种: {len(group_data)} 个", flush=True)

    # 切分训练/验证
    train_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        total = len(df)
        train_end = total - TEST_BARS
        train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS) : train_end]
        test_data[sym] = df.iloc[train_end:]

    print(f"  数据切分: train={TRAIN_BARS} 根, test={TEST_BARS} 根", flush=True)
    print(flush=True)

    # ========== 训练集 GA 优化 ==========
    print("  [训练集] GA 优化中...", flush=True)

    # 清理 DEAP creator
    for _name in ["FitnessGroup", "IndividualGroup"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FitnessGroup", base.Fitness, weights=(1.0,))
    creator.create("IndividualGroup", list, fitness=creator.FitnessGroup)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.IndividualGroup, toolbox.attr_float, n=len(SF_NAMES))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval_ind(ind):
        return _evaluate(ind, train_data)

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
        history.append({"gen": gen + 1, "avg": record["avg"], "max": record["max"]})
        print(f"    Gen {gen + 1:2d}: best={record['max']:+.4f}  avg={record['avg']:+.4f}", flush=True)
        if record["max"] > best_max + 0.001:
            best_max = record["max"]
            stall = 0
        else:
            stall += 1
        if stall >= 5:
            print("    早停：连续 5 代无显著提升", flush=True)
            break

    best = hof[0]
    best_w = _ind_to_weights(best)
    print(f"  最优权重: {best_w}", flush=True)

    # ========== 基准 vs 6因子 对比 ==========
    def eval_data(data, cfg_override=None):
        """在给定数据集上评估，返回 (avg_expR, per_symbol, total_trades, n_valid)"""
        expRs = []
        per_symbol = {}
        total_trades = 0
        for sym, df in sorted(data.items()):
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            if cfg_override:
                cfg.update(cfg_override)
            r = walk_forward_backtest(sym, cfg=cfg, window=200, min_bars=40, df_in=df)
            nt = int(r.get("trades", 0))
            per_symbol[sym] = {"expR": float(r.get("expR", 0)), "win_rate": float(r.get("win_rate", 0)), "trades": nt}
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        avg = float(np.mean(expRs)) if expRs else 0.0
        return avg, per_symbol, total_trades, len(expRs)

    print(flush=True)
    print("  [结果对比]", flush=True)

    train_base, train_base_per, train_base_trades, train_base_valid = eval_data(train_data)
    train_sf, train_sf_per, train_sf_trades, train_sf_valid = eval_data(train_data, {"subfactor_weights": best_w})
    test_base, test_base_per, test_base_trades, test_base_valid = eval_data(test_data)
    test_sf, test_sf_per, test_sf_trades, test_sf_valid = eval_data(test_data, {"subfactor_weights": best_w})

    print(f"  {'':12s} {'基准':>10s} {'6因子':>10s} {'变化':>10s}", flush=True)
    print("  " + "-" * 46, flush=True)
    train_gain_pct = (train_sf - train_base) / abs(train_base) * 100 if abs(train_base) > 0.001 else 0
    test_gain_pct = (test_sf - test_base) / abs(test_base) * 100 if abs(test_base) > 0.001 else 0
    print(f"  {'训练集':12s} {train_base:+.4f}    {train_sf:+.4f}    {train_gain_pct:+.1f}%", flush=True)
    print(f"  {'验证集':12s} {test_base:+.4f}    {test_sf:+.4f}    {test_gain_pct:+.1f}%", flush=True)

    train_gain = train_sf - train_base
    test_gain = test_sf - test_base
    overfit_coef = test_gain / train_gain if abs(train_gain) > 0.001 else 0.0
    print(flush=True)
    print(f"  训练集提升: {train_gain:+.4f}", flush=True)
    print(f"  验证集提升: {test_gain:+.4f}", flush=True)
    print(f"  过拟合系数: {overfit_coef:.2f} (1.0=无过拟合, <0=完全失效)", flush=True)

    result = {
        "group": group_name,
        "best_weights": best_w,
        "train": {
            "base_expR": train_base,
            "sf_expR": train_sf,
            "gain": train_gain,
            "gain_pct": train_gain_pct,
            "n_valid": train_sf_valid,
            "total_trades": train_sf_trades,
            "per_symbol": train_sf_per,
        },
        "test": {
            "base_expR": test_base,
            "sf_expR": test_sf,
            "gain": test_gain,
            "gain_pct": test_gain_pct,
            "n_valid": test_sf_valid,
            "total_trades": test_sf_trades,
            "per_symbol": test_sf_per,
        },
        "overfit_coef": overfit_coef,
        "history": history,
    }
    return result


# ========== 主流程 ==========
print("=" * 60, flush=True)
print("全板块 6 因子 OOS 样本外验证", flush=True)
print(f"训练集: {TRAIN_BARS} 根  |  验证集: {TEST_BARS} 根", flush=True)
print(f"GA: pop={POP}, gen={GEN}", flush=True)
print("=" * 60, flush=True)

for group in GROUPS:
    r = run_oos_for_group(group)
    if r is not None:
        results[group] = r

# 保存结果
with open(OUTFILE, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n{'=' * 60}", flush=True)
print("全部完成，结果汇总", flush=True)
print(f"{'=' * 60}", flush=True)
print(
    f"{'板块':<8s} {'训练基准':>10s} {'训练6因子':>10s} {'验证基准':>10s} {'验证6因子':>10s} {'过拟合系数':>10s}",
    flush=True,
)
print("-" * 64, flush=True)
for group, r in results.items():
    print(
        f"{group:<8s} {r['train']['base_expR']:+.4f}    {r['train']['sf_expR']:+.4f}    "
        f"{r['test']['base_expR']:+.4f}    {r['test']['sf_expR']:+.4f}    "
        f"{r['overfit_coef']:+.2f}",
        flush=True,
    )

print(f"\n结果已保存到: {OUTFILE}", flush=True)
