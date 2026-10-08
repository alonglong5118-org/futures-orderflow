"""
5m 精确回测 vs 日线近似回测 对比验证（UTC时区修正版）

重要发现：5m 数据为 UTC 时区，日线数据为北京时间（UTC+8）。

中国期货交易时段（北京时间）与 UTC 对应关系：
  夜盘: 21:00-23:00 (部分品种到 01:00 或 02:30)
        → UTC: 13:00-15:00 (或 17:00 / 18:30)
  日盘上午: 09:00-10:15, 10:30-11:30
        → UTC: 01:00-02:15, 02:30-03:30
  日盘下午: 13:30-15:00
        → UTC: 05:30-07:00

交易日（北京时）的 5m UTC 时间范围：
  从前一日 13:00 UTC（夜盘开始）到当日 07:00 UTC（日盘结束）

日线开盘价：通常指日盘上午开盘价（09:00 北京 = 01:00 UTC）
"""

import json
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    _FALLBACK_SPEC,
    DEFAULT_CONFIG,
    _atr_array,
    _sim_exit_5m,
    exit_plan,
    load_daily,
    load_min5,
    pipeline,
    risk_gate,
)
from portfolio_manager import symbols_group, symbols_name


def get_slip_pts(symbol, cfg):
    return cfg.get("risk_gate", {}).get("slip_pts", 1)


def get_trading_day_start_utc(trading_day_bj):
    """给定北京时的交易日，返回该交易日在5m数据(UTC)中的起始时间。

    中国期货交易日从夜盘开始：前一日 21:00 北京 = 前一日 13:00 UTC
    如果前一日没有夜盘（周末/节假日），则从当日日盘开始：当日 09:00 北京 = 当日 01:00 UTC
    """
    prev_day = pd.Timestamp(trading_day_bj).normalize() - pd.Timedelta(days=1)
    # 默认夜盘开始时间：前一日 13:00 UTC
    return prev_day + pd.Timedelta(hours=13)


def get_day_session_open_utc(trading_day_bj):
    """给定北京时的交易日，返回日盘开盘的 UTC 时间。

    日盘上午开盘：09:00 北京 = 01:00 UTC
    """
    return pd.Timestamp(trading_day_bj).normalize() + pd.Timedelta(hours=1)


def find_day_session_open(df5, trading_day_bj):
    """找到某交易日的日盘开盘 5m K线（上午盘第一根）。

    日盘上午开盘：09:00 北京 = 01:00 UTC
    返回: (entry_time_utc, entry_price) 或 (None, None)
    """
    day_open_utc = get_day_session_open_utc(trading_day_bj)
    # 在 00:55 ~ 03:30 UTC 范围内找第一根K线（对应 08:55 ~ 11:30 北京）
    search_start = day_open_utc - pd.Timedelta(minutes=10)
    search_end = day_open_utc + pd.Timedelta(hours=2, minutes=30)

    day_bars = df5[(df5.index >= search_start) & (df5.index < search_end)]
    if len(day_bars) > 0:
        return day_bars.index[0], float(day_bars["open"].iloc[0])

    return None, None


def find_night_session_open(df5, trading_day_bj):
    """找到某交易日的夜盘开盘 5m K线。

    夜盘开盘：21:00 北京 = 13:00 UTC（前一天日历日）
    返回: (entry_time_utc, entry_price) 或 (None, None)
    """
    prev_day = pd.Timestamp(trading_day_bj).normalize() - pd.Timedelta(days=1)
    night_open_utc = prev_day + pd.Timedelta(hours=13)
    # 在 12:55 ~ 15:00 UTC 范围内找第一根K线
    search_start = night_open_utc - pd.Timedelta(minutes=10)
    search_end = night_open_utc + pd.Timedelta(hours=2)

    night_bars = df5[(df5.index >= search_start) & (df5.index < search_end)]
    if len(night_bars) > 0:
        return night_bars.index[0], float(night_bars["open"].iloc[0])

    return None, None


