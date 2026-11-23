#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动画极值门禁（motion gate）v3

规则来源：截图只能证明"某一帧"，不能证明"全程"。凡带 animation 的元素，
判定值必须取该动画周期的极值，静止值不作数。

判据（方向判据，不是属性判据）：
  文字宿主：keyframe 不得使元素内文本与其背景的对比度低于 4.5，且不得低于静止基线。
  无字宿主：按 WCAG 1.4.11 量「填充 vs 背底」的极值，阈值 3.0。
            —— 无字不等于不适用，它只是换了一根轴。旧版把这一类直接 SKIP，
               等于把"没测"写成了"不适用"。

v3 改三件事（全部来自 team-lead / visual-architect 的复核）：
  1. @static 回退必须打印并单独计数。旧版把 12.35 同时当静止值和极值，
     读数的人分不出「算过且通过」和「没算、取了静止值、恰好方向安全」。
  2. 分母不能写死成 --bg-0。`.rs-HALTED` 在 `.decision-card` 里，真分母是 --card：
     α.20 叠 bg-0 是 12.35，叠 --card 是 10.40。用错分母只会得到偏乐观的数。
     分母推不出来的，人登记在 DENOM 里并打印出来。
  3. PASS 的理由必须能被读出来，否则它和 SKIP 长得一样。
     每行多一列「理由」：按 keyframe 已算 / 文字不在 keyframe 内 / @static 回退。

