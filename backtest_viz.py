# -*- coding: utf-8 -*-
"""四维策略 · 回测可视化（#17）
=================================
数字说“整体期望 R 是 +0.2”，但你不知道这 +0.2 是“一路稳赚”还是
“先亏 30% 再一把扳回”。回测要看两条曲线才敢信：
    1) 权益曲线 + 水下曲线(drawdown underwater) —— 你最多沉到多深、沉多久
    2) 逐笔 R 散点 —— 盈亏分布是“细水长流”还是“靠几笔大单撑”

数据来源（自动择优）：
    · papertrack_report.json —— 真实 walk-forward 回测（信号后实际行情逐根判定）
    · 回退 trade_journal.json —— 实盘已平仓记录（R 口径与 perf_breakdown 一致）

输出统一为 data()，供 /api/backtest_viz 直接 json 给前端 canvas 渲染。

用法：
    import backtest_viz as bv
    rep = bv.data()
"""
from __future__ import annotations
import os
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_JSON = os.path.join(_HERE, "papertrack_report.json")
F = 0.02   # 单笔计划风险占权益比，用于把 R 折成权益曲线


def _risk_amount(t, equity, risk_pct=2.0):
    sd = t.get("stop_dist")
    if sd:
        try:
            import trade_journal as tj
            mult = tj._MULTIPLIERS.get(t["symbol"], 10)
            r = abs(float(sd)) * mult * int(t.get("lots") or 1)
            if r > 0:
                return r
        except Exception:
            pass
    stop, entry = t.get("stop"), t.get("entry_price")
    if stop and entry:
        try:
            import trade_journal as tj
            mult = tj._MULTIPLIERS.get(t["symbol"], 10)
            r = abs(float(entry) - float(stop)) * mult * int(t.get("lots") or 1)
            if r > 0:
                return r
        except Exception:
            pass
    return max(1.0, equity * risk_pct / 100.0)


def _journal_r_series():
    """实盘已平仓交易的 R 序列（回退源）。返回 [{symbol,time,R,win}, ...]"""
    try:
        import trade_journal as tj
        data = tj._load()
        closed = [t for t in data["trades"] if t.get("pnl") is not None]
        closed.sort(key=lambda t: t.get("time", ""))
        try:
            equity = float(tj._base_equity())
        except Exception:
            equity = 1.0
        try:
            rp = tj._risk_pct()
        except Exception:
            rp = 2.0
        out = []
        for t in closed:
            rAmt = _risk_amount(t, equity, rp)
            try:
                R = float(t["pnl"]) / rAmt
            except Exception:
                R = 0.0
            out.append({"symbol": t.get("symbol", "?"), "time": t.get("time", ""),
                        "R": round(R, 4), "win": R > 0})
        return out
    except Exception:
        return []


def _papertrack_series():
    """真实 walk-forward 回测的 R 序列。"""
    if not os.path.exists(REPORT_JSON):
        return None
    try:
        rep = json.load(open(REPORT_JSON, encoding="utf-8"))
        trades = rep.get("trades", [])
        done = [t for t in trades if t.get("outcome") in ("win", "loss")]
        done.sort(key=lambda t: t.get("time", ""))
        out = [{"symbol": t.get("symbol", "?"), "time": t.get("time", ""),
                "R": round(float(t.get("R", 0.0)), 4), "win": t.get("outcome") == "win"}
               for t in done]
        if not out:
            return None
        return out
    except Exception:
        return None


def _build(series, source):
    """把 R 序列折成 权益曲线 + 水下曲线 + 逐笔散点。"""
    eq = 100.0
    peak = 100.0
    equity = [{"step": 0, "eq": round(eq, 3), "dd_pct": 0.0}]
    scatter = []
    cum_R = 0.0
    for i, s in enumerate(series):
        R = s["R"]
        cum_R += R
        eq = max(1e-6, eq * (1.0 + R * F))
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        equity.append({"step": i + 1, "eq": round(eq, 3), "dd_pct": round(dd, 2)})
        scatter.append({"idx": i, "R": R, "symbol": s["symbol"], "win": s["win"],
                        "time": s["time"]})
    n = len(series)
    wins = sum(1 for s in series if s["win"])
    max_dd = max((e["dd_pct"] for e in equity), default=0.0)
    return {
        "source": source,
        "n": n,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "cum_R": round(cum_R, 4),
        "exp_R": round(cum_R / n, 4) if n else 0.0,
        "max_dd_pct": round(max_dd, 2),
        "equity": equity,
        "scatter": scatter,
    }


def data():
    """统一入口：优先 papertrack 回测，回退实盘 journal。"""
    pt = _papertrack_series()
    if pt is not None:
        out = _build(pt, "papertrack")
        out["ok"] = True
        return out
    jr = _journal_r_series()
    out = _build(jr, "journal")
    out["ok"] = bool(jr)
    out["note"] = ("暂无 papertrack 回测，已回退实盘成交" if not jr else
                   "暂无 papertrack 回测，已用实盘成交序列")
    return out


def print_report(rep):
    print("=" * 56)
    print(f"回测可视化 · 来源 {rep['source']} · {rep['n']} 笔")
    print(f"  胜率 {rep['win_rate']*100:.1f}% · 累计R {rep['cum_R']:+.3f} · "
          f"期望R {rep['exp_R']:+.3f} · 最大回撤 {rep['max_dd_pct']:.1f}%")
    print("  权益曲线端点:", rep["equity"][-1]["eq"] if rep["equity"] else None)
    print("=" * 56)


if __name__ == "__main__":
    print_report(data())
