#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

import time
import json
import os
import threading

# 主力合约映射（品种 → akshare 合约代码）
ALL_CONTRACTS = {
    # 持仓品种
    'zn': 'ZN2610', 'ss': 'SS2610', 'fu': 'FU2611',
    'sp': 'SP2611', 'J': 'J2701', 'eb': 'EB2610', 'pg': 'PG2610',
    # 其他品种（可按需扩展）
    'cu': 'CU2610', 'al': 'AL2610', 'ni': 'NI2610', 'sn': 'SN2610',
    'pb': 'PB2610', 'au': 'AU2610', 'ag': 'AG2610', 'rb': 'RB2610',
    'hc': 'HC2610', 'bu': 'BU2610', 'ru': 'RU2701', 'sc': 'SC2610',
    'eg': 'EG2610', 'l': 'L2701', 'pp': 'PP2701', 'v': 'V2701',
    'm': 'M2701', 'y': 'Y2701', 'a': 'A2611', 'b': 'B2611',
    'p': 'P2701', 'c': 'C2611', 'cs': 'CS2611', 'jd': 'JD2610',
    'lh': 'LH2611', 'fg': 'FG2701', 'sa': 'SA2701', 'ma': 'MA2610',
    'ta': 'TA2701', 'cf': 'CF2701', 'rm': 'RM2609', 'oi': 'OI2611',
    'sr': 'SR2701', 'ur': 'UR2701', 'ap': 'AP2610', 'pk': 'PK2610',
}


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
                close = float(latest['close'])
                # 合理性检查：价格为 0 或负数说明数据异常
                if close <= 0:
                    return None
                return {
                    'close': close,
                    'open': float(latest['open']),
                    'high': float(latest['high']),
                    'low': float(latest['low']),
                    'volume': int(latest['volume']),
                    'hold': int(latest['hold']) if 'hold' in latest.index else 0,
                    'ts': str(latest['datetime']),
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
            code = ALL_CONTRACTS.get(sym_lower)
            if not code:
                code = ALL_CONTRACTS.get(sym.upper())
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
            now_str = time.strftime('%H:%M:%S')
            if self._error_count <= self._max_errors:
                print(f"[akshare_live] ℹ️ {now_str} 大部分品种无数据（可能休市），"
                      f"已获取 {len(results)}/{len(syms)} 个品种")
            # 不标记为 unavailable，只是返回空结果
        else:
            self._error_count = 0
            
        return self._cache.copy()
        
    def get_price(self, symbol):
        """获取单个品种的最新价格"""
        sym = symbol.lower()
        if sym in self._cache:
            return self._cache[sym].get('close', 0)
        return 0
        
    def get_status(self):
        """获取数据源状态"""
        now = time.time()
        data_age = now - self._last_poll if self._last_poll > 0 else -1
        return {
            'available': self._available,
            'last_poll': time.strftime('%H:%M:%S', time.localtime(self._last_poll)) if self._last_poll > 0 else '从未',
            'symbols': len(self._cache),
            'fresh': data_age < 30 and len(self._cache) > 0,
            'data_age_sec': data_age,
            'error_count': self._error_count,
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


if __name__ == '__main__':
    import sys
    syms = sys.argv[1:] if len(sys.argv) > 1 else ['zn', 'ss', 'fu', 'sp', 'J', 'eb', 'pg']
    
    print(f"=== akshare_live 测试 ({time.strftime('%H:%M:%S')}) ===")
    f = feed()
    f.set_symbols(syms)
    
    # Poll twice to test caching
    for i in range(2):
        snap = f.poll()
        print(f"\n轮询 {i+1}:")
        for sym in syms:
            data = snap.get(sym.lower(), {})
            if data:
                print(f"  {sym}: {data['close']} @ {data['ts']}")
            else:
                print(f"  {sym}: 无数据（休市或获取失败）")
        time.sleep(2)
    
    status = f.get_status()
    print(f"\n状态: {status}")
