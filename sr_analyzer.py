"""支撑压力位识别器（Support/Resistance Analyzer）
=================================================================
从日线 OHLCV 识别关键价格结构位，供 pipeline 信号质量过滤和
exit_plan 动态止盈止损参考。三重验证：局部极值 + 成交量剖面 +
多次触及确认。

红线（与 sentiment_engine / macro_context 一致）：
  - 本模块只服务于 live 信号，回测不调用（避免前视）。
  - 任何计算失败返回空列表，不影响 live 运行。
  - SR 位仅作为「信号质量过滤」和「止盈止损微调」，不直接改方向/手数。

算法：
  1. Swing High/Low：滚动窗口内局部极值（不用 scipy，纯 numpy 实现）
  2. 层级聚类：相近价位（±0.3%内）合并为一个结构位，取加权均值
  3. 强度评分：触及次数 × 成交量确认 × 时间新鲜度 → 0-100
  4. 角色判定：当前价上方=压力位，下方=支撑位
"""

from __future__ import annotations

import numpy as np

# ── 参数 ──
SWING_WINDOW = 5  # 局部极值窗口（左右各 5 根 K 线）
CLUSTER_PCT = 0.003  # 聚类阈值：±0.3% 内合并
MAX_LEVELS = 8  # 最多保留 N 个结构位（按强度排序）
MIN_TOUCHES = 2  # 最少触及次数（<2 丢弃）
VOLUME_CONFIRM_RATIO = 1.2  # 成交量确认：该位成交量 > 均量×1.2 加分
PROXIMITY_PCT = 0.008  # 近位判定：价格在结构位 ±0.8% 内算"接近"

# ── 逆向位危险区（方向感知过滤）──
# 逻辑：做多时离压力位太近 → 易被压回；做空时离支撑位太近 → 易被弹回
# 极近位（<0.3%）：逆向位极近可能是真突破，不惩罚
# 危险区（0.3% ~ 1.0%）：靠近但没突破，胜率最低，提高 T 阈值
# 安全区（>=1.0%）：离逆向位够远，正常阈值
HOSTILE_TIGHT_PCT = 0.003  # 极近位阈值：0.3%
HOSTILE_DANGER_LOW = 0.003  # 危险区下限：0.3%
HOSTILE_DANGER_HIGH = 0.010  # 危险区上限：1.0%
HOSTILE_DANGER_PENALTY = 0.30  # 危险区 T阈值惩罚：×1.30（提高门槛）
HOSTILE_TIGHT_BOOST = 0.00  # 极近位：不调整（真突破假设）

# ── 旧参数（保留兼容，现用逆向位方案替代）──
GREY_ZONE_LOW = 0.008
GREY_ZONE_HIGH = 0.016
GREY_ZONE_PENALTY = 0.25
NEAR_ZONE_BOOST = 0.00

TIME_DECAY_DAYS = 60  # 超过 60 天的极值权重衰减

# 缓存：sym -> {levels, nearest_support, nearest_resistance, updated}
_CACHE = {}


def _find_swing_extrema(df, window=SWING_WINDOW):
    """用纯 numpy 找局部极值（Swing High/Low）。

    返回 [(index, price, type, volume), ...]，type='high'/'low'。
    """
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    vol = df["volume"].astype(float).values if "volume" in df else np.ones(len(high))
    dates = df.index

    n = len(high)
    if n < window * 2 + 1:
        return []

    extrema = []
    for i in range(window, n - window):
        # Swing High
        left = high[i - window : i]
        right = high[i + 1 : i + 1 + window]
        if high[i] > left.max() and high[i] > right.max():
            extrema.append((dates[i], float(high[i]), "high", float(vol[i])))
        # Swing Low
        left_l = low[i - window : i]
        right_l = low[i + 1 : i + 1 + window]
        if low[i] < left_l.min() and low[i] < right_l.min():
            extrema.append((dates[i], float(low[i]), "low", float(vol[i])))

    return extrema


def _cluster_levels(extrema, cluster_pct=CLUSTER_PCT):
    """把相近的极值聚合成结构位。

    返回 [{price, touches, types, volumes, last_date, strength}, ...]
    """
    if not extrema:
        return []

    # 按价格排序
    sorted_ex = sorted(extrema, key=lambda x: x[1])
    clusters = []
    current = {
        "prices": [sorted_ex[0][1]],
        "touches": 1,
        "types": [sorted_ex[0][2]],
        "volumes": [sorted_ex[0][3]],
        "last_date": sorted_ex[0][0],
    }

    for i in range(1, len(sorted_ex)):
        _, price, typ, vol = sorted_ex[i]
        ref_price = np.mean(current["prices"])
        if abs(price - ref_price) / ref_price < cluster_pct:
            current["prices"].append(price)
            current["touches"] += 1
            current["types"].append(typ)
            current["volumes"].append(vol)
            if sorted_ex[i][0] > current["last_date"]:
                current["last_date"] = sorted_ex[i][0]
        else:
            clusters.append(_finalize_cluster(current))
            current = {
                "prices": [price],
                "touches": 1,
                "types": [typ],
                "volumes": [vol],
                "last_date": sorted_ex[i][0],
            }
    clusters.append(_finalize_cluster(current))
    return clusters