用法：python3 ux_motion.py [file]
"""
import re, sys

PAGE = (10, 13, 17)          # --bg-0
MIN_CR = 4.5                 # 文字
MIN_OBJ = 3.0                # 无字图形对象（WCAG 1.4.11）

# 分母登记：DOM 位置决定的事，CSS 文本里推不出来，必须人写、必须打印。
# `.rs-HALTED` 坐在 `.decision-card{background:var(--card)}` 里（L2119/L3624）。
# 写死的代价很具体：写成 bg-0 会报 12.35，真值 10.40 —— 错在乐观一侧。
DENOM = {
    '.rs-HALTED': '--card',
    '#kill-banner': '--bg-0',
}


def lin(v):
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def L(c):
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def CR(a, b):
    la, lb = L(a), L(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, bg):
    a = fg[3]
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3)) + (1.0,)


def tokens(src):
    t = {}
    for m in re.finditer(r'(--[a-z0-9-]+)\s*:\s*([^;}]+)', src):
        t.setdefault(m.group(1), m.group(2).strip())
    return t


def res(v, t, d=0, seen=None):
    seen = seen or set()
    v = v.strip()
    m = re.fullmatch(r'var\((--[a-z0-9-]+)\)', v)
    if not m:
        return v
    k = m.group(1)
    if d > 8 or k in seen or k not in t:
        return None
    return res(t[k], t, d + 1, seen | {k})


def col(v, t):
    if v is None:
        return None
    v = res(v, t)
    if v is None:
        return None
    v = v.strip()
    m = re.fullmatch(r'#([0-9a-fA-F]{3})', v)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1)) + (1.0,)
    m = re.fullmatch(r'#([0-9a-fA-F]{6})', v)
    if m:
        return (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16), 1.0)
    m = re.fullmatch(r'rgba?\(\s*var\((--[a-z0-9-]+)\)\s*,\s*([\d.]+)\s*\)', v)
    if m:
        rgb = res('var(%s)' % m.group(1), t)
        p = re.findall(r'\d+', rgb or '')
        return (int(p[0]), int(p[1]), int(p[2]), float(m.group(2))) if len(p) == 3 else None
    m = re.fullmatch(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4) or 1))
    return None


def decls(body):
    out, depth, cur = {}, 0, ''
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


def main():
    f = sys.argv[1] if len(sys.argv) > 1 else 'four_dim_live.html'
    src = open(f, encoding='utf-8').read()
    t = tokens(src)
    css = '\n'.join(m.group(1) for m in re.finditer(r'<style[^>]*>(.*?)</style>', src, re.S))
    css_nc = re.sub(r'/\*.*?\*/', '', css, flags=re.S)

    kfs = {}
    for m in re.finditer(r'@keyframes\s+([\w-]+)\s*\{', css_nc):
        i, depth, start = m.end(), 1, m.end()
        while i < len(css_nc) and depth:
            if css_nc[i] == '{':
                depth += 1
            elif css_nc[i] == '}':
                depth -= 1
            i += 1
        body = css_nc[start:i - 1]
        stops = []
        for sm in re.finditer(r'([\d.%, ]+)\{([^{}]*)\}', body):
            stops.append((sm.group(1).strip(), decls(sm.group(2))))
        kfs[m.group(1)] = stops

    hosts = []
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css_nc):
        sel, body = m.group(1).strip(), m.group(2)
        if not sel or sel.startswith('@'):
            continue
        am = re.search(r'animation:\s*([\w-]+)\s*([^;}]*)', body)
        if not am:
            continue
        if 'infinite' not in am.group(2):
            continue
        hosts.append((sel, decls(body), am.group(1)))

    print('infinite 动画宿主：%d 个' % len(hosts))
    bad = 0
    bad_hosts = set()
    skipped = []
    stat_fb = 0          # @static 回退（未按 keyframe 逐 stop 计算）
    by_kf = 0            # 真正按 keyframe 判定
    no_text = 0          # 无字宿主，按 1.4.11 判定
    obj_bad = 0

    child_text = []
    for cm in re.finditer(r'([^{}]+)\{([^{}]*)\}', css_nc):
        csel = cm.group(1).strip()
        if csel.startswith('@'):
            continue
        cd = decls(cm.group(2))
        if 'color' in cd:
            child_text.append((csel, cd))

    def denom_of(sel, name):
        tok = DENOM.get(sel)
        if tok:
            c = col('var(%s)' % tok, t)
            if c:
                return tuple(c[:3]) + (1.0,), tok
        return PAGE, '页面（未登记，可能偏高）'

    for sel, d, name in hosts:
        fg = col(d.get('color'), t)
        bgv = d.get('background') or d.get('background-color')
        bg = col(bgv, t) if bgv else None
        stops = kfs.get(name, [])

        if not fg:
            # ---------- 无字宿主：换一根轴（1.4.11），不是"不适用" ----------
            fill = bg
            if fill is None:
                bd = d.get('border') or d.get('border-color')
                if bd:
                    mm = re.search(r'rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}|var\(--[a-z0-9-]+\)', bd)
                    fill = col(mm.group(0), t) if mm else None
            if fill is None:
                skipped.append((sel, '无文字且无填充色（纯容器），4.5 与 1.4.11 均不适用'))
                print('  SKIP  %-40s %-14s 无字且无填充色，两根轴都不适用' % (sel[:40], name))
                continue
            base_denom, dname = denom_of(sel, name)
            obs, oat = CR(fill, base_denom), 'static'
            for at, sd in stops:
                f2 = over(col(sd['background-color'], t), base_denom) if 'background-color' in sd else fill
                if 'opacity' in sd:
                    a2 = float(sd['opacity'])
                    f2 = tuple(round(a2 * f2[i] + (1 - a2) * base_denom[i]) for i in range(3)) + (1.0,)
                r2 = CR(f2, base_denom)
                if r2 < obs:
                    obs, oat = r2, at
            ok = obs >= MIN_OBJ
            no_text += 1
            if not ok:
                obj_bad += 1
                bad_hosts.add((sel, name))
            print('  %s  %-40s %-14s 1.4.11 填充 vs %s：静止=%.2f 极值=%.2f @%s  %s'
                  % ('PASS' if ok else 'FAIL', sel[:40], name, dname,
                     CR(fill, base_denom), obs, oat,
                     '按 keyframe 已算' if stops else '@static 回退（未按 keyframe 逐 stop 计算）'))
            continue

        if (not fg) and bg:
            kids = [(cs, cd) for cs, cd in child_text
                    if cs.startswith(sel + ' ') and 'color' in cd]
            if kids:
                for cs, cd in kids:
                    kfg = col(cd['color'], t)
                    if not kfg:
                        continue
                    base_denom, dname = denom_of(sel, name)
                    kb = over(bg, base_denom)
                    kbase = CR(over(kfg, kb), kb)
                    kmin, kat = None, 'static'
                    for at, sd in stops:
                        b2 = over(col(sd['background-color'], t), base_denom) if 'background-color' in sd else kb
                        f2 = kfg
                        if 'opacity' in sd:
                            a2 = float(sd['opacity'])
                            f2 = tuple(round(a2 * kfg[i] + (1 - a2) * base_denom[i]) for i in range(3)) + (1.0,)
                            b2 = tuple(round(a2 * b2[i] + (1 - a2) * base_denom[i]) for i in range(3)) + (1.0,)
                        r2 = CR(f2, b2)
                        if kmin is None or r2 < kmin:
                            kmin, kat = r2, at
                    klo = kbase if kmin is None else kmin
                    ok2 = klo >= MIN_CR
                    if stops:
                        by_kf += 1
                    else:
                        stat_fb += 1
                    if not ok2:
                        bad_hosts.add((sel, name))
                    print('  %s  %-40s %-12s 静止=%.2f 极值=%.2f @%s  [子元素 %s]  理由:%s'
                          % ('PASS' if ok2 else 'FAIL', sel[:40], name, kbase, klo, kat,
                             cs.split(' ', 1)[-1][:18],
                             '按 keyframe 已算' if stops else '@static 回退（未按 keyframe 逐 stop 计算）'))
                continue
            skipped.append((sel, '宿主与子选择器均无文字色（无字装饰）'))
            print('  SKIP  %-42s 宿主与子选择器均无文字色，判定为无文字装饰' % sel[:42])
            continue

        if not fg or not bg:
            skipped.append((sel, '无独立前景/背景声明（文字继承，无法定值）'))
            print('  SKIP  %-42s 无独立前景/背景声明（无文字或继承），不适用' % sel[:42])
            continue

        base_denom, dname = denom_of(sel, name)
        base_bg = over(bg, base_denom)
        base = CR(over(fg, base_bg), base_bg)
        lo, lo_at = None, 'static'
        for at, sd in stops:
            b = over(col(sd['background-color'], t), base_denom) if 'background-color' in sd else base_bg
            f2 = fg
            if 'opacity' in sd:
                a = float(sd['opacity'])
                f2 = tuple(round(a * fg[i] + (1 - a) * base_denom[i]) for i in range(3)) + (1.0,)
                b = tuple(round(a * b[i] + (1 - a) * base_denom[i]) for i in range(3)) + (1.0,)
            if 'color' in sd:
                f2 = over(col(sd['color'], t), b)
            r = CR(f2, b)
            if lo is None or r < lo:
                lo, lo_at = r, at
        if lo is None:
            lo, lo_at = base, 'static'
        ok = lo >= MIN_CR
        dirn = '下降' if lo < base else '不降'
        # 理由列：区分「算过」与「没算」
        text_in_kf = any(('color' in sd) or ('opacity' in sd)
                        or ('background' in sd) or ('background-color' in sd)
                        for _, sd in stops)
        if not stops:
            why = '@static 回退（未按 keyframe 逐 stop 计算）'
            stat_fb += 1
        elif not text_in_kf:
            why = '文字属性不在 keyframe 内 → 静止值即全周期值'
            by_kf += 1
        else:
            why = '按 keyframe 已算，min@%s' % lo_at
            by_kf += 1
        if not ok:
            bad_hosts.add((sel, name))
        print('  %s  %-40s %-14s 静止=%.2f 极值=%.2f @%s  %s  分母:%s  理由:%s'
              % ('PASS' if ok else 'FAIL', sel[:40], name, base, lo, lo_at, dirn, dname, why))
        if not ok:
            print('        → 极值低于 %.1f，且较基线下降 %.2f。动画只许提高文本对比度。' % (MIN_CR, base - lo))

    n = len(bad_hosts)
    print()
    print('极值门禁：%s（检 %d 个宿主｜文字判定 %d｜无字按 1.4.11 判定 %d｜跳过 %d）'
          % ('PASS' if n == 0 else 'FAIL', len(hosts), by_kf + stat_fb, no_text, len(skipped)))
    print('  其中 按 keyframe 逐 stop 计算 %d ｜ @static 回退 %d ｜ 1.4.11 不达标 %d ｜ 不达标合计 %d'
          % (by_kf, stat_fb, obj_bad, n))
    for s_, why in skipped:
        print('   跳过 %-42s %s' % (s_[:42], why))
    for s_, nm in sorted(bad_hosts):
        print('   - %s  [%s]' % (s_, nm))
    return 1 if n else 0


if __name__ == '__main__':
    sys.exit(main())
