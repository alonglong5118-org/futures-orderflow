
# -*- coding: utf-8 -*-
"""
v5.1 回测集成模块
================
将信号质量过滤 + 多周期确认 + 分级止损 集成到 walk_forward_backtest 中。

用法:
    from four_dim_v51_backtest import walk_forward_backtest_v51
    r = walk_forward_backtest_v51("jd", cfg)
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import four_dim_strategy as fd

# v5.1 常量
SIGNAL_QUALITY_MIN_SCORE = 60
BREAKOUT_BODY_MIN_PCT = 0.5
BREAKOUT_VOLUME_MULT = 1.5
VOLUME_MA_PERIOD = 20
MULTI_TIMEFRAME_ENABLED = True
HIGHER_TF_MA_FAST = 20
HIGHER_TF_MA_SLOW = 55
COUNTER_TREND_POS_SCALE = 0.5
COUNTER_TREND_RR_BOOST = 1.3
BREAKEVEN_TRIGGER_R = 1.0
TRAILING_STOP_ATR_MULT = 2.0
MIN_RR_RATIO = 2.0
SIGMA_STOP_MULT = 3.0


def _calc_signal_quality(hist, direction, entry_price):
    """回测版信号质量评分 (0-100分)"""
    if hist is None or len(hist) < 3:
        return 50  # 数据不足时给低分，避免假信号通过
    try:
        recent = hist.tail(3)
        if len(recent) < 2:
            return 50  # 数据不足时给低分
        
        last = recent.iloc[-1]
        body_pct = abs(float(last['close']) - float(last['open'])) / float(last['open']) * 100 if float(last['open']) > 0 else 0
        
        vol_col = 'volume' if 'volume' in hist.columns else 'vol'
        vol_ma = hist[vol_col].tail(VOLUME_MA_PERIOD).mean() if vol_col in hist.columns else 0
        volume_ratio = float(last.get(vol_col, 0)) / vol_ma if vol_ma > 0 else 1.0
        
        # 维度1: 收盘价确认 (30分)
        body_score = min(30, body_pct / BREAKOUT_BODY_MIN_PCT * 30)
        
        # 维度2: 成交量确认 (30分)
        volume_score = min(30, volume_ratio / BREAKOUT_VOLUME_MULT * 30) if vol_ma > 0 else 15
        
        # 维度3: K线形态 (20分)
        body_size = abs(float(last['close']) - float(last['open']))
        upper_wick = float(last['high']) - max(float(last['open']), float(last['close']))
        shape_score = 20 if (body_size > 0 and upper_wick / body_size < 0.5) else 10
        
        # 维度4: 位置合理性 (20分)
        if direction > 0:
            bd = (entry_price - float(last['high'])) / float(last['high']) * 100 if float(last['high']) > 0 else 0
        else:
            bd = (float(last['low']) - entry_price) / float(last['low']) * 100 if float(last['low']) > 0 else 0
        
        if 0.3 <= bd <= 2.0: pos_score = 20
        elif bd < 0.3: pos_score = 10
        elif bd <= 5: pos_score = 15
        else: pos_score = 5
        
        return round(body_score + volume_score + shape_score + pos_score, 1)
    except Exception:
        return 100


def _get_tf_trend(hist):
    """回测版大周期趋势判断"""
    if hist is None or len(hist) < HIGHER_TF_MA_SLOW + 5:
        return "sideways", 0
    try:
        closes = hist['close'].values
        if len(closes) < HIGHER_TF_MA_SLOW:
            return "sideways", 0
        ma_fast = np.mean(closes[-HIGHER_TF_MA_FAST:])
        ma_slow = np.mean(closes[-HIGHER_TF_MA_SLOW:])
        if ma_slow == 0:
            return "sideways", 0
        divergence = (ma_fast - ma_slow) / ma_slow * 100
        strength = min(100, abs(divergence) * 20)
        if divergence > 0.5: trend = "bullish"
        elif divergence < -0.5: trend = "bearish"
        else: trend = "sideways"
        return trend, round(strength, 1)
    except Exception:
        return "sideways", 0


def _apply_tf_filter(direction, tf_trend, tf_strength):
    """回测版多周期过滤: (passed, pos_scale, rr_required)"""
    if not MULTI_TIMEFRAME_ENABLED:
        return True, 1.0, MIN_RR_RATIO
    
    is_long = direction > 0
    is_short = direction < 0
    
    if tf_trend == "sideways":
        return True, 0.8, MIN_RR_RATIO
    
    is_with = (tf_trend == "bullish" and is_long) or (tf_trend == "bearish" and is_short)
    if is_with:
        return True, 1.0, MIN_RR_RATIO
    else:
        return True, COUNTER_TREND_POS_SCALE, MIN_RR_RATIO * COUNTER_TREND_RR_BOOST


def walk_forward_backtest_v51(symbol, cfg=None, min_bars=60, tail=None):
    """
    v5.1 回测: 集成信号质量过滤 + 多周期确认 + 分级止损
    
    与原版 walk_forward_backtest 对比:
    1. 信号生成后先做质量评分，<60分跳过
    2. 多周期趋势确认，逆趋势缩仓+提高盈亏比
    3. 出场使用分级止损状态机
    """
    if cfg is None:
        cfg = fd.DEFAULT_CONFIG
    
    df = fd.load_daily(symbol)
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    if tail:
        df = df.tail(tail)
    if len(df) < min_bars + 20:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    
    n = len(df)
    sp = cfg["contract_specs"].get(symbol, fd._FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]
    trades = []
    roll_skipped = 0
    quality_filtered = 0
    tf_filtered = 0
    tiered_stops = {"initial": 0, "breakeven": 0, "trailing": 0, "hard": 0}
    
    i = min_bars
    last_trade_i = -999
    
    while i < n - 1:
        hist = df.iloc[:i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        
        try:
            pipe = fd.pipeline(symbol, hist, None, cfg, date=date_str)
        except Exception:
            i += 1
            continue
        
        if not (pipe["triggered"] and pipe["dir_T"] != 0):
            i += 1
            continue
        
        if (i - last_trade_i) < 5:  # cooldown_bars
            i += 1
            continue
        
        dir_T = pipe["dir_T"]
        entry = float(df["open"].iloc[i + 1])
        atr_val = fd.strat_atr(hist).iloc[-1]
        if atr_val <= 0 or math.isnan(atr_val):
            i += 1
            continue
        
        # ── v5.1 Step 1: 信号质量评分 ──
        quality_score = _calc_signal_quality(hist, dir_T, entry)
        if quality_score < SIGNAL_QUALITY_MIN_SCORE:
            quality_filtered += 1
            i += 1
            continue
        
        # ── v5.1 Step 2: 多周期趋势确认 ──
        tf_trend, tf_strength = _get_tf_trend(hist)
        tf_pass, pos_scale, rr_required = _apply_tf_filter(dir_T, tf_trend, tf_strength)
        
        if not tf_pass:
            tf_filtered += 1
            i += 1
            continue
        
        # 调整入场参数
        effective_atr = atr_val
        if pos_scale < 1.0:
            effective_atr = atr_val / pos_scale
        
        rg = fd.risk_gate(symbol, entry, effective_atr, cfg)
        if not rg["passed"]:
            i += 1
            continue
        
        ep = fd.exit_plan(symbol, entry, dir_T, effective_atr, pipe["regime"], cfg)
        sd = ep["stop_dist"]
        
        # ── v5.1 Step 3: 分级止损状态机 ──
        tier_level = "initial"
        current_stop = ep["stop"]
        entry_price = entry
        
        # 出场模拟（含分级止损）
        exit_price, reason = None, ""
        tail_active, tail_stop = False, None
        
        for j in range(i + 1, n):
            hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
            
            # 换月跳空识别
            if j > i + 1:
                prev_close = float(df["close"].iloc[j - 1])
                gap = abs(float(df["open"].iloc[j]) - prev_close)
                if gap > max(fd.ROLL_GAP_PCT * prev_close, fd.ROLL_GAP_MULT * sd):
                    roll_skipped += 1
                    continue
            
            current_price = hi if dir_T > 0 else lo
            R_profit = (current_price - entry_price) / sd if dir_T > 0 else (entry_price - current_price) / sd
            
            # ── 分级止损状态转换 ──
            if tier_level == "initial" and R_profit >= BREAKEVEN_TRIGGER_R:
                tier_level = "breakeven"
                current_stop = entry_price
                tiered_stops["breakeven"] += 1
            
            if tier_level == "breakeven" and R_profit >= 2.0:
                tier_level = "trailing"
                tiered_stops["trailing"] += 1
            
            if tier_level == "trailing":
                if dir_T > 0:
                    current_stop = max(current_stop, hi - TRAILING_STOP_ATR_MULT * effective_atr)
                else:
                    current_stop = min(current_stop, lo + TRAILING_STOP_ATR_MULT * effective_atr)
            
            # ── 出场判断 ──
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
            
            # 硬止损 (3σ)
            sigma_stop = entry_price - SIGMA_STOP_MULT * effective_atr if dir_T > 0 else entry_price + SIGMA_STOP_MULT * effective_atr
            
            if dir_T > 0:
                if lo <= current_stop:
                    exit_price, reason = current_stop, f"止损({tier_level})"; break
                if lo <= sigma_stop:
                    exit_price, reason = sigma_stop, "硬止损"; break
                    tiered_stops["hard"] += 1
                if hi >= ep["t2"]:
                    if ep["tail_enabled"]:
                        tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]
                        continue
                    exit_price, reason = ep["t2"], "止盈2R"; break
            else:
                if hi >= current_stop:
                    exit_price, reason = current_stop, f"止损({tier_level})"; break
                if hi >= sigma_stop:
                    exit_price, reason = sigma_stop, "硬止损"; break
                    tiered_stops["hard"] += 1
                if lo <= ep["t2"]:
                    if ep["tail_enabled"]:
                        tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]
                        continue
                    exit_price, reason = ep["t2"], "止盈2R"; break
        
        if exit_price is None:
            exit_price, reason = float(df["close"].iloc[-1]), "期末平"
        
        R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
        slip_R = 2 * fd.get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
        fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
        R_adj = R - slip_R - fee_R
        
        trades.append({
            "dir": dir_T, "R": round(R, 3), "R_adj": round(R_adj, 3),
            "reason": reason, "regime": pipe["regime"],
            "F": pipe["F"], "T_D": pipe["T_D"], "C": pipe["C"],
            "quality_score": quality_score, "tf_trend": tf_trend,
            "tf_strength": tf_strength, "pos_scale": pos_scale,
            "tier_level": tier_level, "rr_required": rr_required
        })
        
        last_trade_i = i
        i = j + 1 if exit_price is not None else i + 1
    
    if not trades:
        return {
            "symbol": symbol, "trades": 0, 
            "note": f"无触发信号 (质量过滤:{quality_filtered}, 周期过滤:{tf_filtered})",
            "roll_skipped": roll_skipped
        }
    
    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    by_regime = {}
    for t in trades:
        by_regime.setdefault(t["regime"], []).append(t["R_adj"])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    
    return {
        "symbol": symbol, "name": fd.SYMBOLS.get(symbol, {}).get("name", symbol),
        "trades": len(trades), "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades_detail": trades,
        "by_regime": {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()},
        "exit_reasons": reasons,
        "roll_skipped": roll_skipped,
        "v51_stats": {
            "quality_filtered": quality_filtered,
            "tf_filtered": tf_filtered,
            "tiered_stops": tiered_stops
        }
    }


def compare_v5_v51(targets, cfg=None):
    """v5 vs v5.1 对比运行器"""
    if cfg is None:
        cfg = fd.DEFAULT_CONFIG
    
    print("=" * 90)
    print("  v5 (原版) vs v5.1 (信号质量+多周期+分级止损) 回测对比")
    print("=" * 90)
    print()
    
    rows = []
    for sym in targets:
        print(f"[{sym}] ", end="", flush=True)
        
        # v5 原版
        r5 = fd.walk_forward_backtest(sym, cfg)
        n5 = r5.get("trades", 0)
        exp5 = r5.get("expR", 0)
        win5 = r5.get("win_rate", 0)
        
        # 计算最大回撤 for v5
        det5 = r5.get("trades_detail", [])
        if det5:
            Rs5 = [t["R_adj"] for t in det5]
            eq5 = np.cumsum(np.array(Rs5))
            peak5 = np.maximum.accumulate(eq5)
            dd5 = round(float((peak5 - eq5).max()), 3)
        else:
            dd5 = 0
        
        # v5.1 新版
        r51 = walk_forward_backtest_v51(sym, cfg)
        n51 = r51.get("trades", 0)
        exp51 = r51.get("expR", 0)
        win51 = r51.get("win_rate", 0)
        
        det51 = r51.get("trades_detail", [])
        if det51:
            Rs51 = [t["R_adj"] for t in det51]
            eq51 = np.cumsum(np.array(Rs51))
            peak51 = np.maximum.accumulate(eq51)
            dd51 = round(float((peak51 - eq51).max()), 3)
        else:
            dd51 = 0
        
        v51_stats = r51.get("v51_stats", {})
        qf = v51_stats.get("quality_filtered", 0)
        tff = v51_stats.get("tf_filtered", 0)
        
        rows.append((sym, n5, n51, exp5, exp51, win5, win51, dd5, dd51, qf, tff))
        
        de = round(exp51 - exp5, 4)
        dw = round(win51 - win5, 3)
        dd = round(dd51 - dd5, 3)
        dt = n51 - n5
        
        print(f"v5: {n5}笔 expR={exp5:.4f} win={win5:.1%} dd={dd5:.2f}R | "
              f"v5.1: {n51}笔 expR={exp51:.4f} win={win51:.1%} dd={dd51:.2f}R "
              f"(质量:{qf} 周期:{tff}) ΔexpR={de:+.4f} Δdd={dd:+.2f}R", flush=True)
    
    print()
    print("=" * 90)
    print("  汇总对比")
    print("=" * 90)
    
    valid = [(s, *rest) for s, *rest in rows if rest[0] > 0 and rest[1] > 0]
    if not valid:
        print("  无有效数据")
        return
    
    n_imp = sum(1 for _, n5, n51, e5, e51, *_ in valid if e51 > e5 + 0.01)
    n_dec = sum(1 for _, n5, n51, e5, e51, *_ in valid if e51 < e5 - 0.01)
    n_flat = len(valid) - n_imp - n_dec
    
    avg_de = round(sum(r[3] - r[2] for _, *r in valid) / len(valid), 4)
    avg_dw = round(sum(r[5] - r[4] for _, *r in valid) / len(valid), 3)
    avg_dd = round(sum(r[7] - r[6] for _, *r in valid) / len(valid), 3)
    avg_dt = round(sum(r[1] - r[0] for _, *r in valid) / len(valid), 1)
    
    print(f"  品种: {len(valid)} 个 | Δ笔数: {avg_dt:+.1f} | ΔexpR: {avg_de:+.4f} | Δ胜率: {avg_dw:+.1%} | Δdd: {avg_dd:+.2f}R")
    print(f"  改善: {n_imp} | 持平: {n_flat} | 下降: {n_dec}")
    
    # 收益风险比
    print()
    print("  收益风险比 (expR / max_dd):")
    for s, n5, n51, e5, e51, w5, w51, d5, d51, qf, tff in valid:
        rr5 = e5 / d5 if d5 > 0 else 0
        rr51 = e51 / d51 if d51 > 0 else 0
        print(f"    {s}: v5={rr5:.2f} → v5.1={rr51:.2f}")
    
    print()
    if avg_de > 0 and avg_dd < 0:
        print("  ✅ v5.1 有效: 期望R提升 + 最大回撤降低")
    elif avg_de > 0:
        print("  ⚠️ v5.1 有改善: 期望R提升，回撤变化不大")
    else:
        print("  ❌ v5.1 效果不明显，需调参")
    
    return rows


if __name__ == "__main__":
    targets = ["jd", "lh", "FG", "SA", "JM", "J"]
    print(f"默认品种: {len(targets)} 个")
    compare_v5_v51(targets)
