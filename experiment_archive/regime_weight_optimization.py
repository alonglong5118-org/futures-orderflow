"""
Regime 权重优化器：分析各品种在不同 regime 下的表现并优化 regime 簇权重

背景：四维策略框架 T/F/C，T 维度 8 策略分 3 簇（trend/mean/seasonal）
当前 regime：趋势、震荡、波动、过渡、未知

功能：
  1. 按 regime 拆分回测表现（expR、胜率、笔数）
  2. 识别弱品种（expR<0 或排名后 20%）
  3. 对弱品种逐 regime 扫描簇权重倍率（trend_mult / mean_mult / seasonal_mult）
  4. 调整思路：差 regime 降趋势/均值权重、提 seasonal 或整体降权；好 regime 维持或提权
  5. walk-forward OOS 验证优化效果
  6. 输出优化前后对比 + 推荐 per-symbol 权重配置
"""

import copy
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import four_dim_strategy as fds
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

# ── 常量 ────────────────────────────────────────────────────────────────────
REGIME_NAMES = ["趋势", "震荡", "波动", "过渡", "未知"]
CLUSTER_NAMES = ["trend", "mean", "seasonal"]

# 保存原始函数（monkey-patch 用）
_ORIGINAL_CLUSTER_WEIGHTS = fds.cluster_weights


# ── 权重注入：monkey-patch cluster_weights ──────────────────────────────────
def make_cluster_weights_wrapper(regime_mult_map):
    """构造一个包装后的 cluster_weights 函数，对指定 regime 的簇权重施加倍率。

    regime_mult_map: {"趋势": {"trend": 0.5, "mean": 0.5, "seasonal": 1.5}, ...}
    未在 map 中的 regime 使用原始权重。
    """
    regime_mult_map = regime_mult_map or {}

    def wrapped(regime, cfg=None, group=None, feat_mgr=None):
        base = _ORIGINAL_CLUSTER_WEIGHTS(regime, cfg, group, feat_mgr)
        mult = regime_mult_map.get(regime)
        if not mult:
            return base
        # base 可能是共享引用 → copy 后修改
        result = dict(base)
        for cname, m in mult.items():
            if cname in result:
                result[cname] = result[cname] * m
        return result

    return wrapped


def apply_regime_mult(regime_mult_map):
    """向 four_dim_strategy 注入带倍率的 cluster_weights。"""
    wrapped = make_cluster_weights_wrapper(regime_mult_map)
    fds.cluster_weights = wrapped


def reset_regime_mult():
    """恢复原始 cluster_weights。"""
    fds.cluster_weights = _ORIGINAL_CLUSTER_WEIGHTS


# ── 工具函数 ────────────────────────────────────────────────────────────────
def run_bt_with_mult(symbol, df_slice, regime_mult_map=None, window=200, cfg=None):
    """带 regime 簇权重倍率的回测。返回 walk_forward_backtest 结果 dict。"""
    base_cfg = cfg if cfg is not None else copy.deepcopy(DEFAULT_CONFIG)
    if regime_mult_map:
        apply_regime_mult(regime_mult_map)
    try:
        r = walk_forward_backtest(symbol, cfg=base_cfg, df_in=df_slice, window=window)
    finally:
        if regime_mult_map:
            reset_regime_mult()
    return r


def regime_breakdown(result):
    """从回测结果中提取各 regime 的详细表现（expR、胜率、笔数）。

    返回 dict: {regime: {"expR": float, "win_rate": float, "trades": int}}
    """
    trades = result.get("trades_detail", [])
    by_regime_trades = {}
    for t in trades:
        rg = t.get("regime", "未知")
        by_regime_trades.setdefault(rg, []).append(t)

    breakdown = {}
    for rg, tr_list in by_regime_trades.items():
        Rs = [t["R_adj"] for t in tr_list]
        wins = [r for r in Rs if r > 0]
        breakdown[rg] = {
            "expR": round(float(np.mean(Rs)), 4),
            "win_rate": round(len(wins) / len(Rs), 3),
            "trades": len(Rs),
        }
    # 补全所有 regime
    for rg in REGIME_NAMES:
        if rg not in breakdown:
            breakdown[rg] = {"expR": 0.0, "win_rate": 0.0, "trades": 0}
    return breakdown


