#!/usr/bin/env python3
"""
ga_vvol_oos.py — 6+V_vol 7 因子（6+V_vol）板块级 GA 优化 + OOS 走步法验证

对比：基准 vs 6因子稳健版 vs 7因子（6+V_vol）稳健版
每个板块：训练集 GA 搜索 → 验证集 OOS 检验
"""

import argparse
import copy
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
from deap import algorithms, base, creator, tools

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, walk_forward_backtest

# ===== 板块定义 =====
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)

# 过滤掉品种太少的板块
VALID_GROUPS = [g for g, s in GROUPS.items() if len(s) >= 3]
VALID_GROUPS = ["化工", "农产品", "有色", "黑系", "能源"]  # 与之前一致

# ===== 6+V_vol 7 因子列表 =====
SF7 = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C", "V_vol"]
SF6 = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]

# ===== GA 参数（稳健版） =====
POP = 15
GEN = 10
CXPB = 0.6
MUTPB = 0.3
MAX_W = 0.35  # 单因子权重上限
ENTROPY_LAMBDA = 0.10  # 熵惩罚系数
TRAIN_BARS = 300
TEST_BARS = 200
WINDOW = 200
MIN_BARS = 40
MIN_TRADES_PER_SYMBOL = 5


def load_group_data(group_name, tail=None):
    """加载板块内所有品种的日线数据。"""
    from four_dim_strategy import load_daily

    syms = GROUPS.get(group_name, [])
    data = {}
    for sym in syms:
        try:
            df = load_daily(sym)
            if df is not None and len(df) > MIN_BARS + WINDOW:
                if tail and len(df) > tail:
                    df = df.iloc[-tail:]
                data[sym] = df
        except Exception:
            pass
    return data


def _normalize_weights(ind):
    """归一化权重，使总和为 1。"""
    s = sum(abs(x) for x in ind)
    if s < 1e-6:
        return [1.0 / len(ind)] * len(ind)
    return [x / s for x in ind]


def _weight_entropy(weights):
    """计算权重熵（归一化后），越大越分散。"""
    ent = 0.0
    for w in weights:
        if w > 1e-6:
            ent -= w * np.log(w)
    max_ent = np.log(len(weights))
    return ent / max_ent if max_ent > 0 else 0.0


def ind_to_weights(ind, factor_names):
    """染色体 → 权重字典。"""
    norm = _normalize_weights(ind)
    return {name: round(w, 4) for name, w in zip(factor_names, norm)}


def evaluate(ind, group_data, factor_names, cfg_template=None, tail=None):
    """评估一组权重：板块内所有品种的平均 expR（带熵惩罚）。"""
    w = ind_to_weights(ind, factor_names)
    cfg = copy.deepcopy(cfg_template or DEFAULT_CONFIG)
    cfg["subfactor_weights"] = w

    expRs = []
    total_trades = 0
    for sym, df in group_data.items():
        try:
            df_use = df
            if tail and len(df) > tail:
                df_use = df.iloc[-tail:]
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW, min_bars=MIN_BARS, df_in=df_use)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES_PER_SYMBOL:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass

    if not expRs:
        return (-5.0,)

    avg_expR = float(np.mean(expRs))

    # 惩罚有效品种太少
    n_valid = len(expRs)
    if n_valid < 3:
        avg_expR -= 0.5 * (3 - n_valid)

    # 熵惩罚（鼓励分散）
    weights = list(w.values())
    ent = _weight_entropy(weights)
    avg_expR += ENTROPY_LAMBDA * ent  # 熵越高奖励越多

    return (avg_expR,)


