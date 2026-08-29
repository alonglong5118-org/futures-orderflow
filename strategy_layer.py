"""da龘 战略层 v1：da哥 8 策略实时重算 + regime 路由 + 仓位预算。

数据无关：输入日线 DataFrame(columns=[open,high,low,close,volume], DatetimeIndex)，
输出战略信号 dict。算法/权重/风控公式沿用 da哥 操作系统（方向中性、8策略、45%红线、3%单笔）。

策略清单：
  1 ma_break  MA突破(趋势)   2 dma 双均线(趋势)   3 turtle 海龟(趋势)
  4 donchian  通道突破(趋势)  5 pullback 回踩(趋势) 6 boll 布林带(均值回归)
  7 rsi       RSI(均值回归)   8 seasonal 季节性(季节性)

优化记录 (2026-08-19):
  1. 精简策略函数：减少重复计算，统一返回格式
  2. 优化滚动计算：使用 pandas 内置方法，减少冗余计算
  3. 常量集中管理：阈值和配置分组清晰
  4. 简化路由逻辑：用映射表替代 if-elif 链
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

import feature_manager as _fmg


# ----------------------------------------------------------------------------
# 基础指标
# ----------------------------------------------------------------------------
def sma(s: pd.Series, n: int) -> pd.Series:
    """简单移动平均。"""
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """指数移动平均。"""
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """平均真实波幅。"""
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """相对强弱指数。"""
    d = s.diff()
    up = d.clip(lower=0).rolling(n, min_periods=n).mean()
    dn = (-d.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def crossover(a: pd.Series, b: pd.Series) -> int:
    """判断金叉/死叉。"""
    if len(a) < 2 or len(b) < 2:
        return 0
    if a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1]:
        return 1
    if a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1]:
        return -1
    return 0


# ----------------------------------------------------------------------------
# numpy 高速指标（只返回最后值，避免 pandas rolling 开销）
# ----------------------------------------------------------------------------
def _sma_last(arr, window):
    """SMA 最后值（numpy 版）。"""
    if len(arr) < window:
        return np.nan
    return arr[-window:].mean()


def _sma_array(arr, window):
    """完整 SMA 序列（cumsum O(n)，比逐次 _sma_last 快 O(n) → O(1) 索引）。
    前 window-1 个为 np.nan，sma[i] = arr[i-window+1 : i+1].mean()。"""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    cumsum = np.cumsum(arr)
    # sma[i] = (cumsum[i] - cumsum[i-window]) / window  for i >= window-1
    # 但第一个 sma[window-1] = cumsum[window-1] / window
    out[window - 1 :] = (cumsum[window - 1 :] - np.concatenate([[0], cumsum[:-window]])) / window
    return out


def _rolling_max_last(arr, window):
    """rolling max 最后值（numpy 版）。"""
    if len(arr) < window:
        return np.nan
    return arr[-window:].max()


def _rolling_min_last(arr, window):
    """rolling min 最后值（numpy 版）。"""
    if len(arr) < window:
        return np.nan
    return arr[-window:].min()


def _rolling_max_array(arr, window):
    """完整 rolling max 序列（numpy sliding_window_view O(n)）。
    返回长度 = len(arr)，前 window-1 个为 nan。"""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    # stride tricks 创建窗口视图（不复制数据），然后沿窗口轴取 max
    from numpy.lib.stride_tricks import sliding_window_view

    out[window - 1 :] = sliding_window_view(arr, window).max(axis=1)
    return out


def _rolling_min_array(arr, window):
    """完整 rolling min 序列（numpy sliding_window_view O(n)）。"""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    from numpy.lib.stride_tricks import sliding_window_view

    out[window - 1 :] = sliding_window_view(arr, window).min(axis=1)
    return out


def _rolling_std_last(arr, window, ddof=1):
    """rolling std 最后值（numpy 版，ddof=1 匹配 pandas 默认）。"""
    if len(arr) < window:
        return np.nan
    return arr[-window:].std(ddof=ddof)


def _rolling_std_array(arr, window, ddof=1):
    """完整 rolling std 序列（cumsum O(n)，ddof=1 匹配 pandas 默认）。
    返回长度 = len(arr)，前 window-1 个为 nan。"""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window:
        return out
    # Var = E[X²] - E[X]²  →  rolling var via cumsum
    cs = np.cumsum(arr)
    cs2 = np.cumsum(arr * arr)
    # mean[i] = (cs[i] - cs[i-window]) / window  for i >= window-1
    # 但 i=window-1 时 cs[i-window] = cs[-1]，需特殊处理
    mean = np.empty(n)
    mean_sq = np.empty(n)
    # 用拼接技巧：cs_pad = [0, cs[:-1]]
    cs_prev = np.concatenate([[0], cs[:-window]])
    cs2_prev = np.concatenate([[0], cs2[:-window]])
    mean[window - 1 :] = (cs[window - 1 :] - cs_prev) / window
    mean_sq[window - 1 :] = (cs2[window - 1 :] - cs2_prev) / window
    var = mean_sq[window - 1 :] - mean[window - 1 :] ** 2
    # 浮点误差可能导致极小负值 → clip
    var = np.maximum(var, 0.0)
    if ddof == 1:
        var = var * window / (window - 1)
    out[window - 1 :] = np.sqrt(var)
    return out


def _rsi_last(arr, window):
    """RSI 最后值（numpy 版，Cutler's RSI：简单移动平均，分母为 window）。
    匹配 pandas rolling.mean() 行为：上涨日计入涨幅，下跌日计入 0，最后除以 window。"""
    if len(arr) < window + 1:
        return np.nan
    delta = np.diff(arr[-window - 1 :])  # window 个 delta
    gain = np.where(delta > 0, delta, 0).mean()
    loss = np.where(delta < 0, -delta, 0).mean()
    if loss == 0:
        return 100.0
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def _rsi_array(arr, window):
    """完整 RSI 序列（Cutler's RSI，cumsum O(n) 滚动均值）。
    返回长度 = len(arr)，前 window 个为 nan，rsi[i] = 以 i 为末尾的 window 期 RSI。
    与 _rsi_last 行为完全一致。"""
    n = len(arr)
    out = np.full(n, np.nan)
    if n < window + 1:
        return out
    delta = np.diff(arr)  # 长度 n-1
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # rolling mean of gain / loss over window periods (cumsum O(n))
    # gain[i] 对应 close[i+1] - close[i]，即第 i+1 根的涨幅
    # 所以 rsi[i] 对应 gain[i-window : i] 的均值（i 从 window-1 到 n-2）
    cs_gain = np.cumsum(gain)
    cs_loss = np.cumsum(loss)
    # avg_gain[k] = mean(gain[k-window+1 : k+1])  for k >= window-1
    # 对应 rsi[k+1]（因为 gain[0] 是 close[1]-close[0]，形成第 1 根后的 RSI 输入）
    m = len(gain)  # m = n-1
    avg_gain = np.full(m, np.nan)
    avg_loss = np.full(m, np.nan)
    avg_gain[window - 1 :] = (cs_gain[window - 1 :] - np.concatenate([[0], cs_gain[:-window]])) / window
    avg_loss[window - 1 :] = (cs_loss[window - 1 :] - np.concatenate([[0], cs_loss[:-window]])) / window
    # rsi: loss == 0 → 100; gain == 0 and loss == 0 → 50 (中性)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi_vals = 100 - 100 / (1 + rs)
        # 当 gain 和 loss 都为 0 时，rsi 设为 50（中性）
        both_zero = (avg_gain == 0) & (avg_loss == 0)
        rsi_vals[both_zero] = 50.0
    # 对齐：rsi_vals[k] 对应 close[k+1] 处的 RSI（用了 k+1 之前的 window 个 delta）
    out[window:] = rsi_vals[window - 1 :]
    return out


def _atr_last(high, low, close, window):
    """ATR 最后值（numpy 版，简单移动平均 TR，尾部切片向量化）。
    只计算最后 window+1 个 TR 值（因为只需要最后 window 个的均值），
    大数组下比全量计算省内存 + 更快。"""
    n = len(close)
    if n < window + 1:
        return np.nan
    # 只需要最后 window+1 根数据（TR 需要前收，所以多取 1 根）
    start = n - window - 1
    h = high[start:]
    l = low[start:]
    c = close[start:]
    m = len(c)  # m = window + 1
    prev_close = np.empty(m)
    prev_close[0] = c[0]
    prev_close[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    # 取最后 window 个 TR 的均值（跳过第一个，因为它的 prev_close 是自身）
    return tr[1:].mean()


def _seasonal_month_stats(rets, months):
    """预计算「同月收益率统计」：对每个位置 i，返回截至 i 的当月历史收益的 (count, sum, sum_sq)。
    返回 3 个数组 (cnt_arr, sum_arr, sumsq_arr)，长度 = len(rets)。
    用途：s_seasonal O(1) 查询，省全量 mask + mean + std 开销。

    向量化实现：12 路 cumsum + fancy indexing，比 Python 循环快 ~3×。
    注意：rets[0] 为 NaN（首根无收益率），计算时从索引 1 开始统计。"""
    n = len(rets)
    if n == 0:
        return (np.array([], dtype=np.int32), np.array([], dtype=np.float64), np.array([], dtype=np.float64))

    # NaN → 0 并生成有效掩码（rets[0] 是 NaN，不参与统计）
    valid = ~np.isnan(rets)
    r_clean = np.where(valid, rets, 0.0)
    r_sq = r_clean * r_clean

    # 12 个月掩码（shape: 12 x n），1-indexed month → 0-indexed row
    m0 = np.arange(12).reshape(-1, 1)
    masks = (months == (m0 + 1)) & valid  # 只计入有效收益

    # 每月累计 count/sum/sumsq（12 路 cumsum，全 numpy）
    cnt_cs = np.cumsum(masks, axis=1).astype(np.int32)
    sum_cs = np.cumsum(masks * r_clean, axis=1)
    sumsq_cs = np.cumsum(masks * r_sq, axis=1)

    # fancy indexing：每个位置取当月累计值
    mi = np.asarray(months, dtype=np.int32) - 1
    idx = np.arange(n)
    cnt_arr = cnt_cs[mi, idx]
    sum_arr = sum_cs[mi, idx]
    sumsq_arr = sumsq_cs[mi, idx]

    return cnt_arr, sum_arr, sumsq_arr


def _atr_array(high, low, close, window):
    """完整 ATR 序列（numpy 向量化版，简单移动平均 TR）。
    返回长度为 n 的数组，前 window 个值为 nan。用于 walk-forward 预计算，循环内直接索引。"""
    n = len(close)
    if n < window + 1:
        return np.full(n, np.nan)
    prev_close = np.empty(n)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    # rolling mean using cumsum (O(n))
    atr = np.full(n, np.nan)
    cumsum = np.cumsum(tr)
    atr[window:] = (cumsum[window:] - cumsum[:-window]) / window
    # 第一个有效值用前 window 个的均值修正（cumsum 从 0 开始）
    atr[window - 1] = cumsum[window - 1] / window
    return atr


def precompute_signals(
    close,
    high,
    low,
    months,
    rets,
    sma5,
    sma20,
    sma60,
    rsi14,
    std20,
    hh20,
    ll20,
    hh55,
    ll55,
    seas_cnt,
    seas_sum,
    seas_sumsq,
):
    """一次性预计算全部 8 个策略的信号数组（全向量化，O(n)）。
    返回 dict: {策略名: signal_array}，signal_array 为 int8 数组，值 ∈ {-1, 0, 1}。
    与各策略函数逐点结果完全一致。"""
    n = len(close)
    sigs = {}

    # ── 1. ma_break ──
    # c > ma20 & ma20 > ma60 → 1; c < ma20 & ma20 < ma60 → -1; else 0
    ma_break = np.zeros(n, dtype=np.int8)
    valid = ~(np.isnan(sma20) | np.isnan(sma60))
    cond_long = valid & (close > sma20) & (sma20 > sma60)
    cond_short = valid & (close < sma20) & (sma20 < sma60)
    ma_break[cond_long] = 1
    ma_break[cond_short] = -1
    sigs["ma_break"] = ma_break

    # ── 2. dma（金叉/死叉） ──
    # ma5_prev <= ma20_prev & ma5 > ma20 → 金叉 1
    # ma5_prev >= ma20_prev & ma5 < ma20 → 死叉 -1
    dma = np.zeros(n, dtype=np.int8)
    # 从 i=1 开始（需要 prev）
    sma5_prev = np.concatenate([[np.nan], sma5[:-1]])
    sma20_prev = np.concatenate([[np.nan], sma20[:-1]])
    valid_dma = ~(np.isnan(sma5) | np.isnan(sma20) | np.isnan(sma5_prev) | np.isnan(sma20_prev))
    golden = valid_dma & (sma5_prev <= sma20_prev) & (sma5 > sma20)
    death = valid_dma & (sma5_prev >= sma20_prev) & (sma5 < sma20)
    dma[golden] = 1
    dma[death] = -1
    sigs["dma"] = dma

    # ── 3. turtle(n=20, f=55) ──
    # c > hh20_prev & c > ll55 → 1
    # c < ll20_prev & c < hh55 → -1
    # hh20_prev = 前一根的 20 日最高 = hh20[i-1]
    # ll20_prev = 前一根的 20 日最低 = ll20[i-1]
    turtle = np.zeros(n, dtype=np.int8)
    hh20_prev = np.concatenate([[np.nan], hh20[:-1]])
    ll20_prev = np.concatenate([[np.nan], ll20[:-1]])
    valid_t = ~(np.isnan(hh20_prev) | np.isnan(ll20_prev) | np.isnan(hh55) | np.isnan(ll55))
    t_long = valid_t & (close > hh20_prev) & (close > ll55)
    t_short = valid_t & (close < ll20_prev) & (close < hh55)
    turtle[t_long] = 1
    turtle[t_short] = -1
    sigs["turtle"] = turtle

    # ── 4. donchian(n=20) ──
    # c >= hh20 → 1; c <= ll20 → -1
    donchian = np.zeros(n, dtype=np.int8)
    valid_d = ~(np.isnan(hh20) | np.isnan(ll20))
    d_long = valid_d & (close >= hh20)
    d_short = valid_d & (close <= ll20)
    donchian[d_long] = 1
    donchian[d_short] = -1
    sigs["donchian"] = donchian

    # ── 5. pullback ──
    # ma20 > ma60 & dev < 0.02 & c > ma60 → 1
    # ma20 < ma60 & dev < 0.02 & c < ma60 → -1
    pullback = np.zeros(n, dtype=np.int8)
    valid_pb = ~(np.isnan(sma20) | np.isnan(sma60))
    with np.errstate(divide="ignore", invalid="ignore"):
        dev = np.abs(close - sma20) / sma20
    pb_long = valid_pb & (sma20 > sma60) & (dev < 0.02) & (close > sma60)
    pb_short = valid_pb & (sma20 < sma60) & (dev < 0.02) & (close < sma60)
    pullback[pb_long] = 1
    pullback[pb_short] = -1
    sigs["pullback"] = pullback

    # ── 6. boll(n=20, k=2) ──
    # c <= ma20 - 2*std → 1; c >= ma20 + 2*std → -1
    boll = np.zeros(n, dtype=np.int8)
    valid_b = ~(np.isnan(sma20) | np.isnan(std20))
    upper = sma20 + 2.0 * std20
    lower = sma20 - 2.0 * std20
    b_long = valid_b & (close <= lower)
    b_short = valid_b & (close >= upper)
    boll[b_long] = 1
    boll[b_short] = -1
    sigs["boll"] = boll

    # ── 7. rsi(n=14, lo=30, hi=70) ──
    # rsi <= 30 → 1; rsi >= 70 → -1
    rsi = np.zeros(n, dtype=np.int8)
    valid_r = ~np.isnan(rsi14)
    r_long = valid_r & (rsi14 <= 30.0)
    r_short = valid_r & (rsi14 >= 70.0)
    rsi[r_long] = 1
    rsi[r_short] = -1
    sigs["rsi"] = rsi

    # ── 8. seasonal(min_samples=12) ──
    # avg > 0.0008 & z > 0.3 → 1
    # avg < -0.0008 & z < -0.3 → -1
    seasonal = np.zeros(n, dtype=np.int8)
    valid_s = seas_cnt >= 12
    if np.any(valid_s):
        avg = np.where(valid_s, seas_sum / np.maximum(seas_cnt, 1), 0.0)
        var = np.where(
            valid_s & (seas_cnt > 1),
            (seas_sumsq - seas_sum * seas_sum / np.maximum(seas_cnt, 1)) / np.maximum(seas_cnt - 1, 1),
            0.0,
        )
        var = np.maximum(var, 0.0)
        std = np.sqrt(var)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where((std > 0), avg / std, 0.0)
        s_long = valid_s & (avg > 0.0008) & (z > 0.3)
        s_short = valid_s & (avg < -0.0008) & (z < -0.3)
        seasonal[s_long] = 1
        seasonal[s_short] = -1
    sigs["seasonal"] = seasonal

    return sigs


# ----------------------------------------------------------------------------
# 8 策略（各返回 signal∈{-1,0,1}, detail）
# ----------------------------------------------------------------------------
def s_ma_break(df=None, _close=None, _sma20=None, _sma60=None, _detail=True, **_):
    """MA突破策略（numpy 高速版）。"""
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < 3:
        return 0, {}
    ma20 = _sma20 if _sma20 is not None else _sma_last(close, 20)
    ma60 = _sma60 if _sma60 is not None else _sma_last(close, 60)
    c = close[-1]
    if any(math.isnan(x) for x in (ma20, ma60)):
        return 0, {}
    if c > ma20 and ma20 > ma60:
        return 1, ({"ma20": round(ma20, 2), "ma60": round(ma60, 2)} if _detail else {})
    if c < ma20 and ma20 < ma60:
        return -1, ({"ma20": round(ma20, 2), "ma60": round(ma60, 2)} if _detail else {})
    return 0, ({"ma20": round(ma20, 2), "ma60": round(ma60, 2)} if _detail else {})


def s_dma(df=None, _close=None, _sma5=None, _sma20=None, _sma5_prev=None, _sma20_prev=None, _detail=True, **_):
    """双均线策略（numpy 高速版）。"""
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < 6:
        return 0, {}
    ma5 = _sma5 if _sma5 is not None else _sma_last(close, 5)
    ma20 = _sma20 if _sma20 is not None else _sma_last(close, 20)
    # 交叉判断：上一根 ma5 <= ma20 且当前 ma5 > ma20 → 金叉
    if len(close) < 6:
        return 0, ({"ma5": round(ma5, 2), "ma20": round(ma20, 2)} if _detail else {})
    # 优先使用外部传入的 prev 值（walk-forward 预计算路径，省切片+均值）
    if _sma5_prev is not None and not math.isnan(_sma5_prev):
        ma5_prev = _sma5_prev
    else:
        ma5_prev = close[-6:-1].mean()
    if _sma20_prev is not None and not math.isnan(_sma20_prev):
        ma20_prev = _sma20_prev
    else:
        ma20_prev = close[-21:-1].mean() if len(close) >= 21 else np.nan
    x = 0
    if ma5_prev <= ma20_prev and ma5 > ma20:
        x = 1
    elif ma5_prev >= ma20_prev and ma5 < ma20:
        x = -1
    return x, ({"ma5": round(ma5, 2), "ma20": round(ma20, 2)} if _detail else {})


def s_turtle(df=None, n=20, f=55, _high=None, _low=None, _close=None, **_):
    """海龟策略（numpy 高速版）。"""
    high = _high if _high is not None else (df["high"].values if df is not None else None)
    low = _low if _low is not None else (df["low"].values if df is not None else None)
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < 3:
        return 0, {}
    c = close[-1]
    # hh.iloc[-2] = 倒数第 2 根处的 rolling(n).max = high[-n-1:-1].max()
    hh_prev2 = high[-n - 1 : -1].max() if len(high) >= n + 1 else np.nan
    ll_prev2 = low[-n - 1 : -1].min() if len(low) >= n + 1 else np.nan
    hh55_last = _rolling_max_last(high, f)
    ll55_last = _rolling_min_last(low, f)
    if c > hh_prev2 and c > ll55_last:
        return 1, {}
    if c < ll_prev2 and c < hh55_last:
        return -1, {}
    return 0, {}


def s_donchian(df=None, n=20, _high=None, _low=None, _close=None, **_):
    """通道突破策略（numpy 高速版）。"""
    high = _high if _high is not None else (df["high"].values if df is not None else None)
    low = _low if _low is not None else (df["low"].values if df is not None else None)
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < 2:
        return 0, {}
    c = close[-1]
    hh = _rolling_max_last(high, n)
    ll = _rolling_min_last(low, n)
    if c >= hh:
        return 1, {}
    if c <= ll:
        return -1, {}
    return 0, {}


def s_pullback(df=None, _close=None, _sma20=None, _sma60=None, _detail=True, **_):
    """回踩策略（numpy 高速版）。"""
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < 3:
        return 0, {}
    ma20 = _sma20 if _sma20 is not None else _sma_last(close, 20)
    ma60 = _sma60 if _sma60 is not None else _sma_last(close, 60)
    c = close[-1]
    if any(math.isnan(x) for x in (ma20, ma60)):
        return 0, {}
    dev = abs(c - ma20) / ma20
    if ma20 > ma60 and dev < 0.02 and c > ma60:
        return 1, ({"dev%": round(dev * 100, 2)} if _detail else {})
    if ma20 < ma60 and dev < 0.02 and c < ma60:
        return -1, ({"dev%": round(dev * 100, 2)} if _detail else {})
    return 0, {}


def s_boll(df=None, n=20, k=2.0, _close=None, _sma20=None, _std20=None, _detail=True, **_):
    """布林带策略（numpy 高速版）。"""
    close = _close if _close is not None else (df["close"].values if df is not None else None)
    if close is None or len(close) < n:
        return 0, {}
    m = _sma20 if _sma20 is not None else _sma_last(close, n)
    if _std20 is not None and not math.isnan(_std20):
        sd = _std20
    else:
        sd = _rolling_std_last(close, n)
    up, lo = m + k * sd, m - k * sd
    c = close[-1]
    if c <= lo:
        return 1, ({"lower": round(lo, 2)} if _detail else {})
    if c >= up:
        return -1, ({"upper": round(up, 2)} if _detail else {})
    return 0, {}


def s_rsi(df=None, n=14, lo=30, hi=70, _close=None, _rsi=None, _detail=True, **_):
    """RSI策略（numpy 高速版）。"""
    if _rsi is not None and not math.isnan(_rsi):
        r = _rsi
    else:
        close = _close if _close is not None else (df["close"].values if df is not None else None)
        if close is None or len(close) < n + 1:
            return 0, {}
        r = _rsi_last(close, n)
        if math.isnan(r):
            return 0, {}
    if r <= lo:
        return 1, ({"rsi": round(r, 1)} if _detail else {})
    if r >= hi:
        return -1, ({"rsi": round(r, 1)} if _detail else {})
    return 0, ({"rsi": round(r, 1)} if _detail else {})


def s_seasonal(
    df=None,
    min_samples=12,
    _close=None,
    _months=None,
    _rets=None,
    _seasonal_cnt=None,
    _seasonal_sum=None,
    _seasonal_sumsq=None,
    _detail=True,
    **_,
):
    """季节性策略（numpy 高速版）。"""
    # 快速路径：外部已预计算同月统计量 → O(1) 直接计算
    if _seasonal_cnt is not None and _seasonal_sum is not None and _seasonal_sumsq is not None and _seasonal_cnt >= 0:
        n = int(_seasonal_cnt)
        if n < min_samples:
            return 0, ({"reason": "样本不足", "n": n} if _detail else {})
        avg = _seasonal_sum / n
        # std = sqrt((sum_sq - sum^2/n) / (n-1)) for ddof=1
        var = (_seasonal_sumsq - _seasonal_sum * _seasonal_sum / n) / (n - 1) if n > 1 else 0.0
        if var < 0:
            var = 0.0  # 浮点误差保护
        std = math.sqrt(var)
        z = (avg / std) if (std and std > 0) else 0.0
        if not _detail:
            if avg > 0.0008 and z > 0.3:
                return 1, {}
            if avg < -0.0008 and z < -0.3:
                return -1, {}
            return 0, {}
        detail = {"month_avg%": round(avg * 100, 3), "n": n, "z": round(z, 2)}
        if avg > 0.0008 and z > 0.3:
            return 1, detail
        if avg < -0.0008 and z < -0.3:
            return -1, detail
        return 0, detail

    # 慢速路径：无预计算时 mask + mean + std（兼容旧调用方式）
    if _months is not None:
        months = _months
    elif df is not None and isinstance(df.index, pd.DatetimeIndex):
        months = df.index.month.values
    elif df is not None and "date" in df.columns:
        months = pd.to_datetime(df["date"]).dt.month.values
    else:
        return 0, {"reason": "无日期"}

    if _rets is not None:
        rets = _rets
    else:
        close = _close if _close is not None else (df["close"].values if df is not None else None)
        if close is None or len(close) < 2:
            return 0, {"reason": "样本不足", "n": 0}
        rets = np.empty(len(close))
        rets[0] = np.nan
        rets[1:] = np.diff(close) / close[:-1]

    if len(rets) < 2:
        return 0, {"reason": "样本不足", "n": 0}

    last_month = months[-1]
    mask = months == last_month
    mask[0] = False  # 排除第一个 NaN
    same = rets[mask]

    n = len(same)
    if n < min_samples:
        return 0, {"reason": "样本不足", "n": int(n)}

    avg = np.mean(same)
    std = np.std(same, ddof=1)  # pandas 默认 ddof=1
    z = (avg / std) if (std and std > 0) else 0.0

    if not _detail:
        if avg > 0.0008 and z > 0.3:
            return 1, {}
        if avg < -0.0008 and z < -0.3:
            return -1, {}
        return 0, {}

    detail = {"month_avg%": round(avg * 100, 3), "n": int(n), "z": round(z, 2)}
    if avg > 0.0008 and z > 0.3:
        return 1, detail
    if avg < -0.0008 and z < -0.3:
        return -1, detail
    return 0, detail


# 策略注册
STRATS = {
    "ma_break": s_ma_break,
    "dma": s_dma,
    "turtle": s_turtle,
    "donchian": s_donchian,
    "pullback": s_pullback,
    "boll": s_boll,
    "rsi": s_rsi,
    "seasonal": s_seasonal,
}
TREND_STRATS = ["ma_break", "dma", "turtle", "donchian", "pullback"]
MEAN_STRATS = ["boll", "rsi"]
SEASONAL_STRATS = ["seasonal"]
ALL_STRATS = list(STRATS.keys())


# ----------------------------------------------------------------------------
# 稳健池配置
# ----------------------------------------------------------------------------
STABILITY_THRESHOLD = 0.70
OOS_EXPR_THRESHOLD = 0.15
ROBUST_POOL = {
    "JM": {"stability": 0.70, "oos_expR": 0.15},
    "SA": {"stability": 0.70, "oos_expR": 0.15},
    "RM": {"stability": 0.70, "oos_expR": 0.15},
    "FG": {"stability": 0.70, "oos_expR": 0.15},
    "CF": {"stability": 0.70, "oos_expR": 0.15},
    "V": {"stability": 0.70, "oos_expR": 0.15},
    "RB": {"stability": 0.70, "oos_expR": 0.15},
    "UR": {"stability": 0.70, "oos_expR": 0.15},
    "P": {"stability": 0.70, "oos_expR": 0.15},
    "PF": {"stability": 0.70, "oos_expR": 0.15},
    "HC": {"stability": 0.70, "oos_expR": 0.15},
}


# ----------------------------------------------------------------------------
# P-H 稳健池门控动态配置
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DRIFT_JSON_PATH = os.path.join(HERE, "calibration_drift.json")
ROBUST_GATE_FILE = os.path.join(HERE, "robust_pool_gate.json")
_ROBUST_GATE = {"stability": STABILITY_THRESHOLD, "oos_expR": OOS_EXPR_THRESHOLD}
_ROBUST_GATE_CFG = {
    "enabled": True,
    "auto_adapt": False,
    "relax_pp": 0.5,
    "max_relax": 0.05,
    "floor_oos": 0.10,
    "default_stability": STABILITY_THRESHOLD,
    "default_oos_expR": OOS_EXPR_THRESHOLD,
}


def configure_robust_gate(
    enabled=None,
    auto_adapt=None,
    relax_pp=None,
    max_relax=None,
    floor_oos=None,
    default_stability=None,
    default_oos_expR=None,
):
    """配置稳健池门控参数。"""
    if enabled is not None:
        _ROBUST_GATE_CFG["enabled"] = bool(enabled)
    if auto_adapt is not None:
        _ROBUST_GATE_CFG["auto_adapt"] = bool(auto_adapt)
    if relax_pp is not None:
        _ROBUST_GATE_CFG["relax_pp"] = float(relax_pp)
    if max_relax is not None:
        _ROBUST_GATE_CFG["max_relax"] = float(max_relax)
    if floor_oos is not None:
        _ROBUST_GATE_CFG["floor_oos"] = float(floor_oos)
    if default_stability is not None:
        _ROBUST_GATE_CFG["default_stability"] = float(default_stability)
    if default_oos_expR is not None:
        _ROBUST_GATE_CFG["default_oos_expR"] = float(default_oos_expR)


def _robust_gate_enabled():
    """稳健池门控总开关：特性开关优先，fallback 旧配置。"""
    try:
        mgr = _fmg.get_manager()
        if mgr is not None:
            return mgr.is_enabled("robust_pool_gate")
    except Exception:
        pass
    return bool(_ROBUST_GATE_CFG.get("enabled", True))


def get_robust_gate():
    """返回当前生效的 (stability, oos_expR) 门槛。"""
    if not _robust_gate_enabled():
        return STABILITY_THRESHOLD, OOS_EXPR_THRESHOLD
    return _ROBUST_GATE["stability"], _ROBUST_GATE["oos_expR"]


def set_robust_gate(stability=None, oos_expR=None):
    """内存注入当前门槛。"""
    if stability is not None:
        _ROBUST_GATE["stability"] = float(stability)
    if oos_expR is not None:
        _ROBUST_GATE["oos_expR"] = float(oos_expR)


def load_robust_gate_file(path=None):
    """进程启动/重载时读回灌文件进内存。"""
    if not _robust_gate_enabled():
        return False
    path = path or ROBUST_GATE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        set_robust_gate(stability=d.get("stability"), oos_expR=d.get("oos_expR"))
        return True
    except Exception:
        set_robust_gate(stability=_ROBUST_GATE_CFG["default_stability"], oos_expR=_ROBUST_GATE_CFG["default_oos_expR"])
        return False


def backfill_robust_pool_gate(drift_json=None, out_path=None, auto_adapt=None, cfg=None):
    """从 calibration_drift.json 回灌稳健池 OOS_expR 门槛。"""
    c = cfg or _ROBUST_GATE_CFG
    aa = auto_adapt if auto_adapt is not None else c["auto_adapt"]
    drift_json = drift_json or DRIFT_JSON_PATH
    out_path = out_path or ROBUST_GATE_FILE
    stab = c["default_stability"]
    oos = c["default_oos_expR"]
    ensemble_recent = None
    relaxed = False
    recents = []
    try:
        with open(drift_json, encoding="utf-8") as f:
            dj = json.load(f)
        for it in dj.get("items", []):
            if (it.get("symbol") or "").upper() in ROBUST_POOL:
                ce = it.get("current_expR")
                if ce is not None:
                    try:
                        recents.append(float(ce))
                    except Exception:
                        pass
    except Exception:
        recents = []
    if recents:
        recents.sort()
        ensemble_recent = recents[len(recents) // 2]
    if aa and ensemble_recent is not None and ensemble_recent < oos:
        gap = oos - ensemble_recent
        relax = min(c["max_relax"], gap * c["relax_pp"])
        oos = max(c["floor_oos"], oos - relax)
        relaxed = oos < c["default_oos_expR"]
    if aa:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "calibration_drift.json",
                        "ensemble_recent_expR": ensemble_recent,
                        "auto_adapt": True,
                        "stability": stab,
                        "oos_expR": oos,
                        "relaxed": relaxed,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        set_robust_gate(stability=stab, oos_expR=oos)
    return {
        "written": bool(aa),
        "stability": stab,
        "oos_expR": oos,
        "ensemble_recent_expR": ensemble_recent,
        "relaxed": relaxed,
    }


def walk_forward_gate(symbol):
    """稳健池准入判定。"""
    m = ROBUST_POOL.get((symbol or "").upper())
    if m is None:
        return {
            "passed": False,
            "status": "观察池",
            "stability": None,
            "oos_expR": None,
            "reason": "未纳入 walk-forward 稳健池（观察池持续更新，不出实盘战略信号）",
        }
    stability, oos = m["stability"], m["oos_expR"]
    stab_th, oos_th = get_robust_gate()
    if oos <= -0.10 and stability < 0.50:
        return {
            "passed": False,
            "status": "稳健池·紧急出池",
            "stability": stability,
            "oos_expR": oos,
            "reason": "极端证伪：OOS_expR≤-0.10 且 stability<0.50，当周紧急出池",
        }
    passed = stability >= stab_th and oos >= oos_th and oos > 0
    return {
        "passed": passed,
        "status": "稳健池" if passed else "观察池",
        "stability": stability,
        "oos_expR": oos,
        "reason": (f"已过门槛(stability≥{stab_th:.2f} & OOS_expR≥{oos_th:.2f})" if passed else "未达稳健池门槛"),
    }


# ----------------------------------------------------------------------------
# Regime 路由
# ----------------------------------------------------------------------------
# Regime 分类阈值
REGIME_THRESHOLDS = {
    "atr_thresh": 0.025,
    "flat_dev": 0.008,
    "flat_atr": 0.012,
    "trend_slope": 0.003,
    "trend_dev": 0.010,
}


def classify_regime_array(close, atr14, sma20, sma20_slope_prev, params=None):
    """向量化 regime 分类：返回 (regime_codes, descriptions)。
    regime_codes: int8 数组，0=未知 1=波动 2=震荡 3=趋势 4=过渡
    与 classify_regime 逐点结果一致。"""
    n = len(close)
    p = params or REGIME_THRESHOLDS
    atr_thresh = p["atr_thresh"]
    flat_dev = p["flat_dev"]
    flat_atr = p["flat_atr"]
    trend_slope = p["trend_slope"]
    trend_dev = p["trend_dev"]

    # 计算各指标
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_r = atr14 / close
        dev = np.abs(close - sma20) / sma20
        slope = (sma20 - sma20_slope_prev) / sma20_slope_prev

    # 数据不足（任何指标为 nan）→ 未知
    valid = ~(np.isnan(atr14) | np.isnan(sma20) | np.isnan(sma20_slope_prev) | (close == 0))
    # 前 24 根 sma20_slope_prev 为 nan → 未知
    valid = valid & ~np.isnan(slope)

    # 优先级：波动 > 震荡 > 趋势 > 过渡
    is_vol = valid & (atr_r > atr_thresh)
    is_flat = valid & ~is_vol & (dev < flat_dev) & (atr_r < flat_atr)
    is_trend = valid & ~is_vol & ~is_flat & (np.abs(slope) > trend_slope) & (dev > trend_dev)
    # 剩下的有效 → 过渡
    is_trans = valid & ~is_vol & ~is_flat & ~is_trend

    regime_codes = np.zeros(n, dtype=np.int8)  # 0=未知
    regime_codes[is_vol] = 1  # 波动
    regime_codes[is_flat] = 2  # 震荡
    regime_codes[is_trend] = 3  # 趋势
    regime_codes[is_trans] = 4  # 过渡

    return regime_codes


REGIME_CODE_TO_NAME = {0: "未知", 1: "波动", 2: "震荡", 3: "趋势", 4: "过渡"}
REGIME_NAME_TO_CODE = {v: k for k, v in REGIME_CODE_TO_NAME.items()}


def classify_regime(
    df=None, params=None, _close=None, _high=None, _low=None, _atr14=None, _sma20=None, _sma20_slope_prev=None, **_
):
    """返回 (regime, 描述)。numpy 高速版。"""
    p = params or REGIME_THRESHOLDS
    close_arr = _close if _close is not None else (df["close"].values if df is not None else None)
    high_arr = _high if _high is not None else (df["high"].values if df is not None else None)
    low_arr = _low if _low is not None else (df["low"].values if df is not None else None)
    if close_arr is None:
        return "未知", "无数据"

    if len(close_arr) < 25:
        return "未知", "数据不足"

    # numpy 版：直接算最后值，比 pandas rolling 快 3~5x
    # 外部已传入则直接用（walk-forward 预计算路径）
    ma20_now = _sma20 if (_sma20 is not None and not math.isnan(_sma20)) else _sma_last(close_arr, 20)
    # sma(close, 20).iloc[-5] = 倒数第 5 根处的 SMA20 = close[n-24 : n-4].mean()
    # 外部已传入 slope_prev 则直接用，省切片+均值
    if _sma20_slope_prev is not None and not math.isnan(_sma20_slope_prev):
        ma20_prev = _sma20_slope_prev
    else:
        ma20_prev = close_arr[-24:-4].mean() if len(close_arr) >= 24 else np.nan
    c = close_arr[-1]
    dev = abs(c - ma20_now) / ma20_now
    atr_val = _atr14 if _atr14 is not None else _atr_last(high_arr, low_arr, close_arr, 14)
    atr_r = atr_val / c
    slope = (ma20_now - ma20_prev) / ma20_prev

    if atr_r > p["atr_thresh"]:
        return "波动", f"ATR占比{atr_r * 100:.1f}%偏高"
    if dev < p["flat_dev"] and atr_r < p["flat_atr"]:
        return "震荡", f"MA偏离{dev * 100:.2f}%收敛"
    if abs(slope) > p["trend_slope"] and dev > p["trend_dev"]:
        return "趋势", f"MA斜率{slope * 100:.2f}%偏离{dev * 100:.1f}%"
    return "过渡", f"斜率{slope * 100:.2f}%偏离{dev * 100:.1f}%"


# Regime → 策略权重映射
REGIME_WEIGHTS = {
    "趋势": {**{k: 1.0 for k in TREND_STRATS}, **{k: 0.3 for k in MEAN_STRATS}, "seasonal": 0.2},
    "震荡": {**{k: 0.3 for k in TREND_STRATS}, **{k: 1.0 for k in MEAN_STRATS}, "seasonal": 0.3},
    "波动": {**{k: 0.5 for k in TREND_STRATS}, **{k: 0.2 for k in MEAN_STRATS}, "seasonal": 0.1},
    "过渡": {k: 0.5 for k in ALL_STRATS},
    "未知": {k: 0.5 for k in ALL_STRATS},
}


# ----------------------------------------------------------------------------
# 综合计算：路由 + 方向偏置 + 仓位预算
# ----------------------------------------------------------------------------
# [DEAD CODE · 实时系统未调用]
# 四维实时链路（four_dim_strategy.py / four_dim_live_runner.py）零引用此函数。
# 四维只复用 strategy_layer 的 8 策略 + classify_regime + strat_atr，
# 仓位预算走自己的 risk_gate（见 four_dim_strategy.risk_gate）。
# 勿在此修改仓位/风控逻辑——改了实时系统也读不到，且易与 risk_gate 产生歧义。
# 仅用于本文件 __main__ 离线自测。
def compute_strategy(
    df,
    equity,
    price,
    mult,
    point_value,
    margin_rate=0.10,
    fee_per_hand=3.0,
    used_margin=0.0,
    red_line=0.45,
    risk_pct=0.03,
    regime_params=None,
    strategy_weights=None,
    symbol=None,
    wf_gate=True,
):
    """返回战略信号 dict。"""
    regime, rdesc = classify_regime(df, regime_params)
    res = {}
    for name, fn in STRATS.items():
        try:
            sig, det = fn(df)
        except Exception:
            sig, det = 0, {}
        res[name] = {"signal": int(sig), "detail": det}

    # 获取 regime 对应的基础权重
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["过渡"]).copy()

    # 分商品差异化
    sw = strategy_weights or {}
    if sw:
        weights = {k: weights.get(k, 0.5) * float(sw.get(k, 1.0)) for k in weights}

    score = sum(res[k]["signal"] * weights[k] for k in STRATS)
    maxscore = sum(weights.values())
    direction = 1 if score > 0.5 else (-1 if score < -0.5 else 0)
    confidence = min(1.0, abs(score) / maxscore * 2) if direction else 0.0

    pos = [k for k in STRATS if res[k]["signal"] == direction and direction != 0]
    main = max(pos, key=lambda k: weights[k] * abs(res[k]["signal"])) if pos else None

    # 止损与仓位
    a = atr(df).iloc[-1]
    stop_pts = max(a * 1.5, point_value * 0.5)
    stop_pts = round(stop_pts, 2)
    stop_price = round(price - direction * stop_pts, 2) if direction else None
    risk_hand = stop_pts * mult + 2 * fee_per_hand
    risk_budget = equity * risk_pct
    N_risk = int(risk_budget // risk_hand) if risk_hand > 0 else 0
    margin_per = price * mult * margin_rate
    budget = max(0.0, equity * red_line - used_margin)
    N_margin = int(budget // margin_per) if margin_per > 0 else 0
    N = min(N_risk, N_margin)

    # walk-forward 稳健池准入
    gate = (
        walk_forward_gate(symbol)
        if (wf_gate and symbol)
        else {"passed": True, "status": "—", "stability": None, "oos_expR": None, "reason": "未启用稳健池门槛"}
    )
    gated = not gate["passed"]
    direction_text = {1: "偏多", -1: "偏空", 0: "中性"}[direction]
    if gated:
        direction = 0
        direction_text = "观望"
        confidence = 0.0
        N = 0

    return {
        "regime": regime,
        "regime_desc": rdesc,
        "direction": direction,
        "direction_text": direction_text,
        "confidence": round(confidence, 2),
        "main_strategy": main,
        "stop_pts": stop_pts,
        "stop_price": stop_price,
        "size": N,
        "risk_amount": round(N * risk_hand, 1),
        "strategies": res,
        "pool_status": gate["status"],
        "pool_passed": gate["passed"],
        "wf_stability": gate["stability"],
        "wf_oos_expR": gate["oos_expR"],
        "gate_reason": gate["reason"],
        "gated": gated,
    }


if __name__ == "__main__":
    np.random.seed(1)
    n = 120
    px = 1000 + np.cumsum(np.random.randn(n) * 5)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({"open": px, "high": px + 3, "low": px - 3, "close": px, "volume": 1000}, index=idx)
    # 仅离线自测用，实时不调用
    out = compute_strategy(df, equity=69522, price=px[-1], mult=20, point_value=20, margin_rate=0.10, fee_per_hand=3.0)
    print(
        "regime:",
        out["regime"],
        "| direction:",
        out["direction_text"],
        "| conf:",
        out["confidence"],
        "| main:",
        out["main_strategy"],
        "| stop:",
        out["stop_pts"],
        "| size:",
        out["size"],
    )
