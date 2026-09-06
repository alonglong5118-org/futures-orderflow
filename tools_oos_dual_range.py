#!/usr/bin/env python3
"""双差值波动估计（Dual Thrust range）OOS 验证，2026-08-31。

假设来源：抖音「海量量化」Dual Thrust 视频——Range = max(HH−LC, HC−LL)
对跳空方向鲁棒（HH−LC 含向上跳空、HC−LL 含向下跳空，取大者不偏不倚）。
中国期货夜盘/长假跳空频繁，ATR 的跳空项 14 根后滚出窗口，双差值则整窗持续。

估计量设计：DR(N)/√N —— 随机游走理论下 E[range_N]/E[TR] = √N，
√N 缩放后与 ATR(N) 同量级（实测 8 品种中位数比值 0.93–1.01，零拟合参数）。

注入方式：monkey-patch four_dim_strategy._atr_array，零生产代码改动。
单点替换覆盖全链路三个 ATR 消费者：Layer0 行情分类(atr_r)、risk_gate 止损、
exit_plan 止盈/尾仓。

验证纪律（同 tools_oos_batch3）：5 折走步法 OOS，逐折配对对比。
品种级达标：均值改善>0 且折胜率>=60%；组合级采纳线：全样本 expR 无恶化。

面板 12 品种：已配 cluster_w（RM/m/jd）+ 金字塔白名单（FG/ru/l→取ru）+
覆盖恶化反例（cu）+ 第三批候选（hc/ag/y/p/MA）+ 农产品扩展。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import four_dim_strategy as fds
from four_dim_strategy import DEFAULT_CONFIG, load_daily

PANEL = ["RM", "m", "jd", "y", "p", "hc", "i", "cu", "ag", "FG", "MA", "ru"]
N_FOLDS = 5
WINDOW = 14


def dual_range_atr_array(high, low, close, window):
    """DR(N)/√N：滚动双差值，随机游走尺度归一到 ATR 量级。"""
    hh = pd.Series(high).rolling(window).max().values
    ll = pd.Series(low).rolling(window).min().values
    hc = pd.Series(close).rolling(window).max().values
    lc = pd.Series(close).rolling(window).min().values
    dr = np.maximum(hh - lc, hc - ll)
    return dr / np.sqrt(window)


def oos_walkforward(symbol, n_folds=N_FOLDS):
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None
    total = len(df)
    fold_size = total // n_folds
    results = []
    for fold in range(n_folds):
        start = fold * fold_size
        end = start + fold_size if fold < n_folds - 1 else total
        df_fold = df.iloc[start:end]
        if len(df_fold) < 80:
            continue
        r = fds.walk_forward_backtest(symbol, cfg=DEFAULT_CONFIG, df_in=df_fold)
        if r.get("trades", 0) > 0:
            results.append(r)
    return results


def agg(results):
    """折级 expR 列表 + 交易加权组合 expR。"""
    exps = [r["expR"] for r in results]
    trades = [r["trades"] for r in results]
    wrs = [r["win_rate"] for r in results]
    total_trades = sum(trades)
    port = sum(e * t for e, t in zip(exps, trades)) / total_trades if total_trades else np.nan
    return exps, float(np.mean(exps)), port, total_trades, float(np.mean(wrs))


def main():
    print(f"双差值波动估计 DR({WINDOW})/√{WINDOW} vs ATR({WINDOW}) · {N_FOLDS} 折走步 OOS")
    print(f"{'=' * 78}")
    print()

    # ── 第一轮：基线（ATR，未打补丁）──
    base_all = {}
    for sym in PANEL:
        base_all[sym] = oos_walkforward(sym)

    # ── 第二轮：补丁（DR/√N）──
    orig = fds._atr_array
    fds._atr_array = dual_range_atr_array
    try:
        test_all = {}
        for sym in PANEL:
            test_all[sym] = oos_walkforward(sym)
    finally:
        fds._atr_array = orig

    # ── 品种级报告 ──
    print(f"{'品种':<6}{'基线折expR':<34}{'均值':>8}  {'测试折expR':<34}{'均值':>8}  {'改善':>8} {'折胜率':>7} {'笔数':>9} {'达标':>4}")
    print("-" * 160)
    rows = []
    for sym in PANEL:
        b, t = base_all[sym], test_all[sym]
        if not b or not t or len(b) != len(t):
            print(f"{sym:<6}数据不足，跳过")
            continue
        be, bm, bp, bt, bw = agg(b)
        te, tm, tp, tt, tw = agg(t)
        wins = sum(1 for x, y in zip(be, te) if y > x)
        n = len(be)
        diff = tm - bm
        ok = diff > 0 and wins / n >= 0.6
        rows.append({
            "symbol": sym, "base_mean": bm, "test_mean": tm, "diff": diff,
            "wins": wins, "n": n, "ok": ok,
            "base_port": bp, "test_port": tp,
            "base_trades": bt, "test_trades": tt,
            "base_wr": bw, "test_wr": tw,
        })
        bs = " / ".join(f"{e:+.3f}" for e in be)
        ts = " / ".join(f"{e:+.3f}" for e in te)
        print(f"{sym:<6}{bs:<34}{bm:>+8.4f}  {ts:<34}{tm:>+8.4f}  {diff:>+8.4f} {wins}/{n}{'':<2} {bt:>4}→{tt:<4} {'✓' if ok else '✗':>4}")

    # ── 组合级报告 ──
    print()
    print(f"{'=' * 78}")
    base_port_all = sum(r["base_port"] * r["base_trades"] for r in rows) / sum(r["base_trades"] for r in rows)
    test_port_all = sum(r["test_port"] * r["test_trades"] for r in rows) / sum(r["test_trades"] for r in rows)
    base_trades_all = sum(r["base_trades"] for r in rows)
    test_trades_all = sum(r["test_trades"] for r in rows)
    base_wr_all = sum(r["base_wr"] * r["base_trades"] for r in rows) / base_trades_all
    test_wr_all = sum(r["test_wr"] * r["test_trades"] for r in rows) / test_trades_all
    print(f"组合级（交易加权全样本，{len(rows)} 品种）：")
    print(f"  ATR 基线： expR {base_port_all:+.4f} · {base_trades_all} 笔 · 胜率 {base_wr_all:.1%}")
    print(f"  DR/√N：    expR {test_port_all:+.4f} · {test_trades_all} 笔 · 胜率 {test_wr_all:.1%}")
    print(f"  ΔexpR {test_port_all - base_port_all:+.4f} · Δ笔数 {test_trades_all - base_trades_all:+d} · Δ胜率 {test_wr_all - base_wr_all:+.1%}")
    print()
    passed = [r for r in rows if r["ok"]]
    improved = [r for r in rows if r["diff"] > 0]
    print(f"品种级达标（改善>0 且折胜率>=60%）：{len(passed)}/{len(rows)} → {', '.join(r['symbol'] for r in passed) or '无'}")
    print(f"品种级均值改善为正：{len(improved)}/{len(rows)} → {', '.join(r['symbol'] for r in improved) or '无'}")
    print()
    print(f"采纳线（组合级 expR 无恶化）：{'✓ 通过' if test_port_all >= base_port_all else '✗ 恶化'}")
    print(f"参考线（品种级达标过半）：{'✓' if len(passed) * 2 >= len(rows) else '✗'}")


if __name__ == "__main__":
    main()
