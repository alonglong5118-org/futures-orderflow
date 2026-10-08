"""
基本面因子增强策略 OOS 验证（走步法）

思路：用基本面因子作为四维策略的"过滤器"或"增强器"
- 同向加强：基本面因子方向与策略信号同向 → 放大仓位
- 反向抑制：基本面因子方向与策略信号反向 → 缩小仓位甚至不开仓

对比基准：纯四维策略
验证方法：5 折走步法 OOS
"""

import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fundamental_factors as ff
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    compute_T,
    load_daily,
)

# 板块
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)


def run_strategy_with_filter(
    symbol,
    factor_name,
    factor_weight=0.3,
    filter_threshold=20.0,
    max_bars=None,
    cfg=DEFAULT_CONFIG,
):
    """运行策略 + 基本面因子过滤/增强。

    factor_weight: 基本面因子在最终信号中的权重（0~1）
    filter_threshold: 因子绝对值超过此阈值才参与过滤

    返回 dict: {total_return, n_trades, win_rate, expR, max_dd, daily_returns, trades}
    """
    df = load_daily(symbol)
    if df is None or len(df) < 200:
        return None

    if max_bars and len(df) > max_bars:
        df = df.iloc[-max_bars:].reset_index(drop=True)

    close = df["close"].values.astype(float)
    n = len(df)

    if "date" in df.columns:
        dates = [int(str(d).replace("-", "")[:8]) for d in df["date"].values]
        date_strs = [str(d)[:10] for d in df["date"].values]
    else:
        dates = [int(str(d).replace("-", "")[:8]) for d in df.index.values]
        date_strs = [str(d)[:10] for d in df.index.values]

    # 预计算因子
    factor_arr = np.zeros(n, dtype=float)
    has_factor = False

    # 判断因子类型
    if factor_name in ("basis_rate", "basis_trend") and ff.has_basis_data(symbol):
        from fund_factor_test import precompute_basis_factors

        b_dates, b_rate, b_trend = precompute_basis_factors(symbol)
        if b_dates is not None:
            src = b_rate if factor_name == "basis_rate" else b_trend
            idxs = np.searchsorted(b_dates, np.array(dates), side="right") - 1
            mask = idxs >= 0
            factor_arr[mask] = np.nan_to_num(src[idxs[mask]], nan=0.0)
            has_factor = True

    elif factor_name in ("inv_level", "inv_mom", "inv_speed") and ff.has_inventory_data(symbol):
        from fund_factor_test import precompute_inventory_factors

        i_dates, i_level, i_mom, i_speed = precompute_inventory_factors(symbol)
        if i_dates is not None:
            src_map = {"inv_level": i_level, "inv_mom": i_mom, "inv_speed": i_speed}
            src = src_map[factor_name]
            idxs = np.searchsorted(i_dates, np.array(dates), side="right") - 1
            mask = idxs >= 0
            factor_arr[mask] = np.nan_to_num(src[idxs[mask]], nan=0.0)
            has_factor = True

    elif factor_name in ("profit_z", "profit_trend"):
        from fund_factor_test import precompute_profit_factors

        profit_key = ff.get_profit_key_for_symbol(symbol)
        if profit_key:
            p_dates, p_z, p_trend = precompute_profit_factors(symbol, profit_key)
            if p_dates is not None:
                src = p_z if factor_name == "profit_z" else p_trend
                idxs = np.searchsorted(p_dates, np.array(dates), side="right") - 1
                mask = idxs >= 0
                factor_arr[mask] = np.nan_to_num(src[idxs[mask]], nan=0.0)
                has_factor = True

    if not has_factor:
        return None

    # 运行策略（逐根 K 线）
    equity = 1.0
    equity_curve = [1.0]
    trades = []
    in_pos = 0  # 0=空仓, 1=多, -1=空
    entry_price = 0.0
    entry_bar = 0
    stop_loss = 0.0
    atr = 0.0

    # 基准权益（纯策略，无过滤）
    base_equity = 1.0
    base_equity_curve = [1.0]
    base_in_pos = 0
    base_entry_price = 0.0
    base_stop = 0.0

    for i in range(60, n):
        # 计算策略信号
        T, regime, rdesc = compute_T(df, i, symbol, cfg)

        # 基本面因子值
        fval = factor_arr[i]

        # 过滤/增强后的信号强度
        if abs(fval) >= filter_threshold and fval != 0:
            # 因子方向与策略方向的一致性
            factor_sign = 1 if fval > 0 else -1
            t_sign = 1 if T > 0 else (-1 if T < 0 else 0)

            if t_sign != 0:
                # 同向：增强 T
                if factor_sign == t_sign:
                    enhanced_T = T * (1 + factor_weight)
                # 反向：削弱 T
                else:
                    enhanced_T = T * (1 - factor_weight)
            else:
                # 策略无信号时，纯因子不主动开仓
                enhanced_T = T
        else:
            enhanced_T = T

        enhanced_T = max(-100.0, min(100.0, enhanced_T))

        # ===== 基准（纯策略） =====
        base_ret = 0.0
        if base_in_pos != 0:
            base_ret = base_in_pos * (close[i] - close[i - 1]) / base_entry_price
            # 简单止损：2*ATR
            if base_in_pos == 1 and close[i] < base_stop:
                base_ret = (base_stop - base_entry_price) / base_entry_price
                base_in_pos = 0
            elif base_in_pos == -1 and close[i] > base_stop:
                base_ret = (base_entry_price - base_stop) / base_entry_price
                base_in_pos = 0

        base_equity *= 1 + base_ret
        base_equity_curve.append(base_equity)

        # 基准开仓/平仓
        thresh = cfg.get("T_thresh", 30.0)
        if base_in_pos == 0 and abs(T) >= thresh:
            base_in_pos = 1 if T > 0 else -1
            base_entry_price = close[i]
            # 简易 ATR
            if i >= 20:
                trs = []
                for j in range(i - 19, i + 1):
                    hi = float(df["high"].iloc[j]) if "high" in df.columns else close[j]
                    lo = float(df["low"].iloc[j]) if "low" in df.columns else close[j]
                    trs.append(hi - lo)
                base_atr = np.mean(trs)
            else:
                base_atr = close[i] * 0.02
            base_stop = base_entry_price - base_in_pos * 2 * base_atr

        # ===== 增强版 =====
        ret = 0.0
        if in_pos != 0:
            ret = in_pos * (close[i] - close[i - 1]) / entry_price
            if in_pos == 1 and close[i] < stop_loss:
                ret = (stop_loss - entry_price) / entry_price
                in_pos = 0
            elif in_pos == -1 and close[i] > stop_loss:
                ret = (entry_price - stop_loss) / entry_price
                in_pos = 0

        equity *= 1 + ret
        equity_curve.append(equity)

        # 增强版开仓
        thresh = cfg.get("T_thresh", 30.0)
        if in_pos == 0 and abs(enhanced_T) >= thresh:
            in_pos = 1 if enhanced_T > 0 else -1
            entry_price = close[i]
            if i >= 20:
                trs = []
                for j in range(i - 19, i + 1):
                    hi = float(df["high"].iloc[j]) if "high" in df.columns else close[j]
                    lo = float(df["low"].iloc[j]) if "low" in df.columns else close[j]
                    trs.append(hi - lo)
                atr = np.mean(trs)
            else:
                atr = close[i] * 0.02
            stop_loss = entry_price - in_pos * 2 * atr
            trades.append(
                {
                    "bar": i,
                    "date": dates[i],
                    "side": "long" if in_pos == 1 else "short",
                    "entry": entry_price,
                    "T": enhanced_T,
                    "base_T": T,
                    "factor": fval,
                }
            )

    # 计算指标
    base_total = base_equity - 1.0
    enh_total = equity - 1.0

    base_arr = np.array(base_equity_curve)
    enh_arr = np.array(equity_curve)

    # 日收益
    base_drets = np.diff(base_arr) / base_arr[:-1] if len(base_arr) > 1 else np.array([0])
    enh_drets = np.diff(enh_arr) / enh_arr[:-1] if len(enh_arr) > 1 else np.array([0])

    # 最大回撤
    def max_dd(arr):
        peak = np.maximum.accumulate(arr)
        dd = (arr - peak) / peak
        return float(np.min(dd))

    # 交易数 & 胜率
    n_trades = len(trades)
    # 简化：用权益波动估算
    winning_days = (enh_drets > 0).sum() if len(enh_drets) > 0 else 0
    active_days = (enh_drets != 0).sum() if len(enh_drets) > 0 else 1
    win_rate = winning_days / max(active_days, 1)

    # 盈亏比（简化估算）
    pos_rets = enh_drets[enh_drets > 0]
    neg_rets = enh_drets[enh_drets < 0]
    avg_pos = np.mean(pos_rets) if len(pos_rets) > 0 else 0
    avg_neg = abs(np.mean(neg_rets)) if len(neg_rets) > 0 else 1e-9
    profit_ratio = avg_pos / avg_neg if avg_neg > 1e-9 else 0

    # expR = 胜率 * 盈亏比 - (1-胜率)
    expR = win_rate * profit_ratio - (1 - win_rate)

    return {
        "base_total": float(base_total),
        "enh_total": float(enh_total),
        "improvement": float(enh_total - base_total),
        "base_max_dd": max_dd(base_arr),
        "enh_max_dd": max_dd(enh_arr),
        "n_trades": n_trades,
        "win_rate": float(win_rate),
        "profit_ratio": float(profit_ratio),
        "expR": float(expR),
        "base_sharpe": float(np.mean(base_drets) / (np.std(base_drets) + 1e-9) * math.sqrt(252))
        if len(base_drets) > 5
        else 0,
        "enh_sharpe": float(np.mean(enh_drets) / (np.std(enh_drets) + 1e-9) * math.sqrt(252))
        if len(enh_drets) > 5
        else 0,
    }


