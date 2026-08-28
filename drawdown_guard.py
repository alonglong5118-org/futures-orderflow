# -*- coding: utf-8 -*-
"""四维策略 · 回撤水位线自动降险（#119）
========================================================
基于「账户动态权益峰值」的分档降险：
  - 权益刷新出新高 → 峰值上移，回撤归零，降险系数回到 1.0
  - 回撤触及水位线 → 新开仓手数按对应系数缩放（graduated，非二值开关）
  - 状态落盘 drawdown_state.json（进程重启不洗白峰值 / 档位）

默认水位线（可在 trade_config.json 的 risk_gate.drawdown_waterlines 覆盖）：
  [5%  → 0.70,  10% → 0.50,  15% → 0.00]
  15% 与硬熔断 KILL_DRAWDOWN 对齐（此时本已禁开，系数 0 为双保险）。

为什么不和 risk_state_machine 的 0.08 二值降档重复：
  旧逻辑只有「回撤>8% → WARNING → 统一 0.5×」一档；本模块把它升级为
  多档渐变、可配置、且跨重启持久化，因此 risk_state_machine 的回撤分支已移除，
  回撤降险的唯一来源是本模块（避免双重惩罚）。

用法（runner 调用）：
  import drawdown_guard as ddg
  ddg.init_from_config()                 # 启动加载水位线
  st = ddg.update(dynamic_equity)        # 每轮喂动态权益
  f  = ddg.scale_factor()                # 应用于信号手数
  st = ddg.current()                     # 供面板展示
  ddg.reset_peak(equity)                 # 人工解除熔断时重置峰值
"""
from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "drawdown_state.json")
# 默认三档：[阈值(小数), 新开仓缩放系数]
_CONFIG = {"waterlines": [(0.05, 0.70), (0.10, 0.50), (0.15, 0.00)]}
_LOCK = threading.RLock()


def init_from_config():
    """从 trade_config.json 读取 risk_gate.drawdown_waterlines（可选覆盖）。
    格式：[[阈值百分比, 系数], ...] 或 [[阈值小数, 系数], ...]（>1 自动视为百分比）。"""
    try:
        from account_tracker import load_config
        cfg = load_config()
        wl = cfg.get("risk_gate", {}).get("drawdown_waterlines")
        if wl:
            parsed = []
            for row in wl:
                th, sc = row[0], row[1]
                th = float(th)
                if th > 1.0:        # 形如 5 / 10 → 视为百分比
                    th = th / 100.0
                parsed.append((th, float(sc)))
            parsed.sort(key=lambda x: x[0])   # 升序，便于查找
            if parsed:
                _CONFIG["waterlines"] = parsed
    except Exception:
        pass
    return _CONFIG["waterlines"]


def waterlines():
    return list(_CONFIG["waterlines"])


def _load():
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE, encoding="utf-8")) or {}
            return d
    except Exception:
        pass
    # P1-13 fix: 添加日内回撤默认字段
    return {"peak_equity": None, "dd_pct": 0.0, "tier": 0,
            "scale": 1.0, "updated": "", "thresholds": waterlines(),
            "intraday_peak": None, "intraday_peak_date": None,
            "intraday_dd_pct": 0.0, "opening_equity": None}


