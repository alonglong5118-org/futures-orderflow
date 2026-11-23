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
    """把引擎持仓与账户层对齐。

    2026-10-28 升级（Ken 铁律：「auto 就是你自行判断自行决定的所有单子……
    全部都是要联动实时更新的」）：
      对 **auto 来源（source == "engine_sync"）的腿做双向实时对齐**：
        ① 引擎新开            → 补入账户层；
        ② 引擎状态推进        → 实时刷新手数/止损/移动止损/浮盈（本次新增，核心）；
        ③ 引擎已平            → 账户层删除该腿（补齐「只补不删」的另一端）。
      对 **非 auto 来源的真实/手动腿，依旧严格不碰**（守住护栏本意：
        不污染账户资金真相源；无 source 标记的孤儿腿同样不删）。

    第二条解决的实际病灶：旧逻辑 L97 `if sym in a_syms: continue` 使补入过的腿被**永久冻结**，
      ⇒ 引擎每轮推进的移动止损线(trailing_stop)、t1 平半后的剩余手数、trailing_active 一律传不到面板，
      ⇒ 面板「移动止损」列长期不更新、止损位停在开仓瞬间。

    Returns: (变动腿数, 日志文本)
    """
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
    leg_of = {str(p.get("symbol")): p for p in a_positions}

    added = []
    updated = []
    # 引擎的有效手数：优先 remaining_lots（t1 平半后 lots 不变、remaining_lots 才减）
    def _alive_lots(p):
        try:
            rl = int(p.get("remaining_lots") or 0)
        except (TypeError, ValueError):
            rl = 0
        try:
            lo = int(p.get("lots") or 0)
        except (TypeError, ValueError):
            lo = 0
        return rl if rl > 0 else lo

    for sym, p in epos.items():
        if not isinstance(p, dict):
            continue
        lots = _alive_lots(p)
        if lots <= 0:
            continue
        entry = float(p.get("entry_price") or p.get("price") or 0)
        if entry <= 0:
            continue
        mult = _mult(sym)
        if not mult:
            if verbose:
                print(f"[引擎同步] 跳过 {sym}：缺乘数，无法估算保证金")
            continue

        direction = p.get("direction") or "多"
        tstop = p.get("trailing_stop")
        initial_stop = p.get("stop_price")
        # 有效止损：移动止损激活后以 tracking stop 为准（面板「止损」列 + 触价红标都读 stop）
        eff_stop = tstop if (p.get("trailing_active") and tstop) else initial_stop
        last_price = p.get("last_price")
        margin = round(entry * mult * lots * _DEFAULT_MARGIN_RATE, 2)
        trail_state = "跟踪" if p.get("trailing_active") else None

        if str(sym) in a_syms:
            # ── 已存在：仅当是本桥补入的 auto 腿才对齐更新，其余一律不碰 ──
            if str(leg_of.get(str(sym), {}).get("source") or "") != "engine_sync":
                continue
            leg = leg_of[str(sym)]
            before = (
                leg.get("lots"), leg.get("stop"), leg.get("trailing_stop"),
                leg.get("trail_state"), leg.get("price"), leg.get("float_pnl"),
            )
            leg["lots"] = lots
            leg["position"] = lots
            leg["stop"] = eff_stop
            leg["stop_price"] = eff_stop
            leg["trailing_stop"] = tstop
            leg["trail_state"] = trail_state
            leg["t1_filled"] = bool(p.get("t1_filled"))
            leg["peak_price"] = p.get("peak_price")
            leg["t1"] = p.get("t1_price")
            leg["t2"] = p.get("t2_price")
            leg["margin"] = margin
            if last_price:
                leg["price"] = float(last_price)
                sign = 1 if str(direction) == "多" else -1
                leg["float_pnl"] = round((float(last_price) - entry) * mult * lots * sign, 2)
            after = (
                leg.get("lots"), leg.get("stop"), leg.get("trailing_stop"),
                leg.get("trail_state"), leg.get("price"), leg.get("float_pnl"),
            )
            if before != after:
                updated.append(f"{sym} 移动止损{'@' + str(tstop) if tstop else '未启用'}/{lots}手")
            continue

        a_positions.append({
            "symbol": sym,
            "name": p.get("name") or sym,
            "direction": direction,
            "position": lots,
            "lots": lots,
            "open_price": entry,
            "price": float(last_price) if last_price else entry,
            "margin": margin,
            "stop": eff_stop,
            "stop_price": eff_stop,
            "t1": p.get("t1_price"),
            "t2": p.get("t2_price"),
            "trailing_stop": tstop,
            "trail_state": trail_state,
            "t1_filled": bool(p.get("t1_filled")),
            "peak_price": p.get("peak_price"),
            "open_time": p.get("open_time") or "",
            "source": "engine_sync",
            "float_pnl": round((float(last_price) - entry) * mult * lots
                               * (1 if str(direction) == "多" else -1), 2) if last_price else 0.0,
        })
        added.append(f"{sym}{direction}{lots}手@{entry}(保证金{margin:.0f})")

    # ── 反向对齐兜底：引擎已无该腿（已自动平仓），账户层仍残留 auto 腿 → 删除 ──
    #   引擎平仓时已通过 remove_account_position 回调清理；此处覆盖回调未生效的
    #   （如引擎状态被打回･重启･跨进程）场景，确保 auto 腿不会在面板上「僵尸留存」。
    removed = []
    if a_positions:
        keep = []
        for x in a_positions:
            sy = str(x.get("symbol") or "")
            is_auto = str(x.get("source") or "") == "engine_sync"
            if is_auto and sy not in {str(k) for k in epos.keys()}:
                removed.append(f"{sy}{x.get('direction') or ''}{x.get('lots') or 0}手")
                continue
            keep.append(x)
        a_positions = keep

    if not (added or updated or removed):
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
            "stop_price": x.get("stop_price"),
            "t1": x.get("t1"),
            "t2": x.get("t2"),
            # auto 腿的实时状态（面板「移动止损」列 / 触价红标依据）
            "trailing_stop": x.get("trailing_stop"),
            "trail_state": x.get("trail_state"),
            "t1_filled": x.get("t1_filled"),
            "peak_price": x.get("peak_price"),
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

    _parts = []
    if added:
        _parts.append(f"补入{len(added)}腿[{', '.join(added)}]")
    if updated:
        _parts.append(f"更新{len(updated)}腿[{', '.join(updated)}]")
    if removed:
        _parts.append(f"清理{len(removed)}腿[{', '.join(removed)}]")
    msg = (
        f"[引擎同步] {' | '.join(_parts)} "
        f"| 保证金合计={total_margin:.2f} 使用率={usage:.2f}%"
    )
    _log_once(msg, "|".join(added + updated + removed))
    return len(added) + len(updated) + len(removed), msg