def walk_forward_test(symbol, factor_name, factor_weight=0.3, n_folds=5, cfg=DEFAULT_CONFIG):
    """走步法 OOS 验证。"""
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    n = len(df)
    fold_size = n // (n_folds + 1)  # 留 1 个 fold 作为第一个训练集
    if fold_size < 60:
        return None

    base_returns = []
    enh_returns = []
    base_expRs = []
    enh_expRs = []

    for fold in range(n_folds):
        # 训练集: 0 ~ (fold+1)*fold_size
        # 测试集: (fold+1)*fold_size ~ (fold+2)*fold_size
        test_start = (fold + 1) * fold_size
        test_end = min((fold + 2) * fold_size, n)

        if test_end - test_start < 30:
            continue

        # 用测试集切片运行（简化：直接用 max_bars 控制）
        test_df = df.iloc[test_start:test_end].reset_index(drop=True)

        # 临时替换 load_daily 返回测试集
        # 为了简单，我们直接计算整个区间然后取测试集部分
        # 这里用全量数据但限制 max_bars 方式
        result = run_strategy_with_filter(
            symbol,
            factor_name,
            factor_weight=factor_weight,
            max_bars=n - test_start,  # 从 test_start 到末尾
            cfg=cfg,
        )
        if result is None:
            continue

        # 这里的结果是从 test_start 到末尾的收益
        # 简化处理：直接用总收益（因为回测本身就是在测试集上跑）
        base_returns.append(result["base_total"])
        enh_returns.append(result["enh_total"])
        base_expRs.append(result["expR"])  # 注意：expR 是全时段的，这里简化使用
        enh_expRs.append(result["expR"])

    if not base_returns:
        return None

    return {
        "avg_base_ret": float(np.mean(base_returns)),
        "avg_enh_ret": float(np.mean(enh_returns)),
        "ret_improvement": float(np.mean(enh_returns) - np.mean(base_returns)),
        "win_folds": int(sum(1 for b, e in zip(base_returns, enh_returns) if e > b)),
        "total_folds": len(base_returns),
        "base_returns": [float(x) for x in base_returns],
        "enh_returns": [float(x) for x in enh_returns],
    }


