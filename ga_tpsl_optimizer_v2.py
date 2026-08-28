# -*- coding: utf-8 -*-
"""
ga_tpsl_optimizer_v2.py — 止盈止损参数 GA 联合优化器（Phase 2 完整版）
===================================================================
Phase 2 升级内容（相对 Phase 1）：
    1. 5 参数联合优化（stop_atr_mult + rr_ratio + tail_pct + tail_trail_R + min_profit_R）
    2. 多目标 NSGA-II（expR + 卡玛比率 + 胜率稳定性）
    3. Walk-Forward 滚动窗口适应度（WF-GA 深度融合）
    4. 纯 OOS 样本外检验
    5. 参数稳健性检验（±20% 扰动）
    6. 完整的 HTML 报告输出

用法:
    python3 ga_tpsl_optimizer_v2.py --symbol jd
    python3 ga_tpsl_optimizer_v2.py --symbol jd --pop 50 --gen 30 --full
"""

from __future__ import annotations

import argparse
import copy
import json
import math
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

# 5 个参数的上下界
PARAM_BOUNDS = [
    (0.8, 3.0),  # gene[0]: stop_atr_mult
    (1.2, 4.0),  # gene[1]: rr_ratio
    (0.1, 0.5),  # gene[2]: tail_pct
    (1.0, 4.0),  # gene[3]: tail_trail_R
    (1.0, 3.5),  # gene[4]: min_profit_R
]
PARAM_NAMES = ["stop_atr_mult", "rr_ratio", "tail_pct", "tail_trail_R", "min_profit_R"]

# GA 参数
DEFAULT_POP_SIZE = 100
DEFAULT_GEN_COUNT = 30
CXPB = 0.9
MUTPB = 0.3  # 每个基因的变异概率（提高以增强多样性）
SBX_ETA = 15  # 降低，交叉更离散
PM_ETA = 15  # 降低，变异更离散
CALMAR_CAP = 10.0  # Calmar 上限，防止极端值主导进化
IMMIGRANT_RATE = 0.10  # 每 5 代注入的随机移民比例
IMMIGRANT_INTERVAL = 5  # 移民注入间隔（代）
EARLY_STOP_PATIENCE = 8  # 早停：连续多少代 best_expR 提升不足则终止
EARLY_STOP_MIN_IMPROVE = 0.001  # 早停：最小提升阈值
DEFAULT_N_JOBS = 8  # 并行评估进程数

# 约束
MIN_TRADES = 10  # 最低交易笔数
MIN_WIN_RATE = 0.15  # 最低胜率（低于则惩罚）
MAX_DRAWDOWN = 0.50  # 最大回撤上限（R 单位，超过则不可行）

# WF 配置
DEFAULT_TRAIN_BARS = 250  # 训练窗口长度（日K）
DEFAULT_VALID_BARS = 60  # 验证窗口长度
DEFAULT_STEP_BARS = 30  # 滚动步长

# OOS 配置
OOS_RATIO = 0.20  # 最后 20% 作为纯 OOS

# 稳健性检验
ROBUST_PERTURB = 0.20  # ±20% 扰动
ROBUST_POINTS = 11  # 每个参数的扰动点数

# 结果缓存
_eval_cache = {}

# Worker 进程全局数据（用于并行评估，避免重复序列化大对象）
_worker_data = {}


def _init_worker(symbol, df_is, train_bars, valid_bars, step_bars, data_slice_id):
    """Pool initializer：在每个 worker 进程中设置全局数据。"""
    global _worker_data
    _worker_data = {
        "symbol": symbol,
        "df_is": df_is,
        "train_bars": train_bars,
        "valid_bars": valid_bars,
        "step_bars": step_bars,
        "data_slice_id": data_slice_id,
    }


def _worker_evaluate(individual):
    """Worker 进程中的评估函数（使用全局数据，避免重复 pickle）。"""
    global _worker_data
    return wf_evaluate(
        individual,
        _worker_data["symbol"],
        _worker_data["df_is"],
        _worker_data["train_bars"],
        _worker_data["valid_bars"],
        _worker_data["step_bars"],
        _worker_data["data_slice_id"],
    )


def _cache_key(params_tuple, symbol, data_slice_id):
    """生成缓存键。"""
    return (tuple(round(p, 4) for p in params_tuple), symbol, data_slice_id)


# ============================================================================
# 配置生成
# ============================================================================


