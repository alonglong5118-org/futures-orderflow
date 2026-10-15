#!/usr/bin/env python3
"""
akshare_live.py — 分钟级实时行情模块

数据源：新浪财经（通过 akshare futures_zh_minute_sina 接口）
工作时段：夜盘 21:00-23:00/02:30，日盘 9:00-15:00
非工作时段：自动返回空数据，由上层回退到 minishare 快照价

合约映射表（主力合约）：
  品种  合约代码    交易所
  zn   ZN2610   上期所    ss   SS2610   上期所
  fu   FU2611   上期所    sp   SP2611   上期所
  J    J2701    大商所    eb   EB2610   大商所
  pg   PG2610   上期所

使用：
    from akshare_live import feed
    f = feed()
    snap = f.poll()   # 返回 {symbol: {'close': price, 'ts': timestamp, ...}}
"""

import threading
import time

# 主力合约映射（品种 → akshare 合约代码）
ALL_CONTRACTS = {
    # 2026-09-08 全量同步 minishare 权威主力合约
    # SYMBOLS 里有 54 个品种，此处全部覆盖
    "AP": "AP2701",
    "CF": "CF2701",
    "FG": "FG2701",
    "J": "J2701",
    "JM": "JM2701",
    "MA": "MA2610",
    "OI": "OI2701",
    "PF": "PF2611",
    "PK": "PK2611",
    "PR": "PR2611",
    "PX": "PX2611",
    "RM": "RM2611",
    "SA": "SA2701",
    "SA01": "SA2701",
    "SH": "SH2611",
    "SR": "SR2701",
    "TA": "TA2701",
    "UR": "UR2701",
    "a": "A2611",
    "ag": "AG2610",
    "al": "AL2610",
    "ao": "AO2701",
    "au": "AU2610",
    "b": "B2611",
    "bu": "BU2611",
    "c": "C2611",
    "cs": "CS2611",
    "cu": "CU2610",
    "eb": "EB2610",
    "ec": "EC2610",
    "eg": "EG2610",
    "fu": "FU2611",
    "hc": "HC2701",
    "i": "I2701",
    "jd": "JD2611",
    "l": "L2701",
    "lc": "LC2610",
    "lh": "LH2611",
    "m": "M2701",
    "ni": "NI2610",
    "p": "P2701",
    "pg": "PG2610",
    "pp": "PP2701",
    "rb": "RB2701",
    "rr": "RR2610",
    "ru": "RU2701",
    "sc": "SC2610",
    "si": "SI2610",
    "sn": "SN2610",
    "sp": "SP2611",
    "ss": "SS2610",
    "v": "V2701",
    "y": "Y2701",
    "zn": "ZN2610",
}


# ============ 动态主力合约解析（2026-10-15 加固）============
# 背景：本文件原有一张纯静态 ALL_CONTRACTS（2026-09-08 手工同步一次）。
# 实测该表 54 个品种中 28 个已陈旧（如 cu=CU2610 而真主力已是 CU2611、zn=ZN2610→ZN2612），
# 而本表被 account_tracker._get_ak_prices_batch 用于拉「持仓品种实时价」，
# 也被 runner 用于后台轮询 => 陈旧表会让持仓浮盈挂在非主力合约上（流动性差、价差大）。
#
# 权威源优先级（后者仅作兜底，永不反向覆盖）：
#   1) main_overrides.json        —— refresh_main_contracts.py 每 2h 刷新（已装 akshare），
#      且 2026-10-15 起会用 minishare_hot_contracts 对齐
#   2) minishare_hot_contracts.json —— minishare_live 运行时真实取价合约（按成交量选主力）
#   3) ALL_CONTRACTS静态表         —— 上述两者都不可用时的兜底
#
# 关键：contract_of() 是唯一对外解析入口，静态表只做兜底，
# 保证「跟踪/成交/浮盈」永远用最新当前主力，而不是几个月前的手工快照。
import json as _json
import os as _os

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_OVR_FILE = _os.path.join(_HERE, "main_overrides.json")
_HOT_FILE = _os.path.join(_HERE, "minishare_hot_contracts.json")

# 解析结果缓存（避免每轮 poll 都读盘）
_resolve_cache = {}          # sym_lower -> (code, source, resolved_at)
_RESOLVE_TTL = 180.0         # 缓存有效期 3 分钟（refresh 每 2h，此频率足够跟换月）
_resolve_lock = threading.Lock()


def _read_json(path):
    try:
        if not _os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        print(f"[akshare_live] 读取 {path} 失败: {e}")
        return None


