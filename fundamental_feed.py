#!/usr/bin/env python3
"""
fundamental_feed · 四维策略 基本面 F 数据源
==========================================
盘前（或按需）拉取：
  - 期现基差  `ak.futures_spot_price_daily(vars_list=[...])`  -> dom_basis / dom_basis_rate 日序列
  - 库存      `ak.futures_inventory_em(symbol=中文名)`         -> 日期/库存/增减
写入 `fundamentals.json`（按品种缓存，含历史序列供回测）。
缺失品种 F 降级中性，不阻断。

现货季节性（鸡蛋中秋备货 / 生猪出栏节奏）由月份派生，无需额外接口。

依赖：akshare（default venv 已装 1.18.78）
"""

import datetime
import json
import os
import warnings
from bisect import bisect_right

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "fundamentals.json")

# 全品种（不含中金所）：symbol → akshare 基差 vars_list 代码 / 库存中文名
SYMBOL_MAP = {
    # 上期所 SHFE
    "cu": {"basis_code": "CU", "inv_name": "铜"},
    "al": {"basis_code": "AL", "inv_name": "铝"},
    "zn": {"basis_code": "ZN", "inv_name": "锌"},
    "ni": {"basis_code": "NI", "inv_name": "镍"},
    "sn": {"basis_code": "SN", "inv_name": "锡"},
    "ao": {"basis_code": "AO", "inv_name": "氧化铝"},
    "au": {"basis_code": "AU", "inv_name": "黄金"},
    "ag": {"basis_code": "AG", "inv_name": "白银"},
    "rb": {"basis_code": "RB", "inv_name": "螺纹钢"},
    "hc": {"basis_code": "HC", "inv_name": "热卷"},
    "ss": {"basis_code": "SS", "inv_name": "不锈钢"},
    "bu": {"basis_code": "BU", "inv_name": "沥青"},
    "fu": {"basis_code": "FU", "inv_name": "燃料油"},
    "ru": {"basis_code": "RU", "inv_name": "橡胶"},
    "sp": {"basis_code": "SP", "inv_name": "纸浆"},
    # 上期能源 INE
    "sc": {"basis_code": "SC", "inv_name": "原油"},
    "ec": {"basis_code": "EC", "inv_name": "集运欧线"},
    # 大商所 DCE
    "i": {"basis_code": "I", "inv_name": "铁矿石"},
    "J": {"basis_code": "J", "inv_name": "焦炭"},
    "JM": {"basis_code": "JM", "inv_name": "焦煤"},
    "eb": {"basis_code": "EB", "inv_name": "苯乙烯"},
    "eg": {"basis_code": "EG", "inv_name": "乙二醇"},
    "l": {"basis_code": "L", "inv_name": "LLDPE"},
    "pp": {"basis_code": "PP", "inv_name": "聚丙烯"},
    "v": {"basis_code": "V", "inv_name": "PVC"},
    "pg": {"basis_code": "PG", "inv_name": "液化气"},
    "m": {"basis_code": "M", "inv_name": "豆粕"},
    "y": {"basis_code": "Y", "inv_name": "豆油"},
    "a": {"basis_code": "A", "inv_name": "豆一"},
    "b": {"basis_code": "B", "inv_name": "豆二"},
    "p": {"basis_code": "P", "inv_name": "棕榈油"},
    "c": {"basis_code": "C", "inv_name": "玉米"},
    "cs": {"basis_code": "CS", "inv_name": "玉米淀粉"},
    "jd": {"basis_code": "JD", "inv_name": "鸡蛋"},
    "lh": {"basis_code": "LH", "inv_name": "生猪"},
    "rr": {"basis_code": "RR", "inv_name": "粳米"},
    # 郑商所 CZCE
    "FG": {"basis_code": "FG", "inv_name": "玻璃"},
    "SA": {"basis_code": "SA", "inv_name": "纯碱"},
    "MA": {"basis_code": "MA", "inv_name": "甲醇"},
    "TA": {"basis_code": "TA", "inv_name": "PTA"},
    "PF": {"basis_code": "PF", "inv_name": "短纤"},
    "PX": {"basis_code": "PX", "inv_name": "对二甲苯"},
    "SH": {"basis_code": "SH", "inv_name": "烧碱"},
    "UR": {"basis_code": "UR", "inv_name": "尿素"},
    "PR": {"basis_code": "PR", "inv_name": "瓶片"},
    "SR": {"basis_code": "SR", "inv_name": "白糖"},
    "CF": {"basis_code": "CF", "inv_name": "棉花"},
    "RM": {"basis_code": "RM", "inv_name": "菜粕"},
    "OI": {"basis_code": "OI", "inv_name": "菜油"},
    "PK": {"basis_code": "PK", "inv_name": "花生"},
    "AP": {"basis_code": "AP", "inv_name": "苹果"},
    # 广期所 GFEX
    "si": {"basis_code": "SI", "inv_name": "工业硅"},
    "lc": {"basis_code": "LC", "inv_name": "碳酸锂"},
}

