#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new_factor_explorer.py — 新因子挖掘与有效性检验
=================================================

候选新因子（5个）：
  1. V_vol      波动率因子    — 滚动波动率分位 + 波动率变化方向
  2. Vol_vol    成交量因子    — 量价配合度（放量涨/缩量涨等）
  3. OI_int     持仓量因子    — 仓价配合（增仓上涨=强趋势）
  4. SR_dist    SR距离因子    — 距最近支撑/压力位的标准化距离
  5. Inv_stock  库存因子      — 库存水平分位 + 库存变化率

检验方法：
  - 单因子 IC（信息系数）：因子值与未来 N 日收益的秩相关
  - IR（信息比率）：IC 均值 / IC 标准差
  - 分层回测：因子值分 5 层，各层未来收益差
  - 方向性胜率：因子>0 时未来收益>0 的比例

用法：
  python3 new_factor_explorer.py --sector 化工 --forward 5
  python3 new_factor_explorer.py --all --save
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fundamental_feed as ff
import sr_analyzer as sra
from four_dim_strategy import SYMBOLS, load_daily

# 板块定义（从 SYMBOLS 构建）
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    # 排除带数字后缀的具体合约（如 SA01）
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)

# ============================================================================
# 新因子计算
# ============================================================================

def compute_V_vol(df, lookback=20):
    """波动率因子：滚动波动率分位 + 波动率变化
    返回 V_vol ∈ [-100, 100]
    逻辑：波动率低=利于做多（正），波动率高+上升=利空（负）
    """
    if df is None or len(df) < lookback + 5:
        return 0.0
    close = df["close"].astype(float).values
    ret = np.diff(np.log(close))

    # 滚动波动率（20日）
    vols = np.array([float(np.std(ret[max(0, i-lookback):i+1])) * np.sqrt(252)
                     for i in range(len(ret))])
    if len(vols) < 10:
        return 0.0

    # 当前波动率在过去 lookback*2 天的分位
    hist_len = min(len(vols), lookback * 2)
    current_vol = vols[-1]
    vol_percentile = np.mean(vols[-hist_len:] <= current_vol)

    # 波动率变化（近 5 日 vs 前 5 日）
    if len(vols) >= 10:
        vol_chg = (np.mean(vols[-5:]) - np.mean(vols[-10:-5])) / (np.mean(vols[-10:-5]) + 1e-8)
    else:
        vol_chg = 0.0

    # 合成：低波动分位 → 正；波动率下降 → 正
    # 分位 0.5 为中性，<0.5 正，>0.5 负
    score_pct = (0.5 - vol_percentile) * 2 * 100  # [-100, 100]
    # 波动率变化：下降为正，上升为负
    score_chg = -max(-1.0, min(1.0, vol_chg * 2)) * 50  # [-50, 50]

    v = score_pct * 0.6 + score_chg * 0.4
    return round(max(-100.0, min(100.0, v)), 1)


def compute_Vol_vol(df, lookback=20):
    """成交量因子：量价配合度
    返回 Vol_vol ∈ [-100, 100]
    逻辑：放量上涨=强多头，缩量下跌=弱空头（可能反弹），放量下跌=强空头
    """
    if df is None or len(df) < lookback + 5:
        return 0.0
    close = df["close"].astype(float).values
    volume = df["volume"].astype(float).values

    # 收益率
    ret = np.diff(close) / (close[:-1] + 1e-8)

    # 成交量变化（相对 20 日均量）
    vol_ma = np.array([float(np.mean(volume[max(0, i-lookback):i+1]))
                       for i in range(len(volume))])
    vol_ratio = volume / (vol_ma + 1e-8)

    # 最近 5 日的量价配合
    n = min(5, len(ret))
    if n < 3:
        return 0.0

    recent_ret = ret[-n:]
    recent_vol_ratio = vol_ratio[-n:]  # volume/vol_ma 对齐到 ret 的索引

    # 量价配合：上涨+放量=强正，下跌+放量=强负
    # 缩量上涨=弱正，缩量下跌=弱负（或中性）
    scores = []
    for i in range(n):
        r = recent_ret[i]
        vr = recent_vol_ratio[i] if i < len(recent_vol_ratio) else 1.0
        # 量能放大因子：vr > 1 放大信号，vr < 1 缩小信号
        vol_mult = 0.5 + 0.5 * min(2.0, vr)  # [0.5, 1.5]
        s = r * 100 * vol_mult  # 收益率 × 量能系数
        scores.append(s)

    # 加权（越近权重越高）
    weights = np.linspace(0.5, 1.5, n)
    weighted = np.average(scores, weights=weights)

    # 归一化到 [-100, 100]
    v = max(-100.0, min(100.0, weighted * 10))
    return round(v, 1)


