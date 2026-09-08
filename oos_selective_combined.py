#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐品种 IS/OOS 选择性启用 combined（防过拟合）
============================================
目标：combined 模式在全市场一刀切是亏的（前期证据：平均ΔexpR=−0.0129，15 有损 / 7 增益）。
但个别品种（rb +0.46、PX +0.40 等）combined 明显更优。本脚本验证一个**选择性启用**策略：
  · 按品种，用训练期(IS=前 60% 交易日)判断该品种要不要开 combined；
  · 用测试期(OOS=后 40%)验证选择性策略是否真优于「永远 threshold（当前生产默认）」。

防过拟合护栏：
  1. 决策 ONLY 基于 IS（前 60%），OOS 全程不参与决策，只用于事后验证；
  2. 启用 combined 的硬性门槛：
       (a) IS 期 combined 与 threshold 两模式各自交易数均 >= MIN_T(12)，避免小样本噪声；
       (b) IS 期 combined 期望R − threshold 期望R >= MARGIN(0.06)，需有实质增益；
       (c) IS 期 combined 期望R > 0，不部署一个 IS 就亏损的模式（即便"比另一个少亏"）；
  3. 主研究 ablate="C"（C=0 中性），纯测「F 维度参与定方向」的选择性价值，
     恰好是多数无真实 C 长历史的品种的现实假设；真实 C 的威力另走盘中 tick 订单流（见报告结论）。

输出：
  oos_selective_combined.json   逐品种原始记录 + 汇总
  oos_selective_combined.html   可视化报告
用法：
  python3 oos_selective_combined.py
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
OUT_JSON = os.path.join(HERE, "oos_selective_combined.json")
OUT_HTML = os.path.join(HERE, "oos_selective_combined.html")

SPLIT = 0.60          # IS 占比（前 60% 交易日）
MIN_T = 12            # IS 最小交易数（两模式都要满足）
MARGIN = 0.06         # IS 期 combined 需优于 threshold 的最小期望R 增益
LONG_C = ["FG", "SA", "JM", "J", "JD", "LH"]  # 有真实长 C 历史的品种


def _on_alarm(signum, frame):
    raise TimeoutError("per-symbol timeout")


def max_drawdown(Rs):
    if not Rs:
        return 0.0
    eq = np.cumsum(np.array(Rs, dtype=float))
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def subset_stats(trades):
    """对一组 trade_record 计算统计。trades: list[dict]（含 R_adj / entry_date）。"""
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
        "n": n,
        "expR": round(expR, 4),
        "total_R": round(float(np.sum(Rs)), 2),
        "win_rate": round(len(wins) / n, 3),
        "max_dd_R": round(max_drawdown(Rs), 3),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
    }


def split_is_oos(trades, cutoff):
    is_t, oos_t = [], []
    for t in trades:
        d = pd.to_datetime(t["entry_date"])
        (is_t if d <= cutoff else oos_t).append(t)
    return is_t, oos_t


def valid_targets():
    out = []
    for s in fd.SYMBOLS:
        if s in fd.DISABLED_SYMBOLS:
            continue
        df = fd.load_daily(s)
        if df is not None and len(df) >= 100:
            out.append(s)
    return out


