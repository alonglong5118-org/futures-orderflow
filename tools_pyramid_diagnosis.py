#!/usr/bin/env python3
"""金字塔加仓立项诊断 v3（2026-08-31）。

v1 教训：测"MFE 峰值后延续"是循环论证。
v2 教训：250bar 窗口内无条件统计首达率无意义（价格总能游走到任何档位），
         且忽略了持仓已被止损打掉后走势与该笔无关的事实。

v3 方法论：首达博弈（first-passage game）——加仓单的精确期望框架。

  加仓单的生命周期：在价格首达 +0.5R 时加仓 1 单位，止损设在原入场价（0R，保本）。
  该加仓单的结局只有两种：先触 +1.5R（赚 1.0R）或先触 0R（赚 0R）。

  P(先到1.5R) > 25% → 有正动量（随机漫步无漂移时该概率恰为 0.5/2.0 = 25%）
  P(先到1.5R) ≤ 25% → 无动量优势，加仓是负期望（付滑点赌硬币）

同理测第二个加仓档：+1.0R 加仓、保本止损设在 +0.5R，先触 +2.0R 还是 +0.5R。
同 bar 双触按保守口径（不利先发生）。
"""

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from four_dim_strategy import (
    DEFAULT_CONFIG, SYMBOLS, _atr_array, exit_plan, load_daily, risk_gate,
)

LABELS_FILE = Path(__file__).parent / "backtest_strategy_labels.json"

# 加仓档位定义：(触发档, 保本档, 目标档)——R 计
ADDONS = [
    ("A1", 0.5, 0.0, 1.5),   # +0.5R 加仓，止损 0R，目标 +1.5R
    ("A2", 1.0, 0.5, 2.0),   # +1.0R 加仓，止损 +0.5R，目标 +2.0R
    ("A3", 1.5, 1.0, 3.0),   # +1.5R 加仓，止损 +1.0R，目标 +3.0R
]
# 随机漫步基准（无漂移）：P(先到目标) = 距离比
BASELINE = {"A1": 0.5 / 2.0, "A2": 0.5 / 1.5, "A3": 0.5 / 2.0}
# 每档期望（R）：P × (目标-触发) - (1-P) × (触发-保本)
PAYOFF = {"A1": (1.0, 0.5), "A2": (1.0, 0.5), "A3": (1.5, 0.5)}


def first_passage(high, low, close, start_j, n, direction, entry, sd,
                  target_r, stop_r):
    """从 start_j 起步，返回 'target' / 'stop' / 'timeout'。

    价格先触及 target_r（R 计，同向）还是 stop_r（R 计，回落）。
    同 bar 双触：保守判定 stop 先发生。
    """
    tgt_price = entry + direction * target_r * sd
    stp_price = entry + direction * stop_r * sd
    for j in range(start_j, n):
        if direction > 0:
            hit_stop = low[j] <= stp_price
            hit_tgt = high[j] >= tgt_price
        else:
            hit_stop = high[j] >= stp_price
            hit_tgt = low[j] <= tgt_price
        if hit_stop:            # 保守：同 bar 双触算 stop
            return "stop"
        if hit_tgt:
            return "target"
    return "timeout"


def analyze_symbol(sym, trades_detail, cfg, max_hold=120):
    df = load_daily(sym)
    if df is None or len(df) < 100:
        return None

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    open_ = df["open"].values
    dates = [str(d)[:10] for d in df.index]
    date_idx = {}
    for i, d in enumerate(dates):
        date_idx.setdefault(d, i)

    atr_all = _atr_array(high, low, close, 14)
    n = len(close)

    results = []
    for t in trades_detail:
        d_str = str(t["entry_date"])[:10]
        i = date_idx.get(d_str)
        if i is None or i < 1:
            continue
        direction = t["dir"]
        atr_val = atr_all[i - 1]
        if atr_val is None or atr_val <= 0 or math.isnan(atr_val):
            continue
        entry = float(open_[i])
        regime = t.get("regime", "震荡")

        rg = risk_gate(sym, entry, atr_val, cfg)
        if not rg.get("passed"):
            continue
        ep = exit_plan(sym, entry, direction, atr_val, regime, cfg)
        sd = ep["stop_dist"]
        if sd <= 0:
            continue

        # 每个加仓档：先定位"首达触发档"的 bar，再测首达博弈
        rec = {
            "symbol": sym, "direction": direction, "regime": regime,
            "strategy": t.get("strategy", ""), "R_adj": t.get("R_adj", 0),
        }
        end = min(i + max_hold, n)
        for name, trig_r, stop_r, tgt_r in ADDONS:
            # 定位首达触发档（在持仓可能存活的窗口内）
            trig_bar = None
            trig_price = entry + direction * trig_r * sd
            for j in range(i, end):
                ext = high[j] if direction > 0 else low[j]
                if (ext - entry) * direction >= trig_r * sd:
                    trig_bar = j
                    break
            if trig_bar is None:
                rec[name] = None
                continue
            # 从触发 bar 的下一根开始测首达博弈（触发当根已计入入场）
            rec[name] = first_passage(
                high, low, close, trig_bar + 1, end, direction, entry, sd,
                tgt_r, stop_r,
            )
        results.append(rec)
    return results


