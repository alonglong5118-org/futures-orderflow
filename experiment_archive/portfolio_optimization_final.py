"""
组合配置最终优化器：带约束的资金分配 + 严谨 WF 验证 + 实盘配置输出

改进点：
  1. 过滤低样本品种（交易笔数 < 30）
  2. 单品种权重上限约束（max_single_weight）
  3. 板块权重上限约束（max_sector_weight）
  4. 完整 Walk-Forward 验证（含凯利/夏普/等权/波动率倒数/最小方差）
  5. 输出 trade_config 兼容的权重配置 JSON
"""

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest


def symbols_name(sym):
    return SYMBOLS.get(sym, {}).get("name", sym)


def symbols_group(sym):
    return SYMBOLS.get(sym, {}).get("group", "其他")


# ── 工具函数 ────────────────────────────────────────────────────────────────
def compute_daily_returns(sym, df):
    """计算日收益率序列。"""
    closes = df["close"].values.astype(float)
    returns = np.diff(closes) / closes[:-1]
    return returns


def compute_trade_daily_series(trades_detail, n_days):
    """将 trades_detail 转换为每日收益序列。

    返回: daily_pnl (n_days,) - 每日盈亏（单位：R）
    """
    daily_pnl = np.zeros(n_days)
    if not trades_detail:
        return daily_pnl

    for t in trades_detail:
        entry_bar = int(t.get("entry_bar", 0))
        exit_bar = int(t.get("exit_bar", entry_bar + 5))
        r_adj = t.get("R_adj", 0)
        exit_bar = min(exit_bar, n_days - 1)
        if exit_bar >= 0 and exit_bar < n_days:
            daily_pnl[exit_bar] += r_adj

    return daily_pnl


# ── 约束优化 ────────────────────────────────────────────────────────────────
def apply_weight_constraints(raw_weights, sym_list, max_single_weight=0.10, max_sector_weight=0.30, min_weight=0.005):
    """应用权重约束：单品种上限、板块上限、最低权重过滤。

    通过迭代截断+归一化实现。
    """
    weights = dict(zip(sym_list, raw_weights))
    sectors = {sym: symbols_group(sym) for sym in sym_list}

    for _ in range(10):  # 最多迭代10次
        changed = False

        # 1. 过滤低于 min_weight 的品种
        for sym in list(weights.keys()):
            if weights[sym] < min_weight and weights[sym] > 0:
                weights[sym] = 0
                changed = True

        # 2. 单品种上限
        for sym, w in weights.items():
            if w > max_single_weight:
                weights[sym] = max_single_weight
                changed = True

        # 3. 板块上限
        sector_weights = {}
        for sym, w in weights.items():
            sec = sectors[sym]
            sector_weights[sec] = sector_weights.get(sec, 0) + w

        for sec, sec_w in sector_weights.items():
            if sec_w > max_sector_weight:
                scale = max_sector_weight / sec_w
                for sym in weights:
                    if sectors[sym] == sec:
                        weights[sym] *= scale
                changed = True

        # 4. 归一化
        total = sum(weights.values())
        if total > 0:
            for sym in weights:
                weights[sym] /= total

        if not changed:
            break

    return np.array([weights[sym] for sym in sym_list])


# ── 分配方法 ────────────────────────────────────────────────────────────────
def equal_weight(sym_list):
    """等权配置。"""
    n = len(sym_list)
    return np.ones(n) / n


def volatility_inverse_weight(sym_list, volatilities):
    """波动率倒数配置。"""
    inv_vol = 1.0 / np.maximum(volatilities, 1e-6)
    return inv_vol / inv_vol.sum()


def kelly_weight(sym_list, expRs, win_rates, max_kelly=1.0):
    """凯利配置：f* = (p*b - q) / b，其中 b = 盈亏比。

    简化：expR = p*b - (1-p) → b = (expR + 1 - p) / p
    则 f* = (expR) / b = expR * p / (expR + 1 - p)
    """
    weights = np.zeros(len(sym_list))
    for i, (expr, wr) in enumerate(zip(expRs, win_rates)):
        if expr <= 0 or wr <= 0 or wr >= 1:
            weights[i] = 0
            continue
        # 盈亏比 b
        b = (expr + 1 - wr) / wr
        if b <= 0:
            weights[i] = 0
            continue
        # 凯利分数
        f = (wr * b - (1 - wr)) / b
        weights[i] = max(0, min(f, max_kelly))

    if weights.sum() > 0:
        weights /= weights.sum()
    return weights


