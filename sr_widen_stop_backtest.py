"""SR 放宽止损验证

反向思路：用 SR 位把止损放宽到更外侧的支撑/压力位，
避免 ATR 止损太近被假突破扫掉。

对比模式：
  - none: 纯 ATR 基准
  - tighten: 收紧止损（当前方案，已验证有害）
  - widen: 放宽止损（新方案）
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


def widen_stop_with_sr(exit_dict, sr_result, direction, entry_price):
    """用 SR 位放宽止损（把止损移到更外侧的支撑/压力位）。

    做多：找 entry 下方最近的支撑位，如果它比 ATR 止损更远 → 放宽止损
    做空：找 entry 上方最近的压力位，如果它比 ATR 止损更远 → 放宽止损

    约束：最多放宽到 2×ATR 止损，避免止损过大。
    """
    if not sr_result or not sr_result.get("levels"):
        return exit_dict

    adjusted = dict(exit_dict)
    orig_stop = exit_dict.get("stop", 0)
    stop_dist = exit_dict.get("stop_dist", 0)
    max_widen_dist = stop_dist * 2.0  # 最多放宽到 2R

    if direction > 0:  # 做多
        ns = sr_result.get("nearest_support")
        if ns and stop_dist > 0:
            sr_stop = ns["price"]
            sr_dist = entry_price - sr_stop
            # SR 支撑位比 ATR 止损更远（更靠下），且不超过 2R
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


def walk_forward_sr_mode(symbol, mode="none", tail=400, min_bars=60, cooldown_bars=5):
    """
    mode: none / tighten / widen
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

            # 基础出场
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], DEFAULT_CONFIG, sr_result=None)

            # SR 调整
            if mode != "none" and sr_result and sr_result.get("levels"):
                orig = {"stop": ep["stop"], "t1": ep["t1"], "t2": ep["t2"], "stop_dist": ep["stop_dist"]}
                if mode == "tighten":
                    adj = sra.adjust_exit_plan(orig, sr_result, dir_T, entry)
                    if adj.get("sr_stop"):
                        ep["stop"] = adj["stop"]
                        ep["stop_dist"] = adj["stop_dist"]
                elif mode == "widen":
                    adj = widen_stop_with_sr(orig, sr_result, dir_T, entry)
                    if adj.get("sr_stop_widen"):
                        ep["stop"] = adj["stop"]
                        ep["stop_dist"] = adj["stop_dist"]

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

    # 盈亏比：盈利交易平均 R / 亏损交易平均 R（绝对值）
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
        "avg_win": round(avg_win, 3),
        "avg_lose": round(avg_lose, 3),
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=400)
    args = parser.parse_args()

    symbols = list(SYMBOLS.keys())
    modes = ["none", "tighten", "widen"]
    mode_labels = {
        "none": "基准(纯ATR)",
        "tighten": "收紧止损(旧方案)",
        "widen": "放宽止损(新方案)",
    }

    print(f"SR 放宽止损验证 · {len(symbols)} 品种 · tail={args.tail}")
    print("=" * 90)

    all_results = {m: [] for m in modes}

    for sym in tqdm(symbols, desc="回测中"):
        for mode in modes:
            try:
                r = walk_forward_sr_mode(sym, mode=mode, tail=args.tail)
                all_results[mode].append(r)
            except Exception as e:
                all_results[mode].append({"symbol": sym, "trades": 0, "note": str(e)})

    # 汇总
    print()
    print("=" * 90)
    print(
        f"{'模式':<18} {'有效':>4} {'提升':>4} {'下降':>4} {'持平':>4} "
        f"{'expR':>8} {'变化%':>8} {'胜率':>6} {'盈亏比':>6}"
    )
    print("-" * 90)

    base_valid = [r for r in all_results["none"] if "expR" in r]
    base_avg = sum(r["expR"] for r in base_valid) / len(base_valid)
    base_wr = sum(r["win_rate"] for r in base_valid) / len(base_valid)
    base_rr = sum(r["rr_ratio"] for r in base_valid) / len(base_valid)

    summary = {}
    base_map = {r["symbol"]: r for r in base_valid if "expR" in r}

    for mode in modes:
        valid = [r for r in all_results[mode] if "expR" in r]
        avg = sum(r["expR"] for r in valid) / len(valid) if valid else 0
        avg_wr = sum(r["win_rate"] for r in valid) / len(valid)
        avg_rr = sum(r["rr_ratio"] for r in valid) / len(valid)

        improved = worsened = unchanged = 0
        for r in valid:
            if r["symbol"] in base_map:
                d = r["expR"] - base_map[r["symbol"]]["expR"]
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
            f"{label:<18} {len(valid):>4} {improved:>4} {worsened:>4} {unchanged:>4} "
            f"{avg:>+8.4f} {delta_pct:>+7.1f}%  {avg_wr:>5.1%}  {avg_rr:>5.2f}"
        )

        summary[mode] = {
            "count": len(valid),
            "improved": improved,
            "worsened": worsened,
            "unchanged": unchanged,
            "avg_expR": round(avg, 4),
            "delta_pct": round(delta_pct, 1),
            "avg_win_rate": round(avg_wr, 3),
            "avg_rr_ratio": round(avg_rr, 3),
        }

    # 板块分析
    print()
    print("【按板块分析 - 放宽止损】")
    groups = {}
    for r in all_results["widen"]:
        if "expR" not in r:
            continue
        grp = SYMBOLS.get(r["symbol"], {}).get("group", "其他")
        base_r = base_map.get(r["symbol"])
        if not base_r:
            continue
        if grp not in groups:
            groups[grp] = []
        d = r["expR"] - base_r["expR"]
        groups[grp].append(
            {
                "sym": r["symbol"],
                "delta": d,
                "base": base_r["expR"],
                "sr": r["expR"],
                "base_wr": base_r["win_rate"],
                "sr_wr": r["win_rate"],
                "base_rr": base_r["rr_ratio"],
                "sr_rr": r["rr_ratio"],
            }
        )

    for grp in sorted(groups.keys()):
        items = groups[grp]
        avg_d = sum(x["delta"] for x in items) / len(items)
        avg_base = sum(x["base"] for x in items) / len(items)
        pct = (avg_d / abs(avg_base) * 100) if avg_base != 0 else 0
        imp = sum(1 for x in items if x["delta"] > 0.001)
        wr_chg = (sum(x["sr_wr"] for x in items) / len(items) - sum(x["base_wr"] for x in items) / len(items)) * 100
        rr_chg = sum(x["sr_rr"] for x in items) / len(items) - sum(x["base_rr"] for x in items) / len(items)
        print(
            f"  {grp:<8} {len(items):>2}品种  expR={avg_d:+.4f} ({pct:+.1f}%)  "
            f"胜率{wr_chg:+.1f}%  盈亏比{rr_chg:+.2f}  提升{imp}"
        )

    # 提升 Top 10
    print()
    print("【放宽止损 - 提升 Top 10】")
    widen_valid = [r for r in all_results["widen"] if "expR" in r]
    widen_with_delta = []
    for r in widen_valid:
        if r["symbol"] in base_map:
            d = r["expR"] - base_map[r["symbol"]]["expR"]
            widen_with_delta.append(
                {
                    **r,
                    "delta": d,
                    "delta_pct": (d / abs(base_map[r["symbol"]]["expR"]) * 100)
                    if base_map[r["symbol"]]["expR"] != 0
                    else 0,
                }
            )
    widen_sorted = sorted(widen_with_delta, key=lambda x: -x["delta_pct"])
    for r in widen_sorted[:10]:
        if r["delta"] > 0.001:
            print(
                f"  {r['symbol']:>5} {r['name']:>6}  "
                f"基准 {base_map[r['symbol']]['expR']:+.4f} → 放宽 {r['expR']:+.4f}  "
                f"({r['delta_pct']:+.1f}%)  "
                f"胜率 {base_map[r['symbol']]['win_rate']:.0%}→{r['win_rate']:.0%}  "
                f"盈亏比 {base_map[r['symbol']]['rr_ratio']:.2f}→{r['rr_ratio']:.2f}"
            )

    print()
    print("【放宽止损 - 下降 Top 10】")
    for r in reversed(widen_sorted[-10:]):
        if r["delta"] < -0.001:
            print(
                f"  {r['symbol']:>5} {r['name']:>6}  "
                f"基准 {base_map[r['symbol']]['expR']:+.4f} → 放宽 {r['expR']:+.4f}  "
                f"({r['delta_pct']:+.1f}%)  "
                f"胜率 {base_map[r['symbol']]['win_rate']:.0%}→{r['win_rate']:.0%}  "
                f"盈亏比 {base_map[r['symbol']]['rr_ratio']:.2f}→{r['rr_ratio']:.2f}"
            )

    # 保存
    out = {
        "summary": summary,
        "by_symbol": {},
    }
    for sym, r in base_map.items():
        out["by_symbol"][sym] = {
            "name": r.get("name", ""),
            "base_expR": r["expR"],
            "base_win_rate": r["win_rate"],
            "base_rr_ratio": r["rr_ratio"],
        }
    for mode in modes:
        for r in all_results[mode]:
            if "expR" in r and r["symbol"] in out["by_symbol"]:
                out["by_symbol"][r["symbol"]][f"{mode}_expR"] = r["expR"]
                out["by_symbol"][r["symbol"]][f"{mode}_win_rate"] = r["win_rate"]
                out["by_symbol"][r["symbol"]][f"{mode}_rr_ratio"] = r["rr_ratio"]

    os.makedirs("logs", exist_ok=True)
    with open("logs/sr_widen_stop_bt.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存到 logs/sr_widen_stop_bt.json")


if __name__ == "__main__":
    main()
