#!/usr/bin/env python3
"""读取 oos_dir_mode_main.json / oos_dir_mode_c_real.json，生成全市场 direction_mode OOS 对比 HTML 报告。"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# 报告输出目录：默认 docs/oos-reports/，可用环境变量 OOS_REPORT_DIR 覆盖
WS = os.environ.get("OOS_REPORT_DIR") or os.path.join(HERE, "docs", "oos-reports")
main = json.load(open(os.path.join(HERE, "oos_dir_mode_main.json"), encoding="utf-8"))
creal = json.load(open(os.path.join(HERE, "oos_dir_mode_c_real.json"), encoding="utf-8"))


def cls(d):
    if d > 0.01:
        return "pos"
    if d < -0.01:
        return "neg"
    return "flat"


def verdict(d):
    if d > 0.01:
        return "▲增益"
    if d < -0.01:
        return "▼有损"
    return "=持平"


# ---------- 主对比全表 ----------
trs = ""
for r in main["rows"]:
    th, co = r["thr"], r["comb"]
    de = r["delta_expR"]
    c = cls(de)
    trs += (
        f'<tr class="{c}"><td class="sym">{r["symbol"]}</td><td>{r["name"]}</td><td>{r["group"]}</td>'
        f'<td>{th["trades"]}</td><td>{th["expR"]}</td><td>{th["total_R"]}</td>'
        f'<td>{th["win_rate"]*100:.1f}%</td><td>{th["max_dd_R"]}</td>'
        f'<td>{co["trades"]}</td><td>{co["expR"]}</td><td>{co["total_R"]}</td>'
        f'<td>{co["win_rate"]*100:.1f}%</td><td>{co["max_dd_R"]}</td>'
        f'<td class="delta {c}">{de:+.3f}</td><td>{r["delta_total_R"]:+.1f}</td>'
        f'<td>{r["delta_trades"]:+d}</td><td class="v">{verdict(de)}</td></tr>'
    )

# ---------- 分组统计（板块） ----------
grp_rows = ""
for g, gs in sorted(main["summary"]["by_group"].items(), key=lambda kv: kv[1]["avg_delta_expR"]):
    gc = cls(gs["avg_delta_expR"])
    grp_rows += (
        f'<tr class="{gc}"><td>{g}</td><td>{gs["n"]}</td><td>{gs["n_imp"]}</td>'
        f'<td>{gs["n_dec"]}</td><td class="delta {gc}">{gs["avg_delta_expR"]:+.3f}</td>'
        f'<td>{gs["avg_delta_total_R"]:+.1f}</td></tr>'
    )

# ---------- C 覆盖分组 ----------
bc = main["summary"]["by_C_coverage"]
c_cov_rows = ""
for key, label in (("has_real_C", "有真实长C历史(6品种)"), ("no_real_C", "无C长历史(其余)")):
    x = bc.get(key, {})
    if not x or x.get("n", 0) == 0:
        c_cov_rows += f'<tr><td>{label}</td><td colspan="5">无有效样本</td></tr>'
        continue
    gc = cls(x.get("avg_delta_expR", 0))
    c_cov_rows += (
        f'<tr class="{gc}"><td>{label}</td><td>{x["n"]}</td><td>{x["n_imp"]}</td>'
        f'<td>{x["n_dec"]}</td><td class="delta {gc}">{x.get("avg_delta_expR",0):+.3f}</td>'
        f'<td>{x.get("avg_delta_total_R",0):+.1f}</td></tr>'
    )

# ---------- 补充对比（真实C） ----------
creal_rows = ""
for r in creal["rows"]:
    th, co = r["thr"], r["comb"]
    de = r["delta_expR"]
    c = cls(de)
    creal_rows += (
        f'<tr class="{c}"><td class="sym">{r["symbol"]}</td><td>{r["name"]}</td>'
        f'<td>{th["trades"]}</td><td>{th["expR"]}</td><td>{co["trades"]}</td>'
        f'<td>{co["expR"]}</td><td class="delta {c}">{de:+.3f}</td>'
        f'<td>{r["delta_total_R"]:+.1f}</td><td class="v">{verdict(de)}</td></tr>'
    )

s = main["summary"]
sc = creal["summary"]
date_str = "2026-09-07"

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全市场 direction_mode OOS 对比 · {date_str}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222;line-height:1.5}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 22px 60px}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:#666;font-size:13px;margin-bottom:18px}}
.card{{background:#fff;border:1px solid #e6e8eb;border-radius:10px;padding:16px 18px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.cards{{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:18px}}
.kpi{{flex:1;min-width:150px;background:#fff;border:1px solid #e6e8eb;border-radius:10px;padding:14px 16px}}
.kpi .v{{font-size:26px;font-weight:700}}
.kpi .l{{font-size:12px;color:#888;margin-top:2px}}
.kpi.red .v{{color:#c0392b}} .kpi.green .v{{color:#1e8449}} .kpi.gray .v{{color:#555}}
h2{{font-size:17px;margin:0 0 12px;border-left:4px solid #c0392b;padding-left:9px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #e9ebee;padding:5px 7px;text-align:center}}
th{{background:#f0f2f5;font-weight:600;position:sticky;top:0}}
td.sym{{font-weight:700}}
td.v{{font-weight:700}}
tr.pos{{background:#fdecea}} tr.neg{{background:#eafaf1}} tr.flat{{background:#fafafa}}
.delta.pos{{color:#c0392b;font-weight:700}} .delta.neg{{color:#1e8449;font-weight:700}} .delta.flat{{color:#95a5a6}}
.note{{font-size:12.5px;color:#555;background:#fffaf0;border:1px solid #f3e2bf;border-radius:8px;padding:11px 14px;margin:10px 0}}
.warn{{font-size:13px;color:#8a4b00;background:#fff4e6;border:1px solid #ffd8a8;border-radius:8px;padding:12px 15px;margin:10px 0}}
.good{{color:#c0392b;font-weight:700}} .bad{{color:#1e8449;font-weight:700}}
ul{{margin:6px 0 6px 18px;padding:0}} li{{margin:5px 0}}
.code{{font-family:monospace;background:#f4f4f6;padding:1px 5px;border-radius:4px}}
.scroll{{overflow-x:auto}}
small{{color:#999}}
</style></head><body><div class="wrap">

<h1>全市场 direction_mode OOS 对比</h1>
<div class="sub">候选改进 <span class="code">combined</span> 模式 vs 当前默认 <span class="code">threshold</span> 模式 · 生成于 {date_str} · 数据：本地日线 + fundamentals.json(F) + cpos_cache(C)</div>

<div class="cards">
  <div class="kpi gray"><div class="v">{s['n_valid']}</div><div class="l">有效对比品种</div></div>
  <div class="kpi red"><div class="v">{s['n_improve']}</div><div class="l">combined 增益 ▲</div></div>
  <div class="kpi green"><div class="v">{s['n_degrade']}</div><div class="l">combined 有损 ▼</div></div>
  <div class="kpi gray"><div class="v">{s['n_flat']}</div><div class="l">持平 =</div></div>
  <div class="kpi green"><div class="v">{s['avg_delta_expR']:+.4f}</div><div class="l">平均Δ期望R/笔</div></div>
</div>

<div class="card">
<h2>一句话结论</h2>
<p><b>combined 模式<b>不应</b>全市场一刀切切换。</b>在剔除资金面 C 泄漏的严格 OOS 下，combined（F 参与定方向）相对 threshold（F 仅调制阈值）<b class="bad">平均轻微负向（{s['avg_delta_expR']:+.4f} R/笔）</b>，
变差品种数（{s['n_degrade']}）是增益品种数（{s['n_improve']}）的两倍以上。F/C 作为<b>方向调制器</b>稳定有效，作为<b>方向决策者</b>反而引入噪声——与前期"C 维度在 threshold 下零贡献"的消融结论一致。</p>
<p>个别品种（<span class="good">rb / ru / c / UR / OI / PX</span>）combined 确有真实增益，可考虑<b>逐品种选择性启用</b>（需 IS/OOS 分离防过拟合）；真实资金面 C 维度的贡献在当前回测框架下<b>无法严格验证</b>（见补充对比）。</p>
</div>

<div class="card">
<h2>方法论</h2>
<ul>
<li><b>OOS 口径</b>：<span class="code">walk_forward_backtest</span> 不调参滚动（window=300 预热），逐 bar 用截至当日数据算 pipeline，下一根开盘入场，stop/2R 出场，扣费扣滑点。</li>
<li><b>主对比（全市场有效品种 = SYMBOLS − DISABLED，共 {s['n_total']} 个有日线）</b>：两模式均 <span class="code">ablate="C"</span>（C=0 中性），<b>剔除无 C 长历史品种回测早期的"最新值回落"泄漏</b>，从而纯测 <b>F 维度参与定方向</b> 的价值（这恰是多数无 C 数据品种的现实假设）。</li>
<li><b>补充对比（6 真实 C 品种）</b>：用真实 C + <span class="code">slice_c_window</span> 切到 C 真实区间，看 F+C 共同定方向增益。</li>
<li><b>判定的差异来源</b>：combined 仅在 <span class="code">dir_T_raw≠0 或 |bias_G|≥bias_g_min(50)</span> 时启用 F/C 定方向，多数 bar 退化为 threshold —— 这解释了为何多数品种 Δ=0。</li>
</ul>
</div>

<div class="card">
<h2>主对比 · 全品种明细（threshold vs combined，C=0 中性）</h2>
<div class="scroll">
<table>
<thead><tr><th rowspan="2">品种</th><th rowspan="2">名称</th><th rowspan="2">板块</th>
<th colspan="5">threshold(默认)</th><th colspan="5">combined(候选)</th>
<th colspan="3">差异</th><th rowspan="2">判定</th></tr>
<tr><th>笔</th><th>期望R</th><th>总R</th><th>胜率</th><th>回撤R</th>
<th>笔</th><th>期望R</th><th>总R</th><th>胜率</th><th>回撤R</th>
<th>ΔexpR</th><th>Δ总R</th><th>Δ笔</th></tr></thead>
<tbody>{trs}</tbody></table></div>
<div class="note">红=增益(好) · 绿=损耗(坏) · 灰=持平。ΔexpR 正为红、负为绿（遵循"涨红跌绿"惯例）。</div>
</div>

<div class="card">
<h2>分组汇总</h2>
<h3 style="font-size:14px;margin:6px 0">按板块</h3>
<table><thead><tr><th>板块</th><th>有效数</th><th>增益</th><th>有损</th><th>平均ΔexpR</th><th>平均Δ总R</th></tr></thead>
<tbody>{grp_rows}</tbody></table>
<h3 style="font-size:14px;margin:14px 0 6px">按资金面 C 资产覆盖</h3>
<table><thead><tr><th>分组</th><th>有效数</th><th>增益</th><th>有损</th><th>平均ΔexpR</th><th>平均Δ总R</th></tr></thead>
<tbody>{c_cov_rows}</tbody></table>
</div>

<div class="card">
<h2>补充对比 · 6 真实 C 品种（真实 C + slice 到 C 真实区间）</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>thr笔</th><th>thr期望R</th><th>comb笔</th><th>comb期望R</th><th>ΔexpR</th><th>Δ总R</th><th>判定</th></tr></thead>
<tbody>{creal_rows}</tbody></table>
<div class="warn"><b>⚠ 样本严重不足，结论不可靠。</b> 6 品种 C 真实区间仅 169–230 天，而 <span class="code">walk_forward window=300</span> 预热要求吞噬了短区间，导致 slice 后有效可交易样本枯竭（FG/SA 仅 1 笔、J/JD/LH 0 笔、JM 5 笔）。
这并非 combined 模式本身问题，而是<b>回测框架预热窗口与 C 历史长度不匹配</b>的方法论限制——真实 C 维度的 OOS 贡献在当前日线龙虎榜框架下无法严格验证。</div>
</div>

<div class="card">
<h2>结论与建议</h2>
<ol>
<li><b>不要全市场切 combined。</b> 全市场 OOS 平均 ΔexpR={s['avg_delta_expR']:+.4f}（轻微负向），变差({s['n_degrade']})≫增益({s['n_improve']})；F/C 作为方向调制器稳定，作为方向决策者引入噪声。</li>
<li><b>逐品种选择性启用（候选路径）。</b> rb/ru/c/UR/OI/PX 等个别品种 combined 有真实增益，可跑「训练期决定开/关 combined → 测试期验证」的 IS/OOS 分离实验，避免样本窥探式过拟合。</li>
<li><b>真实 C 维度贡献无法用日线龙虎榜严格验证。</b> 需①拉长 C 历史至 &gt;380 根（~18 个月，天勤更久回溯或合成）；②或下调 walk_forward window；③<b>或改走实时 C_flow 路径（盘中 tick 订单流）</b>——那才是资金面 C 真正发挥威力的场景，而非日线龙虎榜回填。</li>
<li><b>C 维度定位维持。</b> 日线龙虎榜 C 当前更适合做 threshold 模式下的「阈值调制 + 硬否决」角色；要释放其「定方向」价值，应接实时盘中 C_flow 而非依赖历史龙虎榜。</li>
</ol>
<p><small>数据源：fourd_run/oos_dir_mode_main.json（主对比）、oos_dir_mode_c_real.json（补充对比）。回测不注入任何 live 专属维度（info/HMM/宏观/GARCH），严守 OOS 红线。</small></p>
</div>

</div></body></html>"""

os.makedirs(WS, exist_ok=True)
out = os.path.join(WS, f"全市场_direction_mode_OOS对比_{date_str.replace('-','')}.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("written:", out, "bytes:", len(html))
