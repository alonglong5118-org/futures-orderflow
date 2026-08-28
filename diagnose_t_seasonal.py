"""T_seasonal 因子质量诊断

分析季节性因子的：
1. 信号触发频率（非零比例）
2. 信号方向分布（多/空/中性）
3. 信号预测力（触发后未来 N 日收益）
4. 与 T_trend / T_mean 的相关性
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np

from four_dim_strategy import load_daily
from ga_group_six_factor import get_group_symbols
from strategy_layer import s_seasonal

GROUPS = ["化工", "农产品", "有色", "黑系", "能源", "贵金属", "航运"]

TAIL = 500
FWD_DAYS = [1, 3, 5, 10]  # 未来 N 日收益


def analyze_symbol(symbol):
    """分析单个品种的 seasonal 因子质量。"""
    try:
        df = load_daily(symbol)
        if df is not None and len(df) > TAIL:
            df = df.tail(TAIL).reset_index(drop=True)
    except Exception:
        return None

    if df is None or len(df) < 60:
        return None

    # 逐日计算 seasonal 信号和未来收益
    signals = []
    fwd_rets = {d: [] for d in FWD_DAYS}
    non_zero_count = 0
    pos_count = 0
    neg_count = 0

    # 逐日回测
    for i in range(60, len(df)):
        sub_df = df.iloc[: i + 1]
        try:
            sig, _ = s_seasonal(sub_df)
        except Exception:
            sig = 0

        if sig != 0:
            non_zero_count += 1
            if sig > 0:
                pos_count += 1
            else:
                neg_count += 1

            # 计算未来收益
            close = sub_df["close"].iloc[-1]
            for d in FWD_DAYS:
                if i + d < len(df):
                    fwd_close = df["close"].iloc[i + d]
                    ret = (fwd_close / close - 1) * 100  # 百分比
                    fwd_rets[d].append(ret * sig)  # 乘以信号方向，正确预测为正

    n_days = len(df) - 60
    hit_rates = {}
    avg_aligned_rets = {}
    for d in FWD_DAYS:
        rets = fwd_rets[d]
        if rets:
            hit_rates[d] = sum(1 for r in rets if r > 0) / len(rets)
            avg_aligned_rets[d] = float(np.mean(rets))
        else:
            hit_rates[d] = 0.0
            avg_aligned_rets[d] = 0.0

    return {
        "symbol": symbol,
        "total_days": n_days,
        "nonzero_days": non_zero_count,
        "nonzero_pct": non_zero_count / n_days if n_days > 0 else 0,
        "pos_days": pos_count,
        "neg_days": neg_count,
        "hit_rates": hit_rates,
        "avg_aligned_rets": avg_aligned_rets,
    }


def main():
    print("=" * 70, flush=True)
    print("T_seasonal 因子质量诊断", flush=True)
    print("=" * 70, flush=True)

    all_results = {}
    for group in GROUPS:
        print(f"\n[板块] {group}", flush=True)

        # 加载板块品种
        symbols = get_group_symbols(group)

        group_results = []
        for sym in symbols:
            r = analyze_symbol(sym)
            if r:
                group_results.append(r)

        if not group_results:
            print(f"  无有效品种", flush=True)
            continue

        # 板块汇总
        avg_nonzero_pct = np.mean([r["nonzero_pct"] for r in group_results])
        avg_hit_5d = np.mean([r["hit_rates"][5] for r in group_results])
        avg_ret_5d = np.mean([r["avg_aligned_rets"][5] for r in group_results])

        print(f"  有效品种: {len(group_results)}", flush=True)
        print(f"  平均信号触发率: {avg_nonzero_pct * 100:.1f}%", flush=True)
        print(f"  5日平均命中率: {avg_hit_5d * 100:.1f}%", flush=True)
        print(f"  5日平均对齐收益: {avg_ret_5d:+.3f}%", flush=True)

        # 列出表现最好和最差的品种
        sorted_by_ret = sorted(group_results, key=lambda x: x["avg_aligned_rets"][5], reverse=True)
        print(f"  最佳3品种 (5日对齐收益):", flush=True)
        for r in sorted_by_ret[:3]:
            print(
                f"    {r['symbol']}: {r['avg_aligned_rets'][5]:+.3f}%  "
                f"命中率 {r['hit_rates'][5] * 100:.0f}%  "
                f"触发率 {r['nonzero_pct'] * 100:.1f}%",
                flush=True,
            )
        print(f"  最差3品种:", flush=True)
        for r in sorted_by_ret[-3:]:
            print(
                f"    {r['symbol']}: {r['avg_aligned_rets'][5]:+.3f}%  "
                f"命中率 {r['hit_rates'][5] * 100:.0f}%  "
                f"触发率 {r['nonzero_pct'] * 100:.1f}%",
                flush=True,
            )

        all_results[group] = group_results

    # 全市场汇总
    print(f"\n{'=' * 70}", flush=True)
    print("全市场汇总", flush=True)
    print(f"{'=' * 70}", flush=True)
    all_nonzero = []
    all_hit5 = []
    all_ret5 = []
    for group, results in all_results.items():
        for r in results:
            all_nonzero.append(r["nonzero_pct"])
            all_hit5.append(r["hit_rates"][5])
            all_ret5.append(r["avg_aligned_rets"][5])

    print(f"总品种数: {len(all_nonzero)}", flush=True)
    print(f"平均触发率: {np.mean(all_nonzero) * 100:.1f}%", flush=True)
    print(f"平均5日命中率: {np.mean(all_hit5) * 100:.1f}%", flush=True)
    print(f"平均5日对齐收益: {np.mean(all_ret5):+.3f}%", flush=True)
    print(f"对齐收益 > 0 的品种比例: {sum(1 for r in all_ret5 if r > 0) / len(all_ret5) * 100:.1f}%", flush=True)

    print(f"\n诊断结论:", flush=True)
    mean_ret = np.mean(all_ret5)
    if mean_ret > 0.2:
        print(f"  ✅ 季节性因子有一定预测力（平均5日对齐收益 {mean_ret:+.3f}%）", flush=True)
    elif mean_ret > 0:
        print(f"  ⚠️ 季节性因子预测力较弱（平均5日对齐收益 {mean_ret:+.3f}%）", flush=True)
    else:
        print(f"  ❌ 季节性因子没有预测力（平均5日对齐收益 {mean_ret:+.3f}%）", flush=True)

    if np.mean(all_nonzero) < 0.1:
        print(f"  ⚠️ 信号触发率太低（{np.mean(all_nonzero) * 100:.1f}%），可能因阈值过严", flush=True)


if __name__ == "__main__":
    main()