# 库存中文名 -> 主连代码（反向，用于库存落回品种）
INV_TO_SYM = {v["inv_name"]: k for k, v in SYMBOL_MAP.items()}


def _today_str():
    return datetime.date.today().strftime("%Y%m%d")


def fetch_basis_history(start_day="20210101", end_day=None):
    """拉全部 6 品种基差日序列，返回 {symbol: [{date, dom_basis, dom_basis_rate, spot, dom_price}]}。"""
    import akshare as ak

    end_day = end_day or _today_str()
    codes = [v["basis_code"] for v in SYMBOL_MAP.values()]
    try:
        df = ak.futures_spot_price_daily(start_day=start_day, end_day=end_day, vars_list=codes)
    except Exception as e:
        print("基差拉取失败:", repr(e)[:160])
        return {}
    out = {k: [] for k in SYMBOL_MAP}
    if df is None or getattr(df, "empty", True):
        return out
    for _, r in df.iterrows():
        sym = None
        for k, v in SYMBOL_MAP.items():
            if str(r.get("symbol", "")).upper() == v["basis_code"]:
                sym = k
                break
        if sym is None:
            continue
        try:
            out[sym].append(
                {
                    "date": str(r["date"]),
                    "dom_basis": float(r["dom_basis"]) if r.get("dom_basis") not in (None, "") else None,
                    "dom_basis_rate": float(r["dom_basis_rate"]) if r.get("dom_basis_rate") not in (None, "") else None,
                    "spot": float(r["spot_price"]) if r.get("spot_price") not in (None, "") else None,
                    "dom_price": float(r["dominant_contract_price"])
                    if r.get("dominant_contract_price") not in (None, "")
                    else None,
                }
            )
        except Exception:
            continue
    return out


def fetch_inventory():
    """拉 6 品种库存（部分品种可能无数据），返回 {symbol: [{date, stock, chg}]}。"""
    import akshare as ak

    out = {k: [] for k in SYMBOL_MAP}
    for sym, m in SYMBOL_MAP.items():
        try:
            df = ak.futures_inventory_em(symbol=m["inv_name"])
        except Exception as e:
            print(f"库存拉取失败 {sym}:", repr(e)[:120])
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            try:
                out[sym].append(
                    {
                        "date": str(r["日期"]),
                        "stock": float(r["库存"]) if r.get("库存") not in (None, "") else None,
                        "chg": float(r["增减"]) if r.get("增减") not in (None, "") else None,
                    }
                )
            except Exception:
                continue
    return out


def refresh(cache_file=CACHE_FILE, basis_start="20210101"):
    """拉取并写盘；返回写好的 dict。"""
    basis = fetch_basis_history(start_day=basis_start)
    inv = fetch_inventory()
    data = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "symbols": {}}
    for sym in SYMBOL_MAP:
        bser = basis.get(sym, [])
        iser = inv.get(sym, [])
        latest = {}
        if bser:
            lb = bser[-1]
            latest["date"] = lb["date"]
            latest["dom_basis_rate"] = lb["dom_basis_rate"]
            latest["dom_basis"] = lb["dom_basis"]
            latest["spot"] = lb["spot"]
            latest["dom_price"] = lb["dom_price"]
        if iser:
            li = iser[-1]
            latest["inv_date"] = li["date"]
            latest["inv_stock"] = li["stock"]
            latest["inv_chg"] = li["chg"]
        # 库存近期趋势（近 3 期净变化之和的符号）
        if len(iser) >= 2:
            recent = [x["chg"] for x in iser[-3:] if x["chg"] is not None]
            latest["inv_trend"] = sum(recent) if recent else 0.0
        data["symbols"][sym] = {"basis_series": bser, "inventory": iser, "latest": latest}
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"fundamentals.json 已写：{len(data['symbols'])} 品种，更新 {data['updated']}")
    for s in SYMBOL_MAP:
        bs = len(data["symbols"][s]["basis_series"])
        iv = len(data["symbols"][s]["inventory"])
        print(f"  {s}: 基差{bs}期 库存{iv}期")
    return data


