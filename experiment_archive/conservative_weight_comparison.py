"""
保守权重方案对比（WF OOS 验证）

对比多种权重方案的 OOS 表现，找到最稳健的配置：
  1. equal      等权（基准）
  2. vol_inv    波动率倒数（风险平价，最稳健的非对称方案）
  3. half_kelly 半凯利（expR/方差 * 0.5，更保守）
  4. mild_tilt  轻度倾斜（等权为基础，±30% 范围内按 expR 调整）
  5. kelly      全凯利（对照，预期过拟合）

约束：单品种 ≤ 10%，单板块 ≤ 35%
"""

import copy
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest
from portfolio_manager import symbols_group


def get_symbol_trades(sym, cfg, window=200):
    try:
        df = load_daily(sym)
        if df is None or len(df) < window + 50:
            return None
        r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=window)
        if not r or not r.get("trades_detail"):
            return None
        trades = []
        for t in r["trades_detail"]:
            d = t.get("entry_date")
            if d is not None:
                trades.append({"date": d, "R_adj": t["R_adj"]})
        trades.sort(key=lambda x: x["date"])
        return trades
    except Exception:
        return None


def split_date_windows(all_dates, n_windows=5):
    all_dates = sorted(all_dates)
    n = len(all_dates)
    if n < 100:
        return []
    test_size = n // (n_windows + 2)
    train_min = n // 3
    windows = []
    for i in range(n_windows):
        test_start_idx = train_min + i * test_size
        test_end_idx = test_start_idx + test_size
        if test_end_idx >= n:
            break
        windows.append((all_dates[0], all_dates[test_start_idx], all_dates[test_start_idx], all_dates[test_end_idx]))
    return windows


def apply_weight_constraints(weights, max_single=0.10, max_sector=0.35):
    """应用权重约束（单品种上限 + 板块上限）。"""
    w = dict(weights)

    # 单品种上限（迭代收敛）
    for _ in range(10):
        adjusted = False
        for sym in list(w.keys()):
            if w[sym] > max_single:
                excess = w[sym] - max_single
                w[sym] = max_single
                others = [s for s in w if s != sym and w[s] < max_single]
                if others:
                    total_other = sum(w[s] for s in others)
                    if total_other > 0:
                        for s in others:
                            w[s] += excess * w[s] / total_other
                adjusted = True
        if not adjusted:
            break

    # 板块上限
    sectors = {}
    for sym, sw in w.items():
        g = symbols_group(sym)
        sectors.setdefault(g, []).append(sym)

    for _ in range(10):
        adjusted = False
        for sec, sec_syms in sectors.items():
            sec_weight = sum(w[s] for s in sec_syms)
            if sec_weight > max_sector:
                scale = max_sector / sec_weight
                moved = sec_weight - max_sector
                for s in sec_syms:
                    w[s] *= scale
                other_syms = [s for s in w if s not in sec_syms]
                if other_syms:
                    other_total = sum(w[s] for s in other_syms)
                    if other_total > 0:
                        for s in other_syms:
                            w[s] += moved * w[s] / other_total
                adjusted = True
        if not adjusted:
            break

    total = sum(w.values())
    if total > 0:
        w = {s: v / total for s, v in w.items()}
    return w


