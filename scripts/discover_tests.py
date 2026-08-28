#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动发现测试模块
==================

扫描 tests/ 目录下所有 test_*.py 文件，
自动生成 TEST_MODULES 字典，无需手动注册。

用法:
  python scripts/discover_tests.py           # 输出发现的模块列表
  python scripts/discover_tests.py --update  # 更新 run_tests.py 中的 TEST_MODULES
"""

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def discover_test_modules():
    """扫描 tests/ 目录，发现所有测试模块。"""
    test_dir = os.path.join(ROOT, "tests")
    pattern = os.path.join(test_dir, "test_*.py")
    files = sorted(glob.glob(pattern))

    modules = {}
    for fpath in files:
        fname = os.path.basename(fpath)
        # test_xxx.py → xxx
        mod_name = fname[5:-3]  # 去掉 "test_" 前缀和 ".py" 后缀
        mod_path = f"tests.test_{mod_name}"
        modules[mod_name] = mod_path

    return modules


def categorize_modules(modules):
    """根据模块名猜测分类。"""
    integration = set()
    advanced = set()
    skip_default = set()

    for name in modules:
        if "integration" in name:
            integration.add(name)
        if name in ("property_fuzz", "baseline_regression", "performance"):
            advanced.add(name)
        if name == "performance":
            skip_default.add(name)

    return integration, advanced, skip_default


def main():
    modules = discover_test_modules()
    integration, advanced, skip_default = categorize_modules(modules)

    print(f"发现 {len(modules)} 个测试模块:")
    print()

    for name in sorted(modules.keys()):
        tags = []
        if name in integration:
            tags.append("集成")
        if name in advanced:
            tags.append("高级")
        if name in skip_default:
            tags.append("默认跳过")
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  {name:<30} → {modules[name]}{tag_str}")

    print()
    print(f"  集成测试: {len(integration)} 个")
    print(f"  高级测试: {len(advanced)} 个")
    print(f"  默认跳过: {len(skip_default)} 个")
    print()

    if "--update" in sys.argv:
        update_run_tests(modules, integration, advanced, skip_default)
        print("✅ 已更新 run_tests.py")
        print()


def update_run_tests(modules, integration, advanced, skip_default):
    """更新 run_tests.py 中的 TEST_MODULES 和分类集合。"""
    run_tests_path = os.path.join(ROOT, "run_tests.py")
    with open(run_tests_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 生成新的 TEST_MODULES
    lines = ["TEST_MODULES = {"]
    for name in sorted(modules.keys()):
        lines.append(f'    "{name}": "{modules[name]}",')
    lines.append("}")
    new_test_modules = "\n".join(lines)

    # 替换 TEST_MODULES 块
    pattern = r"TEST_MODULES\s*=\s*\{[^}]*\}"
    content = re.sub(pattern, new_test_modules, content, count=1)

    # 更新分类集合
    # INTEGRATION_TESTS
    int_lines = ["INTEGRATION_TESTS = {"]
    for name in sorted(integration):
        int_lines.append(f'    "{name}",')
    int_lines.append("}")
    new_int = "\n".join(int_lines)
    content = re.sub(r"INTEGRATION_TESTS\s*=\s*\{[^}]*\}", new_int, content, count=1)

    # ADVANCED_TESTS
    adv_lines = ["ADVANCED_TESTS = {"]
    for name in sorted(advanced):
        adv_lines.append(f'    "{name}",')
    adv_lines.append("}")
    new_adv = "\n".join(adv_lines)
    content = re.sub(r"ADVANCED_TESTS\s*=\s*\{[^}]*\}", new_adv, content, count=1)

    # SKIP_BY_DEFAULT
    skip_lines = ["SKIP_BY_DEFAULT = {"]
    for name in sorted(skip_default):
        skip_lines.append(f'    "{name}",  # 性能测试：耗时 + 环境波动大，需显式运行')
    skip_lines.append("}")
    new_skip = "\n".join(skip_lines)
    content = re.sub(r"SKIP_BY_DEFAULT\s*=\s*\{[^}]*\}", new_skip, content, count=1)

    with open(run_tests_path, "w", encoding="utf-8") as f:
        f.write(content)


if __name__ == "__main__":
    main()
