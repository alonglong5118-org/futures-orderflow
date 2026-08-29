"""
GA 优化权重 OOS 验证（修正前视偏差后 v2）
- 走步法：每段训练 GA 权重，在后续一段 OOS 验证
- 5 个板块 × 训练集(前400) / 验证集(后200)
- 对比：默认权重 vs GA 优化权重
"""

import copy
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest
from ga_group_six_factor import SF_NAMES, optimize_group

MIN_TRADES = 5
SECTORS = ["化工", "农产品", "有色", "黑系", "能源"]

# 走步法设置：训练集大小 / OOS 大小
TRAIN_TAIL = 600  # 训练用 600 根（前400训练+后200验证？不，用更早的数据训练，最近的数据OOS）
# 用 800 根数据：前 600 根训练，后 200 根 OOS
TOTAL_BARS = 800
TRAIN_BARS = 600  # 前 600 根 = 训练集
OOS_BARS = 200  # 后 200 根 = 样本外


def load_group_data(group, tail=0):
    syms = [s for s, info in SYMBOLS.items() if info.get("group") == group]
    data = {}
    for sym in syms:
        try:
            df = load_daily(sym)
            if df is None or len(df) < 200:
                continue
            if tail and len(df) > tail:
                df = df.tail(tail)
            data[sym] = df
        except Exception:
            continue
    return data


def evaluate_weights(weights, group_data):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["subfactor_weights"] = weights
    expRs = []
    total_trades = 0
    for sym, df in group_data.items():
        try:
            r = walk_forward_backtest(sym, cfg=cfg, window=300, min_bars=60, df_in=df)
            nt = int(r.get("trades", 0))
            if nt >= MIN_TRADES:
                expRs.append(float(r.get("expR", 0)))
                total_trades += nt
        except Exception:
            pass
    if not expRs:
        return -5.0, 0, 0
    return float(np.mean(expRs)), len(expRs), total_trades