def compute_weights(symbol_trades, train_end_date, method="equal", max_single=0.10, max_sector=0.35):
    """计算不同方法的权重。"""
    syms = list(symbol_trades.keys())
    n = len(syms)

    # 提取训练期数据
    train_data = {}
    for sym, trades in symbol_trades.items():
        train_Rs = [t["R_adj"] for t in trades if t["date"] < train_end_date]
        if len(train_Rs) >= 5:
            train_data[sym] = {
                "expR": float(np.mean(train_Rs)),
                "stdR": float(np.std(train_Rs)),
                "n": len(train_Rs),
            }

    if method == "equal":
        raw = {s: 1.0 / n for s in syms}
        return apply_weight_constraints(raw, max_single, max_sector)

    if not train_data:
        raw = {s: 1.0 / n for s in syms}
        return apply_weight_constraints(raw, max_single, max_sector)

    if method == "vol_inv":
        # 波动率倒数：低波动 → 高权重
        raw = {}
        for sym in syms:
            if sym in train_data and train_data[sym]["stdR"] > 0:
                raw[sym] = 1.0 / train_data[sym]["stdR"]
            else:
                raw[sym] = 1.0  # 数据不足的给中位权重
        total = sum(raw.values())
        raw = {s: v / total for s, v in raw.items()}
        return apply_weight_constraints(raw, max_single, max_sector)

    if method == "half_kelly":
        # 半凯利 = 0.5 * expR / 方差
        raw = {}
        for sym in syms:
            if sym in train_data:
                td = train_data[sym]
                if td["expR"] > 0 and td["stdR"] > 0:
                    raw[sym] = 0.5 * td["expR"] / (td["stdR"] ** 2)
                else:
                    raw[sym] = 0.001  # 负期望给很小权重
            else:
                raw[sym] = 0.001
        total = sum(raw.values())
        if total > 0:
            raw = {s: v / total for s, v in raw.items()}
        return apply_weight_constraints(raw, max_single, max_sector)

    if method == "kelly":
        # 全凯利
        raw = {}
        for sym in syms:
            if sym in train_data:
                td = train_data[sym]
                if td["expR"] > 0 and td["stdR"] > 0:
                    raw[sym] = td["expR"] / (td["stdR"] ** 2)
                else:
                    raw[sym] = 0.001
            else:
                raw[sym] = 0.001
        total = sum(raw.values())
        if total > 0:
            raw = {s: v / total for s, v in raw.items()}
        return apply_weight_constraints(raw, max_single, max_sector)

    if method == "mild_tilt":
        # 轻度倾斜：以等权为基础，按 expR 排名在 ±30% 内调整
        # expR 高 → 1.3x 等权；expR 低 → 0.7x 等权
        valid_syms = [s for s in syms if s in train_data and train_data[s]["n"] >= 8]
        invalid_syms = [s for s in syms if s not in valid_syms]

        # 按 expR 排序
        sorted_syms = sorted(valid_syms, key=lambda s: train_data[s]["expR"], reverse=True)
        n_valid = len(sorted_syms)

        raw = {}
        equal_w = 1.0 / n
        for i, sym in enumerate(sorted_syms):
            # 排名百分位 → 倾斜因子（-30% ~ +30%）
            percentile = i / max(n_valid - 1, 1)  # 0=最好, 1=最差
            tilt = 1.3 - 0.6 * percentile  # 1.3 → 0.7
            raw[sym] = equal_w * tilt

        for sym in invalid_syms:
            raw[sym] = equal_w  # 数据不足的给等权

        total = sum(raw.values())
        if total > 0:
            raw = {s: v / total for s, v in raw.items()}
        return apply_weight_constraints(raw, max_single, max_sector)

    # 回落等权
    raw = {s: 1.0 / n for s in syms}
    return apply_weight_constraints(raw, max_single, max_sector)


def build_portfolio_series(symbol_trades, weights):
    daily_R = {}
    for sym, trades in symbol_trades.items():
        w = weights.get(sym, 0)
        if w <= 0:
            continue
        for t in trades:
            d = t["date"]
            d = pd.Timestamp(d) if hasattr(d, "date") else pd.Timestamp(str(d))
            daily_R[d] = daily_R.get(d, 0) + t["R_adj"] * w
    if not daily_R:
        return pd.Series(dtype=float)
    return pd.Series(daily_R).sort_index()


def compute_metrics(daily_R, start_date, end_date):
    mask = (daily_R.index >= start_date) & (daily_R.index < end_date)
    window_R = daily_R[mask]
    if len(window_R) < 3:
        return None
    cumulative = window_R.cumsum()
    total_R = float(cumulative.iloc[-1])
    running_max = cumulative.cummax()
    max_dd = float((running_max - cumulative).max())
    expR = float(window_R.mean())
    win_rate = float((window_R > 0).mean())
    calmar = total_R / max_dd if max_dd > 0.001 else float("inf")
    return {
        "total_R": round(total_R, 2),
        "expR": round(expR, 4),
        "win_rate": round(win_rate, 4),
        "max_dd": round(max_dd, 3),
        "n_trades": len(window_R),
        "calmar": round(calmar, 2),
    }


