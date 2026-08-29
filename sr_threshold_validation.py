"""
SR 位阈值验证 v4 - 方向感知版
- 区分：顺向（做多近支撑/做空近压力） vs 逆向（做多近压力/做空近支撑）
- 按距离分档，分别统计顺向/逆向的 expR
- 评估不同阈值下的过滤效果
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pandas as pd

import sr_analyzer as sra
from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)


def annotate_trades_with_sr(symbol, trades, min_bars=60):
    """给交易加上 SR 详细标注（区分支撑/压力距离）。"""
    df = load_daily(symbol)
    if df is None:
        return []

    annotated = []
    for t in trades:
        entry_date = t.get("entry_date")
        if entry_date is None:
            continue
        entry_ts = pd.Timestamp(entry_date) if isinstance(entry_date, str) else entry_date

        try:
            entry_pos = df.index.get_loc(entry_ts)
        except (KeyError, ValueError):
            continue

        if entry_pos < min_bars:
            continue

        df_hist = df.iloc[:entry_pos]
        if len(df_hist) < 50:
            continue

        current_price = float(df.iloc[entry_pos]["open"])
        sr_result = sra.analyze(df_hist, current_price=current_price)

        ns = sr_result.get("nearest_support")
        nr = sr_result.get("nearest_resistance")
        sup_dist = ns["distance_pct"] if ns else 999.0
        res_dist = nr["distance_pct"] if nr else 999.0

        direction = t["dir"]

        # 顺向距离：做多→支撑位距离，做空→压力位距离
        # 逆向距离：做多→压力位距离，做空→支撑位距离
        if direction > 0:
            friendly_dist = sup_dist  # 支撑（做多的朋友）
            hostile_dist = res_dist  # 压力（做多的敌人）
        else:
            friendly_dist = res_dist  # 压力（做空的朋友）
            hostile_dist = sup_dist  # 支撑（做空的敌人）

        nearest_dist = min(sup_dist, res_dist)
        nearest_type = "support" if sup_dist < res_dist else "resistance"

        annotated.append(
            {
                "symbol": symbol,
                "direction": direction,
                "R_adj": t["R_adj"],
                "sup_dist": sup_dist,
                "res_dist": res_dist,
                "friendly_dist": friendly_dist,
                "hostile_dist": hostile_dist,
                "nearest_dist": nearest_dist,
                "nearest_type": nearest_type,
                "regime": t.get("regime", "?"),
            }
        )

    return annotated


def analyze_by_zone(trades, near_pct=0.8, grey_pct=1.6, mode="nearest"):
    """
    按距离分档统计。
    mode: 'nearest' = 距最近关键位, 'friendly' = 距顺向位, 'hostile' = 距逆向位
    """
    zone_names = [
        f"近位区 (<{near_pct:.1f}%)",
        f"灰色地带 ({near_pct:.1f}%~{grey_pct:.1f}%)",
        f"远位区 (>={grey_pct:.1f}%)",
    ]
    zones = {name: [] for name in zone_names}

    for t in trades:
        if mode == "nearest":
            d = t["nearest_dist"]
        elif mode == "friendly":
            d = t["friendly_dist"]
        else:
            d = t["hostile_dist"]
        R = t["R_adj"]

        if d < near_pct:
            zones[zone_names[0]].append(R)
        elif d < grey_pct:
            zones[zone_names[1]].append(R)
        else:
            zones[zone_names[2]].append(R)

    results = {}
    for zone, Rs in zones.items():
        n = len(Rs)
        if n == 0:
            results[zone] = {"trades": 0, "expR": 0.0, "win_rate": 0.0}
            continue
        wins = sum(1 for r in Rs if r > 0)
        expR = sum(Rs) / n
        results[zone] = {
            "trades": n,
            "expR": round(expR, 4),
            "win_rate": round(wins / n, 4),
        }
    return results


def fine_grained_bins(trades, mode="nearest"):
    """细粒度分档。"""
    bins = [
        (0, 0.3, "0-0.3%"),
        (0.3, 0.5, "0.3-0.5%"),
        (0.5, 0.8, "0.5-0.8%"),
        (0.8, 1.2, "0.8-1.2%"),
        (1.2, 1.6, "1.2-1.6%"),
        (1.6, 2.0, "1.6-2.0%"),
        (2.0, 3.0, "2.0-3.0%"),
        (3.0, 5.0, "3.0-5.0%"),
        (5.0, 999, ">=5.0%"),
    ]
    stats = []
    for lo, hi, label in bins:
        if mode == "nearest":
            Rs = [t["R_adj"] for t in trades if lo <= t["nearest_dist"] < hi]
        elif mode == "friendly":
            Rs = [t["R_adj"] for t in trades if lo <= t["friendly_dist"] < hi]
        else:
            Rs = [t["R_adj"] for t in trades if lo <= t["hostile_dist"] < hi]
        n = len(Rs)
        if n == 0:
            stats.append((label, n, 0.0, 0.0))
            continue
        wins = sum(1 for r in Rs if r > 0)
        expR = sum(Rs) / n
        wr = wins / n
        stats.append((label, n, expR, wr))
    return stats


def main():
    parser = argparse.ArgumentParser(description="SR 阈值验证（方向感知）")
    parser.add_argument("--symbols", type=str, default="J,eb,SH,cu,al,zn,sp,ag,au,rb")
    parser.add_argument("--near", type=float, default=0.8)
    parser.add_argument("--grey", type=float, default=1.6)
    parser.add_argument("--compare-near", type=float, default=1.5)
    parser.add_argument("--compare-grey", type=float, default=3.0)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 70)
    print(f"SR 阈值验证（方向感知）· {len(symbols)} 个品种")
    print(f"  新阈值: 近位 <{args.near}% / 灰色 {args.near}~{args.grey}% / 远位 >={args.grey}%")
    print(f"  旧阈值: 近位 <{args.compare_near}% / 灰色 {args.compare_near}~{args.compare_grey}%")
    print("=" * 70)

    # 收集数据
    all_trades = []
    for idx, sym in enumerate(symbols):
        print(f"\n[{idx + 1}/{len(symbols)}] {sym} ", end="", flush=True)
        bt = walk_forward_backtest(sym, DEFAULT_CONFIG)
        if not bt or bt.get("trades", 0) == 0:
            print("无交易")
            continue
        trades_detail = bt.get("trades_detail", [])
        print(f"{bt['trades']}笔 expR={bt['expR']:.3f}...", end="", flush=True)
        annotated = annotate_trades_with_sr(sym, trades_detail)
        print(f"标注{len(annotated)}笔")
        all_trades.extend(annotated)

    if not all_trades:
        print("\n无数据！")
        return

    n_total = len(all_trades)
    overall_expR = sum(t["R_adj"] for t in all_trades) / n_total
    overall_wr = sum(1 for t in all_trades if t["R_adj"] > 0) / n_total
    print(f"\n{'=' * 70}")
    print(f"总计: {n_total} 笔  expR={overall_expR:.4f}  胜率={overall_wr * 100:.1f}%")

    # ===== 1. 距最近关键位（不分方向）=====
    print(f"\n{'=' * 70}")
    print("【1】距最近关键位（不分支撑/压力，不分方向）")
    print(f"{'=' * 70}")
    _print_three_zones(all_trades, args.near, args.grey, "nearest", args.compare_near, args.compare_grey)

    # ===== 2. 距顺向位（做多看支撑、做空看压力）=====
    print(f"\n{'=' * 70}")
    print("【2】距顺向关键位（做多看支撑、做空看压力）→ 越近越好")
    print(f"{'=' * 70}")
    _print_three_zones(all_trades, args.near, args.grey, "friendly", args.compare_near, args.compare_grey)

    # ===== 3. 距逆向位（做多看压力、做空看支撑）=====
    print(f"\n{'=' * 70}")
    print("【3】距逆向关键位（做多看压力、做空看支撑）→ 越近越差")
    print(f"{'=' * 70}")
    _print_three_zones(all_trades, args.near, args.grey, "hostile", args.compare_near, args.compare_grey)

    # ===== 4. 细粒度：顺向位 + 逆向位 双维度 =====
    print(f"\n{'=' * 70}")
    print("【4】细粒度分析")
    print(f"{'=' * 70}")

    print("\n顺向位距离分布（做多=支撑, 做空=压力）:")
    friendly_bins = fine_grained_bins(all_trades, "friendly")
    _print_bins(friendly_bins)

    print("\n逆向位距离分布（做多=压力, 做空=支撑）:")
    hostile_bins = fine_grained_bins(all_trades, "hostile")
    _print_bins(hostile_bins)

    # ===== 5. 综合结论 =====
    print(f"\n{'=' * 70}")
    print("【5】结论 & 建议")
    print(f"{'=' * 70}")

    # 顺向位分析：最近的顺向位 expR 高吗？
    f_near = f"近位区 (<{args.near}%)"
    f_grey = f"灰色地带 ({args.near}%~{args.grey}%)"
    f_far = f"远位区 (>={args.grey}%)"
    f_zones = analyze_by_zone(all_trades, args.near, args.grey, "friendly")
    print(f"\n  顺向位（新阈值 {args.near}/{args.grey}%）:")
    print(f"    近位(靠顺向位近): expR={f_zones[f_near]['expR']:.4f} ({f_zones[f_near]['trades']}笔)")
    print(f"    灰色地带:        expR={f_zones[f_grey]['expR']:.4f} ({f_zones[f_grey]['trades']}笔)")
    print(f"    远位(靠顺向位远): expR={f_zones[f_far]['expR']:.4f} ({f_zones[f_far]['trades']}笔)")

    if f_zones[f_near]["expR"] > f_zones[f_far]["expR"]:
        diff = f_zones[f_near]["expR"] - f_zones[f_far]["expR"]
        print(f"    ✓ 靠近顺向位 expR 更高（+{diff:.4f}），顺向位有支撑/压力作用")
    else:
        print("    ⚠ 靠近顺向位 expR 反而更低，顺向位作用不明显")

    # 逆向位分析：最近的逆向位 expR 低吗？
    h_zones = analyze_by_zone(all_trades, args.near, args.grey, "hostile")
    print(f"\n  逆向位（新阈值 {args.near}/{args.grey}%）:")
    print(f"    近位(靠逆向位近): expR={h_zones[f_near]['expR']:.4f} ({h_zones[f_near]['trades']}笔)")
    print(f"    灰色地带:        expR={h_zones[f_grey]['expR']:.4f} ({h_zones[f_grey]['trades']}笔)")
    print(f"    远位(靠逆向位远): expR={h_zones[f_far]['expR']:.4f} ({h_zones[f_far]['trades']}笔)")

    if h_zones[f_near]["expR"] < h_zones[f_far]["expR"]:
        diff = h_zones[f_far]["expR"] - h_zones[f_near]["expR"]
        print(f"    ✓ 靠近逆向位 expR 更低（-{diff:.4f}），逆向位有压制/阻挡作用")
    else:
        print("    ⚠ 靠近逆向位 expR 反而更高，逆向位作用不明显")

    # 最优阈值建议
    print("\n  寻找最优阈值（基于细粒度数据）:")
    # 找顺向位里 expR 最高的近区间
    f_sorted = sorted(friendly_bins, key=lambda x: -x[2])
    print(f"    顺向位 expR 最高: {f_sorted[0][0]} (expR={f_sorted[0][2]:.4f}, {f_sorted[0][1]}笔)")
    # 找逆向位里 expR 最低的近区间
    h_sorted = sorted(hostile_bins, key=lambda x: x[2])
    print(f"    逆向位 expR 最低: {h_sorted[0][0]} (expR={h_sorted[0][2]:.4f}, {h_sorted[0][1]}笔)")


def _print_three_zones(trades, near, grey, mode, comp_near, comp_grey):
    """打印三档对比（新阈值 + 旧阈值）。"""
    new_zones = analyze_by_zone(trades, near, grey, mode)
    old_zones = analyze_by_zone(trades, comp_near, comp_grey, mode)

    print(f"\n  新阈值 ({near}% / {grey}%):")
    _print_zone_row(new_zones)
    print(f"  旧阈值 ({comp_near}% / {comp_grey}%):")
    _print_zone_row(old_zones)

    # 判断灰色地带是否确实是最低
    grey_key = list(new_zones.keys())[1]
    near_key = list(new_zones.keys())[0]
    far_key = list(new_zones.keys())[2]
    g_expR = new_zones[grey_key]["expR"]
    n_expR = new_zones[near_key]["expR"]
    f_expR = new_zones[far_key]["expR"]

    if g_expR < n_expR and g_expR < f_expR:
        print("  ✓ 新阈值灰色地带是 expR 最低区间")
    elif g_expR > n_expR and g_expR > f_expR:
        print("  ⚠ 新阈值灰色地带反而是 expR 最高区间")
    else:
        print("  ~ 新阈值灰色地带处于中间")


def _print_zone_row(zones):
    keys = list(zones.keys())
    print(
        f"    {keys[0]:<28} {zones[keys[0]]['trades']:>4}笔  expR={zones[keys[0]]['expR']:>7.4f}  胜率={zones[keys[0]]['win_rate'] * 100:>5.1f}%"
    )
    print(
        f"    {keys[1]:<28} {zones[keys[1]]['trades']:>4}笔  expR={zones[keys[1]]['expR']:>7.4f}  胜率={zones[keys[1]]['win_rate'] * 100:>5.1f}%"
    )
    print(
        f"    {keys[2]:<28} {zones[keys[2]]['trades']:>4}笔  expR={zones[keys[2]]['expR']:>7.4f}  胜率={zones[keys[2]]['win_rate'] * 100:>5.1f}%"
    )


def _print_bins(bins_data):
    print(f"    {'区间':<12} {'笔数':>5} {'expR':>8} {'胜率':>7}  胜率")
    print(f"    {'-' * 48}")
    for label, n, expR, wr in bins_data:
        if n == 0:
            print(f"    {label:<12} {0:>5} {'-':>8} {'-':>7}")
        else:
            bar = "█" * max(1, int(wr * 25))
            print(f"    {label:<12} {n:>5} {expR:>8.4f} {wr * 100:>6.1f}%  {bar}")


if __name__ == "__main__":
    main()
