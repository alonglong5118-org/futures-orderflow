#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minishare_feed · da龘 全品种实时报价接入
=========================================
用 minishare SDK 的 rt_fut_k(ts_code='*') 拉全市场期货合约实时报价快照，
按「中文名」精确匹配 Ken 关注的 6 个品种（焦煤/焦炭/玻璃/纯碱/鸡蛋/生猪）主连，
后台轮询缓存，供 da龘 前端「全品种实时报价」面板 + JM 实时价补大商所缺口。

关键坑（实测）：
- minishare 的 ts_code 命名不规范：焦炭主连的 code 竟是 "JM"（与焦煤前缀撞），
  故**绝不能用代码前缀匹配**，一律按 name(中文名) 匹配，优先取「主连」。
- rt_fut_k 是快照级（最新价/开高低/量/涨跌幅/时间），非 tick、非历史分钟。
- 试用码限额：实测「30次/日」，超限报 429。轮询已做限流自感知（429 自动退避5分钟并保留上次报价）+ 磁盘缓存（重启不丢）。长期付费可放宽。

安装：pip install minishare --extra-index-url https://minidoc.pages.dev/simple/ -U
配置：minishare.json {"enabled": true, "token": "..."}
"""
import datetime
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "minishare.json")
CACHE_FILE = os.path.join(HERE, "minishare_cache.json")  # 持久化上次成功报价，重启不丢

# (sym_key, 显示名, name匹配关键词) —— 全部用中文名匹配，避开代码前缀冲突
WATCH = [
    ("JM", "焦煤", "焦煤"),
    ("J",  "焦炭", "焦炭"),
    ("FG", "玻璃", "玻璃"),
    ("SA", "纯碱", "纯碱"),
    ("jd", "鸡蛋", "鸡蛋"),
    ("lh", "生猪", "生猪"),
    # 稳健池 DCE/SHFE 品种(免费行情无法订阅 tick, 由 minishare 全市场快照兜底实时价)
    ("V",  "PVC", "PVC"),
    ("RB", "螺纹", "螺纹"),
    ("P",  "棕榈", "棕榈"),
    ("HC", "热卷", "热卷"),
]

# 全市场交易所分类（minishare 的 ts_code 无交易所后缀，需按品种代码映射）
EXCHANGE_NAMES = {"CZCE": "郑商所", "DCE": "大商所", "SHFE": "上期所", "INE": "上期能源",
                  "CFFEX": "中金所", "GFEX": "广期所", "其他": "其他"}
EXCHANGE = {}
for _k in ["CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "RB", "HC", "FU", "RU", "BU", "SP", "SS", "WR"]:
    EXCHANGE[_k] = "SHFE"
for _k in ["SC", "LU", "NR", "BC"]:
    EXCHANGE[_k] = "INE"
for _k in ["A", "B", "C", "CS", "EB", "EG", "FB", "I", "J", "JD", "JM", "L", "LH", "M", "P", "PP", "PG", "RR", "V", "Y"]:
    EXCHANGE[_k] = "DCE"
for _k in ["AP", "CF", "CJ", "CY", "FG", "JR", "LR", "MA", "OI", "PF", "PM", "RI", "RM", "RS", "SA", "SF", "SM", "SR", "TA", "UR", "WH", "ZC"]:
    EXCHANGE[_k] = "CZCE"
for _k in ["IC", "IF", "IH", "IM", "TF", "T", "TS"]:
    EXCHANGE[_k] = "CFFEX"
for _k in ["SI", "LC", "PS"]:
    EXCHANGE[_k] = "GFEX"


def _classify_exchange(ts_code):
    s = str(ts_code).upper()
    s = re.sub(r"\d+$", "", s)                    # 去掉合约月份数字
    if s in EXCHANGE:
        return EXCHANGE[s]                        # 先直接命中（含 IM/IF 等股指，勿误剥 M）
    if s.endswith("M") and s[:-1] in EXCHANGE:    # 主连标记 M（如 JMM->JM, FGM->FG）
        return EXCHANGE[s[:-1]]
    return "其他"

_lock = threading.RLock()  # 可重入: _poll_loop 持锁时调用 _save_persist/_load_persist(内部也加锁)不会自死锁
_cache = {"ok": False, "enabled": False, "error": "", "updated": 0, "quotes": {}, "scan": None}

_ms = None          # minishare 模块（懒加载）
_ms_err = ""


def _lazy_import():
    global _ms, _ms_err
    if _ms is not None or _ms_err:
        return _ms
    try:
        import minishare as ms
        _ms = ms
    except Exception as e:
        _ms_err = repr(e)[:200]
        _ms = None
    return _ms


def _load_cfg():
    try:
        c = json.load(open(CONFIG_PATH, encoding="utf-8"))
        return c if c.get("enabled") else None
    except Exception:
        return None


def poll_interval():
    """轮询间隔（秒）。minishare.json 可配 interval（下限60、上限3600），默认900(15分钟)。
    试用码「30次/日」限额严苛，故默认大幅拉长 + 仅交易时段轮询（见 _poll_loop），
    避免午休/夜休/周末空耗额度；付费额度高可调小做高频扫描。"""
    try:
        c = json.load(open(CONFIG_PATH, encoding="utf-8"))
        iv = int(c.get("interval", 900))
        return max(60, min(3600, iv))
    except Exception:
        return 900


def fetch_once():
    """返回 (quotes_dict, scan_dict, error_str)。quotes/scan 为空表示无数据。"""
    ms = _lazy_import()
    if ms is None:
        msg = ("minishare 未安装(请 pip install minishare --extra-index-url https://minidoc.pages.dev/simple/ -U)"
               if not _ms_err else _ms_err)
        return {}, {}, msg
    cfg = _load_cfg()
    if not cfg:
        return {}, {}, "minishare 未启用(在 minishare.json 设 enabled:true)"
    try:
        df = ms.pro_api(cfg["token"]).rt_fut_k(ts_code="*")
    except Exception as e:
        return {}, {}, ("rt_fut_k 调用失败: " + repr(e)[:160])
    if df is None or getattr(df, "empty", True):
        return {}, {}, "rt_fut_k 返回空"

    quotes = {}
    for sym, label, kw in WATCH:
        try:
            sub = df[df["name"].str.contains(kw)]
            if sub.empty:
                continue
            mc = sub[sub["name"].str.contains("主连")]
            r = (mc.iloc[0] if not mc.empty else sub.sort_values("vol", ascending=False).iloc[0])
            try:
                last = float(r["close"])
            except Exception:
                last = None
            try:
                pct = float(r["pct_chg"]) * 100
            except Exception:
                pct = 0.0
            quotes[sym] = {
                "label": label,
                "code": str(r["ts_code"]),
                "name": str(r["name"]),
                "last": last,
                "open": float(r["open"]) if "open" in r else None,
                "high": float(r["high"]) if "high" in r else None,
                "low": float(r["low"]) if "low" in r else None,
                "pre_close": float(r["pre_close"]) if "pre_close" in r else None,
                "pct": pct,
                "vol": int(r["vol"]) if "vol" in r else 0,
                "time": str(r["date"]),
            }
        except Exception:
            continue
    scan = build_scan(df)
    return quotes, scan, ""


# 全市场扫描时排除的交易所（用户要求：中金所以15分钟延迟且非商品期货，不纳入扫描）
EXCLUDE_EXCHANGES = {"CFFEX"}


def build_scan(df):
    """从全市场快照构造异动扫描：按交易所分组 + 按涨跌幅排序（降序；每所取|涨跌幅|前30异动）。
    已排除 EXCLUDE_EXCHANGES（中金所），只扫商品期货（郑商所/大商所/上期所/上期能源/广期所）。"""
    all_rows = []
    for _, r in df.iterrows():
        try:
            code = str(r["ts_code"]); name = str(r["name"])
            last = float(r["close"]); pct = float(r["pct_chg"]) * 100
            vol = int(r["vol"]) if "vol" in r else 0
        except Exception:
            continue
        exch = _classify_exchange(code)
        if exch in EXCLUDE_EXCHANGES:   # 中金所(股指/国债)延迟15分且非商品，直接剔除
            continue
        all_rows.append({"code": code, "name": name, "last": last, "pct": pct,
                         "vol": vol, "exch": exch})
    total = len(all_rows)
    ex_map = {}
    for r in all_rows:
        ex_map.setdefault(r["exch"], []).append(r)
    order = ["CZCE", "DCE", "SHFE", "INE", "GFEX", "其他"]
    exchanges = []
    for key in order:
        rows = ex_map.get(key, [])
        if not rows:
            continue
        top = sorted(rows, key=lambda x: -abs(x["pct"]))[:30]   # 每所按|涨跌幅|取异动前30
        top.sort(key=lambda x: -x["pct"])                        # 展示按涨跌幅降序（涨在上、跌在下）
        exchanges.append({"key": key, "name": EXCHANGE_NAMES[key],
                          "count": len(rows), "rows": top})
    top_up = sorted(all_rows, key=lambda x: -x["pct"])[:12]
    top_down = sorted(all_rows, key=lambda x: x["pct"])[:12]
    return {"ok": True, "updated": time.time(), "total": total,
            "excluded": ["CFFEX"],
            "exchanges": exchanges, "top_up": top_up, "top_down": top_down}


def _load_persist():
    """启动时从磁盘恢复上次成功报价与扫描，避免重启后面板空白。"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            with _lock:
                _cache["quotes"] = data.get("quotes", {})
                _cache["scan"] = data.get("scan")
                if _cache["quotes"] or _cache.get("scan"):
                    _cache["updated"] = data.get("updated", 0)
                    _cache["ok"] = True
    except Exception:
        pass