def run_ga(group_data, factor_names, cfg_template=None, tail=None, pop_size=15, n_gen=10):
    """运行 GA 优化，返回最优权重。"""
    n_factor = len(factor_names)

    # 注册
    if not hasattr(creator, "FitnessMax"):
        creator.create("FitnessMax", base.Fitness, weights=(1.0,))
        creator.create("Individual", list, fitness=creator.FitnessMax)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.random)
    toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_float, n=n_factor)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        return evaluate(ind, group_data, factor_names, cfg_template, tail)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(1)

    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", np.mean)
    stats.register("max", np.max)

    for gen in range(n_gen):
        offspring = algorithms.varAnd(pop, toolbox, CXPB, MUTPB)

        # 权重约束：单因子不超过 MAX_W
        for ind in offspring:
            for i in range(len(ind)):
                if ind[i] > MAX_W * len(ind):
                    ind[i] = MAX_W * len(ind)
                if ind[i] < 0:
                    ind[i] = 0.01

        fits = toolbox.map(toolbox.evaluate, offspring)
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit

        pop = toolbox.select(pop + offspring, pop_size)
        hof.update(pop)

        best_fit = hof[0].fitness.values[0]
        print(f"    第 {gen + 1}/{n_gen} 代: 最佳 expR={best_fit:.4f}", end="\r", flush=True)

    print()

    best_ind = hof[0]
    best_weights = ind_to_weights(best_ind, factor_names)
    best_raw_fit = best_ind.fitness.values[0]

    # 重新计算纯 expR（不带熵惩罚）
    pure_expR = 0.0
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = best_weights
    expRs = []
    for sym, df in group_data.items():
        try:
            df_use = df
            if tail and len(df) > tail:
                df_use = df.iloc[-tail:]
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW, min_bars=MIN_BARS, df_in=df_use)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES_PER_SYMBOL:
                expRs.append(float(r.get("expR", 0)))
        except Exception:
            pass
    if expRs:
        pure_expR = float(np.mean(expRs))

    return {
        "weights": best_weights,
        "fitness_with_penalty": round(best_raw_fit, 4),
        "pure_expR": round(pure_expR, 4),
        "entropy": round(_weight_entropy(list(best_weights.values())), 3),
        "n_valid": len(expRs),
    }


