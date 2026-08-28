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
# 8 策略（各返回 signal∈{-1,0,1}, detail）
# ----------------------------------------------------------------------------
def s_ma_break(df):
    """MA突破策略。"""
    close = df["close"]
    ma20 = sma(close, 20).iloc[-1]
    ma60 = sma(close, 60).iloc[-1]
    c = close.iloc[-1]
    if any(math.isnan(x) for x in (ma20, ma60)):
        return 0, {}
    if c > ma20 and ma20 > ma60:
        return 1, {"ma20": round(ma20, 2), "ma60": round(ma60, 2)}
    if c < ma20 and ma20 < ma60:
        return -1, {"ma20": round(ma20, 2), "ma60": round(ma60, 2)}
    return 0, {"ma20": round(ma20, 2), "ma60": round(ma60, 2)}


def s_dma(df):
    """双均线策略。"""
    f = sma(df["close"], 5)
    s = sma(df["close"], 20)
    x = crossover(f, s)
    return x, {"ma5": round(f.iloc[-1], 2), "ma20": round(s.iloc[-1], 2)}


def s_turtle(df, n=20, f=55):
    """海龟策略。"""
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    hh55 = df["high"].rolling(f).max()
    ll55 = df["low"].rolling(f).min()
    c = df["close"].iloc[-1]
    if c > hh.iloc[-2] and c > ll55.iloc[-1]:
        return 1, {}
    if c < ll.iloc[-2] and c < hh55.iloc[-1]:
        return -1, {}
    return 0, {}


def s_donchian(df, n=20):
    """通道突破策略。"""
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    c = df["close"].iloc[-1]
    if c >= hh.iloc[-1]:
        return 1, {}
    if c <= ll.iloc[-1]:
        return -1, {}
    return 0, {}


def s_pullback(df):
    """回踩策略。"""
    close = df["close"]
    ma20 = sma(close, 20).iloc[-1]
    ma60 = sma(close, 60).iloc[-1]
    c = close.iloc[-1]
    if any(math.isnan(x) for x in (ma20, ma60)):
        return 0, {}
    dev = abs(c - ma20) / ma20
    if ma20 > ma60 and dev < 0.02 and c > ma60:
        return 1, {"dev%": round(dev * 100, 2)}
    if ma20 < ma60 and dev < 0.02 and c < ma60:
        return -1, {"dev%": round(dev * 100, 2)}
    return 0, {}


def s_boll(df, n=20, k=2.0):
    """布林带策略。"""
    close = df["close"]
    m = sma(close, n)
    sd = close.rolling(n).std()
    up, lo = m + k * sd, m - k * sd
    c = close.iloc[-1]
    if c <= lo.iloc[-1]:
        return 1, {"lower": round(lo.iloc[-1], 2)}
    if c >= up.iloc[-1]:
        return -1, {"upper": round(up.iloc[-1], 2)}
    return 0, {}


def s_rsi(df, n=14, lo=30, hi=70):
    """RSI策略。"""
    r = rsi(df["close"], n).iloc[-1]
    if math.isnan(r):
        return 0, {}
    if r <= lo:
        return 1, {"rsi": round(r, 1)}
    if r >= hi:
        return -1, {"rsi": round(r, 1)}
    return 0, {"rsi": round(r, 1)}


def s_seasonal(df, min_samples=12):
    """季节性策略。"""
    if isinstance(df.index, pd.DatetimeIndex):
        dt = df.index
    elif "date" in df.columns:
        dt = pd.to_datetime(df["date"])
    else:
        return 0, {"reason": "无日期"}
    month = dt[-1].month
    ret = df["close"].pct_change()
    same = ret[dt.month == month].dropna()
    if len(same) < min_samples:
        return 0, {"reason": "样本不足", "n": int(len(same))}
    avg = same.mean()
    std = same.std()
    z = (avg / std) if (std and std > 0) else 0.0
    detail = {"month_avg%": round(avg * 100, 3), "n": int(len(same)), "z": round(z, 2)}
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
        with open(path, "r", encoding="utf-8") as f:
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
        with open(drift_json, "r", encoding="utf-8") as f:
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


def classify_regime(df, params=None):
    """返回 (regime, 描述)。"""
    p = params or REGIME_THRESHOLDS
    close = df["close"]
    if len(close) < 25:
        return "未知", "数据不足"

    ma20_now = sma(close, 20).iloc[-1]
    ma20_prev = sma(close, 20).iloc[-5]
    c = close.iloc[-1]
    dev = abs(c - ma20_now) / ma20_now
    atr_r = atr(df).iloc[-1] / c
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
