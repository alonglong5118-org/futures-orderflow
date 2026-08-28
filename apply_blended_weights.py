"""
应用板块差异化融合权重到 GA 缓存

融合公式: w_blend = alpha * w_ga + (1-alpha) * w_default

各板块 alpha（基于 OOS 验证）：
  化工: 0.6
  有色: 0.7
  能源: 0.3
  农产品: 0.1
  黑系: 0.0
  航运: 0.0

用法:
  python3 apply_blended_weights.py --apply
  python3 apply_blended_weights.py --dry-run
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ga_factor_miner as gfm
from four_dim_strategy import SYMBOLS

# 板块级 alpha（GA 权重占比）
GROUP_ALPHA = {
    "化工": 0.6,
    "有色": 0.7,
    "能源": 0.3,
    "农产品": 0.1,
    "黑系": 0.0,
    "航运": 0.0,
}

DEFAULT_W = {"T": 0.6, "F": 0.25, "C": 0.15}


def blend_weights(ga_w, alpha):
    """融合 GA 权重和默认权重。"""
    return {
        "T": round(alpha * ga_w["T"] + (1 - alpha) * DEFAULT_W["T"], 6),
        "F": round(alpha * ga_w["F"] + (1 - alpha) * DEFAULT_W["F"], 6),
        "C": round(alpha * ga_w["C"] + (1 - alpha) * DEFAULT_W["C"], 6),
    }


def main():
    parser = argparse.ArgumentParser(description="应用板块差异化融合权重")
    parser.add_argument("--apply", action="store_true", help="实际更新缓存文件")
    parser.add_argument("--dry-run", action="store_true", help="只预览不修改")
    args = parser.parse_args()

    # 加载缓存
    cache_file = gfm.WEIGHTS_FILE
    with open(cache_file, encoding="utf-8") as f:
        cache = json.load(f)

    print("=" * 70)
    print("板块差异化融合权重")
    print(f"  缓存品种: {len(cache)}")
    print("  板块 alpha:")
    for grp, alpha in GROUP_ALPHA.items():
        print(f"    {grp}: α={alpha}")
    print("=" * 70)

    # 逐个品种融合
    new_cache = {}
    summary = {}

    for sym, data in cache.items():
        group = SYMBOLS.get(sym, {}).get("group", "其他")
        alpha = GROUP_ALPHA.get(group, 0.0)

        ga_w = data.get("best_weights", {}).get("base", DEFAULT_W)
        regime_adj = data.get("best_weights", {}).get("regime_adjust", {})

        if alpha == 0:
            # alpha=0，完全用默认权重 → 从缓存删除
            summary[sym] = {"group": group, "alpha": 0, "action": "removed",
                            "ga_w": ga_w, "blend_w": DEFAULT_W}
            continue

        # 融合基础权重
        blend_w = blend_weights(ga_w, alpha)

        # regime_adjust 也按 alpha 缩放
        blend_regime = {}
        for regime, adj in regime_adj.items():
            blend_regime[regime] = {
                "T": round(alpha * adj.get("T", 0), 6),
                "F": round(alpha * adj.get("F", 0), 6),
            }

        # 构建新条目
        new_data = dict(data)
        new_data["best_weights"] = {
            "base": blend_w,
            "regime_adjust": blend_regime,
        }
        new_data["blend_alpha"] = alpha
        new_data["blend_group"] = group
        # 保留原始 GA 权重用于审计
        new_data["original_ga_weights"] = {
            "base": ga_w,
            "regime_adjust": regime_adj,
        }

        new_cache[sym] = new_data
        summary[sym] = {"group": group, "alpha": alpha, "action": "blended",
                        "ga_w": ga_w, "blend_w": blend_w}

    # 打印汇总
    from collections import defaultdict
    group_stats = defaultdict(lambda: {"total": 0, "blended": 0, "removed": 0})

    for sym, s in summary.items():
        group_stats[s["group"]]["total"] += 1
        if s["action"] == "blended":
            group_stats[s["group"]]["blended"] += 1
        else:
            group_stats[s["group"]]["removed"] += 1

    print(f"\n--- 汇总 ---")
    print(f"{'板块':<6} {'总数':>5} {'融合':>5} {'移除':>5} {'alpha':>6}")
    print("-" * 35)
    total_blend = 0
    total_remove = 0
    for grp in GROUP_ALPHA:
        st = group_stats.get(grp, {"total": 0, "blended": 0, "removed": 0})
        total_blend += st["blended"]
        total_remove += st["removed"]
        print(f"{grp:<6} {st['total']:>5} {st['blended']:>5} {st['removed']:>5} {GROUP_ALPHA[grp]:>6.1f}")

    other = group_stats.get("其他", {"total": 0, "blended": 0, "removed": 0})
    if other["total"] > 0:
        print(f"其他   {other['total']:>5} {other['blended']:>5} {other['removed']:>5}    0.0")
        total_blend += other["blended"]
        total_remove += other["removed"]

    print("-" * 35)
    print(f"合计   {len(summary):>5} {total_blend:>5} {total_remove:>5}")

    # 融合后的权重 vs 默认权重 vs GA 权重对比（top10）
    print(f"\n--- 融合权重明细（按 alpha*GA影响排序）---")
    print(f"{'品种':<6} {'板块':<5} {'α':>4} "
          f"{'T_ga':>7} {'T_blend':>8} {'T_def':>7} "
          f"{'F_ga':>7} {'F_blend':>8} {'F_def':>7} "
          f"{'C_ga':>7} {'C_blend':>8} {'C_def':>7}")
    print("-" * 85)
    for sym in sorted(summary.keys(),
                      key=lambda s: -(summary[s]["alpha"] * abs(summary[s]["ga_w"].get("T", 0) - DEFAULT_W["T"]))):
        s = summary[sym]
        if s["action"] != "blended":
            continue
        g = s["ga_w"]
        b = s["blend_w"]
        print(f"{sym:<6} {s['group']:<5} {s['alpha']:>4.1f} "
              f"{g['T']:>7.3f} {b['T']:>8.4f} {DEFAULT_W['T']:>7.3f} "
              f"{g['F']:>7.3f} {b['F']:>8.4f} {DEFAULT_W['F']:>7.3f} "
              f"{g['C']:>7.3f} {b['C']:>8.4f} {DEFAULT_W['C']:>7.3f}")

    # 应用
    if args.apply:
        # 备份
        backup = cache_file + ".bak_before_blend"
        shutil.copy2(cache_file, backup)
        print(f"\n原文件已备份到: {backup}")

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(new_cache, f, ensure_ascii=False, indent=2)

        print(f"已应用融合权重: {total_blend} 个保留（融合），{total_remove} 个移除（回退默认）")
        print(f"→ live runner 会自动检测文件变化并重新加载（热更新）")
    else:
        print(f"\n预览模式，未修改文件。加 --apply 参数实际应用。")


if __name__ == "__main__":
    main()
