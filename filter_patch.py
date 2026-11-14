#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按内容 marker 过滤 unified diff，只暂存属于指定任务的新增行（铁律：共享文件不整文件 add）。
用法: git diff -- <file> | filter_patch.py <marker1> <marker2> ... > out.patch"""
import sys

markers = sys.argv[1:]
lines = sys.stdin.read().split("\n")

# 定位 header 结束与各 hunk 边界
hunk_starts = [i for i, l in enumerate(lines) if l.startswith("@@")]
header = lines[: hunk_starts[0]] if hunk_starts else lines
bounds = hunk_starts + [len(lines)]

kept, dropped = [], []
for k in range(len(hunk_starts)):
    seg = lines[bounds[k]: bounds[k + 1]]
    body = [l for l in seg[1:] if l and l[0] in "+-"]
    text = "\n".join(body)
    if any(m in text for m in markers):
        kept.append(seg)
    else:
        dropped.append(seg)

out = header + [l for seg in kept for l in seg]
sys.stdout.write("\n".join(out).rstrip("\n") + "\n")
sys.stderr.write(
    f"keep {len(kept)} hunk / drop {len(dropped)} hunk\n"
)
for i, seg in enumerate(dropped):
    adds = [l for l in seg if l.startswith("+") and not l.startswith("+++")]
    sys.stderr.write(f"  [dropped] +{len(adds)} 行: {adds[0][:70] if adds else ''}\n")