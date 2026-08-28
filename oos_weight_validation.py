"""P2-④ OOS 权重验证 harness。
对四维策略的启发式权重做 walk-forward IS/OOS 退化检验：
  · 历史按时间切 IS(前 60%) / OOS(后 40%)，边界 embargo 防泄漏
  · 在 IS 上扫 combine_weights + bias_synthesis 关键参数，选 expR 最优且成交数达标者
  · 在 OOS 上重跑最优参数 + 默认参数，报 IS→OOS 退化率
  · 只报告，不自动 apply（遵守 auto-stage + 手动 apply 铁律）
用法：python3 oos_weight_validation.py [--symbols jd,lh,FG,SA,JM,J] [--pilot]
"""
import argparse
import copy
import json

import four_dim_strategy as fd


def _deep_merge(base, patch):
    """递归合并 patch 到 base 副本，返回新 dict。"""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def split_is_oos(df, is_ratio=0.6, embargo_bars=20):
    """时间序切分：IS=前 is_ratio，OOS=后 (1-is_ratio)；丢弃边界 embargo_bars 防泄漏。"""
    n = len(df)
    cut = int(n * is_ratio)
    is_df = df.iloc[:cut - embargo_bars]
    oos_df = df.iloc[cut + embargo_bars:]
    return is_df, oos_df


def _metric(r):
    """从 walk_forward_backtest 返回值取 (expR, trades)。"""
    return float(r.get("expR", 0.0)), int(r.get("trades", 0))


def run_oos_validation(symbol, cfg=None, param_grid=None, is_ratio=0.6, embargo_bars=20,
                       min_trades=20):
    base = cfg or fd.DEFAULT_CONFIG
    df = fd.load_daily(symbol)
    if df is None or len(df) < 120:
        return {"symbol": symbol, "error": "数据不足(<120)", "is": None, "oos": None}
    is_df, oos_df = split_is_oos(df, is_ratio, embargo_bars)
    if len(is_df) < 60 or len(oos_df) < 60:
        return {"symbol": symbol, "error": "IS/OOS 切片过短", "is": None, "oos": None}

    # 默认配置 OOS 基线
    r_def = fd.walk_forward_backtest(symbol, base, df_in=oos_df)
    oos_expR_def, oos_trades_def = _metric(r_def)

    if param_grid is None:
        # 仅报默认配置 IS/OOS 退化（不扫参）
        r_is_def = fd.walk_forward_backtest(symbol, base, df_in=is_df)
        is_expR, is_trades = _metric(r_is_def)
        deg = ((is_expR - oos_expR_def) / abs(is_expR)) if is_expR != 0 else None
        return {
            "symbol": symbol, "is_trades": is_trades, "oos_trades": oos_trades_def,
            "is_expR": round(is_expR, 4), "oos_expR_default": round(oos_expR_def, 4),
            "oos_expR_best": round(oos_expR_def, 4), "best_params": None,
            "degradation_pct": round(deg * 100, 1) if deg is not None else None,
            "overfit_flag": (deg is not None and deg > 0.5),
        }

    # 扫参：在 IS 上评估每组参数
    best = None  # (is_expR, params)
    grid_results = []
    for combo in param_grid:
        patched = _deep_merge(base, combo)
        r_is = fd.walk_forward_backtest(symbol, patched, df_in=is_df)
        is_expR, is_trades = _metric(r_is)
        grid_results.append({"params": combo, "is_expR": round(is_expR, 4), "is_trades": is_trades})
        if is_trades < min_trades:
            continue
        if best is None or is_expR > best[0]:
            best = (is_expR, combo)

    if best is None:
        return {"symbol": symbol, "error": "IS 扫参无达标组合(trades<%d)" % min_trades,
                "is": None, "oos": None, "grid": grid_results}

    best_params = best[1]
    patched_best = _deep_merge(base, best_params)
    r_oos_best = fd.walk_forward_backtest(symbol, patched_best, df_in=oos_df)
    oos_expR_best, oos_trades_best = _metric(r_oos_best)
    r_is_best = fd.walk_forward_backtest(symbol, patched_best, df_in=is_df)
    is_expR_best, _ = _metric(r_is_best)

    deg = ((is_expR_best - oos_expR_best) / abs(is_expR_best)) if is_expR_best != 0 else None
    return {
        "symbol": symbol,
        "is_trades": _metric(r_is_best)[1], "oos_trades_best": oos_trades_best,
        "oos_trades_default": oos_trades_def,
        "is_expR_best": round(is_expR_best, 4),
        "oos_expR_best": round(oos_expR_best, 4),
        "oos_expR_default": round(oos_expR_def, 4),
        "best_params": best_params,
        "degradation_pct": round(deg * 100, 1) if deg is not None else None,
        "overfit_flag": (deg is not None and deg > 0.5),
        "grid": grid_results,
    }


def default_grid():
    """默认扫参网格（聚焦 combine_weights，最重要）。27 组合。"""
    grid = []
    for wt in (0.5, 0.6, 0.7):
        for wf in (0.2, 0.25, 0.3):
            wc = round(1.0 - wt - wf, 3)
            if wc < 0.05:
                continue
            grid.append({"combine_weights": {"T": wt, "F": wf, "C": wc}})
    return grid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="jd,lh,FG,SA,JM,J")
    ap.add_argument("--pilot", action="store_true", help="仅报默认配置 IS/OOS 退化，不扫参")
    ap.add_argument("--is-ratio", type=float, default=0.6)
    ap.add_argument("--min-trades", type=int, default=20, help="IS 扫参最低成交数（低波动品种可调低）")
    ap.add_argument("--out", default="oos_validation_report.json", help="报告输出路径（多批跑避免互相覆盖）")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    grid = None if args.pilot else default_grid()

    report = []
    print(f"{'SYM':4} {'IS_expR':>8} {'OOS_expR':>9} {'deg%':>7} {'overfit':>8}  best_params")
    for sym in symbols:
        r = run_oos_validation(sym, param_grid=grid, is_ratio=args.is_ratio, min_trades=args.min_trades)
        report.append(r)
        if "error" in r:
            print(f"{sym:4} ERROR: {r['error']}")
            continue
        bp = r.get("best_params")
        print(f"{sym:4} {str(r.get('is_expR_best', r.get('is_expR'))):>8} "
              f"{str(r.get('oos_expR_best', r.get('oos_expR_default'))):>9} "
              f"{str(r.get('degradation_pct')):>7} {str(r.get('overfit_flag')):>8}  "
              f"{bp if bp else '(default)'}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写 {args.out}")


if __name__ == "__main__":
    main()
