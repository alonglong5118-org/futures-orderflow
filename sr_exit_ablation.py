"""SR 出场微调消融实验

分别测试：
1. 只调止损（SR 支撑/压力位做更紧止损）
2. 只调 T1（SR 位做第一止盈目标）
3. 两者都调（当前方案）
4. 基准（纯 ATR）
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


def walk_forward_sr_mode(symbol, mode="both", tail=400, min_bars=60, cooldown_bars=5):
    """
    mode:
      - "none": 纯 ATR（基准）
      - "stop_only": 只调止损
      - "t1_only": 只调 T1
      - "both": 都调
    """
    df = load_daily(symbol)
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    df = df.tail(tail)
    if len(df) < min_bars + 20:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}

    n = len(df)
    sp = DEFAULT_CONFIG["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]
    trades = []
    i = min_bars
    last_trade_i = -999

    while i < n - 1:
        hist = df.iloc[: i + 1]
        current_price = float(df["close"].iloc[i])

        sr_result = None
        if mode != "none":
            try:
                sr_result = sra.analyze(hist, current_price)
            except Exception:
                sr_result = None

        try:
            pipe = pipeline(symbol, hist, None, DEFAULT_CONFIG)
        except Exception:
            i += 1
            continue

        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry = float(df["open"].iloc[i + 1])
            atr_val = strat_atr(hist).iloc[-1]
            if atr_val <= 0 or math.isnan(atr_val):
                i += 1
                continue
            rg = risk_gate(symbol, entry, atr_val, DEFAULT_CONFIG)
            if not rg["passed"]:
                i += 1
                continue
            dir_T = pipe["dir_T"]

            # 先算基础出场
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], DEFAULT_CONFIG, sr_result=None)

            # 根据模式选择性调整
            if mode != "none" and sr_result and sr_result.get("levels"):
                orig = {"stop": ep["stop"], "t1": ep["t1"], "t2": ep["t2"], "stop_dist": ep["stop_dist"]}
                adj = sra.adjust_exit_plan(orig, sr_result, dir_T, entry)

                if mode == "stop_only":
                    # 只应用止损调整，T1 保持原样
                    if adj.get("sr_stop"):
                        ep["stop"] = adj["stop"]
                        ep["stop_dist"] = adj["stop_dist"]
                elif mode == "t1_only":
                    # 只应用 T1 调整，止损保持原样
                    if adj.get("sr_t1"):
                        ep["t1"] = adj["t1"]
                elif mode == "both":
                    if adj.get("sr_stop"):
                        ep["stop"] = adj["stop"]
                        ep["stop_dist"] = adj["stop_dist"]
                    if adj.get("sr_t1"):
                        ep["t1"] = adj["t1"]

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
            slip_R = 2 * get_slip_pts(symbol, DEFAULT_CONFIG) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R

            trades.append(
                {
                    "dir": dir_T,
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "stop_dist": round(sd, 2),
                }
            )
            last_trade_i = i
            i = j + 1 if exit_price is not None else i + 1
            continue
        i += 1

    if not trades:
        return {"symbol": symbol, "trades": 0, "note": "无触发信号"}

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]

    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "trades": len(trades),
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=400)
    args = parser.parse_args()

    symbols = list(SYMBOLS.keys())
    modes = ["none", "stop_only", "t1_only", "both"]
    mode_labels = {
        "none": "基准(纯ATR)",
        "stop_only": "只调止损",
        "t1_only": "只调T1",
        "both": "止损+T1都调",
    }

    print(f"SR 出场微调消融实验 · {len(symbols)} 品种 · tail={args.tail}")
    print("=" * 80)

    all_results = {m: [] for m in modes}

    for sym in tqdm(symbols, desc="回测中"):
        for mode in modes:
            try:
                r = walk_forward_sr_mode(sym, mode=mode, tail=args.tail)
                all_results[mode].append(r)
            except Exception as e:
                all_results[mode].append({"symbol": sym, "trades": 0, "note": str(e)})

    # 统计
    print()
    print("=" * 80)
    print(f"{'模式':<16} {'有效':>4} {'提升':>4} {'下降':>4} {'持平':>4} {'平均expR':>10} {'变化%':>10}")
    print("-" * 80)

    base_valid = [r for r in all_results["none"] if "expR" in r]
    base_avg = sum(r["expR"] for r in base_valid) / len(base_valid)

    summary = {}
    for mode in modes:
        valid = [r for r in all_results[mode] if "expR" in r]
        avg = sum(r["expR"] for r in valid) / len(valid) if valid else 0

        # 和基准对比
        improved = 0
        worsened = 0
        unchanged = 0
        base_map = {r["symbol"]: r["expR"] for r in base_valid if "expR" in r}
        for r in valid:
            if r["symbol"] in base_map:
                d = r["expR"] - base_map[r["symbol"]]
                if d > 0.001:
                    improved += 1
                elif d < -0.001:
                    worsened += 1
                else:
                    unchanged += 1

        delta = avg - base_avg
        delta_pct = (delta / abs(base_avg) * 100) if base_avg != 0 else 0

        label = mode_labels[mode]
        print(
            f"{label:<16} {len(valid):>4} {improved:>4} {worsened:>4} {unchanged:>4} {avg:>+10.4f} {delta_pct:>+9.1f}%"
        )

        summary[mode] = {
            "count": len(valid),
            "improved": improved,
            "worsened": worsened,
            "unchanged": unchanged,
            "avg_expR": round(avg, 4),
            "delta_pct": round(delta_pct, 1),
        }

    # 板块分析
    print()
    print("【按板块分析 - 只调止损】")
    groups = {}
    for r in all_results["stop_only"]:
        if "expR" not in r:
            continue
        grp = SYMBOLS.get(r["symbol"], {}).get("group", "其他")
        base_r = next((b for b in base_valid if b["symbol"] == r["symbol"]), None)
        if not base_r:
            continue
        if grp not in groups:
            groups[grp] = []
        d = r["expR"] - base_r["expR"]
        groups[grp].append({"sym": r["symbol"], "delta": d, "base": base_r["expR"], "sr": r["expR"]})

    for grp in sorted(groups.keys()):
        items = groups[grp]
        avg_d = sum(x["delta"] for x in items) / len(items)
        avg_base = sum(x["base"] for x in items) / len(items)
        pct = (avg_d / abs(avg_base) * 100) if avg_base != 0 else 0
        imp = sum(1 for x in items if x["delta"] > 0.001)
        print(f"  {grp:<8} {len(items):>2}品种  delta={avg_d:+.4f} ({pct:+.1f}%)  提升{imp}")

    print()
    print("【按板块分析 - 只调T1】")
    groups_t1 = {}
    for r in all_results["t1_only"]:
        if "expR" not in r:
            continue
        grp = SYMBOLS.get(r["symbol"], {}).get("group", "其他")
        base_r = next((b for b in base_valid if b["symbol"] == r["symbol"]), None)
        if not base_r:
            continue
        if grp not in groups_t1:
            groups_t1[grp] = []
        d = r["expR"] - base_r["expR"]
        groups_t1[grp].append({"sym": r["symbol"], "delta": d, "base": base_r["expR"], "sr": r["expR"]})

    for grp in sorted(groups_t1.keys()):
        items = groups_t1[grp]
        avg_d = sum(x["delta"] for x in items) / len(items)
        avg_base = sum(x["base"] for x in items) / len(items)
        pct = (avg_d / abs(avg_base) * 100) if avg_base != 0 else 0
        imp = sum(1 for x in items if x["delta"] > 0.001)
        print(f"  {grp:<8} {len(items):>2}品种  delta={avg_d:+.4f} ({pct:+.1f}%)  提升{imp}")

    # 保存
    out = {
        "summary": summary,
        "by_symbol": {},
    }
    for r in base_valid:
        out["by_symbol"][r["symbol"]] = {"name": r.get("name", ""), "base_expR": r["expR"]}
    for mode in modes:
        for r in all_results[mode]:
            if "expR" in r and r["symbol"] in out["by_symbol"]:
                out["by_symbol"][r["symbol"]][f"{mode}_expR"] = r["expR"]

    os.makedirs("logs", exist_ok=True)
    with open("logs/sr_exit_ablation.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存到 logs/sr_exit_ablation.json")


if __name__ == "__main__":
    main()