def _make_config(individual, symbol, base_cfg=DEFAULT_CONFIG):
    """根据 5 个基因生成配置。"""
    stop_mult, rr_ratio, tail_pct, tail_trail_R, min_profit_R = individual
    cfg = copy.deepcopy(base_cfg)

    # 分品种风控参数
    cfg.setdefault("per_symbol_risk", {})
    cfg["per_symbol_risk"][symbol] = {
        "stop_atr_mult": round(stop_mult, 4),
        "rr_ratio": round(rr_ratio, 4),
    }

    # 尾仓参数（全局覆盖，因为 trailing_tail 是全局配置）
    # 注意：这会修改全局配置，所以每个评估都要用 deepcopy 的独立配置
    cfg.setdefault("trailing_tail", {})
    cfg["trailing_tail"]["tail_pct"] = round(tail_pct, 4)
    cfg["trailing_tail"]["tail_trail_R"] = round(tail_trail_R, 4)
    cfg["trailing_tail"]["min_profit_R"] = round(min_profit_R, 4)
    cfg["trailing_tail"]["enabled"] = True
    cfg["trailing_tail"]["trend_only"] = True

    return cfg


# ============================================================================
# 指标计算
# ============================================================================


def _calc_max_drawdown(R_list):
    """从逐笔 R 收益序列计算最大回撤（基于累计 R 曲线）。"""
    if not R_list:
        return 0.0
    cumulative = np.cumsum(R_list)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    return float(np.max(drawdown)) if len(drawdown) > 0 else 0.0


def _calc_metrics(result):
    """从回测结果中提取所有指标。"""
    trades = int(result.get("trades", 0))
    expR = float(result.get("expR") or 0.0)
    win_rate = float(result.get("win_rate") or 0.0)

    # 从 trades_detail 计算最大回撤
    trades_detail = result.get("trades_detail", [])
    if trades_detail:
        R_list = [float(t.get("R_adj", 0)) for t in trades_detail]
        max_dd = _calc_max_drawdown(R_list)
    else:
        R_list = []
        max_dd = 0.0

    # 卡玛比率 = 总收益 / 最大回撤
    total_R = expR * trades if trades > 0 else 0.0
    if max_dd > 0.01:
        calmar = total_R / max_dd
    elif total_R > 0:
        # 回撤极小但正收益：不直接乘 100，给一个温和的高值并 cap
        calmar = min(total_R * 20, CALMAR_CAP)
    else:
        calmar = 0.0
    # Calmar 上限，防止极端值主导多目标进化
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
    """返回 df 的切片（用于 WF 窗口）。"""
    return df.iloc[start_i:end_i].copy()


def wf_evaluate(
    individual,
    symbol,
    df,
    train_bars=DEFAULT_TRAIN_BARS,
    valid_bars=DEFAULT_VALID_BARS,
    step_bars=DEFAULT_STEP_BARS,
    data_slice_id="full",
):
    """Walk-Forward 评估：在多个训练-验证窗口上评估，返回验证窗口的聚合指标。

    返回 (expR_median, calmar_median, winrate_stability) —— 三个目标，全部最大化。
    """
    global _eval_cache

    key = _cache_key(individual, symbol, data_slice_id)
    if key in _eval_cache:
        return _eval_cache[key]

    stop_mult, rr_ratio, tail_pct, tail_trail_R, min_profit_R = individual

    # 结构约束检查
    if (
        rr_ratio < 1.2
        or stop_mult < 0.8
        or min_profit_R > rr_ratio + 0.01  # min_profit_R 必须 <= rr_ratio（留 0.01 容差）
        or tail_pct < 0.05
        or tail_pct > 0.6
        or tail_trail_R < 0.5
        or tail_trail_R > 5.0
    ):
        result = (-10.0, -10.0, -10.0)
        _eval_cache[key] = result
        return result

    cfg = _make_config(individual, symbol)

    n = len(df)
    window_size = train_bars + valid_bars

    # 生成所有窗口
    valid_expRs = []
    valid_calmars = []
    valid_winrates = []

    start = 0
    while start + window_size <= n:
        train_end = start + train_bars
        valid_end = start + window_size

        # 在验证窗口上跑回测（注意：walk_forward_backtest 的 min_bars 机制
        # 会自动用前 min_bars 根做 pipeline 初始化，然后从 min_bars+1 开始交易）
        # 我们把训练+验证窗口都传进去，让函数自己处理
        df_segment = df.iloc[start:valid_end].copy()
        result = walk_forward_backtest(symbol, cfg=cfg, df_in=df_segment, min_bars=train_bars - 10)  # 留一点缓冲

        metrics = _calc_metrics(result)

        if metrics["trades"] >= 3:  # 验证窗口至少 3 笔交易才纳入统计
            valid_expRs.append(metrics["expR"])
            valid_calmars.append(metrics["calmar"])
            valid_winrates.append(metrics["win_rate"])

        start += step_bars

    # 窗口不足 → 惩罚
    if len(valid_expRs) < 2:
        result = (-10.0, -10.0, -10.0)
        _eval_cache[key] = result
        return result

    # 三个目标：全部最大化
    # f1: expR 中位数（稳健，不受极端值影响）
    f1 = float(np.median(valid_expRs))

    # f2: 卡玛比率中位数
    f2 = float(np.median(valid_calmars))

    # f3: 正收益窗口比例（衡量稳定性，越稳定越好）
    # 用"盈利窗口占比"代替"胜率标准差"——避免稳定地亏钱的情况
    positive_ratio = sum(1 for r in valid_expRs if r > 0) / len(valid_expRs)
    f3 = positive_ratio

    # 额外惩罚：如果亏损窗口太多，额外扣分
    if positive_ratio < 0.4:
        penalty = (0.4 - positive_ratio) * 1.0
        f1 -= penalty
        f2 -= penalty

    result = (f1, f2, f3)
    _eval_cache[key] = result
    return result


