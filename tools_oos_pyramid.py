#!/usr/bin/env python3
"""金字塔加仓 5 折 OOS 验证（P2，2026-08-31）。

方法：同路径 A/B 重放。
- 基线：直接用 walk_forward_backtest 记录的 R_adj（零复现漂移）
- 金字塔：同一价格路径上重放保守阶梯（P1 +1.0R 加 50% / P2 +1.5R 加 25% /
  止损逐级上移 0R→+0.5R / +2.0R 起峰值回撤 1.0R 离场），成本按实际执行次数计
- Δ = R_pyr_adj − R_base_adj，即"同一信号两种持仓管理"的差值

门控子集：白名单 10 品种 × regime=="趋势"（classify_regime 代理 Layer 0 趋势初/中）
× 策略标签 ∈ {趋势, 背离}。

通过标准（与 cluster_w 三批同款纪律）：≥3/5 折 Δ 为正 且 全样本 Δ > 0。

引擎忠实性检查：基线模式重放 vs 记录 R，一致率应 >90%（换月跳空/尾仓逻辑同款）。

intrabar 语义：与 walk_forward 引擎一致——止损优先于触发判定；当根触发的加仓与
新止损自下一根生效（该引擎对 t2→尾仓激活同样处理）。
"""

import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from four_dim_strategy import (
    DEFAULT_CONFIG, ROLL_GAP_MULT, ROLL_GAP_PCT, _atr_array, exit_plan,
    get_slip_pts, load_daily,
)
from pyramid_addon import (
    PYRAMID_LADDER, PYRAMID_TRAIL_DIST_R, PYRAMID_TRAIL_START_R,
    PYRAMID_WHITELIST,
)

LABELS_FILE = Path(__file__).parent / "backtest_strategy_labels.json"
N_FOLDS = 5
GATE_LABELS = {"趋势", "背离"}
GATE_REGIME = "趋势"  # classify_regime 值；Layer 0 trend_early/mid 的数据侧代理


