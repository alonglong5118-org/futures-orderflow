"""
Walk-Forward 滚动验证：P4 组合优化 vs 基线

用 entry_date 对齐各品种交易，按时间切分窗口，模拟定期调仓场景。
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

from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)
from portfolio_manager import symbols_group


def get_symbol_trades(sym, cfg, window=200):
    """获取单品种的交易列表（带日期）。"""
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
                trades.append({"date": d, "R_adj": t["R_adj"], "regime": t.get("regime", "")})
        trades.sort(key=lambda x: x["date"])
        return trades
    except Exception:
        return None


def split_date_windows(all_dates, n_windows=5):
    """按日期切分窗口，训练窗口逐步扩大（anchored）。"""
    all_dates = sorted(all_dates)
    n = len(all_dates)
    if n < 100:
        return []

    test_size = n // (n_windows + 2)
    train_min = n // 3  # 最小训练窗口

    windows = []
    for i in range(n_windows):
        test_start_idx = train_min + i * test_size
        test_end_idx = test_start_idx + test_size
        if test_end_idx >= n:
            break
        train_end_date = all_dates[test_start_idx]
        test_start_date = all_dates[test_start_idx]
        test_end_date = all_dates[test_end_idx]
        windows.append((all_dates[0], train_end_date, test_start_date, test_end_date))

    return windows


def compute_optimal_weights(symbol_trades, train_end_date, max_single=0.10, max_sector=0.35):
    """基于训练期数据计算凯利权重（带约束）。"""
    sym_metrics = {}
    for sym, trades in symbol_trades.items():
        train_trades = [t["R_adj"] for t in trades if t["date"] < train_end_date]
        if len(train_trades) < 8:
            continue

        expR = float(np.mean(train_trades))
        stdR = float(np.std(train_trades)) if len(train_trades) > 1 else 1.0

        if expR <= 0 or stdR <= 0:
            continue

        # 凯利分数 = expR / 方差（简化版，分数越高权重越大）
        kelly = expR / (stdR**2)
        sym_metrics[sym] = {"expR": expR, "stdR": stdR, "kelly": max(0, kelly)}

    if not sym_metrics:
        syms = list(symbol_trades.keys())
        return {s: 1.0 / len(syms) for s in syms}

    # 归一化
    total_kelly = sum(m["kelly"] for m in sym_metrics.values())
    weights = {s: m["kelly"] / total_kelly for s, m in sym_metrics.items()}

    # 单品种上限
    for _ in range(5):  # 迭代几次确保收敛
        adjusted = False
        for sym in list(weights.keys()):
            if weights[sym] > max_single:
                excess = weights[sym] - max_single
                weights[sym] = max_single
                others = [s for s in weights if s != sym and weights[s] < max_single]
                if others:
                    total_other = sum(weights[s] for s in others)
                    if total_other > 0:
                        for s in others:
                            weights[s] += excess * weights[s] / total_other
                adjusted = True
        if not adjusted:
            break

    # 板块上限
    sectors = {}
    for sym, w in weights.items():
        g = symbols_group(sym)
        sectors.setdefault(g, []).append(sym)

    for _ in range(5):
        adjusted = False
        for sec, sec_syms in sectors.items():
            sec_weight = sum(weights[s] for s in sec_syms)
            if sec_weight > max_sector:
                scale = max_sector / sec_weight
                moved = sec_weight - max_sector
                for s in sec_syms:
                    weights[s] *= scale
                # 分配给其他板块
                other_syms = [s for s in weights if s not in sec_syms]
                if other_syms:
                    other_total = sum(weights[s] for s in other_syms)
                    if other_total > 0:
                        for s in other_syms:
                            weights[s] += moved * weights[s] / other_total
                adjusted = True
        if not adjusted:
            break

    # 重新归一化
    total = sum(weights.values())
    if total > 0:
        weights = {s: w / total for s, w in weights.items()}

    return weights


def build_portfolio_series(symbol_trades, weights):
    """构建组合日度收益序列。

    返回: pd.Series (date -> daily_R)，按日期排序
    """
    daily_R = {}
    for sym, trades in symbol_trades.items():
        w = weights.get(sym, 0)
        if w <= 0:
            continue
        for t in trades:
            d = t["date"]
            if hasattr(d, "date"):
                d = pd.Timestamp(d)
            else:
                d = pd.Timestamp(str(d))
            daily_R[d] = daily_R.get(d, 0) + t["R_adj"] * w

    if not daily_R:
        return pd.Series(dtype=float)

    s = pd.Series(daily_R).sort_index()
    return s


def compute_metrics(daily_R, start_date, end_date):
    """计算时间窗口内的指标。"""
    mask = (daily_R.index >= start_date) & (daily_R.index < end_date)
    window_R = daily_R[mask]

    if len(window_R) < 3:
        return None

    cumulative = window_R.cumsum()
    total_R = float(cumulative.iloc[-1])

    running_max = cumulative.cummax()
    drawdowns = running_max - cumulative
    max_dd = float(drawdowns.max())

    n_trades = len(window_R)
    expR = float(window_R.mean())
    win_rate = float((window_R > 0).mean())

    calmar = total_R / max_dd if max_dd > 0.001 else float("inf")

    return {
        "total_R": round(total_R, 2),
        "expR": round(expR, 4),
        "win_rate": round(win_rate, 4),
        "max_dd": round(max_dd, 3),
        "n_trades": n_trades,
        "calmar": round(calmar, 2),
    }


def main():
    print("=" * 70)
    print("Walk-Forward 滚动验证")
    print("=" * 70)

    # 基线配置（组合优化关闭）
    base_cfg = copy.deepcopy(DEFAULT_CONFIG)
    base_cfg["portfolio"]["enabled"] = False

    # 活跃品种
    active_syms = list(base_cfg["per_symbol_risk"].keys())
    print(f"\n[1/4] 加载 {len(active_syms)} 个品种交易数据 ...")

    symbol_trades = {}
    for i, sym in enumerate(active_syms):
        trades = get_symbol_trades(sym, base_cfg)
        if trades and len(trades) >= 15:
            symbol_trades[sym] = trades
        print(f"  [{i + 1}/{len(active_syms)}] {sym:>5}: {len(trades) if trades else 0} 笔", end="\r", flush=True)
    print()
    print(f"  有效品种: {len(symbol_trades)} 个")

    if len(symbol_trades) < 5:
        print("  ⚠️  有效品种太少")
        return

    # 收集所有日期
    all_dates = set()
    for sym, trades in symbol_trades.items():
        for t in trades:
            d = t["date"]
            if hasattr(d, "date"):
                d = pd.Timestamp(d)
            else:
                d = pd.Timestamp(str(d))
            all_dates.add(d)
    all_dates = sorted(all_dates)
    print(f"  时间范围: {all_dates[0].date()} ~ {all_dates[-1].date()}")
    print(f"  交易日数: {len(all_dates)} 天")

    # 切分窗口
    n_windows = 5
    windows = split_date_windows(all_dates, n_windows)
    print(f"\n[2/4] 切分 {len(windows)} 个滚动窗口 ...")
    for i, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
        print(f"  窗口 {i + 1}: 训练 {tr_s.date()}~{tr_e.date()} → 测试 {te_s.date()}~{te_e.date()}")

    # 逐窗口对比
    print("\n[3/4] 逐窗口对比（等权 vs 凯利加权）...")
    print(
        f"\n  {'窗口':>4}  {'等权R':>8} {'凯利R':>8} {'超额':>7} "
        f"{'等权DD':>7} {'凯利DD':>7} {'等权Calmar':>10} {'凯利Calmar':>10} {'胜?':>4}"
    )
    print(f"  {'-' * 80}")

    results = []
    wins = 0
    n_syms = len(symbol_trades)
    equal_weights = {s: 1.0 / n_syms for s in symbol_trades}

    for i, (tr_start, tr_end, te_start, te_end) in enumerate(windows):
        # 基于训练期计算凯利权重
        kelly_weights = compute_optimal_weights(symbol_trades, tr_end, max_single=0.10, max_sector=0.35)

        # 构建日度收益序列
        eq_daily = build_portfolio_series(symbol_trades, equal_weights)
        kw_daily = build_portfolio_series(symbol_trades, kelly_weights)

        # 计算测试期指标
        eq_m = compute_metrics(eq_daily, te_start, te_end)
        kw_m = compute_metrics(kw_daily, te_start, te_end)

        if eq_m and kw_m:
            excess = kw_m["total_R"] - eq_m["total_R"]
            win = "✅" if excess > 0 else "❌"
            if excess > 0:
                wins += 1

            print(
                f"  {i + 1:>4}  {eq_m['total_R']:>+8.2f} {kw_m['total_R']:>+8.2f} "
                f"{excess:>+7.2f} {eq_m['max_dd']:>7.3f} {kw_m['max_dd']:>7.3f} "
                f"{eq_m['calmar']:>10.2f} {kw_m['calmar']:>10.2f}  {win}"
            )

            results.append(
                {
                    "window": i + 1,
                    "train_end": str(tr_end.date()),
                    "test_start": str(te_start.date()),
                    "test_end": str(te_end.date()),
                    "equal": eq_m,
                    "kelly": kw_m,
                    "excess_R": round(excess, 2),
                    "n_weighted_symbols": len(kelly_weights),
                }
            )
        else:
            print(f"  {i + 1:>4}  数据不足")

    # 汇总
    print("\n[4/4] 汇总 ...")
    print(f"\n{'=' * 70}")
    print("  Walk-Forward 验证结果")
    print(f"{'=' * 70}")

    if results:
        eq_total = sum(r["equal"]["total_R"] for r in results)
        kw_total = sum(r["kelly"]["total_R"] for r in results)
        win_rate = wins / len(results) * 100

        eq_dds = [r["equal"]["max_dd"] for r in results]
        kw_dds = [r["kelly"]["max_dd"] for r in results]

        eq_calmars = [r["equal"]["calmar"] for r in results if r["equal"]["calmar"] != float("inf")]
        kw_calmars = [r["kelly"]["calmar"] for r in results if r["kelly"]["calmar"] != float("inf")]

        print("\n  测试期总收益:")
        print(f"    等权基准: {eq_total:+.2f} R")
        print(f"    凯利优化: {kw_total:+.2f} R")
        if eq_total != 0:
            pct = (kw_total - eq_total) / abs(eq_total) * 100
            print(f"    超额收益: {kw_total - eq_total:+.2f} R ({pct:+.1f}%)")

        print(f"\n  窗口胜率: {wins}/{len(results)} ({win_rate:.0f}%)")

        print("\n  平均最大回撤:")
        print(f"    等权基准: {np.mean(eq_dds):.3f} R")
        print(f"    凯利优化: {np.mean(kw_dds):.3f} R")

        if eq_calmars and kw_calmars:
            print("\n  平均 Calmar:")
            print(f"    等权基准: {np.mean(eq_calmars):.2f}")
            print(f"    凯利优化: {np.mean(kw_calmars):.2f}")

        # 稳定性评级
        if win_rate >= 80 and kw_total > eq_total:
            grade = "A+ 非常稳定"
        elif win_rate >= 60 and kw_total > eq_total:
            grade = "A 稳定"
        elif win_rate >= 50 and kw_total > eq_total:
            grade = "B 基本有效"
        elif kw_total > eq_total:
            grade = "C 轻微正向"
        else:
            grade = "D 未跑赢基准"

        print(f"\n  🏆 稳定性评级: {grade}")

    # 保存结果
    os.makedirs("logs", exist_ok=True)
    output = {
        "date": datetime.now().isoformat(),
        "n_symbols": len(symbol_trades),
        "n_windows": len(results),
        "win_rate": wins / len(results) if results else 0,
        "equal_total_R": sum(r["equal"]["total_R"] for r in results) if results else 0,
        "kelly_total_R": sum(r["kelly"]["total_R"] for r in results) if results else 0,
        "windows": results,
    }
    with open("logs/walk_forward_validation.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  结果已保存 → logs/walk_forward_validation.json")


if __name__ == "__main__":
    main()