def remove_account_position(symbol: str, direction: str, lots: int, fully_closed: bool = True,
                           require_engine_source: bool = True) -> str:
    """引擎平仓 → 同步清理账户层对应腿（补齐「只补不删」桥的另一端）。

    背景：
      sync_engine_positions() 是单向补齐，且明确保留账户层已有腿（护栏1「只补不删」），
      本意是不污染账户资金真相源。副作用是：引擎触发止损/T2 自动平仓后，
      账户层仍保留旧腿 ⇒ 面板一直显示「带红止损标却不平仓」。
      本函数由引擎在**真正成交平仓**时回调，补齐链路的另一端。

    安全约束（保住护栏本意，避免污染账户层真相）：
      1. 默认仅处理 source == "engine_sync" 的腿（即本桥曾经补入的腿）；
         非本桥来源的真实/手动腿一律不动，宁可不删也不误删；
      2. 方向必须一致，防止反向真实腿被误删；
      3. 全平 → 删除该腿；部分平 → 按手数扣减，扣完即删除；
      4. 幂等：腿不存在或手数已为 0 时直接处理，不制造抖动；
      5. 原子写盘（tmp + os.replace），失败只返回文本、不上抛异常。

    Args:
        symbol: 品种代码
        direction: 方向（多/空），与引擎 pos["direction"] 同源
        lots: 本次平仓手数
        fully_closed: True=全部平仓（删除腿）；False=部分平仓（扣减手数）
        require_engine_source: 是否要求来源必须是本桥补入（默认 True，安全优先）

    Returns: 日志文本（空串表示账户层本无该腿、无需清理）
    """
    sym = str(symbol).strip()
    try:
        lots = int(lots or 0)
    except (TypeError, ValueError):
        return f"参数异常 lots={lots!r}"
    if lots < 0:
        return f"参数异常 lots={lots}"

    if not os.path.exists(ACCOUNT_STATE):
        return f"账户状态文件不存在: {ACCOUNT_STATE}"

    try:
        with open(ACCOUNT_STATE, encoding="utf-8") as f:
            acct = json.load(f) or {}
    except Exception as e:
        return f"读取失败: {repr(e)[:60]}"

    pos_map = acct.get("positions")
    if not isinstance(pos_map, dict) or sym not in pos_map:
        return ""  # 账户层本就没有该腿 → 无需处理（幂等）

    leg = pos_map.get(sym)
    if not isinstance(leg, dict):
        return f"跳过 {sym}：账户层腿结构异常"

    # 护栏①：仅处理本桥来源的腿（默认开启）
    src = str(leg.get("source") or "")
    if require_engine_source and src != "engine_sync":
        return f"跳过 {sym}：来源非本桥(source={src or '空'})，保住账户层真相未改动"

    # 护栏②：方向必须一致
    leg_dir = str(leg.get("direction") or "")
    if leg_dir and str(direction) and leg_dir != str(direction):
        return f"跳过 {sym}：方向不符(账户层{leg_dir} vs 引擎{direction})，未改动"

    cur_lots = int(leg.get("lots") or 0)
    left = 0 if fully_closed else max(cur_lots - lots, 0)

    if cur_lots <= 0 or left <= 0:
        del pos_map[sym]
        action = f"全平清理 {sym}（原 {cur_lots} 手）"
    else:
        leg["lots"] = left
        pos_map[sym] = leg
        action = f"部分平 {sym} {lots} 手，账户层余 {left} 手"

    # 账户层 positions 与 snapshot 保持同源（与本桥补入逻辑一致）
    snap = acct.get("snapshot")
    if isinstance(snap, dict) and isinstance(snap.get("positions"), list):
        if left <= 0:
            snap["positions"] = [x for x in snap["positions"] if str(x.get("symbol")) != sym]
        else:
            for x in snap["positions"]:
                if str(x.get("symbol")) == sym:
                    x["lots"] = left
                    x["position"] = left
        acct["snapshot"] = snap

    acct["positions"] = pos_map
    try:
        tmp = ACCOUNT_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acct, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACCOUNT_STATE)
    except Exception as e:
        return f"写盘失败: {repr(e)[:60]}"

    return action


if __name__ == "__main__":
    n, m = sync_engine_positions(verbose=True)
    print(m or f"无需同步（引擎无仓或账户层已一致），补入 {n} 腿")