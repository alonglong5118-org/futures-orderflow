#!/usr/bin/env python3
"""
全市场 direction_mode OOS 对比：threshold(当前默认) vs combined(候选改进)
=================================================================
同一份本地日线 + 同一份 DEFAULT_CONFIG（含真实 F=fundamentals.json / C=cpos_cache.json），
对每个有效全市场品种各跑两遍 walk_forward_backtest（不调参 walk-forward = OOS 口径）：
  · threshold = direction_mode="threshold"  → dir 由 T_5m 决定，F/C 仅调制 T 阈值
  · combined  = direction_mode="combined"   → dir=sign(T_5m + 0.5·bias_G)，F/C 直接参与定方向

为剔除「无 C 长历史品种回测早期的 C 最新值回落泄漏」，主对比两模式均 ablate="C"(C=0 中性)，
纯测 **F 维度参与定方向** 的价值（这恰是多数无 C 数据品种的现实假设）。
补充对比：仅 6 个拥有真实长 C 历史的品种（FG/SA/JM/J/JD/LH），用真实 C + slice_c_window
切到 C 真实区间，看 **F+C 共同定方向** 的真实增益。

输出：
  oos_dir_mode_main.json   （全市场有效品种，ablate=C）
  oos_dir_mode_c_real.json （6 真实 C 品种，真实 C + slice）
用法：
  python3 oos_direction_mode_compare.py
"""
import copy
import fcntl
import json
import os
import signal
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import four_dim_strategy as fd

PER_SYM_TIMEOUT = 300  # 单品种(on+off两遍)超时
MAIN_OUT = os.path.join(HERE, "oos_dir_mode_main.json")
CREAL_OUT = os.path.join(HERE, "oos_dir_mode_c_real.json")

# 拥有真实长 C 历史（cpos_cache history >= ~100 天）的品种
LONG_C = ["FG", "SA", "JM", "J", "JD", "LH"]


def _on_alarm(signum, frame):
    raise TimeoutError("per-symbol timeout")


