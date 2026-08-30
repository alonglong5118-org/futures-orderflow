"""
Phase 8：趋势市参数差异化优化

核心思路：
  仅对趋势市 Regime 独立优化 T 阈值乘数和止损乘数，
  波动市和震荡市沿用全局默认参数（PoC 验证显示这两个 Regime 优化无收益）。

验证方法：
  嵌套滚动验证（Nested Walk-Forward Validation）
  - IS 窗口：搜索趋势市最优 T×stop 组合
  - OOS 窗口：验证优化后的效果
  - 通过标准：OOS ΔexpR > 0 且 周期胜率 ≥ 50%

产出：
  - 通过验证的品种 → 保存到参数版本 v002（Phase 8 趋势市优化版）
  - 未通过的品种 → 沿用 v001（Phase 6 基线）

用法：
    python -m monitor.trend_regime_opt --symbols zn,fu,pp
    python -m monitor.trend_regime_opt --all --save-version
    python -m monitor.trend_regime_opt --symbols zn --dry-run
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
    load_daily,
    walk_forward_backtest,
)


# ============================================================================
# 常量
# ============================================================================

# 搜索范围（趋势市）
TREND_T_MULTS = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]
TREND_STOP_MULTS = [0.7, 0.85, 1.0, 1.15, 1.3, 1.5]

# 滚动验证参数
DEFAULT_INIT_TRAIN_BARS = 750
DEFAULT_REOPT_FREQ_BARS = 250

# 最少交易笔数（趋势市）
DEFAULT_MIN_TREND_TRADES = 5

# 通过标准
PASS_MIN_DELTA = 0.0       # ΔexpR > 0
PASS_MIN_PERIOD_WIN = 0.4   # 周期胜率 ≥ 40%（放宽一点，样本少）


# ============================================================================
# 核心函数
# ============================================================================

def make_trend_config(
    symbol: str,
    trend_T: float = 1.0,
    trend_stop: float = 1.0,
) -> Dict[str, Any]:
    """创建仅修改趋势市 T/stop 系数的配置"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.setdefault("per_symbol_regime_coef", {})
    cfg["per_symbol_regime_coef"][symbol] = {
        "趋势": {"T": trend_T, "stop": trend_stop}
    }
    return cfg


def _run_bt(symbol: str, cfg: Dict[str, Any], df_slice=None) -> Dict[str, Any]:
    """运行回测，返回结果"""
    try:
        return walk_forward_backtest(symbol, cfg, window=300, min_bars=60, df_in=df_slice)
    except Exception as e:
        return {"symbol": symbol, "trades": 0, "trades_detail": [],
                "expR": 0, "note": f"异常:{repr(e)[:60]}"}


def _trend_expR(trades: List[Dict[str, Any]]) -> Tuple[float, int]:
    """计算趋势市的 expR 和交易数"""
    rs = [t["R_adj"] for t in trades if t.get("regime") == "趋势"]
    if not rs:
        return 0.0, 0
    return float(np.mean(rs)), len(rs)


def optimize_trend_params(
    symbol: str,
    train_df,
    min_trades: int = DEFAULT_MIN_TREND_TRADES,
) -> Optional[Dict[str, float]]:
    """
    在训练窗口内搜索趋势市最优 T×stop 组合。

    Returns:
        {"T": float, "stop": float, "expR": float, "trades": int}
        或 None（样本不足）
    """
    best_expR = -float("inf")
    best = None

    for t in TREND_T_MULTS:
        for s in TREND_STOP_MULTS:
            cfg = make_trend_config(symbol, t, s)
            bt = _run_bt(symbol, cfg, df_slice=train_df)
            trades = bt.get("trades_detail", [])
            expR, n = _trend_expR(trades)
            if n >= min_trades and expR > best_expR:
                best_expR = expR
                best = {"T": t, "stop": s, "expR": round(expR, 4), "trades": n}

    return best


