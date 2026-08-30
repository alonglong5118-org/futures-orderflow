"""
季度重优化脚本 (Phase 7, Task 3)

功能：
- 封装 Phase 6 的全流程：紧边界网格搜索 → 嵌套滚动验证 → 筛选通过品种
- 支持一键运行季度重优化
- 自动保存新版本参数
- 生成验证报告
- 与参数版本管理器集成

用法（命令行）：
    python -m monitor.quarterly_reopt --output ga_results/q4_2026_reopt.json

用法（Python）：
    from monitor.quarterly_reopt import run_quarterly_reoptimization
    result = run_quarterly_reoptimization(
        symbols=["zn", "al", "c", ...],
        T_range=4,
        stop_range=0.3,
        rr_range=0.5,
    )
"""

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from four_dim_strategy import (  # noqa: E402
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)
from monitor.param_versions import ParamVersionManager  # noqa: E402

# 默认搜索范围（紧边界）
DEFAULT_T_RANGE = 4  # ±4
DEFAULT_STOP_RANGE = 0.3  # ±0.3
DEFAULT_RR_RANGE = 0.5  # ±0.5

# 滚动验证参数
DEFAULT_INIT_TRAIN_BARS = 750
DEFAULT_REOPT_FREQ_BARS = 250

# 验证门槛
DEFAULT_MIN_DELTA = 0.03
DEFAULT_MIN_TRADES_RATIO = 0.7
DEFAULT_MAX_TRADES_RATIO = 1.4


def _get_baseline_params(symbol: str) -> Dict[str, float]:
    """获取品种的基线参数（从 DEFAULT_CONFIG 的 per_symbol_risk 中读取）"""
    per_sym = DEFAULT_CONFIG.get("per_symbol_risk", {})
    sym_cfg = per_sym.get(symbol, {})
    return {
        "T": sym_cfg.get("T_thresh", DEFAULT_CONFIG.get("T_thresh", 14)),
        "stop": sym_cfg.get("stop_atr_mult", DEFAULT_CONFIG["risk_gate"]["stop_atr_mult"]),
        "rr": sym_cfg.get("rr_ratio", DEFAULT_CONFIG["risk_gate"]["rr_ratio"]),
    }


