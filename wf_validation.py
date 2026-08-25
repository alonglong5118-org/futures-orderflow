# -*- coding: utf-8 -*-
"""
wf_validation.py — Walk-Forward 滚动验证工具
=============================================

对 GA 优化出的参数做严格的 Walk-Forward 验证：
  1. 把全量数据切成多个滚动窗口
  2. 用同一组参数在每个窗口上跑回测
  3. 统计各窗口表现的一致性、稳定性、退化程度

用法:
    # 单品种验证
    python3 wf_validation.py --result ga_tpsl_v2_ru_result.json

    # 指定窗口大小和步长
    python3 wf_validation.py --result ga_tpsl_v2_ru_result.json --window 250 --step 60

    # 同时对比基线
    python3 wf_validation.py --result ga_tpsl_v2_ru_result.json --compare-baseline
"""
from __future__ import annotations
import os
import sys
import json
import copy
import argparse
import numpy as np
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import four_dim_strategy as fd
from four_dim_strategy import walk_forward_backtest, DEFAULT_CONFIG, load_daily


# ============================================================================
# 指标计算
# ============================================================================

def _calc_max_drawdown(R_list):
    """从逐笔 R 收益序列计算最大回撤。"""
    if not R_list:
        return 0.0
    cumulative = np.cumsum(R_list)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    return float(np.max(drawdown)) if len(drawdown) > 0 else 0.0


def _calc_metrics(result):
    """从回测结果提取指标。"""
    trades = int(result.get("trades", 0))
    expR = float(result.get("expR") or 0.0)
    win_rate = float(result.get("win_rate") or 0.0)

    trades_detail = result.get("trades_detail", [])
    if trades_detail:
        R_list = [float(t.get("R_adj", 0)) for t in trades_detail]
        max_dd = _calc_max_drawdown(R_list)
    else:
        R_list = []
        max_dd = 0.0

    total_R = expR * trades if trades > 0 else 0.0
    calmar = total_R / max_dd if max_dd > 0.01 else (total_R * 100 if total_R > 0 else 0.0)

    return {
        "trades": trades,
        "expR": expR,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "total_R": total_R,
        "R_list": R_list,
    }


# ============================================================================
# 配置生成（兼容 v2 和 v3）
# ============================================================================

def make_config_v2(params, symbol, base_cfg=DEFAULT_CONFIG):
    """Phase 2: 5 个出场参数。"""
    stop_mult, rr_ratio, tail_pct, tail_trail_R, min_profit_R = params
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("per_symbol_risk", {})
    cfg["per_symbol_risk"][symbol] = {
        "stop_atr_mult": round(stop_mult, 4),
        "rr_ratio": round(rr_ratio, 4),
    }
    cfg.setdefault("trailing_tail", {})
    cfg["trailing_tail"]["tail_pct"] = round(tail_pct, 4)
    cfg["trailing_tail"]["tail_trail_R"] = round(tail_trail_R, 4)
    cfg["trailing_tail"]["min_profit_R"] = round(min_profit_R, 4)
    cfg["trailing_tail"]["enabled"] = True
    cfg["trailing_tail"]["trend_only"] = True
    return cfg


def make_config_v3(params, symbol, base_cfg=DEFAULT_CONFIG):
    """Phase 3: 4 个入场 + 5 个出场参数。"""
    (T_thresh_mult, fc_confirm, fc_hard, cooldown_bars,
     stop_mult, rr_ratio, tail_pct, tail_trail_R, min_profit_R) = params
    cfg = copy.deepcopy(base_cfg)

    # 入场参数
    cfg.setdefault("thresholds_by_symbol", {})
    if symbol not in cfg["thresholds_by_symbol"]:
        cfg["thresholds_by_symbol"][symbol] = {}
    # T_thresh 基线值
    t_base = cfg.get("thresholds_by_symbol", {}).get(symbol, {}).get("T_thresh", 22.0)
    cfg["thresholds_by_symbol"][symbol]["T_thresh"] = round(t_base * T_thresh_mult, 2)

    cfg.setdefault("fc_filter", {})
    cfg["fc_filter"]["confirm_pct"] = round(fc_confirm, 2)
    cfg["fc_filter"]["hard_reject_pct"] = round(fc_hard, 2)
    cfg["fc_filter"]["cooldown_bars"] = int(round(cooldown_bars))

    # 出场参数
    cfg.setdefault("per_symbol_risk", {})
    cfg["per_symbol_risk"][symbol] = {
        "stop_atr_mult": round(stop_mult, 4),
        "rr_ratio": round(rr_ratio, 4),
    }
    cfg.setdefault("trailing_tail", {})
    cfg["trailing_tail"]["tail_pct"] = round(tail_pct, 4)
    cfg["trailing_tail"]["tail_trail_R"] = round(tail_trail_R, 4)
    cfg["trailing_tail"]["min_profit_R"] = round(min_profit_R, 4)
    cfg["trailing_tail"]["enabled"] = True
    cfg["trailing_tail"]["trend_only"] = True
    return cfg


def detect_version_and_params(result_data):
    """从结果 JSON 自动检测版本和参数。"""
    cand = list(result_data["candidates"].values())[0]
    param_names = list(cand["params"].keys())
    params = [cand["params"][n] for n in param_names]

    if len(param_names) == 5:
        version = "v2"
        make_cfg = make_config_v2
    elif len(param_names) == 9:
        version = "v3"
        make_cfg = make_config_v3
    else:
        raise ValueError(f"无法识别版本，参数数量: {len(param_names)}")

    return version, param_names, params, make_cfg


def get_baseline_params(symbol, version):
    """获取基线参数。"""
    cfg = DEFAULT_CONFIG
    psr = cfg.get("per_symbol_risk", {}).get(symbol, {})

    if version == "v2":
        params = [
            psr.get("stop_atr_mult", cfg["risk_gate"]["stop_atr_mult"]),
            psr.get("rr_ratio", cfg["risk_gate"]["rr_ratio"]),
            cfg["trailing_tail"]["tail_pct"],
            cfg["trailing_tail"]["tail_trail_R"],
            cfg["trailing_tail"]["min_profit_R"],
        ]
        param_names = ["stop_atr_mult", "rr_ratio", "tail_pct", "tail_trail_R", "min_profit_R"]
        make_cfg = make_config_v2
    else:  # v3
        t_base = 22.0
        tbs = cfg.get("thresholds_by_symbol", {})
        if symbol in tbs and "T_thresh" in tbs[symbol]:
            t_base = float(tbs[symbol]["T_thresh"])
        params = [
            1.0,  # T_thresh_mult (基线 = 1.0)
            cfg.get("fc_filter", {}).get("confirm_pct", 25.0),
            cfg.get("fc_filter", {}).get("hard_reject_pct", 35.0),
            cfg.get("fc_filter", {}).get("cooldown_bars", 5),
            psr.get("stop_atr_mult", cfg["risk_gate"]["stop_atr_mult"]),
            psr.get("rr_ratio", cfg["risk_gate"]["rr_ratio"]),
            cfg["trailing_tail"]["tail_pct"],
            cfg["trailing_tail"]["tail_trail_R"],
            cfg["trailing_tail"]["min_profit_R"],
        ]
        param_names = [
            "T_thresh_mult", "fc_confirm", "fc_hard", "cooldown_bars",
            "stop_atr_mult", "rr_ratio", "tail_pct", "tail_trail_R", "min_profit_R",
        ]
        make_cfg = make_config_v3

    return param_names, params, make_cfg


# ============================================================================
# 滚动窗口回测
# ============================================================================

def rolling_windows(df, window_bars, step_bars, min_bars_init=60):
    """生成滚动窗口切片。
    
    返回 [(start_idx, end_idx, df_slice), ...]
    每个窗口长度 = window_bars
    """
    windows = []
    n = len(df)
    start = 0
    while start + window_bars <= n:
        end = start + window_bars
        df_slice = df.iloc[start:end].copy()
        windows.append((start, end, df_slice))
        start += step_bars
    return windows


