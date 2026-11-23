#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDP 截图驱动（无 puppeteer/playwright，自实现最小 WebSocket 客户端）
用途：在真实 Chrome 里触发真实的 :hover（Input.dispatchMouseEvent），
      再读 getComputedStyle 拿到实际渲染色 —— 比在 CSS 里复制规则"假装 hover"可信。
"""
import base64, json, os, socket, struct, subprocess, sys, time

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


class WS:
    def __init__(self, url):
        assert url.startswith('ws://'), url
        hostport, _, path = url[5:].partition('/')
        host, _, port = hostport.partition(':')
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.sock.recv(4096)
        head, _, self.rest = buf.partition(b'\r\n\r\n')
        if b'101' not in head.split(b'\r\n')[0]:
            raise RuntimeError('handshake failed: ' + head.decode('utf-8', 'replace'))

    def _read(self, n):
        while len(self.rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError('socket closed')
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack('>H', n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack('>Q', n)
        mask = os.urandom(4)
        hdr += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + body)

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._read(8))[0]
            payload = self._read(ln)
            if op == 0x1:
                return json.loads(payload.decode())
            if op == 0x8:
                raise RuntimeError('server sent close')

    def call(self, mid, method, params=None):
        self.send({'id': mid, 'method': method, 'params': params or {}})
        while True:
            msg = self.recv()
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('%s -> %s' % (method, msg['error']))
                return msg.get('result', {})


def main():
    import urllib.request
    target = os.path.abspath(sys.argv[1])
    profile = '/tmp/_cdp_profile_%d' % os.getpid()
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--remote-debugging-port=%d' % PORT,
         '--remote-allow-origins=*', '--no-sandbox', '--disable-dev-shm-usage',
         '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check',
         '--disable-gpu', '--hide-scrollbars', '--window-size=1440,900',
         '--allow-file-access-from-files', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(100):
        try:
            data = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=1))
            pages = [t for t in data if t.get('type') == 'page']
            if pages:
                ws_url = pages[0]['webSocketDebuggerUrl']
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ws_url:
        proc.kill()
        raise SystemExit('Chrome 未启动 CDP')

    # Chrome 刚起来时 target 的 DevTools agent 可能还没就绪，连上就会被踢。
    # 用一次轻量调用做探活，失败就重取 target 再连。
    ws = None
    last = None
    for attempt in range(25):
        try:
            cand = WS(ws_url)
            cand.send({'id': 999, 'method': 'Runtime.evaluate',
                       'params': {'expression': '1', 'returnByValue': True}})
            while True:
                r = cand.recv()
                if r.get('id') == 999:
                    ws = cand
                    break
            if ws:
                break
        except Exception as e:
            last = e
        time.sleep(0.25)
        try:
            data = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=1))
            pages = [t for t in data if t.get('type') == 'page']
            if pages:
                ws_url = pages[0]['webSocketDebuggerUrl']
        except Exception:
            pass
    if ws is None:
        proc.kill()
        raise SystemExit('探活失败: %r' % (last,))
    print('CDP 探活成功（第 %d 次尝试）' % (attempt + 1))
    mid = [0]

    def C(method, params=None):
        mid[0] += 1
        return ws.call(mid[0], method, params)

    def js(expr):
        r = C('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                   'awaitPromise': True})
        res = r.get('result', {})
        if r.get('exceptionDetails'):
            raise RuntimeError(json.dumps(r['exceptionDetails'], ensure_ascii=False)[:800])
        return res.get('value')

    def shot(path, clip=None):
        p = {'format': 'png', 'captureBeyondViewport': True}
        if clip:
            p['clip'] = dict(clip, scale=1)
        data = C('Page.captureScreenshot', p)['data']
        open(path, 'wb').write(base64.b64decode(data))
        print('  写入 %s  (%d 字节)' % (path, os.path.getsize(path)))

    C('Page.enable')
    C('Runtime.enable')
    C('Emulation.setDeviceMetricsOverride',
      {'width': 1440, 'height': 900, 'deviceScaleFactor': 2, 'mobile': False})
    C('Page.navigate', {'url': 'file://' + target})
    time.sleep(2.5)
    print('页面标题:', js('document.title'))
    globals().update(dict(C=C, js=js, shot=shot, proc=proc, ws=ws))

    # ---------- A. 熔断横幅 + 真实 hover ----------
    print('\n[A] #kill-banner · 真实 hover')
    js("""
    (async()=>{
      // 桩掉网络层，喂一份真实的 halted 载荷，让 refreshKill() 走正常渲染路径
      window._smartFetch = async () => ({json: async () => ({
        halted: true,
        halted_min: 47,
        reason: '组合级回撤触发：账户回撤 8.4% 且日亏 3.1%，已冻结全部新开仓',
        ack: false,
        metrics: {drawdown: 0.084, daily_loss_pct: 0.031, consec_losses: 4},
        flatten_plan: [
          {name:'沪铜', symbol:'cu0', action:'平多', lots:2},
          {name:'螺纹钢', symbol:'rb0', action:'平空', lots:1},
          {name:'豆粕', symbol:'m0', action:'平多', lots:3}
        ]
      })});
      await refreshKill();
      return 1;
    })()
    """)
    time.sleep(0.6)
    box = js("""
    (()=>{const b=document.querySelector('#kill-banner .kb-reset');
      if(!b) return null;
      const r=b.getBoundingClientRect();
      return {x:r.x+r.width/2, y:r.y+r.height/2, w:r.width, h:r.height,
              bannerH: document.getElementById('kill-banner').offsetHeight};})()
    """)
    print('  按钮盒:', box)
    if not box:
        raise SystemExit('找不到 .kb-reset')

    before = js("""
    (()=>{const b=document.querySelector('#kill-banner .kb-reset');
      const s=getComputedStyle(b);
      return {bg:s.backgroundColor,color:s.color};})()
    """)
    print('  hover 前:', before)

    # 真实鼠标事件
    C('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': box['x'], 'y': box['y'],
                                   'button': 'none', 'buttons': 0})
    time.sleep(0.45)

    after = js("""
    (()=>{const b=document.querySelector('#kill-banner .kb-reset');
      const s=getComputedStyle(b);
      return {bg:s.backgroundColor,color:s.color,
              hovered: b.matches(':hover')};})()
    """)
    print('  hover 后:', after)

    def pcr(rgb, c2):
        import re
        def parse(t):
            n = [int(x) for x in re.findall(r'[\d.]+', t)[:3]]
            return tuple(n)
        def lin(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        def L(t):
            r, g, b = parse(t)
            return 0.2126*lin(r)+0.7152*lin(g)+0.0722*lin(b)
        l1, l2 = L(rgb), L(c2)
        hi, lo = max(l1, l2), min(l1, l2)
        return (hi+0.05)/(lo+0.05)

    if after:
        print('  实测 hover CR = %.2f  (bg %s / 字 %s)'
              % (pcr(after['bg'], after['color']), after['bg'], after['color']))
        print('  matches(":hover") =', after['hovered'])

    clip = {'x': 0, 'y': 0, 'width': 1440, 'height': min(900, (box['bannerH'] or 130) + 40)}
    shot('shot_killbanner.png', clip)

    # hover 后再截一张局部（按钮特写）
    shot('shot_killbanner_btn.png',
         {'x': box['x'] - 210, 'y': box['y'] - 26, 'width': 420, 'height': 52})

    # ---------- B. 极端情绪横幅 ----------
    print('\n[B] .extreme-banner · 强制显示')
    js("""
    (()=>{const b=document.getElementById('kill-banner'); if(b) b.style.display='none';
      document.body.style.paddingTop='';
      const er=document.getElementById('sent-extreme-banner');
      er.textContent='⚠ 极端事件 · 全市场跌停家数 412 家，北向净流出 87 亿，情绪指数跌破 8 —— 建议空仓等待';
      // 逐级揭示：祖先里任何一个 display:none 都会让 rect 归零，截出来是空白
      let p=er;
      while(p && p!==document.body){
        p.style.display='block';
        p.style.visibility='visible';
        p.style.opacity='1';
        p=p.parentElement;
      }
      er.style.display='block';
      // 揭示后仍被困在窄栏（w=26）里，故临时脱离文档流按真实阅读宽度呈现；
      // 只动 position/width，不碰任何颜色与字体，CR 不受影响。
      er.style.position='fixed';
      er.style.left='24px';
      er.style.top='96px';
      er.style.width='960px';
      er.style.zIndex='99999';
      return 1;})()
    """)
    time.sleep(0.6)
    ex = js("""
    (()=>{const e=document.getElementById('sent-extreme-banner');
      e.scrollIntoView({block:'center'});
      const s=getComputedStyle(e);
      const r=e.getBoundingClientRect();
      // 向上找第一个非透明祖先，作为"--amber 压在什么底上"的实际依据
      let par=e.parentElement, pbg=null;
      while(par){const ps=getComputedStyle(par);
        if(ps.backgroundColor && ps.backgroundColor!=='rgba(0, 0, 0, 0)'){
          pbg=ps.backgroundColor; break;}
        par=par.parentElement;}
      return {text:e.textContent.slice(0,40), color:s.color, bg:s.backgroundColor,
              animationName:s.animationName, animationDuration:s.animationDuration,
              rect:{x:r.x,y:r.y,w:r.width,h:r.height}, parentBg:pbg};})()
    """)
    print('  计算样式:', json.dumps(ex, ensure_ascii=False))
    if ex:
        print('  animationName = %r  (必须是 none)' % ex['animationName'])
        print('  自身 background = %s （alpha 通道，不能直接拿前三元算 CR）' % ex['bg'])
        print('  第一个不透明祖先底 = %s' % ex['parentBg'])

    def composite_cr(rgba_txt, rgba_bg, base_txt):
        import re
        def nums(t):
            return [float(x) for x in re.findall(r'[\d.]+', t)]
        def lin(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        def lum(rgb):
            return 0.2126*lin(rgb[0]) + 0.7152*lin(rgb[1]) + 0.0722*lin(rgb[2])
        tb = nums(base_txt)[:3]
        bb = nums(rgba_bg)
        fg, a = bb[:3], (bb[3] if len(bb) > 3 else 1.0)
        comp = [fg[i]*a + tb[i]*(1-a) for i in range(3)]
        tc = nums(rgba_txt)[:3]
        l1, l2 = lum(tc), lum(comp)
        hi, lo = max(l1, l2), min(l1, l2)
        return (hi+0.05)/(lo+0.05), comp

    if ex and ex['rect']['w'] > 0:
        v, comp = composite_cr(ex['color'], ex['bg'], ex['parentBg'])
        print('  合成底 = rgb(%d,%d,%d)  → 实测静态 CR = %.2f' % (comp[0], comp[1], comp[2], v))
        r = ex['rect']
        shot('shot_extreme.png',
             {'x': max(0, r['x'] - 8), 'y': max(0, r['y'] - 44),
              'width': min(1440, r['w'] + 16), 'height': r['h'] + 96})
    else:
        print('  !! rect 仍为 0，祖先未揭示成功')

    ws.sock.close()
    proc.terminate()
    time.sleep(0.4)
    proc.kill()
    subprocess.run(['rm', '-rf', profile])


if __name__ == '__main__':
    main()