def _make_symbol_config(symbol: str, T: float, stop: float, rr: float) -> Dict[str, Any]:
    """创建单品种的配置（覆盖 stop_atr_mult 和 rr_ratio）"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["per_symbol_risk"] = {
        symbol: {
            "stop_atr_mult": round(stop, 2),
            "rr_ratio": round(rr, 2),
        }
    }
    # T_thresh 在 risk_gate 或其他位置，这里按 per_symbol_risk 的方式覆盖
    # 注意：T_thresh 的具体覆盖方式取决于策略实现，这里采用保守方式
    return cfg


def _grid_combinations(
    base_T: float,
    base_stop: float,
    base_rr: float,
    T_range: float = DEFAULT_T_RANGE,
    stop_range: float = DEFAULT_STOP_RANGE,
    rr_range: float = DEFAULT_RR_RANGE,
    T_step: int = 2,
    stop_step: float = 0.1,
    rr_step: float = 0.5,
) -> List[Tuple[float, float, float]]:
    """
    生成紧边界网格搜索的所有参数组合。

    Args:
        base_T, base_stop, base_rr: 基线参数
        T_range: T 的搜索范围（±T_range）
        stop_range: stop 的搜索范围（±stop_range）
        rr_range: rr 的搜索范围（±rr_range）
        T_step: T 的步长（整数步）
        stop_step: stop 的步长
        rr_step: rr 的步长

    Returns:
        [(T, stop, rr), ...] 所有组合列表
    """
    combos = []

    T_vals = _frange(base_T - T_range, base_T + T_range + T_step / 2, T_step, int_val=True)
    stop_vals = _frange(base_stop - stop_range, base_stop + stop_range + stop_step / 2, stop_step)
    rr_vals = _frange(base_rr - rr_range, base_rr + rr_range + rr_step / 2, rr_step)

    # 过滤不合理值
    T_vals = [t for t in T_vals if t >= 2]
    stop_vals = [s for s in stop_vals if s >= 0.5]
    rr_vals = [r for r in rr_vals if r >= 1.0]

    for T in T_vals:
        for stop in stop_vals:
            for rr in rr_vals:
                combos.append((round(T, 2), round(stop, 2), round(rr, 2)))

    return combos


def _frange(start: float, end: float, step: float, int_val: bool = False) -> List[float]:
    """浮点范围生成器"""
    vals = []
    v = start
    while v <= end + 1e-9:
        if int_val:
            vals.append(int(round(v)))
        else:
            vals.append(round(v, 4))
        v += step
    return vals


def _evaluate_params(
    symbol: str,
    T: float,
    stop: float,
    rr: float,
    df_slice=None,
    tail: Optional[int] = None,
) -> Dict[str, Any]:
    """
    评估一组参数，返回 expR 和交易数。

    Args:
        symbol: 品种代码
        T, stop, rr: 参数组合
        df_slice: 预切分的 DataFrame（用于滚动验证）
        tail: 仅回测尾部 N 根 K 线

    Returns:
        {"expR": float, "trades": int, "win_rate": float, ...}
    """
    cfg = _make_symbol_config(symbol, T, stop, rr)

    try:
        result = walk_forward_backtest(
            symbol,
            cfg=cfg,
            window=300,
            min_bars=60,
            tail=tail,
            df_in=df_slice,
        )
        return {
            "expR": float(result.get("expR", 0)),
            "trades": int(result.get("trades", 0)),
            "win_rate": float(result.get("win_rate", 0)),
            "total_pnl": float(result.get("total_pnl", 0)),
        }
    except Exception as e:
        return {
            "expR": -999.0,
            "trades": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "error": str(e),
        }


def nested_rolling_validation(
    symbol: str,
    base_params: Dict[str, float],
    T_range: float = DEFAULT_T_RANGE,
    stop_range: float = DEFAULT_STOP_RANGE,
    rr_range: float = DEFAULT_RR_RANGE,
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    min_delta: float = DEFAULT_MIN_DELTA,
    min_trades_ratio: float = DEFAULT_MIN_TRADES_RATIO,
    max_trades_ratio: float = DEFAULT_MAX_TRADES_RATIO,
) -> Dict[str, Any]:
    """
    嵌套滚动验证：
    - 内层：每个训练期内做紧边界网格搜索，找到最优参数
    - 外层：用最优参数在接下来的验证期上测试（OOS）
    - 汇总所有 OOS 周期的表现

    Args:
        symbol: 品种代码
        base_params: 基线参数 {"T", "stop", "rr"}
        T_range, stop_range, rr_range: 紧边界搜索范围
        init_train_bars: 初始训练期长度
        reopt_freq_bars: 重优化频率（每 N 根 K 线重新优化一次）
        min_delta: 通过验证的最小 ΔexpR
        min_trades_ratio: 最小交易数比
        max_trades_ratio: 最大交易数比

    Returns:
        {
            "symbol": str,
            "n_bars": int,
            "n_periods": int,
            "n_grid_combos": int,
            "base_params": {...},
            "full_baseline": {expR, trades},
            "oos": {base_expR, opt_expR, delta, base_trades, opt_trades, trades_ratio},
            "passes_validation": bool,
            "param_distribution": {...},
            "yearly_results": [...],
        }
    """
    df = load_daily(symbol)
    if df is None or len(df) < init_train_bars + reopt_freq_bars:
        return {
            "symbol": symbol,
            "n_bars": 0,
            "n_periods": 0,
            "passes_validation": False,
            "note": "数据不足",
        }

    n_bars = len(df)

    # 生成所有网格组合
    combos = _grid_combinations(
        base_params["T"], base_params["stop"], base_params["rr"],
        T_range=T_range,
        stop_range=stop_range,
        rr_range=rr_range,
    )

    # 全量基线（用基线参数跑整段数据）
    base_full = _evaluate_params(
        symbol,
        base_params["T"], base_params["stop"], base_params["rr"],
        df_slice=df,
    )

    # 滚动验证周期
    yearly_results = []
    oos_base_expRs = []
    oos_opt_expRs = []
    oos_base_trades = 0
    oos_opt_trades = 0
    param_counts = {}

    period_start = init_train_bars
    while period_start + reopt_freq_bars <= n_bars:
        period_end = min(period_start + reopt_freq_bars, n_bars)

        # 训练数据
        train_df = df.iloc[:period_start]

        # 在训练期内做网格搜索，找最优参数
        best_expR = -float("inf")
        best_params = None

        for T, stop, rr in combos:
            result = _evaluate_params(
                symbol, T, stop, rr, df_slice=train_df,
            )
            if result["trades"] >= 5 and result["expR"] > best_expR:
                best_expR = result["expR"]
                best_params = (T, stop, rr)

        if best_params is None:
            best_params = (base_params["T"], base_params["stop"], base_params["rr"])

        # 记录参数分布
        param_key = f"T={best_params[0]},s={best_params[1]},r={best_params[2]}"
        param_counts[param_key] = param_counts.get(param_key, 0) + 1

        # OOS 测试
        oos_df = df.iloc[period_start:period_end]

        base_oos = _evaluate_params(
            symbol,
            base_params["T"], base_params["stop"], base_params["rr"],
            df_slice=oos_df,
        )
        opt_oos = _evaluate_params(
            symbol,
            best_params[0], best_params[1], best_params[2],
            df_slice=oos_df,
        )

        oos_base_expRs.append(base_oos["expR"])
        oos_opt_expRs.append(opt_oos["expR"])
        oos_base_trades += base_oos["trades"]
        oos_opt_trades += opt_oos["trades"]

        yearly_results.append({
            "period_start": period_start,
            "period_end": period_end,
            "params": {"T": best_params[0], "stop": best_params[1], "rr": best_params[2]},
            "opt_expR": round(opt_oos["expR"], 4),
            "base_expR": round(base_oos["expR"], 4),
            "opt_trades": opt_oos["trades"],
            "base_trades": base_oos["trades"],
        })

        period_start = period_end

    if not yearly_results:
        return {
            "symbol": symbol,
            "n_bars": n_bars,
            "n_periods": 0,
            "passes_validation": False,
            "note": "滚动周期不足",
        }

    # 计算 OOS 平均表现
    avg_base_expR = float(np.mean(oos_base_expRs))
    avg_opt_expR = float(np.mean(oos_opt_expRs))
    delta = avg_opt_expR - avg_base_expR
    trades_ratio = oos_opt_trades / oos_base_trades if oos_base_trades > 0 else 1.0

    passes = (
        delta >= min_delta
        and min_trades_ratio <= trades_ratio <= max_trades_ratio
    )

    return {
        "symbol": symbol,
        "n_bars": n_bars,
        "n_periods": len(yearly_results),
        "n_grid_combos": len(combos),
        "base_params": base_params,
        "full_baseline": {
            "expR": round(base_full["expR"], 4),
            "trades": base_full["trades"],
        },
        "oos": {
            "base_expR": round(avg_base_expR, 4),
            "opt_expR": round(avg_opt_expR, 4),
            "delta": round(delta, 4),
            "base_trades": oos_base_trades,
            "opt_trades": oos_opt_trades,
            "trades_ratio": round(trades_ratio, 3),
        },
        "passes_validation": passes,
        "param_distribution": param_counts,
        "yearly_results": yearly_results,
    }


def run_quarterly_reoptimization(
    symbols: Optional[List[str]] = None,
    T_range: float = DEFAULT_T_RANGE,
    stop_range: float = DEFAULT_STOP_RANGE,
    rr_range: float = DEFAULT_RR_RANGE,
    init_train_bars: int = DEFAULT_INIT_TRAIN_BARS,
    reopt_freq_bars: int = DEFAULT_REOPT_FREQ_BARS,
    min_delta: float = DEFAULT_MIN_DELTA,
    min_trades_ratio: float = DEFAULT_MIN_TRADES_RATIO,
    max_trades_ratio: float = DEFAULT_MAX_TRADES_RATIO,
    save_version: bool = True,
    description: str = "季度重优化",
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    运行季度重优化全流程。

    Args:
        symbols: 要优化的品种列表（None=全部可用品种）
        T_range, stop_range, rr_range: 紧边界搜索范围
        init_train_bars: 初始训练期长度
        reopt_freq_bars: 重优化频率
        min_delta: 通过验证的最小 ΔexpR
        min_trades_ratio / max_trades_ratio: 交易数比范围
        save_version: 是否保存为新版本
        description: 版本说明
        output_path: 验证结果输出路径（JSON）

    Returns:
        {
            "timestamp": str,
            "method": "tight_grid_rolling_validation",
            "params": {...搜索参数...},
            "elapsed_seconds": float,
            "results": [...],
            "passing_symbols": [...],
            "n_total": int,
            "n_passing": int,
            "avg_oos_delta": float,
        }
    """
    start_time = time.time()

    # 默认使用所有可用品种
    if symbols is None:
        symbols = sorted(SYMBOLS.keys())

    print(f"=== 季度重优化开始 ===")
    print(f"品种数: {len(symbols)}")
    print(f"搜索范围: T±{T_range}, stop±{stop_range}, rr±{rr_range}")
    print(f"滚动验证: {init_train_bars}bars 训练, {reopt_freq_bars}bars/期")
    print(f"通过门槛: ΔexpR≥{min_delta}, 交易比[{min_trades_ratio}, {max_trades_ratio}]")
    print()

    results = []
    passing_symbols = []

    for i, sym in enumerate(symbols):
        print(f"[{i+1}/{len(symbols)}] {sym}...", end=" ", flush=True)

        try:
            base_params = _get_baseline_params(sym)
            result = nested_rolling_validation(
                sym,
                base_params=base_params,
                T_range=T_range,
                stop_range=stop_range,
                rr_range=rr_range,
                init_train_bars=init_train_bars,
                reopt_freq_bars=reopt_freq_bars,
                min_delta=min_delta,
                min_trades_ratio=min_trades_ratio,
                max_trades_ratio=max_trades_ratio,
            )
            results.append(result)

            status = "✓ 通过" if result["passes_validation"] else "✗ 未通过"
            delta = result["oos"].get("delta", 0)
            print(f"{status} (ΔexpR={delta:+.3f})")

            if result["passes_validation"]:
                passing_symbols.append(sym)

        except Exception as e:
            print(f"✗ 错误: {e}")
            results.append({
                "symbol": sym,
                "passes_validation": False,
                "error": str(e),
            })

    elapsed = time.time() - start_time

    # 统计
    passing_results = [r for r in results if r.get("passes_validation")]
    n_total = len(results)
    n_passing = len(passing_results)
    avg_delta = (
        sum(r["oos"]["delta"] for r in passing_results) / n_passing
        if n_passing > 0
        else 0
    )

    print()
    print(f"=== 重优化完成 ===")
    print(f"总品种数: {n_total}")
    print(f"通过验证: {n_passing} ({n_passing/n_total*100:.1f}%)")
    print(f"平均 OOS ΔexpR: {avg_delta:+.4f}")
    print(f"耗时: {elapsed:.1f}s")

    # 构建最终结果
    full_result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "tight_grid_rolling_validation",
        "params": {
            "T_range": f"±{T_range}",
            "stop_range": f"±{stop_range}",
            "rr_range": f"±{rr_range}",
            "init_train_bars": init_train_bars,
            "reopt_freq_bars": reopt_freq_bars,
            "min_delta": min_delta,
            "min_trades_ratio": min_trades_ratio,
            "max_trades_ratio": max_trades_ratio,
        },
        "elapsed_seconds": round(elapsed, 2),
        "results": results,
        "passing_symbols": passing_symbols,
        "n_total": n_total,
        "n_passing": n_passing,
        "avg_oos_delta": round(avg_delta, 4),
    }

    # 保存验证结果 JSON
    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(full_result, f, indent=2, ensure_ascii=False)
        print(f"验证结果已保存: {output_path}")

    # 保存参数版本
    if save_version and passing_results:
        params_dict = {}
        for r in passing_results:
            sym = r["symbol"]
            # 取出现频率最高的参数组合
            dist = r.get("param_distribution", {})
            if dist:
                top_param_key = max(dist, key=dist.get)
                # 解析 "T=14,s=1.5,r=2.5" 格式
                parts = dict(p.split("=") for p in top_param_key.split(","))
                params_dict[sym] = {
                    "T_thresh": float(parts.get("T", r["base_params"]["T"])),
                    "stop_atr_mult": float(parts.get("s", r["base_params"]["stop"])),
                    "rr_ratio": float(parts.get("r", r["base_params"]["rr"])),
                    "base_expR": r["oos"]["base_expR"],
                    "opt_expR": r["oos"]["opt_expR"],
                    "delta": r["oos"]["delta"],
                    "base_trades": r["oos"]["base_trades"],
                    "opt_trades": r["oos"]["opt_trades"],
                    "trades_ratio": r["oos"]["trades_ratio"],
                }
            else:
                params_dict[sym] = {
                    "stop_atr_mult": r["base_params"]["stop"],
                    "rr_ratio": r["base_params"]["rr"],
                    "T_thresh": r["base_params"]["T"],
                    "base_expR": r["oos"]["base_expR"],
                    "opt_expR": r["oos"]["opt_expR"],
                    "delta": r["oos"]["delta"],
                    "trades_ratio": r["oos"]["trades_ratio"],
                }

        mgr = ParamVersionManager()
        validation_summary = {
            "method": "tight_grid_rolling_validation",
            "n_total": n_total,
            "n_passing": n_passing,
            "avg_oos_delta": round(avg_delta, 4),
        }
        version_id = mgr.save_version(
            params=params_dict,
            description=description,
            author="quarterly_reopt",
            validation_summary=validation_summary,
        )
        print(f"参数版本已保存: {version_id} ({n_passing} 个品种)")

    return full_result