def find_trading_day_first_bar(df5, trading_day_bj):
    """找到某交易日的第一根 5m K线（夜盘或日盘，取更早的）。

    返回: (entry_time_utc, entry_price, session_type)
    session_type: "night" 或 "day"
    """
    night_time, night_price = find_night_session_open(df5, trading_day_bj)
    day_time, day_price = find_day_session_open(df5, trading_day_bj)

    if night_time is not None:
        return night_time, night_price, "night"
    elif day_time is not None:
        return day_time, day_price, "day"
    else:
        return None, None, None


def find_next_session_after(df5, signal_time_utc):
    """找到信号时间之后的下一个交易时段开盘。

    信号通常在日盘收盘后（15:00 北京 = 07:00 UTC）产生。
    下一个时段通常是夜盘（21:00 北京 = 13:00 UTC），如果没有则是次日日盘。

    返回: (entry_time_utc, entry_price, session_type)
    """
    # 从信号时间往后找第一根K线
    next_bars = df5[df5.index >= signal_time_utc]
    if len(next_bars) == 0:
        return None, None, None

    first_time = next_bars.index[0]
    first_price = float(next_bars["open"].iloc[0])

    # 判断是夜盘还是日盘
    hour = first_time.hour
    if 12 <= hour <= 15:  # 12:00-15:00 UTC = 20:00-23:00 Beijing → 夜盘
        session_type = "night"
    elif 0 <= hour <= 7:  # 00:00-07:00 UTC = 08:00-15:00 Beijing → 日盘
        session_type = "day"
    else:
        session_type = "unknown"

    return first_time, first_price, session_type


def backtest_daily_approx(
    symbol,
    cfg=DEFAULT_CONFIG,
    min_bars=60,
    cooldown_bars=5,
    start_date=None,
    end_date=None,
    entry_session="trading_day",
):
    """日线近似回测：日线信号 + 日线开盘入场 + 5m 出场。

    注意：5m数据为UTC时区。

    entry_session:
      - "trading_day": 从交易日第一根K线开始出场跟踪（含夜盘，更完整）
      - "day": 仅从日盘开始出场跟踪（不含夜盘，原近似方式）

    入场价用日线开盘价（近似），出场跟踪用5m K线。
    """
    df = load_daily(symbol)
    df5 = load_min5(symbol, fetch_if_missing=False)
    if df is None or df5 is None or len(df5) < 60:
        return None

    if start_date:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df.index <= pd.Timestamp(end_date)]
    if len(df) < min_bars + 10:
        return None

    sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]

    _open = df["open"].values
    _high = df["high"].values
    _low = df["low"].values
    _close = df["close"].values
    _atr14_arr = _atr_array(_high, _low, _close, 14)

    trades = []
    n = len(df)
    i = min_bars
    last_trade_i = -999

    while i < n - 1:
        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        _i = i + 1
        _prec = {
            "_c": _close[:_i],
            "_h": _high[:_i],
            "_l": _low[:_i],
            "_atr14": _atr14_arr[i],
        }
        try:
            pipe = pipeline(symbol, hist, None, cfg, date=date_str, _precalc=_prec)
        except Exception:
            i += 1
            continue

        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry_date = df.index[i + 1]  # 北京时交易日
            entry = float(_open[i + 1])  # 日线开盘价
            atr_val = _atr14_arr[i]
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

            # 5m 出场：从交易日第一根K线开始（含夜盘）
            if entry_session == "trading_day":
                # 从交易日开始（前一日13:00UTC = 前一日21:00北京夜盘）
                entry_time, _, _ = find_trading_day_first_bar(df5, entry_date)
                if entry_time is None:
                    # 找不到，退回日盘
                    entry_time, _ = find_day_session_open(df5, entry_date)
            else:
                # 仅日盘开始
                entry_time, _ = find_day_session_open(df5, entry_date)

            if entry_time is None:
                i += 1
                continue

            seg = df5[df5.index >= entry_time]
            if len(seg) < 3:
                i += 1
                continue

            exit_price, reason, exit_idx = _sim_exit_5m(seg, dir_T, entry, ep, sd)
            if exit_price is None:
                i += 1
                continue

            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R

            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": seg.index[exit_idx] if exit_idx < len(seg) else seg.index[-1],
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "dir": dir_T,
                    "R": round(R, 3),
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "F": pipe["F"],
                    "T_D": pipe["T_D"],
                    "C": pipe["C"],
                }
            )
            last_trade_i = i

            # 跳转到出场后
            exit_dt = seg.index[exit_idx] if exit_idx < len(seg) else seg.index[-1]
            # 5m时间是UTC，转成北京时的日期来对齐日线
            exit_day_bj = (exit_dt + pd.Timedelta(hours=8)).normalize()
            days_fwd = 1
            for j in range(i + 1, min(i + 20, n)):
                if df.index[j].normalize() > exit_day_bj:
                    days_fwd = j - i
                    break
            i += max(days_fwd, 1)
        else:
            i += 1

    return _summarize(trades, symbol)


