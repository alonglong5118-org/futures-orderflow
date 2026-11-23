"""稳健版 6 因子 OOS 验证

直接用 ga_group_six_factor_robust.py 得到的最优权重，在验证集上评估。
同时与无约束版、基准版做三方对比。
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np

from four_dim_strategy import DEFAULT_CONFIG, walk_forward_backtest
from ga_group_six_factor import load_group_data

ROBUST_FILE = os.path.join(HERE, "logs", "ga_group_six_factor_robust.json")
NO_CONSTRAINT_FILE = os.path.join(HERE, "logs", "ga_group_six_factor_oos.json")
OUTFILE = os.path.join(HERE, "logs", "ga_robust_vs_noconstraint_oos.json")

TRAIN_BARS = 300
TEST_BARS = 200
MIN_TRADES = 5

# 加载稳健版权重
with open(ROBUST_FILE, encoding="utf-8") as f:
    robust_results = json.load(f)

# 加载无约束版 OOS 结果（用于对比）
with open(NO_CONSTRAINT_FILE, encoding="utf-8") as f:
    noconst_results = json.load(f)


def eval_on_data(data, weights=None):
    """在给定数据集上评估，返回 (avg_expR, total_trades, n_valid)"""
    expRs = []
    total_trades = 0
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if weights:
        cfg["subfactor_weights"] = weights

    for sym, df in sorted(data.items()):
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=200, min_bars=40, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass

    avg = float(np.mean(expRs)) if expRs else 0.0
    return avg, total_trades, len(expRs)


results = {}

print("=" * 70, flush=True)
print("稳健版 vs 无约束版 OOS 对比", flush=True)
print(f"训练集: {TRAIN_BARS} 根  |  验证集: {TEST_BARS} 根", flush=True)
print("=" * 70, flush=True)

for group, r_robust in robust_results.items():
    print(f"\n[板块] {group}", flush=True)

    # 加载数据并切分
    group_data = load_group_data(group, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
    if len(group_data) < 3:
        print("  跳过：有效品种不足", flush=True)
        continue

    train_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        total = len(df)
        train_end = total - TEST_BARS
        train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS) : train_end]
        test_data[sym] = df.iloc[train_end:]

    w = r_robust["best_weights"]

    # 基准
    train_base, train_base_trades, train_base_valid = eval_on_data(train_data)
    test_base, test_base_trades, test_base_valid = eval_on_data(test_data)

    # 稳健版
    train_rob, train_rob_trades, train_rob_valid = eval_on_data(train_data, w)
    test_rob, test_rob_trades, test_rob_valid = eval_on_data(test_data, w)

    # 无约束版（从之前 OOS 结果读）
    r_nc = noconst_results.get(group, {})
    train_nc = r_nc.get("train", {}).get("sf_expR", 0)
    test_nc = r_nc.get("test", {}).get("sf_expR", 0)
    overfit_nc = r_nc.get("overfit_coef", 0)

    # 计算过拟合系数
    train_gain_rob = train_rob - train_base
    test_gain_rob = test_rob - test_base
    overfit_rob = test_gain_rob / train_gain_rob if abs(train_gain_rob) > 0.001 else 0.0

    train_gain_nc = train_nc - train_base
    test_gain_nc = test_nc - test_base
    overfit_nc = test_gain_nc / train_gain_nc if abs(train_gain_nc) > 0.001 else 0.0

    print(f"  {'':12s} {'基准':>8s} {'无约束':>8s} {'稳健版':>8s}", flush=True)
    print(f"  {'训练集':12s} {train_base:+.4f}  {train_nc:+.4f}  {train_rob:+.4f}", flush=True)
    print(f"  {'验证集':12s} {test_base:+.4f}  {test_nc:+.4f}  {test_rob:+.4f}", flush=True)
    print(f"  {'过拟合系数':12s} {'—':>8s} {overfit_nc:+.2f}    {overfit_rob:+.2f}", flush=True)

    results[group] = {
        "train": {"base": train_base, "noconst": train_nc, "robust": train_rob},
        "test": {"base": test_base, "noconst": test_nc, "robust": test_rob},
        "overfit": {"noconst": overfit_nc, "robust": overfit_rob},
        "robust_weights": w,
        "train_trades": {"base": train_base_trades, "robust": train_rob_trades},
        "test_trades": {"base": test_base_trades, "robust": test_rob_trades},
    }

# 保存
with open(OUTFILE, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n{'=' * 70}", flush=True)
print("汇总：过拟合系数对比", flush=True)
print(f"{'=' * 70}", flush=True)
print(f"{'板块':<8s} {'无约束':>10s} {'稳健版':>10s} {'改善':>10s}", flush=True)
print("-" * 42, flush=True)
for g, r in results.items():
    of_nc = r["overfit"]["noconst"]
    of_rob = r["overfit"]["robust"]
    delta = of_rob - of_nc
    print(f"{g:<8s} {of_nc:+.2f}    {of_rob:+.2f}    {delta:+.2f}", flush=True)
print(f"\n结果已保存到: {OUTFILE}", flush=True)
