"""
情绪系统回测验证

预计算每日全市场情绪 → 注入 walk-forward 回测 → 对比带/不带情绪的效果。

用法:
  python3 sentiment_backtest.py --symbol v --tail 400
  python3 sentiment_backtest.py --group 化工 --tail 400
  python3 sentiment_backtest.py --all --tail 400
"""

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np

import sentiment_engine as senteng
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    compute_T,
    load_daily,
    strat_atr,
    walk_forward_backtest,
)

# ────────────────────────────────────────────────────────────
# 1. 预计算每日全市场情绪序列
# ────────────────────────────────────────────────────────────

def compute_daily_sentiment(symbols, tail=400, min_bars=60, window=None):
    """计算每日全市场情绪序列。

    对每一天，收集所有品种的快照（T_D, chg_pct, volume_ratio, atr_pct, regime, group），
    调用 sentiment_engine.compute() 得到当日情绪。

    返回: dict {date_str (YYYYMMDD): {score, label, band, bias, scale}}
    """
    if window is None:
        window = min_bars

    print(f"  [1/2] 预加载数据 ({len(symbols)} 个品种)...")
    # 预加载所有品种数据
    sym_data = {}
    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < min_bars + 10:
            continue
        if tail and len(df) > tail:
            df = df.tail(tail).copy()
        sym_data[sym] = df

    print(f"    有效品种: {len(sym_data)} 个")

    # 找到所有日期的并集（取交集太多缺失，用每个品种自己的索引对齐）
    # 方法：取品种数最多的那个作为基准日期序列
    longest_sym = max(sym_data.keys(), key=lambda s: len(sym_data[s]))
    base_dates = sym_data[longest_sym].index

    print(f"  [2/2] 逐日计算情绪 ({len(base_dates)} 天)...")
    sentiment_daily = {}

    for i, date in enumerate(base_dates):
        date_str = date.strftime("%Y%m%d")
        snapshots = {}

        for sym, df in sym_data.items():
            if date not in df.index:
                continue
            # 找到截至当日的数据（用 .loc 切片）
            hist = df.loc[:date]
            if len(hist) < min_bars:
                continue

            # 计算 T_D 和 regime
            try:
                T_D, regime, _ = compute_T(hist, DEFAULT_CONFIG,
                                           group=SYMBOLS.get(sym, {}).get("group"),
                                           symbol=sym)
            except Exception:
                T_D, regime = 0.0, "未知"

            # 涨跌幅（当日 close vs 前一日 close）
            if len(hist) >= 2:
                chg_pct = float(hist["close"].iloc[-1] / hist["close"].iloc[-2] - 1.0)
            else:
                chg_pct = 0.0

            # 量比（当日成交量 / 20日均量）
            vol = float(hist["volume"].iloc[-1]) if "volume" in hist.columns else 0
            if "volume" in hist.columns and len(hist) >= 20:
                avg_vol = float(hist["volume"].iloc[-20:].mean())
                volume_ratio = vol / avg_vol if avg_vol > 0 else 1.0
            else:
                volume_ratio = 1.0

            # ATR%
            try:
                atr_series = strat_atr(hist)
                atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0
                close = float(hist["close"].iloc[-1])
                atr_pct = atr_val / close if close > 0 else 0
            except Exception:
                atr_pct = 0.02  # 默认 2%

            snapshots[sym] = {
                "chg_pct": chg_pct,
                "T_D": T_D,
                "volume_ratio": volume_ratio,
                "atr_pct": atr_pct,
                "regime": regime,
                "group": SYMBOLS.get(sym, {}).get("group", "其他"),
            }

        if len(snapshots) >= 5:  # 至少 5 个品种才算有效
            result = senteng.compute(snapshots)
            sentiment_daily[date_str] = {
                "score": result["score"],
                "label": result["label"],
                "band": result["band"],
                "bias": result["bias"],
                "scale": result["scale"],
                "n_symbols": len(snapshots),
            }

        if (i + 1) % 50 == 0:
            print(f"    已处理 {i+1}/{len(base_dates)} 天...", end="\r", flush=True)

    print(f"    完成: {len(sentiment_daily)} 天有有效情绪")
    return sentiment_daily


# ────────────────────────────────────────────────────────────
# 2. 带情绪的 walk-forward 回测
# ────────────────────────────────────────────────────────────

