"""
分板块 F 配置优化测试

根据各板块基本面因子的效果，差异化配置：
- 强板块（农产品、黑系）：启用增强 F + 高权重（T:0.50, F:0.35, C:0.15）
- 中板块（有色、贵金属）：启用增强 F + 中权重（T:0.55, F:0.30, C:0.15）
- 弱板块（化工、能源）：保持旧版 F + 原权重（T:0.60, F:0.25, C:0.15）
"""

import copy
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# 分板块配置方案
SECTOR_CONFIG = {
    # 强板块：增强 F + 高权重
    "农产品": {"enhanced": True, "weights": {"T": 0.50, "F": 0.35, "C": 0.15}},
    "黑系": {"enhanced": True, "weights": {"T": 0.50, "F": 0.35, "C": 0.15}},
    # 中板块：增强 F + 中权重
    "有色": {"enhanced": True, "weights": {"T": 0.55, "F": 0.30, "C": 0.15}},
    "贵金属": {"enhanced": True, "weights": {"T": 0.55, "F": 0.30, "C": 0.15}},
    # 弱板块：旧版 F + 原权重
    "化工": {"enhanced": False, "weights": {"T": 0.60, "F": 0.25, "C": 0.15}},
    "能源": {"enhanced": False, "weights": {"T": 0.60, "F": 0.25, "C": 0.15}},
    # 默认
    "其他": {"enhanced": False, "weights": {"T": 0.60, "F": 0.25, "C": 0.15}},
    "航运": {"enhanced": False, "weights": {"T": 0.60, "F": 0.25, "C": 0.15}},
}


def run_for_symbol(symbol, enhanced, weights, window=300):
    """运行单个品种的回测。"""
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["combine_weights"] = weights

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

    print("=" * 90)
    print("分板块 F 配置优化  全品种 OOS 对比")
    print("=" * 90)
    print("基准: 全局旧版 F + 权重 T:0.60 F:0.25 C:0.15")
    print("优化: 分板块差异化配置（强板块用增强F+高权重，弱板块保持旧版）")
    print()

    all_results = {}
    base_all = {}
    opt_all = {}

    for sector, syms in sorted(GROUPS.items()):
        cfg_info = SECTOR_CONFIG.get(sector, SECTOR_CONFIG["其他"])
        print(
            f"【{sector}】  ({'增强F' if cfg_info['enhanced'] else '旧版F'} + 权重 T:{cfg_info['weights']['T']:.2f} F:{cfg_info['weights']['F']:.2f} C:{cfg_info['weights']['C']:.2f})"
        )
        print(f"{'品种':<6}{'基准expR':>10}{'优化expR':>10}{'变化':>10}{'基准胜率':>10}{'优化胜率':>10}{'交易数':>8}")
        print("-" * 75)

        sector_results = {}
        improved = 0
        total = 0

        for sym in sorted(syms):
            try:
                # 基准：旧版 F + 默认权重
                res_base = run_for_symbol(
                    sym,
                    enhanced=False,
                    weights={"T": 0.60, "F": 0.25, "C": 0.15},
                    window=window,
                )
                # 优化：分板块配置
                res_opt = run_for_symbol(
                    sym,
                    enhanced=cfg_info["enhanced"],
                    weights=cfg_info["weights"],
                    window=window,
                )
            except Exception as e:
                print(f"{sym:<6}  错误: {str(e)[:40]}")
                continue

            if res_base is None or res_opt is None:
                continue

            expR_base = float(res_base.get("expR", 0))
            expR_opt = float(res_opt.get("expR", 0))
            wr_base = float(res_base.get("win_rate", 0))
            wr_opt = float(res_opt.get("win_rate", 0))
            trades = int(res_opt.get("trades", 0))

            delta = expR_opt - expR_base
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
                f"{expR_opt:>+10.3f}"
                f"{delta:>+10.3f}"
                f"{wr_base:>9.0%}"
                f"{wr_opt:>9.0%}"
                f"{trades:>8}"
            )

            sector_results[sym] = {
                "base_expR": expR_base,
                "opt_expR": expR_opt,
                "delta": delta,
                "base_win_rate": wr_base,
                "opt_win_rate": wr_opt,
                "trades": trades,
            }
            base_all[sym] = expR_base
            opt_all[sym] = expR_opt

        # 板块汇总
        if sector_results:
            base_avg = np.mean([v["base_expR"] for v in sector_results.values()])
            opt_avg = np.mean([v["opt_expR"] for v in sector_results.values()])
            print("-" * 75)
            print(f"{'平均':<6}{base_avg:>+10.3f}{opt_avg:>+10.3f}{opt_avg - base_avg:>+10.3f}")
            print(f"  提升品种: {improved}/{total} = {improved / total:.0%}")
        print()

        all_results[sector] = sector_results

    # 全板块汇总
    print("=" * 90)
    print("全板块汇总")
    print("=" * 90)
    print(f"{'板块':<10}{'品种数':>8}{'基准expR':>12}{'优化expR':>12}{'变化':>12}{'提升占比':>10}")
    print("-" * 70)

    grand_base = []
    grand_opt = []
    grand_improved = 0
    grand_total = 0

    for sector in sorted(all_results.keys()):
        sr = all_results[sector]
        if not sr:
            continue
        base_avg = np.mean([v["base_expR"] for v in sr.values()])
        opt_avg = np.mean([v["opt_expR"] for v in sr.values()])
        improved = sum(1 for v in sr.values() if v["delta"] > 0)
        total = len(sr)

        grand_base.append(base_avg)
        grand_opt.append(opt_avg)
        grand_improved += improved
        grand_total += total

        print(
            f"{sector:<10}{total:>8}"
            f"{base_avg:>+12.3f}"
            f"{opt_avg:>+12.3f}"
            f"{opt_avg - base_avg:>+12.3f}"
            f"{improved / total:>9.0%}"
        )

    print("-" * 70)
    gb = np.mean(grand_base)
    go = np.mean(grand_opt)
    print(f"{'合计':<10}{grand_total:>8}{gb:>+12.3f}{go:>+12.3f}{go - gb:>+12.3f}{grand_improved / grand_total:>9.0%}")
    print(f"  平均 expR 提升: {(go - gb):+.3f} ({(go - gb) / abs(gb) * 100:+.1f}%)")

    # 全品种等权平均
    all_base = list(base_all.values())
    all_opt = list(opt_all.values())
    print(
        f"\n全品种等权 expR: {np.mean(all_base):+.3f} → {np.mean(all_opt):+.3f} ({np.mean(all_opt) - np.mean(all_base):+.3f})"
    )

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sector_opt_F_oos.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "sector_config": SECTOR_CONFIG,
                "results": all_results,
                "summary": {
                    "base_avg": float(gb),
                    "opt_avg": float(go),
                    "delta": float(go - gb),
                    "improve_rate": float(grand_improved / grand_total),
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=float,
        )

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
