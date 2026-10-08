"""SR 止损/止盈微调回测验证

对比：启用 SR 位调整出场 vs 纯 ATR 出场
验证维度：expR、胜率、盈亏比、最大回撤
"""

import json
import math
import os
import sys

import numpy as np
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sr_analyzer as sra
from four_dim_strategy import (
    _FALLBACK_SPEC,
    DEFAULT_CONFIG,
    ROLL_GAP_MULT,
    ROLL_GAP_PCT,
    SYMBOLS,
    exit_plan,
    get_slip_pts,
    load_daily,
    pipeline,
    risk_gate,
    strat_atr,
)


def walk_forward_with_sr(
    symbol, cfg=DEFAULT_CONFIG, min_bars=60, window=300, tail=None, cooldown_bars=5, sr_enabled=True, df_in=None
):
    """带 SR 出场微调的 walk-forward 回测。"""
    df = df_in if df_in is not None else load_daily(symbol)
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    if tail and df_in is None:
        df = df.tail(tail)
    if len(df) < min_bars + 20:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}

    n = len(df)
    sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]
    trades = []
    roll_skipped = 0
    i = min_bars
    last_trade_i = -999

    while i < n - 1:
        hist = df.iloc[: i + 1]
        current_price = float(df["close"].iloc[i])

        # 计算 SR 位
        sr_result = None
        if sr_enabled:
            try:
                sr_result = sra.analyze(hist, current_price)
            except Exception:
                sr_result = None

        try:
            pipe = pipeline(symbol, hist, None, cfg)
        except Exception:
            i += 1
            continue

        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry = float(df["open"].iloc[i + 1])
            atr_val = strat_atr(hist).iloc[-1]
            if atr_val <= 0 or math.isnan(atr_val):
                i += 1
                continue
            rg = risk_gate(symbol, entry, atr_val, cfg)
            if not rg["passed"]:
                i += 1
                continue
            dir_T = pipe["dir_T"]

            # 关键：传入 sr_result 调整出场位
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], cfg, sr_result=sr_result)
            sd = ep["stop_dist"]

            # 出场模拟
            exit_price, reason = None, ""
            tail_active, tail_stop = False, None
            for j in range(i + 1, n):
                hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
                if j > i + 1:
                    prev_close = float(df["close"].iloc[j - 1])
                    gap = abs(float(df["open"].iloc[j]) - prev_close)
                    if gap > max(ROLL_GAP_PCT * prev_close, ROLL_GAP_MULT * sd):
                        roll_skipped += 1
                        continue
                if tail_active:
                    if dir_T > 0:
                        if lo <= tail_stop:
                            exit_price, reason = tail_stop, "尾仓离场"
                            break
                        tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
                    else:
                        if hi >= tail_stop:
                            exit_price, reason = tail_stop, "尾仓离场"
                            break
                        tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
                    continue
                if dir_T > 0:
                    if lo <= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"
                        break
                    if hi >= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]
                            continue
                        exit_price, reason = ep["t2"], "止盈2R"
                        break
                else:
                    if hi >= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"
                        break
                    if lo <= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]
                            continue
                        exit_price, reason = ep["t2"], "止盈2R"
                        break
            if exit_price is None:
                exit_price, reason = float(df["close"].iloc[-1]), "期末平"
            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R

            # 记录 SR 调整情况
            sr_adj_note = ep.get("sr_note", "")
            sr_stop_used = "sr_stop" in sr_adj_note or "止损调至" in sr_adj_note
            sr_t1_used = "sr_t1" in sr_adj_note or "T1调至" in sr_adj_note

            trades.append(
                {
                    "dir": dir_T,
                    "R": round(R, 3),
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "entry_date": df.index[i + 1],
                    "F": pipe["F"],
                    "T_D": pipe["T_D"],
                    "C": pipe["C"],
                    "sr_stop_used": sr_stop_used,
                    "sr_t1_used": sr_t1_used,
                    "sr_adjusted": sr_stop_used or sr_t1_used,
                    "stop_dist": round(sd, 2),
                }
            )
            last_trade_i = i
            i = j + 1 if exit_price is not None else i + 1
            continue
        i += 1

    if not trades:
        return {"symbol": symbol, "trades": 0, "note": "无触发信号", "roll_skipped": roll_skipped}

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    by_regime = {}
    for t in trades:
        by_regime.setdefault(t["regime"], []).append(t["R_adj"])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    # SR 调整统计
    sr_adjusted = [t for t in trades if t["sr_adjusted"]]
    sr_stop_only = [t for t in trades if t["sr_stop_used"] and not t["sr_t1_used"]]
    sr_t1_only = [t for t in trades if t["sr_t1_used"] and not t["sr_stop_used"]]
    sr_both = [t for t in trades if t["sr_stop_used"] and t["sr_t1_used"]]

    def _avg(lst):
        return round(float(np.mean([t["R_adj"] for t in lst])), 4) if lst else 0

    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "trades": len(trades),
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades_detail": trades,
        "by_regime": {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()},
        "exit_reasons": reasons,
        "roll_skipped": roll_skipped,
        "sr_stats": {
            "adjusted_count": len(sr_adjusted),
            "adjusted_pct": round(len(sr_adjusted) / len(trades) * 100, 1) if trades else 0,
            "stop_only_count": len(sr_stop_only),
            "t1_only_count": len(sr_t1_only),
            "both_count": len(sr_both),
            "adjusted_expR": _avg(sr_adjusted),
            "unadjusted_expR": _avg([t for t in trades if not t["sr_adjusted"]]),
        },
    }


