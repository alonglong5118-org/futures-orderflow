"""
SR 过滤效果模拟（基于已标注数据）
用 sr_threshold_validation.py 生成的标注数据，模拟不同过滤方案的效果。

核心：SR 过滤不是直接删交易，而是提高 T 阈值 → 有些信号就不会触发了。
但这里我们简化：假设"被 SR 惩罚的交易"（危险区的）直接过滤掉，
看剩下的交易 expR 变化（偏乐观估计，因为实际是提高阈值不是全过滤）。
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


def annotate_trades(symbol, trades, min_bars=60):
    """给交易标注 SR 距离（顺向/逆向）。"""
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
        if direction > 0:
            friendly_dist = sup_dist
            hostile_dist = res_dist
        else:
            friendly_dist = res_dist
            hostile_dist = sup_dist

        nearest_dist = min(sup_dist, res_dist)

        annotated.append(
            {
                "symbol": symbol,
                "direction": direction,
                "R_adj": t["R_adj"],
                "T_D": t.get("T_D", 0),
                "nearest_dist": nearest_dist,
                "friendly_dist": friendly_dist,
                "hostile_dist": hostile_dist,
                "regime": t.get("regime", "?"),
            }
        )

    return annotated


def simulate_filter(trades, filter_name, is_filtered_fn):
    """模拟过滤：is_filtered_fn(t) 返回 True 表示这笔被过滤掉。"""
    kept = [t for t in trades if not is_filtered_fn(t)]
    filtered_out = len(trades) - len(kept)

    if not kept:
        return {"name": filter_name, "trades": 0, "expR": 0, "win_rate": 0, "filtered": filtered_out}

    expR = sum(t["R_adj"] for t in kept) / len(kept)
    wr = sum(1 for t in kept if t["R_adj"] > 0) / len(kept)

    return {
        "name": filter_name,
        "trades": len(kept),
        "expR": round(expR, 4),
        "win_rate": round(wr, 4),
        "filtered": filtered_out,
    }


def main():
    parser = argparse.ArgumentParser(description="SR 过滤效果模拟")
    parser.add_argument("--symbols", type=str, default="J,eb,SH,cu,al,zn,sp,ag,au,rb")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 75)
    print(f"SR 过滤效果模拟 · {len(symbols)} 个品种")
    print("  （基于回测交易明细 + SR 历史标注，模拟过滤后的 expR 变化）")
    print("=" * 75)

    # 收集所有标注交易
    all_trades = []
    for idx, sym in enumerate(symbols):
        print(f"\n[{idx + 1}/{len(symbols)}] {sym} ", end="", flush=True)
        bt = walk_forward_backtest(sym, DEFAULT_CONFIG)
        if not bt or bt.get("trades", 0) == 0:
            print(f"无交易")
            continue
        trades_detail = bt.get("trades_detail", [])
        print(f"{bt['trades']}笔 expR={bt['expR']:.3f}...", end="", flush=True)
        annotated = annotate_trades(sym, trades_detail)
        print(f"标注{len(annotated)}笔")
        all_trades.extend(annotated)

    if not all_trades:
        print("\n无数据！")
        return

    n_total = len(all_trades)
    base_expR = sum(t["R_adj"] for t in all_trades) / n_total
    base_wr = sum(1 for t in all_trades if t["R_adj"] > 0) / n_total

    print(f"\n{'=' * 75}")
    print(f"基准: {n_total} 笔  expR={base_expR:.4f}  胜率={base_wr * 100:.1f}%")

    # 定义各种过滤方案
    filters = [
        ("无过滤（基准）", lambda t: False),
        # 旧方案：灰色地带 1.5~3.0%（不分方向）
        ("旧灰色地带 1.5~3.0%", lambda t: 1.5 <= t["nearest_dist"] < 3.0),
        # 旧方案+：近位区也过滤 0~1.5%（不分方向）
        ("旧近位+灰色 0~3.0%", lambda t: t["nearest_dist"] < 3.0),
        # 新方案：逆向位危险区 0.3~1.0%
        ("逆向危险区 0.3~1.0%", lambda t: 0.3 <= t["hostile_dist"] < 1.0),
        # 新方案扩展：逆向位 0.3~1.5%
        ("逆向危险区 0.3~1.5%", lambda t: 0.3 <= t["hostile_dist"] < 1.5),
        # 逆向位极近+危险 0~1.0%
        ("逆向近位全过滤 0~1.0%", lambda t: t["hostile_dist"] < 1.0),
        # 逆向位危险 + 顺向位也近（双近位，最模糊的情况）
        ("双近位 都<1.0%", lambda t: t["hostile_dist"] < 1.0 and t["friendly_dist"] < 1.0),
    ]

    print(f"\n{'=' * 75}")
    print("过滤方案对比")
    print(f"{'=' * 75}")
    print(f"{'方案':<25} {'保留笔数':>8} {'过滤掉':>6} {'expR':>8} {'胜率':>8} {'expR变化':>10}")
    print("-" * 68)

    results = []
    for name, fn in filters:
        r = simulate_filter(all_trades, name, fn)
        results.append(r)
        if r["trades"] == 0:
            print(f"{name:<25} {0:>8} {r['filtered']:>6} {'-':>8} {'-':>8} {'-':>10}")
            continue
        diff = r["expR"] - base_expR
        diff_pct = diff / abs(base_expR) * 100 if base_expR != 0 else 0
        diff_str = f"{diff:+.4f} ({diff_pct:+.1f}%)"
        print(
            f"{name:<25} {r['trades']:>8} {r['filtered']:>6} "
            f"{r['expR']:>8.4f} {r['win_rate'] * 100:>7.1f}% {diff_str:>10}"
        )

    # 逐品种：逆向危险区 0.3~1.0% 的效果
    print(f"\n{'=' * 75}")
    print("逐品种对比：逆向危险区 0.3~1.0% 过滤效果")
    print(f"{'=' * 75}")
    print(f"{'品种':<6} {'基准expR':>10} {'基准笔数':>8} {'过滤后expR':>12} {'过滤笔数':>8} {'变化':>10}")
    print("-" * 62)

    hostile_filter = lambda t: 0.3 <= t["hostile_dist"] < 1.0

    n_better = 0
    n_worse = 0
    for sym in symbols:
        sym_trades = [t for t in all_trades if t["symbol"] == sym]
        if not sym_trades:
            print(f"{sym:<6} {'-':>10} {'-':>8} {'-':>12} {'-':>8} {'-':>10}")
            continue

        base_e = sum(t["R_adj"] for t in sym_trades) / len(sym_trades)
        kept = [t for t in sym_trades if not hostile_filter(t)]
        if not kept:
            print(f"{sym:<6} {base_e:>10.4f} {len(sym_trades):>8} {'全部过滤':>12} {len(sym_trades):>8} {'-':>10}")
            continue

        new_e = sum(t["R_adj"] for t in kept) / len(kept)
        diff = new_e - base_e
        diff_pct = diff / abs(base_e) * 100 if base_e != 0 else 0
        diff_str = f"{diff_pct:+.1f}%"

        if diff > 0:
            n_better += 1
        elif diff < 0:
            n_worse += 1

        print(
            f"{sym:<6} {base_e:>10.4f} {len(sym_trades):>8} "
            f"{new_e:>12.4f} {len(sym_trades) - len(kept):>8} {diff_str:>10}"
        )

    print(f"\n  提升品种: {n_better}  下降品种: {n_worse}  持平/无数据: {len(symbols) - n_better - n_worse}")

    # 结论
    print(f"\n{'=' * 75}")
    print("结论")
    print(f"{'=' * 75}")

    best = max(results[1:], key=lambda r: r["expR"] if r["trades"] > 10 else -999)
    print(f"\n  expR 最高的方案: {best['name']} (expR={best['expR']:.4f}, 保留{best['trades']}笔)")
    print(f"  相对基准提升: {(best['expR'] - base_expR) / abs(base_expR) * 100 if base_expR else 0:+.1f}%")

    # 推荐方案
    print(f"\n  推荐方案评估:")
    hostile_r = [r for r in results if "逆向危险区 0.3~1.0%" in r["name"]][0]
    old_r = [r for r in results if "旧灰色地带 1.5~3.0%" in r["name"]][0]

    print(
        f"    逆向危险区 0.3~1.0%: expR={hostile_r['expR']:.4f} "
        f"({(hostile_r['expR'] - base_expR) / abs(base_expR) * 100 if base_expR else 0:+.1f}%)"
    )
    print(
        f"    旧灰色地带 1.5~3.0%:  expR={old_r['expR']:.4f} "
        f"({(old_r['expR'] - base_expR) / abs(base_expR) * 100 if base_expR else 0:+.1f}%)"
    )

    if hostile_r["expR"] > old_r["expR"] and hostile_r["expR"] > base_expR:
        print(f"  ✓ 逆向位危险区方案胜出，且优于基准")
    elif hostile_r["expR"] > base_expR:
        print(f"  ~ 逆向位方案优于基准，但旧方案更好")
    else:
        print(f"  ⚠ 两种方案都没有提升 expR，可能需要调整参数或换思路")


if __name__ == "__main__":
    main()
