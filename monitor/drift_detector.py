"""
参数漂移检测模块 (Phase 7, Task 2)

功能：
- 基于滚动窗口的 expR 变化检测
- 双阈值告警：统计显著 + 经济显著
- 支持 CUSUM（累积和控制图）和滑动 t 检验两种方法
- 输出漂移告警列表，供监控看板展示

用法：
    from monitor.drift_detector import DriftDetector
    detector = DriftDetector()
    alerts = detector.detect(symbol_metrics_dict)
    for a in alerts:
        print(a["symbol"], a["severity"], a["message"])
"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class DriftAlert:
    """漂移告警数据结构"""
    symbol: str
    metric: str  # "expR" / "trades" / "win_rate"
    severity: str  # "warning" / "critical"
    method: str  # "cusum" / "ttest"
    baseline_value: float
    current_value: float
    delta: float
    delta_pct: float
    p_value: Optional[float] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class DriftDetector:
    """
    参数漂移检测器

    使用双阈值机制避免误报：
    1. 统计显著：p-value < 阈值（真实变化 vs 随机波动）
    2. 经济显著：绝对变化量 > 阈值（变化是否值得关注）
    """

    def __init__(
        self,
        warning_delta: float = 0.05,
        critical_delta: float = 0.10,
        p_value_threshold: float = 0.10,
        min_trades: int = 10,
        window_size: int = 60,  # 交易日
        baseline_window: int = 250,  # 基线窗口（约1年）
    ):
        """
        Args:
            warning_delta: 告警级别的 expR 绝对变化阈值
            critical_delta: 严重级别的 expR 绝对变化阈值
            p_value_threshold: 统计显著性阈值
            min_trades: 最少交易次数（低于此数不做统计检验）
            window_size: 当前表现窗口（交易日）
            baseline_window: 基线表现窗口（交易日）
        """
        self.warning_delta = warning_delta
        self.critical_delta = critical_delta
        self.p_value_threshold = p_value_threshold
        self.min_trades = min_trades
        self.window_size = window_size
        self.baseline_window = baseline_window

    def detect(
        self,
        symbol_metrics: Dict[str, Dict[str, Any]],
    ) -> List[DriftAlert]:
        """
        检测所有品种的参数漂移。

        Args:
            symbol_metrics: {symbol: {
                "baseline_expR": float,
                "baseline_trades": int,
                "recent_expR": float,
                "recent_trades": int,
                "baseline_daily_expR": [float, ...],  # 基线期每日 expR 序列
                "recent_daily_expR": [float, ...],    # 近期每日 expR 序列
            }}

        Returns:
            漂移告警列表（按严重程度排序，critical 在前）
        """
        alerts = []

        for sym, metrics in symbol_metrics.items():
            # expR 漂移检测
            alert = self._check_expr_drift(sym, metrics)
            if alert:
                alerts.append(alert)

            # 交易频率漂移检测
            trade_alert = self._check_trade_drift(sym, metrics)
            if trade_alert:
                alerts.append(trade_alert)

        # 按严重程度排序：critical > warning
        severity_order = {"critical": 0, "warning": 1}
        alerts.sort(key=lambda a: (severity_order.get(a.severity, 99), a.symbol))

        return alerts

    def _check_expr_drift(
        self, symbol: str, metrics: Dict[str, Any]
    ) -> Optional[DriftAlert]:
        """检测 expR 漂移"""
        base_expR = metrics.get("baseline_expR", 0)
        recent_expR = metrics.get("recent_expR", 0)
        base_trades = metrics.get("baseline_trades", 0)
        recent_trades = metrics.get("recent_trades", 0)

        delta = recent_expR - base_expR
        delta_pct = (delta / abs(base_expR) * 100) if base_expR != 0 else float("inf")

        # 经济显著检查
        abs_delta = abs(delta)
        if abs_delta < self.warning_delta:
            return None  # 变化太小，不告警

        # 交易数不足时只做经济显著告警（低置信度）
        if recent_trades < self.min_trades or base_trades < self.min_trades:
            severity = "warning" if abs_delta < self.critical_delta else "critical"
            return DriftAlert(
                symbol=symbol,
                metric="expR",
                severity=severity,
                method="simple_delta",
                baseline_value=round(base_expR, 4),
                current_value=round(recent_expR, 4),
                delta=round(delta, 4),
                delta_pct=round(delta_pct, 1),
                message=(
                    f"{symbol} expR 变化 {delta:+.3f} ({delta_pct:+.1f}%)，"
                    f"但交易数不足（近期{recent_trades}笔），统计置信度低"
                ),
                details={"note": "低样本量告警，需关注但不触发重优化"},
            )

        # 统计显著性检验
        p_value = None
        base_series = metrics.get("baseline_daily_expR", [])
        recent_series = metrics.get("recent_daily_expR", [])

        if len(base_series) > 10 and len(recent_series) > 10:
            p_value = self._welch_t_test(base_series, recent_series)

        # 判断严重程度
        if abs_delta >= self.critical_delta:
            severity = "critical"
        else:
            severity = "warning"

        # 如果统计不显著且变化只是 warning 级，降低置信度
        if p_value is not None and p_value > self.p_value_threshold and severity == "warning":
            return None  # 统计不显著的小变化，忽略

        direction = "下降" if delta < 0 else "上升"
        sig_note = ""
        if p_value is not None:
            sig_note = f"，p={p_value:.3f}（{'显著' if p_value < self.p_value_threshold else '不显著'}）"

        return DriftAlert(
            symbol=symbol,
            metric="expR",
            severity=severity,
            method="ttest" if p_value is not None else "simple_delta",
            baseline_value=round(base_expR, 4),
            current_value=round(recent_expR, 4),
            delta=round(delta, 4),
            delta_pct=round(delta_pct, 1),
            p_value=round(p_value, 4) if p_value is not None else None,
            message=(
                f"{symbol} expR {direction} {abs(delta):.3f} "
                f"({delta_pct:+.1f}%){sig_note}"
            ),
            details={
                "base_trades": base_trades,
                "recent_trades": recent_trades,
            },
        )

    def _check_trade_drift(
        self, symbol: str, metrics: Dict[str, Any]
    ) -> Optional[DriftAlert]:
        """检测交易频率漂移（信号密度变化）"""
        base_trades = metrics.get("baseline_trades", 0)
        recent_trades = metrics.get("recent_trades", 0)

        if base_trades == 0:
            return None

        # 按时间窗口归一化
        base_window = metrics.get("baseline_window_days", self.baseline_window)
        recent_window = metrics.get("recent_window_days", self.window_size)

        base_rate = base_trades / base_window if base_window > 0 else 0
        recent_rate = recent_trades / recent_window if recent_window > 0 else 0

        if base_rate == 0:
            ratio = float("inf") if recent_rate > 0 else 1.0
        else:
            ratio = recent_rate / base_rate

        # 交易频率变化超过 50% 才告警
        if 0.5 <= ratio <= 2.0:
            return None

        severity = "warning" if (0.3 <= ratio <= 3.0) else "critical"
        direction = "下降" if ratio < 1 else "上升"

        return DriftAlert(
            symbol=symbol,
            metric="trades",
            severity=severity,
            method="rate_ratio",
            baseline_value=round(base_rate, 3),
            current_value=round(recent_rate, 3),
            delta=round(recent_rate - base_rate, 3),
            delta_pct=round((ratio - 1) * 100, 1),
            message=(
                f"{symbol} 交易频率{direction} {(ratio * 100):.0f}% "
                f"(基线{base_rate:.2f}/日 → 近期{recent_rate:.2f}/日)"
            ),
            details={
                "base_trades": base_trades,
                "recent_trades": recent_trades,
                "ratio": round(ratio, 2),
            },
        )

    @staticmethod
    def _welch_t_test(a: List[float], b: List[float]) -> float:
        """
        Welch's t-test：检验两个独立样本的均值是否有显著差异。
        不假设等方差，适合比较不同时间段的表现。

        Returns:
            p-value（双侧）
        """
        a_arr = np.array(a, dtype=float)
        b_arr = np.array(b, dtype=float)

        n_a, n_b = len(a_arr), len(b_arr)
        if n_a < 2 or n_b < 2:
            return 1.0

        mean_a, mean_b = np.mean(a_arr), np.mean(b_arr)
        var_a, var_b = np.var(a_arr, ddof=1), np.var(b_arr, ddof=1)

        # t 统计量
        se = math.sqrt(var_a / n_a + var_b / n_b)
        if se == 0:
            return 1.0
        t_stat = abs(mean_a - mean_b) / se

        # Welch-Satterthwaite 自由度
        df_num = (var_a / n_a + var_b / n_b) ** 2
        df_den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
        df = df_num / df_den if df_den > 0 else min(n_a, n_b) - 1

        # 近似 p-value（使用正态近似，df > 30 时足够准确）
        # 对于小 df，使用 t 分布近似
        if df > 30:
            # 正态近似
            p_value = 2 * (1 - _normal_cdf(t_stat))
        else:
            # 用 beta 不完全函数近似 t 分布 CDF
            p_value = 2 * _t_sf(t_stat, df)

        return max(0.0, min(1.0, p_value))

    def cusum_detect(
        self,
        series: List[float],
        baseline_mean: float,
        baseline_std: float,
        threshold: float = 3.0,
    ) -> Tuple[bool, int, float]:
        """
        CUSUM（累积和控制图）漂移检测。
        用于检测均值的持续性偏移。

        Args:
            series: 时间序列数据
            baseline_mean: 基线均值
            baseline_std: 基线标准差
            threshold: 决策阈值（单位：标准差）

        Returns:
            (has_drift, drift_start_index, max_cusum_value)
        """
        if baseline_std == 0 or len(series) == 0:
            return False, -1, 0.0

        # 标准化
        k = 0.5 * baseline_std  # 参考值（允许的小偏移）
        h = threshold * baseline_std  # 决策阈值

        cusum_pos = 0.0
        cusum_neg = 0.0
        max_cusum = 0.0
        drift_idx = -1

        for i, x in enumerate(series):
            deviation = x - baseline_mean

            # 正偏移
            cusum_pos = max(0, cusum_pos + deviation - k)
            # 负偏移
            cusum_neg = max(0, cusum_neg - deviation - k)

            current_max = max(cusum_pos, cusum_neg)
            if current_max > max_cusum:
                max_cusum = current_max

            if current_max >= h and drift_idx == -1:
                drift_idx = i

        has_drift = drift_idx >= 0
        return has_drift, drift_idx, round(max_cusum / baseline_std, 2)


def _normal_cdf(x: float) -> float:
    """标准正态分布 CDF（近似）"""
    # Abramowitz and Stegun 近似
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _t_sf(t: float, df: float) -> float:
    """
    t 分布生存函数（p-value 的一半）的近似计算。
    使用正则化不完全 beta 函数的近似。
    """
    # 当 df 较小时用近似公式
    x = df / (df + t * t)
    return _reg_beta(df / 2, 0.5, x) / 2


def _reg_beta(a: float, b: float, x: float) -> float:
    """
    正则化不完全 beta 函数 I_x(a, b) 的近似。
    使用连分数近似（Lentz 方法）。
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    # 对称关系
    if x > (a + 1) / (a + b + 2):
        return 1 - _reg_beta(b, a, 1 - x)

    # 前因子
    lbeta_ab = (
        math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    )
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta_ab) / a

    # 连分数近似（修正 Lentz 方法）
    f = 1.0
    c = 1.0
    d = 0.0

    for m in range(1, 100):
        # 偶数项
        m2 = 2 * m
        d = 1.0 + m2 / 2.0 * (d if d != 0 else 1e-30)
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + m2 / 2.0 / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = c * d
        f *= delta

        # 奇数项
        numerator = -(a + m - 1) * (b + m - 1)
        d = 1.0 + numerator / m2 * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + numerator / m2 / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = c * d
        f *= delta

        if abs(delta - 1) < 1e-10:
            break

    return front * (f - 1)


