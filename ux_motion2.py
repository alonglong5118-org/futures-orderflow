#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
两段式动画门禁（2026-11-22 定稿）

第一段 A · 允许碰哪些属性（按「宿主是否承载文字」判，而不是按 keyframe 定义判）
    带文字的宿主，其循环 keyframe 定义体中不得出现 opacity / background / color
    —— 即使当下算得出达标值，动画也会把判定值变成「全程最差值」，而门禁量的是定义值。
    例外必须逐条登记（ALLOWED），不得类推。

第二段 B · 装饰动画清点
    只在「纯装饰宿主」（元素内无文本节点）上跑的循环 keyframe 个数。
    这些可以动 opacity，因为没有字会被稀释。

判据：
    A == 0
    B == 2
"""
import io, re, sys

CSS = None
HTML = None
path = sys.argv[1] if len(sys.argv) > 1 else 'four_dim_live.html'
src = io.open(path, encoding='utf-8').read()

# 注释用等长空格替换：既不让 `/* ... */` 混进选择器，又保住行号偏移
def blank_comments(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s.startswith('/*', i):
            j = s.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(''.join('\n' if ch == '\n' else ' ' for ch in s[i:j]))
            i = j
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)

src = blank_comments(src)

# ---------- 1. 收集 @keyframes 定义体 ----------
kf = {}
for m in re.finditer(r'@keyframes\s+([A-Za-z0-9_-]+)\s*\{', src):
    name = m.group(1)
    i = m.end() - 1
    depth = 0
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[m.end():i]
    props = sorted(set(re.findall(r'([a-z-]+)\s*:', body)))
    kf[name] = props

# ---------- 2. 收集 animation:<name> 的宿主选择器 ----------
hosts = []          # (selector, name, line)
for m in re.finditer(r'animation\s*:\s*([A-Za-z0-9_-]+)', src):
    name = m.group(1)
    if name not in kf:
        continue
    line = src.count('\n', 0, m.start()) + 1
    # 往回找最近的 '{'，再从那个 '{' 往前取选择器
    j = src.rfind('{', 0, m.start())
    if j < 0:
        continue
    k = j - 1
    while k >= 0 and src[k] in ' \t\r\n':
        k -= 1
    e = k + 1
    while k >= 0 and src[k] not in '{}':
        k -= 1
    sel = ' '.join(src[k + 1:e].split())
    infinite = 'infinite' in src[m.start():src.find(';', m.start()) + 1] or True
    hosts.append((sel, name, line))

# 只保留循环动画（infinite）；一次性入场动画不在本门禁范围
loop_hosts = []
for sel, name, line in hosts:
    seg_start = src.rfind('{', 0, src.find(name, 0))
    semi = src.find(';', src.find('animation:' + name))
    decl = src[src.find('animation:' + name):semi if semi > 0 else semi]
    loop_hosts.append((sel, name, line, 'infinite' in decl))

# ---------- 3. 宿主是否承载文字（人工核查表，逐条给依据） ----------
# True = 元素内含文本节点；False = 纯装饰（无字圆点/logo）
TEXT_BEARING = {
    '.brand .logo':               (False, 'SVG/图标 logo，无文本节点'),
    '.muted-empty .dot':          (False, '6px 圆点，无文本节点'),
    '.tp-dot.current':            (False, '10px 圆点，无文本节点'),
    '.extreme-banner':            (True,  '内为提示文案'),
    '.hard-filter-bar.active-ban':   (True, '内 .hf-text 有「做多」等文案'),
    '.hard-filter-bar.active-short': (True, '内 .hf-text 有「做空」等文案'),
    '.hard-filter-bar.active-both':  (True, '内 .hf-text 有「双向」等文案'),
    '.tp-badge.current':          (True,  '内为档位数字/字母'),
    '.rs-HALTED':                 (True,  '内为「熔断」状态字'),
    '#kill-banner':               (True,  '内为组合级熔断文案'),
    '#stale-banner':              (True,  '内为行情源陈旧文案'),
}

# 登记例外：带文字宿主 × 碰 background-color/opacity/color，但两 stop 均达标且方向单一
ALLOWED = {
    '.rs-HALTED': ('haltpulse',
                   '动 background-color，两 stop --txt 实测 12.35 / 14.46，'
                   '均远高于 4.5 且抖动方向单一（只会更暗不会更亮），按最差值入册'),
}

BAD_PROPS = re.compile(r'^(opacity|background|background-color|color)$')

print('=' * 80)
print('循环动画宿主清点（只有 infinite 才计入）')
print('=' * 80)
print('%-34s %-14s %-6s %s' % ('宿主选择器', 'keyframe', '含字', 'keyframe 动到的属性'))
print('-' * 80)
a_viol = []
b_deco = []
for sel, name, line, is_loop in sorted(set(loop_hosts)):
    if not is_loop:
        continue
    props = kf.get(name, [])
    known, why = TEXT_BEARING.get(sel, (None, '未登记'))
    touches_bad = [p for p in props if BAD_PROPS.match(p)]
    mark = ''
    if known is True:
        if touches_bad:
            if sel in ALLOWED and ALLOWED[sel][0] == name:
                mark = '  [登记例外]'
            else:
                mark = '  <<< A 违规'
                a_viol.append((sel, name, touches_bad))
    elif known is False:
        b_deco.append((sel, name))
    else:
        mark = '  <<< 未登记宿主，无法判定'
        a_viol.append((sel, name, ['未登记']))
    print('%-34s %-14s %-6s %s%s' % (sel[:34], name,
                                     {True: '是', False: '否'}.get(known, '?'),
                                     '/'.join(props) or '(空)', mark))

print()
print('-' * 80)
print('A · 带文字宿主 × 循环 keyframe 碰 opacity/background/color（扣登记例外）')
if a_viol:
    for sel, name, ps in a_viol:
        print('    FAIL  %s  →  %s  (%s)' % (sel, name, ','.join(ps)))
else:
    print('    0 条 —— PASS')
for sel, (name, why) in ALLOWED.items():
    print('    例外登记：%s ← %s\n              %s' % (sel, name, why))

print()
dec_names = sorted(set(n for _, n in b_deco))
print('B · 纯装饰宿主的循环 keyframe 个数')
for n in dec_names:
    hs = sorted(set(s for s, nn in b_deco if nn == n))
    print('    %-12s 属性:%-10s 宿主: %s' % (n, '/'.join(kf.get(n, [])), ', '.join(hs)))
print('    合计 = %d   %s' % (len(dec_names), 'PASS' if len(dec_names) == 2 else 'FAIL（应为 2）'))

print()
print('=' * 80)
A = len(a_viol)
B = len(dec_names)
print('门禁结果：  动画纪律-A = %d  (要求 0)    %s' % (A, 'PASS' if A == 0 else 'FAIL'))
print('            动画纪律-B = %d  (要求 2)    %s' % (B, 'PASS' if B == 2 else 'FAIL'))
print('=' * 80)
sys.exit(0 if (A == 0 and B == 2) else 1)