def compare_symbol(symbol, tail=400):
    """对比单个品种的 SR 启用/禁用效果。"""
    base = walk_forward_with_sr(symbol, tail=tail, sr_enabled=False)
    sr = walk_forward_with_sr(symbol, tail=tail, sr_enabled=True)

    if base["trades"] == 0:
        return {"symbol": symbol, "note": "基准无交易"}

    base_expR = base["expR"]
    sr_expR = sr["expR"]
    delta = sr_expR - base_expR
    delta_pct = (delta / abs(base_expR) * 100) if base_expR != 0 else float("inf")

    return {
        "symbol": symbol,
        "name": base.get("name", ""),
        "base_trades": base["trades"],
        "base_expR": base_expR,
        "base_win_rate": base["win_rate"],
        "sr_trades": sr["trades"],
        "sr_expR": sr_expR,
        "sr_win_rate": sr["win_rate"],
        "delta": round(delta, 4),
        "delta_pct": round(delta_pct, 1),
        "sr_stats": sr.get("sr_stats", {}),
        "improved": delta > 0.001,
        "worsened": delta < -0.001,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=400)
    parser.add_argument("--symbols", type=str, default=None, help="逗号分隔的品种列表，默认全市场")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = list(SYMBOLS.keys())

    print(f"SR 出场微调回测验证 · {len(symbols)} 个品种 · tail={args.tail}")
    print("=" * 80)

    results = []
    for sym in tqdm(symbols, desc="回测中"):
        try:
            r = compare_symbol(sym, tail=args.tail)
            results.append(r)
        except Exception as e:
            results.append({"symbol": sym, "note": f"错误: {e}"})

    # 过滤有效结果
    valid = [r for r in results if "base_expR" in r]
    improved = [r for r in valid if r["improved"]]
    worsened = [r for r in valid if r["worsened"]]
    unchanged = [r for r in valid if not r["improved"] and not r["worsened"]]

    avg_base = sum(r["base_expR"] for r in valid) / len(valid) if valid else 0
    avg_sr = sum(r["sr_expR"] for r in valid) / len(valid) if valid else 0
    delta = avg_sr - avg_base
    delta_pct = (delta / abs(avg_base) * 100) if avg_base != 0 else 0

    # SR 调整统计汇总
    total_adj = sum(r["sr_stats"].get("adjusted_count", 0) for r in valid)
    total_trades = sum(r["base_trades"] for r in valid)

    print()
    print("=" * 80)
    print("【汇总】")
    print(f"  有效品种: {len(valid)}")
    print(f"  提升: {len(improved)}  下降: {len(worsened)}  持平: {len(unchanged)}")
    print(f"  基准平均 expR: {avg_base:+.4f}")
    print(f"  SR调整 expR:   {avg_sr:+.4f}")
    print(f"  变化: {delta:+.4f} ({delta_pct:+.1f}%)")
    print(
        f"  SR调整交易占比: {total_adj}/{total_trades} ({total_adj / total_trades * 100:.1f}%)" if total_trades else ""
    )

    print()
    print("【提升 Top 10】")
    improved_sorted = sorted(valid, key=lambda x: -x["delta_pct"])
    for r in improved_sorted[:10]:
        if r["improved"]:
            adj = r["sr_stats"]
            print(
                f"  {r['symbol']:>5} {r['name']:>6}  "
                f"基准 {r['base_expR']:+.4f} → SR {r['sr_expR']:+.4f}  "
                f"({r['delta_pct']:+.1f}%)  "
                f"调整占比 {adj['adjusted_pct']}%"
            )

    print()
    print("【下降 Top 10】")
    worsened_sorted = sorted(valid, key=lambda x: x["delta_pct"])
    for r in worsened_sorted[:10]:
        if r["worsened"]:
            adj = r["sr_stats"]
            print(
                f"  {r['symbol']:>5} {r['name']:>6}  "
                f"基准 {r['base_expR']:+.4f} → SR {r['sr_expR']:+.4f}  "
                f"({r['delta_pct']:+.1f}%)  "
                f"调整占比 {adj['adjusted_pct']}%"
            )

    # 保存
    out = {
        "summary": {
            "count": len(valid),
            "improved": len(improved),
            "worsened": len(worsened),
            "unchanged": len(unchanged),
            "avg_base_expR": round(avg_base, 4),
            "avg_sr_expR": round(avg_sr, 4),
            "delta": round(delta, 4),
            "delta_pct": round(delta_pct, 1),
            "sr_adjusted_pct": round(total_adj / total_trades * 100, 1) if total_trades else 0,
        },
        "results": [{k: v for k, v in r.items() if k != "trades_detail"} for r in results],
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/sr_exit_adjust_bt.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n结果已保存到 logs/sr_exit_adjust_bt.json")


if __name__ == "__main__":
    main()
