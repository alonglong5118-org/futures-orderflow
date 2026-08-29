"""
增强版 F 因子 OOS 验证（走步法）

对比：
1. 基准：旧版 F（基差 0.6 + 库存 0.1 + 季节性 0.3）
2. 增强版 F（7 因子 + 分板块差异化权重）

方法：
- monkey patch fundamental_feed.precompute_F_array
- 用 walk_forward_backtest 跑 5 折走步法
- 对比 expR / 胜率 / 交易数
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import four_dim_strategy as fds
import fundamental_factors as nff  # new fundamental factors
import fundamental_feed as ff_mod
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

# 板块
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)


def run_comparison(symbol, window=300, cooldown_bars=5):
    """对比旧 F 和增强 F 的走步法回测。

    返回 dict: {base, enhanced}
    """
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    # 预计算增强 F
    date_ints = df.index.year.values * 10000 + df.index.month.values * 100 + df.index.day.values

    sector = SYMBOLS.get(symbol, {}).get("group", "其他")
    try:
        enh_F = nff.precompute_enhanced_F_array(symbol, date_ints=date_ints, sector=sector)
    except Exception as e:
        print(f"  [WARN] {symbol} 增强 F 计算失败: {e}")
        return None

    # 构建 F_map
    F_map = {}
    for i, di in enumerate(date_ints):
        F_map[str(int(di))] = float(enh_F[i])

    # 基准回测
    base_res = walk_forward_backtest(
        symbol,
        cfg=DEFAULT_CONFIG,
        window=window,
        cooldown_bars=cooldown_bars,
        df_in=df,
    )

    # patch
    orig_ff = ff_mod.precompute_F_array
    orig_fds = fds.ff.precompute_F_array

    def patched_precompute_F(sym, date_strs=None, date_ints=None, months=None, **kwargs):
        if date_ints is not None:
            result = np.zeros(len(date_ints), dtype=float)
            for i, di in enumerate(date_ints):
                result[i] = F_map.get(str(int(di)), 0.0)
            return result
        elif date_strs is not None:
            result = np.zeros(len(date_strs), dtype=float)
            for i, ds in enumerate(date_strs):
                d_clean = str(ds).replace("-", "")[:8]
                result[i] = F_map.get(d_clean, 0.0)
            return result
        return np.zeros(100)

    ff_mod.precompute_F_array = patched_precompute_F
    fds.ff.precompute_F_array = patched_precompute_F

    try:
        enh_res = walk_forward_backtest(
            symbol,
            cfg=DEFAULT_CONFIG,
            window=window,
            cooldown_bars=cooldown_bars,
            df_in=df,
        )
    finally:
        ff_mod.precompute_F_array = orig_ff
        fds.ff.precompute_F_array = orig_fds

    return {"base": base_res, "enhanced": enh_res}


def extract_metrics(result):
    """从回测结果提取关键指标。"""
    if not result or not isinstance(result, dict):
        return None
    return {
        "expR": float(result.get("expR", 0)),
        "win_rate": float(result.get("win_rate", 0)),
        "n_trades": int(result.get("trades", 0)),
    }


def main():
    t0 = time.time()

    test_sectors = ["农产品", "有色", "能源", "黑系", "化工", "贵金属"]
    window = 300  # 走步法窗口

    print("=" * 90)
    print("增强版 F 因子 OOS 对比（走步法，window=300）")
    print("=" * 90)

    all_results = {}

    for sector in test_sectors:
        syms = GROUPS.get(sector, [])
        if not syms:
            continue

        print(f"\n【{sector}】")
        print(f"{'品种':<8}{'基准expR':>10}{'增强expR':>10}{'变化':>10}{'基准胜率':>10}{'增强胜率':>10}{'交易数':>8}")
        print("-" * 75)

        sector_results = {}
        n_improved = 0
        n_total = 0

        for sym in syms:
            if sym not in SYMBOLS:
                continue

            try:
                result = run_comparison(sym, window=window)
            except Exception as e:
                print(f"  {sym}: 错误 {e}")
                continue

            if result is None:
                continue

            base_m = extract_metrics(result["base"])
            enh_m = extract_metrics(result["enhanced"])

            if base_m is None or enh_m is None:
                continue

            delta = enh_m["expR"] - base_m["expR"]
            n_total += 1
            if delta > 0:
                n_improved += 1

            # 标记
            if delta > 0.05:
                flag = "✅"
            elif delta > 0:
                flag = "↗️"
            elif delta > -0.05:
                flag = "↘️"
            else:
                flag = "❌"

            print(
                f"{flag}{sym:<7}"
                f"{base_m['expR']:>+10.3f}"
                f"{enh_m['expR']:>+10.3f}"
                f"{delta:>+10.3f}"
                f"{base_m['win_rate']:>10.1%}"
                f"{enh_m['win_rate']:>10.1%}"
                f"{enh_m['n_trades']:>8}"
            )

            sector_results[sym] = {
                "base": base_m,
                "enhanced": enh_m,
                "delta_expR": float(delta),
                "delta_winrate": float(enh_m["win_rate"] - base_m["win_rate"]),
            }

        if sector_results:
            all_results[sector] = sector_results

            # 板块汇总
            deltas = [v["delta_expR"] for v in sector_results.values()]
            avg_delta = np.mean(deltas)
            pos_ratio = n_improved / n_total if n_total > 0 else 0

            print(f"{'平均':<8}{'':>10}{'':>10}{avg_delta:>+10.3f}{'':>10}{'':>10}{'':>8}")
            print(f"  提升品种占比: {n_improved}/{n_total} = {pos_ratio:.0%}")

    # 全板块汇总
    print("\n")
    print("=" * 90)
    print("全板块汇总")
    print("=" * 90)
    print(f"{'板块':<10}{'品种数':>8}{'平均expR提升':>14}{'提升占比':>12}")
    print("-" * 50)

    total_syms = 0
    total_improved = 0
    all_deltas = []

    for sector, sec_res in all_results.items():
        n = len(sec_res)
        deltas = [v["delta_expR"] for v in sec_res.values()]
        avg_d = np.mean(deltas)
        pos = sum(1 for d in deltas if d > 0)

        total_syms += n
        total_improved += pos
        all_deltas.extend(deltas)

        print(f"{sector:<10}{n:>8}{avg_d:>+14.3f}{pos}/{n} = {pos / n:.0%}")

    print("-" * 50)
    print(
        f"{'合计':<10}{total_syms:>8}{np.mean(all_deltas):>+14.3f}"
        f"{total_improved}/{total_syms} = {total_improved / total_syms:.0%}"
    )

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "enhanced_F_oos.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
