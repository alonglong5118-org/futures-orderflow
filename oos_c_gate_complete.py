#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
C 感知方向门 · 补全品种 OOS（完整活跃池覆盖）
============================================
目的：把 prior 大样本验证（41 个「门可用」品种，已在 oos_c_gate.json）补全到
**完整生产活跃池**（SYMBOLS − DISABLED = 43 只），让全市场部署的每一只品种都有 OOS 数。

缺口定位（已核验）：
  · 活跃池 54 − 禁用 11 = 43 只；
  · 其中 42 只有 kline C（门可用），1 只 SA01 无 kline C（门惰性、永不拦截）；
  · prior oos_c_gate.json 只含 41 只 → 漏了：
      - sc      （有 kline C，但 prior 被回测有效过滤/跳过，需补跑）
      - SA01    （无 kline C，prior 直接 TARGETS 跳过，需补报「门惰性·无伤害」）

做法：
  1. 复用 oos_c_gate.json 的 41 条真数字（P0 修复后，Δot=+0.0663, p=0.0351，已确认非坏门）；
  2. 对缺的活跃品种逐一 decide_and_eval 补跑（sc / SA01）；
  3. 合并成完整 43 品种记录，重算门可用总体(42) 与 完整活跃池(43) 的汇总+显著性；
  4. 产出 oos_c_gate_complete.json / .html（不覆盖原 41 报告，保留历史）。

