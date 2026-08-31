#!/usr/bin/env python3
"""frontend-defensive-lint — 9 API drift anti-patterns

Exit code: 0=ok, 1=blocking violation found
"""
import re, sys, argparse
from pathlib import Path

SCRIPT_BLOCK_RE = re.compile(r'<script[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
FN_DEF_RE = re.compile(r'(?:async\s+)?function\s+\w+')

# ============ BLOCKING RULES ============

def check_1_field_name_drift(code, _path):
    """🔴 #1: JS 模板里用 .name 但 API 可能只给 .group"""
    issues = []
    if '.innerHTML' in code:
        lines = code.split('\n')
        for i, line in enumerate(lines):
            if '.innerHTML' in line and '$' in line:
                if re.search(r'\.\w+\.name\}', line) and 'g.group' not in line and 'g.group_name' not in line:
                    issues.append((i+1, "模板 .name — API 可能已改名 .group"))
    return issues

def check_3_mixed_json_types(code, _path):
    """🔴 #3: json.load() 后的 dict 迭代 — 数据文件可能混入 list key"""
    issues = []
    # 找所有 json.load/json.loads 调用及其后续迭代
    for m in re.finditer(r'json\.loads?\s*\(', code):
        # 往后搜 50 行找对结果变量的 .items() 迭代
        search_from = m.start()
        region = code[search_from:search_from+3000]
        # 提取赋值变量名（如 data = json.load(...)）
        assign = re.search(r'(\w+)\s*=\s*json\.loads?\(', region)
        if not assign:
            continue
        var = assign.group(1)
        # 找对这个变量的 items() 迭代
        iter_m = re.search(r'for\s+\w+\s*,\s*\w+\s+in\s+' + var + r'\.items\(\):', region)
        if not iter_m:
            continue
        # 找迭代体内的 .get() 调用（没有 isinstance 保护）
        body_search_start = search_from + iter_m.end()
        body_region = code[body_search_start:body_search_start+800]
        if '.get(' in body_region and 'isinstance' not in body_region:
            lineno = code[:body_search_start].count('\n') + 1
            issues.append((lineno,
                f"json.load 变量 `{var}` 迭代后 .get() — 数据文件混入 list key 会炸"))
    return issues

def check_6_tdz_global(code, _path):
    """🔴 #6: TDZ — 只检查 <script> 顶层 let 声明"""
    issues = []
    exclude = {'r','d','v','s','g','f','z','h','b','c','x','y','n','p','t','l','w','u','o','k','j','m','i','a','e','ds','res','ctx'}
    for block in SCRIPT_BLOCK_RE.findall(code):
        slines = block.split('\n')
        fn_ranges = []
        i = 0
        while i < len(slines):
            if FN_DEF_RE.match(slines[i].strip()):
                depth = slines[i].count('{') - slines[i].count('}')
                j = i + 1
                while j < len(slines):
                    depth += slines[j].count('{') - slines[j].count('}')
                    if depth <= 0 and j > i + 1: break
                    j += 1
                fn_ranges.append((i, j))
                i = j
            else: i += 1
        def in_fn(li):
            return any(s <= li <= e for s, e in fn_ranges)
        decl_vars = {}
        for i, line in enumerate(slines):
            if in_fn(i): continue
            for m in re.finditer(r'let\s+([a-zA-Z_]\w*)\s*[=;]', line):
                var = m.group(1)
                if len(var) <= 2 or var.lower() in exclude: continue
                decl_vars.setdefault(var, i)
        for var, decl_idx in decl_vars.items():
            for i, line in enumerate(slines):
                if i >= decl_idx: break
                if in_fn(i): continue
                stripped = line.strip()
                if stripped.startswith('//'): continue
                if re.search(r'\b' + re.escape(var) + r'\b', stripped):
                    issues.append((i+1, f"script 顶层 `{var}` let 在 L{decl_idx+1}，L{i+1} 已引用 — TDZ"))
                    break
    return issues

def check_7_tab_refresh(code, _path):
    """🔴 #7: switchTab 某分支 refresh 为 0"""
    issues = []
    EXEMPT = {'crosscheck','focus','health','cockpit','paper','seat','chat'}
    for m in re.finditer(r"if\(name==='(\w+)'\)\{(.*?)\}", code, re.DOTALL):
        tab = m.group(1)
        body = m.group(2)
        calls = re.findall(r'(?:refresh|load|render)\w*\(', body)
        if len(calls) == 0 and tab not in EXEMPT:
            issues.append((code[:m.start()].count('\n')+1,
                f"switchTab('{tab}') 无 refresh — 子面板可能永远不加载"))
    return issues

# ============ WARNING RULES ============

def check_5_dom_guard(code, _path):
    """🟡 #5: getElementById 后 3 行内无 null guard"""
    lines = code.split('\n')
    issues = []
    for i, line in enumerate(lines):
        m = re.search(r'(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\(', line)
        if not m: continue
        var_name = m.group(1)
        if f'if(!{var_name})' in line or f'if (!{var_name})' in line: continue
        found = False
        for j in range(i+1, min(i+4, len(lines))):
            c = lines[j].strip()
            if f'if(!{var_name})' in c or f'if (!{var_name})' in c or f'{var_name} &&' in c:
                found = True; break
        if not found:
            issues.append((i+1, f"{var_name} = getElementById 无 guard"))
    return issues

def check_8_numeric_string(code, _path):
    """🟡 #8: .toFixed() 前无 parseFloat — 仅 JS 文件"""
    if '<script' not in code:
        return []
    lines = code.split('\n')
    issues = []
    for i, line in enumerate(lines):
        if '.toFixed(' not in line or 'parseFloat' in line or 'parseInt' in line:
            continue
        left = line.split('.toFixed')[0].strip()
        if re.match(r'^[\d.]+[+\-*/]?\s*$', left): continue
        if left.endswith(')'):
            inner = re.findall(r'\(([^)]+)\)', left)
            if inner and any(x for x in inner if 'Math.' in x or 'parseFloat' in x or 'parseInt' in x):
                continue
        # 排除已知返回 number 的 API 字段（保守：只在明显是模板插值时报）
        if '${' in left or '${' in line:
            issues.append((i+1, f"{left[:40]}.toFixed() — 模板变量可能是 string"))
    return issues

def check_9_no_try_catch(code, _path):
    """🟡 #9: fetch 但无 try-catch"""
    issues = []
    for m in re.finditer(r'(?:async\s+)?function\s+(refresh\w+|load\w+|render\w+)\s*\(', code):
        fname = m.group(1)
        start = m.start()
        brace = code.index('{', start)
        depth, j = 0, brace
        while j < len(code):
            if code[j] == '{': depth += 1
            elif code[j] == '}':
                depth -= 1
                if depth == 0: break
            j += 1
        body = code[brace:j+1]
        if ('fetch' in body or '_smartFetch' in body) and 'try' not in body:
            issues.append((code[:start].count('\n')+1, f"{fname}() fetch 无 try-catch"))
    return issues

BLOCKING = [
    ('#1 字段名漂移', check_1_field_name_drift),
    ('#3 json 混合类型', check_3_mixed_json_types),
    ('#7 Tab refresh 漏调', check_7_tab_refresh),
]
WARNING = [
    ('#5 DOM guard', check_5_dom_guard),
    ('#6 TDZ 隐式全局', check_6_tdz_global),
    ('#8 数值未归一', check_8_numeric_string),
    ('#9 try-catch', check_9_no_try_catch),
]

def run_lint(filepath):
    p = Path(filepath)
    if not p.exists(): print(f"❌ 不存在: {filepath}"); return None
    code = p.read_text(encoding='utf-8')
    r = {'blocking': [], 'warning': [], 'file': filepath}
    for name, fn in BLOCKING:
        for lineno, msg in fn(code, filepath): r['blocking'].append((name, lineno, msg))
    for name, fn in WARNING:
        for lineno, msg in fn(code, filepath): r['warning'].append((name, lineno, msg))
    return r

def report(r):
    if not r: return False
    bl, wl = len(r['blocking']), len(r['warning'])
    name = Path(r['file']).name
    if bl == 0 and wl == 0:
        print(f"  ✅ {name} 无违规")
        return False
    print(f"\n  📋 {name}")
    if r['blocking']:
        print(f"  🔴 BLOCKING ({bl}):")
        for n, l, m in r['blocking']: print(f"    L{l:>5} [{n}] {m}")
    if r['warning']:
        print(f"  🟡 WARN ({wl}):")
        for n, l, m in r['warning'][:10]: print(f"    L{l:>5} [{n}] {m}")
        if wl > 10: print(f"    ... +{wl-10} more")
    print(f"  → 🔴{bl} / 🟡{wl}")
    return bl > 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('--report', action='store_true', help='报告模式（exit 0）')
    args = ap.parse_args()
    any_block = False
    results = []
    for f in args.files:
        r = run_lint(f)
        if r: results.append(r)
        if report(r): any_block = True
    sys.exit(0 if args.report or not any_block else 1)

if __name__ == '__main__': main()
