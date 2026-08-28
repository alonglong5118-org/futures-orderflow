# -*- coding: utf-8 -*-
"""GA 因子挖掘与权重优化框架（GA Factor Miner）
=================================================================
两个功能：
  1. F/T/C 三维修分权重优化 —— 用 GA 寻找每个品种每个 regime 下的最优权重
  2. 因子挖掘 —— 从 8 策略候选池中筛选有效因子组合 + 最优权重

参考 Kara说量化 的遗传算法因子挖掘思路，适配到四维策略框架。
复用现有 walk_forward_backtest 基础设施 + DEAP NSGA-II 框架。

红线（与 ga_tpsl_optimizer_v3 一致）：
  - 所有优化基于 walk-forward OOS，杜绝前视偏差
  - ±20% 参数扰动稳健性检验
  - 优化结果写入 calibration_params.json 供 pipeline 消费

用法：
  # 权重优化
  python3 ga_factor_miner.py --mode weight --symbol jd
  # 因子挖掘
  python3 ga_factor_miner.py --mode factor --symbol jd
  # API 调用（runner 集成）
  import ga_factor_miner as gfm
  result = gfm.optimize_weights("jd", df_daily)
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

try:
    from deap import base, creator, tools
    _HAVE_DEAP = True
except Exception:
    _HAVE_DEAP = False

# ============================================================================
# 权重优化：F/T/C 三维修分权重
# ============================================================================

# 基线权重（与 DEFAULT_CONFIG["combine_weights"] 一致）
BASE_WEIGHTS = {"T": 0.6, "F": 0.25, "C": 0.15}

# 权重搜索范围
WEIGHT_BOUNDS = {
    "T": (0.30, 0.80),
    "F": (0.00, 0.50),
    "C": (0.00, 0.40),
}

# Regime 级别权重偏移（在基础权重上加减）
REGIME_ADJUST_BOUNDS = {
    "trend_T": (-0.10, 0.10),
    "trend_F": (-0.05, 0.05),
    "vol_T": (-0.05, 0.05),
    "vol_F": (-0.05, 0.05),
    "range_T": (-0.15, 0.05),
    "range_F": (-0.05, 0.10),
}

# GA 参数
WEIGHT_POP = 60
WEIGHT_GEN = 20
WEIGHT_CXPB = 0.8
WEIGHT_MUTPB = 0.3
WEIGHT_EARLY_STOP = 6
MIN_TRADES_WEIGHT = 8

# 结果存储
WEIGHTS_FILE = os.path.join(HERE, "ga_weights_cache.json")


def _normalize_weights(t, f, c):
    """归一化权重使 T+F+C≈1.0。"""
    total = t + f + c
    if total <= 0:
        return 0.6, 0.25, 0.15
    return t / total, f / total, c / total


def _chromosome_to_weights(ind):
    """染色体 → combine_weights dict + regime 调整。

    染色体布局（9 基因）：
    [0] T_base, [1] F_base, [2] C_base  → 归一化为基础权重
    [3] trend_T_adj, [4] trend_F_adj    → 趋势 regime 偏移
    [5] vol_T_adj, [6] vol_F_adj         → 波动 regime 偏移
    [7] range_T_adj, [8] range_F_adj     → 震荡 regime 偏移
    """
    t, f, c = _normalize_weights(ind[0], ind[1], ind[2])
    return {
        "base": {"T": round(t, 4), "F": round(f, 4), "C": round(round(1 - t - f, 4), 4)},
        "regime_adjust": {
            "趋势": {"T": round(ind[3], 4), "F": round(ind[4], 4)},
            "波动": {"T": round(ind[5], 4), "F": round(ind[6], 4)},
            "震荡": {"T": round(ind[7], 4), "F": round(ind[8], 4)},
        },
    }


def _make_config_with_weights(ind, symbol, base_cfg=DEFAULT_CONFIG):
    """根据染色体生成带权重的配置。"""
    cfg = copy.deepcopy(base_cfg)
    w = _chromosome_to_weights(ind)
    cfg["combine_weights"] = w["base"]

    # Regime 级别权重：通过 regime_coef 间接调整
    # 在 combine_bias 中权重是全局的，regime 调整通过乘以偏移实现
    cfg["combine_weights_regime"] = w["regime_adjust"]
    return cfg


def _evaluate_weights(ind, symbol, df_daily, train_start, train_end, tail=None):
    """评估一条权重染色体的适应度。

    返回 (expR, win_rate, n_trades) 三元组。
    tail: 仅用尾部 N 根日线做回测（加速用）。None=全量。
    """
    cfg = _make_config_with_weights(ind, symbol)

    try:
        df_use = df_daily
        if tail and df_use is not None and len(df_use) > tail:
            df_use = df_use.tail(tail).copy()
        r = walk_forward_backtest(symbol, cfg=cfg, window=300,
                                  df_in=df_use,
                                  tail=tail)
        expR = float(r.get("expR", 0))
        win_rate = float(r.get("win_rate", 0))
        n_trades = int(r.get("trades", 0))
        if n_trades < MIN_TRADES_WEIGHT:
            return -10.0, 0.0, n_trades
        win_rate = min(max(win_rate, 0.0), 1.0)
        return expR, win_rate, n_trades
    except Exception:
        return -10.0, 0.0, 0


def optimize_weights(symbol, df_daily=None, pop_size=WEIGHT_POP, n_gen=WEIGHT_GEN,
                     verbose=True, tail=None):
    """对指定品种运行 GA 权重优化。

    返回 dict:
      - best_weights: {base: {T,F,C}, regime_adjust: {...}}
      - best_expR: 最优期望 R
      - best_calmar: 最优卡玛比率
      - n_trades: 交易笔数
      - robust_score: 稳健性评分（±20%扰动后 OOS 均值 / 原始 OOS）
      - history: 进化历史
      - elapsed: 耗时（秒）
      - tail: 使用的回测长度
    """
    if not _HAVE_DEAP:
        return _fallback_weights(symbol)

    if df_daily is None:
        df_daily = load_daily(symbol)
    if df_daily is None or len(df_daily) < 200:
        return _fallback_weights(symbol)

    n = len(df_daily)
    train_start = max(0, n - 400)
    train_end = n

    if verbose:
        print(f"[GA权重] {symbol}: pop={pop_size} gen={n_gen} bars={n}"
              f"{' tail='+str(tail) if tail else ''}")

    # 创建 fitness（多目标：最大化 expR + 最大化 calmar）
    if "FitnessWeight" in dir(creator):
        del creator.FitnessWeight
    if "IndividualWeight" in dir(creator):
        del creator.IndividualWeight
    creator.create("FitnessWeight", base.Fitness, weights=(1.0, 1.0))
    creator.create("IndividualWeight", list, fitness=creator.FitnessWeight)

    toolbox = base.Toolbox()
    toolbox.register("attr_T", random.uniform, *WEIGHT_BOUNDS["T"])
    toolbox.register("attr_F", random.uniform, *WEIGHT_BOUNDS["F"])
    toolbox.register("attr_C", random.uniform, *WEIGHT_BOUNDS["C"])
    toolbox.register("attr_trend_T", random.uniform, *REGIME_ADJUST_BOUNDS["trend_T"])
    toolbox.register("attr_trend_F", random.uniform, *REGIME_ADJUST_BOUNDS["trend_F"])
    toolbox.register("attr_vol_T", random.uniform, *REGIME_ADJUST_BOUNDS["vol_T"])
    toolbox.register("attr_vol_F", random.uniform, *REGIME_ADJUST_BOUNDS["vol_F"])
    toolbox.register("attr_range_T", random.uniform, *REGIME_ADJUST_BOUNDS["range_T"])
    toolbox.register("attr_range_F", random.uniform, *REGIME_ADJUST_BOUNDS["range_F"])

    toolbox.register("individual", tools.initCycle, creator.IndividualWeight,
                     (toolbox.attr_T, toolbox.attr_F, toolbox.attr_C,
                      toolbox.attr_trend_T, toolbox.attr_trend_F,
                      toolbox.attr_vol_T, toolbox.attr_vol_F,
                      toolbox.attr_range_T, toolbox.attr_range_F), n=1)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        expR, calmar, n_tr = _evaluate_weights(
            ind, symbol, df_daily, train_start, train_end, tail=tail)
        return (expR, calmar)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.5)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.05, indpb=0.3)
    toolbox.register("select", tools.selNSGA2)

    pop = toolbox.population(n=pop_size)
    hof = tools.ParetoFront()

    # 评估初始种群
    fitnesses = list(map(toolbox.evaluate, pop))
    for ind, fit in zip(pop, fitnesses):
        ind.fitness.values = fit

    best_expR = -999
    history = []
    no_improve = 0

    for gen in range(n_gen):
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        for i in range(1, len(offspring), 2):
            if random.random() < WEIGHT_CXPB:
                offspring[i - 1:i + 1] = toolbox.mate(offspring[i - 1], offspring[i + 1] if i + 1 < len(offspring) else offspring[i - 1])
                del offspring[i - 1].fitness.values, offspring[i].fitness.values

        for i in range(len(offspring)):
            if random.random() < WEIGHT_MUTPB:
                offspring[i] = toolbox.mutate(offspring[i])[0]
                if offspring[i].fitness.valid:
                    del offspring[i].fitness.values

        invalid = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = list(map(toolbox.evaluate, invalid))
        for ind, fit in zip(invalid, fitnesses):
            ind.fitness.values = fit

        pop[:] = offspring
        hof.update(pop)

        gen_best = max(ind.fitness.values[0] for ind in pop)
        history.append({"gen": gen, "best_expR": round(gen_best, 4)})
        if verbose and gen % 5 == 0:
            print(f"  gen {gen}: best_expR={gen_best:.4f}")

        if gen_best > best_expR + 0.001:
            best_expR = gen_best
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= WEIGHT_EARLY_STOP:
                if verbose:
                    print(f"  早停：连续 {no_improve} 代无提升")
                break

    best = tools.selBest(pop, 1)[0]
    best_weights = _chromosome_to_weights(best)
    best_expR_final = best.fitness.values[0]
    best_winrate = best.fitness.values[1]

    # 稳健性检验
    robust_score = _robust_test(best, symbol, df_daily, tail=tail)

    result = {
        "symbol": symbol,
        "best_weights": best_weights,
        "best_expR": round(float(best_expR_final), 4),
        "best_winrate": round(float(best_winrate), 4),
        "robust_score": round(robust_score, 4),
        "history": history,
        "elapsed": 0,
        "tail": tail,
    }
    _save_weights(result)
    return result


def _robust_test(ind, symbol, df_daily, n_perturb=7, tail=None):
    """±20% 参数扰动稳健性检验。"""
    perturbed = list(ind)
    base_expR = _evaluate_weights(perturbed, symbol, df_daily, 0, 0, tail=tail)[0]
    results = [base_expR]
    for _ in range(n_perturb):
        perturbed = list(ind)
        for i in range(len(perturbed)):
            perturbed[i] *= (1.0 + random.uniform(-0.2, 0.2))
        expR, _, _ = _evaluate_weights(perturbed, symbol, df_daily, 0, 0, tail=tail)
        results.append(expR)
    mean_r = float(np.mean(results))
    return mean_r / max(abs(base_expR), 0.001) if base_expR != 0 else 0.0


def _save_weights(result):
    """保存优化结果到缓存文件。"""
    try:
        cache = {}
        if os.path.exists(WEIGHTS_FILE):
            cache = json.load(open(WEIGHTS_FILE, encoding="utf-8"))
        cache[result["symbol"]] = result
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_weights(symbol):
    """从缓存读取优化后的权重（供 pipeline 消费）。"""
    try:
        if os.path.exists(WEIGHTS_FILE):
            cache = json.load(open(WEIGHTS_FILE, encoding="utf-8"))
            return cache.get(symbol, {})
    except Exception:
        pass
    return {}


# ============================================================================
# 因子挖掘：从 8 策略候选池筛选有效因子组合
# ============================================================================

# 候选因子（来自 strategy_layer 的 8 策略 + 扩展）
CANDIDATE_FACTORS = [
    "ma_break",    # MA 突破
    "dma",         # 双均线
    "turtle",      # 海龟
    "donchian",    # 通道突破
    "pullback",    # 回踩
    "boll",        # 布林带
    "rsi",         # RSI
    "seasonal",    # 季节性
    "momentum",    # 动量（扩展）
    "vol_break",   # 波动率突破（扩展）
]

# 因子组合方式
COMBINE_METHODS = ["weighted_sum", "vote", "max_score"]

# GA 参数
FACTOR_POP = 80
FACTOR_GEN = 25
FACTOR_CXPB = 0.7
FACTOR_MUTPB = 0.4
FACTOR_EARLY_STOP = 6


def _chromosome_to_factor_config(ind):
    """染色体 → 因子配置。

    染色体布局（变长）：
    [0..N] 各候选因子权重（连续值，0=不选，>0.1=入选）
    [N] 组合方式（0=加权求和, 1=投票, 2=最大值）
    [N+1] 阈值倍率
    """
    n = len(CANDIDATE_FACTORS)
    weights = {}
    for i, name in enumerate(CANDIDATE_FACTORS):
        w = float(ind[i])
        if w > 0.1:
            weights[name] = round(w, 3)
    method_idx = int(ind[n]) % len(COMBINE_METHODS)
    method = COMBINE_METHODS[method_idx]
    threshold_mult = float(ind[n + 1])
    return {
        "selected_factors": list(weights.keys()),
        "factor_weights": weights,
        "combine_method": method,
        "threshold_mult": round(threshold_mult, 3),
    }


def _evaluate_factor(ind, symbol, df_daily, train_start, train_end):
    """评估因子挖掘染色体。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    fc = _chromosome_to_factor_config(ind)
    if len(fc["selected_factors"]) < 2:
        return -10.0, 0.0, 0

    cfg["ga_factor_config"] = fc

    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=300)
        expR = float(r.get("expR", 0))
        win_rate = float(r.get("win_rate", 0))
        n_trades = int(r.get("trades", 0))
        if n_trades < MIN_TRADES_WEIGHT:
            return -10.0, 0.0, 0
        win_rate = min(max(win_rate, 0.0), 1.0)
        # 因子数量惩罚：偏好简洁模型
        n_factors = len(fc["selected_factors"])
        penalty = max(0, (n_factors - 5) * 0.05)
        return expR - penalty, win_rate, n_trades
    except Exception:
        return -10.0, 0.0, 0