def main():
    parser = argparse.ArgumentParser(description="季度参数重优化")
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="要优化的品种列表（空=全部）",
    )
    parser.add_argument("--T-range", type=float, default=DEFAULT_T_RANGE, help="T 搜索范围 (±)")
    parser.add_argument("--stop-range", type=float, default=DEFAULT_STOP_RANGE, help="stop 搜索范围 (±)")
    parser.add_argument("--rr-range", type=float, default=DEFAULT_RR_RANGE, help="rr 搜索范围 (±)")
    parser.add_argument("--min-delta", type=float, default=DEFAULT_MIN_DELTA, help="通过验证的最小 ΔexpR")
    parser.add_argument("--init-train", type=int, default=DEFAULT_INIT_TRAIN_BARS, help="初始训练期 bars")
    parser.add_argument("--reopt-freq", type=int, default=DEFAULT_REOPT_FREQ_BARS, help="重优化频率 bars")
    parser.add_argument("--output", type=str, default=None, help="验证结果输出 JSON 路径")
    parser.add_argument("--no-save-version", action="store_true", help="不保存参数版本")
    parser.add_argument("--description", type=str, default="季度重优化", help="版本说明")
    parser.add_argument("--dry-run", action="store_true", help="仅运行少量品种测试")

    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.dry_run:
        # 试运行：只跑 3 个品种
        args.symbols = ["zn", "al", "c"]
        args.min_delta = 0.0
        print("⚠️  DRY RUN 模式：仅运行 3 个测试品种，无门槛过滤")

    if args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = os.path.join(project_root, "ga_results", f"qtr_reopt_{timestamp}.json")

    result = run_quarterly_reoptimization(
        symbols=args.symbols,
        T_range=args.T_range,
        stop_range=args.stop_range,
        rr_range=args.rr_range,
        init_train_bars=args.init_train,
        reopt_freq_bars=args.reopt_freq,
        min_delta=args.min_delta,
        save_version=not args.no_save_version,
        description=args.description,
        output_path=args.output,
    )

    return result


if __name__ == "__main__":
    main()