def replay(sym, arrays, entry_idx, direction, entry, atr_val, regime, cfg, mode):
    """重放一笔交易。mode='baseline' 返回 (R_raw, reason)（一致性检查用）；
    mode='pyramid' 返回 (R_adj, detail)。

    arrays: dict(high/low/close/open, n, dates)
    entry_idx: 入场 bar 索引（= 信号 bar + 1）
    """
    high, low, close, open_ = arrays["high"], arrays["low"], arrays["close"], arrays["open"]
    n = arrays["n"]
    d = direction

    ep = exit_plan(sym, entry, d, atr_val, regime, cfg)
    sd = ep["stop_dist"]
    if sd <= 0:
        return None

    mv = cfg.get("contract_specs", {}).get(sym, {}).get("multiplier", 10)
    fee = cfg.get("contract_specs", {}).get(sym, {}).get("fee", 5.0)
    slip = get_slip_pts(sym, cfg)
    per_exec_cost = slip / sd + fee / (sd * mv) if sd > 0 else 0.0

    if mode == "baseline":
        # ── 与 walk_forward 同款：止损 / t2 / 尾仓跟踪 ──
        exit_price, reason = None, ""
        tail_active, tail_stop = False, None
        for j in range(entry_idx, n):
            hi, lo = high[j], low[j]
            if j > entry_idx:
                gap = abs(open_[j] - close[j - 1])
                if gap > max(ROLL_GAP_PCT * close[j - 1], ROLL_GAP_MULT * sd):
                    continue
            if tail_active:
                if d > 0:
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
            if d > 0:
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
            exit_price, reason = close[-1], "期末平"
        r_raw = (exit_price - entry) / sd if d > 0 else (entry - exit_price) / sd
        return r_raw, reason

    # ── 金字塔模式 ──
    # 状态（全部以 R 计，相对 entry）
    units = 1.0
    unit_entries = [0.0]            # 各单位的入场 R 偏移
    stop_r = -1.0                   # 当前全仓止损（R）
    p1_done = p2_done = False
    trail_active = False
    peak_r = 0.0
    exit_r = None
    exit_reason = ""

    p1_trig = PYRAMID_LADDER[0]["trigger_r"]   # 1.0
    p1_add = PYRAMID_LADDER[0]["add_ratio"]    # 0.5
    p2_trig = PYRAMID_LADDER[1]["trigger_r"]   # 1.5
    p2_add = PYRAMID_LADDER[1]["add_ratio"]    # 0.25
    p2_stop_r = PYRAMID_LADDER[1]["new_stop_r"]  # 0.5

    for j in range(entry_idx, n):
        hi, lo = high[j], low[j]
        # 换月跳空跳过（与基线同款：入场根不跳过）
        if j > entry_idx:
            gap = abs(open_[j] - close[j - 1])
            if gap > max(ROLL_GAP_PCT * close[j - 1], ROLL_GAP_MULT * sd):
                continue

        # 1) 止损判定（当根生效的止损位，与 walk_forward 同为 bar 开端状态）
        stop_price = entry + d * stop_r * sd
        hit_stop = lo <= stop_price if d > 0 else hi >= stop_price
        if hit_stop:
            exit_r, exit_reason = stop_r, "阶梯止损" if stop_r > 0 else ("保本离场" if stop_r == 0 else "止损")
            break

        # 2) 触发加仓（当根末端处理，新止损下一根生效——与 t2→尾仓激活同语义）
        bar_ext_r = (hi - entry) / sd if d > 0 else (entry - lo) / sd
        if not p1_done and bar_ext_r >= p1_trig:
            units += p1_add
            unit_entries.append(p1_trig)
            p1_done = True
            stop_r = 0.0  # 保本
        if p1_done and not p2_done and bar_ext_r >= p2_trig:
            units += p2_add
            unit_entries.append(p2_trig)
            p2_done = True
            stop_r = p2_stop_r

        # 3) 峰值/移动止损更新（下一根生效）
        peak_r = max(peak_r, bar_ext_r)
        if p2_done and peak_r >= PYRAMID_TRAIL_START_R:
            trail_active = True
        if trail_active:
            stop_r = max(stop_r, peak_r - PYRAMID_TRAIL_DIST_R)

    if exit_r is None:
        exit_r = (close[-1] - entry) / sd if d > 0 else (entry - close[-1]) / sd
        exit_reason = "期末平"

    # 金字塔 R：各单位 P&L 之和（单位归一化）
    r_raw = sum(u * (exit_r - e) for u, e in zip([1.0, p1_add if p1_done else 0, p2_add if p2_done else 0], [0.0, p1_trig, p2_trig]))
    # 成本按实际执行次数：入场 + 加仓次数 + 出场
    n_exec = 2 + (1 if p1_done else 0) + (1 if p2_done else 0)
    r_adj = r_raw - n_exec * per_exec_cost

    detail = {
        "p1_done": p1_done, "p2_done": p2_done,
        "units": round(units, 2), "exit_r": round(exit_r, 3),
        "exit_reason": exit_reason, "n_exec": n_exec,
        "r_raw": round(r_raw, 3), "r_adj": round(r_adj, 3),
    }
    return r_adj, detail