def walk_forward_with_sentiment(symbol, sentiment_daily, cfg=DEFAULT_CONFIG,
                                 min_bars=60, window=300, tail=None,
                                 cooldown_bars=5, ablate=None, df_in=None):
    """带情绪调制的 walk-forward 回测。

    sentiment_daily: {date_str: {band, score, ...}}  每日情绪
    其他参数同 walk_forward_backtest。
    """
    # 直接复用原函数的逻辑，但在 pipeline 调用时注入 sentiment_label
    # 我们手动实现一遍（因为原函数不支持 sentiment 参数）

    from four_dim_strategy import (
        _FALLBACK_SPEC,
        ROLL_GAP_MULT,
        ROLL_GAP_PCT,
        exit_plan,
        get_slip_pts,
        pipeline,
        risk_gate,
    )

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
        hist = df.iloc[:i + 1]
        date_str = df.index[i].strftime("%Y%m%d")

        # 查当日情绪
        sent_band = None
        if date_str in sentiment_daily:
            sent_band = sentiment_daily[date_str]["band"]

        try:
            pipe = pipeline(symbol, hist, None, cfg, date=date_str,
                            ablate=ablate, sentiment_label=sent_band)
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
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], cfg)
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
                            exit_price, reason = tail_stop, "尾仓离场"; break
                        tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
                    else:
                        if hi >= tail_stop:
                            exit_price, reason = tail_stop, "尾仓离场"; break
                        tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
                    continue
                if dir_T > 0:
                    if lo <= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"; break
                    if hi >= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]; continue
                        exit_price, reason = ep["t2"], "止盈2R"; break
                else:
                    if hi >= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"; break
                    if lo <= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]; continue
                        exit_price, reason = ep["t2"], "止盈2R"; break
            if exit_price is None:
                exit_price, reason = float(df["close"].iloc[-1]), "期末平"
            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R
            trades.append({
                "dir": dir_T, "R": round(R, 3), "R_adj": round(R_adj, 3),
                "reason": reason, "regime": pipe["regime"],
                "entry_date": df.index[i + 1],
                "F": pipe["F"], "T_D": pipe["T_D"], "C": pipe["C"],
                "sentiment": sent_band,  # 记录触发时的情绪
                "T_thresh_eff": pipe["T_thresh_eff"],
            })
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

    # 按情绪分档统计
    by_sentiment = {}
    for t in trades:
        s = t.get("sentiment") or "neutral"
        by_sentiment.setdefault(s, []).append(t["R_adj"])

    return {
        "symbol": symbol, "name": SYMBOLS[symbol]["name"],
        "trades": len(trades), "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades_detail": trades,
        "by_regime": {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()},
        "by_sentiment": {k: {"trades": len(v), "expR": round(float(np.mean(v)), 4)}
                          for k, v in by_sentiment.items()},
        "exit_reasons": reasons,
        "roll_skipped": roll_skipped,
    }


# ────────────────────────────────────────────────────────────
# 3. 对比验证
# ────────────────────────────────────────────────────────────

def compare_symbol(symbol, sentiment_daily, tail=400, min_bars=60, window=200):
    """对比单品种：带情绪 vs 不带情绪。"""
    # 基线（不带情绪）
    r_base = walk_forward_backtest(symbol, tail=tail, min_bars=min_bars, window=window)

    # 带情绪
    r_sent = walk_forward_with_sentiment(symbol, sentiment_daily,
                                          tail=tail, min_bars=min_bars, window=window)

    base_expR = r_base.get("expR", 0)
    sent_expR = r_sent.get("expR", 0)
    delta = sent_expR - base_expR
    delta_pct = (delta / abs(base_expR) * 100) if base_expR != 0 else 0

    return {
        "symbol": symbol,
        "group": SYMBOLS.get(symbol, {}).get("group", "?"),
        "base": {"expR": base_expR, "win_rate": r_base.get("win_rate", 0),
                  "trades": r_base.get("trades", 0)},
        "sentiment": {"expR": sent_expR, "win_rate": r_sent.get("win_rate", 0),
                       "trades": r_sent.get("trades", 0)},
        "delta": round(delta, 4),
        "delta_pct": round(delta_pct, 2),
        "by_sentiment": r_sent.get("by_sentiment", {}),
    }


