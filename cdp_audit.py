#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDP 实测审计：不靠手写色值模型，直接读 Chrome 的 computed style，
把「从根到元素」每一层的 backgroundColor 逐层合成，得到元素实际渲染色，
再取 color 算 WCAG 对比度。hidden 元素也能算（computed style 不依赖可见性）。

专治两类镜像错误：
  - 暗字压淡染（.review-kpi-tag）
  - 亮字压同色系淡染（.sec-tag 变体）
以及「hover / 激活之后才掉下去」的状态型取值。
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
_ns = {}
exec(open(os.path.join(HERE, 'cdp_shots.py'), encoding='utf-8').read().split("def main()")[0], _ns)
WS = _ns['WS']

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9377

PAINT = r"""
(() => {
  function parse(c){
    const m = c.match(/[\d.]+/g); if(!m) return null;
    const n = m.map(Number);
    return {r:n[0], g:n[1], b:n[2], a: n.length>3 ? n[3] : 1};
  }
  function over(top, bot){
    const a = top.a;
    return {r: top.r*a + bot.r*(1-a), g: top.g*a + bot.g*(1-a),
            b: top.b*a + bot.b*(1-a), a: 1};
  }
  function name(node){
    if(node.id) return '#'+node.id;
    let t = node.tagName.toLowerCase();
    if(node.className && typeof node.className==='string')
      t += '.'+node.className.trim().split(/\s+/).filter(x=>x.indexOf('_probe')<0&&x.indexOf('__probe')<0).join('.');
    return t;
  }
  function stack(el){
    const chain = []; let p = el;
    while(p){ chain.unshift(p); p = p.parentElement; }
    let acc = {r:255,g:255,b:255,a:1};
    const layers = [];
    for(const node of chain){
      const s = getComputedStyle(node);
      const bg = parse(s.backgroundColor);
      const bi = s.backgroundImage;
      if(bi && bi !== 'none' && (!bg || bg.a < 1)){
        const g = bi.match(/rgba?\([^)]+\)/g);
        if(g && g.length){
          const gb = parse(g[0]);
          if(gb && gb.a > 0){ acc = over(gb, acc); layers.push('渐变首标 '+g[0]+' ('+name(node)+')'); }
        }
      }
      if(bg && bg.a > 0){ acc = over(bg, acc); layers.push(s.backgroundColor+'  ('+name(node)+')'); }
    }
    return {painted:[Math.round(acc.r),Math.round(acc.g),Math.round(acc.b)], layers};
  }
  function lin(c){ c/=255; return c<=0.04045 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); }
  function Lstr(t){ const m=t.match(/[\d.]+/g).map(Number);
    return 0.2126*lin(m[0]) + 0.7152*lin(m[1]) + 0.0722*lin(m[2]); }
  function lum(p){ return 0.2126*lin(p[0]) + 0.7152*lin(p[1]) + 0.0722*lin(p[2]); }
  function cr(a,b){ const hi=Math.max(a,b), lo=Math.min(a,b); return (hi+0.05)/(lo+0.05); }

  window.__probe = function(sel){
    const el = document.querySelector(sel);
    if(!el) return {sel, missing:true};
    const r = stack(el);
    const s = getComputedStyle(el);
    return {sel, color: s.color, painted: 'rgb('+r.painted.join(',')+')',
            cr: +cr(Lstr(s.color), lum(r.painted)).toFixed(2),
            animation: s.animationName, bgc: s.backgroundColor, layers: r.layers};
  };
  return 1;
})()
"""

INJECT = r"""
(() => {
  const mk = (tag, id, cls, txt, parent) => {
    const e = document.createElement(tag);
    e.id = id; e.className = cls; e.textContent = txt;
    (parent || document.body).appendChild(e); return e;
  };
  if(!document.getElementById('kill-banner')){
    const b = document.createElement('div'); b.id='kill-banner';
    b.innerHTML = '<div class="kb-top"><span class="kb-tag">🛑 账户硬熔断</span>'+
      '<span class="kb-reason">组合级回撤</span><span class="kb-btns">'+
      '<button>已按清单全平</button><button class="kb-reset">解除熔断</button></span></div>';
    document.body.appendChild(b);
  }
  const host = document.querySelector('.daily-review-summary') || document.body;
  mk('span','__probe_base','review-kpi-tag','标签',host);
  mk('span','__probe_record','review-kpi-tag record','记录',host);
  const tb = document.createElement('table'); tb.className='ps-ladder';
  tb.innerHTML = '<thead><tr><th id="__probe_th">方向</th></tr></thead><tbody><tr><td>多</td></tr></tbody>';
  document.body.appendChild(tb);
  mk('span','__probe_halted','rs-HALTED','熔断');
  // sec-tag 的规则挂在 `.sym-picker .sym-item .sec-tag`，探针必须按真实层级搭：
  // .sym-picker → .sym-dropdown(show, --surface) → .sym-item → .sec-tag
  const pick = document.querySelector('.sym-picker') || document.body;
  const dd = document.createElement('div'); dd.className='sym-dropdown show';
  pick.appendChild(dd);
  const item = document.createElement('div'); item.className='sym-item';
  dd.appendChild(item);
  mk('span','__probe_sectag_dn','sec-tag sec-农产品','农产品',item);
  mk('span','__probe_sectag_up','sec-tag sec-黑色系','黑色系',item);
  mk('span','__probe_sectag_warn','sec-tag sec-建材化工','建材化工',item);
  mk('span','__probe_sectag_acc','sec-tag sec-能源','能源',item);
  mk('span','__probe_sectag_oth','sec-tag sec-其他','其他',item);
  // 同一 chip 放进 .sym-item.active —— 它和 :hover 共用同一条声明块，
  // 用来证明「换成不透明底后，父级高亮不再改变 chip 的可读性」
  const item2 = document.createElement('div'); item2.className='sym-item active';
  dd.appendChild(item2);
  mk('span','__probe_sectag_dn_hl','sec-tag sec-农产品','农产品',item2);
  mk('span','__probe_sectag_up_hl','sec-tag sec-黑色系','黑色系',item2);
  mk('span','__probe_sectag_base_hl','sec-tag','未分类',item2);
  return 1;
})()
"""