def _save(d):
    try:
        tmp = STATE_FILE + ".tmp"
        json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def update(equity):
    """每轮喂入动态权益，更新峰值 / 回撤 / 档位，返回当前状态 dict。
    
    P1-13 fix: 新增日内峰值追踪，用于计算日内最大回撤（独立于全周期峰值）。
    日内峰值在每日首次调用时初始化为当日开盘权益。"""
    with _LOCK:
        st = _load()
        peak = st.get("peak_equity")
        # P1-13: 日内峰值追踪
        today = time.strftime("%Y-%m-%d")
        intraday_peak = st.get("intraday_peak")
        intraday_date = st.get("intraday_peak_date")
        opening_equity = st.get("opening_equity")
        try:
            eq = float(equity)
        except Exception:
            eq = None
        if eq is None or eq <= 0:
            st["thresholds"] = waterlines()
            return st
        # P1-13: 新的一天 → 初始化日内峰值
        if intraday_date != today:
            intraday_peak = eq
            st["intraday_peak_date"] = today
            st["opening_equity"] = eq  # 记录日初权益
            st["intraday_peak"] = eq
        elif intraday_peak is None or eq > intraday_peak:
            intraday_peak = eq
            st["intraday_peak"] = eq
        # 全周期峰值
        if peak is None or eq > peak:
            peak = eq
        # 全周期回撤
        dd = (peak - eq) / peak if peak > 0 else 0.0
        # P1-13: 日内回撤
        intraday_dd = (intraday_peak - eq) / intraday_peak if intraday_peak > 0 else 0.0
        # P1-13 深度重审加固：档位查找取 max(全周期dd, 日内dd)，
        # 避免"历史峰值高→全周期回撤小但当日回撤巨大"时风控档位偏松的问题。
        # 例如：账户长期从 200w 涨到 400w 峰值，今日从 400w 跌回 380w →
        #       全周期 dd=5% 偏松，但 intraday_dd=5% 已经接近 10% 档位，
        #       用 max 就能在当日剧烈回撤时及时降仓。
        _dd_for_scale = max(dd, intraday_dd)
        wls = waterlines()
        scale = 1.0
        tier = 0
        for i, (th, sc) in enumerate(wls):
            if _dd_for_scale >= th:
                scale = sc
                tier = i + 1
        st["peak_equity"] = peak
        st["dd_pct"] = round(dd * 100, 2)
        # P1-13 fix: 同时输出日内回撤指标
        st["intraday_dd_pct"] = round(intraday_dd * 100, 2)
        st["intraday_peak"] = intraday_peak
        st["opening_equity"] = st.get("opening_equity", eq)
        st["tier"] = tier
        st["scale"] = scale
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["thresholds"] = wls
        _save(st)
        return st


def scale_factor():
    try:
        return float(_load().get("scale", 1.0))
    except Exception:
        return 1.0


def current():
    st = _load()
    st["thresholds"] = waterlines()
    return st


def reset_peak(equity=None):
    """重置峰值权益（人工解除熔断时调用），使回撤归零、降险系数回 1.0。
    
    P1-13 fix: 同时重置日内峰值追踪。"""
    with _LOCK:
        st = _load()
        if equity is not None:
            try:
                st["peak_equity"] = float(equity)
                st["intraday_peak"] = float(equity)
                st["opening_equity"] = float(equity)
                st["intraday_peak_date"] = time.strftime("%Y-%m-%d")
            except Exception:
                st["peak_equity"] = None
                st["intraday_peak"] = None
        else:
            st["peak_equity"] = None
            st["intraday_peak"] = None
        st["dd_pct"] = 0.0
        st["intraday_dd_pct"] = 0.0
        st["tier"] = 0
        st["scale"] = 1.0
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["thresholds"] = waterlines()
        _save(st)
        return st


if __name__ == "__main__":
    import tempfile
    # 自测：用临时状态文件，不污染真实文件
    STATE_FILE = os.path.join(tempfile.gettempdir(), "ddg_selftest.json")
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    init_from_config()
    print("水位线:", [(round(t * 100, 1), s) for t, s in waterlines()])
    print("100万:", update(1_000_000)["scale"], "(应=1.0)")
    print("97万 :", update(970_000)["scale"], "(应=1.0, 未触线)")
    print("94万 :", update(940_000)["scale"], "(应=0.7, 触5%线)")
    print("89万 :", update(890_000)["scale"], "(应=0.5, 触10%线)")
    print("84万 :", update(840_000)["scale"], "(应=0.0, 触15%线)")
    print("105万:", update(1_050_000)["scale"], "(应=1.0, 新高归零)")
    print("reset:", reset_peak(990_000)["scale"], "(应=1.0)")
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass
