#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ux_static_text.py —— 静态文本对比度「人工台账」

本表覆盖 N 条人工登记的（选择器, 字色, 分母）三元组。未登记的不在本表内，
也不代表通过。不做自动发现 —— 本文件是单体 HTML + 动态 innerHTML，
DOM 父子关系无法从 CSS 静态推出，任何「自动」都会退化为选择器字符串猜谜。

只登记「推不出来的那一项」：
  - 字色、色层 token、alpha 一律从 CSS 实际声明读取，不抄进表。
    抄 = 冷冻当前值；文件一改，表就变成第二个假源。
  - 唯一手写的是分母 BASE（.decision-card 的 --card 这类推不出的层），
    并且逐行打印出来，供人复核。

父链纪律（本条是踩出来的）：
  「父面」不等于「最近的祖先」。父元素自己的背景也是一层，必须逐层合成。
  `.hf-text span` 的底不是 --card(24,31,39)，而是
  `.hard-filter-bar` 自己的 rgba(--mut-rgb,.06) 叠在 --card 上 = (32,39,47)。
  漏掉这一层，常态 span 会从 4.36 被算成 4.80 —— 一个 FAIL 变成一个假 PASS。

token 陷阱（本轮踩过）：
  `--mut` 与 `--txt-2` 同值、`--mut2` 与 `--txt-3` 同值 —— 语义名和亮度是反的。
  名为 "mut" 的那个其实是四档文字里偏亮的一档。所以 `rgba(var(--mut-rgb),.06)`
  叠在深色卡面上得到的是「提亮底」，不是「压暗底」；一眼按名字假定它是暗的，方向会算反。

期望值 = 已定的决策，不是观测结果。偏离即报 EXPECT 并计入 FAIL，
因为「0 FAIL」在判据被放宽时也会出现。
    hf span active-ban / short / both = 5.86 / 5.44 / 5.56
    hf span 常态                      = 5.84（L174 = var(--txt-2)，B 决议）
    若有人把 L174 改回 var(--mut2)，常态会变成 4.36 —— 本表报 EXPECT，不静默接受。

