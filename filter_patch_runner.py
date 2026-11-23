#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按内容标记生成过滤补丁：只把「我的 hunk」放进暂存区，不碰他人并行改动。

背景（2026-10-15实测）：four_dim_live_runner.py 同时含
  · 我的改动：自动对账降噪（_LAST_HEAL_SIG）、引擎仓同步（_sea）
  · 他人并行开发：VWAP 微观惯性同源对齐（VWAP_TILT_N 等常量与全部引用）
若整文件 `git add`，会把他人未完成的 VWAP 工作一起提交，
违反铁律「共享工作区不得覆盖/夹带他人并行改动」。

做法：
  1. 以 HEAD 版本为基底，构造一个临时工作副本；
  2. 只把「含我的内容标记」的 hunk 应用上去；
  3. 用 git diff --no-index 生成补丁，git apply --cached 只更新暂存区。
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = "four_dim_live_runner.py"

# 我的内容标记（命中任一即认定该 hunk 属于我）
MY_MARKERS = (
    "_LAST_HEAL_SIG",
    "_LAST_HEAL_SIG_N",
    "sync_engine_to_account as _sea",
    "_sea.sync_engine_positions",
    "[引擎同步]",
    "同指纹只首次打印",
)


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main():
    # 仓库根 = 脚本所在目录（fourd_run 即 git 仓库工作树根）
    repo = HERE
    tmp = tempfile.mkdtemp(prefix="filterpatch_")
    base = os.path.join(tmp, TARGET)
    mine = os.path.join(tmp, "mine_" + TARGET)

    # 1) HEAD 版本作基底
    r = run(["git", "show", f"HEAD:{TARGET}"], cwd=repo)
    if r.returncode != 0:
        print("无法读取 HEAD 版本:", r.stderr[:200])
        return 1
    with open(base, "w", encoding="utf-8") as f:
        f.write(r.stdout)

    # 2) 取工作区真实改动，按 hunk 切分
    r = run(["git", "diff", "--", TARGET], cwd=repo)
    if r.returncode != 0 or not r.stdout.strip():
        print("工作区无改动")
        return 0
    hunks = re.split(r"(?m)^(?=@@ )", r.stdout)
    head, parts = hunks[0], [h for h in hunks[1:] if h.strip()]
    kept, dropped = [], []
    for h in parts:
        (kept if any(m in h for m in MY_MARKERS) else dropped).append(h)

    print(f"总 hunk: {len(parts)}  | 属于我: {len(kept)}  | 他人: {len(dropped)}")
    for h in dropped:
        first = next((x for x in h.splitlines() if x.startswith("+")), "")
        print(f"  [他人·保留未提交] {first[:88]}")

    if not kept:
        print("无属于我的 hunk")
        return 0

    # 3) 组装 mine 文件
    with open(base, encoding="utf-8") as f:
        base_lines = f.readlines()
    out, pos = [], 0
    for h in kept:
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", h)
        old_start = int(m.group(1))
        out.extend(base_lines[pos:old_start - 1])
        for line in h.splitlines()[1:]:
            if line.startswith(("+", "-")):
                if line.startswith("-"):
                    pos += 1
                else:
                    out.append(line[1:] + "\n")
            elif line.startswith(" "):
                out.append(line[1:] + "\n")
                pos += 1
        pos = max(pos, old_start - 1 + int(m.group(2) or 1))
    out.extend(base_lines[pos:])
    with open(mine, "w", encoding="utf-8") as f:
        f.writelines(out)

    # 4) 生成并只应用到暂存区
    r = run(["diff", "-u", base, mine])
    patch = r.stdout
    # 修正补丁头为仓库相对路径
    patch = re.sub(r"^--- .*$", f"--- a/{TARGET}", patch, count=1, flags=re.M)
    patch = re.sub(r"^\+\+\+ .*$", f"+++ b/{TARGET}", patch, count=1, flags=re.M)

    proc = subprocess.run(
        ["git", "apply", "--cached", "--unidiff-zero", "-"],
        cwd=repo, input=patch, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # 退回标准模式重试
        proc = subprocess.run(["git", "apply", "--cached", "-"],
                              cwd=repo, input=patch, capture_output=True, text=True)
    print("apply --cached:", "OK" if proc.returncode == 0 else proc.stderr[:200])

    # 5) 审计暂存内容
    r = run(["git", "diff", "--cached", "--", TARGET], cwd=repo)
    added = [x for x in r.stdout.splitlines() if x.startswith("+") and not x.startswith("+++")]
    print(f"\n暂存新增 {len(added)} 行，逐行确认无 VWAP 等他人内容：")
    bad = [x for x in added if re.search(r"vwap|VWAP", x, re.I)]
    print("  含 VWAP 的行数:", len(bad), "→", "干净" if not bad else "夹带了！")
    for x in added:
        print("  ", x[:96])
    return 0 if proc.returncode == 0 and not bad else 1


if __name__ == "__main__":
    sys.exit(main())