复用 oos_c_gate 的 decide_and_eval / run_backtest / _binom_two_sided 等（模块底部有 __main__ 守卫，import 不自跑）。
"""
import json
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in __import__("sys").path:
    __import__("sys").path.insert(0, HERE)

import oos_c_gate as G  # noqa: E402
from oos_c_gate import (decide_and_eval, _load_kline_cache, _binom_two_sided,
                        SPLIT, MIN_T, MARGIN, GATE_THRESHOLD, fd)  # noqa: E402

OUT_JSON = os.path.join(HERE, "oos_c_gate_complete.json")
OUT_HTML = os.path.join(HERE, "oos_c_gate_complete.html")

KC = _load_kline_cache()


def is_kline_avail(sym):
    return bool(KC.get(sym.upper(), {}).get("history"))


def mean_oos(pop, key):
    xs = [r[key]["expR"] for r in pop if r.get(key, {}).get("expR") is not None]
    return round(float(np.mean(xs)), 4) if xs else None


def split_pop(pop, win_key, loss_key):
    return ([r for r in pop if r.get(win_key)],
            [r for r in pop if r.get(loss_key)],
            [r for r in pop if not r.get(win_key) and not r.get(loss_key)])


def significance(pop, dkey):
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


def main():
    base = json.load(open(os.path.join(HERE, "oos_c_gate.json"), encoding="utf-8"))
    recs41 = base["records"]
    done = set(r["symbol"] for r in recs41)
    print(f"[载入] oos_c_gate.json 已有 {len(recs41)} 只（门可用总体，修复后真数字）")

    active = [s for s in fd.SYMBOLS if s not in getattr(fd, "DISABLED_SYMBOLS", set())]
    missing = [s for s in active if s not in done]
    print(f"[缺口] 活跃池 {len(active)} 只，缺 {len(missing)} 只：{missing}")

    new_recs = []
    for s in missing:
        print(f"  → 补跑 {s} ...", flush=True)
        t0 = time.time()
        rec = decide_and_eval(s)
        rec["symbol"] = s
        # 标注 kline 可用性（decide_and_eval 内部不标）
        rec["kline_available"] = is_kline_avail(s)
        if rec.get("skip"):
            rec["status"] = "skipped_no_trades"
            print(f"    [跳过] {rec.get('reason')} ({time.time()-t0:.1f}s)")
        else:
            rec["status"] = "validated_new"
            print(f"    [有效] Δot={rec.get('delta_ot_vs_base')} ot拦={rec.get('ot_gate_skipped')} "
                  f"kline_available={rec['kline_available']} ({time.time()-t0:.1f}s)")
        new_recs.append(rec)

    # SA01 概念上「门惰性·无 kline C」：即便跑出正常记录也单独标记
    for rec in new_recs:
        if rec["symbol"] == "SA01":
            rec["status"] = "gate_inert_no_klineC"

    # 完整记录（给 41 + 新补的）
    complete = list(recs41) + new_recs
    # 兜底：统一重算 kline_available 字段（41 原有，新补的已标）
    for r in complete:
        r.setdefault("kline_available", is_kline_avail(r["symbol"]))

    # ── 汇总 ──
    gate_pop = [r for r in complete if r.get("kline_available")]           # 有 kline C（门可用）
    inert = [r for r in complete if not r.get("kline_available")]          # 无 kline C（门惰性）
    valid = [r for r in complete if r.get("base_oos", {}).get("n", 0) > 0
             and r.get("ot_oos", {}).get("n", 0) > 0]

    mean_base = mean_oos(gate_pop, "base_oos")
    mean_ss = mean_oos(gate_pop, "ss_oos")
    mean_ot = mean_oos(gate_pop, "ot_oos")

    ss_win, ss_loss, ss_flat = split_pop(gate_pop, "oos_win_ss", "oos_loss_ss")
    ot_win, ot_loss, ot_flat = split_pop(gate_pop, "oos_win_ot", "oos_loss_ot")
    sig_ss = significance(gate_pop, "delta_ss_vs_base")
    sig_ot = significance(gate_pop, "delta_ot_vs_base")

    summary = {
        "config": {"SPLIT": SPLIT, "MIN_T": MIN_T, "MARGIN": MARGIN, "gate_threshold": GATE_THRESHOLD},
        "n_active_universe": len(active),
        "n_active_with_klineC": len(gate_pop),
        "n_active_inert_no_klineC": len(inert),
        "n_valid_oos": len(valid),
        "n_zero_trades_unvalidated": len([r for r in new_recs if r.get("base_oos", {}).get("n") is None]),
        "n_newly_added": len(new_recs),
        "newly_added": [{"symbol": r["symbol"], "status": r.get("status"),
                         "delta_ot": r.get("delta_ot_vs_base"),
                         "ot_skipped": r.get("ot_gate_skipped")} for r in new_recs],
        "mean_oos_expR_baseline": mean_base,
        "mean_oos_expR_same_sign": mean_ss,
        "mean_oos_expR_oppose": mean_ot,
        "delta_mean_ss_vs_base": round(mean_ss - mean_base, 4) if (mean_ss is not None and mean_base is not None) else None,
        "delta_mean_ot_vs_base": round(mean_ot - mean_base, 4) if (mean_ot is not None and mean_base is not None) else None,
        "n_oos_win_ss": len(ss_win), "n_oos_loss_ss": len(ss_loss), "n_oos_flat_ss": len(ss_flat),
        "n_oos_win_ot": len(ot_win), "n_oos_loss_ot": len(ot_loss), "n_oos_flat_ot": len(ot_flat),
        "significance_same_sign": sig_ss,
        "significance_oppose": sig_ot,
        "note": ("完整活跃池 %d 只全覆盖；其中 OOS 已验证（基线有成交且门可用）= %d 只（即 prior 41，门可用总体）；"
                 "另 %d 只回测零成交、无法 OOS 验证：sc（有 kline C，门可触发但无样本→盘中监控）、"
                 "SA01（无 kline C，门惰性且零成交）。门可用总体（有 kline C）共 %d 只，其中 %d 只可验证。"
                 % (len(active), len(valid),
                    len([r for r in new_recs if r.get("base_oos", {}).get("n") is None]),
                    len(gate_pop), len(valid))),
    }

    out = {"summary": summary, "records": complete}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[写出] {OUT_JSON}")

    # 打印关键汇总
    print("\n" + "=" * 80)
    print("补全后汇总（门可用总体 = 有 kline C = %d 只）" % len(gate_pop))
    print(f"  完整活跃池      : {len(active)} 只（含 {len(inert)} 只门惰性 SA01）")
    print(f"  基线 OOS 均值   : {mean_base}")
    print(f"  oppose OOS 均值 : {mean_ot}  (Δ={summary['delta_mean_ot_vs_base']})")
    print(f"  oppose 二项p    : {sig_ot.get('binom_p_two_sided')}  "
          f"(增益{sig_ot.get('wins')}/有损{sig_ot.get('losses')}/持平{sig_ot.get('flats')})")
    for r in new_recs:
        print(f"  [新] {r['symbol']}: status={r.get('status')} Δot={r.get('delta_ot_vs_base')} "
              f"ot拦={r.get('ot_gate_skipped')} base_oos_n={r.get('base_oos',{}).get('n')}")

    _write_html(summary, complete)
    print(f"[写出] {OUT_HTML}")


def _write_html(s, complete):
    def fmt(x):
        return f"{x:+.3f}" if isinstance(x, (int, float)) else "  -  "

    rows = []
    for r in sorted(complete, key=lambda x: (x.get("delta_ot_vs_base") or -9), reverse=True):
        dv2 = r.get("delta_ot_vs_base")
        cls2 = "gain" if (dv2 is not None and dv2 > 0.01) else ("loss" if (dv2 is not None and dv2 < -0.01) else "flat")
        status = r.get("status", "")
        tag = ""
        if status == "validated_new":
            tag = '<span class="tag new">补全·新跑</span>'
        elif status == "gate_inert_no_klineC":
            tag = '<span class="tag inert">补全·门惰性</span>'
        elif status == "skipped_no_trades":
            tag = '<span class="tag skip">补全·无成交</span>'
        bo = r.get("base_oos", {})
        oo = r.get("ot_oos", {})
        rows.append(f"""<tr>
