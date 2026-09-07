#!/usr/bin/env python3
"""策略配置改动 —— 全品种合并 A/B 检验台（2026-09-07）。

【用途】
任何涉及策略行为的改动（regime 权重、regime_coef、黑名单、阈值……）在合入生产前，
必须在这里跑一次合并 A/B。**禁止用单品种或 tail-500 之类小窗口下结论**。

【为什么必须合并】
单品种样本极少：rb 全历史 4207 根 bar 仅 12 笔成交；`backtest_rb_tail500` 基线只有 1 笔，
expR +1.112 就是那一笔的盈亏。在 1–12 笔上比较 expR 没有统计意义
（2026-09-07 教训：据此误判"rb 从 +1.112 退化到 −0.344"，实为噪声）。

【判据】
  · 合并 expR（按成交数加权）+ 总 R
  · 逐品种胜负 + 符号检验 p 值
  · p > 0.05 → 无证据，默认不改；Δ 显著为负 → 回滚

【内置变体（整改③ 分解）】
  V0 全旧              regime_weights 倾斜 + 旧 regime_coef/bias_hard/fc_offset
  V1 全中性            四项全拉平
  V2 仅簇权重中性      保留 regime_coef 等历史调参
  V3 仅 coef 中性      保留簇权重倾斜
实测（41 品种，2026-09-07）：V0 +0.6140R / V1 +0.2809R(p=0.004) / V2 +0.3882R(p=0.117) / V3 +0.4835R(p=0.028)
→ 四项中性化全部为负贡献，整改③ 已回滚。此脚本用于防止该改动被再次误提。
"""
import copy
import json
import os
import sys
import time
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import four_dim_strategy as fds
from four_dim_strategy import (DEFAULT_CONFIG, DISABLED_SYMBOLS, SYMBOLS, load_daily,
                               walk_forward_backtest)

# ── 历史（旧）参数，作为 A/B 的对照组 ──
OLD_REGIME_W = {
    "趋势": {"ma_break": 1.0, "dma": 1.0, "turtle": 1.0, "donchian": 1.0, "pullback": 1.0,
             "boll": 0.3, "rsi": 0.3, "seasonal": 0.2},
    "震荡": {"ma_break": 0.3, "dma": 0.3, "turtle": 0.3, "donchian": 0.3, "pullback": 0.3,
             "boll": 1.0, "rsi": 1.0, "seasonal": 0.3},
    "波动": {"ma_break": 0.5, "dma": 0.5, "turtle": 0.5, "donchian": 0.5, "pullback": 0.5,
             "boll": 0.2, "rsi": 0.2, "seasonal": 0.1},
}
OLD_REGIME_COEF = {
    "趋势": {"T": 0.85, "conv": 0.90, "stop": 1.0, "cooldown": 300},
    "震荡": {"T": 1.35, "conv": 1.15, "stop": 1.0, "cooldown": 450},
    "波动": {"T": 1.15, "conv": 1.00, "stop": 1.2, "cooldown": 300},
}
OLD_BIAS_HARD = {"趋势": 60, "波动": 65, "震荡": 70}
OLD_FC_OFFSET = {"趋势": 0, "波动": 5, "震荡": 10}
NEW_BIAS_HARD = {"趋势": 65, "波动": 65, "震荡": 65}
# 中性侧 regime_coef：三档拉平（★必须显式写，不能依赖 DEFAULT_CONFIG 的当前值）
NEW_REGIME_COEF = {rg: {"T": 1.0, "conv": 1.0, "stop": 1.0, "cooldown": 300}
                   for rg in ("趋势", "震荡", "波动")}

VARIANTS = {
    "V0 全旧": dict(w_old=True, c_old=True),
    "V1 全中性": dict(w_old=False, c_old=False),
    "V2 仅簇权重中性": dict(w_old=False, c_old=True),
    "V3 仅coef中性": dict(w_old=True, c_old=False),
}


def _old_w(regime):
    d = OLD_REGIME_W.get(regime)
    return dict(d) if d is not None else {k: 0.5 for k in fds.STRATS}


def _neutral_w(regime):
    """中性簇权重：所有策略恒 1.0。
    ★必须显式安装：不能靠"不打补丁就用模块默认"，因为模块 regime_weights
      会随生产配置变化（2026-09-07 踩过：回滚后模块变回旧倾斜版，
      导致"中性"变体实际跑的是旧配置，四个变体结果全部相同，A/B 静默失效）。"""
    return {k: 1.0 for k in fds.STRATS}


def run(name, w_old, c_old):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["strat_blacklist"] = {}  # 双侧均关黑名单，隔离 regime 效应
    if c_old:
        cfg["regime_coef"] = copy.deepcopy(OLD_REGIME_COEF)
        cfg["bias_hard_by_regime"] = dict(OLD_BIAS_HARD)
        cfg.setdefault("bias_synthesis", {})["fc_hard_regime_offset"] = dict(OLD_FC_OFFSET)
    else:
        cfg["regime_coef"] = copy.deepcopy(NEW_REGIME_COEF)
        cfg["bias_hard_by_regime"] = dict(NEW_BIAS_HARD)
        cfg.setdefault("bias_synthesis", {})["fc_hard_regime_offset"] = {"趋势": 0, "波动": 0, "震荡": 0}
    saved = fds.regime_weights
    # 两侧都显式安装，不依赖模块默认值
    fds.regime_weights = _old_w if w_old else _neutral_w
    fds._CLUSTER_WEIGHTS_BASE = fds._precompute_cluster_base()
    tot, n, per = 0.0, 0, {}
    try:
        for sym in SYMBOLS:
            if sym in DISABLED_SYMBOLS:
                continue
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=cfg, df_in=df)
            ti = int(r.get("trades", 0))
            if ti == 0:
                continue
            e = float(r.get("expR", 0.0))
            tot += e * ti
            n += ti
            per[sym] = round(e, 4)
    finally:
        fds.regime_weights = saved
        fds._CLUSTER_WEIGHTS_BASE = fds._precompute_cluster_base()
    return (tot / n if n else 0.0), tot, n, per


def _sign_test(w_a, w_b):
    m = w_a + w_b
    if m == 0:
        return None
    k = min(w_a, w_b)
    return min(2 * sum(comb(m, i) for i in range(k + 1)) / (2 ** m), 1.0)


def main():
    t0 = time.time()
    print("策略配置 A/B（全品种合并；单品种样本极小，禁止逐品种下结论）")
    print("=" * 92)
    res = {}
    for name, kw in VARIANTS.items():
        e, tot, n, per = run(name, **kw)
        res[name] = {"expR": e, "totR": tot, "n": n, "per": per}
        print(f"  {name:20} 合并 expR = {e:+.4f}R   总 R = {tot:+.2f}R   n = {n}")
    print("-" * 92)
    base = res["V0 全旧"]
    for name in ["V1 全中性", "V2 仅簇权重中性", "V3 仅coef中性"]:
        r = res[name]
        syms = set(base["per"]) & set(r["per"])
        w_new = sum(1 for s in syms if r["per"][s] > base["per"][s])
        w_old = sum(1 for s in syms if base["per"][s] > r["per"][s])
        p = _sign_test(w_new, w_old)
        print(f"  {name:20} Δ vs V0 = {r['expR'] - base['expR']:+.4f}R   "
              f"逐品种 新优 {w_new} / 旧优 {w_old}" + (f"   p ≈ {p:.3f}" if p is not None else ""))
    print(f"耗时 {time.time() - t0:.0f}s")
    out = "/tmp/regime_ab_result.json"
    with open(out, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"结果写入 {out}")


if __name__ == "__main__":
    main()
