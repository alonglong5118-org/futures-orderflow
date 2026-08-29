"""统一基准评估：在完全相同的数据和参数下，对比三种方案

方案：
A. 基准（默认 T/F/C 三因子权重）
B. 6 因子无约束版
C. 6 因子稳健版
D. 5 因子稳健版（剔除 T_seasonal）

统一参数：
- 训练集: 300 根
- 验证集: 200 根
- 回测窗口: 300 根（与 GA 训练一致）
- min_bars: 60
- 最低交易数: 5 笔/品种
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

TRAIN_BARS = 300
TEST_BARS = 200
WINDOW = 300
MIN_BARS = 60
MIN_TRADES = 5

GROUPS = ["化工", "农产品", "有色", "黑系", "能源"]
OUTFILE = os.path.join(HERE, "logs", "unified_oos_compare.json")

# 加载各方案权重
NOCONST_FILE = os.path.join(HERE, "logs", "ga_group_six_factor.json")
ROBUST_FILE = os.path.join(HERE, "logs", "ga_group_six_factor_robust.json")
FIVE_FACTOR_FILE = os.path.join(HERE, "logs", "ga_five_factor_oos.json")

with open(NOCONST_FILE) as f:
    noconst_w = json.load(f)
with open(ROBUST_FILE) as f:
    robust_w = json.load(f)
with open(FIVE_FACTOR_FILE) as f:
    five_factor_data = json.load(f)


def eval_weights(data, subfactor_weights=None):
    """用给定权重在给定数据上评估。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if subfactor_weights:
        cfg["subfactor_weights"] = subfactor_weights

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
        return {"expR": -5.0, "trades": 0, "n_valid": 0}
    avg = float(np.mean(expRs))
    if len(expRs) < 3:
        avg -= 0.5 * (3 - len(expRs))
    return {"expR": avg, "trades": total_trades, "n_valid": len(expRs)}


results = {}

print("=" * 80, flush=True)
print("统一基准评估：4 方案 OOS 对比", flush=True)
print(f"训练集 {TRAIN_BARS} 根 / 验证集 {TEST_BARS} 根 / 窗口 {WINDOW} 根", flush=True)
print("=" * 80, flush=True)

for group in GROUPS:
    print(f"\n[板块] {group}", flush=True)

    # 加载数据并切分
    group_data = load_group_data(group, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
    if len(group_data) < 3:
        print("  跳过", flush=True)
        continue

    train_data = {}
    test_data = {}
    for sym, df in sorted(group_data.items()):
        total = len(df)
        train_end = total - TEST_BARS
        train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS) : train_end]
        test_data[sym] = df.iloc[train_end:]

    print(f"  品种数: {len(group_data)}", flush=True)

    # A. 基准
    base_train = eval_weights(train_data)
    base_test = eval_weights(test_data)

    # B. 6因子无约束版
    w_nc = noconst_w.get(group, {}).get("best_weights")
    nc_train = eval_weights(train_data, w_nc) if w_nc else {"expR": 0, "trades": 0, "n_valid": 0}
    nc_test = eval_weights(test_data, w_nc) if w_nc else {"expR": 0, "trades": 0, "n_valid": 0}

    # C. 6因子稳健版
    w_rob = robust_w.get(group, {}).get("best_weights")
    rob_train = eval_weights(train_data, w_rob) if w_rob else {"expR": 0, "trades": 0, "n_valid": 0}
    rob_test = eval_weights(test_data, w_rob) if w_rob else {"expR": 0, "trades": 0, "n_valid": 0}

    # D. 5因子稳健版
    w5_raw = five_factor_data.get(group, {}).get("best_weights")
    if w5_raw:
        w5 = {
            "T_trend": w5_raw.get("T_trend", 0),
            "T_mean": w5_raw.get("T_mean", 0),
            "T_seasonal": 0.0,
            "F_basis": w5_raw.get("F_basis", 0),
            "F_seasonal": w5_raw.get("F_seasonal", 0),
            "C": w5_raw.get("C", 0),
        }
    else:
        w5 = None
    ff_train = eval_weights(train_data, w5) if w5 else {"expR": 0, "trades": 0, "n_valid": 0}
    ff_test = eval_weights(test_data, w5) if w5 else {"expR": 0, "trades": 0, "n_valid": 0}

    # 过拟合系数
    def overfit(train_r, test_r, base_train_r=base_train["expR"], base_test_r=base_test["expR"]):
        gain_train = train_r - base_train_r
        gain_test = test_r - base_test_r
        if abs(gain_train) < 0.001:
            return 0.0
        return gain_test / gain_train

    of_nc = overfit(nc_train["expR"], nc_test["expR"])
    of_rob = overfit(rob_train["expR"], rob_test["expR"])
    of_ff = overfit(ff_train["expR"], ff_test["expR"])

    print(f"  {'方案':<12s} {'训练expR':>10s} {'验证expR':>10s} {'过拟合':>8s}", flush=True)
    print(f"  {'基准':<12s} {base_train['expR']:+.4f}    {base_test['expR']:+.4f}    {'—':>8s}", flush=True)
    print(f"  {'6因子无约束':<12s} {nc_train['expR']:+.4f}    {nc_test['expR']:+.4f}    {of_nc:+.2f}", flush=True)
    print(f"  {'6因子稳健':<12s} {rob_train['expR']:+.4f}    {rob_test['expR']:+.4f}    {of_rob:+.2f}", flush=True)
    print(f"  {'5因子稳健':<12s} {ff_train['expR']:+.4f}    {ff_test['expR']:+.4f}    {of_ff:+.2f}", flush=True)

    results[group] = {
        "n_symbols": len(group_data),
        "base": {"train": base_train, "test": base_test},
        "noconst": {"train": nc_train, "test": nc_test, "weights": w_nc, "overfit": of_nc},
        "robust6": {"train": rob_train, "test": rob_test, "weights": w_rob, "overfit": of_rob},
        "five_factor": {"train": ff_train, "test": ff_test, "weights": w5_raw, "overfit": of_ff},
    }

with open(OUTFILE, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n{'=' * 80}", flush=True)
print(f"结果已保存到: {OUTFILE}", flush=True)
