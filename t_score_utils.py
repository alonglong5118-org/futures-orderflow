"""
T 评分合成 — 纯函数工具
==========================

从 four_dim_strategy.compute_T 中提取的核心计算逻辑，
覆盖 P-A 整改的三大去相关机制：
  1. cluster_vote       — 簇投票（坍缩共线策略）
  2. crowd_penalty_factor — 拥挤降权（趋势簇一致度过高 → 打折）
  3. contrarian_damping  — 反向阻尼（趋势 vs 均值回归背离 → 整体打折）

历史 bug / 决策覆盖：
  - P-A ①：同簇共线坍缩（旧逻辑 5 个趋势策略 = 5 票，T 容易顶满 100）
  - P-A ②：拥挤降权（一致度越高越打折，抑制追高杀低）
  - P-A ③：反向阻尼（趋势与均值回归背离 → T 幅值打折）
"""

from typing import Dict, Tuple


def cluster_vote_and_consensus(
    sig: Dict[str, float],
    clusters: Dict[str, list],
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    计算各策略簇的投票均值和一致度。

    簇投票（P-A ①）：
      同簇共线策略先坍缩为「簇投票」(簇内 mean signal∈[-1,1])，
      避免 5 个趋势策略同向 = "5 次投同一方向" 导致 T 顶满 100。

    一致度：
      簇内与均值同向的策略占比（0~1）。
      用于拥挤降权的输入。

    参数:
        sig:       各策略信号值（如 {"ma_break": 1, "boll": -1, ...}）
        clusters:  簇定义（如 {"trend": ["ma_break", ...], "mean": ["boll", ...]}）

    返回: (cluster_vote, cluster_consensus)
        cluster_vote:     dict，各簇投票均值 ∈ [-1, 1]
        cluster_consensus: dict，各簇一致度 ∈ [0, 1]
    """
    vote = {}
    consensus = {}

    for cname, members in clusters.items():
        votes = [sig.get(m, 0) for m in members]
        if not votes:
            vote[cname] = 0.0
            consensus[cname] = 0.0
            continue

        mean_v = sum(votes) / len(votes)
        vote[cname] = mean_v

        # 一致度：与均值同向的比例
        if mean_v > 0:
            sgn = 1
        elif mean_v < 0:
            sgn = -1
        else:
            sgn = 0

        if sgn != 0:
            agree = sum(1 for v in votes if v == sgn) / len(votes)
        else:
            agree = 0.0

        consensus[cname] = agree

    return vote, consensus


def crowd_penalty_factor(
    consensus: float,
    crowd_thresh: float = 0.8,
    crowd_pen: float = 0.35,
) -> float:
    """
    计算拥挤降权系数（P-A ②）。

    规则：
    - 一致度 <= 阈值 → 不降权（factor = 1.0）
    - 一致度 > 阈值 → 线性降权，一致度越高降权越多
    - 最大降权幅度 = crowd_pen（如 0.35 表示最多打 65 折）
    - factor 范围：[1 - crowd_pen, 1.0]

    公式：
      over = min(1.0, (consensus - crowd_thresh) / (1 - crowd_thresh))
      factor = max(0, 1 - crowd_pen * over)

    设计意图：
    趋势簇内部一致度过高 → 可能处于趋势末端（"所有人都看多就该跌了"），
    对趋势簇贡献打折，抑制追高杀低。
    """
    if crowd_pen <= 0:
        return 1.0
    if consensus <= crowd_thresh:
        return 1.0

    denom = 1.0 - crowd_thresh
    if denom <= 0:
        denom = 1.0

    over = min(1.0, (consensus - crowd_thresh) / denom)
    factor = max(0.0, 1.0 - crowd_pen * over)
    return factor


def contrarian_damping_factor(
    trend_contrib: float,
    mean_contrib: float,
    contr_damp: float = 0.25,
) -> float:
    """
    计算反向阻尼系数（P-A ③）。

    规则：
    - 趋势簇和均值回归簇同向 → 不阻尼（factor = 1.0）
    - 两者反向 → 按背离程度打折
    - 阻尼比例 = min(|trend|, |mean|) / |trend| × contr_damp
    - factor 范围：[1 - contr_damp, 1.0]

    设计意图：
    趋势与均值回归反向（动量末端背离）→ 整体 T 幅值打折，
    显式引入 contrarian 维度平衡动量末端风险。

    注意：只看趋势和均值的贡献值（已经乘过权重和拥挤系数），
    不看原始信号方向。
    """
    if contr_damp <= 0:
        return 1.0
    if trend_contrib * mean_contrib >= 0:
        # 同向或其中一个为 0 → 不阻尼
        return 1.0

    # 反向：计算背离程度
    # div = 较小的 / 较大的（这里用趋势做分母，即"均值反向抵消了多少趋势"）
    abs_trend = abs(trend_contrib)
    abs_mean = abs(mean_contrib)
    if abs_trend <= 1e-12:
        return 1.0

    div = min(abs_trend, abs_mean) / abs_trend
    factor = 1.0 - contr_damp * div
    return max(0.0, factor)


def compute_T_score(
    sig: Dict[str, float],
    clusters: Dict[str, list],
    cluster_weights: Dict[str, float],
    base_cluster_weights: Dict[str, float],
    crowd_thresh: float = 0.8,
    crowd_pen: float = 0.35,
    contr_damp: float = 0.25,
) -> dict:
    """
    完整 T 评分合成（P-A 去相关版本，纯函数）。

    计算链路：
    1. 簇投票 + 一致度
    2. 拥挤降权（趋势簇）
    3. 各簇贡献求和 → raw
    4. 反向阻尼（趋势 vs 均值背离时）
    5. 归一化到 [-100, 100]

    参数:
        sig:                各策略信号值 dict
        clusters:           簇定义 dict
        cluster_weights:    各簇权重（可能含 seasonal_boost 放大）
        base_cluster_weights: 基准簇权重（未加权，用于归一化分母）
        crowd_thresh:       拥挤降权阈值
        crowd_pen:          拥挤最大惩罚系数
        contr_damp:         反向阻尼最大系数

    返回 dict:
        T_score:            float，[-100, 100]
        regime:             str（这里不做 regime 判断，由调用方传）
        cluster_vote:       dict，各簇投票
        cluster_consensus:  dict，各簇一致度
        crowd_factor:       float，拥挤降权系数
        contr_factor:       float，反向阻尼系数
        raw_score:          float，归一化前的原始值
    """
    # 1. 簇投票 + 一致度
    cv, cc = cluster_vote_and_consensus(sig, clusters)

    # 2. 拥挤降权（仅趋势簇）
    cf = crowd_penalty_factor(cc.get("trend", 0.0), crowd_thresh, crowd_pen)

    # 3. 各簇贡献
    trend_contrib = cluster_weights.get("trend", 0) * cv.get("trend", 0) * cf
    mean_contrib = cluster_weights.get("mean", 0) * cv.get("mean", 0)
    seas_contrib = cluster_weights.get("seasonal", 0) * cv.get("seasonal", 0)
    raw = trend_contrib + mean_contrib + seas_contrib

    # 4. 反向阻尼
    contr_factor = contrarian_damping_factor(trend_contrib, mean_contrib, contr_damp)
    if contr_factor < 1.0:
        raw = raw * contr_factor

    # 5. 归一化到 [-100, 100]
    maxw = sum(abs(base_cluster_weights.get(c, 0)) for c in clusters)
    if maxw <= 0:
        T_score = 0.0
    else:
        import math

        T_score = math.copysign(min(100.0, abs(raw) / maxw * 100.0), raw)

    return {
        "T_score": round(T_score, 1),
        "cluster_vote": cv,
        "cluster_consensus": cc,
        "crowd_factor": round(cf, 4),
        "contr_factor": round(contr_factor, 4),
        "raw_score": round(raw, 4),
    }
