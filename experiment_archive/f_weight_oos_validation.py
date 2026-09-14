"""
P0 OOS 验证：F 权重优化的样本外稳健性测试
方案：walk-forward 多折交叉验证
- 每折：前 train_bars 训练（确定最优 F 权重），后 oos_bars 验证
- 滑动窗口，多折取平均
- 对比：默认权重 vs 训练最优权重在 OOS 上的表现
"""

import copy
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)


def run_bt_fw(sym, df_slice, f_weight, window=200):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    c_weight = cfg["combine_weights"]["C"]
    t_weight = 1.0 - f_weight - c_weight
    if t_weight < 0.05:
        t_weight = 0.05
        f_weight = 1.0 - t_weight - c_weight
    cfg["combine_weights"] = {"T": round(t_weight, 4), "F": round(f_weight, 4), "C": round(c_weight, 4)}
    r = walk_forward_backtest(sym, cfg=cfg, df_in=df_slice, window=window)
    return r


def find_best_f_weight(sym, df_train, f_candidates):
    """在训练集上找最优 F 权重。"""
    best_f = 0.25
    best_expr = -999

    for fw in f_candidates:
        r = run_bt_fw(sym, df_train, fw)
        expr = r.get("expR", 0)
        trades = r.get("trades", 0)
        # 交易数不能太少
        if trades < 8:
            continue
        if expr > best_expr:
            best_expr = expr
            best_f = fw

    return best_f, best_expr


def oos_fold(sym, df, train_end_idx, oos_bars=100, window=200):
    """跑一折 OOS：训练集确定最优 F，OOS 集验证。"""
    f_candidates = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]

    # 训练集（前 train_end_idx 根，需要足够 warmup）
    train_start = max(0, train_end_idx - 400)
    df_train = df.iloc[train_start:train_end_idx]

    # OOS 集（后 oos_bars 根，需要 window 根 warmup）
    oos_start = max(0, train_end_idx - window)
    oos_end = min(len(df), train_end_idx + oos_bars)
    if oos_end - oos_start < window + 10:
        return None

    df_oos = df.iloc[oos_start:oos_end]

    if len(df_train) < window + 50:
        return None

    # 训练：找最优 F
    best_f, best_train_expr = find_best_f_weight(sym, df_train, f_candidates)

    # OOS：默认权重 vs 最优权重
    r_default = run_bt_fw(sym, df_oos, 0.25)
    r_opt = run_bt_fw(sym, df_oos, best_f)

    default_expr = r_default.get("expR", 0)
    opt_expr = r_opt.get("expR", 0)

    return {
        "train_end": train_end_idx,
        "best_f": best_f,
        "train_expr": round(best_train_expr, 4),
        "default_oos_expr": round(default_expr, 4),
        "opt_oos_expr": round(opt_expr, 4),
        "delta_oos": round(opt_expr - default_expr, 4),
        "default_trades": r_default.get("trades", 0),
        "opt_trades": r_opt.get("trades", 0),
    }