# 进程内缓存：按 (路径, mtime) 命中，避免回测中每笔 walk-forward 迭代都重新 json.load 整个 fundamentals.json
_load_cache = {"path": None, "mtime": None, "data": None}

# 二分查找索引缓存：{cache_file: {symbol: {"basis_dates": [...], "inventory_dates": [...]}}}
_bisect_index_cache = {}


def _ensure_bisect_index(data, symbol, cache_file):
    """确保某品种的二分查找索引已构建（懒加载 + 按 cache_file+mtime 失效）。"""
    cache_key = cache_file
    if cache_key not in _bisect_index_cache:
        _bisect_index_cache.clear()
        _bisect_index_cache[cache_key] = {}
    slot = _bisect_index_cache[cache_key]
    if symbol not in slot:
        ser = data.get("symbols", {}).get(symbol, {})
        slot[symbol] = {
            "basis_dates": [r["date"] for r in ser.get("basis_series", [])],
            "inventory_dates": [r["date"] for r in ser.get("inventory", [])],
        }
    return slot[symbol]


def load(cache_file=CACHE_FILE):
    """读缓存；不存在则现场 refresh。带进程内缓存（按文件 mtime 失效），
    避免回测/扫描中每次 compute_F 都重新解析整个基本面 JSON（曾是 5m 回测卡死的根因）。"""
    if not os.path.exists(cache_file):
        return refresh(cache_file)
    try:
        mtime = os.path.getmtime(cache_file)
        if _load_cache["path"] == cache_file and _load_cache["mtime"] == mtime and _load_cache["data"] is not None:
            return _load_cache["data"]
        data = json.load(open(cache_file, encoding="utf-8"))
        _load_cache["path"] = cache_file
        _load_cache["mtime"] = mtime
        _load_cache["data"] = data
        return data
    except Exception:
        return refresh(cache_file)


def basis_rate_on(symbol, date_str, cache_file=CACHE_FILE):
    """回测用：取某日期的 dom_basis_rate；无精确日则取最近 ≤该日 的一条。缺失返回 None。
    性能优化：bisect 二分查找（O(log n)）+ 索引缓存。"""
    data = load(cache_file)
    ser = data.get("symbols", {}).get(symbol, {}).get("basis_series", [])
    if not ser:
        return None
    idx = _ensure_bisect_index(data, symbol, cache_file)
    pos = bisect_right(idx["basis_dates"], date_str) - 1
    if pos < 0:
        # 所有日期都 > date_str，取第一条（保持原逻辑）
        return ser[0].get("dom_basis_rate")
    return ser[pos].get("dom_basis_rate")


def inventory_trend_on(symbol, date_str, cache_file=CACHE_FILE):
    """回测用：取某日期前最近库存趋势（近3期净变化）。缺失返回 0.0。
    性能优化：bisect 二分查找（O(log n)）+ 索引缓存。"""
    data = load(cache_file)
    ser = data.get("symbols", {}).get(symbol, {}).get("inventory", [])
    if not ser:
        return 0.0
    idx = _ensure_bisect_index(data, symbol, cache_file)
    pos = bisect_right(idx["inventory_dates"], date_str)  # 第一个 > date_str 的位置
    # pos-1 是最后一个 <= date_str 的位置，取近 3 期
    end = pos
    start = max(0, end - 3)
    if end < 2:  # 不足 2 条有效数据
        return 0.0
    recent = [ser[i]["chg"] for i in range(start, end) if ser[i].get("chg") is not None]
    return sum(recent) if recent else 0.0