def compute_OI_int(df, lookback=20):
    """持仓量因子：仓价配合
    返回 OI_int ∈ [-100, 100]
    逻辑：增仓上涨=多头主动进场（强趋势），减仓下跌=多头平仓（弱趋势）
    """
    if df is None or len(df) < lookback + 5:
        return 0.0
    close = df["close"].astype(float).values
    oi = df["oi"].astype(float).values

    if len(oi) < 10:
        return 0.0

    # 持仓量变化率（近 5 日）
    oi_chg_5d = (oi[-1] - oi[-6]) / (oi[-6] + 1e-8) if len(oi) >= 6 else 0.0

    # 近 5 日收益率
    ret_5d = (close[-1] - close[-6]) / (close[-6] + 1e-8) if len(close) >= 6 else 0.0

    # 持仓量趋势（20日方向）
    if len(oi) >= lookback:
        oi_trend = (oi[-1] - np.mean(oi[-lookback:])) / (np.mean(oi[-lookback:]) + 1e-8)
    else:
        oi_trend = 0.0

    # 仓价配合：
    # 增仓 + 上涨 = 强多（正）
    # 增仓 + 下跌 = 强空（负）
    # 减仓 + 上涨 = 弱多（正但小）
    # 减仓 + 下跌 = 弱空（负但小）
    oi_direction = 1 if oi_chg_5d > 0 else -1
    price_direction = 1 if ret_5d > 0 else -1

    # 同向 = 强趋势（增仓确认方向）
    # 反向 = 弱趋势（减仓 = 平仓，趋势可能衰竭）
    if oi_direction == price_direction:
        strength = 1.0  # 增仓同向 = 强
    else:
        strength = 0.4  # 反向 = 弱

    # 基础分 = 价格方向 × 强度 × 幅度
    base = price_direction * strength * min(1.0, abs(ret_5d) * 20) * 100

    # 加上持仓趋势的确认
    trend_confirm = oi_trend * 50 * (1 if oi_trend * ret_5d > 0 else 0.3)

    v = base * 0.7 + trend_confirm * 0.3
    return round(max(-100.0, min(100.0, v)), 1)


def compute_SR_dist(symbol, df, lookback=100):
    """SR 距离因子：距最近支撑/压力位的标准化距离
    返回 SR_dist ∈ [-100, 100]
    逻辑：靠近支撑位 → 做多信号（正），靠近压力位 → 做空信号（负）
    支撑越近越正，压力越近越负
    """
    if df is None or len(df) < 60:
        return 0.0
    try:
        # 用最近 lookback 根 K 线找 SR 位
        df_recent = df.iloc[-min(lookback, len(df)):].copy()
        result = sra.find_sr_levels(df_recent, symbol=symbol)
        if not result or not result.get("levels"):
            return 0.0

        current_price = float(df["close"].iloc[-1])
        nearest_sup = result.get("nearest_support")
        nearest_res = result.get("nearest_resistance")

        sup_dist = nearest_sup["distance_pct"] if nearest_sup else 999.0
        res_dist = nearest_res["distance_pct"] if nearest_res else 999.0

        # 距离越小，信号越强
        # 支撑信号：近支撑 → 正分
        sup_score = max(0.0, 1.0 - sup_dist / 5.0) * 100  # 5% 以内有信号
        # 压力信号：近压力 → 负分
        res_score = max(0.0, 1.0 - res_dist / 5.0) * 100

        v = sup_score - res_score
        return round(max(-100.0, min(100.0, v)), 1)
    except Exception:
        return 0.0


