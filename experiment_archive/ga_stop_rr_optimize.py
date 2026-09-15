"""
板块级止损止盈 GA 优化 + OOS 验证
- 优化参数：stop_atr_mult + rr_ratio（2个参数/板块）
- 对比基准：当前 per_symbol_risk（逐品种校准）
- 回答：止损止盈参数的 GA 优化是否比权重优化更有效？
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

SECTORS = {
    "化工": ["PF", "PX", "SA", "SH", "TA", "eb", "l", "pp", "ru", "sp", "v", "ma", "fg", "UR", "nr", "bu"],
    "农产品": ["AP", "CF", "CJ", "CS", "SR", "a", "c", "jd", "lh", "m", "p", "y", "rm", "OI", "PK"],
    "有色": ["al", "ao", "cu", "ni", "pb", "ss", "zn", "si", "bc", "br"],
    "黑系": ["i", "j", "jm", "rb", "hc", "sf", "sm", "ss", "fg"],
    "能源": ["sc", "lu", "bu", "fu", "pg", "eb", "l", "pp", "v", "ru", "nr"],
}

# GA 参数
POP_SIZE = 40
N_GEN = 15
MIN_TRADES_PER_SYMBOL = 8

# 参数搜索范围
STOP_RANGE = (0.8, 3.0)  # stop_atr_mult
RR_RANGE = (1.0, 4.0)  # rr_ratio


def get_sector_symbols(sector):
    syms = []
    for s in SECTORS.get(sector, []):
        if s in SYMBOLS:
            syms.append(s)
    return syms


def make_config_with_stop_rr(sector_symbols, stop_atr_mult, rr_ratio):
    """生成配置：对板块内所有品种统一设置 stop/rr"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    # 清除 per_symbol_risk 中的 stop/rr，用统一值
    cfg["risk_gate"]["stop_atr_mult"] = stop_atr_mult
    cfg["risk_gate"]["rr_ratio"] = rr_ratio
    # 清空逐品种覆盖，确保用全局值
    for sym in sector_symbols:
        if sym in cfg["per_symbol_risk"]:
            if "stop_atr_mult" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["stop_atr_mult"]
            if "rr_ratio" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["rr_ratio"]
    return cfg


def evaluate_sector(sector_symbols, cfg, df_dict):
    """评估板块整体表现：平均 expR"""
    expRs = []
    total_trades = 0
    n_valid = 0
    for sym in sector_symbols:
        df = df_dict.get(sym)
        if df is None:
            continue
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=300, min_bars=60, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES_PER_SYMBOL:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
                n_valid += 1
        except Exception:
            pass

    if not expRs or n_valid < 3:
        return -5.0, 0, n_valid

    avg_expR = float(np.mean(expRs))
    return avg_expR, total_trades, n_valid


def run_ga_stop_rr(sector, df_dict):
    """GA 优化板块级 stop_atr_mult + rr_ratio"""
    if not _HAVE_DEAP:
        return None

    syms = get_sector_symbols(sector)
    valid_syms = [s for s in syms if s in df_dict]

    if len(valid_syms) < 3:
        return None

    if not hasattr(creator, "StopRRInd"):
        creator.create("StopRRFitness", base.Fitness, weights=(1.0,))
        creator.create("StopRRInd", list, fitness=creator.StopRRFitness)

    toolbox = base.Toolbox()
    toolbox.register("attr_stop", random.uniform, *STOP_RANGE)
    toolbox.register("attr_rr", random.uniform, *RR_RANGE)
    toolbox.register("individual", tools.initCycle, creator.StopRRInd, (toolbox.attr_stop, toolbox.attr_rr), n=1)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        stop_atr, rr = ind[0], ind[1]
        cfg = make_config_with_stop_rr(valid_syms, stop_atr, rr)
        avg_expR, _, n_valid = evaluate_sector(valid_syms, cfg, df_dict)
        # 惩罚有效品种太少
        if n_valid < 3:
            avg_expR -= 1.0
        return (avg_expR,)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.15, indpb=0.4)
    toolbox.register("select", tools.selTournament, tournsize=3)

    # 边界约束
    def _check_bounds(ind):
        ind[0] = max(STOP_RANGE[0], min(STOP_RANGE[1], ind[0]))
        ind[1] = max(RR_RANGE[0], min(RR_RANGE[1], ind[1]))
        return ind

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
                _check_bounds(offspring[i - 1])
                _check_bounds(offspring[i])
                del offspring[i - 1].fitness.values, offspring[i].fitness.values

        for ind in offspring:
            if random.random() < 0.3:
                toolbox.mutate(ind)
                _check_bounds(ind)
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
        if stagnation >= 6:
            break

    best = hof[0]
    return {
        "stop_atr_mult": round(best[0], 3),
        "rr_ratio": round(best[1], 3),
        "train_expR": best_fitness,
    }


def baseline_current(sector, df_dict):
    """当前逐品种校准配置的表现（baseline）"""
    syms = get_sector_symbols(sector)
    valid_syms = [s for s in syms if s in df_dict]

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    # 使用默认的 per_symbol_risk（逐品种校准）
    avg_expR, total_trades, n_valid = evaluate_sector(valid_syms, cfg, df_dict)

    return {
        "avg_expR": avg_expR,
        "total_trades": total_trades,
        "n_valid": n_valid,
    }