def main():
    results = {}

    for sector in SECTORS:
        print(f"\n{'=' * 70}")
        print(f"【{sector}】OOS 样本外验证")
        print(f"{'=' * 70}")

        # 加载完整数据
        full_data = load_group_data(sector, tail=TOTAL_BARS)
        if len(full_data) < 3:
            print(f"  有效品种不足（{len(full_data)}个），跳过")
            continue

        # 切分训练集 / OOS 集
        train_data = {}
        oos_data = {}
        for sym, df in full_data.items():
            if len(df) < TOTAL_BARS:
                # 数据不够，按比例切
                split = int(len(df) * 0.75)
            else:
                split = TRAIN_BARS
            train_data[sym] = df.iloc[:split]
            oos_data[sym] = df.iloc[split:]

        n_train = len([s for s, d in train_data.items() if len(d) >= 200])
        n_oos = len([s for s, d in oos_data.items() if len(d) >= 100])
        print(f"  训练集: {n_train} 个品种 (~{TRAIN_BARS}根)")
        print(f"  OOS 集: {n_oos} 个品种 (~{OOS_BARS}根)")

        # 1. 默认权重在训练集和 OOS 集的表现
        default_w = {
            "T_trend": 0.20,
            "T_mean": 0.15,
            "T_seasonal": 0.05,
            "F_basis": 0.20,
            "F_seasonal": 0.10,
            "C": 0.30,
        }

        e_default_train, n_default_train, _ = evaluate_weights(default_w, train_data)
        e_default_oos, n_default_oos, _ = evaluate_weights(default_w, oos_data)

        print("\n  [默认权重]")
        print(f"    训练集 expR: {e_default_train:+.4f} ({n_default_train} 品种)")
        print(f"    OOS 集 expR:  {e_default_oos:+.4f} ({n_default_oos} 品种)")
        oos_decay_default = e_default_oos - e_default_train
        print(f"    OOS 衰减:     {oos_decay_default:+.4f}")

        # 2. GA 优化权重（只用训练集优化）
        print("\n  [GA 优化 (训练集)]")
        ga_result = optimize_group(sector, pop_size=20, n_gen=10, verbose=False, tail=0, min_bars=200)

        if "error" in ga_result:
            print(f"    优化失败: {ga_result['error']}")
            continue

        ga_w = ga_result["best_weights"]
        e_ga_train = ga_result["best_avg_expR"]
        n_ga_train = ga_result["n_valid_symbols"]
        print(f"    最优权重: {ga_w}")
        print(f"    训练集 expR: {e_ga_train:+.4f} ({n_ga_train} 品种)")

        # GA 权重在 OOS 集上验证
        e_ga_oos, n_ga_oos, _ = evaluate_weights(ga_w, oos_data)
        print(f"    OOS 集 expR:  {e_ga_oos:+.4f} ({n_ga_oos} 品种)")
        oos_decay_ga = e_ga_oos - e_ga_train
        print(f"    OOS 衰减:     {oos_decay_ga:+.4f}")

        # 3. 对比 GA vs 默认
        train_gain = e_ga_train - e_default_train
        oos_gain = e_ga_oos - e_default_oos
        overfit_coef = oos_gain / train_gain if train_gain > 0.001 else float("inf")

        print("\n  [GA vs 默认 对比]")
        print(f"    训练集提升: {train_gain:+.4f} ({train_gain / e_default_train * 100:+.1f}%)")
        print(
            f"    OOS 集提升:  {oos_gain:+.4f} ({oos_gain / e_default_oos * 100:+.1f}%)"
            if e_default_oos != 0
            else f"    OOS 集提升:  {oos_gain:+.4f}"
        )
        print(
            f"    过拟合系数:  {overfit_coef:.2f}"
            if overfit_coef != float("inf")
            else "    过拟合系数:  N/A (训练集无提升)"
        )

        verdict = ""
        if oos_gain > 0 and overfit_coef > 0.5:
            verdict = "✅ 有效提升"
        elif oos_gain > 0 and overfit_coef > 0.3:
            verdict = "⚠️ 微弱有效"
        elif oos_gain > 0:
            verdict = "⚠️ 过拟合严重"
        else:
            verdict = "❌ OOS 失效"
        print(f"    结论: {verdict}")

        # 4. 等权重 OOS 对比
        equal_w = {n: 1 / 6 for n in SF_NAMES}
        e_eq_oos, n_eq_oos, _ = evaluate_weights(equal_w, oos_data)
        print(f"\n  [等权重 OOS] expR: {e_eq_oos:+.4f} ({n_eq_oos} 品种)")

        results[sector] = {
            "default": {
                "train_expR": e_default_train,
                "oos_expR": e_default_oos,
                "n_train": n_default_train,
                "n_oos": n_default_oos,
            },
            "ga": {
                "weights": ga_w,
                "train_expR": e_ga_train,
                "oos_expR": e_ga_oos,
                "n_train": n_ga_train,
                "n_oos": n_ga_oos,
                "oos_decay": oos_decay_ga,
            },
            "comparison": {
                "train_gain": train_gain,
                "oos_gain": oos_gain,
                "overfit_coef": overfit_coef,
                "verdict": verdict,
            },
            "equal_weight_oos": e_eq_oos,
        }

    # 汇总
    print(f"\n\n{'=' * 70}")
    print("全板块 OOS 验证汇总")
    print(f"{'=' * 70}")
    print(f"{'板块':<8}{'默认OOS':>10}{'GA_OOS':>10}{'OOS提升':>10}{'过拟合系数':>12}{'结论':>14}")
    print("-" * 70)

    for sector, r in results.items():
        d = r["default"]["oos_expR"]
        g = r["ga"]["oos_expR"]
        gain = r["comparison"]["oos_gain"]
        ofc = r["comparison"]["overfit_coef"]
        v = r["comparison"]["verdict"]
        ofc_str = f"{ofc:.2f}" if ofc != float("inf") else "N/A"
        print(f"{sector:<8}{d:>+10.4f}{g:>+10.4f}{gain:>+10.4f}{ofc_str:>12}{v:>14}")

    # 保存
    out_path = "logs/ga_oos_v2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