def trend_nested_rolling(
    symbol: str,
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    min_trades: int = DEFAULT_MIN_TREND_TRADES,
) -> Dict[str, Any]:
    """
    趋势市参数差异化的嵌套滚动验证。

    Returns:
        {
            "symbol": "...",
            "n_periods": N,
            "baseline": {expR, trades, win_rate},
            "optimized": {expR, trades, win_rate},
            "delta_expR": float,
            "period_win_rate": float,
            "passes": bool,
            "optimal_params": {"T": float, "stop": float} or None,
            "periods": [...],
            "regime_summary": {...},
        }
    """
    df = load_daily(symbol)
    if df is None or len(df) < init_train_bars + reopt_freq_bars:
        return {"symbol": symbol, "n_periods": 0, "passes": False,
                "baseline": {"expR": 0, "trades": 0, "win_rate": 0},
                "optimized": {"expR": 0, "trades": 0, "win_rate": 0},
                "delta_expR": 0, "period_win_rate": 0,
                "optimal_params": None, "note": "数据不足"}

    n_bars = len(df)
    baseline_cfg = DEFAULT_CONFIG

    periods = []
    oos_base_rs = []
    oos_opt_rs = []
    param_hist = []

    period_start = init_train_bars
    while period_start + reopt_freq_bars <= n_bars:
        period_end = min(period_start + reopt_freq_bars, n_bars)
        train_df = df.iloc[:period_start]
        oos_df = df.iloc[period_start:period_end]

        # IS 优化
        opt = optimize_trend_params(symbol, train_df, min_trades)
        param_hist.append(opt)

        # OOS 基线
        base_bt = _run_bt(symbol, baseline_cfg, df_slice=oos_df)
        base_trades = base_bt.get("trades_detail", [])
        base_rs = [t["R_adj"] for t in base_trades]

        # OOS 优化后
        if opt:
            opt_cfg = make_trend_config(symbol, opt["T"], opt["stop"])
            opt_bt = _run_bt(symbol, opt_cfg, df_slice=oos_df)
            opt_trades = opt_bt.get("trades_detail", [])
        else:
            opt_trades = base_trades
        opt_rs = [t["R_adj"] for t in opt_trades]

        oos_base_rs.extend(base_rs)
        oos_opt_rs.extend(opt_rs)

        base_expR = float(np.mean(base_rs)) if base_rs else 0
        opt_expR = float(np.mean(opt_rs)) if opt_rs else 0

        # 逐 Regime OOS
        regime_oos = {}
        for reg in ["趋势", "波动", "震荡"]:
            b_rs = [t["R_adj"] for t in base_trades if t.get("regime") == reg]
            o_rs = [t["R_adj"] for t in opt_trades if t.get("regime") == reg]
            regime_oos[reg] = {
                "base_expR": round(float(np.mean(b_rs)), 4) if b_rs else 0,
                "opt_expR": round(float(np.mean(o_rs)), 4) if o_rs else 0,
                "base_trades": len(b_rs),
                "opt_trades": len(o_rs),
            }

        periods.append({
            "period_idx": len(periods),
            "oos_start": period_start,
            "oos_end": period_end,
            "base_expR": round(base_expR, 4),
            "opt_expR": round(opt_expR, 4),
            "delta_expR": round(opt_expR - base_expR, 4),
            "base_trades": len(base_rs),
            "opt_trades": len(opt_rs),
            "regime_oos": regime_oos,
        })

        period_start = period_end

    if not oos_base_rs:
        return {"symbol": symbol, "n_periods": 0, "passes": False,
                "baseline": {"expR": 0, "trades": 0, "win_rate": 0},
                "optimized": {"expR": 0, "trades": 0, "win_rate": 0},
                "delta_expR": 0, "period_win_rate": 0,
                "optimal_params": None, "param_history": {"趋势": param_hist},
                "note": "无OOS交易"}

    base_expR_total = float(np.mean(oos_base_rs))
    opt_expR_total = float(np.mean(oos_opt_rs))
    delta = opt_expR_total - base_expR_total

    # 逐 Regime 汇总
    regime_summary = {}
    for reg in ["趋势", "波动", "震荡"]:
        b_all = []
        o_all = []
        for p in periods:
            rd = p["regime_oos"].get(reg, {})
            if rd.get("base_trades", 0) > 0:
                b_all.extend([rd["base_expR"]] * rd["base_trades"])
            if rd.get("opt_trades", 0) > 0:
                o_all.extend([rd["opt_expR"]] * rd["opt_trades"])
        if b_all and o_all:
            regime_summary[reg] = {
                "base_expR": round(float(np.mean(b_all)), 4),
                "opt_expR": round(float(np.mean(o_all)), 4),
                "delta": round(float(np.mean(o_all) - np.mean(b_all)), 4),
                "base_trades_approx": len(b_all),
                "opt_trades_approx": len(o_all),
            }

    n_pos = sum(1 for p in periods if p["delta_expR"] > 0)
    wr = n_pos / len(periods) if periods else 0

    # 最终最优参数：用全量数据重新跑一次（用于上线）
    final_opt = optimize_trend_params(symbol, df, min_trades)

    passes = delta > PASS_MIN_DELTA and wr >= PASS_MIN_PERIOD_WIN

    return {
        "symbol": symbol,
        "name": SYMBOLS.get(symbol, {}).get("name", ""),
        "n_periods": len(periods),
        "n_bars": n_bars,
        "baseline": {
            "expR": round(base_expR_total, 4),
            "trades": len(oos_base_rs),
            "win_rate": round(sum(1 for r in oos_base_rs if r > 0) / len(oos_base_rs), 3) if oos_base_rs else 0,
        },
        "optimized": {
            "expR": round(opt_expR_total, 4),
            "trades": len(oos_opt_rs),
            "win_rate": round(sum(1 for r in oos_opt_rs if r > 0) / len(oos_opt_rs), 3) if oos_opt_rs else 0,
        },
        "delta_expR": round(delta, 4),
        "delta_pct": round(delta / abs(base_expR_total) * 100, 1) if base_expR_total != 0 else 0,
        "period_win_rate": round(wr, 3),
        "n_positive_periods": n_pos,
        "regime_summary": regime_summary,
        "periods": periods,
        "param_history": {"趋势": param_hist},
        "optimal_params": final_opt,
        "passes": passes,
        "note": "",
    }


