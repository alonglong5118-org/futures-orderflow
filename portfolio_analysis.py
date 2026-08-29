"""组合层面分析模块 v1.0
=================================================================
四维策略多品种组合分析：相关性分析、资金分配、组合回测、绩效报告。

功能模块：
1. 品种相关性分析（价格收益 + 交易信号）
2. 资金分配方法（等权/波动率倒数/凯利/夏普/最小方差）
3. 组合回测框架（合并交易、组合指标、walk-forward）
4. 输出报告（文字热力图、绩效对比、推荐配置）
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# 项目内导入
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    DISABLED_SYMBOLS,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

TRADING_DAYS_PER_YEAR = 252


# ============================================================================
# 1. 品种相关性分析
# ============================================================================


def get_whitelist_symbols(cfg=DEFAULT_CONFIG, min_trades=10):
    """获取白名单品种（expR > 0 的品种）。

    返回: list of symbol codes
    """
    whitelist = []
    for sym in SYMBOLS:
        if sym in DISABLED_SYMBOLS:
            continue
        if sym not in SYMBOLS or "SA01" in sym:
            continue  # 跳过具体交割合约
        try:
            result = walk_forward_backtest(sym, cfg)
            if result.get("trades", 0) >= min_trades and result.get("expR", 0) > 0:
                whitelist.append(sym)
        except Exception:
            continue
    return whitelist


def compute_price_return_correlation(symbols, lookback_days=None):
    """基于日线收盘价的日收益率相关矩阵。

    参数:
        symbols: list of symbol codes
        lookback_days: 回溯天数，None 表示使用全部可用数据

    返回:
        pd.DataFrame: 相关矩阵（index/columns 为 symbol）
    """
    close_dict = {}
    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < 30:
            continue
        close = df["close"].copy()
        if lookback_days and len(close) > lookback_days:
            close = close.tail(lookback_days)
        close_dict[sym] = close

    if not close_dict:
        return pd.DataFrame()

    # 对齐日期索引
    close_df = pd.DataFrame(close_dict)
    close_df = close_df.dropna(how="all")
    returns = close_df.pct_change().dropna(how="all")

    # 用 pairwise 相关（容忍部分缺失）
    corr = returns.corr(method="pearson")
    return corr


def compute_signal_direction_series(symbols, cfg=DEFAULT_CONFIG):
    """计算各品种每日交易方向序列（1=多, -1=空, 0=无持仓）。

    通过 walk_forward_backtest 获取交易，然后构建每日方向序列。

    参数:
        symbols: list of symbol codes
        cfg: 策略配置

    返回:
        pd.DataFrame: 每日方向矩阵（index=日期, columns=symbol, values=1/-1/0）
    """
    direction_dict = {}
    all_dates = set()

    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < 30:
            continue

        try:
            result = walk_forward_backtest(sym, cfg)
        except Exception:
            continue

        trades = result.get("trades_detail", [])
        if not trades:
            continue

        # 初始化方向序列为 0
        dates = df.index
        dir_series = pd.Series(0, index=dates, dtype=int)

        for trade in trades:
            entry_date = trade.get("entry_date")
            if entry_date is None:
                continue
            # 找到入场 bar 的索引
            if entry_date not in dates:
                continue
            entry_idx = dates.get_loc(entry_date)

            # 估算持仓天数：根据 R 值和 ATR 粗略估计
            # 更精确的方式需要 exit_date，这里简化处理：
            # 假设每笔交易平均持仓 5 根日线（典型趋势跟踪）
            # 实际项目中应从 trades_detail 中取 exit_date
            direction = int(trade.get("dir", 0))
            # 从入场日开始设方向，直到下一根或默认 5 天
            hold_bars = 5  # 默认持仓天数（实际应由 exit_date 确定）
            end_idx = min(entry_idx + hold_bars, len(dates) - 1)
            dir_series.iloc[entry_idx : end_idx + 1] = direction

        direction_dict[sym] = dir_series
        all_dates.update(dates)

    if not direction_dict:
        return pd.DataFrame()

    dir_df = pd.DataFrame(direction_dict)
    dir_df = dir_df.fillna(0).astype(int)
    return dir_df


def compute_signal_correlation(symbols, cfg=DEFAULT_CONFIG):
    """基于各品种每日交易方向的相关性矩阵。

    参数:
        symbols: list of symbol codes
        cfg: 策略配置

    返回:
        pd.DataFrame: 信号相关矩阵
    """
    dir_df = compute_signal_direction_series(symbols, cfg)
    if dir_df.empty or len(dir_df.columns) < 2:
        return pd.DataFrame()

    # 只计算非零日的相关性更有意义，但为了矩阵完整性使用全部日期
    corr = dir_df.corr(method="pearson")
    return corr


def group_by_sector(symbols):
    """按板块分组品种。

    返回: dict {group_name: [symbols]}
    """
    groups = defaultdict(list)
    for sym in symbols:
        info = SYMBOLS.get(sym, {})
        group = info.get("group", "其他")
        groups[group].append(sym)
    return dict(groups)


def find_high_corr_pairs(corr_matrix, threshold=0.6):
    """识别高相关对（|r| > threshold）。

    返回: list of (sym1, sym2, corr_value) 按相关系数绝对值降序
    """
    pairs = []
    syms = corr_matrix.columns.tolist()
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            val = corr_matrix.iloc[i, j]
            if pd.isna(val):
                continue
            if abs(val) > threshold:
                pairs.append((syms[i], syms[j], round(float(val), 4)))
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    return pairs


def find_low_corr_pairs(corr_matrix, threshold=0.2):
    """识别低相关对（|r| < threshold）。

    返回: list of (sym1, sym2, corr_value) 按相关系数绝对值升序
    """
    pairs = []
    syms = corr_matrix.columns.tolist()
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            val = corr_matrix.iloc[i, j]
            if pd.isna(val):
                continue
            if abs(val) < threshold:
                pairs.append((syms[i], syms[j], round(float(val), 4)))
    pairs.sort(key=lambda x: abs(x[2]))
    return pairs


# ============================================================================
# 2. 资金分配方法
# ============================================================================


def _normalize_weights(weights_dict):
    """归一化权重，使权重之和为 1。"""
    total = sum(weights_dict.values())
    if total <= 0:
        n = len(weights_dict)
        return {k: 1.0 / n for k in weights_dict}
    return {k: v / total for k, v in weights_dict.items()}


def equal_weight(symbols):
    """等权配置（基准）。

    返回: dict {symbol: weight}
    """
    n = len(symbols)
    if n == 0:
        return {}
    return {sym: 1.0 / n for sym in symbols}


def volatility_inverse_weight(symbols, lookback_days=252):
    """波动率倒数配置（风险平价简化版）。

    权重 = 1 / 日收益率波动率，然后归一化。

    返回: dict {symbol: weight}
    """
    vol_dict = {}
    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < 30:
            continue
        close = df["close"]
        if lookback_days and len(close) > lookback_days:
            close = close.tail(lookback_days)
        returns = close.pct_change().dropna()
        vol = float(returns.std())
        if vol > 0:
            vol_dict[sym] = 1.0 / vol

    if not vol_dict:
        return equal_weight(symbols)

    return _normalize_weights(vol_dict)


def kelly_weight(symbol_metrics):
    """凯利配置（基于 expR 和胜率）。

    简化版 Kelly: f* = (p * b - q) / b
    其中 p=胜率, q=1-p, b=盈亏比

    参数:
        symbol_metrics: dict {symbol: {"expR": float, "win_rate": float}}

    返回: dict {symbol: weight}
    """
    weights = {}
    for sym, metrics in symbol_metrics.items():
        expR = metrics.get("expR", 0)
        win_rate = metrics.get("win_rate", 0.5)
        lose_rate = 1.0 - win_rate

        # 估算盈亏比: expR = win_rate * avg_win - lose_rate * avg_loss
        # 设 avg_loss = 1R (止损), 则 avg_win = (expR + lose_rate) / win_rate
        if win_rate > 0 and lose_rate > 0:
            avg_win_ratio = (expR + lose_rate) / win_rate  # 盈亏比
            if avg_win_ratio > 0:
                kelly_f = (win_rate * avg_win_ratio - lose_rate) / avg_win_ratio
                # 取 max(0, kelly_f)，负期望不配
                weights[sym] = max(0.0, kelly_f)
            else:
                weights[sym] = 0.0
        else:
            weights[sym] = 0.0

    if sum(weights.values()) <= 0:
        return equal_weight(list(symbol_metrics.keys()))

    return _normalize_weights(weights)


def sharpe_ratio_weight(symbol_metrics):
    """夏普比例配置（expR / 波动率）。

    权重与夏普比例成正比，负夏普不配。

    参数:
        symbol_metrics: dict {symbol: {"expR": float, "volatility": float}}

    返回: dict {symbol: weight}
    """
    weights = {}
    for sym, metrics in symbol_metrics.items():
        expR = metrics.get("expR", 0)
        vol = metrics.get("volatility", 1.0)
        if vol > 0 and expR > 0:
            # 用 expR / 波动率 作为夏普代理
            sharpe_approx = expR / vol
            weights[sym] = max(0.0, sharpe_approx)
        else:
            weights[sym] = 0.0

    if sum(weights.values()) <= 0:
        return equal_weight(list(symbol_metrics.keys()))

    return _normalize_weights(weights)


def min_variance_weight(symbols, lookback_days=252):
    """最小方差配置（基于协方差矩阵）。

    使用闭式解求解最小方差组合:
    w_min = (Sigma^-1 * 1) / (1^T * Sigma^-1 * 1)

    参数:
        symbols: list of symbol codes
        lookback_days: 计算协方差的回溯天数

    返回: dict {symbol: weight}
    """
    close_dict = {}
    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < 30:
            continue
        close = df["close"].copy()
        if lookback_days and len(close) > lookback_days:
            close = close.tail(lookback_days)
        close_dict[sym] = close

    if not close_dict:
        return equal_weight(symbols)

    close_df = pd.DataFrame(close_dict).dropna()
    returns = close_df.pct_change().dropna()

    if returns.empty or len(returns.columns) < 2:
        return equal_weight(list(close_dict.keys()))

    # 协方差矩阵
    cov = returns.cov().values
    n = cov.shape[0]

    try:
        # 正则化：添加微小对角线项确保可逆
        cov_reg = cov + np.eye(n) * 1e-10
        inv_cov = np.linalg.inv(cov_reg)
        ones = np.ones(n)
        w = inv_cov @ ones
        w = w / (ones @ w)

        # 确保非负权重（若有负权重则裁剪后重归一化）
        w = np.maximum(w, 0)
        if w.sum() > 0:
            w = w / w.sum()
        else:
            w = np.ones(n) / n

        result = {}
        for i, sym in enumerate(returns.columns):
            result[sym] = float(w[i])
        return result
    except np.linalg.LinAlgError:
        return equal_weight(list(close_dict.keys()))


# ============================================================================
# 3. 组合回测框架
# ============================================================================


def get_symbol_backtest(sym, cfg=DEFAULT_CONFIG):
    """获取单品种回测结果，返回标准化字典。

    返回:
        dict: {symbol, expR, win_rate, trades_detail, trades_count, volatility}
    """
    result = walk_forward_backtest(sym, cfg)
    if result.get("trades", 0) == 0:
        return None

    # 计算价格波动率
    df = load_daily(sym)
    volatility = 0.0
    if df is not None and len(df) > 30:
        returns = df["close"].pct_change().dropna()
        volatility = float(returns.std()) * np.sqrt(TRADING_DAYS_PER_YEAR)

    return {
        "symbol": sym,
        "expR": float(result.get("expR", 0)),
        "win_rate": float(result.get("win_rate", 0)),
        "trades_detail": result.get("trades_detail", []),
        "trades_count": result.get("trades", 0),
        "volatility": volatility,
    }


def build_daily_trade_returns(trades_detail, symbol, df_daily):
    """构建单品种每日交易收益序列。

    将每笔交易的 R_adj 分摊到持仓期间，或标记在退出日。
    简化处理：假设整笔收益在退出日实现，入场日记 0。

    参数:
        trades_detail: list of trade dicts
        symbol: 品种代码
        df_daily: 日线 DataFrame（用于日期索引对齐）

    返回:
        pd.Series: 每日收益（单位 R），索引为日期
    """
    dates = df_daily.index
    daily_r = pd.Series(0.0, index=dates)

    for trade in trades_detail:
        entry_date = trade.get("entry_date")
        r_adj = trade.get("R_adj", 0)
        direction = int(trade.get("dir", 0))

        if entry_date is None or entry_date not in dates:
            continue

        entry_idx = dates.get_loc(entry_date)

        # 估算退出日期：基于 R_adj 与方向推算
        # 正 R_adj 表示盈利（达到止盈），负表示止损
        # 简化：假设每笔交易持仓 N 天，N 由典型持仓期决定
        # 更精确的实现需要 trades_detail 包含 exit_date
        hold_bars = _estimate_hold_bars(trade, df_daily, entry_idx)
        exit_idx = min(entry_idx + hold_bars, len(dates) - 1)

        if exit_idx < len(dates):
            # 收益在退出日确认
            daily_r.iloc[exit_idx] += r_adj

    return daily_r


def _estimate_hold_bars(trade, df_daily, entry_idx):
    """估算持仓 bar 数。

    基于交易的 R_adj 和方向估算：
    - 止损（R_adj < -0.8）：通常 1-2 根
    - 止盈（R_adj > 1.5）：通常 3-8 根
    - 其他：默认 5 根

    实际项目中应从 trades_detail 的 exit_date 字段直接读取。
    """
    r_adj = trade.get("R_adj", 0)
    reason = trade.get("reason", "")

    if "止损" in reason or r_adj < -0.7:
        return 1  # 止损通常较快
    elif "止盈" in reason or r_adj > 1.5:
        return 5  # 止盈通常需要时间
    elif "尾仓" in reason:
        return 8  # 尾仓持有更久
    else:
        return 3  # 默认


def portfolio_backtest(weights, symbol_results, cfg=DEFAULT_CONFIG):
    """组合回测核心函数。

    将各品种的交易按时间合并，按权重加权计算组合每日收益。
    R_adj 转换为实际权益收益率：每笔交易收益 = 权重 × R_adj × 每笔风险比例。

    参数:
        weights: dict {symbol: weight}（权重之和为 1，表示风险预算分配）
        symbol_results: dict {symbol: backtest_result_dict}
            backtest_result_dict 含 trades_detail, expR, win_rate, volatility
        cfg: 配置（用于获取账户风险参数）

    返回:
        dict: 组合绩效指标
            - total_return: 总收益率
            - annual_return: 年化收益率
            - annual_volatility: 年化波动率
            - max_drawdown: 最大回撤
            - sharpe_ratio: 夏普比率
            - calmar_ratio: 卡玛比率
            - total_trades: 总交易笔数
            - win_rate: 组合层面胜率
            - daily_returns: pd.Series 日收益序列
            - equity_curve: pd.Series 权益曲线
    """
    # 每笔交易的风险比例（占总权益的百分比）
    risk_per_trade = cfg.get("account", {}).get("risk_pct", 1.5) / 100.0

    # 收集所有品种的日收益序列
    daily_returns_dict = {}
    all_dates = set()

    for sym, weight in weights.items():
        if sym not in symbol_results or weight <= 0:
            continue
        result = symbol_results[sym]
        trades = result.get("trades_detail", [])
        if not trades:
            continue

        df = load_daily(sym)
        if df is None:
            continue

        daily_r = build_daily_trade_returns(trades, sym, df)
        # 按权重 × 风险比例缩放，转换为实际权益收益率
        daily_returns_dict[sym] = daily_r * weight * risk_per_trade
        all_dates.update(df.index)

    if not daily_returns_dict:
        return _empty_portfolio_result()

    # 对齐到共同日期索引
    all_dates_sorted = sorted(all_dates)
    portfolio_daily = pd.Series(0.0, index=all_dates_sorted)

    for sym, daily_r in daily_returns_dict.items():
        # 重新索引并填充 0
        aligned = daily_r.reindex(all_dates_sorted, fill_value=0.0)
        portfolio_daily += aligned

    # 去掉全零的起始段（没交易的日子）
    first_nonzero = (portfolio_daily != 0).idxmax()
    if portfolio_daily.loc[first_nonzero] == 0:
        return _empty_portfolio_result()
    portfolio_daily = portfolio_daily.loc[first_nonzero:]

    # 计算权益曲线（累计 R 收益）
    equity_curve = (1 + portfolio_daily).cumprod()

    # 计算绩效指标
    total_return = float(equity_curve.iloc[-1] - 1.0)

    n_days = len(portfolio_daily)
    n_years = max(n_days / TRADING_DAYS_PER_YEAR, 1e-6)
    annual_return = float((1 + total_return) ** (1 / n_years) - 1.0) if total_return > -1 else -1.0

    daily_vol = float(portfolio_daily.std())
    annual_volatility = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)

    # 最大回撤
    peak = equity_curve.expanding().max()
    drawdown = (equity_curve - peak) / peak
    max_drawdown = float(abs(drawdown.min()))

    # 夏普比率（假设无风险利率为 0）
    sharpe_ratio = annual_return / annual_volatility if annual_volatility > 0 else 0.0

    # 卡玛比率
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0.0

    # 总交易笔数和胜率
    total_trades = sum(
        symbol_results[sym].get("trades_count", 0) for sym in weights if sym in symbol_results and weights[sym] > 0
    )
    # 组合层面胜率：盈利天数 / 有收益天数
    nonzero_days = portfolio_daily[portfolio_daily != 0]
    win_rate = float((nonzero_days > 0).mean()) if len(nonzero_days) > 0 else 0.0

    return {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "annual_volatility": round(annual_volatility, 4),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "calmar_ratio": round(calmar_ratio, 4),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "n_days": n_days,
        "daily_returns": portfolio_daily,
        "equity_curve": equity_curve,
    }


def _empty_portfolio_result():
    """返回空组合结果（零交易）。"""
    return {
        "total_return": 0.0,
        "annual_return": 0.0,
        "annual_volatility": 0.0,
        "max_drawdown": 0.0,
        "sharpe_ratio": 0.0,
        "calmar_ratio": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "n_days": 0,
        "daily_returns": pd.Series(dtype=float),
        "equity_curve": pd.Series(dtype=float),
    }


def walk_forward_portfolio_allocation(
    symbols,
    allocation_fn,
    cfg=DEFAULT_CONFIG,
    lookback_days=252,
    rebalance_bars=60,
    min_bars=60,
):
    """Walk-Forward 验证配置稳健性。

    思路：
    1. 将数据按 rebalance_bars 分割为多个窗口
    2. 每个窗口用前 lookback_days 数据计算权重
    3. 在下一个 rebalance_bars 窗口使用该权重
    4. 拼接所有窗口的收益得到完整 WF 收益序列

    参数:
        symbols: 品种列表
        allocation_fn: 分配函数名 ("equal", "vol_inverse", "kelly", "sharpe", "min_var")
        cfg: 配置
        lookback_days: 用于计算权重的回溯期
        rebalance_bars: 再平衡周期（交易日）
        min_bars: 最少数据量

    返回:
        dict: WF 组合绩效指标（结构同 portfolio_backtest）
    """
    # 获取所有品种的日线数据和回测结果
    daily_data = {}
    bt_results = {}
    for sym in symbols:
        df = load_daily(sym)
        if df is None or len(df) < min_bars:
            continue
        daily_data[sym] = df

    valid_symbols = list(daily_data.keys())
    if len(valid_symbols) < 2:
        return _empty_portfolio_result()

    # 找共同日期范围
    common_dates = None
    for sym in valid_symbols:
        dates = set(daily_data[sym].index)
        common_dates = dates if common_dates is None else common_dates & dates
    common_dates = sorted(common_dates)

    if len(common_dates) < lookback_days + rebalance_bars:
        return _empty_portfolio_result()

    # 为每个品种预计算每日 R 收益序列（用全量回测）
    daily_r_dict = {}
    for sym in valid_symbols:
        result = get_symbol_backtest(sym, cfg)
        if result is None:
            continue
        trades = result.get("trades_detail", [])
        daily_r = build_daily_trade_returns(trades, sym, daily_data[sym])
        daily_r_dict[sym] = daily_r
        bt_results[sym] = result

    # 每笔交易的风险比例
    risk_per_trade = cfg.get("account", {}).get("risk_pct", 1.5) / 100.0

    # Walk-Forward 循环
    all_wf_returns = []
    start_idx = lookback_days

    while start_idx + rebalance_bars <= len(common_dates):
        # 训练窗口
        train_end_idx = start_idx
        train_start_idx = max(0, train_end_idx - lookback_days)
        train_dates = common_dates[train_start_idx:train_end_idx]

        # 测试窗口
        test_start_idx = start_idx
        test_end_idx = min(start_idx + rebalance_bars, len(common_dates))
        test_dates = common_dates[test_start_idx:test_end_idx]

        # 计算当前窗口的权重
        weights = _compute_allocation_for_window(allocation_fn, valid_symbols, daily_data, bt_results, train_dates)

        # 计算测试窗口的组合收益（R 单位 → 实际收益率）
        window_return = pd.Series(0.0, index=test_dates)
        for sym, w in weights.items():
            if sym in daily_r_dict and w > 0:
                sym_r = daily_r_dict[sym].reindex(test_dates, fill_value=0.0)
                window_return += sym_r * w * risk_per_trade

        all_wf_returns.append(window_return)
        start_idx += rebalance_bars

    if not all_wf_returns:
        return _empty_portfolio_result()

    # 拼接所有窗口
    wf_daily = pd.concat(all_wf_returns).sort_index()
    wf_daily = wf_daily[wf_daily != 0]  # 只保留有交易的天数
    if wf_daily.empty:
        return _empty_portfolio_result()

    # 计算绩效
    equity = (1 + wf_daily).cumprod()
    total_ret = float(equity.iloc[-1] - 1.0)
    n_days = len(wf_daily)
    n_years = max(n_days / TRADING_DAYS_PER_YEAR, 1e-6)
    ann_ret = float((1 + total_ret) ** (1 / n_years) - 1.0) if total_ret > -1 else -1.0
    ann_vol = float(wf_daily.std() * np.sqrt(TRADING_DAYS_PER_YEAR))

    peak = equity.expanding().max()
    dd = (equity - peak) / peak
    max_dd = float(abs(dd.min()))

    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    calmar = ann_ret / max_dd if max_dd > 0 else 0.0

    nonzero = wf_daily[wf_daily != 0]
    wr = float((nonzero > 0).mean()) if len(nonzero) > 0 else 0.0

    total_trades_count = sum(bt_results[sym].get("trades_count", 0) for sym in valid_symbols if sym in bt_results)

    return {
        "total_return": round(total_ret, 4),
        "annual_return": round(ann_ret, 4),
        "annual_volatility": round(ann_vol, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "total_trades": total_trades_count,
        "win_rate": round(wr, 4),
        "n_days": n_days,
        "daily_returns": wf_daily,
        "equity_curve": equity,
    }


def _compute_allocation_for_window(allocation_fn, symbols, daily_data, bt_results, train_dates):
    """在指定时间窗口内计算分配权重。"""
    train_start, train_end = train_dates[0], train_dates[-1]

    if allocation_fn == "equal":
        return equal_weight(symbols)

    elif allocation_fn == "vol_inverse":
        vol_dict = {}
        for sym in symbols:
            df = daily_data[sym]
            seg = df.loc[train_start:train_end, "close"]
            if len(seg) > 10:
                ret = seg.pct_change().dropna()
                vol = float(ret.std())
                if vol > 0:
                    vol_dict[sym] = 1.0 / vol
        if not vol_dict:
            return equal_weight(symbols)
        return _normalize_weights(vol_dict)

    elif allocation_fn == "kelly":
        # 简化：用全样本 expR/win_rate（WF 下应只用窗口内数据，
        # 但 trades_detail 是全量的，窗口内统计需要额外处理）
        metrics = {}
        for sym in symbols:
            if sym in bt_results:
                r = bt_results[sym]
                metrics[sym] = {
                    "expR": r.get("expR", 0),
                    "win_rate": r.get("win_rate", 0.5),
                }
        if not metrics:
            return equal_weight(symbols)
        return kelly_weight(metrics)

    elif allocation_fn == "sharpe":
        metrics = {}
        for sym in symbols:
            if sym in bt_results:
                r = bt_results[sym]
                df = daily_data[sym]
                seg = df.loc[train_start:train_end, "close"]
                vol = 0.0
                if len(seg) > 10:
                    ret = seg.pct_change().dropna()
                    vol = float(ret.std()) * np.sqrt(TRADING_DAYS_PER_YEAR)
                metrics[sym] = {
                    "expR": r.get("expR", 0),
                    "volatility": vol if vol > 0 else 1.0,
                }
        if not metrics:
            return equal_weight(symbols)
        return sharpe_ratio_weight(metrics)

    elif allocation_fn == "min_var":
        close_dict = {}
        for sym in symbols:
            df = daily_data[sym]
            seg = df.loc[train_start:train_end, "close"]
            if len(seg) > 10:
                close_dict[sym] = seg
        if len(close_dict) < 2:
            return equal_weight(list(close_dict.keys()) if close_dict else symbols)
        close_df = pd.DataFrame(close_dict).dropna()
        returns = close_df.pct_change().dropna()
        if returns.empty or len(returns.columns) < 2:
            return equal_weight(list(close_dict.keys()))
        cov = returns.cov().values
        n = cov.shape[0]
        try:
            cov_reg = cov + np.eye(n) * 1e-10
            inv_cov = np.linalg.inv(cov_reg)
            ones = np.ones(n)
            w = inv_cov @ ones
            w = w / (ones @ w)
            w = np.maximum(w, 0)
            if w.sum() > 0:
                w = w / w.sum()
            else:
                w = np.ones(n) / n
            result = {}
            for i, sym in enumerate(returns.columns):
                result[sym] = float(w[i])
            return result
        except np.linalg.LinAlgError:
            return equal_weight(list(close_dict.keys()))

    else:
        return equal_weight(symbols)


# ============================================================================
# 4. 输出报告
# ============================================================================


def print_text_heatmap(corr_matrix, title="相关性矩阵", width=8):
    """文字版相关性热力图。

    使用字符密度表示相关性强度：
    ' '  无相关 (|r| < 0.1)
    '·'  极低 (0.1 <= |r| < 0.2)
    '░'  低 (0.2 <= |r| < 0.4)
    '▒'  中 (0.4 <= |r| < 0.6)
    '▓'  高 (0.6 <= |r| < 0.8)
    '█'  极高 (|r| >= 0.8)
    正相关用绿色字符概念（用 + 号标记），负相关用红色概念（用 - 号标记）
    这里用字符密度 + 正负号前缀表示。
    """
    if corr_matrix.empty:
        print("  [无数据]")
        return

    syms = corr_matrix.columns.tolist()
    n = len(syms)

    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")

    # 表头
    header = "        "
    for sym in syms:
        header += f"{sym:>7}"
    print(header)
    print("        " + "-" * (7 * n))

    for i, sym_row in enumerate(syms):
        row_str = f"{sym_row:>7} "
        for j, sym_col in enumerate(syms):
            val = corr_matrix.iloc[i, j]
            if pd.isna(val):
                row_str += "    NA "
                continue
            char = _corr_char(val)
            val_str = f"{val:+.3f}"
            row_str += f"{char}{val_str}"
        print(row_str)

    # 图例
    print("\n  图例: ░低(0.2) ▒中(0.4) ▓高(0.6) █极高(0.8)")
    print("       +正相关 / -负相关")


def _corr_char(val):
    """根据相关系数返回表示字符。"""
    av = abs(val)
    sign = "+" if val >= 0 else "-"
    if av < 0.1:
        return " "
    elif av < 0.2:
        return sign
    elif av < 0.4:
        return "░" + sign
    elif av < 0.6:
        return "▒" + sign
    elif av < 0.8:
        return "▓" + sign
    else:
        return "█" + sign


def print_sector_correlation(corr_matrix, symbols, title="按板块相关性"):
    """按板块分组展示相关性。"""
    groups = group_by_sector(symbols)

    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")

    for group, syms in sorted(groups.items()):
        # 过滤出在相关矩阵中的品种
        valid_syms = [s for s in syms if s in corr_matrix.columns]
        if len(valid_syms) < 1:
            continue

        avg_corr = 0.0
        count = 0
        if len(valid_syms) >= 2:
            for i in range(len(valid_syms)):
                for j in range(i + 1, len(valid_syms)):
                    v = corr_matrix.loc[valid_syms[i], valid_syms[j]]
                    if not pd.isna(v):
                        avg_corr += abs(v)
                        count += 1
            avg_corr = avg_corr / count if count > 0 else 0

        print(f"\n  ▶ {group} ({len(valid_syms)}个品种, 平均|r|={avg_corr:.3f})")
        for s in valid_syms:
            name = SYMBOLS.get(s, {}).get("name", s)
            print(f"    - {s} ({name})")


def print_allocation_comparison(allocation_results):
    """各配置方法的组合绩效对比表。

    参数:
        allocation_results: dict {method_name: {metrics...}}
    """
    print(f"\n{'=' * 80}")
    print("  各配置方法组合绩效对比")
    print(f"{'=' * 80}")

    header = (
        f"{'配置方法':<14} {'总收益':>8} {'年化收益':>8} {'年化波动':>8} "
        f"{'最大回撤':>8} {'夏普比':>8} {'卡玛比':>8} {'交易数':>8} {'胜率':>8}"
    )
    print(header)
    print("-" * 80)

    for method, metrics in sorted(allocation_results.items()):
        print(
            f"{method:<14} "
            f"{metrics['total_return']:>8.2%} "
            f"{metrics['annual_return']:>8.2%} "
            f"{metrics['annual_volatility']:>8.2%} "
            f"{metrics['max_drawdown']:>8.2%} "
            f"{metrics['sharpe_ratio']:>8.2f} "
            f"{metrics['calmar_ratio']:>8.2f} "
            f"{metrics['total_trades']:>8d} "
            f"{metrics['win_rate']:>8.1%}"
        )


def print_weight_table(weights_dict, title="权重分配"):
    """打印权重分配表。"""
    print(f"\n  {title}:")
    print(f"  {'-' * 50}")
    print(f"  {'品种':<8} {'名称':<10} {'板块':<8} {'权重':>8}")
    print(f"  {'-' * 50}")

    for sym, w in sorted(weights_dict.items(), key=lambda x: x[1], reverse=True):
        info = SYMBOLS.get(sym, {})
        name = info.get("name", "")
        group = info.get("group", "")
        print(f"  {sym:<8} {name:<10} {group:<8} {w:>7.2%}")

    print(f"  {'-' * 50}")
    print(f"  {'合计':<8} {'':<10} {'':<8} {sum(weights_dict.values()):>7.2%}")


def recommend_allocation(allocation_results, weights_dict_all):
    """推荐配置方案。

    综合考虑夏普比率、卡玛比率、最大回撤等因素，给出推荐。

    返回: dict {method, reason, details}
    """
    # 打分系统
    scores = {}
    for method, metrics in allocation_results.items():
        # 夏普比率得分 (0-40)
        sharpe_score = min(metrics["sharpe_ratio"] / 3.0, 1.0) * 40
        # 卡玛比率得分 (0-30)
        calmar_score = min(metrics["calmar_ratio"] / 5.0, 1.0) * 30
        # 最大回撤得分 (0-20，回撤越小分越高)
        dd_score = max(0, 1.0 - metrics["max_drawdown"] / 0.5) * 20
        # 稳定性得分 (0-10，交易数越多越稳定)
        trades_score = min(metrics["total_trades"] / 200, 1.0) * 10

        total = sharpe_score + calmar_score + dd_score + trades_score
        scores[method] = float(total)

    best_method = max(scores, key=scores.get)

    reasons = []
    best_metrics = allocation_results[best_method]
    reasons.append(f"综合得分最高 ({scores[best_method]:.1f}/100)")
    reasons.append(f"夏普比率 {best_metrics['sharpe_ratio']:.2f}")
    reasons.append(f"卡玛比率 {best_metrics['calmar_ratio']:.2f}")
    reasons.append(f"最大回撤 {best_metrics['max_drawdown']:.1%}")

    return {
        "method": best_method,
        "score": round(float(scores[best_method]), 1),
        "reasons": reasons,
        "weights": weights_dict_all.get(best_method, {}),
        "all_scores": {k: round(float(v), 1) for k, v in scores.items()},
    }


# ============================================================================
# 主函数
# ============================================================================


def run_allocation_methods(symbols, symbol_results, daily_data, cfg=DEFAULT_CONFIG):
    """运行所有分配方法并返回结果。

    返回:
        dict {method_name: portfolio_metrics}
        dict {method_name: weights_dict}
    """
    methods = {}
    weights_all = {}

    # 1. 等权配置
    w_eq = equal_weight(symbols)
    weights_all["等权配置"] = w_eq
    methods["等权配置"] = portfolio_backtest(w_eq, symbol_results, cfg)

    # 2. 波动率倒数
    w_vol = volatility_inverse_weight(symbols)
    weights_all["波动率倒数"] = w_vol
    methods["波动率倒数"] = portfolio_backtest(w_vol, symbol_results, cfg)

    # 3. 凯利配置
    kelly_metrics = {}
    for sym in symbols:
        if sym in symbol_results:
            kelly_metrics[sym] = {
                "expR": symbol_results[sym]["expR"],
                "win_rate": symbol_results[sym]["win_rate"],
            }
    w_kelly = kelly_weight(kelly_metrics)
    weights_all["凯利配置"] = w_kelly
    methods["凯利配置"] = portfolio_backtest(w_kelly, symbol_results, cfg)

    # 4. 夏普比例
    sharpe_metrics = {}
    for sym in symbols:
        if sym in symbol_results:
            sharpe_metrics[sym] = {
                "expR": symbol_results[sym]["expR"],
                "volatility": symbol_results[sym]["volatility"],
            }
    w_sharpe = sharpe_ratio_weight(sharpe_metrics)
    weights_all["夏普比例"] = w_sharpe
    methods["夏普比例"] = portfolio_backtest(w_sharpe, symbol_results, cfg)

    # 5. 最小方差
    w_mv = min_variance_weight(symbols)
    weights_all["最小方差"] = w_mv
    methods["最小方差"] = portfolio_backtest(w_mv, symbol_results, cfg)

    return methods, weights_all


def main():
    """组合分析主函数。

    执行流程：
    1. 获取白名单品种（expR > 0）
    2. 相关性分析（价格收益 + 交易信号）
    3. 运行多种资金分配方法
    4. 组合回测与绩效对比
    5. Walk-Forward 验证（等权 + 最优配置）
    6. 输出报告与推荐配置
    """
    print("=" * 80)
    print("  四维策略 · 组合层面分析报告")
    print("=" * 80)
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cfg = DEFAULT_CONFIG.copy()

    # ------------------------------------------------------------------
    # Step 1: 获取白名单品种
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 1: 白名单品种筛选 (expR > 0)")
    print(f"{'=' * 80}")

    whitelist = get_whitelist_symbols(cfg)
    print(f"\n  共筛选出 {len(whitelist)} 个正期望品种:")
    for sym in whitelist:
        info = SYMBOLS.get(sym, {})
        print(f"    - {sym} ({info.get('name', '')}) [{info.get('group', '')}]")

    if len(whitelist) < 2:
        print("\n  [警告] 正期望品种不足 2 个，无法进行组合分析。")
        return

    # ------------------------------------------------------------------
    # Step 2: 相关性分析
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 2: 品种相关性分析")
    print(f"{'=' * 80}")

    # 价格收益相关性
    print("\n  --- 2.1 价格收益相关性 (日收益率) ---")
    price_corr = compute_price_return_correlation(whitelist)
    print_text_heatmap(price_corr, title="价格收益相关矩阵")

    # 高/低相关对
    high_pairs = find_high_corr_pairs(price_corr, threshold=0.6)
    low_pairs = find_low_corr_pairs(price_corr, threshold=0.2)

    if high_pairs:
        print(f"\n  高相关对 (|r| > 0.6) [{len(high_pairs)}对]:")
        for s1, s2, r in high_pairs[:10]:
            n1 = SYMBOLS.get(s1, {}).get("name", s1)
            n2 = SYMBOLS.get(s2, {}).get("name", s2)
            print(f"    {s1}/{s2} ({n1}/{n2}): r = {r:+.3f}")

    if low_pairs:
        print(f"\n  低相关对 (|r| < 0.2) [{len(low_pairs)}对]:")
        for s1, s2, r in low_pairs[:10]:
            n1 = SYMBOLS.get(s1, {}).get("name", s1)
            n2 = SYMBOLS.get(s2, {}).get("name", s2)
            print(f"    {s1}/{s2} ({n1}/{n2}): r = {r:+.3f}")

    # 按板块分组
    print_sector_correlation(price_corr, whitelist, title="价格相关性 · 按板块")

    # 交易信号相关性
    print("\n  --- 2.2 交易信号相关性 (每日方向) ---")
    signal_corr = compute_signal_correlation(whitelist, cfg)
    if not signal_corr.empty:
        print_text_heatmap(signal_corr, title="交易信号相关矩阵")

        sig_high = find_high_corr_pairs(signal_corr, threshold=0.6)
        if sig_high:
            print(f"\n  信号高相关对 (|r| > 0.6) [{len(sig_high)}对]:")
            for s1, s2, r in sig_high[:10]:
                print(f"    {s1}/{s2}: r = {r:+.3f}")
    else:
        print("  [信号相关性数据不足]")

    # ------------------------------------------------------------------
    # Step 3: 单品种回测结果汇总
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 3: 单品种回测结果汇总")
    print(f"{'=' * 80}")

    symbol_results = {}
    daily_data = {}
    for sym in whitelist:
        result = get_symbol_backtest(sym, cfg)
        if result is not None:
            symbol_results[sym] = result
            df = load_daily(sym)
            if df is not None:
                daily_data[sym] = df

    print(f"\n  {'品种':<8} {'名称':<10} {'expR':>8} {'胜率':>8} {'交易数':>8} {'年化波动':>10}")
    print(f"  {'-' * 60}")
    for sym in sorted(symbol_results.keys()):
        r = symbol_results[sym]
        info = SYMBOLS.get(sym, {})
        print(
            f"  {sym:<8} {info.get('name', ''):<10} "
            f"{r['expR']:>+8.3f} {r['win_rate']:>7.1%} "
            f"{r['trades_count']:>8d} {r['volatility']:>9.2%}"
        )

    # ------------------------------------------------------------------
    # Step 4: 资金分配 & 组合回测
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 4: 资金分配方法对比")
    print(f"{'=' * 80}")

    allocation_results, weights_all = run_allocation_methods(
        list(symbol_results.keys()), symbol_results, daily_data, cfg
    )
    print_allocation_comparison(allocation_results)

    # 打印各方法权重
    for method, weights in weights_all.items():
        print_weight_table(weights, title=f"{method}权重")

    # ------------------------------------------------------------------
    # Step 5: Walk-Forward 验证
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 5: Walk-Forward 稳健性验证")
    print(f"{'=' * 80}")
    print("  (回溯期 252 日, 再平衡周期 60 日)")

    wf_results = {}
    wf_methods = ["equal", "vol_inverse", "min_var"]
    method_names = {"equal": "等权", "vol_inverse": "波动率倒数", "min_var": "最小方差"}

    for method in wf_methods:
        try:
            wf_res = walk_forward_portfolio_allocation(
                list(symbol_results.keys()),
                method,
                cfg,
                lookback_days=252,
                rebalance_bars=60,
            )
            wf_results[method_names[method]] = wf_res
        except Exception as e:
            print(f"  {method_names[method]} WF 验证失败: {e}")

    if wf_results:
        print("\n  Walk-Forward 绩效对比:")
        header = f"  {'配置方法':<12} {'总收益':>8} {'年化收益':>8} {'最大回撤':>8} {'夏普比':>8} {'卡玛比':>8}"
        print(header)
        print(f"  {'-' * 60}")
        for method, metrics in wf_results.items():
            print(
                f"  {method:<12} "
                f"{metrics['total_return']:>8.2%} "
                f"{metrics['annual_return']:>8.2%} "
                f"{metrics['max_drawdown']:>8.2%} "
                f"{metrics['sharpe_ratio']:>8.2f} "
                f"{metrics['calmar_ratio']:>8.2f}"
            )

    # ------------------------------------------------------------------
    # Step 6: 推荐配置
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Step 6: 推荐配置方案")
    print(f"{'=' * 80}")

    recommendation = recommend_allocation(allocation_results, weights_all)
    print(f"\n  ▶ 推荐方法: {recommendation['method']}")
    print(f"  ▶ 综合得分: {recommendation['score']}/100")
    print("  ▶ 推荐理由:")
    for reason in recommendation["reasons"]:
        print(f"    - {reason}")

    if recommendation["weights"]:
        print_weight_table(recommendation["weights"], title="推荐权重")

    print("\n  各方法得分明细:")
    for method, score in sorted(recommendation["all_scores"].items(), key=lambda x: x[1], reverse=True):
        bar = "█" * int(score / 2) + "░" * (50 - int(score / 2))
        print(f"    {method:<10} [{bar}] {score:>5.1f}")

    # ------------------------------------------------------------------
    # 总结
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  报告结束")
    print(f"{'=' * 80}")

    return {
        "whitelist": whitelist,
        "price_correlation": price_corr,
        "signal_correlation": signal_corr,
        "symbol_results": symbol_results,
        "allocation_results": allocation_results,
        "weights": weights_all,
        "wf_results": wf_results,
        "recommendation": recommendation,
    }


if __name__ == "__main__":
    main()