def run_wf_validation(symbol, params, make_cfg, df_full, window_bars=250,
                      step_bars=60, min_bars_init=60, label="optimized"):
    """对一组参数做滚动窗口验证。
    
    返回 windows 列表，每个元素包含:
      - window_idx, start_date, end_date
      - metrics: expR, trades, win_rate, calmar, max_drawdown, total_R
    """
    print(f"\n🔄 开始 Walk-Forward 验证 [{label}]")
    print(f"   窗口大小: {window_bars} 根日K, 步长: {step_bars} 根")

    windows_data = rolling_windows(df_full, window_bars, step_bars, min_bars_init)
    print(f"   窗口数量: {len(windows_data)}")

    results = []
    for i, (start_idx, end_idx, df_win) in enumerate(windows_data):
        cfg = make_cfg(params, symbol)
        result = walk_forward_backtest(symbol, cfg=cfg, df_in=df_win, min_bars=min_bars_init)
        metrics = _calc_metrics(result)

        start_date = str(df_win.index[min_bars_init].date()) if len(df_win) > min_bars_init else str(df_win.index[0].date())
        end_date = str(df_win.index[-1].date())

        results.append({
            "window_idx": i,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "start_date": start_date,
            "end_date": end_date,
            "metrics": {k: v for k, v in metrics.items() if k != "R_list"},
        })

        status = "✅" if metrics["expR"] > 0 else "❌"
        print(f"   窗口{i+1:2d} [{start_date} ~ {end_date}]: "
              f"expR={metrics['expR']:.4f}, "
              f"trades={metrics['trades']:3d}, "
              f"win_rate={metrics['win_rate']:.1%} "
              f"{status}")

    return results


# ============================================================================
# 统计分析
# ============================================================================

def analyze_windows(windows_results, label="optimized"):
    """分析窗口结果，计算一致性指标。"""
    expRs = [w["metrics"]["expR"] for w in windows_results if w["metrics"]["trades"] >= 3]
    trades_list = [w["metrics"]["trades"] for w in windows_results]
    win_rates = [w["metrics"]["win_rate"] for w in windows_results if w["metrics"]["trades"] >= 3]
    calmars = [w["metrics"]["calmar"] for w in windows_results if w["metrics"]["trades"] >= 3]
    max_dds = [w["metrics"]["max_drawdown"] for w in windows_results if w["metrics"]["trades"] >= 3]

    n_valid = len(expRs)
    if n_valid == 0:
        return {"label": label, "valid_windows": 0}

    profitable = sum(1 for r in expRs if r > 0)
    win_rate_ratio = profitable / n_valid

    analysis = {
        "label": label,
        "total_windows": len(windows_results),
        "valid_windows": n_valid,
        "profitable_windows": profitable,
        "profit_ratio": round(win_rate_ratio, 4),
        # expR 统计
        "expR_median": round(float(np.median(expRs)), 4),
        "expR_mean": round(float(np.mean(expRs)), 4),
        "expR_std": round(float(np.std(expRs)), 4),
        "expR_min": round(float(np.min(expRs)), 4),
        "expR_max": round(float(np.max(expRs)), 4),
        "expR_p25": round(float(np.percentile(expRs, 25)), 4),
        "expR_p75": round(float(np.percentile(expRs, 75)), 4),
        # 其他指标
        "trades_total": sum(trades_list),
        "trades_per_window": round(float(np.mean(trades_list)), 1),
        "win_rate_median": round(float(np.median(win_rates)), 4),
        "calmar_median": round(float(np.median(calmars)), 2),
        "max_dd_median": round(float(np.median(max_dds)), 4),
        # 稳定性指标
        "expR_cv": round(float(np.std(expRs) / abs(np.mean(expRs))), 4) if abs(np.mean(expRs)) > 0.001 else 999.0,
    }

    return analysis


def print_analysis(analysis):
    """打印分析结果。"""
    label = analysis["label"]
    print(f"\n📊 Walk-Forward 分析结果 [{label}]")
    print(f"{'='*50}")
    print(f"   有效窗口数: {analysis['valid_windows']} / {analysis['total_windows']}")
    print(f"   盈利窗口占比: {analysis['profit_ratio']:.1%} ({analysis['profitable_windows']}/{analysis['valid_windows']})")
    print()
    print(f"   expR 中位数:  {analysis['expR_median']:.4f}")
    print(f"   expR 均值:    {analysis['expR_mean']:.4f}")
    print(f"   expR 标准差:  {analysis['expR_std']:.4f}")
    print(f"   expR 范围:    [{analysis['expR_min']:.4f}, {analysis['expR_max']:.4f}]")
    print(f"   expR 四分位:  [{analysis['expR_p25']:.4f}, {analysis['expR_p75']:.4f}]")
    print(f"   expR 变异系数: {analysis['expR_cv']:.4f}")
    print()
    print(f"   总交易笔数:   {analysis['trades_total']}")
    print(f"   每窗均笔数:   {analysis['trades_per_window']}")
    print(f"   胜率中位数:   {analysis['win_rate_median']:.1%}")
    print(f"   Calmar中位数: {analysis['calmar_median']:.2f}")
    print(f"   回撤中位数:   {analysis['max_dd_median']:.4f}R")