def main():
    parser = argparse.ArgumentParser(description="情绪系统回测验证")
    parser.add_argument("--symbol", type=str, default="", help="单品种验证")
    parser.add_argument("--group", type=str, default="", help="按板块验证")
    parser.add_argument("--all", action="store_true", help="全市场验证")
    parser.add_argument("--tail", type=int, default=400, help="回测尾部N根日线")
    parser.add_argument("--min-bars", type=int, default=60, help="最少训练根数")
    parser.add_argument("--window", type=int, default=200, help="walk-forward窗口")
    parser.add_argument("--cache", type=str, default="logs/sentiment_daily_cache.json",
                        help="每日情绪缓存文件")
    parser.add_argument("--output", type=str, default="logs/sentiment_backtest_result.json",
                        help="结果输出")
    args = parser.parse_args()

    print("=" * 70)
    print("情绪系统回测验证")
    print(f"  tail={args.tail} | min_bars={args.min_bars} | window={args.window}")
    print("=" * 70)

    # 确定验证范围
    if args.symbol:
        target_syms = [args.symbol]
    elif args.group:
        target_syms = [s for s, info in SYMBOLS.items() if info.get("group") == args.group]
    elif args.all:
        target_syms = list(SYMBOLS.keys())
    else:
        target_syms = ["v", "TA", "al", "MA", "m", "bu", "lh", "c"]
        print(f"  默认验证品种: {target_syms}")

    # 用于计算情绪的品种（全市场，越多越准）
    all_syms = list(SYMBOLS.keys())

    # 加载或计算每日情绪
    sentiment_daily = {}
    if os.path.exists(args.cache):
        print(f"\n加载情绪缓存: {args.cache}")
        with open(args.cache, encoding="utf-8") as f:
            sentiment_daily = json.load(f)
        print(f"  已加载 {len(sentiment_daily)} 天")

    if not sentiment_daily:
        print(f"\n计算每日情绪序列...")
        t0 = time.time()
        sentiment_daily = compute_daily_sentiment(all_syms, tail=args.tail + 50,
                                                   min_bars=args.min_bars,
                                                   window=args.window)
        elapsed = time.time() - t0
        print(f"  耗时: {elapsed:.0f}s")

        # 保存缓存
        os.makedirs(os.path.dirname(args.cache), exist_ok=True)
        with open(args.cache, "w", encoding="utf-8") as f:
            json.dump(sentiment_daily, f, ensure_ascii=False, indent=2)
        print(f"  已缓存到: {args.cache}")

    # 情绪分布统计
    bands = {}
    for d in sentiment_daily.values():
        b = d.get("band", "neutral")
        bands[b] = bands.get(b, 0) + 1
    print(f"\n情绪分布: {bands}")

    # 逐个品种对比
    print(f"\n--- 逐品种对比 ({len(target_syms)} 个) ---")
    print(f"{'品种':<6} {'板块':<5} "
          f"{'基准expR':>9} {'情绪expR':>9} {'差值':>8} {'变化%':>8} "
          f"{'基准笔数':>8} {'情绪笔数':>8} {'基准胜率':>8} {'情绪胜率':>8}")
    print("-" * 85)

    results = []
    for sym in target_syms:
        if sym not in SYMBOLS:
            print(f"  {sym}: 未知品种，跳过")
            continue
        try:
            r = compare_symbol(sym, sentiment_daily, tail=args.tail,
                               min_bars=args.min_bars, window=args.window)
            results.append(r)
            arrow = "↑" if r["delta"] > 0.01 else ("↓" if r["delta"] < -0.01 else "→")
            print(f"{sym:<6} {r['group']:<5} "
                  f"{r['base']['expR']:>+9.4f} {r['sentiment']['expR']:>+9.4f} "
                  f"{r['delta']:>+8.4f} {r['delta_pct']:>+7.0f}% "
                  f"{r['base']['trades']:>8} {r['sentiment']['trades']:>8} "
                  f"{r['base']['win_rate']*100:>7.1f}% {r['sentiment']['win_rate']*100:>7.1f}%")
        except Exception as e:
            print(f"  {sym}: 失败 - {e}")

    # 汇总
    print(f"\n--- 汇总 ---")
    if results:
        n = len(results)
        avg_base = sum(r["base"]["expR"] for r in results) / n
        avg_sent = sum(r["sentiment"]["expR"] for r in results) / n
        improved = sum(1 for r in results if r["delta"] > 0.01)
        worsened = sum(1 for r in results if r["delta"] < -0.01)
        unchanged = sum(1 for r in results if abs(r["delta"]) <= 0.01)
        delta = avg_sent - avg_base
        delta_pct = (delta / abs(avg_base) * 100) if avg_base != 0 else 0

        print(f"  品种数: {n}")
        print(f"  基准平均 expR: {avg_base:+.4f}")
        print(f"  情绪平均 expR: {avg_sent:+.4f}")
        print(f"  变化: {delta:+.4f} ({delta_pct:+.1f}%)")
        print(f"  提升: {improved} | 下降: {worsened} | 持平: {unchanged}")

        # 按情绪分档的表现（汇总所有交易）
        all_by_sent = {}
        for r in results:
            for band, data in r.get("by_sentiment", {}).items():
                if band not in all_by_sent:
                    all_by_sent[band] = {"trades": 0, "total_expR": 0}
                all_by_sent[band]["trades"] += data["trades"]
                # expR × 交易数 = 总R（近似），后面再按总笔数平均
                all_by_sent[band]["total_expR"] += data["expR"] * data["trades"]

        print(f"\n  各情绪档表现:")
        for band in ["extreme_greed", "greed", "neutral", "fear", "extreme_fear"]:
            if band in all_by_sent and all_by_sent[band]["trades"] > 0:
                d = all_by_sent[band]
                avg = d["total_expR"] / d["trades"]
                label = {"extreme_greed": "极度贪婪", "greed": "贪婪",
                         "neutral": "中性", "fear": "恐惧", "extreme_fear": "极度恐惧"}[band]
                print(f"    {label:<6} ({band}): {d['trades']:>3} 笔, expR={avg:+.4f}")

        # 保存
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({
                "config": {"tail": args.tail, "min_bars": args.min_bars, "window": args.window},
                "sentiment_distribution": bands,
                "results": results,
                "summary": {
                    "count": n,
                    "avg_base_expR": round(avg_base, 4),
                    "avg_sentiment_expR": round(avg_sent, 4),
                    "delta": round(delta, 4),
                    "delta_pct": round(delta_pct, 2),
                    "improved": improved,
                    "worsened": worsened,
                    "unchanged": unchanged,
                },
            }, f, ensure_ascii=False, indent=2)
        print(f"\n详细结果已保存到: {args.output}")


if __name__ == "__main__":
    main()