# ── 弱品种识别 ──────────────────────────────────────────────────────────────
def identify_weak_symbols(all_results, bottom_pct=0.2, min_trades=20):
    """识别弱品种：expR<0 或排名后 bottom_pct，且交易笔数足够。

    返回: set of symbol codes
    """
    valid = {sym: r for sym, r in all_results.items() if r.get("trades", 0) >= min_trades}
    if not valid:
        return set()

    sorted_syms = sorted(valid.keys(), key=lambda s: valid[s].get("expR", 0))
    n = len(sorted_syms)
    bottom_n = max(1, int(n * bottom_pct))
    bottom_set = set(sorted_syms[:bottom_n])
    negative_set = {s for s, r in valid.items() if r.get("expR", 0) < 0}
    return bottom_set | negative_set


# ── 权重扫描 ────────────────────────────────────────────────────────────────
def generate_mult_candidates(strategy="balanced"):
    """生成簇倍率候选组合。

    strategy:
      "conservative" - 小范围调整（0.7~1.3）
      "balanced" - 中等范围（0.5~1.5）
      "aggressive" - 大范围（0.3~2.0）
    """
    if strategy == "conservative":
        trend_mults = [0.7, 1.0, 1.3]
        mean_mults = [0.7, 1.0, 1.3]
        seasonal_mults = [1.0, 1.5, 2.0]
    elif strategy == "aggressive":
        trend_mults = [0.3, 0.5, 1.0, 1.5, 2.0]
        mean_mults = [0.3, 0.5, 1.0, 1.5, 2.0]
        seasonal_mults = [0.5, 1.0, 2.0, 3.0]
    else:  # balanced
        trend_mults = [0.5, 0.7, 1.0, 1.3]
        mean_mults = [0.5, 0.7, 1.0, 1.3]
        seasonal_mults = [1.0, 1.5, 2.0]

    candidates = []
    for t in trend_mults:
        for m in mean_mults:
            for s in seasonal_mults:
                # 跳过全 1.0（=默认）
                if t == 1.0 and m == 1.0 and s == 1.0:
                    continue
                candidates.append({"trend": t, "mean": m, "seasonal": s})
    return candidates


def score_config(expr, trades, min_trades=8):
    """综合评分：expR 为主，低笔数惩罚。"""
    if trades < min_trades:
        return -999.0
    # 基础分 = expR * 100
    score = expr * 100
    # 笔数过少惩罚
    if trades < min_trades * 2:
        score -= (min_trades * 2 - trades) * 2
    return score


