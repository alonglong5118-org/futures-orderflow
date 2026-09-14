"""
ga_tpsl_optimizer.py — 止盈止损参数 GA 联合优化器（Phase 1 原型版）
=================================================================
Phase 1：2 参数（stop_atr_mult + rr_ratio）+ 单目标 + 全样本回测
用途：验证方法论可行性，跑通 DEAP + 回测引擎的最小闭环

用法:
    python3 ga_tpsl_optimizer.py --symbol jd --tail 500
    python3 ga_tpsl_optimizer.py --symbol jd --pop 30 --gen 20

后续升级方向（Phase 2）：
    - 扩展到 5 参数（加入尾仓 3 参数）
    - 多目标 NSGA-II（expR + 卡玛比率 + 胜率稳定性）
    - Walk-Forward 滚动窗口适应度
    - 纯 OOS 检验
    - 参数稳健性检验
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from deap import base, creator, tools

from four_dim_strategy import DEFAULT_CONFIG, walk_forward_backtest

# ============================================================================
# 配置
# ============================================================================

# 参数范围（基因 0: stop_atr_mult, 基因 1: rr_ratio）
PARAM_BOUNDS = [
    (0.8, 3.0),  # stop_atr_mult
    (1.2, 4.0),  # rr_ratio
]
PARAM_NAMES = ["stop_atr_mult", "rr_ratio"]

# GA 参数（Phase 1 用较小种群和代数，快速验证）
DEFAULT_POP_SIZE = 50
DEFAULT_GEN_COUNT = 30
CXPB = 0.9  # 交叉概率
MUTPB = 0.2  # 变异概率（每个基因）
SBX_ETA = 20  # SBX 分布指数
PM_ETA = 20  # 多项式变异分布指数

# 最低交易笔数（低于此值的解惩罚）
MIN_TRADES = 10

# 结果缓存（参数 → 回测结果），避免重复计算
_eval_cache = {}


def _cache_key(stop_mult, rr_ratio, symbol, tail):
    """生成缓存键：参数取 3 位小数，避免浮点误差导致缓存失效。"""
    return (round(stop_mult, 3), round(rr_ratio, 3), symbol, tail)


# ============================================================================
# 适应度函数
# ============================================================================


def _make_config(stop_mult, rr_ratio, symbol, base_cfg=DEFAULT_CONFIG):
    """根据参数生成分品种配置。"""
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("per_symbol_risk", {})
    cfg["per_symbol_risk"][symbol] = {
        "stop_atr_mult": round(stop_mult, 3),
        "rr_ratio": round(rr_ratio, 3),
    }
    return cfg


def evaluate(individual, symbol, tail=None):
    """评估个体：返回 (expR, ) —— Phase 1 单目标。

    注意：DEAP 要求返回 tuple（即使单目标）。
    """
    global _eval_cache

    stop_mult = individual[0]
    rr_ratio = individual[1]

    # 查缓存
    key = _cache_key(stop_mult, rr_ratio, symbol, tail)
    if key in _eval_cache:
        return _eval_cache[key]

    # 结构约束：rr_ratio 必须 >= 1.2（盈亏比下限）
    if rr_ratio < 1.2 or stop_mult < 0.8:
        result = (-10.0,)  # 不可行解惩罚
        _eval_cache[key] = result
        return result

    cfg = _make_config(stop_mult, rr_ratio, symbol)
    raw_result = walk_forward_backtest(symbol, cfg=cfg, tail=tail)

    trades = int(raw_result.get("trades", 0))
    expR = float(raw_result.get("expR") or 0.0)

    # 交易笔数不足 → 惩罚
    if trades < MIN_TRADES:
        penalty = (MIN_TRADES - trades) * 0.05
        fitness = (expR - penalty,)
    else:
        fitness = (expR,)

    _eval_cache[key] = fitness
    return fitness


# ============================================================================
# DEAP 初始化
# ============================================================================


def setup_deap(symbol, tail=None):
    """初始化 DEAP toolbox。"""
    # 单目标最大化
    if "FitnessMax" not in creator.__dict__:
        creator.create("FitnessMax", base.Fitness, weights=(1.0,))
    if "Individual" not in creator.__dict__:
        creator.create("Individual", list, fitness=creator.FitnessMax)

    toolbox = base.Toolbox()

    # 属性生成：在参数范围内均匀随机
    def attr_float(idx):
        low, high = PARAM_BOUNDS[idx]
        return random.uniform(low, high)

    toolbox.register("attr_stop", attr_float, 0)
    toolbox.register("attr_rr", attr_float, 1)

    # 个体结构：2 个实数基因
    toolbox.register("individual", tools.initCycle, creator.Individual, (toolbox.attr_stop, toolbox.attr_rr), n=1)

    # 种群
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    # 评估
    toolbox.register("evaluate", evaluate, symbol=symbol, tail=tail)

    # 交叉：模拟二进制交叉（SBX）
    toolbox.register(
        "mate",
        tools.cxSimulatedBinaryBounded,
        low=[b[0] for b in PARAM_BOUNDS],
        up=[b[1] for b in PARAM_BOUNDS],
        eta=SBX_ETA,
    )

    # 变异：多项式变异
    toolbox.register(
        "mutate",
        tools.mutPolynomialBounded,
        low=[b[0] for b in PARAM_BOUNDS],
        up=[b[1] for b in PARAM_BOUNDS],
        eta=PM_ETA,
        indpb=MUTPB,
    )

    # 选择：锦标赛选择
    toolbox.register("select", tools.selTournament, tournsize=3)

    return toolbox


# ============================================================================
# 主优化流程
# ============================================================================


def run_optimization(symbol, pop_size=DEFAULT_POP_SIZE, gen_count=DEFAULT_GEN_COUNT, tail=None, seed=42):
    """运行 GA 优化。

    返回:
        {
            "symbol": str,
            "baseline": {params, expR, trades, win_rate},
            "best": {params, expR, trades, win_rate},
            "history": [{"gen": int, "best": float, "avg": float}, ...],
            "final_population": [{"params": [...], "fitness": float}, ...],
            "runtime_sec": float,
        }
    """
    random.seed(seed)
    np.random.seed(seed)

    toolbox = setup_deap(symbol, tail=tail)

    # 基线（当前参数）
    baseline_cfg = copy.deepcopy(DEFAULT_CONFIG)
    baseline_result = walk_forward_backtest(symbol, cfg=baseline_cfg, tail=tail)
    baseline_params = {
        "stop_atr_mult": DEFAULT_CONFIG["risk_gate"]["stop_atr_mult"],
        "rr_ratio": DEFAULT_CONFIG["risk_gate"]["rr_ratio"],
    }
    # 如果有 per_symbol_risk 覆盖，用覆盖值
    psr = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(symbol, {})
    if "stop_atr_mult" in psr:
        baseline_params["stop_atr_mult"] = psr["stop_atr_mult"]
    if "rr_ratio" in psr:
        baseline_params["rr_ratio"] = psr["rr_ratio"]

    print(f"\n{'=' * 60}")
    print(f"📊 基线参数（{symbol}）:")
    print(f"   stop_atr_mult = {baseline_params['stop_atr_mult']}")
    print(f"   rr_ratio = {baseline_params['rr_ratio']}")
    print(f"   expR = {baseline_result.get('expR', 'N/A')}")
    print(f"   trades = {baseline_result.get('trades', 0)}")
    print(f"   win_rate = {baseline_result.get('win_rate', 'N/A')}")
    print(f"{'=' * 60}\n")

    # 初始化种群
    pop = toolbox.population(n=pop_size)

    # 初始评估
    print(f"🚀 开始 GA 优化：种群={pop_size}, 代数={gen_count}")
    print(f"   优化参数: {PARAM_NAMES}")
    print(f"   参数范围: {PARAM_BOUNDS}")
    print()

    start_time = time.time()
    fitnesses = list(map(toolbox.evaluate, pop))
    for ind, fit in zip(pop, fitnesses):
        ind.fitness.values = fit

    history = []
    best_expR = max(f[0] for f in fitnesses)
    avg_expR = sum(f[0] for f in fitnesses) / len(fitnesses)
    print(f"  Gen 0: best={best_expR:.4f}, avg={avg_expR:.4f}")
    history.append({"gen": 0, "best": best_expR, "avg": avg_expR})

    # 进化主循环
    for gen in range(1, gen_count + 1):
        gen_start = time.time()

        # 选择
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        # 交叉
        for child1, child2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < CXPB:
                toolbox.mate(child1, child2)
                del child1.fitness.values
                del child2.fitness.values

        # 变异
        for mutant in offspring:
            if random.random() < MUTPB:
                toolbox.mutate(mutant)
                del mutant.fitness.values

        # 重新评估无效个体
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # 替换种群
        pop[:] = offspring

        # 统计
        fits = [ind.fitness.values[0] for ind in pop]
        best_expR = max(fits)
        avg_expR = sum(fits) / len(fits)
        gen_time = time.time() - gen_start
        print(f"  Gen {gen:2d}: best={best_expR:.4f}, avg={avg_expR:.4f}  ({gen_time:.1f}s)")
        history.append({"gen": gen, "best": best_expR, "avg": avg_expR})

    runtime = time.time() - start_time
    print(f"\n✅ 优化完成，用时 {runtime:.1f} 秒")

    # 选出最优个体
    best_ind = tools.selBest(pop, 1)[0]
    best_params = {PARAM_NAMES[i]: round(best_ind[i], 3) for i in range(len(PARAM_NAMES))}
    best_result = walk_forward_backtest(symbol, cfg=_make_config(best_ind[0], best_ind[1], symbol), tail=tail)

    # 最终种群（前 20 名）
    top_individuals = tools.selBest(pop, min(20, len(pop)))
    final_pop = []
    for ind in top_individuals:
        final_pop.append(
            {
                "params": {PARAM_NAMES[i]: round(ind[i], 3) for i in range(len(PARAM_NAMES))},
                "fitness": round(ind.fitness.values[0], 4),
            }
        )

    print("\n🏆 最优解:")
    for k, v in best_params.items():
        print(f"   {k} = {v}")
    print(f"   expR = {best_result.get('expR', 'N/A')}")
    print(f"   trades = {best_result.get('trades', 0)}")
    print(f"   win_rate = {best_result.get('win_rate', 'N/A')}")

    improvement = best_result.get("expR", 0) - baseline_result.get("expR", 0)
    improvement_pct = (improvement / baseline_result.get("expR", 1)) * 100 if baseline_result.get("expR", 0) > 0 else 0
    print(
        f"\n📈 相比基线: {'+' if improvement >= 0 else ''}{improvement:.4f} "
        f"({'+' if improvement_pct >= 0 else ''}{improvement_pct:.1f}%)"
    )

    return {
        "symbol": symbol,
        "baseline": {
            "params": baseline_params,
            "expR": baseline_result.get("expR"),
            "trades": baseline_result.get("trades"),
            "win_rate": baseline_result.get("win_rate"),
        },
        "best": {
            "params": best_params,
            "expR": best_result.get("expR"),
            "trades": best_result.get("trades"),
            "win_rate": best_result.get("win_rate"),
            "improvement": round(improvement, 4),
            "improvement_pct": round(improvement_pct, 2),
        },
        "history": history,
        "final_population": final_pop,
        "runtime_sec": round(runtime, 1),
        "pop_size": pop_size,
        "gen_count": gen_count,
    }


# ============================================================================
# 网格搜索对比（验证 GA 是否有效）
# ============================================================================


def grid_search_baseline(symbol, tail=None):
    """网格搜索作为基线对比：和 four_dim_calibrate.py 类似的离散扫描。"""
    stop_cands = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
    rr_cands = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

    best_expR = -999
    best_params = None
    best_result = None
    results = []

    print(f"\n🔍 网格搜索基线对比：{len(stop_cands)} × {len(rr_cands)} = {len(stop_cands) * len(rr_cands)} 组")

    for s in stop_cands:
        for r in rr_cands:
            cfg = _make_config(s, r, symbol)
            result = walk_forward_backtest(symbol, cfg=cfg, tail=tail)
            expR = float(result.get("expR") or 0.0)
            results.append({"stop": s, "rr": r, "expR": expR, "trades": result.get("trades", 0)})
            if expR > best_expR and result.get("trades", 0) >= MIN_TRADES:
                best_expR = expR
                best_params = {"stop_atr_mult": s, "rr_ratio": r}
                best_result = result

    print(
        f"   网格最优: stop={best_params['stop_atr_mult']}, "
        f"rr={best_params['rr_ratio']}, expR={best_result.get('expR')}"
    )
    return {"best_params": best_params, "best_result": best_result, "all_results": results}


# ============================================================================
# 输出结果
# ============================================================================


def save_results(result, grid_result=None, output_dir=None):
    """保存优化结果到 JSON。"""
    if output_dir is None:
        output_dir = HERE

    symbol = result["symbol"]
    filename = f"ga_tpsl_{symbol}_result.json"
    filepath = os.path.join(output_dir, filename)

    output = copy.deepcopy(result)
    if grid_result:
        output["grid_search"] = {
            "best_params": grid_result["best_params"],
            "best_expR": grid_result["best_result"].get("expR"),
        }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n💾 结果已保存: {filepath}")
    return filepath


# ============================================================================
# CLI
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="止盈止损参数 GA 联合优化器（Phase 1 原型）")
    parser.add_argument("--symbol", type=str, default="jd", help="品种代码")
    parser.add_argument("--tail", type=int, default=None, help="只用最后 N 根K线（加速测试）")
    parser.add_argument("--pop", type=int, default=DEFAULT_POP_SIZE, help="种群大小")
    parser.add_argument("--gen", type=int, default=DEFAULT_GEN_COUNT, help="进化代数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--grid", action="store_true", help="同时运行网格搜索做对比")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    print("=" * 60)
    print("🧬 止盈止损参数 GA 联合优化器（Phase 1 原型）")
    print("=" * 60)

    # 运行 GA 优化
    result = run_optimization(
        symbol=args.symbol,
        pop_size=args.pop,
        gen_count=args.gen,
        tail=args.tail,
        seed=args.seed,
    )

    # 网格搜索对比
    grid_result = None
    if args.grid:
        grid_result = grid_search_baseline(args.symbol, tail=args.tail)

        # 对比
        ga_best = result["best"]["expR"]
        grid_best = grid_result["best_result"].get("expR", 0)
        diff = ga_best - grid_best
        diff_pct = (diff / grid_best) * 100 if grid_best > 0 else 0
        print("\n📊 GA vs 网格搜索对比:")
        print(f"   网格搜索最优 expR = {grid_best:.4f}")
        print(f"   GA 最优 expR     = {ga_best:.4f}")
        print(f"   GA 优势 = {'+' if diff >= 0 else ''}{diff:.4f} ({'+' if diff_pct >= 0 else ''}{diff_pct:.1f}%)")

    # 保存结果
    save_results(result, grid_result, args.output)

    return result


if __name__ == "__main__":
    main()
