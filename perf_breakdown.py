# -*- coding: utf-8 -*-
"""四维策略 · 绩效多维分解（#10）
=====================================
原来的绩效只有一个总数：总盈亏、总胜率、总期望 R。总数最大的问题是「把赚的和亏的
搅在一起」—— 你可能整体微亏，但其实「趋势市做多」大赚、「震荡市做空」大亏，
总数把这个结构完全糊住了，于是你继续两件事都做，继续原地打转。

本模块把已平仓交易按 8 个维度切开，各自算 笔数/胜率/净盈亏/平均R/期望R/盈亏比：
    方向 · 时段 · 品种 · 板块 · 市场状态(regime) · 出场原因 · 持仓时长 · 星期
并给出「最赚的切片 / 最亏的切片 / 建议砍掉的切片」。

R 口径：单笔 R = pnl / 计划风险额。计划风险额优先用 stop_dist×乘数×手数，
缺失时回退 权益×risk_pct（与 trade_journal 的 R 口径保持一致）。

用法：
    import perf_breakdown as pb
    rep = pb.full_report()          # 全维度
    pb.print_report(rep)            # 终端人话版
"""
from __future__ import annotations
import time
from datetime import datetime

import trade_journal as tj

_REGIME_CACHE = {}      # (symbol, date) -> regime 文本
_REGIME_TTL = 86400
_MIN_N = 3              # 少于 3 笔的切片不下结论（样本太小）


# ── 单笔指标 ──────────────────────────────────────────────────────────────
def _risk_amount(t, equity, risk_pct):
    """该笔计划风险额（元）。"""
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