def scan_regime_weights_for_symbol(
    symbol,
    df_train,
    rb_default,
    window=200,
    strategy="balanced",
    min_trades_per_regime=5,
):
    """对单个品种逐 regime 扫描最优簇权重。

    对每个表现差的 regime（expR < 0 或低于全品种均值），尝试调整该 regime 的簇权重；
    表现好的 regime 维持默认。

    返回: {
        "best_mult_map": {regime: {trend, mean, seasonal}},
        "train_expr_default": float,
        "train_expr_best": float,
        "delta": float,
        "per_regime_optimization": {regime: {...}}
    }
    """
    default_expr = rb_default.get("expR", 0)
    default_trades = rb_default.get("trades", 0)
    default_brk = regime_breakdown(rb_default)

    # 判定哪些 regime 需要优化
    # 差 regime: expR < 0 且笔数 >= min_trades_per_regime
    bad_regimes = []
    good_regimes = []
    for rg in REGIME_NAMES:
        info = default_brk.get(rg, {"expR": 0, "trades": 0})
        if info["trades"] >= min_trades_per_regime:
            if info["expR"] < 0:
                bad_regimes.append((rg, info))
            else:
                good_regimes.append((rg, info))

    if not bad_regimes:
        return {
            "best_mult_map": {},
            "train_expr_default": default_expr,
            "train_expr_best": default_expr,
            "delta": 0.0,
            "per_regime_optimization": {},
            "note": "无差表现 regime，跳过优化",
        }

    candidates = generate_mult_candidates(strategy)
    per_regime_best = {}
    best_mult_map = {}

    # 对每个差 regime 独立找最优倍率
    for rg, info in bad_regimes:
        best_score = -999
        best_mult = None
        best_expr = info["expR"]

        for mult in candidates:
            # 只调整当前 regime，其他用默认
            rmm = {rg: mult}
            r = run_bt_with_mult(symbol, df_train, rmm, window=window)
            if r.get("trades", 0) < 5:
                continue

            brk = regime_breakdown(r)
            rg_info = brk.get(rg, {"expR": 0, "trades": 0})
            rg_expr = rg_info["expR"]
            rg_trades = rg_info["trades"]

            # 评分：关注该 regime 的 expR 改善，同时不严重降低整体
            overall_expr = r.get("expR", 0)
            s = score_config(rg_expr, rg_trades, min_trades=min_trades_per_regime)
            # 整体不恶化奖励
            if overall_expr >= default_expr * 0.9:
                s += 10

            if s > best_score:
                best_score = s
                best_mult = mult
                best_expr = rg_expr

        if best_mult is not None and best_expr > info["expR"]:
            per_regime_best[rg] = {
                "default_expr": info["expR"],
                "best_expr": best_expr,
                "best_mult": best_mult,
                "default_trades": info["trades"],
            }
            best_mult_map[rg] = best_mult

    if not best_mult_map:
        return {
            "best_mult_map": {},
            "train_expr_default": default_expr,
            "train_expr_best": default_expr,
            "delta": 0.0,
            "per_regime_optimization": {},
            "note": "扫描未找到改善配置",
        }

    # 用最优组合跑一次整体验证
    r_best = run_bt_with_mult(symbol, df_train, best_mult_map, window=window)
    best_expr = r_best.get("expR", 0)

    return {
        "best_mult_map": best_mult_map,
        "train_expr_default": round(default_expr, 4),
        "train_expr_best": round(best_expr, 4),
        "delta": round(best_expr - default_expr, 4),
        "per_regime_optimization": per_regime_best,
        "default_trades": default_trades,
        "best_trades": r_best.get("trades", 0),
    }


# ── Walk-Forward OOS 验证 ───────────────────────────────────────────────────
def wf_oos_fold(symbol, df, train_end_idx, oos_bars=100, window=200, strategy="balanced"):
    """跑一折 OOS：训练集确定最优 regime 权重，OOS 集验证。"""
    # 训练集
    train_start = max(0, train_end_idx - 400)
    df_train = df.iloc[train_start:train_end_idx]

    # OOS 集（含 window 根 warmup）
    oos_start = max(0, train_end_idx - window)
    oos_end = min(len(df), train_end_idx + oos_bars)
    if oos_end - oos_start < window + 10:
        return None

    df_oos = df.iloc[oos_start:oos_end]

    if len(df_train) < window + 50:
        return None

    # 默认回测（训练集）
    r_default_train = run_bt_with_mult(symbol, df_train, None, window=window)
    if r_default_train.get("trades", 0) < 10:
        return None

    # 训练：扫描最优权重
    opt_result = scan_regime_weights_for_symbol(
        symbol,
        df_train,
        r_default_train,
        window=window,
        strategy=strategy,
    )
    best_mult = opt_result["best_mult_map"]

    # OOS：默认 vs 最优
    r_default_oos = run_bt_with_mult(symbol, df_oos, None, window=window)
    r_opt_oos = run_bt_with_mult(symbol, df_oos, best_mult, window=window) if best_mult else r_default_oos

    default_expr = r_default_oos.get("expR", 0)
    opt_expr = r_opt_oos.get("expR", 0)

    return {
        "train_end": train_end_idx,
        "best_mult_map": best_mult,
        "train_default_expr": round(r_default_train.get("expR", 0), 4),
        "train_best_expr": opt_result["train_expr_best"],
        "train_delta": opt_result["delta"],
        "oos_default_expr": round(default_expr, 4),
        "oos_opt_expr": round(opt_expr, 4),
        "oos_delta": round(opt_expr - default_expr, 4),
        "default_trades_oos": r_default_oos.get("trades", 0),
        "opt_trades_oos": r_opt_oos.get("trades", 0),
        "per_regime_opt": opt_result.get("per_regime_optimization", {}),
    }