def walk_forward_oos(sym, df, n_folds=5, oos_bars=100, window=200):
    """多折 walk-forward OOS 验证。"""
    n = len(df)
    if n < window + oos_bars + 200:
        return None

    # 计算每折的训练结束位置
    # 最后一折的 OOS 结束位置 = n
    # 第一折的训练结束位置 = n - n_folds * oos_bars
    first_train_end = n - n_folds * oos_bars
    if first_train_end < window + 100:
        # 减少折数
        n_folds = max(2, (n - window - 100) // oos_bars)
        first_train_end = n - n_folds * oos_bars

    folds = []
    for k in range(n_folds):
        train_end = first_train_end + k * oos_bars
        fold = oos_fold(sym, df, train_end, oos_bars, window)
        if fold:
            folds.append(fold)

    if not folds:
        return None

    # 汇总
    avg_default = np.mean([f["default_oos_expr"] for f in folds])
    avg_opt = np.mean([f["opt_oos_expr"] for f in folds])
    avg_delta = avg_opt - avg_default
    win_folds = sum(1 for f in folds if f["delta_oos"] > 0)
    total_default_trades = sum(f["default_trades"] for f in folds)
    total_opt_trades = sum(f["opt_trades"] for f in folds)
    avg_best_f = np.mean([f["best_f"] for f in folds])

    return {
        "symbol": sym,
        "n_folds": len(folds),
        "avg_default_expr": round(avg_default, 4),
        "avg_opt_expr": round(avg_opt, 4),
        "avg_delta": round(avg_delta, 4),
        "win_rate": round(win_folds / len(folds), 3),
        "total_default_trades": total_default_trades,
        "total_opt_trades": total_opt_trades,
        "avg_best_f": round(avg_best_f, 3),
        "folds": folds,
    }


def main():
    print("=" * 80)
    print("P0 OOS 验证：F 权重优化的 walk-forward 稳健性测试")
    print("=" * 80)
    print("  方案：多折 walk-forward，每折训练集确定最优 F，OOS 验证")
    print("  F 候选: 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7")

    all_syms = sorted(SYMBOLS.keys())
    results = {}

    print("\n[1/1] 全品种 OOS 验证...")
    for i, sym in enumerate(all_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 500:
                continue
            r = walk_forward_oos(sym, df, n_folds=5, oos_bars=100)
            if r:
                results[sym] = r
            marker = "↑" if r and r["avg_delta"] > 0.02 else ("↓" if r and r["avg_delta"] < -0.02 else "·")
            delta_str = f"{r['avg_delta']:+.3f}" if r else "N/A"
            print(
                f"  [{i + 1}/{len(all_syms)}] {sym:>5} {marker} Δ_OOS={delta_str}  胜率={r['win_rate'] * 100:.0f}%  平均F={r['avg_best_f']:.2f}",
                end="\r",
                flush=True,
            )
        except Exception:
            pass
    print()

    # 统计
    valid = results
    pos = {k: v for k, v in valid.items() if v["avg_delta"] > 0.02}
    neg = {k: v for k, v in valid.items() if v["avg_delta"] < -0.02}
    neutral = {k: v for k, v in valid.items() if -0.02 <= v["avg_delta"] <= 0.02}

    print("\n  OOS 结果汇总")
    print(f"  验证品种: {len(valid)} 个")
    print(f"  正收益（Δ>+0.02）: {len(pos)} 个")
    print(f"  负收益（Δ<-0.02）: {len(neg)} 个")
    print(f"  无显著变化: {len(neutral)} 个")

    if pos:
        pos_sorted = sorted(pos.items(), key=lambda x: x[1]["avg_delta"], reverse=True)
        print("\n  OOS 正收益品种（按 Δ 排序）:")
        print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'笔数':>8}  {'平均F':>6}")
        for sym, r in pos_sorted:
            print(
                f"    {sym:>5}  {r['avg_default_expr']:>8.3f}  {r['avg_opt_expr']:>8.3f}  "
                f"{r['avg_delta']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['total_default_trades']:>4}→{r['total_opt_trades']:<4}  {r['avg_best_f']:>6.2f}"
            )

    if neg:
        neg_sorted = sorted(neg.items(), key=lambda x: x[1]["avg_delta"])
        print("\n  OOS 负收益品种（按 Δ 排序）:")
        print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'笔数':>8}  {'平均F':>6}")
        for sym, r in neg_sorted:
            print(
                f"    {sym:>5}  {r['avg_default_expr']:>8.3f}  {r['avg_opt_expr']:>8.3f}  "
                f"{r['avg_delta']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['total_default_trades']:>4}→{r['total_opt_trades']:<4}  {r['avg_best_f']:>6.2f}"
            )

    # 保存
    os.makedirs("logs", exist_ok=True)
    with open("logs/f_weight_oos_validation.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/f_weight_oos_validation.json")


if __name__ == "__main__":
    main()
