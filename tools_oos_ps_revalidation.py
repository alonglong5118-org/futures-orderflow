#!/usr/bin/env python3
"""P-S 策略选择 OOS 重验（2026-09-07 修正版）。

【重要 · 本脚本曾给出错误结论】
旧版有两处基线污染，导致「生产黑名单 OOS 增益 +0.000R、过拟合坐实」这一结论不可信：
  1) `wfb_expR(sym, df, None)` 直接沿用 DEFAULT_CONFIG，而 DEFAULT_CONFIG **自带生产黑名单**
     → 所谓"基线"其实已经含黑名单 → `prod_test − base_test` 恒为 +0.000（20/20 品种全零）。
     那个 +0.000R 是测量假象，不是发现。
  2) `greedy_derive` 的累进基线同样被污染 → 重推黑名单是在错误基线上贪心搜出来的。
修正：基线一律显式置空 `cfg["strat_blacklist"] = {}`。

⚠️ 本脚本已被 `tools_ps_true_oos.py` 取代（后者同时给出 A/B/C 三组 + 合并统计 + 符号检验）。
   保留本脚本仅为历史可追溯，新评估请用 tools_ps_true_oos.py。

【样本量警告】单品种成交极少（rb 全历史仅 12 笔），逐品种判优劣无统计意义，
必须看跨品种合并统计与符号检验。本脚本已补上 pooled 输出。

判定：derived_test > base_test → 黑名单泛化（OOS 正）；
      prod_test 明显 > derived_test → 生产黑名单在测试集上过拟合（因拟合时含测试集）；
      derived_test <= base_test → 黑名单不泛化，in-sample 增益是假象。
      **以上任一判定在 p > 0.05 时均不成立，不得据此改动生产配置。**
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


def wfb_expR(symbol, df, blacklist=None):
    """★修正点：无论 blacklist 是否为 None，都显式写 cfg["strat_blacklist"]，
    确保"无黑名单"基线真的没有黑名单（旧版会继承 DEFAULT_CONFIG 的生产黑名单）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["strat_blacklist"] = {symbol: list(blacklist)} if blacklist else {}
    r = walk_forward_backtest(symbol, cfg=cfg, df_in=df)
    return r.get("expR"), r.get("trades", 0)


def greedy_derive(symbol, train_df):
    """在**干净基线**上贪心重推黑名单（与 v6.3 同法，但只用训练集）。"""
    base, _ = wfb_expR(symbol, train_df, None)  # 干净基线（修正后）
    bl = []
    while True:
        best_strat, best_gain = None, 0.0
        for s in ALL_STRATS:
            if s in bl:
                continue
            # 防止掏空整个簇
            if _would_empty_cluster(symbol, bl + [s]):
                continue
            expR, _ = wfb_expR(symbol, train_df, bl + [s])
            gain = expR - base
            if gain > best_gain:
                best_gain, best_strat = gain, s
        if best_strat is None:
            break
        bl.append(best_strat)
        base = base + best_gain  # 累进基线（贪心逐次验证）
    return bl


def _would_empty_cluster(symbol, bl):
    bl_set = set(bl)
    for members in STRAT_CLUSTERS.values():
        if all(m in bl_set for m in members):
            return True
    return False


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
    rows = []
    print(f"P-S 策略选择 OOS 重验（{len(SYMBOLS)} 品种，训练 2/3 推导 / 测试 1/3 held-out，基线已修正）")
    print("=" * 92)
    agg_base = [0.0, 0]
    agg_derived = [0.0, 0]
    agg_prod = [0.0, 0]
    win_d = win_b = 0
    for sym in SYMBOLS:
        df = load_daily(sym)
        if df is None or len(df) < 300:
            print(f"{sym}: 数据不足，跳过")
            continue
        k = int(len(df) * TRAIN_RATIO)
        train, test = df.iloc[:k], df.iloc[k:]
        base_test, n_test = wfb_expR(sym, test, None)
        if n_test == 0:
            print(f"{sym}: 测试集无成交，跳过")
            continue
        derived = greedy_derive(sym, train)
        derived_test, _ = wfb_expR(sym, test, derived)
        prod = PROD_BL.get(sym, [])
        prod_test, _ = wfb_expR(sym, test, prod) if prod else (base_test, n_test)
        d_is = derived_test - base_test
        p_is = prod_test - base_test
        verdict = "✅泛化" if d_is > 0.02 else ("⚠️退化" if d_is < -0.02 else "➖持平")
        agg_base[0] += base_test * n_test
        agg_base[1] += n_test
        agg_derived[0] += derived_test * n_test
        agg_derived[1] += n_test
        agg_prod[0] += prod_test * n_test
        agg_prod[1] += n_test
        if derived_test > base_test:
            win_d += 1
        elif base_test > derived_test:
            win_b += 1
        rows.append({"sym": sym, "base_test": round(base_test, 4), "derived_test": round(derived_test, 4),
                     "prod_test": round(prod_test, 4), "d_is": round(d_is, 4), "p_is": round(p_is, 4),
                     "derived_bl": derived, "prod_bl": prod, "n_test": n_test, "verdict": verdict})
        print(f"{sym:4} base={base_test:+.3f} derived(OOS)={derived_test:+.3f} ({d_is:+.3f}) "
              f"prod={prod_test:+.3f} ({p_is:+.3f}) n={n_test:3d} {verdict} | 重推={derived}")
    n_gen = sum(1 for r in rows if r["verdict"].startswith("✅"))
    n_neg = sum(1 for r in rows if r["verdict"].startswith("⚠️"))
    print("=" * 92)
    print(f"品种数={len(rows)}  逐品种判定：泛化✅={n_gen}  退化⚠️={n_neg}  持平➖={len(rows) - n_gen - n_neg}")
    # ── 合并统计（关键：逐品种判定的样本量不足以定论）──
    pb = agg_base[0] / agg_base[1] if agg_base[1] else 0.0
    pd_ = agg_derived[0] / agg_derived[1] if agg_derived[1] else 0.0
    pp = agg_prod[0] / agg_prod[1] if agg_prod[1] else 0.0
    print(f"【合并】base(无黑名单)={pb:+.4f}R   derived(OOS)={pd_:+.4f}R   prod={pp:+.4f}R   n={agg_base[1]}")
    print(f"  derived − base = {pd_ - pb:+.4f}R     prod − base = {pp - pb:+.4f}R")
    p = _sign_test(win_d, win_b)
    if p is not None:
        print(f"  符号检验（derived vs base）p ≈ {p:.3f}"
              + ("  → 无统计证据，不得据此改动生产配置" if p > 0.05 else "  → 方向显著"))
    print("  ⚠️ 逐品种判定仅供参考：单品种测试集成交常为个位数（rb n=6），不具统计意义。")
    print(f"耗时 {time.time() - t0:.0f}s")
    out = "/tmp/ps_oos_reval_result.json"
    with open(out, "w") as f:
        json.dump({"rows": rows, "pooled": {"base": pb, "derived": pd_, "prod": pp},
                   "sign_test_p": p}, f, ensure_ascii=False, indent=2)
    print(f"结果写入 {out}")


if __name__ == "__main__":
    main()
