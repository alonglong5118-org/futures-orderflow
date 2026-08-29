"""
P0 v2: 全品种 F 权重扫描 + fc_confirm 失效深度诊断
"""

import copy
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fundamental_feed as ff
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    _atr_array,
    load_daily,
    precompute_C_array,
    precompute_T_array,
    regime_params_for,
    walk_forward_backtest,
)
from strategy_layer import (
    _rolling_max_array,
    _rolling_min_array,
    _rolling_std_array,
    _rsi_array,
    _seasonal_month_stats,
    _sma_array,
    classify_regime_array,
    precompute_signals,
)


def calc_extended(result):
    trades = result.get("trades_detail", [])
    if not trades:
        return {"total_R": 0, "max_dd": 0, "pf": 0}
    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r < 0]
    cum = np.cumsum(Rs)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.max(peak - cum))
    total_R = float(np.sum(Rs))
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 99.9
    return {"total_R": round(total_R, 2), "max_dd": round(max_dd, 3), "pf": round(pf, 2)}


def sweep_f_weight(sym, df):
    """扫描 F 权重（T 权重相应调整，C 不变）。"""
    c_weight = DEFAULT_CONFIG["combine_weights"]["C"]  # 0.15
    f_weights = [0.0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7]

    baseline = None
    results = []

    for fw in f_weights:
        t_weight = 1.0 - fw - c_weight
        if t_weight < 0.05:
            continue
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["combine_weights"] = {"T": round(t_weight, 4), "F": round(fw, 4), "C": round(c_weight, 4)}

        r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=200)
        ext = calc_extended(r)

        entry = {
            "f_weight": fw,
            "t_weight": round(t_weight, 4),
            "expR": r.get("expR", 0),
            "win_rate": r.get("win_rate", 0),
            "trades": r.get("trades", 0),
            **ext,
        }

        if abs(fw - 0.25) < 0.001:
            baseline = entry
        results.append(entry)

    return baseline, results


