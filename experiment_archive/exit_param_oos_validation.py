"""
P1 OOS 验证：退出参数（stop_atr_mult + rr_ratio）的 walk-forward 稳健性测试
方案：多折 walk-forward，每折训练集确定最优参数，OOS 集验证
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
    load_daily,
    walk_forward_backtest,
)


def run_bt(sym, df_slice, stop_mult, rr_ratio, window=200):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    psr = cfg.setdefault("per_symbol_risk", {})
    sym_cfg = psr.get(sym, {})
    sym_cfg["stop_atr_mult"] = stop_mult
    sym_cfg["rr_ratio"] = rr_ratio
    psr[sym] = sym_cfg
    r = walk_forward_backtest(sym, cfg=cfg, df_in=df_slice, window=window)
    return r


def find_best_params(sym, df_train, candidates):
    """在训练集上找最优 (stop, rr) 组合。"""
    best = None
    best_score = -999

    for sm, rr in candidates:
        r = run_bt(sym, df_train, sm, rr)
        expr = r.get("expR", 0)
        trades = r.get("trades", 0)
        if trades < 8:
            continue
        # 计算 max_dd
        trades_detail = r.get("trades_detail", [])
        if trades_detail:
            Rs = [t["R_adj"] for t in trades_detail]
            cum = np.cumsum(Rs)
            peak = np.maximum.accumulate(cum)
            max_dd = float(np.max(peak - cum))
        else:
            max_dd = 0
        # 综合得分：expR - 0.1 * max_dd
        score = expr * 100 - max_dd * 0.5
        if score > best_score:
            best_score = score
            best = (sm, rr, expr, max_dd)

    return best


def oos_fold(sym, df, train_end_idx, oos_bars=100, window=200):
    """跑一折 OOS。"""
    # 参数候选（粗粒度）
    candidates = [
        (1.0, 2.0),
        (1.0, 3.0),
        (1.0, 4.0),
        (1.5, 1.5),
        (1.5, 2.0),
        (1.5, 2.5),
        (1.5, 3.0),
        (1.5, 4.0),
        (2.0, 2.0),
        (2.0, 2.5),
        (2.0, 3.0),
        (2.0, 4.0),
        (2.5, 2.0),
        (2.5, 3.0),
        (2.5, 4.0),
        (3.0, 2.0),
        (3.0, 3.0),
    ]

    # 训练集
    train_start = max(0, train_end_idx - 400)
    df_train = df.iloc[train_start:train_end_idx]

    # OOS 集
    oos_start = max(0, train_end_idx - window)
    oos_end = min(len(df), train_end_idx + oos_bars)
    if oos_end - oos_start < window + 10:
        return None
    df_oos = df.iloc[oos_start:oos_end]

    if len(df_train) < window + 50:
        return None

    # 当前默认参数
    psr = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(sym, {})
    def_stop = psr.get("stop_atr_mult", 1.5)
    def_rr = psr.get("rr_ratio", 2.0)

    # 训练：找最优
    best = find_best_params(sym, df_train, candidates)
    if not best:
        return None

    best_stop, best_rr, train_expr, train_dd = best

    # OOS：默认 vs 最优
    r_default = run_bt(sym, df_oos, def_stop, def_rr)
    r_opt = run_bt(sym, df_oos, best_stop, best_rr)

    return {
        "train_end": train_end_idx,
        "best_stop": best_stop,
        "best_rr": best_rr,
        "train_expr": round(train_expr, 4),
        "default_oos_expr": round(r_default.get("expR", 0), 4),
        "opt_oos_expr": round(r_opt.get("expR", 0), 4),
        "delta_oos": round(r_opt.get("expR", 0) - r_default.get("expR", 0), 4),
        "default_trades": r_default.get("trades", 0),
        "opt_trades": r_opt.get("trades", 0),
    }


def walk_forward_oos(sym, df, n_folds=5, oos_bars=100, window=200):
    """多折 walk-forward OOS。"""
    n = len(df)
    if n < window + oos_bars + 200:
        return None

    first_train_end = n - n_folds * oos_bars
    if first_train_end < window + 100:
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

    avg_default = np.mean([f["default_oos_expr"] for f in folds])
    avg_opt = np.mean([f["opt_oos_expr"] for f in folds])
    avg_delta = avg_opt - avg_default
    win_folds = sum(1 for f in folds if f["delta_oos"] > 0)
    avg_stop = np.mean([f["best_stop"] for f in folds])
    avg_rr = np.mean([f["best_rr"] for f in folds])

    return {
        "symbol": sym,
        "n_folds": len(folds),
        "avg_default_expr": round(avg_default, 4),
        "avg_opt_expr": round(avg_opt, 4),
        "avg_delta": round(avg_delta, 4),
        "win_rate": round(win_folds / len(folds), 3),
        "avg_best_stop": round(avg_stop, 2),
        "avg_best_rr": round(avg_rr, 2),
        "total_default_trades": sum(f["default_trades"] for f in folds),
        "total_opt_trades": sum(f["opt_trades"] for f in folds),
        "folds": folds,
    }


def main():
    print("=" * 80)
    print("P1 OOS 验证：退出参数（stop + rr）walk-forward 稳健性测试")
    print("=" * 80)

    # 有 per_symbol_risk 配置的品种（数据量足够的）
    test_syms = sorted(DEFAULT_CONFIG.get("per_symbol_risk", {}).keys())
    # 加上几个有代表性的
    extra = ["rb", "hc", "FG", "au", "ru", "CF", "ss"]
    for s in extra:
        if s not in test_syms:
            test_syms.append(s)

    print(f"  测试品种数: {len(test_syms)}")
    print()

    results = {}
    for i, sym in enumerate(test_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 500:
                continue
            r = walk_forward_oos(sym, df, n_folds=5, oos_bars=100)
            if r:
                results[sym] = r
            marker = "↑" if r and r["avg_delta"] > 0.05 else ("↓" if r and r["avg_delta"] < -0.05 else "·")
            delta_str = f"{r['avg_delta']:+.3f}" if r else "N/A"
            print(
                f"  [{i + 1}/{len(test_syms)}] {sym:>5} {marker} Δ_OOS={delta_str}  胜率={r['win_rate'] * 100:.0f}%  stop={r['avg_best_stop']:.1f}  rr={r['avg_best_rr']:.1f}",
                end="\r",
                flush=True,
            )
        except Exception:
            pass
    print()

    # 统计
    valid = results
    pos = {k: v for k, v in valid.items() if v["avg_delta"] > 0.05}
    neg = {k: v for k, v in valid.items() if v["avg_delta"] < -0.05}
    neutral = {k: v for k, v in valid.items() if -0.05 <= v["avg_delta"] <= 0.05}

    print("\n  OOS 结果汇总")
    print(f"  验证品种: {len(valid)} 个")
    print(f"  正收益（Δ>+0.05）: {len(pos)} 个")
    print(f"  负收益（Δ<-0.05）: {len(neg)} 个")
    print(f"  无显著变化: {len(neutral)} 个")

    if pos:
        pos_sorted = sorted(pos.items(), key=lambda x: x[1]["avg_delta"], reverse=True)
        print("\n  OOS 正收益品种（Top 15）:")
        print(
            f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'stop':>5}  {'rr':>5}  {'笔数':>8}"
        )
        for sym, r in pos_sorted[:15]:
            print(
                f"    {sym:>5}  {r['avg_default_expr']:>8.3f}  {r['avg_opt_expr']:>8.3f}  "
                f"{r['avg_delta']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['avg_best_stop']:>5.1f}  {r['avg_best_rr']:>5.1f}  "
                f"{r['total_default_trades']:>4}→{r['total_opt_trades']:<4}"
            )

    if neg:
        neg_sorted = sorted(neg.items(), key=lambda x: x[1]["avg_delta"])
        print("\n  OOS 负收益品种:")
        print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'stop':>5}  {'rr':>5}")
        for sym, r in neg_sorted[:10]:
            print(
                f"    {sym:>5}  {r['avg_default_expr']:>8.3f}  {r['avg_opt_expr']:>8.3f}  "
                f"{r['avg_delta']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                f"{r['avg_best_stop']:>5.1f}  {r['avg_best_rr']:>5.1f}"
            )

    # 保存
    os.makedirs("logs", exist_ok=True)
    with open("logs/exit_param_oos_validation.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/exit_param_oos_validation.json")


if __name__ == "__main__":
    main()
