"""
策略质量诊断脚本 v2
- 品种全景（全量数据）
- F/T/C 三维度消融（边际贡献）
- 退出原因分布
- Regime 表现拆分
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


def calc_extended_stats(result):
    """从 trades_detail 计算 total_R, max_dd, profit_factor 等。"""
    trades = result.get("trades_detail", [])
    if not trades:
        return {"total_R": 0, "max_dd": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0}

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r < 0]

    # 累计权益曲线 + 最大回撤
    cum = np.cumsum(Rs)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0

    total_R = float(np.sum(Rs))
    avg_win = float(np.mean(wins)) if wins else 0
    avg_loss = float(np.mean(losses)) if losses else 0
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    return {
        "total_R": round(total_R, 3),
        "max_dd": round(max_dd, 4),
        "profit_factor": round(pf, 3) if pf != float("inf") else 99.9,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "win_loss_ratio": round(abs(avg_win / avg_loss), 3) if avg_loss != 0 else 99.9,
    }


def dimension_ablation(symbol, df):
    """F/T/C 三维度边际贡献 = 全量 - 消融后。
    正确做法：消融某维度时，将其权重置 0，其他维度按比例缩放保持总和=1。"""
    full = walk_forward_backtest(symbol, cfg=DEFAULT_CONFIG, df_in=df, window=200)
    fe = full.get("expR", 0)
    full_ext = calc_extended_stats(full)

    # 默认权重: T=0.6, F=0.25, C=0.15
    # 消融 F: T + C 按比例缩放 → T=0.6/0.75=0.8, C=0.15/0.75=0.2
    cfg_noF = copy.deepcopy(DEFAULT_CONFIG)
    cfg_noF["combine_weights"] = {"T": 0.8, "F": 0.0, "C": 0.2}
    r_noF = walk_forward_backtest(symbol, cfg=cfg_noF, df_in=df, window=200)

    # 消融 T: F + C 按比例缩放 → F=0.25/0.4=0.625, C=0.15/0.4=0.375
    cfg_noT = copy.deepcopy(DEFAULT_CONFIG)
    cfg_noT["combine_weights"] = {"T": 0.0, "F": 0.625, "C": 0.375}
    r_noT = walk_forward_backtest(symbol, cfg=cfg_noT, df_in=df, window=200)

    # 消融 C: T + F 按比例缩放 → T=0.6/0.85≈0.706, F=0.25/0.85≈0.294
    cfg_noC = copy.deepcopy(DEFAULT_CONFIG)
    cfg_noC["combine_weights"] = {"T": 0.706, "F": 0.294, "C": 0.0}
    r_noC = walk_forward_backtest(symbol, cfg=cfg_noC, df_in=df, window=200)

    # Regime 拆分（全量）
    by_regime = full.get("by_regime", {})
    exit_reasons = full.get("exit_reasons", {})

    return {
        "full_expR": round(fe, 4),
        "full_trades": full.get("trades", 0),
        "full_win_rate": round(full.get("win_rate", 0), 3),
        **full_ext,
        "contrib_F": round(fe - r_noF.get("expR", 0), 4),
        "contrib_T": round(fe - r_noT.get("expR", 0), 4),
        "contrib_C": round(fe - r_noC.get("expR", 0), 4),
        "noF_expR": round(r_noF.get("expR", 0), 4),
        "noT_expR": round(r_noT.get("expR", 0), 4),
        "noC_expR": round(r_noC.get("expR", 0), 4),
        "by_regime": by_regime,
        "exit_reasons": exit_reasons,
    }


def all_symbols_overview():
    """所有有数据品种的回测表现概览（全量数据）。"""
    results = {}
    all_syms = sorted(SYMBOLS.keys())

    for i, sym in enumerate(all_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 260:
                results[sym] = {"note": f"数据不足({len(df) if df is not None else 0}根)"}
                continue
            r = walk_forward_backtest(sym, cfg=DEFAULT_CONFIG, df_in=df, window=200)
            ext = calc_extended_stats(r)
            results[sym] = {
                "expR": round(r.get("expR", 0), 4),
                "win_rate": round(r.get("win_rate", 0), 3),
                "trades": r.get("trades", 0),
                **ext,
                "total_bars": len(df),
            }
        except Exception as e:
            results[sym] = {"error": str(e)[:80]}
        status = f"expR={results[sym].get('expR', '?')}" if "expR" in results[sym] else results[sym].get("note", "?")
        print(f"  [{i + 1}/{len(all_syms)}] {sym}: {status}", end="\r", flush=True)
    print()
    return results


def main():
    print("=" * 80)
    print("策略质量诊断 v2")
    print("=" * 80)

    # 品种全景
    print("\n[1/2] 品种全景扫描（全量数据）...")
    overview = all_symbols_overview()

    # 统计
    valid = {k: v for k, v in overview.items() if "expR" in v and v.get("trades", 0) >= 20}
    pos = {k: v for k, v in valid.items() if v["expR"] > 0}
    neg = {k: v for k, v in valid.items() if v["expR"] <= 0}

    print("\n[2/2] 统计汇总")
    print(f"  全部品种: {len(overview)} 个")
    print(f"  有效品种（≥20笔）: {len(valid)} 个")
    print(f"  正期望: {len(pos)} 个  |  负期望: {len(neg)} 个")

    if pos:
        avg_expr = sum(v["expR"] for v in pos.values()) / len(pos)
        avg_win = sum(v["win_rate"] for v in pos.values()) / len(pos)
        avg_pf = sum(v["profit_factor"] for v in pos.values() if v["profit_factor"] < 99) / max(
            1, sum(1 for v in pos.values() if v["profit_factor"] < 99)
        )
        print(f"  正期望平均: expR={avg_expr:.3f}  胜率={avg_win * 100:.1f}%  PF={avg_pf:.2f}")

    if neg:
        avg_expr = sum(v["expR"] for v in neg.values()) / len(neg)
        avg_win = sum(v["win_rate"] for v in neg.values()) / len(neg)
        print(f"  负期望平均: expR={avg_expr:.3f}  胜率={avg_win * 100:.1f}%")

    # Top 15
    print("\n  Top 15（按 expR，≥20笔）:")
    sorted_pos = sorted(pos.items(), key=lambda x: x[1]["expR"], reverse=True)
    print(
        f"    {'品种':>5}  {'expR':>7}  {'胜率':>6}  {'笔数':>4}  {'总R':>7}  {'PF':>5}  {'盈亏比':>6}  {'最大DD':>7}"
    )
    for sym, v in sorted_pos[:15]:
        print(
            f"    {sym:>5}  {v['expR']:>7.3f}  {v['win_rate'] * 100:>5.1f}%  {v['trades']:>4}  {v['total_R']:>7.2f}  {v['profit_factor']:>5.2f}  {v['win_loss_ratio']:>6.2f}  {v['max_dd']:>7.3f}"
        )

    # Bottom 15
    print("\n  Bottom 15（按 expR，≥20笔）:")
    sorted_neg = sorted(valid.items(), key=lambda x: x[1]["expR"])
    print(
        f"    {'品种':>5}  {'expR':>7}  {'胜率':>6}  {'笔数':>4}  {'总R':>7}  {'PF':>5}  {'盈亏比':>6}  {'最大DD':>7}"
    )
    for sym, v in sorted_neg[:15]:
        print(
            f"    {sym:>5}  {v['expR']:>7.3f}  {v['win_rate'] * 100:>5.1f}%  {v['trades']:>4}  {v['total_R']:>7.2f}  {v['profit_factor']:>5.2f}  {v['win_loss_ratio']:>6.2f}  {v['max_dd']:>7.3f}"
        )

    # 深度诊断（选 3 个代表：强 / 中 / 弱）
    print("\n  深度诊断 - 维度边际贡献（F/T/C）:")
    deep_syms = []
    if sorted_pos:
        deep_syms.append(sorted_pos[0][0])  # 最强
    mid = len(sorted_pos) // 2
    if len(sorted_pos) > mid + 1:
        deep_syms.append(sorted_pos[mid][0])  # 中位
    if sorted_neg:
        deep_syms.append(sorted_neg[0][0])  # 最弱

    deep_results = {}
    for sym in deep_syms:
        df = load_daily(sym)
        if df is None:
            continue
        print(f"    {sym}...", end="\r", flush=True)
        dim = dimension_ablation(sym, df)
        deep_results[sym] = dim
        print(
            f"    {sym}: full={dim['full_expR']:.3f}  ΔF={dim['contrib_F']:+.3f}  ΔT={dim['contrib_T']:+.3f}  ΔC={dim['contrib_C']:+.3f}  笔={dim['full_trades']}"
        )

    # 深度诊断 - 退出原因
    print("\n  深度诊断 - 退出原因分布:")
    for sym, d in deep_results.items():
        reasons = d.get("exit_reasons", {})
        total = sum(reasons.values())
        print(
            f"    {sym}: "
            + ", ".join(f"{k}={v}({v / total * 100:.0f}%)" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        )

    # 深度诊断 - Regime 拆分
    print("\n  深度诊断 - Regime 表现:")
    for sym, d in deep_results.items():
        regimes = d.get("by_regime", {})
        print(f"    {sym}: " + ", ".join(f"{k}={v:.3f}" for k, v in sorted(regimes.items(), key=lambda x: -x[1])))

    # 保存结果
    out = {
        "overview": overview,
        "deep_diagnosis": deep_results,
        "stats": {
            "total_valid": len(valid),
            "positive": len(pos),
            "negative": len(neg),
        },
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/strategy_quality_diagnosis.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n  详细结果 → logs/strategy_quality_diagnosis.json")


if __name__ == "__main__":
    main()
