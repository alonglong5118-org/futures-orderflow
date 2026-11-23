"""主循环周期探针 —— 三感参谋 B·P0 的**埋点前置件**

状态：**只读**。不改 runner、不改状态文件，只通过 /api/health 观测
`last_cycle_ago_sec`，用「数值回落」推出一圈的真实耗时。

## 为什么先做这个

B 的方案是「先埋点、再调度」——没有每轮耗时分解，改完调度无法证明有效。
但在改代码之前，还有一个更便宜的前置动作：**连续采样，把周期分布钉死**。
这个脚本就是那件事，且零风险，11-09 前后都能跑。

## 用法

    # 采 10 分钟，2 秒一次
    python3 cycle_probe.py --minutes 10

    # 长时间基线（建议放自动化，每 30 分钟一次，跑一天看 p95）
    python3 cycle_probe.py --minutes 60 --append

输出：stdout 摘要 + `cycle_history.jsonl`（每次运行追加一行）
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "cycle_history.jsonl")
DEFAULT_HEALTH = "http://127.0.0.1:8741/api/health"


def _get(url: str, timeout: float = 6.0) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def sample(minutes: float, interval: float, url: str) -> Dict[str, Any]:
    deadline = time.time() + minutes * 60
    prev: Optional[float] = None
    cycles: List[float] = []
    max_seen = 0.0
    misses = 0
    samples = 0
    t0 = time.time()

    while time.time() < deadline:
        d = _get(url)
        if not d or not d.get("ok"):
            misses += 1
            time.sleep(interval)
            continue
        samples += 1
        v = d.get("last_cycle_ago_sec")
        if not isinstance(v, (int, float)):
            time.sleep(interval)
            continue
        v = float(v)
        max_seen = max(max_seen, v)
        # 数值回落 = 一圈完成，回落前的值就是该圈周期
        if prev is not None and v < prev:
            cycles.append(prev)
        prev = v
        time.sleep(interval)

    stats: Dict[str, Any] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_sec": round(time.time() - t0, 1),
        "interval_sec": interval,
        "samples": samples,
        "misses": misses,
        "cycles_n": len(cycles),
        "cycles": [round(c, 1) for c in cycles],
        "max_cycle_ago": round(max_seen, 1),
    }
    if cycles:
        stats.update(
            {
                "p50": round(statistics.median(cycles), 1),
                "min": round(min(cycles), 1),
                "max": round(max(cycles), 1),
                "p95": round(sorted(cycles)[max(0, int(len(cycles) * 0.95) - 1)], 1),
            }
        )

    # 分级：与看门狗 180s 自杀线的余量
    worst = max(cycles) if cycles else max_seen
    if worst >= 175:
        grade, note = "🔴🔴", "随时可能被 _stall_watchdog 杀死"
    elif worst >= 150:
        grade, note = "🔴", f"距自杀线仅 {180 - worst:.0f}s"
    elif worst >= 100:
        grade, note = "🟡", f"余量 {(180 - worst) / 180 * 100:.0f}%，需盯"
    else:
        grade, note = "🟢", "健康"
    stats.update({"grade": grade, "note": note})
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主循环周期只读探针")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--url", default=DEFAULT_HEALTH)
    ap.add_argument("--append", action="store_true", help="追加写入 cycle_history.jsonl")
    args = ap.parse_args(argv)

    s = sample(args.minutes, args.interval, args.url)
    if args.append:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in s.items() if k != "cycles"},
                               ensure_ascii=False) + "\n")

    print(f"[{s['grade']}] {s['note']}")
    print(f"采样 {s['samples']} 次 / {s['window_sec']:.0f}s，抓到 {s['cycles_n']} 个完整周期")
    if s["cycles_n"]:
        print(f"周期 p50={s['p50']}s  min={s['min']}s  p95={s['p95']}s  max={s['max']}s")
        print(f"序列: {s['cycles']}")
    else:
        print(f"窗口内无一次回落 ⇒ 单圈 >{s['window_sec']:.0f}s，"
              f"实测最大 cycle_ago={s['max_cycle_ago']}s")
    if s["misses"]:
        print(f"⚠️ {s['misses']} 次探测无响应（runner 卡住或重启中）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
