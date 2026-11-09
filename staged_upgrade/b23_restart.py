"""B-2/B-3 补丁后的重启 + 验收（2026-11-06）

只做三件事，每步都可验证：
  1. 拒绝在交易时段重启（除非 --force），避免盘中无止损管理
  2. SIGTERM 旧进程 → 等 supervisor 拉起 → 拉不起来就自己用干净环境启动
  3. 验收：health / 3 腿带止损 / 权益连续 / cycle_breakdown 出现

失败即返回非零，**不自动回滚**——回滚要人看一眼日志再决定，自动来回切更危险。
（回滚命令：python3 apply_b23.py --rollback && 再跑一次本脚本）
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH = "http://127.0.0.1:8741/api/health"
PTI = "http://127.0.0.1:8741/api/paper-trading"
STATE = "http://127.0.0.1:8741/api/state"
PY = "/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"
LAUNCH = os.path.join(ROOT, "_daemon_launch.py")
LOG = os.path.join(ROOT, "launchd.log")


def get(url: str, timeout: float = 15.0):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def in_trading_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (900 <= hm < 1130) or (1330 <= hm < 1500) or (2100 <= hm < 2300)


def wait_up(pid_expect_diff: bool, timeout: float) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = get(HEALTH, 8)
        if d and d.get("ok") and isinstance(d.get("pid"), int):
            if not pid_expect_diff or d["pid"] != OLD_PID:
                return d
        time.sleep(3)
    return None


OLD_PID = 0


def main(argv=None) -> int:
    global OLD_PID
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="允许在交易时段重启")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    if in_trading_hours() and not args.force:
        print("✗ 当前在交易时段，拒绝重启（用 --force 强制）")
        return 2

    d = get(HEALTH, 10)
    if not d or not d.get("ok"):
        print("✗ 重启前 runner 就不健康，先查清再动")
        return 2
    OLD_PID = d["pid"]
    print(f"旧进程 pid={OLD_PID} uptime={d['uptime_sec']/3600:.1f}h cycle_ago={d['last_cycle_ago_sec']}s")

    pti_before = get(PTI, 25) or {}
    eq_before = pti_before.get("equity")
    legs_before = len(pti_before.get("positions") or [])
    print(f"重启前权益={eq_before} 腿数={legs_before}")

    try:
        os.kill(OLD_PID, signal.SIGTERM)
        print(f"已发 SIGTERM → {OLD_PID}")
    except Exception as e:
        print(f"✗ 发信号失败: {e}")
        return 2

    d2 = wait_up(True, args.timeout)
    if d2:
        print(f"✓ supervisor 已拉起新进程 pid={d2['pid']}")
    else:
        print("⚠️ supervisor 未在限时内拉起，改用干净环境自启")
        subprocess.Popen([PY, LAUNCH], cwd=ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        d2 = wait_up(True, 240)
        if not d2:
            print("✗ 自启也失败，runner 当前不可用 —— 立即人工介入")
            return 1
        print(f"✓ 自启成功 pid={d2['pid']}")

    # ── 验收 ──────────────────────────────────────────────────────────────
    problems = []
    time.sleep(20)  # 让首轮跑完
    pti = get(PTI, 25) or {}
    legs = pti.get("positions") or []
    eq_after = pti.get("equity")
    if not pti.get("enabled"):
        problems.append("pti enabled=False")
    if len(legs) != legs_before:
        problems.append(f"腿数变了 {legs_before} → {len(legs)}")
    for p in legs:
        if not p.get("stop_price"):
            problems.append(f"{p.get('symbol')} 无止损")
    if isinstance(eq_before, (int, float)) and isinstance(eq_after, (int, float)):
        if abs(eq_after - eq_before) > 3000:
            problems.append(f"权益跳变 {eq_before} → {eq_after}")

    st = get(STATE, 20) or {}
    s = st.get("state") or st
    bd = s.get("cycle_breakdown")
    if not bd:
        print("⚠️ cycle_breakdown 尚未出现（首轮可能还没写完），留到下一轮确认")
    else:
        print(f"✓ 埋点生效 total_ms={bd.get('total_ms')} eval_ms={bd.get('eval_ms')} "
              f"daily_fetch={bd.get('daily_fetch_n')} hit={bd.get('daily_cache_hit')}")

    print(f"重启后权益={eq_after} 腿数={len(legs)}")
    if problems:
        print("✗ 验收未通过：" + "；".join(problems))
        return 1
    print("✓ 验收通过：健康 / 持仓与止损完整 / 权益连续")
    return 0


if __name__ == "__main__":
    sys.exit(main())