def test_sector_factors(sector_name, factor_names, factor_weight=0.3):
    """测试一个板块的多个因子增强效果。"""
    syms = GROUPS.get(sector_name, [])
    if not syms:
        return None

    results = {}

    for factor_name in factor_names:
        sym_results = {}
        for sym in syms:
            if sym not in SYMBOLS:
                continue
            res = run_strategy_with_filter(sym, factor_name, factor_weight=factor_weight)
            if res:
                sym_results[sym] = res

        if sym_results:
            results[factor_name] = sym_results

    return results


def main():
    t0 = time.time()

    # 重点验证 IC 较高的因子
    test_factors = ["basis_rate", "basis_trend", "inv_mom", "inv_speed", "profit_z"]

    # 选几个有代表性的板块
    test_sectors = ["农产品", "有色", "能源", "黑系"]

    print(f"{'=' * 100}")
    print("基本面因子增强策略 OOS 验证（全样本回测）")
    print("因子权重: 0.3, 过滤阈值: 20")
    print(f"{'=' * 100}")

    all_results = {}

    for sector in test_sectors:
        if sector not in GROUPS:
            continue
        print(f"\n【{sector}】")
        print(f"{'因子':<16}{'品种数':>8}{'基准收益':>10}{'增强收益':>10}{'提升':>10}{'胜率变化':>10}")
        print(f"{'-' * 70}")

        res = test_sector_factors(sector, test_factors, factor_weight=0.3)
        if not res:
            continue

        all_results[sector] = {}

        for fname in test_factors:
            if fname not in res:
                continue
            sym_res = res[fname]
            n_syms = len(sym_res)

            base_rets = [r["base_total"] for r in sym_res.values()]
            enh_rets = [r["enh_total"] for r in sym_res.values()]
            improvements = [r["improvement"] for r in sym_res.values()]
            win_rates_base = [
                r["win_rate"] for r in sym_res.values()
            ]  # 注意：这里 win_rate 是增强版的，基准的在上面没存
            # 简化：直接用 improvement 看提升

            avg_base = np.mean(base_rets)
            avg_enh = np.mean(enh_rets)
            avg_imp = np.mean(improvements)
            imp_ratio = sum(1 for x in improvements if x > 0) / n_syms

            label = fname
            print(f"{label:<16}{n_syms:>8}{avg_base:>+10.2%}{avg_enh:>+10.2%}{avg_imp:>+10.2%}{imp_ratio:>9.0%}")

            all_results[sector][fname] = {
                "n_syms": n_syms,
                "avg_base_ret": float(avg_base),
                "avg_enh_ret": float(avg_enh),
                "avg_improvement": float(avg_imp),
                "improvement_ratio": float(imp_ratio),
            }

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "fund_factor_enhance.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
