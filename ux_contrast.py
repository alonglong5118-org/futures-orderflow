#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对比度门禁 v2：对「同一条规则里同时声明了 background 和 color」的组合，算出真实 CR。
判据取自现象（实际渲染出的前景/背景对），不取自写法（不搜固定模式）。

存在理由：本轮两次漏检都源于「只搜一个方向」
  - 正向：红底 + 亮字（2.34）
  - 反向：浅底 + 红字（2.34）  ← .kb-reset 就是这么漏的，而 .kb-tag 的同类就在它上面 12 行

v2 变更（fe-impl 提的假阳性收口）：
  半透明背景不再一律叠在 --bg-0 上，而是**向上找最近的已声明 background 的祖先选择器**。
  旧行为把「卡片 > 徽标」的两层结构算成 1.02（.acc-badge 压 accent 底，真值 6.95），
  假阳性会让门禁失去可信度 —— 报出 6.95 的东西报成 1.02，等于把判据本身判坏了。
  回溯到的是**选择器祖先**而不是 DOM 祖先：只认 CSS 里写出来的那几层，不做 DOM 推断。
  若祖先也没有，仍退回页面色，并在输出里标「页面」。

门禁纪律：
  - SKIP 必须计数并报出，不得静默跳过。
  - 每条使用祖先基准的规则都打印基准链，供人工复核（否则没人知道分母怎么选的）。

