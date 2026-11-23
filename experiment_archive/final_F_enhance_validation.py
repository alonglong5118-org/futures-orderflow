"""
最终验证：增强版 F + 调整权重 全品种 OOS 回测

配置：
  - 启用增强版 F（7 因子 + 分板块权重）
  - T: 0.50, F: 0.35, C: 0.15

对比基准：
  - 旧版 F（基差0.6 + 库存0.1 + 季节性0.3）
  - T: 0.60, F: 0.25, C: 0.15
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy

import fundamental_feed as ff
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


def run_backtest(symbol, cfg, enhanced=False, window=300):
    """运行回测。"""
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    prev = ff.ENHANCED_F_ENABLED
    ff.enable_enhanced_F(enhanced)
    try:
        res = walk_forward_backtest(symbol, cfg=cfg, window=window, cooldown_bars=5, df_in=df)
    finally:
        ff.enable_enhanced_F(prev)

    return res


def main():
    t0 = time.time()
    window = 300

    # 基准配置
    cfg_base = DEFAULT_CONFIG

    # 增强配置
    cfg_enh = copy.deepcopy(DEFAULT_CONFIG)
    cfg_enh["combine_weights"] = {"T": 0.50, "F": 0.35, "C": 0.15}

    print("=" * 90)
    print("最终验证：增强版 F + 调整权重  全品种 OOS 对比")
    print("=" * 90)
    print("基准: 旧版 F（基差0.6+库存0.1+季节性0.3） + 权重 T:0.60 F:0.25 C:0.15")
    print("增强: 新版 F（7因子分板块权重）       + 权重 T:0.50 F:0.35 C:0.15")
    print()

    all_results = {}

    for sector, syms in sorted(GROUPS.items()):
        print(f"【{sector}】")
        print(f"{'品种':<6}{'基准expR':>10}{'增强expR':>10}{'变化':>10}{'基准胜率':>10}{'增强胜率':>10}{'交易数':>8}")
        print("-" * 75)

        sector_results = {}
        improved = 0
        total = 0

        for sym in sorted(syms):
            try:
                res_base = run_backtest(sym, cfg_base, enhanced=False, window=window)
                res_enh = run_backtest(sym, cfg_enh, enhanced=True, window=window)
            except Exception as e:
                print(f"{sym:<6}  错误: {str(e)[:40]}")
                continue

            if res_base is None or res_enh is None:
                continue

            expR_base = float(res_base.get("expR", 0))
            expR_enh = float(res_enh.get("expR", 0))
            wr_base = float(res_base.get("win_rate", 0))
            wr_enh = float(res_enh.get("win_rate", 0))
            trades = int(res_enh.get("trades", 0))

            delta = expR_enh - expR_base
            total += 1
            if delta > 0:
                improved += 1

            # 标记
            if delta > 0.05:
                mark = "✅"
            elif delta < -0.05:
                mark = "❌"
            elif delta > 0:
                mark = "↗"
            else:
                mark = "↘"

            print(
                f"{mark}{sym:<5}"
                f"{expR_base:>+10.3f}"
                f"{expR_enh:>+10.3f}"
                f"{delta:>+10.3f}"
                f"{wr_base:>9.0%}"
                f"{wr_enh:>9.0%}"
                f"{trades:>8}"
            )

            sector_results[sym] = {
                "base_expR": expR_base,
                "enh_expR": expR_enh,
                "delta": delta,
                "base_win_rate": wr_base,
                "enh_win_rate": wr_enh,
                "trades": trades,
            }

        # 板块汇总
        if sector_results:
            base_avg = np.mean([v["base_expR"] for v in sector_results.values()])
            enh_avg = np.mean([v["enh_expR"] for v in sector_results.values()])
            print("-" * 75)
            print(f"{'平均':<6}{base_avg:>+10.3f}{enh_avg:>+10.3f}{enh_avg - base_avg:>+10.3f}")
            print(f"  提升品种: {improved}/{total} = {improved / total:.0%}")
        print()

        all_results[sector] = sector_results

    # 全板块汇总
    print("=" * 90)
    print("全板块汇总")
    print("=" * 90)
    print(f"{'板块':<10}{'品种数':>8}{'基准expR':>12}{'增强expR':>12}{'变化':>12}{'提升占比':>10}")
    print("-" * 70)

    grand_base = []
    grand_enh = []
    grand_improved = 0
    grand_total = 0

    for sector in sorted(all_results.keys()):
        sr = all_results[sector]
        if not sr:
            continue
        base_avg = np.mean([v["base_expR"] for v in sr.values()])
        enh_avg = np.mean([v["enh_expR"] for v in sr.values()])
        improved = sum(1 for v in sr.values() if v["delta"] > 0)
        total = len(sr)

        grand_base.append(base_avg)
        grand_enh.append(enh_avg)
        grand_improved += improved
        grand_total += total

        print(
            f"{sector:<10}{total:>8}"
            f"{base_avg:>+12.3f}"
            f"{enh_avg:>+12.3f}"
            f"{enh_avg - base_avg:>+12.3f}"
            f"{improved / total:>9.0%}"
        )

    print("-" * 70)
    gb = np.mean(grand_base)
    ge = np.mean(grand_enh)
    print(f"{'合计':<10}{grand_total:>8}{gb:>+12.3f}{ge:>+12.3f}{ge - gb:>+12.3f}{grand_improved / grand_total:>9.0%}")
    print(f"  平均 expR 提升: {(ge - gb):+.3f} ({(ge - gb) / abs(gb) * 100:+.1f}%)")

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "final_F_enhance_oos.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
