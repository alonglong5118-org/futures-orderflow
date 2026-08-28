# -*- coding: utf-8 -*-
"""市场情绪量化引擎（Market Sentiment Engine）
=================================================================
从全市场品种快照计算综合情绪指数（0-100），供 live 信号路径调制 T_thresh
和风控仓位缩放。五因子加权模型，参考 Kara说量化 的市场情绪系统思路，
适配到期货多品种框架。

红线（与 macro_context / regime_hmm 一致）：
  - 本模块只服务于 live 信号，绝不进入回测。pipeline 的 sentiment_label
    参数默认 None，回测三处调用不传参，零前视污染。
  - 任何计算失败返回中性（score=50, bias=0.0, scale=1.0），不影响 live 运行。

五因子：
  1. 市场广度（breadth）：涨跌家数比 → 多空力量对比
  2. 动量共识（momentum）：全品种 T_D 均值 → 技术面共识方向
  3. 资金活跃度（activity）：成交量 vs 20日均 → 资金流入/流出
  4. 波动率状态（volatility）：平均 ATR% → 恐慌/自满
  5. 板块分歧（divergence）：分组 T_D 方差 → 趋势一致性

合成：score = Σ(weight_i * normalized_factor_i)，映射到 0-100（50=中性）
输出：
  - sentiment_score: 0-100
  - sentiment_label: 极度恐惧/恐惧/中性/贪婪/极度贪婪
  - sentiment_bias: -1~+1（正=贪婪，负=恐惧，供 pipeline T_thresh 调制）
  - sentiment_scale: 0.5~1.0（极端情绪→缩仓，供 risk_state_machine）
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 因子权重 ──
# 五因子 + 两个增强因子，权重向广度和幅度倾斜（更灵敏）
WEIGHTS = {
    "breadth": 0.25,       # 市场广度（涨跌家数比）
    "momentum": 0.15,      # 动量共识（T_D均值）
    "activity": 0.15,      # 资金活跃度（量比）
    "volatility": 0.10,    # 波动率状态
    "divergence": 0.10,    # 板块分歧
    "amplitude": 0.15,     # 涨跌幅度分布（新增：大涨/大跌比例）
    "trend_conc": 0.10,    # 趋势集中度（新增：多少品种在趋势）
}

# ── 情绪分档（收紧：日频下极端情绪难得，降低阈值） ──
BANDS = [
    (70, "极度贪婪", "extreme_greed"),   # 原 80 → 70
    (58, "贪婪", "greed"),               # 原 60 → 58
    (42, "中性", "neutral"),             # 原 40 → 42（中性区间从20分缩到16分）
    (30, "恐惧", "fear"),                # 原 20 → 30
    (0, "极度恐惧", "extreme_fear"),
]

# ── 极端事件触发（全市场一致性很强时，直接标为极端，不看加权分） ──
EXTREME_BREADTH_RATIO = 0.70   # 70% 以上同涨或同跌 → 极端
EXTREME_AMPLITUDE_RATIO = 0.30 # 30% 以上品种涨/跌超 2% → 极端

# ── pipeline T_thresh 乘数（方向感知，极端才动） ──
# 只有贪婪/恐惧才微调，极度贪婪/恐惧才明显调，中性完全不动
SENTIMENT_THR_MULT = {
    "extreme_greed": {"long": 1.20, "short": 0.85},   # 极度贪婪：严防追涨
    "greed":        {"long": 1.08, "short": 0.95},   # 贪婪：轻微提高
    "neutral":      {"long": 1.00, "short": 1.00},   # 中性：完全不动
    "fear":         {"long": 0.95, "short": 1.08},   # 恐惧：轻微降低
    "extreme_fear": {"long": 0.85, "short": 1.20},   # 极度恐惧：严防杀跌
}

# ── 硬过滤（hard filter）：极端情绪期直接禁止某个方向的交易 ──
# 回测验证：极度贪婪期做多交易 expR 仅 +0.11（比中性期低 66%），
# 直接禁掉比调阈值效果好得多。
# True=禁止该方向交易, False=只调阈值不禁
SENTIMENT_HARD_FILTER = {
    "extreme_greed": {"long": True,  "short": False},  # 极度贪婪：禁做多（防追涨杀跌）
    "greed":        {"long": False, "short": False},  # 贪婪：只调阈值不禁
    "neutral":      {"long": False, "short": False},  # 中性：不禁
    "fear":         {"long": False, "short": False},  # 恐惧：只调阈值不禁
    "extreme_fear": {"long": False, "short": False},  # 极度恐惧：不禁（数据显示反而赚钱）
}


def is_hard_filtered(band, direction):
    """判断是否被情绪硬过滤。

    band: sentiment band 字符串
    direction: +1=做多, -1=做空
    返回 (is_filtered: bool, reason: str)
    """
    if not band:
        return False, ""
    entry = SENTIMENT_HARD_FILTER.get(band, {})
    if direction > 0 and entry.get("long", False):
        label = BAND_LABELS.get(band, band)
        return True, f"情绪硬过滤({label}禁做多)"
    if direction < 0 and entry.get("short", False):
        label = BAND_LABELS.get(band, band)
        return True, f"情绪硬过滤({label}禁做空)"
    return False, ""


# band → 中文标签映射
BAND_LABELS = {
    "extreme_greed": "极度贪婪",
    "greed": "贪婪",
    "neutral": "中性",
    "fear": "恐惧",
    "extreme_fear": "极度恐惧",
}

# ── 风控仓位缩放（极端情绪才缩仓，中性完全不动） ──
SENTIMENT_RISK_SCALE = {
    "extreme_greed": 0.75,    # 极度贪婪：缩仓 25%
    "greed": 0.92,            # 贪婪：轻微缩仓 8%
    "neutral": 1.00,          # 中性：完全不动
    "fear": 0.92,             # 恐惧：轻微缩仓 8%
    "extreme_fear": 0.75,     # 极度恐惧：缩仓 25%
}

# 缓存：runner 每轮 evaluate 时更新
_CACHE = {
    "score": 50.0,
    "label": "中性",
    "band": "neutral",
    "bias": 0.0,
    "scale": 1.0,
    "factors": {},
    "extreme_reason": None,
    "hard_filter": {
        "ban_long": False,
        "ban_short": False,
        "label": "无限制",
        "desc": "情绪正常，多空均可开仓",
    },
    "updated": None,
    "snapshots": {},  # sym -> {price, chg_pct, T_D, volume_ratio, group}
    "history": [],    # 最近 N 条情绪历史 [{ts, score, band, label}]
}

MAX_HISTORY = 60  # 保留最近 60 个采样点（约 30-60 分钟）

# 持久化路径（供 /api/sentiment 读取，前端面板消费）
CACHE_FILE = os.path.join(HERE, "sentiment_cache.json")

# 启动时从缓存恢复历史数据（重启不丢历史）
try:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as _f:
            _saved = json.load(_f)
            if isinstance(_saved.get("history"), list):
                _CACHE["history"] = _saved["history"][-MAX_HISTORY:]
except Exception:
    pass


def _label_for(score):
    for threshold, label, band in BANDS:
        if score >= threshold:
            return label, band
    return "极度恐惧", "extreme_fear"


def _thr_mult(band, direction):
    entry = SENTIMENT_THR_MULT.get(band, SENTIMENT_THR_MULT["neutral"])
    if direction > 0:
        return entry["long"]
    elif direction < 0:
        return entry["short"]
    return 1.0


def _risk_scale(band):
    return SENTIMENT_RISK_SCALE.get(band, 1.0)


# ── 单因子计算 ──

def _factor_breadth(snapshots):
    """市场广度：涨跌家数比。
    返回 0-100（50=涨跌平衡）。"""
    valid = [s for s in snapshots.values() if s.get("chg_pct") is not None]
    if not valid:
        return 50.0
    advances = sum(1 for s in valid if s["chg_pct"] > 0)
    declines = sum(1 for s in valid if s["chg_pct"] < 0)
    total = len(valid)
    if total == 0:
        return 50.0
    ratio = (advances - declines) / total  # -1 ~ +1
    return 50.0 + ratio * 50.0


def _factor_momentum(snapshots):
    """动量共识：全品种 T_D 加权均值。
    返回 0-100（50=多空平衡）。"""
    valid = [s for s in snapshots.values() if s.get("T_D") is not None]
    if not valid:
        return 50.0
    scores = [s["T_D"] for s in valid]
    avg = float(np.mean(scores))
    # T_D 量程约 [-100, 100]，直接映射
    return 50.0 + np.clip(avg, -50, 50)  # clamp to 0-100


def _factor_activity(snapshots):
    """资金活跃度：成交量 vs 20日均比。
    高于1=资金涌入（贪婪），低于1=资金观望（恐惧）。
    返回 0-100。"""
    valid = [s for s in snapshots.values() if s.get("volume_ratio") is not None and s["volume_ratio"] > 0]
    if not valid:
        return 50.0
    ratios = [s["volume_ratio"] for s in valid]
    avg_ratio = float(np.mean(ratios))
    # 1.0=中性(50), 2.0=极度活跃(100), 0.5=极度冷清(0)
    score = 50.0 + (avg_ratio - 1.0) * 50.0
    return float(np.clip(score, 0, 100))


def _factor_amplitude(snapshots):
    """涨跌幅度分布：大涨/大跌的比例。

    比单纯的涨跌家数更灵敏——全市场普涨2%以上=真正的情绪高涨。
    返回 0-100（50=平衡）。"""
    valid = [s for s in snapshots.values() if s.get("chg_pct") is not None]
    if not valid:
        return 50.0
    total = len(valid)
    if total == 0:
        return 50.0

    big_rise = sum(1 for s in valid if s["chg_pct"] > 0.02)    # 涨超 2%
    big_fall = sum(1 for s in valid if s["chg_pct"] < -0.02)   # 跌超 2%
    mid_up = sum(1 for s in valid if 0 < s["chg_pct"] <= 0.02) # 小幅涨
    mid_down = sum(1 for s in valid if -0.02 <= s["chg_pct"] < 0) # 小幅跌

    # 加权计分：大涨/大跌权重更高
    rise_score = big_rise * 2.0 + mid_up * 1.0
    fall_score = big_fall * 2.0 + mid_down * 1.0
    total_weight = rise_score + fall_score

    if total_weight == 0:
        return 50.0

    ratio = (rise_score - fall_score) / total_weight  # -1 ~ +1
    return 50.0 + ratio * 50.0


def _factor_trend_conc(snapshots):
    """趋势集中度：多少品种处于趋势 regime。

    趋势集中=市场有明确方向（可能贪婪或恐惧），
    震荡集中=市场迷茫（偏中性）。
    返回 0-100（50=中性）。"""
    valid = [s for s in snapshots.values() if s.get("regime")]
    if not valid:
        return 50.0
    total = len(valid)

    n_trend = sum(1 for s in valid if s["regime"] == "趋势")
    n_shock = sum(1 for s in valid if s["regime"] == "震荡")
    n_vol = sum(1 for s in valid if s["regime"] == "波动")

    if total == 0:
        return 50.0

    # 趋势比例越高，情绪越明确（方向看 T_D 均值）
    trend_ratio = n_trend / total
    # 震荡比例越高，情绪越低迷
    shock_ratio = n_shock / total

    # 趋势方向
    tds = [s.get("T_D", 0) for s in valid if s.get("T_D") is not None]
    avg_td = float(np.mean(tds)) if tds else 0

    # 趋势集中 + 向上 → 高（贪婪）
    # 趋势集中 + 向下 → 低（恐惧）
    # 震荡集中 → 中（中性）
    if trend_ratio > 0.6:
        # 趋势集中
        direction_bonus = np.clip(avg_td / 50.0, -1, 1) * 30.0
        score = 50.0 + direction_bonus
    elif shock_ratio > 0.6:
        # 震荡集中 → 迷茫，偏中性
        score = 50.0 + np.clip(avg_td / 100.0, -0.5, 0.5) * 10.0
    else:
        # 混合
        score = 50.0 + np.clip(avg_td / 50.0, -1, 1) * 15.0

    return float(np.clip(score, 0, 100))


def _factor_volatility(snapshots):
    """波动率状态：平均 ATR% vs 历史。
    低波动=自满(贪婪→高)，高波动=恐慌(→低)。
    返回 0-100。"""
    valid = [s for s in snapshots.values() if s.get("atr_pct") is not None and s["atr_pct"] > 0]
    if not valid:
        return 50.0
    avg_atr = float(np.mean([s["atr_pct"] for s in valid]))
    # 经验阈值：ATR% < 1.0%=低波动(贪婪), > 3.0%=高波动(恐惧)
    # 映射：atr_pct 0.5%→90, 1.5%→50, 3.0%→10
    score = 100.0 - (avg_atr - 0.005) / (0.03 - 0.005) * 80.0
    return float(np.clip(score, 0, 100))


def _factor_divergence(snapshots):
    """板块分歧：分组 T_D 方差 → 趋势一致性。
    分歧大=混乱(中性偏恐惧)，分歧小=一致(强趋势,可贪婪或恐惧)。
    返回 0-100。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for s in snapshots.values():
        g = s.get("group")
        td = s.get("T_D")
        if g and td is not None:
            groups[g].append(float(td))
    if len(groups) < 2:
        return 50.0
    group_avgs = [float(np.mean(v)) for v in groups.values() if v]
    if len(group_avgs) < 2:
        return 50.0
    spread = float(np.std(group_avgs))
    # spread 0=完全一致, 50=极度分歧
    # 一致时(低spread)→情绪明确→高分; 分歧时→混乱→低分
    # 但要注意：一致看空也是"明确"，不应直接给高分
    avg_direction = float(np.mean(group_avgs))
    # 如果方向一致且偏多→高; 一致偏空→中; 分歧→低
    if spread < 10:
        # 板块一致
        score = 50.0 + np.clip(avg_direction, -50, 50) * 0.5
    elif spread < 25:
        # 中度分歧
        score = 50.0 - (spread - 10) * 0.5
    else:
        # 极度分歧
        score = 30.0
    return float(np.clip(score, 0, 100))


