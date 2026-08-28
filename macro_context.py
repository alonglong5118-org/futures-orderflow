#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macro_context.py — 跨资产宏观语境因子（纯 stdlib，跑 live runner venv 3.13）

读取 fetch_macro_context.py（系统3.9+akshare）写出的 macro_context.json 缓存，
计算「宏观语境偏置」macro_bias ∈ [-1,1]，供 live 信号路径调制 bias_G。

红线（与 info_dimension / regime_hmm 一致）：
  - 本模块只服务于 live 信号，绝不进入回测。pipeline 的 macro_label 参数默认 None，
    回测三处调用只传 date=，数学上不进宏观语境，零前视污染。

语境因子（全部用近20日变化，滞后一档，无前视）：
  equity_mom  沪深300 近20日收益率 → 风险偏好（正=risk-on，权益走强利多风险资产）
  rate_trend  10Y 国债收益率近20日变化 → 利率环境（收益率下行=宽松=正，利多商品/权益）
  fx_trend    美元/人民币近20日变化 → 汇率（美元贬值=人民币升=内盘商品相对利多=正）
合成：macro_bias = 0.40*equity_mom + 0.30*rate_trend + 0.30*fx_trend，夹断[-1,1]。
各分项先经 tanh 归一化到(-1,1)再加权。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "macro_context.json")
_MACRO_CACHE = {}


def _load():
    global _MACRO_CACHE
    try:
        if os.path.exists(CACHE):
            _MACRO_CACHE = json.load(open(CACHE, encoding="utf-8")) or {}
    except Exception:
        _MACRO_CACHE = {}


def _norm_tanh(x, scale):
    import math
    if not scale:
        return 0.0
    return math.tanh(x / scale)


def _series(name):
    s = (_MACRO_CACHE.get("series") or {}).get(name) or []
    return [float(x) for x in s if x is not None]


def compute():
    """返回 {macro_bias, equity_mom, rate_trend, fx_trend, as_of, available}。无数据→available=False, bias=0。"""
    if not _MACRO_CACHE:
        _load()
    eq = _series("hs300")
    rt = _series("cgb10")
    fx = _series("usdcny")
    out = {"macro_bias": 0.0, "equity_mom": 0.0, "rate_trend": 0.0, "fx_trend": 0.0,
           "as_of": _MACRO_CACHE.get("as_of"), "available": False}
    if len(eq) < 21 or len(rt) < 21 or len(fx) < 21:
        return out
    eq_ret = eq[-1] / eq[-21] - 1.0          # 近20日收益率
    rate_chg = rt[-1] - rt[-21]              # 10Y收益率变化(百分点)
    fx_chg = fx[-1] - fx[-21]                # 美元/人民币变化
    equity_mom = _norm_tanh(eq_ret, 0.08)
    rate_trend = _norm_tanh(-rate_chg, 0.0020)   # 收益率下行=正
    fx_trend = _norm_tanh(-fx_chg, 0.020)        # 美元贬值=正
    bias = 0.40 * equity_mom + 0.30 * rate_trend + 0.30 * fx_trend
    bias = max(-1.0, min(1.0, bias))
    out.update({"macro_bias": round(bias, 4), "equity_mom": round(equity_mom, 4),
                "rate_trend": round(rate_trend, 4), "fx_trend": round(fx_trend, 4),
                "available": True})
    # 扩展宏观分项（原油/南华/USDA代理）：仅暴露 momentum，不进 macro_bias
    # （避免影响 live 信号；待 OOS 验证后再决定是否融合进 bias 公式）
    crude = _series("crude")
    nh = _series("nh_comm")
    ag = _series("ag_spot")
    crude_mom = _norm_tanh(crude[-1] / crude[-21] - 1.0, 0.10) if len(crude) >= 21 else 0.0
    nh_mom = _norm_tanh(nh[-1] / nh[-21] - 1.0, 0.10) if len(nh) >= 21 else 0.0
    ag_mom = _norm_tanh(ag[-1] / ag[-21] - 1.0, 0.10) if len(ag) >= 21 else 0.0
    out.update({"crude_mom": round(crude_mom, 4), "nh_mom": round(nh_mom, 4),
                "ag_mom": round(ag_mom, 4),
                "crude_available": len(crude) >= 21, "nh_available": len(nh) >= 21,
                "ag_available": len(ag) >= 21})
    return out


def macro_bias():
    """live 信号路径调用：返回 macro_bias 浮点（无数据→0.0，不影响信号）。"""
    return compute().get("macro_bias", 0.0)


def refresh():
    """重新从磁盘加载缓存（_update_aux 周期调用，捕获最新 macro_context.json）。"""
    _load()


if __name__ == "__main__":
    print(json.dumps(compute(), ensure_ascii=False, indent=2))