def walk_forward_oos(symbol, df, n_folds=5, oos_bars=100, window=200, strategy="balanced"):
    """多折 walk-forward OOS 验证 regime 权重优化效果。"""
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
        fold = wf_oos_fold(symbol, df, train_end, oos_bars, window, strategy)
        if fold:
            folds.append(fold)

    if not folds:
        return None

    avg_default_oos = np.mean([f["oos_default_expr"] for f in folds])
    avg_opt_oos = np.mean([f["oos_opt_expr"] for f in folds])
    avg_delta = avg_opt_oos - avg_default_oos
    win_folds = sum(1 for f in folds if f["oos_delta"] > 0)

    # 聚合各折的最优 mult（取各 regime 各倍率中位数）
    all_mults_by_regime = {}
    for f in folds:
        for rg, mult in f["best_mult_map"].items():
            all_mults_by_regime.setdefault(rg, []).append(mult)

    recommended_mult = {}
    for rg, mult_list in all_mults_by_regime.items():
        recommended_mult[rg] = {c: round(float(np.median([m[c] for m in mult_list])), 2) for c in CLUSTER_NAMES}

    return {
        "symbol": symbol,
        "n_folds": len(folds),
        "avg_default_oos_expr": round(avg_default_oos, 4),
        "avg_opt_oos_expr": round(avg_opt_oos, 4),
        "avg_delta_oos": round(avg_delta, 4),
        "win_rate": round(win_folds / len(folds), 3),
        "recommended_mult": recommended_mult,
        "total_default_trades_oos": sum(f["default_trades_oos"] for f in folds),
        "total_opt_trades_oos": sum(f["opt_trades_oos"] for f in folds),
        "folds": folds,
    }


# ── 全市场 baseline 回测 ────────────────────────────────────────────────────
def run_baseline_all(symbols, window=200, tail=None):
    """跑全品种默认权重回测，用于识别弱品种。"""
    results = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=DEFAULT_CONFIG, df_in=df, window=window, tail=tail)
            if r and r.get("trades", 0) >= 5:
                results[sym] = r
            print(
                f"  [{i + 1}/{total}] {sym:>5}  expR={r.get('expR', 0):+.3f}  trades={r.get('trades', 0):>3}",
                end="\r",
                flush=True,
            )
        except Exception:
            continue
    print()
    return results


# ── 报告输出 ────────────────────────────────────────────────────────────────
def print_regime_breakdown_table(symbol, breakdown):
    """打印单品种 regime 明细表。"""
    print(f"\n  品种 {symbols_name(symbol)} ({symbol}) 各 regime 表现:")
    print(f"    {'Regime':>6}  {'expR':>8}  {'胜率':>6}  {'笔数':>6}")
    for rg in REGIME_NAMES:
        info = breakdown.get(rg, {"expR": 0, "win_rate": 0, "trades": 0})
        flag = "  "
        if info["trades"] >= 5:
            if info["expR"] < 0:
                flag = "▼"
            elif info["expR"] > 0.1:
                flag = "▲"
        print(f"    {rg:>6}  {info['expR']:>+8.3f}  {info['win_rate'] * 100:>5.1f}%  {info['trades']:>4} {flag}")


def symbols_name(sym):
    return SYMBOLS.get(sym, {}).get("name", sym)


