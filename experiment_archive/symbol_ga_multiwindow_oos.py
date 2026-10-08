"""
品种级 T/F/C GA 多窗口滚动 OOS 验证
- 对每个实盘品种，在 4 个时间窗口内分别做 GA 优化 + OOS 验证
- 对比：默认权重 vs GA 优化权重
- 回答：品种级 T/F/C GA 优化到底有没有用？
"""

import copy
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest

# 尝试使用 ga_factor_miner 的优化函数
try:
    _HAVE_GFM = True
except Exception:
    _HAVE_GFM = False

try:
    from deap import base, creator, tools

    _HAVE_DEAP = True
except Exception:
    _HAVE_DEAP = False

WINDOWS = [500, 700, 900, 1100]
WINDOW_LABELS = ["W1(最近)", "W2", "W3", "W4(最早)"]

DEFAULT_W = {"T": 0.6, "F": 0.25, "C": 0.15}

# 实盘品种列表（从备份文件读取）
BACKUP_FILE = "ga_weights_cache.json.bak_20260829_pre_rollback"

# GA 参数（缩小规模以加快速度，22品种×4窗口=88次优化）
POP_SIZE = 30
N_GEN = 12
MIN_TRADES = 8


def get_live_symbols():
    """从备份文件获取实盘品种列表"""
    if os.path.exists(BACKUP_FILE):
        with open(BACKUP_FILE) as f:
            data = json.load(f)
        syms = []
        for sym, info in data.items():
            g = info.get("blend_group", "未知")
            expR = info.get("best_expR", -999)
            syms.append((sym, g, expR))
        return syms
    # fallback：从 SYMBOLS 里取
    return [(s, info.get("group", "未知"), 0) for s, info in SYMBOLS.items()]