def mine_factors(symbol, df_daily=None, pop_size=FACTOR_POP, n_gen=FACTOR_GEN, verbose=True):
    """对指定品种运行 GA 因子挖掘。

    返回 dict:
      - best_factors: {selected_factors, factor_weights, combine_method, threshold_mult}
      - best_expR, best_calmar, n_trades
      - robust_score
      - history
    """
    if not _HAVE_DEAP:
        return {"error": "DEAP not installed", "symbol": symbol}

    if df_daily is None:
        df_daily = load_daily(symbol)
    if df_daily is None or len(df_daily) < 200:
        return {"error": "insufficient data", "symbol": symbol}

    n = len(df_daily)
    train_start = max(0, n - 400)
    train_end = n

    if verbose:
        print(f"[GA因子] {symbol}: pop={pop_size} gen={n_gen} candidates={len(CANDIDATE_FACTORS)}")

    n_factors = len(CANDIDATE_FACTORS)
    n_genes = n_factors + 2  # 因子权重 + 组合方式 + 阈值倍率

    if "FitnessFactor" in dir(creator):
        del creator.FitnessFactor
    if "IndividualFactor" in dir(creator):
        del creator.IndividualFactor
    creator.create("FitnessFactor", base.Fitness, weights=(1.0, 1.0))
    creator.create("IndividualFactor", list, fitness=creator.FitnessFactor)

    toolbox = base.Toolbox()
    for i, name in enumerate(CANDIDATE_FACTORS):
        toolbox.register(f"attr_f{i}", random.uniform, 0.0, 1.0)
    toolbox.register("attr_method", random.uniform, 0, len(COMBINE_METHODS))
    toolbox.register("attr_threshold", random.uniform, 0.5, 2.0)

    attrs = [getattr(toolbox, f"attr_f{i}") for i in range(n_factors)]
    attrs += [toolbox.attr_method, toolbox.attr_threshold]

    toolbox.register("individual", tools.initCycle, creator.IndividualFactor, tuple(attrs), n=1)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        expR, calmar, n_tr = _evaluate_factor(ind, symbol, df_daily, train_start, train_end)
        return (expR, calmar)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.5)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.1, indpb=0.4)
    toolbox.register("select", tools.selNSGA2)

    pop = toolbox.population(n=pop_size)
    hof = tools.ParetoFront()

    fitnesses = list(map(toolbox.evaluate, pop))
    for ind, fit in zip(pop, fitnesses):
        ind.fitness.values = fit

    best_expR = -999
    history = []
    no_improve = 0

    for gen in range(n_gen):
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        for i in range(1, len(offspring), 2):
            if random.random() < FACTOR_CXPB and i + 1 < len(offspring):
                a, b = offspring[i - 1], offspring[i + 1]
                offspring[i - 1:i + 1] = toolbox.mate(a, b)
                if offspring[i - 1].fitness.valid:
                    del offspring[i - 1].fitness.values
                if i < len(offspring) and offspring[i].fitness.valid:
                    del offspring[i].fitness.values

        for i in range(len(offspring)):
            if random.random() < FACTOR_MUTPB:
                offspring[i], = toolbox.mutate(offspring[i])
                if offspring[i].fitness.valid:
                    del offspring[i].fitness.values

        invalid = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = list(map(toolbox.evaluate, invalid))
        for ind, fit in zip(invalid, fitnesses):
            ind.fitness.values = fit

        pop[:] = offspring
        hof.update(pop)

        gen_best = max(ind.fitness.values[0] for ind in pop)
        history.append({"gen": gen, "best_expR": round(gen_best, 4)})
        if verbose and gen % 5 == 0:
            print(f"  gen {gen}: best_expR={gen_best:.4f}")

        if gen_best > best_expR + 0.001:
            best_expR = gen_best
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= FACTOR_EARLY_STOP:
                if verbose:
                    print(f"  早停：连续 {no_improve} 代无提升")
                break

    best = tools.selBest(pop, 1)[0]
    best_config = _chromosome_to_factor_config(best)
    best_expR_final = best.fitness.values[0]
    best_calmar = best.fitness.values[1]

    result = {
        "symbol": symbol,
        "best_factors": best_config,
        "best_expR": round(float(best_expR_final), 4),
        "best_calmar": round(float(best_calmar), 4),
        "history": history,
    }
    return result