def _finalize_cluster(c):
    """计算聚合位的加权价格和强度。"""
    prices = np.array(c["prices"])
    vols = np.array(c["volumes"])
    # 成交量加权均价
    if vols.sum() > 0:
        price = float(np.average(prices, weights=vols))
    else:
        price = float(np.mean(prices))

    touches = c["touches"]
    types = c["types"]
    # 兼容性：既有 high 又有 low =更强的结构位（双面验证）
    has_high = "high" in types
    has_low = "low" in types
    dual_sided = has_high and has_low

    return {
        "price": round(price, 2),
        "touches": touches,
        "dual_sided": dual_sided,
        "avg_volume": float(np.mean(vols)) if len(vols) else 0,
        "last_date": c["last_date"],
        "strength": 0,  # 待计算
    }


def _score_strength(levels, df, current_price):
    """给每个结构位打分（0-100）。

    因子：
      - 触及次数（2次=30, 3次=50, 4+=70, 5+=90）
      - 成交量确认（该位均量 vs 全局均量）
      - 时间新鲜度（越近越强，60天外衰减）
      - 双面验证（high+low 共存加分）
    """
    if len(df) == 0:
        return levels

    global_vol = float(df["volume"].astype(float).mean()) if "volume" in df else 1.0
    latest_date = df.index[-1]

    for lv in levels:
        # 触及次数分
        touch_score = min(90, 20 + (lv["touches"] - 1) * 25)

        # 成交量确认分
        if global_vol > 0 and lv["avg_volume"] > 0:
            vol_ratio = lv["avg_volume"] / global_vol
            vol_score = min(20, vol_ratio / VOLUME_CONFIRM_RATIO * 10)
        else:
            vol_score = 0

        # 时间新鲜度分
        try:
            days_ago = (latest_date - lv["last_date"]).days
            if days_ago <= 20:
                time_score = 10
            elif days_ago <= TIME_DECAY_DAYS:
                time_score = 10 * (1 - (days_ago - 20) / TIME_DECAY_DAYS)
            else:
                time_score = 0
        except Exception:
            time_score = 5

        # 双面验证加分
        dual_bonus = 5 if lv["dual_sided"] else 0

        lv["strength"] = round(min(100, touch_score + vol_score + time_score + dual_bonus), 1)

    return levels


def _classify(levels, current_price):
    """按当前价分类：上方=压力位，下方=支撑位。"""
    for lv in levels:
        lv["role"] = "resistance" if lv["price"] > current_price else "support"
        lv["distance_pct"] = round(abs(lv["price"] - current_price) / current_price * 100, 2)
    return levels


def analyze(df, current_price=None):
    """识别支撑压力位。

    参数：
      df: 日线 DataFrame（需要 high, low, volume 列，DatetimeIndex）
      current_price: 当前价格（None=用 df close 最后值）

    返回 dict:
      levels: [{price, role, strength, touches, distance_pct, dual_sided}, ...]
      nearest_support: 最近的支撑位（price, strength, distance_pct）
      nearest_resistance: 最近的压力位
      at_support: 是否接近支撑位（distance < PROXIMITY_PCT）
      at_resistance: 是否接近压力位
      current_price: 使用的当前价
    """
    if df is None or len(df) < SWING_WINDOW * 2 + 5:
        return _empty_result(current_price)

    if current_price is None:
        try:
            current_price = float(df["close"].iloc[-1])
        except Exception:
            return _empty_result(None)

    # 1. 找局部极值
    extrema = _find_swing_extrema(df)

    # 2. 聚类
    levels = _cluster_levels(extrema)

    # 3. 过滤弱位
    levels = [lv for lv in levels if lv["touches"] >= MIN_TOUCHES]

    if not levels:
        return _empty_result(current_price)

    # 4. 打分
    levels = _score_strength(levels, df, current_price)

    # 5. 分类
    levels = _classify(levels, current_price)

    # 6. 排序 & 截断
    levels.sort(key=lambda x: -x["strength"])
    levels = levels[:MAX_LEVELS]

    # 找最近支撑/压力
    supports = [lv for lv in levels if lv["role"] == "support"]
    resistances = [lv for lv in levels if lv["role"] == "resistance"]

    nearest_support = min(supports, key=lambda x: x["distance_pct"]) if supports else None
    nearest_resistance = min(resistances, key=lambda x: x["distance_pct"]) if resistances else None

    at_support = nearest_support and nearest_support["distance_pct"] < PROXIMITY_PCT * 100
    at_resistance = nearest_resistance and nearest_resistance["distance_pct"] < PROXIMITY_PCT * 100

    # 价格区间分类
    sup_dist = nearest_support["distance_pct"] if nearest_support else 99.0
    res_dist = nearest_resistance["distance_pct"] if nearest_resistance else 99.0
    nearest_dist = min(sup_dist, res_dist)
    nearest_dist_frac = nearest_dist / 100.0
    if nearest_dist_frac < GREY_ZONE_LOW:
        zone = "near"
        zone_label = "近位区"
    elif nearest_dist_frac < GREY_ZONE_HIGH:
        zone = "grey"
        zone_label = "灰色地带"
    else:
        zone = "far"
        zone_label = "远位区"

    return {
        "levels": levels,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "at_support": bool(at_support),
        "at_resistance": bool(at_resistance),
        "zone": zone,
        "zone_label": zone_label,
        "nearest_dist_pct": round(nearest_dist, 2),
        "current_price": current_price,
    }