def _save_persist():
    try:
        with _lock:
            data = {"updated": _cache["updated"], "quotes": dict(_cache["quotes"]),
                    "scan": _cache.get("scan")}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _is_rate_limit(err):
    """试用码有「30次/日」限额，超了报 429。识别后进入冷却而非暴力重试。"""
    return bool(err) and ("429" in err or "调用次数超过限制" in err or "超过限制" in err)


# ---- 交易时段(含夜盘)判断：避免在午休/夜休/周末空耗每日30次额度 ----
_TRADING_STARTS = [(9, 0), (13, 30), (21, 0)]  # 日盘09:00 / 午后13:30 / 夜盘21:00

def _in_trading_session(dt):
    """周一~周五且在 09:00-11:30 / 13:30-15:00 / 21:00-23:00 内。"""
    if dt.weekday() >= 5:  # 周六、周日无交易
        return False
    t = dt.hour * 60 + dt.minute
    if (9 * 60) <= t <= (11 * 60 + 30):
        return True
    if (13 * 60 + 30) <= t <= (15 * 60):
        return True
    if (21 * 60) <= t <= (23 * 60):
        return True
    return False

def _seconds_to_next_session(dt):
    """距离下一个交易时段开始的秒数（单次最多睡12小时）。"""
    if dt.weekday() >= 5:  # 周末：直接睡到下一个周一 09:00
        d = dt.date()
        while True:
            d += datetime.timedelta(days=1)
            if d.weekday() == 0:
                break
        tgt = datetime.datetime(d.year, d.month, d.day, 9, 0, 0)
        return min(43200.0, (tgt - dt).total_seconds())
    cur = dt.hour * 60 + dt.minute + dt.second / 60.0
    for sh, sm in _TRADING_STARTS:
        start = sh * 60 + sm
        if start > cur:
            return min(43200.0, (start - cur) * 60)
    # 今天剩余时段已用完 -> 下一个工作日 09:00
    d = dt.date()
    while True:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            break
    tgt = datetime.datetime(d.year, d.month, d.day, 9, 0, 0)
    return min(43200.0, (tgt - dt).total_seconds())


