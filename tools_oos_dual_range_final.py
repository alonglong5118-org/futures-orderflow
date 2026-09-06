#!/usr/bin/env python3
"""双差值波动估计 OOS 验证 · 第三轮（终版）：采纳组合最终评估。

前两轮结论：
  ① 全链路替换组合级 +0.0508，但折级不显著（p=0.38~0.79）；
  ② 通道分解：改善 100% 来自止损/止盈通道（Δ+0.0675），行情分类通道为负（Δ-0.0340）；
  ③ 品种级筛选：RM/m/p/FG/ru 五品种折胜率>=60% 且均值改善为正 → 采纳候选。

本轮：采纳组合（5 品种 × 仅止损通道 DR/√N）的完整统计评估——
  B=5000 品种级聚类 bootstrap · 折级配对 Wilcoxon（纯 numpy 手写，scipy 二进制不兼容）
  · 窗口敏感性 N=10/14/20 · 风险画像（笔数/胜率）。
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

ADOPT = ["RM", "m", "p", "FG", "ru"]
N_FOLDS = 5
B_BOOT = 5000
W_BASE = 14

_W = {"w": 14}
_stash = {}


def dual_range(high, low, close, window):
    w = _W["w"]
    hh = pd.Series(high).rolling(w).max().values
    ll = pd.Series(low).rolling(w).min().values
    hc = pd.Series(close).rolling(w).max().values
    lc = pd.Series(close).rolling(w).min().values
    return np.maximum(hh - lc, hc - ll) / np.sqrt(w)


def atr_array_dr(high, low, close, window):
    _stash["hlc"] = (high, low, close, window)
    return dual_range(high, low, close, window)


def atr_array_orig(high, low, close, window):
    _stash["hlc"] = (high, low, close, window)
    return orig_atr_array(high, low, close, window)


def classify_regime_with_atr(close, atr14, sma20, sma20_slope_prev, params=None):
    h, l, c, w = _stash["hlc"]
    return orig_classify_regime_array(close, orig_atr_array(h, l, c, w), sma20, sma20_slope_prev, params)


def setup(variant):
    fds._atr_array = orig_atr_array
    fds.classify_regime_array = orig_classify_regime_array
    if variant == "base":
        pass
    elif variant == "stop_only":
        fds._atr_array = atr_array_dr
        fds.classify_regime_array = classify_regime_with_atr


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
            out.append((r["expR"], r["trades"], r["win_rate"]))
    return out


def portfolio(runs):
    num = den = 0.0
    for folds in runs.values():
        for e, t, _ in folds:
            num += e * t
            den += t
    return num / den if den else np.nan


def fold_pairs(runs_b, runs_t):
    pairs = []
    for sym in runs_b:
        if sym not in runs_t:
            continue
        for (eb, tb, _), (et, tt, _) in zip(runs_b[sym], runs_t[sym]):
            pairs.append((sym, eb, et))
    return pairs


def wilcoxon_signed_rank(diffs):
    diffs = np.asarray(diffs, dtype=float)
    diffs = diffs[diffs != 0]
    n = len(diffs)
    if n < 5:
        return np.nan, np.nan
    ranks = np.argsort(np.argsort(np.abs(diffs))) + 1.0
    w_plus = ranks[diffs > 0].sum()
    mu = n * (n + 1) / 4
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    z = (w_plus - mu) / sigma
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return w_plus, p


def binom_test(k, n, p0=0.5):
    p_ge = sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i) for i in range(k, n + 1))
    return min(1.0, 2 * p_ge)


def run_variant(variant, panel, w):
    _W["w"] = w
    setup(variant)
    runs = {}
    for sym in panel:
        folds = oos_folds(sym)
        if folds:
            runs[sym] = folds
    setup("base")
    return runs


def main():
    print(f"双差值波动估计 · 采纳组合终版评估 · N(W)={W_BASE} · {N_FOLDS} 折走步")
    print(f"{'=' * 74}")
    print(f"采纳组合：{' / '.join(ADOPT)}（仅止损/止盈通道用 DR/√N，行情分类保持 ATR）")
    print()

    base_runs = run_variant("base", ADOPT, W_BASE)
    test_runs = run_variant("stop_only", ADOPT, W_BASE)

    # ── 品种级明细 ──
    print("品种级明细（折级 expR）")
    print(f"{'品种':<5}{'基线折':<40}{'均值':>8}  {'DR折':<40}{'均值':>8}  {'Δ':>8} {'胜/折':>6}")
    print("-" * 120)
    for sym in ADOPT:
        b, t = base_runs[sym], test_runs[sym]
        be = [e for e, _, _ in b]
        te = [e for e, _, _ in t]
        wins = sum(1 for x, y in zip(be, te) if y > x)
        print(f"{sym:<5}{' / '.join(f'{e:+.3f}' for e in be):<40}{np.mean(be):>+8.4f}  "
              f"{' / '.join(f'{e:+.3f}' for e in te):<40}{np.mean(te):>+8.4f}  "
              f"{np.mean(te) - np.mean(be):>+8.4f} {wins}/{len(be):<3}")
    print()

    # ── 组合级 ──
    base_port = portfolio(base_runs)
    test_port = portfolio(test_runs)
    base_trades = sum(t for f in base_runs.values() for _, t, _ in f)
    test_trades = sum(t for f in test_runs.values() for _, t, _ in f)
    base_wr = sum(w * t for f in base_runs.values() for _, t, w in f) / base_trades
    test_wr = sum(w * t for f in test_runs.values() for _, t, w in f) / test_trades
    print(f"{'=' * 74}")
    print("组合级（交易加权全样本）")
    print(f"  ATR 基线： expR {base_port:+.4f} · {base_trades} 笔 · 胜率 {base_wr:.1%}")
    print(f"  DR/√N：    expR {test_port:+.4f} · {test_trades} 笔 · 胜率 {test_wr:.1%}")
    diff = test_port - base_port
    print(f"  ΔexpR {diff:+.4f}（相对 {diff / abs(base_port):+.0%}）· Δ笔数 {test_trades - base_trades:+d} · Δ胜率 {test_wr - base_wr:+.1%}")
    print()

    # ── 折级配对检验 ──
    pairs = fold_pairs(base_runs, test_runs)
    diffs = np.array([t - b for _, b, t in pairs])
    wins = int((diffs > 0).sum())
    n = len(diffs)
    _, p_w = wilcoxon_signed_rank(diffs)
    p_b = binom_test(wins, n)
    print(f"折级配对（{n} 折）：胜 {wins}/{n}（{wins / n:.0%}）")
    print(f"  Wilcoxon 符号秩 p = {p_w:.4f} · 符号检验 p = {p_b:.4f}")
    print()

    # ── 品种级聚类 bootstrap ──
    print(f"品种级聚类 bootstrap（B={B_BOOT}，品种为独立单元重采样）")
    rng = np.random.default_rng(42)
    deltas = []
    for _ in range(B_BOOT):
        sample = rng.choice(ADOPT, size=len(ADOPT), replace=True)
        b = {s: base_runs[s] for s in sample}
        t = {s: test_runs[s] for s in sample}
        deltas.append(portfolio(t) - portfolio(b))
    deltas = np.array(deltas)
    lo, hi = np.percentile(deltas, [2.5, 97.5])
    print(f"  ΔexpR 中位数 {np.median(deltas):+.4f} · 均值 {deltas.mean():+.4f}")
    print(f"  95%CI [{lo:+.4f}, {hi:+.4f}] · P(Δ<=0) = {(deltas <= 0).mean():.1%}")
    print()

    # ── 窗口敏感性 ──
    print(f"{'=' * 74}")
    print("窗口敏感性（组合级 ΔexpR，仅止损通道）")
    for w in [10, 14, 20]:
        if w == W_BASE:
            d = diff
        else:
            tw = run_variant("stop_only", ADOPT, w)
            d = portfolio(tw) - portfolio(base_runs)
        print(f"  N={w:>2}: ΔexpR {d:+.4f}")

    print()
    ok = diff > 0 and wins / n >= 0.6 and lo > 0
    print(f"{'=' * 74}")
    print(f"采纳判定（改善>0 且折胜率>=60% 且 CI 下界>0）：{'✓ 全部通过' if ok else '✗ 未全部通过'}")


if __name__ == "__main__":
    main()
