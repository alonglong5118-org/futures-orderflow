#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""硬过滤三态副文案截图：把三条 active 并排渲染在 --card 卡面上，复现真实分母。"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ns = {}
exec(open(os.path.join(HERE, 'cdp_shots.py'), encoding='utf-8').read().split("def main()")[0], ns)
WS = ns['WS']
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9411

BUILD = r"""
(() => {
  const old = document.getElementById('__hf_shot');
  if (old) old.remove();
  const wrap = document.createElement('div');
  wrap.id = '__hf_shot';
  // 真实分母：.hard-filter-bar 嵌在 .decision-card{background:var(--card)} 里
  wrap.style.cssText = 'position:fixed;left:24px;top:70px;width:1000px;z-index:99999;'+
    'background:var(--card);padding:16px;border:1px solid var(--rule-1);border-radius:2px';
  const cases = [
    ['active-ban',   '🚫', '硬过滤 · 禁止开多', '情绪贪婪过热（92），多头拥挤度 3.4σ，只允许开空'],
    ['active-short', '🚫', '硬过滤 · 禁止开空', '情绪极度恐惧（8），空头拥挤度 3.1σ，只允许开多'],
    ['active-both',  '🔒', '硬过滤 · 双向锁定', '极端事件叠加流动性枯竭，暂停全部新开仓'],
  ];
  cases.forEach(([cls, icon, title, desc]) => {
    const bar = document.createElement('div');
    bar.className = 'hard-filter-bar ' + cls;
    bar.innerHTML = '<div class="hf-icon">' + icon + '</div>' +
      '<div class="hf-text"><b>' + title + '</b><span>' + desc + '</span></div>';
    wrap.appendChild(bar);
  });
  document.body.appendChild(wrap);
  return 1;
})()
"""

PROBE = r"""
(() => {
  const out = [];
  document.querySelectorAll('#__hf_shot .hard-filter-bar').forEach(bar => {
    const sp = bar.querySelector('.hf-text span');
    const s = getComputedStyle(sp);
    // 从元素向上逐层合成（inclusive），拿到真实渲染色
    const chain = []; let p = sp;
    while (p) { chain.unshift(p); p = p.parentElement; }
    let acc = {r:255,g:255,b:255,a:1};
    const layers = [];
    for (const n of chain) {
      const bg = getComputedStyle(n).backgroundColor.match(/[\d.]+/g);
      if (!bg) continue;
      const o = {r:+bg[0], g:+bg[1], b:+bg[2], a: bg.length>3 ? +bg[3] : 1};
      if (o.a === 0) continue;
      acc = {r:o.r*o.a+acc.r*(1-o.a), g:o.g*o.a+acc.g*(1-o.a), b:o.b*o.a+acc.b*(1-o.a), a:1};
      layers.push(getComputedStyle(n).backgroundColor + '(' +
        (n.id ? '#'+n.id : n.tagName.toLowerCase()+'.'+String(n.className).trim().split(/\s+/).join('.')) + ')');
    }
    const lin = c => { c/=255; return c<=0.04045 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); };
    const lum = t => 0.2126*lin(t[0]) + 0.7152*lin(t[1]) + 0.0722*lin(t[2]);
    const tc = s.color.match(/[\d.]+/g).map(Number);
    const l1 = lum(tc), l2 = lum([acc.r,acc.g,acc.b]);
    const cr = (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
    out.push({cls: bar.className, color: s.color,
      painted: 'rgb('+Math.round(acc.r)+','+Math.round(acc.g)+','+Math.round(acc.b)+')',
      cr: +cr.toFixed(2), layers});
  });
  return out;
})()
"""


def main():
    import urllib.request
    target = os.path.abspath(sys.argv[1])
    profile = '/tmp/_hf%d' % os.getpid()
    p = subprocess.Popen([CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
                          '--remote-allow-origins=*', '--no-sandbox', '--disable-dev-shm-usage',
                          '--user-data-dir=' + profile, '--no-first-run', '--disable-gpu',
                          '--hide-scrollbars', 'about:blank'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = None
    for _ in range(120):
        try:
            d = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=1))
            pg = [t for t in d if t.get('type') == 'page']
            if pg:
                url = pg[0]['webSocketDebuggerUrl']
                break
        except Exception:
            pass
        time.sleep(0.1)
    ws = None
    for _ in range(25):
        try:
            c = WS(url)
            c.send({'id': 999, 'method': 'Runtime.evaluate', 'params': {'expression': '1'}})
            while True:
                if c.recv().get('id') == 999:
                    ws = c
                    break
            if ws:
                break
        except Exception:
            time.sleep(0.25)
    mid = [0]

    def C(m, pp=None):
        mid[0] += 1
        return ws.call(mid[0], m, pp or {})

    def js(e):
        r = C('Runtime.evaluate', {'expression': e, 'returnByValue': True})
        if r.get('exceptionDetails'):
            raise RuntimeError(json.dumps(r['exceptionDetails'], ensure_ascii=False)[:600])
        return r.get('result', {}).get('value')

    C('Page.enable')
    C('Emulation.setDeviceMetricsOverride',
      {'width': 1100, 'height': 520, 'deviceScaleFactor': 2, 'mobile': False})
    C('Page.navigate', {'url': 'file://' + target})
    time.sleep(2.5)
    js(BUILD)
    time.sleep(0.5)
    rows = js(PROBE)
    for r in rows:
        print('  %-42s 字 %s 压 %s  CR %5.2f  %s'
              % (r['cls'], r['color'], r['painted'], r['cr'], 'PASS' if r['cr'] >= 4.5 else 'FAIL'))
        print('      链: ' + '  →  '.join(r['layers']))
    import base64
    data = C('Page.captureScreenshot',
             {'format': 'png', 'captureBeyondViewport': True,
              'clip': {'x': 16, 'y': 60, 'width': 1060, 'height': 300, 'scale': 1}})['data']
    open('shot_hf_active.png', 'wb').write(base64.b64decode(data))
    print('  写入 shot_hf_active.png (%d 字节)' % os.path.getsize('shot_hf_active.png'))
    ws.sock.close()
    p.terminate()
    time.sleep(0.3)
    p.kill()
    subprocess.run(['rm', '-rf', profile])


if __name__ == '__main__':
    main()