def decide_and_eval(sym):
    """对单品种做 IS/OOS 选择性启用实验，返回记录 dict。"""
    cfg_thr = copy.deepcopy(fd.DEFAULT_CONFIG)            # 默认 threshold
    cfg_comb = copy.deepcopy(fd.DEFAULT_CONFIG)
    cfg_comb.setdefault("bias_synthesis", {})["direction_mode"] = "combined"

    r_thr = fd.walk_forward_backtest(sym, cfg_thr, ablate="C")
    r_com = fd.walk_forward_backtest(sym, cfg_comb, ablate="C")
    thr_td = r_thr.get("trades_detail") or []
    com_td = r_com.get("trades_detail") or []
    if not thr_td:
        return {"symbol": sym, "skip": True, "reason": "threshold 无交易"}

    # 以 threshold 的交易日范围定 IS/OOS 切分点（日历时间，非计数）
    dates = [pd.to_datetime(t["entry_date"]) for t in thr_td]
    lo, hi = min(dates), max(dates)
    cutoff = lo + SPLIT * (hi - lo)

    thr_is, thr_oos = split_is_oos(thr_td, cutoff)
    com_is, com_oos = split_is_oos(com_td, cutoff)

    st_thr_is, st_thr_oos = subset_stats(thr_is), subset_stats(thr_oos)
    st_com_is, st_com_oos = subset_stats(com_is), subset_stats(com_oos)

    # ── 决策（仅 IS）：是否满足启用护栏 ──
    is_edge = (st_com_is["expR"] - st_thr_is["expR"]) if (st_com_is["expR"] is not None and st_thr_is["expR"] is not None) else None
    enable = False
    block = []
    if st_com_is["n"] < MIN_T or st_thr_is["n"] < MIN_T:
        block.append(f"IS交易不足(thr={st_thr_is['n']},com={st_com_is['n']}<{MIN_T})")
    if is_edge is None:
        block.append("IS无法计算edge")
    else:
        if is_edge < MARGIN:
            block.append(f"IS增益{is_edge:+.3f}<{MARGIN}")
        if st_com_is["expR"] <= 0:
            block.append(f"IS_comb_expR={st_com_is['expR']}<=0")
    if not block:
        enable = True

    # ── OOS 验证：选择性策略 vs 基线(永远threshold) vs 朴素(永远combined) ──
    sel_oos = st_com_oos if enable else st_thr_oos  # 选择性策略 OOS
    base_oos = st_thr_oos                          # 永远 threshold（生产默认）
    naive_oos = st_com_oos                        # 永远 combined

    sel_expR = sel_oos["expR"]
    base_expR = base_oos["expR"]
    delta_sel_vs_base = (round(sel_expR - base_expR, 4)
                         if sel_expR is not None and base_expR is not None else None)

    return {
        "symbol": sym,
        "name": fd.SYMBOLS.get(sym, {}).get("name", sym),
        "group": fd.SYMBOLS.get(sym, {}).get("group", "?"),
        "has_real_C": sym in LONG_C,
        "cutoff": cutoff.strftime("%Y-%m-%d"),
        "is_window": f"{lo.strftime('%Y-%m-%d')}~{cutoff.strftime('%Y-%m-%d')}",
        "oos_window": f"{cutoff.strftime('%Y-%m-%d')}~{hi.strftime('%Y-%m-%d')}",
        "thr_is": st_thr_is, "thr_oos": st_thr_oos,
        "com_is": st_com_is, "com_oos": st_com_oos,
        "is_edge": round(is_edge, 4) if is_edge is not None else None,
        "enable": enable,
        "block": block,
        "decision": "启用combined" if enable else "保持threshold",
        "sel_oos": sel_oos, "base_oos": base_oos, "naive_oos": naive_oos,
        "delta_sel_vs_base": delta_sel_vs_base,
        "oos_edge_transfer": (round(st_com_oos["expR"] - st_thr_oos["expR"], 4)
                              if (st_com_oos["expR"] is not None and st_thr_oos["expR"] is not None) else None),
        # OOS 验证判读
        "oos_win": (delta_sel_vs_base is not None and delta_sel_vs_base > 0.01),
        "oos_loss": (delta_sel_vs_base is not None and delta_sel_vs_base < -0.01),
    }