def evaluate_symbol(symbol, weights, df):
    """评估一组权重在给定数据上的 expR"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["combine_weights"] = weights["base"]
    # regime_adjust 暂时用 base 权重评估（简化，避免 regime 判定复杂性）
    try:
        r = walk_forward_backtest(symbol, cfg=cfg, window=300, min_bars=60, df_in=df)
        nt = int(r.get("trades", 0))
        expR = float(r.get("expR", 0))
        if nt >= MIN_TRADES:
            return expR, nt
    except Exception:
        pass
    return -10.0, 0


def run_ga_for_symbol(symbol, df_train):
    """对单个品种在训练集上做 GA 优化，返回最优权重"""
    if not _HAVE_DEAP:
        return None

    # 使用简化版 GA：只优化 3 个基础权重（T, F, C），不优化 regime 调整
    # 这样参数更少，过拟合风险更低，也更快
    if not hasattr(creator, "SymbolWeight"):
        creator.create("SymbolWeight", base.Fitness, weights=(1.0,))
        creator.create("IndWeight", list, fitness=creator.SymbolWeight)

    toolbox = base.Toolbox()
    toolbox.register("attr_T", random.uniform, 0.30, 0.80)
    toolbox.register("attr_F", random.uniform, 0.00, 0.50)
    toolbox.register("attr_C", random.uniform, 0.00, 0.40)
    toolbox.register(
        "individual", tools.initCycle, creator.IndWeight, (toolbox.attr_T, toolbox.attr_F, toolbox.attr_C), n=1
    )
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def _eval(ind):
        t, f, c = ind[0], ind[1], ind[2]
        total = t + f + c
        if total <= 0:
            return (-10.0,)
        t_n, f_n, c_n = t / total, f / total, c / total
        w = {"base": {"T": t_n, "F": f_n, "C": c_n}, "regime_adjust": {}}
        expR, nt = evaluate_symbol(symbol, w, df_train)
        return (expR,)

    toolbox.register("evaluate", _eval)
    toolbox.register("mate", tools.cxBlend, alpha=0.5)
    toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.05, indpb=0.3)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=POP_SIZE)
    hof = tools.HallOfFame(5)

    # 初始评估
    fits = list(map(toolbox.evaluate, pop))
    for ind, fit in zip(pop, fits):
        ind.fitness.values = fit
    hof.update(pop)

    best_fitness = fits[0][0]
    stagnation = 0

    for gen in range(N_GEN):
        # 选择
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        # 交叉
        for i in range(1, len(offspring), 2):
            if random.random() < 0.8:
                offspring[i - 1], offspring[i] = toolbox.mate(offspring[i - 1], offspring[i])
                del offspring[i - 1].fitness.values, offspring[i].fitness.values

        # 变异
        for ind in offspring:
            if random.random() < 0.3:
                toolbox.mutate(ind)
                if ind.fitness.valid:
                    del ind.fitness.values

        # 评估无效个体
        invalid = [ind for ind in offspring if not ind.fitness.valid]
        fits = list(map(toolbox.evaluate, invalid))
        for ind, fit in zip(invalid, fits):
            ind.fitness.values = fit

        hof.update(offspring)
        pop[:] = offspring

        # 早停
        current_best = max(ind.fitness.values[0] for ind in pop)
        if current_best > best_fitness + 0.001:
            best_fitness = current_best
            stagnation = 0
        else:
            stagnation += 1
        if stagnation >= 5:
            break

    # 返回最优权重
    best = hof[0]
    t, f, c = best[0], best[1], best[2]
    total = t + f + c
    if total <= 0:
        t_n, f_n, c_n = 0.6, 0.25, 0.15
    else:
        t_n, f_n, c_n = t / total, f / total, c / total

    return {
        "base": {"T": round(t_n, 4), "F": round(f_n, 4), "C": round(c_n, 4)},
        "regime_adjust": {},
        "train_expR": best_fitness,
    }


def run_single_symbol_oos(symbol):
    """单个品种的 4 窗口 OOS 验证"""
    results = []

    for wi, tail in enumerate(WINDOWS):
        label = WINDOW_LABELS[wi]

        # 加载数据
        df = load_daily(symbol)
        if df is None or len(df) < 200:
            results.append(None)
            continue

        if len(df) > tail:
            df = df.tail(tail)

        # 切分训练 / OOS
        n = len(df)
        split = int(n * 0.75)
        df_train = df.iloc[:split]
        df_oos = df.iloc[split:]

        if len(df_train) < 150 or len(df_oos) < 50:
            results.append(None)
            continue

        # 默认权重
        def_train_expR, def_train_nt = evaluate_symbol(symbol, {"base": DEFAULT_W, "regime_adjust": {}}, df_train)
        def_oos_expR, def_oos_nt = evaluate_symbol(symbol, {"base": DEFAULT_W, "regime_adjust": {}}, df_oos)

        # GA 优化（训练集）
        ga_result = run_ga_for_symbol(symbol, df_train)
        if ga_result is None:
            results.append(None)
            continue

        ga_train_expR = ga_result["train_expR"]
        ga_w = ga_result["base"]

        # GA OOS 评估
        ga_oos_expR, ga_oos_nt = evaluate_symbol(symbol, {"base": ga_w, "regime_adjust": {}}, df_oos)

        # 计算指标
        train_gain = ga_train_expR - def_train_expR
        oos_gain = ga_oos_expR - def_oos_expR
        overfit_coef = oos_gain / train_gain if train_gain > 0.001 else float("inf")

        results.append(
            {
                "window": label,
                "tail": tail,
                "default_train_expR": def_train_expR,
                "default_train_nt": def_train_nt,
                "default_oos_expR": def_oos_expR,
                "default_oos_nt": def_oos_nt,
                "ga_weights": ga_w,
                "ga_train_expR": ga_train_expR,
                "ga_oos_expR": ga_oos_expR,
                "ga_oos_nt": ga_oos_nt,
                "train_gain": train_gain,
                "oos_gain": oos_gain,
                "overfit_coef": overfit_coef,
            }
        )

    return results


def main():
    symbols = get_live_symbols()
    print(f"实盘品种数: {len(symbols)}")
    print(f"窗口数: {len(WINDOWS)} ({', '.join(WINDOW_LABELS)})")
    print(f"GA 参数: pop={POP_SIZE}, gen={N_GEN}")
    print(f"预计优化次数: {len(symbols)} × {len(WINDOWS)} = {len(symbols) * len(WINDOWS)}")
    print()

    all_results = {}

    for idx, (sym, group, old_expR) in enumerate(symbols):
        print(f"[{idx + 1}/{len(symbols)}] {sym} ({group}) 旧 expR={old_expR:+.3f}")

        results = run_single_symbol_oos(sym)
        valid = [r for r in results if r is not None]

        if not valid:
            print("  无有效窗口，跳过")
            all_results[sym] = {"group": group, "windows": [], "summary": {"error": "无有效数据"}}
            continue

        avg_oos_gain = float(np.mean([r["oos_gain"] for r in valid]))
        win_rate = float(np.mean([1 if r["oos_gain"] > 0 else 0 for r in valid]))
        ofc_vals = [
            r["overfit_coef"] for r in valid if r["overfit_coef"] != float("inf") and abs(r["overfit_coef"]) < 10
        ]
        avg_ofc = float(np.mean(ofc_vals)) if ofc_vals else float("inf")

        # 逐窗口打印
        for r in valid:
            print(
                f"  {r['window']}: 默认OOS={r['default_oos_expR']:+.3f}({r['default_oos_nt']}笔) "
                f"GA_OOS={r['ga_oos_expR']:+.3f}({r['ga_oos_nt']}笔) "
                f"Δ={r['oos_gain']:+.3f} OFC={r['overfit_coef']:.2f}"
            )

        if win_rate >= 0.75 and avg_ofc > 0.3 and avg_oos_gain > 0:
            verdict = "✅ 稳健有效"
        elif win_rate >= 0.5 and avg_oos_gain > 0:
            verdict = "⚠️ 部分有效"
        else:
            verdict = "❌ 整体无效"

        print(f"  汇总: 平均OOS提升={avg_oos_gain:+.4f} 胜率={win_rate * 100:.0f}% OFC={avg_ofc:.2f} → {verdict}")
        print()

        all_results[sym] = {
            "group": group,
            "old_expR": old_expR,
            "windows": results,
            "summary": {
                "avg_oos_gain": avg_oos_gain,
                "oos_win_rate": win_rate,
                "avg_overfit_coef": avg_ofc,
                "n_valid_windows": len(valid),
                "verdict": verdict,
            },
        }

    # 全市场汇总
    print("=" * 80)
    print("全市场品种级 GA OOS 验证汇总")
    print("=" * 80)

    by_group = {}
    for sym, r in all_results.items():
        g = r["group"]
        if g not in by_group:
            by_group[g] = []
        by_group[g].append(r)

    print(f"\n{'品种':<8}{'板块':<8}{'旧expR':>10}{'OOS提升':>10}{'胜率':>8}{'OFC':>8}{'结论':>14}")
    print("-" * 70)

    effective_count = 0
    partial_count = 0
    invalid_count = 0

    for sym in sorted(all_results.keys()):
        r = all_results[sym]
        s = r["summary"]
        if "error" in s:
            print(f"{sym:<8}{r['group']:<8}    N/A       N/A     N/A     N/A    数据不足")
            continue
        gain = s["avg_oos_gain"]
        wr = s["oos_win_rate"]
        ofc = s["avg_overfit_coef"]
        v = s["verdict"]
        ofc_str = f"{ofc:.2f}" if ofc != float("inf") else "N/A"
        old_expR = r.get("old_expR", 0)
        old_str = f"{old_expR:+.3f}" if old_expR > -9 else "  N/A"

        if "稳健有效" in v:
            effective_count += 1
        elif "部分有效" in v:
            partial_count += 1
        else:
            invalid_count += 1

        print(f"{sym:<8}{r['group']:<8}{old_str:>10}{gain:>+10.4f}{wr * 100:>7.0f}%{ofc_str:>8}{v:>14}")

    print()
    print(f"稳健有效: {effective_count} 个品种")
    print(f"部分有效: {partial_count} 个品种")
    print(f"整体无效: {invalid_count} 个品种")
    print(f"有效率: {effective_count}/{len(all_results)} = {effective_count / len(all_results) * 100:.0f}%")

    # 保存
    out_path = "logs/symbol_ga_multiwindow_oos.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