# ---------- F 打分（§1.1 基本面） ----------
def seasonal_f(symbol, date_str):
    """现货季节性（动机视角，非价格回报）：鸡蛋中秋前偏多、生猪节前偏多、其余中性。
    返回 -40~+40。简单月份模型，回测可校准。"""
    try:
        dt = datetime.date.fromisoformat(date_str)
    except Exception:
        return 0.0
    m = dt.month
    if symbol == "jd":  # 鸡蛋：7-9 中秋备货偏多，节后 10-11 偏弱
        if 7 <= m <= 9:
            return 35
        if m in (10, 11):
            return -20
        return 0.0
    if symbol == "lh":  # 生猪：年底腌腊/春节前偏多，节后淡
        if m in (11, 12, 1):
            return 30
        if m in (3, 4):
            return -15
        return 0.0
    if symbol == "FG":  # 玻璃：金九银十地产链偏多
        if m == 9 or m == 10:
            return 20
        return 0.0
    if symbol == "SA":  # 纯碱：下游玻璃旺季带动，与 FG 略同步
        if m in (9, 10):
            return 15
        return 0.0
    return 0.0


def seasonal_f_by_month(symbol, m):
    """seasonal_f 的月份版本（直接传月份 int，省 fromisoformat 解析）。"""
    if symbol == "jd":
        if 7 <= m <= 9:
            return 35
        if m in (10, 11):
            return -20
        return 0.0
    if symbol == "lh":
        if m in (11, 12, 1):
            return 30
        if m in (3, 4):
            return -15
        return 0.0
    if symbol == "FG":
        if m == 9 or m == 10:
            return 20
        return 0.0
    if symbol == "SA":
        if m in (9, 10):
            return 15
        return 0.0
    return 0.0


# F 维度各分量权重（P-D，2026-08-14）：季节性由 0.2 → 0.3，库存同步 0.2 → 0.1（总和仍=1.0）。
#   鸡蛋中秋备货 / 生猪腌腊 / 玻璃金九银十 / 纯碱旺季 是真实 alpha，原 0.2 被基差淹没；
#   提升后 F 对这些强季节性品种的基本面判读更敏感。改此处即改全局 F 季节性强度（无需动调用方）。
SEASONAL_F_WEIGHT = 0.3
INV_F_WEIGHT = 0.1
BASIS_F_WEIGHT = 0.6


def compute_F(symbol, date_str, cache_file=CACHE_FILE):
    """返回 F_score(-100~+100)。基差(主) + 库存趋势(辅) + 现货季节性(辅)。缺失降级中性。"""
    rate = basis_rate_on(symbol, date_str, cache_file)
    if rate is None:
        return 0.0  # 缺失降级中性
    # 基差 score：back(正)看多 / contango(负)看空；|rate|>=0.10 封顶 ±100
    basis_s = max(-100.0, min(100.0, rate / 0.10 * 100))
    inv_t = inventory_trend_on(symbol, date_str, cache_file)
    # 库存趋势 score：用相对量级归一（库存单位差异大，仅取符号+弱强度）
    inv_s = 0.0
    if inv_t != 0.0:
        inv_s = 25.0 if inv_t < 0 else -25.0  # 降库看多 / 累库看空
    seas_s = seasonal_f(symbol, date_str)
    # 加权（P-D）：基差 0.6 / 库存 0.1 / 季节性 0.3（季节性分量提升）
    F = BASIS_F_WEIGHT * basis_s + INV_F_WEIGHT * inv_s + SEASONAL_F_WEIGHT * seas_s
    return max(-100.0, min(100.0, F))


def compute_F_subfactors(symbol, date_str, cache_file=CACHE_FILE):
    """返回 F 维度的子因子分解：(basis_score, seasonal_score)
    每个子因子范围 [-100, 100]，独立计算。缺失则返回 0。"""
    rate = basis_rate_on(symbol, date_str, cache_file)
    basis_s = 0.0
    if rate is not None:
        basis_s = max(-100.0, min(100.0, rate / 0.10 * 100))
    seas_s = seasonal_f(symbol, date_str)
    return round(basis_s, 1), round(seas_s, 1)