def _hold_minutes(t):
    try:
        a = datetime.strptime(t["time"], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(t["exit_time"], "%Y-%m-%d %H:%M:%S")
        return max(0.0, (b - a).total_seconds() / 60.0)
    except Exception:
        return None


def _hold_bucket(m):
    if m is None:
        return "未知"
    if m < 30:
        return "<30分(抢反弹)"
    if m < 120:
        return "30分~2时"
    if m < 480:
        return "2~8时(日内)"
    if m < 1440:
        return "8~24时"
    return ">1天(过夜)"


def _weekday(t):
    try:
        d = datetime.strptime(t["time"][:10], "%Y-%m-%d")
        return "周" + "一二三四五六日"[d.weekday()]
    except Exception:
        return "未知"


def _group_of(sym):
    try:
        import four_dim_strategy as fd
        return fd.SYMBOLS.get(sym, {}).get("group") or "其他"
    except Exception:
        return "其他"


def _regime_of(t):
    """开仓当日的市场状态（趋势/震荡）。日线不可得则「未知」，不中断分析。"""
    sym = t.get("symbol")
    date = (t.get("time") or "")[:10]
    key = (sym, date)
    hit = _REGIME_CACHE.get(key)
    if hit and time.time() - hit[0] < _REGIME_TTL:
        return hit[1]
    reg = "未知"
    try:
        import four_dim_strategy as fd
        df = fd.load_daily(sym)
        if df is not None and len(df) > 30:
            sub = df[df.index <= date] if date else df
            if len(sub) > 30:
                _, regime, _ = fd.compute_T(sub)
                reg = str(regime)
    except Exception:
        reg = "未知"
    _REGIME_CACHE[key] = (time.time(), reg)
    return reg


# ── 切片统计 ──────────────────────────────────────────────────────────────
def _stat(trades):
    n = len(trades)
    if not n:
        return None
    pnl = sum(t["_pnl"] for t in trades)
    wins = [t for t in trades if t["_pnl"] > 0]
    losses = [t for t in trades if t["_pnl"] <= 0]
    rs = [t["_R"] for t in trades]
    gp = sum(t["_pnl"] for t in wins)
    gl = abs(sum(t["_pnl"] for t in losses))
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "pnl": round(pnl, 2),
        "avg_pnl": round(pnl / n, 2),
        "expR": round(sum(rs) / n, 3),
        "avg_win_R": round(sum(t["_R"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_R": round(sum(t["_R"] for t in losses) / len(losses), 2) if losses else 0.0,
        "pf": round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0),
        "best": round(max(rs), 2), "worst": round(min(rs), 2),
        "reliable": n >= _MIN_N,
    }


def _breakdown(trades, keyfn, label):
    buckets = {}
    for t in trades:
        try:
            k = keyfn(t)
        except Exception:
            k = "未知"
        buckets.setdefault(k or "未知", []).append(t)
    rows = []
    for k, ts in buckets.items():
        s = _stat(ts)
        if s:
            s["key"] = k
            rows.append(s)
    rows.sort(key=lambda r: -r["pnl"])
    return {"dim": label, "rows": rows}


def _closed_trades():
    data = tj._load()
    equity = tj._base_equity()
    risk_pct = tj._risk_pct()
    out = []
    for t in data.get("trades", []):
        if t.get("pnl") is None:
            continue
        t = dict(t)
        t["_pnl"] = float(t["pnl"])
        t["_R"] = round(t["_pnl"] / _risk_amount(t, equity, risk_pct), 3)
        out.append(t)
    return out, equity


def full_report(with_regime=True):
    """全维度分解。with_regime=False 可跳过日线计算（快很多）。"""
    trades, equity = _closed_trades()
    if not trades:
        return {"ok": False, "reason": "暂无已平仓交易", "n": 0}
    dims = [
        _breakdown(trades, lambda t: t.get("direction") or "未知", "方向"),
        _breakdown(trades, lambda t: tj._session_of(t.get("exit_time") or t.get("time")), "时段"),
        _breakdown(trades, lambda t: t.get("symbol"), "品种"),
        _breakdown(trades, lambda t: _group_of(t.get("symbol")), "板块"),
        _breakdown(trades, lambda t: t.get("exit_reason") or "未知", "出场原因"),
        _breakdown(trades, lambda t: _hold_bucket(_hold_minutes(t)), "持仓时长"),
        _breakdown(trades, _weekday, "星期"),
    ]
    if with_regime:
        dims.append(_breakdown(trades, _regime_of, "市场状态"))
        dims.append(_breakdown(
            trades, lambda t: f"{_regime_of(t)}·{t.get('direction')}", "状态×方向"))
    overall = _stat(trades)
    # 最赚 / 最亏 / 建议砍掉（样本够 且 期望R 明显为负）
    best = worst = None
    cut = []
    for d in dims:
        for r in d["rows"]:
            if not r["reliable"]:
                continue
            tag = f"{d['dim']}:{r['key']}"
            if best is None or r["expR"] > best["expR"]:
                best = dict(r, tag=tag)
            if worst is None or r["expR"] < worst["expR"]:
                worst = dict(r, tag=tag)
            if r["expR"] <= -0.15 and r["n"] >= max(_MIN_N, 5):
                cut.append({"tag": tag, "n": r["n"], "expR": r["expR"],
                            "pnl": r["pnl"], "win_rate": r["win_rate"]})
    cut.sort(key=lambda x: x["expR"])
    return {"ok": True, "n": len(trades), "equity": equity,
            "overall": overall, "dims": dims,
            "best": best, "worst": worst, "cut": cut[:6],
            "min_n": _MIN_N,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def print_report(rep=None):
    rep = rep or full_report()
    if not rep.get("ok"):
        print(rep.get("reason"))
        return
    o = rep["overall"]
    print(f"\n{'='*70}")
    print(f"绩效多维分解 · 已平仓 {rep['n']} 笔 · 权益基准 {rep['equity']:.0f}")
    print(f"总体：胜率 {o['win_rate']}% | 净盈亏 {o['pnl']:.0f} | 期望R {o['expR']:+.3f} "
          f"| 盈亏比 {o['pf']} | 均盈 {o['avg_win_R']:+.2f}R 均亏 {o['avg_loss_R']:+.2f}R")
    print("=" * 70)
    for d in rep["dims"]:
        print(f"\n【{d['dim']}】")
        print(f"  {'切片':<16}{'笔数':>5}{'胜率':>8}{'净盈亏':>11}{'期望R':>9}{'盈亏比':>8}")
        for r in d["rows"]:
            mark = "" if r["reliable"] else "  (样本少)"
            print(f"  {str(r['key']):<16}{r['n']:>5}{r['win_rate']:>7.1f}%"
                  f"{r['pnl']:>11.0f}{r['expR']:>+9.3f}{r['pf']:>8.2f}{mark}")
    if rep.get("best"):
        b = rep["best"]
        print(f"\n✅ 最赚切片：{b['tag']}（{b['n']}笔 期望R {b['expR']:+.3f} 净{b['pnl']:.0f}）")
    if rep.get("worst"):
        w = rep["worst"]
        print(f"❌ 最亏切片：{w['tag']}（{w['n']}笔 期望R {w['expR']:+.3f} 净{w['pnl']:.0f}）")
    if rep.get("cut"):
        print(f"\n🔪 建议砍掉（样本≥5 且 期望R≤-0.15）：")
        for c in rep["cut"]:
            print(f"   · {c['tag']}：{c['n']}笔 胜率{c['win_rate']}% "
                  f"期望R {c['expR']:+.3f} 净{c['pnl']:.0f}")
    else:
        print("\n🔪 暂无「样本够且明显负期望」的切片需要砍。")


if __name__ == "__main__":
    print_report()
