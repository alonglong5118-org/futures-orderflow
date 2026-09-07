#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
C 感知方向门（threshold 增强）的 IS/OOS 验证
============================================
背景：threshold 模式下 C 维度结构性惰性（kline C 与 dragon C 回测结果逐笔一致）→ 单纯把 C 喂进 score_C 不改交易。
本脚本验证一个**非 combined**的改进：给 threshold 模式加「C 感知方向门」——
  · same_sign        —— T 看多但 C 强负（或反向且 C≠0）则不开多；要求 C 与 T 同向；
  · oppose_threshold —— 仅当 C 与 T 反向且 |C|>阈值(30) 才不开（中性放行，更温和）。
门消费的是**丰富、独立的 kline C**（c_source="kline"，17 年 1 分钟 K 线造）；dragon C 近零→门惰性，故必须用 kline。

验证范围：fd.SYMBOLS 全部真实品种（跳过合成 SA01；main() 再过滤 DISABLED 与无 kline C 的品种）。
依赖 cflow_kline_cache.json（build_kline_cflow_all.py 造，17 年 1 分钟 K 线 → 全部品种 kline C）。
目的：大样本判断「C 感知方向门」是否统计显著地让资金面 C 真正起作用（而非 6 样本过拟合假象）。

防过拟合护栏（同 oos_selective_combined 方法论）：
  1. 决策 ONLY 基于 IS（前 60% 交易日），OOS 全程不参与决策，只用于事后验证；
  2. 启用门的硬性门槛（对主门 same_sign）：
       (a) IS 期 门模式 与 基线 两模式各自交易数均 >= MIN_T(12)；
       (b) IS 期 门期望R − 基线期望R >= MARGIN(0.06)；
       (c) IS 期 门期望R > 0；
  3. 以基线(threshold)交易日范围定共同 IS/OOS 切分点（日历时间），保证同窗口对比。

输出：
  oos_c_gate.json   逐品种原始记录 + 汇总
  oos_c_gate.html   可视化报告
