"""
基本面新因子单因子 IC/IR 全板块检验（优化版：批量预计算）

因子列表：
- basis_rate: 基差率 z-score（期限结构水平）
- basis_trend: 基差率 10 日变化（期限结构变化）
- inv_level: 库存水平 z-score
- inv_mom: 库存环比变化率
- inv_speed: 累库/去库速度
- profit_z: 产业利润 z-score
- profit_trend: 产业利润变化趋势

优化：批量预计算因子序列，然后计算 IC，速度提升 10~100 倍。
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fundamental_factors as ff
from four_dim_strategy import SYMBOLS, load_daily

# 从 SYMBOLS 构建板块列表
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)

SECTORS = [g for g in GROUPS.keys() if g != "其他"]

# 前瞻天数
FORWARD_DAYS = [1, 3, 5, 10]

# 因子定义
FACTOR_DEFS = {
    "basis_rate": {"name": "基差率(Basis)", "category": "期限结构", "requires": "basis"},
    "basis_trend": {"name": "基差趋势", "category": "期限结构", "requires": "basis"},
    "inv_level": {"name": "库存水平", "category": "库存", "requires": "inventory"},
    "inv_mom": {"name": "库存环比", "category": "库存", "requires": "inventory"},
    "inv_speed": {"name": "库存速度", "category": "库存", "requires": "inventory"},
    "profit_z": {"name": "产业利润z", "category": "产业利润", "requires": "profit"},
    "profit_trend": {"name": "利润趋势", "category": "产业利润", "requires": "profit"},
}


# ===========================================================================
# 批量预计算因子序列
# ===========================================================================


def precompute_basis_factors(symbol, z_window=60, trend_days=10):
    """批量预计算基差因子序列。

    返回 (date_ints, basis_rate_arr, basis_trend_arr)
    """
    if not ff.has_basis_data(symbol):
        return None, None, None

    data = ff._load_fundamentals()
    bs = data[symbol].get("basis_series", [])
    if len(bs) < z_window + trend_days + 10:
        return None, None, None

    dates = np.array([int(d["date"]) for d in bs])
    rates = np.array([d.get("dom_basis_rate", 0) for d in bs], dtype=float)
    n = len(rates)

    # 滚动 z-score（向量化）
    basis_rate_arr = np.full(n, np.nan)
    for i in range(z_window, n):
        window = rates[i - z_window : i]
        mean = np.mean(window)
        std = np.std(window)
        if std > 1e-8:
            z = (rates[i] - mean) / std
            basis_rate_arr[i] = np.clip(z * 20.0, -100, 100)

    # 趋势
    basis_trend_arr = np.full(n, np.nan)
    for i in range(trend_days, n):
        change = rates[i] - rates[i - trend_days]
        basis_trend_arr[i] = np.clip(change * 2000.0, -100, 100)

    return dates, basis_rate_arr, basis_trend_arr


def precompute_inventory_factors(symbol, z_window=30, speed_periods=4):
    """批量预计算库存因子序列。

    返回 (date_ints, inv_level_arr, inv_mom_arr, inv_speed_arr)
    """
    if not ff.has_inventory_data(symbol):
        return None, None, None, None

    data = ff._load_fundamentals()
    inv = data[symbol].get("inventory", [])
    if len(inv) < z_window + speed_periods:
        return None, None, None, None

    dates = np.array([int(d["date"].replace("-", "")) for d in inv])
    stocks = np.array([float(d.get("stock", 0) or 0) for d in inv], dtype=float)
    n = len(stocks)

    # 库存水平 z-score（低库存利多 → 负z → 正分）
    inv_level_arr = np.full(n, np.nan)
    for i in range(z_window, n):
        if stocks[i] <= 0:
            continue
        window = stocks[i - z_window : i]
        mean_s = np.mean(window)
        std_s = np.std(window)
        if std_s > 1e-6:
            z = (stocks[i] - mean_s) / std_s
            inv_level_arr[i] = np.clip(-z * 20.0, -100, 100)

    # 环比变化率
    inv_mom_arr = np.full(n, np.nan)
    for i in range(1, n):
        if stocks[i - 1] > 0:
            chg_rate = (stocks[i] - stocks[i - 1]) / stocks[i - 1]
            inv_mom_arr[i] = np.clip(-chg_rate * 500.0, -100, 100)

    # 累库/去库速度（近 N 期平均）
    inv_speed_arr = np.full(n, np.nan)
    for i in range(speed_periods, n):
        changes = []
        for j in range(i - speed_periods + 1, i + 1):
            if stocks[j - 1] > 0:
                changes.append((stocks[j] - stocks[j - 1]) / stocks[j - 1])
        if changes:
            avg_chg = np.mean(changes)
            inv_speed_arr[i] = np.clip(-avg_chg * 800.0, -100, 100)

    return dates, inv_level_arr, inv_mom_arr, inv_speed_arr


def precompute_profit_factors(symbol, profit_key, z_window=60, trend_days=10):
    """批量预计算产业利润因子序列。

    返回 (date_ints, profit_z_arr, profit_trend_arr)
    """
    if profit_key not in ff.PROFIT_DEFS:
        return None, None, None

    pdef = ff.PROFIT_DEFS[profit_key]
    product = pdef["product"]
    raws = pdef["raws"]
    kind = pdef["kind"]

    all_syms = [product] + [s for s, _ in raws]

    # 加载所有品种的日线
    price_data = {}
    min_len = float("inf")
    for sym in all_syms:
        df = load_daily(sym)
        if df is None or len(df) < z_window + trend_days + 10:
            return None, None, None

        closes = df["close"].values.astype(float)
        if "date" in df.columns:
            dates_arr = np.array([int(str(d).replace("-", "")[:8]) for d in df["date"].values])
        else:
            dates_arr = np.array([int(str(d).replace("-", "")[:8]) for d in df.index.values])

        price_data[sym] = (dates_arr, closes)
        min_len = min(min_len, len(closes))

    if min_len < z_window + trend_days + 10:
        return None, None, None

    # 以 product 的日期为主，逐日对齐计算利润
    prod_dates, prod_closes = price_data[product]
    n = len(prod_dates)

    # 预计算每个 raw 品种在 product 每个日期的价格索引
    raw_indices = {}
    for raw_sym, _ in raws:
        raw_dates, _ = price_data[raw_sym]
        # 对每个 product 日期，找到 raw 的对应索引
        idxs = np.searchsorted(raw_dates, prod_dates, side="right") - 1
        idxs = np.clip(idxs, 0, len(raw_dates) - 1)
        raw_indices[raw_sym] = idxs

    # 计算利润序列
    profit_series = np.zeros(n, dtype=float)
    if kind == "spread":
        profit_series[:] = prod_closes
        for raw_sym, ratio in raws:
            raw_closes = price_data[raw_sym][1]
            idxs = raw_indices[raw_sym]
            profit_series -= ratio * raw_closes[idxs]
    elif kind == "ratio":
        profit_series[:] = prod_closes
        for raw_sym, ratio in raws:
            raw_closes = price_data[raw_sym][1]
            idxs = raw_indices[raw_sym]
            r_vals = raw_closes[idxs]
            # 避免除零
            r_vals = np.where(r_vals > 1e-6, r_vals, 1e-6)
            profit_series = profit_series / r_vals
            break  # ratio 只除第一个

    # z-score + 趋势
    profit_z_arr = np.full(n, np.nan)
    profit_trend_arr = np.full(n, np.nan)

    for i in range(z_window + trend_days, n):
        window = profit_series[i - z_window : i]
        mean_p = np.mean(window)
        std_p = np.std(window)
        if std_p > 1e-10 and not np.isnan(profit_series[i]):
            z = (profit_series[i] - mean_p) / std_p
            profit_z_arr[i] = np.clip(z * 20.0, -100, 100)

        if i >= trend_days:
            past_val = profit_series[i - trend_days]
            if not np.isnan(past_val) and abs(past_val) > 1e-10:
                change = (profit_series[i] - past_val) / abs(past_val)
                profit_trend_arr[i] = np.clip(change * 200.0, -100, 100)

    return prod_dates, profit_z_arr, profit_trend_arr


# ===========================================================================
# IC 计算
# ===========================================================================


def compute_ic(factor_arr, ret_arr):
    """计算 IC 和相关统计量。"""
    valid = (~np.isnan(factor_arr)) & (~np.isnan(ret_arr))
    if valid.sum() < 30:
        return None

    ic = float(np.corrcoef(factor_arr[valid], ret_arr[valid])[0, 1])
    n_valid = int(valid.sum())

    # 分层收益（5 分位）
    sorted_idx = np.argsort(factor_arr[valid])
    n_q = n_valid // 5
    q_rets = []
    for q in range(5):
        q_start = q * n_q
        q_end = (q + 1) * n_q if q < 4 else n_valid
        q_idx = sorted_idx[q_start:q_end]
        q_rets.append(float(np.mean(ret_arr[valid][q_idx])))

    ls_ret = q_rets[-1] - q_rets[0]

    # 胜率
    pos_factor = factor_arr[valid] > 0
    pos_ret = ret_arr[valid] > 0
    if pos_factor.sum() > 0:
        win_rate = float((pos_ret[pos_factor]).sum() / pos_factor.sum())
    else:
        win_rate = 0.5

    return {
        "ic": ic,
        "n_valid": n_valid,
        "win_rate": win_rate,
        "ls_ret": ls_ret,
        "q_rets": q_rets,
    }


def test_symbol(symbol):
    """单品种所有因子 IC 检验。"""
    df = load_daily(symbol)
    if df is None or len(df) < 200:
        return None

    close = df["close"].values.astype(float)
    if "date" in df.columns:
        dates = np.array([int(str(d).replace("-", "")[:8]) for d in df["date"].values])
    else:
        dates = np.array([int(str(d).replace("-", "")[:8]) for d in df.index.values])
    n = len(dates)

    results = {}

    # 1. 基差因子
    if ff.has_basis_data(symbol):
        b_dates, b_rate, b_trend = precompute_basis_factors(symbol)
        if b_dates is not None:
            # 将基差因子对齐到日线日期
            rate_aligned = np.full(n, np.nan)
            trend_aligned = np.full(n, np.nan)
            idxs = np.searchsorted(b_dates, dates, side="right") - 1
            mask = idxs >= 0
            rate_aligned[mask] = b_rate[idxs[mask]]
            trend_aligned[mask] = b_trend[idxs[mask]]

            for fd in FORWARD_DAYS:
                if fd >= n:
                    continue
                fut_ret = np.full(n, np.nan)
                fut_ret[:-fd] = (close[fd:] - close[:-fd]) / close[:-fd]

                ic_rate = compute_ic(rate_aligned, fut_ret)
                if ic_rate:
                    if "basis_rate" not in results:
                        results["basis_rate"] = {}
                    results["basis_rate"][fd] = ic_rate

                ic_trend = compute_ic(trend_aligned, fut_ret)
                if ic_trend:
                    if "basis_trend" not in results:
                        results["basis_trend"] = {}
                    results["basis_trend"][fd] = ic_trend

    # 2. 库存因子
    if ff.has_inventory_data(symbol):
        i_dates, i_level, i_mom, i_speed = precompute_inventory_factors(symbol)
        if i_dates is not None:
            level_aligned = np.full(n, np.nan)
            mom_aligned = np.full(n, np.nan)
            speed_aligned = np.full(n, np.nan)
            idxs = np.searchsorted(i_dates, dates, side="right") - 1
            mask = idxs >= 0
            level_aligned[mask] = i_level[idxs[mask]]
            mom_aligned[mask] = i_mom[idxs[mask]]
            speed_aligned[mask] = i_speed[idxs[mask]]

            for fd in FORWARD_DAYS:
                if fd >= n:
                    continue
                fut_ret = np.full(n, np.nan)
                fut_ret[:-fd] = (close[fd:] - close[:-fd]) / close[:-fd]

                for fname, f_arr in [
                    ("inv_level", level_aligned),
                    ("inv_mom", mom_aligned),
                    ("inv_speed", speed_aligned),
                ]:
                    ic_res = compute_ic(f_arr, fut_ret)
                    if ic_res:
                        if fname not in results:
                            results[fname] = {}
                        results[fname][fd] = ic_res

    # 3. 利润因子
    profit_key = ff.get_profit_key_for_symbol(symbol)
    if profit_key:
        p_dates, p_z, p_trend = precompute_profit_factors(symbol, profit_key)
        if p_dates is not None:
            z_aligned = np.full(n, np.nan)
            trend_aligned = np.full(n, np.nan)
            # 利润日期就是 product 的日期，直接用（都是同一品种的话完全对齐）
            if len(p_dates) == n and np.array_equal(p_dates, dates):
                z_aligned = p_z
                trend_aligned = p_trend
            else:
                idxs = np.searchsorted(p_dates, dates, side="right") - 1
                mask = idxs >= 0
                z_aligned[mask] = p_z[idxs[mask]]
                trend_aligned[mask] = p_trend[idxs[mask]]

            for fd in FORWARD_DAYS:
                if fd >= n:
                    continue
                fut_ret = np.full(n, np.nan)
                fut_ret[:-fd] = (close[fd:] - close[:-fd]) / close[:-fd]

                ic_z = compute_ic(z_aligned, fut_ret)
                if ic_z:
                    if "profit_z" not in results:
                        results["profit_z"] = {}
                    results["profit_z"][fd] = ic_z

                ic_trend = compute_ic(trend_aligned, fut_ret)
                if ic_trend:
                    if "profit_trend" not in results:
                        results["profit_trend"] = {}
                    results["profit_trend"][fd] = ic_trend

    return results


def test_sector(sector_name):
    """测试一个板块的所有因子。"""
    syms = GROUPS.get(sector_name, [])
    if not syms:
        return None

    results = {}

    for sym in syms:
        if sym not in SYMBOLS:
            continue
        sym_res = test_symbol(sym)
        if sym_res:
            results[sym] = sym_res

    return results


def print_sector_results(sector_name, results):
    """打印板块结果。"""
    print(f"\n{'=' * 100}")
    print(f"【{sector_name}】单因子 IC 检验（按品种平均）")
    print(f"{'=' * 100}")
    print(f"{'因子':<16}{'品种数':>8}", end="")
    for fd in FORWARD_DAYS:
        print(f"  IC({fd}d)", end="")
    print("  方向一致性")
    print(f"{'-' * 80}")

    # 按因子聚合
    factor_stats = {}
    for fname in FACTOR_DEFS:
        sym_ics = {}
        for sym, sym_res in results.items():
            if fname not in sym_res:
                continue
            fd_ics = {}
            for fd, icres in sym_res[fname].items():
                fd_ics[fd] = icres["ic"]
            sym_ics[sym] = fd_ics

        if not sym_ics:
            continue

        avg_ics = []
        for fd in FORWARD_DAYS:
            ics = [sym_ics[s][fd] for s in sym_ics if fd in sym_ics[s]]
            avg_ics.append(np.mean(ics) if ics else np.nan)

        # 方向一致性：正 IC 的品种比例
        fd5_ics = [sym_ics[s][5] for s in sym_ics if 5 in sym_ics[s]]
        pos_ratio = np.mean([1 if ic > 0 else 0 for ic in fd5_ics]) if fd5_ics else 0

        factor_stats[fname] = {
            "n_syms": len(sym_ics),
            "avg_ics": avg_ics,
            "pos_ratio": pos_ratio,
        }

    # 按 5 日 IC 排序
    ranked = sorted(factor_stats.items(), key=lambda x: -abs(x[1]["avg_ics"][2]))

    for fname, stats in ranked:
        fdef = FACTOR_DEFS[fname]
        label = f"{fname}({fdef['category'][:2]})"
        print(f"{label:<16}{stats['n_syms']:>8}", end="")
        for ic in stats["avg_ics"]:
            print(f"  {ic:+.4f}" if not np.isnan(ic) else "    N/A ", end="")
        pos_pct = stats["pos_ratio"] * 100
        print(f"   {pos_pct:.0f}%")

    return factor_stats


def main():
    t0 = time.time()

    all_results = {}
    all_factor_stats = {}

    for sector in SECTORS:
        print(f"正在计算 {sector}...", end="", flush=True)
        t1 = time.time()
        res = test_sector(sector)
        dt = time.time() - t1
        print(f" 完成 ({dt:.1f}s)")

        if res:
            all_results[sector] = res
            stats = print_sector_results(sector, res)
            all_factor_stats[sector] = stats

    # 全板块汇总
    print(f"\n\n{'=' * 100}")
    print("全板块汇总：各因子 5 日 IC")
    print(f"{'=' * 100}")
    print(f"{'因子':<16}{'板块数':>8}", end="")
    for fd in FORWARD_DAYS:
        print(f"  IC({fd}d)", end="")
    print("    IR    结论")
    print(f"{'-' * 85}")

    summary = {}
    for fname in FACTOR_DEFS:
        sector_ics = []
        n_sectors = 0
        all_fd_ics = {fd: [] for fd in FORWARD_DAYS}

        for sector, sec_stats in all_factor_stats.items():
            if fname not in sec_stats:
                continue
            stats = sec_stats[fname]
            if stats["n_syms"] < 2:
                continue
            sector_ics.append(stats["avg_ics"][2])  # 5 日 IC
            n_sectors += 1
            for fd_i, fd in enumerate(FORWARD_DAYS):
                if not np.isnan(stats["avg_ics"][fd_i]):
                    all_fd_ics[fd].append(stats["avg_ics"][fd_i])

        if not sector_ics:
            continue

        avg_5d = np.mean(sector_ics)
        std_5d = np.std(sector_ics)
        ir = avg_5d / std_5d if std_5d > 0.001 else 0

        avg_by_fd = [np.mean(all_fd_ics[fd]) if all_fd_ics[fd] else np.nan for fd in FORWARD_DAYS]

        if avg_5d > 0.02 and abs(ir) > 0.5:
            verdict = "✅ 强有效"
        elif avg_5d > 0.01:
            verdict = "⚠️ 弱有效"
        elif avg_5d < -0.02 and abs(ir) > 0.5:
            verdict = "❌ 反向有效"
        elif avg_5d < -0.01:
            verdict = "⚠️ 弱反向"
        else:
            verdict = "⚪ 无效"

        fdef = FACTOR_DEFS[fname]
        label = f"{fname}({fdef['category'][:2]})"
        print(f"{label:<16}{n_sectors:>8}", end="")
        for ic in avg_by_fd:
            print(f"  {ic:+.4f}" if not np.isnan(ic) else "    N/A ", end="")
        print(f"  {ir:+.2f}  {verdict}")

        summary[fname] = {
            "avg_ic_5d": float(avg_5d),
            "ir": float(ir),
            "n_sectors": n_sectors,
            "avg_by_fd": [float(x) if not np.isnan(x) else None for x in avg_by_fd],
            "verdict": verdict,
        }

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "fund_factor_ic.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": summary,
                "by_sector": {k: _serialize(v) for k, v in all_factor_stats.items()},
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=float,
        )

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


def _serialize(obj):
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize(x) for x in obj]
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    else:
        return obj


if __name__ == "__main__":
    main()
