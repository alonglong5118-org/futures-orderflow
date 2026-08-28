#!/usr/bin/env python3
"""
版本号管理脚本
用法:
    python3 bump_version.py [major|minor|patch] [描述]

示例:
    python3 bump_version.py patch "修复价格保护逻辑"
    python3 bump_version.py minor "新增账户追踪器同步"
    python3 bump_version.py major "架构重构"
"""

import datetime
import re
import sys

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))


def read_file(path):
    with open(path, "r") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def get_current_version():
    content = read_file(f"{HERE}/four_dim_live_runner.py")
    match = re.search(r'APP_VERSION = "v(\d+)\.(\d+)\.(\d+)"', content)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def bump_version(level, description=""):
    """升级版本号"""
    current = get_current_version()
    if not current:
        print("❌ 无法读取当前版本号")
        sys.exit(1)

    major, minor, patch = current

    if level == "major":
        major += 1
        minor = 0
        patch = 0
    elif level == "minor":
        minor += 1
        patch = 0
    elif level == "patch":
        patch += 1
    else:
        print(f"❌ 未知的版本级别: {level}")
        print("   可选值: major, minor, patch")
        sys.exit(1)

    new_version = f"v{major}.{minor}.{patch}"
    cache_tag = f"{new_version} ({description})" if description else new_version

    # 更新 four_dim_live_runner.py
    runner = read_file(f"{HERE}/four_dim_live_runner.py")
    runner = re.sub(r'APP_VERSION = "v\d+\.\d+\.\d+"', f'APP_VERSION = "{new_version}"', runner)
    write_file(f"{HERE}/four_dim_live_runner.py", runner)
    print(f"✅ four_dim_live_runner.py: {new_version}")

    # 更新 four_dim_live.html
    html = read_file(f"{HERE}/four_dim_live.html")
    html = re.sub(r'id="navver">v[^<]+', f'id="navver">{cache_tag}', html)
    write_file(f"{HERE}/four_dim_live.html", html)
    print(f"✅ four_dim_live.html: {cache_tag}")

    # 更新 sw.js 缓存版本
    sw = read_file(f"{HERE}/sw.js")
    match = re.search(r"four-dim-v(\d+)", sw)
    if match:
        cache_ver = int(match.group(1)) + 1
        sw = re.sub(r"four-dim-v\d+", f"four-dim-v{cache_ver}", sw)
        write_file(f"{HERE}/sw.js", sw)
        print(f"✅ sw.js: 缓存版本 → four-dim-v{cache_ver}")

    print(f"\n📊 版本升级: v{current[0]}.{current[1]}.{current[2]} → {new_version}")
    if description:
        print(f"📝 更新说明: {description}")
    print(f"🕐 更新时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def show_version():
    """显示当前版本号"""
    current = get_current_version()
    if current:
        print(f"当前版本: v{current[0]}.{current[1]}.{current[2]}")
    else:
        print("❌ 无法读取版本号")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("当前文件中的版本:")
        show_version()
        sys.exit(0)

    if sys.argv[1] == "show":
        show_version()
        sys.exit(0)

    level = sys.argv[1]
    description = sys.argv[2] if len(sys.argv) > 2 else ""

    if level not in ("major", "minor", "patch"):
        print(f"❌ 无效的版本级别: {level}")
        print("   可选值: major, minor, patch, show")
        sys.exit(1)

    bump_version(level, description)