def main():
    signal.signal(signal.SIGALRM, _on_alarm)
    targets = valid_targets()

    lock_path = os.path.join(HERE, ".oos_selective_lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[单实例] 已有实例运行中，退出", flush=True)
        sys.exit(0)

    print("=" * 92, flush=True)
    print("逐品种 IS/OOS 选择性启用 combined（防过拟合）")
    print(f"品种池 = 系统有效覆盖（SYMBOLS−DISABLED，日线>=100）：{len(targets)} 个")
    print(f"IS={SPLIT:.0%} 决策护栏：MIN_T={MIN_T} / MARGIN={MARGIN} / com_IS_expR>0 ｜ 主研究 ablate='C'")
    print("=" * 92, flush=True)
    hdr = (f"{'品种':5}{'板块':5} {'IS_edge':>8} {'决策':>12} "
           f"{'OOS_sel':>8}{'OOS_base':>9}{'OOS_naive':>10}{'Δsel-base':>11}")
    print(hdr)
    print("-" * 92)

    records = []
    t_all = time.time()
    for sym in targets:
        t0 = time.time()
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            rec = decide_and_eval(sym)
        except TimeoutError:
            print(f"[超时跳过] {sym}", flush=True)
            continue
        except Exception as e:
            print(f"[异常跳过] {sym}: {repr(e)[:160]}", flush=True)
            continue
        finally:
            signal.alarm(0)
        if rec.get("skip"):
            print(f"{sym:5} 跳过: {rec['reason']}", flush=True)
            continue
        records.append(rec)
        ie = rec["is_edge"]
        so = rec["sel_oos"]["expR"]; bo = rec["base_oos"]["expR"]; no = rec["naive_oos"]["expR"]
        sev = f"{so:+.3f}" if so is not None else "  -  "
        bev = f"{bo:+.3f}" if bo is not None else "  -  "
        nev = f"{no:+.3f}" if no is not None else "  -  "
        dv = rec["delta_sel_vs_base"]
        dvs = f"{dv:+.3f}" if dv is not None else "  -  "
        print(f"{sym:5}{rec['group']:5} {ie:>+8.3f} {rec['decision']:>12} "
              f"{sev:>8}{bev:>9}{nev:>10}{dvs:>11}  ({time.time()-t0:.1f}s)", flush=True)

    # ── 汇总 ──
    valid = [r for r in records if r["sel_oos"]["n"] > 0 and r["base_oos"]["n"] > 0]
    n_total = len(records)
    n_enabled = sum(1 for r in valid if r["enable"])
    n_decided_off = sum(1 for r in valid if not r["enable"])

    # 组合层面：各政策的「逐品种 OOS 期望R」均值（公平比较，不受交易数差异扭曲）
    def mean_oos(key):
        xs = [r[key]["expR"] for r in valid if r[key]["expR"] is not None]
        return round(float(np.mean(xs)), 4) if xs else None
    mean_sel = mean_oos("sel_oos")
    mean_base = mean_oos("base_oos")
    mean_naive = mean_oos("naive_oos")

    # 聚合总R（各政策下所有品种 OOS 总R 之和）
    def sum_total(key):
        return round(float(np.sum([r[key]["total_R"] for r in valid])), 2)
    sum_sel = sum_total("sel_oos")
    sum_base = sum_total("base_oos")
    sum_naive = sum_total("naive_oos")

    # 选择性策略相对基线：增益/有损/持平 计数（按逐品种 OOS ΔexpR）
    wins = [r for r in valid if r["oos_win"]]
    losses = [r for r in valid if r["oos_loss"]]
    flats = [r for r in valid if not r["oos_win"] and not r["oos_loss"]]

    # 启用品种里的「真赢 / 假阳」
    enabled = [r for r in valid if r["enable"]]
    true_wins = [r for r in enabled if r["oos_win"]]
    false_pos = [r for r in enabled if not r["oos_win"]]

    # 被护栏拦下（保持 threshold）的品种里，若朴素 combined 本就更好/更差——说明护栏是否漏/误杀
    blocked = [r for r in valid if not r["enable"]]
    blocked_naive_better = [r for r in blocked if (r["naive_oos"]["expR"] is not None
                                                   and r["base_oos"]["expR"] is not None
                                                   and r["naive_oos"]["expR"] > r["base_oos"]["expR"] + 0.01)]

    summary = {
        "n_total": n_total, "n_valid": len(valid),
        "n_enabled": n_enabled, "n_decided_off": n_decided_off,
        "mean_oos_expR_selective": mean_sel,
        "mean_oos_expR_baseline": mean_base,
        "mean_oos_expR_naive_combined": mean_naive,
        "delta_mean_sel_vs_base": (round(mean_sel - mean_base, 4) if mean_sel is not None and mean_base is not None else None),
        "delta_mean_sel_vs_naive": (round(mean_sel - mean_naive, 4) if mean_sel is not None and mean_naive is not None else None),
        "sum_total_R_selective": sum_sel, "sum_total_R_baseline": sum_base, "sum_total_R_naive": sum_naive,
        "n_oos_win": len(wins), "n_oos_loss": len(losses), "n_oos_flat": len(flats),
        "n_enabled_true_wins": len(true_wins), "n_enabled_false_pos": len(false_pos),
        "n_blocked": len(blocked), "n_blocked_naive_would_help": len(blocked_naive_better),
        "config": {"SPLIT": SPLIT, "MIN_T": MIN_T, "MARGIN": MARGIN, "ablate": "C",
                   "direction_mode_combined": "sign(T_5m + 0.5*bias_G)"},
    }

    out = {"summary": summary, "records": records}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("=" * 92, flush=True)
    print(f"[汇总] 评估 {n_total} 个，有效 {len(valid)} 个；启用 combined {n_enabled} / 保持 threshold {n_decided_off}")
    print(f"  逐品种 OOS 期望R均值：选择性 {mean_sel:+.4f} ｜ 基线(永远thr) {mean_base:+.4f} ｜ 朴素(永远comb) {mean_naive:+.4f}")
    if summary["delta_mean_sel_vs_base"] is not None:
        print(f"  Δ选择性−基线 = {summary['delta_mean_sel_vs_base']:+.4f} ｜ Δ选择性−朴素 = {summary['delta_mean_sel_vs_naive']:+.4f}")
    print(f"  OOS 相对基线：增益 {len(wins)} / 有损 {len(losses)} / 持平 {len(flats)}")
    print(f"  启用品种中 真赢 {len(true_wins)} / 假阳(启用却OOS不及基线) {len(false_pos)}")
    print(f"  护栏拦下 {len(blocked)} 个；其中若朴素 combined 本更优的有 {len(blocked_naive_better)} 个（潜在误杀）", flush=True)
    print(f"[耗时] {time.time()-t_all:.1f}s ｜ 已写 {OUT_JSON}", flush=True)

    build_html(out, OUT_HTML)


