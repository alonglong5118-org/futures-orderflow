# -*- coding: utf-8 -*-
"""方向源偏差监控（红线①量化管理，回测结论 2026-08-16）
================================================

背景：回测用日线 T_D 定方向、实盘用 T_5m 定方向，4070 日样本分歧 **48.4%**。
红线①的本质风险是"回测结论建立在错误的方向源上"。探针结论（统一方向源到 T_5m
后 6/6 核心改进仍成立）→ 红线①降级为"已知偏差、可量化管理"，不推翻框架。

本模块把该分歧做成**可量化、可告警的运行时监控**：
  - divergence(T_D, T_5m)       : 单笔方向一致性（同号/异号/无方向）
  - DivergenceTracker            : 滚动统计分歧率 + SA 专项敏感度
  - alert_level(symbol, T_D, T_5m): 更新全局 tracker 并返回告警级别（供 runner 调用）

设计要点：
  · 分歧率超基线即告警，但**不阻断信号**（改进对方向源不敏感，仅提示降权）。
  · SA 单独记一个专项序列（对方向源最敏感），面板可单独看。
  · 纯函数 + 单例 tracker，runner 每轮调用 alert_level() 即可，零副作用风险。
"""

from __future__ import annotations

# ── 回测实测基准 ──
BASELINE_DIVERGENCE = 0.484  # TD vs T5m 全样本分歧率
WARN_DIVERGENCE = 0.55  # 滚动分歧率 ≥ 此 → WARN（建议信号降权）
HIGH_DIVERGENCE = 0.65  # 滚动分歧率 ≥ 此 → HIGH（建议暂停新开/人工复核）
SA_SENSITIVE = True  # SA 对方向源最敏感

_tracker = None


def divergence(T_D, T_5m):
    """单笔方向一致性：同号→True / 异号→False / 任一方无方向→None。"""
    d = 1 if T_D > 0 else (-1 if T_D < 0 else 0)
    m = 1 if T_5m > 0 else (-1 if T_5m < 0 else 0)
    if d == 0 or m == 0:
        return None
    return d == m


class DivergenceTracker:
    """滚动统计方向源分歧率，支持 SA 专项序列。"""

    def __init__(self, window=200):
        self.window = window
        self.samples = []  # 一致性 True/False 序列（全品种）
        self.sa_samples = []  # SA 专项一致性序列
        self.last = None  # 最近一次 summary 快照

    def update(self, symbol, T_D, T_5m):
        ag = divergence(T_D, T_5m)
        if ag is None:
            return self.summary()
        self.samples.append(ag)
        if len(self.samples) > self.window:
            self.samples.pop(0)
        if symbol == "SA":
            self.sa_samples.append(ag)
            if len(self.sa_samples) > self.window:
                self.sa_samples.pop(0)
        self.last = self.summary()
        return self.last

    @staticmethod
    def _rate(arr):
        if not arr:
            return None
        return 1.0 - sum(arr) / len(arr)  # 分歧率

    def summary(self):
        rate = self._rate(self.samples)
        sa_rate = self._rate(self.sa_samples)
        level = "OK"
        if rate is not None:
            if rate >= HIGH_DIVERGENCE:
                level = "HIGH"
            elif rate >= WARN_DIVERGENCE:
                level = "WARN"
        return {
            "divergence_rate": round(rate, 3) if rate is not None else None,
            "baseline": BASELINE_DIVERGENCE,
            "level": level,
            "sa_divergence_rate": round(sa_rate, 3) if sa_rate is not None else None,
            "sa_sensitive": SA_SENSITIVE,
            "n": len(self.samples),
        }


def get_tracker():
    global _tracker
    if _tracker is None:
        _tracker = DivergenceTracker()
    return _tracker


def alert_level(symbol, T_D, T_5m):
    """runner 每轮调用：更新全局 tracker 并返回当前告警级别。

    返回 summary dict；level ∈ {OK, WARN, HIGH}。仅告警、不阻断信号。
    """
    return get_tracker().update(symbol, T_D, T_5m)


def reset_tracker():
    """测试/跨日重置用。"""
    global _tracker
    _tracker = DivergenceTracker()
    return _tracker.summary()


if __name__ == "__main__":
    # 自测：方向同号/异号/无方向 + 滚动分歧率
    t = DivergenceTracker()
    print("同号:", divergence(1, 1), "异号:", divergence(1, -1), "无方向:", divergence(0, 1))
    # 模拟：SA 连续 10 笔全同号、其余品种 10 笔全异号
    for _ in range(10):
        t.update("SA", 1, 1)
    for _ in range(10):
        t.update("rb", 1, -1)
    s = t.summary()
    print("分歧率(全):", s["divergence_rate"], "level:", s["level"], "SA专项:", s["sa_divergence_rate"])