def max_drawdown(Rs):
    if not Rs:
        return 0.0
    eq = np.cumsum(np.array(Rs, dtype=float))
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def summarize(r):
    trades = r.get("trades_detail") or []
    Rs = [t["R_adj"] for t in trades]
    n = len(Rs)
    expR = float(np.mean(Rs)) if n else 0.0
    wins = [x for x in Rs if x > 0]
    losses = [x for x in Rs if x < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (float("inf") if gross_win > 0 else 0.0)
    win = len(wins) / n if n else 0.0
    reasons = r.get("exit_reasons", {})
    t2 = reasons.get("止盈2R", 0) + reasons.get("尾仓离场", 0)
    return {
        "trades": n,
        "expR": round(expR, 4),
        "total_R": round(float(np.sum(Rs)), 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "win_rate": round(win, 3),
        "max_dd_R": round(max_drawdown(Rs), 3),
        "t2_rate": round(t2 / n, 3) if n else 0.0,
        "by_regime": r.get("by_regime", {}),
    }


def run_one(symbol, cfg, ablate, df_in=None):
    try:
        return fd.walk_forward_backtest(symbol, cfg, ablate=ablate, df_in=df_in)
    except Exception as e:
        return {"symbol": symbol, "trades": 0, "note": f"异常:{repr(e)[:60]}", "trades_detail": []}


def slice_c_window(symbol):
    """取 C 数据真实存在的回测窗口（min C 日期起 → 日线末尾），避免旧 bar 的'最新值回落'泄漏。"""
    CPOS = os.path.join(HERE, "cpos_cache.json")
    c = json.load(open(CPOS, encoding="utf-8"))
    entry = c.get(symbol) or c.get(symbol.upper()) or c.get(symbol.lower())
    h = entry.get("history", []) if entry else []
    if not h:
        return None, None, None
    cmin = min(h, key=lambda x: x["date"])["date"]
    cmax = max(h, key=lambda x: x["date"])["date"]
    df = fd.load_daily(symbol)
    if df is None:
        return None, cmin, cmax
    cut = f"{cmin[:4]}-{cmin[4:6]}-{cmin[6:8]}"
    df_s = df[df.index >= cut]
    return df_s, cmin, cmax


def valid_targets():
    """系统有效覆盖品种 = SYMBOLS − DISABLED_SYMBOLS 且有足够本地日线。"""
    out = []
    for s in fd.SYMBOLS:
        if s in fd.DISABLED_SYMBOLS:
            continue
        df = fd.load_daily(s)
        if df is not None and len(df) >= 100:
            out.append(s)
    return out


def _build_out(targets, rows, c_mode, window_note):
    out = {
        "targets": targets,
        "c_mode": c_mode,
        "window_note": window_note,
        "note": "threshold=direction_mode='threshold'(F/C仅调阈值)  vs  combined=direction_mode='combined'(F/C定方向)；"
                "walk-forward 不调参=OOS口径；其余同DEFAULT_CONFIG",
        "rows": [
            {
                "symbol": s,
                "name": fd.SYMBOLS.get(s, {}).get("name", s),
                "group": fd.SYMBOLS.get(s, {}).get("group", "?"),
                "has_real_C": s in LONG_C,
                "thr": a,
                "comb": b,
                "delta_expR": round(b["expR"] - a["expR"], 4),
                "delta_total_R": round(b["total_R"] - a["total_R"], 2),
                "delta_win": round(b["win_rate"] - a["win_rate"], 3),
                "delta_dd": round(b["max_dd_R"] - a["max_dd_R"], 3),
                "delta_pf": (round(b["profit_factor"] - a["profit_factor"], 2)
                             if b["profit_factor"] is not None and a["profit_factor"] is not None else None),
                "delta_trades": b["trades"] - a["trades"],
            }
            for s, a, b in rows
        ],
    }
    rs = out["rows"]
    valid = [x for x in rs if x["thr"]["trades"] > 0 and x["comb"]["trades"] > 0]
    n_imp = sum(1 for x in valid if x["delta_expR"] > 0.01)
    n_dec = sum(1 for x in valid if x["delta_expR"] < -0.01)
    n_flat = len(valid) - n_imp - n_dec
    avg_de = round(sum(x["delta_expR"] for x in valid) / len(valid), 4) if valid else 0.0
    avg_dt = round(sum(x["delta_total_R"] for x in valid) / len(valid), 2) if valid else 0.0
    avg_dw = round(sum(x["delta_win"] for x in valid) / len(valid), 3) if valid else 0.0
    avg_dd = round(sum(x["delta_dd"] for x in valid) / len(valid), 3) if valid else 0.0
    # 分组：按板块
    by_group = {}
    for x in valid:
        g = x["group"]
        by_group.setdefault(g, []).append(x)
    group_stat = {}
    for g, xs in by_group.items():
        group_stat[g] = {
            "n": len(xs),
            "n_imp": sum(1 for x in xs if x["delta_expR"] > 0.01),
            "n_dec": sum(1 for x in xs if x["delta_expR"] < -0.01),
            "avg_delta_expR": round(sum(x["delta_expR"] for x in xs) / len(xs), 4),
            "avg_delta_total_R": round(sum(x["delta_total_R"] for x in xs) / len(xs), 2),
        }
    # 分组：有C真实 vs 无C长历史
    real_c = [x for x in valid if x["has_real_C"]]
    no_c = [x for x in valid if not x["has_real_C"]]
    def _mini(xs):
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "n_imp": sum(1 for x in xs if x["delta_expR"] > 0.01),
            "n_dec": sum(1 for x in xs if x["delta_expR"] < -0.01),
            "avg_delta_expR": round(sum(x["delta_expR"] for x in xs) / len(xs), 4),
            "avg_delta_total_R": round(sum(x["delta_total_R"] for x in xs) / len(xs), 2),
        }
    out["summary"] = {
        "n_total": len(rs),
        "n_valid": len(valid),
        "n_improve": n_imp,
        "n_degrade": n_dec,
        "n_flat": n_flat,
        "avg_delta_expR": avg_de,
        "avg_delta_total_R": avg_dt,
        "avg_delta_win": avg_dw,
        "avg_delta_dd": avg_dd,
        "by_group": group_stat,
        "by_C_coverage": {"has_real_C": _mini(real_c), "no_real_C": _mini(no_c)},
    }
    return out


def main():
    signal.signal(signal.SIGALRM, _on_alarm)
    targets = valid_targets()
    cfg_thr = copy.deepcopy(fd.DEFAULT_CONFIG)  # 默认 threshold
    cfg_comb = copy.deepcopy(fd.DEFAULT_CONFIG)
    cfg_comb.setdefault("bias_synthesis", {})["direction_mode"] = "combined"

    lock_path = os.path.join(HERE, ".oos_dir_mode_lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[单实例] 已有对比实例运行中，本进程退出", flush=True)
        sys.exit(0)

    print("=" * 90, flush=True)
    print("全市场 direction_mode OOS 对比：threshold(默认) vs combined(候选改进)")
    print(f"品种池 = 系统有效覆盖（SYMBOLS−DISABLED，有日线）：{len(targets)} 个")
    print("主对比：两模式均 ablate='C'(C=0 中性) → 纯测 F 参与定方向的价值", flush=True)
    print("=" * 90, flush=True)
    hdr = (f"{'品种':5}{'板块':5}{'模式':8}{'笔':>4}{'期望R':>9}{'总R':>10}"
           f"{'PF':>7}{'胜率':>8}{'最大回撤R':>10}")
    print(hdr)
    print("-" * 90)

    rows_main = []
    t_all = time.time()
    for sym in targets:
        t0 = time.time()
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            r_thr = summarize(run_one(sym, cfg_thr, "C", None))
            r_comb = summarize(run_one(sym, cfg_comb, "C", None))
        except TimeoutError:
            print(f"[超时跳过] {sym}", flush=True)
            continue
        except Exception as e:
            print(f"[异常跳过] {sym}: {repr(e)[:160]}", flush=True)
            continue
        finally:
            signal.alarm(0)
        rows_main.append((sym, r_thr, r_comb))
        for tag, r in (("thr ", r_thr), ("comb", r_comb)):
            pf = r["profit_factor"] if r["profit_factor"] is not None else "∞"
            print(f"{sym:5}{fd.SYMBOLS.get(sym,{}).get('group','?'):5}{tag:8}{r['trades']:>4}"
                  f"{r['expR']:>9}{r['total_R']:>10}{str(pf):>7}{r['win_rate']*100:>7.1f}%{r['max_dd_R']:>10}",
                  flush=True)
        de = r_comb["expR"] - r_thr["expR"]
        verdict = "▲增益" if de > 0.01 else ("▼有损" if de < -0.01 else "=持平")
        print(f"   └ ΔexpR={de:+.3f}  Δ总R={r_comb['total_R']-r_thr['total_R']:+.2f}  "
              f"Δ笔={r_comb['trades']-r_thr['trades']:+d}  → combined {verdict}  ({time.time()-t0:.1f}s)",
              flush=True)

    out_main = _build_out(targets, rows_main, "ablate=C (C=0 中性)", "全样本 walk-forward OOS")
    with open(MAIN_OUT, "w", encoding="utf-8") as f:
        json.dump(out_main, f, ensure_ascii=False, indent=2)
    print("=" * 90, flush=True)
    print(f"[主对比] 已写入 {MAIN_OUT}  （{time.time()-t_all:.1f}s）", flush=True)
    s = out_main["summary"]
    print(f"  有效 {s['n_valid']} 个：{s['n_improve']} 增益 / {s['n_degrade']} 有损 / {s['n_flat']} 持平 ｜ "
          f"平均ΔexpR={s['avg_delta_expR']:+.4f} ｜ 平均Δ总R={s['avg_delta_total_R']:+.2f}", flush=True)

    # ── 补充对比：6 个真实 C 品种，真实 C + slice ──
    print("\n" + "=" * 90, flush=True)
    print("补充对比：6 个有真实长 C 历史的品种（真实 C + slice 到 C 真实区间）", flush=True)
    print("=" * 90, flush=True)
    rows_creal = []
    for sym in LONG_C:
        df_in, cmin, cmax = slice_c_window(sym)
        if df_in is None or len(df_in) < 80:
            print(f"[跳过] {sym} C窗口切片不足", flush=True)
            continue
        t0 = time.time()
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            r_thr = summarize(run_one(sym, cfg_thr, None, df_in))
            r_comb = summarize(run_one(sym, cfg_comb, None, df_in))
        except TimeoutError:
            print(f"[超时跳过] {sym}", flush=True)
            continue
        except Exception as e:
            print(f"[异常跳过] {sym}: {repr(e)[:160]}", flush=True)
            continue
        finally:
            signal.alarm(0)
        rows_creal.append((sym, r_thr, r_comb))
        for tag, r in (("thr ", r_thr), ("comb", r_comb)):
            pf = r["profit_factor"] if r["profit_factor"] is not None else "∞"
            print(f"{sym:5}{fd.SYMBOLS.get(sym,{}).get('group','?'):5}{tag:8}{r['trades']:>4}"
                  f"{r['expR']:>9}{r['total_R']:>10}{str(pf):>7}{r['win_rate']*100:>7.1f}%{r['max_dd_R']:>10}",
                  flush=True)
        de = r_comb["expR"] - r_thr["expR"]
        verdict = "▲增益" if de > 0.01 else ("▼有损" if de < -0.01 else "=持平")
        print(f"   └ ΔexpR={de:+.3f}  Δ总R={r_comb['total_R']-r_thr['total_R']:+.2f}  "
              f"→ combined {verdict}  (C窗口 {cmin}→{cmax}, {time.time()-t0:.1f}s)", flush=True)

    out_creal = _build_out(LONG_C, rows_creal, "真实C (cpos_cache长历史)", "slice 到 C 真实区间")
    with open(CREAL_OUT, "w", encoding="utf-8") as f:
        json.dump(out_creal, f, ensure_ascii=False, indent=2)
    print("=" * 90, flush=True)
    print(f"[补充] 已写入 {CREAL_OUT}", flush=True)
    sc = out_creal["summary"]
    if sc["n_valid"]:
        print(f"  有效 {sc['n_valid']} 个：{sc['n_improve']} 增益 / {sc['n_degrade']} 有损 / {sc['n_flat']} 持平 ｜ "
              f"平均ΔexpR={sc['avg_delta_expR']:+.4f}", flush=True)
    print(f"\n[总耗时] {time.time()-t_all:.1f}s", flush=True)


if __name__ == "__main__":
    main()