用法：python3 ux_static_text.py [file]
"""

import hashlib
import re
import sys

MIN = 4.5          # AA 正文阈值
BAND = MIN * 1.15  # 余量带下限（只约束本轮新选值，不倒查存量）

# 分母：由人登记。这是唯一「CSS 里推不出来」的东西。
# .decision-card{background:var(--card)} 不透明，断掉了到 body 的链，
# 所以 #sent-card / #sent-hard-filter 内元素的底最终落在 --card。
CARD = '#181F27'
PAGE = '#0A0D11'

# (名称, 读字色的选择器, 读不到时的回退, 读色层背景的选择器, 分母)
# 色层 token 与 alpha 都从「读色层背景的选择器」里读，不抄。
CASES = [
    ('hf span 常态',         '.hard-filter-bar .hf-text span',
                             '.hard-filter-bar .hf-text span',
                             '.hard-filter-bar',                 CARD),
    ('hf span active-ban',   '.hard-filter-bar.active-ban .hf-text span',
                             '.hard-filter-bar .hf-text span',
                             '.hard-filter-bar.active-ban',      CARD),
    ('hf span active-short', '.hard-filter-bar.active-short .hf-text span',
                             '.hard-filter-bar .hf-text span',
                             '.hard-filter-bar.active-short',    CARD),
    ('hf span active-both',  '.hard-filter-bar.active-both .hf-text span',
                             '.hard-filter-bar .hf-text span',
                             '.hard-filter-bar.active-both',     CARD),
    ('hf b 常态',            '.hard-filter-bar .hf-text b',
                             '.hard-filter-bar .hf-text b',
                             '.hard-filter-bar',                 CARD),
    ('hf b active-ban',      '.hard-filter-bar.active-ban .hf-text b',
                             '.hard-filter-bar .hf-text b',
                             '.hard-filter-bar.active-ban',      CARD),
    ('hf b active-short',    '.hard-filter-bar.active-short .hf-text b',
                             '.hard-filter-bar .hf-text b',
                             '.hard-filter-bar.active-short',    CARD),
    ('hf b active-both',     '.hard-filter-bar.active-both .hf-text b',
                             '.hard-filter-bar .hf-text b',
                             '.hard-filter-bar.active-both',     CARD),
    ('extreme-banner',       '.extreme-banner',
                             '.extreme-banner',
                             '.extreme-banner',                  CARD),
    ('kill-banner 正文',     '#kill-banner',
                             '#kill-banner',
                             '#kill-banner',                     PAGE),
    ('kb-tag',               '#kill-banner .kb-tag',
                             '#kill-banner .kb-tag',
                             '#kill-banner .kb-tag',             PAGE),
    ('kb-reset 常态',        '#kill-banner .kb-reset',
                             '#kill-banner .kb-reset',
                             '#kill-banner .kb-reset',           PAGE),
    ('kb-reset:hover',       '#kill-banner .kb-reset:hover',
                             '#kill-banner .kb-reset',
                             '#kill-banner .kb-reset:hover',     PAGE),
]

EXPECT = {
    'hf span 常态':          5.84,
    'hf span active-ban':    5.86,
    'hf span active-short':  5.44,
    'hf span active-both':   5.56,
    'extreme-banner':        6.47,
    'kill-banner 正文':     15.10,
    'kb-tag':                5.55,
    'kb-reset:hover':       11.92,
}
TOL = 0.02


# ---------------- token：两种物理形态 ----------------
# 《x: #RRGGBB》与《x-rgb: R,G,B》是同一族东西的两种形态。
# 只认一种，脚本会在「第一行需要计算的地方」崩，或静默返回 None。

def load_tokens(src):
    toks = {}
    for m in re.finditer(r'(--[a-z0-9-]+)\s*:\s*([^;}]+)', src):
        toks.setdefault(m.group(1), m.group(2).strip())
    return toks


TOKS = {}


def parse_color(v, d=0):
    s = (v or '').strip()
    m = re.fullmatch(r'var\((--[a-z0-9-]+)\)', s)
    if m and d < 10:
        k = m.group(1)
        if k not in TOKS:
            raise KeyError(k)
        return parse_color(TOKS[k], d + 1)
    if s.startswith('#'):
        h = s[1:]
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    if ',' in s:
        p = [int(x) for x in re.findall(r'\d+', s)]
        if len(p) >= 3:
            return tuple(p[:3])
    raise ValueError(v)


def over(fg, a, bg):
    return tuple(round(x * a + y * (1 - a)) for x, y in zip(fg, bg))


def lum(c):
    def f(x):
        x /= 255.0
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = map(f, c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def cr(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------- CSS 规则表 ----------------

def split_decls(body):
    out, depth, cur = {}, 0, ''
    for ch in body:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ';' and depth == 0:
            if ':' in cur:
                k, v = cur.split(':', 1)
                out[k.strip().lower()] = v.strip()
            cur = ''
        else:
            cur += ch
    if ':' in cur:
        k, v = cur.split(':', 1)
        out[k.strip().lower()] = v.strip()
    return out


def load_rules(src):
    css = '\n'.join(m.group(1) for m in re.finditer(r'<style[^>]*>(.*?)</style>', src, re.S))
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    rules = {}
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel, body = m.group(1).strip(), m.group(2)
        if not sel or sel.startswith('@'):
            continue
        d = split_decls(body)
        for part in sel.split(','):
            part = part.strip()
            if part:
                rules.setdefault(part, {}).update(d)
    return rules


def bg_layer(sel, rules):
    """返回 (色层值, alpha)；无背景返回 (None, 0.0)。不抄：从 CSS 读。"""
    d = rules.get(sel) or {}
    v = d.get('background') or d.get('background-color')
    if not v:
        return None, 0.0
    v = v.strip()
    if v.startswith(('linear-gradient', 'radial-gradient', 'none', 'url', 'transparent')):
        return None, 0.0
    m = re.fullmatch(r'rgba?\(\s*(var\(--[a-z0-9-]+\))\s*,\s*([\d.]+)\s*\)', v)
    if m:
        return m.group(1), float(m.group(2))
    return v, 1.0


def chain_backgrounds(sel, rules):
    """宿主链上所有非 transparent 的自身 background（由近到远）。
    用于结构断言：链上有底，则该行的 α 不得为 0。"""
    skip = ('transparent', 'none', 'linear-gradient', 'radial-gradient', 'url', 'inherit', 'initial')
    out = []
    cs = sel.split()
    for i in range(len(cs), 0, -1):
        cand = ' '.join(cs[:i])
        d = rules.get(cand) or {}
        v = d.get('background') or d.get('background-color')
        if v and not v.strip().startswith(skip):
            out.append((cand, v.strip()))
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'four_dim_live.html'
    raw = open(path, 'rb').read()
    src = raw.decode('utf-8')
    global TOKS
    TOKS = load_tokens(src)
    rules = load_rules(src)

    print('锚点  %s' % hashlib.md5(raw).hexdigest())
    print('文件  %s' % path)
    print('口径  AA 正文 %.2f ｜ 余量带 %.2f×1.15 = %.3f（只约束本轮新选值，不倒查存量）'
          % (MIN, MIN, BAND))
    print()
    print('%-22s %-20s %-6s %-7s %-14s %-7s %-7s %s'
          % ('名称', '字色', 'fs', 'α', '分母(合成底)', 'CR', '余量', '判定'))
    print('-' * 118)

    fails = 0
    # 断言自证用的三个计数。全部在循环里数，不写死。
    #   checked     —— 走到结构断言的行数（「字色读不到」提前 continue 的不算）
    #   has_chain   —— 其中宿主链上确有非 transparent 自身 background 的行数
    #   assert_hit  —— 断言实际触发次数
    # 理由：通过时不打印，则「断言通过了」与「这段断言不存在」在输出上无法区分。
    checked = has_chain = assert_hit = 0
    for name, ink_sel, fb_sel, bg_sel, base_hex in CASES:
        d = rules.get(ink_sel) or {}
        if 'color' not in d:
            d = rules.get(fb_sel) or {}
        ink_v = d.get('color')
        fs = d.get('font-size') or (rules.get(fb_sel) or {}).get('font-size', '—')
        fs = fs.replace('px', '').strip() if fs != '—' else '—'

        if not ink_v:
            print('%-22s %-20s %-6s %-7s %-14s %-7s %-7s %s'
                  % (name, '?', fs, '—', base_hex, '—', '—', '字色读不到'))
            fails += 1
            continue

        base = parse_color(base_hex)
        tint, alpha = bg_layer(bg_sel, rules)

        # 结构断言：宿主链上存在非 transparent 的自身 background，则该行 α 不得为 0。
        # 理由：α=0 等于宣称「文字直接压在最底层上」，而它其实还压着一层。
        # 漏这一层方向恒为高估（浅底当深底 ⇒ 假 PASS），本轮已因此连错两次。
        # 这条断言让同一个 bug 不可能再发生第二次 —— 修一次只能修一次。
        chain = chain_backgrounds(ink_sel, rules)
        checked += 1
        if chain:
            has_chain += 1
        if chain and alpha <= 0:
            assert_hit += 1
            print('%-22s %-20s %-6s %-7s %-14s %-7s %-7s %s'
                  % (name, ink_v, fs, '0.00', base_hex, '—', '—',
                     'ASSERT 宿主链有底但 α=0（%s 声明 %s）' % (chain[0][0], chain[0][1])))
            fails += 1
            continue

        bg = over(parse_color(tint), alpha, base) if tint and alpha > 0 else base
        fg = parse_color(ink_v)

        r = cr(fg, bg)
        margin = r / MIN - 1

        exp = EXPECT.get(name)
        if exp is not None and abs(r - exp) > TOL:
            verdict = 'EXPECT 期望 %.2f，实测 %.2f' % (exp, r)
            fails += 1
        elif r < MIN:
            verdict = 'FAIL'
            fails += 1
        elif r < BAND:
            verdict = '通过·薄带（余量不足，已登记，二期）'
        else:
            verdict = '通过'

        print('%-22s %-20s %-6s %-7.2f #%02X%02X%02X        %-7.2f %+6.1f%% %s'
              % (name, ink_v, fs, alpha, bg[0], bg[1], bg[2], r, margin * 100, verdict))

    print('-' * 118)
    # 断言的 PASS 行：无条件打印，计数取自循环。
    # 触发时不打这行 —— 同一个断言不能同时说两句话。
    if assert_hit == 0:
        print('ASSERT 父链 α=0 未触发（检 %d 行 ｜ 其中 %d 行宿主链有非 transparent 底）'
              % (checked, has_chain))
    else:
        print('ASSERT 父链 α=0 触发 %d 行 —— 见上表，本次判定不可用' % assert_hit)
    print('登记 %d 条 ｜ FAIL %d 条 ｜ 未登记项不在本表内，也不代表通过'
          % (len(CASES), fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