def _fallback_weights(symbol):
    """无 DEAP 时的兜底。"""
    return {
        "symbol": symbol,
        "best_weights": {"base": BASE_WEIGHTS, "regime_adjust": {
            "趋势": {"T": 0, "F": 0}, "波动": {"T": 0, "F": 0}, "震荡": {"T": 0, "F": 0}}},
        "best_expR": 0.0,
        "best_calmar": 0.0,
        "robust_score": 1.0,
        "history": [],
        "elapsed": 0,
        "note": "DEAP not available, using default weights",
    }


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="GA 因子挖掘与权重优化")
    parser.add_argument("--mode", choices=["weight", "factor"], default="weight",
                        help="weight=权重优化, factor=因子挖掘")
    parser.add_argument("--symbol", required=True, help="品种代号")
    parser.add_argument("--pop", type=int, default=60, help="种群大小")
    parser.add_argument("--gen", type=int, default=20, help="迭代代数")
    args = parser.parse_args()

    if args.mode == "weight":
        result = optimize_weights(args.symbol, pop_size=args.pop, n_gen=args.gen)
        print(f"\n=== {args.symbol} 权重优化结果 ===")
        print(f"expR={result['best_expR']}, calmar={result['best_calmar']}")
        print(f"稳健性={result.get('robust_score', 'N/A')}")
        w = result["best_weights"]
        print(f"基础权重: T={w['base']['T']}, F={w['base']['F']}, C={w['base']['C']}")
        for reg, adj in w["regime_adjust"].items():
            print(f"  {reg}: T_adj={adj['T']}, F_adj={adj['F']}")
    else:
        result = mine_factors(args.symbol, pop_size=args.pop, n_gen=args.gen)
        print(f"\n=== {args.symbol} 因子挖掘结果 ===")
        print(f"expR={result['best_expR']}, calmar={result['best_calmar']}")
        fc = result["best_factors"]
        print(f"选中因子: {fc['selected_factors']}")
        print(f"因子权重: {fc['factor_weights']}")
        print(f"组合方式: {fc['combine_method']}, 阈值倍率: {fc['threshold_mult']}")


if __name__ == "__main__":
    main()
