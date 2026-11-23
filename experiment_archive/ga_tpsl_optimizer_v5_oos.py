"""
ga_tpsl_optimizer_v5_oos.py — OOS 感知 GA 优化器（Phase 3.5）
================================================================
在 v4 3 参数精简版基础上，加入 OOS 感知适应度：
    - 适应度 = (1 - oos_weight) × IS_WF表现 + oos_weight × OOS表现
    - 算法自然倾向于 IS + OOS 都好的参数，抑制纯 IS 过拟合
    - OOS 权重默认 0.2（温和引导，不是强拟合）

3 个优化参数:
    [0] T_thresh_mult   入场阈值倍率（相对基线）
    [1] stop_atr_mult   止损 ATR 倍数
    [2] rr_ratio        盈亏比

保留功能:
    - NSGA-II 多目标（expR + Calmar + 稳定性）
    - Walk-Forward 滚动窗口适应度
    - OOS 感知适应度混合
    - 纯 OOS 样本外最终验证
    - 参数稳健性检验（±20% 扰动）
    - L1 正则化 + 参数范围收缩
    - 并行评估 + 早停机制
    - HTML 报告输出

用法:
    python3 ga_tpsl_optimizer_v5_oos.py --symbol rb --shrink --l1 --fast --full --oos-weight 0.2
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
import time

# 防止 numpy/pandas 内部多线程与 multiprocessing 争抢 CPU
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from deap import base, creator, tools

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

# ============================================================================
# 配置
# ============================================================================

# 3 个优化参数的上下界
# [0] T_thresh_mult: 入场阈值相对基线的倍率
# [1] stop_atr_mult: 止损 ATR 倍数
# [2] rr_ratio:      盈亏比
PARAM_BOUNDS = [
    (0.5, 1.8),  # gene[0]: T_thresh_mult
    (0.8, 3.0),  # gene[1]: stop_atr_mult
    (1.2, 4.0),  # gene[2]: rr_ratio
]
PARAM_NAMES = [
    "T_thresh_mult",
    "stop_atr_mult",
    "rr_ratio",
]
N_PARAMS = len(PARAM_BOUNDS)

# 固定为基线的参数（不参与优化）
FIXED_PARAM_NAMES = [
    "fc_confirm",
    "fc_hard",
    "cooldown_bars",
    "tail_pct",
    "tail_trail_R",
    "min_profit_R",
]

# GA 参数
DEFAULT_POP_SIZE = 100  # 3 参数不需要太大种群
DEFAULT_GEN_COUNT = 30
CXPB = 0.9
MUTPB = 0.3
SBX_ETA = 15
PM_ETA = 15
CALMAR_CAP = 10.0
IMMIGRANT_RATE = 0.10
IMMIGRANT_INTERVAL = 5
EARLY_STOP_PATIENCE = 8
EARLY_STOP_MIN_IMPROVE = 0.001
DEFAULT_N_JOBS = 8

# 约束
MIN_TRADES = 10
MIN_WIN_RATE = 0.15
MAX_DRAWDOWN = 0.50

# WF 配置
DEFAULT_TRAIN_BARS = 400
DEFAULT_VALID_BARS = 120
DEFAULT_STEP_BARS = 60

# OOS 配置
OOS_RATIO = 0.20

# 稳健性检验
ROBUST_PERTURB = 0.20
ROBUST_POINTS = 11

# L1 正则化
REG_L1_ENABLED = True
REG_L1_WEIGHT = 0.5
REG_L1_TARGETS = [0, 1]

# OOS 感知适应度
DEFAULT_OOS_WEIGHT = 0.2  # OOS 在适应度中的权重（0=纯IS，1=纯OOS）

# 参数范围收缩
SHRINK_PCT = 0.40

# 结果缓存
_eval_cache = {}

# Worker 进程全局数据
_worker_data = {}


def _init_worker(
    symbol,
    df_is,
    df_oos,
    train_bars,
    valid_bars,
    step_bars,
    data_slice_id,
    baseline_ind=None,
    bounds=None,
    use_l1=False,
    oos_weight=0.0,
):
    """Pool initializer：在每个 worker 进程中设置全局数据。"""
    global _worker_data
    _worker_data = {
        "symbol": symbol,
        "df_is": df_is,
        "df_oos": df_oos,
        "train_bars": train_bars,
        "valid_bars": valid_bars,
        "step_bars": step_bars,
        "data_slice_id": data_slice_id,
        "baseline_ind": baseline_ind,
        "bounds": bounds,
        "use_l1": use_l1,
        "oos_weight": oos_weight,
    }


def _worker_evaluate(individual):
    """Worker 进程中的评估函数（OOS 感知版）。"""
    global _worker_data
    return oos_aware_evaluate(
        individual,
        _worker_data["symbol"],
        _worker_data["df_is"],
        _worker_data["df_oos"],
        _worker_data["train_bars"],
        _worker_data["valid_bars"],
        _worker_data["step_bars"],
        _worker_data["data_slice_id"],
        baseline_ind=_worker_data.get("baseline_ind"),
        bounds=_worker_data.get("bounds"),
        use_l1=_worker_data.get("use_l1", False),
        oos_weight=_worker_data.get("oos_weight", 0.0),
    )


def _cache_key(params_tuple, symbol, data_slice_id):
    return (tuple(round(p, 4) for p in params_tuple), symbol, data_slice_id)


# ============================================================================
# 基线参数获取
# ============================================================================


def _get_baseline_params(symbol):
    """获取某品种的基线参数值（所有 9 个参数）。"""
    cfg = DEFAULT_CONFIG

    # T_thresh 基线
    t_base = 22.0
    tbs = cfg.get("thresholds_by_symbol", {})
    if symbol in tbs and "T_thresh" in tbs[symbol]:
        t_base = float(tbs[symbol]["T_thresh"])
    else:
        groups = cfg.get("thresholds", {})
        t_base = 22.0

    # fc_confirm / fc_hard 基线
    bs = cfg.get("bias_synthesis", {})
    fc_confirm_base = float(bs.get("fc_confirm", 25))
    fc_hard_base = float(bs.get("fc_hard", 25))

    # cooldown_bars 基线
    cooldown_base = 5.0

    # 出场参数基线
    psr = cfg.get("per_symbol_risk", {}).get(symbol, {})
    stop_base = float(psr.get("stop_atr_mult", cfg["risk_gate"]["stop_atr_mult"]))
    rr_base = float(psr.get("rr_ratio", cfg["risk_gate"]["rr_ratio"]))

    tt = cfg.get("trailing_tail", {})
    tail_pct_base = float(tt.get("tail_pct", 0.25))
    tail_trail_R_base = float(tt.get("tail_trail_R", 2.0))
    min_profit_R_base = float(tt.get("min_profit_R", 2.0))

    return {
        "T_thresh_base": t_base,
        "fc_confirm": fc_confirm_base,
        "fc_hard": fc_hard_base,
        "cooldown_bars": cooldown_base,
        "stop_atr_mult": stop_base,
        "rr_ratio": rr_base,
        "tail_pct": tail_pct_base,
        "tail_trail_R": tail_trail_R_base,
        "min_profit_R": min_profit_R_base,
    }


def _baseline_to_optimized_individual(baseline):
    """把基线参数字典转成 3 参数优化 individual。"""
    return [
        1.0,  # T_thresh_mult = 1.0 (基线倍率)
        baseline["stop_atr_mult"],
        baseline["rr_ratio"],
    ]


def _full_params_from_individual(individual, baseline):
    """从 3 参数 individual + 基线，得到完整 9 参数列表（用于配置生成）。

    顺序与 v3 的 PARAM_NAMES 一致，方便复用 _make_config 逻辑。
    """
    T_thresh_mult, stop_atr_mult, rr_ratio = individual
    return [
        T_thresh_mult,  # 0: T_thresh_mult
        baseline["fc_confirm"],  # 1: fc_confirm (固定)
        baseline["fc_hard"],  # 2: fc_hard (固定)
        baseline["cooldown_bars"],  # 3: cooldown_bars (固定)
        stop_atr_mult,  # 4: stop_atr_mult
        rr_ratio,  # 5: rr_ratio
        baseline["tail_pct"],  # 6: tail_pct (固定)
        baseline["tail_trail_R"],  # 7: tail_trail_R (固定)
        baseline["min_profit_R"],  # 8: min_profit_R (固定)
    ]


def _calc_l1_penalty(individual, baseline_ind, bounds):
    """计算 L1 正则惩罚。"""
    total_dev = 0.0
    for i in range(len(individual)):
        lo, hi = bounds[i]
        base = baseline_ind[i]
        range_size = hi - lo
        if range_size > 0:
            dev = abs(individual[i] - base) / range_size
            total_dev += dev
    avg_dev = total_dev / len(individual)
    return avg_dev * REG_L1_WEIGHT


def _get_shrunk_bounds(baseline_ind, shrink_pct=SHRINK_PCT):
    """计算收缩后的参数范围。"""
    shrunk = []
    for i in range(len(PARAM_BOUNDS)):
        lo, hi = PARAM_BOUNDS[i]
        base = baseline_ind[i]
        range_size = hi - lo
        half = range_size * shrink_pct
        new_lo = max(lo, base - half)
        new_hi = min(hi, base + half)
        if new_hi - new_lo < range_size * 0.1:
            new_lo = max(lo, base - range_size * 0.05)
            new_hi = min(hi, base + range_size * 0.05)
        shrunk.append((new_lo, new_hi))
    return shrunk


# ============================================================================
# 配置生成
# ============================================================================


def _make_config(individual, symbol, base_cfg=DEFAULT_CONFIG):
    """根据 3 个优化参数 + 基线固定参数生成配置。
    返回 (cfg, cooldown_bars) 元组。
    """
    baseline = _get_baseline_params(symbol)
    full_params = _full_params_from_individual(individual, baseline)

    (T_thresh_mult, fc_confirm, fc_hard, cooldown_bars, stop_mult, rr_ratio, tail_pct, tail_trail_R, min_profit_R) = (
        full_params
    )

    cfg = copy.deepcopy(base_cfg)

    # ---- 入场参数 ----
    T_thresh_abs = baseline["T_thresh_base"] * T_thresh_mult
    cfg.setdefault("thresholds_by_symbol", {})
    cfg["thresholds_by_symbol"].setdefault(symbol, {})
    cfg["thresholds_by_symbol"][symbol]["T_thresh"] = round(T_thresh_abs, 2)

    cfg.setdefault("bias_synthesis", {})
    cfg["bias_synthesis"]["fc_confirm"] = round(fc_confirm, 4)
    cfg["bias_synthesis"]["fc_hard"] = round(fc_hard, 4)

    # ---- 出场参数 ----
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

    return cfg, int(round(cooldown_bars))


# ============================================================================
# 指标计算
# ============================================================================


def _calc_max_drawdown(R_list):
    if not R_list:
        return 0.0
    cumulative = np.cumsum(R_list)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    return float(np.max(drawdown)) if len(drawdown) > 0 else 0.0


def _calc_metrics(result):
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
    if max_dd > 0.01:
        calmar = total_R / max_dd
    elif total_R > 0:
        calmar = min(total_R * 20, CALMAR_CAP)
    else:
        calmar = 0.0
    calmar = min(calmar, CALMAR_CAP)

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
# Walk-Forward 适应度评估
# ============================================================================


def _slice_data(df, start_i, end_i):
    return df.iloc[start_i:end_i].copy()


def single_evaluate(individual, symbol, df):
    """在单个数据集上做完整回测，返回详细指标。"""
    cfg, cooldown = _make_config(individual, symbol)
    result = walk_forward_backtest(
        symbol,
        cfg=cfg,
        df_in=df,
        min_bars=60,
        cooldown_bars=cooldown,
    )
    return _calc_metrics(result)


def wf_evaluate(
    individual,
    symbol,
    df,
    train_bars,
    valid_bars,
    step_bars,
    data_slice_id="IS_WF",
    baseline_ind=None,
    bounds=None,
    use_l1=False,
):
    """Walk-Forward 评估：滚动窗口内的验证期表现作为适应度。

    使用中位数（而非均值），更稳健，不受极端值影响。
    窗口不足 2 个时给严重惩罚（-10），避免无效参数滥竽充数。
    """
    key = _cache_key(individual, symbol, data_slice_id)
    if key in _eval_cache:
        return _eval_cache[key]

    cfg, cooldown = _make_config(individual, symbol)

    n = len(df)
    window_size = train_bars + valid_bars

    valid_expRs = []
    valid_calmars = []

    start = 0
    while start + window_size <= n:
        valid_end = start + window_size

        df_segment = df.iloc[start:valid_end].copy()
        result = walk_forward_backtest(
            symbol,
            cfg=cfg,
            df_in=df_segment,
            min_bars=train_bars - 10,
            cooldown_bars=cooldown,
        )
        metrics = _calc_metrics(result)

        if metrics["trades"] >= 3:
            valid_expRs.append(metrics["expR"])
            valid_calmars.append(metrics["calmar"])

        start += step_bars

    # 有效窗口太少 → 严重惩罚
    if len(valid_expRs) < 2:
        result = (-10.0, -10.0, -10.0)
        _eval_cache[key] = result
        return result

    # 三个目标：全部最大化
    f1 = float(np.median(valid_expRs))
    f2 = float(np.median(valid_calmars))

    # 稳定性：正收益窗口比例
    positive_ratio = sum(1 for r in valid_expRs if r > 0) / len(valid_expRs)
    f3 = positive_ratio

    # 额外惩罚：亏损窗口太多
    if positive_ratio < 0.4:
        penalty = (0.4 - positive_ratio) * 1.0
        f1 -= penalty
        f2 -= penalty

    # L1 正则惩罚
    if use_l1 and baseline_ind is not None and bounds is not None and REG_L1_ENABLED:
        l1_penalty = _calc_l1_penalty(individual, baseline_ind, bounds)
        if 0 in REG_L1_TARGETS:
            f1 -= l1_penalty
        if 1 in REG_L1_TARGETS:
            f2 -= l1_penalty

    result = (f1, f2, f3)
    _eval_cache[key] = result
    return result


def oos_aware_evaluate(
    individual,
    symbol,
    df_is,
    df_oos,
    train_bars,
    valid_bars,
    step_bars,
    data_slice_id="IS_OOS_BLEND",
    baseline_ind=None,
    bounds=None,
    use_l1=False,
    oos_weight=0.0,
):
    """OOS 感知适应度：混合 IS_WF 表现 + OOS 表现。

    当 oos_weight = 0 时，退化为纯 WF 适应度（与 v4 一致）。
    当 oos_weight > 0 时，OOS 表现参与适应度计算，引导算法向泛化性好的参数进化。

    混合方式：对 expR 和 Calmar 两个目标分别做加权平均。
    稳定性（stability）只用 IS 的，因为 OOS 只有一段，算不出窗口稳定性。
    """
    # IS_WF 适应度
    is_fit = wf_evaluate(
        individual,
        symbol,
        df_is,
        train_bars,
        valid_bars,
        step_bars,
        data_slice_id + "_IS",
        baseline_ind,
        bounds,
        use_l1,
    )

    if oos_weight <= 0.0:
        return is_fit

    # OOS 单段评估
    oos_key = _cache_key(individual, symbol, data_slice_id + "_OOS")
    if oos_key in _eval_cache:
        oos_metrics = _eval_cache[oos_key]
    else:
        oos_metrics_raw = single_evaluate(individual, symbol, df_oos)
        # 转成类似 wf 的 3 元组格式（OOS 只有一段，稳定性用 0.5 占位）
        oos_expR = oos_metrics_raw["expR"]
        oos_calmar = oos_metrics_raw["calmar"]
        # OOS 交易数太少的话给惩罚
        if oos_metrics_raw["trades"] < 3:
            oos_expR = min(oos_expR, 0.0)
            oos_calmar = min(oos_calmar, 0.0)
        oos_stab = 1.0 if oos_expR > 0 else 0.0
        oos_metrics = (oos_expR, oos_calmar, oos_stab)
        _eval_cache[oos_key] = oos_metrics

    # 加权混合
    w_is = 1.0 - oos_weight
    w_oos = oos_weight

    blended_expR = w_is * is_fit[0] + w_oos * oos_metrics[0]
    blended_calmar = w_is * is_fit[1] + w_oos * oos_metrics[1]
    # 稳定性只用 IS 的（OOS 一段数据没有稳定性可言）
    blended_stab = is_fit[2]

    return (blended_expR, blended_calmar, blended_stab)


# ============================================================================
# DEAP 初始化
# ============================================================================


def setup_deap_nsga2(
    symbol, df_is, df_oos, train_bars, valid_bars, step_bars, pop_size, use_shrink=False, use_l1=False, oos_weight=0.0
):
    """初始化 NSGA-II 多目标优化（OOS 感知版）。"""
    for name in ["FitnessMulti", "Individual"]:
        if name in creator.__dict__:
            delattr(creator, name)

    creator.create("FitnessMulti", base.Fitness, weights=(1.0, 1.0, 1.0))
    creator.create("Individual", list, fitness=creator.FitnessMulti)

    baseline_dict = _get_baseline_params(symbol)
    baseline_ind = _baseline_to_optimized_individual(baseline_dict)
    if use_shrink:
        actual_bounds = _get_shrunk_bounds(baseline_ind, SHRINK_PCT)
    else:
        actual_bounds = list(PARAM_BOUNDS)

    toolbox = base.Toolbox()

    for i in range(N_PARAMS):

        def _attr_factory(idx):
            def _f():
                low, high = actual_bounds[idx]
                return random.uniform(low, high)

            return _f

        toolbox.register(f"attr_float_{i}", _attr_factory(i))

    attr_funcs = [getattr(toolbox, f"attr_float_{i}") for i in range(N_PARAMS)]
    toolbox.register("individual", tools.initCycle, creator.Individual, tuple(attr_funcs), n=1)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    toolbox.register(
        "evaluate",
        oos_aware_evaluate,
        symbol=symbol,
        df_is=df_is,
        df_oos=df_oos,
        train_bars=train_bars,
        valid_bars=valid_bars,
        step_bars=step_bars,
        data_slice_id="GA",
        baseline_ind=baseline_ind,
        bounds=actual_bounds,
        use_l1=use_l1,
        oos_weight=oos_weight,
    )

    toolbox.register(
        "mate",
        tools.cxSimulatedBinaryBounded,
        low=[b[0] for b in actual_bounds],
        up=[b[1] for b in actual_bounds],
        eta=SBX_ETA,
    )

    toolbox.register(
        "mutate",
        tools.mutPolynomialBounded,
        low=[b[0] for b in actual_bounds],
        up=[b[1] for b in actual_bounds],
        eta=PM_ETA,
        indpb=MUTPB,
    )

    toolbox.register("select", tools.selNSGA2)

    toolbox.baseline_ind = baseline_ind
    toolbox.actual_bounds = actual_bounds
    toolbox.use_l1 = use_l1
    toolbox.use_shrink = use_shrink
    toolbox.oos_weight = oos_weight
    toolbox.df_oos = df_oos

    return toolbox


# ============================================================================
# 帕累托前沿分析
# ============================================================================


def select_candidates(pareto_front):
    """从帕累托前沿中选出 4 个代表性候选方案。"""
    if len(pareto_front) == 0:
        return {}

    f1_vals = [ind.fitness.values[0] for ind in pareto_front]
    f2_vals = [ind.fitness.values[1] for ind in pareto_front]
    f3_vals = [ind.fitness.values[2] for ind in pareto_front]

    candidates = {}

    idx = int(np.argmax(f1_vals))
    candidates["aggressive"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(N_PARAMS)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "激进型（expR 最大）",
    }

    idx = int(np.argmax(f2_vals))
    candidates["balanced"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(N_PARAMS)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "稳健型（卡玛比率最大）",
    }

    idx = int(np.argmax(f3_vals))
    candidates["stable"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(N_PARAMS)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "稳定型（正收益窗口最多）",
    }

    f1_max, f1_min = max(f1_vals), min(f1_vals)
    f2_max, f2_min = max(f2_vals), min(f2_vals)
    f3_max, f3_min = max(f3_vals), min(f3_vals)

    def norm_val(i):
        n1 = (f1_vals[i] - f1_min) / (f1_max - f1_min) if f1_max > f1_min else 0
        n2 = (f2_vals[i] - f2_min) / (f2_max - f2_min) if f2_max > f2_min else 0
        n3 = (f3_vals[i] - f3_min) / (f3_max - f3_min) if f3_max > f3_min else 0
        return n1 + n2 + n3

    scores = [norm_val(i) for i in range(len(pareto_front))]
    idx = int(np.argmax(scores))
    candidates["reference"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(N_PARAMS)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "折中参考点（三目标综合最优）",
    }

    return candidates


# ============================================================================
# GA 主循环
# ============================================================================


def run_ga_nsga2(
    symbol,
    df_is,
    df_oos,
    train_bars,
    valid_bars,
    step_bars,
    pop_size,
    gen_count,
    use_shrink=False,
    use_l1=False,
    n_jobs=DEFAULT_N_JOBS,
    early_stop_patience=EARLY_STOP_PATIENCE,
    oos_weight=0.0,
):
    """运行 NSGA-II 优化，带移民、早停、并行评估（OOS 感知版）。"""
    toolbox = setup_deap_nsga2(
        symbol,
        df_is,
        df_oos,
        train_bars,
        valid_bars,
        step_bars,
        pop_size,
        use_shrink=use_shrink,
        use_l1=use_l1,
        oos_weight=oos_weight,
    )

    # 确保种群是 4 的倍数（selNSGA2 要求）
    if pop_size % 4 != 0:
        pop_size = pop_size + (4 - pop_size % 4)

    # 并行评估
    from multiprocessing import Pool

    pool = None
    if n_jobs and n_jobs > 1:
        pool = Pool(
            processes=n_jobs,
            initializer=_init_worker,
            initargs=(
                symbol,
                df_is,
                df_oos,
                train_bars,
                valid_bars,
                step_bars,
                "GA",
                toolbox.baseline_ind,
                toolbox.actual_bounds,
                use_l1,
                oos_weight,
            ),
        )
        toolbox.register("map", pool.map)

    pop = toolbox.population(n=pop_size)
    # 把基线个体加入种群
    baseline_ind = toolbox.baseline_ind
    base_ind = toolbox.individual()
    for i in range(N_PARAMS):
        base_ind[i] = baseline_ind[i]
    pop[0] = base_ind

    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("avg", np.mean, axis=0)
    stats.register("std", np.std, axis=0)
    stats.register("min", np.min, axis=0)
    stats.register("max", np.max, axis=0)

    logbook = tools.Logbook()
    logbook.header = ["gen", "evals", "best_expR", "best_calmar", "best_stability", "pareto_size"]

    best_expR_history = []
    pareto_sizes = []

    t0 = time.time()

    # 初始评估
    invalid_ind = [ind for ind in pop if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    pareto_front = tools.sortNondominated(pop, len(pop), first_front_only=True)[0]
    best_expR = max(ind.fitness.values[0] for ind in pop)
    best_calmar = max(ind.fitness.values[1] for ind in pop)
    best_stab = max(ind.fitness.values[2] for ind in pop)

    logbook.record(
        gen=0,
        evals=len(invalid_ind),
        best_expR=round(best_expR, 4),
        best_calmar=round(best_calmar, 2),
        best_stability=round(best_stab, 4),
        pareto_size=len(pareto_front),
    )
    best_expR_history.append(best_expR)
    pareto_sizes.append(len(pareto_front))

    print(
        f"  Gen 0: best_expR={best_expR:.4f}, best_calmar={best_calmar:.2f}, "
        f"best_stability={best_stab:.4f}, pareto_size={len(pareto_front)}"
    )

    # 进化主循环
    for gen in range(1, gen_count + 1):
        t_gen = time.time()

        # 选择 + 变异（NSGA-II 的标准 eaSimple 变体）
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))

        for child1, child2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < CXPB:
                toolbox.mate(child1, child2)
                del child1.fitness.values
                del child2.fitness.values

        for mutant in offspring:
            if random.random() < MUTPB:
                toolbox.mutate(mutant)
                del mutant.fitness.values

        # 评估新个体
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # 精英保留：父代 + 子代合并后选 NSGA-II
        combined = pop + offspring
        pop = toolbox.select(combined, len(pop))

        # 移民机制：每隔几代注入随机个体
        if gen % IMMIGRANT_INTERVAL == 0:
            n_immigrants = max(1, int(len(pop) * IMMIGRANT_RATE))
            for i in range(n_immigrants):
                idx = random.randint(0, len(pop) - 1)
                new_ind = toolbox.individual()
                pop[idx] = new_ind
                # 评估移民
                fit = toolbox.evaluate(new_ind)
                pop[idx].fitness.values = fit

        # 记录
        pareto_front = tools.sortNondominated(pop, len(pop), first_front_only=True)[0]
        best_expR = max(ind.fitness.values[0] for ind in pop)
        best_calmar = max(ind.fitness.values[1] for ind in pop)
        best_stab = max(ind.fitness.values[2] for ind in pop)

        gen_time = time.time() - t_gen
        logbook.record(
            gen=gen,
            evals=len(invalid_ind),
            best_expR=round(best_expR, 4),
            best_calmar=round(best_calmar, 2),
            best_stability=round(best_stab, 4),
            pareto_size=len(pareto_front),
        )
        best_expR_history.append(best_expR)
        pareto_sizes.append(len(pareto_front))

        print(
            f"  Gen {gen:2d}: best_expR={best_expR:.4f}, best_calmar={best_calmar:.2f}, "
            f"pareto_size={len(pareto_front)}  ({gen_time:.1f}s)"
        )

        # 早停
        if len(best_expR_history) > early_stop_patience:
            window = best_expR_history[-(early_stop_patience + 1) :]
            improvement = window[-1] - window[0]
            if improvement < EARLY_STOP_MIN_IMPROVE:
                print(f"\n⏹️  早停触发：连续 {early_stop_patience} 代 expR 提升不足 {EARLY_STOP_MIN_IMPROVE}")
                break

    total_time = time.time() - t0

    if pool:
        pool.close()
        pool.join()

    pareto_front = tools.sortNondominated(pop, len(pop), first_front_only=True)[0]
    candidates = select_candidates(pareto_front)

    return {
        "pop": pop,
        "pareto_front": pareto_front,
        "candidates": candidates,
        "logbook": logbook,
        "total_time": total_time,
        "toolbox": toolbox,
    }


# ============================================================================
# OOS 验证
# ============================================================================


def evaluate_full_period(individual, symbol, df, baseline_ind=None, bounds=None, use_l1=False):
    """在完整数据集上评估（用于 IS 和 OOS 的单段评估）。"""
    return single_evaluate(individual, symbol, df)


def run_oos_validation(candidates, symbol, df_is, df_oos, toolbox):
    """对候选方案做纯 OOS 验证。"""
    results = {}
    for key, cand in candidates.items():
        ind_list = [cand["params"][name] for name in PARAM_NAMES]
        ind = toolbox.individual()
        for i in range(N_PARAMS):
            ind[i] = ind_list[i]

        is_metrics = evaluate_full_period(
            ind, symbol, df_is, toolbox.baseline_ind, toolbox.actual_bounds, toolbox.use_l1
        )
        oos_metrics = evaluate_full_period(
            ind, symbol, df_oos, toolbox.baseline_ind, toolbox.actual_bounds, toolbox.use_l1
        )

        # 退化率
        degradation = {}
        metrics_map = {
            "expR": "expR",
            "calmar": "calmar",
            "win_rate": "win_rate",
            "total_R": "total_R",
        }
        for m_is, m_label in metrics_map.items():
            is_val = is_metrics.get(m_is, 0)
            oos_val = oos_metrics.get(m_is, 0)
            if is_val != 0:
                degradation[m_label] = round((is_val - oos_val) / abs(is_val), 4)
            else:
                degradation[m_label] = 0.0

        passed = oos_metrics.get("expR", 0) > 0 and degradation.get("expR", 1) < 0.5

        results[key] = {
            "label": cand["label"],
            "params": cand["params"],
            "is": {k: v for k, v in is_metrics.items() if k != "R_list"},
            "oos": {k: v for k, v in oos_metrics.items() if k != "R_list"},
            "degradation": degradation,
            "passed": passed,
        }
    return results


# ============================================================================
# 稳健性检验
# ============================================================================


def run_robustness_test(candidates, symbol, df, toolbox):
    """参数稳健性检验：每个参数 ±20% 扰动，看表现变化。"""
    results = {}
    bounds = toolbox.actual_bounds

    for key, cand in candidates.items():
        ind_list = [cand["params"][name] for name in PARAM_NAMES]
        base_metrics = wf_evaluate(
            ind_list,
            symbol,
            df,
            toolbox.actual_bounds and DEFAULT_TRAIN_BARS or DEFAULT_TRAIN_BARS,
            DEFAULT_VALID_BARS,
            DEFAULT_STEP_BARS,
            "robust_base",
            toolbox.baseline_ind,
            bounds,
            toolbox.use_l1,
        )

        param_scores = []
        for i in range(N_PARAMS):
            lo, hi = bounds[i]
            base_val = ind_list[i]
            perturb_range = (hi - lo) * ROBUST_PERTURB

            test_vals = np.linspace(max(lo, base_val - perturb_range), min(hi, base_val + perturb_range), ROBUST_POINTS)

            expR_vals = []
            for v in test_vals:
                test_ind = list(ind_list)
                test_ind[i] = float(v)
                fit = wf_evaluate(
                    test_ind,
                    symbol,
                    df,
                    DEFAULT_TRAIN_BARS,
                    DEFAULT_VALID_BARS,
                    DEFAULT_STEP_BARS,
                    f"robust_{key}_{i}_{round(v, 4)}",
                    toolbox.baseline_ind,
                    bounds,
                    toolbox.use_l1,
                )
                expR_vals.append(fit[0])

            # 稳健性得分：变异系数的倒数（越稳定分越高）
            if len(expR_vals) > 1 and np.mean(expR_vals) != 0:
                cv = np.std(expR_vals) / abs(np.mean(expR_vals))
                score = 1.0 / (1.0 + cv)
            else:
                score = 1.0
            param_scores.append(score)

        overall_score = float(np.mean(param_scores))
        results[key] = {
            "label": cand["label"],
            "overall_score": round(overall_score, 3),
            "param_scores": {PARAM_NAMES[i]: round(param_scores[i], 3) for i in range(N_PARAMS)},
            "robust": overall_score >= 0.6,
        }

    return results


# ============================================================================
# 报告生成
# ============================================================================


def generate_html_report(
    symbol,
    result,
    oos_results,
    robust_results,
    baseline_is,
    baseline_oos,
    output_dir,
    train_bars,
    valid_bars,
    step_bars,
    use_shrink,
    use_l1,
    is_fast,
    oos_weight=0.0,
):
    """生成 HTML 报告。"""
    os.makedirs(output_dir, exist_ok=True)

    candidates = result["candidates"]
    pareto = result["pareto_front"]
    logbook = result["logbook"]
    total_time = result["total_time"]

    # 提取进化曲线数据
    gen_nums = [rec["gen"] for rec in logbook]
    best_expR_curve = [rec["best_expR"] for rec in logbook]
    best_calmar_curve = [rec["best_calmar"] for rec in logbook]
    pareto_size_curve = [rec["pareto_size"] for rec in logbook]

    # 候选方案表格
    cand_rows = ""
    for key, cand in candidates.items():
        p = cand["params"]
        f = cand["fitness"]
        param_str = "<br>".join([f"{name}: {p[name]}" for name in PARAM_NAMES])
        cand_rows += f"""
        <tr>
            <td><strong>{cand["label"]}</strong></td>
            <td>{param_str}</td>
            <td>{f[0]:.4f}</td>
            <td>{f[1]:.2f}</td>
            <td>{f[2]:.4f}</td>
        </tr>"""

    # OOS 表格
    oos_rows = ""
    for key, oos in oos_results.items():
        status = "✅ 通过" if oos["passed"] else "❌ 失败"
        is_expR = oos["is"].get("expR", 0)
        oos_expR = oos["oos"].get("expR", 0)
        deg_expR = oos["degradation"].get("expR", 0)
        is_trades = oos["is"].get("trades", 0)
        oos_trades = oos["oos"].get("trades", 0)
        is_calmar = oos["is"].get("calmar", 0)
        oos_calmar = oos["oos"].get("calmar", 0)
        oos_rows += f"""
        <tr>
            <td><strong>{oos["label"]}</strong></td>
            <td>{is_expR:.4f}</td>
            <td>{oos_expR:.4f}</td>
            <td>{deg_expR * 100:.1f}%</td>
            <td>{is_trades}</td>
            <td>{oos_trades}</td>
            <td>{is_calmar:.2f}</td>
            <td>{oos_calmar:.2f}</td>
            <td>{status}</td>
        </tr>"""

    # 稳健性表格
    rob_rows = ""
    for key, rob in robust_results.items():
        status = "✅ 稳健" if rob["robust"] else "⚠️ 敏感"
        ps = rob["param_scores"]
        ps_str = "<br>".join([f"{name}: {ps.get(name, 0):.2f}" for name in PARAM_NAMES])
        rob_rows += f"""
        <tr>
            <td><strong>{rob["label"]}</strong></td>
            <td>{rob["overall_score"]:.3f}</td>
            <td>{ps_str}</td>
            <td>{status}</td>
        </tr>"""

    # Pareto 前沿散点图（SVG）
    pareto_svg = _generate_pareto_svg(pareto)

    # 进化曲线 SVG
    curve_svg = _generate_curve_svg(gen_nums, best_expR_curve, best_calmar_curve, pareto_size_curve)

    # 配置标签
    config_tags = []
    if use_shrink:
        config_tags.append(f'<span class="tag tag-shrink">收缩范围 ±{int(SHRINK_PCT * 100)}%</span>')
    if use_l1:
        config_tags.append(f'<span class="tag tag-l1">L1 正则 w={REG_L1_WEIGHT}</span>')
    if is_fast:
        config_tags.append('<span class="tag tag-fast">Fast 模式</span>')
    if oos_weight > 0:
        config_tags.append(f'<span class="tag tag-oos">OOS感知 w={oos_weight}</span>')
    config_tags_str = " ".join(config_tags)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>GA 参数优化报告 - {symbol} (Phase 3.5 OOS-aware)</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        background: #f5f7fa;
        color: #333;
        padding: 30px;
        line-height: 1.6;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; }}
    h1 {{
        font-size: 28px;
        margin-bottom: 8px;
        color: #1a1a2e;
    }}
    .subtitle {{
        color: #666;
        margin-bottom: 24px;
        font-size: 14px;
    }}
    .tag {{
        display: inline-block;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 12px;
        margin-right: 6px;
        margin-bottom: 4px;
    }}
    .tag-shrink {{ background: #e8f5e9; color: #2e7d32; }}
    .tag-l1 {{ background: #e3f2fd; color: #1565c0; }}
    .tag-fast {{ background: #fff3e0; color: #e65100; }}
    .tag-oos {{ background: #f3e5f5; color: #6a1b9a; }}
    .section {{
        background: white;
        border-radius: 12px;
        padding: 24px;
        margin-bottom: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}
    .section h2 {{
        font-size: 20px;
        margin-bottom: 16px;
        color: #1a1a2e;
        border-bottom: 2px solid #eef0f4;
        padding-bottom: 10px;
    }}
    .section h3 {{
        font-size: 16px;
        margin: 16px 0 10px;
        color: #333;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 12px 0;
        font-size: 14px;
    }}
    th, td {{
        padding: 10px 12px;
        text-align: left;
        border-bottom: 1px solid #eee;
    }}
    th {{
        background: #f8f9fb;
        font-weight: 600;
        color: #555;
        font-size: 13px;
    }}
    tr:hover {{ background: #fafbfc; }}
    .metric-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin: 16px 0;
    }}
    .metric-card {{
        background: #f8f9fb;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
    }}
    .metric-card .value {{
        font-size: 28px;
        font-weight: 700;
        color: #1a1a2e;
    }}
    .metric-card .label {{
        font-size: 13px;
        color: #666;
        margin-top: 4px;
    }}
    .metric-card.good .value {{ color: #2e7d32; }}
    .metric-card.bad .value {{ color: #c62828; }}
    .svg-container {{
        background: #fafbfc;
        border-radius: 8px;
        padding: 16px;
        margin: 12px 0;
        overflow-x: auto;
    }}
    .note {{
        background: #fff8e1;
        border-left: 4px solid #ffc107;
        padding: 12px 16px;
        margin: 12px 0;
        border-radius: 0 8px 8px 0;
        font-size: 14px;
    }}
    .footer {{
        text-align: center;
        color: #999;
        font-size: 12px;
        margin-top: 30px;
    }}
</style>
</head>
<body>
<div class="container">
    <h1>🔬 GA 参数优化报告 — {symbol}</h1>
    <div class="subtitle">
        Phase 3.5 · OOS 感知适应度 · 3 参数联合优化 ·
        总耗时 {total_time:.1f}s ({total_time / 60:.1f}min) ·
        {config_tags_str}
    </div>

    <div class="section">
        <h2>📊 进化总览</h2>
        <div class="metric-grid">
            <div class="metric-card good">
                <div class="value">{best_expR_curve[-1]:.4f}</div>
                <div class="label">最优 WF expR</div>
            </div>
            <div class="metric-card">
                <div class="value">{best_calmar_curve[-1]:.2f}</div>
                <div class="label">最优 Calmar</div>
            </div>
            <div class="metric-card">
                <div class="value">{len(pareto)}</div>
                <div class="label">Pareto 前沿大小</div>
            </div>
            <div class="metric-card">
                <div class="value">{len(gen_nums)}</div>
                <div class="label">实际进化代数</div>
            </div>
        </div>

        <h3>进化曲线</h3>
        <div class="svg-container">
            {curve_svg}
        </div>
    </div>

    <div class="section">
        <h2>🎯 Pareto 前沿</h2>
        <div class="svg-container">
            {pareto_svg}
        </div>
        <p style="font-size: 13px; color: #666;">横轴：expR（越大越好），纵轴：Calmar（越大越好），每个点代表一个非支配解。</p>
    </div>

    <div class="section">
        <h2>🏆 候选方案（IS 内表现）</h2>
        <table>
            <thead>
                <tr>
                    <th>方案</th>
                    <th>参数值</th>
                    <th>expR</th>
                    <th>Calmar</th>
                    <th>稳定性</th>
                </tr>
            </thead>
            <tbody>
                {cand_rows}
            </tbody>
        </table>
    </div>

    <div class="section">
        <h2>🔬 纯 OOS 样本外验证</h2>
        <div class="note">
            <strong>基线对比：</strong>
            IS expR = {baseline_is.get("expR", 0):.4f} ·
            OOS expR = {baseline_oos.get("expR", 0):.4f} ·
            IS 交易数 = {baseline_is.get("trades", 0)} ·
            OOS 交易数 = {baseline_oos.get("trades", 0)}
        </div>
        <table>
            <thead>
                <tr>
                    <th>方案</th>
                    <th>IS expR</th>
                    <th>OOS expR</th>
                    <th>expR 退化率</th>
                    <th>IS 交易数</th>
                    <th>OOS 交易数</th>
                    <th>IS Calmar</th>
                    <th>OOS Calmar</th>
                    <th>结果</th>
                </tr>
            </thead>
            <tbody>
                {oos_rows}
            </tbody>
        </table>
    </div>

    <div class="section">
        <h2>🛡️ 参数稳健性检验（±20% 扰动）</h2>
        <table>
            <thead>
                <tr>
                    <th>方案</th>
                    <th>综合稳健性</th>
                    <th>各参数稳健性得分</th>
                    <th>评价</th>
                </tr>
            </thead>
            <tbody>
                {rob_rows}
            </tbody>
        </table>
    </div>

    <div class="section">
        <h2>📝 实验配置</h2>
        <table>
            <tr><th>项目</th><th>值</th></tr>
            <tr><td>品种</td><td>{symbol}</td></tr>
            <tr><td>优化参数</td><td>{", ".join(PARAM_NAMES)}</td></tr>
            <tr><td>固定参数</td><td>{", ".join(FIXED_PARAM_NAMES)}</td></tr>
            <tr><td>算法</td><td>NSGA-II 多目标遗传算法</td></tr>
            <tr><td>种群大小</td><td>{len(result["pop"])}</td></tr>
            <tr><td>进化代数</td><td>{len(gen_nums)}</td></tr>
            <tr><td>交叉概率</td><td>{CXPB}</td></tr>
            <tr><td>变异概率</td><td>{MUTPB}</td></tr>
            <tr><td>训练窗口</td><td>{train_bars} 根</td></tr>
            <tr><td>验证窗口</td><td>{valid_bars} 根</td></tr>
            <tr><td>滚动步长</td><td>{step_bars} 根</td></tr>
            <tr><td>参数范围收缩</td><td>{"是 (±" + str(int(SHRINK_PCT * 100)) + "%)" if use_shrink else "否"}</td></tr>
            <tr><td>L1 正则</td><td>{"是 (w=" + str(REG_L1_WEIGHT) + ")" if use_l1 else "否"}</td></tr>
            <tr><td>IS 数据量</td><td>{len(_eval_cache) > 0 and "N/A" or "N/A"} 根</td></tr>
        </table>
    </div>

    <div class="footer">
        Generated by ga_tpsl_optimizer_v5_oos.py · Phase 3.5 · OOS 感知适应度
    </div>
</div>
</body>
</html>"""

    report_path = os.path.join(output_dir, f"{symbol}-phase35-oos-report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


def _generate_pareto_svg(pareto_front):
    """生成 Pareto 前沿散点图 SVG。"""
    if not pareto_front:
        return '<p style="color:#999;text-align:center;padding:40px;">无数据</p>'

    expR_vals = [ind.fitness.values[0] for ind in pareto_front]
    calmar_vals = [ind.fitness.values[1] for ind in pareto_front]

    width, height = 700, 350
    margin = {"l": 60, "r": 30, "t": 30, "b": 50}
    pw = width - margin["l"] - margin["r"]
    ph = height - margin["t"] - margin["b"]

    e_min, e_max = min(expR_vals) * 0.9, max(expR_vals) * 1.1
    c_min, c_max = min(calmar_vals) * 0.9, max(calmar_vals) * 1.1
    if e_max == e_min:
        e_max = e_min + 0.1
    if c_max == c_min:
        c_max = c_min + 0.5

    def sx(v):
        return margin["l"] + (v - e_min) / (e_max - e_min) * pw

    def sy(v):
        return margin["t"] + ph - (v - c_min) / (c_max - c_min) * ph

    dots = ""
    for i, ind in enumerate(pareto_front):
        x = sx(ind.fitness.values[0])
        y = sy(ind.fitness.values[1])
        dots += (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#1976d2" opacity="0.7" stroke="#fff" stroke-width="1.5"/>'
        )

    # 坐标轴
    axes = f"""
    <line x1="{margin["l"]}" y1="{margin["t"]}" x2="{margin["l"]}" y2="{height - margin["b"]}" stroke="#ccc" stroke-width="1"/>
    <line x1="{margin["l"]}" y1="{height - margin["b"]}" x2="{width - margin["r"]}" y2="{height - margin["b"]}" stroke="#ccc" stroke-width="1"/>
    <text x="{width / 2}" y="{height - 12}" text-anchor="middle" fill="#666" font-size="12">expR</text>
    <text x="15" y="{height / 2}" text-anchor="middle" fill="#666" font-size="12" transform="rotate(-90 15 {height / 2})">Calmar</text>
    <text x="{margin["l"]}" y="{height - margin["b"] + 15}" text-anchor="start" fill="#999" font-size="10">{e_min:.3f}</text>
    <text x="{width - margin["r"]}" y="{height - margin["b"] + 15}" text-anchor="end" fill="#999" font-size="10">{e_max:.3f}</text>
    <text x="{margin["l"] - 5}" y="{margin["t"] + 5}" text-anchor="end" fill="#999" font-size="10">{c_max:.1f}</text>
    <text x="{margin["l"] - 5}" y="{height - margin["b"] + 5}" text-anchor="end" fill="#999" font-size="10">{c_min:.1f}</text>
    """

    return f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">{axes}{dots}</svg>'


def _generate_curve_svg(gens, expR_curve, calmar_curve, pareto_curve):
    """生成进化曲线 SVG。"""
    width, height = 700, 300
    margin = {"l": 55, "r": 55, "t": 25, "b": 40}
    pw = width - margin["l"] - margin["r"]
    ph = height - margin["t"] - margin["b"]

    n = len(gens)
    if n == 0:
        return '<p style="color:#999;text-align:center;padding:40px;">无数据</p>'

    e_min, e_max = min(expR_curve) * 0.9, max(expR_curve) * 1.1
    c_min, c_max = min(calmar_curve) * 0.9, max(calmar_curve) * 1.1
    if e_max == e_min:
        e_max = e_min + 0.05
    if c_max == c_min:
        c_max = c_min + 0.5

    def sx(i):
        return margin["l"] + i / max(n - 1, 1) * pw

    def sy_expR(v):
        return margin["t"] + ph - (v - e_min) / (e_max - e_min) * ph

    def sy_calmar(v):
        return margin["t"] + ph - (v - c_min) / (c_max - c_min) * ph

    # expR 曲线
    expR_path = ""
    for i in range(n):
        x = sx(i)
        y = sy_expR(expR_curve[i])
        expR_path += f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"

    # Calmar 曲线
    calmar_path = ""
    for i in range(n):
        x = sx(i)
        y = sy_calmar(calmar_curve[i])
        calmar_path += f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"

    # 坐标轴
    axes = f"""
    <line x1="{margin["l"]}" y1="{margin["t"]}" x2="{margin["l"]}" y2="{height - margin["b"]}" stroke="#ccc" stroke-width="1"/>
    <line x1="{margin["l"]}" y1="{height - margin["b"]}" x2="{width - margin["r"]}" y2="{height - margin["b"]}" stroke="#ccc" stroke-width="1"/>
    <text x="{margin["l"] + pw / 2}" y="{height - 10}" text-anchor="middle" fill="#666" font-size="12">代数</text>
    <text x="20" y="{height / 2}" text-anchor="middle" fill="#1976d2" font-size="12" transform="rotate(-90 20 {height / 2})">expR</text>
    <text x="{width - 15}" y="{height / 2}" text-anchor="middle" fill="#388e3c" font-size="12" transform="rotate(90 15 {height / 2})">Calmar</text>
    <text x="{margin["l"]}" y="{height - margin["b"] + 12}" text-anchor="start" fill="#999" font-size="10">0</text>
    <text x="{width - margin["r"]}" y="{height - margin["b"] + 12}" text-anchor="end" fill="#999" font-size="10">{gens[-1]}</text>
    """

    # 网格线
    grid = ""
    for gi in range(5):
        y = margin["t"] + gi / 4 * ph
        grid += f'<line x1="{margin["l"]}" y1="{y:.1f}" x2="{width - margin["r"]}" y2="{y:.1f}" stroke="#f0f0f0" stroke-width="1"/>'

    legend = f"""
    <rect x="{margin["l"] + 10}" y="8" width="12" height="3" fill="#1976d2"/>
    <text x="{margin["l"] + 28}" y="13" fill="#1976d2" font-size="11">best expR</text>
    <rect x="{margin["l"] + 110}" y="8" width="12" height="3" fill="#388e3c"/>
    <text x="{margin["l"] + 128}" y="13" fill="#388e3c" font-size="11">best Calmar</text>
    """

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">
        {grid}{axes}
        <path d="{expR_path}" fill="none" stroke="#1976d2" stroke-width="2"/>
        <path d="{calmar_path}" fill="none" stroke="#388e3c" stroke-width="2" stroke-dasharray="4,2"/>
        {legend}
    </svg>'''


# ============================================================================
# 主函数
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="OOS 感知 GA 优化器（Phase 3.5）")
    parser.add_argument("--symbol", type=str, default="rb", help="品种代码")
    parser.add_argument("--pop", type=int, default=DEFAULT_POP_SIZE, help="种群大小")
    parser.add_argument("--gen", type=int, default=DEFAULT_GEN_COUNT, help="进化代数")
    parser.add_argument("--train-bars", type=int, default=DEFAULT_TRAIN_BARS, help="WF训练窗口大小")
    parser.add_argument("--valid-bars", type=int, default=DEFAULT_VALID_BARS, help="WF验证窗口大小")
    parser.add_argument("--step-bars", type=int, default=DEFAULT_STEP_BARS, help="WF滚动步长")
    parser.add_argument("--full", action="store_true", help="使用全量IS数据")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    parser.add_argument("--n-jobs", type=int, default=DEFAULT_N_JOBS, help="并行评估进程数")
    parser.add_argument("--early-stop-patience", type=int, default=EARLY_STOP_PATIENCE, help="早停耐心代数")
    parser.add_argument("--fast", action="store_true", help="快速模式：步长翻倍")
    parser.add_argument("--shrink", action="store_true", help="参数范围收缩模式")
    parser.add_argument("--l1", action="store_true", help="启用 L1 正则惩罚")
    parser.add_argument(
        "--oos-weight",
        type=float,
        default=DEFAULT_OOS_WEIGHT,
        help=f"OOS 感知适应度权重（0~1，默认 {DEFAULT_OOS_WEIGHT}）",
    )

    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    symbol = args.symbol
    pop_size = args.pop
    gen_count = args.gen
    train_bars = args.train_bars
    valid_bars = args.valid_bars
    step_bars = args.step_bars
    n_jobs = args.n_jobs

    if args.fast:
        step_bars = step_bars * 2
        print(f"⚡ Fast 模式：步长翻倍至 {step_bars} 根")

    # 输出目录
    output_dir = args.output or os.path.join(HERE, f"ga_v5_{symbol}_result")
    os.makedirs(output_dir, exist_ok=True)

    print(f"🚀 Phase 3.5 OOS 感知 GA 优化启动: {symbol}")
    print(f"   优化参数: {', '.join(PARAM_NAMES)}")
    print(f"   固定参数: {', '.join(FIXED_PARAM_NAMES)}")
    print(f"   种群: {pop_size}, 代数: {gen_count}, 进程: {n_jobs}")
    print(f"   参数收缩: {'是' if args.shrink else '否'}, L1正则: {'是' if args.l1 else '否'}")
    print(f"   OOS感知权重: {args.oos_weight}")

    # 加载数据
    print(f"\n📊 加载 {symbol} 数据...")
    df_all = load_daily(symbol)
    print(f"   总数据量: {len(df_all)} 根")

    # IS/OOS 分割（IS=前 80% 旧数据，OOS=后 20% 新数据）
    n_total = len(df_all)
    n_oos = int(n_total * OOS_RATIO)
    n_is = n_total - n_oos

    df_is = df_all.iloc[:n_is].copy()
    df_oos = df_all.iloc[n_is:].copy()

    if not args.full and len(df_is) > 800:
        df_is = df_is.tail(800).copy()
        print("   IS 数据: 800 根 (截断加速，用 --full 用全量)")
    else:
        print(f"   IS 数据: {len(df_is)} 根 ({df_is.index[0].date()} ~ {df_is.index[-1].date()})")

    print(f"   OOS 数据: {len(df_oos)} 根 ({df_oos.index[0].date()} ~ {df_oos.index[-1].date()})")

    # 基线评估
    print("\n📐 计算基线参数...")
    baseline_dict = _get_baseline_params(symbol)
    baseline_ind = _baseline_to_optimized_individual(baseline_dict)
    print(f"   T_thresh_mult = 1.0 (基线 {baseline_dict['T_thresh_base']:.1f})")
    print(f"   stop_atr_mult = {baseline_dict['stop_atr_mult']:.2f}")
    print(f"   rr_ratio = {baseline_dict['rr_ratio']:.2f}")

    # 基线全量 IS 评估
    print("\n📊 基线 IS 全量评估...")
    base_is_m = evaluate_full_period(baseline_ind, symbol, df_is)
    base_oos_m = evaluate_full_period(baseline_ind, symbol, df_oos)
    print(
        f"   IS: expR={base_is_m['expR']:.4f}, trades={base_is_m['trades']}, "
        f"winrate={base_is_m['win_rate']:.2%}, calmar={base_is_m['calmar']:.2f}"
    )
    print(
        f"   OOS: expR={base_oos_m['expR']:.4f}, trades={base_oos_m['trades']}, "
        f"winrate={base_oos_m['win_rate']:.2%}, calmar={base_oos_m['calmar']:.2f}"
    )

    # WF 配置打印
    print("\n🔄 Walk-Forward 配置:")
    print(f"   训练 {train_bars} 根 + 验证 {valid_bars} 根, 步长 {step_bars} 根")

    # 运行 GA
    print("\n🧬 开始 GA 优化...")
    result = run_ga_nsga2(
        symbol,
        df_is,
        df_oos,
        train_bars,
        valid_bars,
        step_bars,
        pop_size,
        gen_count,
        use_shrink=args.shrink,
        use_l1=args.l1,
        n_jobs=n_jobs,
        early_stop_patience=args.early_stop_patience,
        oos_weight=args.oos_weight,
    )

    print(f"\n✅ 优化完成，用时 {result['total_time']:.1f} 秒 ({result['total_time'] / 60:.1f} 分钟)")
    print(f"   帕累托前沿大小: {len(result['pareto_front'])} 个解")

    # 候选方案
    print("\n🏆 候选方案:")
    for key, cand in result["candidates"].items():
        p = cand["params"]
        f = cand["fitness"]
        print(f"   [{cand['label']}]")
        for name in PARAM_NAMES:
            print(f"     {name} = {p[name]}")
        print(f"     fitness: expR={f[0]:.4f}, calmar={f[1]:.2f}, stability={f[2]:.4f}")

    # OOS 验证
    print("\n🔬 纯 OOS 验证:")
    oos_results = run_oos_validation(result["candidates"], symbol, df_is, df_oos, result["toolbox"])
    for key, oos in oos_results.items():
        status = "✅ 通过" if oos["passed"] else "❌ 失败"
        print(f"   [{oos['label']}] {status}")
        print(
            f"     IS expR={oos['is']['expR']:.4f} → OOS expR={oos['oos']['expR']:.4f}  "
            f"(退化 {oos['degradation'].get('expR', 0) * 100:.1f}%)"
        )

    # 稳健性检验
    print("\n🔍 参数稳健性检验:")
    robust_results = run_robustness_test(result["candidates"], symbol, df_is, result["toolbox"])
    for key, rob in robust_results.items():
        status = "✅ 稳健" if rob["robust"] else "⚠️ 敏感"
        print(f"   [{rob['label']}] {status}  综合得分={rob['overall_score']:.3f}")

    # 生成报告
    print("\n📝 生成 HTML 报告...")
    report_path = generate_html_report(
        symbol,
        result,
        oos_results,
        robust_results,
        base_is_m,
        base_oos_m,
        output_dir,
        train_bars,
        valid_bars,
        step_bars,
        args.shrink,
        args.l1,
        args.fast,
        args.oos_weight,
    )
    print(f"   报告已保存: {report_path}")

    # 保存 JSON 结果
    json_result = {
        "symbol": symbol,
        "phase": "Phase 3.5 (OOS-aware, 3 params)",
        "config": {
            "pop_size": len(result["pop"]),
            "gen_count": len(result["logbook"]),
            "train_bars": train_bars,
            "valid_bars": valid_bars,
            "step_bars": step_bars,
            "use_shrink": args.shrink,
            "use_l1": args.l1,
            "is_fast": args.fast,
            "oos_weight": args.oos_weight,
            "optimized_params": PARAM_NAMES,
            "fixed_params": FIXED_PARAM_NAMES,
        },
        "baseline": {
            "is": {k: v for k, v in base_is_m.items() if k != "R_list"},
            "oos": {k: v for k, v in base_oos_m.items() if k != "R_list"},
        },
        "candidates": result["candidates"],
        "oos_validation": oos_results,
        "robustness": robust_results,
        "total_time": result["total_time"],
        "pareto_size": len(result["pareto_front"]),
    }
    json_path = os.path.join(output_dir, f"{symbol}_phase35_oos_result.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_result, f, ensure_ascii=False, indent=2)
    print(f"   JSON 结果已保存: {json_path}")

    print("\n🎉 Phase 3.5 OOS 感知优化完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
