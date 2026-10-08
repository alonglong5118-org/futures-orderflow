"""
P3: 弱品种筛选 + 动态门控策略分析工具

功能：
  1. 弱品种筛选：基于长期表现建立白名单/黑名单
  2. 动态门控分析：
     - 连续亏损熔断：连续 N 笔亏损后暂停 M 根K线
     - 滚动收益门控：滚动窗口 expR 低于阈值时降仓/暂停
     - 最大回撤熔断：回撤超阈值时暂停
  3. Walk-forward OOS 验证各方案效果
"""

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


def symbols_name(sym):
    return SYMBOLS.get(sym, {}).get("name", sym)


# ── 弱品种筛选 ──────────────────────────────────────────────────────────────
def screen_symbols(results, criteria=None):
    """按筛选条件对品种分级。

    criteria: {
        "min_trades": 20,           # 最少交易笔数
        "min_expR": 0.05,           # 最低 expR（白名单门槛）
        "max_dd": 30,               # 最大回撤上限（R 单位）
        "min_win_rate": 0.25,       # 最低胜率
        "blacklist_expR": -0.1,     # expR 低于此值 → 黑名单
    }
    """
    if criteria is None:
        criteria = {
            "min_trades": 20,
            "min_expR": 0.05,
            "max_dd": 30,
            "min_win_rate": 0.25,
            "blacklist_expR": -0.1,
        }

    valid = {s: r for s, r in results.items() if r.get("trades", 0) >= criteria["min_trades"]}

    whitelist = {}
    watchlist = {}
    blacklist = {}

    for sym, r in valid.items():
        expr = r.get("expR", 0)
        wr = r.get("win_rate", 0)
        dd = r.get("max_dd", 0)
        trades = r.get("trades", 0)

        score = 0
        # expR 评分（权重最大）
        if expr >= 0.3:
            score += 3
        elif expr >= 0.1:
            score += 2
        elif expr >= 0.05:
            score += 1
        elif expr >= -0.1:
            score += 0
        else:
            score -= 2
        # 胜率评分
        if wr >= 0.35:
            score += 1
        elif wr >= 0.28:
            score += 0.5
        # 回撤评分
        if dd <= 10:
            score += 1
        elif dd <= 20:
            score += 0.5
        elif dd > 40:
            score -= 1
        # 笔数评分（样本量）
        if trades >= 100:
            score += 1
        elif trades >= 50:
            score += 0.5

        if expr >= criteria["min_expR"] and wr >= criteria["min_win_rate"] and dd <= criteria["max_dd"]:
            whitelist[sym] = {"score": round(score, 1), "expR": expr, "win_rate": wr, "max_dd": dd, "trades": trades}
        elif expr < criteria["blacklist_expR"] or dd > criteria["max_dd"] * 1.5:
            blacklist[sym] = {"score": round(score, 1), "expR": expr, "win_rate": wr, "max_dd": dd, "trades": trades}
        else:
            watchlist[sym] = {"score": round(score, 1), "expR": expr, "win_rate": wr, "max_dd": dd, "trades": trades}

    return {
        "whitelist": dict(sorted(whitelist.items(), key=lambda x: x[1]["score"], reverse=True)),
        "watchlist": dict(sorted(watchlist.items(), key=lambda x: x[1]["score"], reverse=True)),
        "blacklist": dict(sorted(blacklist.items(), key=lambda x: x[1]["score"])),
    }


# ── 动态门控：连续亏损熔断 ──────────────────────────────────────────────────
def apply_consecutive_loss_gate(trades_detail, max_consecutive_loss=3, cooldown_bars=20):
    """连续亏损熔断：连续 max_consecutive_loss 笔亏损后，暂停 cooldown_bars 根K线。

    返回过滤后的 trades_detail（被熔断的交易被移除）。
    """
    if not trades_detail:
        return trades_detail

    filtered = []
    consecutive_losses = 0
    cooldown_until_bar = -1

    for trade in trades_detail:
        entry_bar = trade.get("entry_bar", 0)

        # 检查是否在冷却期
        if entry_bar < cooldown_until_bar:
            continue  # 跳过这笔交易

        filtered.append(trade)

        if trade.get("R_adj", 0) < 0:
            consecutive_losses += 1
            if consecutive_losses >= max_consecutive_loss:
                # 触发熔断，进入冷却
                cooldown_until_bar = entry_bar + cooldown_bars
                consecutive_losses = 0
        else:
            consecutive_losses = 0

    return filtered