def compute(snapshots=None):
    """计算综合市场情绪。

    参数：
      snapshots: dict {sym: {chg_pct, T_D, volume_ratio, atr_pct, group}}
                 None=返回缓存

    返回 dict:
      score: 0-100
      label: 极度恐惧/恐惧/中性/贪婪/极度贪婪
      band: extreme_fear/fear/neutral/greed/extreme_greed
      bias: -1~+1（正=贪婪，负=恐惧）
      scale: 0.5~1.0（风控仓位缩放）
      factors: {breadth, momentum, activity, volatility, divergence}
      updated: timestamp
    """
    if snapshots is None:
        return dict(_CACHE)

    factors = {}
    try:
        factors["breadth"] = _factor_breadth(snapshots)
    except Exception:
        factors["breadth"] = 50.0
    try:
        factors["momentum"] = _factor_momentum(snapshots)
    except Exception:
        factors["momentum"] = 50.0
    try:
        factors["activity"] = _factor_activity(snapshots)
    except Exception:
        factors["activity"] = 50.0
    try:
        factors["volatility"] = _factor_volatility(snapshots)
    except Exception:
        factors["volatility"] = 50.0
    try:
        factors["divergence"] = _factor_divergence(snapshots)
    except Exception:
        factors["divergence"] = 50.0
    try:
        factors["amplitude"] = _factor_amplitude(snapshots)
    except Exception:
        factors["amplitude"] = 50.0
    try:
        factors["trend_conc"] = _factor_trend_conc(snapshots)
    except Exception:
        factors["trend_conc"] = 50.0

    score = sum(WEIGHTS[k] * factors[k] for k in WEIGHTS)
    score = float(np.clip(score, 0, 100))

    # 极端事件检测：全市场一致性极强时直接标为极端
    extreme_reason = None
    valid_breadth = [s for s in snapshots.values() if s.get("chg_pct") is not None]
    if valid_breadth:
        total = len(valid_breadth)
        adv = sum(1 for s in valid_breadth if s["chg_pct"] > 0)
        dec = sum(1 for s in valid_breadth if s["chg_pct"] < 0)
        # 涨跌家数比超过阈值 → 极端
        if adv / total >= EXTREME_BREADTH_RATIO:
            score = max(score, 75.0)
            extreme_reason = f"普涨（{adv}/{total}）"
        elif dec / total >= EXTREME_BREADTH_RATIO:
            score = min(score, 25.0)
            extreme_reason = f"普跌（{dec}/{total}）"

        # 大涨/大跌比例超过阈值 → 极端
        big_rise = sum(1 for s in valid_breadth if s["chg_pct"] > 0.02)
        big_fall = sum(1 for s in valid_breadth if s["chg_pct"] < -0.02)
        if big_rise / total >= EXTREME_AMPLITUDE_RATIO:
            score = max(score, 78.0)
            extreme_reason = (extreme_reason + " + " if extreme_reason else "") + f"大涨{big_rise}只"
        elif big_fall / total >= EXTREME_AMPLITUDE_RATIO:
            score = min(score, 22.0)
            extreme_reason = (extreme_reason + " + " if extreme_reason else "") + f"大跌{big_fall}只"

    label, band = _label_for(score)
    bias = (score - 50.0) / 50.0  # -1 ~ +1
    scale = _risk_scale(band)

    # 硬过滤状态（供前端面板显示）
    _hf_entry = SENTIMENT_HARD_FILTER.get(band, {})
    _ban_long = _hf_entry.get("long", False)
    _ban_short = _hf_entry.get("short", False)
    if _ban_long and _ban_short:
        _hard_filter_label = "禁多空"
        _hard_filter_desc = f"{BAND_LABELS.get(band, band)}：双向禁止开仓"
    elif _ban_long:
        _hard_filter_label = "禁做多"
        _hard_filter_desc = f"{BAND_LABELS.get(band, band)}：禁止做多，仅可做空"
    elif _ban_short:
        _hard_filter_label = "禁做空"
        _hard_filter_desc = f"{BAND_LABELS.get(band, band)}：禁止做空，仅可做多"
    else:
        _hard_filter_label = "无限制"
        _hard_filter_desc = "情绪正常，多空均可开仓"

    result = {
        "score": round(score, 1),
        "label": label,
        "band": band,
        "bias": round(bias, 3),
        "scale": scale,
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "extreme_reason": extreme_reason,
        "hard_filter": {
            "ban_long": _ban_long,
            "ban_short": _ban_short,
            "label": _hard_filter_label,
            "desc": _hard_filter_desc,
        },
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshots": dict(snapshots) if snapshots else {},
    }

    _CACHE.update(result)

    # 追加历史记录（保留最近 MAX_HISTORY 条）
    _hist_entry = {
        "ts": result["updated"],
        "score": result["score"],
        "band": result["band"],
        "label": result["label"],
    }
    _CACHE["history"].append(_hist_entry)
    if len(_CACHE["history"]) > MAX_HISTORY:
        _CACHE["history"] = _CACHE["history"][-MAX_HISTORY:]

    _save_cache(result)
    return result


