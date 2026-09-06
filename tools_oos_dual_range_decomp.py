#!/usr/bin/env python3
"""双差值波动估计 OOS 验证 · 第二轮：通道分解 + 聚类 bootstrap。

第一轮结论（tools_oos_dual_range.py）：全链路替换组合级 +0.0508，但折级检验
不显著（p=0.38~0.79）。本轮回答两个问题：

① 效果来自哪个通道？
   - R 通道（行情分类）：classify_regime_array 的 atr_r → 波动/震荡边界
   - S 通道（止损/止盈）：risk_gate stop_pts + exit_plan 的 atr_val
   分解方式：双 patch。_atr_array 决定 atr_val（S 通道），classify_regime_array
   包装器决定 regime 输入（R 通道），stash 传递原始 hlc 数据。

② 组合级改善的不确定性有多大？
   品种级聚类 bootstrap（B=2000，品种为独立单元重采样）→ ΔexpR 的 95%CI。
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import four_dim_strategy as fds
from four_dim_strategy import DEFAULT_CONFIG, load_daily
from strategy_layer import _atr_array as orig_atr_array
from strategy_layer import classify_regime_array as orig_classify_regime_array

PANEL = ["RM", "m", "jd", "y", "p", "hc", "i", "cu", "ag", "FG", "MA", "ru"]
N_FOLDS = 5
WINDOW = 14
B_BOOT = 2000

_stash = {}


def dual_range(high, low, close, window):
    hh = pd.Series(high).rolling(window).max().values
    ll = pd.Series(low).rolling(window).min().values
    hc = pd.Series(close).rolling(window).max().values
    lc = pd.Series(close).rolling(window).min().values
    return np.maximum(hh - lc, hc - ll) / np.sqrt(window)


def atr_array_dr(high, low, close, window):
    _stash["hlc"] = (high, low, close, window)
    return dual_range(high, low, close, window)


def atr_array_orig(high, low, close, window):
    _stash["hlc"] = (high, low, close, window)
    return orig_atr_array(high, low, close, window)


def classify_regime_with_atr(close, atr14, sma20, sma20_slope_prev, params=None):
    """R 通道冻结：regime 用原始 ATR（S 通道实验用）。"""
    h, l, c, w = _stash["hlc"]
    return orig_classify_regime_array(close, orig_atr_array(h, l, c, w), sma20, sma20_slope_prev, params)


def classify_regime_with_dr(close, atr14, sma20, sma20_slope_prev, params=None):
    """R 通道激活：regime 用 DR/√N（R 通道实验用）。"""
    h, l, c, w = _stash["hlc"]
    return orig_classify_regime_array(close, dual_range(h, l, c, w), sma20, sma20_slope_prev, params)


def setup(variant):
    """variant: full / stop_only / regime_only / base。"""
    fds._atr_array = orig_atr_array
    fds.classify_regime_array = orig_classify_regime_array
    if variant == "base":
        pass
    elif variant == "full":
        fds._atr_array = atr_array_dr
    elif variant == "stop_only":  # 止损见 DR，regime 见 ATR
        fds._atr_array = atr_array_dr
        fds.classify_regime_array = classify_regime_with_atr
    elif variant == "regime_only":  # 止损见 ATR，regime 见 DR
        fds._atr_array = atr_array_orig
        fds.classify_regime_array = classify_regime_with_dr


def oos_folds(symbol):
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None
    total = len(df)
    fold_size = total // N_FOLDS
    out = []
    for fold in range(N_FOLDS):
        start = fold * fold_size
        end = start + fold_size if fold < N_FOLDS - 1 else total
        df_fold = df.iloc[start:end]
        if len(df_fold) < 80:
            continue
        r = fds.walk_forward_backtest(symbol, cfg=DEFAULT_CONFIG, df_in=df_fold)
        if r.get("trades", 0) > 0:
            out.append((r["expR"], r["trades"]))
    return out


def portfolio(results_by_sym):
    """交易加权组合 expR。"""
    num = den = 0.0
    for folds in results_by_sym.values():
        for e, t in folds:
            num += e * t
            den += t
    return num / den if den else np.nan


def main():
    all_runs = {}
    for variant in ["base", "full", "stop_only", "regime_only"]:
        setup(variant)
        runs = {}
        for sym in PANEL:
            folds = oos_folds(sym)
            if folds:
                runs[sym] = folds
        all_runs[variant] = runs
    setup("base")

    # ── 通道分解表 ──
    print(f"通道分解 · {N_FOLDS} 折走步 · 组合级（交易加权）")
    print(f"{'=' * 70}")
    base_port = portfolio(all_runs["base"])
    print(f"基线（ATR 全链路）:        expR {base_port:+.4f}")
    for variant, label in [("full", "全链路 DR"), ("stop_only", "仅止损通道 DR"), ("regime_only", "仅行情通道 DR")]:
        p = portfolio(all_runs[variant])
        print(f"{label:<18} expR {p:+.4f}  Δ {p - base_port:+.4f}")

    print()
    print(f"{'=' * 70}")
    print("品种级明细（Δ = 变体 − 基线，均值）")
    header = f"{'品种':<5}"
    for _, label in [("full", "全链路"), ("stop_only", "仅止损"), ("regime_only", "仅行情")]:
        header += f"{label:>10}"
    print(header)
    for sym in PANEL:
        row = f"{sym:<5}"
        for variant in ["full", "stop_only", "regime_only"]:
            b = np.mean([e for e, _ in all_runs["base"].get(sym, [])])
            t = np.mean([e for e, _ in all_runs[variant].get(sym, [])])
            row += f"{t - b:>+10.4f}"
        print(row)

    # ── 聚类 bootstrap（全链路 vs 基线）──
    print()
    print(f"{'=' * 70}")
    print(f"品种级聚类 bootstrap（B={B_BOOT}，品种重采样）· 全链路 DR vs ATR")
    rng = np.random.default_rng(42)
    syms = [s for s in PANEL if s in all_runs["base"] and s in all_runs["full"]]
    deltas = []
    for _ in range(B_BOOT):
        sample = rng.choice(syms, size=len(syms), replace=True)
        b_runs = {s: all_runs["base"][s] for s in sample}
        t_runs = {s: all_runs["full"][s] for s in sample}
        deltas.append(portfolio(t_runs) - portfolio(b_runs))
    deltas = np.array(deltas)
    lo, hi = np.percentile(deltas, [2.5, 97.5])
    p_le0 = float((deltas <= 0).mean())
    print(f"  ΔexpR 中位数 {np.median(deltas):+.4f} · 均值 {deltas.mean():+.4f}")
    print(f"  95%CI [{lo:+.4f}, {hi:+.4f}] · P(Δ<=0) = {p_le0:.1%}")


if __name__ == "__main__":
    main()
