#!/usr/bin/env python3
"""P-S 策略选择 OOS 重验（2026-09-07）。

问题：v6.3 的 strat_blacklist 是「全样本 ablation + 贪心剔除」得到的（决策 v6.3 / v1.2.1），
属 in-sample。本脚本做严格时间切分 OOS：
  - 前 2/3 历史 = 训练集，贪心重推黑名单（与 v6.3 同法，但只用训练集）
  - 后 1/3 历史 = 测试集（held-out），评估重推黑名单在测试集的 expR
  - 同时报告：生产黑名单在测试集的表现（prod_test），对比「重推 OOS」是否退化

判定：derived_test > base_test → 黑名单泛化（OOS 正）；
      prod_test 明显 > derived_test → 生产黑名单在测试集上过拟合（因拟合时含测试集）；
      derived_test <= base_test → 黑名单不泛化，in-sample 增益是假象。
"""
import copy, json, sys, time
import numpy as np

HERE = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, HERE)
from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest, STRAT_CLUSTERS

ALL_STRATS = [s for members in STRAT_CLUSTERS.values() for s in members]
PROD_BL = DEFAULT_CONFIG.get("strat_blacklist", {})
SYMBOLS = sorted(PROD_BL.keys())
TRAIN_RATIO = 2.0 / 3.0


def wfb_expR(symbol, df, blacklist=None):
    cfg = DEFAULT_CONFIG
    if blacklist:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["strat_blacklist"] = {symbol: list(blacklist)}
    r = walk_forward_backtest(symbol, cfg=cfg, df_in=df)
    return r.get("expR"), r.get("trades", 0)


def greedy_derive(symbol, train_df):
    """在训练集上贪心重推黑名单（与 v6.3 同法）。"""
    base, _ = wfb_expR(symbol, train_df, None)
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


def main():
    t0 = time.time()
    rows = []
    print(f"P-S 策略选择 OOS 重验（{len(SYMBOLS)} 品种，训练 2/3 推导 / 测试 1/3 held-out）")
    print("=" * 92)
    for sym in SYMBOLS:
        df = load_daily(sym)
        if df is None or len(df) < 300:
            print(f"{sym}: 数据不足，跳过"); continue
        k = int(len(df) * TRAIN_RATIO)
        train, test = df.iloc[:k], df.iloc[k:]
        base_test, n_test = wfb_expR(sym, test, None)
        if n_test == 0:
            print(f"{sym}: 测试集无成交，跳过"); continue
        derived = greedy_derive(sym, train)
        derived_test, _ = wfb_expR(sym, test, derived)
        prod = PROD_BL.get(sym, [])
        prod_test, _ = wfb_expR(sym, test, prod) if prod else (base_test, n_test)
        d_is = derived_test - base_test
        p_is = prod_test - base_test
        verdict = "✅泛化" if d_is > 0.02 else ("⚠️退化" if d_is < -0.02 else "➖持平")
        rows.append({"sym": sym, "base_test": round(base_test, 4), "derived_test": round(derived_test, 4),
                     "prod_test": round(prod_test, 4), "d_is": round(d_is, 4), "p_is": round(p_is, 4),
                     "derived_bl": derived, "prod_bl": prod, "n_test": n_test, "verdict": verdict})
        print(f"{sym:4} base={base_test:+.3f} derived(OOS)={derived_test:+.3f} ({d_is:+.3f}) "
              f"prod={prod_test:+.3f} ({p_is:+.3f}) n={n_test:3d} {verdict} | 重推={derived}")
    n_gen = sum(1 for r in rows if r["verdict"].startswith("✅"))
    n_neg = sum(1 for r in rows if r["verdict"].startswith("⚠️"))
    agg_d = float(np.mean([r["d_is"] for r in rows])) if rows else 0
    agg_p = float(np.mean([r["p_is"] for r in rows])) if rows else 0
    print("=" * 92)
    print(f"品种数={len(rows)}  泛化✅={n_gen}  退化⚠️={n_neg}  持平➖={len(rows)-n_gen-n_neg}")
    print(f"平均 OOS 增益(重推)={agg_d:+.4f}R   平均增益(生产黑名单)={agg_p:+.4f}R")
    print(f"结论: {'黑名单整体泛化' if agg_d > 0.02 else ('生产黑名单过拟合风险' if agg_p > agg_d + 0.02 else '增益不稳健/样本不足')}")
    print(f"耗时 {time.time()-t0:.0f}s")
    with open("/tmp/ps_oos_reval_result.json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
