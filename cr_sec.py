#!/usr/bin/env python3
# 单一职责：把「alpha 着色层叠在父底上」算成实际渲染色，再算 WCAG 对比度。
# 两个方向都覆盖：暗字压淡染（review-kpi-tag）、亮字压同色系淡染（sec-tag 变体）。

def hx(s):
    s = s.lstrip('#')
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))

def lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def L(rgb):
    r, g, b = [lin(v) for v in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def over(fg, a, bg):
    """fg 以透明度 a 叠在不透明底色 bg 上，返回合成后的不透明色"""
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3))

def cr(c1, c2):
    l1, l2 = L(c1), L(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)

def hexs(t):
    return '#%02X%02X%02X' % t

# ---- 色板（:root 实值）----
C = {
    '--bg-0': hx('0A0D11'), '--bg-1': hx('0F1318'), '--bg-2': hx('181F27'),
    '--bg-3': hx('222A34'), '--surface': hx('0F1318'), '--card': hx('181F27'),
    '--txt': hx('DEE3EA'), '--txt-1b': hx('C4CBD6'), '--txt-2': hx('98A2B0'),
    '--txt-3': hx('808B99'), '--mut': hx('98A2B0'), '--mut2': hx('808B99'),
    '--accent': hx('4CC2FF'), '--gold': hx('4CC2FF'), '--gold-light': hx('7FD4FF'),
    '--red': hx('FF5C63'), '--green': hx('1FCB86'), '--up': hx('FF5C63'),
    '--dn': hx('1FCB86'), '--amber': hx('E8A33D'), '--blue': hx('4CC2FF'),
    '--violet': hx('A29BFE'), '--st-warn': hx('E8A33D'), '--st-danger': hx('FF3B4E'),
    '--st-danger-ink': hx('0A0D11'), '--rule-2': hx('3A4553'), '--rule-1': hx('2A3340'),
}
R = {
    '--up-rgb': (255, 92, 99), '--dn-rgb': (31, 203, 134), '--warn-rgb': (232, 163, 61),
    '--violet-rgb': (162, 155, 254), '--mut-rgb': (152, 162, 176),
    '--mut2-rgb': (128, 139, 153), '--accent-rgb': (76, 194, 255),
    '--st-danger-rgb': (255, 59, 78),
}

print('=' * 78)
print('A. 门禁值 —— 本次 7 项写入的实测')
print('=' * 78)

# 1. bannerpulse 50% stop：rgba(--st-danger-rgb, α) 叠在 bg-0 上，与 bg-0 的 CR
bg0 = C['--bg-0']
print('\n[1] bannerpulse 50%  (rgba(var(--st-danger-rgb),α) over --bg-0 #0A0D11)')
for a in (1.00, 0.80, 0.75, 0.70, 0.65):
    c = over(R['--st-danger-rgb'], a, bg0)
    v = cr(c, bg0)
    flag = 'PASS' if v >= 3.0 else 'FAIL'
    mark = '  <<< 本次采用' if a == 0.75 else ''
    print('    α=%.2f  合成 %s   CR %.2f  %s%s' % (a, hexs(c), v, flag, mark))
stops = {'0%,100% (--st-danger)': cr(C['--st-danger'], bg0),
         '50% (α=.75)': cr(over(R['--st-danger-rgb'], .75, bg0), bg0)}
print('    两 stop：' + ' / '.join('%s=%.2f' % (k, v) for k, v in stops.items())
      + '  → 最差值 %.2f' % min(stops.values()))

# 2. .kb-reset:hover
print('\n[2] #kill-banner .kb-reset:hover  (--txt-1b 底 / --bg-0 字)')
base = cr(C['--txt'], C['--bg-0'])
hov = cr(C['--txt-1b'], C['--bg-0'])
old = cr(C['--st-danger-ink'], over((255, 255, 255), .18, bg0))
print('    基态  --txt   on --txt        = %5.2f' % base)
print('    旧 hover rgba(255,255,255,.18) over bg-0 = %s  → %.2f   FAIL' % (hexs(over((255,255,255), .18, bg0)), old))
print('    新 hover --txt-1b 底 / --bg-0 字 = %5.2f   %s' % (hov, 'PASS' if hov >= 4.5 else 'FAIL'))

# 3. review-kpi-tag
print('\n[3] .review-kpi-tag（卡面 --card #181F27）')
print('    base      --txt-2 on --bg-3   = %5.2f' % cr(C['--txt-2'], C['--bg-3']))
print('    .live     旧 --green on 自身  = %5.2f  (现 --accent) ' % cr(C['--bg-0'], (31, 203, 134)))
print('    .live     新 --accent 底墨字  = %5.2f   %s' % (cr(C['--bg-0'], C['--accent']), 'PASS' if cr(C['--bg-0'], C['--accent']) >= 4.5 else 'FAIL'))
print('    .record   --blue 底墨字       = %5.2f' % cr(C['--bg-0'], C['--blue']))