def precompute_F_array(symbol, date_strs=None, cache_file=CACHE_FILE, date_ints=None, months=None):
    """批量预计算 F 分数数组（双指针 O(n) 扫描基差/库存，比逐次 bisect+load 快 ~10×）。

    参数:
        symbol: 品种代码
        date_strs: 日期字符串列表（YYYYMMDD 格式），兼容旧调用
        cache_file: 基本面缓存文件路径
        date_ints: 整数日期数组（YYYYMMDD as int），优先使用（比字符串快 ~15×）
        months: 月份数组（1-12），提供则用 seasonal_f_by_month 省 fromisoformat

    返回:
        numpy.ndarray: F 分数数组 (-100~+100)
    """
    import numpy as np

    # 确定长度和日期比较方式
    if date_ints is not None:
        n = len(date_ints)
        use_int = True
    elif date_strs is not None:
        n = len(date_strs)
        use_int = False
    else:
        return np.array([], dtype=np.float64)

    if n == 0:
        return np.array([], dtype=np.float64)

    data = load(cache_file)
    sym_data = data.get("symbols", {}).get(symbol, {})
    basis_series = sym_data.get("basis_series", [])
    inv_series = sym_data.get("inventory", [])

    result = np.zeros(n, dtype=np.float64)

    # ── 基差：双指针 O(n) 扫描 ──
    basis_rates = np.full(n, np.nan)
    if basis_series:
        j = 0
        nb = len(basis_series)
        if use_int:
            # 预转整数日期（支持 YYYYMMDD 和 YYYY-MM-DD 两种格式）
            b_dates = [int(d["date"].replace("-", "")) for d in basis_series if d.get("date")]
            b_rates = [d.get("dom_basis_rate") for d in basis_series if d.get("date")]
            nb = len(b_dates)
            for i in range(n):
                di = date_ints[i]
                while j < nb and b_dates[j] <= di:
                    j += 1
                if j > 0:
                    r = b_rates[j - 1]
                    if r is not None:
                        basis_rates[i] = r
                elif nb > 0:
                    r = b_rates[0]
                    if r is not None:
                        basis_rates[i] = r
        else:
            for i in range(n):
                d = date_strs[i]
                while j < nb and basis_series[j]["date"] <= d:
                    j += 1
                if j > 0:
                    r = basis_series[j - 1].get("dom_basis_rate")
                    if r is not None:
                        basis_rates[i] = r
                elif nb > 0:
                    r = basis_series[0].get("dom_basis_rate")
                    if r is not None:
                        basis_rates[i] = r

    # ── 库存趋势：双指针 O(n) 扫描 ──
    inv_trends = np.zeros(n, dtype=np.float64)
    if inv_series and len(inv_series) >= 2:
        j = 0
        ni = len(inv_series)
        if use_int:
            i_dates = [int(d["date"].replace("-", "")) for d in inv_series if d.get("date")]
            i_chgs = [d.get("chg") for d in inv_series if d.get("date")]
            ni = len(i_dates)
            for i in range(n):
                di = date_ints[i]
                while j < ni and i_dates[j] <= di:
                    j += 1
                end = j
                start = max(0, end - 3)
                if end >= 2:
                    recent = [i_chgs[k] for k in range(start, end) if i_chgs[k] is not None]
                    if recent:
                        inv_trends[i] = sum(recent)
        else:
            for i in range(n):
                d = date_strs[i]
                while j < ni and inv_series[j]["date"] <= d:
                    j += 1
                end = j
                start = max(0, end - 3)
                if end >= 2:
                    recent = [inv_series[k]["chg"] for k in range(start, end) if inv_series[k].get("chg") is not None]
                    if recent:
                        inv_trends[i] = sum(recent)

    # ── 季节性：有 months 用 fast path，否则回退 seasonal_f ──
    if months is not None:
        seasonal_scores = np.array([seasonal_f_by_month(symbol, int(m)) for m in months], dtype=np.float64)
    elif date_strs is not None:
        seasonal_scores = np.array([seasonal_f(symbol, d) for d in date_strs], dtype=np.float64)
    else:
        seasonal_scores = np.zeros(n, dtype=np.float64)

    # ── 合成 F 分数 ──
    valid = ~np.isnan(basis_rates)
    if np.any(valid):
        basis_s = np.clip(basis_rates[valid] / 0.10 * 100, -100.0, 100.0)
        inv_s = np.where(inv_trends[valid] < 0, 25.0, np.where(inv_trends[valid] > 0, -25.0, 0.0))
        seas_s = seasonal_scores[valid]
        F = BASIS_F_WEIGHT * basis_s + INV_F_WEIGHT * inv_s + SEASONAL_F_WEIGHT * seas_s
        result[valid] = np.clip(F, -100.0, 100.0)

    return result