"""
import copy
import fcntl
import json
import os
import signal
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import four_dim_strategy as fd

PER_SYM_TIMEOUT = 300
OUT_JSON = os.path.join(HERE, "oos_c_gate.json")
OUT_HTML = os.path.join(HERE, "oos_c_gate.html")

SPLIT = 0.60
MIN_T = 12
MARGIN = 0.06
GATE_THRESHOLD = 30.0
# 全品种（从 fd.SYMBOLS 派生，跳过合成 SA01；main() 再过滤 DISABLED 与无 kline C 的品种）
# 注意：fd.SYMBOLS 键用小写 jd/lh；score_C 内部会 upper() 查到缓存的大写键，无需手动大写
TARGETS = [s for s in fd.SYMBOLS if s != "SA01"]


def _on_alarm(signum, frame):
    raise TimeoutError("per-symbol timeout")


def max_drawdown(Rs):
    if not Rs:
        return 0.0
    eq = np.cumsum(np.array(Rs, dtype=float))
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def subset_stats(trades):
    if not trades:
        return {"n": 0, "expR": None, "total_R": 0.0, "win_rate": None,
                "max_dd_R": 0.0, "profit_factor": None}
    Rs = [t["R_adj"] for t in trades]
    n = len(Rs)
    expR = float(np.mean(Rs))
    wins = [x for x in Rs if x > 0]
    losses = [x for x in Rs if x < 0]
    gw = sum(wins)
    gl = -sum(losses)
    pf = (gw / gl) if gl > 1e-9 else (float("inf") if gw > 0 else 0.0)
    return {
        "n": n, "expR": round(expR, 4), "total_R": round(float(np.sum(Rs)), 2),
        "win_rate": round(len(wins) / n, 3), "max_dd_R": round(max_drawdown(Rs), 3),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
    }


def split_is_oos(trades, cutoff):
    is_t, oos_t = [], []
    for t in trades:
        d = pd.to_datetime(t["entry_date"])
        (is_t if d <= cutoff else oos_t).append(t)
    return is_t, oos_t


def _binom_two_sided(k, n):
    """精确二项检验（H0: p=0.5，双尾）。返回 p 值；n==0 返回 None。"""
    if n == 0:
        return None
    from math import comb
    p = 0.5

    def pmf(x):
        return comb(n, x) * (p ** x) * ((1 - p) ** (n - x))

    pk = pmf(k)
    pval = sum(pmf(x) for x in range(n + 1) if pmf(x) <= pk + 1e-18)
    return min(1.0, pval)


def _load_kline_cache():
    p = os.path.join(HERE, "cflow_kline_cache.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def run_backtest(sym, c_source, c_gate=None, thr=GATE_THRESHOLD):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    cfg.setdefault("bias_synthesis", {})["c_source"] = c_source
    if c_gate:
        cfg["bias_synthesis"]["c_gate"] = c_gate
        cfg["bias_synthesis"]["c_gate_threshold"] = thr
    return fd.walk_forward_backtest(sym, cfg=cfg)


def decide_and_eval(sym):
    # 基线 = 生产默认（threshold + dragon C，门关）
    r_base = run_backtest(sym, "dragon", None)
    # 门模式 = threshold + kline C（丰富独立信号）+ 门
    r_ss = run_backtest(sym, "kline", "same_sign")
    r_ot = run_backtest(sym, "kline", "oppose_threshold", GATE_THRESHOLD)

    base_td = r_base.get("trades_detail") or []
    ss_td = r_ss.get("trades_detail") or []
    ot_td = r_ot.get("trades_detail") or []
    if not base_td:
        return {"symbol": sym, "skip": True, "reason": "基线无交易"}

    # 共同 IS/OOS 切分点（按基线交易日范围）
    dates = [pd.to_datetime(t["entry_date"]) for t in base_td]
    lo, hi = min(dates), max(dates)
    cutoff = lo + SPLIT * (hi - lo)

    base_is, base_oos = split_is_oos(base_td, cutoff)
    ss_is, ss_oos = split_is_oos(ss_td, cutoff)
    ot_is, ot_oos = split_is_oos(ot_td, cutoff)

    st_base_is, st_base_oos = subset_stats(base_is), subset_stats(base_oos)
    st_ss_is, st_ss_oos = subset_stats(ss_is), subset_stats(ss_oos)
    st_ot_is, st_ot_oos = subset_stats(ot_is), subset_stats(ot_oos)

    # ── 决策（仅 IS，主门 same_sign）──
    is_edge = (st_ss_is["expR"] - st_base_is["expR"]) if (st_ss_is["expR"] is not None and st_base_is["expR"] is not None) else None
    enable = False
    block = []
    if st_ss_is["n"] < MIN_T or st_base_is["n"] < MIN_T:
        block.append(f"IS交易不足(ss={st_ss_is['n']},base={st_base_is['n']}<{MIN_T})")
    if is_edge is None:
        block.append("IS无法计算edge")
    else:
        if is_edge < MARGIN:
            block.append(f"IS增益{is_edge:+.3f}<{MARGIN}")
        if (st_ss_is["expR"] or 0) <= 0:
            block.append("IS_ss_expR<=0")
    if not block:
        enable = True

    # ── OOS 验证 ──
    sel_oos = st_ss_oos if enable else st_base_oos  # 选择性策略 OOS
    base_oos_s = st_base_oos
    delta_ss = (round(st_ss_oos["expR"] - st_base_oos["expR"], 4)
                if (st_ss_oos["expR"] is not None and st_base_oos["expR"] is not None) else None)
    delta_ot = (round(st_ot_oos["expR"] - st_base_oos["expR"], 4)
                if (st_ot_oos["expR"] is not None and st_base_oos["expR"] is not None) else None)

    return {
        "symbol": sym, "name": fd.SYMBOLS.get(sym, {}).get("name", sym),
        "group": fd.SYMBOLS.get(sym, {}).get("group", "?"),
        "cutoff": cutoff.strftime("%Y-%m-%d"),
        "is_window": f"{lo.strftime('%Y-%m-%d')}~{cutoff.strftime('%Y-%m-%d')}",
        "oos_window": f"{cutoff.strftime('%Y-%m-%d')}~{hi.strftime('%Y-%m-%d')}",
        "base_is": st_base_is, "base_oos": st_base_oos,
        "ss_is": st_ss_is, "ss_oos": st_ss_oos,
        "ot_is": st_ot_is, "ot_oos": st_ot_oos,
        "is_edge": round(is_edge, 4) if is_edge is not None else None,
        "enable": enable, "block": block,
        "decision": "启用C门" if enable else "保持threshold",
        "delta_ss_vs_base": delta_ss, "delta_ot_vs_base": delta_ot,
        "ss_gate_skipped": r_ss.get("c_gate_skipped", 0),
        "ot_gate_skipped": r_ot.get("c_gate_skipped", 0),
        "oos_win_ss": (delta_ss is not None and delta_ss > 0.01),
        "oos_loss_ss": (delta_ss is not None and delta_ss < -0.01),
        "oos_win_ot": (delta_ot is not None and delta_ot > 0.01),
        "oos_loss_ot": (delta_ot is not None and delta_ot < -0.01),
    }


def main():
    signal.signal(signal.SIGALRM, _on_alarm)
    targets = [s for s in TARGETS if s in fd.SYMBOLS and s not in getattr(fd, "DISABLED_SYMBOLS", set())]

    lock_fd = open(os.path.join(HERE, ".oos_c_gate_lock"), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[单实例] 已有实例运行中，退出", flush=True)
        sys.exit(0)

    print("=" * 92, flush=True)
    print("C 感知方向门 IS/OOS 验证（threshold 增强，门消费 kline C）")
    print(f"品种池 = 有 kline C 长历史：{targets}")
    print(f"IS={SPLIT:.0%} 护栏：MIN_T={MIN_T}/MARGIN={MARGIN}/ss_IS_expR>0 ｜ 门阈值={GATE_THRESHOLD}")
    print("=" * 92, flush=True)
    hdr = (f"{'品种':5}{'板块':5} {'IS_edge':>8} {'决策':>10} "
           f"{'OOS_ss':>8}{'OOS_base':>9}{'Δss':>8}{'OOS_ot':>8}{'Δot':>8}")
    print(hdr); print("-" * 92)

    records = []
    t_all = time.time()
    for sym in targets:
        t0 = time.time()
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            rec = decide_and_eval(sym)
        except TimeoutError:
            print(f"[超时] {sym}", flush=True); continue
        except Exception as e:
            print(f"[异常] {sym}: {repr(e)[:160]}", flush=True); continue
        finally:
            signal.alarm(0)
        if rec.get("skip"):
            print(f"{sym:5} 跳过: {rec['reason']}", flush=True); continue
        records.append(rec)
        ie = rec["is_edge"]
        so = rec["ss_oos"]["expR"]; bo = rec["base_oos"]["expR"]; oo = rec["ot_oos"]["expR"]
        sev = f"{so:+.3f}" if so is not None else "  -  "
        bev = f"{bo:+.3f}" if bo is not None else "  -  "
        oev = f"{oo:+.3f}" if oo is not None else "  -  "
        dv = rec["delta_ss_vs_base"]; dv2 = rec["delta_ot_vs_base"]
        dsv = f"{dv:+.3f}" if dv is not None else "  -  "
        d2v = f"{dv2:+.3f}" if dv2 is not None else "  -  "
        print(f"{sym:5}{rec['group']:5} {ie:>+8.3f} {rec['decision']:>10} "
              f"{sev:>8}{bev:>9}{dsv:>8}{oev:>8}{d2v:>8}  ({time.time()-t0:.1f}s)", flush=True)

    valid = [r for r in records if r["base_oos"]["n"] > 0 and r["ss_oos"]["n"] > 0]
    n_total = len(records)
    n_enabled = sum(1 for r in valid if r["enable"])

    # kline C 可用性（决定门是否有意义：无 kline C 的品种门退化为惰性 = 基线，应从门统计中剔除）
    _kc = _load_kline_cache()
    for r in records:
        r["kline_available"] = bool(_kc.get(r["symbol"].upper(), {}).get("history"))

    # 门统计总体 = 有 kline C 且回测有效（门在这些品种上才真有作用空间）
    gate_pop = [r for r in valid if r["kline_available"]]
    # 基线总体（所有有效，含无 kline C 的，作参照）
    base_pop = valid

    def mean_oos(pop, key):
        xs = [r[key]["expR"] for r in pop if r[key]["expR"] is not None]
        return round(float(np.mean(xs)), 4) if xs else None

    mean_base_all = mean_oos(base_pop, "base_oos")
    mean_base = mean_oos(gate_pop, "base_oos")
    mean_ss = mean_oos(gate_pop, "ss_oos")
    mean_ot = mean_oos(gate_pop, "ot_oos")

    # 相对基线（逐品种 OOS ΔexpR）
    def _split(pop, win_key, loss_key):
        return ([r for r in pop if r[win_key]],
                [r for r in pop if r[loss_key]],
                [r for r in pop if not r[win_key] and not r[loss_key]])
    ss_wins, ss_loss, ss_flat = _split(gate_pop, "oos_win_ss", "oos_loss_ss")
    ot_wins, ot_loss, ot_flat = _split(gate_pop, "oos_win_ot", "oos_loss_ot")

    def _significance(pop, dkey):
        ds = [r[dkey] for r in pop if r.get(dkey) is not None]
        if not ds:
            return {"n": 0}
        wins = sum(1 for x in ds if x > 0.01)
        losses = sum(1 for x in ds if x < -0.01)
        flats = len(ds) - wins - losses
        pval = _binom_two_sided(wins, wins + losses)
        return {
            "n": len(ds), "wins": wins, "losses": losses, "flats": flats,
            "median_delta": round(float(np.median(ds)), 4),
            "mean_delta": round(float(np.mean(ds)), 4),
            "win_rate_over_all": round(wins / len(ds), 3),
            "binom_p_two_sided": round(pval, 4) if pval is not None else None,
        }

    sig_ss = _significance(gate_pop, "delta_ss_vs_base")
    sig_ot = _significance(gate_pop, "delta_ot_vs_base")

    summary = {
        "n_total": n_total, "n_valid": len(valid), "n_gate_pop": len(gate_pop),
        "n_enabled": n_enabled,
        "mean_oos_expR_baseline_all": mean_base_all,
        "mean_oos_expR_baseline": mean_base,
        "mean_oos_expR_same_sign": mean_ss,
        "mean_oos_expR_oppose": mean_ot,
        "delta_mean_ss_vs_base": (round(mean_ss - mean_base, 4) if mean_ss is not None and mean_base is not None else None),
        "delta_mean_ot_vs_base": (round(mean_ot - mean_base, 4) if mean_ot is not None and mean_base is not None else None),
        "n_oos_win_ss": len(ss_wins), "n_oos_loss_ss": len(ss_loss), "n_oos_flat_ss": len(ss_flat),
        "n_oos_win_ot": len(ot_wins), "n_oos_loss_ot": len(ot_loss), "n_oos_flat_ot": len(ot_flat),
        "significance_same_sign": sig_ss,
        "significance_oppose": sig_ot,
        "config": {"SPLIT": SPLIT, "MIN_T": MIN_T, "MARGIN": MARGIN,
                   "gate_threshold": GATE_THRESHOLD, "c_source_for_gate": "kline"},
    }

    out = {"summary": summary, "records": records}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("=" * 92, flush=True)
    print(f"[汇总] 评估 {n_total} 个，回测有效 {len(valid)} 个，门可用总体 {len(gate_pop)} 个；IS 决定启用 C 门 {n_enabled}")
    print(f"  逐品种 OOS 期望R均值(门可用总体)：基线(threshold) {mean_base:+.4f} ｜ same_sign {mean_ss:+.4f} ｜ oppose {mean_ot:+.4f}")
    if summary["delta_mean_ss_vs_base"] is not None:
        print(f"  Δsame_sign−基线 = {summary['delta_mean_ss_vs_base']:+.4f} ｜ Δoppose−基线 = {summary['delta_mean_ot_vs_base']:+.4f}")
    print(f"  same_sign  OOS: 增益 {len(ss_wins)} / 有损 {len(ss_loss)} / 持平 {len(ss_flat)}")
    print(f"  oppose     OOS: 增益 {len(ot_wins)} / 有损 {len(ot_loss)} / 持平 {len(ot_flat)}")
    print(f"  显著性(二项双尾, 仅计增益/有损): same_sign p={sig_ss['binom_p_two_sided']} ｜ oppose p={sig_ot['binom_p_two_sided']}")
    print(f"  中位数 Δ: same_sign {sig_ss.get('median_delta')} ｜ oppose {sig_ot.get('median_delta')} ｜ 增益占比 oppose {sig_ot.get('win_rate_over_all')}", flush=True)
    print(f"[耗时] {time.time()-t_all:.1f}s ｜ 已写 {OUT_JSON}", flush=True)

    build_html(out, OUT_HTML)


def build_html(out, path):
    s = out["summary"]
    recs = out["records"]
    # 门可用总体（有 kline C 且回测有效）—— 门在这些品种上才有作用空间
    valid = [r for r in recs if r.get("kline_available")
             and r.get("base_oos", {}).get("n", 0) > 0 and r.get("ss_oos", {}).get("n", 0) > 0]

    def fmt(x):
        return f"{x:+.3f}" if isinstance(x, (int, float)) else "  -  "
    rows = []
    for r in sorted(valid, key=lambda x: (x["delta_ss_vs_base"] or -9), reverse=True):
        dv = r["delta_ss_vs_base"]; dv2 = r["delta_ot_vs_base"]
        cls = "gain" if (dv is not None and dv > 0.01) else ("loss" if (dv is not None and dv < -0.01) else "flat")
        cls2 = "gain" if (dv2 is not None and dv2 > 0.01) else ("loss" if (dv2 is not None and dv2 < -0.01) else "flat")
        dec_cls = "on" if r["enable"] else "off"
        rows.append(f"""<tr>