def _empty_result(current_price):
    return {
        "levels": [],
        "nearest_support": None,
        "nearest_resistance": None,
        "at_support": False,
        "at_resistance": False,
        "zone": "far",
        "zone_label": "无数据",
        "nearest_dist_pct": 99.0,
        "current_price": current_price,
    }


# ── pipeline 集成接口 ──


def signal_quality_boost(sr_result, direction):
    """根据 SR 位给信号质量加分/减分（逆向位方向感知版 v2）。

    核心逻辑（基于回测数据验证）：
      做多时关注压力位、做空时关注支撑位 → 这是"逆向位"（与交易方向相反的关键位）
      - 极近位（距逆向位 < 0.3%）：可能是真突破，正常阈值
      - 危险区（距逆向位 0.3% ~ 1.0%）：靠近但没突破，胜率最低 → 提高 T 阈值（×1.3）
      - 安全区（距逆向位 >= 1.0%）：离逆向位够远，正常阈值

    direction: +1=做多, -1=做空
    返回 (boost, reason)：
      boost: -0.3 ~ 0.0（T_thresh_eff 的乘数偏移，负=提高门槛）
      reason: 文字描述
    """
    if not sr_result or not sr_result.get("levels"):
        return 0.0, ""

    ns = sr_result.get("nearest_support")
    nr = sr_result.get("nearest_resistance")

    sup_dist = ns["distance_pct"] if ns else 99.0
    res_dist = nr["distance_pct"] if nr else 99.0

    # 逆向位：做多→压力位，做空→支撑位
    if direction > 0:
        hostile_dist = res_dist
        hostile_lv = nr
        hostile_cn = "压力"
    else:
        hostile_dist = sup_dist
        hostile_lv = ns
        hostile_cn = "支撑"

    hostile_frac = hostile_dist / 100.0  # 转小数

    if hostile_frac < HOSTILE_TIGHT_PCT:
        # 极近位：逆向位极近，可能是真突破
        price = hostile_lv["price"] if hostile_lv else "?"
        return HOSTILE_TIGHT_BOOST, f"近{hostile_cn}位突破区({hostile_dist:.1f}%)"
    elif hostile_frac < HOSTILE_DANGER_HIGH:
        # 危险区：靠近逆向位但没突破，胜率最低
        price = hostile_lv["price"] if hostile_lv else "?"
        return -HOSTILE_DANGER_PENALTY, f"危险区({hostile_dist:.1f}%近{hostile_cn}位，提高门槛)"
    else:
        # 安全区：离逆向位够远
        return 0.0, f"离{hostile_cn}位安全({hostile_dist:.1f}%)"


