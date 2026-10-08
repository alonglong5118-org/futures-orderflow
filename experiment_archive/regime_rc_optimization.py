"""
Regime 风控参数优化器：按品种×regime 优化 T 阈值系数和止损系数

核心洞察：
  - 弱品种的大亏集中在特定 regime（如 au 趋势市、hc 过渡市、MA 波动市）
  - 当前 regime_coef 是全局的，过渡 regime 直接回退到波动配置
  - 优化思路：对差表现 regime 提高 T 阈值（减少假信号）+ 收紧止损（降低单笔亏损）

优化参数（per symbol × per regime）：
  - T_mult: T 阈值倍率（>1 = 更难触发，减少假信号）
  - stop_mult: 止损倍率（<1 = 更紧止损，降低单笔亏损）

验证方法：walk-forward OOS 5折
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

REGIME_NAMES = ["趋势", "震荡", "波动", "过渡", "未知"]


# ── 工具函数 ────────────────────────────────────────────────────────────────
def make_config_with_regime_coef(symbol, base_cfg, regime_coef_overrides):
    """构造带 per-regime 风控系数覆盖的配置。

    regime_coef_overrides: {"趋势": {"T": 1.2, "stop": 0.8}, ...}
    只覆盖指定 regime 的指定参数，其他沿用全局 regime_coef。
    """
    cfg = copy.deepcopy(base_cfg)
    # 深拷贝全局 regime_coef 作为基础
    base_rc = copy.deepcopy(cfg["regime_coef"])
    # 应用覆盖
    for rg, overrides in regime_coef_overrides.items():
        if rg not in base_rc:
            base_rc[rg] = copy.deepcopy(base_rc.get("波动", {"T": 1.0, "conv": 1.0, "stop": 1.2, "cooldown": 300}))
        for k, v in overrides.items():
            base_rc[rg][k] = v
    cfg["regime_coef"] = base_rc
    return cfg


def run_bt_with_rc(symbol, df_slice, regime_coef_overrides=None, window=200, cfg=None):
    """带 regime 风控系数覆盖的回测。"""
    base_cfg = cfg if cfg is not None else DEFAULT_CONFIG
    if regime_coef_overrides:
        test_cfg = make_config_with_regime_coef(symbol, base_cfg, regime_coef_overrides)
    else:
        test_cfg = base_cfg
    return walk_forward_backtest(symbol, cfg=test_cfg, df_in=df_slice, window=window)


def regime_breakdown(result):
    """从回测结果中提取各 regime 的详细表现（expR、胜率、笔数、总R）。"""
    trades = result.get("trades_detail", [])
    by_regime_trades = {}
    for t in trades:
        rg = t.get("regime", "未知")
        by_regime_trades.setdefault(rg, []).append(t)

    breakdown = {}
    for rg, tr_list in by_regime_trades.items():
        Rs = [t["R_adj"] for t in tr_list]
        wins = [r for r in Rs if r > 0]
        losses = [r for r in Rs if r < 0]
        breakdown[rg] = {
            "expR": round(float(np.mean(Rs)), 4),
            "win_rate": round(len(wins) / len(Rs), 3) if Rs else 0,
            "trades": len(Rs),
            "total_R": round(float(sum(Rs)), 3),
            "avg_win": round(float(np.mean(wins)), 3) if wins else 0,
            "avg_loss": round(float(np.mean(losses)), 3) if losses else 0,
        }
    for rg in REGIME_NAMES:
        if rg not in breakdown:
            breakdown[rg] = {"expR": 0.0, "win_rate": 0.0, "trades": 0, "total_R": 0, "avg_win": 0, "avg_loss": 0}
    return breakdown


# ── 弱品种识别 ──────────────────────────────────────────────────────────────
def identify_weak_symbols(all_results, bottom_pct=0.25, min_trades=20):
    """识别弱品种：expR<0 或排名后 bottom_pct，且交易笔数足够。"""
    valid = {sym: r for sym, r in all_results.items() if r.get("trades", 0) >= min_trades}
    if not valid:
        return set()
    sorted_syms = sorted(valid.keys(), key=lambda s: valid[s].get("expR", 0))
    n = len(sorted_syms)
    bottom_n = max(1, int(n * bottom_pct))
    bottom_set = set(sorted_syms[:bottom_n])
    negative_set = {s for s, r in valid.items() if r.get("expR", 0) < 0}
    return bottom_set | negative_set


# ── 参数扫描 ────────────────────────────────────────────────────────────────
def generate_rc_candidates(strategy="balanced"):
    """生成 regime 风控参数候选组合。

    每个候选是 (T_mult, stop_mult) 对：
      - T_mult > 1: 提高阈值，减少触发（过滤假信号）
      - T_mult < 1: 降低阈值，增加触发
      - stop_mult < 1: 收紧止损，降低单笔亏损
      - stop_mult > 1: 放宽止损，避免被震荡出局
    """
    if strategy == "conservative":
        T_mults = [0.9, 1.0, 1.2, 1.5]
        stop_mults = [0.8, 1.0, 1.2]
    elif strategy == "aggressive":
        T_mults = [0.7, 0.85, 1.0, 1.3, 1.7, 2.0]
        stop_mults = [0.6, 0.8, 1.0, 1.3, 1.5]
    else:  # balanced
        T_mults = [0.8, 1.0, 1.3, 1.7]
        stop_mults = [0.7, 0.85, 1.0, 1.3]

    candidates = []
    for t in T_mults:
        for s in stop_mults:
            if t == 1.0 and s == 1.0:
                continue  # 跳过默认
            candidates.append({"T": t, "stop": s})
    return candidates


def score_result(result, baseline_expr, baseline_trades):
    """综合评分：expR 提升为主，兼顾回撤和笔数稳定性。"""
    expr = result.get("expR", 0)
    trades = result.get("trades", 0)
    max_dd = result.get("max_dd", 0)
    base_dd = 10  # 基准 DD

    if trades < max(5, baseline_trades * 0.3):
        return -999.0  # 笔数太少，无意义

    delta_expr = expr - baseline_expr
    score = delta_expr * 100

    # 回撤惩罚
    dd_penalty = max(0, max_dd - base_dd) * 0.5
    score -= dd_penalty

    # 笔数过于减少惩罚（说明可能过度过滤）
    if trades < baseline_trades * 0.5:
        score -= (0.5 - trades / baseline_trades) * 20

    return score


def scan_rc_for_symbol(symbol, df_train, rb_default, window=200, strategy="balanced", min_trades_per_regime=5):
    """对单个品种逐 regime 扫描最优风控参数。

    只优化 expR<0 且笔数足够的 regime，其他 regime 保持默认。
    """
    default_expr = rb_default.get("expR", 0)
    default_trades = rb_default.get("trades", 0)
    default_brk = regime_breakdown(rb_default)

    # 找出差表现 regime
    bad_regimes = []
    for rg in REGIME_NAMES:
        info = default_brk.get(rg, {"expR": 0, "trades": 0})
        if info["trades"] >= min_trades_per_regime and info["expR"] < 0:
            bad_regimes.append((rg, info))

    if not bad_regimes:
        return {
            "best_overrides": {},
            "default_expr": round(default_expr, 4),
            "best_expr": round(default_expr, 4),
            "delta": 0.0,
            "per_regime": {},
            "note": "无差表现 regime",
        }

    candidates = generate_rc_candidates(strategy)
    per_regime_best = {}
    best_overrides = {}

    # 对每个差 regime 独立找最优参数
    for rg, info in bad_regimes:
        best_score = -999
        best_params = None
        best_regime_expr = info["expR"]

        for params in candidates:
            overrides = {rg: params}
            r = run_bt_with_rc(symbol, df_train, overrides, window=window)
            if r.get("trades", 0) < 5:
                continue

            brk = regime_breakdown(r)
            rg_expr = brk.get(rg, {}).get("expR", 0)
            rg_trades = brk.get(rg, {}).get("trades", 0)

            # 评分：该 regime expR 改善 + 整体不严重恶化
            overall_expr = r.get("expR", 0)
            s = (rg_expr - info["expR"]) * 100  # 该 regime 改善
            if rg_trades < min_trades_per_regime:
                s -= 20
            # 整体表现不恶化奖励
            if overall_expr >= default_expr * 0.85:
                s += 15
            # 整体提升额外奖励
            if overall_expr > default_expr:
                s += (overall_expr - default_expr) * 50

            if s > best_score:
                best_score = s
                best_params = params
                best_regime_expr = rg_expr

        if best_params is not None and best_regime_expr > info["expR"]:
            per_regime_best[rg] = {
                "default_expr": info["expR"],
                "best_expr": best_regime_expr,
                "best_params": best_params,
                "default_trades": info["trades"],
            }
            best_overrides[rg] = best_params

    if not best_overrides:
        return {
            "best_overrides": {},
            "default_expr": round(default_expr, 4),
            "best_expr": round(default_expr, 4),
            "delta": 0.0,
            "per_regime": {},
            "note": "扫描未找到改善配置",
        }

    # 用最优组合跑一次整体验证
    r_best = run_bt_with_rc(symbol, df_train, best_overrides, window=window)
    best_expr = r_best.get("expR", 0)

    return {
        "best_overrides": best_overrides,
        "default_expr": round(default_expr, 4),
        "best_expr": round(best_expr, 4),
        "delta": round(best_expr - default_expr, 4),
        "per_regime": per_regime_best,
        "default_trades": default_trades,
        "best_trades": r_best.get("trades", 0),
        "default_max_dd": rb_default.get("max_dd", 0),
        "best_max_dd": r_best.get("max_dd", 0),
    }


# ── Walk-Forward OOS 验证 ───────────────────────────────────────────────────
def wf_oos_fold(symbol, df, train_end_idx, oos_bars=100, window=200, strategy="balanced"):
    """一折 OOS：训练集确定最优参数，OOS 集验证。"""
    train_start = max(0, train_end_idx - 400)
    df_train = df.iloc[train_start:train_end_idx]

    oos_start = max(0, train_end_idx - window)
    oos_end = min(len(df), train_end_idx + oos_bars)
    if oos_end - oos_start < window + 10:
        return None

    df_oos = df.iloc[oos_start:oos_end]

    if len(df_train) < window + 50:
        return None

    r_default_train = run_bt_with_rc(symbol, df_train, None, window=window)
    if r_default_train.get("trades", 0) < 10:
        return None

    opt_result = scan_rc_for_symbol(symbol, df_train, r_default_train, window=window, strategy=strategy)
    best_overrides = opt_result["best_overrides"]

    r_default_oos = run_bt_with_rc(symbol, df_oos, None, window=window)
    r_opt_oos = run_bt_with_rc(symbol, df_oos, best_overrides, window=window) if best_overrides else r_default_oos

    return {
        "train_end": train_end_idx,
        "best_overrides": best_overrides,
        "train_default_expr": round(r_default_train.get("expR", 0), 4),
        "train_best_expr": opt_result["best_expr"],
        "train_delta": opt_result["delta"],
        "oos_default_expr": round(r_default_oos.get("expR", 0), 4),
        "oos_opt_expr": round(r_opt_oos.get("expR", 0), 4),
        "oos_delta": round(r_opt_oos.get("expR", 0) - r_default_oos.get("expR", 0), 4),
        "default_trades_oos": r_default_oos.get("trades", 0),
        "opt_trades_oos": r_opt_oos.get("trades", 0),
        "per_regime_opt": opt_result.get("per_regime", {}),
    }


def walk_forward_oos(symbol, df, n_folds=5, oos_bars=100, window=200, strategy="balanced"):
    """多折 walk-forward OOS 验证。"""
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

    avg_default = np.mean([f["oos_default_expr"] for f in folds])
    avg_opt = np.mean([f["oos_opt_expr"] for f in folds])
    avg_delta = avg_opt - avg_default
    win_folds = sum(1 for f in folds if f["oos_delta"] > 0)

    # 聚合各折最优参数（中位数）
    all_params_by_regime = {}
    for f in folds:
        for rg, params in f["best_overrides"].items():
            all_params_by_regime.setdefault(rg, []).append(params)

    recommended = {}
    for rg, params_list in all_params_by_regime.items():
        recommended[rg] = {
            "T": round(float(np.median([p["T"] for p in params_list])), 2),
            "stop": round(float(np.median([p["stop"] for p in params_list])), 2),
        }

    return {
        "symbol": symbol,
        "n_folds": len(folds),
        "avg_default_expr": round(avg_default, 4),
        "avg_opt_expr": round(avg_opt, 4),
        "avg_delta": round(avg_delta, 4),
        "win_rate": round(win_folds / len(folds), 3),
        "recommended": recommended,
        "total_default_trades": sum(f["default_trades_oos"] for f in folds),
        "total_opt_trades": sum(f["opt_trades_oos"] for f in folds),
        "folds": folds,
    }


# ── 全市场 baseline ─────────────────────────────────────────────────────────
def run_baseline_all(symbols, window=200):
    """全品种默认参数回测。"""
    results = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=DEFAULT_CONFIG, df_in=df, window=window)
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


def symbols_name(sym):
    return SYMBOLS.get(sym, {}).get("name", sym)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("Regime 风控参数优化器：T阈值 + 止损系数 按品种×regime 优化")
    print("=" * 80)

    # 候选品种
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
        "a",
        "m",
        "b",
        "RM",
        "rr",
    ]
    for s in extra:
        if s not in candidate_syms and s in SYMBOLS:
            candidate_syms.append(s)

    print(f"\n  候选品种数: {len(candidate_syms)}")
    print("  阶段 1/4: 全市场 baseline 回测 ...")

    baseline = run_baseline_all(candidate_syms, window=200)
    print(f"  有效品种: {len(baseline)} 个")

    # 排序
    sorted_by_expr = sorted(baseline.items(), key=lambda x: x[1].get("expR", 0), reverse=True)
    print("\n  全品种 expR 排行（前20 + 后10）:")
    print(f"    {'排名':>4}  {'品种':>5}  {'名称':>8}  {'expR':>7}  {'胜率':>6}  {'笔数':>5}  {'最大DD':>7}")
    for rank, (sym, r) in enumerate(sorted_by_expr[:20], 1):
        print(
            f"    {rank:>4}  {sym:>5}  {symbols_name(sym):>8}  "
            f"{r.get('expR', 0):>+7.3f}  {r.get('win_rate', 0) * 100:>5.1f}%  "
            f"{r.get('trades', 0):>5}  {r.get('max_dd', 0):>7.2f}"
        )
    print(f"    ... 共 {len(sorted_by_expr)} 个品种 ...")
    for rank, (sym, r) in enumerate(sorted_by_expr[-10:], len(sorted_by_expr) - 9):
        print(
            f"    {rank:>4}  {sym:>5}  {symbols_name(sym):>8}  "
            f"{r.get('expR', 0):>+7.3f}  {r.get('win_rate', 0) * 100:>5.1f}%  "
            f"{r.get('trades', 0):>5}  {r.get('max_dd', 0):>7.2f}"
        )

    # 识别弱品种
    weak_syms = identify_weak_symbols(baseline, bottom_pct=0.25, min_trades=20)
    print(f"\n  弱品种（expR<0 或后25%，笔数>=20）: {len(weak_syms)} 个")
    for sym in sorted(weak_syms):
        r = baseline[sym]
        brk = regime_breakdown(r)
        bad_rgs = [rg for rg in REGIME_NAMES if brk[rg]["trades"] >= 5 and brk[rg]["expR"] < 0]
        print(
            f"    {sym:>5} {symbols_name(sym):>8}  expR={r['expR']:+.3f}  "
            f"trades={r['trades']}  差regime={','.join(bad_rgs) if bad_rgs else '无'}"
        )

    # 阶段 2: 弱品种 regime 明细
    print("\n  阶段 2/4: 弱品种 regime 明细 ...")
    for sym in sorted(weak_syms):
        brk = regime_breakdown(baseline[sym])
        print(f"\n  {sym} ({symbols_name(sym)}):")
        print(f"    {'Regime':>6}  {'expR':>8}  {'胜率':>6}  {'笔数':>6}  {'总R':>8}")
        for rg in REGIME_NAMES:
            info = brk[rg]
            flag = ""
            if info["trades"] >= 5:
                if info["expR"] < -0.1:
                    flag = "  ◆ 大亏"
                elif info["expR"] < 0:
                    flag = "  ▼ 小亏"
                elif info["expR"] > 0.2:
                    flag = "  ▲ 强"
            print(
                f"    {rg:>6}  {info['expR']:>+8.3f}  {info['win_rate'] * 100:>5.1f}%  "
                f"{info['trades']:>5}  {info['total_R']:>+8.3f}{flag}"
            )

    # 阶段 3: IS 扫描
    print("\n  阶段 3/4: 弱品种 regime 风控参数扫描（IS） ...")
    scan_results = {}
    for i, sym in enumerate(sorted(weak_syms)):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 500:
                continue
            print(f"  [{i + 1}/{len(weak_syms)}] {sym:>5} 扫描中...", end="\r", flush=True)
            opt = scan_rc_for_symbol(sym, df, baseline[sym], window=200, strategy="balanced")
            scan_results[sym] = opt
        except Exception:
            pass
    print()

    # IS 汇总
    improved_is = {s: r for s, r in scan_results.items() if r.get("delta", 0) > 0.03}
    print(f"\n  IS 扫描结果：明显改善（Δ>+0.03）{len(improved_is)} 个")

    if improved_is:
        print("\n  IS 改善品种:")
        print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔIS':>7}  {'DD变化':>8}  {'优化regime':>10}")
        for sym, r in sorted(improved_is.items(), key=lambda x: x[1]["delta"], reverse=True):
            ddd = r.get("best_max_dd", 0) - r.get("default_max_dd", 0)
            rgs = ",".join(r["best_overrides"].keys())
            print(
                f"    {sym:>5}  {r['default_expr']:>+8.3f}  {r['best_expr']:>+8.3f}  "
                f"{r['delta']:>+7.3f}  {ddd:>+7.2f}  {rgs:>10}"
            )
            for rg, info in r.get("per_regime", {}).items():
                bp = info["best_params"]
                print(
                    f"        {rg}: {info['default_expr']:+.3f} → {info['best_expr']:+.3f}  "
                    f"T×{bp['T']:.2f} stop×{bp['stop']:.2f}"
                )

    # 阶段 4: OOS 验证
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

    # OOS 汇总
    if oos_results:
        pos_oos = {k: v for k, v in oos_results.items() if v["avg_delta"] > 0}
        robust_oos = {k: v for k, v in pos_oos.items() if v["win_rate"] >= 0.6}  # 胜率>=60%才算稳健

        print(f"\n{'=' * 80}")
        print("  OOS 验证结果汇总")
        print(f"{'=' * 80}")
        print(f"  验证品种: {len(oos_results)} 个")
        print(f"  OOS 正收益（Δ>0）: {len(pos_oos)} 个")
        print(f"  稳健通过（Δ>0 且 胜率≥60%）: {len(robust_oos)} 个")

        if robust_oos:
            print("\n  ✅ 稳健通过品种:")
            print(f"    {'品种':>5}  {'默认expR':>8}  {'最优expR':>8}  {'ΔOOS':>7}  {'胜率':>6}  {'笔数':>10}")
            for sym, r in sorted(robust_oos.items(), key=lambda x: x[1]["avg_delta"], reverse=True):
                print(
                    f"    {sym:>5}  {r['avg_default_expr']:>+8.3f}  {r['avg_opt_expr']:>+8.3f}  "
                    f"{r['avg_delta']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  "
                    f"{r['total_default_trades']:>4}→{r['total_opt_trades']:<4}"
                )

        if pos_oos and len(pos_oos) > len(robust_oos):
            print("\n  ⚠️  正收益但胜率不足60%:")
            for sym, r in sorted(pos_oos.items(), key=lambda x: x[1]["avg_delta"], reverse=True):
                if sym not in robust_oos:
                    print(f"    {sym:>5}  ΔOOS={r['avg_delta']:+.3f}  胜率={r['win_rate'] * 100:.0f}%")

        neg_oos = {k: v for k, v in oos_results.items() if v["avg_delta"] <= 0}
        if neg_oos:
            print("\n  ❌ OOS 失效品种:")
            for sym, r in sorted(neg_oos.items(), key=lambda x: x[1]["avg_delta"]):
                print(f"    {sym:>5}  ΔOOS={r['avg_delta']:+.3f}  胜率={r['win_rate'] * 100:.0f}%")

        # 推荐配置
        if robust_oos:
            print(f"\n{'=' * 80}")
            print("  推荐 per-symbol regime 风控参数（稳健通过 OOS）")
            print(f"{'=' * 80}")
            print()
            for sym, r in sorted(robust_oos.items(), key=lambda x: x[1]["avg_delta"], reverse=True):
                rec = r["recommended"]
                print(
                    f"  {sym:>5} ({symbols_name(sym)})  ΔOOS={r['avg_delta']:+.3f}  OOS胜率={r['win_rate'] * 100:.0f}%"
                )
                for rg, params in sorted(rec.items()):
                    print(f"    {rg}: T×{params['T']:.2f}  stop×{params['stop']:.2f}")
                print()

    # 保存结果
    os.makedirs("logs", exist_ok=True)
    output = {
        "baseline": {sym: {k: v for k, v in r.items() if k != "trades_detail"} for sym, r in baseline.items()},
        "weak_symbols": sorted(weak_syms),
        "scan_results": scan_results,
        "oos_results": {sym: {k: v for k, v in r.items() if k != "folds"} for sym, r in oos_results.items()},
    }
    with open("logs/regime_rc_optimization.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  详细结果 → logs/regime_rc_optimization.json")
    print("=" * 80)


if __name__ == "__main__":
    main()
