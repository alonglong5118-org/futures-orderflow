#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
da龘 开盘前一键巡检 (preflight_check.py)
=======================================
开盘/夜盘前跑一下，把所有数据源、信号、通知、作战卡状态列成清单：
  🟢 就绪   🟡 待处理/待开盘   🔴 异常(需处理)

用法:
  python3 preflight_check.py                 # 终端彩色输出
  python3 preflight_check.py --no-color      # 纯文本(供 automation / 日志)
  python3 preflight_check.py --strict        # 交易时段严格模式: CTP未连/实时tick缺失判为 ❌
  python3 preflight_check.py --notify        # 额外触发一次 /api/test_alert 验证通知通道(会响铃+弹横幅)

退出码: 0=全绿  1=有黄(待处理)  2=有红(异常)
"""
import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.request

HOST_DEFAULT = "127.0.0.1:8731"

# ---- 中国期货 2026 年休市日（周末自动休 + 以下法定节假日区间）----
# 期货规则：周末休市，且国家法定节假日休市；补班周末(调休)期货仍休市。
# 每年初按国务院当年放假安排更新此表即可。
_HOLIDAY_RANGES_2026 = [
    ("2026-01-01", "2026-01-03"),   # 元旦
    ("2026-02-16", "2026-02-22"),   # 春节
    ("2026-04-04", "2026-04-06"),   # 清明
    ("2026-05-01", "2026-05-05"),   # 劳动
    ("2026-06-19", "2026-06-21"),   # 端午
    ("2026-09-25", "2026-09-27"),   # 中秋
    ("2026-10-01", "2026-10-07"),   # 国庆
]


def _build_holiday_set():
    s = set()
    for a, b in _HOLIDAY_RANGES_2026:
        d = datetime.date.fromisoformat(a)
        end = datetime.date.fromisoformat(b)
        while d <= end:
            s.add(d.isoformat())
            d += datetime.timedelta(days=1)
    return s


HOLIDAY_SET_2026 = _build_holiday_set()


def is_trading_day(d=None):
    """期货交易日 = 周一~周五 且 非法定节假日（不调休）。"""
    d = d or datetime.date.today()
    if d.weekday() >= 5:            # 5=周六 6=周日
        return False
    if d.isoformat() in HOLIDAY_SET_2026:
        return False
    return True
MAIN6 = ["FG", "JM", "SA", "J", "jd", "lh"]
POOL_ONLY = ["RM", "CF", "V", "RB", "UR", "P", "PF", "HC"]

# ---- 颜色 ----
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_DIM = "\033[90m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"


def now_ts():
    return time.time()


def parse_ts(ts):
    """兼容秒/毫秒时间戳 -> 秒"""
    if not ts:
        return 0
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return 0
    if t > 1e12:
        t /= 1000.0
    return t


def fetch(host):
    url = f"http://{host}/api/signals"
    req = urllib.request.Request(url, headers={"User-Agent": "preflight"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST_DEFAULT)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="强制巡检(忽略交易日判断, 用于测试/特殊日期)")
    args = ap.parse_args()

    # 交易日判断：休市日(周末/法定节假日)直接跳过并静默退出
    if not args.force:
        today = datetime.date.today()
        if not is_trading_day(today):
            print()
            print("🟡 今天(%s) 非期货交易日（周末/法定节假日），跳过巡检。" % today.isoformat())
            print("   如需强制检查请加 --force")
            print()
            sys.exit(0)

    use_color = (not args.no_color) and sys.stdout.isatty()
    g = (lambda s: s) if use_color else (lambda s: "")

    report = []  # (level, title, detail)  level: 0绿 1黄 2红

    def add(level, title, detail=""):
        report.append((level, title, detail))

    # ---------- 1. 后端可达 ----------
    try:
        d = fetch(args.host)
        add(0, "后端进程", f"http://{args.host} 在线, server_time={d.get('server_time')}")
    except urllib.error.URLError as e:
        add(2, "后端进程", f"无法连接 http://{args.host} — 请先 `bash start.sh` 启动后端")
        _finish(report, use_color, g, strict=args.strict)
        return 2
    except Exception as e:
        add(2, "后端进程", f"请求异常: {e}")
        _finish(report, use_color, g, strict=args.strict)
        return 2

    # ---------- 2. 天勤行情连接 ----------
    conn = d.get("connected")
    if conn:
        add(0, "天勤实时行情", d.get("conn_msg", "已连接"))
    else:
        add(2, "天勤实时行情", d.get("conn_msg", "未连接") + " — 检查 tq_config.json 账号")

    # ---------- 3. 交易时段 ----------
    live = d.get("live")
    sess = d.get("session", "未知")
    if live:
        add(0, "交易时段", f"{sess} (live)")
    else:
        add(1, "交易时段", f"当前 {sess} (非交易时段, 实时tick/信号待开盘后生效)")

    # ---------- 4. CTP 真tick (大商所 JM/J/jd/lh) ----------
    ctp = d.get("ctp_tick", {}) or {}
    ctp_conn = ctp.get("connected")
    covered = ctp.get("covered", []) or []
    last = ctp.get("last", {}) or {}
    if ctp_conn:
        fresh = []
        stale = []
        for sym in covered:
            age = now_ts() - parse_ts(last.get(sym, 0))
            (fresh if age < 15 else stale).append(f"{sym}({age:.0f}s)")
        if not stale:
            add(0, "CTP真tick网关", f"已连接 {ctp.get('gateway')} | 回流: {', '.join(fresh)}")
        else:
            lvl = 2 if args.strict else 1
            add(lvl, "CTP真tick网关", f"已连接但回流延迟: 新鲜={', '.join(fresh) or '无'} 延迟={', '.join(stale)}")
    else:
        lvl = 2 if args.strict else 1
        add(lvl, "CTP真tick网关",
            f"{ctp.get('msg','未连接')} — 唤醒 Windows 虚拟机并启动 ctp_gateway.py (1011修复已就位, 连上即回流)")

    # ---------- 5. 主6品种 战略层(日线)就绪 ----------
    syms = d.get("symbols", {}) or {}
    miss_strat = [s for s in MAIN6 if not (syms.get(s, {}) or {}).get("strategy")]
    if not miss_strat:
        add(0, "主6品种·战略层", "FG/JM/SA/J/jd/lh 日线策略已全部就绪")
    else:
        add(2, "主6品种·战略层", f"日线未填充: {', '.join(miss_strat)} — 等待策略层首跑(冷启动≤40s)")

    # ---------- 6. pool_only 8品种 日线 + 实时 ----------
    pool_ready = []
    pool_noday = []
    for s in POOL_ONLY:
        rec = syms.get(s, {}) or {}
        if rec.get("strategy"):
            pool_ready.append(s)
        else:
            pool_noday.append(s)
    if pool_noday and live:
        add(1, "稳健池8品种·日线", f"实时tick已订阅但日线缺失: {', '.join(pool_noday)} (开盘后自动补齐)")
    elif pool_noday:
        add(1, "稳健池8品种·日线", f"周末无日线(预期): {', '.join(pool_noday)} — 开盘自动补齐; 已就绪={len(pool_ready)}/8")
    else:
        add(0, "稳健池8品种·日线", f"RM/CF/V/RB/UR/P/PF/HC 全部就绪 ({len(pool_ready)}/8)")

    # 实时tick接入(仅live时严格)
    if live:
        pool_no_sig = [s for s in POOL_ONLY if not (syms.get(s, {}) or {}).get("signal")]
        if pool_no_sig:
            add(1, "稳健池8品种·实时tick", f"实时tick未回流: {', '.join(pool_no_sig)} — 检查天勤 KQ.m 主连订阅")
        else:
            add(0, "稳健池8品种·实时tick", "全部品种实时tick已接入")

    # ---------- 7. 作战卡 + LLM ----------
    cards = d.get("cards", {}) or {}
    llm_on = sum(1 for v in cards.values() if v.get("llm"))
    if cards:
        add(0, "作战卡", f"已生成 {len(cards)} 张 | 云端LLM叙事 {llm_on} 张 (DeepSeek)")
    else:
        add(1, "作战卡", "当前无品种生成作战卡(周末信号弱/无strategy+signal) — 开盘后自动生成")

    # ---------- 8. 风控状态机 ----------
    rs = d.get("risk_state", {}) or {}
    state = rs.get("state", "未知")
    scale = rs.get("scale", 1.0)
    if state == "NORMAL":
        add(0, "风控状态机", f"{state} · 仓位缩放×{scale} (正常)")
    elif state == "WARNING":
        add(1, "风控状态机", f"{state} · 仓位缩放×{scale} (连续止损/回撤, 谨慎)")
    else:
        add(2, "风控状态机", f"{state} · 仓位缩放×{scale} (锁单, 禁新开)")

    # ---------- 9. 通知通道 (可选触发) ----------
    if args.notify:
        try:
            url = f"http://{args.host}/api/test_alert"
            req = urllib.request.Request(url, method="POST",
                                         headers={"Content-Type": "application/json"},
                                         data=b"{}")
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode("utf-8"))
            add(0 if j.get("ok") else 2, "通知通道",
                "已触发 test_alert (红横幅+语音+响铃应出现)" if j.get("ok")
                else f"test_alert 返回: {j}")
        except Exception as e:
            add(2, "通知通道", f"test_alert 调用失败: {e}")
    else:
        add(1, "通知通道", "未触发(加 --notify 可在开盘前验证 红横幅/语音/响铃)")

    # ---------- 汇总 ----------
    _finish(report, use_color, g, strict=args.strict)
    reds = sum(1 for r in report if r[0] == 2)
    yellows = sum(1 for r in report if r[0] == 1)
    if reds:
        return 2
    if yellows:
        return 1
    return 0


def _finish(report, use_color, g, strict=False):
    icon = {0: "🟢", 1: "🟡", 2: "🔴"}
    col = {0: C_GREEN, 1: C_YELLOW, 2: C_RED}
    print()
    print(g(C_BOLD) + "========== da龘 开盘前巡检报告 ==========" + g(C_RESET))
    for level, title, detail in report:
        line = f"  {icon[level]} {title}"
        if detail:
            line += f" — {detail}"
        if use_color:
            print(col[level] + line + C_RESET)
        else:
            print(line)
    reds = sum(1 for r in report if r[0] == 2)
    yellows = sum(1 for r in report if r[0] == 1)
    if reds:
        verdict = "ERR"
    elif yellows:
        verdict = "WARN"
    else:
        verdict = "OK"
    # 严格模式下黄也算ERR? 这里黄=待处理不算错误, 仅红算错误
    print(g(C_BOLD) + f"----------------------------------------" + g(C_RESET))
    print(g(C_BOLD) + f"SUMMARY: {verdict}  (红={reds} 黄={yellows})" + g(C_RESET))
    print()


if __name__ == "__main__":
    sys.exit(main())