def backtest_5m_precise(
    symbol, cfg=DEFAULT_CONFIG, min_bars=60, cooldown_bars=5, start_date=None, end_date=None, entry_mode="day_open"
):
    """5m 精确回测。

    entry_mode:
      - "day_open": 日盘开盘入场（与日线近似对齐，用于公平对比）
      - "next_session": 下一个交易时段入场（更贴近实盘，信号触发后尽快入场）
      - "trading_day_open": 交易日第一根K线入场（夜盘或日盘，以早的为准）
    """
    df = load_daily(symbol)
    df5 = load_min5(symbol, fetch_if_missing=False)
    if df is None or df5 is None or len(df5) < 100:
        return None

    if start_date:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df.index <= pd.Timestamp(end_date)]
    if len(df) < min_bars + 10:
        return None

    df5_start = df5.index[0]
    df5_end = df5.index[-1]

    sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]

    _open = df["open"].values
    _high = df["high"].values
    _low = df["low"].values
    _close = df["close"].values
    _atr14_arr = _atr_array(_high, _low, _close, 14)

    trades = []
    n = len(df)
    i = min_bars
    last_trade_day = None

    while i < n - 1:
        signal_date = df.index[i]  # 北京时的信号日

        # 检查5m数据是否覆盖
        # 信号日的下一个交易时段至少要在5m数据范围内
        signal_day_end_utc = signal_date.normalize() + pd.Timedelta(hours=7)  # 15:00北京
        if signal_day_end_utc > df5_end:
            i += 1
            continue

        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        _i = i + 1
        _prec = {
            "_c": _close[:_i],
            "_h": _high[:_i],
            "_l": _low[:_i],
            "_atr14": _atr14_arr[i],
        }
        try:
            pipe = pipeline(symbol, hist, None, cfg, date=date_str, _precalc=_prec)
        except Exception:
            i += 1
            continue

        if pipe["triggered"] and pipe["dir_T"] != 0:
            entry_date_daily = df.index[i + 1]  # 北京时的入场交易日

            # 冷却期
            if last_trade_day is not None:
                days_since = (entry_date_daily.normalize() - last_trade_day.normalize()).days
                if days_since < cooldown_bars:
                    i += 1
                    continue

            # 找到精确入场点
            entry_time = None
            entry = None

            if entry_mode == "day_open":
                # 日盘开盘入场（与日线对齐）：09:00北京 = 01:00UTC
                entry_time, entry = find_day_session_open(df5, entry_date_daily)
            elif entry_mode == "trading_day_open":
                # 交易日第一根K线入场（夜盘或日盘）
                entry_time, entry, _ = find_trading_day_first_bar(df5, entry_date_daily)
            elif entry_mode == "next_session":
                # 下一时段入场：信号日收盘后，下一个交易时段
                # 信号在日盘收盘时产生（15:00北京 = 07:00UTC）
                signal_end_utc = signal_date.normalize() + pd.Timedelta(hours=7)
                entry_time, entry, _ = find_next_session_after(df5, signal_end_utc)

            if entry is None or entry_time is None:
                i += 1
                continue

            atr_val = _atr14_arr[i]
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

            # 5m 出场：从入场时刻开始
            seg = df5[df5.index >= entry_time]
            if len(seg) < 3:
                i += 1
                continue

            exit_price, reason, exit_idx = _sim_exit_5m(seg, dir_T, entry, ep, sd)
            if exit_price is None:
                i += 1
                continue

            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R

            exit_time = seg.index[exit_idx] if exit_idx < len(seg) else seg.index[-1]

            trades.append(
                {
                    "entry_date": entry_time,
                    "exit_date": exit_time,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "dir": dir_T,
                    "R": round(R, 3),
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "F": pipe["F"],
                    "T_D": pipe["T_D"],
                    "C": pipe["C"],
                }
            )
            last_trade_day = entry_date_daily

            # 跳转：将5m退出时间(UTC)转成北京时日期，对齐日线索引
            exit_day_bj = (exit_time + pd.Timedelta(hours=8)).normalize()
            days_fwd = 1
            for j in range(i + 1, min(i + 20, n)):
                if df.index[j].normalize() > exit_day_bj:
                    days_fwd = j - i
                    break
            i += max(days_fwd, 1)
        else:
            i += 1

    return _summarize(trades, symbol)


