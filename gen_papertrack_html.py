"""gen_papertrack_html.py
把 papertrack_report.json(四维策略真实 walk-forward 回测)渲染成自包含可视化面板。

特性:
  · 零依赖、零联网: 数据内联为 JS 对象 + 纯 JS/SVG 画图, 双击 HTML 即看。
  · 用途: 每天盘后跑一次, 刷新真实表现(胜率/期望R/权益曲线/品种热力图/交易明细)。

用法:
    cd /Users/ken/WorkBuddy/futures-orderflow
    python3 gen_papertrack_html.py
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "papertrack_report.json")
OUT = os.path.join(HERE, "papertrack_dashboard.html")

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>四维策略 · 真实回测面板</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --line:#2a323d;
    --txt:#e6edf3; --mut:#8b97a6; --red:#ff5c5c; --grn:#3fd47a;
    --ylw:#ffcf4d; --blue:#5aa9ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1080px;margin:0 auto;padding:22px 20px 60px;}
  h1{font-size:20px;margin:0 0 2px;font-weight:650}
  .sub{color:var(--mut);font-size:12.5px;margin-bottom:18px;line-height:1.5}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 8px;text-align:center}
  .kpi .v{font-size:20px;font-weight:700;line-height:1.1}
  .kpi .l{font-size:11px;color:var(--mut);margin-top:5px}
  .pos{color:var(--grn)} .neg{color:var(--red)} .warn{color:var(--ylw)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:20px}
  .card h2{font-size:14px;margin:0 0 12px;font-weight:600;color:var(--txt)}
  .card h2 small{color:var(--mut);font-weight:400;font-size:11.5px;margin-left:8px}
  svg{display:block;width:100%;height:auto}
  .heat{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
  .hc{border-radius:9px;padding:9px 10px;color:#0c0f13;overflow:hidden}
  .hc .sym{font-weight:700;font-size:14px}
  .hc .sym span{font-weight:400;font-size:11px}
  .hc .n{font-size:10.5px;opacity:.78;margin-top:1px}
  .hc .r{font-size:18px;font-weight:800;margin-top:3px}
  .hc .x{font-size:10px;opacity:.82;margin-top:1px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line)}
  th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--panel)}
  td.l,th.l{text-align:left}
  .scroll{max-height:440px;overflow:auto;border-radius:8px}
  .tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;font-weight:600}
  .tw{background:rgba(63,212,122,.18);color:var(--grn)}
  .tl{background:rgba(255,92,92,.18);color:var(--red)}
  .foot{color:var(--mut);font-size:11px;margin-top:8px;line-height:1.6}
  .legend{display:flex;gap:14px;font-size:11px;color:var(--mut);margin-top:8px;flex-wrap:wrap}
  .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
  .att-tbl{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:4px}
  .att-tbl th,.att-tbl td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
  .att-tbl th{color:var(--mut);font-weight:600}
  .att-tbl td.l,.att-tbl th.l{text-align:left}
  .att-bar{display:flex;align-items:center;gap:8px;margin:9px 0}
  .atb-lab{width:128px;font-size:12px;color:var(--mut)}
  .atb-track{position:relative;flex:1;height:26px;background:#11161d;border-radius:6px;overflow:hidden}
  .atb-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#465060}
  .atb-fill{position:absolute;top:0;bottom:0}
  .atb-val{width:62px;text-align:right;font-weight:700;font-size:13px}
  .dim-badge{display:inline-block;min-width:22px;text-align:center;padding:1px 5px;border-radius:5px;font-size:11px;font-weight:700;margin-right:3px}
  .db-up{background:rgba(63,212,122,.2);color:var(--grn)}
  .db-dn{background:rgba(255,92,92,.2);color:var(--red)}
</style>
</head>
<body>
<div class="wrap">
  <h1>四维策略 · 真实回测面板</h1>
  <div class="sub" id="sub"></div>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2>权益曲线 <small>累计真实 R（按时间顺序，信号后实际行情 walk-forward 判定）</small></h2>
    <div id="chart"></div>
    <div class="legend">
      <span><span class="dot" style="background:var(--blue)"></span>等权累计R</span>
      <span><span class="dot" style="background:var(--ylw)"></span>手数加权累计R</span>
    </div>
  </div>

  <div class="card">
    <h2>四维盈亏归因 (P1-3) <small>按引擎权重 0.6T / 0.25F / 0.15C 拆解每笔盈亏来源；维度投票与交易方向一致才记功/记过</small></h2>
    <div id="attTable"></div>
    <div style="margin-top:14px;color:var(--mut);font-size:11.5px">归因R（各维对累计R的贡献，仅计「投票=交易方向」的维度；中标/中立维记0，故三维修正R和 ≤ 总R）</div>
    <div id="attBars"></div>
    <div class="foot" id="attNote"></div>
  </div>

  <div class="card">
    <h2>按品种胜率热力图 <small>颜色=胜率（红低→绿高），标注笔数 / 胜率 / 累计R，按累计R降序</small></h2>
    <div class="heat" id="heat"></div>
  </div>

  <div class="card">
    <h2>交易明细 <small id="tcount"></small></h2>
    <div class="scroll">
      <table id="tbl">
        <thead><tr>
          <th class="l">时间</th><th class="l">品种</th><th class="l">方向</th>
          <th>入场</th><th>止损</th><th>目标</th>
          <th>结果</th><th>实际R</th><th>持仓(根)</th><th>精度</th><th class="l">维度投票</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="foot" id="foot"></div>
  </div>
</div>

<script>
const DATA = __DATA__;
const symCN = {FG:"玻璃",SA:"纯碱",JM:"焦煤",J:"焦炭",jd:"鸡蛋",lh:"生猪",
  hc:"热卷",eb:"苯乙烯",zn:"沪锌",al:"沪铝",cs:"玉米淀粉",fu:"燃油",
  sp:"纸浆",y:"豆油",CF:"棉花",p:"棕榈",au:"黄金",ag:"白银",ss:"不锈钢",
  rb:"螺纹",i:"铁矿",m:"豆粕",RM:"菜粕",OI:"菜油",SR:"白糖",TA:"PTA",
  MA:"甲醇",PP:"聚丙烯",l:"塑料",v:"PVC",RU:"橡胶",BU:"沥青",pg:"液化气"};
const dirCN = {long:"多",short:"空"};

document.getElementById('sub').textContent =
  '数据源 '+(DATA.meta.source||'')+' · 生成于 '+(DATA.meta.generated_at||'')+
  ' · 方法: 信号后实际行情逐根判定(先触目标/先触止损) · 不触达信号不计入胜率';

const h = DATA.headline;
const fmt = (x,d=2)=> (x>=0?'+':'')+Number(x).toFixed(d);
const kpis = [
  {l:'真实胜率', v:(h.win_rate*100).toFixed(1)+'%', c: h.win_rate>=0.5?'pos':(h.win_rate>=0.4?'warn':'neg')},
  {l:'真实期望R', v:fmt(h.expected_R), c: h.expected_R>=0?'pos':'neg'},
  {l:'累计R(等权)', v:fmt(h.final_cum_R), c: h.final_cum_R>=0?'pos':'neg'},
  {l:'累计R(加权)', v:fmt(h.final_cum_R_lotweighted), c: h.final_cum_R_lotweighted>=0?'pos':'neg'},
  {l:'最长连亏', v:h.max_consecutive_losses+(h.consecutive_loss_warning?' ⚠':''), c: h.consecutive_loss_warning?'warn':'pos'},
  {l:'已判定/待定', v:DATA.summary.cumulative_done+'/'+(DATA.summary.pending_count||0), c:'pos'},
];
document.getElementById('kpis').innerHTML = kpis.map(k=>
  `<div class="kpi"><div class="v ${k.c}">${k.v}</div><div class="l">${k.l}</div></div>`).join('');

// ---------- 权益曲线 ----------
function drawChart(){
  const eq = DATA.equity;
  if(!eq.length){document.getElementById('chart').innerHTML='<div class="foot">暂无已判定交易</div>';return;}
  const W=1000,H=300,pl=46,pr=16,pt=16,pb=26;
  const ys1 = eq.map(e=>e.cum_R), ys2 = eq.map(e=>e.cum_R_lotweighted);
  const all=ys1.concat(ys2);
  let ymin=Math.min(0,...all), ymax=Math.max(0,...all);
  const pad=(ymax-ymin)*0.1||1; ymin-=pad; ymax+=pad;
  const X=i=> pl + (i/(eq.length-1||1))*(W-pl-pr);
  const Y=v=> pt + (1-(v-ymin)/(ymax-ymin||1))*(H-pt-pb);
  let g='';
  for(let k=0;k<=4;k++){
    const v=ymin+(ymax-ymin)*k/4, y=Y(v);
    g+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="#2a323d" stroke-width="1"/>`;
    g+=`<text x="${pl-6}" y="${y+4}" fill="#8b97a6" font-size="10" text-anchor="end">${v.toFixed(1)}</text>`;
  }
  if(ymin<0&&ymax>0){const yz=Y(0);g+=`<line x1="${pl}" y1="${yz}" x2="${W-pr}" y2="${yz}" stroke="#465060" stroke-width="1.5"/>`;}
  const path=ys=>ys.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
  g+=`<path d="${path(ys1)}" fill="none" stroke="#5aa9ff" stroke-width="2"/>`;
  g+=`<path d="${path(ys2)}" fill="none" stroke="#ffcf4d" stroke-width="2"/>`;
  const lx=X(eq.length-1);
  g+=`<circle cx="${lx}" cy="${Y(ys1[ys1.length-1])}" r="3.5" fill="#5aa9ff"/>`;
  g+=`<circle cx="${lx}" cy="${Y(ys2[ys2.length-1])}" r="3.5" fill="#ffcf4d"/>`;
  g+=`<text x="${lx-4}" y="${Y(ys1[ys1.length-1])-8}" fill="#5aa9ff" font-size="11" text-anchor="end">${ys1[ys1.length-1].toFixed(2)}</text>`;
  g+=`<text x="${lx-4}" y="${Y(ys2[ys2.length-1])+16}" fill="#ffcf4d" font-size="11" text-anchor="end">${ys2[ys2.length-1].toFixed(2)}</text>`;
  document.getElementById('chart').innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}
drawChart();

// ---------- 品种热力图 ----------
function wrColor(wr){
  wr=Math.max(0,Math.min(1,wr));
  const r = wr<0.5 ? 230 : Math.round(230*(1-(wr-0.5)*2));
  const g = wr>0.5 ? 205 : Math.round(205*wr*2);
  return `rgb(${r},${g},70)`;
}
const syms=Object.entries(DATA.by_symbol).sort((a,b)=>b[1].final_cum_R-a[1].final_cum_R);
document.getElementById('heat').innerHTML = syms.map(([s,b])=>{
  const wr=b.win_rate, r=b.final_cum_R;
  return `<div class="hc" style="background:${wrColor(wr)}">
    <div class="sym">${s} <span>${symCN[s]||''}</span></div>
    <div class="n">${b.total}笔 · 胜${b.wins}/亏${b.losses}</div>
    <div class="r">${(wr*100).toFixed(0)}%</div>
    <div class="x">累计R ${r>=0?'+':''}${r.toFixed(2)}</div>
  </div>`;
}).join('');

// ---------- 交易明细 ----------
const tb=document.querySelector('#tbl tbody');
document.getElementById('tcount').textContent = '共 '+DATA.trades.length+' 笔已判定';
tb.innerHTML = DATA.trades.slice().reverse().map(t=>{
  const win=t.outcome==='win', R=t.R;
  return `<tr>
    <td class="l">${(t.time||'').slice(0,16)}</td>
    <td class="l">${t.symbol} ${symCN[t.symbol]||''}</td>
    <td class="l">${dirCN[t.direction]||t.direction}</td>
    <td>${t.entry}</td><td>${t.stop}</td><td>${t.target}</td>
    <td><span class="tag ${win?'tw':'tl'}">${win?'盈':'损'}</span></td>
    <td class="${R>=0?'pos':'neg'}">${R>=0?'+':''}${R}</td>
    <td>${t.holding_bars||'-'}</td>
    <td>${t.gran||'-'}</td>
    <td>${(()=>{const dv=t.dim_votes; if(!dv) return '<span style="color:var(--mut)">—</span>';
      return ['F','T','C'].map(k=>{const x=dv[k]; if(!x||x.vote===0||x.vote==null) return '<span class="dim-badge db-dn" style="opacity:.4">'+k+'–</span>';
        const cls=x.agree?'db-up':'db-dn'; const arr=x.vote>0?'▲':'▼';
        return '<span class="dim-badge '+cls+'">'+k+arr+'</span>';}).join('');})()}</td>
  </tr>`;
}).join('');

  document.getElementById('foot').innerHTML =
    '盈亏平衡胜率(1/(1+期望R)): '+(DATA.summary.breakeven!=null?(DATA.summary.breakeven*100).toFixed(1)+'%':'-')+
    ' · 待定信号='+(DATA.summary.pending_count||0)+' 笔(行情未走完, 下次自动重评)';

// ---------- 四维盈亏归因 (P1-3) ----------
function drawAttribution(){
  const att = DATA.summary.attribution;
  if(!att || !att.dims){document.getElementById('attTable').innerHTML='<div class="foot">暂无归因数据</div>';return;}
  const dims = att.dims||{};
  const overall = att.overall_win_rate||0;
  const rows = ['F','T','C'].map(k=>dims[k]).filter(Boolean).map(d=>{
    const agr=d.agreement_rate, wia=d.win_if_agree;
    const agrCls = agr>=0.7?'pos':(agr>=0.5?'warn':'neg');
    const wiaCls = wia!=null ? (wia>overall?'pos':(wia<overall?'neg':'warn')) : '';
    return `<tr>
      <td class="l">${d.label}</td>
      <td>${d.n_voted}</td>
      <td class="${agrCls}">${(agr*100).toFixed(0)}%</td>
      <td class="${wiaCls}">${wia!=null?(wia*100).toFixed(0)+'%':'—'}</td>
      <td class="${d.attr_R>=0?'pos':'neg'}">${d.attr_R>=0?'+':''}${d.attr_R.toFixed(2)}</td>
      <td class="${d.attr_R_per_trade>=0?'pos':'neg'}">${d.attr_R_per_trade!=null?(d.attr_R_per_trade>=0?'+':'')+d.attr_R_per_trade.toFixed(3):'—'}</td>
    </tr>`;
  }).join('');
  let grows='';
  const g=att.G;
  if(g){grows=`<tr>
    <td class="l">${g.label} <span style="color:var(--mut);font-size:11px">参考维·不计入R和</span></td>
    <td>${g.n_voted}</td>
    <td class="${g.agreement_rate>=0.7?'pos':'warn'}">${(g.agreement_rate*100).toFixed(0)}%</td>
    <td>${g.win_if_agree!=null?(g.win_if_agree*100).toFixed(0)+'%':'—'}</td>
    <td>—</td><td>—</td></tr>`;}
  document.getElementById('attTable').innerHTML = `
    <table class="att-tbl"><thead><tr>
      <th class="l">维度</th><th>投票数</th><th>方向一致率</th><th>同意时胜率</th><th>归因R</th><th>每笔均R</th>
    </tr></thead><tbody>${rows}${grows}</tbody></table>
    <div class="foot">总胜率 ${(overall*100).toFixed(1)}% · 维度权重 T0.6 / F0.25 / C0.15 · 一致率=该维投票与交易方向同向的比例；「同意时胜率」高于总胜率=该维带来正边缘</div>`;

  const arr=['F','T','C'].map(k=>dims[k]).filter(Boolean);
  if(arr.length){
    const maxv=Math.max(0.01,...arr.map(d=>Math.abs(d.attr_R)));
    document.getElementById('attBars').innerHTML = arr.map(d=>{
      const v=d.attr_R, pct=Math.abs(v)/maxv*50;
      const col=v>=0?'var(--grn)':'var(--red)';
      const fill = v>=0 ? `left:50%;width:${pct}%` : `right:50%;width:${pct}%`;
      return `<div class="att-bar">
        <div class="atb-lab">${d.label.split('(')[0]}</div>
        <div class="atb-track"><div class="atb-zero"></div><div class="atb-fill" style="${fill};background:${col}"></div></div>
        <div class="atb-val ${v>=0?'pos':'neg'}">${v>=0?'+':''}${v.toFixed(2)}</div>
      </div>`;
    }).join('');
  }

  const f=dims.F, t=dims.T;
  const ins=[];
  if(f && f.win_if_agree!=null)
    ins.push(`<b>基本面(F)</b> 同意时胜率 ${(f.win_if_agree*100).toFixed(0)}% ${f.win_if_agree>overall?'高于':'低于或等于'}总胜率，归因R ${f.attr_R>=0?'+':''}${f.attr_R.toFixed(1)} → ${f.win_if_agree>overall?'是本系统的真实边际来源':'未带来边缘'}`);
  if(t && t.win_if_agree!=null)
    ins.push(`<b>技术触发(T)</b> 同意时胜率 ${(t.win_if_agree*100).toFixed(0)}% ${t.win_if_agree<overall?'低于':'高于或等于'}总胜率 ${t.win_if_agree<overall?'→ 仅「扣扳机」而不自带预测力，甚至与结果反向关联':''}`);
  document.getElementById('attNote').innerHTML = ins.join('<br>');
}
drawAttribution();
</script>
</body>
</html>
"""


