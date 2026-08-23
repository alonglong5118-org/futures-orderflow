# -*- coding: utf-8 -*-
"""四维策略 · 纪律自动体检 / blunder 检测（#12）
====================================================
“管住手”三个字说起来容易，做起来全是细节。本模块扫一遍成交记录（已平仓 + 在持），
自动揪出典型的“手没管住”操作，给一份体检报告：
    严重程度  说明
    H 高危    逆信号手动开仓 / 无止损裸奔 / 连亏后加仓(martingale)
    M 中危    单笔亏损≥3R / 超计划手数 / 30分钟内同品种反手
    L 轻危    单笔盈利≥5R 却未减仓（贪）等

报告输出：扣分制（满分100，按严重度扣），给等级(A/B/C/D) + 各类违规计数 + 明细。

只扫描、只提醒，不代做任何操作。可每日/每周由 runner 节流跑，结果进面板。

用法：
    import blunder_check as bc
    rep = bc.check()                 # 扫全量 journal
    bc.print_report(rep)             # 终端人话版
"""
from __future__ import annotations
from datetime import datetime

import trade_journal as tj

# 各品种计划手数上限（超过即“超计划”）；缺省用通用上限
MAX_LOTS = {
    "FG": 30, "SA": 30, "JM": 20, "J": 20, "jd": 20, "lh": 15,
    "rb": 40, "hc": 40, "i": 30, "m": 30, "RM": 30, "CF": 20,
    "V": 25, "UR": 25, "P": 15, "PF": 20, "RU": 15, "CU": 10,
}
DEFAULT_MAX_LOTS = 30
FLIP_WINDOW_MIN = 30          # 同品种反手时间窗
SEV_DEDUCT = {"H": 15, "M": 8, "L": 3}
LOSS_R_THRESHOLD = -3.0       # 单笔亏损≥3R 计中危
GREED_R_THRESHOLD = 5.0       # 单笔盈利≥5R 未减仓计轻危


def _risk_amount(t, equity, risk_pct):
    sd = t.get("stop_dist")
    if sd:
        try:
            mult = tj._MULTIPLIERS.get(t["symbol"], 10)
            r = abs(float(sd)) * mult * int(t.get("lots") or 1)
            if r > 0:
                return r
        except Exception:
            pass
    stop, entry = t.get("stop"), t.get("entry_price")
    if stop and entry:
        try:
            mult = tj._MULTIPLIERS.get(t["symbol"], 10)
            r = abs(float(entry) - float(stop)) * mult * int(t.get("lots") or 1)
            if r > 0:
                return r
        except Exception:
            pass
    return max(1.0, equity * risk_pct / 100.0)