TARGETS = [
    ('.extreme-banner',          '极端事件横幅（自身 10% warn 淡染）', True),
    ('#kill-banner .kb-reset',   '熔断·解除按钮（基态）',              True),
    ('#kill-banner .kb-tag',     '熔断·红色标牌',                      True),
    ('#review-status-tag',       '复盘标签·live（真实节点）',          True),
    ('#__probe_record',          '复盘标签·record',                    True),
    ('#__probe_base',            '复盘标签·兜底（bg-3）',              True),
    ('#__probe_th',              '止盈止损表头 th',                    True),
    ('#__probe_halted',          '风控状态·HALTED',                    True),
    ('#__probe_sectag_dn',       'sec-tag·农产品',                     True),
    ('#__probe_sectag_up',       'sec-tag·黑色系',                     True),
    ('#__probe_sectag_warn',     'sec-tag·建材化工',                   True),
    ('#__probe_sectag_acc',      'sec-tag·能源',                       True),
    ('#__probe_sectag_oth',      'sec-tag·其他',                       True),
    ('#__probe_sectag_base_hl',  'sec-tag·兜底（父级高亮态）',          True),
    ('#__probe_sectag_dn_hl',    'sec-tag·农产品（父级高亮态）',        True),
    ('#__probe_sectag_up_hl',    'sec-tag·黑色系（父级高亮态）',        True),
    ('.muted-empty .dot',        '空态圆点（纯装饰，无文字）',         False),
    ('.brand .logo',             '品牌 logo（纯装饰，无文字）',        False),
]


def main():
    import urllib.request
    target = os.path.abspath(sys.argv[1])
    profile = '/tmp/_cdp_audit_%d' % os.getpid()
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
         '--remote-allow-origins=*', '--no-sandbox', '--disable-dev-shm-usage',
         '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check',
         '--disable-gpu', '--hide-scrollbars', '--window-size=1440,900',
         '--allow-file-access-from-files', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(120):
        try:
            d = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=1))
            pg = [t for t in d if t.get('type') == 'page']
            if pg:
                ws_url = pg[0]['webSocketDebuggerUrl']
                break
        except Exception:
            pass
        time.sleep(0.1)
    ws = None
    for _ in range(25):
        try:
            cand = WS(ws_url)
            cand.send({'id': 999, 'method': 'Runtime.evaluate',
                       'params': {'expression': '1', 'returnByValue': True}})
            while True:
                if cand.recv().get('id') == 999:
                    ws = cand
                    break
            if ws:
                break
        except Exception:
            time.sleep(0.25)
    if ws is None:
        proc.kill()
        raise SystemExit('探活失败')

    mid = [0]

    def C(m, p=None):
        mid[0] += 1
        return ws.call(mid[0], m, p or {})

    def js(e):
        r = C('Runtime.evaluate', {'expression': e, 'returnByValue': True, 'awaitPromise': True})
        if r.get('exceptionDetails'):
            raise RuntimeError(json.dumps(r['exceptionDetails'], ensure_ascii=False)[:600])
        return r.get('result', {}).get('value')

    C('Page.enable')
    C('Emulation.setDeviceMetricsOverride',
      {'width': 1440, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
    C('Page.navigate', {'url': 'file://' + target})
    time.sleep(2.5)
    js(PAINT)
    js(INJECT)

    print('=' * 94)
    print('CDP 实测：祖先背景逐层合成后的真实渲染色 × computed color（量消费者，不量定义）')
    print('=' * 94)
    rows = []
    for sel, label, is_text in TARGETS:
        r = js('__probe(%s)' % json.dumps(sel))
        if r.get('missing'):
            print('\n%-42s 未找到' % label)
            continue
        rows.append((label, r, is_text))
        tag = ('PASS' if r['cr'] >= 4.5 else ('*** FAIL' if is_text else 'N/A 无文字'))
        print('\n%s' % label)
        print('   字 %s   压 %s    CR %5.2f   %s' % (r['color'], r['painted'], r['cr'], tag))
        print('   自身 background = %s    animation = %s' % (r['bgc'], r['animation']))
        print('   合成链: ' + '  →  '.join(r['layers']))

    fails = [(l, r) for l, r, t in rows if t and r['cr'] < 4.5]
    print('\n' + '=' * 94)
    print('低于 4.5 的「承载文字」条目: %d' % len(fails))
    for l, r in fails:
        print('   %-40s CR %.2f' % (l, r['cr']))
    deco = [(l, r) for l, r, t in rows if not t]
    if deco:
        print('\n纯装饰（无文字节点，正文 CR 不适用）:')
        for l, r in deco:
            print('   %-40s 背景 %s   animation=%s' % (l, r['painted'], r['animation']))

    ws.sock.close()
    proc.terminate()
    time.sleep(0.3)
    proc.kill()
    subprocess.run(['rm', '-rf', profile])


if __name__ == '__main__':
    main()