def sharpe_ratio_weight(sym_list, expRs, volatilities):
    """夏普比例配置：权重与 expR/波动率 成正比。"""
    sharpes = expRs / np.maximum(volatilities, 1e-6)
    sharpes = np.maximum(sharpes, 0)
    if sharpes.sum() > 0:
        return sharpes / sharpes.sum()
    return equal_weight(sym_list)


def min_variance_weight(sym_list, corr_matrix, volatilities):
    """最小方差配置（非负约束，迭代法）。"""
    n = len(sym_list)
    # 协方差矩阵
    cov = np.outer(volatilities, volatilities) * corr_matrix

    # 初始：等权
    w = np.ones(n) / n

    for _ in range(100):
        # 梯度
        grad = 2 * cov @ w
        # 找负梯度且权重>0的，或正梯度且权重=0的 → 更新
        min_grad_idx = np.argmin(grad)
        max_grad_idx = np.argmax(grad)

        if grad[min_grad_idx] >= grad[max_grad_idx] - 1e-10:
            break  # 已收敛

        # 从最大梯度转移到最小梯度
        transfer = min(w[max_grad_idx], 0.01)
        w[min_grad_idx] += transfer
        w[max_grad_idx] -= transfer

    return w


# ── 组合回测 ────────────────────────────────────────────────────────────────
def portfolio_backtest(sym_list, weights, all_trades, all_daily_pnl, risk_per_trade=0.015):
    """组合回测。

    all_trades: dict {symbol: trades_detail}
    all_daily_pnl: dict {symbol: daily_pnl_array}
    risk_per_trade: 单笔交易风险占净值比例（默认 1.5%）
    """
    n_days = max(len(v) for v in all_daily_pnl.values()) if all_daily_pnl else 0
    if n_days == 0:
        return {}

    # 组合日收益（按权重加权）
    port_daily = np.zeros(n_days)
    for i, sym in enumerate(sym_list):
        if sym in all_daily_pnl:
            pnl = all_daily_pnl[sym]
            w = weights[i]
            # 对齐长度
            if len(pnl) < n_days:
                padded = np.zeros(n_days)
                padded[: len(pnl)] = pnl
                pnl = padded
            port_daily += w * pnl * risk_per_trade

    # 权益曲线
    equity = np.cumprod(1 + port_daily)
    total_return = equity[-1] - 1

    # 年化（假设 252 个交易日）
    n_years = n_days / 252.0
    annual_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1
    annual_vol = np.std(port_daily) * np.sqrt(252)

    # 最大回撤
    running_max = np.maximum.accumulate(equity)
    drawdowns = (running_max - equity) / running_max
    max_dd = float(np.max(drawdowns))

    # 夏普 & 卡玛
    sharpe = annual_return / annual_vol if annual_vol > 0 else 0
    calmar = annual_return / max_dd if max_dd > 0 else 0

    # 总交易数 & 胜率
    total_trades = sum(len(all_trades.get(sym, [])) for sym in sym_list)
    all_Rs = []
    for sym in sym_list:
        for t in all_trades.get(sym, []):
            all_Rs.append(t.get("R_adj", 0))
    win_rate = sum(1 for r in all_Rs if r > 0) / len(all_Rs) if all_Rs else 0

    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "annual_vol": round(annual_vol * 100, 2),
        "max_dd": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 3),
        "equity": equity,
        "daily_returns": port_daily,
    }


