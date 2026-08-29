"""板块差异化策略探索

对每个板块测试多种策略原型，找出最适合的配置。
用 2 折走步法验证，降低验证集过拟合风险。

策略原型：
A. conservative : 6因子 + 强约束 (max_w=0.30, λ=0.20)
B. balanced     : 6因子 + 中约束 (max_w=0.40, λ=0.10)
C. aggressive   : 6因子 + 弱约束 (max_w=0.60, λ=0.03)
D. focused      : 5因子 + 中约束 (max_w=0.45, λ=0.08)
E. baseline     : 基准（默认权重）

走步法（2-fold）：
  Fold 1: train [0:300], val [300:400], test [400:500]
  Fold 2: train [100:400], val [400:450], test [450:500]
每个 fold 内，用训练集 GA 优化，用验证集选最优原型，然后在测试集上评估。
最终汇总两折测试集表现。
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
    "balanced": {"n_factor": 6, "max_w": 0.40, "entropy_lambda": 0.10, "label": "均衡稳健型"},
    "aggressive": {"n_factor": 6, "max_w": 0.60, "entropy_lambda": 0.03, "label": "集中进攻型"},
    "focused": {"n_factor": 5, "max_w": 0.45, "entropy_lambda": 0.08, "label": "精简聚焦型"},
}

# ===== GA 参数 =====
POP = 12
GEN = 8
CXPB = 0.6
MUTPB = 0.3
WINDOW = 200
MIN_BARS = 40
MIN_TRADES = 5

GROUPS = ["化工", "农产品", "有色", "黑系", "能源"]
OUTFILE = os.path.join(HERE, "logs", "ga_sector_differentiated.json")

SF6 = ["T_trend", "T_mean", "T_seasonal", "F_basis", "F_seasonal", "C"]
SF5 = ["T_trend", "T_mean", "F_basis", "F_seasonal", "C"]


def get_factor_names(n_factor):
    return SF6 if n_factor == 6 else SF5


def ind_to_weights(ind, n_factor):
    names = get_factor_names(n_factor)
    s = sum(max(0, x) for x in ind)
    if s < 1e-6:
        return {n: 1.0 / len(names) for n in names}
    norm = [max(0, x) / s for x in ind]
    return {name: round(w, 4) for name, w in zip(names, norm)}


def weight_entropy(weights_dict):
    w = np.array([max(v, 1e-6) for v in weights_dict.values()])
    w = w / w.sum()
    entropy = -np.sum(w * np.log(w))
    max_entropy = np.log(len(w))
    return float(entropy / max_entropy)


def make_sf6_weights(w):
    """把 5 因子或 6 因子权重转成 6 因子格式（策略层需要）。"""
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
    """用给定权重评估数据集，返回 (avg_expR, total_trades, n_valid)。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if weights:
        cfg["subfactor_weights"] = make_sf6_weights(weights)

    expRs = []
    total_trades = 0
    for sym, df in sorted(data.items()):
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=WINDOW, min_bars=MIN_BARS, df_in=df)
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
    """对给定数据做 GA 优化，返回最优权重。"""
    n_factor = archetype_cfg["n_factor"]
    max_w = archetype_cfg["max_w"]
    entropy_lambda = archetype_cfg["entropy_lambda"]
    n_vars = n_factor

    # 清理旧的 creator 类
    for _name in ["FDiffFit", "FDiffInd"]:
        if _name in dir(creator):
            delattr(creator, _name)

    creator.create("FDiffFit", base.Fitness, weights=(1.0,))
    creator.create("FDiffInd", list, fitness=creator.FDiffFit)

    toolbox = base.Toolbox()
    toolbox.register("attr_float", random.uniform, 0, 1)
    toolbox.register("individual", tools.initRepeat, creator.FDiffInd, toolbox.attr_float, n=n_vars)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _evaluate(ind):
        w = ind_to_weights(ind, n_factor)
        # 权重上限惩罚
        cur_max = max(w.values())
        penalty = max(0, cur_max - max_w) * 5.0
        # 熵奖励
        ent = weight_entropy(w)
        entropy_bonus = entropy_lambda * (ent - 0.5)
        # 评估
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
        if stall >= 4:
            break

    best = hof[0]
    best_w = ind_to_weights(best, n_factor)
    return best_w


def split_fold(group_data, fold_idx, total_bars=500):
    """切分第 fold_idx 折的数据。

    Fold 0: train [0:300], val [300:400], test [400:500]
    Fold 1: train [50:350], val [350:425], test [425:500]
    （两折有重叠，用不同测试段评估稳健性）
    """
    if fold_idx == 0:
        train_end = 300
        val_end = 400
        test_end = 500
    else:
        train_end = 350
        val_end = 425
        test_end = 500

    train_data = {}
    val_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        if len(df) < test_end:
            continue
        train_data[sym] = df.iloc[:train_end]
        val_data[sym] = df.iloc[train_end:val_end]
        test_data[sym] = df.iloc[val_end:test_end]

    return train_data, val_data, test_data