def diagnose_fc(sym, df):
    """诊断 fc_confirm 为什么失效：统计 bias_FC 分布、同向比例等。"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    months = df.index.month.values
    date_ints = df.index.year.values * 10000 + df.index.month.values * 100 + df.index.day.values

    F_arr = ff.precompute_F_array(sym, date_ints=date_ints, months=months)
    C_arr = precompute_C_array(sym, date_ints=date_ints)

    # T 数组
    sma5 = _sma_array(close, 5)
    sma20 = _sma_array(close, 20)
    sma60 = _sma_array(close, 60)
    rsi14 = _rsi_array(close, 14)
    std20 = _rolling_std_array(close, 20)
    hh20 = _rolling_max_array(high, 20)
    ll20 = _rolling_min_array(low, 20)
    hh55 = _rolling_max_array(high, 55)
    ll55 = _rolling_min_array(low, 55)
    rets = np.empty(len(close))
    rets[0] = np.nan
    rets[1:] = np.diff(close) / close[:-1]
    seas_cnt, seas_sum, seas_sumsq = _seasonal_month_stats(rets, months)
    atr14 = _atr_array(high, low, close, 14)
    sig_arrays = precompute_signals(
        close,
        high,
        low,
        months,
        rets,
        sma5,
        sma20,
        sma60,
        rsi14,
        std20,
        hh20,
        ll20,
        hh55,
        ll55,
        seas_cnt,
        seas_sum,
        seas_sumsq,
    )
    sma20_slope_prev = np.concatenate([np.full(4, np.nan), sma20[:-4]])
    rp = regime_params_for(sym, DEFAULT_CONFIG)
    regime_codes = classify_regime_array(close, atr14, sma20, sma20_slope_prev, rp)
    group = SYMBOLS.get(sym, {}).get("group")
    T_arr = precompute_T_array(sig_arrays, regime_codes, DEFAULT_CONFIG, group)

    bias_FC = np.round(0.25 * F_arr + 0.15 * C_arr, 1)
    bias_G = 0.6 * T_arr + 0.25 * F_arr + 0.15 * C_arr
    dir_T = np.sign(T_arr).astype(np.int8)
    fc_sign = np.sign(bias_FC)

    valid = ~np.isnan(T_arr) & (dir_T != 0)
    n_valid = np.sum(valid)

    # bias_FC 统计
    abs_fc = np.abs(bias_FC)
    pct_ge5 = np.sum(abs_fc >= 5) / len(abs_fc) * 100
    pct_ge10 = np.sum(abs_fc >= 10) / len(abs_fc) * 100
    pct_ge25 = np.sum(abs_fc >= 25) / len(abs_fc) * 100

    # 同向比例（只在 dir_T != 0 的位置）
    if n_valid > 0:
        same_dir_fc = np.sum((fc_sign == dir_T) & valid) / n_valid * 100
        # fc_confirm=5 时同向且达标比例
        fc_align_5 = np.sum((fc_sign == dir_T) & (abs_fc >= 5) & valid) / n_valid * 100
        fc_align_10 = np.sum((fc_sign == dir_T) & (abs_fc >= 10) & valid) / n_valid * 100
        fc_align_25 = np.sum((fc_sign == dir_T) & (abs_fc >= 25) & valid) / n_valid * 100
    else:
        same_dir_fc = fc_align_5 = fc_align_10 = fc_align_25 = 0

    return {
        "F_mean": round(float(np.mean(F_arr)), 2),
        "F_std": round(float(np.std(F_arr)), 2),
        "bias_FC_mean": round(float(np.mean(bias_FC)), 2),
        "bias_FC_std": round(float(np.std(bias_FC)), 2),
        "pct_FC_ge5": round(pct_ge5, 1),
        "pct_FC_ge10": round(pct_ge10, 1),
        "pct_FC_ge25": round(pct_ge25, 1),
        "same_dir_pct": round(same_dir_fc, 1),
        "fc_align_5pct": round(fc_align_5, 1),
        "fc_align_10pct": round(fc_align_10, 1),
        "fc_align_25pct": round(fc_align_25, 1),
        "dir_nonzero_pct": round(n_valid / len(T_arr) * 100, 1),
    }


def main():
    print("=" * 80)
    print("P0 v2: 全品种 F 权重扫描 + fc_confirm 失效诊断")
    print("=" * 80)

    all_syms = sorted(SYMBOLS.keys())
    results = {}
    diagnoses = {}

    print("\n[1/2] F 权重全品种扫描...")
    for i, sym in enumerate(all_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 260:
                continue
            baseline, sweep = sweep_f_weight(sym, df)
            diag = diagnose_fc(sym, df)
            results[sym] = {"baseline": baseline, "sweep": sweep}
            diagnoses[sym] = diag

            best = max(sweep, key=lambda x: x["expR"])
            delta = best["expR"] - baseline["expR"] if baseline else 0
            marker = "↑" if delta > 0.03 else ("↓" if delta < -0.03 else "·")
            print(
                f"  [{i + 1}/{len(all_syms)}] {sym:>5} {marker} base={baseline['expR']:.3f} best={best['expR']:.3f}(F={best['f_weight']:.1f}) Δ={delta:+.3f}",
                end="\r",
                flush=True,
            )
        except Exception:
            pass
    print()

    # 汇总
    valid_results = {k: v for k, v in results.items() if v["baseline"] and v["baseline"]["trades"] >= 20}

    # 分类：F 权重提升有正面效果 / 负面 / 无影响
    positive_impact = []
    negative_impact = []
    no_impact = []

    for sym, data in valid_results.items():
        b = data["baseline"]
        best = max(data["sweep"], key=lambda x: x["expR"])
        delta = best["expR"] - b["expR"]
        if delta > 0.03:
            positive_impact.append((sym, b["expR"], best["expR"], best["f_weight"], delta, b["trades"]))
        elif delta < -0.03:
            negative_impact.append((sym, b["expR"], best["expR"], best["f_weight"], delta, b["trades"]))
        else:
            no_impact.append((sym, b["expR"], best["expR"], best["f_weight"], delta, b["trades"]))

    print("\n[2/2] 汇总")
    print(f"  有效品种（≥20笔）: {len(valid_results)} 个")
    print(f"  F 权重优化正收益（Δ>+0.03）: {len(positive_impact)} 个")
    print(f"  F 权重优化负收益（Δ<-0.03）: {len(negative_impact)} 个")
    print(f"  影响不显著: {len(no_impact)} 个")

    if positive_impact:
        positive_impact.sort(key=lambda x: x[4], reverse=True)
        print("\n  正收益品种（按 Δ 排序）:")
        print(f"    {'品种':>5}  {'基线expR':>8}  {'最优expR':>8}  {'最优F':>6}  {'Δ':>7}  {'笔数':>5}")
        for sym, b_expr, best_expr, best_fw, delta, trades in positive_impact:
            print(f"    {sym:>5}  {b_expr:>8.3f}  {best_expr:>8.3f}  {best_fw:>6.2f}  {delta:>+7.3f}  {trades:>5}")

    if negative_impact:
        negative_impact.sort(key=lambda x: x[4])
        print("\n  负收益品种（按 Δ 排序）:")
        print(f"    {'品种':>5}  {'基线expR':>8}  {'最优expR':>8}  {'最优F':>6}  {'Δ':>7}  {'笔数':>5}")
        for sym, b_expr, best_expr, best_fw, delta, trades in negative_impact:
            print(f"    {sym:>5}  {b_expr:>8.3f}  {best_expr:>8.3f}  {best_fw:>6.2f}  {delta:>+7.3f}  {trades:>5}")

    # fc_confirm 失效诊断汇总
    print("\n  fc_confirm 失效诊断（bias_FC 分布）:")
    print(
        f"    {'品种':>5}  {'F_mean':>7}  {'FC_mean':>7}  {'FC≥5%':>7}  {'FC≥10%':>7}  {'FC≥25%':>7}  {'同向%':>6}  {'对齐≥5%':>8}  {'对齐≥25%':>9}"
    )

    diag_sorted = sorted(diagnoses.items(), key=lambda x: x[1]["pct_FC_ge25"], reverse=True)
    for sym, d in diag_sorted[:15]:
        print(
            f"    {sym:>5}  {d['F_mean']:>7.1f}  {d['bias_FC_mean']:>7.1f}  {d['pct_FC_ge5']:>6.1f}%  "
            f"{d['pct_FC_ge10']:>6.1f}%  {d['pct_FC_ge25']:>6.1f}%  {d['same_dir_pct']:>5.1f}%  "
            f"{d['fc_align_5pct']:>7.1f}%  {d['fc_align_25pct']:>8.1f}%"
        )

    # 保存
    os.makedirs("logs", exist_ok=True)
    out = {
        "f_weight_sweep": results,
        "fc_diagnosis": diagnoses,
        "summary": {
            "positive_impact": len(positive_impact),
            "negative_impact": len(negative_impact),
            "no_impact": len(no_impact),
        },
    }
    with open("logs/f_weight_full_sweep.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/f_weight_full_sweep.json")


if __name__ == "__main__":
    main()