def main():
    with open(REPORT, encoding="utf-8") as f:
        rep = json.load(f)

    s = rep.get("summary", {})
    data = {
        "meta": rep.get("meta", {}),
        "headline": s.get("headline", {}),
        "by_symbol": s.get("by_symbol", {}),
        "equity": rep.get("equity_curve", []),
        "trades": rep.get("trades", []),
        "summary": {
            "new_scored": s.get("new_scored"),
            "pending_count": s.get("pending_count"),
            "cumulative_done": s.get("cumulative_done"),
            "breakeven": s.get("breakeven_required_winrate"),
            "attribution": s.get("attribution"),
        },
    }

    js = json.dumps(data, ensure_ascii=False)
    js = js.replace("</", "<\\/")  # 防止 </script> 破坏 HTML
    html_doc = TEMPLATE.replace("__DATA__", js)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print("已生成面板:", OUT)
    print(
        "  KPI: 胜率 %.1f%%  期望R %+.3f  累计R %+.2f(等权)/%+.2f(加权)  已判定 %d / 待定 %d"
        % (
            s.get("headline", {}).get("win_rate", 0) * 100,
            s.get("headline", {}).get("expected_R", 0),
            s.get("headline", {}).get("final_cum_R", 0),
            s.get("headline", {}).get("final_cum_R_lotweighted", 0),
            s.get("cumulative_done", 0),
            s.get("pending_count", 0),
        )
    )


if __name__ == "__main__":
    main()