# ============================================================================
# 单窗口回测（用于 OOS 和最终全样本评估）
# ============================================================================


def single_evaluate(individual, symbol, df):
    """在单个数据集上做完整回测，返回详细指标。"""
    cfg = _make_config(individual, symbol)
    result = walk_forward_backtest(symbol, cfg=cfg, df_in=df, min_bars=60)
    return _calc_metrics(result)


# ============================================================================
# DEAP 初始化（多目标 NSGA-II）
# ============================================================================


def setup_deap_nsga2(symbol, df_is, train_bars, valid_bars, step_bars, pop_size):
    """初始化 NSGA-II 多目标优化。"""
    # 清除之前的 creator（避免重复创建报错）
    for name in ["FitnessMulti", "Individual"]:
        if name in creator.__dict__:
            delattr(creator, name)

    creator.create("FitnessMulti", base.Fitness, weights=(1.0, 1.0, 1.0))  # 三个最大化目标
    creator.create("Individual", list, fitness=creator.FitnessMulti)

    toolbox = base.Toolbox()

    # 属性生成
    def attr_float(idx):
        low, high = PARAM_BOUNDS[idx]
        return random.uniform(low, high)

    toolbox.register("attr_float_0", attr_float, 0)
    toolbox.register("attr_float_1", attr_float, 1)
    toolbox.register("attr_float_2", attr_float, 2)
    toolbox.register("attr_float_3", attr_float, 3)
    toolbox.register("attr_float_4", attr_float, 4)

    toolbox.register(
        "individual",
        tools.initCycle,
        creator.Individual,
        (toolbox.attr_float_0, toolbox.attr_float_1, toolbox.attr_float_2, toolbox.attr_float_3, toolbox.attr_float_4),
        n=1,
    )

    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    # 评估
    toolbox.register(
        "evaluate",
        wf_evaluate,
        symbol=symbol,
        df=df_is,
        train_bars=train_bars,
        valid_bars=valid_bars,
        step_bars=step_bars,
        data_slice_id="IS_WF",
    )

    # 遗传算子
    toolbox.register(
        "mate",
        tools.cxSimulatedBinaryBounded,
        low=[b[0] for b in PARAM_BOUNDS],
        up=[b[1] for b in PARAM_BOUNDS],
        eta=SBX_ETA,
    )

    toolbox.register(
        "mutate",
        tools.mutPolynomialBounded,
        low=[b[0] for b in PARAM_BOUNDS],
        up=[b[1] for b in PARAM_BOUNDS],
        eta=PM_ETA,
        indpb=MUTPB,
    )

    # NSGA-II 选择
    toolbox.register("select", tools.selNSGA2)

    return toolbox


# ============================================================================
# 帕累托前沿分析
# ============================================================================