def compute_Inv_stock(symbol, date_str):
    """库存因子：库存变化趋势
    返回 Inv_stock ∈ [-100, 100]
    逻辑：去库（库存下降）= 利多（正），累库（库存上升）= 利空（负）
    用 fundamental_feed.inventory_trend_on 获取近 3 期净变化
    """
    try:
        inv_chg = ff.inventory_trend_on(symbol, date_str)
        if inv_chg is None or inv_chg == 0:
            return 0.0

        # 库存变化率归一化：去库为正，累库为负
        # 假设典型库存波动幅度为 5%（需要校准）
        v = -float(inv_chg) * 20  # 去库 5% → +100 分
        return round(max(-100.0, min(100.0, v)), 1)
    except Exception:
        return 0.0


# ============================================================================
# 单因子有效性检验
# ============================================================================

NEW_FACTOR_NAMES = ["V_vol", "Vol_vol", "OI_int", "SR_dist", "Inv_stock"]


def compute_all_new_factors(symbol, df_daily, date_idx=-1):
    """计算所有新因子，返回 dict。"""
    result = {}
    df = df_daily

    # V_vol
    try:
        result["V_vol"] = compute_V_vol(df)
    except Exception:
        result["V_vol"] = 0.0

    # Vol_vol
    try:
        result["Vol_vol"] = compute_Vol_vol(df)
    except Exception:
        result["Vol_vol"] = 0.0

    # OI_int
    try:
        result["OI_int"] = compute_OI_int(df)
    except Exception:
        result["OI_int"] = 0.0

    # SR_dist
    try:
        result["SR_dist"] = compute_SR_dist(symbol, df)
    except Exception:
        result["SR_dist"] = 0.0

    # Inv_stock
    try:
        if hasattr(df, 'index') and len(df) > 0:
            date_str = str(df.index[date_idx].date()) if date_idx < 0 else str(df.index[date_idx].date())
        else:
            date_str = None
        result["Inv_stock"] = compute_Inv_stock(symbol, date_str) if date_str else 0.0
    except Exception:
        result["Inv_stock"] = 0.0

    return result