def _save_cache(result):
    try:
        out = {k: v for k, v in result.items() if k != "snapshots"}
        out["history"] = _CACHE.get("history", [])[-MAX_HISTORY:]
        out["snapshots_summary"] = {
            sym: {
                "chg_pct": round(s.get("chg_pct", 0), 2) if s.get("chg_pct") is not None else None,
                "T_D": round(s.get("T_D", 0), 1) if s.get("T_D") is not None else None,
                "group": s.get("group", ""),
            }
            for sym, s in (result.get("snapshots") or {}).items()
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_cache():
    """读取持久化缓存（供 API/面板消费）。"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"score": 50.0, "label": "中性", "band": "neutral", "bias": 0.0, "scale": 1.0}


def get_thr_mult(direction=0):
    """获取当前情绪对应的 T_thresh 乘数（供 pipeline 调用）。
    direction: +1=做多, -1=做空, 0=无方向（中性乘1.0）。"""
    band = _CACHE.get("band", "neutral")
    return _thr_mult(band, direction)


def get_risk_scale():
    """获取当前情绪对应的风控仓位缩放系数（供 risk_state_machine 调用）。"""
    return _CACHE.get("scale", 1.0)


def get_snapshot():
    """获取当前情绪快照（完整 dict）。"""
    return dict(_CACHE)


def build_snapshots_from_runner(state_symbols, feed, SYMBOLS):
    """从 runner 的 evaluate 循环中收集品种快照。

    参数：
      state_symbols: runner state["symbols"] dict {sym: {pipe, price, ...}}
      feed: minishare_feed 实例（获取实时价格）
      SYMBOLS: 品种元数据 dict（含 group/name）

    返回：snapshots dict，可直接喂 compute()
    """
    snapshots = {}
    for sym, sdata in (state_symbols or {}).items():
        pipe = sdata.get("pipe") or {}
        price = sdata.get("price")
        group = SYMBOLS.get(sym, {}).get("group", "")

        chg_pct = None
        if price and pipe.get("prev_close"):
            try:
                chg_pct = (price / pipe["prev_close"] - 1.0)
            except Exception:
                pass

        snapshots[sym] = {
            "chg_pct": chg_pct,
            "T_D": pipe.get("T_D"),
            "T_5m": pipe.get("T_5m"),
            "C": pipe.get("C"),
            "regime": pipe.get("regime", ""),
            "price": price,
            "group": group,
            "volume_ratio": pipe.get("volume_ratio"),
            "atr_pct": pipe.get("atr_pct"),
        }
    return snapshots
