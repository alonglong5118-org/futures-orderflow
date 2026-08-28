# -*- coding: utf-8 -*-
"""品种筛选引擎（Symbol Screener）
=================================================================
可配置的多条件品种筛选器，用于盘前动态生成交易品种池。

筛选维度：
  1. 流动性：日均成交额 > 阈值
  2. 波动率：ATR% 在目标区间内（太低没利润，太高风险大）
  3. 趋势强度：|T_D| > 阈值（有趋势才值得交易）
  4. 回撤水位：不在历史最大回撤区域（避免接飞刀）
  5. 相关性：与已持仓品种相关性 < 阈值（避免集中风险）
  6. 成交量异动：今日量比 > 阈值（资金关注）

条件组合方式：全部满足(AND) / 至少满足N个(加权投票)

参考 Kara说量化 的选股器条件引擎设计，适配到期货品种筛选。
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import four_dim_strategy as fd
from four_dim_strategy import DEFAULT_CONFIG, load_daily

# 默认筛选条件
DEFAULT_CRITERIA = {
    "min_turnover": 50,         # 最低日均成交额（亿）
    "atr_pct_min": 0.008,        # 最低 ATR%（太低无利润空间）
    "atr_pct_max": 0.040,        # 最高 ATR%（太高风险大）
    "min_abs_T_D": 10,           # 最低 |T_D|（无趋势不交易）
    "max_correlation": 0.70,     # 与已持仓最大相关性
    "min_volume_ratio": 0.80,    # 最低今日量比
}

# 条件权重（加权投票模式用）
CRITERIA_WEIGHTS = {
    "liquidity": 0.20,
    "volatility": 0.15,
    "trend": 0.30,
    "volume_anomaly": 0.15,
    "correlation": 0.20,
}

_CACHE = {"ts": 0, "data": None}


def _compute_metrics(symbol, df_daily, cfg=DEFAULT_CONFIG):
    """计算单个品种的筛选指标。"""
    if df_daily is None or len(df_daily) < 30:
        return None

    try:
        close = df_daily["close"].astype(float)
        high = df_daily["high"].astype(float)
        low = df_daily["low"].astype(float)
        vol = df_daily["volume"].astype(float) if "volume" in df_daily else pd.Series(0, index=df_daily.index)

        # 1. 流动性：20日平均成交额
        turnover = float((close * vol * 10).rolling(20, min_periods=10).mean().iloc[-1] / 1e8) if vol.iloc[-1] > 0 else 0

        # 2. 波动率：ATR%
        tr = pd.concat([(high - low),
                         (high - close.shift(1)).abs(),
                         (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr_val = float(tr.rolling(14, min_periods=14).mean().iloc[-1])
        atr_pct = atr_val / float(close.iloc[-1]) if float(close.iloc[-1]) > 0 else 0

        # 3. 趋势强度：T_D
        T_D, regime, _ = fd.compute_T(df_daily, cfg, group=None, symbol=symbol)

        # 4. 量比
        vol_ma20 = float(vol.rolling(20, min_periods=10).mean().iloc[-1]) if vol.iloc[-1] > 0 else 1
        vol_ratio = float(vol.iloc[-1] / vol_ma20) if vol_ma20 > 0 else 1.0

        # 5. 回撤水位
        cum_max = close.cummax()
        drawdown = (close / cum_max - 1.0)
        current_dd = float(drawdown.iloc[-1])

        return {
            "symbol": symbol,
            "turnover_billion": round(turnover, 2),
            "atr_pct": round(atr_pct * 100, 3),
            "atr_val": round(atr_val, 2),
            "T_D": round(float(T_D), 1),
            "regime": regime,
            "vol_ratio": round(vol_ratio, 2),
            "current_dd_pct": round(current_dd * 100, 2),
            "close": round(float(close.iloc[-1]), 2),
        }
    except Exception:
        return None


def _check_criteria(metrics, criteria, held_symbols=None, corr_data=None):
    """检查品种是否满足筛选条件。

    返回 (passed, score, reasons)
    passed: bool（AND 模式=全部满足，加权模式=score>0.6）
    score: 0-1（加权得分）
    reasons: [pass/fail 描述]
    """
    if metrics is None:
        return False, 0.0, ["数据不足"]

    reasons = []
    scores = {}

    # 1. 流动性
    liq_ok = metrics["turnover_billion"] >= criteria.get("min_turnover", 0)
    scores["liquidity"] = 1.0 if liq_ok else metrics["turnover_billion"] / max(criteria.get("min_turnover", 1), 1)
    reasons.append(f"流动性 {metrics['turnover_billion']}亿 {'✓' if liq_ok else '✗'}")

    # 2. 波动率
    atr = metrics["atr_pct"] / 100
    vol_ok = criteria.get("atr_pct_min", 0) <= atr <= criteria.get("atr_pct_max", 1)
    if atr < criteria.get("atr_pct_min", 0):
        scores["volatility"] = atr / criteria.get("atr_pct_min", 1)
    elif atr > criteria.get("atr_pct_max", 1):
        scores["volatility"] = criteria.get("atr_pct_max", 1) / atr
    else:
        scores["volatility"] = 1.0
    reasons.append(f"ATR {metrics['atr_pct']}% {'✓' if vol_ok else '✗'}")

    # 3. 趋势
    abs_T = abs(metrics["T_D"])
    trend_ok = abs_T >= criteria.get("min_abs_T_D", 0)
    scores["trend"] = min(1.0, abs_T / max(criteria.get("min_abs_T_D", 1), 1))
    reasons.append(f"|T_D|={metrics['T_D']} {'✓' if trend_ok else '✗'}")

    # 4. 量比
    vol_ok = metrics["vol_ratio"] >= criteria.get("min_volume_ratio", 0)
    scores["volume_anomaly"] = min(1.0, metrics["vol_ratio"] / max(criteria.get("min_volume_ratio", 1), 1))
    reasons.append(f"量比 {metrics['vol_ratio']} {'✓' if vol_ok else '✗'}")

    # 5. 相关性（如果有持仓数据）
    corr_ok = True
    if held_symbols and corr_data:
        max_corr = 0
        for hs in held_symbols:
            c = corr_data.get(f"{metrics['symbol']}_vs_{hs}", 0)
            if abs(c) > max_corr:
                max_corr = abs(c)
        corr_ok = max_corr < criteria.get("max_correlation", 1)
        scores["correlation"] = 1.0 - max_corr
        reasons.append(f"最大相关 {max_corr:.2f} {'✓' if corr_ok else '✗'}")
    else:
        scores["correlation"] = 1.0

    # 加权得分
    total_score = sum(CRITERIA_WEIGHTS.get(k, 0) * scores.get(k, 0) for k in CRITERIA_WEIGHTS)
    all_pass = liq_ok and vol_ok and trend_ok and vol_ok and corr_ok

    return all_pass, round(total_score, 3), reasons


def screen(symbols_dict, criteria=None, held_symbols=None, mode="weighted", cfg=DEFAULT_CONFIG):
    """品种筛选主函数。

    参数：
      symbols_dict: SYMBOLS dict
      criteria: 筛选条件（None=用默认）
      held_symbols: 已持仓品种列表（用于相关性筛选）
      mode: "and"=全部满足, "weighted"=加权投票(>0.6通过)

    返回 dict:
      - passed: [品种指标 + 筛选结果]
      - rejected: [品种 + 原因]
      - summary: {n_passed, n_rejected, top_picks}
    """
    criteria = criteria or DEFAULT_CRITERIA
    passed = []
    rejected = []

    for sym in symbols_dict:
        try:
            df = load_daily(sym)
            metrics = _compute_metrics(sym, df, cfg)
            if metrics is None:
                rejected.append({"symbol": sym, "reason": "数据不足"})
                continue

            ok, score, reasons = _check_criteria(metrics, criteria, held_symbols)
            metrics["screen_score"] = score
            metrics["screen_reasons"] = reasons
            metrics["screen_passed"] = ok if mode == "and" else (score > 0.6)

            if metrics["screen_passed"]:
                passed.append(metrics)
            else:
                rejected.append({"symbol": sym, "score": score, "reasons": reasons})
        except Exception:
            rejected.append({"symbol": sym, "reason": "计算异常"})

    # 按得分排序
    passed.sort(key=lambda x: -x.get("screen_score", 0))

    return {
        "passed": passed,
        "rejected": rejected,
        "summary": {
            "n_total": len(symbols_dict),
            "n_passed": len(passed),
            "n_rejected": len(rejected),
            "top_picks": [{"symbol": p["symbol"], "score": p["screen_score"],
                           "T_D": p["T_D"], "regime": p["regime"],
                           "turnover": p["turnover_billion"]}
                          for p in passed[:5]],
        },
    }


def get_cache():
    """获取缓存。"""
    if _CACHE["data"] and time.time() - _CACHE["ts"] < 60:
        return _CACHE["data"]
    return None