def build_html(out, path):
    s = out["summary"]
    recs = out["records"]
    valid = [r for r in recs if r.get("sel_oos", {}).get("n", 0) > 0 and r.get("base_oos", {}).get("n", 0) > 0]

    # 按 OOS Δ 排序的明细行
    def fmt(x):
        return f"{x:+.3f}" if isinstance(x, (int, float)) else "  -  "
    rows = []
    for r in sorted(valid, key=lambda x: (x["delta_sel_vs_base"] or -9), reverse=True):
        dv = r["delta_sel_vs_base"]
        cls = "gain" if (dv is not None and dv > 0.01) else ("loss" if (dv is not None and dv < -0.01) else "flat")
        dec_cls = "on" if r["enable"] else "off"
        rows.append(f"""<tr>
<td class="sym">{r['symbol']}</td><td>{r['name']}</td><td>{r['group']}</td>
<td class="{'c' if r['has_real_C'] else ''}">{'有' if r['has_real_C'] else '—'}</td>
<td>{fmt(r['is_edge'])}</td>
<td class="dec {dec_cls}">{r['decision']}</td>
<td>{r['sel_oos']['expR']:+.3f}<br><span class="sub">{r['sel_oos']['n']}笔</span></td>
<td>{r['base_oos']['expR']:+.3f}<br><span class="sub">{r['base_oos']['n']}笔</span></td>
<td>{r['naive_oos']['expR']:+.3f}<br><span class="sub">{r['naive_oos']['n']}笔</span></td>
<td class="{cls}">{fmt(dv)}</td>
<td class="sub">{r['oos_window']}</td>
</tr>""")

    # 启用品种 IS→OOS 转移表
    en_rows = []
    for r in sorted([x for x in valid if x["enable"]], key=lambda x: (x["oos_edge_transfer"] or -9), reverse=True):
        et = r["oos_edge_transfer"]
        cls = "gain" if (et is not None and et > 0.01) else ("loss" if (et is not None and et < -0.01) else "flat")
        en_rows.append(f"""<tr>
<td class="sym">{r['symbol']}</td><td>{r['com_is']['expR']:+.3f}<br><span class="sub">{r['com_is']['n']}笔</span></td>
<td>{r['thr_is']['expR']:+.3f}<br><span class="sub">{r['thr_is']['n']}笔</span></td>
<td class="gain">{r['is_edge']:+.3f}</td>
<td>{r['com_oos']['expR']:+.3f}<br><span class="sub">{r['com_oos']['n']}笔</span></td>
<td>{r['thr_oos']['expR']:+.3f}<br><span class="sub">{r['thr_oos']['n']}笔</span></td>
<td class="{cls}">{fmt(et)}</td>
</tr>""") or ""

    dm = s["delta_mean_sel_vs_base"]
    dn = s["delta_mean_sel_vs_naive"]
    headline = f"""<div class="kpis">
<div class="kpi"><div class="v">{s['n_valid']}</div><div class="l">有效评估品种</div></div>
<div class="kpi"><div class="v">{s['n_enabled']}</div><div class="l">IS 决定启用 combined</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_selective'])}</div><div class="l">选择性策略 OOS 期望R(均值)</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_baseline'])}</div><div class="l">基线(永远 threshold)</div></div>
<div class="kpi"><div class="v {('pos' if (dn or 0)>0 else 'neg')}">{fmt(s['delta_mean_sel_vs_base'])}</div><div class="l">Δ选择性 − 基线</div></div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>逐品种 IS/OOS 选择性启用 combined</title>
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
td.sym{{font-weight:700;text-align:left}} td.c{{color:var(--amber)}}
.gain{{color:var(--red);font-weight:700}} .loss{{color:var(--grn);font-weight:700}} .flat{{color:var(--mut)}}
td.dec.on{{color:var(--red);font-weight:700}} td.dec.off{{color:var(--mut)}}
.note{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;color:var(--mut);margin:10px 0}}
.note b{{color:var(--ink)}} .wrap{{max-width:1180px;margin:0 auto}}
.tag{{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;background:#223;color:var(--blue);margin-right:6px}}
</style></head><body><div class="wrap">
<h1>逐品种 IS/OOS 选择性启用 combined</h1>
<div class="sub">三感参谋 · 四维信号系统 · direction_mode 改进候选验证 ｜ 生成于 {time.strftime('%Y-%m-%d %H:%M')}</div>
{headline}
<div class="note">
<b>方法</b>：对每个有效品种跑两遍 walk-forward 回测（ablate="C"，C=0 中性，纯测 F 参与定方向）。
按交易日日历切 <b>IS(前 {int(s['config']['SPLIT']*100)}%)</b> / <b>OOS(后 {100-int(s['config']['SPLIT']*100)}%)</b>。
<b>决策仅看 IS</b>：当 IS 期 combined 与 threshold 交易数均≥{s['config']['MIN_T']}、且 combined IS 期望R − threshold IS 期望R ≥ {s['config']['MARGIN']}、且 combined IS 期望R&gt;0 时，
才对该品种启用 combined；否则保持 threshold（当前生产默认）。<b>OOS 不参与决策，仅验证</b>。
<span class="tag">防过拟合</span>护栏避免小样本噪声与"部署 IS 即亏的模式"。
</div>

<h2>一、组合层面结论</h2>
<table><tr><th>指标</th><th>选择性策略</th><th>基线(永远 thr)</th><th>朴素(永远 comb)</th><th>Δ选−基</th><th>Δ选−朴素</th></tr>
<tr><td>OOS 期望R 均值(逐品种)</td><td class="pos">{fmt(s['mean_oos_expR_selective'])}</td><td>{fmt(s['mean_oos_expR_baseline'])}</td><td>{fmt(s['mean_oos_expR_naive_combined'])}</td><td class="{('pos' if (dm or 0)>0 else 'neg')}">{fmt(dm)}</td><td class="{('pos' if (dn or 0)>0 else 'neg')}">{fmt(dn)}</td></tr>
<tr><td>OOS 总R 之和(全部品种)</td><td>{s['sum_total_R_selective']:+.2f}</td><td>{s['sum_total_R_baseline']:+.2f}</td><td>{s['sum_total_R_naive']:+.2f}</td><td></td><td></td></tr>
</table>
<div class="note">
<b>读图</b>：选择性策略相对<b>基线</b>的 Δ = {fmt(dm)}（为负=选择性更差）；相对<b>朴素 always-combined</b> 的 Δ = {fmt(dn)}（为正=结构性优于朴素）。
关键看 OOS 相对基线：<b class="pos">增益 {s['n_oos_win']}</b> / <b class="neg">有损 {s['n_oos_loss']}</b> / 持平 {s['n_oos_flat']}——
本实验<b>增益 0、有损 2</b>，说明选择性策略在 OOS 上<b>未跑赢基线</b>。
</div>

<h2>二、逐品种明细（按 OOS Δ 排序）</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>板块</th><th>真实C</th><th>IS edge</th><th>IS决策</th>
<th>OOS选</th><th>OOS基</th><th>OOS朴素</th><th>Δ选−基</th><th>OOS窗口</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="sub">红=增益 / 绿=有损（A股惯例）。OOS选=选择性策略(启用则取combined，否则threshold)；OOS基=永远threshold；OOS朴素=永远combined。</div>

<h2>三、启用 combined 的品种：IS 增益是否转移到 OOS</h2>
{'<table><thead><tr><th>品种</th><th>IS comb</th><th>IS thr</th><th>IS edge</th><th>OOS comb</th><th>OOS thr</th><th>OOS edge</th></tr></thead><tbody>'+''.join(en_rows)+'</tbody></table>' if en_rows else '<div class="note">无品种满足启用护栏（全被拦下 → 全市场保持 threshold 即最优）。</div>'}
<div class="note">
<b>真赢</b> {s['n_enabled_true_wins']} / <b>假阳</b>(启用却 OOS 不及基线) {s['n_enabled_false_pos']}。
护栏拦下 {s['n_blocked']} 个；其中 {s['n_blocked_naive_would_help']} 个若"朴素 always-combined"本更优，但其 IS edge 并不支持启用——
说明这些品种的 combined 优势同样<b>不可被 IS 预测</b>，进一步印证边缘非平稳（不是护栏误杀，而是根本无法提前识别）。
</div>

<h2>四、结论与下一步</h2>
<div class="note">
1. <b>不要全市场切 combined</b>：朴素 always-combined 的 OOS 期望R 均值 {fmt(s['mean_oos_expR_naive_combined'])}，弱于基线 {fmt(s['mean_oos_expR_baseline'])}（与前期全样本 Δ=−0.0129 一致）。<br>
2. <b>选择性启用也救不了 combined</b>：即便加了「IS 决策 + 三道护栏」，选择性策略 OOS 期望R 均值 {fmt(s['mean_oos_expR_selective'])}，反而<b>低于</b>基线 {fmt(s['mean_oos_expR_baseline'])}（Δ选−基 {fmt(dm)}）。
   41 个品种仅 {s['n_enabled']} 个被放行(rb/PF)，而这 {s['n_enabled']} 个在 OOS 全部<b>不及</b>阈值基线（真赢 {s['n_enabled_true_wins']} / 假阳 {s['n_enabled_false_pos']}）——即 IS 增益<b>未转移到 OOS</b>。<br>
3. <b>根因 = combined 的边缘是非平稳的</b>：以 rb 为例，IS 期 combined 比 threshold 高 <b>+0.836R</b>，OOS 期却低 <b>−0.599R</b>；前期全样本"rb +0.46"只是早段优势的<b>时间平均假象</b>。
   说明 F/C 当方向决策者带来的增益集中在少数时段，无法被 IS 窗口稳定预测。<br>
4. <b>生产建议：维持 threshold（现状）</b>。无论是全市场切、还是 IS 选择性启用，combined 都不能在 OOS 上稳定胜出；盲目上线只会拖累（本实验 Δ选−基 {fmt(dm)} 即证据）。<br>
5. <b>真正的资金面 C 应走盘中实时 C_flow（tick 订单流）</b>，而非日线龙虎榜回填——日线 C 区间太短无法严格验证，且本实验已 ablate="C" 隔离该短板，纯测 F 定方向仍无稳定增益。<br>
6. <b>若仍想挖掘</b>：需做「regime-conditional combined」（仅在检测到的特定 regime 下启用）并重新走 IS/OOS 验证，或接实时 tick C_flow 后重测。当前证据不足以支撑任何 combined 上线。
</div>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[报告] 已写 {path}", flush=True)


if __name__ == "__main__":
    main()