def generate_weight_config(symbol, recommended_mult):
    """生成 per-symbol 权重配置（可直接放入 trade_config.json 的结构）。

    返回 dict: {"regime_cluster_mult": {regime: {cluster: mult}}}
    """
    return {"regime_cluster_mult": recommended_mult}


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("Regime 权重优化器：按 regime 分析表现 + walk-forward OOS 验证")
    print("=" * 80)

    # 候选品种：有 per_symbol_risk 配置的 + 代表性品种
    candidate_syms = sorted(DEFAULT_CONFIG.get("per_symbol_risk", {}).keys())
    extra = [
        "rb",
        "hc",
        "FG",
        "au",
        "ru",
        "CF",
        "ss",
        "cu",
        "al",
        "zn",
        "ag",
        "J",
        "JM",
        "i",
        "sc",
        "SA",
        "MA",
        "TA",
        "v",
        "pp",
        "l",
        "y",
        "p",
        "c",
    ]
    for s in extra:
        if s not in candidate_syms and s in SYMBOLS:
            candidate_syms.append(s)

    print(f"\n  候选品种数: {len(candidate_syms)}")
    print("  阶段 1/4: 全市场 baseline 回测 ...")

    baseline = run_baseline_all(candidate_syms, window=200)
    print(f"  有效品种: {len(baseline)} 个")

    # 全市场 expR 排序
    sorted_by_expr = sorted(baseline.items(), key=lambda x: x[1].get("expR", 0), reverse=True)

    print("\n  全品种 expR 排行:")
    print(f"    {'排名':>4}  {'品种':>5}  {'名称':>8}  {'expR':>7}  {'胜率':>6}  {'笔数':>5}")
    for rank, (sym, r) in enumerate(sorted_by_expr, 1):
        print(
            f"    {rank:>4}  {sym:>5}  {symbols_name(sym):>8}  "
            f"{r.get('expR', 0):>+7.3f}  "
            f"{r.get('win_rate', 0) * 100:>5.1f}%  "
            f"{r.get('trades', 0):>4}"
        )

    # 识别弱品种
    weak_syms = identify_weak_symbols(baseline, bottom_pct=0.2, min_trades=20)
    print(f"\n  弱品种（expR<0 或后20%，笔数>=20）: {len(weak_syms)} 个")
    for sym in sorted(weak_syms):
        r = baseline[sym]
        print(f"    {sym:>5} {symbols_name(sym):>8}  expR={r['expR']:+.3f}  trades={r['trades']}")

    # 阶段 2: 各 regime 详细表现
    print("\n  阶段 2/4: 弱品种 regime 明细 ...")
    weak_breakdowns = {}
    for sym in sorted(weak_syms):
        brk = regime_breakdown(baseline[sym])
        weak_breakdowns[sym] = brk
        print_regime_breakdown_table(sym, brk)

    # 阶段 3: 权重扫描（训练集内）
    print("\n  阶段 3/4: 弱品种 regime 权重扫描（IS） ...")
    scan_results = {}
    for i, sym in enumerate(sorted(weak_syms)):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 500:
                continue
            print(f"  [{i + 1}/{len(weak_syms)}] {sym:>5} 扫描中...", end="\r", flush=True)
            opt = scan_regime_weights_for_symbol(
                sym,
                df,
                baseline[sym],
                window=200,
                strategy="balanced",
            )
            scan_results[sym] = opt
        except Exception:
            pass
    print()

    # IS 结果汇总
    improved_is = {s: r for s, r in scan_results.items() if r.get("delta", 0) > 0.05}
    worsened_is = {s: r for s, r in scan_results.items() if r.get("delta", 0) < -0.05}
    print(f"\n  IS 扫描结果：改善（Δ>+0.05）{len(improved_is)} 个，恶化（Δ<-0.05）{len(worsened_is)} 个")

    if improved_is:
        print("\n  IS 改善品种（Top）:")
        print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔIS':>7}  {'优化regime数':>8}")
        for sym, r in sorted(improved_is.items(), key=lambda x: x[1]["delta"], reverse=True):
            n_rg = len(r.get("best_mult_map", {}))
            print(
                f"    {sym:>5}  {r['train_expr_default']:>+8.3f}  {r['train_expr_best']:>+8.3f}  "
                f"{r['delta']:>+7.3f}  {n_rg:>6}个"
            )
            for rg, info in r.get("per_regime_optimization", {}).items():
                bm = info["best_mult"]
                print(
                    f"        {rg}: {info['default_expr']:+.3f} → {info['best_expr']:+.3f}  "
                    f"mult=trend:{bm['trend']:.1f} mean:{bm['mean']:.1f} seasonal:{bm['seasonal']:.1f}"
                )

    # 阶段 4: Walk-Forward OOS
    print("\n  阶段 4/4: Walk-Forward OOS 验证（IS 改善品种） ...")
    oos_candidates = sorted(improved_is.keys())
    oos_results = {}

    for i, sym in enumerate(oos_candidates):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 600:
                continue
            print(f"  [{i + 1}/{len(oos_candidates)}] {sym:>5} WF-OOS 5折验证中...", end="\r", flush=True)
            oos_r = walk_forward_oos(sym, df, n_folds=5, oos_bars=100, window=200, strategy="balanced")
            if oos_r:
                oos_results[sym] = oos_r
        except Exception:
            pass
    print()

    # OOS 结果汇总
    if oos_results:
        pos_oos = {k: v for k, v in oos_results.items() if v["avg_delta_oos"] > 0}
        neg_oos = {k: v for k, v in oos_results.items() if v["avg_delta_oos"] < 0}

        print(f"\n{'=' * 80}")
        print("  OOS 验证结果汇总")
        print(f"{'=' * 80}")
        print(f"  验证品种: {len(oos_results)} 个")
        print(f"  OOS 正收益（Δ>0）: {len(pos_oos)} 个")
        print(f"  OOS 负收益（Δ<0）: {len(neg_oos)} 个")

        if pos_oos:
            print("\n  OOS 正收益品种:")
            print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'笔数':>8}")
            for sym, r in sorted(pos_oos.items(), key=lambda x: x[1]["avg_delta_oos"], reverse=True):
                print(
                    f"    {sym:>5}  {r['avg_default_oos_expr']:>+8.3f}  {r['avg_opt_oos_expr']:>+8.3f}  "
                    f"{r['avg_delta_oos']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                    f"{r['total_default_trades_oos']:>4}→{r['total_opt_trades_oos']:<4}"
                )

        if neg_oos:
            print("\n  OOS 负收益品种:")
            print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}")
            for sym, r in sorted(neg_oos.items(), key=lambda x: x[1]["avg_delta_oos"]):
                print(
                    f"    {sym:>5}  {r['avg_default_oos_expr']:>+8.3f}  {r['avg_opt_oos_expr']:>+8.3f}  "
                    f"{r['avg_delta_oos']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%"
                )

        # 推荐权重配置
        print(f"\n{'=' * 80}")
        print("  推荐 per-symbol regime 簇权重配置（OOS 验证通过的品种）")
        print(f"{'=' * 80}")
        print("  （仅包含 OOS Δ>0 的品种，可直接作为 trade_config.json 参考）")
        print()

        recommended_configs = {}
        for sym, r in sorted(pos_oos.items(), key=lambda x: x[1]["avg_delta_oos"], reverse=True):
            rec = r["recommended_mult"]
            recommended_configs[sym] = generate_weight_config(sym, rec)
            print(f"  {sym:>5} ({symbols_name(sym)})  ΔOOS={r['avg_delta_oos']:+.3f}  胜率={r['win_rate'] * 100:.0f}%")
            for rg, mult in sorted(rec.items()):
                print(
                    f"    {rg}: trend={mult['trend']:.2f}x  mean={mult['mean']:.2f}x  seasonal={mult['seasonal']:.2f}x"
                )
            print()

    else:
        print("\n  无 OOS 验证结果（可能品种数据不足）")

    # 保存完整结果
    os.makedirs("logs", exist_ok=True)
    output = {
        "baseline": {sym: {k: v for k, v in r.items() if k != "trades_detail"} for sym, r in baseline.items()},
        "weak_symbols": sorted(weak_syms),
        "scan_results": scan_results,
        "oos_results": {sym: {k: v for k, v in r.items() if k != "folds"} for sym, r in oos_results.items()},
    }
    with open("logs/regime_weight_optimization.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  详细结果 → logs/regime_weight_optimization.json")
    print("=" * 80)


if __name__ == "__main__":
    main()
