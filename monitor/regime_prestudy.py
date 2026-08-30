"""
Phase 8 预研：Regime 级参数差异化可行性分析

核心问题：
  现有方案：全局一套参数 × Regime 系数（T 乘数、stop 乘数）
  Phase 8 方案：每个 Regime 一套独立优化的参数（stop_atr_mult / T_thresh / rr_ratio）

预研目标：
  1. 统计各 Regime 的交易分布与表现差异
  2. 验证逐 Regime 优化是否能带来显著的 expR 提升
  3. 估算 Phase 8 的潜在收益上限（IS 上界，仅供参考）
  4. 识别哪些品种 / 哪些 Regime 最有优化空间

用法：
    python -m monitor.regime_prestudy --symbols zn,al,RB
    python -m monitor.regime_prestudy --all --output report.html
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
    DISABLED_SYMBOLS,
    SYMBOLS,
    walk_forward_backtest,
)
from strategy_layer import REGIME_CODE_TO_NAME  # noqa: E402


# ============================================================================
# 常量
# ============================================================================

REGIMES = ["趋势", "震荡", "波动", "过渡"]
MAIN_REGIMES = ["趋势", "震荡", "波动"]  # 排除过渡态，用于核心分析

# 预研用网格搜索范围（较宽，用于探测上限）
GRID_T_MULT_RANGE = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]
GRID_STOP_MULT_RANGE = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]
GRID_RR_RANGE = [1.5, 2.0, 2.5, 3.0]

# 最少交易笔数（低于此数的 Regime 不做优化）
MIN_TRADES_PER_REGIME = 8


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

    regime_coefs: {"趋势": {"T": 0.8, "stop": 1.2, "rr": 0.8}, ...}
    T 和 stop 是乘数（相对于基线），rr 是绝对值乘数
    """
    cfg = copy.deepcopy(base_cfg or DEFAULT_CONFIG)

    # 构建 per_symbol_regime_coef 结构
    per_sym = {}
    for regime, coefs in regime_coefs.items():
        per_sym[regime] = {}
        if "T" in coefs:
            per_sym[regime]["T"] = coefs["T"]
        if "stop" in coefs:
            per_sym[regime]["stop"] = coefs["stop"]

    cfg["per_symbol_regime_coef"] = {symbol: per_sym}
    return cfg


def _run_backtest(symbol: str, cfg: Dict[str, Any] = None, tail: int = None) -> Dict[str, Any]:
    """运行单次回测，返回标准化结果"""
    cfg = cfg or DEFAULT_CONFIG
    try:
        result = walk_forward_backtest(symbol, cfg, tail=tail)
        return result
    except Exception as e:
        return {"symbol": symbol, "trades": 0, "note": f"异常:{repr(e)[:80]}"}