def _summarize(trades, symbol):
    if not trades:
        return {
            "symbol": symbol,
            "name": symbols_name(symbol),
            "group": symbols_group(symbol),
            "trades": 0,
            "expR": 0,
            "win_rate": 0,
            "max_dd": 0,
            "total_R": 0,
            "trades_detail": trades,
            "by_regime": {},
            "exit_reasons": {},
            "profit_factor": 0,
            "avg_win_R": 0,
            "avg_loss_R": 0,
        }

    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r < 0]

    expR = float(np.mean(Rs))
    win_rate = len(wins) / len(Rs)
    cumulative = np.cumsum(Rs)
    running_max = np.maximum.accumulate(cumulative)
    max_dd = float(np.max(running_max - cumulative))

    by_regime = {}
    for t in trades:
        rg = t.get("regime", "未知")
        by_regime.setdefault(rg, []).append(t["R_adj"])
    by_regime = {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()}

    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    avg_win = float(np.mean(wins)) if wins else 0
    avg_loss = abs(float(np.mean(losses))) if losses else 1
    pf = (avg_win * len(wins)) / (avg_loss * len(losses)) if losses and avg_loss > 0 else float("inf")

    return {
        "symbol": symbol,
        "name": symbols_name(symbol),
        "group": symbols_group(symbol),
        "trades": len(trades),
        "expR": round(expR, 4),
        "win_rate": round(win_rate, 4),
        "max_dd": round(max_dd, 2),
        "total_R": round(sum(Rs), 2),
        "trades_detail": trades,
        "by_regime": by_regime,
        "exit_reasons": reasons,
        "profit_factor": round(pf, 3) if pf != float("inf") else 999,
        "avg_win_R": round(avg_win, 3),
        "avg_loss_R": round(avg_loss, 3),
    }


def compare_symbol(symbol, cfg=DEFAULT_CONFIG):
    """对比单个品种。"""
    df5 = load_min5(symbol, fetch_if_missing=False)
    if df5 is None or len(df5) < 100:
        return None

    # 5m数据范围(UTC)转成北京时的日期范围
    start_date_bj = (df5.index[0] + pd.Timedelta(hours=8)).strftime("%Y-%m-%d")
    end_date_bj = (df5.index[-1] + pd.Timedelta(hours=8)).strftime("%Y-%m-%d")

    r_daily = backtest_daily_approx(symbol, cfg, start_date=start_date_bj, end_date=end_date_bj)
    r_5m = backtest_5m_precise(symbol, cfg, start_date=start_date_bj, end_date=end_date_bj, entry_mode="day_open")

    if not r_daily or not r_5m or r_daily["trades"] < 3:
        return None

    diff = {
        "trades_diff": r_5m["trades"] - r_daily["trades"],
        "trades_diff_pct": round((r_5m["trades"] - r_daily["trades"]) / max(r_daily["trades"], 1) * 100, 1),
        "expR_diff": round(r_5m["expR"] - r_daily["expR"], 4),
        "expR_diff_pct": round((r_5m["expR"] - r_daily["expR"]) / abs(r_daily["expR"]) * 100, 1)
        if r_daily["expR"] != 0
        else 0,
        "win_rate_diff": round(r_5m["win_rate"] - r_daily["win_rate"], 4),
        "total_R_diff": round(r_5m["total_R"] - r_daily["total_R"], 2),
        "total_R_diff_pct": round((r_5m["total_R"] - r_daily["total_R"]) / abs(r_daily["total_R"]) * 100, 1)
        if r_daily["total_R"] != 0
        else 0,
    }

    return {
        "symbol": symbol,
        "name": symbols_name(symbol),
        "group": symbols_group(symbol),
        "daily": r_daily,
        "precise_5m": r_5m,
        "diff": diff,
        "period": f"{start_date_bj} ~ {end_date_bj}",
        "n_bars_5m": len(df5),
    }


