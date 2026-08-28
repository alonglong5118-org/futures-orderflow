"""
GA 优化结果质量筛选
- 过滤掉权重极端、稳健性差、收益为负的品种
- 合格的保留 GA 权重，不合格的回退到默认权重
- 输出筛选报告

用法:
  python3 ga_quality_filter.py
  python3 ga_quality_filter.py --apply   # 直接更新缓存（不合格的删除，让策略回退默认权重）
"""

import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ga_factor_miner as gfm
from four_dim_strategy import SYMBOLS, DEFAULT_CONFIG


# 质量筛选阈值（放宽版：T下限极低，F/C上限放宽）
QUALITY_THRESHOLDS = {
    "min_expR": 0.0,           # 最低期望收益
    "min_robust": 0.5,         # 最低稳健性
    "max_abs_T": 1.5,          # T 权重绝对值上限
    "min_T": 0.01,             # T 权重下限（放宽：允许 T 很小，只要正）
    "max_abs_F": 1.5,          # F 权重绝对值上限（放宽）
    "max_abs_C": 1.5,          # C 权重绝对值上限（放宽）
    "min_total_weight": 0.3,   # 三因子权重和下限
    "max_total_weight": 3.0,   # 三因子权重和上限
}


def check_quality(symbol, data):
    """检查单个品种的 GA 结果质量，返回 (passed: bool, reasons: list)。"""
    reasons = []
    bw = data.get("best_weights", {}).get("base", {})
    expR = data.get("best_expR", -999)
    robust = data.get("robust_score", -999)

    T = bw.get("T", 0)
    F = bw.get("F", 0)
    C = bw.get("C", 0)
    total = abs(T) + abs(F) + abs(C)

    th = QUALITY_THRESHOLDS

    # 1. expR 必须正
    if expR <= th["min_expR"]:
        reasons.append(f"expR={expR:.4f} ≤ {th['min_expR']}")

    # 2. 稳健性
    if robust < th["min_robust"]:
        reasons.append(f"robust={robust:.3f} < {th['min_robust']}")

    # 3. T 权重范围
    if T < th["min_T"]:
        reasons.append(f"T={T:.3f} < {th['min_T']}")
    if abs(T) > th["max_abs_T"]:
        reasons.append(f"|T|={abs(T):.3f} > {th['max_abs_T']}")

    # 4. F 权重范围
    if abs(F) > th["max_abs_F"]:
        reasons.append(f"|F|={abs(F):.3f} > {th['max_abs_F']}")

    # 5. C 权重范围
    if abs(C) > th["max_abs_C"]:
        reasons.append(f"|C|={abs(C):.3f} > {th['max_abs_C']}")

    # 6. 权重总和
    if total < th["min_total_weight"]:
        reasons.append(f"权重和={total:.3f} < {th['min_total_weight']}")
    if total > th["max_total_weight"]:
        reasons.append(f"权重和={total:.3f} > {th['max_total_weight']}")

    return len(reasons) == 0, reasons


