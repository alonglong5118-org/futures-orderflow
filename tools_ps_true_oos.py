#!/usr/bin/env python3
"""P-S 生产黑名单 —— 干净 OOS 检验（2026-09-07 修正版）。

【为什么要有这个脚本】
原 `tools_oos_ps_revalidation.py` 有两处基线污染，导致结论不可信：
  1) 评估基线 base 用 DEFAULT_CONFIG，而 DEFAULT_CONFIG 自带生产黑名单
     → `prod_test − base_test` 恒为 +0.000（20/20 品种全零），"过拟合坐实"是测量假象；
  2) greedy_derive 的搜索基线同样被污染 → 重推出的黑名单是在错误基线上搜出来的。
本脚本全程以「黑名单彻底关闭」为基线重推 + 评估。

【样本量警告 · 必读】
单品种成交极少（rb 全历史 4207 根 bar 仅 12 笔；tail-500 窗口只有 1 笔）。
**禁止逐品种下结论**，一律看跨品种合并统计（pooled expR / 总 R / 符号检验）。

口径：
  A 无黑名单（干净基线）
  B 生产黑名单（含测试集拟合信息，偏乐观，仅作对照）
  C 训练集重推 → 测试集评估（真正 OOS）
判定：C 相对 A 的合并增益 + 符号检验 p 值。p > 0.05 即视为「无证据」，不得据此改动生产配置。
"""
import copy
import json
import os
import sys
import time
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from four_dim_strategy import DEFAULT_CONFIG, STRAT_CLUSTERS, load_daily, walk_forward_backtest

ALL_STRATS = [s for members in STRAT_CLUSTERS.values() for s in members]
PROD_BL = DEFAULT_CONFIG.get("strat_blacklist", {}) or {}
SYMBOLS = sorted(PROD_BL.keys())
TRAIN_RATIO = 2.0 / 3.0


def run(sym, df, blacklist):
    """blacklist 为空 → 彻底关闭（显式置空，绝不继承 DEFAULT_CONFIG 的黑名单）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["strat_blacklist"] = {sym: list(blacklist)} if blacklist else {}
    r = walk_forward_backtest(sym, cfg=cfg, df_in=df)
    return r.get("expR", 0.0), int(r.get("trades", 0))


def _would_empty(bl):
    s = set(bl)
    return any(all(m in s for m in members) for members in STRAT_CLUSTERS.values())


def greedy_derive_clean(sym, train_df):
    """在干净基线上，用训练集贪心重推黑名单。"""
    base, n = run(sym, train_df, [])
    if n == 0:
        return [], base
    bl, cur = [], base
    while True:
        best_strat, best_gain = None, 0.0
        for s in ALL_STRATS:
            if s in bl or _would_empty(bl + [s]):
                continue
            e, _ = run(sym, train_df, bl + [s])
            if e - cur > best_gain:
                best_gain, best_strat = e - cur, s
        if best_strat is None:
            break
        bl.append(best_strat)
        cur += best_gain
    return bl, cur


def _pooled(agg):
    tot, n = agg
    return (tot / n if n else 0.0), tot, n


def _sign_test(w_a, w_b):
    m = w_a + w_b
    if m == 0:
        return None
    k = min(w_a, w_b)
    return min(2 * sum(comb(m, i) for i in range(k + 1)) / (2 ** m), 1.0)


def main():
    t0 = time.time()
    if not SYMBOLS:
        print("生产黑名单为空，无可检验品种。退出。")
        return
    print(f"P-S 干净 OOS 检验（{len(SYMBOLS)} 品种，训练 {TRAIN_RATIO:.0%} 重推 → 测试 {1 - TRAIN_RATIO:.0%} 评估）")
    print("=" * 100)
    agg = {"A": [0.0, 0], "B": [0.0, 0], "C": [0.0, 0]}
    win_c = win_a = 0
    rows = []
    for sym in SYMBOLS:
        df = load_daily(sym)
        if df is None or len(df) < 300:
            continue
        k = int(len(df) * TRAIN_RATIO)
        train, test = df.iloc[:k], df.iloc[k:]
        a_e, a_n = run(sym, test, [])
        if a_n == 0:
            print(f"{sym:4} 测试集无成交，跳过")
            continue
        b_e, b_n = run(sym, test, PROD_BL.get(sym, []))
        derived, _ = greedy_derive_clean(sym, train)
        c_e, c_n = run(sym, test, derived)
        for tag, (e, n) in (("A", (a_e, a_n)), ("B", (b_e, b_n)), ("C", (c_e, c_n))):
            agg[tag][0] += e * n
            agg[tag][1] += n
        if c_e > a_e:
            win_c += 1
        elif a_e > c_e:
            win_a += 1
        rows.append({"sym": sym, "A_nobl": round(a_e, 4), "B_prod": round(b_e, 4),
                     "C_derived": round(c_e, 4), "n": a_n, "derived": derived})
        print(f"{sym:4} A无黑名单={a_e:+.3f}  B生产={b_e:+.3f}  C重推(OOS)={c_e:+.3f}  "
              f"n={a_n:3d}  重推={derived}")

    print("=" * 100)
    pooled = {}
    for tag, label in (("A", "无黑名单  "), ("B", "生产黑名单"), ("C", "重推(OOS)")):
        pooled[tag], tot, n = _pooled(agg[tag])
        print(f"  {tag} {label}  合并 expR = {pooled[tag]:+.4f}R   总 R = {tot:+.2f}R   n = {n}")
    print(f"  C 相对 A：Δ = {pooled['C'] - pooled['A']:+.4f}R   逐品种 C 优 {win_c} / A 优 {win_a}")
    p = _sign_test(win_c, win_a)
    if p is not None:
        print(f"  符号检验 p ≈ {p:.3f}"
              + ("  → 无统计证据，不得据此改动生产配置" if p > 0.05 else "  → 方向显著"))
    print(f"耗时 {time.time() - t0:.0f}s")
    out = "/tmp/ps_true_oos_result.json"
    with open(out, "w") as f:
        json.dump({"pooled": pooled, "sign_test_p": p, "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"结果写入 {out}")


if __name__ == "__main__":
    main()