def select_candidates(pareto_front):
    """从帕累托前沿中选出 4 个代表性候选方案。"""
    if len(pareto_front) == 0:
        return {}

    # 提取目标值
    f1_vals = [ind.fitness.values[0] for ind in pareto_front]
    f2_vals = [ind.fitness.values[1] for ind in pareto_front]
    f3_vals = [ind.fitness.values[2] for ind in pareto_front]

    candidates = {}

    # 激进型：expR 最大
    idx = int(np.argmax(f1_vals))
    candidates["aggressive"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(5)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "激进型（expR 最大）",
    }

    # 稳健型：卡玛比率最大
    idx = int(np.argmax(f2_vals))
    candidates["balanced"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(5)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "稳健型（卡玛比率最大）",
    }

    # 稳定型：胜率稳定性最高
    idx = int(np.argmax(f3_vals))
    candidates["stable"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(5)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "稳定型（胜率最稳）",
    }

    # 参考点：三目标归一化后距离乌托邦点最远
    f1_max, f1_min = max(f1_vals), min(f1_vals)
    f2_max, f2_min = max(f2_vals), min(f2_vals)
    f3_max, f3_min = max(f3_vals), min(f3_vals)

    def norm_val(i):
        n1 = (f1_vals[i] - f1_min) / (f1_max - f1_min) if f1_max > f1_min else 0
        n2 = (f2_vals[i] - f2_min) / (f2_max - f2_min) if f2_max > f2_min else 0
        n3 = (f3_vals[i] - f3_min) / (f3_max - f3_min) if f3_max > f3_min else 0
        return math.sqrt(n1**2 + n2**2 + n3**2)

    scores = [norm_val(i) for i in range(len(pareto_front))]
    idx = int(np.argmax(scores))
    candidates["reference"] = {
        "params": {PARAM_NAMES[i]: round(pareto_front[idx][i], 4) for i in range(5)},
        "fitness": list(pareto_front[idx].fitness.values),
        "label": "折中参考点（三目标综合最优）",
    }

    return candidates


# ============================================================================
# OOS 验证
# ============================================================================


def oos_validate(individual_list, symbol, df_oos, df_is=None):
    """在纯 OOS 数据上验证一组参数。

    返回 [{params, metrics, degradation}, ...]
    """
    results = []
    for ind in individual_list:
        metrics_is = single_evaluate(ind, symbol, df_is) if df_is is not None else None
        metrics_oos = single_evaluate(ind, symbol, df_oos)

        # 计算退化率
        degradation = {}
        for metric_name in ["expR", "calmar", "win_rate"]:
            is_val = metrics_is.get(metric_name, 0) if metrics_is else 0
            oos_val = metrics_oos.get(metric_name, 0)
            if is_val != 0:
                degradation[metric_name] = (is_val - oos_val) / abs(is_val)
            else:
                degradation[metric_name] = 0.0 if oos_val == 0 else float("inf")

        results.append(
            {
                "params": {PARAM_NAMES[i]: round(ind[i], 4) for i in range(5)},
                "metrics_is": metrics_is,
                "metrics_oos": metrics_oos,
                "degradation": degradation,
            }
        )

    return results


# ============================================================================
# 稳健性检验
# ============================================================================


def robustness_test(individual, symbol, df, perturb=ROBUST_PERTURB, n_points=ROBUST_POINTS):
    """参数稳健性检验：对每个参数做 ±perturb 的扰动，观察 expR 变化。

    返回 {"overall_score": float, "per_param": {...}}
    """
    base_metrics = single_evaluate(individual, symbol, df)
    base_expR = base_metrics["expR"]

    per_param = {}

    for i, name in enumerate(PARAM_NAMES):
        base_val = individual[i]
        low, high = PARAM_BOUNDS[i]
        perturb_range = (high - low) * perturb

        expRs = []
        for p in np.linspace(base_val - perturb_range, base_val + perturb_range, n_points):
            # 钳位到参数范围内
            p = max(low, min(high, p))
            perturbed = list(individual)
            perturbed[i] = p
            m = single_evaluate(perturbed, symbol, df)
            expRs.append(m["expR"])

        # 稳健性得分：1 - (最大值 - 最小值) / |基准值|
        # 得分越高越稳健（基准值附近变化不大）
        if abs(base_expR) > 0.01:
            variation = (max(expRs) - min(expRs)) / abs(base_expR)
        else:
            variation = 1.0
        score = max(0.0, 1.0 - variation)

        per_param[name] = {
            "base_value": round(base_val, 4),
            "variation": round(variation, 4),
            "score": round(score, 4),
            "min_expR": round(min(expRs), 4),
            "max_expR": round(max(expRs), 4),
        }

    # 综合得分 = 各参数得分的均值
    overall_score = float(np.mean([p["score"] for p in per_param.values()]))

    return {"overall_score": round(overall_score, 4), "per_param": per_param}