<td class="sym">{r['symbol']}</td><td>{r['name']}</td><td>{r['group']}</td>
<td>{fmt(r['is_edge'])}</td>
<td class="dec {dec_cls}">{r['decision']}</td>
<td>{r['ss_oos']['expR']:+.3f}<br><span class="sub">{r['ss_oos']['n']}笔</span></td>
<td>{r['base_oos']['expR']:+.3f}<br><span class="sub">{r['base_oos']['n']}笔</span></td>
<td class="{cls}">{fmt(dv)}</td>
<td>{r['ot_oos']['expR']:+.3f}<br><span class="sub">{r['ot_oos']['n']}笔</span></td>
<td class="{cls2}">{fmt(dv2)}</td>
<td class="sub">ss拦{r['ss_gate_skipped']}/ot拦{r['ot_gate_skipped']}</td>
</tr>""")

    dm = s["delta_mean_ss_vs_base"]; dn = s["delta_mean_ot_vs_base"]

    def _sig_html(sig, label):
        p = sig.get("binom_p_two_sided")
        sig_cls = "gain" if (p is not None and p < 0.05) else "flat"
        sig_txt = "显著✓" if (p is not None and p < 0.05) else "不显著"
        return (f"<tr><td>{label}</td><td>{sig.get('n')}</td><td class='gain'>{sig.get('wins')}</td>"
                f"<td class='loss'>{sig.get('losses')}</td><td>{sig.get('flats')}</td>"
                f"<td>{fmt(sig.get('median_delta'))}</td><td>{fmt(sig.get('mean_delta'))}</td>"
                f"<td>{sig.get('win_rate_over_all')}</td><td class='{sig_cls}'>{p}</td>"
                f"<td class='{sig_cls}'>{sig_txt}</td></tr>")
    sig_ss_html = _sig_html(s.get("significance_same_sign", {}), "same_sign")
    sig_ot_html = _sig_html(s.get("significance_oppose", {}), f"oppose({s['config']['gate_threshold']})")

    headline = f"""<div class="kpis">