def adjust_exit_plan(exit_dict, sr_result, direction, entry_price):
    """用 SR 位微调止盈止损。

    策略：
      - 止损：如果 SR 位比 ATR 止损更紧（且不超 0.8×ATR止损），用 SR 位
      - 止盈 T1：如果 SR 位在 1R 内，用 SR 位做目标
      - 止盈 T2：保持 R 倍数不变（趋势利润不让 SR 位限制）
    """
    if not sr_result or not sr_result.get("levels"):
        return exit_dict

    adjusted = dict(exit_dict)
    orig_stop = exit_dict.get("stop", 0)
    orig_t1 = exit_dict.get("t1", 0)
    stop_dist = exit_dict.get("stop_dist", 0)

    if direction > 0:  # 做多
        # 止损参考支撑位
        ns = sr_result.get("nearest_support")
        if ns and stop_dist > 0:
            sr_stop = ns["price"]
            sr_dist = entry_price - sr_stop
            # SR 止损更紧（但不少于 0.5×ATR 止损，避免太紧被扫）
            if 0 < sr_dist < stop_dist and sr_dist > 0.5 * stop_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop"] = True
        # T1 参考压力位
        nr = sr_result.get("nearest_resistance")
        if nr and stop_dist > 0:
            sr_target = nr["price"]
            sr_target_dist = sr_target - entry_price
            if 0 < sr_target_dist < stop_dist * 2:  # 在 2R 内的压力位做 T1
                adjusted["t1"] = round(sr_target, 2)
                adjusted["sr_t1"] = True

    elif direction < 0:  # 做空
        nr = sr_result.get("nearest_resistance")
        if nr and stop_dist > 0:
            sr_stop = nr["price"]
            sr_dist = sr_stop - entry_price
            if 0 < sr_dist < stop_dist and sr_dist > 0.5 * stop_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop"] = True
        ns = sr_result.get("nearest_support")
        if ns and stop_dist > 0:
            sr_target = ns["price"]
            sr_target_dist = entry_price - sr_target
            if 0 < sr_target_dist < stop_dist * 2:
                adjusted["t1"] = round(sr_target, 2)
                adjusted["sr_t1"] = True

    return adjusted


# ── #9 SR 位放宽止损（v2：分板块差异化配置）──
# 回测验证：全局平均 +18.2%（2.5R），但板块差异大，分板块配置更优
# None = 不启用放宽止损（该板块 SR 位反而有害）
SR_WIDEN_STOP_GROUP_CONFIG = {
    "农产品": 2.5,  # 2.5R · 提升 +2383%（16品种）
    "化工": 2.5,  # 2.5R · 提升 +16.0%（16品种）
    "有色": 1.8,  # 1.8R · 提升 +102%（5品种）
    "能源": 1.8,  # 1.8R · 提升 +76.5%（3品种）
    "黑系": 1.5,  # 1.5R · 提升 +2.4%（6品种，保守）
    "航运": None,  # 不启用 · 反而有害
    "贵金属": None,  # 不启用 · 无效果
}

# 默认值（不在上面列表的板块）
SR_WIDEN_STOP_DEFAULT = None  # 默认不启用，保守起见


def get_widen_stop_mult(symbol, symbol_meta=None):
    """获取某个品种的放宽止损倍数。

    返回 None = 不启用，float = 最大放宽倍数（×ATR止损）
    """
    if symbol_meta is None:
        return SR_WIDEN_STOP_DEFAULT
    group = symbol_meta.get("group", "")
    return SR_WIDEN_STOP_GROUP_CONFIG.get(group, SR_WIDEN_STOP_DEFAULT)


def widen_stop_with_sr(exit_dict, sr_result, direction, entry_price, max_mult=2.0):
    """用 SR 位放宽止损（把止损移到更外侧的支撑/压力位）。

    做多：找 entry 下方最近的支撑位，如果它比 ATR 止损更远 → 放宽止损
    做空：找 entry 上方最近的压力位，如果它比 ATR 止损更远 → 放宽止损

    约束：最多放宽到 max_mult × ATR 止损。

    返回调整后的 dict，新增 sr_stop_widen=True 表示被调整过。
    """
    if not sr_result or not sr_result.get("levels") or max_mult is None:
        return exit_dict

    adjusted = dict(exit_dict)
    stop_dist = exit_dict.get("stop_dist", 0)
    if stop_dist <= 0:
        return exit_dict

    max_widen_dist = stop_dist * max_mult

    if direction > 0:  # 做多
        ns = sr_result.get("nearest_support")
        if ns:
            sr_stop = ns["price"]
            sr_dist = entry_price - sr_stop
            # SR 支撑位比 ATR 止损更远（更靠下），且不超过上限
            if sr_dist > stop_dist and sr_dist <= max_widen_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop_widen"] = True

    elif direction < 0:  # 做空
        nr = sr_result.get("nearest_resistance")
        if nr:
            sr_stop = nr["price"]
            sr_dist = sr_stop - entry_price
            if sr_dist > stop_dist and sr_dist <= max_widen_dist:
                adjusted["stop"] = round(sr_stop, 2)
                adjusted["stop_dist"] = round(sr_dist, 2)
                adjusted["sr_stop_widen"] = True

    return adjusted


def compute_and_cache(symbol, df_daily, current_price=None):
    """计算并缓存 SR 位（runner 每轮 evaluate 调用一次）。"""
    try:
        result = analyze(df_daily, current_price)
        _CACHE[symbol] = result
        return result
    except Exception:
        _CACHE[symbol] = _empty_result(current_price)
        return _CACHE[symbol]


def get_cached(symbol):
    """获取缓存的 SR 分析结果。"""
    return _CACHE.get(symbol, _empty_result(None))
