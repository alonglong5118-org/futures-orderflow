#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只测量，不改文件。

为什么要把 #sent-hard-filter 的 id 临时摘掉：
页面 refreshSentiment() 会周期性执行 `hf.className='hard-filter-bar'` 再按数据 add 激活类。
测量窗口一旦落在轮询之间，读到的就是被重置后的常态样式（padding 14 / border-left 1），
表现为"某几态量不准、每次跑结果还不一样"。JS 单线程，但这个轮询会在两次 CDP 调用之间跑。
摘掉 id 后 getElementById 返回 null，轮询跳过它 —— 测量窗口内元素不再被外部改写。

量四件事：
  1. getBoundingClientRect().width      判据：四态全等
  2. padding-left / border-left-width / box-sizing 计算值（交叉验证 1+14 = 4+11 = 15）
  3. .hf-text b 与 .hf-text span 的 rect.x   判据：四态全等（左文字起点不位移）
  4. 四张截图
"""
import base64, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ns = {}
exec(open(os.path.join(HERE, 'cdp_shots.py'), encoding='utf-8').read().split("def main()")[0], ns)
WS = ns['WS']
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9447

PREP = r"""
(() => {
  const bar = document.getElementById('sent-hard-filter');
  if (!bar) return {err: 'no #sent-hard-filter'};
  let p = bar;
  while (p && p !== document.body) {
    if (getComputedStyle(p).display === 'none') p.style.display = 'block';
    p = p.parentElement;
  }
  bar.querySelector('.hf-text b').textContent = '硬过滤 · 双向锁定';
  bar.querySelector('.hf-text span').textContent = '极端事件叠加流动性枯竭，暂停全部新开仓';
  bar.scrollIntoView({block: 'center'});
  // 摘 id：让轮询的 getElementById 找不到它，测量窗口内类名不再被外部改写
  bar.id = '';
  // 关掉 transition：.hard-filter-bar 自带 transition:.3s，
  // padding 与 border-width 会在 300ms 内插值 —— 不关掉读到的是动画途中值
  // （实测会拿到 13.6632px / 12.5945px 这种分数，且每次跑都不一样）。
  bar.style.transition = 'none';
  window.__bar = bar;
  return {ok: 1, rect: (r => ({x: r.x, y: r.y, w: r.width, h: r.height}))(bar.getBoundingClientRect())};
})()
"""

MEASURE = r"""
(cls) => {
  const bar = window.__bar;
  bar.className = 'hard-filter-bar' + (cls ? ' ' + cls : '');
  void bar.offsetWidth;                       // 强制重排
  const r  = bar.getBoundingClientRect();
  const b  = bar.querySelector('.hf-text b').getBoundingClientRect();
  const sp = bar.querySelector('.hf-text span').getBoundingClientRect();
  const cs = getComputedStyle(bar);
  return {
    cls: cls || '(常态)',
    nowClass: bar.className,
    width: +r.width.toFixed(3),
    height: +r.height.toFixed(3),
    x: +r.x.toFixed(3),
    padL: cs.paddingLeft,
    blW: cs.borderLeftWidth,
    blC: cs.borderLeftColor,
    boxSizing: cs.boxSizing,
    titleX: +b.x.toFixed(3),
    titleW: +b.width.toFixed(3),
    spanX: +sp.x.toFixed(3),
    sum: parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth)
  };
}
"""


def main():
    import urllib.request
    target = os.path.abspath(sys.argv[1])
    profile = '/tmp/_mw2%d' % os.getpid()
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
        r = C('Runtime.evaluate', {'expression': e, 'returnByValue': True, 'awaitPromise': True})
        if r.get('exceptionDetails'):
            raise RuntimeError(json.dumps(r['exceptionDetails'], ensure_ascii=False)[:800])
        return r.get('result', {}).get('value')

    C('Page.enable')
    C('Emulation.setDeviceMetricsOverride',
      {'width': 1440, 'height': 900, 'deviceScaleFactor': 2, 'mobile': False})
    C('Page.navigate', {'url': 'file://' + target})
    time.sleep(2.6)
    prep = js(PREP)
    print('准备:', prep)
    time.sleep(0.4)
    js("window.__m = %s" % MEASURE)

    states = ['', 'active-ban', 'active-short', 'active-both']
    rows = []
    for st in states:
        r = js('__m(%s)' % json.dumps(st))
        rows.append(r)
        # 截图：class 已被固定（id 已摘，轮询碰不到），单独一次 CDP 调用拿图
        # clip 用的是「页面坐标」，不是视口坐标 —— 必须补 scrollX/scrollY，
        # 否则四次都会截到同一块（实测四张图 md5 完全相同，就是这么来的）。
        rect = js("""(()=>{const el=window.__bar;
                       const r=el.getBoundingClientRect();
                       return {x:r.x+window.scrollX, y:r.y+window.scrollY,
                               w:r.width, h:r.height};})()""")
        clip = {'x': max(0, rect['x'] - 12), 'y': max(0, rect['y'] - 14),
                'width': min(1440, rect['w'] + 24), 'height': rect['h'] + 28, 'scale': 1}
        data = C('Page.captureScreenshot',
                 {'format': 'png', 'captureBeyondViewport': True, 'clip': clip})['data']
        fn = 'shot_hf_state_%s.png' % (st or 'normal')
        open(fn, 'wb').write(base64.b64decode(data))
        print('  写入 %-30s %6d B' % (fn, os.path.getsize(fn)))

    # 收尾：还原 id，避免影响后续人工查看
    js("""(()=>{const b=window.__bar; if(b){b.id='sent-hard-filter'; b.className='hard-filter-bar';} return 1;})()""")

    print()
    hdr = ('状态', 'width', 'height', 'padding-L', 'bl-width', 'box-sizing', '标题起点x', '副文案x', 'padL+blW')
    print('%-13s %-9s %-8s %-10s %-9s %-11s %-10s %-9s %s' % hdr)
    print('-' * 100)
    for r in rows:
        print('%-13s %-9s %-8s %-10s %-9s %-11s %-10s %-9s %s'
              % (r['cls'], r['width'], r['height'], r['padL'], r['blW'],
                 r['boxSizing'], r['titleX'], r['spanX'], r['sum']))

    ws_v = [r['width'] for r in rows]
    tx = [r['titleX'] for r in rows]
    sx = [r['spanX'] for r in rows]
    print()
    print('width      四态 %s  全等=%s  极差=%.3f' % (ws_v, len(set(ws_v)) == 1, max(ws_v) - min(ws_v)))
    print('标题起点x  四态 %s  全等=%s  极差=%.3f' % (tx, len(set(tx)) == 1, max(tx) - min(tx)))
    print('副文案x    四态 %s  全等=%s  极差=%.3f' % (sx, len(set(sx)) == 1, max(sx) - min(sx)))
    print('padL+blW   四态 %s  全等=%s' % ([r['sum'] for r in rows],
                                          len(set(r['sum'] for r in rows)) == 1))

    ws.sock.close()
    p.terminate()
    time.sleep(0.3)
    p.kill()
    subprocess.run(['rm', '-rf', profile])


if __name__ == '__main__':
    main()