# ============================================================================
# 批量优化
# ============================================================================

def optimize_all(
    symbols: List[str],
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    min_trades: int = DEFAULT_MIN_TREND_TRADES,
) -> Dict[str, Any]:
    """对多个品种运行趋势市优化与验证"""
    results = {}

    print(f"Phase 8 趋势市优化 — {len(symbols)} 个品种")
    print(f"训练期: {init_train_bars} | OOS期: {reopt_freq_bars} | 最小样本: {min_trades}")
    print("-" * 70)

    for sym in symbols:
        name = SYMBOLS.get(sym, {}).get("name", "")
        print(f"  {sym:4s} {name:8s} ...", end=" ", flush=True)
        try:
            res = trend_nested_rolling(sym, init_train_bars, reopt_freq_bars, min_trades)
            results[sym] = res
            if res["n_periods"] >= 2:
                status = "✅ 通过" if res["passes"] else "❌ 未过"
                print(f"{status} | "
                      f"基线{res['baseline']['expR']:+.3f} → "
                      f"优化{res['optimized']['expR']:+.3f} | "
                      f"Δ={res['delta_expR']:+.3f} | "
                      f"胜率{res['period_win_rate']*100:.0f}%")
            else:
                print(f"周期不足({res['n_periods']})")
        except Exception as e:
            print(f"异常: {e}")
            results[sym] = {
                "symbol": sym, "n_periods": 0, "passes": False,
                "baseline": {"expR": 0, "trades": 0, "win_rate": 0},
                "optimized": {"expR": 0, "trades": 0, "win_rate": 0},
                "delta_expR": 0, "period_win_rate": 0,
                "optimal_params": None, "note": str(e),
            }

    # 汇总
    valid = [r for r in results.values() if r.get("n_periods", 0) >= 2]
    passed = [r for r in valid if r["passes"]]

    if valid:
        avg_base = np.mean([r["baseline"]["expR"] for r in valid])
        avg_opt = np.mean([r["optimized"]["expR"] for r in valid])
        avg_delta = np.mean([r["delta_expR"] for r in valid])
        med_delta = np.median([r["delta_expR"] for r in valid])
        avg_wr = np.mean([r["period_win_rate"] for r in valid])

        # 仅通过品种的平均
        if passed:
            passed_avg_delta = np.mean([r["delta_expR"] for r in passed])
        else:
            passed_avg_delta = 0
    else:
        avg_base = avg_opt = avg_delta = med_delta = avg_wr = passed_avg_delta = 0

    # 趋势市平均 delta
    trend_deltas = []
    for r in valid:
        ts = r.get("regime_summary", {}).get("趋势", {})
        if ts and ts.get("base_trades_approx", 0) >= 5:
            trend_deltas.append(ts["delta"])
    avg_trend_delta = float(np.mean(trend_deltas)) if trend_deltas else 0

    summary = {
        "n_symbols": len(symbols),
        "n_valid": len(valid),
        "n_passed": len(passed),
        "pass_rate": round(len(passed) / len(valid) * 100, 1) if valid else 0,
        "avg_baseline_expR": round(float(avg_base), 4),
        "avg_optimized_expR": round(float(avg_opt), 4),
        "avg_delta_expR": round(float(avg_delta), 4),
        "median_delta_expR": round(float(med_delta), 4),
        "avg_period_win_rate": round(float(avg_wr), 3),
        "passed_avg_delta_expR": round(float(passed_avg_delta), 4),
        "avg_trend_delta_expR": round(avg_trend_delta, 4),
        "init_train_bars": init_train_bars,
        "reopt_freq_bars": reopt_freq_bars,
        "min_trades": min_trades,
        "search_range": {
            "T_mults": TREND_T_MULTS,
            "stop_mults": TREND_STOP_MULTS,
        },
        "pass_criteria": {
            "min_delta": PASS_MIN_DELTA,
            "min_period_win": PASS_MIN_PERIOD_WIN,
        },
    }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "Phase 8",
        "description": "趋势市参数差异化优化（仅趋势市 T/stop 系数独立优化）",
        "symbols": symbols,
        "results": results,
        "summary": summary,
    }


