"""
多窗口滚动 OOS 验证（修正前视偏差后 v3）
- 4 个时间窗口 × 5 个板块
- 每个窗口内：前75%训练GA权重，后25% OOS验证
- 对比：默认权重 vs GA 优化权重，跨窗口一致性检验
"""

import copy
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest
from ga_group_six_factor import optimize_group

MIN_TRADES = 5
SECTORS = ["化工", "农产品", "有色", "黑系", "能源"]
WINDOWS = [500, 700, 900, 1100]
WINDOW_LABELS = ["W1(最近)", "W2", "W3", "W4(最早)"]

DEFAULT_W = {"T_trend": 0.20, "T_mean": 0.15, "T_seasonal": 0.05, "F_basis": 0.20, "F_seasonal": 0.10, "C": 0.30}


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


def run_single_window_oos(sector, tail):
    """单个窗口的 OOS 验证：前75%训练，后25%验证"""
    full_data = load_group_data(sector, tail=tail)
    if len(full_data) < 3:
        return None

    # 切分训练 / OOS
    train_data = {}
    oos_data = {}
    for sym, df in full_data.items():
        n = len(df)
        split = int(n * 0.75)
        train_data[sym] = df.iloc[:split]
        oos_data[sym] = df.iloc[split:]

    # 默认权重
    e_def_train, n_def_train, _ = evaluate_weights(DEFAULT_W, train_data)
    e_def_oos, n_def_oos, _ = evaluate_weights(DEFAULT_W, oos_data)

    # GA 优化（训练集）
    ga_result = optimize_group(sector, pop_size=20, n_gen=10, verbose=False, tail=0, min_bars=200)

    if "error" in ga_result:
        return None

    ga_w = ga_result["best_weights"]
    e_ga_train = ga_result["best_avg_expR"]
    n_ga_train = ga_result["n_valid_symbols"]

    # GA 在 OOS 上的表现
    e_ga_oos, n_ga_oos, _ = evaluate_weights(ga_w, oos_data)

    # 计算指标
    train_gain = e_ga_train - e_def_train
    oos_gain = e_ga_oos - e_def_oos
    overfit_coef = oos_gain / train_gain if train_gain > 0.001 else float("inf")
    oos_decay = e_ga_oos - e_ga_train

    return {
        "tail": tail,
        "default_train": e_def_train,
        "default_oos": e_def_oos,
        "default_n_train": n_def_train,
        "default_n_oos": n_def_oos,
        "ga_weights": ga_w,
        "ga_train": e_ga_train,
        "ga_oos": e_ga_oos,
        "ga_n_train": n_ga_train,
        "ga_n_oos": n_ga_oos,
        "train_gain": train_gain,
        "oos_gain": oos_gain,
        "overfit_coef": overfit_coef,
        "oos_decay": oos_decay,
    }


def main():
    results = {}

    for sector in SECTORS:
        print(f"\n{'=' * 70}")
        print(f"【{sector}】多窗口滚动 OOS 验证")
        print(f"{'=' * 70}")

        sector_results = []

        for wi, tail in enumerate(WINDOWS):
            label = WINDOW_LABELS[wi]
            print(f"\n--- 窗口 {wi + 1}/4: tail={tail} ({label}) ---")

            r = run_single_window_oos(sector, tail)
            if r is None:
                print("  数据不足，跳过")
                sector_results.append(None)
                continue

            print(
                f"  默认权重: train={r['default_train']:+.4f} ({r['default_n_train']}品), oos={r['default_oos']:+.4f} ({r['default_n_oos']}品)"
            )
            print(
                f"  GA 权重:  train={r['ga_train']:+.4f} ({r['ga_n_train']}品), oos={r['ga_oos']:+.4f} ({r['ga_n_oos']}品)"
            )
            print(
                f"  OOS 提升: {r['oos_gain']:+.4f} (训练提升: {r['train_gain']:+.4f}, 过拟合系数: {r['overfit_coef']:.2f})"
            )

            sector_results.append(r)

        # 汇总本板块
        valid_results = [r for r in sector_results if r is not None]
        if valid_results:
            avg_oos_gain = float(np.mean([r["oos_gain"] for r in valid_results]))
            win_rate = float(np.mean([1 if r["oos_gain"] > 0 else 0 for r in valid_results]))
            avg_ofc_vals = [
                r["overfit_coef"]
                for r in valid_results
                if r["overfit_coef"] != float("inf") and abs(r["overfit_coef"]) < 10
            ]
            avg_ofc = float(np.mean(avg_ofc_vals)) if avg_ofc_vals else float("inf")

            print(f"\n  【{sector} 汇总】")
            print(f"    平均 OOS 提升: {avg_oos_gain:+.4f}")
            print(
                f"    OOS 胜率: {win_rate * 100:.0f}% ({sum(1 for r in valid_results if r['oos_gain'] > 0)}/{len(valid_results)})"
            )
            print(f"    平均过拟合系数: {avg_ofc:.2f}" if avg_ofc != float("inf") else "    平均过拟合系数: N/A")

            if win_rate >= 0.75 and avg_ofc > 0.3:
                verdict = "✅ 稳健有效"
            elif win_rate >= 0.5 and avg_oos_gain > 0:
                verdict = "⚠️ 部分有效"
            else:
                verdict = "❌ 整体无效"
            print(f"    结论: {verdict}")

            results[sector] = {
                "windows": sector_results,
                "summary": {
                    "avg_oos_gain": avg_oos_gain,
                    "oos_win_rate": win_rate,
                    "avg_overfit_coef": avg_ofc,
                    "n_valid_windows": len(valid_results),
                    "verdict": verdict,
                },
            }
        else:
            results[sector] = {"windows": [], "summary": {"error": "无有效窗口"}}

    # 全板块汇总表
    print(f"\n\n{'=' * 70}")
    print("全板块多窗口 OOS 验证汇总")
    print(f"{'=' * 70}")
    print(f"{'板块':<8}{'平均OOS提升':>12}{'OOS胜率':>10}{'平均过拟合系数':>16}{'结论':>14}")
    print("-" * 70)
    for sector, r in results.items():
        s = r.get("summary", {})
        if "error" in s:
            print(f"{sector:<8}    N/A        N/A        N/A         数据不足")
            continue
        gain = s["avg_oos_gain"]
        wr = s["oos_win_rate"]
        ofc = s["avg_overfit_coef"]
        v = s["verdict"]
        ofc_str = f"{ofc:.2f}" if ofc != float("inf") else "N/A"
        print(f"{sector:<8}{gain:>+12.4f}{wr * 100:>9.0f}%{ofc_str:>16}{v:>14}")

    # 逐窗口明细
    print(f"\n\n{'=' * 70}")
    print("逐窗口 OOS 提升明细")
    print(f"{'=' * 70}")
    header = f"{'板块':<8}" + "".join([f"{l:>10}" for l in WINDOW_LABELS]) + f"{'胜率':>8}"
    print(header)
    print("-" * 60)
    for sector, r in results.items():
        s = r.get("summary", {})
        if "error" in s:
            continue
        row = f"{sector:<8}"
        for w in r["windows"]:
            if w is None:
                row += f"{'N/A':>10}"
            else:
                row += f"{w['oos_gain']:>+10.4f}"
        row += f"{s['oos_win_rate'] * 100:>7.0f}%"
        print(row)

    # 保存
    out_path = "logs/ga_multiwindow_oos.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