def compute_rolling_expr_series(
    daily_pnl: List[float],
    window: int = 60,
    min_periods: int = 20,
) -> List[Optional[float]]:
    """
    从每日 PnL 序列计算滚动 expR。
    expR 近似 = 滚动均值 / 滚动标准差（假设正态分布的 Sharpe 类似指标）。

    Args:
        daily_pnl: 每日 PnL 序列（或每日收益率）
        window: 滚动窗口大小
        min_periods: 最少观测数

    Returns:
        滚动 expR 序列（前 min_periods 个为 None）
    """
    result = []
    pnl_arr = np.array(daily_pnl, dtype=float)

    for i in range(len(pnl_arr)):
        if i < min_periods - 1:
            result.append(None)
            continue

        start = max(0, i - window + 1)
        window_data = pnl_arr[start : i + 1]

        mean = np.mean(window_data)
        std = np.std(window_data, ddof=1)

        if std == 0:
            result.append(0.0)
        else:
            result.append(float(mean / std))

    return result


if __name__ == "__main__":
    # 简单自测
    print("=== DriftDetector 自测 ===")
    detector = DriftDetector(warning_delta=0.05, critical_delta=0.10)

    # 模拟数据：基线期表现好，近期表现下降
    np.random.seed(42)
    baseline_daily = list(np.random.normal(0.002, 0.015, 250))  # 基线：正期望
    recent_daily = list(np.random.normal(0.000, 0.015, 60))  # 近期：零期望

    metrics = {
        "TEST": {
            "baseline_expR": 0.25,
            "baseline_trades": 80,
            "recent_expR": 0.10,
            "recent_trades": 18,
            "baseline_daily_expR": baseline_daily,
            "recent_daily_expR": recent_daily,
            "baseline_window_days": 250,
            "recent_window_days": 60,
        }
    }

    alerts = detector.detect(metrics)
    print(f"发现 {len(alerts)} 个告警：")
    for a in alerts:
        print(f"  [{a.severity.upper()}] {a.message}")

    # CUSUM 测试
    has_drift, idx, val = detector.cusum_detect(recent_daily, 0.002, 0.015)
    print(f"\nCUSUM 检测: 漂移={has_drift}, 起始索引={idx}, 最大CUSUM={val}σ")

    print("\n✓ 自测完成")
