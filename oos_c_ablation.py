#!/usr/bin/env python3
"""
四维策略 · 资金面 C 维度贡献消融（OOS）
=====================================
同一份本地日线数据 + 同一份 DEFAULT_CONFIG（含真实 cpos_cache 龙虎榜历史），
对 6 个品种各跑两遍 walk_forward_backtest：
  · C_on  = ablate=None   → C 用真实龙虎榜历史（cpos_cache.json 已回填）
  · C_off = ablate="C"    → 管线内强制 C=0（其余维度/权重/数据完全一致）

两遍唯一差异 = 资金面 C 是否注入，因此 Δ 即为 C 维度的边际贡献（OOS 口径）。
复用 four_dim_oos_compare 的 summarize + 断点续跑 + 单品种超时逻辑。

用法：
  python3 oos_c_ablation.py            # 默认 6 品种
  python3 oos_c_ablation.py jd lh      # 指定品种
"""
import copy
import fcntl
import json
import os
import signal
import sys
import time

import numpy as np

import four_dim_strategy as fd

PER_SYM_TIMEOUT = 1200  # 单品种(on+off两遍)超时，超时跳过防卡死

DEFAULT_TARGETS = ["jd", "lh", "FG", "SA", "JM", "J"]
OUT_NAME = "oos_c_ablation_result.json"


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
    tail = reasons.get("尾仓离场", 0)
    return {
        "trades": n,
        "expR": round(expR, 4),
        "total_R": round(float(np.sum(Rs)), 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "win_rate": round(win, 3),
        "max_dd_R": round(max_drawdown(Rs), 3),
        "t2_rate": round(t2 / n, 3) if n else 0.0,
        "tail_share": round(tail / n, 3) if n else 0.0,
        "by_regime": r.get("by_regime", {}),
    }


def run_one(symbol, cfg, ablate, df_in=None):
    try:
        return fd.walk_forward_backtest(symbol, cfg, ablate=ablate, df_in=df_in)
    except Exception as e:
        return {"symbol": symbol, "trades": 0, "note": f"异常:{repr(e)[:60]}", "trades_detail": []}


def slice_c_window(symbol):
    """取 C 数据真实存在的回测窗口（min C 日期起 → 日线末尾），
    使对比区间内 C 逐日真实、无'最新值回落'泄漏。返回 (df_slice, c_start, c_end)。"""
    import json
    CPOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpos_cache.json")
    c = json.load(open(CPOS, encoding="utf-8"))
    # 缓存键统一大写（与 precompute_C_array/score_C 的 .upper() 查表一致）
    entry = c.get(symbol) or c.get(symbol.upper()) or c.get(symbol.lower())
    h = entry.get("history", []) if entry else []
    if not h:
        return None, None, None
    cmin = min(h, key=lambda x: x["date"])["date"]
    cmax = max(h, key=lambda x: x["date"])["date"]
    df = fd.load_daily(symbol)
    if df is None:
        return None, cmin, cmax
    # cmin 形如 '20251107' → 截到该日及之后
    cut = f"{cmin[:4]}-{cmin[4:6]}-{cmin[6:8]}"
    df_s = df[df.index >= cut]
    return df_s, cmin, cmax


def _build_out(targets, rows):
    out = {
        "targets": targets,
        "note": "C_on=ablate=None(真实龙虎榜C) C_off=ablate='C'(C强制0)；回测窗口=各品种C真实区间(~10个月)；其余同DEFAULT_CONFIG",
        "rows": [
            {
                "symbol": s,
                "C_on": a,
                "C_off": b,
                "delta_expR": round(a["expR"] - b["expR"], 4),
                "delta_total_R": round(a["total_R"] - b["total_R"], 2),
                "delta_win": round(a["win_rate"] - b["win_rate"], 3),
                "delta_dd": round(a["max_dd_R"] - b["max_dd_R"], 3),
                "delta_pf": (round(a["profit_factor"] - b["profit_factor"], 2)
                             if a["profit_factor"] is not None and b["profit_factor"] is not None else None),
            }
            for s, a, b in rows
        ],
    }
    rs = out["rows"]
    valid = [x for x in rs if x["C_on"]["trades"] > 0 and x["C_off"]["trades"] > 0]
    n_imp = sum(1 for x in valid if x["delta_expR"] > 0.01)
    n_dec = sum(1 for x in valid if x["delta_expR"] < -0.01)
    n_flat = len(valid) - n_imp - n_dec
    avg_de = round(sum(x["delta_expR"] for x in valid) / len(valid), 4) if valid else 0.0
    avg_dt = round(sum(x["delta_total_R"] for x in valid) / len(valid), 2) if valid else 0.0
    out["summary"] = {
        "n_total": len(rs),
        "n_valid": len(valid),
        "n_improve": n_imp,
        "n_degrade": n_dec,
        "n_flat": n_flat,
        "avg_delta_expR": avg_de,
        "avg_delta_total_R": avg_dt,
    }
    return out


def main():
    global OUT_NAME
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_combined = "--combined" in sys.argv
    targets = args if args else DEFAULT_TARGETS
    # 必须在续跑检查前确定输出文件，避免误读另一模式的残留结果
    if use_combined:
        OUT_NAME = "oos_c_ablation_combined.json"

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".oos_c_ablation_lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[单实例] 已有回测实例运行中，本进程退出", flush=True)
        sys.exit(0)

    rows = []
    completed_syms = set()
    if os.path.exists(OUT_NAME):
        try:
            with open(OUT_NAME, encoding="utf-8") as f:
                prev = json.load(f)
            for rr in prev.get("rows", []):
                rows.append((rr["symbol"], rr["C_on"], rr["C_off"]))
                completed_syms.add(rr["symbol"])
            print(f"[续跑] 已恢复 {len(completed_syms)} 个品种", flush=True)
        except Exception as e:
            print(f"[续跑] 读取失败，从头跑: {e}", flush=True)

    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    if use_combined:
        cfg.setdefault("bias_synthesis", {})["direction_mode"] = "combined"
    todo = [s for s in targets if s not in completed_syms]
    print("=" * 82, flush=True)
    print("资金面 C 维度贡献消融：C_on(真实龙虎榜) vs C_off(C=0)")
    print("回测窗口 = 各品种 C 数据真实存在的区间（~10个月），避免旧 bar 的'最新值回落'泄漏", flush=True)
    print(f"数据：本地日线 ｜ 回测：walk_forward_backtest ｜ 品种：{targets}", flush=True)
    print("=" * 82, flush=True)
    hdr = (f"{'品种':4} {'模式':5} {'笔':>4} {'期望R':>8} {'总R':>9} {'PF':>6} "
           f"{'胜率':>7} {'最大回撤R':>9}")
    print(hdr)
    print("-" * 82)

    signal.signal(signal.SIGALRM, _on_alarm)
    for sym in todo:
        t0 = time.time()
        df_in, cmin, cmax = slice_c_window(sym)
        if df_in is None or len(df_in) < 80:
            print(f"[跳过] {sym} C窗口切片不足或无数据", flush=True)
            continue
        print(f"[窗口] {sym} C真实区间 {cmin}→{cmax}  回测 {len(df_in)} 根", flush=True)
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            r_on = summarize(run_one(sym, cfg, None, df_in=df_in))
            r_off = summarize(run_one(sym, cfg, "C", df_in=df_in))
        except TimeoutError:
            print(f"[超时跳过] {sym} 超过 {PER_SYM_TIMEOUT}s", flush=True)
            continue
        except Exception as e:
            print(f"[异常跳过] {sym}: {repr(e)[:200]}", flush=True)
            continue
        finally:
            signal.alarm(0)
        rows.append((sym, r_on, r_off))
        for tag, r in (("C_on ", r_on), ("C_off", r_off)):
            pf = r["profit_factor"] if r["profit_factor"] is not None else "∞"
            print(
                f"{sym:4} {tag:5} {r['trades']:>4} {r['expR']:>8} {r['total_R']:>9} "
                f"{str(pf):>6} {r['win_rate'] * 100:>6.1f}% {r['max_dd_R']:>9}",
                flush=True,
            )
        de = r_on["expR"] - r_off["expR"]
        dt = r_on["total_R"] - r_off["total_R"]
        verdict = "C有增益▲" if de > 0.01 else ("C有损▼" if de < -0.01 else "C无影响=")
        print(f"   └ ΔexpR={de:+.3f}  Δ总R={dt:+.2f}  → {verdict}  ({time.time()-t0:.0f}s)", flush=True)
        print(flush=True)
        out = _build_out(targets, rows)
        with open(OUT_NAME, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    out = _build_out(targets, rows)
    print("=" * 82, flush=True)
    print(f"结果已写入 {OUT_NAME}", flush=True)
    valid = [x for x in out["rows"] if x["C_on"]["trades"] > 0 and x["C_off"]["trades"] > 0]
    if valid:
        print(
            f"汇总（{len(valid)} 有效）：{out['summary']['n_improve']} C增益 / "
            f"{out['summary']['n_degrade']} C有损 / {out['summary']['n_flat']} 持平 ｜ "
            f"平均ΔexpR={out['summary']['avg_delta_expR']:+.4f} ｜ "
            f"平均Δ总R={out['summary']['avg_delta_total_R']:+.2f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
