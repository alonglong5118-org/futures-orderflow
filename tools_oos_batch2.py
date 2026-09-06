#!/usr/bin/env python3
"""第二批品种级簇权重 OOS 验证（P2 第二批，2026-08-31）。

候选来源：backtest_strategy_labels.json 标签分布（23 个分化品种中筛出 7 个单簇证据最干净的）。
验证纪律：5 折走步法 OOS，改善>0 且胜率 >=60% 才可写入。
"""

import copy
import sys

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

# 候选配置：symbol -> {regime: {"cluster_w": {...}}}
CANDIDATES = {
    "TA": {"趋势": {"mean": 0.3}, "震荡": {"mean": 0.3}, "波动": {"mean": 0.3}},
    "eg": {"趋势": {"trend": 0.3, "mean": 2.0}, "震荡": {"trend": 0.3, "mean": 2.0}, "波动": {"trend": 0.3, "mean": 2.0}},
    "eb": {"趋势": {"trend": 0.3, "mean": 2.0}, "震荡": {"trend": 0.3, "mean": 2.0}, "波动": {"trend": 0.3, "mean": 2.0}},
    "bu": {"趋势": {"mean": 0.3}, "震荡": {"mean": 0.3}, "波动": {"mean": 0.3}},
    "UR": {"趋势": {"mean": 0.3}, "震荡": {"mean": 0.3}, "波动": {"mean": 0.3}},
    "ru": {"趋势": {"seasonal": 2.0}, "震荡": {"seasonal": 2.0}, "波动": {"seasonal": 2.0}},
    "SR": {"趋势": {"seasonal": 0.3}, "震荡": {"seasonal": 0.3}, "波动": {"seasonal": 0.3}},
}

N_FOLDS = 5


def oos_walkforward(symbol, cfg, n_folds=N_FOLDS):
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None
    total = len(df)
    fold_size = total // n_folds
    results = []
    for fold in range(n_folds):
        start = fold * fold_size
        end = start + fold_size if fold < n_folds - 1 else total
        df_fold = df.iloc[start:end]
        if len(df_fold) < 80:
            continue
        r = walk_forward_backtest(symbol, cfg=cfg, df_in=df_fold)
        if r.get("trades", 0) > 0:
            results.append(r["expR"])
    return results


def main():
    print(f"第二批品种级簇权重 OOS 验证（{N_FOLDS} 折）")
    print(f"{'='*70}")
    print()

    verdicts = []
    for sym, cluster_w in CANDIDATES.items():
        cfg_test = copy.deepcopy(DEFAULT_CONFIG)
        # 保留已有 T/stop 覆盖，仅注入 cluster_w
        sym_cfg = cfg_test["per_symbol_regime_coef"].get(sym, {})
        for regime, w in cluster_w.items():
            sym_cfg.setdefault(regime, {})
            sym_cfg[regime]["cluster_w"] = w
        cfg_test["per_symbol_regime_coef"][sym] = sym_cfg

        base = oos_walkforward(sym, DEFAULT_CONFIG)
        test = oos_walkforward(sym, cfg_test)
        if not base or not test or len(base) != len(test):
            print(f"{sym}: 数据不足，跳过")
            continue

        base_mean = float(np.mean(base))
        test_mean = float(np.mean(test))
        improves = sum(1 for b, t in zip(base, test) if t > b)
        n = len(base)

        w_desc = ", ".join(f"{k}×{v}" for k, v in next(iter(cluster_w.values())).items())
        print(f"{sym} ({w_desc}):")
        print(f"  基线: {' / '.join(f'{e:+.3f}' for e in base)} → {base_mean:+.4f}")
        print(f"  覆盖: {' / '.join(f'{e:+.3f}' for e in test)} → {test_mean:+.4f}")
        diff = test_mean - base_mean
        ok = diff > 0 and improves / n >= 0.6
        print(f"  改善 {diff:+.4f}, 胜率 {improves}/{n} ({improves/n:.0%}) → {'✓ 达标' if ok else '✗ 不达标'}")
        print()
        verdicts.append({"symbol": sym, "w": cluster_w, "base": base_mean, "test": test_mean,
                         "diff": diff, "wins": improves, "n": n, "ok": ok})

    print(f"{'='*70}")
    passed = [v for v in verdicts if v["ok"]]
    failed = [v for v in verdicts if not v["ok"]]
    print(f"达标 {len(passed)}: {', '.join(v['symbol'] for v in passed)}")
    fail_desc = ", ".join("%s(%+.3f,%d/%d)" % (v["symbol"], v["diff"], v["wins"], v["n"]) for v in failed)
    print(f"不达标 {len(failed)}: {fail_desc}")


if __name__ == "__main__":
    main()