def main():
    with open(LABELS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    all_trades = []
    for sym, r in data.items():
        trades = r.get("trades_detail", [])
        if not trades:
            continue
        res = analyze_symbol(sym, trades, DEFAULT_CONFIG)
        if res:
            all_trades.extend(res)

    n = len(all_trades)
    print(f"金字塔加仓诊断 v3：首达博弈（加仓单精确期望）")
    print(f"样本：{n} 笔，持仓观察窗 120 bar")
    print(f"{'='*70}")
    print()
    print("随机漫步基准（无漂移时的 P(先到目标)）：")
    for name, base in BASELINE.items():
        print(f"  {name}: {base:.1%}")
    print()

    def game_stats(trades, name):
        valid = [t[name] for t in trades if t.get(name)]
        if not valid:
            return None
        tgt = valid.count("target")
        stp = valid.count("stop")
        tmo = valid.count("timeout")
        total = len(valid)
        p = tgt / total
        win_r, lose_r = PAYOFF[name]
        ev = p * win_r - (stp / total) * lose_r
        return {"n": total, "p": p, "stop": stp / total, "timeout": tmo / total, "ev": ev}

    # ── 1) 全样本首达博弈 ──
    print("【全样本】加仓档位首达博弈")
    print(f"{'档位':<5} {'n':>5} {'P(先到目标)':>10} {'P(先触保本)':>10} {'超时':>6} {'期望(R)':>8} {'vs基准':>7}")
    for name, trig_r, stop_r, tgt_r in ADDONS:
        s = game_stats(all_trades, name)
        if s:
            base = BASELINE[name]
            edge = s["p"] - base
            print(f"{name:<5} {s['n']:>5} {s['p']:>10.1%} {s['stop']:>10.1%} {s['timeout']:>6.1%} "
                  f"{s['ev']:>+8.3f} {edge:>+7.1%}")
    print()

    # ── 2) 按 regime 分层 ──
    print("【状态分层】A1 档（+0.5R 加仓）P(先到1.5R)")
    by_regime = {}
    for t in all_trades:
        by_regime.setdefault(t["regime"], []).append(t)
    for rg, trades in sorted(by_regime.items(), key=lambda x: -len(x[1])):
        s = game_stats(trades, "A1")
        s2 = game_stats(trades, "A2")
        if s:
            extra = f"  A2: P={s2['p']:.1%}, EV={s2['ev']:+.3f}R" if s2 else ""
            print(f"  {rg:<4} n={s['n']:>5}  A1: P={s['p']:.1%}(基准25%), EV={s['ev']:+.3f}R{extra}")
    print()

    # ── 3) 按策略标签分层 ──
    print("【策略标签分层】A1 档 P(先到1.5R)")
    by_strat = {}
    for t in all_trades:
        by_strat.setdefault(t["strategy"], []).append(t)
    for st, trades in sorted(by_strat.items(), key=lambda x: -len(x[1])):
        s = game_stats(trades, "A1")
        if s:
            print(f"  {st:<6} n={s['n']:>5}  P={s['p']:.1%}, EV={s['ev']:+.3f}R")
    print()

    # ── 4) 品种分层（A1 EV 排序，样本≥30）──
    print("【品种分层】A1 档期望（R/次），样本≥30，前 10 / 后 5")
    by_sym = {}
    for t in all_trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    rows = []
    for sym, trades in by_sym.items():
        s = game_stats(trades, "A1")
        if s and s["n"] >= 30:
            rows.append((sym, SYMBOLS.get(sym, {}).get("name", sym), s["n"], s["p"], s["ev"]))
    rows.sort(key=lambda x: -x[4])
    print(f"  {'品种':<6} {'名称':<6} {'n':>4} {'P(先到1.5R)':>10} {'EV(R/次)':>9}")
    for sym, name, cnt, p, ev in rows[:10]:
        print(f"  {sym:<6} {name:<6} {cnt:>4} {p:>10.1%} {ev:>+9.3f}")
    print("  ...")
    for sym, name, cnt, p, ev in rows[-5:]:
        print(f"  {sym:<6} {name:<6} {cnt:>4} {p:>10.1%} {ev:>+9.3f}")
    print()

    # ── 5) 胜率反馈冲突预演 ──
    print("【胜率结构】A1 加仓单的胜率（P(先到目标)）——金字塔单是'低胜率高盈亏比'结构")
    s = game_stats(all_trades, "A1")
    if s:
        print(f"  全样本 A1 胜率 {s['p']:.1%}（vs 近20笔胜率<40%转防御的现行规则）")
        print(f"  → 若胜率反馈不分桶，金字塔单会持续触发防御降仓")


if __name__ == "__main__":
    main()