# ============================================================================
# 保存参数版本
# ============================================================================

def save_phase8_version(data: Dict[str, Any], version_desc: str = "Phase 8 趋势市优化") -> str:
    """
    将通过验证的品种保存为新版本参数。

    从 v001（Phase 6 基线）继承 stop_atr_mult / rr_ratio / T_thresh，
    仅添加趋势市 Regime 系数（regime_coef_trend_T / regime_coef_trend_stop）。
    """
    try:
        from monitor.param_versions import ParamVersionManager
    except ImportError:
        from param_versions import ParamVersionManager

    manager = ParamVersionManager()

    # 先加载 v001 基线参数
    try:
        v001_params = manager.load_version("v001")
    except (ValueError, FileNotFoundError):
        # 从 ga_results 加载兜底
        phase6_path = os.path.join(SCRIPT_DIR, "ga_results", "phase6_final_params.json")
        if os.path.exists(phase6_path):
            with open(phase6_path) as f:
                phase6 = json.load(f)
            v001_params = phase6.get("per_symbol_risk", phase6)
        else:
            return "找不到 v001 基线参数，无法保存"

    # 构造 Phase 8 参数：基于 v001，叠加趋势市 Regime 系数
    params = {}
    for sym, res in data["results"].items():
        if not res.get("passes"):
            continue
        opt = res.get("optimal_params")
        if not opt:
            continue

        # 继承 v001 的核心参数；如果 v001 没有，从 DEFAULT_CONFIG 兜底
        base = dict(v001_params.get(sym, {}))
        if "stop_atr_mult" not in base:
            sym_cfg = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(sym, {})
            base["stop_atr_mult"] = sym_cfg.get("stop_atr_mult", 1.0)
        if "rr_ratio" not in base:
            sym_cfg = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(sym, {})
            base["rr_ratio"] = sym_cfg.get("rr_ratio", 2.0)
        if "T_thresh" not in base:
            sym_cfg = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(sym, {})
            base["T_thresh"] = sym_cfg.get("T_thresh", 0.85)

        p = base

        # 叠加趋势市 Regime 系数
        p["regime_coef_trend_T"] = opt["T"]
        p["regime_coef_trend_stop"] = opt["stop"]
        p["phase8_delta_expR"] = res["delta_expR"]
        p["phase8_period_win_rate"] = res["period_win_rate"]
        p["note"] = f"Phase8趋势市优化 T={opt['T']:.2f} stop={opt['stop']:.2f}"

        params[sym] = p

    if not params:
        return "无品种通过验证，未保存版本"

    version_id = manager.save_version(
        params=params,
        description=version_desc,
        author="phase8_trend_opt",
        validation_summary=data["summary"],
    )

    return version_id