用法：python3 ux_contrast.py [file] [--min 4.5] [--page #0A0D11] [--explain 选择器片段]
"""

import re
import sys
import argparse


# ---------------- token ----------------

def load_tokens(src):
    toks = {}
    for m in re.finditer(r'(--[a-z0-9-]+)\s*:\s*([^;}]+)', src):
        toks.setdefault(m.group(1), m.group(2).strip())
    return toks


def resolve(val, toks, depth=0, seen=None):
    """token 递归解析（含二级别名与环检测）"""
    seen = seen or set()
    v = val.strip()
    m = re.fullmatch(r'var\((--[a-z0-9-]+)\)', v)
    if m:
        t = m.group(1)
        if depth > 8 or t in seen:
            return None
        if t not in toks:
            return None
        return resolve(toks[t], toks, depth + 1, seen | {t})
    return v


def parse_color(v, toks):
    """返回 (r,g,b,a) 或 None"""
    if v is None:
        return None
    v = resolve(v, toks)
    if v is None:
        return None
    v = v.strip().rstrip('!important').strip()
    if v == 'transparent':
        return (0, 0, 0, 0.0)
    m = re.fullmatch(r'#([0-9a-fA-F]{3})', v)
    if m:
        h = m.group(1)
        return tuple(int(c * 2, 16) for c in h) + (1.0,)
    m = re.fullmatch(r'#([0-9a-fA-F]{6})', v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r'rgba?\(\s*var\((--[a-z0-9-]+)\)\s*,\s*([\d.]+)\s*\)', v)
    if m:
        rgb = resolve('var(%s)' % m.group(1), toks)
        parts = re.findall(r'\d+', rgb or '')
        if len(parts) == 3:
            return (int(parts[0]), int(parts[1]), int(parts[2]), float(m.group(2)))
        return None
    m = re.fullmatch(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4) or 1.0))
    return None


def over(fg, bg):
    """fg 叠在 bg 上的合成色（均为 (r,g,b,a)）"""
    a = fg[3]
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3)) + (1.0,)


def lum(c):
    def f(x):
        x /= 255.0
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = f(c[0]), f(c[1]), f(c[2])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def cr(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def split_decls(body):
    out = {}
    depth = 0
    cur = ''
    for ch in body:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ';' and depth == 0:
            if ':' in cur:
                k, v = cur.split(':', 1)
                out.setdefault(k.strip().lower(), v.strip())
            cur = ''
        else:
            cur += ch
    if ':' in cur:
        k, v = cur.split(':', 1)
        out.setdefault(k.strip().lower(), v.strip())
    return out


# ---------------- 选择器几何 ----------------

def sel_group(sel):
    """按顶层逗号拆选择器组"""
    parts, depth, cur = [], 0, ''
    for ch in sel:
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return [p for p in parts if p]


def compounds(sel):
    """按顶层空格拆复合选择器"""
    out, depth, cur = [], 0, ''
    for ch in sel:
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
                cur = ''
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def ancestor_candidates(sel):
    """
    由近到远的祖先选择器候选。
    '.a .b .c' -> ['.a .b', '.b', '.a']
    两个都列：祖先规则可能写全路径，也可能只写它自己那一段。
    """
    cs = compounds(sel)
    res = []
    for i in range(len(cs) - 1, 0, -1):
        full = ' '.join(cs[:i])
        if full not in res:
            res.append(full)
        last = cs[i - 1]
        if last not in res:
            res.append(last)
    return res


def strip_pseudo(sel):
    """去掉最后一个复合选择器上的伪类/伪元素，返回 (base, 是否含伪)"""
    cs = compounds(sel)
    if not cs:
        return sel, False
    last = cs[-1]
    base = re.sub(r'::?[a-zA-Z-]+(\([^)]*\))?', '', last)
    if base == last:
        return sel, False
    if not base:
        return ' '.join(cs[:-1]), True
    return ' '.join(cs[:-1] + [base]), True


def inherit_color(sel, colormap):
    """
    背景-only 规则的字色继承：
      1) 同元素基态规则（剥掉 :hover / .active 之类的伪类）声明的 color
      2) 最近的声明了 color 的祖先选择器
    返回 (color值, 来源选择器) 或 (None, None)
    """
    base, _ = strip_pseudo(sel)
    if base in colormap:
        return colormap[base], base
    for cand in ancestor_candidates(base):
        if cand in colormap:
            return colormap[cand], cand
        cb, _ = strip_pseudo(cand)
        if cb in colormap:
            return colormap[cb], cb
    return None, None


def backdrop_for(sel, bgmap, toks, page, trace, depth=0, seen=None):
    """
    向上回溯最近的已声明背景。返回合成后的不透明底色。
    trace 里按「远 → 近」记录途经的选择器，供人工复核分母。
    """
    seen = seen or set()
    if depth > 4:
        return page
    # 显式登记的面优先于任何推断（推不出来的那几层，人拍）
    base_key = None
    for b in BASES:
        if sel == b or sel.startswith(b + ' ') or sel.startswith(b + '.'):
            if base_key is None or len(b) > len(base_key):
                base_key = b
    if base_key:
        c = parse_color('var(%s)' % BASES[base_key], toks)
        if c:
            trace.append((sel + ' 〔登记面〕', 'var(%s)' % BASES[base_key], c))
            # 精确命中：登记的这张面就是该元素脚下的面
            if sel == base_key:
                return tuple(c[:3]) + (1.0,)
            # 前缀命中：sel 是 b 的后代 → b 自己的背景也是一层，不能跳过
            own = bgmap.get(base_key)
            if own:
                co = parse_color(own, toks)
                if co:
                    return over(co, c)
            return tuple(c[:3]) + (1.0,)
    for cand in ancestor_candidates(sel):
        if cand in seen:
            continue
        bgv = bgmap.get(cand)
        if not bgv:
            continue
        c = parse_color(bgv, toks)
        if not c:
            continue
        if c[3] >= 0.999:
            trace.append((cand, bgv, c))
            return tuple(c[:3]) + (1.0,)
        under = backdrop_for(cand, bgmap, toks, page, trace, depth + 1, seen | {cand})
        trace.append((cand, bgv, c))
        return over(c, under)
    return page


# ---------------- 「这个元素上屏的到底是什么」 ----------------
# 门禁量的是「文字压底色」。先证明有文字，再谈 4.5。
# 三类要分开：
#   text  —— 有字母/数字/汉字：4.5
#   icon  —— 只有符号（○ ● ▲ ✕ ⚠）：它是图标不是文字，1.4.11 的 3:1
#   none  —— 静态空叶子、JS 也不往里写字：纯装饰，不适用；但若它自带尺寸
#            （width/height 或 border-radius:50%）就是「有意义的图形对象」，
#            此时量的是「填充 vs 背底」而不是 color vs background
#   unknown —— 有子元素或静态空但 JS 会填：按 text 保守处理
# 不做这一步会产出 .nav-cat::before / .sec-dot 这类 1.00 的假阳性；
# 一刀切按 4.5 又会把 .hf-icon 的 ○ 判成文字失败 —— 两个方向都得挡住。

FORM_TAGS = ('select', 'input', 'textarea', 'option')
RANK = {'none': 0, 'icon': 1, 'unknown': 2, 'text': 3}


# ---------------- 人工表已删除（2026-11-22） ----------------
# 这张表曾经在这里存在过，2026-11-22 删除，理由如下（不要加回来）：
#   同一批三元组曾在两个脚本里各有一份 —— 本文件旧版的表定义，与
#   ux_static_text.py 的表。两处都在打同一张表，且已经漂移：
#     C6 段：PASS 5.84  底(32,39,47)      （含 .hard-filter-bar 自身 α.06 层）
#     F  段：EXPECT 期望 4.80 实测 6.43    （α 0.00，漏了那一层）
#   同一元素两套分母，就是第八个「谁对」的锚点之争。
#   人工表的唯一真源 = ux_static_text.py（gate 的 F 段），因为它有 余量 / 薄带 / EXPECT 断言。
#   本脚本只做自动扫描：能推的分母自动合成，推不出的计入 color-only 未覆盖并报出。
#   下一轮若有人想再加一张表，请先读这一行：两张表 = 两个真源 = 下一轮吵架的起点。

# 显式声明：这些选择器所在的"面"拍死在这里，不再向上猜
BASES = {
    '.hard-filter-bar': '--card',      # 父级 #sent-card = .decision-card，--card:#181F27 不透明，把链断在卡面
    '.hard-filter-bar.active-ban': '--card',
    '.hard-filter-bar.active-short': '--card',
    '.hard-filter-bar.active-both': '--card',
    '.extreme-banner': '--card',
}


def owner_bg(sel, bgmap):
    """color-only 规则：最长前缀匹配，找给它铺底的那条规则"""
    best = None
    for p in bgmap:
        if sel.startswith(p + ' ') and (best is None or len(p) > len(best)):
            best = p
    return best



def rightmost(sel_part):
    cs = compounds(sel_part)
    return cs[-1] if cs else sel_part


def js_writes(key, js):
    """
    这个 class/id 在 JS 里是否被「当作 DOM 元素」写入文字（静态空 ≠ 运行时空）。
    必须区分两种情况：
      - 元素被当元素选中：querySelector('.dot') / getElementById('dot') / .dot.textContent
      - 元素只是出现在一段 HTML 字符串里：innerHTML='<span class="dot"></span>'
    后者说明「它在这种写法里就是空的」，不构成「会被填字」的证据。
    """
    pats = [r'[.#]%s(?![\w-])' % re.escape(key),
            r'getElementById\(\s*[\'"]%s[\'"]' % re.escape(key),
            r'getElementsByClassName\(\s*[\'"]%s[\'"]' % re.escape(key),
            r'querySelector(?:All)?\(\s*[\'"][^\'"]*[.#]%s(?![\w-])' % re.escape(key)]
    for p in pats:
        for m in re.finditer(p, js):
            w = js[max(0, m.start() - 220):m.start() + 260]
            if re.search(r'textContent|innerHTML|innerText|insertAdjacentHTML|'
                         r'insertAdjacentText|\.append\(', w):
                return True
    return False


def text_kind(sel_part, d, html, js):
    last = rightmost(sel_part)
    if re.search(r'::?(?:before|after)\b', last):
        c = (d.get('content') or '').strip()
        if not c or c in ("''", '""', 'none', 'normal'):
            return 'none'
        return 'text' if re.search(r'[0-9A-Za-z\u4e00-\u9fff]', c.strip('\'"')) else 'icon'

    mid = re.search(r'#([A-Za-z0-9_-]+)', last)
    if mid:
        key = mid.group(1)
        pat = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)[^>]*\bid="%s"[^>]*>' % re.escape(key))
    else:
        cm = re.findall(r'\.([A-Za-z0-9_-]+)', last)
        if not cm:
            return 'unknown'
        key = cm[-1]
        pat = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)[^>]*\bclass="[^"]*(?<![\w-])%s(?![\w-])[^"]*"[^>]*>'
                         % re.escape(key))

    found, kinds = False, []
    for m in pat.finditer(html):
        found = True
        tag = m.group(1).lower()
        if tag in FORM_TAGS:
            kinds.append('text')      # 表单控件渲染的是值/option 文字
            continue
        seg = html[m.end():m.end() + 500]
        lead = seg.lstrip()
        raw = seg[:seg.find('<')] if '<' in seg else seg
        txt = re.sub(r'<[^>]*>|&[a-z]+;', '', raw).strip()
        if txt:
            kinds.append('text' if re.search(r'[0-9A-Za-z\u4e00-\u9fff]', txt) else 'icon')
            continue
        if lead.startswith('</'):
            kinds.append('unknown' if js_writes(key, js) else 'none')
        else:
            kinds.append('unknown')   # 有子元素，字可能在子节点里
    if not found:
        return 'unknown'
    return max(kinds, key=lambda k: RANK[k])


def sized_object(d):
    """无字元素是否带明确尺寸/圆形——是则是「图形对象」，否则只是「底」"""
    return bool(re.search(r'\b(width|height)\s*:\s*\d+(?:\.\d+)?px', ';'.join(
        '%s:%s' % kv for kv in d.items())) or re.search(r'border-radius\s*:\s*(50%|999px)', ';'.join(
        '%s:%s' % kv for kv in d.items())))


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file', nargs='?', default='four_dim_live.html')
    ap.add_argument('--min', type=float, default=4.5)
    ap.add_argument('--min-obj', type=float, default=3.0,
                    help='无文字图形对象的阈值（WCAG 1.4.11）')
    ap.add_argument('--page', default='#0A0D11')
    ap.add_argument('--explain', default=None, help='打印指定选择器的基准解析过程')
    a = ap.parse_args()

    src = open(a.file, encoding='utf-8').read()
    toks = load_tokens(src)
    page = parse_color(a.page, toks)

    css = '\n'.join(m.group(1) for m in re.finditer(r'<style[^>]*>(.*?)</style>', src, re.S))
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    html = re.sub(r'<style[^>]*>.*?</style>', '', src, flags=re.S)
    js = '\n'.join(m.group(1) for m in re.finditer(r'<script[^>]*>(.*?)</script>', src, re.S))
    js = re.sub(r'/\*.*?\*/', '', js, flags=re.S)

    rules = []
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel, body = m.group(1).strip(), m.group(2)
        if not sel or sel.startswith('@'):
            continue
        rules.append((sel, split_decls(body), body))

    bgmap = {}
    for sel, d, _ in rules:
        bgv = d.get('background') or d.get('background-color')
        if not bgv or bgv.startswith(('linear-gradient', 'radial-gradient', 'none', 'url')):
            continue
        if not parse_color(bgv, toks):
            continue
        for p in sel_group(sel):
            bgmap[p] = bgv

    colormap = {}
    for sel, d, _ in rules:
        if 'color' not in d or not parse_color(d['color'], toks):
            continue
        for p in sel_group(sel):
            colormap[p] = d['color']

    if a.explain:
        target = [p for p in bgmap if a.explain in p]
        print('[explain] 匹配背景规则 %d 条' % len(target))
        for p in sorted(target)[:20]:
            tr = []
            bd = backdrop_for(p, bgmap, toks, page, tr)
            print('  %-46s bg=%-28s 祖先底=#%02X%02X%02X  %s'
                  % (p[:46], bgmap[p][:28], bd[0], bd[1], bd[2],
                     ' ← '.join('%s(%s)' % (c, v) for c, v, _ in tr) or '页面'))
        return 0

    text_hits, obj_hits = [], []
    skipped = {'渐变背景（无法定值）': 0, '颜色无法解析': 0}
    ancestor_users = []
    evaluated = 0
    inh_eval = 0
    inh_skip = {'渐/无效底': 0, '字色继承不到': 0}
    kinds = {'text': 0, 'icon': 0, 'none·装饰': 0, 'none·有尺寸→按图形对象': 0, 'unknown': 0}
    watch = []

    def under_of(sel, trace_out):
        for p in sel_group(sel):
            tr = []
            cand = backdrop_for(p, bgmap, toks, page, tr)
            if tr:
                trace_out.extend(tr)
                return cand
        return page

    def record(lst, r, sel, body, bg_c, fg_c, tag):
        fs = re.search(r'font-size:\s*([\d.]+)px', body)
        w = re.search(r'font-weight:\s*(\d+)', body)
        lst.append((r, tag, sel[:46], round(float(fs.group(1)), 1) if fs else None,
                    w.group(1) if w else '', bg_c[:3], fg_c[:3]))

    for sel, d, body in rules:
        bgv = d.get('background') or d.get('background-color')
        if not bgv:
            continue
        has_fg = bool(d.get('color'))
        if bgv.startswith(('linear-gradient', 'radial-gradient', 'none', 'url')):
            if has_fg:
                skipped['渐变背景（无法定值）'] += 1
            else:
                inh_skip['渐/无效底'] += 1
            continue
        bg = parse_color(bgv, toks)
        if not bg:
            if has_fg:
                skipped['颜色无法解析'] += 1
            else:
                inh_skip['渐/无效底'] += 1
            continue

        first = sel_group(sel)[0]
        if has_fg:
            owner, src = d['color'], '本规则'
        else:
            owner, src = inherit_color(first, colormap)
            if not owner:
                inh_skip['字色继承不到'] += 1
                continue
        fg = parse_color(owner, toks)
        if not fg:
            if has_fg:
                skipped['颜色无法解析'] += 1
            else:
                inh_skip['渐/无效底'] += 1
            continue

        kind = text_kind(first, d, html, js)
        if kind == 'none':
            if sized_object(d) and bg[3] >= 0.999:
                kind = 'none·obj'
            else:
                kind = 'none·bed'

        trace = []
        under = under_of(sel, trace)
        bg_c = over(bg, under)
        tag = '' if has_fg else ('继承:' + (src or '?') + ' ')

        if has_fg:
            evaluated += 1
            if trace:
                ancestor_users.append((sel, trace))
        else:
            inh_eval += 1

        if kind == 'text' or kind == 'unknown':
            kinds['text' if kind == 'text' else 'unknown'] += 1
            fg_c = over(fg, bg_c)
            r = cr(fg_c, bg_c)
            if r < a.min:
                record(text_hits, r, sel, body, bg_c, fg_c,
                       tag + ('字' if kind == 'text' else '字未定（按有字计）'))
        elif kind == 'icon':
            kinds['icon'] += 1
            fg_c = over(fg, bg_c)
            r = cr(fg_c, bg_c)
            if r < a.min_obj:
                record(obj_hits, r, sel, body, bg_c, fg_c, tag + '图标')
            elif r < a.min:
                watch.append((r, sel[:46], tag + '图标：达 3:1 未达 4.5'))
        elif kind == 'none·obj':
            kinds['none·有尺寸→按图形对象'] += 1
            r = cr(bg_c, under)
            if r < a.min_obj:
                record(obj_hits, r, sel, body, under, bg_c, tag + '图形对象·填充 vs 背底')
        else:
            kinds['none·装饰'] += 1

    # ================= color-only 补充扫描 =================
    # 上一版的致命缺口：color 在自己规则里、background 在另一条规则里 —— 这类规则
    # 在第一行 `if not bgv: continue` 就被丢掉，既不进判定、也不进 SKIP、也不进任何计数。
    # 「文字压底色」这个判据里 color-only 恰恰是最常见的写法（.hf-text span 就是）。
    # 现在：给它找最长前缀的着色祖先；找不到的**计入未覆盖并报出**，不许静默消失。
    co_total = co_covered = 0
    co_uncovered = []
    for sel, d, body in rules:
        if 'color' not in d:
            continue
        if d.get('background') or d.get('background-color'):
            continue
        co_total += 1
        first = sel_group(sel)[0]
        host = owner_bg(first, bgmap)
        if host is None:
            co_uncovered.append(first)
            continue
        fg = parse_color(d['color'], toks)
        bg = parse_color(bgmap[host], toks)
        if not fg or not bg:
            co_uncovered.append(first)
            continue
        co_covered += 1
        tr = []
        under = backdrop_for(host, bgmap, toks, page, tr)
        bg_c = over(bg, under)
        kind = text_kind(first, d, html, js)
        if kind not in ('text', 'unknown', 'icon'):
            continue
        fg_c = over(fg, bg_c)
        r = cr(fg_c, bg_c)
        lim = a.min if kind != 'icon' else a.min_obj
        if r < lim:
            record(text_hits, r, sel, body, bg_c, fg_c,
                   'color-only · 底来自 %s%s' % (host[:22], '' if tr else '（分母=页面，可能偏高）'))

    print('规则总数 %d / 双声明判定 %d / 继承字色判定 %d / color-only 判定 %d'
          % (len(rules), evaluated, inh_eval, co_covered))
    print('跳过 → 双声明：' + '，'.join('%s %d' % (k, v) for k, v in skipped.items() if v)
          + '  ｜ 继承：' + '，'.join('%s %d' % (k, v) for k, v in inh_skip.items() if v))
    print('元素性质：文字 %d / 图标（3:1）%d / 有尺寸图形对象（3:1）%d / 纯装饰底（不适用）%d / 未定按文字计 %d'
          % (kinds['text'], kinds['icon'], kinds['none·有尺寸→按图形对象'],
             kinds['none·装饰'], kinds['unknown']))
    print('半透明背景改用祖先基准：%d 条' % len(ancestor_users))
    for sel, tr in ancestor_users:
        print('  %-44s ← %s' % (sel[:44], ' ← '.join('%s(%s)' % (c, v) for c, v, _ in tr)))
    print()
    text_hits.sort()
    print('文字级低对比（< %.1f）：%d 处' % (a.min, len(text_hits)))
    for r, tag, sel, fs, w, bg, fg in text_hits:
        print('  %.2f  %-46s fs=%-5s w=%-3s 字%s on 底%s  [%s]' % (r, sel, fs, w, fg, bg, tag))
    obj_hits.sort()
    print('图形级低对比（< %.1f）：%d 处' % (a.min_obj, len(obj_hits)))
    for r, tag, sel, fs, w, bg, fg in obj_hits:
        print('  %.2f  %-46s fs=%-5s 色%s vs 底%s  [%s]' % (r, sel, fs, fg, bg, tag))
    watch.sort()
    if watch:
        print('观察项（达 3:1、未达 4.5 的图标，不判 FAIL）：%d 处' % len(watch))
        for r, sel, why in watch:
            print('  %.2f  %-46s %s' % (r, sel, why))
    print()
    n_skip_all = sum(skipped.values()) + sum(inh_skip.values())
    print()
    print('F 段覆盖：判定 %d 条 / SKIP %d 条 / color-only 未覆盖 %d 条（color-only 共 %d 条）'
          % (evaluated + inh_eval + co_covered, n_skip_all,
             len(co_uncovered), co_total))
    print('  ← 归零不等于问题归零。只报"0 处"的段，会让读的人以为静态对比度已清零。')
    if co_uncovered:
        print('  color-only 未覆盖前 8 例：%s' % '，'.join(sorted(co_uncovered)[:8]))
    if evaluated + inh_eval + co_covered == 0:
        print('  ✗ 判定 0 条 —— 门禁是死的，不是全绿。')
        return 1
    return 1 if (text_hits or obj_hits) else 0


if __name__ == '__main__':
    sys.exit(main())
