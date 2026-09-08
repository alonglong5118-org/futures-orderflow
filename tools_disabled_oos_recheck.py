#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools_disabled_oos_recheck.py — 被禁品种（DISABLED_SYMBOLS）解禁复查台
====================================================================
背景（2026-09-08 通读发现）：
  DISABLED_SYMBOLS 的判死依据是 calibration_params.json 的 mean_oos，
  但 v1.2.2「28 品种重校准」大改 T_thresh 后，禁用清单未随之重跑，
  导致 calibration 与当前 DEFAULT_CONFIG 实测严重脱节（hc 校准 −0.615 → 实测 +0.24）。
  更关键：AUTO_RECOVER_SYMBOLS 只有 {"hc"}，其余 10 个硬禁品种**永不自动复查**。

本脚本对全部被禁品种做三重判定：
  A. recovery_check 口径（系统自带解禁标准）：tail=250，expR>=0 且 胜率>=0.45 且 n>=10
  B. 真·OOS：数据后 1/3 切片，用当前 DEFAULT_CONFIG 直接评估（配置未在该切片上拟合）
  C. 活跃品种同口径基准线：43 个活跃品种在 B 口径下的 expR 分布，用于判断
     「被禁品种是否达到活跃品种水平」——避免只看 expR>0 就解禁

判定建议（保守，遵循「无证据默认不改」）：
  - 建议解禁需同时满足：A 通过 且 B 的 expR > 0 且 B 的 expR >= 活跃品种中位数
  - 仅 A 通过 / 仅 B 为正 → 标「观察」，不入建议解禁清单

用法:
    python3 tools_disabled_oos_recheck.py                # 全量
    python3 tools_disabled_oos_recheck.py --symbols hc JM  # 指定品种
    python3 tools_disabled_oos_recheck.py --tail 500      # 改 A 口径窗口

输出：控制台报告 + /tmp/disabled_oos_recheck.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (  # noqa: E402
    DEFAULT_CONFIG,
    DISABLED_SYMBOLS,
    SYMBOLS,
    load_daily,
    recovery_check,
    walk_forward_backtest,
)

OUT_JSON = "/tmp/disabled_oos_recheck.json"
# B 口径：后 1/3 作为样本外切片
OOS_FRACTION = 1.0 / 3.0
# C 口径基准：活跃品种也用同样比例切片，保证同口径可比


def _slice_oos(df, frac=OOS_FRACTION):
    """取数据后 frac 比例作为样本外切片。"""
    if df is None or len(df) < 300:
        return None
    n = max(60, int(len(df) * frac))
    return df.tail(n)


def eval_oos(symbol, cfg=DEFAULT_CONFIG, frac=OOS_FRACTION):
    """在样本外切片上评估，返回 dict。"""
    df = load_daily(symbol)
    oos = _slice_oos(df, frac)
    if oos is None:
        return {"symbol": symbol, "ok": False, "note": "数据不足"}
    try:
        r = walk_forward_backtest(symbol, cfg=cfg, df_in=oos)
    except Exception as e:
        return {"symbol": symbol, "ok": False, "note": f"回测异常:{repr(e)[:60]}"}
    if int(r.get("trades", 0)) == 0:
        return {
            "symbol": symbol, "ok": False, "trades": 0,
            "note": "样本外 0 成交（早退）",
        }
    return {
        "symbol": symbol,
        "ok": True,
        "expR": round(float(r.get("expR") or 0.0), 4),
        "win_rate": round(float(r.get("win_rate") or 0.0), 4),
        "trades": int(r.get("trades", 0)),
        "bars": len(oos),
    }


def active_baseline(cfg=DEFAULT_CONFIG, frac=OOS_FRACTION, limit=None):
    """活跃品种在同口径下的 expR 分布，作为解禁基准线。"""
    actives = sorted(s for s in SYMBOLS if s not in DISABLED_SYMBOLS)
    if limit:
        actives = actives[:limit]
    rows = []
    for s in actives:
        r = eval_oos(s, cfg=cfg, frac=frac)
        if r.get("ok"):
            rows.append(r)
    return rows