def compute_metrics_from_trades(trades_detail):
    """从 trades_detail 计算绩效指标。"""
    if not trades_detail:
        return {"expR": 0, "win_rate": 0, "trades": 0, "total_R": 0, "max_dd": 0}

    Rs = [t["R_adj"] for t in trades_detail]
    wins = [r for r in Rs if r > 0]
    total_R = sum(Rs)

    # 计算最大回撤
    cumulative = np.cumsum(Rs)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

    return {
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades": len(Rs),
        "total_R": round(total_R, 3),
        "max_dd": round(max_dd, 3),
    }


# ── 动态门控：滚动收益降仓 ──────────────────────────────────────────────────
def apply_rolling_return_gate(trades_detail, window=10, min_expr=-0.2, reduce_pct=0.5):
    """滚动收益门控：滚动 window 笔交易 expR 低于 min_expr 时，后续交易收益按 reduce_pct 打折（模拟降仓）。

    返回调整后的 trades_detail（R_adj 被打折）。
    """
    if not trades_detail or len(trades_detail) < window:
        return trades_detail

    adjusted = []
    for i, trade in enumerate(trades_detail):
        new_trade = dict(trade)
        if i >= window:
            # 计算前 window 笔的 expR
            recent_Rs = [trades_detail[j]["R_adj"] for j in range(i - window, i)]
            recent_expr = float(np.mean(recent_Rs))
            if recent_expr < min_expr:
                # 降仓：收益打折
                new_trade["R_adj"] = round(trade["R_adj"] * reduce_pct, 4)
                new_trade["gated"] = True
                new_trade["gate_reason"] = f"rolling_{window}_expR={recent_expr:.3f}"
        adjusted.append(new_trade)

    return adjusted