def main():
    print("=" * 70)
    print("保守权重方案对比（Walk-Forward OOS 验证）")
    print("=" * 70)

    base_cfg = copy.deepcopy(DEFAULT_CONFIG)
    base_cfg["portfolio"]["enabled"] = False
    active_syms = list(base_cfg["per_symbol_risk"].keys())

    # 加载数据
    print(f"\n[1/3] 加载 {len(active_syms)} 个品种 ...")
    symbol_trades = {}
    for i, sym in enumerate(active_syms):
        trades = get_symbol_trades(sym, base_cfg)
        if trades and len(trades) >= 10:
            symbol_trades[sym] = trades
        print(f"  [{i + 1}/{len(active_syms)}] {sym:>5}", end="\r", flush=True)
    print(f"\n  有效品种: {len(symbol_trades)} 个")

    # 日期范围
    all_dates = set()
    for sym, trades in symbol_trades.items():
        for t in trades:
            d = pd.Timestamp(t["date"]) if hasattr(t["date"], "date") else pd.Timestamp(str(t["date"]))
            all_dates.add(d)
    all_dates = sorted(all_dates)
    print(f"  时间范围: {all_dates[0].date()} ~ {all_dates[-1].date()} ({len(all_dates)} 天)")

    # 窗口
    n_windows = 5
    windows = split_date_windows(all_dates, n_windows)
    print(f"  测试窗口: {len(windows)} 个")

    # 方法对比
    methods = ["equal", "vol_inv", "mild_tilt", "half_kelly", "kelly"]
    method_names = {
        "equal": "等权基准",
        "vol_inv": "波动率倒数",
        "mild_tilt": "轻度倾斜±30%",
        "half_kelly": "半凯利",
        "kelly": "全凯利",
    }

    print("\n[2/3] 逐方法逐窗口计算 ...")

    # 存储所有结果
    all_results = {m: [] for m in methods}

    for w_idx, (tr_start, tr_end, te_start, te_end) in enumerate(windows):
        print(f"\n  窗口 {w_idx + 1} ({te_start.date()} ~ {te_end.date()})")

        for method in methods:
            weights = compute_weights(symbol_trades, tr_end, method=method, max_single=0.10, max_sector=0.35)
            daily = build_portfolio_series(symbol_trades, weights)
            m = compute_metrics(daily, te_start, te_end)
            if m:
                all_results[method].append(m)

    # 汇总对比
    print("\n[3/3] 汇总对比")
    print(f"\n{'=' * 70}")
    print("  OOS 表现对比（所有测试窗口汇总）")
    print(f"{'=' * 70}")
    print(f"\n  {'方案':<14} {'总收益':>8} {'窗口胜率':>8} {'平均DD':>8} {'平均Calmar':>10} {'排名':>5}")
    print(f"  {'-' * 65}")

    summary = {}
    for method in methods:
        results = all_results[method]
        if not results:
            continue

        total_R = sum(r["total_R"] for r in results)
        avg_dd = float(np.mean([r["max_dd"] for r in results]))
        calmar = total_R / avg_dd if avg_dd > 0 else float("inf")

        # 对比等权的窗口胜率
        eq_results = all_results["equal"]
        wins = sum(1 for i, r in enumerate(results) if i < len(eq_results) and r["total_R"] > eq_results[i]["total_R"])
        win_rate = wins / len(results) * 100 if results else 0

        summary[method] = {
            "total_R": round(total_R, 2),
            "win_rate_vs_equal": round(win_rate, 1),
            "avg_max_dd": round(avg_dd, 3),
            "calmar": round(calmar, 2),
            "n_windows": len(results),
        }

    # 按总收益排序
    ranked = sorted(summary.items(), key=lambda x: -x[1]["total_R"])
    for rank, (method, s) in enumerate(ranked, 1):
        print(
            f"  {method_names[method]:<14} {s['total_R']:>+8.2f} "
            f"{s['win_rate_vs_equal']:>7.0f}% {s['avg_max_dd']:>8.3f} "
            f"{s['calmar']:>10.2f}  #{rank}"
        )

    # 推荐方案
    print(f"\n{'=' * 70}")
    print("  🏆 推荐方案")
    print(f"{'=' * 70}")

    best_method = ranked[0][0]
    best_total = ranked[0][1]["total_R"]
    equal_total = summary.get("equal", {}).get("total_R", 0)

    if best_method == "equal":
        print("\n  ⚠️  等权基准就是最优解！")
        print("  这符合金融研究中的经典结论：1/N 策略很难被击败。")
        print("  建议：保持等权，把精力放在单品种优化（P0-P2）上。")
    else:
        excess = best_total - equal_total
        pct = excess / abs(equal_total) * 100 if equal_total != 0 else 0
        print(f"\n  ✅ 推荐: {method_names[best_method]}")
        print(f"     超额收益: {excess:+.2f} R ({pct:+.1f}%)")
        print(f"     窗口胜率: {ranked[0][1]['win_rate_vs_equal']:.0f}%")

    # 稳健性排序
    print("\n  稳健性排序（综合收益 + 胜率 + Calmar）：")
    robust_score = {}
    for method, s in summary.items():
        eq = summary.get("equal", {})
        # 收益排名分 + 胜率分 + Calmar 分
        total_rank = next(i for i, (m, _) in enumerate(ranked) if m == method)
        score = (5 - total_rank) * 2 + s["win_rate_vs_equal"] / 20 + (s["calmar"] / 2)
        robust_score[method] = round(score, 2)

    robust_ranked = sorted(robust_score.items(), key=lambda x: -x[1])
    for rank, (method, score) in enumerate(robust_ranked, 1):
        print(f"    {rank}. {method_names[method]:<12} 得分 {score:.1f}")

    # 保存
    os.makedirs("logs", exist_ok=True)
    output = {
        "date": datetime.now().isoformat(),
        "n_symbols": len(symbol_trades),
        "n_windows": len(windows),
        "methods": {m: {"name": method_names[m], "summary": summary.get(m, {})} for m in methods},
        "ranked_by_return": [m for m, _ in ranked],
        "best_method": best_method,
        "robust_ranking": [m for m, _ in robust_ranked],
    }
    with open("logs/conservative_weight_comparison.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  结果已保存 → logs/conservative_weight_comparison.json")

    # 更新部署配置中的推荐权重
    print("\n  更新部署配置 ...")
    deploy_path = os.path.join(HERE, "deploy", "trade_config_deploy.json")
    if os.path.exists(deploy_path):
        with open(deploy_path, encoding="utf-8") as f:
            deploy_cfg = json.load(f)

        # 基于最新 WF 验证结果，使用最佳方案的全样本权重
        best_method_wf = robust_ranked[0][0]  # 最稳健的方法
        print(f"  采用最稳健方案: {method_names[best_method_wf]}")

        # 用全量数据计算推荐权重
        last_date = all_dates[-1]
        rec_weights = compute_weights(symbol_trades, last_date, method=best_method_wf, max_single=0.10, max_sector=0.35)

        active_list = sorted(rec_weights.keys())
        deploy_cfg["portfolio"]["weights"] = {k: round(v, 4) for k, v in rec_weights.items()}
        deploy_cfg["portfolio"]["active_symbols"] = active_list
        deploy_cfg["portfolio"]["mode"] = "manual"
        deploy_cfg["portfolio"]["note"] = (
            f"WF OOS 验证后修正：采用 {method_names[best_method_wf]} 方案，"
            f"OOS 超额收益 {ranked[0][1]['total_R'] - equal_total:+.2f}R，"
            f"窗口胜率 {ranked[0][1]['win_rate_vs_equal']:.0f}%"
        )
        deploy_cfg["portfolio"]["recommended_method"] = best_method_wf

        with open(deploy_path, "w", encoding="utf-8") as f:
            json.dump(deploy_cfg, f, ensure_ascii=False, indent=2)
        print("  ✅ 已更新 deploy/trade_config_deploy.json")

        # 板块分布
        sectors = {}
        for sym, w in rec_weights.items():
            g = symbols_group(sym)
            sectors[g] = sectors.get(g, 0) + w
        print("\n  推荐权重板块分布：")
        for g, w in sorted(sectors.items(), key=lambda x: -x[1]):
            bar = "█" * int(w * 40)
            print(f"    {g:<6} {w * 100:>5.1f}%  {bar}")


if __name__ == "__main__":
    main()