def single_factor_test(symbol, factor_fn, factor_name, forward_days=5, min_bars=60, max_bars=None, step=1):
    """单因子有效性检验：IC、IR、分层收益、胜率
    factor_fn(symbol, df) -> float 返回 df 最后一根的因子值
    max_bars: 限制最大使用的 bar 数（从末尾取）
    step: 每隔 step 根计算一次（加速用）
    """
    df = load_daily(symbol)
    if df is None or len(df) < min_bars + forward_days:
        return None

    # 限制最大样本数（从末尾截取）
    if max_bars and len(df) > max_bars:
        df = df.iloc[-max_bars:]

    close = df["close"].astype(float).values
    n = len(df)

    # 计算每根 bar 的因子值和未来收益
    factor_vals = []
    future_rets = []
    valid_idx = []

    for i in range(min_bars, n - forward_days, step):
        try:
            df_slice = df.iloc[:i+1]
            fv = factor_fn(symbol, df_slice)
            # 未来 N 日收益率
            ret = (close[i + forward_days] - close[i + 1]) / (close[i + 1] + 1e-8) * 100
            factor_vals.append(fv)
            future_rets.append(ret)
            valid_idx.append(i)
        except Exception:
            continue

    if len(factor_vals) < 30:
        return None

    fv = np.array(factor_vals)
    fr = np.array(future_rets)

    # IC: Spearman 秩相关（用 numpy 实现，避免 scipy 版本兼容问题）
    def _spearman_r(x, y):
        """计算 Spearman 秩相关系数。"""
        rx = np.argsort(np.argsort(x)).astype(float)
        ry = np.argsort(np.argsort(y)).astype(float)
        rx -= rx.mean()
        ry -= ry.mean()
        denom = np.sqrt((rx**2).sum() * (ry**2).sum())
        if denom < 1e-12:
            return 0.0
        return float((rx * ry).sum() / denom)

    ic = _spearman_r(fv, fr)
    # p 值近似（大样本下用 Fisher z 变换）
    n = len(fv)
    if n > 10:
        z = 0.5 * np.log((1 + ic) / (1 - ic + 1e-8))
        se = 1.0 / np.sqrt(n - 3)
        from math import erfc
        p_val = erfc(abs(z) / (se * np.sqrt(2)))
    else:
        p_val = 1.0

    # 滚动 IC 计算 IR
    window = 20
    if len(fv) >= window * 2:
        ics = []
        for w in range(window, len(fv), window):
            s = max(0, w - window)
            try:
                ic_w = _spearman_r(fv[s:w], fr[s:w])
                if not np.isnan(ic_w):
                    ics.append(ic_w)
            except Exception:
                pass
        ir = np.mean(ics) / (np.std(ics) + 1e-8) if ics else 0
    else:
        ir = 0.0

    # 分层检验（5 层）
    n_layers = 5
    sorted_idx = np.argsort(fv)
    layer_size = len(fv) // n_layers
    layer_returns = []
    for l in range(n_layers):
        start = l * layer_size
        end = (l + 1) * layer_size if l < n_layers - 1 else len(fv)
        layer_ret = np.mean(fr[sorted_idx[start:end]])
        layer_returns.append(layer_ret)

    # 多空收益（顶层 - 底层）
    ls_return = layer_returns[-1] - layer_returns[0]

    # 方向性胜率：因子>0 时 未来收益>0 的比例
    pos_mask = fv > 0
    neg_mask = fv < 0
    win_rate_pos = np.mean(fr[pos_mask] > 0) if np.sum(pos_mask) > 10 else 0.5
    win_rate_neg = np.mean(fr[neg_mask] < 0) if np.sum(neg_mask) > 10 else 0.5

    return {
        "symbol": symbol,
        "factor": factor_name,
        "ic": round(float(ic), 4),
        "ir": round(float(ir), 3),
        "p_value": round(float(p_val), 4),
        "n_samples": len(fv),
        "layer_returns": [round(r, 3) for r in layer_returns],
        "long_short_return": round(float(ls_return), 3),
        "win_rate_pos": round(float(win_rate_pos), 3),
        "win_rate_neg": round(float(win_rate_neg), 3),
        "mean_abs_factor": round(float(np.mean(np.abs(fv))), 2),
    }


# ============================================================================
# 主程序
# ============================================================================

def test_sector(sector_name, forward=5, max_bars=600, step=3):
    """测试一个板块所有品种的所有新因子。"""
    syms = GROUPS.get(sector_name, [])
    if not syms:
        print(f"板块 {sector_name} 无品种")
        return {}

    results = {}
    for sym in syms:
        print(f"  {sym}...", end=" ", flush=True)
        sym_results = {}
        for fname in NEW_FACTOR_NAMES:
            fn = _make_factor_fn(fname)
            r = single_factor_test(sym, fn, fname, forward_days=forward,
                                   max_bars=max_bars, step=step)
            if r:
                sym_results[fname] = r
        if sym_results:
            results[sym] = sym_results
            valid = [k for k, v in sym_results.items() if abs(v["ic"]) > 0.02]
            print(f"有效因子: {len(valid)}/5" if valid else "全弱")
        else:
            print("数据不足")

    return results


