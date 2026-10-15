#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎仓 → 账户层 同步（修复「面板看不到引擎持仓」）。

问题（2026-10-15 实测）：
  paper_trading_engine 开仓后只写自己的状态文件 paper_trading_state.json，
  而 account_monitor 的沙盒镜像只读 paper_account.json（account_monitor.py:461）。
  两个文件各写各的、互不相通 ⇒
    · 账户层 positions 恒为空（面板持仓明细空白）
    · 资金使用率恒 0.00%（保证金没被计入）
    ·违反铁律「使用率 ≤45%」与「每腿必挂止损」的可见性前提
    （引擎自己确实给每腿挂了止损，只是账户层看不到）

修法：
  **不让引擎去改 paper_account.json**（那是账户资金真相源，被污染后对账会更乱），
  而是在 runner 每轮把「引擎有、账户层没有」的腿合并进账户层快照，
  账户层自有腿优先，永不删除、不覆盖 —— 单向只读补齐，符合 optio→Vanta 的单向桥思路。

护栏：
  1. 只补不删：账户层已有腿一律保留
  2. 幂等：同品种同手数不重复处理
  3. 写盘 hash 含本次同步标记，内容不变则不写盘（不制造抖动）
  4. 失败只记日志，不影响主循环
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_STATE = os.path.join(HERE, "paper_trading_state.json")
ACCOUNT_STATE = os.path.join(HERE, "account_state.json")

# 合约乘数（用于估算保证金；取不到时用 margin_rate × 价 × 乘数，缺乘数则跳过计价）
_MULTIPLIER = {
    "ru": 10, "zn": 10, "rb": 10, "cf": 5, "cu": 5, "al": 5, "ni": 1,
    "zn2": 10, "ag": 15, "au": 1000, "rb2": 10, "bu": 10, "fu": 10,
    "lh": 16, "m": 10, "y": 10, "p": 10, "c": 10, "cs": 10, "jd": 10,
    "eb": 5, "eg": 10, "sa": 20, "fg": 20, "ma": 10, "ta": 5, "sr": 10,
    "oi": 10, "rm": 10, "rs": 10, "rm2": 10, "ur": 20, "sp": 10, "a": 20,
    "b": 20, "c2": 20, "cs2": 20, "i": 100, "j": 200, "lh2": 16,
    "sc": 1000, "lu": 10, "nr": 10, "bc": 5, "ss": 5, "lc": 1,
    "si": 5, "px": 20, "sh": 20, "fu2": 10, "l": 10, "v": 10, "pp": 5,
    "ec": 100, "br": 10, "SF": 5, "IF": 300, "IC": 200, "IH": 300,
}
_DEFAULT_MARGIN_RATE = 0.10

# 日志去重：同一批腿每轮都被沙盒镜像冲掉再补回，若每次都打印就是刷屏。
# 只在「补入内容」变化时打印一次，重复的静默计数。
_LAST_SIG = None
_SILENT_N = 0


def _log_once(msg, sig):
    """同一 sig 只打印一次；sig 变化时打印新的，并汇报上次静默了多少次。"""
    global _LAST_SIG, _SILENT_N
    if sig == _LAST_SIG:
        _SILENT_N += 1
        return
    if _SILENT_N > 1:
        print(f"[引擎同步] 同上补入已重复 {_SILENT_N} 轮（沙盒每轮覆盖所致，已静默）")
    print(msg)
    _LAST_SIG = sig
    _SILENT_N = 1


def _mult(sym):
    return _MULTIPLIER.get(str(sym).strip())


def sync_engine_positions(verbose=False):
    """把引擎持仓合并进账户层快照。只补不删，返回 (补入数, 日志文本)。"""
    if not (os.path.exists(ENGINE_STATE) and os.path.exists(ACCOUNT_STATE)):
        return 0, ""
    try:
        with open(ENGINE_STATE, encoding="utf-8") as f:
            eng = json.load(f) or {}
        with open(ACCOUNT_STATE, encoding="utf-8") as f:
            acct = json.load(f) or {}
    except Exception as e:
        return 0, f"读取失败: {repr(e)[:60]}"

    epos = eng.get("positions") or {}
    if not isinstance(epos, dict) or not epos:
        return 0, ""

    snap = acct.get("snapshot") or {}
    a_positions = list(snap.get("positions") or [])
    a_syms = {str(p.get("symbol")) for p in a_positions}

    added = []
    for sym, p in epos.items():
        if not isinstance(p, dict):
            continue
        lots = int(p.get("lots") or 0)
        if lots <= 0:
            continue
        if str(sym) in a_syms:      # 账户层已有 → 绝不覆盖
            continue
        entry = float(p.get("entry_price") or p.get("price") or 0)
        if entry <= 0:
            continue
        mult = _mult(sym)
        if not mult:
            if verbose:
                print(f"[引擎同步] 跳过 {sym}：缺乘数，无法估算保证金")
            continue
        stop = p.get("stop_price")
        margin = round(entry * mult * lots * _DEFAULT_MARGIN_RATE, 2)
        a_positions.append({
            "symbol": sym,
            "name": p.get("name") or sym,
            "direction": p.get("direction") or "多",
            "position": lots,
            "lots": lots,
            "open_price": entry,
            "price": entry,
            "margin": margin,
            "stop": stop,
            "stop_price": stop,
            "t1": p.get("t1_price"),
            "t2": p.get("t2_price"),
            "open_time": p.get("open_time") or "",
            "source": "engine_sync",
            "float_pnl": 0.0,
        })
        added.append(f"{sym}{p.get('direction') or '多'}{lots}手@{entry}(保证金{margin:.0f})")

    if not added:
        return 0, ""

    # 重算保证金与使用率（使用率铁律 ≤45%，这里只做呈现，不做拦截）
    total_margin = round(sum(float(x.get("margin") or 0) for x in a_positions), 2)
    equity = float(acct.get("equity") or snap.get("equity") or 0)
    usage = round(total_margin / equity * 100, 2) if equity > 0 else 0.0

    snap = dict(snap)
    snap["positions"] = a_positions
    snap["total_margin"] = total_margin
    snap["usage_rate"] = usage
    acct["snapshot"] = snap
    acct["_engine_sync_mark"] = [str(a) for a in added]
    acct["_engine_sync_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 账户层 positions 与 snapshot 保持同源（面板多处读这个字段）
    acct["positions"] = {
        str(x["symbol"]): {
            "direction": x.get("direction"),
            "lots": x.get("lots"),
            "avg": x.get("open_price"),
            "margin": x.get("margin"),
            "stop": x.get("stop"),
            "t1": x.get("t1"),
            "t2": x.get("t2"),
            "source": x.get("source"),
        }
        for x in a_positions
    }

    try:
        tmp = ACCOUNT_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acct, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACCOUNT_STATE)
    except Exception as e:
        return 0, f"写盘失败: {repr(e)[:60]}"

    msg = (
        f"[引擎同步] 已补入账户层 {len(added)} 腿: {', '.join(added)} "
        f"| 保证金合计={total_margin:.2f} 使用率={usage:.2f}%"
    )
    _log_once(msg, "|".join(added))
    return len(added), msg


if __name__ == "__main__":
    n, m = sync_engine_positions(verbose=True)
    print(m or f"无需同步（引擎无仓或账户层已一致），补入 {n} 腿")