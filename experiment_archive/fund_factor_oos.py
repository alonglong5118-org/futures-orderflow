"""
基本面因子增强 OOS 验证（走步法）

使用 walk_forward_backtest 的 F_override 参数，
对比基准 F vs 增强版 F（加入新因子）。

增强方案：
- F_enhanced = w_base * F_base + w_basis_trend * basis_trend + w_inv_mom * inv_mom + w_profit * profit_z
- 权重根据 IC 检验结果设定
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fundamental_factors as ff
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

# 板块
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)


def precompute_enhanced_F(symbol, df, weights=None):
    """预计算增强版 F 序列。

    weights: dict of {factor_name: weight}
    返回 (date_strs, F_enhanced_arr)
    """
    if weights is None:
        weights = {"basis_trend": 0.15, "inv_mom": 0.15, "profit_z": 0.1}

    n = len(df)
    close = df["close"].values.astype(float)

    if "date" in df.columns:
        dates_arr = [int(str(d).replace("-", "")[:8]) for d in df["date"].values]
        date_strs = [str(d)[:10] for d in df["date"].values]
    else:
        dates_arr = [int(str(d).replace("-", "")[:8]) for d in df.index.values]
        date_strs = [str(d)[:10] for d in df.index.values]

    dates_np = np.array(dates_arr)
    F_enh = np.zeros(n, dtype=float)

    # 1. 基准 F
    from fundamental_feed import precompute_F_array

    base_F = precompute_F_array(symbol, date_strs=date_strs)
    if base_F is not None and len(base_F) == n:
        F_enh += base_F
    else:
        return None  # 连基准 F 都没有就不用测了

    # 2. 基差趋势因子
    if "basis_trend" in weights and ff.has_basis_data(symbol):
        from fund_factor_test import precompute_basis_factors

        b_dates, _, b_trend = precompute_basis_factors(symbol)
        if b_dates is not None:
            idxs = np.searchsorted(b_dates, dates_np, side="right") - 1
            mask = idxs >= 0
            trend_vals = np.zeros(n)
            trend_vals[mask] = np.nan_to_num(b_trend[idxs[mask]], nan=0.0)
            F_enh += weights["basis_trend"] * trend_vals

    # 3. 库存环比因子
    if "inv_mom" in weights and ff.has_inventory_data(symbol):
        from fund_factor_test import precompute_inventory_factors

        i_dates, _, i_mom, _ = precompute_inventory_factors(symbol)
        if i_dates is not None:
            idxs = np.searchsorted(i_dates, dates_np, side="right") - 1
            mask = idxs >= 0
            mom_vals = np.zeros(n)
            mom_vals[mask] = np.nan_to_num(i_mom[idxs[mask]], nan=0.0)
            F_enh += weights["inv_mom"] * mom_vals

    # 4. 产业利润因子
    if "profit_z" in weights:
        from fund_factor_test import precompute_profit_factors

        profit_key = ff.get_profit_key_for_symbol(symbol)
        if profit_key:
            p_dates, p_z, _ = precompute_profit_factors(symbol, profit_key)
            if p_dates is not None:
                idxs = np.searchsorted(p_dates, dates_np, side="right") - 1
                mask = idxs >= 0
                profit_vals = np.zeros(n)
                profit_vals[mask] = np.nan_to_num(p_z[idxs[mask]], nan=0.0)
                F_enh += weights["profit_z"] * profit_vals

    # 裁剪到 [-100, 100]
    F_enh = np.clip(F_enh, -100.0, 100.0)

    return date_strs, F_enh


def run_wf_comparison(symbol, weights=None, n_folds=5, window=300):
    """对比基准 F 和增强 F 的走步法回测。

    方法：monkey patch score_F 来注入增强版 F，回测后恢复。
    """
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    # 预计算增强 F
    enh_data = precompute_enhanced_F(symbol, df, weights)
    if enh_data is None:
        return None
    date_strs, F_enh_arr = enh_data

    # 构建日期->F 映射（YYYYMMDD 字符串格式）
    F_map = {}
    for i, ds in enumerate(date_strs):
        # 转成 YYYYMMDD 格式（不带横杠）
        d_clean = ds.replace("-", "")
        F_map[d_clean] = F_enh_arr[i]

    # 基准回测
    base_res = walk_forward_backtest(
        symbol,
        cfg=DEFAULT_CONFIG,
        window=window,
        df_in=df,
    )

    # 增强版回测：monkey patch precompute_F_array
    import four_dim_strategy as _fds
    import fundamental_feed as _ff_mod

    original_precompute_F = _ff_mod.precompute_F_array
    original_fds_ff = _fds.ff.precompute_F_array

    def enhanced_precompute_F(sym, date_strs=None, date_ints=None, **kwargs):
        if date_ints is not None:
            result = np.zeros(len(date_ints), dtype=float)
            for i, di in enumerate(date_ints):
                result[i] = F_map.get(str(int(di)), 0.0)
            return result
        elif date_strs is not None:
            result = np.zeros(len(date_strs), dtype=float)
            for i, ds in enumerate(date_strs):
                d_clean = str(ds).replace("-", "")[:8]
                result[i] = F_map.get(d_clean, 0.0)
            return result
        return np.zeros(100)

    _ff_mod.precompute_F_array = enhanced_precompute_F
    _fds.ff.precompute_F_array = enhanced_precompute_F

    try:
        enh_res = walk_forward_backtest(
            symbol,
            cfg=DEFAULT_CONFIG,
            window=window,
            df_in=df,
        )
    finally:
        _ff_mod.precompute_F_array = original_precompute_F
        _fds.ff.precompute_F_array = original_fds_ff

    return {
        "base": base_res,
        "enhanced": enh_res,
    }


def extract_metrics(result):
    """从 walk_forward_backtest 结果中提取关键指标。"""
    if not result or not isinstance(result, dict):
        return None

    # 尝试不同的 key
    total_ret = result.get("total_return", result.get("total_ret", 0))
    expR = result.get("expR", result.get("exp_r", 0))
    n_trades = result.get("n_trades", result.get("trades", 0))
    win_rate = result.get("win_rate", 0)
    max_dd = result.get("max_dd", result.get("max_drawdown", 0))
    sharpe = result.get("sharpe", result.get("sharpe_ratio", 0))

    return {
        "total_return": float(total_ret) if total_ret is not None else 0,
        "expR": float(expR) if expR is not None else 0,
        "n_trades": int(n_trades) if n_trades is not None else 0,
        "win_rate": float(win_rate) if win_rate is not None else 0,
        "max_dd": float(max_dd) if max_dd is not None else 0,
        "sharpe": float(sharpe) if sharpe is not None else 0,
    }


def main():
    t0 = time.time()

    # 测试板块和品种
    test_sectors = ["农产品", "有色", "能源", "黑系", "化工"]

    # 增强权重（根据 IC 检验结果）
    # IC 高的因子给更高权重
    enh_weights = {
        "basis_trend": 0.20,  # 期限结构变化，农产品/能源 IC 高
        "inv_mom": 0.20,  # 库存环比，有色 IC 最高
        "profit_z": 0.10,  # 产业利润，IC 较弱，给低权重
    }

    print(f"{'=' * 100}")
    print("基本面因子增强策略 OOS 对比（走步法）")
    print(
        f"增强权重: basis_trend={enh_weights['basis_trend']}, "
        f"inv_mom={enh_weights['inv_mom']}, profit_z={enh_weights['profit_z']}"
    )
    print(f"{'=' * 100}")

    all_results = {}

    for sector in test_sectors:
        syms = GROUPS.get(sector, [])
        if not syms:
            continue

        print(f"\n【{sector}】")
        print(
            f"{'品种':<8}{'基准expR':>10}{'增强expR':>10}{'expR提升':>10}"
            f"{'基准收益':>10}{'增强收益':>10}{'收益提升':>10}"
        )
        print(f"{'-' * 70}")

        sector_results = {}

        for sym in syms:
            if sym not in SYMBOLS:
                continue
            if not ff.has_basis_data(sym) and not ff.has_inventory_data(sym):
                continue  # 没有基本面数据的跳过

            try:
                result = run_wf_comparison(sym, weights=enh_weights, window=300)
            except Exception as e:
                print(f"{sym:<8}  错误: {e}")
                continue

            if result is None:
                continue

            base_m = extract_metrics(result["base"])
            enh_m = extract_metrics(result["enhanced"])

            if base_m is None or enh_m is None:
                continue

            expR_imp = enh_m["expR"] - base_m["expR"]
            ret_imp = enh_m["total_return"] - base_m["total_return"]

            # 判断好坏
            if expR_imp > 0.02:
                flag = "✅"
            elif expR_imp > 0:
                flag = "↗️"
            elif expR_imp > -0.02:
                flag = "↘️"
            else:
                flag = "❌"

            print(
                f"{flag}{sym:<7}"
                f"{base_m['expR']:>+10.3f}"
                f"{enh_m['expR']:>+10.3f}"
                f"{expR_imp:>+10.3f}"
                f"{base_m['total_return']:>+10.2%}"
                f"{enh_m['total_return']:>+10.2%}"
                f"{ret_imp:>+10.2%}"
            )

            sector_results[sym] = {
                "base": base_m,
                "enhanced": enh_m,
                "expR_improvement": float(expR_imp),
                "ret_improvement": float(ret_imp),
            }

        if sector_results:
            all_results[sector] = sector_results

            # 板块汇总
            expR_imps = [v["expR_improvement"] for v in sector_results.values()]
            ret_imps = [v["ret_improvement"] for v in sector_results.values()]
            n_pos = sum(1 for x in expR_imps if x > 0)

            print(f"{'平均':<8}", end="")
            print(f"{'':>10}{'':>10}{np.mean(expR_imps):>+10.3f}{'':>10}{'':>10}{np.mean(ret_imps):>+10.2%}")
            print(f"胜率提升品种占比: {n_pos}/{len(expR_imps)} = {n_pos / len(expR_imps):.0%}")

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "fund_factor_oos.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
