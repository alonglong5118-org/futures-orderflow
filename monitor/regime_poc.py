"""
Phase 8 PoC：逐 Regime 参数差异化的嵌套滚动验证

验证问题：
  逐 Regime 独立优化 T 和 stop 系数，能否在 OOS 上带来显著的 expR 提升？

方法：
  1. 嵌套滚动验证（walk-forward）
  2. 内层（IS）：对每个主要 Regime（趋势/震荡/波动）独立搜索 T×stop 系数
  3. 外层（OOS）：用 IS 上找到的逐 Regime 最优系数，在 OOS 上验证
  4. 与基线（全局 Regime 系数）做同周期对比

与 quarterly_reopt 的区别：
  - quarterly_reopt: 优化全局一套 T/stop/rr 参数
  - 本脚本: 优化逐 Regime 的 T 乘数和 stop 乘数（在基线参数上调整）

用法：
    python -m monitor.regime_poc --symbols zn,fu,pp
    python -m monitor.regime_poc --symbols zn --train-bars 750 --oos-bars 250
"""

import argparse
import copy
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from four_dim_strategy import (  # noqa: E402
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)


# ============================================================================
# 常量
# ============================================================================

MAIN_REGIMES = ["趋势", "震荡", "波动"]

# 搜索范围：T 乘数 和 stop 乘数
DEFAULT_T_MULT_RANGE = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]
DEFAULT_STOP_MULT_RANGE = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]

# 最少交易笔数（某 Regime 低于此数不做优化，沿用基线）
DEFAULT_MIN_TRADES_PER_REGIME = 6

# 滚动验证默认参数
DEFAULT_INIT_TRAIN_BARS = 750
DEFAULT_REOPT_FREQ_BARS = 250


# ============================================================================
# 工具函数
# ============================================================================