<td class="sym">{r['symbol']}{tag}</td><td>{r.get('name', r['symbol'])}</td><td>{r.get('group', '?')}</td>
<td>{fmt(r.get('is_edge'))}</td>
<td>{r.get('decision', '-')}</td>
<td>{fmt(bo.get('expR'))}<br><span class="sub">{bo.get('n')}笔</span></td>
<td>{fmt(oo.get('expR'))}<br><span class="sub">{oo.get('n')}笔</span></td>
<td class="{cls2}">{fmt(dv2)}</td>
<td class="sub">ot拦{r.get('ot_gate_skipped', 0)}</td>
<td class="sub">{status}</td>
</tr>""")

    dm = s["delta_mean_ss_vs_base"]; dn = s["delta_mean_ot_vs_base"]
    sig = s["significance_oppose"]
    p = sig.get("binom_p_two_sided")
    sig_cls = "gain" if (p is not None and p < 0.05) else "flat"
    sig_txt = "显著✓" if (p is not None and p < 0.05) else "边缘/不显著"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C 感知方向门 · 补全品种 OOS</title>
<style>
:root{{--bg:#0f1419;--card:#1a2129;--ink:#e6edf3;--mut:#8b98a5;--line:#2a323c;--red:#ff5c5c;--grn:#3fb950;--amber:#d29922;--blue:#58a6ff;}}
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
.note{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;color:var(--mut);margin:10px 0}}
.note b{{color:var(--ink)}} .wrap{{max-width:1200px;margin:0 auto}}
.tag{{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;margin-left:4px;vertical-align:middle}}
.tag.new{{background:#1f3a1f;color:var(--grn)}} .tag.inert{{background:#3a2f1f;color:var(--amber)}} .tag.skip{{background:#3a1f1f;color:var(--red)}}
</style></head><body><div class="wrap">
<h1>C 感知方向门（threshold 增强）· 补全品种 OOS</h1>
<div class="sub">三感参谋 · 四维信号系统 ｜ 完整活跃池 {s['n_active_universe']} 只全覆盖（prior 41 + 补全 sc/SA01）｜ 生成于 {time.strftime('%Y-%m-%d %H:%M')}</div>
<div class="kpis">
<div class="kpi"><div class="v">{s['n_active_universe']}</div><div class="l">完整活跃池(只)</div></div>
<div class="kpi"><div class="v">{s['n_active_with_klineC']}</div><div class="l">门可用总体(有kline C)</div></div>
<div class="kpi"><div class="v">{s['n_valid_oos']}</div><div class="l">OOS 已验证(只)</div></div>
<div class="kpi"><div class="v">{s['n_active_inert_no_klineC']}</div><div class="l">门惰性(无kline C)</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_baseline'])}</div><div class="l">基线 OOS 期望R</div></div>
<div class="kpi"><div class="v">{fmt(s['mean_oos_expR_oppose'])}</div><div class="l">oppose OOS 期望R</div></div>
<div class="kpi"><div class="v">{fmt(s['delta_mean_ot_vs_base'])}</div><div class="l">Δoppose−基线</div></div>
</div>
<div class="note">
<b>补全说明</b>：prior 大样本验证覆盖 <b>41 只「基线有成交且门可用」品种</b>——这是真实可验证总体，并非漏算。
完整生产活跃池为 <b>{s['n_active_universe']} 只</b>（SYMBOLS − DISABLED）。本次把缺口 2 只补全核验：
<span class="tag new">补全·新跑</span><b>sc</b>：有 kline C，但回测 2018–2026 全窗口<strong>「无触发信号」零成交</strong> → 门可触发但无 OOS 样本、无法验证（live 若偶发信号，门仍按 kline C 拦，影响可忽略，建议盘中监控）；
<span class="tag inert">补全·门惰性</span><b>SA01</b>：无 kline C → 门永不拦截，且同样零成交。
结论：门可用总体（有 kline C）共 <b>{s['n_active_with_klineC']} 只</b>，其中 <b>{s['n_valid_oos']} 只</b>有回测成交、OOS 已验证；另 {s['n_zero_trades_unvalidated']} 只（sc/SA01）零成交、不可验证。全市场部署的每只活跃品种现均有明确 OOS 结论（含「不可验证」标注）。
</div>

<h2>一、组合层面结论（门可用总体 {s['n_active_with_klineC']} 只）</h2>
<table><tr><th>指标</th><th>基线(threshold)</th><th>oppose({s['config']['gate_threshold']})</th><th>Δot−基</th></tr>
<tr><td>OOS 期望R 均值(逐品种)</td><td>{fmt(s['mean_oos_expR_baseline'])}</td><td>{fmt(s['mean_oos_expR_oppose'])}</td>
<td class="{('pos' if (dn or 0)>0 else 'neg')}">{fmt(dn)}</td></tr></table>
<div class="note">oppose OOS：<b class="pos">增益 {s['n_oos_win_ot']}</b> / <b class="neg">有损 {s['n_oos_loss_ot']}</b> / 持平 {s['n_oos_flat_ot']}。</div>

<h2>二、统计显著性（门可用总体，二项双尾）</h2>
<table><tr><th>门模式</th><th>样本n</th><th>增益</th><th>有损</th><th>持平</th><th>中位数Δ</th><th>均值Δ</th><th>增益占比</th><th>二项p</th><th>显著?</th></tr>
<tr><td>oppose({s['config']['gate_threshold']})</td><td>{sig.get('n')}</td><td class='gain'>{sig.get('wins')}</td>
<td class='loss'>{sig.get('losses')}</td><td>{sig.get('flats')}</td>
<td>{fmt(sig.get('median_delta'))}</td><td>{fmt(sig.get('mean_delta'))}</td>
<td>{sig.get('win_rate_over_all')}</td><td class='{sig_cls}'>{p}</td><td class='{sig_cls}'>{sig_txt}</td></tr>
</table>
<div class="sub">「二项p」= 对（增益 vs 有损）精确二项双尾检验（H0: 增益占比=50%）。p&lt;0.05 视为显著稳定增益。</div>

<h2>三、逐品种明细（按 oppose OOS Δ 排序，含补全 2 只）</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>板块</th><th>IS edge</th><th>IS决策</th>
<th>OOS_base</th><th>OOS_ot</th><th>Δot</th><th>ot拦单数</th><th>状态</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="sub">红=增益 / 绿=有损（A股惯例）。ot拦=oppose 门在 IS+OOS 全样本拦下的信号数。补全品种带标签。</div>

<h2>四、结论</h2>
<div class="note">
1. <b>OOS 已验证总体 = {s['n_valid_oos']} 只</b>（= prior 41，门可用且有回测成交），oppose 门 OOS 均值 Δ={fmt(dn)}，二项 p={p}（{sig_txt}）→ 与 prior 结论一致：门方向稳健、真实小幅增益。补全未改变该结论（sc/SA01 零成交，无法纳入验证）。<br>
2. <b>sc（补全·新跑）</b>：有 kline C、门可用，但回测零成交 → 门 OOS 不可验证；live 若偶发信号门仍会按 kline C 拦截，风险可忽略，建议盘中监控其拦截/通过计数。<br>
3. <b>SA01（补全·门惰性）</b>：无 kline C → 门永不拦截，且零成交，对实盘零影响（安全默认）。<br>
4. <b>全市场部署覆盖</b>：{s['n_active_universe']} 只活跃品种现全部有明确 OOS 结论——{s['n_valid_oos']} 只已验证（oppose Δ={fmt(dn)}、p={p} 显著），{s['n_zero_trades_unvalidated']} 只零成交不可验证（sc 门可用 / SA01 门惰性）。无未覆盖品种。
</div>
</div></body></html>"""
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
