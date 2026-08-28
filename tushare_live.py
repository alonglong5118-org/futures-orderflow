#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tushare_live.py — Tushare Pro 实时行情模块

数据源优先级：
  1. rt_fut_min  (Tushare 期货实时分钟数据)  ← 主数据源（需单独权限 1000元/月）
  2. fut_daily   (Tushare 期货日线数据)    ← 参考/结算价（需 2000+ 积分）
  3. akshare     (新浪财经分钟级数据)       ← 免费实时回退
  4. account_state 存储价                   ← 最终回退

当 Tushare 权限开通后，系统自动切换到 Tushare 实时数据，无需改代码。
"""

import json
import os
import threading
import time
import urllib.request

# —— 主力合约映射（品种 → Tushare ts_code）——
_TUSHARE_CONTRACTS = {
    'zn': 'ZN2610.SHF',   'ss': 'SS2610.SHF',   'fu': 'FU2611.SHF',
    'sp': 'SP2611.SHF',   'J':  'J2701.DCE',    'eb': 'EB2610.DCE',
    'pg': 'PG2610.SHF',
    'cu': 'CU2610.SHF',   'al': 'AL2610.SHF',   'ni': 'NI2610.SHF',
    'sn': 'SN2610.SHF',   'pb': 'PB2610.SHF',   'au': 'AU2610.SHF',
    'ag': 'AG2610.SHF',
    'rb': 'RB2610.SHF',   'hc': 'HC2610.SHF',   'bu': 'BU2610.SHF',
    'ru': 'RU2701.SHF',   'sc': 'SC2610.INE',
    'eg': 'EG2610.DCE',   'l':  'L2701.DCE',    'pp': 'PP2701.DCE',
    'v':  'V2701.DCE',    'm':  'M2701.DCE',    'y':  'Y2701.DCE',
    'a':  'A2611.DCE',   'b':  'B2611.DCE',    'p':  'P2701.DCE',
    'c':  'C2611.DCE',   'cs': 'CS2611.DCE',   'jd': 'JD2610.DCE',
    'lh': 'LH2611.DCE',
    'fg': 'FG2701.CZC',  'sa': 'SA2701.CZC',   'ma': 'MA2610.CZC',
    'ta': 'TA2701.CZC',  'cf': 'CF2701.CZC',   'rm': 'RM2609.CZC',
    'oi': 'OI2611.CZC',  'sr': 'SR2701.CZC',   'ur': 'UR2701.CZC',
    'ap': 'AP2610.CZC',  'pk': 'PK2610.CZC',
}


class TushareFeed:
    """Tushare Pro 实时行情源。"""

    TUSHARE_URL = "http://api.tushare.pro"
    TOKEN_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tushare_token.txt"
    )

    def __init__(self, poll_interval=10):
        self._interval = poll_interval
        self._cache = {}
        self._last_poll = 0
        self._lock = threading.Lock()
        self._available = False
        self._error_count = 0
        self._max_errors = 10
        self._symbol_list = []

        # Tushare HTTP 客户端
        self._token = self._load_token()
        self._init_time = time.time()

        # 权限检测结果
        self._has_rt_fut_min = False
        self._has_fut_daily = False
        self._has_fut_basic = False

        # Tushare 连接状态
        self._tushare_available = False
        self._tushare_error = None

        # 初始化
        self._init_tushare()
        self._check_permissions()

        # akshare 回退
        self._ak_feed = None
        self._ak_available = False
        self._init_akshare()

        # 最终标记
        self._available = self._tushare_available or self._ak_available
        print(f"[tushare_live] ✅ 初始化完成: Tushare={self._tushare_available} akshare={self._ak_available}")

    def _load_token(self):
        """读取 Tushare Token。"""
        token = os.environ.get("TUSHARE_TOKEN", "").strip()
        if token:
            return token
        if os.path.exists(self.TOKEN_FILE):
            try:
                with open(self.TOKEN_FILE, encoding="utf-8") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
                    if lines:
                        return lines[0]
            except Exception:
                pass
        return ""

    def _init_tushare(self):
        """测试 Tushare HTTP API 连通性。"""
        if not self._token:
            print("[tushare_live] ⚠️ 未找到 TUSHARE_TOKEN，Tushare 不可用")
            self._tushare_available = False
            return
        try:
            resp = self._call_api("fut_basic", {"exchange": "SHFE", "fut_type": "1"})
            if resp.get("code") == 0:
                self._tushare_available = True
                print("[tushare_live] ✅ Tushare HTTP API 连通")
            else:
                self._tushare_available = False
                msg = resp.get("msg", "")[:100]
                self._tushare_error = msg
                print(f"[tushare_live] ⚠️ Tushare API: code={resp.get('code')} msg={msg}")
        except Exception as e:
            self._tushare_available = False
            self._tushare_error = str(e)
            print(f"[tushare_live] ⚠️ Tushare API 连接失败: {e}")

    def _check_permissions(self):
        """检测账户权限级别。"""
        if not self._tushare_available:
            return

        # rt_fut_min
        try:
            resp = self._call_api("rt_fut_min", {"ts_code": "ZN2610.SHF", "freq": "1MIN"})
            if resp.get("code") == 0:
                self._has_rt_fut_min = True
                print("[tushare_live]   🟢 rt_fut_min 实时分钟权限 ✅")
            else:
                print(f"[tushare_live]   🟡 rt_fut_min 无权限: {resp.get('msg', '')[:80]}")
        except Exception:
            pass

        # fut_daily
        try:
            resp = self._call_api("fut_daily", {"ts_code": "ZN2610.SHF"})
            if resp.get("code") == 0:
                self._has_fut_daily = True
                print("[tushare_live]   🟢 fut_daily 日线权限 ✅")
            else:
                print(f"[tushare_live]   🟡 fut_daily 无权限: {resp.get('msg', '')[:80]}")
        except Exception:
            pass

        self._has_fut_basic = self._tushare_available

    def _init_akshare(self):
        """初始化 akshare 回退源。"""
        try:
            import akshare_live as _ak
            self._ak_feed = _ak.feed()
            self._ak_available = True
            print("[tushare_live]   🟢 akshare 回退源可用")
        except ImportError:
            self._ak_available = False

    def _call_api(self, api_name, params=None, fields=""):
        """POST 调用 Tushare HTTP API。"""
        if not self._token:
            return {"code": -1, "msg": "无 TUSHARE_TOKEN"}
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params or {},
            "fields": fields or "",
        }
        req = urllib.request.Request(
            self.TUSHARE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp
        except Exception as e:
            return {"code": -1, "msg": str(e)}

    def set_symbols(self, symbols):
        """设置需要监控的品种列表。"""
        self._symbol_list = [s.lower() for s in symbols]
        if self._ak_feed:
            self._ak_feed.set_symbols(self._symbol_list)

    def poll(self, symbols=None):
        """获取实时行情快照（Tushare 优先 → akshare 回退）。"""
        syms = symbols or self._symbol_list
        if not syms:
            return {}

        now = time.time()
        with self._lock:
            if now - self._last_poll < self._interval:
                return self._cache.copy()

        results = {}

        # 策略 1：Tushare rt_fut_min（实时分钟）
        if self._has_rt_fut_min and self._tushare_available:
            results = self._poll_tushare_realtime(syms)
            if results:
                self._update_cache(results)
                return self._cache.copy()

        # 策略 2：Tushare fut_daily（日线结算价）
        if self._has_fut_daily and self._tushare_available:
            daily_results = self._poll_tushare_daily(syms)
            if daily_results:
                for sym, data in daily_results.items():
                    data["source"] = "tushare_daily"
                results.update(daily_results)

        # 策略 3：akshare 实时回退
        if self._ak_available and self._ak_feed:
            ak_results = self._poll_akshare(syms)
            if ak_results:
                for sym, data in ak_results.items():
                    if sym not in results:
                        data["source"] = "akshare"
                        results[sym] = data

        # 策略 4：缓存旧值
        for sym in syms:
            sym_lower = sym.lower()
            if sym_lower not in results and sym_lower in self._cache:
                results[sym_lower] = self._cache[sym_lower]

        self._update_cache(results)
        return self._cache.copy()

    def _poll_tushare_realtime(self, symbols):
        """通过 rt_fut_min 获取实时分钟数据。"""
        results = {}
        codes = []
        code_to_sym = {}

        for sym in symbols:
            sym_lower = sym.lower()
            ts_code = _TUSHARE_CONTRACTS.get(sym_lower)
            if not ts_code:
                ts_code = _TUSHARE_CONTRACTS.get(sym.upper())
            if ts_code:
                codes.append(ts_code)
                code_to_sym[ts_code] = sym_lower

        if not codes:
            return results

        batch_codes = ",".join(codes)
        try:
            resp = self._call_api(
                "rt_fut_min",
                {"ts_code": batch_codes, "freq": "1MIN"},
                "ts_code,freq,time,open,close,high,low,vol,oi",
            )
            if resp.get("code") != 0:
                self._error_count += 1
                if self._error_count <= self._max_errors:
                    print(f"[tushare_live] rt_fut_min 错误: {resp.get('msg', '')[:80]}")
                return results

            data = resp.get("data", {})
            items = data.get("items", [])
            fields = data.get("fields", [])

            for item in items:
                row = dict(zip(fields, item))
                ts_code = row.get("ts_code", "")
                sym_lower = code_to_sym.get(ts_code)
                if sym_lower:
                    close = float(row.get("close", 0))
                    if close > 0:
                        results[sym_lower] = {
                            "close": close,
                            "open": float(row.get("open", 0)),
                            "high": float(row.get("high", 0)),
                            "low": float(row.get("low", 0)),
                            "volume": int(row.get("vol", 0)),
                            "hold": float(row.get("oi", 0)),
                            "ts": str(row.get("time", "")),
                            "source": "tushare_rt",
                        }

            self._error_count = 0
            return results

        except Exception as e:
            self._error_count += 1
            if self._error_count <= self._max_errors:
                print(f"[tushare_live] rt_fut_min 异常: {e}")
            return results

    def _poll_tushare_daily(self, symbols):
        """通过 fut_daily 获取日线数据。"""
        results = {}
        today = time.strftime("%Y%m%d")

        for sym in symbols:
            sym_lower = sym.lower()
            ts_code = _TUSHARE_CONTRACTS.get(sym_lower)
            if not ts_code:
                ts_code = _TUSHARE_CONTRACTS.get(sym.upper())
            if not ts_code:
                continue

            try:
                resp = self._call_api(
                    "fut_daily",
                    {"ts_code": ts_code, "trade_date": today},
                    "ts_code,trade_date,open,high,low,close,settle,vol,oi",
                )
                if resp.get("code") == 0 and resp.get("data"):
                    data = resp["data"]
                    items = data.get("items", [])
                    if items:
                        row = dict(zip(data.get("fields", []), items[-1]))
                        close = float(row.get("close", 0))
                        settle = float(row.get("settle", close))
                        if close > 0:
                            results[sym_lower] = {
                                "close": close,
                                "settle": settle,
                                "open": float(row.get("open", 0)),
                                "high": float(row.get("high", 0)),
                                "low": float(row.get("low", 0)),
                                "volume": int(row.get("vol", 0)),
                                "hold": float(row.get("oi", 0)),
                                "ts": str(row.get("trade_date", "")),
                                "source": "tushare_daily",
                            }
                time.sleep(0.3)
            except Exception:
                pass

        return results

    def _poll_akshare(self, symbols):
        """通过 akshare 获取实时数据（回退源）。"""
        if not self._ak_feed:
            return {}
        try:
            return self._ak_feed.poll(symbols)
        except Exception:
            return {}

    def _update_cache(self, results):
        """更新缓存。"""
        with self._lock:
            self._cache = results
            self._last_poll = time.time()

    def get_price(self, symbol):
        """获取单个品种的最新价格。"""
        sym = symbol.lower()
        if sym in self._cache:
            return self._cache[sym].get("close", 0)
        return 0

    def get_status(self):
        """获取数据源状态。"""
        now = time.time()
        data_age = now - self._last_poll if self._last_poll > 0 else -1
        return {
            "tushare_available": self._tushare_available,
            "has_rt_fut_min": self._has_rt_fut_min,
            "has_fut_daily": self._has_fut_daily,
            "akshare_available": self._ak_available,
            "last_poll": time.strftime("%H:%M:%S", time.localtime(self._last_poll)) if self._last_poll > 0 else "从未",
            "symbols": len(self._cache),
            "fresh": data_age < 30 and len(self._cache) > 0,
            "data_age_sec": data_age,
            "error_count": self._error_count,
        }


# ── 全局单例 ──────────────────────────────────────────────────────────
_feed_instance = None
_feed_lock = threading.Lock()


def feed():
    """获取全局 TushareFeed 实例。"""
    global _feed_instance
    with _feed_lock:
        if _feed_instance is None:
            _feed_instance = TushareFeed(poll_interval=10)
        return _feed_instance


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["zn", "ss", "fu", "sp", "J", "eb", "pg"]
    print(f"=== tushare_live 测试 ({time.strftime('%H:%M:%S')}) ===")
    f = feed()
    f.set_symbols(syms)
    for i in range(2):
        snap = f.poll()
        print(f"\n轮询 {i + 1}:")
        for sym in syms:
            data = snap.get(sym.lower(), {})
            if data:
                print(f"  {sym}: {data.get('close', 'N/A')} @ {data.get('ts', 'N/A')} [{data.get('source', '?')}]")
            else:
                print(f"  {sym}: 无数据")
        time.sleep(2)
    status = f.get_status()
    print(f"\n状态: {json.dumps(status, indent=2, ensure_ascii=False)}")