# 4. sec-tag 变体：亮字压在 12% 自色淡染上
print('\n' + '=' * 78)
print('B. .sym-picker .sec-tag 六变体（字 / 12% 淡染 / 叠 --surface #0F1318）')
print('=' * 78)
surf = C['--surface']
hover_bg = over(R['--accent-rgb'], .12, surf)   # .sym-item:hover / .active

cases = [
    ('兜底（现 rgbe(mut-rgb,.18) + txt-2）', C['--txt-2'], over(R['--mut-rgb'], .18, surf), None),
    ('兜底（若改 bg-2 + txt-2）',           C['--txt-2'], C['--bg-2'], None),
    ('.sec-黑色系    up on 12% up',         C['--up'],    over(R['--up-rgb'], .12, surf), '--up-rgb'),
    ('.sec-建材化工  warn on 12% warn',     C['--st-warn'], over(R['--warn-rgb'], .12, surf), '--warn-rgb'),
    ('.sec-农产品    dn on 12%(81,207,102)', C['--dn'],   over((81, 207, 102), .12, surf), None),
    ('.sec-有色贵金属 warn on 12% warn',    C['--st-warn'], over(R['--warn-rgb'], .12, surf), '--warn-rgb'),
    ('.sec-能源      accent on 12% accent', C['--accent'], over(R['--accent-rgb'], .12, surf), '--accent-rgb'),
    ('.sec-其他      txt-2 on 12% mut2',    C['--txt-2'],  over(R['--mut2-rgb'], .12, surf), '--mut2-rgb'),
]
print('\n静态底（--surface）:')
for name, txt, bgc, _ in cases:
    v = cr(txt, bgc)
    print('    %-34s 字%s 底%s  CR %5.2f  %s' % (name, hexs(txt), hexs(bgc), v, 'PASS' if v >= 4.5 else '*** FAIL'))

print('\n最差态（.sym-item:hover 底 = 12%% accent over surface = %s）:' % hexs(hover_bg))
for name, txt, bgc, _ in cases:
    if name.startswith('兜底（若改'):
        continue
    # 12% 淡染叠在 hover 底上
    a = 0.12 if 'bg-2' not in name else None
    if name == '兜底（现 rgbe(mut-rgb,.18) + txt-2）':
        real = over(R['--mut-rgb'], .18, hover_bg)
    elif '--bg-2' in name:
        continue
    else:
        # 取原淡染色的 RGB
        rgb = {'--up-rgb': R['--up-rgb'], '--warn-rgb': R['--warn-rgb'], '--accent-rgb': R['--accent-rgb'], '--mut2-rgb': R['--mut2-rgb']}
        m = {'up': R['--up-rgb'], 'warn': R['--warn-rgb'], 'accent': R['--accent-rgb'], 'mut2': R['--mut2-rgb']}
        if '农产品' in name:
            real = over((81, 207, 102), .12, hover_bg)
        elif '黑色系' in name:
            real = over(R['--up-rgb'], .12, hover_bg)
        elif '建材化工' in name or '有色' in name:
            real = over(R['--warn-rgb'], .12, hover_bg)
        elif '能源' in name:
            real = over(R['--accent-rgb'], .12, hover_bg)
        else:
            real = over(R['--mut2-rgb'], .12, hover_bg)
    v = cr(txt, real)
    print('    %-34s 字%s 底%s  CR %5.2f  %s' % (name, hexs(txt), hexs(real), v, 'PASS' if v >= 4.5 else '*** FAIL'))

# 5. 其余参照
print('\n' + '=' * 78)
print('C. 参照项')
print('=' * 78)
amb_bg = over(R['--warn-rgb'], .1, C['--bg-1'])
print('  .extreme-banner 静态  --amber on 10%% warn over bg-1 = %s → %.2f' % (hexs(amb_bg), cr(C['--amber'], amb_bg)))
print('  .tp-badge.current 静态 --gold 底墨字                = %.2f' % cr(C['--bg-0'], C['--gold']))
print('  .ps-ladder th   --txt-2 on --bg-3                    = %.2f' % cr(C['--txt-2'], C['--bg-3']))
print('  .ps-ladder th   --txt-2 on --bg-2                    = %.2f' % cr(C['--txt-2'], C['--bg-2']))
print('  .rs-HALTED      --txt on 20%% danger over bg-0        = %.2f' % cr(C['--txt'], over(R['--st-danger-rgb'], .20, bg0)))
print('  .rs-HALTED      --txt on 6%%  danger over bg-0        = %.2f' % cr(C['--txt'], over(R['--st-danger-rgb'], .06, bg0)))