def main():
    print("=" * 70)
    print("5m 精确回测 vs 日线近似 对比验证（UTC时区修正版）")
    print("=" * 70)

    active_syms = list(DEFAULT_CONFIG["per_symbol_risk"].keys())
    print(f"\n[1/3] 检查 {len(active_syms)} 个活跃品种的 5m 数据 ...")

    valid_syms = []
    for sym in active_syms:
        df5 = load_min5(sym, fetch_if_missing=False)
        if df5 is not None and len(df5) >= 100:
            valid_syms.append(sym)

    print(f"  有 5m 数据且足量: {len(valid_syms)} 个品种")

    # 逐品种对比
    print("\n[2/3] 逐品种对比 (entry_mode=day_open 日盘开盘对齐) ...")
    results = []

    for i, sym in enumerate(valid_syms):
        r = compare_symbol(sym, DEFAULT_CONFIG)
        if r and r["daily"]["trades"] >= 3:
            results.append(r)
            d = r["diff"]
            direction = "🟢" if d["expR_diff"] >= 0 else "🔴"
            print(
                f"  [{i + 1}/{len(valid_syms)}] {sym:>5}: "
                f"日线={r['daily']['expR']:+.3f} "
                f"5m={r['precise_5m']['expR']:+.3f} "
                f"({d['expR_diff']:+.3f}) {direction}"
            )
        else:
            print(f"  [{i + 1}/{len(valid_syms)}] {sym:>5}: 交易不足", end="\r")
    print()

    if not results:
        print("  ⚠️  没有足够数据")
        return

    # 汇总
    print("\n[3/3] 汇总分析")
    print(f"\n{'=' * 70}")
    print("  品种对比表（按 expR 差异排序）")
    print(f"{'=' * 70}")
    print(
        f"\n  {'品种':>5} {'名称':>6} {'板块':>6} "
        f"{'日笔数':>5} {'5m笔数':>6} {'笔差':>5} "
        f"{'日expR':>8} {'5mexpR':>8} {'差异':>7} {'方向':>4}"
    )
    print(f"  {'-' * 75}")

    results.sort(key=lambda x: x["diff"]["expR_diff"], reverse=True)

    for r in results:
        d = r["diff"]
        dd = r["daily"]
        d5 = r["precise_5m"]
        direction = "🟢" if d["expR_diff"] >= 0 else "🔴"
        print(
            f"  {r['symbol']:>5} {r['name']:>6} {r['group']:>6} "
            f"{dd['trades']:>5} {d5['trades']:>6} {d['trades_diff']:>+5.0f} "
            f"{dd['expR']:>+8.3f} {d5['expR']:>+8.3f} {d['expR_diff']:>+7.3f} {direction}"
        )

    # 统计
    print(f"\n{'=' * 70}")
    print("  汇总统计")
    print(f"{'=' * 70}")

    n = len(results)
    pos = sum(1 for r in results if r["diff"]["expR_diff"] >= 0)
    neg = n - pos

    daily_total = sum(r["daily"]["total_R"] for r in results)
    m5_total = sum(r["precise_5m"]["total_R"] for r in results)

    daily_avg_expR = float(np.mean([r["daily"]["expR"] for r in results]))
    m5_avg_expR = float(np.mean([r["precise_5m"]["expR"] for r in results]))

    daily_trades = sum(r["daily"]["trades"] for r in results)
    m5_trades = sum(r["precise_5m"]["trades"] for r in results)

    # 差异幅度（绝对值的平均）
    abs_expR_diffs = [abs(r["diff"]["expR_diff"]) for r in results]
    avg_abs_diff = float(np.mean(abs_expR_diffs))

    # 相对差异
    rel_diffs = []
    for r in results:
        if abs(r["daily"]["expR"]) > 0.05:  # 只算有一定量级的
            rel_diffs.append(abs(r["diff"]["expR_diff"]) / abs(r["daily"]["expR"]) * 100)
    avg_rel_diff = float(np.mean(rel_diffs)) if rel_diffs else 0

    print(f"\n  对比品种数: {n} 个")
    print(f"  5m 跑赢日线: {pos}/{n} ({pos / n * 100:.0f}%)")
    print(f"  5m 跑输日线: {neg}/{n} ({neg / n * 100:.0f}%)")

    print("\n  总交易笔数:")
    print(f"    日线近似: {daily_trades} 笔")
    print(f"    5m 精确: {m5_trades} 笔")
    print(
        f"    差异: {m5_trades - daily_trades:+d} 笔 ({(m5_trades - daily_trades) / max(daily_trades, 1) * 100:+.1f}%)"
    )

    print("\n  平均 expR:")
    print(f"    日线近似: {daily_avg_expR:+.4f}")
    print(f"    5m 精确: {m5_avg_expR:+.4f}")
    print(f"    差异: {m5_avg_expR - daily_avg_expR:+.4f}")
    print(f"    平均绝对差异: {avg_abs_diff:.4f}")
    if avg_rel_diff > 0:
        print(f"    平均相对差异: {avg_rel_diff:.1f}%")

    print("\n  等权组合总收益:")
    print(f"    日线近似: {daily_total:+.2f} R")
    print(f"    5m 精确: {m5_total:+.2f} R")
    total_diff_pct = (m5_total - daily_total) / abs(daily_total) * 100 if daily_total != 0 else 0
    print(f"    差异: {m5_total - daily_total:+.2f} R ({total_diff_pct:+.1f}%)")

    # 一致性评级
    if abs(total_diff_pct) <= 10:
        grade = "A 高度一致"
    elif abs(total_diff_pct) <= 20:
        grade = "B 基本一致"
    elif abs(total_diff_pct) <= 35:
        grade = "C 有差异"
    else:
        grade = "D 差异很大"

    print(f"\n  🏆 一致性评级: {grade}")

    # 板块维度分析
    print(f"\n{'=' * 70}")
    print("  板块维度对比")
    print(f"{'=' * 70}")
    by_group = {}
    for r in results:
        g = r["group"]
        by_group.setdefault(g, []).append(r)

    print(f"\n  {'板块':>6} {'品种数':>5} {'日总R':>8} {'5m总R':>8} {'差异':>7}")
    print(f"  {'-' * 45}")
    for g, gr in sorted(by_group.items(), key=lambda x: -sum(r["daily"]["total_R"] for r in x[1])):
        d_t = sum(r["daily"]["total_R"] for r in gr)
        m_t = sum(r["precise_5m"]["total_R"] for r in gr)
        print(f"  {g:>6} {len(gr):>5} {d_t:>+8.2f} {m_t:>+8.2f} {m_t - d_t:>+7.2f}")

    # 保存
    os.makedirs("logs", exist_ok=True)
    save_results = []
    for r in results:
        sr = {
            "symbol": r["symbol"],
            "name": r["name"],
            "group": r["group"],
            "period": r["period"],
            "daily": {k: v for k, v in r["daily"].items() if k != "trades_detail"},
            "precise_5m": {k: v for k, v in r["precise_5m"].items() if k != "trades_detail"},
            "diff": r["diff"],
        }
        save_results.append(sr)

    output = {
        "date": datetime.now().isoformat(),
        "entry_mode": "day_open (日盘开盘对齐，UTC修正)",
        "timezone_note": "5m数据为UTC时区，已正确对齐北京时交易日",
        "n_symbols": n,
        "total_daily_trades": daily_trades,
        "total_5m_trades": m5_trades,
        "daily_total_R": round(daily_total, 2),
        "m5_total_R": round(m5_total, 2),
        "total_diff_pct": round(total_diff_pct, 1),
        "avg_abs_expR_diff": round(avg_abs_diff, 4),
        "avg_rel_expR_diff": round(avg_rel_diff, 1),
        "consistency_grade": grade,
        "symbols": save_results,
    }
    with open("logs/5m_vs_daily_comparison.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("\n  结果已保存 → logs/5m_vs_daily_comparison.json")


if __name__ == "__main__":
    main()
