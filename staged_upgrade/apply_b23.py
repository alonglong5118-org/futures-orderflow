"""B-2 / B-3 / B 埋点 的最小改动补丁器（2026-11-06）

只改三处，全部是「加防护 + 加观测」，不动任何决策逻辑：
  B-2  日线抓取失败后的负缓存冷却（防每轮重跑整条抓取链）
  B-3  TTL 加确定性抖动（避免 43 品种同时过期）
  埋点 主循环耗时分解 → state["cycle_breakdown"] / cycle_history

## 用法

    python3 apply_b23.py --dry-run     # 在临时副本上试打，编译校验，不动真实文件
    python3 apply_b23.py --apply       # 备份 → 打补丁 → 编译 → 回归门禁
    python3 apply_b23.py --rollback    # 从 .bak_b23_<ts> 还原

`--apply` 成功后**不会自动重启**，需由调用方决定何时重启（收盘后）。

## 门槛判据（不是「全绿」）

`tests/test_baseline_regression.py` 在改动前就已经有 1 个失败：
`test_backtest_hc_baseline: trades=19 > max=16`。
所以门禁判据是「**失败集合与改动前一致**」，不是「零失败」。
硬写成"必须全绿"= 永远过不了的假门禁。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNER = os.path.join(ROOT, "four_dim_live_runner.py")
STRAT = os.path.join(ROOT, "four_dim_strategy.py")
PY = "/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"

# 改动前已知的失败用例（见模块 docstring）。门禁只要求「不要新增」。
KNOWN_FAIL_BEFORE = {"test_backtest_hc_baseline"}


# ── 补丁定义 ──────────────────────────────────────────────────────────────

PATCHES: List[Tuple[str, str, str, str]] = [
    # (文件, 名称, old, new)
    (
        STRAT,
        "B-2/B-3 常量与辅助函数",
        '_DAILY_CACHE = {}  # symbol -> (df, timestamp) 进程内缓存',
        '_DAILY_CACHE = {}  # symbol -> (df, timestamp) 进程内缓存\n'
        '# ── B-2 负缓存：抓取失败后的冷却，防止「每轮都重跑整条抓取链」────────────────\n'
        '# 背景（2026-11-06）：分支 2 原先只在抓取成功时写 _DAILY_CACHE，tqsdk 与免费源\n'
        '# 同时失败 ⇒ 缓存永不写入 ⇒ 每一轮都重试（tqsdk 单品种 45–70s 超时 × N 品种），\n'
        '# 主循环直接被顶到 180s 看门狗自杀线。11-04 23:39 与 11-06 10:52 两次自杀同源。\n'
        '_DAILY_FAIL_TS = {}  # symbol -> 最近一次抓取失败的时间戳\n'
        '_DAILY_FAIL_COOLDOWN = 300  # 失败冷却（秒），期间直接返回 None 不再重试\n'
        '# ── B-3 TTL 抖动：43 品种启动时集中加载 ⇒ 1800s 后同一轮集体过期、串行重拉 ──\n'
        '_DAILY_JITTER_SPAN = 450  # 抖动幅度（秒），把过期时刻摊到 ±7.5 分钟\n'
        '_DAILY_STAT = {"hit": 0, "fetch": 0}  # 埋点：命中/抓取计数（由 runner 每轮读取后清零）\n'
        '\n'
        '\n'
        'def _daily_ttl_jitter(symbol, span=None):\n'
        '    """按品种名生成**确定性** TTL 抖动（秒）。\n'
        '\n'
        '    用 zlib.crc32 而不是内置 hash()：后者受 PYTHONHASHSEED 随机化影响，\n'
        '    同一品种在不同进程里抖动值不同，实验不可复现。\n'
        '    """\n'
        '    import zlib\n'
        '\n'
        '    s = _DAILY_JITTER_SPAN if span is None else span\n'
        '    return int(zlib.crc32(str(symbol).encode("utf-8")) % (2 * s + 1)) - s',
    ),
    (
        STRAT,
        "B-3 命中判定加抖动 + B-2 冷却前置",
        '    cached = _DAILY_CACHE.get(symbol)\n'
        '    if cached and (_t.time() - cached[1]) < ttl:\n'
        '        return cached[0]',
        '    cached = _DAILY_CACHE.get(symbol)\n'
        '    if cached and (_t.time() - cached[1]) < (ttl + _daily_ttl_jitter(symbol)):\n'
        '        _DAILY_STAT["hit"] += 1\n'
        '        return cached[0]\n'
        '    # B-2：失败冷却期内直接放行（返回 None），不再重跑整条抓取链\n'
        '    _fts = _DAILY_FAIL_TS.get(symbol)\n'
        '    if _fts and (_t.time() - _fts) < _DAILY_FAIL_COOLDOWN:\n'
        '        return None',
    ),
    (
        STRAT,
        "B-2 结果落盘：成功写缓存、失败记负缓存",
        '            _DAILY_CACHE[symbol] = (df, _t.time())\n'
        '        except Exception as e:\n'
        '            print(f"  [daily refresh] {symbol} akshare 兜底失败: {e}")\n'
        '    return df',
        '            _DAILY_CACHE[symbol] = (df, _t.time())\n'
        '        except Exception as e:\n'
        '            print(f"  [daily refresh] {symbol} akshare 兜底失败: {e}")\n'
        '    # B-2：无论抓到没抓到都要落「结果时间戳」。抓到 → 进正常缓存；\n'
        '    # 抓不到 → 进负缓存冷却 300s，否则下一轮立刻重跑整条链。\n'
        '    if df is not None and len(df) > 0:\n'
        '        _DAILY_CACHE[symbol] = (df, _t.time())\n'
        '        _DAILY_FAIL_TS.pop(symbol, None)\n'
        '    else:\n'
        '        _DAILY_FAIL_TS[symbol] = _t.time()\n'
        '    return df',
    ),
    (
        RUNNER,
        "埋点：循环起点打点",
        '    while True:\n'
        '        now = datetime.now()\n'
        '        today = now.strftime("%Y%m%d")',
        '    while True:\n'
        '        _t_cycle0 = time.perf_counter()  # 埋点：本轮起点\n'
        '        _t_eval_done = None  # 埋点：evaluate 结束时刻（休市轮次为空）\n'
        '        now = datetime.now()\n'
        '        today = now.strftime("%Y%m%d")',
    ),
    (
        RUNNER,
        "埋点：evaluate 结束打点",
        '        if feed_ok and is_trading_now(now):\n'
        '            _poll_feed(feed)\n'
        '            evaluate(feed, today, last_fire, state, corr_histories)',
        '        if feed_ok and is_trading_now(now):\n'
        '            _poll_feed(feed)\n'
        '            evaluate(feed, today, last_fire, state, corr_histories)\n'
        '            _t_eval_done = time.perf_counter()  # 埋点：poll+evaluate 合计到此',
    ),
    (
        RUNNER,
        "埋点：本轮耗时写进 state",
        '        state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")',
        '        # ── B 埋点：本轮耗时分解（只读观测，不参与任何决策）──────────────\n'
        '        # 没有这个字段，任何「调度优化」都无法证明有效；warn 阈值：\n'
        '        # total>95s 说明循环体已经吃掉 35s 以上，距 180s 自杀线余量不足一半。\n'
        '        try:\n'
        '            _t_end = time.perf_counter()\n'
        '            _bd = {\n'
        '                "ts": now.strftime("%H:%M:%S"),\n'
        '                "total_ms": int((_t_end - _t_cycle0) * 1000),\n'
        '                "eval_ms": int((_t_eval_done - _t_cycle0) * 1000) if _t_eval_done else 0,\n'
        '                "other_ms": 0,\n'
        '                "sleep_sec": POLL_SEC,\n'
        '                "daily_fetch_n": int(fd._DAILY_STAT.get("fetch", 0)),\n'
        '                "daily_cache_hit": int(fd._DAILY_STAT.get("hit", 0)),\n'
        '            }\n'
        '            _bd["other_ms"] = max(0, _bd["total_ms"] - _bd["eval_ms"])\n'
        '            fd._DAILY_STAT["fetch"] = 0\n'
        '            fd._DAILY_STAT["hit"] = 0\n'
        '            state["cycle_breakdown"] = _bd\n'
        '            _hist = list(state.get("cycle_history") or [])\n'
        '            _hist.append(_bd)\n'
        '            state["cycle_history"] = _hist[-30:]\n'
        '        except Exception as _be:\n'
        '            state["cycle_breakdown_error"] = repr(_be)[:80]\n'
        '        state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")',
    ),
]


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def apply_patches(root: str, dry: bool) -> int:
    files = {
        "runner": os.path.join(root, "four_dim_live_runner.py"),
        "strat": os.path.join(root, "four_dim_strategy.py"),
    }
    texts = {}
    for key, path in files.items():
        if not os.path.exists(path):
            print(f"  ✗ 找不到 {path}")
            return 1
        texts[path] = _read(path)

    ok = 0
    for path, name, old, new in PATCHES:
        real = files["runner"] if path == RUNNER else files["strat"]
        t = texts[real]
        n = t.count(old)
        if n != 1:
            print(f"  ✗ [{name}] 匹配 {n} 次（要求恰好 1 次），中止 — 文件可能已被改动")
            return 1
        texts[real] = t.replace(old, new, 1)
        ok += 1
        print(f"  ✓ [{name}]")

    if dry:
        import tempfile

        tmp = tempfile.mkdtemp(prefix="b23_dry_")
        for key, path in files.items():
            real = files["runner"] if path == RUNNER else files["strat"]
            dst = os.path.join(tmp, os.path.basename(path))
            _write(dst, texts[real])
            r = subprocess.run([sys.executable, "-m", "py_compile", dst],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ✗ 编译失败 {os.path.basename(path)}: {r.stderr[-400:]}")
                return 1
            print(f"  ✓ 编译通过 {os.path.basename(path)}  ({len(texts[real].splitlines())} 行)")
        print(f"\nDRY-RUN 完成：{ok} 处补丁全部可打、可编译（真实文件未改动）")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    for key, path in files.items():
        real = files["runner"] if path == RUNNER else files["strat"]
        bak = f"{real}.bak_b23_{ts}"
        shutil.copy2(real, bak)
        print(f"  备份 {os.path.basename(bak)}")
    for key, path in files.items():
        real = files["runner"] if path == RUNNER else files["strat"]
        _write(real, texts[real])

    for key, path in files.items():
        r = subprocess.run([sys.executable, "-m", "py_compile", path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ✗ 编译失败，自动回滚: {r.stderr[-300:]}")
            rollback(ts)
            return 1
        print(f"  ✓ 编译通过 {os.path.basename(path)}")
    print(f"\nAPPLY 完成（备份后缀 .bak_b23_{ts}）。下一步：跑回归门禁。")
    return 0


def regression_gate() -> int:
    if not os.path.exists(PY):
        print(f"  ⚠️ 找不到项目解释器 {PY}，跳过门禁")
        return 0
    res = subprocess.run([PY, "-m", "pytest", "tests/test_baseline_regression.py",
                          "-q", "--tb=no"], cwd=ROOT, capture_output=True, text=True)
    out = (res.stdout or "") + (res.stderr or "")
    failed = set()
    for line in out.splitlines():
        if line.startswith("FAILED "):
            frag = line.split("::")[-1].split(" ")[0]
            failed.add(frag)
    new_fail = failed - KNOWN_FAIL_BEFORE
    print(f"  回归：失败 {sorted(failed) or '无'}")
    if new_fail:
        print(f"  ✗ 新增失败 {sorted(new_fail)} → 判定回归，必须回滚")
        return 1
    print("  ✓ 无新增失败（改动前已知失败仍为 " + ", ".join(sorted(KNOWN_FAIL_BEFORE)) + "）")
    return 0


def rollback(ts: str | None = None) -> int:
    ts = ts or ""
    import glob

    cands = sorted(glob.glob(os.path.join(ROOT, "four_dim_live_runner.py.bak_b23_*")))
    if not cands:
        print("  ✗ 找不到任何 .bak_b23_* 备份")
        return 1
    latest = cands[-1]
    suf = latest.rsplit(".bak_b23_", 1)[1]
    n = 0
    for name in ("four_dim_live_runner.py", "four_dim_strategy.py"):
        src = os.path.join(ROOT, f"{name}.bak_b23_{suf}")
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(ROOT, name))
            print(f"  还原 {name} ← {os.path.basename(src)}")
            n += 1
    print(f"回滚完成 {n} 个文件（后缀 {suf}）")
    return 0 if n else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="B-2/B-3/埋点 补丁器")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--gate", action="store_true", help="只跑回归门禁")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args(argv)

    if args.dry_run:
        return apply_patches(ROOT, dry=True)
    if args.apply:
        rc = apply_patches(ROOT, dry=False)
        if rc:
            return rc
        return regression_gate()
    if args.gate:
        return regression_gate()
    if args.rollback:
        return rollback()
    return 1


if __name__ == "__main__":
    sys.exit(main())
