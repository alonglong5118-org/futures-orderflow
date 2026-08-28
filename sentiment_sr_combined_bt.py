"""情绪硬过滤 + SR放宽止损 组合回测验证

对比 4 种模式：
  1. base     : 纯 ATR + 无情绪过滤（基准）
  2. sentiment: 只开情绪硬过滤
  3. sr_widen : 只开 SR 放宽止损
  4. combined : 情绪 + SR 都开（当前 live 配置）
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


def build_sentiment_daily(df):
    """从历史日线构建每日情绪快照（简化版，用滚动窗口模拟）。

    真实 live 是全市场实时计算，这里用单品种自身的涨跌和动能近似，
    主要是验证硬过滤的方向性影响，绝对精度次要。
    """
    sentiment_daily = {}
    n = len(df)
    for i in range(60, n):
        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")

        # 简化版情绪：用 20 日涨跌幅 + 波动率位置
        chg_20d = (float(df["close"].iloc[i]) / float(df["close"].iloc[max(0, i - 20)]) - 1) * 100
        # 映射到 0-100 分
        score = 50 + chg_20d * 3
        score = max(0, min(100, score))

        # 分档
        if score >= 80:
            band, label = "extreme_greed", "极度贪婪"
        elif score >= 65:
            band, label = "greed", "贪婪"
        elif score <= 20:
            band, label = "extreme_fear", "极度恐惧"
        elif score <= 35:
            band, label = "fear", "恐惧"
        else:
            band, label = "neutral", "中性"

        sentiment_daily[date_str] = {
            "score": round(score, 1),
            "band": band,
            "label": label,
        }
    return sentiment_daily


def walk_forward_combined(symbol, mode="base", tail=400, min_bars=60, cooldown_bars=5):
    """
    mode: base / sentiment / sr_widen / combined
    """
    df = load_daily(symbol)
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    df = df.tail(tail)
    if len(df) < min_bars + 20:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}

    # 构建每日情绪
    use_sentiment = mode in ("sentiment", "combined")
    sentiment_daily = build_sentiment_daily(df) if use_sentiment else {}

    use_sr = mode in ("sr_widen", "combined")

    n = len(df)
    sp = DEFAULT_CONFIG["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]
    trades = []
    i = min_bars
    last_trade_i = -999

    while i < n - 1:
        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        current_price = float(df["close"].iloc[i])

        # 情绪
        sent_band = None
        if use_sentiment and date_str in sentiment_daily:
            sent_band = sentiment_daily[date_str]["band"]

        # SR
        sr_result = None
        if use_sr:
            try:
                sr_result = sra.analyze(hist, current_price)
            except Exception:
                pass

        try:
            pipe = pipeline(symbol, hist, None, DEFAULT_CONFIG, date=date_str, sentiment_label=sent_band)
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

            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], DEFAULT_CONFIG, sr_result=sr_result)
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
                    "sentiment": sent_band,
                    "sr_note": ep.get("sr_note", ""),
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
        "avg_win": round(avg_win, 3),
        "avg_lose": round(avg_lose, 3),
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=400)
    args = parser.parse_args()

    symbols = list(SYMBOLS.keys())
    modes = ["base", "sentiment", "sr_widen", "combined"]
    mode_labels = {
        "base": "基准",
        "sentiment": "情绪硬过滤",
        "sr_widen": "SR放宽止损",
        "combined": "情绪+SR组合",
    }

    print(f"情绪 + SR 放宽止损 组合回测验证 · {len(symbols)} 品种 · tail={args.tail}")
    print("=" * 100)

    all_results = {m: [] for m in modes}

    for sym in tqdm(symbols, desc="回测中"):
        for mode in modes:
            try:
                r = walk_forward_combined(sym, mode=mode, tail=args.tail)
                all_results[mode].append(r)
            except Exception as e:
                all_results[mode].append({"symbol": sym, "trades": 0, "note": str(e)})

    # 汇总
    print()
    print("=" * 100)
    print(
        f"{'模式':<16} {'有效':>4} {'提升':>4} {'下降':>4} {'持平':>4} "
        f"{'expR':>8} {'变化%':>8} {'胜率':>6} {'盈亏比':>6}"
    )
    print("-" * 100)

    base_valid = [r for r in all_results["base"] if "expR" in r]
    base_avg = sum(r["expR"] for r in base_valid) / len(base_valid)
    base_wr = sum(r["win_rate"] for r in base_valid) / len(base_valid)
    base_rr = sum(r["rr_ratio"] for r in base_valid) / len(base_valid)
    base_map = {r["symbol"]: r for r in base_valid if "expR" in r}

    print(
        f"{'基准':<16} {len(base_valid):>4}    0    0 {len(base_valid):>4} "
        f"{base_avg:>+8.4f}   +0.0%  {base_wr:>5.1%}  {base_rr:>5.2f}"
    )

    summary = {
        "base": {
            "avg_expR": round(base_avg, 4),
            "count": len(base_valid),
            "avg_win_rate": round(base_wr, 3),
            "avg_rr_ratio": round(base_rr, 3),
        }
    }

    for mode in modes[1:]:
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
            f"{label:<16} {len(valid):>4} {improved:>4} {worsened:>4} {unchanged:>4} "
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

    # 协同效应分析：combined - (sentiment + sr_widen - base)
    sent_avg = summary["sentiment"]["avg_expR"]
    sr_avg = summary["sr_widen"]["avg_expR"]
    combined_avg = summary["combined"]["avg_expR"]
    expected_add = sent_avg + sr_avg - base_avg
    synergy = combined_avg - expected_add
    synergy_pct = (synergy / abs(expected_add) * 100) if expected_add != 0 else 0

    print()
    print("=" * 100)
    print("【协同效应分析】")
    print(f"  情绪单独提升:   {sent_avg - base_avg:+.4f} ({summary['sentiment']['delta_pct']:+.1f}%)")
    print(f"  SR放宽单独提升: {sr_avg - base_avg:+.4f} ({summary['sr_widen']['delta_pct']:+.1f}%)")
    print(f"  线性叠加预期:   {expected_add - base_avg:+.4f} (简单相加)")
    print(f"  实际组合提升:   {combined_avg - base_avg:+.4f} ({summary['combined']['delta_pct']:+.1f}%)")
    print(f"  协同效应:       {synergy:+.4f} ({synergy_pct:+.1f}%)")
    if synergy > 0.001:
        print("  → 正协同：1 + 1 > 2，组合效果比简单叠加更好")
    elif synergy < -0.001:
        print("  → 负协同：1 + 1 < 2，两者有重叠/抵消")
    else:
        print("  → 近似线性叠加，两者独立起作用")

    # 板块分析
    print()
    print("【各板块组合效果】")
    from collections import defaultdict

    groups = defaultdict(list)
    for sym in base_map:
        grp = SYMBOLS.get(sym, {}).get("group", "其他")
        groups[grp].append(sym)

    print(f"{'板块':<8} {'品种数':>4} {'基准expR':>10} {'情绪%':>8} {'SR%':>8} {'组合%':>8} {'协同':>8}")
    print("-" * 70)

    group_data = {}
    for grp in sorted(groups.keys()):
        syms = groups[grp]
        base_g = [base_map[s]["expR"] for s in syms if s in base_map]
        sent_g = [
            next((r["expR"] for r in all_results["sentiment"] if r["symbol"] == s and "expR" in r), 0) for s in syms
        ]
        sr_g = [next((r["expR"] for r in all_results["sr_widen"] if r["symbol"] == s and "expR" in r), 0) for s in syms]
        comb_g = [
            next((r["expR"] for r in all_results["combined"] if r["symbol"] == s and "expR" in r), 0) for s in syms
        ]

        if not base_g:
            continue
        b_avg = float(np.mean(base_g))
        s_avg = float(np.mean(sent_g))
        sr_avg_g = float(np.mean(sr_g))
        c_avg = float(np.mean(comb_g))

        s_pct = (s_avg - b_avg) / abs(b_avg) * 100 if b_avg != 0 else 0
        sr_pct = (sr_avg_g - b_avg) / abs(b_avg) * 100 if b_avg != 0 else 0
        c_pct = (c_avg - b_avg) / abs(b_avg) * 100 if b_avg != 0 else 0
        syn = c_avg - (s_avg + sr_avg_g - b_avg)

        print(f"{grp:<8} {len(syms):>4} {b_avg:>+10.4f} {s_pct:>+7.1f}% {sr_pct:>+7.1f}% {c_pct:>+7.1f}% {syn:>+7.3f}")

        group_data[grp] = {
            "count": len(syms),
            "base_expR": round(b_avg, 4),
            "sentiment_delta_pct": round(s_pct, 1),
            "sr_widen_delta_pct": round(sr_pct, 1),
            "combined_delta_pct": round(c_pct, 1),
            "synergy": round(syn, 4),
        }

    # 保存
    out = {
        "summary": summary,
        "synergy": {
            "sentiment_only": round(sent_avg - base_avg, 4),
            "sr_widen_only": round(sr_avg - base_avg, 4),
            "expected_additive": round(expected_add - base_avg, 4),
            "actual_combined": round(combined_avg - base_avg, 4),
            "synergy": round(synergy, 4),
            "synergy_pct": round(synergy_pct, 1),
        },
        "by_group": group_data,
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/sentiment_sr_combined_bt.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存到 logs/sentiment_sr_combined_bt.json")


if __name__ == "__main__":
    main()