def _regime_stats(trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    从逐笔交易中计算各 Regime 的统计指标。

    Returns:
        {regime_name: {"trades": N, "expR": float, "win_rate": float, "wins": N, "total_R": float}}
    """
    by_regime: Dict[str, List[float]] = {}
    for t in trades:
        regime = t.get("regime", "未知")
        r = t.get("R_adj", 0)
        by_regime.setdefault(regime, []).append(r)

    stats = {}
    for regime, rs in by_regime.items():
        wins = sum(1 for r in rs if r > 0)
        stats[regime] = {
            "trades": len(rs),
            "expR": round(float(np.mean(rs)), 4),
            "win_rate": round(wins / len(rs), 3) if rs else 0,
            "wins": wins,
            "total_R": round(float(np.sum(rs)), 4),
            "std_R": round(float(np.std(rs)), 4),
        }
    return stats


# ============================================================================
# 基线分析
# ============================================================================

def analyze_baseline(symbol: str, tail: int = None) -> Dict[str, Any]:
    """
    分析基线参数下的 Regime 表现。

    Returns:
        {
            "symbol": "...",
            "total_trades": N,
            "overall_expR": float,
            "by_regime": {regime: stats},
            "regime_distribution": {regime: pct},
        }
    """
    result = _run_backtest(symbol, tail=tail)
    trades = result.get("trades_detail", [])

    if not trades:
        return {
            "symbol": symbol,
            "total_trades": 0,
            "overall_expR": 0,
            "by_regime": {},
            "note": result.get("note", "无交易"),
        }

    regime_stats = _regime_stats(trades)
    total = len(trades)

    return {
        "symbol": symbol,
        "name": result.get("name", ""),
        "total_trades": total,
        "overall_expR": result.get("expR", 0),
        "overall_win_rate": result.get("win_rate", 0),
        "by_regime": regime_stats,
        "regime_distribution": {
            r: round(s["trades"] / total * 100, 1) if total > 0 else 0
            for r, s in regime_stats.items()
        },
        "by_regime_result": result.get("by_regime", {}),
    }


# ============================================================================
# 逐 Regime 网格搜索（IS 上界分析）
# ============================================================================

def optimize_per_regime(
    symbol: str,
    tail: int = None,
    min_trades: int = MIN_TRADES_PER_REGIME,
) -> Dict[str, Any]:
    """
    对每个 Regime 独立做网格搜索，找到最优参数组合。

    注意：这是 IS 分析，结果是上界估计，会有过拟合。
    用于判断「是否值得做 Phase 8」，而非最终参数。

    Returns:
        {
            "symbol": "...",
            "baseline_expR": float,
            "optimized_expR": float,
            "delta_expR": float,
            "per_regime_optimal": {regime: {T, stop, expR, trades}},
            "regimes_optimized": [regime1, regime2, ...],
            "note": "...",
        }
    """
    # 1. 先跑基线，获取逐笔交易
    baseline_result = _run_backtest(symbol, tail=tail)
    baseline_trades = baseline_result.get("trades_detail", [])

    if not baseline_trades:
        return {
            "symbol": symbol,
            "baseline_expR": 0,
            "optimized_expR": 0,
            "delta_expR": 0,
            "per_regime_optimal": {},
            "regimes_optimized": [],
            "note": "无交易数据",
        }

    baseline_expR = baseline_result.get("expR", 0)

    # 2. 按 Regime 分组统计
    regime_stats = _regime_stats(baseline_trades)

    # 3. 对每个有足够样本的 Regime 做网格搜索
    per_regime_optimal = {}
    optimized_regimes = []

    for regime in MAIN_REGIMES:
        stats = regime_stats.get(regime)
        if not stats or stats["trades"] < min_trades:
            continue

        best_expR = stats["expR"]  # 初始 = 基线
        best_params = {"T_mult": 1.0, "stop_mult": 1.0}
        best_trades = stats["trades"]

        # 网格搜索 T 乘数 × stop 乘数
        for t_mult in GRID_T_MULT_RANGE:
            for stop_mult in GRID_STOP_MULT_RANGE:
                # 构造配置：只修改该 regime 的 T 和 stop 系数
                regime_cfg = _make_regime_config(symbol, {
                    regime: {"T": t_mult, "stop": stop_mult}
                })

                result = _run_backtest(symbol, regime_cfg, tail=tail)
                trades = result.get("trades_detail", [])

                if not trades:
                    continue

                # 筛选该 regime 下的交易
                regime_trades = [t for t in trades if t.get("regime") == regime]
                if len(regime_trades) < min_trades:
                    continue

                regime_expR = float(np.mean([t["R_adj"] for t in regime_trades]))

                if regime_expR > best_expR:
                    best_expR = round(regime_expR, 4)
                    best_params = {"T_mult": t_mult, "stop_mult": stop_mult}
                    best_trades = len(regime_trades)

        per_regime_optimal[regime] = {
            "baseline_expR": stats["expR"],
            "optimized_expR": best_expR,
            "delta_expR": round(best_expR - stats["expR"], 4),
            "baseline_trades": stats["trades"],
            "optimized_trades": best_trades,
            "best_T_mult": best_params["T_mult"],
            "best_stop_mult": best_params["stop_mult"],
        }
        optimized_regimes.append(regime)

    # 4. 估算整体优化后的 expR（加权平均）
    if not per_regime_optimal:
        return {
            "symbol": symbol,
            "baseline_expR": baseline_expR,
            "optimized_expR": baseline_expR,
            "delta_expR": 0,
            "per_regime_optimal": {},
            "regimes_optimized": [],
            "note": "没有足够样本的 Regime",
        }

    # 用各 Regime 的交易数加权
    total_weight = 0
    weighted_expR = 0.0
    for regime, opt in per_regime_optimal.items():
        w = opt["optimized_trades"]
        weighted_expR += opt["optimized_expR"] * w
        total_weight += w

    # 加上未优化的 Regime（用基线值）
    for regime in MAIN_REGIMES:
        if regime not in per_regime_optimal and regime in regime_stats:
            w = regime_stats[regime]["trades"]
            weighted_expR += regime_stats[regime]["expR"] * w
            total_weight += w

    optimized_expR = round(weighted_expR / total_weight, 4) if total_weight > 0 else baseline_expR

    return {
        "symbol": symbol,
        "name": baseline_result.get("name", ""),
        "baseline_expR": baseline_expR,
        "baseline_trades": len(baseline_trades),
        "optimized_expR": optimized_expR,
        "delta_expR": round(optimized_expR - baseline_expR, 4),
        "delta_pct": round((optimized_expR - baseline_expR) / abs(baseline_expR) * 100, 1) if baseline_expR != 0 else 0,
        "per_regime_optimal": per_regime_optimal,
        "regimes_optimized": optimized_regimes,
        "baseline_regime_stats": regime_stats,
    }


# ============================================================================
# 批量分析
# ============================================================================

def run_prestudy(
    symbols: List[str],
    tail: int = None,
    min_trades: int = MIN_TRADES_PER_REGIME,
) -> Dict[str, Any]:
    """
    对多个品种运行预研分析。

    Returns:
        {
            "timestamp": "...",
            "symbols": [...],
            "results": {symbol: opt_result},
            "summary": {...},
        }
    """
    results = {}
    for sym in symbols:
        print(f"  分析 {sym}...", end=" ", flush=True)
        try:
            opt = optimize_per_regime(sym, tail=tail, min_trades=min_trades)
            results[sym] = opt
            print(f"expR: {opt['baseline_expR']:+.3f} → {opt['optimized_expR']:+.3f} "
                  f"(Δ={opt['delta_expR']:+.3f}) "
                  f"[{', '.join(opt['regimes_optimized'])}]")
        except Exception as e:
            print(f"异常: {e}")
            results[sym] = {"symbol": sym, "baseline_expR": 0, "optimized_expR": 0,
                            "delta_expR": 0, "regimes_optimized": [], "note": str(e)}

    # 汇总
    valid_results = [r for r in results.values() if r.get("baseline_trades", 0) >= 10]
    if valid_results:
        avg_baseline = np.mean([r["baseline_expR"] for r in valid_results])
        avg_optimized = np.mean([r["optimized_expR"] for r in valid_results])
        avg_delta = np.mean([r["delta_expR"] for r in valid_results])
        median_delta = np.median([r["delta_expR"] for r in valid_results])
        n_improved = sum(1 for r in valid_results if r["delta_expR"] > 0)
    else:
        avg_baseline = avg_optimized = avg_delta = median_delta = 0
        n_improved = 0

    # 按 Regime 统计平均提升
    regime_deltas = {r: [] for r in MAIN_REGIMES}
    for r in valid_results:
        for regime, opt in r.get("per_regime_optimal", {}).items():
            if regime in regime_deltas:
                regime_deltas[regime].append(opt["delta_expR"])

    regime_avg_deltas = {}
    for r, deltas in regime_deltas.items():
        if deltas:
            regime_avg_deltas[r] = round(float(np.mean(deltas)), 4)

    summary = {
        "n_symbols": len(symbols),
        "n_valid": len(valid_results),
        "avg_baseline_expR": round(float(avg_baseline), 4),
        "avg_optimized_expR": round(float(avg_optimized), 4),
        "avg_delta_expR": round(float(avg_delta), 4),
        "median_delta_expR": round(float(median_delta), 4),
        "n_improved": n_improved,
        "improvement_rate": round(n_improved / len(valid_results) * 100, 1) if valid_results else 0,
        "regime_avg_deltas": regime_avg_deltas,
        "min_trades_per_regime": min_trades,
        "tail_bars": tail,
    }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols,
        "results": results,
        "summary": summary,
    }


# ============================================================================
# HTML 报告生成
# ============================================================================

def generate_report(data: Dict[str, Any], output_path: str) -> str:
    """生成 Phase 8 预研 HTML 报告"""
    summary = data["summary"]
    results = data["results"]
    timestamp = data["timestamp"]

    # 品种结果表格
    rows_html = ""
    for sym, res in sorted(results.items(), key=lambda x: -x[1].get("delta_expR", -999)):
        baseline = res.get("baseline_expR", 0)
        optimized = res.get("optimized_expR", 0)
        delta = res.get("delta_expR", 0)
        trades = res.get("baseline_trades", 0)
        regimes = ", ".join(res.get("regimes_optimized", []))
        name = res.get("name", "")

        if delta > 0.02:
            delta_class = "pos"
            delta_icon = "▲"
        elif delta < -0.02:
            delta_class = "neg"
            delta_icon = "▼"
        else:
            delta_class = "neutral"
            delta_icon = "—"

        rows_html += f"""
        <tr>
          <td class="sym"><strong>{sym}</strong><br><span class="muted">{name}</span></td>
          <td class="num">{trades}</td>
          <td class="num">{baseline:+.3f}</td>
          <td class="num">{optimized:+.3f}</td>
          <td class="num {delta_class}">{delta_icon} {delta:+.3f}</td>
          <td>{regimes or "—"}</td>
        </tr>
        """

    # Regime 平均提升
    regime_deltas_html = ""
    for regime, delta in summary.get("regime_avg_deltas", {}).items():
        bar_width = min(abs(delta) * 200, 100)  # 缩放
        color = "#059669" if delta > 0 else "#dc2626"
        regime_deltas_html += f"""
        <div class="regime-row">
          <div class="regime-name">{regime}</div>
          <div class="regime-bar-wrap">
            <div class="regime-bar" style="width:{bar_width}%;background:{color}"></div>
          </div>
          <div class="regime-val">{delta:+.4f}</div>
        </div>
        """

    # 逐品种逐 Regime 详情
    detail_cards = ""
    for sym, res in sorted(results.items(), key=lambda x: -x[1].get("delta_expR", -999)):
        if not res.get("per_regime_optimal"):
            continue

        card_content = ""
        for regime, opt in res.get("per_regime_optimal", {}).items():
            bl = opt["baseline_expR"]
            opt_expR = opt["optimized_expR"]
            delta = opt["delta_expR"]
            n_trades = opt["optimized_trades"]
            t_mult = opt["best_T_mult"]
            stop_mult = opt["best_stop_mult"]

            card_content += f"""
            <div class="detail-regime">
              <div class="detail-regime-header">
                <span class="regime-tag">{regime}</span>
                <span class="detail-trades">{n_trades} 笔</span>
              </div>
              <div class="detail-grid">
                <div><span class="muted">基线 expR</span><br><strong>{bl:+.3f}</strong></div>
                <div><span class="muted">优化后</span><br><strong style="color:#059669">{opt_expR:+.3f}</strong></div>
                <div><span class="muted">Δ</span><br><strong class="{'pos' if delta>0 else 'neg'}">{delta:+.3f}</strong></div>
                <div><span class="muted">最优 T×</span><br><strong>{t_mult:.2f}</strong></div>
                <div><span class="muted">最优 stop×</span><br><strong>{stop_mult:.2f}</strong></div>
              </div>
            </div>
            """

        detail_cards += f"""
        <div class="detail-card">
          <div class="detail-card-header">
            <strong>{sym}</strong> {res.get('name','')}
            <span class="detail-delta">Δ {res.get('delta_expR',0):+.3f}</span>
          </div>
          <div class="detail-regimes-grid">
            {card_content}
          </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phase 8 预研报告 — Regime 级参数差异化</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", sans-serif;
    background: #f8fafc;
    color: #0f172a;
    font-size: 14px;
    line-height: 1.6;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 0 20px; }}

  .header {{
    background: linear-gradient(135deg, #7c3aed, #db2777);
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
    padding: 20px;
    margin-bottom: 20px;
  }}
  section h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 14px; color: #1e293b; }}
  section h3 {{ font-size: 14px; font-weight: 600; margin: 14px 0 8px; color: #334155; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #f1f5f9; }}
  th {{ background: #f8fafc; font-weight: 600; color: #475569; font-size: 12px; }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  .sym {{ min-width: 80px; }}
  .muted {{ color: #94a3b8; font-size: 11px; }}

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

  .regime-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }}
  .regime-name {{ width: 50px; font-size: 13px; font-weight: 500; }}
  .regime-bar-wrap {{
    flex: 1;
    height: 20px;
    background: #f1f5f9;
    border-radius: 4px;
    overflow: hidden;
  }}
  .regime-bar {{
    height: 100%;
    border-radius: 4px;
    transition: width 0.3s;
  }}
  .regime-val {{ width: 70px; text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}

  .detail-card {{
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 12px;
  }}
  .detail-card-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #f1f5f9;
  }}
  .detail-delta {{ font-weight: 700; font-size: 15px; }}
  .detail-regimes-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
  }}
  .detail-regime {{
    background: #f8fafc;
    border-radius: 6px;
    padding: 10px 12px;
  }}
  .detail-regime-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }}
  .regime-tag {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    background: #ede9fe;
    color: #6d28d9;
  }}
  .detail-trades {{ font-size: 11px; color: #64748b; }}
  .detail-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    font-size: 12px;
  }}
  .detail-grid > div {{ text-align: center; }}
  .detail-grid .muted {{ font-size: 10px; display: block; margin-bottom: 2px; }}

  .footnote {{
    font-size: 11px;
    color: #94a3b8;
    margin-top: 20px;
    padding-top: 12px;
    border-top: 1px solid #e2e8f0;
    line-height: 1.6;
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
    <h1>Phase 8 预研：Regime 级参数差异化</h1>
    <div class="subtitle">逐 Regime 独立优化 T 阈值与止损系数的潜在收益分析</div>
    <div class="meta">
      <span>📅 {timestamp}</span>
      <span>📊 分析品种：{summary['n_symbols']} 个</span>
      <span>📈 有效样本：{summary['n_valid']} 个品种</span>
      <span>🔬 最小样本/Regime：{summary['min_trades_per_regime']} 笔</span>
    </div>
  </div>
</div>

<div class="container">

  <div class="callout warning">
    <strong>⚠️ 注意：本报告为 IS（样本内）分析，结果是理论上界</strong><br>
    逐 Regime 网格搜索存在过拟合风险，实际 OOS 收益预计为 IS 收益的 30%~50%。
    本报告用于判断「是否值得投入 Phase 8」，而非给出最终参数。
  </div>

  <!-- KPI 概览 -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="label">平均基线 expR</div>
      <div class="value">{summary['avg_baseline_expR']:+.3f}</div>
      <div class="sub">{summary['n_valid']} 个有效品种</div>
    </div>
    <div class="kpi-card">
      <div class="label">优化后 expR（IS 上界）</div>
      <div class="value pos">{summary['avg_optimized_expR']:+.3f}</div>
      <div class="sub">逐 Regime 独立优化</div>
    </div>
    <div class="kpi-card">
      <div class="label">平均 ΔexpR</div>
      <div class="value {'pos' if summary['avg_delta_expR']>0 else 'neg'}">{summary['avg_delta_expR']:+.3f}</div>
      <div class="sub">中位数 {summary['median_delta_expR']:+.3f}</div>
    </div>
    <div class="kpi-card">
      <div class="label">改善比例</div>
      <div class="value pos">{summary['improvement_rate']:.0f}%</div>
      <div class="sub">{summary['n_improved']} / {summary['n_valid']} 个品种提升</div>
    </div>
  </div>

  <!-- 逐 Regime 平均提升 -->
  <section>
    <h2>各 Regime 平均 ΔexpR</h2>
    {regime_deltas_html if regime_deltas_html else '<p class="muted">暂无足够数据</p>'}
    <div class="footnote">
      正值表示该 Regime 下优化参数能带来提升。注意：这是 IS 结果，OOS 预计打 3~5 折。
    </div>
  </section>

  <!-- 品种结果总表 -->
  <section>
    <h2>品种结果总览</h2>
    <table>
      <thead>
        <tr>
          <th>品种</th>
          <th class="num">总交易</th>
          <th class="num">基线 expR</th>
          <th class="num">优化后 expR</th>
          <th class="num">ΔexpR</th>
          <th>可优化 Regime</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </section>

  <!-- 逐品种逐 Regime 详情 -->
  <section>
    <h2>逐品种 Regime 优化详情</h2>
    {detail_cards if detail_cards else '<p class="muted">暂无详情数据</p>'}
  </section>

  <!-- 结论与建议 -->
  <section>
    <h2>结论与建议</h2>

    <h3>1. 是否值得做 Phase 8？</h3>
    <p>
      IS 平均提升 <strong>{summary['avg_delta_expR']:+.3f}</strong>，
      按 OOS 折扣率 30%~50% 估算，实际收益约 <strong>+{summary['avg_delta_expR']*0.3:+.3f} ~ +{summary['avg_delta_expR']*0.5:+.3f}</strong>。
    </p>
    <p style="margin-top:8px">
      {'✅ 建议推进 Phase 8' if summary['avg_delta_expR']*0.3 >= 0.02 else '⚠️ 边际收益有限，建议优先其他方向'}
    </p>

    <h3>2. 优先级建议</h3>
    <ul style="padding-left:20px; margin-top:8px">
      <li><strong>先从波动市 Regime 入手</strong>：通常差异最大，优化空间最明显</li>
      <li><strong>重点品种</strong>：ΔexpR 最大的前 3~5 个品种优先做 walk-forward 验证</li>
      <li><strong>先做 2-Regime 简化版</strong>：高波动 / 非高波动，降低复杂度</li>
    </ul>

    <h3>3. 风险提示</h3>
    <ul style="padding-left:20px; margin-top:8px">
      <li>过拟合风险：逐 Regime 优化参数空间变大，需要更严格的 OOS 验证</li>
      <li>样本不足：部分 Regime 交易数少，统计置信度低</li>
      <li>Regime 切换成本：参数切换期间可能有额外滑点 / 摩擦</li>
    </ul>

    <div class="footnote">
      分析方法：对每个 Regime 独立做 T×stop 二维网格搜索（各 6 个水平 = 36 组合），
      取该 Regime 下 expR 最高的参数组合。所有结果基于全样本 IS，未做 walk-forward OOS 验证。
      下一步 Phase 8 需要用嵌套滚动验证来获得无偏估计。
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
        description="Phase 8 预研：Regime 级参数差异化分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m monitor.regime_prestudy --symbols zn,al,RB
  python -m monitor.regime_prestudy --all --output phase8_prestudy.html
  python -m monitor.regime_prestudy --symbols zn --min-trades 5 --tail 500
        """,
    )
    parser.add_argument("--symbols", type=str, default=None,
                        help="分析品种列表，逗号分隔（如 zn,al,RB）")
    parser.add_argument("--all", action="store_true",
                        help="分析所有非禁用品种")
    parser.add_argument("--output", type=str, default=None,
                        help="HTML 报告输出路径")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES_PER_REGIME,
                        help=f"每 Regime 最少交易笔数（默认 {MIN_TRADES_PER_REGIME}）")
    parser.add_argument("--tail", type=int, default=None,
                        help="仅用尾部 N 根 K 线（快速验证用）")
    parser.add_argument("--json", type=str, default=None,
                        help="JSON 结果输出路径")

    args = parser.parse_args()

    # 确定品种列表
    if args.all:
        symbols = [s for s in SYMBOLS.keys() if s not in DISABLED_SYMBOLS]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        # 默认：选几个代表性品种
        symbols = ["zn", "al", "RB", "M", "MA", "SR"]

    print(f"Phase 8 Regime 预研 — {len(symbols)} 个品种")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"最小样本/Regime: {args.min_trades} 笔")
    if args.tail:
        print(f"尾部模式: 最近 {args.tail} 根")
    print("-" * 60)

    data = run_prestudy(symbols, tail=args.tail, min_trades=args.min_trades)

    print("-" * 60)
    s = data["summary"]
    print(f"平均基线 expR: {s['avg_baseline_expR']:+.4f}")
    print(f"优化后 expR:   {s['avg_optimized_expR']:+.4f}")
    print(f"平均 ΔexpR:    {s['avg_delta_expR']:+.4f} (中位数 {s['median_delta_expR']:+.4f})")
    print(f"改善比例:     {s['improvement_rate']:.0f}% ({s['n_improved']}/{s['n_valid']})")
    print()
    print("各 Regime 平均 ΔexpR:")
    for r, d in s.get("regime_avg_deltas", {}).items():
        print(f"  {r}: {d:+.4f}")

    # 输出 JSON
    if args.json:
        # 移除 trades_detail 等大数据
        clean = copy.deepcopy(data)
        for sym, res in clean["results"].items():
            if "baseline_regime_stats" in res:
                res.pop("baseline_regime_stats", None)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 结果已保存: {args.json}")

    # 输出 HTML
    output_path = args.output
    if not output_path:
        output_path = os.path.join(SCRIPT_DIR, "monitor", "phase8_prestudy_report.html")

    generate_report(data, output_path)
    print(f"\nHTML 报告已生成: {output_path}")


if __name__ == "__main__":
    main()