def process_group(group_name):
    """处理单个板块：两折走步法 + 多原型对比。"""
    print(f"\n{'=' * 65}", flush=True)
    print(f"[板块] {group_name}", flush=True)
    print(f"{'=' * 65}", flush=True)

    group_data = load_group_data(group_name, min_bars=500, tail=0)
    if len(group_data) < 3:
        print("  跳过：有效品种不足", flush=True)
        return None

    print(f"  有效品种: {len(group_data)}", flush=True)

    fold_results = []

    for fold in range(2):
        print(f"\n  --- Fold {fold + 1} ---", flush=True)
        train_d, val_d, test_d = split_fold(group_data, fold)

        if len(train_d) < 3 or len(test_d) < 3:
            print("    跳过：数据不足", flush=True)
            continue

        # 基准
        base_train = eval_weights(train_d)[0]
        base_val = eval_weights(val_d)[0]
        base_test = eval_weights(test_d)[0]
        print(f"    基准: train={base_train:+.3f}  val={base_val:+.3f}  test={base_test:+.3f}", flush=True)

        # 各原型 GA 优化 + 验证集评估
        archetype_results = {}
        best_archetype = None
        best_val_expR = -999

        for aname, acfg in ARCHETYPES.items():
            w = ga_optimize(train_d, aname, acfg)
            val_expR, val_trades, val_valid = eval_weights(val_d, w)
            test_expR, test_trades, test_valid = eval_weights(test_d, w)
            train_expR, _, _ = eval_weights(train_d, w)

            archetype_results[aname] = {
                "weights": w,
                "train_expR": train_expR,
                "val_expR": val_expR,
                "test_expR": test_expR,
                "val_trades": val_trades,
                "val_valid": val_valid,
            }

            gain = val_expR - base_val
            print(
                f"    {aname:14s}: train={train_expR:+.3f}  val={val_expR:+.3f}  "
                f"test={test_expR:+.3f}  val_gain={gain:+.3f}",
                flush=True,
            )

            if val_expR > best_val_expR:
                best_val_expR = val_expR
                best_archetype = aname

        # 差异化选择：验证集选最优 → 测试集评估
        diff_test_expR = archetype_results[best_archetype]["test_expR"]
        print(f"    >>> 选择: {best_archetype}, test_expR={diff_test_expR:+.3f}", flush=True)

        fold_results.append(
            {
                "fold": fold,
                "base_test": base_test,
                "best_archetype": best_archetype,
                "differentiated_test": diff_test_expR,
                "archetypes": {
                    k: {"test_expR": v["test_expR"], "val_expR": v["val_expR"]} for k, v in archetype_results.items()
                },
            }
        )

    # 汇总两折
    if not fold_results:
        return None

    avg_base_test = float(np.mean([f["base_test"] for f in fold_results]))
    avg_diff_test = float(np.mean([f["differentiated_test"] for f in fold_results]))

    # 各原型的平均测试集表现
    avg_archetype_test = {}
    for aname in ARCHETYPES:
        vals = [f["archetypes"][aname]["test_expR"] for f in fold_results if aname in f["archetypes"]]
        avg_archetype_test[aname] = float(np.mean(vals)) if vals else 0.0

    best_uniform = max(avg_archetype_test, key=avg_archetype_test.get)

    print("\n  两折汇总:", flush=True)
    print(f"    基准平均 test_expR:    {avg_base_test:+.3f}", flush=True)
    for aname, avg_v in sorted(avg_archetype_test.items(), key=lambda x: -x[1]):
        marker = " ★" if aname == best_uniform else ""
        print(f"    {aname:14s} 平均: {avg_v:+.3f}{marker}", flush=True)
    print(f"    差异化策略 平均:     {avg_diff_test:+.3f}", flush=True)
    print(f"    差异化 vs 最优统一:  {avg_diff_test - avg_archetype_test[best_uniform]:+.3f}", flush=True)

    return {
        "group": group_name,
        "n_symbols": len(group_data),
        "fold_results": fold_results,
        "avg_base_test": avg_base_test,
        "avg_archetype_test": avg_archetype_test,
        "best_uniform_archetype": best_uniform,
        "avg_differentiated_test": avg_diff_test,
        "differentiated_vs_best_uniform": avg_diff_test - avg_archetype_test[best_uniform],
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
    print(f"\n{'=' * 65}", flush=True)
    print(f"全部完成，耗时 {elapsed / 60:.1f} 分钟", flush=True)
    print(f"{'=' * 65}", flush=True)

    # 汇总表
    print(f"\n{'板块':<8s} {'基准':>8s} {'最优统一':>10s} {'差异化':>8s} {'提升':>8s} {'最优原型':>10s}", flush=True)
    print("-" * 65, flush=True)
    for g, r in results.items():
        bu = r["best_uniform_archetype"]
        bu_val = r["avg_archetype_test"][bu]
        diff = r["avg_differentiated_test"]
        delta = diff - bu_val
        print(
            f"{g:<8s} {r['avg_base_test']:+.3f}    {bu_val:+.3f}    {diff:+.3f}    {delta:+.3f}    {bu:>10s}",
            flush=True,
        )

    # 各板块每折选择的原型
    print("\n各板块每折选择的原型:", flush=True)
    for g, r in results.items():
        choices = [f"F{f['fold'] + 1}:{f['best_archetype']}" for f in r["fold_results"]]
        print(f"  {g}: {', '.join(choices)}", flush=True)

    print(f"\n结果已保存到: {OUTFILE}", flush=True)


if __name__ == "__main__":
    main()