def _poll_loop(interval):
    """后台轮询。仅在交易时段轮询以省「30次/日」额度；遇限流(429)自动退避5分钟、保留上次报价。"""
    backoff_until = 0.0
    while True:
        now = time.time()
        now_dt = datetime.datetime.fromtimestamp(now)
        # 非交易时段(午休/夜休/周末)：不发包，sleep 到下一开盘，避免空耗每日额度
        if not _in_trading_session(now_dt):
            time.sleep(_seconds_to_next_session(now_dt))
            continue
        # 限流冷却中：不发包，保留上次数据，短暂等待后复查
        if now < backoff_until:
            time.sleep(min(30.0, backoff_until - now))
            continue
        try:
            if _load_cfg() is None:
                with _lock:
                    _cache["enabled"] = False
                    _cache["ok"] = False
                    _cache["error"] = "minishare 未启用"
                    _cache["updated"] = time.time()
                time.sleep(interval)
                continue
            q, scan, err = fetch_once()
            if err and _is_rate_limit(err):
                backoff_until = time.time() + 300  # 限流冷却 5 分钟
                with _lock:
                    _cache["enabled"] = True
                    _cache["updated"] = time.time()
                    _cache["ok"] = False
                    _cache["error"] = err + "（已自动退避5分钟，保留上次报价）"
                time.sleep(interval)
                continue
            with _lock:
                _cache["enabled"] = True
                _cache["updated"] = time.time()
                if q:
                    _cache["quotes"] = q
                if scan:
                    _cache["scan"] = scan
                if q or scan:
                    _cache["ok"] = True
                    _cache["error"] = ""
                    _save_persist()
                else:
                    _cache["ok"] = False
                    _cache["error"] = err or "未取到任何品种报价"
        except Exception as e:
            with _lock:
                _cache["ok"] = False
                _cache["error"] = repr(e)[:200]
                _cache["updated"] = time.time()
        time.sleep(interval)


def start_poll(interval=30):
    """启动后台轮询线程（幂等：若已在跑则不再起）。起步先恢复磁盘缓存。"""
    _load_persist()
    t = threading.Thread(target=_poll_loop, args=(interval,), daemon=True)
    t.start()
    return t


def get_quotes():
    with _lock:
        return {
            "ok": _cache["ok"],
            "enabled": _cache["enabled"],
            "error": _cache["error"],
            "updated": _cache["updated"],
            "quotes": dict(_cache["quotes"]),
        }


def get_scan():
    with _lock:
        return _cache.get("scan")


if __name__ == "__main__":
    import pprint
    start_poll(5)
    time.sleep(8)
    pprint.pprint(get_quotes())