def main(symbols=None, tail=250, with_baseline=True):
    targets = sorted(symbols) if symbols else sorted(DISABLED_SYMBOLS)
    cfg = DEFAULT_CONFIG

    print("=" * 96)
    print("被禁品种解禁复查  |  口径A=recovery_check(tail=%d)  口径B=真OOS(后1/3切片)" % tail)
    print("=" * 96)

    results = []
    for s in targets:
        a = recovery_check(s, cfg=cfg, tail=tail)
        b = eval_oos(s, cfg=cfg)
        results.append({"symbol": s, "A_recovery": a, "B_oos": b})

    # ── 活跃品种基准线 ──
    base_rows = []
    med = None
    if with_baseline:
        print("\n[基准线] 正在评估活跃品种同口径样本外表现 ...", flush=True)
        base_rows = active_baseline(cfg=cfg)
        if base_rows:
            med = statistics.median([r["expR"] for r in base_rows])
            mean_ = statistics.mean([r["expR"] for r in base_rows])
            pos = sum(1 for r in base_rows if r["expR"] > 0)
            print(
                f"  活跃品种 {len(base_rows)} 个：样本外 expR 中位数 {med:+.4f} / "
                f"均值 {mean_:+.4f} / 正期望 {pos}/{len(base_rows)}"
            )

    # A 口径有效性自检：tail 窗口过短时 min_trades 几乎不可达，recovery_check 会
    # 系统性返回「样本不足」，导致自适应恢复机制形同虚设（2026-09-08 实测：
    # 11 个品种中 10 个因 n<10 无法判定）。此时判定必须以 B 口径为准。
    a_blocked = sum(
        1 for r in results if "样本不足" in str(r["A_recovery"].get("note", ""))
    )
    if results and a_blocked / len(results) >= 0.5:
        print(
            f"\n[警告] A 口径(recovery_check tail={tail}) 有 {a_blocked}/{len(results)} "
            f"个品种因样本不足(n<10)无法判定 —— 该窗口下自适应恢复机制实际失效，\n"
            f"       请以下方 B 口径(真·样本外, 样本量更大)与活跃品种基准线为准。"
        )

    print("\n" + "-" * 96)
    hdr = (
        f"{'品种':<6}{'A:expR':>9}{'A:胜率':>8}{'A:n':>6}{'判定':>10}   "
        f"{'B:expR':>9}{'B:胜率':>8}{'B:n':>6}{'vs基准':>9}   {'建议':>10}"
    )
    print(hdr)
    print("-" * 96)

    verdicts = {}
    for r in results:
        s = r["symbol"]
        a, b = r["A_recovery"], r["B_oos"]
        a_ok = bool(a.get("recover"))
        a_e = a.get("expR")
        a_w = a.get("win_rate")
        a_n = a.get("trades", 0)
        a_txt = "通过" if a_ok else ("样本不足" if "样本不足" in str(a.get("note", "")) else "未过")

        if b.get("ok"):
            b_e, b_w, b_n = b["expR"], b["win_rate"], b["trades"]
            if a_ok and b_e > 0 and (med is None or b_e >= med):
                verdict = "建议解禁"
            elif a_ok or b_e > 0:
                verdict = "观察"
            else:
                verdict = "维持禁用"
        else:
            b_e = b_w = b_n = None
            verdict = "维持禁用"

        verdicts[s] = verdict
        se = f"{a_e:+.4f}" if isinstance(a_e, (int, float)) else "   n/a"
        sw = f"{a_w*100:6.1f}%" if isinstance(a_w, (int, float)) else "   n/a"
        be = f"{b_e:+.4f}" if isinstance(b_e, (int, float)) else "   n/a"
        bw = f"{b_w*100:6.1f}%" if isinstance(b_w, (int, float)) else "   n/a"
        vs = f"{b_e - med:+.4f}" if (isinstance(b_e, (int, float)) and med is not None) else "     n/a"
        print(
            f"{s:<6}{se:>9}{sw:>8}{a_n:>6}{a_txt:>10}   "
            f"{be:>9}{bw:>8}{(b_n if b_n is not None else 0):>6}{vs:>9}   {verdict:>10}"
        )

    print("-" * 96)
    rec = [s for s, v in verdicts.items() if v == "建议解禁"]
    obs = [s for s, v in verdicts.items() if v == "观察"]
    keep = [s for s, v in verdicts.items() if v == "维持禁用"]
    print(f"\n建议解禁({len(rec)}): {rec}")
    print(f"观察({len(obs)}):     {obs}")
    print(f"维持禁用({len(keep)}): {keep}")

    if rec:
        tot_r = tot_n = 0
        for r in results:
            if verdicts[r["symbol"]] == "建议解禁" and r["B_oos"].get("ok"):
                tot_r += r["B_oos"]["expR"] * r["B_oos"]["trades"]
                tot_n += r["B_oos"]["trades"]
        if tot_n:
            print(f"\n建议解禁组合并样本外 expR = {tot_r/tot_n:+.4f}R (n={tot_n})")

    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "tail": tail,
        "oos_fraction": OOS_FRACTION,
        "baseline": {
            "n_active": len(base_rows),
            "median_expR": med,
            "mean_expR": (statistics.mean([r["expR"] for r in base_rows]) if base_rows else None),
            "rows": base_rows,
        },
        "a_blocked": a_blocked,
        "symbols": {r["symbol"]: {**r, "verdict": verdicts[r["symbol"]]} for r in results},
        "verdicts": verdicts,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n明细已写入 {OUT_JSON}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None, help="指定品种，默认全部被禁品种")
    ap.add_argument("--tail", type=int, default=250, help="口径A 的近期窗口（默认250）")
    ap.add_argument("--no-baseline", action="store_true", help="跳过活跃品种基准线（更快）")
    args = ap.parse_args()
    main(symbols=args.symbols, tail=args.tail, with_baseline=not args.no_baseline)
