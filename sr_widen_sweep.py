"""SR 放宽止损参数扫描

测试不同最大放宽倍数：1.2R / 1.5R / 1.8R / 2.0R / 2.5R / 3.0R
找全局最优参数 + 各板块最优参数
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


def widen_stop_with_sr(exit_dict, sr_result, direction, entry_price, max_mult=2.0):
    """用 SR 位放宽止损。max_mult = 最大放宽倍数（相对 ATR 止损）"""
    if not sr_result or not sr_result.get("levels"):
        return exit_dict

    adjusted = dict(exit_dict)
    stop_dist = exit_dict.get("stop_dist", 0)
    max_widen_dist = stop_dist * max_mult

    if direction > 0:  # 做多
        ns = sr_result.get("nearest_support")
        if ns and stop_dist > 0:
            sr_stop = ns["price"]
            sr_dist = entry_price - sr_stop
            if sr_dist > stop_dist and sr_dist <= max_widen_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop_widen"] = True

    elif direction < 0:  # 做空
        nr = sr_result.get("nearest_resistance")
        if nr and stop_dist > 0:
            sr_stop = nr["price"]
            sr_dist = sr_stop - entry_price
            if sr_dist > stop_dist and sr_dist <= max_widen_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop_widen"] = True

    return adjusted


def walk_forward_sr_widen(symbol, max_mult=2.0, tail=400, min_bars=60, cooldown_bars=5):
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
        try:
            sr_result = sra.analyze(hist, current_price)
        except Exception:
            pass

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

            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], DEFAULT_CONFIG, sr_result=None)

            if sr_result and sr_result.get("levels"):
                orig = {"stop": ep["stop"], "t1": ep["t1"], "t2": ep["t2"], "stop_dist": ep["stop_dist"]}
                adj = widen_stop_with_sr(orig, sr_result, dir_T, entry, max_mult=max_mult)
                if adj.get("sr_stop_widen"):
                    ep["stop"] = adj["stop"]
                    ep["stop_dist"] = adj["stop_dist"]

            sd = ep["stop_dist"]

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
    win_trades = [r for r in Rs if r > 0]
    lose_trades = [abs(r) for r in Rs if r < 0]
    avg_win = float(np.mean(win_trades)) if win_trades else 0
    avg_lose = float(np.mean(lose_trades)) if lose_trades else 0
    rr_ratio = round(avg_win / avg_lose, 3) if avg_lose > 0 else 0

    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "trades": len(trades),
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "rr_ratio": rr_ratio,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=400)
    args = parser.parse_args()

    symbols = list(SYMBOLS.keys())
    multipliers = [1.2, 1.5, 1.8, 2.0, 2.5, 3.0]

    print(f"SR 放宽止损参数扫描 · {len(symbols)} 品种 · tail={args.tail}")
    print(f"扫描参数: {multipliers}")
    print("=" * 100)

    # 先算基准
    print("计算基准...")
    base_results = {}
    for sym in tqdm(symbols, desc="基准"):
        try:
            df = load_daily(sym)
            if df is None:
                continue
            from four_dim_strategy import walk_forward_backtest

            r = walk_forward_backtest(sym, tail=args.tail)
            if r["trades"] > 0:
                base_results[sym] = r
        except Exception:
            pass

    print(f"基准有效品种: {len(base_results)}")
    base_avg = sum(r["expR"] for r in base_results.values()) / len(base_results)
    print(f"基准平均 expR: {base_avg:+.4f}")

    # 扫描各参数
    all_results = {}
    for mult in multipliers:
        all_results[mult] = {}
        print(f"\n扫描 max_mult={mult}R ...")
        for sym in tqdm(symbols, desc=f"{mult}R"):
            if sym not in base_results:
                continue
            try:
                r = walk_forward_sr_widen(sym, max_mult=mult, tail=args.tail)
                if r["trades"] > 0:
                    all_results[mult][sym] = r
            except Exception:
                pass

    # 汇总表
    print()
    print("=" * 100)
    print(
        f"{'参数':>8} {'有效':>4} {'提升':>4} {'下降':>4} {'持平':>4} "
        f"{'expR':>8} {'变化%':>8} {'胜率':>6} {'盈亏比':>6} {'胜率变化':>8} {'盈亏比变化':>10}"
    )
    print("-" * 100)

    base_wr = sum(r["win_rate"] for r in base_results.values()) / len(base_results)
    # 基准 rr_ratio 也得算
    base_rr_list = []
    for sym, br in base_results.items():
        trades = br.get("trades_detail", [])
        if not trades:
            continue
        rs = [t["R_adj"] for t in trades]
        wins = [r for r in rs if r > 0]
        loses = [abs(r) for r in rs if r < 0]
        if loses and wins:
            base_rr_list.append(float(np.mean(wins)) / float(np.mean(loses)))
    base_rr = float(np.mean(base_rr_list)) if base_rr_list else 0

    print(
        f"{'基准':>8} {len(base_results):>4}    0    0 {len(base_results):>4} "
        f"{base_avg:>+8.4f}   +0.0%  {base_wr:>5.1%}  {base_rr:>5.2f}        —          —"
    )

    summary = {
        "base": {
            "avg_expR": round(base_avg, 4),
            "count": len(base_results),
            "avg_win_rate": round(base_wr, 3),
            "avg_rr_ratio": round(base_rr, 3),
        }
    }

    for mult in multipliers:
        valid = all_results[mult]
        if not valid:
            continue

        avg_expR = sum(r["expR"] for r in valid.values()) / len(valid)
        avg_wr = sum(r["win_rate"] for r in valid.values()) / len(valid)
        avg_rr = sum(r["rr_ratio"] for r in valid.values()) / len(valid)

        improved = worsened = unchanged = 0
        for sym, r in valid.items():
            if sym in base_results:
                d = r["expR"] - base_results[sym]["expR"]
                if d > 0.001:
                    improved += 1
                elif d < -0.001:
                    worsened += 1
                else:
                    unchanged += 1

        delta = avg_expR - base_avg
        delta_pct = (delta / abs(base_avg) * 100) if base_avg != 0 else 0
        wr_chg = (avg_wr - base_wr) * 100
        rr_chg = avg_rr - base_rr

        label = f"{mult}R"
        print(
            f"{label:>8} {len(valid):>4} {improved:>4} {worsened:>4} {unchanged:>4} "
            f"{avg_expR:>+8.4f} {delta_pct:>+7.1f}%  {avg_wr:>5.1%}  {avg_rr:>5.2f}  "
            f"{wr_chg:>+6.1f}%  {rr_chg:>+8.3f}"
        )

        summary[mult] = {
            "count": len(valid),
            "improved": improved,
            "worsened": worsened,
            "unchanged": unchanged,
            "avg_expR": round(avg_expR, 4),
            "delta_pct": round(delta_pct, 1),
            "avg_win_rate": round(avg_wr, 3),
            "avg_rr_ratio": round(avg_rr, 3),
            "wr_chg_pct": round(wr_chg, 1),
            "rr_chg": round(rr_chg, 3),
        }

    # 板块最优参数
    print()
    print("【各板块最优 max_mult】")
    groups = {}
    for sym in base_results:
        grp = SYMBOLS.get(sym, {}).get("group", "其他")
        groups.setdefault(grp, []).append(sym)

    print(f"{'板块':<8} {'品种数':>4} {'最优':>6} {'expR变化':>10} {'胜率变化':>8} {'盈亏比变化':>10}")
    print("-" * 60)

    group_best = {}
    for grp, syms in sorted(groups.items()):
        best_mult = None
        best_delta = -999
        best_data = None
        for mult in multipliers:
            valid_syms = [s for s in syms if s in all_results[mult]]
            if not valid_syms:
                continue
            base_grp_expR = sum(base_results[s]["expR"] for s in valid_syms) / len(valid_syms)
            sr_grp_expR = sum(all_results[mult][s]["expR"] for s in valid_syms) / len(valid_syms)
            delta = sr_grp_expR - base_grp_expR
            if delta > best_delta:
                best_delta = delta
                best_mult = mult
                best_data = {
                    "delta": delta,
                    "count": len(valid_syms),
                    "base_expR": base_grp_expR,
                    "sr_expR": sr_grp_expR,
                }

        if best_data:
            delta_pct = (best_data["delta"] / abs(best_data["base_expR"]) * 100) if best_data["base_expR"] != 0 else 0

            # 胜率和盈亏比变化
            base_wr_g = sum(base_results[s].get("win_rate", 0) for s in syms if s in base_results) / len(
                [s for s in syms if s in base_results]
            )
            sr_wr_g = sum(all_results[best_mult][s]["win_rate"] for s in syms if s in all_results[best_mult]) / len(
                [s for s in syms if s in all_results[best_mult]]
            )
            sr_rr_g = sum(all_results[best_mult][s]["rr_ratio"] for s in syms if s in all_results[best_mult]) / len(
                [s for s in syms if s in all_results[best_mult]]
            )

            wr_chg = (sr_wr_g - base_wr_g) * 100
            # 基准 rr 也估算一下
            base_rr_g_list = []
            for s in syms:
                if s in base_results:
                    trades = base_results[s].get("trades_detail", [])
                    if trades:
                        rs = [t["R_adj"] for t in trades]
                        wins = [r for r in rs if r > 0]
                        loses = [abs(r) for r in rs if r < 0]
                        if loses and wins:
                            base_rr_g_list.append(float(np.mean(wins)) / float(np.mean(loses)))
            base_rr_g = float(np.mean(base_rr_g_list)) if base_rr_g_list else 0
            rr_chg = sr_rr_g - base_rr_g

            print(
                f"{grp:<8} {best_data['count']:>4} {best_mult:>4}R  "
                f"{best_data['delta']:+.4f} ({delta_pct:+.1f}%)  "
                f"{wr_chg:>+5.1f}%  {rr_chg:>+8.3f}"
            )

            group_best[grp] = {
                "best_mult": best_mult,
                "delta_pct": round(delta_pct, 1),
                "count": best_data["count"],
            }

    # 保存
    out = {
        "summary": summary,
        "group_best": group_best,
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/sr_widen_sweep.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n结果已保存到 logs/sr_widen_sweep.json")


if __name__ == "__main__":
    main()
