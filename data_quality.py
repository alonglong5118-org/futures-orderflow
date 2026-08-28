# -*- coding: utf-8 -*-
"""四维策略 · 数据质量 / 陈旧监控（#14）
==========================================
信号再准，喂进去的行情是“假”的也白搭：断流、冻结（价格卡死不动）、
涨停/跌停焊死、跳变（错误 tick）。这些藏在面板背后，肉眼难察觉，
却会让策略在“错误的事实”上做决定。

本模块自我追踪每个品种行情的“最后见到时间”与“最后价格”，每轮 poll 后调用
observe() 更新；check() 给出每个品种的健康状态：
    正常 / 陈旧(太久没更新) / 冻结(价格连续N次不变) / 异常(价格≤0或跳变)
并汇总整体数据健康度、需要警惕的品种清单。

纯本地、无外部依赖；runner 每轮 _poll_feed 后调用 observe，_update_aux 里调用 check。

用法：
    import data_quality as dq
    dq.observe(FEED)                 # 每轮行情后
    rep = dq.check()                 # 取健康快照
"""
from __future__ import annotations

import time
from datetime import datetime

try:
    from four_dim_strategy import SYMBOLS
except Exception:
    SYMBOLS = {}

STALE_TRADING_SEC = 120      # 交易时段：超过 120s 没更新 → 陈旧
STALE_IDLE_SEC = 600         # 非交易时段：超过 600s → 陈旧
FROZEN_N = 6                 # 价格连续不变次数（≈ 每轮~数秒，6次≈半分钟卡死）
JUMP_RATIO = 0.05            # 相邻价格跳变超过 5% → 异常跳变（疑似错误 tick）

_last_seen = {}              # sym -> wall ts
_last_price = {}             # sym -> price
_frozen = {}                 # sym -> 连续不变计数
_prev_price = {}             # sym -> 上轮价格（用于跳变检测）
_ever_seen = set()


def observe(feed, now_ts=None):
    """每轮 poll 后调用：记录每个有价格品种的最后见到时间，并检测冻结/跳变。"""
    now_ts = now_ts or time.time()
    syms = list(SYMBOLS.keys())
    for sym in syms:
        try:
            p = feed.price(sym)
        except Exception:
            p = None
        if p is None or p <= 0:
            continue
        _ever_seen.add(sym)
        _last_seen[sym] = now_ts
        # 冻结检测
        if sym in _last_price and _last_price[sym] == p:
            _frozen[sym] = _frozen.get(sym, 0) + 1
        else:
            _frozen[sym] = 0
        # 跳变检测
        pp = _prev_price.get(sym)
        if pp and pp > 0:
            ratio = abs(p - pp) / pp
            if ratio > JUMP_RATIO:
                # 记一次跳变（不长期留存，仅本轮状态里体现）
                _frozen[sym] = _frozen.get(sym, 0)  # 占位，跳变走 _jumps
                _jumps[sym] = _jumps.get(sym, 0) + 1
        _prev_price[sym] = p
        _last_price[sym] = p


_jumps = {}                  # sym -> 最近累计跳变（观察窗口内）


def check(now_ts=None, trading=True):
    now_ts = now_ts or time.time()
    stale_sec = STALE_TRADING_SEC if trading else STALE_IDLE_SEC
    rows = []
    stale_list, frozen_list, bad_list, jump_list = [], [], [], []
    for sym in SYMBOLS:
        name = SYMBOLS[sym].get("name", sym)
        if sym not in _ever_seen:
            st = "未订阅"
            age = None
        else:
            age = now_ts - _last_seen.get(sym, 0)
            price = _last_price.get(sym)
            if price is None or price <= 0:
                st = "异常"
                bad_list.append(sym)
            elif age > stale_sec:
                st = "陈旧"
                stale_list.append(sym)
            elif _frozen.get(sym, 0) >= FROZEN_N:
                st = "冻结"
                frozen_list.append(sym)
            elif _jumps.get(sym, 0) >= 3:
                st = "跳变"
                jump_list.append(sym)
            else:
                st = "正常"
        rows.append({
            "symbol": sym, "name": name, "status": st,
            "age_sec": round(age, 1) if age is not None else None,
            "price": _last_price.get(sym),
            "frozen_n": _frozen.get(sym, 0),
        })
    tracked = [r for r in rows if r["status"] != "未订阅"]
    n_ok = sum(1 for r in tracked if r["status"] == "正常")
    health = round(n_ok / len(tracked) * 100, 1) if tracked else 0.0
    worst = []
    for r in rows:
        if r["status"] in ("陈旧", "冻结", "异常", "跳变"):
            worst.append(r)
    return {
        "ok": True,
        "health_pct": health,
        "trading": trading,
        "stale_sec": stale_sec,
        "counts": {
            "正常": n_ok,
            "陈旧": len(stale_list),
            "冻结": len(frozen_list),
            "异常": len(bad_list),
            "跳变": len(jump_list),
            "未订阅": sum(1 for r in rows if r["status"] == "未订阅"),
        },
        "stale": stale_list,
        "frozen": frozen_list,
        "bad": bad_list,
        "jumps": jump_list,
        "worst": sorted(worst, key=lambda r: r["status"] != "异常"),
        "rows": rows,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def reset_jumps():
    """观察窗口结束后清空跳变计数（调用方在 check 后定期清）。"""
    _jumps.clear()


def print_report(rep):
    print("=" * 56)
    print(f"数据质量 · 健康度 {rep['health_pct']}% · 交易时段={rep['trading']}")
    c = rep["counts"]
    print(f"  正常{c['正常']} 陈旧{c['陈旧']} 冻结{c['冻结']} 异常{c['异常']} 跳变{c['跳变']} 未订阅{c['未订阅']}")
    if rep["worst"]:
        for r in rep["worst"][:10]:
            print(f"  ⚠️ {r['symbol']}({r['name']}) {r['status']} "
                  f"age={r['age_sec']}s price={r['price']}")
    else:
        print("  ✅ 全部品种数据正常")
    print("=" * 56)


if __name__ == "__main__":
    # 自测：造一个假 feed
    class FakeFeed:
        def __init__(self, d): self.d = d
        def price(self, s): return self.d.get(s)
    f = FakeFeed({"FG": 1500.0, "SA": 2000.0, "JM": 1700.0, "J": 2300.0,
                  "jd": 4000.0, "lh": 18000.0})
    observe(f)
    time.sleep(0.1)
    observe(f)
    print_report(check(trading=False))