<div class="kpi"><div class="v">{s['n_valid']}</div><div class="l">回测有效品种</div></div>
<div class="kpi"><div class="v">{s['n_gate_pop']}</div><div class="l">门可用总体(有kline C)</div></div>
<div class="kpi"><div class="v">{s['n_enabled']}</div><div class="l">IS 决定启用 C 门</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_baseline'])}</div><div class="l">基线 OOS 期望R(均值)</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_same_sign'])}</div><div class="l">same_sign OOS</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_oppose'])}</div><div class="l">oppose OOS</div></div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C 感知方向门 IS/OOS 验证</title>
<style>
:root{{--bg:#0f1419;--card:#1a2129;--ink:#e6edf3;--mut:#8b98a5;--line:#2a323c;
--red:#ff5c5c;--grn:#3fb950;--amber:#d29922;--blue:#58a6ff;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px;color:var(--blue);border-left:3px solid var(--blue);padding-left:8px}}
.sub{{color:var(--mut);font-size:11px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:150px}}
.kpi .v{{font-size:24px;font-weight:700}} .kpi .l{{color:var(--mut);font-size:12px;margin-top:4px}}
.pos{{color:var(--red)}} .neg{{color:var(--grn)}}
table{{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}}
th,td{{padding:7px 8px;text-align:center;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--bg)}}
td.sym{{font-weight:700;text-align:left}}
.gain{{color:var(--red);font-weight:700}} .loss{{color:var(--grn);font-weight:700}} .flat{{color:var(--mut)}}
td.dec.on{{color:var(--red);font-weight:700}} td.dec.off{{color:var(--mut)}}
.note{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;color:var(--mut);margin:10px 0}}
.note b{{color:var(--ink)}} .wrap{{max-width:1180px;margin:0 auto}}
.tag{{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;background:#223;color:var(--blue);margin-right:6px}}
</style></head><body><div class="wrap">
<h1>C 感知方向门（threshold 增强）IS/OOS 验证</h1>
<div class="sub">三感参谋 · 四维信号系统 · 让资金面 C 真正参与决策（不部署 combined）｜ 生成于 {time.strftime('%Y-%m-%d %H:%M')}</div>
{headline}
<div class="note">
<b>动机</b>：threshold 模式下 C 维度<b>结构性惰性</b>（kline C 与 dragon C 回测逐笔一致）→ 单纯喂 C 不改交易。
本验证给 threshold 加<b>C 感知方向门</b>（不让 C 定方向，只拦单）：门消费<b>丰富的 kline C</b>（17 年 1 分钟 K 线造）。
<span class="tag">same_sign</span>T 看多但 C 反向(且≠0)则不开多；<span class="tag">oppose</span>仅 |C|&gt;{s['config']['gate_threshold']} 反向才拦（更温和）。
<b>方法</b>：全部 {s['n_gate_pop']} 个有 kline C 的品种（17 年 1 分钟 K 线批量造）；按基线交易日切 <b>IS(前{int(s['config']['SPLIT']*100)}%)</b>/<b>OOS(后{100-int(s['config']['SPLIT']*100)}%)</b>，
决策仅看 IS（护栏 MIN_T={s['config']['MIN_T']}、IS增益≥{s['config']['MARGIN']}、IS_ss_expR&gt;0），OOS 仅验证；
<b>二项双尾检验</b>判断「增益品种占比」是否显著&gt;50%（H0: 门无方向性增益）。
</div>

<h2>一、组合层面结论</h2>
<table><tr><th>指标</th><th>基线(threshold)</th><th>same_sign</th><th>oppose({s['config']['gate_threshold']})</th><th>Δss−基</th><th>Δot−基</th></tr>
<tr><td>OOS 期望R 均值(逐品种)</td><td>{fmt(s['mean_oos_expR_baseline'])}</td><td>{fmt(s['mean_oos_expR_same_sign'])}</td><td>{fmt(s['mean_oos_expR_oppose'])}</td><td class="{('pos' if (dm or 0)>0 else 'neg')}">{fmt(dm)}</td><td class="{('pos' if (dn or 0)>0 else 'neg')}">{fmt(dn)}</td></tr>
</table>
<div class="note">
<b>读图</b>：same_sign / oppose 相对基线的 Δ（红=增益/绿=有损，A股惯例）。
same_sign OOS：<b class="pos">增益 {s['n_oos_win_ss']}</b> / <b class="neg">有损 {s['n_oos_loss_ss']}</b> / 持平 {s['n_oos_flat_ss']}；
oppose OOS：<b class="pos">增益 {s['n_oos_win_ot']}</b> / <b class="neg">有损 {s['n_oos_loss_ot']}</b> / 持平 {s['n_oos_flat_ot']}。
</div>

<h2>一·二、统计显著性（门可用总体，二项双尾）</h2>
<table><tr><th>门模式</th><th>样本n</th><th>增益</th><th>有损</th><th>持平</th><th>中位数Δ</th><th>均值Δ</th><th>增益占比</th><th>二项p(双尾)</th><th>显著?</th></tr>
{sig_ss_html}
{sig_ot_html}
</table>
<div class="sub">「二项p」= 对（增益 vs 有损）做精确二项双尾检验（H0: 增益占比=50%）。p&lt;0.05 视为门在该模式上显著稳定增益；p 越大越像随机抛硬币，门不值得全市场部署。</div>

<h2>二、逐品种明细（按 same_sign OOS Δ 排序）</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>板块</th><th>IS edge</th><th>IS决策</th>
<th>OOS_ss</th><th>OOS_base</th><th>Δss</th><th>OOS_ot</th><th>Δot</th><th>拦单数</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="sub">红=增益 / 绿=有损。OOS_ss=启用 same_sign 门；OOS_base=永远 threshold；OOS_ot=oppose 门。拦单数=该模式在 IS+OOS 全样本拦下的信号数。</div>

<h2>三、结论</h2>
<div class="note">
1. <b>门能否让 C 真正起作用</b>：取决于 OOS 相对基线是否稳定增益。见上表 Δss/Δot 与各品种增益/有损计数。<br>
2. <b>护栏防过拟合</b>：IS 决定、OOS 仅验证；仅 {s['n_enabled']} 个品种被放行启用 same_sign 门。<br>
3. <b>生产建议</b>：若 same_sign / oppose 的 OOS 均值 Δ 为负或增益≪有损（二项p不显著），则门不值得全市场部署，维持 threshold（现状）；若某品种 IS+OOS 双优可单独启用。<br>
4. <b>结论判据</b>：门是否有意义 = 门可用总体(有 kline C)上 OOS 均值 Δ 为正 <b>且</b> 二项双尾 p&lt;0.05（增益品种占比显著&gt;50%）。仅 6 样本时易过拟合，现已扩到 {s['n_gate_pop']} 个品种大样本复核。
</div>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[报告] 已写 {path}", flush=True)


if __name__ == "__main__":
    main()