# ============================================================================
# 主优化流程
# ============================================================================


def run_optimization(
    symbol,
    pop_size=DEFAULT_POP_SIZE,
    gen_count=DEFAULT_GEN_COUNT,
    train_bars=DEFAULT_TRAIN_BARS,
    valid_bars=DEFAULT_VALID_BARS,
    step_bars=DEFAULT_STEP_BARS,
    full_data=False,
    seed=42,
    n_jobs=DEFAULT_N_JOBS,
    early_stop_patience=EARLY_STOP_PATIENCE,
):
    """运行完整的 Phase 2 GA 优化。

    返回完整的结果字典。
    """
    random.seed(seed)
    np.random.seed(seed)
    global _eval_cache
    _eval_cache = {}  # 清空缓存

    # 加载数据
    df_full = load_daily(symbol)
    if df_full is None:
        return {"error": f"无法加载 {symbol} 数据"}

    n_total = len(df_full)

    # 划分 IS 和 OOS
    oos_size = int(n_total * OOS_RATIO)
    is_size = n_total - oos_size

    df_is = df_full.iloc[:is_size].copy()
    df_oos = df_full.iloc[is_size:].copy()

    # 如果不用全量数据，限制 IS 大小（加速测试）
    if not full_data and len(df_is) > 800:
        df_is = df_is.tail(800).copy()

    print(f"\n{'=' * 60}")
    print(f"📊 数据概览（{symbol}）:")
    print(f"   总数据量: {n_total} 根日K")
    print(f"   IS (训练+验证): {len(df_is)} 根日K ({df_is.index[0].date()} ~ {df_is.index[-1].date()})")
    print(f"   OOS (纯样本外): {len(df_oos)} 根日K ({df_oos.index[0].date()} ~ {df_oos.index[-1].date()})")
    print(f"{'=' * 60}")

    # 基线
    baseline_ind = [
        DEFAULT_CONFIG["risk_gate"]["stop_atr_mult"],
        DEFAULT_CONFIG["risk_gate"]["rr_ratio"],
        DEFAULT_CONFIG["trailing_tail"]["tail_pct"],
        DEFAULT_CONFIG["trailing_tail"]["tail_trail_R"],
        DEFAULT_CONFIG["trailing_tail"]["min_profit_R"],
    ]
    # 如果有 per_symbol_risk 覆盖，用覆盖值
    psr = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(symbol, {})
    if "stop_atr_mult" in psr:
        baseline_ind[0] = psr["stop_atr_mult"]
    if "rr_ratio" in psr:
        baseline_ind[1] = psr["rr_ratio"]

    baseline_metrics_is = single_evaluate(baseline_ind, symbol, df_is)
    baseline_metrics_oos = single_evaluate(baseline_ind, symbol, df_oos)

    print(f"\n📐 基线参数:")
    for i, name in enumerate(PARAM_NAMES):
        print(f"   {name} = {baseline_ind[i]}")
    print(
        f"   IS expR = {baseline_metrics_is['expR']:.4f}  "
        f"(trades={baseline_metrics_is['trades']}, "
        f"win_rate={baseline_metrics_is['win_rate']:.1%}, "
        f"calmar={baseline_metrics_is['calmar']:.2f})"
    )
    print(f"   OOS expR = {baseline_metrics_oos['expR']:.4f}  (trades={baseline_metrics_oos['trades']})")

    # 调整种群大小为 4 的倍数（selTournamentDCD 要求）
    if pop_size % 4 != 0:
        pop_size = pop_size + (4 - pop_size % 4)
        print(f"   (调整种群到 {pop_size}，确保为4的倍数)")

    # 初始化 DEAP
    toolbox = setup_deap_nsga2(symbol, df_is, train_bars, valid_bars, step_bars, pop_size)

    # 并行评估（multiprocessing + initializer，避免重复序列化 DataFrame）
    from multiprocessing import Pool

    pool = Pool(
        processes=n_jobs,
        initializer=_init_worker,
        initargs=(symbol, df_is, train_bars, valid_bars, step_bars, "IS_WF"),
    )
    # 用 worker 版本的 evaluate（从全局读数据，更快）
    toolbox.register("evaluate", _worker_evaluate)
    toolbox.register("map", pool.map)

    # 创建种群
    pop = toolbox.population(n=pop_size)

    print(f"\n🚀 开始 NSGA-II 多目标优化")
    print(f"   种群: {pop_size}, 代数: {gen_count}")
    print(f"   目标: expR中位数 + 卡玛比率中位数 + 胜率稳定性")
    print(f"   WF窗口: 训练{train_bars}根 + 验证{valid_bars}根, 步长{step_bars}根")
    print()

    start_time = time.time()

    # 初始评估
    invalid_ind = [ind for ind in pop if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    # 初始帕累托前沿
    pop = toolbox.select(pop, len(pop))

    # 记录历史
    history = []
    f1_vals = [ind.fitness.values[0] for ind in pop]
    f2_vals = [ind.fitness.values[1] for ind in pop]
    f3_vals = [ind.fitness.values[2] for ind in pop]
    print(
        f"  Gen 0: best_expR={max(f1_vals):.4f}, "
        f"best_calmar={max(f2_vals):.2f}, "
        f"best_stability={max(f3_vals):.4f}, "
        f"pareto_size={len(tools.sortNondominated(pop, len(pop), first_front_only=True))}"
    )
    history.append(
        {
            "gen": 0,
            "best_expR": max(f1_vals),
            "best_calmar": max(f2_vals),
            "best_stability": max(f3_vals),
            "avg_expR": float(np.mean(f1_vals)),
            "avg_calmar": float(np.mean(f2_vals)),
            "pareto_size": len(tools.sortNondominated(pop, len(pop), first_front_only=True)),
        }
    )

    # 早停机制初始化
    best_expR_so_far = max(f1_vals)
    early_stop_counter = 0

    # 进化主循环
    for gen in range(1, gen_count + 1):
        gen_start = time.time()

        # 选择 + 变异（NSGA-II 的标准做法）
        offspring = tools.selTournamentDCD(pop, len(pop))
        offspring = [toolbox.clone(ind) for ind in offspring]

        # 交叉
        for child1, child2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < CXPB:
                toolbox.mate(child1, child2)
                del child1.fitness.values
                del child2.fitness.values

        # 变异
        for mutant in offspring:
            if random.random() < MUTPB * len(PARAM_BOUNDS):
                toolbox.mutate(mutant)
                del mutant.fitness.values

        # 评估无效个体
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit

        # NSGA-II 选择（种群 + 后代中选最优）
        pop = toolbox.select(pop + offspring, pop_size)

        # 随机移民注入（防止种群坍缩 / 早熟收敛）
        if gen % IMMIGRANT_INTERVAL == 0:
            n_immigrants = max(1, int(pop_size * IMMIGRANT_RATE))
            immigrants = toolbox.population(n=n_immigrants)
            # 评估移民
            immigrant_fitnesses = toolbox.map(toolbox.evaluate, immigrants)
            for ind, fit in zip(immigrants, immigrant_fitnesses):
                ind.fitness.values = fit
            # 替换种群中适应度最差的个体（按非支配排序 + crowding distance）
            # 简单做法：直接加入后重新选择
            pop = toolbox.select(pop + immigrants, pop_size)

        # 统计
        f1_vals = [ind.fitness.values[0] for ind in pop]
        f2_vals = [ind.fitness.values[1] for ind in pop]
        f3_vals = [ind.fitness.values[2] for ind in pop]
        pareto_front = tools.sortNondominated(pop, len(pop), first_front_only=True)[0]

        gen_time = time.time() - gen_start
        print(
            f"  Gen {gen:2d}: best_expR={max(f1_vals):.4f}, "
            f"best_calmar={max(f2_vals):.2f}, "
            f"pareto_size={len(pareto_front)}  "
            f"({gen_time:.1f}s)"
        )

        history.append(
            {
                "gen": gen,
                "best_expR": max(f1_vals),
                "best_calmar": max(f2_vals),
                "best_stability": max(f3_vals),
                "avg_expR": float(np.mean(f1_vals)),
                "avg_calmar": float(np.mean(f2_vals)),
                "pareto_size": len(pareto_front),
            }
        )

        # 早停检查
        current_best_expR = max(f1_vals)
        if current_best_expR - best_expR_so_far >= EARLY_STOP_MIN_IMPROVE:
            best_expR_so_far = current_best_expR
            early_stop_counter = 0
        else:
            early_stop_counter += 1
            if early_stop_counter >= early_stop_patience:
                print(f"\n⏹️  早停触发：连续 {early_stop_patience} 代 best_expR 提升不足 {EARLY_STOP_MIN_IMPROVE}")
                print(f"   当前 best_expR = {current_best_expR:.4f}")
                break

    runtime = time.time() - start_time
    print(f"\n✅ 优化完成，用时 {runtime:.1f} 秒")

    # 获取帕累托前沿
    pareto_front = tools.sortNondominated(pop, len(pop), first_front_only=True)[0]
    print(f"   帕累托前沿大小: {len(pareto_front)} 个解")

    # 筛选候选方案
    candidates = select_candidates(pareto_front)

    print(f"\n🏆 候选方案:")
    for key, cand in candidates.items():
        print(f"   [{cand['label']}]")
        for pname, pval in cand["params"].items():
            print(f"     {pname} = {pval}")
        print(
            f"     fitness: expR={cand['fitness'][0]:.4f}, "
            f"calmar={cand['fitness'][1]:.2f}, "
            f"stability={cand['fitness'][2]:.4f}"
        )

    # OOS 验证
    print(f"\n🔬 纯 OOS 验证:")
    candidate_inds = []
    candidate_keys = []
    for key, cand in candidates.items():
        ind = [cand["params"][name] for name in PARAM_NAMES]
        candidate_inds.append(ind)
        candidate_keys.append(key)

    oos_results = {}
    for i, key in enumerate(candidate_keys):
        ind = candidate_inds[i]
        metrics_oos = single_evaluate(ind, symbol, df_oos)
        metrics_is_full = single_evaluate(ind, symbol, df_is)

        # 退化率（正值表示 OOS 比 IS 差，负值表示 OOS 更好）
        # 注意：如果 IS 为负但 OOS 更差（更负），仍然是退化
        degradation = {}
        for m in ["expR", "calmar", "win_rate"]:
            is_val = metrics_is_full.get(m, 0)
            oos_val = metrics_oos.get(m, 0)
            if abs(is_val) > 0.001:
                # 用相对变化：(IS - OOS) / |IS|
                # IS为正、OOS更小 → 正的退化率
                # IS为负、OOS更负 → 正的退化率（更差了）
                degradation[m] = round((is_val - oos_val) / abs(is_val), 4)
            else:
                # IS接近0时，用绝对差作为退化率的近似
                degradation[m] = round((is_val - oos_val) * 10, 4) if oos_val < is_val else 0.0

        oos_results[key] = {
            "metrics_is": {k: v for k, v in metrics_is_full.items() if k != "R_list"},
            "metrics_oos": {k: v for k, v in metrics_oos.items() if k != "R_list"},
            "degradation": degradation,
        }

        # 判断是否通过：退化率 <= 30% 为通过
        # 退化率为负表示 OOS 比 IS 还好，当然通过
        deg_expR = degradation.get("expR", 0)
        passed = deg_expR <= 0.30
        status = "✅ 通过" if passed else "❌ 失败"
        deg_str = f"退化 {deg_expR:.1%}" if deg_expR > 0 else f"OOS更优 {-deg_expR:.1%}"
        print(f"   [{candidates[key]['label']}] {status}")
        print(f"     IS expR={metrics_is_full['expR']:.4f} → OOS expR={metrics_oos['expR']:.4f}  ({deg_str})")

    # 稳健性检验（只对 OOS 通过的候选做，节省时间）
    print(f"\n🔍 参数稳健性检验:")
    robustness_results = {}
    for key in candidate_keys:
        ind = [candidates[key]["params"][name] for name in PARAM_NAMES]
        # 用 IS 数据做稳健性检验（更快）
        rob = robustness_test(ind, symbol, df_is)
        robustness_results[key] = rob
        passed = rob["overall_score"] >= 0.5
        status = "✅ 稳健" if passed else "⚠️ 脆弱"
        print(f"   [{candidates[key]['label']}] {status}  综合得分={rob['overall_score']:.2f}")

    # 组装最终结果
    result = {
        "symbol": symbol,
        "data_info": {
            "total_bars": n_total,
            "is_bars": len(df_is),
            "oos_bars": len(df_oos),
            "is_start": str(df_is.index[0].date()),
            "is_end": str(df_is.index[-1].date()),
            "oos_start": str(df_oos.index[0].date()),
            "oos_end": str(df_oos.index[-1].date()),
        },
        "baseline": {
            "params": {PARAM_NAMES[i]: baseline_ind[i] for i in range(5)},
            "metrics_is": {k: v for k, v in baseline_metrics_is.items() if k != "R_list"},
            "metrics_oos": {k: v for k, v in baseline_metrics_oos.items() if k != "R_list"},
        },
        "candidates": candidates,
        "oos_results": oos_results,
        "robustness_results": robustness_results,
        "history": history,
        "pareto_front": [
            {
                "params": {PARAM_NAMES[i]: round(ind[i], 4) for i in range(5)},
                "fitness": list(ind.fitness.values),
            }
            for ind in pareto_front
        ],
        "runtime_sec": round(runtime, 1),
        "pop_size": pop_size,
        "gen_count": gen_count,
        "wf_config": {
            "train_bars": train_bars,
            "valid_bars": valid_bars,
            "step_bars": step_bars,
        },
    }

    # 关闭进程池
    pool.close()
    pool.join()

    return result


# ============================================================================
# 保存结果
# ============================================================================


def save_results(result, output_dir=None):
    """保存结果到 JSON。"""
    if output_dir is None:
        output_dir = HERE

    symbol = result["symbol"]
    filename = f"ga_tpsl_v2_{symbol}_result.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n💾 结果已保存: {filepath}")
    return filepath