def _make_factor_fn(factor_name):
    """构造单参数因子函数供 single_factor_test 使用。"""
    def fn(symbol, df):
        if factor_name == "V_vol":
            return compute_V_vol(df)
        elif factor_name == "Vol_vol":
            return compute_Vol_vol(df)
        elif factor_name == "OI_int":
            return compute_OI_int(df)
        elif factor_name == "SR_dist":
            return compute_SR_dist(symbol, df)
        elif factor_name == "Inv_stock":
            try:
                date_str = str(df.index[-1].date())
                return compute_Inv_stock(symbol, date_str)
            except Exception:
                return 0.0
        return 0.0
    return fn


def summarize_results(results_by_sector):
    """汇总全板块结果。"""
    summary = {}
    for sector, results in results_by_sector.items():
        sector_summary = {}
        for fname in NEW_FACTOR_NAMES:
            ics = []
            irs = []
            lss = []
            wr_pos = []
            wr_neg = []
            n_valid = 0
            for sym, sym_res in results.items():
                if fname in sym_res:
                    r = sym_res[fname]
                    ics.append(r["ic"])
                    irs.append(r["ir"])
                    lss.append(r["long_short_return"])
                    wr_pos.append(r["win_rate_pos"])
                    wr_neg.append(r["win_rate_neg"])
                    n_valid += 1
            if ics:
                sector_summary[fname] = {
                    "avg_ic": round(float(np.mean(ics)), 4),
                    "median_ic": round(float(np.median(ics)), 4),
                    "avg_ir": round(float(np.mean(irs)), 3),
                    "avg_ls_return": round(float(np.mean(lss)), 3),
                    "avg_wr_pos": round(float(np.mean(wr_pos)), 3),
                    "avg_wr_neg": round(float(np.mean(wr_neg)), 3),
                    "n_valid": n_valid,
                    "n_positive_ic": sum(1 for ic in ics if ic > 0),
                    "n_significant": sum(1 for ic in ics if abs(ic) > 0.03),
                }
        summary[sector] = sector_summary
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", type=str, default=None, help="测试单个板块")
    parser.add_argument("--all", action="store_true", help="测试所有板块")
    parser.add_argument("--forward", type=int, default=5, help="预测天数")
    parser.add_argument("--max-bars", type=int, default=600, help="每品种最大bar数")
    parser.add_argument("--step", type=int, default=3, help="每隔几根bar计算一次")
    parser.add_argument("--save", action="store_true", help="保存结果到 JSON")
    args = parser.parse_args()

    sectors = []
    if args.all:
        sectors = list(GROUPS.keys())
    elif args.sector:
        sectors = [args.sector]
    else:
        sectors = ["化工"]  # 默认

    print(f"=== 新因子有效性检验（预测 {args.forward} 日）===")
    print(f"候选因子: {', '.join(NEW_FACTOR_NAMES)}")
    print(f"板块: {', '.join(sectors)}")
    print(f"参数: max_bars={args.max_bars}, step={args.step}")
    print()

    all_results = {}
    for sector in sectors:
        print(f"\n【{sector}】")
        r = test_sector(sector, forward=args.forward,
                        max_bars=args.max_bars, step=args.step)
        all_results[sector] = r

    # 汇总
    summary = summarize_results(all_results)

    print("\n" + "=" * 70)
    print("汇总：各因子平均 IC")
    print("=" * 70)
    print(f"{'板块':<8}", end="")
    for fname in NEW_FACTOR_NAMES:
        print(f"{fname:>12}", end="")
    print()
    for sector, s in summary.items():
        print(f"{sector:<8}", end="")
        for fname in NEW_FACTOR_NAMES:
            if fname in s:
                ic = s[fname]["avg_ic"]
                mark = "*" if abs(ic) > 0.03 else " "
                print(f"{ic:>11.4f}{mark}", end="")
            else:
                print(f"{'N/A':>12}", end="")
        print()

    print("\n|IC|>0.03 标记 *，|IC|>0.05 标记 **")
    print()

    if args.save:
        out_file = os.path.join(HERE, "logs", f"new_factor_explore_f{args.forward}.json")
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "detail": all_results,
                       "forward_days": args.forward}, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {out_file}")


if __name__ == "__main__":
    main()