def main():
    sectors = ["化工", "农产品", "有色", "黑系", "能源"]
    all_results = {}

    for sector in sectors:
        print(f"\n{'=' * 60}")
        print(f"【{sector}】 止损止盈 GA 优化 + OOS 验证")
        print(f"{'=' * 60}")

        syms = get_sector_symbols(sector)

        # 加载数据
        df_dict = {}
        for sym in syms:
            df = load_daily(sym)
            if df is not None and len(df) >= 200:
                df_dict[sym] = df

        if len(df_dict) < 3:
            print(f"  有效品种不足（{len(df_dict)}个），跳过")
            continue

        print(f"  有效品种: {len(df_dict)} 个")

        # 全样本 baseline（当前逐品种校准）
        bl_all = baseline_current(sector, df_dict)
        print(
            f"  全样本 基线(逐品种校准) expR: {bl_all['avg_expR']:+.4f} ({bl_all['total_trades']}笔, {bl_all['n_valid']}有效品种)"
        )

        # 全样本 GA 优化
        ga_all = run_ga_stop_rr(sector, df_dict)
        if ga_all is None:
            print("  GA 优化失败")
            continue

        cfg_ga = make_config_with_stop_rr(list(df_dict.keys()), ga_all["stop_atr_mult"], ga_all["rr_ratio"])
        ga_all_eval = evaluate_sector(list(df_dict.keys()), cfg_ga, df_dict)
        print(
            f"  全样本 GA 最优: stop={ga_all['stop_atr_mult']:.3f}, rr={ga_all['rr_ratio']:.3f}, "
            f"expR={ga_all_eval[0]:+.4f} ({ga_all_eval[1]}笔)"
        )
        print(f"  全样本 提升: {ga_all_eval[0] - bl_all['avg_expR']:+.4f}")

        # ========================================
        # OOS 验证：75/25 切分
        # ========================================
        train_dict = {}
        oos_dict = {}
        for sym, df in df_dict.items():
            n = len(df)
            split = int(n * 0.75)
            train_dict[sym] = df.iloc[:split]
            oos_dict[sym] = df.iloc[split:]

        # 训练集 baseline
        bl_train = baseline_current(sector, train_dict)
        bl_oos = baseline_current(sector, oos_dict)
        print(f"\n  训练集 基线 expR: {bl_train['avg_expR']:+.4f}")
        print(f"  OOS集   基线 expR: {bl_oos['avg_expR']:+.4f}")

        # 训练集 GA 优化
        ga_train = run_ga_stop_rr(sector, train_dict)
        if ga_train is None:
            print("  训练集 GA 优化失败")
            continue

        cfg_ga_train = make_config_with_stop_rr(
            list(train_dict.keys()), ga_train["stop_atr_mult"], ga_train["rr_ratio"]
        )
        ga_train_eval = evaluate_sector(list(train_dict.keys()), cfg_ga_train, train_dict)
        ga_oos_eval = evaluate_sector(list(oos_dict.keys()), cfg_ga_train, oos_dict)

        train_gain = ga_train_eval[0] - bl_train["avg_expR"]
        oos_gain = ga_oos_eval[0] - bl_oos["avg_expR"]
        ofc = oos_gain / train_gain if train_gain > 0.001 else float("inf")

        print(
            f"  训练集 GA 最优: stop={ga_train['stop_atr_mult']:.3f}, rr={ga_train['rr_ratio']:.3f}, "
            f"expR={ga_train_eval[0]:+.4f}"
        )
        print(f"  训练集 GA 提升: {train_gain:+.4f}")
        print(f"  OOS集   GA expR: {ga_oos_eval[0]:+.4f}")
        print(f"  OOS集   GA 提升: {oos_gain:+.4f}")
        print(f"  过拟合系数 OFC: {ofc:.3f}")

        if oos_gain > 0.02 and ofc > 0.3:
            verdict = "✅ OOS 有效"
        elif oos_gain > 0:
            verdict = "⚠️ OOS 微弱有效"
        else:
            verdict = "❌ OOS 失效"
        print(f"  结论: {verdict}")

        all_results[sector] = {
            "n_symbols": len(df_dict),
            "baseline_all": bl_all,
            "ga_all": {
                "params": ga_all,
                "eval": {"avg_expR": ga_all_eval[0], "trades": ga_all_eval[1], "n_valid": ga_all_eval[2]},
            },
            "baseline_train": bl_train,
            "baseline_oos": bl_oos,
            "ga_train": {"params": ga_train, "eval": {"avg_expR": ga_train_eval[0], "trades": ga_train_eval[1]}},
            "ga_oos": {"avg_expR": ga_oos_eval[0], "trades": ga_oos_eval[1], "n_valid": ga_oos_eval[2]},
            "train_gain": train_gain,
            "oos_gain": oos_gain,
            "overfit_coef": ofc,
            "verdict": verdict,
        }

    # 汇总
    print(f"\n{'=' * 60}")
    print("全板块汇总")
    print(f"{'=' * 60}")
    print(f"{'板块':<8}{'品种数':>8}{'基线OOS':>10}{'GA_OOS':>10}{'ΔOOS':>10}{'OFC':>8}{'结论':>14}")
    print("-" * 70)
    for sector in sectors:
        if sector not in all_results:
            continue
        r = all_results[sector]
        print(
            f"{sector:<8}{r['n_symbols']:>8}"
            f"{r['baseline_oos']['avg_expR']:>+10.4f}"
            f"{r['ga_oos']['avg_expR']:>+10.4f}"
            f"{r['oos_gain']:>+10.4f}"
            f"{r['overfit_coef']:>8.2f}"
            f"{r['verdict']:>14}"
        )

    # 保存
    out_path = "logs/stop_rr_ga_optimization.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
