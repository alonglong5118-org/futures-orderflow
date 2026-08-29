"""
基本面因子库 — 期限结构 / 库存深化 / 产业利润

因子分类：
1. 期限结构因子（Basis / Term Structure）
   - basis_rate: 基差率（现货-期货）/ 现货价
   - basis_z: 基差率 z-score（相对历史滚动窗口）
   - basis_trend: 基差率 N 日变化（期限结构陡化/平化）

2. 库存因子深化（Inventory）
   - inv_level_z: 库存水平 z-score（当前库存相对历史分位）
   - inv_mom: 库存环比变化率（最近一期变化 / 当前库存）
   - inv_speed: 累库/去库速度（近 N 期平均变化率）

3. 产业利润因子（Industry Profit）
   - profit_z: 盘面利润 z-score（跨品种价差/比价）
   - profit_trend: 利润 N 日变化

所有因子返回值范围：[-100, +100]，正值=利多（看涨），负值=利空（看跌）。

数据源：
- 基差/库存：fundamentals.json（akshare 拉取）
- 产业利润：期货收盘价计算（跨品种比价/价差）
"""

import bisect
import json
import math
import os

import numpy as np

# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
_FUND_DATA = None
_FUND_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fundamentals.json")


def _load_fundamentals():
    """加载 fundamentals.json（懒加载 + 缓存）。"""
    global _FUND_DATA
    if _FUND_DATA is not None:
        return _FUND_DATA
    try:
        with open(_FUND_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        _FUND_DATA = raw.get("symbols", raw)
    except Exception:
        _FUND_DATA = {}
    return _FUND_DATA


def has_basis_data(symbol):
    """是否有该品种的基差数据。"""
    data = _load_fundamentals()
    sym_data = data.get(symbol, {})
    bs = sym_data.get("basis_series", [])
    return len(bs) > 20


def has_inventory_data(symbol):
    """是否有该品种的库存数据。"""
    data = _load_fundamentals()
    sym_data = data.get(symbol, {})
    inv = sym_data.get("inventory", [])
    return len(inv) > 5


# ---------------------------------------------------------------------------
# 辅助：日期对齐
# ---------------------------------------------------------------------------
def _find_basis_at(symbol, date_int):
    """在基差序列中找到 date_int（YYYYMMDD int）对应的数据，返回 dict 或 None。"""
    data = _load_fundamentals()
    sym_data = data.get(symbol, {})
    bs = sym_data.get("basis_series", [])
    if not bs:
        return None
    dates = [int(d["date"]) for d in bs]
    idx = bisect.bisect_right(dates, date_int) - 1
    if idx < 0:
        return None
    return bs[idx]


def _find_inventory_at(symbol, date_str):
    """在库存序列中找到 date_str（'YYYY-MM-DD'）之前最近的一条数据，返回 dict 或 None。"""
    data = _load_fundamentals()
    sym_data = data.get(symbol, {})
    inv = sym_data.get("inventory", [])
    if not inv:
        return None
    dates = [d["date"] for d in inv]
    idx = bisect.bisect_right(dates, date_str) - 1
    if idx < 0:
        return None
    return inv[idx]


def _rolling_zscore(series, window=60):
    """计算序列的滚动 z-score。返回与 series 等长的数组。"""
    n = len(series)
    result = np.full(n, np.nan)
    for i in range(window, n):
        window_data = series[i - window : i]
        mean = np.mean(window_data)
        std = np.std(window_data)
        if std > 1e-10:
            z = (series[i] - mean) / std
            result[i] = z
    return result


def _clip_to_100(val):
    """将值截断到 [-100, 100]。"""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return 0.0
    return max(-100.0, min(100.0, val))


# ===========================================================================
# 1. 期限结构因子
# ===========================================================================


def basis_rate_factor(symbol, date_int, z_window=60, trend_days=10):
    """基差率因子（期限结构）。

    返回 dict:
      - basis_rate: 基差率 z-score × 20（映射到 ±100 区间）
        · 正基差（backwardation，现货升水）→ 正分（利多）
        · 负基差（contango，现货贴水）→ 负分（利空）
      - basis_trend: 基差率 N 日变化 × 系数（结构变化方向）
        · 基差扩大（backwardation 加深 / contango 收窄）→ 正分
        · 基差收窄（contango 加深 / backwardation 收窄）→ 负分
    """
    if not has_basis_data(symbol):
        return {"basis_rate": 0.0, "basis_trend": 0.0}

    data = _load_fundamentals()
    bs = data[symbol].get("basis_series", [])
    if len(bs) < z_window + trend_days:
        return {"basis_rate": 0.0, "basis_trend": 0.0}

    dates = [int(d["date"]) for d in bs]
    rates = [d.get("dom_basis_rate", 0) for d in bs]

    # 找到当前索引
    idx = bisect.bisect_right(dates, date_int) - 1
    if idx < z_window:
        return {"basis_rate": 0.0, "basis_trend": 0.0}

    # 滚动 z-score
    window_rates = rates[idx - z_window : idx]
    mean = np.mean(window_rates)
    std = np.std(window_rates)
    if std < 1e-8:
        z = 0.0
    else:
        z = (rates[idx] - mean) / std
    basis_rate_score = _clip_to_100(z * 20.0)  # z=5 → 100

    # 趋势（N 日变化）
    if idx >= trend_days:
        change = rates[idx] - rates[idx - trend_days]
        # 变化放大：1% 的基差变化 → 约 20 分
        basis_trend_score = _clip_to_100(change * 2000.0)
    else:
        basis_trend_score = 0.0

    return {
        "basis_rate": basis_rate_score,
        "basis_trend": basis_trend_score,
    }


# ===========================================================================
# 2. 库存因子深化
# ===========================================================================


def inventory_factors(symbol, date_str, z_window=30, speed_periods=4):
    """库存深化因子。

    参数:
      - z_window: 计算库存水平 z-score 的历史窗口（期数）
      - speed_periods: 计算累库/去库速度的期数

    返回 dict:
      - inv_level: 库存水平 z-score × 20（低库存=正分，高库存=负分）
      - inv_mom: 库存环比变化率 × 系数（去库=正分，累库=负分）
      - inv_speed: 近 N 期平均去库/累库速度（去库=正分）
    """
    if not has_inventory_data(symbol):
        return {"inv_level": 0.0, "inv_mom": 0.0, "inv_speed": 0.0}

    data = _load_fundamentals()
    inv = data[symbol].get("inventory", [])
    if len(inv) < z_window:
        return {"inv_level": 0.0, "inv_mom": 0.0, "inv_speed": 0.0}

    dates = [d["date"] for d in inv]
    stocks = [float(d.get("stock", 0) or 0) for d in inv]

    # 找到当前索引（最近一期库存数据）
    idx = bisect.bisect_right(dates, date_str) - 1
    if idx < z_window:
        return {"inv_level": 0.0, "inv_mom": 0.0, "inv_speed": 0.0}

    current_stock = stocks[idx]
    if current_stock <= 0:
        return {"inv_level": 0.0, "inv_mom": 0.0, "inv_speed": 0.0}

    # 库存水平 z-score（低库存利多 → 负z → 正分）
    window_stocks = stocks[idx - z_window : idx]
    mean_s = np.mean(window_stocks)
    std_s = np.std(window_stocks)
    if std_s > 1e-6:
        z = (current_stock - mean_s) / std_s
        inv_level_score = _clip_to_100(-z * 20.0)  # 低库存 → 正分
    else:
        inv_level_score = 0.0

    # 环比变化率（去库 → 正分）
    if idx > 0 and stocks[idx - 1] > 0:
        chg_rate = (stocks[idx] - stocks[idx - 1]) / stocks[idx - 1]
        inv_mom_score = _clip_to_100(-chg_rate * 500.0)  # -20% → +100
    else:
        inv_mom_score = 0.0

    # 累库/去库速度（近 N 期平均变化率，去库 → 正分）
    if idx >= speed_periods:
        changes = []
        for i in range(idx - speed_periods + 1, idx + 1):
            if stocks[i - 1] > 0:
                changes.append((stocks[i] - stocks[i - 1]) / stocks[i - 1])
        if changes:
            avg_chg = np.mean(changes)
            inv_speed_score = _clip_to_100(-avg_chg * 800.0)  # 平均-12.5%/期 → +100
        else:
            inv_speed_score = 0.0
    else:
        inv_speed_score = 0.0

    return {
        "inv_level": inv_level_score,
        "inv_mom": inv_mom_score,
        "inv_speed": inv_speed_score,
    }


# ===========================================================================
# 3. 产业利润因子
# ===========================================================================

# 产业利润定义：{name: (product_sym, [(raw_sym, ratio), ...], fixed_cost)}
# 利润 = product - Σ(raw × ratio) - fixed
# 正值 → 利润高（通常对应供给增加压力，偏空？需实证检验）
PROFIT_DEFS = {
    # 黑系
    "steel_iron": {
        "name": "螺矿比（螺纹钢/铁矿石）",
        "product": "rb",
        "raws": [("i", 1.0 / 1.6)],  # 吨钢耗约 1.6 吨铁矿
        "fixed": 0,
        "kind": "ratio",  # 比价
        "group": "黑系",
    },
    "coke_coal": {
        "name": "焦煤焦炭比",
        "product": "J",
        "raws": [("JM", 1.33)],  # 吨焦耗 1.33 吨焦煤
        "fixed": 0,
        "kind": "ratio",
        "group": "黑系",
    },
    # 化工
    "pta_process": {
        "name": "PTA加工费（PTA - 0.66×PX）",
        "product": "TA",
        "raws": [("PX", 0.66)],
        "fixed": 0,
        "kind": "spread",
        "group": "化工",
    },
    "pf_process": {
        "name": "短纤加工费（PF - 0.86×PTA）",
        "product": "PF",
        "raws": [("TA", 0.86)],
        "fixed": 0,
        "kind": "spread",
        "group": "化工",
    },
    "glass_soda": {
        "name": "玻璃纯碱比",
        "product": "FG",
        "raws": [("SA", 0.2)],
        "fixed": 0,
        "kind": "ratio",
        "group": "建材",
    },
    # 农产品
    "soy_oil_meal": {
        "name": "油粕比",
        "product": "y",
        "raws": [("m", 1.0)],
        "fixed": 0,
        "kind": "ratio",
        "group": "农产品",
    },
    "corn_starch": {
        "name": "玉米淀粉价差",
        "product": "cs",
        "raws": [("c", 1.0)],
        "fixed": 0,
        "kind": "spread",
        "group": "农产品",
    },
    # 有色
    "copper_zinc": {
        "name": "铜锌比价",
        "product": "cu",
        "raws": [("zn", 1.0)],
        "fixed": 0,
        "kind": "ratio",
        "group": "有色",
    },
    "aluminum_zinc": {
        "name": "铝锌比价",
        "product": "al",
        "raws": [("zn", 1.0)],
        "fixed": 0,
        "kind": "ratio",
        "group": "有色",
    },
}


def _get_close_at(symbol, date_int, df_cache=None):
    """获取某品种在 date_int（YYYYMMDD int）的收盘价。"""
    if df_cache is None or symbol not in df_cache:
        from four_dim_strategy import load_daily  # 延迟导入

        df = load_daily(symbol)
        if df is None:
            return None, None
        df_cache[symbol] = df
    df = df_cache.get(symbol)
    if df is None:
        return None, None

    # date_int → 行索引
    dates = df["date"].values if "date" in df.columns else df.index.values
    # 尝试转为 int
    try:
        date_ints = [int(d) for d in dates]
    except (ValueError, TypeError):
        date_ints = [int(str(d).replace("-", "")[:8]) for d in dates]

    idx = bisect.bisect_right(date_ints, date_int) - 1
    if idx < 0:
        return None, date_ints

    close = df["close"].values[idx]
    return float(close), date_ints


def profit_factor(symbol, date_int, profit_key, z_window=60, trend_days=10, df_cache=None):
    """产业利润因子。

    参数:
      - profit_key: PROFIT_DEFS 中的键
      - z_window: 滚动 z-score 窗口
      - trend_days: 趋势计算天数

    返回 dict:
      - profit_z: 利润 z-score × 20（映射到 ±100）
        · 高利润 → 正分还是负分？由因子检验确定方向
      - profit_trend: 利润 N 日变化 × 系数
        · 利润扩大 → 正分
        · 利润收窄 → 负分
    """
    if profit_key not in PROFIT_DEFS:
        return {"profit_z": 0.0, "profit_trend": 0.0}

    pdef = PROFIT_DEFS[profit_key]
    product = pdef["product"]
    raws = pdef["raws"]
    kind = pdef["kind"]

    if df_cache is None:
        df_cache = {}

    # 获取各品种收盘价序列（需要足够长的历史）
    all_syms = [product] + [s for s, _ in raws]
    price_data = {}
    min_len = float("inf")

    for sym in all_syms:
        from four_dim_strategy import load_daily  # 延迟导入

        df = load_daily(sym)
        if df is None or len(df) < z_window + trend_days + 10:
            return {"profit_z": 0.0, "profit_trend": 0.0}

        closes = df["close"].values.astype(float)
        # 日期转 int
        if "date" in df.columns:
            dates = [int(str(d).replace("-", "")[:8]) for d in df["date"].values]
        else:
            dates = [int(str(d).replace("-", "")[:8]) for d in df.index.values]

        price_data[sym] = (dates, closes)
        min_len = min(min_len, len(closes))

    if min_len < z_window + trend_days + 10:
        return {"profit_z": 0.0, "profit_trend": 0.0}

    # 对齐日期，计算每日利润/比价序列
    # 以 product 的日期为主
    prod_dates, prod_closes = price_data[product]

    # 找到 date_int 对应的 product 索引
    prod_idx = bisect.bisect_right(prod_dates, date_int) - 1
    if prod_idx < z_window + trend_days:
        return {"profit_z": 0.0, "profit_trend": 0.0}

    # 计算每一日的利润/比价
    profit_series = []
    for i in range(prod_idx - z_window - trend_days + 1, prod_idx + 1):
        d = prod_dates[i]
        p_price = prod_closes[i]

        # 获取各 raw 在同一日期的价格
        ok = True
        profit_val = p_price if kind == "spread" else p_price
        for raw_sym, ratio in raws:
            raw_dates, raw_closes = price_data[raw_sym]
            ri = bisect.bisect_right(raw_dates, d) - 1
            if ri < 0:
                ok = False
                break
            r_price = raw_closes[ri]
            if kind == "spread":
                profit_val -= ratio * r_price
            elif kind == "ratio":
                profit_val = profit_val / r_price if r_price > 0 else profit_val

        if ok:
            profit_series.append(profit_val)
        else:
            profit_series.append(np.nan)

    profit_series = np.array(profit_series, dtype=float)
    valid = ~np.isnan(profit_series)
    if valid.sum() < z_window:
        return {"profit_z": 0.0, "profit_trend": 0.0}

    # z-score（用最后 z_window 个有效数据）
    valid_indices = np.where(valid)[0]
    if len(valid_indices) < z_window:
        return {"profit_z": 0.0, "profit_trend": 0.0}

    window_vals = profit_series[valid_indices[-z_window:]]
    mean_p = np.mean(window_vals)
    std_p = np.std(window_vals)
    current_val = profit_series[-1]
    if std_p > 1e-10 and not math.isnan(current_val):
        z = (current_val - mean_p) / std_p
        profit_z_score = _clip_to_100(z * 20.0)
    else:
        profit_z_score = 0.0

    # 趋势：当前值 vs trend_days 前的值
    if len(valid_indices) >= trend_days + 1:
        current_idx = valid_indices[-1]
        # 找 trend_days 前的有效值
        target_idx = current_idx - trend_days
        # 找到最接近 target_idx 的 valid index
        past_valid = [vi for vi in valid_indices if vi <= target_idx]
        if past_valid:
            past_val = profit_series[past_valid[-1]]
            if not math.isnan(past_val) and abs(past_val) > 1e-10:
                change = (current_val - past_val) / abs(past_val)
                profit_trend_score = _clip_to_100(change * 200.0)
            else:
                profit_trend_score = 0.0
        else:
            profit_trend_score = 0.0
    else:
        profit_trend_score = 0.0

    return {
        "profit_z": profit_z_score,
        "profit_trend": profit_trend_score,
    }


# ===========================================================================
# 统一接口：计算某品种某日的所有基本面新因子
# ===========================================================================

NEW_FUND_FACTOR_NAMES = [
    "basis_rate",
    "basis_trend",  # 期限结构
    "inv_level",
    "inv_mom",
    "inv_speed",  # 库存深化
    # 产业利润因子按品种匹配（见 get_profit_key_for_symbol）
]


def get_profit_key_for_symbol(symbol):
    """获取某品种对应的产业利润因子 key（如果它是某个利润定义的 product）。"""
    for key, pdef in PROFIT_DEFS.items():
        if pdef["product"] == symbol:
            return key
    return None


def compute_all_fund_factors(symbol, date_int, date_str=None, df_cache=None):
    """计算某品种在某日的所有基本面新因子值。

    参数:
      - symbol: 品种代码
      - date_int: 日期 int（YYYYMMDD）
      - date_str: 日期 str（'YYYY-MM-DD'），用于库存数据对齐
      - df_cache: 价格数据缓存 dict（可选，加速批量计算）

    返回 dict: {factor_name: float_score}，分数范围 [-100, +100]
    """
    if date_str is None:
        date_str = f"{date_int // 10000}-{(date_int // 100) % 100:02d}-{date_int % 100:02d}"

    result = {}

    # 1. 期限结构因子
    basis = basis_rate_factor(symbol, date_int)
    result["basis_rate"] = basis["basis_rate"]
    result["basis_trend"] = basis["basis_trend"]

    # 2. 库存深化因子
    inv = inventory_factors(symbol, date_str)
    result["inv_level"] = inv["inv_level"]
    result["inv_mom"] = inv["inv_mom"]
    result["inv_speed"] = inv["inv_speed"]

    # 3. 产业利润因子（仅当品种是某个利润定义的主产品时）
    profit_key = get_profit_key_for_symbol(symbol)
    if profit_key:
        pf = profit_factor(symbol, date_int, profit_key, df_cache=df_cache)
        result["profit_z"] = pf["profit_z"]
        result["profit_trend"] = pf["profit_trend"]
    else:
        result["profit_z"] = 0.0
        result["profit_trend"] = 0.0

    return result


# 所有可用的基本面新因子名（含利润）
ALL_FUND_FACTOR_NAMES = [
    "basis_rate",
    "basis_trend",
    "inv_level",
    "inv_mom",
    "inv_speed",
    "profit_z",
    "profit_trend",
]


# ===========================================================================
# 增强版 F 因子：分板块差异化权重
# ===========================================================================

# 板块级因子权重配置（基于 IC 检验结果优化）
# 设计原则：
#   - IC 高的因子给更高权重
#   - 方向一致的板块正向加权，反向的反向加权或降权
#   - 所有子因子权重之和 = 1.0（内部归一），输出仍为 [-100, +100]
#   - 无数据的因子自动降级为 0（不影响其他因子）

SECTOR_FACTOR_WEIGHTS = {
    # 农产品：期限结构最强，库存动量次之，利润反向
    "农产品": {
        "basis_rate": 0.30,
        "basis_trend": 0.30,
        "inv_mom": 0.20,
        "inv_speed": 0.10,
        "profit_z": -0.10,  # 反向因子：负权重=反向使用
    },
    # 有色：库存最强，利润也正向，期限结构反向
    "有色": {
        "inv_mom": 0.35,
        "inv_speed": 0.25,
        "profit_z": 0.15,
        "profit_trend": 0.10,
        "basis_rate": 0.05,  # 弱反向，给小权重
        "basis_trend": 0.10,
    },
    # 能源：期限结构最强
    "能源": {
        "basis_rate": 0.40,
        "basis_trend": 0.35,
        "inv_mom": 0.15,
        "inv_speed": 0.10,
    },
    # 黑系：整体偏弱，库存速度强反向 → 作为反向过滤器
    # 注意：权重较小，黑系主要靠技术面和政策面驱动
    "黑系": {
        "basis_rate": 0.20,
        "basis_trend": 0.15,
        "inv_mom": 0.25,
        "inv_speed": -0.20,  # 反向因子
        "profit_z": -0.20,  # 反向因子
    },
    # 化工：因子效果一般，均衡配置
    "化工": {
        "basis_rate": 0.20,
        "basis_trend": 0.15,
        "inv_mom": 0.25,
        "inv_speed": 0.15,
        "profit_z": 0.15,
        "profit_trend": 0.10,
    },
    # 贵金属：只有基差数据，效果一般
    "贵金属": {
        "basis_rate": 0.45,
        "basis_trend": 0.55,
    },
    # 建材：默认均衡
    "建材": {
        "basis_rate": 0.25,
        "basis_trend": 0.25,
        "inv_mom": 0.25,
        "inv_speed": 0.25,
    },
}

# 默认权重（兜底用）
DEFAULT_FACTOR_WEIGHTS = {
    "basis_rate": 0.20,
    "basis_trend": 0.20,
    "inv_mom": 0.20,
    "inv_speed": 0.15,
    "profit_z": 0.15,
    "profit_trend": 0.10,
}


def compute_enhanced_F(symbol, date_int, date_str=None, sector=None, df_cache=None):
    """计算增强版 F 分数（分板块差异化权重）。

    参数:
      - symbol: 品种代码
      - date_int: 日期 int（YYYYMMDD）
      - date_str: 日期 str（'YYYY-MM-DD'），可选
      - sector: 板块名，可选。为 None 时从 SYMBOLS 自动获取
      - df_cache: 价格数据缓存 dict（可选，加速批量计算）

    返回: float, F 分数 [-100, +100]
    """
    # 获取板块
    if sector is None:
        from four_dim_strategy import SYMBOLS

        sector = SYMBOLS.get(symbol, {}).get("group", "其他")

    # 获取权重配置
    weights = SECTOR_FACTOR_WEIGHTS.get(sector, DEFAULT_FACTOR_WEIGHTS)

    # 计算所有子因子
    factors = compute_all_fund_factors(symbol, date_int, date_str=date_str, df_cache=df_cache)

    # 加权求和
    total_weight = 0.0
    f_score = 0.0

    for fname, w in weights.items():
        fval = factors.get(fname, 0.0)
        if fval is None or (isinstance(fval, float) and fval == 0.0 and fname not in ("basis_rate",)):
            # 因子为 0 可能是无数据，跳过该因子的权重
            # 注意：basis_rate 为 0 可能真的是中性，不跳过
            continue
        f_score += w * fval
        total_weight += abs(w)

    if total_weight > 0:
        f_score = f_score / total_weight * 2.0  # 放大使合理因子组合能达到 ±100
        f_score = max(-100.0, min(100.0, f_score))

    return round(f_score, 2)


def precompute_enhanced_F_array(symbol, date_ints=None, date_strs=None, sector=None):
    """批量预计算增强版 F 数组（O(n) 双指针加速）。

    参数:
      - symbol: 品种代码
      - date_ints: numpy array of int (YYYYMMDD)，优先级高
      - date_strs: list of str，可选
      - sector: 板块名，可选

    返回: numpy array of float, F 分数
    """
    from fund_factor_test import (
        precompute_basis_factors,
        precompute_inventory_factors,
        precompute_profit_factors,
    )

    # 获取板块
    if sector is None:
        from four_dim_strategy import SYMBOLS

        sector = SYMBOLS.get(symbol, {}).get("group", "其他")

    weights = SECTOR_FACTOR_WEIGHTS.get(sector, DEFAULT_FACTOR_WEIGHTS)

    # 确定日期数组
    if date_ints is not None:
        dates_np = np.array(date_ints, dtype=np.int64)
    elif date_strs is not None:
        dates_np = np.array([int(str(d).replace("-", "")[:8]) for d in date_strs], dtype=np.int64)
    else:
        return np.zeros(0)

    n = len(dates_np)
    F_arr = np.zeros(n, dtype=float)
    total_weight = np.zeros(n, dtype=float)

    # 1. 基差因子
    if ff_has_basis_data(symbol):
        b_dates, b_rate, b_trend = precompute_basis_factors(symbol)
        if b_dates is not None:
            idxs = np.searchsorted(b_dates, dates_np, side="right") - 1
            mask = idxs >= 0

            if "basis_rate" in weights:
                w = weights["basis_rate"]
                vals = np.zeros(n)
                vals[mask] = np.nan_to_num(b_rate[idxs[mask]], nan=0.0)
                F_arr += w * vals
                total_weight += np.where(vals != 0, abs(w), 0.0)

            if "basis_trend" in weights:
                w = weights["basis_trend"]
                vals = np.zeros(n)
                vals[mask] = np.nan_to_num(b_trend[idxs[mask]], nan=0.0)
                F_arr += w * vals
                total_weight += np.where(vals != 0, abs(w), 0.0)

    # 2. 库存因子
    if ff_has_inventory_data(symbol):
        i_dates, i_level, i_mom, i_speed = precompute_inventory_factors(symbol)
        if i_dates is not None:
            idxs = np.searchsorted(i_dates, dates_np, side="right") - 1
            mask = idxs >= 0

            for fname, src_arr in [
                ("inv_level", i_level),
                ("inv_mom", i_mom),
                ("inv_speed", i_speed),
            ]:
                if fname in weights:
                    w = weights[fname]
                    vals = np.zeros(n)
                    vals[mask] = np.nan_to_num(src_arr[idxs[mask]], nan=0.0)
                    F_arr += w * vals
                    total_weight += np.where(vals != 0, abs(w), 0.0)

    # 3. 利润因子
    profit_key = get_profit_key_for_symbol(symbol)
    if profit_key and ("profit_z" in weights or "profit_trend" in weights):
        p_dates, p_z, p_trend = precompute_profit_factors(symbol, profit_key)
        if p_dates is not None:
            idxs = np.searchsorted(p_dates, dates_np, side="right") - 1
            mask = idxs >= 0

            if "profit_z" in weights:
                w = weights["profit_z"]
                vals = np.zeros(n)
                vals[mask] = np.nan_to_num(p_z[idxs[mask]], nan=0.0)
                F_arr += w * vals
                total_weight += np.where(vals != 0, abs(w), 0.0)

            if "profit_trend" in weights:
                w = weights["profit_trend"]
                vals = np.zeros(n)
                vals[mask] = np.nan_to_num(p_trend[idxs[mask]], nan=0.0)
                F_arr += w * vals
                total_weight += np.where(vals != 0, abs(w), 0.0)

    # 归一化并裁剪
    with np.errstate(divide="ignore", invalid="ignore"):
        F_arr = np.where(total_weight > 0, F_arr / total_weight * 2.0, 0.0)
    F_arr = np.clip(F_arr, -100.0, 100.0)

    return F_arr


# 别名：保持与 fundamental_feed 一致的命名
def ff_has_basis_data(symbol):
    """判断是否有基差数据。"""
    return has_basis_data(symbol)


def ff_has_inventory_data(symbol):
    """判断是否有库存数据。"""
    return has_inventory_data(symbol)


if __name__ == "__main__":
    # 快速测试
    import time

    t0 = time.time()

    # 测试基差因子
    print("=== 期限结构因子测试（rb, 2025-06-30）===")
    res = basis_rate_factor("rb", 20250630)
    print(f"  basis_rate: {res['basis_rate']:+.2f}")
    print(f"  basis_trend: {res['basis_trend']:+.2f}")

    # 测试库存因子
    print("\n=== 库存因子测试（rb, 2026-07-01）===")
    res = inventory_factors("rb", "2026-07-01")
    print(f"  inv_level: {res['inv_level']:+.2f}")
    print(f"  inv_mom: {res['inv_mom']:+.2f}")
    print(f"  inv_speed: {res['inv_speed']:+.2f}")

    # 测试产业利润因子
    print("\n=== 产业利润因子测试（rb: steel_iron, 2025-06-30）===")
    pf = profit_factor("rb", 20250630, "steel_iron")
    print(f"  profit_z: {pf['profit_z']:+.2f}")
    print(f"  profit_trend: {pf['profit_trend']:+.2f}")

    print(f"\n耗时: {time.time() - t0:.2f}s")
