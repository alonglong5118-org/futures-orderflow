"""板块差异化策略探索 v2

标准 300 训练 / 200 验证 分割。
每个板块跑 4 种策略原型，分析各自最优的原型类型。
最后评估：差异化选择 vs 统一策略 的 OOS 表现对比。

用训练集特征来做差异化选择（避免用验证集表现做选择导致过拟合）：
- 因子集中度（训练集上单因子最大权重）→ 高集中度板块用 aggressive
- 因子多样性 → 高多样性板块用 conservative
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
from ga_group_six_factor import load_group_data

# ===== 策略原型定义 =====
ARCHETYPES = {
    "conservative": {"n_factor": 6, "max_w": 0.30, "entropy_lambda": 0.20, "label": "保守分散型"},
    "balanced":     {"n_factor": 6, "max_w": 0.40, "entropy_lambda": 0.10, "label": "均衡稳健型"},
    "aggressive":   {"n_factor": 6, "max_w": 0.60, "entropy_lambda": 0.03, "label": "集中进攻型"},
    "focused":      {"n_factor": 5, "max_w": 0.45, "entropy_lambda": 0.08, "label": "精简聚焦型"},
}

# ===== GA 参数 =====
POP = 15
GEN = 10
CXPB = 0.6
MUTPB = 0.3
TRAIN_BARS = 300
TEST_BARS = 200
WINDOW = 200
MIN_BARS = 40
MIN_TRADES = 5

GROUPS = ["化工", "农产品", "有色", "黑系", "能源"]
OUTFILE = os.path.join(HERE, "logs", "ga_sector_diff_v2.json")

SF6 = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]
SF5 = ["T_trend", "T_mean", "F_basis", "F_seasonal", "C"]


def get_factor_names(n_factor):
    return SF6 if n_factor == 6 else SF5


def ind_to_weights(ind, n_factor):
    names = get_factor_names(n_factor)
    s = sum(max(0, x) for x in ind)
    if s < 1e-6:
        return {n: 1.0/len(names) for n in names}
    norm = [max(0, x) / s for x in ind]
    return {name: round(w, 4) for name, w in zip(names, norm)}


def weight_entropy(weights_dict):
    w = np.array([max(v, 1e-6) for v in weights_dict.values()])
    w = w / w.sum()
    entropy = -np.sum(w * np.log(w))
    max_entropy = np.log(len(w))
    return float(entropy / max_entropy)


def make_sf6_weights(w):
    if "T_seasonal" in w:
        return w
    return {
        "T_trend": w.get("T_trend", 0),
        "T_mean": w.get("T_mean", 0),
        "T_seasonal": 0.0,
        "F_basis": w.get("F_basis", 0),
        "F_seasonal": w.get("F_seasonal", 0),
        "C": w.get("C", 0),
    }


def eval_weights(data, weights=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if weights:
        cfg["subfactor_weights"] = make_sf6_weights(weights)

    expRs = []
    total_trades = 0
    for sym, df in sorted(data.items()):
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


def ga_optimize(train_data, archetype_name, archetype_cfg):
    n_factor = archetype_cfg["n_factor"]
    max_w = archetype_cfg["max_w"]
    entropy_lambda = archetype_cfg["entropy_lambda"]
    n_vars = n_factor

    for _name in ["FDiff2Fit", "FDiff2Ind"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FDiff2Fit", base.Fitness, weights=(1.0,))
    creator.create("FDiff2Ind", list, fitness=creator.FDiff2Fit)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.FDiff2Ind,
                     toolbox.attr_float, n=n_vars)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _evaluate(ind):
        w = ind_to_weights(ind, n_factor)
        cur_max = max(w.values())
        penalty = max(0, cur_max - max_w) * 5.0
        ent = weight_entropy(w)
        entropy_bonus = entropy_lambda * (ent - 0.5)
        avg_expR, _, _ = eval_weights(train_data, w)
        fitness = avg_expR + entropy_bonus - penalty
        return (fitness,)

    toolbox.register("evaluate", _evaluate)
    toolbox.register("mate", tools.cxBlend, alpha=0.3)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=POP)
    hof = tools.HallOfFame(3)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", lambda x: float(np.mean(x)))
    stats.register("max", lambda x: float(np.max(x)))

    best_max = -999
    stall = 0
    for gen in range(GEN):
        offspring = algorithms.varAnd(pop, toolbox, cxpb=CXPB, mutpb=MUTPB)
        fits = list(map(toolbox.evaluate, offspring))
        for ind, fit in zip(offspring, fits):
            ind.fitness.values = fit
        hof.update(offspring)
        pop = toolbox.select(offspring, k=len(pop))
        record = stats.compile(pop)
        if record["max"] > best_max + 0.001:
            best_max = record["max"]
            stall = 0
        else:
            stall += 1
        if stall >= 5:
            break

    best = hof[0]
    best_w = ind_to_weights(best, n_factor)
    return best_w


def compute_sector_characteristics(train_data):
    """计算板块特征：单因子强度、因子多样性等。

    方法：评估每个单因子在训练集上的 expR，
    然后计算 max_factor_expR / sum(|factor_expR|) 作为集中度。
    """
    factor_names = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]
    factor_expRs = {}

    for f in factor_names:
        single_w = {name: 0.0 for name in factor_names}
        single_w[f] = 1.0
        expR, _, _ = eval_weights(train_data, single_w)
        factor_expRs[f] = expR

    vals = list(factor_expRs.values())
    max_factor = max(vals)
    abs_sum = sum(abs(v) for v in vals)
    concentration = max_factor / abs_sum if abs_sum > 0.001 else 0

    # 正收益因子数
    n_positive = sum(1 for v in vals if v > 0)

    # 最强因子
    strongest = max(factor_expRs, key=factor_expRs.get)

    return {
        "factor_expRs": factor_expRs,
        "max_factor_expR": max_factor,
        "concentration": concentration,
        "n_positive_factors": n_positive,
        "strongest_factor": strongest,
    }


def process_group(group_name):
    print(f"\n{'='*65}", flush=True)
    print(f"[板块] {group_name}", flush=True)
    print(f"{'='*65}", flush=True)

    group_data = load_group_data(group_name, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
    if len(group_data) < 3:
        print(f"  跳过：有效品种不足", flush=True)
        return None

    print(f"  有效品种: {len(group_data)}", flush=True)

    # 切分训练/验证
    train_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        total = len(df)
        train_end = total - TEST_BARS
        train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS):train_end]
        test_data[sym] = df.iloc[train_end:]

    # 基准
    base_train, base_tr_train, base_nv_train = eval_weights(train_data)
    base_test, base_tr_test, base_nv_test = eval_weights(test_data)
    print(f"  基准: train={base_train:+.3f}  test={base_test:+.3f}", flush=True)

    # 板块特征（训练集计算）
    print(f"\n  [特征] 训练集因子分析:", flush=True)
    chars = compute_sector_characteristics(train_data)
    for f, v in chars["factor_expRs"].items():
        print(f"    {f:14s}: {v:+.3f}", flush=True)
    print(f"  最强因子: {chars['strongest_factor']} ({chars['max_factor_expR']:+.3f})", flush=True)
    print(f"  集中度: {chars['concentration']:.3f}", flush=True)
    print(f"  正收益因子数: {chars['n_positive_factors']}/6", flush=True)

    # 各原型 GA 优化
    print(f"\n  [GA] 各原型优化:", flush=True)
    archetype_results = {}

    for aname, acfg in ARCHETYPES.items():
        print(f"    {aname}...", end=" ", flush=True)
        w = ga_optimize(train_data, aname, acfg)
        train_expR, train_trades, train_valid = eval_weights(train_data, w)
        test_expR, test_trades, test_valid = eval_weights(test_data, w)

        ent = weight_entropy(w)
        max_w_val = max(w.values())

        archetype_results[aname] = {
            "weights": w,
            "train_expR": train_expR,
            "test_expR": test_expR,
            "train_trades": train_trades,
            "test_trades": test_trades,
            "entropy": ent,
            "max_weight": max_w_val,
        }

        train_gain = train_expR - base_train
        test_gain = test_expR - base_test
        print(f"train={train_expR:+.3f}({train_gain:+.2f})  "
              f"test={test_expR:+.3f}({test_gain:+.2f})  "
              f"entropy={ent:.2f}", flush=True)

    # 最优 OOS 原型（事后诸葛亮，用于上限参考）
    best_oos = max(archetype_results, key=lambda k: archetype_results[k]["test_expR"])
    best_oos_val = archetype_results[best_oos]["test_expR"]

    # 最优训练集原型
    best_train = max(archetype_results, key=lambda k: archetype_results[k]["train_expR"])

    # 基于特征的差异化选择（用训练集特征选，不用 OOS）
    # 规则：集中度 > 0.5 → aggressive, 0.35~0.5 → balanced, < 0.35 → conservative
    if chars["concentration"] > 0.5:
        feature_selected = "aggressive"
    elif chars["concentration"] > 0.35:
        feature_selected = "balanced"
    else:
        feature_selected = "conservative"

    feature_selected_test = archetype_results[feature_selected]["test_expR"]

    # 统一最优（所有板块用同一个原型）
    print(f"\n  [汇总]", flush=True)
    print(f"    基准 test:       {base_test:+.3f}", flush=True)
    for aname in ARCHETYPES:
        r = archetype_results[aname]
        print(f"    {aname:14s} test: {r['test_expR']:+.3f}  "
              f"(train: {r['train_expR']:+.3f})", flush=True)
    print(f"    最优 OOS: {best_oos} ({best_oos_val:+.3f})", flush=True)
    print(f"    特征选择: {feature_selected} ({feature_selected_test:+.3f})", flush=True)

    return {
        "group": group_name,
        "n_symbols": len(group_data),
        "base": {"train": base_train, "test": base_test},
        "characteristics": chars,
        "archetypes": archetype_results,
        "best_oos_archetype": best_oos,
        "best_oos_test_expR": best_oos_val,
        "feature_selected_archetype": feature_selected,
        "feature_selected_test_expR": feature_selected_test,
    }


def main():
    t0 = time.time()
    results = {}

    for g in GROUPS:
        r = process_group(g)
        if r:
            results[g] = r

    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f"\n{'='*65}", flush=True)
    print(f"全部完成，耗时 {elapsed/60:.1f} 分钟", flush=True)
    print(f"{'='*65}", flush=True)

    # 汇总表
    print(f"\n{'板块':<8s} {'基准':>8s} {'保守':>8s} {'均衡':>8s} {'进攻':>8s} {'精简':>8s} {'最优OOS':>10s}", flush=True)
    print("-" * 70, flush=True)
    for g, r in results.items():
        a = r["archetypes"]
        print(f"{g:<8s} {r['base']['test']:+.3f}    "
              f"{a['conservative']['test_expR']:+.3f}    "
              f"{a['balanced']['test_expR']:+.3f}    "
              f"{a['aggressive']['test_expR']:+.3f}    "
              f"{a['focused']['test_expR']:+.3f}    "
              f"{r['best_oos_archetype']:>10s}", flush=True)

    # 差异化 vs 统一策略对比
    print(f"\n差异化 vs 统一策略（测试集平均 expR）:", flush=True)
    # 统一策略：每个原型取所有板块的平均
    for aname in ARCHETYPES:
        avg = np.mean([r["archetypes"][aname]["test_expR"] for r in results.values()])
        print(f"  统一-{aname:14s}: {avg:+.3f}", flush=True)

    # 特征选择差异化
    feat_avg = np.mean([r["feature_selected_test_expR"] for r in results.values()])
    print(f"  特征选择差异化: {feat_avg:+.3f}", flush=True)

    # 最优 OOS（上限，有过拟合风险）
    best_oos_avg = np.mean([r["best_oos_test_expR"] for r in results.values()])
    print(f"  最优 OOS (上限): {best_oos_avg:+.3f}", flush=True)

    base_avg = np.mean([r["base"]["test"] for r in results.values()])
    print(f"  基准: {base_avg:+.3f}", flush=True)

    print(f"\n结果已保存到: {OUTFILE}", flush=True)


if __name__ == "__main__":
    main()
