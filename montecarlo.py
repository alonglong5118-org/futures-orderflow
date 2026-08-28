# -*- coding: utf-8 -*-
"""四维策略 · 蒙特卡洛权益曲线置信区间（#11）
============================================
已平仓交易只有一条「真实」权益曲线，但它只是无数种可能里的一条。
真正该问的是：以我这套打法（R 序列的胜率/赔率分布），未来 N 笔下来，
账户大概率去哪？最坏会多惨？会不会爆？

本模块对历史逐笔 R 序列做 bootstrap 重抽样（有放回随机抽取），
模拟 thousands 条未来路径，给出：
    · 权益曲线分位带（5/25/50/75/95 分位）—— 一眼看出"正常区间"和"危险边界"
    · 终值置信区间（p5 / p50 / p95）+ 盈利概率 + 破产概率
    · 最大回撤分布（p50 / p95）—— 你该为多大回撤做准备

R 口径与 perf_breakdown 一致：单笔 R = 净盈亏 / 计划风险额。
权益演化：eq_{t+1} = eq_t * (1 + R_t * f)，f = 单笔计划风险占权益比（默认 2%）。

用法：
    import montecarlo as mc
    rep = mc.simulate()              # 用 journal 真实 R 序列
    mc.print_report(rep)             # 终端人话版

优化记录 (2026-08-19):
  1. 向量化 bootstrap 模拟：numpy 矩阵运算替代 Python for 循环
  2. 使用 np.percentile 替代自定义百分位计算
  3. 预生成所有随机索引，减少随机数生成开销
  4. 批量计算最大回撤，避免逐路径计算
"""
from __future__ import annotations

import json
import os

import numpy as np

import trade_journal as tj

N_PATHS = 2000          # 模拟路径数
DEFAULT_F = 0.02        # 单笔计划风险占权益比（2%）
MIN_TRADES = 8          # 样本少于此数不做蒙特卡洛
HORIZON_FLOOR = 25      # 至少向前模拟 25 笔


def _equity():
    """账户基准权益。"""
    try:
        return float(tj._base_equity())
    except Exception:
        return 0.0


def _risk_amount(t, equity, risk_pct):
    """计算单笔交易的风险金额。"""
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


def get_r_series(equity=None, risk_pct=None):
    """从 journal 取已平仓交易，按时间序返回 R 列表。"""
    data = tj._load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]
    closed.sort(key=lambda t: t.get("time", ""))
    if equity is None:
        equity = _equity() or 1.0
    if risk_pct is None:
        try:
            risk_pct = tj._risk_pct()
        except Exception:
            risk_pct = 2.0
    out = []
    for t in closed:
        rAmt = _risk_amount(t, equity, risk_pct)
        try:
            R = float(t["pnl"]) / rAmt
        except Exception:
            R = 0.0
        out.append(R)
    return out