def oos_evaluate(weights, group_data, train_bars, test_bars):
    """OOS 验证：在验证集上评估权重表现。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = weights

    # 截取验证集（后 test_bars 根）
    expRs = []
    trades_list = []
    for sym, df in group_data.items():
        if len(df) < train_bars + test_bars:
            continue
        try:
            df_test = df.iloc[train_bars : train_bars + test_bars]
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW, min_bars=MIN_BARS, df_in=df_test)
            nt = int(r.get("trades", 0))
            if nt >= 2:
                expRs.append(float(r.get("expR", 0)))
                trades_list.append(nt)
        except Exception:
            pass

    if not expRs:
        return {"expR": -5.0, "trades": 0, "n_valid": 0}

    return {
        "expR": round(float(np.mean(expRs)), 4),
        "trades": int(np.sum(trades_list)),
        "n_valid": len(expRs),
    }


def baseline_oos(group_data, train_bars, test_bars):
    """基准 OOS（默认权重）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.pop("subfactor_weights", None)

    expRs = []
    trades_list = []
    for sym, df in group_data.items():
        if len(df) < train_bars + test_bars:
            continue
        try:
            df_test = df.iloc[train_bars : train_bars + test_bars]
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW, min_bars=MIN_BARS, df_in=df_test)
            nt = int(r.get("trades", 0))
            if nt >= 2:
                expRs.append(float(r.get("expR", 0)))
                trades_list.append(nt)
        except Exception:
            pass

    if not expRs:
        return {"expR": -5.0, "trades": 0, "n_valid": 0}

    return {
        "expR": round(float(np.mean(expRs)), 4),
        "trades": int(np.sum(trades_list)),
        "n_valid": len(expRs),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", type=str, default=None, help="单个板块")
    parser.add_argument("--all", action="store_true", help="所有板块")
    parser.add_argument("--pop", type=int, default=15)
    parser.add_argument("--gen", type=int, default=10)
    parser.add_argument("--train", type=int, default=TRAIN_BARS, help="训练集 bar 数")
    parser.add_argument("--test", type=int, default=TEST_BARS, help="验证集 bar 数")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    pop_size = args.pop
    gen_count = args.gen

    sectors = []
    if args.all:
        sectors = VALID_GROUPS
    elif args.sector:
        sectors = [args.sector]
    else:
        sectors = ["化工"]

    print("=== 6+V_vol 7 因子 GA + OOS 验证 ===")
    print(f"因子: {', '.join(SF7)}")
    print(f"板块: {', '.join(sectors)}")
    print(f"训练集: {args.train} bar, 验证集: {args.test} bar")
    print(f"GA: pop={POP}, gen={GEN}, max_w={MAX_W}, entropy_lambda={ENTROPY_LAMBDA}")
    print()

    results = {}
    for sector in sectors:
        print(f"\n【{sector}】")
        data = load_group_data(sector)
        n_syms = len(data)
        print(f"  有效品种: {n_syms}")

        # 1. 基准
        print("  基准 OOS...", end=" ", flush=True)
        base_oos = baseline_oos(data, args.train, args.test)
        print(f"expR={base_oos['expR']:+.4f}  交易={base_oos['trades']}")

        # 2. 6 因子 GA（训练集）
        print("  6 因子 GA 训练...")
        train_data = {sym: df.iloc[: args.train] for sym, df in data.items() if len(df) >= args.train}
        ga6_result = run_ga(train_data, SF6, pop_size=pop_size, n_gen=gen_count)
        print(f"    最优权重: {ga6_result['weights']}")
        print(f"    训练集 expR: {ga6_result['pure_expR']:+.4f}, 熵: {ga6_result['entropy']:.3f}")

        # 3. 6 因子 OOS
        print("  6 因子 OOS 验证...", end=" ", flush=True)
        ga6_oos = oos_evaluate(ga6_result["weights"], data, args.train, args.test)
        print(f"expR={ga6_oos['expR']:+.4f}  交易={ga6_oos['trades']}")

        # 4. 6+V_vol 7 因子 GA（训练集）
        print("  6+V_vol 7 因子 GA 训练...")
        ga7_result = run_ga(train_data, SF7, pop_size=pop_size, n_gen=gen_count)
        print(f"    最优权重: {ga7_result['weights']}")
        print(f"    训练集 expR: {ga7_result['pure_expR']:+.4f}, 熵: {ga7_result['entropy']:.3f}")
        sr_w = ga7_result["weights"].get("V_vol", 0)
        print(f"    V_vol 权重: {sr_w:.4f} ({sr_w * 100:.1f}%)")

        # 5. 6+V_vol 7 因子 OOS
        print("  6+V_vol 7 因子 OOS 验证...", end=" ", flush=True)
        ga7_oos = oos_evaluate(ga7_result["weights"], data, args.train, args.test)
        print(f"expR={ga7_oos['expR']:+.4f}  交易={ga7_oos['trades']}")

        # 计算增量
        train_gain_7 = ga7_result["pure_expR"] - base_oos["expR"]  # 近似
        train_gain_6 = ga6_result["pure_expR"] - base_oos["expR"]
        oos_gain_7 = ga7_oos["expR"] - base_oos["expR"]
        oos_gain_6 = ga6_oos["expR"] - base_oos["expR"]
        overfit_7 = oos_gain_7 / (train_gain_7 + 1e-8) if train_gain_7 > 0 else -999
        overfit_6 = oos_gain_6 / (train_gain_6 + 1e-8) if train_gain_6 > 0 else -999

        # V_vol 增量
        sr_delta_oos = ga7_oos["expR"] - ga6_oos["expR"]

        results[sector] = {
            "n_symbols": n_syms,
            "baseline": base_oos,
            "ga6_train": ga6_result,
            "ga6_oos": ga6_oos,
            "ga7_train": ga7_result,
            "ga7_oos": ga7_oos,
            "oos_gain_6": round(oos_gain_6, 4),
            "oos_gain_7": round(oos_gain_7, 4),
            "overfit_6": round(overfit_6, 3),
            "overfit_7": round(overfit_7, 3),
            "sr_delta_oos": round(sr_delta_oos, 4),
            "sr_weight": round(sr_w, 4),
        }

        print(f"\n  对比: 基准={base_oos['expR']:+.4f} → 6因子={ga6_oos['expR']:+.4f} → 7因子={ga7_oos['expR']:+.4f}")
        print(
            f"  V_vol 增量: {sr_delta_oos:+.4f} ({'+' if sr_delta_oos > 0 else ''}{sr_delta_oos / (abs(ga6_oos['expR']) + 1e-8) * 100:.1f}%)"
        )

    # 汇总
    print("\n" + "=" * 80)
    print("汇总：OOS expR 对比")
    print("=" * 80)
    print(f"{'板块':<8}{'基准':>10}{'6因子':>10}{'7因子':>10}{'6因子提升':>12}{'7因子提升':>12}{'V_vol增量':>10}")
    for sector, r in results.items():
        b = r["baseline"]["expR"]
        g6 = r["ga6_oos"]["expR"]
        g7 = r["ga7_oos"]["expR"]
        d6 = r["oos_gain_6"]
        d7 = r["oos_gain_7"]
        ds = r["sr_delta_oos"]
        print(f"{sector:<8}{b:>+10.4f}{g6:>+10.4f}{g7:>+10.4f}{d6:>+12.4f}{d7:>+12.4f}{ds:>+10.4f}")

    if args.save:
        out_file = os.path.join(HERE, "logs", "ga_vvol_oos.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "params": {
                        "train_bars": args.train,
                        "test_bars": args.test,
                        "pop": pop_size,
                        "gen": gen_count,
                        "max_w": MAX_W,
                        "entropy_lambda": ENTROPY_LAMBDA,
                        "factors_7": SF7,
                        "factors_6": SF6,
                    },
                    "results": results,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n结果已保存: {out_file}")


if __name__ == "__main__":
    main()
