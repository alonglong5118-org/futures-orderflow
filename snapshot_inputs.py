#!/usr/bin/env python3
"""内容寻址快照库：固化 M2 引擎的可变未入库输入（fundamentals.json / info_dimension.json）。

为什么：这两个文件是 four_dim_strategy.py 的 F/I 维度输入，每日盘前刷新、gitignore、从未入库，
导致任何回测天然不可复现。本工具按 sha256 内容寻址存到 snapshots/，使每次回测能精确定位当时输入。

用法：
  python snapshot_inputs.py snapshot   快照当前两文件 -> snapshots/<stem>_<hash16>.json + 更新 MANIFEST
  python snapshot_inputs.py status     显示 live 文件 hash 与已存快照清单
  python snapshot_inputs.py restore <hash16> [fundamentals|info_dimension]  还原某快照到 live 路径
  python snapshot_inputs.py manifest   打印 MANIFEST

约定：
  - 快照文件按内容寻址、幂等（同 hash 不重复拷贝）
  - snapshots/ 整体 gitignore（重文件不入库），仅 MANIFEST.json 可入库做跨会话追溯
  - 回测产物应记录 snapshot() 返回的 hash，复现时先 restore 再跑
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAP_DIR = ROOT / "snapshots"
MANIFEST = SNAP_DIR / "MANIFEST.json"
FILES = {
    "fundamentals": ROOT / "fundamentals.json",
    "info_dimension": ROOT / "info_dimension.json",
}
HASH_LEN = 16


def sha256_of(path: Path, n: int = HASH_LEN) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def _snap_one(name: str) -> dict:
    live = FILES[name]
    if not live.exists():
        return {"name": name, "present": False}
    h = sha256_of(live)
    dest = SNAP_DIR / f"{name}_{h}.json"
    if not dest.exists():
        shutil.copy2(live, dest)
    return {"name": name, "present": True, "hash": h, "bytes": live.stat().st_size, "snapshot": dest.name}


def snapshot_all() -> dict:
    if not SNAP_DIR.is_dir():
        SNAP_DIR.mkdir(parents=True)
    result = {name: _snap_one(name) for name in FILES}
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "fundamentals": result["fundamentals"].get("hash"),
        "info_dimension": result["info_dimension"].get("hash"),
        "fundamentals_bytes": result["fundamentals"].get("bytes"),
        "info_dimension_bytes": result["info_dimension"].get("bytes"),
    }
    rows = read_manifest()
    rows.append(entry)
    write_manifest(rows)
    return {k: v.get("hash") for k, v in result.items() if v.get("present")}


def read_manifest() -> list:
    if not MANIFEST.exists():
        return []
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return []


def write_manifest(rows: list) -> None:
    MANIFEST.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def restore(hash16: str, which: str | None = None) -> None:
    targets = [which] if which else list(FILES)
    for name in targets:
        if name not in FILES:
            print(f"[restore] 未知名称: {name}（可选: {', '.join(FILES)}）")
            continue
        cand = sorted(SNAP_DIR.glob(f"{name}_{hash16}*.json"))
        if not cand:
            print(f"[restore] 未找到 {name} 的快照以 {hash16} 开头")
            continue
        src = cand[0]
        shutil.copy2(src, FILES[name])
        print(f"[restore] {name}.json <- {src.name}")


def status() -> None:
    print(f"snapshots dir: {SNAP_DIR} (exists={SNAP_DIR.exists()})")
    for name, live in FILES.items():
        if live.exists():
            print(f"  live {name:14s} sha256[:{HASH_LEN}]={sha256_of(live)}  {live.stat().st_size} bytes")
        else:
            print(f"  live {name:14s} 缺失")
    rows = read_manifest()
    print(f"MANIFEST 条目数: {len(rows)}")
    if rows:
        last = rows[-1]
        print(
            f"  最近: ts={last.get('ts')} fundamentals={last.get('fundamentals')} info_dimension={last.get('info_dimension')}"
        )


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "snapshot":
        r = snapshot_all()
        print("snapshot 完成:")
        for k, v in r.items():
            print(f"  {k} = {v}")
        print(f"  MANIFEST 条目: {len(read_manifest())}")
    elif cmd == "status":
        status()
    elif cmd == "restore":
        if len(argv) < 3:
            print("用法: snapshot_inputs.py restore <hash16> [fundamentals|info_dimension]")
            return 1
        restore(argv[2], argv[3] if len(argv) > 3 else None)
    elif cmd == "manifest":
        print(json.dumps(read_manifest(), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
