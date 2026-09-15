"""
SR 过滤方案对比回测
对比三种模式：
  1. 无 SR 过滤（baseline）
  2. 旧方案：灰色地带过滤（1.5/3.0%，不分方向）
  3. 新方案：逆向位危险区过滤（0.3/1.0%，方向感知）

用法: python3 sr_filter_backtest_compare.py --symbols J,eb,SH,cu,al,zn,sp,ag,au,rb
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np

import sr_analyzer as sra
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    pipeline,
)


def backtest_with_sr_filter(symbol, cfg=DEFAULT_CONFIG, min_bars=60, filter_mode="none", cooldown=5):
    """逐 bar 回测，可选 SR 过滤模式。

    filter_mode:
      - 'none': 无 SR 过滤（baseline）
      - 'old': 旧灰色地带过滤（不分方向，1.5~3.0%）
      - 'hostile': 逆向位危险区过滤（方向感知，0.3~1.0%）
    """
    df = load_daily(symbol)
    if df is None or len(df) < min_bars + 50:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}

    sp = cfg["contract_specs"].get(symbol, {"multiplier": 10, "fee": 1.0})
    mv, fee = sp["multiplier"], sp["fee"]

    trades = []
    n = len(df)
    i = min_bars
    last_trade_i = -999
    filtered = 0  # 被 SR 过滤掉的信号数

    while i < n - 1:
        if i - last_trade_i < cooldown:
            i += 1
            continue

        df_seg = df.iloc[: i + 1]
        pipe = pipeline(symbol, df_seg, cfg=cfg)

        T_D = pipe["T_D"]
        dir_T = np.sign(T_D)
        if dir_T == 0:
            i += 1
            continue

        # 基准 T 阈值
        sym_th = cfg.get("thresholds_by_symbol", {}).get(symbol, {})
        if "T_thresh" in sym_th:
            T_thresh_eff = float(sym_th["T_thresh"])
        else:
            group = SYMBOLS.get(symbol, {}).get("group", "化工")
            T_thresh_eff = float(cfg["thresholds"][group]["T_thresh"])

        regime = pipe["regime"]
        regime_coef = cfg.get("regime_coef", {}).get(regime, {}).get("T", 1.0)
        T_thresh_eff = T_thresh_eff * regime_coef

        # --- SR 过滤 ---
        if filter_mode != "none":
            current_price = float(df.iloc[i + 1]["open"])
            sr_result = sra.analyze(df_seg, current_price=current_price)

            if filter_mode == "old":
                # 旧方案：不分方向，最近关键位距离
                ns = sr_result.get("nearest_support")
                nr = sr_result.get("nearest_resistance")
                sup_dist = ns["distance_pct"] if ns else 99.0
                res_dist = nr["distance_pct"] if nr else 99.0
                nearest = min(sup_dist, res_dist) / 100.0
                # 灰色地带 1.5~3.0% → 提高阈值 ×1.25
                if 0.015 <= nearest < 0.030:
                    T_thresh_eff = T_thresh_eff * 1.25

            elif filter_mode == "hostile":
                # 新方案：逆向位危险区
                ns = sr_result.get("nearest_support")
                nr = sr_result.get("nearest_resistance")
                sup_dist = ns["distance_pct"] if ns else 99.0
                res_dist = nr["distance_pct"] if nr else 99.0

                # 逆向位：做多→压力位，做空→支撑位
                if dir_T > 0:
                    hostile_dist = res_dist
                else:
                    hostile_dist = sup_dist

                hostile_frac = hostile_dist / 100.0
                # 危险区 0.3~1.0% → 提高阈值 ×1.30
                if 0.003 <= hostile_frac < 0.010:
                    T_thresh_eff = T_thresh_eff * 1.30

        # T 阈值判断
        if abs(T_D) < T_thresh_eff:
            filtered += 1
            i += 1
            continue

        # --- 模拟出场 ---
        entry = float(df.iloc[i + 1]["open"])
        atr = pipe["atr"]
        sd = atr * cfg["risk_gate"]["stop_atr_mult"]
        if sd <= 0:
            i += 1
            continue

        rr = cfg["risk_gate"]["rr_ratio"]
        if dir_T > 0:
            stop = entry - sd
            t2 = entry + rr * sd
        else:
            stop = entry + sd
            t2 = entry - rr * sd

        # 日线出场模拟（简化版：逐 bar 检查高低点）
        exit_price = None
        reason = ""
        exit_j = 0
        for j in range(i + 1, min(i + 20, n)):
            hi = float(df["high"].iloc[j])
            lo = float(df["low"].iloc[j])
            if dir_T > 0:
                if lo <= stop:
                    exit_price = stop
                    reason = "止损"
                    exit_j = j - i
                    break
                if hi >= t2:
                    exit_price = t2
                    reason = "止盈"
                    exit_j = j - i
                    break
            else:
                if hi >= stop:
                    exit_price = stop
                    reason = "止损"
                    exit_j = j - i
                    break
                if lo <= t2:
                    exit_price = t2
                    reason = "止盈"
                    exit_j = j - i
                    break

        if exit_price is None:
            exit_price = float(df["close"].iloc[min(i + 19, n - 1)])
            reason = "期末平"
            exit_j = min(19, n - 1 - i)

        # 计算 R
        R = dir_T * (exit_price - entry) / sd
        slip = 0.0005 * entry
        slip_R = 2 * slip / sd
        fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
        R_adj = R - slip_R - fee_R

        trades.append(
            {
                "R_adj": R_adj,
                "regime": regime,
                "reason": reason,
            }
        )

        last_trade_i = i
        i = i + exit_j + 1

    if not trades:
        return {"symbol": symbol, "trades": 0, "filtered": filtered, "note": "无信号"}

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    expR = float(np.mean(Rs))
    win_rate = len(wins) / len(Rs)

    return {
        "symbol": symbol,
        "trades": len(trades),
        "expR": round(expR, 4),
        "win_rate": round(win_rate, 4),
        "filtered": filtered,
        "Rs": Rs,
    }


def main():
    parser = argparse.ArgumentParser(description="SR 过滤方案对比回测")
    parser.add_argument("--symbols", type=str, default="J,eb,SH,cu,al,zn,sp,ag,au,rb")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 80)
    print(f"SR 过滤方案对比回测 · {len(symbols)} 个品种")
    print("  方案 A: 无 SR 过滤 (baseline)")
    print("  方案 B: 旧灰色地带过滤 (1.5~3.0%, 不分方向, 阈值×1.25)")
    print("  方案 C: 逆向位危险区过滤 (0.3~1.0%, 方向感知, 阈值×1.30)")
    print("=" * 80)

    modes = [
        ("none", "无过滤(A)"),
        ("old", "旧灰色(B)"),
        ("hostile", "逆向危险(C)"),
    ]

    results = {name: [] for _, name in modes}

    for idx, sym in enumerate(symbols):
        row = f"\n[{idx + 1}/{len(symbols)}] {sym}: "
        print(row, end="", flush=True)

        for mode, name in modes:
            r = backtest_with_sr_filter(sym, filter_mode=mode)
            results[name].append(r)
            if r["trades"] > 0:
                print(f"  {name}:{r['trades']}笔 expR={r['expR']:.3f}", end="", flush=True)
            else:
                print(f"  {name}:无", end="", flush=True)

    # 汇总
    print(f"\n\n{'=' * 80}")
    print("汇总对比")
    print(f"{'=' * 80}")

    # 全市场合并统计
    all_Rs = {}
    for _, name in modes:
        all_R = []
        for r in results[name]:
            if r.get("Rs"):
                all_R.extend(r["Rs"])
        all_Rs[name] = all_R

    print(f"\n{'方案':<14} {'总笔数':>6} {'expR':>8} {'胜率':>8} {'vs基准':>10}")
    print("-" * 52)

    baseline_expR = None
    for mode, name in modes:
        Rs = all_Rs[name]
        n = len(Rs)
        if n == 0:
            continue
        expR = sum(Rs) / n
        wr = sum(1 for r in Rs if r > 0) / n
        if baseline_expR is None:
            baseline_expR = expR
            diff_str = "—"
        else:
            diff = expR - baseline_expR
            pct = diff / abs(baseline_expR) * 100 if baseline_expR != 0 else 0
            diff_str = f"{diff:+.4f} ({pct:+.1f}%)"
        print(f"{name:<14} {n:>6} {expR:>8.4f} {wr * 100:>7.1f}% {diff_str:>10}")

    # 逐品种对比表
    print(f"\n{'=' * 80}")
    print("逐品种对比")
    print(f"{'=' * 80}")
    print(f"{'品种':<6} {'无过滤 expR':>12} {'旧灰色 expR':>12} {'逆向危险 expR':>14} {'逆向提升':>10}")
    print("-" * 62)

    for idx, sym in enumerate(symbols):
        r_none = results["无过滤(A)"][idx]
        r_old = results["旧灰色(B)"][idx]
        r_new = results["逆向危险(C)"][idx]

        e_none = r_none.get("expR", 0) if r_none["trades"] > 0 else float("nan")
        e_old = r_old.get("expR", 0) if r_old["trades"] > 0 else float("nan")
        e_new = r_new.get("expR", 0) if r_new["trades"] > 0 else float("nan")

        # 相对无过滤的提升
        if not np.isnan(e_none) and not np.isnan(e_new) and e_none != 0:
            lift = (e_new - e_none) / abs(e_none) * 100
            lift_str = f"{lift:+.1f}%"
        else:
            lift_str = "—"

        n_none = r_none["trades"]
        n_new = r_new["trades"]
        print(
            f"{sym:<6} {e_none:>8.4f}({n_none:>3}) {e_old:>8.4f}({r_old['trades']:>3}) "
            f"{e_new:>8.4f}({n_new:>3}) {lift_str:>10}"
        )

    # 胜率对比
    print(f"\n{'=' * 80}")
    print("胜率对比")
    print(f"{'=' * 80}")
    print(f"{'品种':<6} {'无过滤':>8} {'旧灰色':>8} {'逆向危险':>10} {'变化':>8}")
    print("-" * 48)

    for idx, sym in enumerate(symbols):
        r_none = results["无过滤(A)"][idx]
        r_old = results["旧灰色(B)"][idx]
        r_new = results["逆向危险(C)"][idx]

        w_none = r_none.get("win_rate", 0) * 100 if r_none["trades"] > 0 else float("nan")
        w_old = r_old.get("win_rate", 0) * 100 if r_old["trades"] > 0 else float("nan")
        w_new = r_new.get("win_rate", 0) * 100 if r_new["trades"] > 0 else float("nan")

        if not np.isnan(w_none) and not np.isnan(w_new):
            diff = w_new - w_none
            diff_str = f"{diff:+.1f}%"
        else:
            diff_str = "—"

        print(f"{sym:<6} {w_none:>7.1f}% {w_old:>7.1f}% {w_new:>9.1f}% {diff_str:>8}")

    # 结论
    print(f"\n{'=' * 80}")
    print("结论")
    print(f"{'=' * 80}")

    new_expR = sum(all_Rs["逆向危险(C)"]) / len(all_Rs["逆向危险(C)"]) if all_Rs["逆向危险(C)"] else 0
    old_expR = sum(all_Rs["旧灰色(B)"]) / len(all_Rs["旧灰色(B)"]) if all_Rs["旧灰色(B)"] else 0

    print("\n  整体 expR:")
    print(f"    无过滤 (基准): {baseline_expR:.4f}")
    print(
        f"    旧灰色地带:   {old_expR:.4f} ({(old_expR - baseline_expR) / abs(baseline_expR) * 100 if baseline_expR else 0:+.1f}%)"
    )
    print(
        f"    逆向位危险区: {new_expR:.4f} ({(new_expR - baseline_expR) / abs(baseline_expR) * 100 if baseline_expR else 0:+.1f}%)"
    )

    n_better = sum(
        1
        for idx in range(len(symbols))
        if results["逆向危险(C)"][idx]["trades"] > 0
        and results["无过滤(A)"][idx]["trades"] > 0
        and results["逆向危险(C)"][idx]["expR"] > results["无过滤(A)"][idx]["expR"]
    )
    n_worse = sum(
        1
        for idx in range(len(symbols))
        if results["逆向危险(C)"][idx]["trades"] > 0
        and results["无过滤(A)"][idx]["trades"] > 0
        and results["逆向危险(C)"][idx]["expR"] < results["无过滤(A)"][idx]["expR"]
    )
    print("\n  逐品种胜负（逆向危险 vs 无过滤）:")
    print(f"    提升: {n_better} 个品种")
    print(f"    下降: {n_worse} 个品种")
    print(f"    持平/无数据: {len(symbols) - n_better - n_worse} 个品种")

    if new_expR > baseline_expR:
        print("\n  ✓ 逆向位危险区过滤整体提升 expR，方案有效")
    else:
        print("\n  ⚠ 逆向位危险区过滤整体 expR 反而下降，需要调整参数或逻辑")


if __name__ == "__main__":
    main()