# ── Walk-Forward 验证 ──────────────────────────────────────────────────────
def walk_forward_portfolio(
    sym_list,
    all_dfs,
    all_results,
    method="equal",
    lookback=252,
    rebalance=60,
    risk_per_trade=0.015,
    max_single_weight=0.10,
    max_sector_weight=0.30,
):
    """Walk-Forward 组合配置验证。"""
    # 确定共同日期范围
    min_len = min(len(df) for df in all_dfs.values()) if all_dfs else 0
    if min_len < lookback + rebalance * 2:
        return None

    n_days = min_len
    port_daily_full = np.zeros(n_days)
    weight_history = []

    # 滑动再平衡
    current_weights = None

    for rebal_day in range(lookback, n_days, rebalance):
        # 训练窗口：[rebal_day - lookback, rebal_day)
        train_start = rebal_day - lookback

        # 计算训练期内的指标
        expRs_train = []
        vols_train = []
        win_rates_train = []
        valid_syms = []

        for sym in sym_list:
            if sym not in all_results or sym not in all_dfs:
                continue
            df = all_dfs[sym]
            trades = all_results[sym].get("trades_detail", [])

            # 筛选训练期内的交易
            train_trades = [t for t in trades if train_start <= int(t.get("entry_bar", 0)) < rebal_day]

            if len(train_trades) < 5:
                continue

            Rs = [t["R_adj"] for t in train_trades]
            expr = float(np.mean(Rs))
            wr = sum(1 for r in Rs if r > 0) / len(Rs)

            # 训练期波动率
            ret_slice = compute_daily_returns(sym, df.iloc[train_start:rebal_day])
            vol = np.std(ret_slice) * np.sqrt(252) if len(ret_slice) > 10 else 0.2

            if expr <= 0:
                # 训练期负期望，不给权重
                continue

            valid_syms.append(sym)
            expRs_train.append(expr)
            vols_train.append(vol)
            win_rates_train.append(wr)

        if not valid_syms:
            current_weights = None
            continue

        expRs_arr = np.array(expRs_train)
        vols_arr = np.array(vols_train)
        wr_arr = np.array(win_rates_train)

        # 计算权重
        if method == "equal":
            raw_w = equal_weight(valid_syms)
        elif method == "vol_inv":
            raw_w = volatility_inverse_weight(valid_syms, vols_arr)
        elif method == "kelly":
            raw_w = kelly_weight(valid_syms, expRs_arr, wr_arr)
        elif method == "sharpe":
            raw_w = sharpe_ratio_weight(valid_syms, expRs_arr, vols_arr)
        elif method == "min_var":
            # 简化：用波动率估计协方差（对角线）
            n = len(valid_syms)
            corr = np.eye(n)  # 假设不相关，保守估计
            raw_w = min_variance_weight(valid_syms, corr, vols_arr)
        else:
            raw_w = equal_weight(valid_syms)

        # 应用约束
        constrained_w = apply_weight_constraints(raw_w, valid_syms, max_single_weight, max_sector_weight)

        weight_map = dict(zip(valid_syms, constrained_w))
        weight_history.append({"day": rebal_day, "weights": weight_map})

        # 应用到 OOS 期（下一个 rebalance 周期）
        oos_end = min(rebal_day + rebalance, n_days)
        current_weights = weight_map

        for sym in sym_list:
            if sym not in all_results or sym not in all_dfs:
                continue
            w = weight_map.get(sym, 0)
            if w <= 0:
                continue
            trades = all_results[sym].get("trades_detail", [])
            oos_trades = [t for t in trades if rebal_day <= int(t.get("exit_bar", 0)) < oos_end]
            for t in oos_trades:
                exit_bar = int(t.get("exit_bar", 0))
                if 0 <= exit_bar < n_days:
                    port_daily_full[exit_bar] += w * t.get("R_adj", 0) * risk_per_trade

    # 计算绩效
    equity = np.cumprod(1 + port_daily_full)
    total_return = equity[-1] - 1
    n_years = n_days / 252.0
    annual_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1
    annual_vol = np.std(port_daily_full) * np.sqrt(252)

    running_max = np.maximum.accumulate(equity)
    drawdowns = (running_max - equity) / running_max
    max_dd = float(np.max(drawdowns))

    sharpe = annual_return / annual_vol if annual_vol > 0 else 0
    calmar = annual_return / max_dd if max_dd > 0 else 0

    return {
        "method": method,
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "annual_vol": round(annual_vol * 100, 2),
        "max_dd": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "n_rebalances": len(weight_history),
        "weight_history": weight_history,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("组合配置最终优化器：约束加权 + WF 验证 + 实盘配置输出")
    print("=" * 80)

    # 约束参数
    MAX_SINGLE = 0.10  # 单品种最大权重 10%
    MAX_SECTOR = 0.30  # 单板块最大权重 30%
    MIN_TRADES = 30  # 最少交易笔数
    RISK_PER_TRADE = 0.015  # 单笔风险 1.5%

    print("\n  约束参数:")
    print(f"    单品种最大权重: {MAX_SINGLE * 100:.0f}%")
    print(f"    单板块最大权重: {MAX_SECTOR * 100:.0f}%")
    print(f"    最少交易笔数: {MIN_TRADES}")
    print(f"    单笔风险: {RISK_PER_TRADE * 100:.1f}%")

    # 候选品种
    candidate_syms = sorted(DEFAULT_CONFIG.get("per_symbol_risk", {}).keys())
    extra = [
        "rb",
        "hc",
        "FG",
        "au",
        "ru",
        "CF",
        "ss",
        "cu",
        "al",
        "zn",
        "ag",
        "J",
        "JM",
        "i",
        "sc",
        "SA",
        "MA",
        "TA",
        "v",
        "pp",
        "l",
        "y",
        "p",
        "c",
        "a",
        "m",
        "b",
        "RM",
        "rr",
    ]
    for s in extra:
        if s not in candidate_syms and s in SYMBOLS:
            candidate_syms.append(s)

    print(f"\n  候选品种: {len(candidate_syms)} 个")
    print("  阶段 1/5: 全品种回测 + 筛选 ...")

    all_results = {}
    all_dfs = {}
    all_daily_pnl = {}
    total = len(candidate_syms)

    for i, sym in enumerate(candidate_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=DEFAULT_CONFIG, df_in=df, window=200)
            if r and r.get("trades", 0) >= MIN_TRADES and r.get("expR", 0) > 0:
                all_results[sym] = r
                all_dfs[sym] = df
                all_daily_pnl[sym] = compute_trade_daily_series(r.get("trades_detail", []), len(df))
            print(
                f"  [{i + 1}/{total}] {sym:>5}  expR={r.get('expR', 0):+.3f}  trades={r.get('trades', 0):>3}",
                end="\r",
                flush=True,
            )
        except Exception:
            continue
    print()

    sym_list = sorted(all_results.keys())
    print(f"  入选品种（expR>0 且笔数≥{MIN_TRADES}）: {len(sym_list)} 个")

    # 阶段 2: 相关性分析
    print("\n  阶段 2/5: 相关性分析 ...")

    # 计算价格收益相关性
    n_syms = len(sym_list)
    min_len = min(len(all_dfs[s]) for s in sym_list) - 1
    ret_matrix = np.zeros((min_len, n_syms))
    for i, sym in enumerate(sym_list):
        rets = compute_daily_returns(sym, all_dfs[sym].iloc[-min_len - 1 :])
        ret_matrix[:, i] = rets[:min_len]

    corr_matrix = np.corrcoef(ret_matrix.T)

    # 板块分组统计
    sectors = {}
    for sym in sym_list:
        g = symbols_group(sym)
        sectors.setdefault(g, []).append(sym)

    print("\n  板块分布:")
    for sec, syms in sorted(sectors.items(), key=lambda x: -len(x[1])):
        avg_corr = 0
        count = 0
        idx = [sym_list.index(s) for s in syms]
        for i in range(len(idx)):
            for j in range(i + 1, len(idx)):
                avg_corr += abs(corr_matrix[idx[i], idx[j]])
                count += 1
        avg_corr = avg_corr / count if count > 0 else 0
        print(f"    {sec}: {len(syms)} 个品种，组内平均 |r|={avg_corr:.3f}")

    # 高相关对
    high_corr = []
    for i in range(n_syms):
        for j in range(i + 1, n_syms):
            if abs(corr_matrix[i, j]) > 0.6:
                high_corr.append((sym_list[i], sym_list[j], corr_matrix[i, j]))
    high_corr.sort(key=lambda x: -abs(x[2]))

    print(f"\n  高相关对（|r|>0.6）: {len(high_corr)} 对")
    for s1, s2, r in high_corr[:15]:
        print(f"    {s1:>5} - {s2:<5}  r={r:+.3f}  ({symbols_name(s1)}-{symbols_name(s2)})")

    # 阶段 3: 各分配方法对比（全样本）
    print("\n  阶段 3/5: 资金分配方法对比（全样本，带约束）...")

    # 计算各品种指标
    expRs = np.array([all_results[s]["expR"] for s in sym_list])
    win_rates = np.array([all_results[s].get("win_rate", 0.3) for s in sym_list])

    # 波动率（基于交易收益）
    vols = []
    for sym in sym_list:
        Rs = [t["R_adj"] for t in all_results[sym].get("trades_detail", [])]
        vols.append(np.std(Rs) * np.sqrt(252) if len(Rs) > 5 else 1.0)
    vols = np.array(vols)

    methods = {
        "等权配置": ("equal", None),
        "波动率倒数": ("vol_inv", None),
        "凯利配置": ("kelly", None),
        "夏普比例": ("sharpe", None),
        "最小方差": ("min_var", None),
    }

    portfolio_results = {}
    for name, (method, _) in methods.items():
        if method == "equal":
            raw_w = equal_weight(sym_list)
        elif method == "vol_inv":
            raw_w = volatility_inverse_weight(sym_list, vols)
        elif method == "kelly":
            raw_w = kelly_weight(sym_list, expRs, win_rates)
        elif method == "sharpe":
            raw_w = sharpe_ratio_weight(sym_list, expRs, vols)
        elif method == "min_var":
            raw_w = min_variance_weight(sym_list, corr_matrix, vols)
        else:
            raw_w = equal_weight(sym_list)

        constrained_w = apply_weight_constraints(raw_w, sym_list, MAX_SINGLE, MAX_SECTOR)
        result = portfolio_backtest(
            sym_list,
            constrained_w,
            {s: all_results[s].get("trades_detail", []) for s in sym_list},
            all_daily_pnl,
            RISK_PER_TRADE,
        )
        result["weights"] = constrained_w
        result["raw_weights"] = raw_w
        portfolio_results[name] = result

    print(f"\n  {'方法':<12}  {'总收益':>8}  {'年化':>8}  {'波动':>8}  {'回撤':>8}  {'夏普':>6}  {'卡玛':>6}")
    print("  " + "-" * 70)
    for name, r in portfolio_results.items():
        print(
            f"  {name:<12}  {r['total_return']:>+7.2f}%  {r['annual_return']:>+7.2f}%  "
            f"{r['annual_vol']:>7.2f}%  {r['max_dd']:>7.2f}%  "
            f"{r['sharpe']:>6.2f}  {r['calmar']:>6.2f}"
        )

    # 阶段 4: Walk-Forward 验证
    print("\n  阶段 4/5: Walk-Forward 验证（回溯252日，再平衡60日）...")

    wf_results = {}
    wf_methods = ["equal", "vol_inv", "kelly", "sharpe", "min_var"]
    wf_names = {"equal": "等权", "vol_inv": "波动率倒数", "kelly": "凯利", "sharpe": "夏普比例", "min_var": "最小方差"}

    for i, method in enumerate(wf_methods):
        print(f"    [{i + 1}/{len(wf_methods)}] {wf_names[method]} ...", end="\r", flush=True)
        wf_r = walk_forward_portfolio(
            sym_list,
            all_dfs,
            all_results,
            method=method,
            lookback=252,
            rebalance=60,
            risk_per_trade=RISK_PER_TRADE,
            max_single_weight=MAX_SINGLE,
            max_sector_weight=MAX_SECTOR,
        )
        if wf_r:
            wf_results[method] = wf_r
    print()

    print(f"\n  {'方法':<12}  {'总收益':>8}  {'年化':>8}  {'波动':>8}  {'回撤':>8}  {'夏普':>6}  {'卡玛':>6}")
    print("  " + "-" * 70)
    for method, r in wf_results.items():
        print(
            f"  {wf_names[method]:<12}  {r['total_return']:>+7.2f}%  {r['annual_return']:>+7.2f}%  "
            f"{r['annual_vol']:>7.2f}%  {r['max_dd']:>7.2f}%  "
            f"{r['sharpe']:>6.2f}  {r['calmar']:>6.2f}"
        )

    # 阶段 5: 推荐配置输出
    print("\n  阶段 5/5: 推荐配置输出 ...")

    # 基于 WF 结果选最优（夏普+卡玛综合）
    if wf_results:
        best_method = max(
            wf_results.keys(), key=lambda m: wf_results[m]["sharpe"] * 0.5 + wf_results[m]["calmar"] * 0.5
        )
        best_name = wf_names[best_method]
        best_wf = wf_results[best_method]

        # 取最近一期权重
        latest_weights = best_wf["weight_history"][-1]["weights"] if best_wf["weight_history"] else {}
    else:
        best_method = "kelly"
        best_name = "凯利配置"
        latest_weights = dict(zip(sym_list, portfolio_results["凯利配置"]["weights"]))

    print(f"\n{'=' * 80}")
    print(f"  推荐配置: {best_name}")
    print(f"{'=' * 80}")
    print("  WF 验证绩效:")
    if best_wf:
        print(f"    总收益: {best_wf['total_return']:+.2f}%")
        print(f"    年化收益: {best_wf['annual_return']:+.2f}%")
        print(f"    年化波动: {best_wf['annual_vol']:.2f}%")
        print(f"    最大回撤: {best_wf['max_dd']:.2f}%")
        print(f"    夏普比率: {best_wf['sharpe']:.2f}")
        print(f"    卡玛比率: {best_wf['calmar']:.2f}")
        print(f"    再平衡次数: {best_wf['n_rebalances']}")

    print("\n  推荐权重（最新一期）:")
    print(f"  {'品种':>5}  {'名称':>8}  {'板块':>8}  {'权重':>8}")
    print("  " + "-" * 35)

    sorted_weights = sorted(latest_weights.items(), key=lambda x: -x[1])
    total_w = 0
    for sym, w in sorted_weights:
        if w > 0.001:
            print(f"  {sym:>5}  {symbols_name(sym):>8}  {symbols_group(sym):>8}  {w * 100:>7.2f}%")
            total_w += w

    # 板块汇总
    sector_weights = {}
    for sym, w in latest_weights.items():
        sec = symbols_group(sym)
        sector_weights[sec] = sector_weights.get(sec, 0) + w

    print("\n  板块权重分布:")
    for sec, w in sorted(sector_weights.items(), key=lambda x: -x[1]):
        bar = "█" * int(w * 50)
        print(f"    {sec:<8} {w * 100:>6.2f}% {bar}")

    # 输出 JSON 配置
    config_output = {
        "method": best_name,
        "method_code": best_method,
        "risk_per_trade_pct": RISK_PER_TRADE * 100,
        "max_single_weight_pct": MAX_SINGLE * 100,
        "max_sector_weight_pct": MAX_SECTOR * 100,
        "min_trades_filter": MIN_TRADES,
        "n_symbols": len([s for s, w in latest_weights.items() if w > 0.001]),
        "weights": {sym: round(w, 6) for sym, w in sorted_weights if w > 0.001},
        "sector_weights": {sec: round(w, 4) for sec, w in sector_weights.items()},
        "wf_performance": {
            "total_return_pct": best_wf["total_return"] if best_wf else None,
            "annual_return_pct": best_wf["annual_return"] if best_wf else None,
            "annual_vol_pct": best_wf["annual_vol"] if best_wf else None,
            "max_dd_pct": best_wf["max_dd"] if best_wf else None,
            "sharpe": best_wf["sharpe"] if best_wf else None,
            "calmar": best_wf["calmar"] if best_wf else None,
        },
    }

    os.makedirs("logs", exist_ok=True)
    with open("logs/portfolio_allocation.json", "w", encoding="utf-8") as f:
        json.dump(config_output, f, ensure_ascii=False, indent=2)

    print("\n  配置已保存 → logs/portfolio_allocation.json")
    print("=" * 80)


if __name__ == "__main__":
    main()