# ============================================================================
# 按年度分析
# ============================================================================

def analyze_by_year(df_full, symbol, params, make_cfg, min_bars_init=60, label="optimized"):
    """按自然年分析表现。"""
    print(f"\n📅 按年度分析 [{label}]")

    df = df_full.copy()
    years = sorted(set(df.index.year))

    yearly_results = []
    for year in years:
        df_year = df[df.index.year == year].copy()
        if len(df_year) < min_bars_init + 10:
            continue

        cfg = make_cfg(params, symbol)
        result = walk_forward_backtest(symbol, cfg=cfg, df_in=df_year, min_bars=min_bars_init)
        metrics = _calc_metrics(result)

        if metrics["trades"] >= 3:
            status = "✅" if metrics["expR"] > 0 else "❌"
            print(f"   {year}: expR={metrics['expR']:.4f}, "
                  f"trades={metrics['trades']:3d}, "
                  f"win_rate={metrics['win_rate']:.1%} "
                  f"{status}")

            yearly_results.append({
                "year": year,
                "metrics": {k: v for k, v in metrics.items() if k != "R_list"},
            })

    return yearly_results


# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 滚动验证工具")
    parser.add_argument("--result", type=str, required=True, help="GA 优化结果 JSON 文件路径")
    parser.add_argument("--window", type=int, default=250, help="滚动窗口大小（日K根数），默认250")
    parser.add_argument("--step", type=int, default=60, help="滚动步长（日K根数），默认60")
    parser.add_argument("--compare-baseline", action="store_true", help="同时对比基线参数")
    parser.add_argument("--yearly", action="store_true", help="额外输出年度分析")
    parser.add_argument("--output", type=str, default=None, help="输出 JSON 报告路径")
    args = parser.parse_args()

    # 加载结果
    with open(args.result, 'r') as f:
        result_data = json.load(f)

    symbol = result_data["symbol"]
    version, param_names, opt_params, make_cfg_opt = detect_version_and_params(result_data)

    print(f"\n{'='*60}")
    print(f"📈 Walk-Forward 滚动验证")
    print(f"{'='*60}")
    print(f"   品种: {symbol}")
    print(f"   版本: {version} ({len(param_names)} 参数)")
    print(f"   窗口: {args.window} 根, 步长: {args.step} 根")

    # 加载数据
    df_full = load_daily(symbol)
    if df_full is None:
        print(f"❌ 无法加载 {symbol} 数据")
        return

    print(f"   数据: {len(df_full)} 根日K ({df_full.index[0].date()} ~ {df_full.index[-1].date()})")

    # 优化参数验证
    opt_windows = run_wf_validation(
        symbol, opt_params, make_cfg_opt, df_full,
        window_bars=args.window, step_bars=args.step,
        label="优化参数"
    )
    opt_analysis = analyze_windows(opt_windows, label="优化参数")
    print_analysis(opt_analysis)

    # 基线对比
    baseline_analysis = None
    baseline_windows = None
    if args.compare_baseline:
        bl_names, bl_params, make_cfg_bl = get_baseline_params(symbol, version)
        print(f"\n📐 基线参数:")
        for n, v in zip(bl_names, bl_params):
            print(f"   {n} = {v}")

        baseline_windows = run_wf_validation(
            symbol, bl_params, make_cfg_bl, df_full,
            window_bars=args.window, step_bars=args.step,
            label="基线参数"
        )
        baseline_analysis = analyze_windows(baseline_windows, label="基线参数")
        print_analysis(baseline_analysis)

        # 对比
        print(f"\n⚖️ 优化 vs 基线 对比")
        print(f"{'='*50}")
        print(f"   指标          优化参数      基线参数      提升")
        print(f"   {'-'*48}")
        print(f"   盈利窗口占比   {opt_analysis['profit_ratio']:6.1%}      "
              f"{baseline_analysis['profit_ratio']:6.1%}      "
              f"{(opt_analysis['profit_ratio'] - baseline_analysis['profit_ratio']):+6.1%}")
        print(f"   expR 中位数    {opt_analysis['expR_median']:8.4f}    "
              f"{baseline_analysis['expR_median']:8.4f}    "
              f"{(opt_analysis['expR_median'] - baseline_analysis['expR_median']):+8.4f}")
        print(f"   expR 均值      {opt_analysis['expR_mean']:8.4f}    "
              f"{baseline_analysis['expR_mean']:8.4f}    "
              f"{(opt_analysis['expR_mean'] - baseline_analysis['expR_mean']):+8.4f}")
        print(f"   expR 标准差    {opt_analysis['expR_std']:8.4f}    "
              f"{baseline_analysis['expR_std']:8.4f}    "
              f"{'更低' if opt_analysis['expR_std'] < baseline_analysis['expR_std'] else '更高':>6s}")
        print(f"   Calmar 中位   {opt_analysis['calmar_median']:8.2f}    "
              f"{baseline_analysis['calmar_median']:8.2f}    "
              f"{(opt_analysis['calmar_median'] - baseline_analysis['calmar_median']):+8.2f}")

    # 年度分析
    opt_yearly = None
    baseline_yearly = None
    if args.yearly:
        opt_yearly = analyze_by_year(df_full, symbol, opt_params, make_cfg_opt, label="优化参数")
        if args.compare_baseline:
            bl_names, bl_params, make_cfg_bl = get_baseline_params(symbol, version)
            baseline_yearly = analyze_by_year(df_full, symbol, bl_params, make_cfg_bl, label="基线参数")

    # 输出报告
    report = {
        "symbol": symbol,
        "version": version,
        "param_names": param_names,
        "optimized_params": {param_names[i]: opt_params[i] for i in range(len(param_names))},
        "wf_config": {
            "window_bars": args.window,
            "step_bars": args.step,
            "total_bars": len(df_full),
            "data_start": str(df_full.index[0].date()),
            "data_end": str(df_full.index[-1].date()),
        },
        "optimized": {
            "analysis": opt_analysis,
            "windows": opt_windows,
        },
    }

    if baseline_analysis:
        report["baseline"] = {
            "params": {bl_names[i]: bl_params[i] for i in range(len(bl_names))},
            "analysis": baseline_analysis,
            "windows": baseline_windows,
        }

    if opt_yearly:
        report["optimized"]["yearly"] = opt_yearly
    if baseline_yearly:
        report["baseline"]["yearly"] = baseline_yearly

    # 保存
    output_path = args.output
    if output_path is None:
        base = os.path.splitext(os.path.basename(args.result))[0]
        output_path = os.path.join(HERE, f"{base}_wf_validation.json")

    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n💾 验证报告已保存: {output_path}")
    print()

    # 最终结论
    print(f"🎯 结论")
    print(f"{'='*50}")
    pr = opt_analysis["profit_ratio"]
    if pr >= 0.7:
        print(f"   ✅ 稳健性良好：{pr:.0%} 的窗口盈利")
    elif pr >= 0.5:
        print(f"   ⚠️ 稳健性一般：仅 {pr:.0%} 的窗口盈利")
    else:
        print(f"   ❌ 稳健性较差：仅 {pr:.0%} 的窗口盈利")

    cv = opt_analysis["expR_cv"]
    if cv < 0.5:
        print(f"   ✅ 表现稳定：变异系数 {cv:.2f}")
    elif cv < 1.0:
        print(f"   ⚠️ 表现波动：变异系数 {cv:.2f}")
    else:
        print(f"   ❌ 表现不稳定：变异系数 {cv:.2f}")

    if opt_analysis["expR_median"] > 0.3:
        print(f"   ✅ 收益可观：中位 expR = {opt_analysis['expR_median']:.4f}")
    elif opt_analysis["expR_median"] > 0:
        print(f"   ⚠️ 收益一般：中位 expR = {opt_analysis['expR_median']:.4f}")
    else:
        print(f"   ❌ 收益为负：中位 expR = {opt_analysis['expR_median']:.4f}")


if __name__ == "__main__":
    main()