def _papertrack_r():
    """真实 walk-forward 回测的 R 序列，作 journal 太薄时的回退。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "papertrack_report.json")
    if not os.path.exists(p):
        return []
    try:
        rep = json.load(open(p, encoding="utf-8"))
        done = [t for t in rep.get("trades", []) if t.get("outcome") in ("win", "loss")]
        done.sort(key=lambda t: t.get("time", ""))
        return [float(t.get("R", 0.0)) for t in done]
    except Exception:
        return []


def simulate(r_series=None, n_paths=N_PATHS, f=DEFAULT_F, horizon=None,
             start_eq=100.0, seed=None):
    """对 R 序列做 bootstrap 重抽样，返回权益曲线分位带 + 终值/回撤分布。

    向量化版本：使用 numpy 矩阵运算同时生成所有路径。
    """
    source = "journal"
    if r_series is None:
        r_series = get_r_series()
        if len(r_series) < MIN_TRADES:
            _pt = _papertrack_r()
            if len(_pt) >= MIN_TRADES:
                r_series = _pt
                source = "papertrack"
    if len(r_series) < MIN_TRADES:
        return {"ok": False,
                "reason": f"已平仓样本仅 {len(r_series)} 笔（需≥{MIN_TRADES}）"
                          f"，蒙特卡洛暂无意义，先多积几笔真实交易。",
                "n_trades": len(r_series), "source": source, "bands": [], "terminal": {},
                "maxdd": {}, "real_curve": r_series}

    rng = np.random.default_rng(seed)
    H = int(horizon) if horizon else max(HORIZON_FLOOR, len(r_series))
    
    # 向量化 bootstrap：预生成所有路径的索引
    r_arr = np.array(r_series)
    n_r = len(r_arr)
    indices = rng.integers(0, n_r, size=(n_paths, H))
    
    # 批量生成所有路径的 R 值
    path_R = r_arr[indices]  # shape: (n_paths, H)
    
    # 向量化权益演化：eq_{t+1} = eq_t * (1 + R_t * f)
    growth = 1.0 + path_R * f  # shape: (n_paths, H)
    # 累积乘积得到权益路径
    paths_eq = np.empty((n_paths, H + 1))
    paths_eq[:, 0] = start_eq
    paths_eq[:, 1:] = start_eq * np.cumprod(growth, axis=1)
    
    # 向量化计算每条路径的最大回撤
    peaks = np.maximum.accumulate(paths_eq, axis=1)
    drawdowns = (peaks - paths_eq) / np.maximum(peaks, 1e-10)
    maxdds = np.max(drawdowns, axis=1)
    
    # 计算分位带：每一步在路径间取分位
    bands = []
    for step in range(H + 1):
        col = paths_eq[:, step]
        p5, p25, p50, p75, p95 = np.percentile(col, [5, 25, 50, 75, 95])
        bands.append({
            "step": step,
            "p5": round(float(p5), 3),
            "p25": round(float(p25), 3),
            "p50": round(float(p50), 3),
            "p75": round(float(p75), 3),
            "p95": round(float(p95), 3),
        })
    
    # 终值统计
    terminal_vals = paths_eq[:, -1]
    terminal = {
        "p5": round(float(np.percentile(terminal_vals, 5)), 3),
        "p50": round(float(np.percentile(terminal_vals, 50)), 3),
        "p95": round(float(np.percentile(terminal_vals, 95)), 3),
        "mean": round(float(np.mean(terminal_vals)), 3),
        "prob_profit": round(float(np.mean(terminal_vals > start_eq)), 3),
        "prob_ruin": round(float(np.mean(terminal_vals < start_eq * 0.5)), 3),
    }
    
    # 最大回撤统计
    maxdd = {
        "p50": round(float(np.percentile(maxdds, 50)) * 100, 2),
        "p95": round(float(np.percentile(maxdds, 95)) * 100, 2),
        "mean": round(float(np.mean(maxdds)) * 100, 2),
    }
    
    # 历史真实累计 R 曲线
    real = []
    acc = 0.0
    for R in r_series:
        acc += R
        real.append(round(acc * f * start_eq + start_eq, 3))
    
    win_rate = float(np.mean(r_arr > 0))
    avg_R = float(np.mean(r_arr))
    
    return {
        "ok": True,
        "reason": "",
        "source": source,
        "n_trades": len(r_series),
        "horizon": H,
        "f": f,
        "bands": bands,
        "terminal": terminal,
        "maxdd": maxdd,
        "real_curve": real,
        "win_rate": round(win_rate, 3),
        "avg_R": round(avg_R, 4),
    }


def print_report(rep):
    """打印蒙特卡洛模拟结果。"""
    if not rep.get("ok"):
        print(f"蒙特卡洛：{rep.get('reason')}")
        return
    t = rep["terminal"]
    m = rep["maxdd"]
    print("=" * 64)
    print(f"蒙特卡洛 · 来源 {rep.get('source','journal')} · 样本 {rep['n_trades']} 笔 · "
          f"模拟 {rep['horizon']} 笔窗口 · 单笔风险 {rep['f']*100:.1f}%")
    print(f"  胜率 {rep['win_rate']*100:.1f}% · 平均 R {rep['avg_R']:+.3f}")
    print("-" * 64)
    print(f"  终值(基准100)：p5={t['p5']}  p50={t['p50']}  p95={t['p95']}  均值={t['mean']}")
    print(f"  盈利概率 {t['prob_profit']*100:.1f}% · 破产概率(腰斩) {t['prob_ruin']*100:.1f}%")
    print(f"  最大回撤：中位 {m['p50']:.1f}% · 95分位 {m['p95']:.1f}%")
    print("=" * 64)


if __name__ == "__main__":
    print_report(simulate())