# ── Walk-Forward OOS 验证动态门控 ──────────────────────────────────────────
def wf_validate_gating(symbol, df, gate_type="consecutive_loss", gate_params=None, n_folds=5, oos_bars=100, window=200):
    """Walk-forward OOS 验证动态门控效果。

    gate_type: "consecutive_loss" | "rolling_return"
    """
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

        # OOS 区间
        oos_start = max(0, train_end - window)
        oos_end = min(len(df), train_end + oos_bars)
        if oos_end - oos_start < window + 10:
            continue

        df_oos = df.iloc[oos_start:oos_end]

        # 基线回测
        r_base = walk_forward_backtest(symbol, cfg=DEFAULT_CONFIG, df_in=df_oos, window=window)
        base_trades = r_base.get("trades_detail", [])
        if not base_trades:
            continue

        # 应用门控
        if gate_type == "consecutive_loss":
            max_cl = gate_params.get("max_consecutive_loss", 3) if gate_params else 3
            cd_bars = gate_params.get("cooldown_bars", 20) if gate_params else 20
            gated_trades = apply_consecutive_loss_gate(base_trades, max_cl, cd_bars)
        elif gate_type == "rolling_return":
            w = gate_params.get("window", 10) if gate_params else 10
            min_e = gate_params.get("min_expr", -0.2) if gate_params else -0.2
            rp = gate_params.get("reduce_pct", 0.5) if gate_params else 0.5
            gated_trades = apply_rolling_return_gate(base_trades, w, min_e, rp)
        else:
            gated_trades = base_trades

        base_metrics = compute_metrics_from_trades(base_trades)
        gated_metrics = compute_metrics_from_trades(gated_trades)

        folds.append(
            {
                "train_end": train_end,
                "base": base_metrics,
                "gated": gated_metrics,
                "delta_expr": round(gated_metrics["expR"] - base_metrics["expR"], 4),
                "delta_dd": round(gated_metrics["max_dd"] - base_metrics["max_dd"], 4),
            }
        )

    if not folds:
        return None

    avg_base_expr = np.mean([f["base"]["expR"] for f in folds])
    avg_gated_expr = np.mean([f["gated"]["expR"] for f in folds])
    avg_delta = avg_gated_expr - avg_base_expr
    win_folds = sum(1 for f in folds if f["delta_expr"] > 0)

    avg_base_dd = np.mean([f["base"]["max_dd"] for f in folds])
    avg_gated_dd = np.mean([f["gated"]["max_dd"] for f in folds])

    return {
        "symbol": symbol,
        "n_folds": len(folds),
        "gate_type": gate_type,
        "gate_params": gate_params or {},
        "avg_base_expr": round(avg_base_expr, 4),
        "avg_gated_expr": round(avg_gated_expr, 4),
        "avg_delta_expr": round(avg_delta, 4),
        "win_rate": round(win_folds / len(folds), 3),
        "avg_base_dd": round(avg_base_dd, 3),
        "avg_gated_dd": round(avg_gated_dd, 3),
        "avg_delta_dd": round(avg_gated_dd - avg_base_dd, 3),
        "folds": folds,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("P3: 弱品种筛选 + 动态门控策略分析")
    print("=" * 80)

    # 全品种回测
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

    results = {}
    total = len(candidate_syms)
    for i, sym in enumerate(candidate_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=DEFAULT_CONFIG, df_in=df, window=200)
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

    # 阶段 1: 弱品种筛选
    print("\n  阶段 1/4: 弱品种筛选")
    screening = screen_symbols(results)
    print(f"    白名单（可交易）: {len(screening['whitelist'])} 个")
    print(f"    观察名单: {len(screening['watchlist'])} 个")
    print(f"    黑名单（应规避）: {len(screening['blacklist'])} 个")

    print("\n  🟢 白名单品种（按综合得分排序）:")
    print(f"    {'排名':>4}  {'品种':>5}  {'名称':>8}  {'得分':>5}  {'expR':>7}  {'胜率':>6}  {'DD':>7}  {'笔数':>5}")
    for rank, (sym, info) in enumerate(screening["whitelist"].items(), 1):
        print(
            f"    {rank:>4}  {sym:>5}  {symbols_name(sym):>8}  {info['score']:>5.1f}  "
            f"{info['expR']:>+7.3f}  {info['win_rate'] * 100:>5.1f}%  "
            f"{info['max_dd']:>7.2f}  {info['trades']:>5}"
        )

    if screening["watchlist"]:
        print("\n  🟡 观察名单:")
        for sym, info in list(screening["watchlist"].items())[:10]:
            print(
                f"    {sym:>5} {symbols_name(sym):>8}  score={info['score']:.1f}  "
                f"expR={info['expR']:+.3f}  DD={info['max_dd']:.1f}"
            )

    if screening["blacklist"]:
        print("\n  🔴 黑名单品种（建议剔除）:")
        for sym, info in screening["blacklist"].items():
            print(
                f"    {sym:>5} {symbols_name(sym):>8}  score={info['score']:.1f}  "
                f"expR={info['expR']:+.3f}  DD={info['max_dd']:.1f}  trades={info['trades']}"
            )

    # 阶段 2: 连续亏损熔断 - 参数扫描（弱品种）
    print("\n  阶段 2/4: 连续亏损熔断 - 弱品种参数扫描")
    weak_syms = list(screening["blacklist"].keys()) + list(screening["watchlist"].keys())[:5]
    weak_syms = [s for s in weak_syms if s in results]

    # 候选参数组合
    cl_params_list = [
        {"max_consecutive_loss": 2, "cooldown_bars": 10},
        {"max_consecutive_loss": 3, "cooldown_bars": 15},
        {"max_consecutive_loss": 3, "cooldown_bars": 20},
        {"max_consecutive_loss": 4, "cooldown_bars": 20},
        {"max_consecutive_loss": 4, "cooldown_bars": 30},
    ]

    print(f"\n    弱品种数: {len(weak_syms)}")
    print(f"    参数组合数: {len(cl_params_list)}")
    print()

    cl_results_by_sym = {}
    for sym in sorted(weak_syms)[:8]:  # 限制数量避免太慢
        r = results[sym]
        trades = r.get("trades_detail", [])
        if len(trades) < 20:
            continue

        base_metrics = compute_metrics_from_trades(trades)
        best_result = None
        best_delta = -999

        for params in cl_params_list:
            gated = apply_consecutive_loss_gate(trades, params["max_consecutive_loss"], params["cooldown_bars"])
            gated_metrics = compute_metrics_from_trades(gated)
            delta = gated_metrics["expR"] - base_metrics["expR"]
            dd_reduction = base_metrics["max_dd"] - gated_metrics["max_dd"]

            # 综合评分：expR 提升 + 回撤降低
            score = delta * 100 + dd_reduction * 0.3

            if score > best_delta:
                best_delta = score
                best_result = {
                    "params": params,
                    "base": base_metrics,
                    "gated": gated_metrics,
                    "delta_expr": round(delta, 4),
                    "delta_dd": round(gated_metrics["max_dd"] - base_metrics["max_dd"], 3),
                    "trades_reduction": base_metrics["trades"] - gated_metrics["trades"],
                }

        if best_result:
            cl_results_by_sym[sym] = best_result
            p = best_result["params"]
            print(
                f"    {sym:>5} ({symbols_name(sym)}): "
                f"expR {best_result['base']['expR']:+.3f} → {best_result['gated']['expR']:+.3f} "
                f"({best_result['delta_expr']:+.3f})  "
                f"DD {best_result['base']['max_dd']:.1f} → {best_result['gated']['max_dd']:.1f}  "
                f"参数: CL={p['max_consecutive_loss']} CD={p['cooldown_bars']}"
            )

    # 阶段 3: 滚动收益降仓 - 参数扫描
    print("\n  阶段 3/4: 滚动收益降仓 - 弱品种参数扫描")

    rr_params_list = [
        {"window": 5, "min_expr": -0.3, "reduce_pct": 0.5},
        {"window": 8, "min_expr": -0.25, "reduce_pct": 0.5},
        {"window": 10, "min_expr": -0.2, "reduce_pct": 0.5},
        {"window": 10, "min_expr": -0.3, "reduce_pct": 0.3},
        {"window": 15, "min_expr": -0.2, "reduce_pct": 0.4},
    ]

    rr_results_by_sym = {}
    for sym in sorted(weak_syms)[:8]:
        r = results[sym]
        trades = r.get("trades_detail", [])
        if len(trades) < 20:
            continue

        base_metrics = compute_metrics_from_trades(trades)
        best_result = None
        best_delta = -999

        for params in rr_params_list:
            gated = apply_rolling_return_gate(trades, params["window"], params["min_expr"], params["reduce_pct"])
            gated_metrics = compute_metrics_from_trades(gated)
            delta = gated_metrics["expR"] - base_metrics["expR"]
            dd_reduction = base_metrics["max_dd"] - gated_metrics["max_dd"]

            score = delta * 100 + dd_reduction * 0.3
            if score > best_delta:
                best_delta = score
                best_result = {
                    "params": params,
                    "base": base_metrics,
                    "gated": gated_metrics,
                    "delta_expr": round(delta, 4),
                    "delta_dd": round(gated_metrics["max_dd"] - base_metrics["max_dd"], 3),
                }

        if best_result:
            rr_results_by_sym[sym] = best_result
            p = best_result["params"]
            print(
                f"    {sym:>5} ({symbols_name(sym)}): "
                f"expR {best_result['base']['expR']:+.3f} → {best_result['gated']['expR']:+.3f} "
                f"({best_result['delta_expr']:+.3f})  "
                f"DD {best_result['base']['max_dd']:.1f} → {best_result['gated']['max_dd']:.1f}  "
                f"参数: w={p['window']} minR={p['min_expr']} reduce={p['reduce_pct']}"
            )

    # 阶段 4: Walk-Forward OOS 验证最佳方案
    print("\n  阶段 4/4: Walk-Forward OOS 验证（IS 改善品种）")

    # 选择 IS 改善的品种做 OOS 验证
    oos_candidates_cl = [s for s, r in cl_results_by_sym.items() if r["delta_expr"] > 0.02]
    oos_candidates_rr = [s for s, r in rr_results_by_sym.items() if r["delta_expr"] > 0.02]

    print(f"\n    连续亏损熔断 - OOS 验证品种: {len(oos_candidates_cl)}")
    cl_oos_results = {}
    for i, sym in enumerate(oos_candidates_cl[:6]):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 600:
                continue
            params = cl_results_by_sym[sym]["params"]
            print(f"      [{i + 1}/{min(6, len(oos_candidates_cl))}] {sym:>5} OOS 验证中...", end="\r", flush=True)
            oos_r = wf_validate_gating(sym, df, "consecutive_loss", params, n_folds=5, oos_bars=100)
            if oos_r:
                cl_oos_results[sym] = oos_r
        except Exception:
            pass
    print()

    print(f"    滚动收益降仓 - OOS 验证品种: {len(oos_candidates_rr)}")
    rr_oos_results = {}
    for i, sym in enumerate(oos_candidates_rr[:6]):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 600:
                continue
            params = rr_results_by_sym[sym]["params"]
            print(f"      [{i + 1}/{min(6, len(oos_candidates_rr))}] {sym:>5} OOS 验证中...", end="\r", flush=True)
            oos_r = wf_validate_gating(sym, df, "rolling_return", params, n_folds=5, oos_bars=100)
            if oos_r:
                rr_oos_results[sym] = oos_r
        except Exception:
            pass
    print()

    # OOS 结果汇总
    print(f"\n{'=' * 80}")
    print("  OOS 验证结果汇总")
    print(f"{'=' * 80}")

    if cl_oos_results:
        pos_cl = {k: v for k, v in cl_oos_results.items() if v["avg_delta_expr"] > 0}
        print("\n  连续亏损熔断:")
        print(f"    验证品种: {len(cl_oos_results)} 个，正收益: {len(pos_cl)} 个")
        if pos_cl:
            print(f"    {'品种':>5}  {'基准expR':>8}  {'门控expR':>8}  {'ΔexpR':>7}  {'胜率':>6}  {'ΔDD':>7}")
            for sym, r in sorted(pos_cl.items(), key=lambda x: x[1]["avg_delta_expr"], reverse=True):
                print(
                    f"    {sym:>5}  {r['avg_base_expr']:>+8.3f}  {r['avg_gated_expr']:>+8.3f}  "
                    f"{r['avg_delta_expr']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  {r['avg_delta_dd']:>+7.2f}"
                )

    if rr_oos_results:
        pos_rr = {k: v for k, v in rr_oos_results.items() if v["avg_delta_expr"] > 0}
        print("\n  滚动收益降仓:")
        print(f"    验证品种: {len(rr_oos_results)} 个，正收益: {len(pos_rr)} 个")
        if pos_rr:
            print(f"    {'品种':>5}  {'基准expR':>8}  {'门控expR':>8}  {'ΔexpR':>7}  {'胜率':>6}  {'ΔDD':>7}")
            for sym, r in sorted(pos_rr.items(), key=lambda x: x[1]["avg_delta_expr"], reverse=True):
                print(
                    f"    {sym:>5}  {r['avg_base_expr']:>+8.3f}  {r['avg_gated_expr']:>+8.3f}  "
                    f"{r['avg_delta_expr']:>+7.3f}  {r['win_rate'] * 100:>5.1f}%  {r['avg_delta_dd']:>+7.2f}"
                )

    # 最终推荐
    print(f"\n{'=' * 80}")
    print("  最终建议")
    print(f"{'=' * 80}")
    print("\n  1. 弱品种筛选建议:")
    print(f"     - 白名单（{len(screening['whitelist'])} 个）：保留并重点配置")
    print(f"     - 观察名单（{len(screening['watchlist'])} 个）：保留但谨慎监控")
    print(f"     - 黑名单（{len(screening['blacklist'])} 个）：建议剔除或仅做模拟")

    robust_cl = {k: v for k, v in cl_oos_results.items() if v["avg_delta_expr"] > 0 and v["win_rate"] >= 0.6}
    robust_rr = {k: v for k, v in rr_oos_results.items() if v["avg_delta_expr"] > 0 and v["win_rate"] >= 0.6}

    if robust_cl or robust_rr:
        print("\n  2. 动态门控建议（OOS 稳健通过）:")
        if robust_cl:
            print(f"     连续亏损熔断 - {len(robust_cl)} 个品种稳健:")
            for sym, r in sorted(robust_cl.items(), key=lambda x: x[1]["avg_delta_expr"], reverse=True):
                p = r["gate_params"]
                print(
                    f"       {sym:>5}: Δ={r['avg_delta_expr']:+.3f} 胜率={r['win_rate'] * 100:.0f}% "
                    f"(连续{p['max_consecutive_loss']}亏→冷却{p['cooldown_bars']}根)"
                )
        if robust_rr:
            print(f"     滚动收益降仓 - {len(robust_rr)} 个品种稳健:")
            for sym, r in sorted(robust_rr.items(), key=lambda x: x[1]["avg_delta_expr"], reverse=True):
                p = r["gate_params"]
                print(
                    f"       {sym:>5}: Δ={r['avg_delta_expr']:+.3f} 胜率={r['win_rate'] * 100:.0f}% "
                    f"(滚动{p['window']}笔expR<{p['min_expr']}→降仓{p['reduce_pct'] * 100:.0f}%)"
                )
    else:
        print("\n  2. 动态门控：OOS 验证暂未发现稳健有效的方案，建议继续观察")

    # 保存结果
    os.makedirs("logs", exist_ok=True)
    output = {
        "screening": {
            "whitelist": screening["whitelist"],
            "watchlist": screening["watchlist"],
            "blacklist": screening["blacklist"],
        },
        "consecutive_loss_is": {
            sym: {k: v for k, v in r.items() if k != "base" and k != "gated"} for sym, r in cl_results_by_sym.items()
        },
        "rolling_return_is": {
            sym: {k: v for k, v in r.items() if k != "base" and k != "gated"} for sym, r in rr_results_by_sym.items()
        },
        "consecutive_loss_oos": {
            sym: {k: v for k, v in r.items() if k != "folds"} for sym, r in cl_oos_results.items()
        },
        "rolling_return_oos": {sym: {k: v for k, v in r.items() if k != "folds"} for sym, r in rr_oos_results.items()},
    }
    with open("logs/p3_gating_analysis.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  详细结果 → logs/p3_gating_analysis.json")
    print("=" * 80)


if __name__ == "__main__":
    main()