def main():
    with open(LABELS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    cfg = DEFAULT_CONFIG

    # 收集门控交易 + 数据缓存
    df_cache = {}
    gated = []          # 全门控
    no_label = []       # 消融：去标签门
    no_regime = []      # 消融：去行情门
    base_all = []       # 一致性检查样本（全白名单）

    for sym, r in data.items():
        if sym not in PYRAMID_WHITELIST:
            continue
        trades = r.get("trades_detail", [])
        if not trades:
            continue
        df = load_daily(sym)
        if df is None or len(df) < 100:
            continue
        dates = [str(x)[:10] for x in df.index]
        date_idx = {}
        for k, dt in enumerate(dates):
            date_idx.setdefault(dt, k)
        arrays = {
            "high": df["high"].values, "low": df["low"].values,
            "close": df["close"].values, "open": df["open"].values,
            "n": len(df), "dates": dates,
        }
        df_cache[sym] = (df, arrays, date_idx)
        atr_all = _atr_array(arrays["high"], arrays["low"], arrays["close"], 14)

        for t in trades:
            d_str = str(t["entry_date"])[:10]
            k = date_idx.get(d_str)
            if k is None or k < 1:
                continue
            entry = float(arrays["open"][k])
            atr_val = atr_all[k - 1]
            if atr_val is None or atr_val <= 0 or math.isnan(atr_val):
                continue
            rec = {"sym": sym, "t": t, "k": k, "entry": entry, "atr": atr_val}
            base_all.append(rec)
            if t.get("regime") == GATE_REGIME and t.get("strategy") in GATE_LABELS:
                gated.append(rec)
            if t.get("regime") == GATE_REGIME:
                no_label.append(rec)
            if t.get("strategy") in GATE_LABELS:
                no_regime.append(rec)

    print(f"金字塔 5 折 OOS 验证（同路径 A/B 重放）")
    print(f"白名单品种: {sorted(PYRAMID_WHITELIST)}")
    print(f"{'='*70}")
    print(f"门控交易：全门控 {len(gated)} 笔 | 去标签门 {len(no_label)} | 去行情门 {len(no_regime)}")
    print()

    # ── 引擎忠实性检查（基线模式 vs 记录 R_raw）──
    print("【引擎忠实性检查】基线重放 vs 记录 R（抽样 300 笔）")
    sample = base_all[::max(1, len(base_all) // 300)][:300]
    diffs, ok = [], 0
    for rec in sample:
        sym, t, k = rec["sym"], rec["t"], rec["k"]
        df, arrays, _ = df_cache[sym]
        out = replay(sym, arrays, k, t["dir"], rec["entry"], rec["atr"], t.get("regime", "震荡"), cfg, "baseline")
        if out is None:
            continue
        r_rep, reason = out
        diffs.append(abs(r_rep - t["R"]))
        if abs(r_rep - t["R"]) < 0.05:
            ok += 1
    rate = ok / len(diffs) if diffs else 0
    print(f"  抽样 {len(diffs)} 笔: |ΔR|<0.05 一致率 {rate:.1%}, 中位偏差 {np.median(diffs):.4f}")
    if rate < 0.85:
        print("  ⚠️ 一致率偏低——金字塔侧结果需谨慎解读")
    print()

    # ── 主验证：全门控 5 折 OOS ──
    print(f"【主验证】全门控子集（白名单 × {GATE_REGIME} × {'/'.join(sorted(GATE_LABELS))}标签），{N_FOLDS} 折")
    deltas = []
    for rec in gated:
        sym, t, k = rec["sym"], rec["t"], rec["k"]
        df, arrays, _ = df_cache[sym]
        out = replay(sym, arrays, k, t["dir"], rec["entry"], rec["atr"], t.get("regime", "震荡"), cfg, "pyramid")
        if out is None:
            continue
        r_pyr, detail = out
        deltas.append({
            "sym": sym, "date": str(t["entry_date"])[:10],
            "delta": r_pyr - t["R_adj"],
            "base": t["R_adj"], "pyr": r_pyr, "detail": detail,
        })

    deltas.sort(key=lambda x: x["date"])
    n = len(deltas)
    print(f"  有效重放 {n} 笔")
    d_arr = np.array([x["delta"] for x in deltas])
    print(f"  全样本 Δ: 合计 {d_arr.sum():+.2f}R, 均值 {d_arr.mean():+.4f}R/笔, "
          f"中位 {np.median(d_arr):+.4f}R, 改善比例 {(d_arr > 0).mean():.1%}")
    # 无额外成本口径（金字塔按 2 次执行计）
    print()

    fold_size = n // N_FOLDS
    fold_results = []
    for f in range(N_FOLDS):
        s = f * fold_size if f < N_FOLDS - 1 else (N_FOLDS - 1) * fold_size
        e = (f + 1) * fold_size if f < N_FOLDS - 1 else n
        fd = d_arr[s:e]
        fold_results.append({
            "fold": f + 1, "n": len(fd), "sum": float(fd.sum()),
            "mean": float(fd.mean()) if len(fd) else 0,
            "pos_rate": float((fd > 0).mean()) if len(fd) else 0,
        })
    print(f"  {'折':>3} {'n':>5} {'Δ合计(R)':>10} {'Δ均值':>9} {'改善比例':>8}")
    for fr in fold_results:
        print(f"  {fr['fold']:>3} {fr['n']:>5} {fr['sum']:>+10.2f} {fr['mean']:>+9.4f} {fr['pos_rate']:>8.1%}")
    pos_folds = sum(1 for fr in fold_results if fr["sum"] > 0)
    total = float(d_arr.sum())
    print(f"  正折数: {pos_folds}/{N_FOLDS} | 全样本 Δ 合计: {total:+.2f}R")
    verdict = "PASS ✅" if (pos_folds >= 3 and total > 0) else "FAIL ❌"
    print(f"  判定: {verdict}（标准: ≥3/5 折正 且 全样本 Δ>0）")
    print()

    # ── 品种分解 ──
    print("【品种分解】Δ 合计（R），按贡献排序")
    by_sym = {}
    for x in deltas:
        by_sym.setdefault(x["sym"], []).append(x["delta"])
    rows = [(s, len(v), float(np.sum(v)), float(np.mean(v))) for s, v in by_sym.items()]
    rows.sort(key=lambda r: -r[2])
    print(f"  {'品种':<5} {'n':>4} {'Δ合计':>8} {'Δ均值':>8}")
    for s, cnt, sm, mn in rows:
        print(f"  {s:<5} {cnt:>4} {sm:>+8.2f} {mn:>+8.4f}")
    print()

    # ── 出场结构分解 ──
    print("【出场结构】金字塔模式下的离场原因分布")
    reason_cnt = Counter(x["detail"]["exit_reason"] for x in deltas)
    for rsn, c in reason_cnt.most_common():
        sub = [x["delta"] for x in deltas if x["detail"]["exit_reason"] == rsn]
        print(f"  {rsn:<8} n={c:>4} ({c/n:>5.1%})  Δ均值 {np.mean(sub):+.4f}")
    p1_rate = sum(1 for x in deltas if x["detail"]["p1_done"]) / n
    p2_rate = sum(1 for x in deltas if x["detail"]["p2_done"]) / n
    print(f"  P1 触发率 {p1_rate:.1%} | P2 触发率 {p2_rate:.1%}")
    print()

    # ── 消融：各门的边际价值 ──
    print("【消融】各门边际价值（全样本 Δ 合计）")
    for name, subset in [("全门控", gated), ("去标签门", no_label), ("去行情门", no_regime)]:
        ds = []
        for rec in subset:
            sym, t, k = rec["sym"], rec["t"], rec["k"]
            df, arrays, _ = df_cache[sym]
            out = replay(sym, arrays, k, t["dir"], rec["entry"], rec["atr"], t.get("regime", "震荡"), cfg, "pyramid")
            if out is None:
                continue
            ds.append(out[0] - t["R_adj"])
        if ds:
            arr = np.array(ds)
            print(f"  {name:<6} n={len(ds):>5}  Δ合计 {arr.sum():+9.2f}R  Δ均值 {arr.mean():+.4f}")
    print()

    # ── 结论输出 ──
    print("【结论】")
    print(f"  金字塔阶梯 vs 原 T1/T2（回测口径：止损/t2/尾仓）:")
    print(f"  门控子集每笔 Δ {d_arr.mean():+.4f}R，{pos_folds}/{N_FOLDS} 折为正，合计 {total:+.2f}R")
    if verdict.startswith("PASS"):
        print("  → OOS 验证通过，金字塔模块具备小仓实盘资格（建议 0.5× 缩放先执行 P1）")
    else:
        print("  → OOS 未通过——维持影子模式，不进入实盘")


if __name__ == "__main__":
    main()
