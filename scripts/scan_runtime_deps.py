#!/usr/bin/env python3
"""运行时依赖扫描器 —— 防深度清理误移（2026-09-14 教训固化）。

背景：
  深度清理把 187 个 .py 移到 experiment_archive/ 时，依赖分析只覆盖静态/顶层 import，
  漏掉"延迟 import"（函数内部 from xxx import），导致 32 个运行时模块被误移、
  hc 回测 expR 从 -0.2025 漂移到 -0.6169（fundamental_factors 被移走后 compute_F_v2
  静默 return 0.0，F 分数全 0）。

用法：
  python scripts/scan_runtime_deps.py            # 扫 .py 依赖：列出被移到 archive 但仍被运行时引用的模块
  python scripts/scan_runtime_deps.py --restore  # 自动恢复上述缺失模块（shutil.move 回根目录）
  python scripts/scan_runtime_deps.py --json     # 扫 .json 引用：列出零引用的候选归档（需人工确认再移）

铁律：
  1. 只移动"零引用"的文件；账户/风控/实盘/配置/缓存类即使零引用也【禁止移动】。
  2. 移动用 shutil.move（归档不删除），可随时回退。
  3. 移动后必须重跑 `run_tests.py` 并确认 EXIT=0，再检查 8741 线上 runner 健康。
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARCH = os.path.join(ROOT, "experiment_archive")

# 提取 import 的正则：覆盖 import x / from x import y / from x import (…) / import x.y
IMPORT_PAT = re.compile(
    r'^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.MULTILINE
)
# 动态 import：importlib.import_module("x") / import_module('x') / __import__("x")
DYNAMIC_PAT = re.compile(
    r'(?:importlib\.)?import_module\s*\(\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']'
    r'|__import__\s*\(\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']'
)


def _arch_modules():
    """experiment_archive/ 里所有 .py 的 basename（不含扩展名）→ 绝对路径。"""
    out = {}
    if not os.path.isdir(ARCH):
        return out
    for f in os.listdir(ARCH):
        if f.endswith(".py"):
            out[f[:-3]] = os.path.join(ARCH, f)
    return out


def _read(f):
    try:
        with open(f, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _iter_imports(txt):
    """提取文本里所有本地模块名（静态 + 动态 import）。"""
    for m in IMPORT_PAT.findall(txt):
        yield m
    for grp in DYNAMIC_PAT.findall(txt):
        yield grp[0] or grp[1]


def scan_py(restore=False):
    """扫 .py 依赖：找出在 archive 但不在根目录、却被运行时 .py 引用的模块。"""
    arch = _arch_modules()
    runtime_py = [
        f for f in os.listdir(ROOT)
        if f.endswith(".py") and not f.startswith("_")
    ]
    tests_py = [
        os.path.join("tests", f) for f in os.listdir(os.path.join(ROOT, "tests"))
        if f.endswith(".py")
    ]
    seeds = [f for f in runtime_py] + tests_py

    missing = set()          # 缺失模块名集合
    missing_src = {}         # 模块名 → 引用它的文件
    scanned = set()

    def _resolve(path):
        return path if os.path.isabs(path) else os.path.join(ROOT, path)

    queue = [_resolve(p) for p in seeds]
    while queue:
        f = queue.pop(0)
        if f in scanned:
            continue
        scanned.add(f)
        for mod in _iter_imports(_read(f)):
            # 只关心"本地模块"：在 archive 里（被移走）且根目录没有同名 .py
            if mod in arch and not os.path.exists(os.path.join(ROOT, mod + ".py")):
                missing.add(mod)
                missing_src.setdefault(mod, []).append(os.path.relpath(f, ROOT))
                queue.append(arch[mod])  # 递归扫被移模块自己的依赖

    if not missing:
        print("✅ 无缺失依赖：archive 里的模块均不被运行时 .py 引用")
        return 0

    print(f"⚠️  发现 {len(missing)} 个被误移的运行时依赖模块：")
    for mod in sorted(missing):
        print(f"  {mod}  <-  {', '.join(sorted(set(missing_src[mod])))}")

    if restore:
        for mod in sorted(missing):
            shutil.move(arch[mod], os.path.join(ROOT, mod + ".py"))
        print(f"\n已恢复 {len(missing)} 个模块到根目录")
    else:
        print("\n提示：加 --restore 自动恢复（移动不删除，可回退）")
    return 0


def scan_json():
    """扫 .json 引用：列出根目录里零精确引用的 .json（候选归档，需人工确认）。"""
    runtime_py = [
        f for f in os.listdir(ROOT)
        if f.endswith(".py") and not f.startswith("_")
    ]
    tests_py = [
        os.path.join("tests", f) for f in os.listdir(os.path.join(ROOT, "tests"))
        if f.endswith(".py")
    ]
    texts = {}
    for p in runtime_py + tests_py:
        texts[p] = _read(os.path.join(ROOT, p))

    json_files = sorted(f for f in os.listdir(ROOT) if f.endswith(".json"))
    unreferenced = []
    for jf in json_files:
        pat = re.compile(r'["\']' + re.escape(jf) + r'["\']')
        if not any(pat.search(t) for t in texts.values()):
            unreferenced.append(jf)

    print(f"根目录 .json 总数: {len(json_files)}")
    print(f"零精确引用（候选归档，需人工确认）: {len(unreferenced)}")
    print("\n⚠️  以下仅零引用，不代表可安全归档——账户/风控/实盘/配置/缓存类【禁止移动】：")
    for jf in unreferenced:
        print(f"  {jf}")
    print("\n提示：仅归档明确实验输出（ga_*/oos_*/v51_* 等），其余保守保留。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="运行时依赖扫描器（防深度清理误移）")
    ap.add_argument("--json", action="store_true", help="扫 .json 引用（列出候选归档）")
    ap.add_argument("--restore", action="store_true", help="自动恢复缺失的 .py 依赖")
    args = ap.parse_args()

    if args.json:
        sys.exit(scan_json())
    sys.exit(scan_py(restore=args.restore))


if __name__ == "__main__":
    main()