if __name__ == "__main__":
    # 默认刷新
    refresh()
    print("\n=== compute_F 当前值（6 品种）===")
    for s in SYMBOL_MAP:
        try:
            print(f"  {s}: F={compute_F(s, _today_str()):+.1f}")
        except Exception as e:
            print(f"  {s}: 计算失败 {repr(e)[:80]}")


# ===========================================================================
# 增强版 F 因子（v2）：7 因子 + 分板块差异化权重
# 2026-08-29 新增：basis_trend / inv_mom / inv_speed / profit_z 等因子
# 接入方式：分板块配置，默认全部关闭，可按板块/全局启用
# ===========================================================================

# 全局开关（优先于分板块配置，用于快速全局切换）
ENHANCED_F_ENABLED = False  # 默认关闭

# 分板块增强 F 配置（ENHANCED_F_ENABLED=False 时按此配置逐板块启用）
# True = 该板块用增强版 F，False = 用旧版 F
SECTOR_ENHANCED_F = {
    "农产品": True,  # ✅ 提升显著（+0.030）
    "黑系": True,  # ✅ 提升显著（+0.061）
    "有色": True,  # ✅ 提升较好（+0.026）
    "贵金属": True,  # ✅ 小幅提升（+0.009）
    "化工": False,  # ❌ 下降，保持旧版
    "能源": False,  # ❌ 下降，保持旧版
    "航运": False,  # 无数据影响
    "其他": False,
}


def enable_enhanced_F(enable=True):
    """全局启用/禁用增强版 F 因子。"""
    global ENHANCED_F_ENABLED
    ENHANCED_F_ENABLED = enable


def is_enhanced_F_for(symbol):
    """判断指定品种是否使用增强版 F。

    优先级：全局开关 > 分板块配置 > 默认 False
    """
    if ENHANCED_F_ENABLED:
        return True

    # 尝试从 SYMBOLS 获取板块
    try:
        from four_dim_strategy import SYMBOLS

        sector = SYMBOLS.get(symbol, {}).get("group", "其他")
    except Exception:
        sector = "其他"

    return SECTOR_ENHANCED_F.get(sector, False)


def compute_F_v2(symbol, date_str):
    """增强版 F 计算（单点）。

    调用 fundamental_factors.compute_enhanced_F，
    支持分板块差异化权重和 7 个基本面子因子。
    """
    try:
        import fundamental_factors as nff

        date_int = int(date_str.replace("-", "")) if isinstance(date_str, str) else int(date_str)
        return nff.compute_enhanced_F(symbol, date_int, date_str=str(date_str))
    except Exception:
        return 0.0


def precompute_F_array_v2(symbol, date_strs=None, date_ints=None, months=None):
    """增强版 F 批量预计算。

    调用 fundamental_factors.precompute_enhanced_F_array。
    """
    try:
        import fundamental_factors as nff

        return nff.precompute_enhanced_F_array(symbol, date_ints=date_ints, date_strs=date_strs)
    except Exception:
        import numpy as np

        if date_ints is not None:
            return np.zeros(len(date_ints))
        elif date_strs is not None:
            return np.zeros(len(date_strs))
        return np.array([])


# 包装原函数：根据开关决定用哪个版本
_original_compute_F = compute_F
_original_precompute_F_array = precompute_F_array


def compute_F_dispatcher(symbol, date_str, cache_file=CACHE_FILE):
    """F 计算分发器：根据品种配置返回 v1 或 v2。"""
    if is_enhanced_F_for(symbol):
        return compute_F_v2(symbol, date_str)
    else:
        return _original_compute_F(symbol, date_str, cache_file)


def precompute_F_array_dispatcher(symbol, date_strs=None, cache_file=CACHE_FILE, date_ints=None, months=None):
    """F 批量计算分发器：根据品种配置返回 v1 或 v2。"""
    if is_enhanced_F_for(symbol):
        return precompute_F_array_v2(symbol, date_strs=date_strs, date_ints=date_ints)
    else:
        return _original_precompute_F_array(
            symbol,
            date_strs=date_strs,
            cache_file=cache_file,
            date_ints=date_ints,
            months=months,
        )


# 替换全局函数（保留原函数作为 fallback）
compute_F = compute_F_dispatcher
precompute_F_array = precompute_F_array_dispatcher