def _norm_map(raw):
    """把主力映射统一成 {sym_lower: code}。兼容 value 为 dict（含 main 键）与 str 两种形态。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if isinstance(v, dict):
            code = v.get("main") or v.get("contract") or v.get("code") or ""
        else:
            code = v
        if code and k:
            out[str(k).lower()] = str(code).upper()
    return out


def contract_of(sym, force=False):
    """解析品种的当前主力合约代码。**唯一对外入口**，静态表仅兜底。

    返回 code（字符串，找不到返回 None）。
    缓存 TTL=_RESOLVE_TTL 秒，换月后最多 3 分钟内跟随新主力。
    """
    if not sym:
        return None
    s = str(sym).lower()
    now = time.time()
    with _resolve_lock:
        hit = _resolve_cache.get(s)
        if hit and not force and (now - hit[2]) < _RESOLVE_TTL:
            return hit[0]

    code = None
    src = None
    ov = _norm_map(_read_json(_OVR_FILE))
    if ov.get(s):
        code, src = ov[s], "override"
    if not code:
        hot = _norm_map(_read_json(_HOT_FILE))
        if hot.get(s):
            code, src = hot[s], "hot"

    # 兜底：静态表（大小写两种键都试）
    if not code:
        code = ALL_CONTRACTS.get(s) or ALL_CONTRACTS.get(s.upper())
        src = "static_fallback" if code else None

    with _resolve_lock:
        _resolve_cache[s] = (code, src, now)
    return code


def contract_source(sym, force=False):
    """返回 (code, source)，用于诊断面板显示合约来源。"""
    code = contract_of(sym, force=force)
    with _resolve_lock:
        src = (_resolve_cache.get(str(sym).lower()) or (None, None, 0))[1]
    return code, src


def resolve_all(force=False):
    """批量解析并返回 {sym_lower: code}，同时报告与静态表的差异（换月自检用）。"""
    out = {}
    diffs = []
    for k in set(list(ALL_CONTRACTS.keys()) + ["cu", "zn", "sc", "si", "lc", "ru", "jd"]):
        s = str(k).lower()
        c, src = contract_source(s, force=force)
        if not c:
            continue
        out[s] = c
        static = ALL_CONTRACTS.get(s) or ALL_CONTRACTS.get(s.upper())
        if static and str(static).upper() != c:
            diffs.append((s, static, c, src))
    return out, diffs


# 启动即做一次解析自检，把「静态表 vs 真实主力」的差异直接打进日志（换月可视）
try:
    _map0, _diffs0 = resolve_all(force=True)
    if _diffs0:
        print(f"[akshare_live] 主力合约动态解析启用：{len(_map0)} 品种，"
              f"{len(_diffs0)} 个静态表陈旧已自动跟随真实主力")
        for _s, _old, _new, _src in _diffs0[:8]:
            print(f"[akshare_live]   换月跟随: {_s} {_old} → {_new}（源={_src}）")
        if len(_diffs0) > 8:
            print(f"[akshare_live]   ... 另有 {len(_diffs0) - 8} 个（详见 resolve_all()）")
    else:
        print(f"[akshare_live] 主力合约动态解析启用：{len(_map0)} 品种，静态表无陈旧")
except Exception as e:
    print(f"[akshare_live] 主力动态解析初始化异常（退回纯静态表）: {e}")


class AkshareFeed:
    """akshare 实时行情源（新浪财经分钟级数据）。

    特点：
    - 交易时段（21:00-23:00 / 09:00-15:00）提供分钟级实时数据
    - 非交易时段自动返回空数据，调用方应回退到其他数据源
    - 内置缓存机制，避免过于频繁的 API 调用
    """

    def __init__(self, poll_interval=10):
        """
        Args:
            poll_interval: 内部缓存有效期（秒），默认 10 秒
        """
        self._interval = poll_interval
        self._cache = {}
        self._last_poll = 0
        self._lock = threading.Lock()
        self._available = False
        self._error = None
        self._error_count = 0
        self._max_errors = 5
        self._symbol_list = []
        self._last_error_time = 0
        self._import_akshare()

    def _import_akshare(self):
        """延迟导入 akshare"""
        try:
            import akshare as ak

            self._ak = ak
            self._available = True
            print(f"[akshare_live] akshare v{ak.__version__} 已加载")
        except ImportError as e:
            self._available = False
            self._error = str(e)
            print(f"[akshare_live] ❌ akshare 未安装: {e}")

    def available(self):
        return self._available

    def set_symbols(self, symbols):
        """设置需要监控的品种列表"""
        self._symbol_list = [s.lower() for s in symbols]

    def _fetch_contract(self, code):
        """获取单个合约的最新分钟数据。

        Returns:
            dict or None: 数据字典，或 None（获取失败/休市）
        """
        try:
            df = self._ak.futures_zh_minute_sina(symbol=code)
            if df is not None and len(df) > 0:
                latest = df.iloc[-1]
                close = float(latest["close"])
                # 合理性检查：价格为 0 或负数说明数据异常
                if close <= 0:
                    return None
                return {
                    "close": close,
                    "open": float(latest["open"]),
                    "high": float(latest["high"]),
                    "low": float(latest["low"]),
                    "volume": int(latest["volume"]),
                    "hold": int(latest["hold"]) if "hold" in latest.index else 0,
                    "ts": str(latest["datetime"]),
                }
            return None
        except Exception:
            # 休市时段或 API 限流时会抛异常，静默返回 None
            return None

    def poll(self, symbols=None):
        """获取实时行情快照。

        Args:
            symbols: 品种列表，None 则使用 set_symbols 设置的列表

        Returns:
            dict: {symbol: {'close': price, 'ts': timestamp, ...}, ...}
                  休市时段返回空 dict（不是旧缓存）
        """
        if not self._available:
            return {}

        syms = symbols or self._symbol_list
        if not syms:
            return {}

        now = time.time()

        # 检查缓存是否仍有效
        with self._lock:
            if now - self._last_poll < self._interval:
                # 缓存有效，返回旧缓存
                return self._cache.copy()

        results = {}
        current_errors = 0

        for sym in syms:
            sym_lower = sym.lower()
            # 2026-10-15: 走动态解析（override → hot → 静态兜底），
            # 不再直读静态表 —— 静态表 28/54 陈旧，会拉错合约的价格。
            code = contract_of(sym_lower)
            if not code:
                # 未知品种，尝试使用缓存中的旧值
                if sym_lower in self._cache:
                    results[sym_lower] = self._cache[sym_lower]
                continue

            data = self._fetch_contract(code)
            if data:
                results[sym_lower] = data
                self._error_count = 0
            else:
                # 获取失败，使用缓存中的旧值（如果有）
                current_errors += 1
                if sym_lower in self._cache:
                    results[sym_lower] = self._cache[sym_lower]

            # 礼貌性延迟
            time.sleep(0.35)

        # 更新缓存
        with self._lock:
            self._cache = results
            self._last_poll = now

        # 检查连续错误（可能说明已休市）
        if current_errors >= len(syms) * 0.8:  # 80% 以上品种失败
            self._error_count += 1
            now_str = time.strftime("%H:%M:%S")
            if self._error_count <= self._max_errors:
                print(
                    f"[akshare_live] ℹ️ {now_str} 大部分品种无数据（可能休市），已获取 {len(results)}/{len(syms)} 个品种"
                )
            # 不标记为 unavailable，只是返回空结果
        else:
            self._error_count = 0

        return self._cache.copy()

    def get_price(self, symbol):
        """获取单个品种的最新价格"""
        sym = symbol.lower()
        if sym in self._cache:
            return self._cache[sym].get("close", 0)
        return 0

    def get_status(self):
        """获取数据源状态"""
        now = time.time()
        data_age = now - self._last_poll if self._last_poll > 0 else -1
        return {
            "available": self._available,
            "last_poll": time.strftime("%H:%M:%S", time.localtime(self._last_poll)) if self._last_poll > 0 else "从未",
            "symbols": len(self._cache),
            "fresh": data_age < 30 and len(self._cache) > 0,
            "data_age_sec": data_age,
            "error_count": self._error_count,
        }


# 全局单例
_feed_instance = None
_feed_lock = threading.Lock()


def feed():
    """获取全局 AkshareFeed 实例"""
    global _feed_instance
    with _feed_lock:
        if _feed_instance is None:
            _feed_instance = AkshareFeed(poll_interval=10)
        return _feed_instance


if __name__ == "__main__":
    import sys

    syms = sys.argv[1:] if len(sys.argv) > 1 else ["zn", "ss", "fu", "sp", "J", "eb", "pg"]

    print(f"=== akshare_live 测试 ({time.strftime('%H:%M:%S')}) ===")
    f = feed()
    f.set_symbols(syms)

    # Poll twice to test caching
    for i in range(2):
        snap = f.poll()
        print(f"\n轮询 {i + 1}:")
        for sym in syms:
            data = snap.get(sym.lower(), {})
            if data:
                print(f"  {sym}: {data['close']} @ {data['ts']}")
            else:
                print(f"  {sym}: 无数据（休市或获取失败）")
        time.sleep(2)

    status = f.get_status()
    print(f"\n状态: {status}")