def _make_regime_config(
    symbol: str,
    regime_coefs: Dict[str, Dict[str, float]],
    base_cfg: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    创建带有指定 Regime 系数的配置。

    regime_coefs: {"趋势": {"T": 0.8, "stop": 1.2}, "波动": {"T": 1.2, "stop": 0.8}, ...}
    未指定的 Regime 沿用全局默认值。
    """
    cfg = copy.deepcopy(base_cfg or DEFAULT_CONFIG)

    per_sym = {}
    for regime, coefs in regime_coefs.items():
        per_sym[regime] = {}
        if "T" in coefs:
            per_sym[regime]["T"] = coefs["T"]
        if "stop" in coefs:
            per_sym[regime]["stop"] = coefs["stop"]

    cfg["per_symbol_regime_coef"] = {symbol: per_sym}
    return cfg


def _run_backtest(symbol: str, cfg: Dict[str, Any], df_slice=None) -> Dict[str, Any]:
    """运行单次回测，返回结果"""
    try:
        result = walk_forward_backtest(
            symbol, cfg,
            window=300,
            min_bars=60,
            df_in=df_slice,
        )
        return result
    except Exception as e:
        return {"symbol": symbol, "trades": 0, "note": f"异常:{repr(e)[:80]}", "expR": 0}


def _regime_expR(trades: List[Dict[str, Any]], regime: str) -> Tuple[float, int]:
    """计算指定 Regime 下的 expR 和交易数"""
    rs = [t["R_adj"] for t in trades if t.get("regime") == regime]
    if not rs:
        return 0.0, 0
    return float(np.mean(rs)), len(rs)


# ============================================================================
# 逐 Regime 网格搜索（在一个训练窗口内）
# ============================================================================

def _optimize_per_regime(
    symbol: str,
    train_df,
    t_mults: List[float],
    stop_mults: List[float],
    min_trades: int,
) -> Dict[str, Dict[str, float]]:
    """
    在训练窗口内，对每个 Regime 独立搜索最优 T×stop 系数组合。

    Returns:
        {regime: {"T": t_mult, "stop": stop_mult, "expR": float, "trades": int}}
    """
    result = {}

    for regime in MAIN_REGIMES:
        best_expR = -float("inf")
        best_T = 1.0
        best_stop = 1.0
        best_trades = 0

        for t_mult in t_mults:
            for stop_mult in stop_mults:
                cfg = _make_regime_config(symbol, {
                    regime: {"T": t_mult, "stop": stop_mult}
                })
                bt = _run_backtest(symbol, cfg, df_slice=train_df)
                trades = bt.get("trades_detail", [])

                expR, n = _regime_expR(trades, regime)
                if n >= min_trades and expR > best_expR:
                    best_expR = expR
                    best_T = t_mult
                    best_stop = stop_mult
                    best_trades = n

        if best_trades >= min_trades:
            result[regime] = {
                "T": best_T,
                "stop": best_stop,
                "expR": round(best_expR, 4),
                "trades": best_trades,
            }
        # 否则不加入，沿用全局默认

    return result


# ============================================================================
# 嵌套滚动验证
# ============================================================================

def regime_nested_rolling(
    symbol: str,
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    t_mults: List[float] = None,
    stop_mults: List[float] = None,
    min_trades_per_regime: int = DEFAULT_MIN_TRADES_PER_REGIME,
) -> Dict[str, Any]:
    """
    逐 Regime 参数差异化的嵌套滚动验证。

    流程：
    1. 将数据分为多个滚动周期（训练期 → OOS 期）
    2. 每个训练期内，逐 Regime 搜索最优 T/stop 系数
    3. 在对应的 OOS 期上，用逐 Regime 优化的系数 vs 基线系数做对比
    4. 汇总所有 OOS 周期的结果

    Returns:
        {
            "symbol": "...",
            "n_periods": N,
            "n_bars": N,
            "baseline": {expR, trades, win_rate},
            "regime_opt": {expR, trades, win_rate},
            "delta_expR": float,
            "delta_pct": float,
            "periods": [...],  # 逐周期详情
            "param_history": {regime: [coef_per_period, ...]},
            "passes": bool,
            "note": "...",
        }
    """
    t_mults = t_mults or DEFAULT_T_MULT_RANGE
    stop_mults = stop_mults or DEFAULT_STOP_MULT_RANGE

    df = load_daily(symbol)
    if df is None or len(df) < init_train_bars + reopt_freq_bars:
        return {
            "symbol": symbol,
            "n_periods": 0,
            "n_bars": 0,
            "passes": False,
            "note": "数据不足",
        }

    n_bars = len(df)

    # 基线配置（默认全局 Regime 系数）
    baseline_cfg = DEFAULT_CONFIG

    # 滚动周期
    periods = []
    oos_base_rs = []      # 基线每笔 R（用于总体 expR 计算）
    oos_opt_rs = []       # 逐 Regime 优化每笔 R
    param_history = {r: [] for r in MAIN_REGIMES}  # 每周期的最优系数

    period_start = init_train_bars
    while period_start + reopt_freq_bars <= n_bars:
        period_end = min(period_start + reopt_freq_bars, n_bars)

        # ---- 内层：IS 训练 ----
        train_df = df.iloc[:period_start]

        # 逐 Regime 优化
        opt_coefs = _optimize_per_regime(
            symbol, train_df, t_mults, stop_mults, min_trades_per_regime
        )

        # 记录参数历史
        for regime in MAIN_REGIMES:
            if regime in opt_coefs:
                param_history[regime].append({
                    "T": opt_coefs[regime]["T"],
                    "stop": opt_coefs[regime]["stop"],
                })
            else:
                param_history[regime].append(None)

        # ---- 外层：OOS 验证 ----
        oos_df = df.iloc[period_start:period_end]

        # 基线 OOS
        base_bt = _run_backtest(symbol, baseline_cfg, df_slice=oos_df)
        base_trades = base_bt.get("trades_detail", [])

        # 逐 Regime 优化 OOS
        opt_cfg = _make_regime_config(symbol, {
            r: {"T": c["T"], "stop": c["stop"]}
            for r, c in opt_coefs.items()
        })
        opt_bt = _run_backtest(symbol, opt_cfg, df_slice=oos_df)
        opt_trades = opt_bt.get("trades_detail", [])

        base_rs = [t["R_adj"] for t in base_trades]
        opt_rs = [t["R_adj"] for t in opt_trades]

        oos_base_rs.extend(base_rs)
        oos_opt_rs.extend(opt_rs)

        base_expR = float(np.mean(base_rs)) if base_rs else 0
        opt_expR = float(np.mean(opt_rs)) if opt_rs else 0

        # 逐 Regime 的 OOS 表现
        regime_oos = {}
        for regime in MAIN_REGIMES:
            b_rs = [t["R_adj"] for t in base_trades if t.get("regime") == regime]
            o_rs = [t["R_adj"] for t in opt_trades if t.get("regime") == regime]
            regime_oos[regime] = {
                "base_expR": round(float(np.mean(b_rs)), 4) if b_rs else 0,
                "opt_expR": round(float(np.mean(o_rs)), 4) if o_rs else 0,
                "base_trades": len(b_rs),
                "opt_trades": len(o_rs),
                "delta": round(float(np.mean(o_rs) - np.mean(b_rs)), 4) if b_rs and o_rs else 0,
            }

        periods.append({
            "period_idx": len(periods),
            "train_end": period_start,
            "oos_start": period_start,
            "oos_end": period_end,
            "opt_coefs": opt_coefs,
            "base_expR": round(base_expR, 4),
            "opt_expR": round(opt_expR, 4),
            "delta_expR": round(opt_expR - base_expR, 4),
            "base_trades": len(base_rs),
            "opt_trades": len(opt_rs),
            "regime_oos": regime_oos,
        })

        period_start = period_end

    # ---- 汇总 ----
    if not oos_base_rs and not oos_opt_rs:
        return {
            "symbol": symbol,
            "n_periods": 0,
            "n_bars": n_bars,
            "passes": False,
            "note": "无 OOS 交易",
        }

    base_total_expR = float(np.mean(oos_base_rs)) if oos_base_rs else 0
    opt_total_expR = float(np.mean(oos_opt_rs)) if oos_opt_rs else 0
    delta = opt_total_expR - base_total_expR
    delta_pct = (delta / abs(base_total_expR) * 100) if base_total_expR != 0 else 0

    # 逐 Regime 汇总
    regime_summary = {}
    for regime in MAIN_REGIMES:
        b_all = []
        o_all = []
        for p in periods:
            rdata = p["regime_oos"].get(regime, {})
            if rdata.get("base_trades", 0) > 0:
                b_all.extend([rdata["base_expR"]] * rdata["base_trades"])  # 近似
            if rdata.get("opt_trades", 0) > 0:
                o_all.extend([rdata["opt_expR"]] * rdata["opt_trades"])
        if b_all and o_all:
            regime_summary[regime] = {
                "base_expR": round(float(np.mean(b_all)), 4),
                "opt_expR": round(float(np.mean(o_all)), 4),
                "delta": round(float(np.mean(o_all) - np.mean(b_all)), 4),
                "base_trades_approx": len(b_all),
                "opt_trades_approx": len(o_all),
            }

    # 正收益周期数
    n_positive = sum(1 for p in periods if p["delta_expR"] > 0)
    win_rate_periods = n_positive / len(periods) if periods else 0

    return {
        "symbol": symbol,
        "n_periods": len(periods),
        "n_bars": n_bars,
        "baseline": {
            "expR": round(base_total_expR, 4),
            "trades": len(oos_base_rs),
            "win_rate": round(sum(1 for r in oos_base_rs if r > 0) / len(oos_base_rs), 3) if oos_base_rs else 0,
        },
        "regime_opt": {
            "expR": round(opt_total_expR, 4),
            "trades": len(oos_opt_rs),
            "win_rate": round(sum(1 for r in oos_opt_rs if r > 0) / len(oos_opt_rs), 3) if oos_opt_rs else 0,
        },
        "delta_expR": round(delta, 4),
        "delta_pct": round(delta_pct, 1),
        "period_win_rate": round(win_rate_periods, 3),
        "n_positive_periods": n_positive,
        "regime_summary": regime_summary,
        "periods": periods,
        "param_history": param_history,
        "passes": delta > 0 and win_rate_periods >= 0.5,
        "note": "",
    }


# ============================================================================
# 批量运行
# ============================================================================

def run_poc(
    symbols: List[str],
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    min_trades_per_regime: int = DEFAULT_MIN_TRADES_PER_REGIME,
) -> Dict[str, Any]:
    """对多个品种运行 PoC 验证"""
    results = {}

    for sym in symbols:
        print(f"  PoC {sym}...", end=" ", flush=True)
        try:
            res = regime_nested_rolling(
                sym,
                init_train_bars=init_train_bars,
                reopt_freq_bars=reopt_freq_bars,
                min_trades_per_regime=min_trades_per_regime,
            )
            results[sym] = res
            if res["n_periods"] > 0:
                print(f"OOS: {res['baseline']['expR']:+.3f} → {res['regime_opt']['expR']:+.3f} "
                      f"(Δ={res['delta_expR']:+.3f}, "
                      f"胜率{res['period_win_rate']*100:.0f}%)")
            else:
                print(f"周期数不足: {res.get('note', '')}")
        except Exception as e:
            print(f"异常: {e}")
            results[sym] = {
                "symbol": sym, "n_periods": 0, "passes": False,
                "baseline": {"expR": 0, "trades": 0},
                "regime_opt": {"expR": 0, "trades": 0},
                "delta_expR": 0, "note": str(e),
            }

    # 汇总
    valid = [r for r in results.values() if r.get("n_periods", 0) >= 2]
    if valid:
        avg_base = np.mean([r["baseline"]["expR"] for r in valid])
        avg_opt = np.mean([r["regime_opt"]["expR"] for r in valid])
        avg_delta = np.mean([r["delta_expR"] for r in valid])
        median_delta = np.median([r["delta_expR"] for r in valid])
        n_pass = sum(1 for r in valid if r["delta_expR"] > 0)
        avg_period_wr = np.mean([r["period_win_rate"] for r in valid])
    else:
        avg_base = avg_opt = avg_delta = median_delta = avg_period_wr = 0
        n_pass = 0

    summary = {
        "n_symbols": len(symbols),
        "n_valid": len(valid),
        "avg_baseline_expR": round(float(avg_base), 4),
        "avg_opt_expR": round(float(avg_opt), 4),
        "avg_delta_expR": round(float(avg_delta), 4),
        "median_delta_expR": round(float(median_delta), 4),
        "n_positive": n_pass,
        "positive_rate": round(n_pass / len(valid) * 100, 1) if valid else 0,
        "avg_period_win_rate": round(float(avg_period_wr), 3),
        "init_train_bars": init_train_bars,
        "reopt_freq_bars": reopt_freq_bars,
        "min_trades_per_regime": min_trades_per_regime,
    }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols,
        "results": results,
        "summary": summary,
    }


# ============================================================================
# HTML 报告
# ============================================================================

def generate_report(data: Dict[str, Any], output_path: str) -> str:
    """生成 PoC 验证 HTML 报告"""
    summary = data["summary"]
    results = data["results"]
    timestamp = data["timestamp"]

    # 品种结果表格
    rows_html = ""
    for sym, res in sorted(results.items(), key=lambda x: -x[1].get("delta_expR", -999)):
        bl = res.get("baseline", {}).get("expR", 0)
        opt = res.get("regime_opt", {}).get("expR", 0)
        delta = res.get("delta_expR", 0)
        bl_trades = res.get("baseline", {}).get("trades", 0)
        opt_trades = res.get("regime_opt", {}).get("trades", 0)
        n_periods = res.get("n_periods", 0)
        period_wr = res.get("period_win_rate", 0)
        passes = res.get("passes", False)

        if delta > 0.02:
            dc = "pos"; di = "▲"
        elif delta < -0.02:
            dc = "neg"; di = "▼"
        else:
            dc = "neutral"; di = "—"

        pass_icon = "✅" if passes else "❌"

        rows_html += f"""
        <tr>
          <td><strong>{sym}</strong></td>
          <td class="num">{n_periods}</td>
          <td class="num">{bl_trades}</td>
          <td class="num">{bl:+.3f}</td>
          <td class="num">{opt:+.3f}</td>
          <td class="num {dc}">{di} {delta:+.3f}</td>
          <td class="num">{period_wr*100:.0f}%</td>
          <td>{pass_icon}</td>
        </tr>
        """

    # 逐周期图表（用纯 HTML 条形图）
    period_charts = ""
    for sym, res in sorted(results.items(), key=lambda x: -x[1].get("delta_expR", -999)):
        periods = res.get("periods", [])
        if not periods:
            continue

        bars_html = ""
        for p in periods:
            d = p["delta_expR"]
            h = abs(d) * 150  # 缩放
            h = min(max(h, 2), 100)
            color = "#059669" if d > 0 else "#dc2626"
            pos_class = "bar-pos" if d > 0 else "bar-neg"
            bars_html += f"""
            <div class="bar-col" title="周期{p['period_idx']}: Δ{d:+.3f}">
              <div class="bar {pos_class}" style="height:{h}%; background:{color}"></div>
            </div>
            """

        period_charts += f"""
        <div class="period-chart-card">
          <div class="period-chart-header">
            <strong>{sym}</strong>
            <span class="period-delta {'pos' if res['delta_expR']>0 else 'neg'}">
              Δ {res['delta_expR']:+.3f} ({res['period_win_rate']*100:.0f}% 正收益周期)
            </span>
          </div>
          <div class="bar-chart">
            {bars_html}
          </div>
          <div class="chart-zero-line"></div>
          <div class="chart-labels">
            <span>周期 1</span>
            <span>周期 {len(periods)}</span>
          </div>
        </div>
        """

    # Regime 汇总
    regime_html = ""
    for regime in MAIN_REGIMES:
        deltas = []
        for res in results.values():
            rs = res.get("regime_summary", {}).get(regime)
            if rs and rs.get("base_trades_approx", 0) >= 3:
                deltas.append(rs["delta"])
        if deltas:
            avg_d = round(float(np.mean(deltas)), 4)
            bar_w = min(abs(avg_d) * 200, 100)
            color = "#059669" if avg_d > 0 else "#dc2626"
            regime_html += f"""
            <div class="regime-row">
              <div class="regime-name">{regime}</div>
              <div class="regime-bar-wrap">
                <div class="regime-bar" style="width:{bar_w}%; background:{color}"></div>
              </div>
              <div class="regime-val">{avg_d:+.4f}</div>
              <div class="regime-trades">{len(deltas)} 个品种</div>
            </div>
            """

    # 结论
    avg_delta = summary["avg_delta_expR"]
    pos_rate = summary["positive_rate"]
    if avg_delta >= 0.03 and pos_rate >= 60:
        verdict = "✅ PoC 通过：OOS 收益显著，建议推进 Phase 8 全量开发"
        verdict_class = "success"
    elif avg_delta > 0 and pos_rate >= 50:
        verdict = "⚠️ PoC 部分通过：有正向趋势但幅度有限，建议扩大样本验证"
        verdict_class = "warning"
    else:
        verdict = "❌ PoC 未通过：OOS 收益不显著，暂不建议投入 Phase 8"
        verdict_class = "danger"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phase 8 PoC 验证报告 — Regime 级参数差异化 OOS</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "WenQuanYi Micro Hei", sans-serif;
    background: #f8fafc;
    color: #0f172a;
    font-size: 14px;
    line-height: 1.65;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 0 20px; }}

  .header {{
    background: linear-gradient(135deg, #059669, #0891b2);
    color: #fff;
    padding: 28px 0;
    margin-bottom: 24px;
  }}
  .header h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 6px; }}
  .header .subtitle {{ font-size: 14px; opacity: 0.9; }}
  .header .meta {{ display: flex; gap: 24px; margin-top: 14px; font-size: 12px; opacity: 0.85; flex-wrap: wrap; }}

  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .kpi-card {{
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 16px;
  }}
  .kpi-card .label {{ font-size: 12px; color: #64748b; margin-bottom: 6px; }}
  .kpi-card .value {{ font-size: 24px; font-weight: 700; }}
  .kpi-card .sub {{ font-size: 11px; color: #94a3b8; margin-top: 4px; }}
  .pos {{ color: #059669; }}
  .neg {{ color: #dc2626; }}
  .neutral {{ color: #64748b; }}

  section {{
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 22px;
    margin-bottom: 20px;
  }}
  section h2 {{ font-size: 17px; font-weight: 600; margin-bottom: 16px; color: #1e293b; }}
  section h3 {{ font-size: 14px; font-weight: 600; margin: 16px 0 8px; color: #334155; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #f1f5f9; }}
  th {{ background: #f8fafc; font-weight: 600; color: #475569; font-size: 12px; }}
  td.num {{ font-variant-numeric: tabular-nums; }}

  .callout {{
    padding: 14px 18px;
    border-radius: 8px;
    margin-bottom: 16px;
    font-size: 13px;
    line-height: 1.7;
  }}
  .callout.info {{ background: #eff6ff; border-left: 3px solid #2563eb; color: #1e40af; }}
  .callout.warning {{ background: #fffbeb; border-left: 3px solid #d97706; color: #92400e; }}
  .callout.success {{ background: #f0fdf4; border-left: 3px solid #059669; color: #065f46; }}
  .callout.danger {{ background: #fef2f2; border-left: 3px solid #dc2626; color: #991b1b; }}

  .regime-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }}
  .regime-name {{ width: 50px; font-size: 13px; font-weight: 600; }}
  .regime-bar-wrap {{
    flex: 1;
    height: 22px;
    background: #f1f5f9;
    border-radius: 4px;
    overflow: hidden;
  }}
  .regime-bar {{
    height: 100%;
    border-radius: 4px;
    min-width: 2px;
  }}
  .regime-val {{ width: 70px; text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; }}
  .regime-trades {{ width: 80px; text-align: right; font-size: 11px; color: #64748b; }}

  /* 周期柱状图 */
  .period-chart-card {{
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 12px;
  }}
  .period-chart-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }}
  .period-delta {{ font-weight: 700; font-size: 14px; }}
  .bar-chart {{
    display: flex;
    align-items: flex-end;
    justify-content: center;
    gap: 3px;
    height: 120px;
    padding: 0 10px;
    position: relative;
  }}
  .bar-col {{
    flex: 1;
    max-width: 30px;
    height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
  }}
  .bar {{
    width: 100%;
    border-radius: 2px 2px 0 0;
    min-height: 2px;
    transition: height 0.2s;
  }}
  .chart-zero-line {{
    height: 1px;
    background: #cbd5e1;
    margin: 0 10px;
    position: relative;
    top: -60px;
  }}
  .chart-labels {{
    display: flex;
    justify-content: space-between;
    font-size: 10px;
    color: #94a3b8;
    padding: 0 10px;
    margin-top: -50px;
  }}

  .footnote {{
    font-size: 11px;
    color: #94a3b8;
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid #e2e8f0;
    line-height: 1.7;
  }}

  @media (max-width: 768px) {{
    .header h1 {{ font-size: 20px; }}
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="header">
  <div class="container">
    <h1>Phase 8 PoC：Regime 级参数差异化 OOS 验证</h1>
    <div class="subtitle">嵌套滚动验证 — 逐 Regime 优化 T/stop 系数的 OOS 收益</div>
    <div class="meta">
      <span>📅 {timestamp}</span>
      <span>📊 品种：{summary['n_symbols']} 个</span>
      <span>🔄 滚动周期：{summary['init_train_bars']}训练 + {summary['reopt_freq_bars']}OOS</span>
      <span>🔬 最小样本/Regime：{summary['min_trades_per_regime']} 笔</span>
    </div>
  </div>
</div>

<div class="container">

  <div class="callout {verdict_class}">
    <strong>{verdict}</strong><br>
    OOS 平均 ΔexpR = <strong>{avg_delta:+.3f}</strong>，
    {summary['n_positive']}/{summary['n_valid']} 个品种正收益（{pos_rate:.0f}%），
    平均周期胜率 {summary['avg_period_win_rate']*100:.0f}%。
  </div>

  <!-- KPI -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="label">基线 OOS expR</div>
      <div class="value">{summary['avg_baseline_expR']:+.3f}</div>
      <div class="sub">{summary['n_valid']} 个有效品种</div>
    </div>
    <div class="kpi-card">
      <div class="label">逐 Regime 优化 OOS expR</div>
      <div class="value pos">{summary['avg_opt_expR']:+.3f}</div>
      <div class="sub">嵌套滚动验证</div>
    </div>
    <div class="kpi-card">
      <div class="label">OOS ΔexpR</div>
      <div class="value {'pos' if avg_delta>0 else 'neg'}">{avg_delta:+.3f}</div>
      <div class="sub">中位数 {summary['median_delta_expR']:+.3f}</div>
    </div>
    <div class="kpi-card">
      <div class="label">正收益品种比例</div>
      <div class="value {'pos' if pos_rate>=50 else 'neg'}">{pos_rate:.0f}%</div>
      <div class="sub">{summary['n_positive']} / {summary['n_valid']}</div>
    </div>
  </div>

  <!-- 结果总表 -->
  <section>
    <h2>品种 OOS 结果总览</h2>
    <table>
      <thead>
        <tr>
          <th>品种</th>
          <th class="num">滚动周期</th>
          <th class="num">OOS 交易数</th>
          <th class="num">基线 expR</th>
          <th class="num">优化后 expR</th>
          <th class="num">ΔexpR</th>
          <th class="num">周期胜率</th>
          <th>通过</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </section>

  <!-- 逐周期表现 -->
  <section>
    <h2>逐周期 OOS ΔexpR</h2>
    <p style="color:#64748b; font-size:13px; margin-bottom:12px">
      每个柱状代表一个滚动验证周期的 ΔexpR（优化后 − 基线），绿色为正，红色为负。
    </p>
    {period_charts}
  </section>

  <!-- 逐 Regime OOS -->
  <section>
    <h2>各 Regime OOS ΔexpR</h2>
    {regime_html if regime_html else '<p class="muted">暂无足够数据</p>'}
    <div class="footnote">
      趋势市通常样本最多，统计最可靠；震荡市样本少，参考价值有限。
    </div>
  </section>

  <!-- 结论 -->
  <section>
    <h2>结论与建议</h2>

    <h3>核心结论</h3>
    <ul style="padding-left:20px">
      <li><strong>OOS 平均收益</strong>：ΔexpR = {avg_delta:+.3f}（{'正' if avg_delta>0 else '负'}向）</li>
      <li><strong>稳定性</strong>：{pos_rate:.0f}% 品种正收益，平均周期胜率 {summary['avg_period_win_rate']*100:.0f}%</li>
      <li><strong>样本量</strong>：{summary['n_valid']} 个品种，共 {sum(r['baseline']['trades'] for r in results.values() if r.get('n_periods',0)>=2)} 笔 OOS 交易</li>
    </ul>

    <h3>下一步建议</h3>
    <div class="callout info">
      {'<strong>建议推进 Phase 8 全量开发</strong><br>PoC 验证通过，OOS 收益显著且方向稳定。可以按 8 人日计划启动全品种优化与上线。' if avg_delta >= 0.03 and pos_rate >= 60 else
       '<strong>建议扩大样本量后再决策</strong><br>当前样本量有限（品种少/交易数少），建议增加到 8-10 个品种再评估。如果结果一致，再推进 Phase 8。' if avg_delta > 0 and pos_rate >= 50 else
       '<strong>建议暂不推进 Phase 8</strong><br>PoC 结果不达预期，逐 Regime 优化未带来稳定的 OOS 收益。可考虑其他方向（如多参数维度扩展、组合优化等）。'}
    </div>

    <div class="footnote">
      <strong>验证方法：</strong>
      嵌套滚动验证（Nested Walk-Forward Validation）。
      训练期 {summary['init_train_bars']} 根 K 线，每 {summary['reopt_freq_bars']} 根重优化一次。
      内层：对趋势/震荡/波动三个 Regime 独立搜索 T 乘数（6 档）× stop 乘数（6 档），
      取该 Regime 下 expR 最高的组合。外层：用 IS 上找到的逐 Regime 最优系数在 OOS 上验证。
      基线为 DEFAULT_CONFIG 的全局 Regime 系数 + 逐品种手动调优值。
      每 Regime 最少 {summary['min_trades_per_regime']} 笔交易才做优化，否则沿用全局默认。
    </div>
  </section>

</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Phase 8 PoC：逐 Regime 参数差异化的嵌套滚动 OOS 验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m monitor.regime_poc --symbols zn,fu,pp
  python -m monitor.regime_poc --symbols zn --train-bars 750 --oos-bars 250
  python -m monitor.regime_poc --symbols zn,al --output phase8_poc.html
        """,
    )
    parser.add_argument("--symbols", type=str, default="zn,fu,pp",
                        help="验证品种列表，逗号分隔（默认 zn,fu,pp）")
    parser.add_argument("--train-bars", type=int, default=DEFAULT_INIT_TRAIN_BARS,
                        help=f"初始训练期长度（默认 {DEFAULT_INIT_TRAIN_BARS}）")
    parser.add_argument("--oos-bars", type=int, default=DEFAULT_REOPT_FREQ_BARS,
                        help=f"每个 OOS 周期长度（默认 {DEFAULT_REOPT_FREQ_BARS}）")
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES_PER_REGIME,
                        help=f"每 Regime 最少交易笔数（默认 {DEFAULT_MIN_TRADES_PER_REGIME}）")
    parser.add_argument("--output", type=str, default=None,
                        help="HTML 报告输出路径")
    parser.add_argument("--json", type=str, default=None,
                        help="JSON 结果输出路径")

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print(f"Phase 8 PoC — 逐 Regime 参数差异化 OOS 验证")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"品种: {', '.join(symbols)}")
    print(f"训练期: {args.train_bars} 根 | OOS 期: {args.oos_bars} 根")
    print(f"最小样本/Regime: {args.min_trades} 笔")
    print("-" * 65)

    data = run_poc(
        symbols,
        init_train_bars=args.train_bars,
        reopt_freq_bars=args.oos_bars,
        min_trades_per_regime=args.min_trades,
    )

    s = data["summary"]
    print("-" * 65)
    print(f"OOS 基线 expR:     {s['avg_baseline_expR']:+.4f}")
    print(f"OOS 优化后 expR:   {s['avg_opt_expR']:+.4f}")
    print(f"OOS ΔexpR:        {s['avg_delta_expR']:+.4f} (中位数 {s['median_delta_expR']:+.4f})")
    print(f"正收益品种:       {s['n_positive']}/{s['n_valid']} ({s['positive_rate']:.0f}%)")
    print(f"平均周期胜率:     {s['avg_period_win_rate']*100:.0f}%")

    # JSON 输出
    if args.json:
        # 移除大字段节省空间
        clean = copy.deepcopy(data)
        for sym, res in clean["results"].items():
            for p in res.get("periods", []):
                p.pop("opt_coefs", None)
                p.pop("regime_oos", None)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 结果: {args.json}")

    # HTML 输出
    output_path = args.output
    if not output_path:
        output_path = os.path.join(SCRIPT_DIR, "monitor", "phase8_poc_report.html")

    generate_report(data, output_path)
    print(f"HTML 报告: {output_path}")


if __name__ == "__main__":
    main()