def _to_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def check(equity=None, risk_pct=None):
    if equity is None:
        try:
            equity = float(tj._base_equity())
        except Exception:
            equity = 1.0
    if risk_pct is None:
        try:
            risk_pct = tj._risk_pct()
        except Exception:
            risk_pct = 2.0
    data = tj._load()
    trades = sorted(data["trades"], key=lambda t: t.get("time", ""))
    closed = [t for t in trades if t.get("pnl") is not None]
    open_t = [t for t in trades if t.get("pnl") is None]
    blunders = []

    prev_pnl = None      # 上一笔已平仓盈亏（用于连亏加仓判定）
    prev_ts = None
    prev_sym = None
    # 同品种上一笔开仓（含方向/时间/手数），用于反手&加仓
    last_entry = {}      # sym -> (ts, direction, lots)
    for t in trades:
        sym = t.get("symbol", "?")
        ts = _to_ts(t.get("time", ""))
        lots = int(t.get("lots") or 0)
        direc = t.get("direction", "")
        # —— 开仓侧检查 ——
        # 1) 逆信号手动开仓：无 signal_id
        if not t.get("signal_id"):
            blunders.append(_b("无信号手动开仓", "H", sym, t.get("time", ""),
                               f"{direc} {lots}手 无对应信号ID，属“手痒单”",
                               "只在四维信号触发时才动手；手动单需先写理由"))
        # 2) 无止损计划：开仓未带 stop
        if t.get("stop") is None and t.get("stop_dist") is None:
            blunders.append(_b("无止损计划", "H", sym, t.get("time", ""),
                               f"{direc} {lots}手 未设止损", "开仓必带止损，先想好退路再进"))
        # 3) 超计划手数
        cap = MAX_LOTS.get(sym, DEFAULT_MAX_LOTS)
        if lots > cap:
            blunders.append(_b("超计划手数", "M", sym, t.get("time", ""),
                               f"{direc} {lots}手 超上限 {cap}手", f"单品种控制在 {cap} 手内"))
        # 4) 频繁反手：同品种近窗口内反向开仓
        le = last_entry.get(sym)
        if le and abs(ts - le[0]) <= FLIP_WINDOW_MIN * 60:
            if le[1] != direc:
                blunders.append(_b("频繁反手", "M", sym, t.get("time", ""),
                                   f"{le[1]}→{direc} 在 {FLIP_WINDOW_MIN}分钟内翻转", "反手=认错，先平再看，不要对着干"))
        # 5) 连亏加仓：上一笔亏，这一笔开仓手数更大
        if prev_pnl is not None and prev_pnl < 0 and lots > (last_entry.get(sym, (0, "", 0))[2] or 0):
            blunders.append(_b("连亏加仓", "H", sym, t.get("time", ""),
                               f"上一笔亏损后本笔加至 {lots}手（martingale）", "亏了就缩手，绝不加码摊平"))
        last_entry[sym] = (ts, direc, lots)
        if t in closed:
            prev_pnl = t.get("pnl")
        # —— 平仓侧检查 ——
        if t in closed:
            rAmt = _risk_amount(t, equity, risk_pct)
            try:
                R = float(t["pnl"]) / rAmt if rAmt > 0 else 0.0
            except Exception:
                R = 0.0
            if R <= LOSS_R_THRESHOLD:
                blunders.append(_b("单笔超大亏损", "M", sym, t.get("exit_time", ""),
                                   f"该笔 R={R:.2f}（亏 {t.get('pnl')}元）", "复盘：是信号错还是执行错？别让一笔拖垮一周"))
            elif R >= GREED_R_THRESHOLD:
                blunders.append(_b("盈利未减仓", "L", sym, t.get("exit_time", ""),
                                   f"该笔 R={R:.2f} 大赚却全程不动", "大盈利分批了结，锁住一部分利润"))

    # 扣分 + 等级
    score = 100
    for b in blunders:
        score -= SEV_DEDUCT.get(b["sev"], 3)
    score = max(0, score)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    by_type = {}
    for b in blunders:
        by_type[b["type"]] = by_type.get(b["type"], 0) + 1
    blunders.sort(key=lambda b: _to_ts(b.get("time", "")), reverse=True)
    return {
        "ok": True,
        "n_closed": len(closed),
        "n_open": len(open_t),
        "score": score,
        "grade": grade,
        "counts": by_type,
        "n_blunders": len(blunders),
        "blunders": blunders[:40],
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _b(type_, sev, sym, time_, detail, suggestion):
    return {"type": type_, "sev": sev, "symbol": sym, "time": time_,
            "detail": detail, "suggestion": suggestion}


def print_report(rep):
    print("=" * 60)
    print(f"纪律体检 · 已平 {rep['n_closed']} 笔 / 在持 {rep['n_open']} 笔")
    print(f"  评分 {rep['score']} 分 · 等级 {rep['grade']} · 违规 {rep['n_blunders']} 项")
    if rep["counts"]:
        print("  分布: " + "  ".join(f"{k}×{v}" for k, v in rep["counts"].items()))
    print("-" * 60)
    for b in rep["blunders"][:15]:
        tag = {"H": "🔴", "M": "🟠", "L": "🟡"}.get(b["sev"], "·")
        print(f"  {tag}[{b['sev']}] {b['type']} · {b['symbol']} · {b['time']}")
        print(f"       {b['detail']}")
        print(f"       → {b['suggestion']}")
    if not rep["blunders"]:
        print("  ✅ 未发现明显纪律违规，保持。")
    print("=" * 60)


if __name__ == "__main__":
    print_report(check())