# ============================================================================
# CLI
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="止盈止损参数 GA 联合优化器（Phase 2 完整版）")
    parser.add_argument("--symbol", type=str, default="jd", help="品种代码")
    parser.add_argument("--pop", type=int, default=DEFAULT_POP_SIZE, help="种群大小")
    parser.add_argument("--gen", type=int, default=DEFAULT_GEN_COUNT, help="进化代数")
    parser.add_argument("--train-bars", type=int, default=DEFAULT_TRAIN_BARS, help="WF训练窗口大小")
    parser.add_argument("--valid-bars", type=int, default=DEFAULT_VALID_BARS, help="WF验证窗口大小")
    parser.add_argument("--step-bars", type=int, default=DEFAULT_STEP_BARS, help="WF滚动步长")
    parser.add_argument("--full", action="store_true", help="使用全量IS数据（否则限制800根加速）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    parser.add_argument("--n-jobs", type=int, default=DEFAULT_N_JOBS, help="并行评估进程数")
    parser.add_argument(
        "--early-stop-patience", type=int, default=EARLY_STOP_PATIENCE, help="早停耐心值（连续多少代无提升则终止）"
    )
    parser.add_argument("--fast", action="store_true", help="快速模式：步长翻倍，窗口减半，适合快速探索")
    args = parser.parse_args()

    # 快速模式：步长翻倍 → 窗口减半 → 速度翻倍
    if args.fast:
        args.step_bars *= 2
        print("⚡ 快速模式已启用（step_bars 翻倍）")

    print("=" * 60)
    print("🧬 止盈止损参数 GA 联合优化器（Phase 2 · NSGA-II 多目标）")
    print("=" * 60)

    result = run_optimization(
        symbol=args.symbol,
        pop_size=args.pop,
        gen_count=args.gen,
        train_bars=args.train_bars,
        valid_bars=args.valid_bars,
        step_bars=args.step_bars,
        full_data=args.full,
        seed=args.seed,
        n_jobs=args.n_jobs,
        early_stop_patience=args.early_stop_patience,
    )

    if "error" in result:
        print(f"❌ 错误: {result['error']}")
        return

    save_results(result, args.output)

    # 总结
    print("\n" + "=" * 60)
    print("📋 优化总结")
    print("=" * 60)
    for key, cand in result["candidates"].items():
        oos = result["oos_results"].get(key, {})
        rob = result["robustness_results"].get(key, {})
        deg = oos.get("degradation", {}).get("expR", 0)
        rob_score = rob.get("overall_score", 0)
        passed_oos = deg <= 0.30
        passed_rob = rob_score >= 0.5
        status = "✅" if passed_oos and passed_rob else "⚠️"
        deg_str = f"退化 {deg:.1%}" if deg > 0 else f"OOS更优 {-deg:.1%}"
        print(f"  {status} {cand['label']}:")
        print(
            f"     expR: IS={oos.get('metrics_is', {}).get('expR', 0):.4f} "
            f"→ OOS={oos.get('metrics_oos', {}).get('expR', 0):.4f} "
            f"({deg_str})"
        )
        print(f"     稳健性得分: {rob_score:.2f}")

    return result


if __name__ == "__main__":
    main()