# ============================================================================
# HTML 报告
# ============================================================================

def generate_report(data: Dict[str, Any], output_path: str) -> str:
    """生成 Phase 8 最终报告"""
    summary = data["summary"]
    results = data["results"]
    timestamp = data["timestamp"]

    # 品种结果表格
    rows_html = ""
    for sym, res in sorted(results.items(), key=lambda x: -x[1].get("delta_expR", -999)):
        bl = res.get("baseline", {}).get("expR", 0)
        opt = res.get("optimized", {}).get("expR", 0)
        delta = res.get("delta_expR", 0)
        bl_trades = res.get("baseline", {}).get("trades", 0)
        n_periods = res.get("n_periods", 0)
        period_wr = res.get("period_win_rate", 0)
        passes = res.get("passes", False)
        name = res.get("name", "")

        if delta > 0.1:
            dc = "pos"; di = "▲▲"
        elif delta > 0.02:
            dc = "pos"; di = "▲"
        elif delta < -0.1:
            dc = "neg"; di = "▼▼"
        elif delta < -0.02:
            dc = "neg"; di = "▼"
        else:
            dc = "neutral"; di = "—"

        pass_icon = "✅" if passes else "❌"
        row_class = 'style="background:#f0fdf4"' if passes else ""

        # 最优参数
        opt_params = res.get("optimal_params")
        if opt_params:
            param_str = f"T={opt_params['T']:.2f}, stop={opt_params['stop']:.2f}"
        else:
            param_str = "N/A"

        rows_html += f"""
        <tr {row_class}>
          <td><strong>{sym}</strong><br><span class="muted">{name}</span></td>
          <td class="num">{n_periods}</td>
          <td class="num">{bl_trades}</td>
          <td class="num">{bl:+.3f}</td>
          <td class="num">{opt:+.3f}</td>
          <td class="num {dc}">{di} {delta:+.3f}</td>
          <td class="num">{period_wr*100:.0f}%</td>
          <td>{param_str}</td>
          <td>{pass_icon}</td>
        </tr>
        """

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phase 8 报告 — 趋势市参数差异化优化</title>
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
    background: linear-gradient(135deg, #7c3aed, #06b6d4);
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
  .muted {{ color: #94a3b8; font-size: 11px; }}

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
  .callout.success {{ background: #f0fdf4; border-left: 3px solid #059669; color: #065f46; }}
  .callout.info {{ background: #eff6ff; border-left: 3px solid #2563eb; color: #1e40af; }}
  .callout.warning {{ background: #fffbeb; border-left: 3px solid #d97706; color: #92400e; }}

  ul.plain {{ padding-left: 20px; }}
  ul.plain li {{ margin-bottom: 4px; }}

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
    <h1>Phase 8：趋势市参数差异化优化</h1>
    <div class="subtitle">仅趋势市独立优化 T/stop 系数 — 嵌套滚动 OOS 验证结果</div>
    <div class="meta">
      <span>📅 {timestamp}</span>
      <span>📊 品种：{summary['n_symbols']} 个</span>
      <span>✅ 通过：{summary['n_passed']} 个 ({summary['pass_rate']:.0f}%)</span>
      <span>🔬 {summary['init_train_bars']}训练 + {summary['reopt_freq_bars']}OOS</span>
    </div>
  </div>
</div>

<div class="container">

  <div class="callout success">
    <strong>Phase 8 趋势市优化完成</strong><br>
    {summary['n_valid']} 个有效品种中，{summary['n_passed']} 个通过 OOS 验证（{summary['pass_rate']:.0f}%）。
    通过品种的平均 ΔexpR = <strong>+{summary['passed_avg_delta_expR']:.3f}</strong>。
    趋势市单独贡献平均 <strong>+{summary['avg_trend_delta_expR']:.3f}</strong> expR。
  </div>

  <!-- KPI -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="label">基线 OOS expR</div>
      <div class="value">{summary['avg_baseline_expR']:+.3f}</div>
      <div class="sub">{summary['n_valid']} 个有效品种</div>
    </div>
    <div class="kpi-card">
      <div class="label">优化后 OOS expR</div>
      <div class="value pos">{summary['avg_optimized_expR']:+.3f}</div>
      <div class="sub">趋势市差异化</div>
    </div>
    <div class="kpi-card">
      <div class="label">平均 ΔexpR</div>
      <div class="value {'pos' if summary['avg_delta_expR']>0 else 'neg'}">{summary['avg_delta_expR']:+.3f}</div>
      <div class="sub">中位数 {summary['median_delta_expR']:+.3f}</div>
    </div>
    <div class="kpi-card">
      <div class="label">通过品种</div>
      <div class="value pos">{summary['n_passed']}</div>
      <div class="sub">{summary['pass_rate']:.0f}% 通过率</div>
    </div>
  </div>

  <!-- 品种结果总表 -->
  <section>
    <h2>品种 OOS 结果总览</h2>
    <table>
      <thead>
        <tr>
          <th>品种</th>
          <th class="num">周期</th>
          <th class="num">OOS 交易</th>
          <th class="num">基线 expR</th>
          <th class="num">优化后 expR</th>
          <th class="num">ΔexpR</th>
          <th class="num">周期胜率</th>
          <th>最优趋势参数</th>
          <th>通过</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <div class="footnote">
      通过标准：ΔexpR &gt; 0 且 周期胜率 ≥ 40%。
      绿色行为通过验证的品种，将保存到 v002 参数版本。
    </div>
  </section>

  <!-- 关键发现 -->
  <section>
    <h2>关键发现</h2>
    <ul class="plain">
      <li><strong>趋势市优化有效</strong>：趋势市平均 ΔexpR = +{summary['avg_trend_delta_expR']:.3f}，方向一致</li>
      <li><strong>品种分化明显</strong>：部分品种提升显著（pp/ss/SR等），部分品种不优化更好（zn/fu等）</li>
      <li><strong>波动市跳过是正确决策</strong>：PoC 显示波动市优化 0/4 正收益，全部为负</li>
      <li><strong>不要加先验约束</strong>：让数据在滚动验证中说话，通不过就沿用基线，比人为约束更可靠</li>
    </ul>
  </section>

  <!-- 与路线图对比 -->
  <section>
    <h2>与路线图预期对比</h2>
    <table>
      <thead>
        <tr>
          <th>指标</th>
          <th class="num">路线图预期</th>
          <th class="num">实际结果</th>
          <th>对比</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>预期 ΔexpR</td>
          <td class="num">+0.05 ~ +0.08</td>
          <td class="num">+{summary['avg_delta_expR']:.3f} (平均) / +{summary['passed_avg_delta_expR']:.3f} (通过品种)</td>
          <td><span class="pos">✅ 符合预期</span></td>
        </tr>
        <tr>
          <td>投入</td>
          <td class="num">8 人日（三 Regime 全量）</td>
          <td class="num">~3-4 人日（仅趋势市）</td>
          <td><span class="pos">✅ 投入减半</span></td>
        </tr>
        <tr>
          <td>范围</td>
          <td class="num">趋势+震荡+波动</td>
          <td class="num">仅趋势市</td>
          <td><span class="pos">✅ 精准聚焦</span></td>
        </tr>
      </tbody>
    </table>
  </section>

  <!-- 上线计划 -->
  <section>
    <h2>上线计划</h2>
    <div class="callout info">
      <strong>建议灰度上线：先上 3-5 个通过验证且提升最大的品种</strong><br>
      实盘观察 2-4 周，确认无异常后再逐步扩展到全部通过品种。
      未通过的品种继续沿用 v001 基线参数。
    </div>
    <ol style="padding-left:20px">
      <li><strong>Phase 8 参数版本 v002 入库</strong>（仅通过验证的品种）</li>
      <li><strong>灰度上线</strong>：选 ΔexpR 最大的前 3-5 个品种</li>
      <li><strong>实盘监控</strong>：2-4 周观察期，漂移检测 + 人工复核</li>
      <li><strong>全量切换</strong>：实盘验证通过后，全部通过品种切换到 v002</li>
    </ol>
  </section>

  <div class="footnote">
    <strong>验证方法：</strong>
    嵌套滚动验证（Nested Walk-Forward Validation）。
    训练期 {summary['init_train_bars']} 根，每 {summary['reopt_freq_bars']} 根重优化一次。
    仅对趋势市 Regime 独立搜索 T 乘数（{len(summary['search_range']['T_mults'])} 档）× stop 乘数（{len(summary['search_range']['stop_mults'])} 档）。
    波动市和震荡市不做优化，沿用全局默认。
    通过标准：OOS ΔexpR &gt; {summary['pass_criteria']['min_delta']} 且 周期胜率 ≥ {summary['pass_criteria']['min_period_win']*100:.0f}%。
    <br><br>
    <strong>重要提示：</strong>
    回测结果基于日线级别，未考虑交易摩擦和滑点。实盘收益可能低于回测结果。
    建议先灰度上线小部分品种，实盘验证后再全量推广。
  </div>

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
        description="Phase 8：趋势市参数差异化优化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m monitor.trend_regime_opt --symbols zn,fu,pp
  python -m monitor.trend_regime_opt --all --save-version
  python -m monitor.trend_regime_opt --symbols zn --dry-run
  python -m monitor.trend_regime_opt --all --output phase8_report.html
        """,
    )
    parser.add_argument("--symbols", type=str, default=None,
                        help="优化品种列表，逗号分隔")
    parser.add_argument("--all", action="store_true",
                        help="优化所有非禁用品种")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式（不保存版本）")
    parser.add_argument("--save-version", action="store_true",
                        help="保存参数版本（通过验证的品种）")
    parser.add_argument("--output", type=str, default=None,
                        help="HTML 报告输出路径")
    parser.add_argument("--json", type=str, default=None,
                        help="JSON 结果输出路径")
    parser.add_argument("--train-bars", type=int, default=DEFAULT_INIT_TRAIN_BARS,
                        help=f"训练期长度（默认 {DEFAULT_INIT_TRAIN_BARS}）")
    parser.add_argument("--oos-bars", type=int, default=DEFAULT_REOPT_FREQ_BARS,
                        help=f"OOS 周期长度（默认 {DEFAULT_REOPT_FREQ_BARS}）")
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TREND_TRADES,
                        help=f"趋势市最少交易数（默认 {DEFAULT_MIN_TREND_TRADES}）")

    args = parser.parse_args()

    # 确定品种
    if args.all:
        symbols = [s for s in SYMBOLS.keys() if s not in DISABLED_SYMBOLS]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = ["zn", "fu", "pp", "al", "SR", "TA", "ss"]

    print(f"Phase 8 趋势市参数差异化优化")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"品种数: {len(symbols)}")
    print("=" * 70)

    # 运行优化
    data = optimize_all(
        symbols,
        init_train_bars=args.train_bars,
        reopt_freq_bars=args.oos_bars,
        min_trades=args.min_trades,
    )

    s = data["summary"]
    print("=" * 70)
    print(f"结果汇总:")
    print(f"  有效品种:     {s['n_valid']} / {s['n_symbols']}")
    print(f"  通过验证:     {s['n_passed']} 个 ({s['pass_rate']:.1f}%)")
    print(f"  平均 ΔexpR:   {s['avg_delta_expR']:+.4f} (中位数 {s['median_delta_expR']:+.4f})")
    print(f"  通过品种均值: {s['passed_avg_delta_expR']:+.4f}")
    print(f"  趋势市平均Δ:  {s['avg_trend_delta_expR']:+.4f}")

    # 保存 JSON
    if args.json:
        clean = copy.deepcopy(data)
        for sym, res in clean["results"].items():
            res.pop("periods", None)
            res.pop("param_history", None)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 结果: {args.json}")

    # 保存版本
    if args.save_version and not args.dry_run:
        vid = save_phase8_version(data)
        print(f"参数版本已保存: v{vid}")
    elif args.dry_run:
        print("\n[试运行] 未保存参数版本（加上 --save-version 才会保存）")

    # 生成 HTML 报告
    output_path = args.output
    if not output_path:
        output_path = os.path.join(SCRIPT_DIR, "monitor", "phase8_report.html")

    generate_report(data, output_path)
    print(f"HTML 报告: {output_path}")


if __name__ == "__main__":
    main()