def main():
    parser = argparse.ArgumentParser(description="GA 优化结果质量筛选")
    parser.add_argument("--apply", action="store_true", help="应用筛选：不合格的从缓存删除")
    parser.add_argument("--min-expR", type=float, default=None, help="自定义最低 expR")
    parser.add_argument("--min-robust", type=float, default=None, help="自定义最低稳健性")
    args = parser.parse_args()

    if args.min_expR is not None:
        QUALITY_THRESHOLDS["min_expR"] = args.min_expR
    if args.min_robust is not None:
        QUALITY_THRESHOLDS["min_robust"] = args.min_robust

    # 加载缓存
    cache_file = gfm.WEIGHTS_FILE
    if not os.path.exists(cache_file):
        print(f"缓存文件不存在: {cache_file}")
        return

    with open(cache_file, encoding="utf-8") as f:
        cache = json.load(f)

    print("=" * 70)
    print("GA 优化结果质量筛选")
    print(f"  总品种: {len(cache)}")
    print(f"  阈值: expR>{QUALITY_THRESHOLDS['min_expR']}, "
          f"robust>={QUALITY_THRESHOLDS['min_robust']}, "
          f"T∈[{QUALITY_THRESHOLDS['min_T']},{QUALITY_THRESHOLDS['max_abs_T']}], "
          f"|F|≤{QUALITY_THRESHOLDS['max_abs_F']}, "
          f"|C|≤{QUALITY_THRESHOLDS['max_abs_C']}")
    print("=" * 70)

    # 逐个检查
    passed = []
    failed = []

    for sym in sorted(cache.keys()):
        data = cache[sym]
        ok, reasons = check_quality(sym, data)
        grp = SYMBOLS.get(sym, {}).get("group", "?")
        expR = data.get("best_expR", 0)
        rob = data.get("robust_score", 0)
        bw = data.get("best_weights", {}).get("base", {})

        entry = {
            "symbol": sym,
            "expR": expR,
            "robust": rob,
            "T": bw.get("T", 0),
            "F": bw.get("F", 0),
            "C": bw.get("C", 0),
            "group": grp,
            "reasons": reasons,
        }

        if ok:
            passed.append(entry)
        else:
            failed.append(entry)

    # 打印通过的
    print(f"\n✓ 通过筛选: {len(passed)}/{len(cache)}")
    if passed:
        print(f"\n{'品种':<6} {'expR':>8} {'胜率':>8} {'稳健性':>8} "
              f"{'T':>6} {'F':>6} {'C':>6} {'板块':<6}")
        print("-" * 62)
        for p in sorted(passed, key=lambda x: -x["expR"]):
            wr = cache[p["symbol"]].get("best_winrate", 0)
            print(f"{p['symbol']:<6} {p['expR']:>8.4f} {wr*100:>7.1f}% "
                  f"{p['robust']:>8.3f} {p['T']:>6.3f} {p['F']:>6.3f} "
                  f"{p['C']:>6.3f} {p['group']:<6}")

    # 打印未通过的（分类别）
    print(f"\n✗ 未通过: {len(failed)}/{len(cache)}")

    # 按原因分类
    from collections import Counter
    reason_counter = Counter()
    for f in failed:
        for r in f["reasons"]:
            # 提取原因类型
            reason_type = r.split("=")[0]
            reason_counter[reason_type] += 1

    print(f"\n  失败原因分布:")
    for reason, count in reason_counter.most_common():
        print(f"    {reason}: {count} 个")

    print(f"\n  未通过明细:")
    for f in sorted(failed, key=lambda x: -x["expR"]):
        reason_str = "; ".join(f["reasons"][:2])
        print(f"    {f['symbol']:<6} expR={f['expR']:>7.4f} "
              f"robust={f['robust']:>6.3f} | {reason_str}")

    # 板块统计（通过的）
    if passed:
        from collections import defaultdict
        groups = defaultdict(list)
        for p in passed:
            groups[p["group"]].append(p["expR"])
        print(f"\n通过品种的板块平均 expR:")
        for grp, expRs in sorted(groups.items(), key=lambda x: -sum(x[1])/len(x[1])):
            avg = sum(expRs) / len(expRs)
            print(f"  {grp}: {avg:.4f} (n={len(expRs)})")

    # 应用筛选
    if args.apply:
        new_cache = {p["symbol"]: cache[p["symbol"]] for p in passed}
        removed = len(cache) - len(new_cache)

        # 备份原文件
        backup = cache_file + ".bak"
        import shutil
        shutil.copy2(cache_file, backup)
        print(f"\n原文件已备份到: {backup}")

        # 保存新缓存
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(new_cache, f, ensure_ascii=False, indent=2)

        print(f"已应用筛选: 删除 {removed} 个不合格品种，保留 {len(new_cache)} 个")
        print(f"  → 不合格品种将使用默认权重")

    print(f"\n提示: 加 --apply 参数可实际更新缓存文件")


if __name__ == "__main__":
    main()
