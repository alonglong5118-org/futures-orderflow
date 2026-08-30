"""四维策略 · 实盘信号 runner（独立）
=================================================================
盘中循环：盘前用 akshare 刷新 F，盘中每 60s 用 minishare 实时快照算
pipeline，触发即出信号（红横幅+语音+日志+Web 面板）。

数据分工（终态，2026-08-11 用户拍板）：
  - 实时价 / 实盘 T@5m / C_flow  → minishare rt_fut_k（不限次）
  - 历史日线（ATR/偏置）          → 本地 CSV（量化回测/_XX0_daily.csv）
  - 基本面 F（基差/库存）          → akshare（minishare 无 fut_basis 权限）
  - C_pos 历史                     → 本地 cpos_cache（minishare 无 fut_lhb 权限）

运行：
  python four_dim_live_runner.py                 # 常驻
  python four_dim_live_runner.py --once          # 单次评估（测试，忽略时段）
  python four_dim_live_runner.py --no-voice      # 关语音
  python four_dim_live_runner.py --port 8741     # 面板端口
依赖：four_dim_strategy / minishare_live / fundamental_feed / strategy_layer
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from datetime import time as dtime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# 概率校准 OOS 期望R源（variety_edge/_load_calib 依赖；重构时漏定义导致 edge 静默返回空）
CALIB_FILE = os.path.join(HERE, "calibration_params.json")
sys.path.insert(0, HERE)

# 系统版本号（方案 B：由 /api/state 暴露，前端侧栏实时渲染，避免文档升级漏改面板标签）
APP_VERSION = "v3.5.0"

# —— 日志强化（P1 + P2-2，2026-08-13）——
# launchd 下 stdout/stderr 是管道而非 TTY：①Python 默认块缓冲(~8KB)，print 的异常会滞留
# 缓冲区、在日志里"消失"（P0 当初因此被掩盖）→ 需非缓冲/行缓冲；②日志文件无限增长、
# 旧版 KeyError 'SA01' 等已失效 traceback 堆积不轮转（P2-2）→ 接管为按大小轮转。
# 策略：非 TTY 且能定位 launchd 日志路径时，用「行缓冲 + 按大小轮转」流替换 stdout/stderr；
# 否则退化为强制行缓冲。并装 sys.excepthook 把未捕获异常另存 crash.log 双保险。


class _RotatingLogStream:
    """行缓冲 + 按大小轮转的文本流，用于接管 sys.stdout/stderr。"""

    def __init__(self, path, cap=5 * 1024 * 1024, backups=3):
        self.path = path
        self.cap = cap
        self.backups = backups
        self.encoding = "utf-8"
        self.errors = "strict"
        self._open()

    def _open(self):
        self._f = open(self.path, "a", encoding="utf-8", buffering=1)  # 行缓冲

    def write(self, s):
        try:
            self._f.write(s)
            self._f.flush()
            if self._f.tell() >= self.cap:
                self._rotate()
        except Exception:
            pass

    def _rotate(self):
        try:
            self._f.close()
        except Exception:
            pass
        for i in range(self.backups - 1, 0, -1):
            _src, _dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(_src):
                try:
                    os.replace(_src, _dst)
                except Exception:
                    pass
        if os.path.exists(self.path):
            try:
                os.replace(self.path, f"{self.path}.1")
            except Exception:
                pass
        self._open()

    def flush(self):
        try:
            self._f.flush()
        except Exception:
            pass

    def fileno(self):
        return self._f.fileno()

    def isatty(self):
        return False

    def writable(self):
        return True

    def writelines(self, lines):
        for _ln in lines:
            self.write(_ln)

    @property
    def closed(self):
        return False


def _install_rotating_logs():
    """非 TTY（launchd 管道）且能解析到自身 plist 的日志路径时，
    把 stdout/stderr 替换为行缓冲+按大小轮转流，根治 P2-2 日志堆积。"""
    if sys.stdout.isatty() or sys.stderr.isatty():
        return
    _plist = os.path.expanduser("~/Library/LaunchAgents/com.ken.futures-orderflow.live.plist")
    _out = _err = None
    try:
        import plistlib

        _d = plistlib.load(open(_plist, "rb"))
        _out, _err = _d.get("StandardOutPath"), _d.get("StandardErrorPath")
    except Exception:
        pass
    if _out and os.path.isfile(_out):
        try:
            sys.stdout = _RotatingLogStream(_out)
        except Exception:
            pass
    if _err and os.path.isfile(_err):
        try:
            sys.stderr = _RotatingLogStream(_err)
        except Exception:
            pass


def _harden_logging():
    _install_rotating_logs()
    # 未被轮转流接管的（交互/手动启动）→ 至少强制行缓冲，确保异常即时落盘（P1）
    try:
        if not isinstance(sys.stdout, _RotatingLogStream):
            sys.stdout.reconfigure(line_buffering=True)
        if not isinstance(sys.stderr, _RotatingLogStream):
            sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _crash_excepthook(etype, exc, tb):
    import traceback as _tb

    _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _msg = f"[{_ts}] [FATAL] 未捕获异常:\n" + "".join(_tb.format_exception(etype, exc, tb))
    try:
        sys.stderr.write(_msg)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        with open(os.path.join(HERE, "crash.log"), "a") as _f:
            _f.write(_msg)
            _f.flush()
    except Exception:
        pass


_harden_logging()
sys.excepthook = _crash_excepthook

import four_dim_strategy as fd
from four_dim_strategy import (
    AUTO_RECOVER_SYMBOLS,
    DEFAULT_CONFIG,
    DISABLED_SYMBOLS,
    SYMBOLS,
    build_signal,
    exit_plan,
    load_daily_refreshed,
    pipeline,
    risk_gate,
    score_F,
    variety_of,
)

# P-B/P-C（2026-08-14）：合并 trade_config.json 的 bias_synthesis 覆盖，使策略合成参数可调参而不改码。
# 缺省用 four_dim_strategy.DEFAULT_CONFIG["bias_synthesis"]；trade_config.json 同名字段覆盖（浅合并）。
_STRAT_CFG = dict(DEFAULT_CONFIG)
try:
    _tc_bs = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8")).get("bias_synthesis")
    if isinstance(_tc_bs, dict):
        _STRAT_CFG["bias_synthesis"] = {**DEFAULT_CONFIG.get("bias_synthesis", {}), **_tc_bs}
except Exception:
    pass
# P-H (2026-08-14): 稳健池门槛动态回灌配置注入(trade_config.json 覆盖 DEFAULT_CONFIG)
try:
    _tc_rpg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8")).get("robust_pool_gate")
    if isinstance(_tc_rpg, dict):
        _STRAT_CFG["robust_pool_gate"] = {**DEFAULT_CONFIG.get("robust_pool_gate", {}), **_tc_rpg}
except Exception:
    pass
# P-A / P-D / P-F / P-G / ⚪权益错配 (2026-08-14): 把 live 路径依赖的全部可调参块从 trade_config.json
# 覆盖进 _STRAT_CFG。否则 live 只吃 DEFAULT_CONFIG 的浅拷贝副本、trade_config.json 调参对实盘不生效
# （⚪ 关键：account/risk_gate/contract_specs 之前未合并，导致 risk_gate 手数预算按 DEFAULT_CONFIG 的
#   equity=100000 计算，而非 trade_config 的 612140，实盘手数/保证金上限/最大手数与账户真实状态错配）。
_tc_all = {}
try:
    _tc_all = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8")) or {}
except Exception:
    _tc_all = {}
for _blk in (
    "decorrelate",
    "seasonal_boost",
    "regime_params",
    "trailing_tail",
    "account",
    "risk_gate",
    "contract_specs",
    "per_symbol_risk",
    "thresholds_by_symbol",  # GA优化T_thresh逐品种覆盖 (2026-08-29 Phase 3.5)
):
    _tc_blk = _tc_all.get(_blk)
    if isinstance(_tc_blk, dict):
        _STRAT_CFG[_blk] = {**DEFAULT_CONFIG.get(_blk, {}), **_tc_blk}
import strategy_layer
from strategy_layer import atr as strat_atr

# P-H (2026-08-14): 启动注入稳健池回灌配置 + 读回灌文件(enabled 时生效)
try:
    strategy_layer.configure_robust_gate(**_STRAT_CFG.get("robust_pool_gate", {}))
    strategy_layer.load_robust_gate_file()
except Exception:
    pass
import account_tracker as at
import akshare_live as al
import four_dim_papertrack as pt  # 真实 walk-forward 回测 + 动态表现门控

# ★ 2026-08-28: 主数据源切换为 minishare rt_fut_k（不限次快照、实时性更好）
import fundamental_feed as ff
import minishare_live as ml

# —— 从 da龘 合并进来的四大能力 ——
import risk_state_machine as rsm  # 仓位状态机（风控升级）
import trade_journal as tj
import tushare_live as tl  # 保留兼容（降级用）

try:
    import direction_source_monitor as dsm  # 方向源偏差监控（红线①，2026-08-16）
except Exception:
    dsm = None
import account_monitor as am  # 账户监控驱动 papertrack 自动化
import anomaly_scan as asc  # 异动扫描层（广度选品）
import backtest_viz as bv  # #17 回测可视化(水下曲线/逐笔散点)
import blunder_check as bc  # #12 纪律自动体检(blunder检测)
import broker_import as bi  # #9 经纪商成交明细自动回灌
import calibration as cal  # #120 概率校准 + 置信度分层命中率
import consistency_watchdog as cw  # #5 训练/服务一致性看门狗(train/serve parity)
import data_quality as dq  # #14 数据质量/陈旧监控
import discipline_review as dr
import drawdown_guard as ddg  # #119 回撤水位线自动降险（渐变 + 持久化）
import event_calendar as ec  # #13 事件/数据日历闸门
import execution_planner as exp  # #7 大单拆分/冰山/TWAP 执行建议
import feature_manager as fmg  # 特性开关管理器（热加载/切换/日志）
import four_dim_calibrate as fdc  # #121 已接入 CLI：真重校准扫描
import four_dim_recalibrate as fdr  # #121 已接入 CLI：校准漂移检测
import fundamental_metrics as fm  # G1 基本面指标（利润/比价/价差）
import ga_factor_miner as gfm  # #10 GA 因子挖掘+权重优化(live 专属)
import gbm_garch as gg  # #7 (续) GBM/GARCH 波动率动力学+前向情景(live 专属)
import gen_papertrack_html as gph  # #121 已接入 CLI：回测报告→HTML
import info_dimension as idim  # #1 信息维度(资讯/新闻/情绪/另类数据)F 覆盖层
import macro_context as mctx  # #6 跨资产宏观语境(live 专属，回测 macro_label=None 不进)
import market_scanner as mscan  # #11 全市场批量扫描(并行)
import montecarlo as mc  # #11 蒙特卡洛权益曲线置信区间
import paper_trading_integration as pti  # 自动模拟交易引擎集成
import push_notify as pn  # #15 手机推送(Telegram/Bark/企业微信)
import regime_hmm as rhmm  # #7 HMM 市场状态识别(live 专属，回测不要调用)
import sentiment_engine as senteng  # #8 市场情绪系统(live 专属，回测 sentiment_label=None 不进)
import signal_explain as sexp  # #4 信号解释(确定性 driver 解释 + 可选 LLM 增强层)
import sr_analyzer as sra  # #9 支撑压力位识别(live 专属，回测 sr_result=None 不进)
import symbol_screener as sscreener  # #11 品种筛选引擎

# —— #3 盘口级订单流：把真实 tick 的 Delta/吸收/失衡 接入 C_flow（push_tick） ——
import tick_orderflow as tof
import viz_upgrade as viz  # #11 回测可视化增强(Plotly)

# ---------------------------------------------------------------------------
# #121 已接入 CLI 工具箱：把原本「纯命令行、面板无入口」的工具接到面板，
# 后台线程运行（避免阻塞 HTTP 处理），结果落 tools_state.json 供面板回看。
# ---------------------------------------------------------------------------
_TOOL_DEFS = {
    "calibrate": {"mod": fdc, "fn": "main", "label": "真重校准扫描", "args": (), "kwargs": {}},
    "recalibrate": {"mod": fdr, "fn": "main", "label": "校准漂移检测", "args": (), "kwargs": {"apply": False}},
    "papertrack_html": {"mod": gph, "fn": "main", "label": "生成回测可视化HTML", "args": (), "kwargs": {}},
}
_TOOL_STATE = {"running": None, "last": {}}
_TOOL_LOCK = threading.Lock()
_TOOLS_STATE_FILE = os.path.join(HERE, "tools_state.json")


def _load_tools_state():
    try:
        d = json.load(open(_TOOLS_STATE_FILE, encoding="utf-8"))
        _TOOL_STATE["last"] = d.get("last", {}) or {}
    except Exception:
        pass


def _run_tool_thread(name):
    import contextlib
    import io
    import traceback

    spec = _TOOL_DEFS.get(name)
    if not spec:
        return
    buf = io.StringIO()
    try:
        fn = getattr(spec["mod"], spec["fn"])
        with contextlib.redirect_stdout(buf):
            fn(*spec.get("args", ()), **spec.get("kwargs", {}))
        out = buf.getvalue()
        res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": True, "output": out[-4000:], "error": None}
    except Exception:
        res = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False,
            "output": buf.getvalue()[-2000:],
            "error": traceback.format_exc()[-2000:],
        }
    with _TOOL_LOCK:
        _TOOL_STATE["last"][name] = res
        _TOOL_STATE["running"] = None
        try:
            json.dump({"last": _TOOL_STATE["last"]}, open(_TOOLS_STATE_FILE, "w"), ensure_ascii=False, indent=2)
        except Exception:
            pass


def run_tool(name):
    """后台启动一个已接入 CLI 工具；若已有工具在跑则返回 False。"""
    with _TOOL_LOCK:
        if _TOOL_STATE["running"]:
            return False, f"已有工具在运行: {_TOOL_STATE['running']}"
        _TOOL_STATE["running"] = name
    t = threading.Thread(target=_run_tool_thread, args=(name,), daemon=True)
    t.start()
    return True, f"已启动 {_TOOL_DEFS.get(name, {}).get('label', name)}"


# ── 运行时禁用集合（自适应恢复）：初始化为 DISABLED_SYMBOLS，恢复期自动移除 ──
# 硬禁(au/ag/.../RM)永不自动恢复；AUTO_RECOVER_SYMBOLS(JM/hc)周期性检查，walk-forward
# 转正即从本集合移除→重新参与信号评估。重启后重置回 DISABLED_SYMBOLS 重新评估。
RUNTIME_DISABLED = set(DISABLED_SYMBOLS)
RECOVERY_SEC = 3600  # 自适应恢复检查周期（秒）
last_recover = 0.0  # 上次恢复检查时间戳（=0 让启动即检一次）           # 管住手复盘卡（日/周/月纪律评分）

# —— 动态表现门控：某品种近期真实回测转负则自动暂停发信号（卡片保留），恢复后自动解除 ——
GATE_CACHE = {"ts": 0.0, "gates": {}}

FEED = None  # 全局实时行情实例，main 启动时赋值，供 Web 接口算浮动盈亏
FEED_LAST_UPDATE = 0.0  # 最近一次成功 poll 的时间戳（行情健康指示用）
FEED_AVAILABLE = False  # 行情源是否可用（minishare 接口可达）

# ★ 2026-08-27: akshare 实时行情（分钟级，新浪数据源）
_AK_PRICES = {}  # {symbol: price} 最新 akshare 价格
_AK_PRICE_TS = 0.0  # akshare 最近更新时间戳
_AK_AVAILABLE = False  # akshare 数据源是否可用
_AK_SYMBOLS = []  # 需要监控的品种列表
_AK_LOCK = threading.Lock()

# ★ 2026-08-28: Tushare Pro 主数据源（Tushare → akshare 自动降级）
_TS_PRICES = {}  # {symbol: price} 最新 Tushare 价格
_TS_PRICE_TS = 0.0  # Tushare 最近更新时间戳
_TS_AVAILABLE = False  # Tushare 数据源是否可用
_TS_SYMBOLS = []  # 需要监控的品种列表
_TS_LOCK = threading.Lock()


def _ak_poller():
    """后台线程：每 5 秒轮询 akshare 实时行情（分钟级，新浪数据源）。"""
    global _AK_PRICES, _AK_PRICE_TS, _AK_AVAILABLE, _AK_SYMBOLS
    print("[akshare_live] 后台轮询线程启动（5秒/次）")
    _AK_FEED = None
    _ak_errors = 0
    while True:
        try:
            if _AK_FEED is None:
                _AK_FEED = al.feed()
                _AK_FEED.set_symbols(_AK_SYMBOLS or list(al.ALL_CONTRACTS.keys()))
            snap = _AK_FEED.poll()
            if snap:
                with _AK_LOCK:
                    _AK_PRICES = {s: d.get("close", 0) for s, d in snap.items() if d and d.get("close", 0) > 0}
                    _AK_PRICE_TS = time.time()
                    _AK_AVAILABLE = True
                _ak_errors = 0
            else:
                _ak_errors += 1
                if _ak_errors >= 5:
                    _AK_AVAILABLE = False
        except Exception:
            _ak_errors += 1
            if _ak_errors >= 5:
                _AK_AVAILABLE = False
        time.sleep(5)


def _ts_poller():
    """后台线程：每 5 秒轮询 minishare rt_fut_k 快照（非交易时段回退源）。

    ⚠️ 2026-08-28 修复：minishare rt_fut_k 仅在收盘时更新数据，盘中返回的是
    上一收盘价（stale）。因此本线程不再覆盖 _AK_PRICES（新浪实时缓存），
    只维护 _TS_PRICES 供非交易时段回退使用。
    盘中实时价由 _ak_poller（新浪财经）提供。"""
    global _TS_PRICES, _TS_PRICE_TS, _TS_AVAILABLE, _TS_SYMBOLS
    print("[minishare_live] rt_fut_k 后台轮询线程启动（5秒/次，非交易时段回退源）")
    _ms_feed = None
    _ts_errors = 0
    while True:
        try:
            if _ms_feed is None:
                _ms_feed = ml.feed()  # minishare_live.rt_fut_k 单例
                if not _ms_feed.available():
                    print("[minishare_live] rt_fut_k 不可用，等待恢复...")
                    time.sleep(10)
                    continue
            # 调用 rt_fut_k 获取全市场快照
            snap = _ms_feed.poll()
            if snap:
                with _TS_LOCK:
                    _TS_PRICES = {s: d.get("close", 0) for s, d in snap.items() if d and d.get("close", 0) > 0}
                    _TS_PRICE_TS = time.time()
                    _TS_AVAILABLE = True
                _ts_errors = 0
            else:
                _ts_errors += 1
                if _ts_errors >= 5:
                    _TS_AVAILABLE = False
        except Exception:
            _ts_errors += 1
            if _ts_errors >= 5:
                _TS_AVAILABLE = False
        time.sleep(5)


def _is_trading_hours():
    """判断当前是否在期货交易时段（夜盘 21:00-23:00/02:30，日盘 9:00-15:00）。"""
    from datetime import datetime as _dt

    now = _dt.now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    # 夜盘 21:00 - 23:00 (部分品种到 02:30)
    if t >= 21 * 60 or t <= 2 * 60 + 30:
        return True
    # 日盘 9:00 - 15:00
    if 9 * 60 <= t <= 15 * 60:
        return True
    return False


def get_realtime_price(sym):
    """获取实时价格。

    ⚠️ 2026-08-28 修复：minishare rt_fut_k 盘中返回上一收盘价（stale），
    而 akshare（新浪财经）提供盘中实时数据。因此盘中优先 akshare，
    非交易时段或 akshare 不可用时回退 minishare。

    优先级：
    1) 交易时段：akshare（新浪实时）→ minishare → account_state
    2) 非交易时段：minishare → akshare → account_state
    """
    sym_lower = sym.lower()
    trading = _is_trading_hours()

    if trading:
        # 交易时段：优先 akshare（新浪实时）
        if _AK_AVAILABLE and _AK_PRICE_TS > 0 and (time.time() - _AK_PRICE_TS) < 60:
            if sym_lower in _AK_PRICES and _AK_PRICES[sym_lower] > 0:
                return _AK_PRICES[sym_lower]
        # 按需从 akshare 拉取（缓存未命中时）
        try:
            import akshare_live as _al_on_demand

            _f = _al_on_demand.feed()
            if _f.available():
                _code = _al_on_demand.ALL_CONTRACTS.get(sym_lower, _al_on_demand.ALL_CONTRACTS.get(sym.upper()))
                if _code:
                    _snap = _f.poll([sym_lower])
                    if _snap and sym_lower in _snap:
                        _px = _snap[sym_lower].get("close", 0)
                        if _px and _px > 0:
                            return _px
        except Exception:
            pass
        # 回退 minishare（虽过时但总比没有好）
        if FEED is not None:
            px = FEED.price(sym)
            if px and px > 0:
                return px
        if _TS_AVAILABLE and _TS_PRICE_TS > 0:
            if sym_lower in _TS_PRICES and _TS_PRICES[sym_lower] > 0:
                return _TS_PRICES[sym_lower]
    else:
        # 非交易时段：优先 minishare（收盘快照价）
        if FEED is not None:
            px = FEED.price(sym)
            if px and px > 0:
                return px
        if _TS_AVAILABLE and _TS_PRICE_TS > 0:
            if sym_lower in _TS_PRICES and _TS_PRICES[sym_lower] > 0:
                return _TS_PRICES[sym_lower]
        # 回退 akshare
        if _AK_AVAILABLE and _AK_PRICE_TS > 0:
            if sym_lower in _AK_PRICES and _AK_PRICES[sym_lower] > 0:
                return _AK_PRICES[sym_lower]

    # 最终回退：account_state 存储价
    try:
        st = at.load_state()
        pos = st["positions"].get(sym_lower, st["positions"].get(sym.upper(), {}))
        return pos.get("price", 0) or 0
    except Exception:
        return 0


# —— #16 进程看门狗心跳 ——
START_TIME = time.time()  # 进程启动时刻（/api/health 用）
LAST_CYCLE_TS = 0.0  # 主循环最近一次完成的时间戳（卡死检测用）
_LAST_HEAL_TS = 0.0  # 上次自动对账时间戳（确保数据实时同步）
_HEAL_INTERVAL = 30  # 自动对账间隔（秒）


def _poll_feed(feed):
    """轮询行情并刷新健康时间戳/可用标志（行情健康指示用）。"""
    global FEED_LAST_UPDATE, FEED_AVAILABLE
    try:
        feed.poll()
        FEED_LAST_UPDATE = datetime.now().timestamp()
        FEED_AVAILABLE = bool(feed.available())
    except Exception:
        FEED_AVAILABLE = False


POLL_SEC = 60  # minishare 轮询间隔（与 minishare.json interval 对齐）

# ── #3 盘口级订单流连接器 ──
# 把真实 tick 流（本地 jsonl 流文件 或 WebSocket）转成订单流分数，灌入
# FEED.flow[sym].push_tick()，与 minishare 快照净流混合进 C_flow。
# 优雅降级：无 tick 源时静默空转，C_flow 完全退回 minishare 驱动（不破坏现有路径）。
TICK_FEED_ENABLED = os.environ.get("TICK_FEED_ENABLED", "0") == "1"
TICK_STREAM_FILE = os.environ.get("TICK_STREAM_FILE", os.path.join(HERE, "tick_stream.jsonl"))
TICK_WS_URL = os.environ.get("TICK_WS_URL", "")  # 例: ws://10.211.55.3:8765 （da龘 CTP tick 网关）


class TickFeedConnector:
    def __init__(self):
        self.acc = {}  # sym -> tof.TickOrderflow
        self.last_read = 0
        self.running = False

    def _acc_for(self, sym):
        if sym not in self.acc:
            self.acc[sym] = tof.TickOrderflow(sym)
        return self.acc[sym]

    def push(self, sym, tick):
        """接收一笔 tick，更新订单流累积器并把分数 push 进 feed.flow。"""
        global FEED
        a = self._acc_for(sym)
        a.push(
            tick.get("price", 0),
            tick.get("vol", 0),
            tick.get("side"),
            tick.get("bid_vol"),
            tick.get("ask_vol"),
            tick.get("ts"),
        )
        score = a.score()
        if FEED is not None and hasattr(FEED, "flow") and sym in FEED.flow and FEED.flow[sym] is not None:
            FEED.flow[sym].push_tick(score)

    def _run_stream_file(self):
        """tail 本地 tick 流文件（jsonl，增量读取）。"""
        import os as _os

        if not _os.path.exists(TICK_STREAM_FILE):
            return
        with open(TICK_STREAM_FILE) as f:
            size = _os.path.getsize(TICK_STREAM_FILE)
            if size < self.last_read:
                self.last_read = 0  # tick 源重启会 truncate 文件 → 从头重读，避免 C_flow 永久空转
            f.seek(self.last_read)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except Exception:
                    continue
                sym = t.get("symbol")
                if sym:
                    self.push(sym, t)
            self.last_read = f.tell()

    def loop(self):
        self.running = True
        while self.running:
            try:
                if TICK_WS_URL:
                    self._run_ws()  # 阻塞式 WS（内部断线重连）
                else:
                    self._run_stream_file()
                    time.sleep(2)
            except Exception:
                time.sleep(5)

    def _run_ws(self):
        try:
            import websocket  # 需 pip install websocket-client
        except ImportError:
            time.sleep(30)
            return
        while self.running:
            try:
                ws = websocket.create_connection(TICK_WS_URL, timeout=10)
                while self.running:
                    raw = ws.recv()
                    try:
                        t = json.loads(raw)
                    except Exception:
                        continue
                    sym = t.get("symbol")
                    if sym:
                        self.push(sym, t)
            except Exception:
                time.sleep(5)


# 全局连接器实例（main 启动时起线程）
TICK_CONNECTOR = TickFeedConnector()

# —— 信号去重/去抖（取代旧“固定60min翻转冷却”，根治临界抖动连环弹窗）——
#   ① 持续性去抖：dir_T 须同号连续 2 轮才认信号（单轮临界抖动不触发）
#   ② 签名去重：sym|方向|价格桶 完全一致=同一信号，窗口内抑制；
#      不同签名(反向/新价位)=真实新信号，立即推送（窗口不固定为60min）
PRICE_BUCKET_PCT = 0.005  # 0.5% 价格桶：价格变动>0.5% 视为新信号
DEDUPE_SAME_SIG_MIN = 60  # 相同签名信号的最小抑制窗口（分钟）：同一品种同方向 60min 内只推一次

# —— 组合层风险约束（P0-1）：相关性同向 + 总风险预算 ——
# 解决单品种检查盲区：JM(焦煤)/J(焦炭)相关常>0.9、FG/SA 亦高相关，
# 同轮同时触发同向 → 系统发两个独立信号、各算各手数 = 同板块双倍暴露。
PORTFOLIO_CORR_THRESHOLD = 0.70  # 两两日收益相关≥此值视为「同风险敞口」
PORTFOLIO_RISK_PCT = 4.5  # 全组合风险预算上限 = 权益 × 4.5%（Σ|入场-止损|×乘数×手数；P0-3整改：原2.0%≈单笔1.5%导致只能持1个满仓单+新仓偏小，现约容纳3个1.5%单笔；corr/sector/net-dir分项上限仍兜底集中度）
PORTFOLIO_CORR_BUCKET_PCT = 1.0  # 同相关桶（同向）风险上限 = 权益 × 1%（防双倍暴露）
# ── #6 板块集中度 + 单边净敞口上限 ──
# 相关性桶只管「两两高相关」，管不住「五个化工品两两相关都 0.6，加起来却是一把大赌注」；
# 净敞口则管不住「全组合清一色做多」的方向性风险。两道闸补上这两个洞。
PORTFOLIO_SECTOR_PCT = 1.2  # 单板块（化工/黑系/农产品…）风险上限 = 权益 × 1.2%
PORTFOLIO_NET_DIR_PCT = 1.5  # 单边净敞口上限 = 权益 × 1.5%（Σ多风险 或 Σ空风险）
PORTFOLIO_SECTOR_MAX_N = 3

# ─────────────────────────────────────────────────────────────────
# v5 风控增强常量（基于7大量化策略研究）
# ─────────────────────────────────────────────────────────────────
MAX_SINGLE_TRADE_RISK_PCT = 1.0  # 单笔风险上限 = 权益 × 1%（趋势跟踪1%铁律）
MIN_RR_RATIO = 2.0  # 最低盈亏比 = 2:1（趋势跟踪盈亏比要求）
DRAWDOWN_FULL_STOP_PCT = 10.0  # 10%回撤→强制全平+休息（趋势跟踪熔断）
DRAWDOWN_FORCE_REST_SEC = 86400  # 10%回撤后强制休息24小时
TIME_STOP_MAX_DAYS = 5  # 持仓超过5天未达T1→时间止损检查
SIGMA_STOP_MULT = 3.0  # 3σ标准差硬止损（统计套利风控）
CONSEC_LOSS_HARD_LIMIT = 5  # 连亏5次→强制冻结（AI策略风控）

# ── v5.1 分级止损常量 ──
BREAKEVEN_TRIGGER_R = 1.0  # 浮盈达到1R时触发保本止损
TRAILING_STOP_ATR_MULT = 2.0  # 移动止损的ATR倍数
STOP_LOSS_LEVEL_INITIAL = "initial"  # 初始止损
STOP_LOSS_LEVEL_BREAKEVEN = "breakeven"  # 保本止损
STOP_LOSS_LEVEL_TRAILING = "trailing"  # 移动止损
STOP_LOSS_LEVEL_HARD = "hard"  # 硬止损（3σ）

# ── v5.1 信号质量过滤常量 ──
SIGNAL_QUALITY_MIN_SCORE = 60  # 信号质量最低通过分数（百分制）
BREAKOUT_BODY_MIN_PCT = 0.5  # 突破K线实体最小幅度（%）
BREAKOUT_VOLUME_MULT = 1.5  # 突破成交量需达到均量的倍数
BREAKOUT_PULLBACK_CONFIRM = True  # 是否需要回踩确认
VOLUME_MA_PERIOD = 20  # 成交量均线周期

# ── v5.1 多周期确认常量 ──
MULTI_TIMEFRAME_ENABLED = True  # 是否启用多周期确认
HIGHER_TF_TREND_PERIOD = "1d"  # 大周期（日线）
ENTRY_TF_PERIOD = "1h"  # 入场周期（1小时）
HIGHER_TF_MA_FAST = 20  # 大周期快线周期
HIGHER_TF_MA_SLOW = 55  # 大周期慢线周期
COUNTER_TREND_POS_SCALE = 0.5  # 逆大周期时的仓位缩放比例
COUNTER_TREND_RR_BOOST = 1.3  # 逆大周期时盈亏比要求倍率

# ═══════════════════════════════════════════
# v6.0 新增常量 — 市场状态引擎（阶段一）
# ═══════════════════════════════════════════

# ── 市场状态定义 ──
MARKET_STATE_TREND_EARLY = "trend_early"  # 趋势初期
MARKET_STATE_TREND_MID = "trend_mid"  # 趋势中期
MARKET_STATE_TREND_LATE = "trend_late"  # 趋势末期
MARKET_STATE_SIDEWAYS = "sideways"  # 震荡市

# ── 状态切换确认机制 ──
STATE_CONFIRM_BARS = 3  # 连续N根K线确认才切换状态
STATE_HYSTERESIS_PCT = 10  # 状态切换迟滞（评分差>10%才切换）

# ── 技术面识别指标权重（总和=100%） ──
TECH_WEIGHT_MA_ALIGNMENT = 25
TECH_WEIGHT_VOL_LEVEL = 20
TECH_WEIGHT_VOL_CHANGE = 15
TECH_WEIGHT_VOLUME_PRICE = 20
TECH_WEIGHT_TREND_STRENGTH = 20

# ── 技术面识别周期参数 ──
TECH_MA_FAST = 20
TECH_MA_SLOW = 55
TECH_ATR_PERIOD = 14
TECH_VOLUME_MA_PERIOD = 20
TECH_ADX_PERIOD = 14
TECH_LOOKBACK_BARS = 10

# ── 表现面反馈窗口 ──
PERF_WINDOW_SHORT = 5
PERF_WINDOW_MID = 20
PERF_WEIGHT_WINRATE = 30
PERF_WEIGHT_PROFIT_FACTOR = 30
PERF_WEIGHT_STREAK = 20
PERF_WEIGHT_RECENT_R = 20

# ── 状态判定阈值（技术面得分 0-100） ──
TECH_SCORE_TREND_EARLY_MIN = 55
TECH_SCORE_TREND_MID_MIN = 70
TECH_SCORE_TREND_LATE_MIN = 80
TECH_SCORE_SIDEWAYS_MAX = 45

# ── 状态判定阈值（表现面得分 0-100） ──
PERF_SCORE_GOOD_MIN = 65
PERF_SCORE_MID_MIN = 40

# ── 动态参数映射（相对于v5.1基准值的倍数） ──
STATE_PARAM_MAPPING = {
    MARKET_STATE_TREND_EARLY: {
        "single_trade_risk_mult": 1.2,
        "min_rr_ratio_mult": 0.8,
        "quality_score_mult": 0.85,
        "atr_stop_mult": 1.2,
        "time_stop_days_mult": 1.2,
        "max_positions_mult": 1.2,
        "take_profit_mult": 1.5,
    },
    MARKET_STATE_TREND_MID: {
        "single_trade_risk_mult": 1.0,
        "min_rr_ratio_mult": 1.0,
        "quality_score_mult": 1.0,
        "atr_stop_mult": 1.0,
        "time_stop_days_mult": 1.0,
        "max_positions_mult": 1.0,
        "take_profit_mult": 1.0,
    },
    MARKET_STATE_TREND_LATE: {
        "single_trade_risk_mult": 0.5,
        "min_rr_ratio_mult": 1.5,
        "quality_score_mult": 1.3,
        "atr_stop_mult": 0.7,
        "time_stop_days_mult": 0.6,
        "max_positions_mult": 0.6,
        "take_profit_mult": 0.7,
    },
    MARKET_STATE_SIDEWAYS: {
        "single_trade_risk_mult": 0.6,
        "min_rr_ratio_mult": 0.75,
        "quality_score_mult": 1.15,
        "atr_stop_mult": 0.8,
        "time_stop_days_mult": 0.8,
        "max_positions_mult": 0.7,
        "take_profit_mult": 0.6,
    },
}

# ── 引擎开关（阶段一只显示状态，不启用动态参数） ──
MARKET_STATE_ENGINE_ENABLED = True
DYNAMIC_PARAMS_ENABLED = False

# ── v6.0 Phase 2：状态切换日志 ──
STATE_LOG_ENABLED = True
STATE_LOG_MAX_RECORDS = 100
STATE_LOG_FILE = "market_state_log.json"

# ── v6.0 Phase 2：分级止盈状态机常量 ──
TAKE_PROFIT_LEVEL_NONE = "tp_none"
TAKE_PROFIT_LEVEL_T1 = "tp_t1"
TAKE_PROFIT_LEVEL_T2 = "tp_t2"
TAKE_PROFIT_LEVEL_T3 = "tp_t3"
TAKE_PROFIT_LEVEL_DONE = "tp_done"
TP_T1_ATR_MULT = 3.0  # T1: ATR×3 减仓1/3 + 止损上移至保本
TP_T2_ATR_MULT = 5.0  # T2: ATR×5 再减1/3 + 止损上移至T1
TP_T3_TRAILING_ATR_MULT = 2.0  # T3: ATR×2 移动止盈跟踪
TP_SIDEWAYS_SKIP_T3 = True  # 震荡市跳过T3，T2全平
TP_T1_REDUCE_RATIO = 0.33  # T1减仓比例（1/3）
TP_T2_REDUCE_RATIO = 0.50  # T2减仓比例（剩余的1/2 = 总1/3）
TP_T1_STOP_MOVE_TO_BREAKEVEN = True  # T1触发后止损上移至保本
TP_T2_STOP_MOVE_TO_T1 = True  # T2触发后止损上移至T1价位

# ═══════════════════════════════════════════
# v6.0 Phase 3: 策略参数自优化
# ═══════════════════════════════════════════

# ── 总开关 ──
AUTO_OPTIMIZE_ENABLED = False

# ── 可调参数定义 ──
AUTO_OPTIMIZE_PARAMS = {
    "quality_threshold": {
        "label": "信号质量门槛",
        "base": 60,
        "min": 50,
        "max": 75,
        "step": 5,
        "unit": "分",
        "direction": "inverse",
        "locked": False,
        "current_value": 60,
        "last_adjust_time": 0,
        "adjust_count": 0,
    },
    "single_trade_risk_pct": {
        "label": "单笔风险比例",
        "base": 1.0,
        "min": 0.5,
        "max": 1.5,
        "step": 0.1,
        "unit": "%",
        "direction": "direct",
        "locked": False,
        "current_value": 1.0,
        "last_adjust_time": 0,
        "adjust_count": 0,
    },
    "atr_stop_mult": {
        "label": "ATR止损倍数",
        "base": 2.0,
        "min": 1.5,
        "max": 3.0,
        "step": 0.2,
        "unit": "x",
        "direction": "adaptive",
        "locked": False,
        "current_value": 2.0,
        "last_adjust_time": 0,
        "adjust_count": 0,
    },
    "min_rr_ratio": {
        "label": "盈亏比要求",
        "base": 2.0,
        "min": 1.5,
        "max": 3.0,
        "step": 0.2,
        "unit": ":1",
        "direction": "inverse",
        "locked": False,
        "current_value": 2.0,
        "last_adjust_time": 0,
        "adjust_count": 0,
    },
    "time_stop_days": {
        "label": "时间止损天数",
        "base": 5,
        "min": 3,
        "max": 7,
        "step": 1,
        "unit": "天",
        "direction": "adaptive",
        "locked": False,
        "current_value": 5,
        "last_adjust_time": 0,
        "adjust_count": 0,
    },
}

# ── 表现统计窗口 ──
AUTO_OPT_WINDOW_SHORT = 5
AUTO_OPT_WINDOW_MID = 20
AUTO_OPT_WINDOW_STOP = 10

# ── 触发阈值 ──
AUTO_OPT_STREAK_THRESHOLD = 3
AUTO_OPT_WINRATE_HIGH = 60
AUTO_OPT_WINRATE_LOW = 40
AUTO_OPT_PF_HIGH = 2.5
AUTO_OPT_PF_LOW = 1.5
AUTO_OPT_STOP_RATE_HIGH = 70
AUTO_OPT_STOP_RATE_LOW = 30

# ── 冷却期 & 回退 ──
AUTO_OPT_COOLDOWN_HOURS = 24
AUTO_OPT_ROLLBACK_TRADES = 5
AUTO_OPT_ROLLBACK_THRESHOLD = -0.5

# ── 日志 ──
AUTO_OPT_LOG_FILE = "auto_optimize_log.json"
AUTO_OPT_LOG_MAX_RECORDS = 200

# ═══════════════════════════════════════════
# v6.0 Phase 4: 知识增强版 — 6大模块常量
# ═══════════════════════════════════════════
PHASE4_ENABLED = True  # 知识增强总开关

# ── 模块A: 认知偏差防御 ──
COGNITIVE_BIAS_ENABLED = True
OVERCONFIDENCE_WIN_STREAK = 4  # 连赢次数触发过度自信检测
OVERCONFIDENCE_SHRINK_PCT = 30  # 过度自信时缩仓30%
OVERCONFIDENCE_RAISE_THRESHOLD = 10  # 过度自信时提高信号门槛10分
REVENGE_TRADING_LOSS_STREAK = 3  # 连亏次数触发报复性交易检测
REVENGE_TRADING_COOLDOWN_MIN = 60  # 报复性交易冷却期(分钟)

# ── 模块B: 期望值决策引擎 ──
EXPECTED_VALUE_ENGINE_ENABLED = True
EV_MIN_THRESHOLD = 0.3  # 期望值最低通过阈值(R)
EV_DIMENSIONS = {
    "trend_strength": 0.25,  # 趋势强度权重
    "volume_confirmation": 0.20,  # 成交量确认权重
    "market_state": 0.20,  # 市场状态权重
    "risk_reward": 0.20,  # 盈亏比质量权重
    "timing": 0.15,  # 时机把握权重
}
EV_CONFIDENCE_BOOST = 0.15  # 多维度一致时置信度加成

# ── 模块C: 反脆弱风控 ──
ANTIFRAGILE_ENABLED = True
ANTIFRAGILE_DRAWDOWN_TRIGGER = 3.0  # 回撤达3%触发反脆弱加仓
ANTIFRAGILE_MAX_ADD_PCT = 50  # 最多加仓至原仓位50%
ANTIFRAGILE_MIN_QUALITY = 70  # 反脆弱加仓最低信号质量
ANTIFRAGILE_COOLDOWN_TRADES = 3  # 反脆弱加仓后冷却交易数

# ── 模块D: 交易者状态监控 ──
TRADER_STATE_ENABLED = True
TRADER_FATIGUE_HOURS = 4  # 连续运行4小时触发疲劳检测
TRADER_DAILY_MAX_TRADES = 20  # 每日最多交易次数
TRADER_EMOTION_STREAK = 3  # 连续亏损次数触发情绪检测
TRADER_SCARCITY_THRESHOLD = 0.2  # 权益回撤20%触发稀缺心态检测

# ── 模块E: 策略进化复盘 ──
STRATEGY_REVIEW_ENABLED = True
REVIEW_MIN_TRADES = 10  # 最少交易数才触发复盘
REVIEW_QUALITY_WEIGHTS = {
    "decision_quality": 0.4,  # 决策质量权重
    "execution_quality": 0.3,  # 执行质量权重
    "result_quality": 0.3,  # 结果质量权重
}
DECISION_DIARY_FILE = "decision_diary.json"

# ── 模块F: 第二层思维过滤 ──
SECOND_LEVEL_THINKING_ENABLED = True
CONSENSUS_EXTREME_HIGH = 80  # 共识极端乐观阈值(%)
CONSENSUS_EXTREME_LOW = 20  # 共识极端悲观阈值(%)
CONSENSUS_MIN_SAMPLES = 50  # 计算共识最小样本数
SECOND_LEVEL_BARRIER_BOOST = 15  # 第二层思维时提高门槛15分

# 单板块最多同时持有/发信 3 个品种
PORTFOLIO_VAR_PCT_CAP = (
    3.3  # P1-4：组合 1日95% VaR 上限 = 权益 × 3.3%（≈2%×1.65，与线性预算对齐；超则本轮回禁新增信号）
)
PAIR_CORR_TTL = 1800  # 两两相关性缓存有效期（秒，30min）

# —— P2（2026-08-14）：涨跌停锁死头寸尾部应力加计（可配置项） ——
# 背景：P0-1 已让日亏线含浮亏 MTM，但没对「封板不可平」头寸做尾部应力缓冲。
# 当某亏损头寸因封板平不掉时，真实尾部风险比账面浮亏更大（次日可能再穿一个停板），
# 会出现「账面浮亏未到 8% 熔断线、实则已临近爆雷」的盲区。此项补上。
# 默认 OFF：精确模式需 feed 提供昨结算(pre_close)才可靠判定封板；数据源就绪后开启。
# 2026-08-14 用户确认开启：feed(minishare_live) 已存 pre_close 支撑精确模式，
# 缺 pre_close 时自动降级启发式（浮亏≥1个停板幅度才计提），半自动系统仅影响告警/熔断线、不误下单。
STRESS_LIMIT_LOCKED = True  # 总开关（精确模式优先，缺 pre_close 降级启发式）
STRESS_TRIGGER_MULT = 1.0  # 启发式门槛：浮亏/名义 ≥ 该倍数×limit_pct 才计提应力
STRESS_BUFFER_MULT = 1.0  # 计提幅度系数：应力 = limit_pct × 名义 × 此系数（即假设再穿一个停板）

# —— 交易时段 / 无夜盘品种 ——
# 日盘 09:00-15:00（全品种）；夜盘 21:00-23:00（仅“有夜盘”品种）。
# 无夜盘品种（仅日盘交易）：收盘后（尤其 21:00 起）不再推送信号。
NO_NIGHT = {
    "jd",
    "lh",
    "AP",
    "CJ",
    "PK",
    "RS",
    "PM",
    "WH",
    "JR",
    "LR",
    "CS",
    "rr",
    "lc",
    "si",
    "UR",
    "RM",
    "OI",
    "c",
}  # 与 four_dim_strategy.NO_NIGHT_DEFAULT 保持同步


def _in_session(sym, now=None):
    """该品种当前是否处于交易时段（决定是否允许推送信号）。
    G4：夜盘资格由 SYMBOLS[sym]["night"] 统一真值源决定（替代旧的 NO_NIGHT 集合）。
    周六/周日 全天休市，不推送任何信号。"""
    now = now or datetime.now()
    # 周末全天休市（周六=5，周日=6）
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    # 日盘：09:00-10:15 / 10:30-11:30 / 13:30-15:00
    day = (540 <= t <= 615) or (630 <= t <= 690) or (810 <= t <= 900)
    # 夜盘资格：SYMBOLS 注入的 night 字段（无夜盘品种=False）；未知品种默认有夜盘
    night_eligible = SYMBOLS.get(sym, {}).get("night", True)
    if not night_eligible:
        return day
    night = 1260 <= t <= 1380  # 21:00-23:00 夜盘
    return day or night


def _market_open_now(now=None):
    """全局是否处于任一交易时段（日盘 09-15 / 夜盘 21-23），用于组合级报警
    （无品种代码，如『组合风险预算接近上限』）的时段判断：完全休市
    （午休 11:30-13:30 / 15:00-21:00 / 23:00-次日09:00 / 周末）视为非交易时段。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (540 <= t <= 615) or (630 <= t <= 690) or (810 <= t <= 900) or (1260 <= t <= 1380)


def _should_suppress(sig, now=None):
    """非交易时段是否应静默该通知（不弹窗/不语音/不手机推送，仅控制台打印）。
    返回 True=静默。紧急例外（始终推送）：缺口击穿止损、配置异常拦截；
    硬熔断走 notify_killswitch 独立路径，不经此函数。"""
    now = now or datetime.now()
    at = sig.get("alert_type")
    if sig.get("force_close") or at in ("缺口击穿止损", "配置异常"):
        return False
    sym = sig.get("symbol")
    sess = _in_session(sym, now) if sym is not None else _market_open_now(now)
    return not sess


# —— 去重状态持久化（根治“进程重启/多进程 → 内存去重丢失 → 连环重发”）——
DEDUP_STATE_FILE = os.path.join(HERE, "signal_dedup_state.json")


def load_dedup_state(last_fire):
    """从磁盘载入去重状态（last_fire + _SIG_PREV_DIR），每次 evaluate 开头调用，
    使重启/多进程共享同一份去重记忆，避免重复推送。"""
    global _SIG_PREV_DIR
    try:
        with open(DEDUP_STATE_FILE) as f:
            d = json.load(f)
        last_fire.clear()
        last_fire.update(d.get("last", {}))
        _SIG_PREV_DIR.clear()
        _SIG_PREV_DIR.update(d.get("prev", {}))
    except Exception:
        pass
    # 兜底：若磁盘状态完全为空（无 last 也无 prev，如刚重置/损坏），
    # 从今日聊天流反推已发信号，避免重启后把 60min 内刚发过的信号再发一遍。
    # 注意：仅当 last_fire 与 _SIG_PREV_DIR 都为空时才重建——
    # 否则会每次轮询都从聊天流覆盖掉内存里刚积累的「上一轮方向」，
    # 破坏「同号连续 2 轮才认信号」的去抖（曾导致首轮后迟迟不触发）。
    if not last_fire and not _SIG_PREV_DIR:
        _rebuild_dedup_from_chat(last_fire)


def save_dedup_state(last_fire):
    """原子写回去重状态到磁盘。"""
    try:
        tmp = DEDUP_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last": last_fire, "prev": _SIG_PREV_DIR}, f, ensure_ascii=False, default=_json_default)
        os.replace(tmp, DEDUP_STATE_FILE)
    except Exception:
        pass


# —— 持仓触价告警去重状态持久化（根治"进程重启 → 内存去重丢失 → 连环重发"）——
POS_ALERT_DEDUP_FILE = os.path.join(HERE, "position_alert_dedup_state.json")


def load_pos_alert_dedup():
    """从磁盘载入持仓触价告警去重状态，每次 evaluate/_update_aux 开头调用。"""
    global _POS_ALERT_GUARD
    try:
        with open(POS_ALERT_DEDUP_FILE) as f:
            d = json.load(f)
        _POS_ALERT_GUARD.clear()
        for sym, levels in d.get("guards", {}).items():
            _POS_ALERT_GUARD[sym] = {}
            for kind, state in levels.items():
                if isinstance(state, dict) and "fired" in state:
                    _POS_ALERT_GUARD[sym][kind] = {"fired": bool(state["fired"]), "ts": float(state.get("ts", 0))}
                else:
                    _POS_ALERT_GUARD[sym][kind] = {"fired": bool(state), "ts": 0}
            # ★ 2026-08-27: 确保从旧文件加载时也有 gap_stop 字段
            if "gap_stop" not in _POS_ALERT_GUARD[sym]:
                _POS_ALERT_GUARD[sym]["gap_stop"] = {"fired": False, "ts": 0.0}
    except Exception:
        pass


def save_pos_alert_dedup():
    """原子写回持仓触价告警去重状态到磁盘。"""
    try:
        tmp = POS_ALERT_DEDUP_FILE + ".tmp"
        serializable = {}
        for sym, levels in _POS_ALERT_GUARD.items():
            serializable[sym] = {}
            for kind, state in levels.items():
                serializable[sym][kind] = {"fired": state.get("fired", False), "ts": state.get("ts", 0.0)}
        with open(tmp, "w") as f:
            json.dump({"guards": serializable}, f, ensure_ascii=False, default=_json_default)
        os.replace(tmp, POS_ALERT_DEDUP_FILE)
    except Exception:
        pass


PORT = 8741
STATE_FILE = os.path.join(HERE, "four_dim_live_state.json")
SIGNAL_LOG = os.path.join(HERE, "four_dim_signals.json")
HTML_FILE = os.path.join(HERE, "four_dim_live.html")
FUNDAMENTALS = os.path.join(HERE, "fundamentals.json")

# 交易时段：日盘 09:00-15:00（全品种）；夜盘 21:00-23:00（部分品种无夜盘）。
# G4：夜盘资格统一由 SYMBOLS[sym]["night"] 决定（four_dim_strategy.py 注入），
# 旧的 _NIGHTLESS 字面量已废弃（见 _in_session / NIGHT_SYMS 改为读 night 字段）。
# 止损基准 ATR：夜盘用 30 分钟 ATR（中间档，介于 5m 太细 / 日线太粗之间）
ATR_WINDOW = 14  # 30min ATR 周期上限（自适应取 min(上限, 可用K-1)）
ATR_MIN_K = 3  # 30min K 最少样本；不足则回退 日线ATR×经验比例
ATR_30M_FALLBACK = 0.40  # 样本不足时的 30min ATR 估计（≈ 日线×0.4）
ATR_FLOOR = 0.25  # 止损基准 ATR 地板（≥ 日线ATR×此值），防个别品种异常窄
# G4：有夜盘品种列表（由 SYMBOLS 的 night 字段派生，单一真值源）
NIGHT_SYMS = [s for s in SYMBOLS if SYMBOLS.get(s, {}).get("night", True)]

# 品种→交易合约映射（来自 trade_config.json 的 contract_specs.contract），供信号卡/持仓卡展示合约代码
try:
    _TCFG = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
    CONTRACT_MAP = {
        k: ml.normalize_contract_code(v.get("contract", k)) for k, v in _TCFG.get("contract_specs", {}).items()
    }
except Exception:
    CONTRACT_MAP = {}


# 2026-08-14：合约映射随动态主力解析刷新（根治写死月份导致盯市/展示价过时）。
# minishare_live._authoritative_contracts() 已按持仓量动态返回当前主力/次主力合约。
#
# ⚠️ 重要不变量（2026-08-20 确立）：
#   展示层（面板/信号卡/品种选择器）一律使用 _authoritative_contracts() 作为主力合约代码源，
#   不得直接使用 FEED.contract_of() / sym2code。因为后者会被 _pin_account_positions()
#   钉死到持仓开仓合约（用于盯市/浮盈亏计算），而非当前主力合约。
#   违反此不变量将导致：面板显示持仓合约而非主力合约 → 用户按错误合约下单。
def refresh_contract_map():
    global CONTRACT_MAP
    try:
        # 1) 用权威映射（_authoritative_contracts）更新 CONTRACT_MAP
        d = {k: ml.normalize_contract_code(v) for k, v in CONTRACT_MAP.items()}
        for k, v in ml._authoritative_contracts().items():
            d[k] = ml.normalize_contract_code(v)
        CONTRACT_MAP = d
        # 2) 一致性自检：对比 FEED.contract_of（持仓钉死源）与权威映射
        #    若有差异（说明有持仓品种被钉到非主力），记录 WARNING 供排查
        try:
            _feed0 = FEED if FEED is not None else ml.feed()
            _auth = ml._authoritative_contracts()
            _mismatches = []
            for _s in _auth:
                try:
                    _fc = _feed0.contract_of(_s) if _feed0 else None
                    _ac = _auth.get(_s)
                    if _fc and _ac and _fc != _ac:
                        _mismatches.append(f"{_s}: FEED={_fc} vs AUTH={_ac}")
                except Exception:
                    pass
            if _mismatches:
                print(
                    f"[CONTRACT_MAP WARNING] 持仓品种与主力合约不一致({len(_mismatches)}个): "
                    + ", ".join(_mismatches[:5])
                )
        except Exception:
            pass
    except Exception:
        pass


def _get_main_contract(sym):
    """获取指定品种的当前主力合约代码（优先使用 _authoritative_contracts）。"""
    if not sym:
        return None
    try:
        _auth = ml._authoritative_contracts()
        _code = _auth.get(sym)
        if _code:
            return ml.normalize_contract_code(_code)
    except Exception:
        pass
    return CONTRACT_MAP.get(sym, sym)


# —— 可选通知（从 da龘 合并，默认关闭）——
# trade_config.json 的 "notify" 键可持久化；此处为运行时全局，可被 /api/notify 改写。
def _load_notify_cfg():
    try:
        cfg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
        n = cfg.get("notify", {})
    except Exception:
        n = {}
    return {
        "enabled": bool(n.get("enabled", False)),  # 默认关
        "native": bool(n.get("native", True)),
        "voice": bool(n.get("voice", True)),
        "sound": bool(n.get("sound", False)),
    }


NOTIFY_CFG = _load_notify_cfg()

# #122 信号时效 TTL：信号生成后 valid_minutes 分钟内有效，超时视为过期（灰显 / 禁用于跟单）
# 默认 120 分钟（2 小时滚动窗口），可在 trade_config.json 顶层 "signal_ttl_minutes" 覆盖。
DEFAULT_SIGNAL_TTL_MIN = 120


def signal_ttl_minutes():
    try:
        cfg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
        v = cfg.get("signal_ttl_minutes")
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    except Exception:
        pass
    return DEFAULT_SIGNAL_TTL_MIN


# P2-C（2026-08-14 整改）：风控阈值参数化——把 STOP_COOLDOWN_SEC / PORTFOLIO_VAR_PCT_CAP 等
# 从硬编码常量改为可由 trade_config.json 顶层键覆盖（带 60s 缓存，缺省回退代码常量）。
_TC_CACHE = {"t": 0.0, "v": None}


def _load_tc():
    """读取 trade_config.json 顶层（60s 缓存），供阈值参数化。"""
    global _TC_CACHE
    _now = datetime.now().timestamp()
    if _TC_CACHE["v"] is not None and (_now - _TC_CACHE["t"]) < 60:
        return _TC_CACHE["v"]
    try:
        v = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
    except Exception:
        v = {}
    _TC_CACHE = {"t": _now, "v": v}
    return v


def _tc_num(key, default):
    """从 trade_config.json 顶层读数值型风控阈值，非数值/缺失则回退 default。"""
    try:
        v = _load_tc().get(key)
        if isinstance(v, (int, float)):
            return float(v)
    except Exception:
        pass
    return default


def annotate_signal_ttl(sig):
    """给信号补算时效状态：created_at / valid_minutes / expired / expires_at / remaining_min / age_min。
    - created_at 缺省回退 sig['time']（历史信号兼容）；
    - valid_minutes 缺省 0 → 视为“无时效信息”，不判过期（向后兼容老信号）。
    返回新 dict，不改动入参。"""
    created = (sig.get("created_at") or sig.get("time")) if isinstance(sig, dict) else None
    vmin = int(sig.get("valid_minutes") or 0) if isinstance(sig, dict) else 0
    out = dict(sig) if isinstance(sig, dict) else {}
    out["created_at"] = created
    out["valid_minutes"] = vmin
    out["expired"] = False
    out["expires_at"] = None
    out["remaining_min"] = None
    out["age_min"] = None
    if created and vmin > 0:
        try:
            ct = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            age = (now - ct).total_seconds() / 60.0
            exp = ct + timedelta(minutes=vmin)
            out["age_min"] = int(round(age))
            out["expires_at"] = exp.strftime("%Y-%m-%d %H:%M:%S")
            out["remaining_min"] = int(round((exp - now).total_seconds() / 60.0))
            out["expired"] = now > exp
        except Exception:
            pass
    return out


def sh_escape(s):
    return str(s).replace('"', "'").replace("`", "'")


def _json_default(o):
    """numpy 类型转原生，避免 json 序列化报错。"""
    if isinstance(o, bool):
        return o
    try:
        if hasattr(o, "item"):
            return o.item()
    except Exception:
        pass
    return str(o)


# ---------------------------------------------------------------------------
# 聊天式消息推送（面板内“消息”流）：把每条信号/持仓报警作为一条消息写入
# state["chat"] 并落盘，前端以“对话”气泡呈现——非阻塞、可事后回看分析。
# ---------------------------------------------------------------------------
STATE_REF = None  # main() 启动时指向活动 state 字典，供 append_chat 直接写入


def chat_feed_path(date_str=None):
    """按天落盘：signal_chat_feed_YYYY-MM-DD.jsonl。默认今天。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    # Security: validate date_str to prevent path traversal injection
    # Only allow strictly formatted YYYY-MM-DD strings, reject anything else
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_str)):
        raise ValueError(f"Invalid date_str format: {date_str!r}, expected YYYY-MM-DD")
    # Normalize and ensure resolved path stays within HERE directory
    raw_path = os.path.join(HERE, f"signal_chat_feed_{date_str}.jsonl")
    resolved = os.path.realpath(raw_path)
    if not resolved.startswith(os.path.realpath(HERE) + os.sep):
        raise ValueError(f"Path escape detected: {date_str!r}")
    return raw_path


def list_chat_days():
    """返回所有有消息的日期（降序），供复盘日历/下拉选择。"""
    days = []
    try:
        for fn in os.listdir(HERE):
            if fn.startswith("signal_chat_feed_") and fn.endswith(".jsonl"):
                d = fn[len("signal_chat_feed_") : -len(".jsonl")]
                if len(d) == 10:
                    days.append(d)
    except Exception:
        pass
    days.sort(reverse=True)
    return days


def load_chat_feed(date_str=None, limit=None):
    """读取指定日期的消息（默认今天）。limit=None 则全部返回（用于完整复盘）。"""
    out = []
    path = chat_feed_path(date_str)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    if limit:
        out = out[-limit:]
    return out


def append_chat(entry):
    """把一条通知写入聊天流（内存 state + 按天落盘），前端以对话气泡呈现，
    按消息所属日期归类到对应日文件，便于逐日复盘。"""
    keep = (
        "time",
        "kind",
        "symbol",
        "name",
        "direction",
        "lots",
        "price",
        "stop",
        "target",
        "t1",
        "t2",
        "reason",
        "alert_type",
        "alert_label",
        "signal_type",
        "atr_src",
        "contract",
        "push_suppressed",
        "hold_context",
        "action_advice",
        "advice_type",
    )
    e = {k: entry.get(k) for k in keep}
    # 2026-08-21: auto-resolve main contract code (with logging + fallback)
    # —— 主力合约代码是交易决策关键字段，缺失必须告警，不能静默吞掉
    if not e.get("contract") and e.get("symbol"):
        try:
            _sym = e["symbol"]
            _auth = ml._authoritative_contracts()
            _code = _auth.get(_sym)
            if _code:
                e["contract"] = ml.normalize_contract_code(_code)
            else:
                _fb = CONTRACT_MAP.get(_sym)
                if _fb and _fb != _sym:
                    e["contract"] = _fb
                else:
                    print(f"[append_chat] WARN: 品种 {_sym} 未解析到主力合约代码，权威映射共 {len(_auth)} 条")
        except Exception as _e:
            print(f"[append_chat] ERROR: 解析主力合约失败 {e.get('symbol')}: {repr(_e)[:120]}")
    e["time"] = e.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 按 entry 时间所属日期归类（缺省归今天）
    date_str = (e.get("time") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    try:
        with open(chat_feed_path(date_str), "a") as f:
            f.write(json.dumps(e, ensure_ascii=False, default=_json_default) + "\n")
    except Exception:
        pass
    if STATE_REF is not None:
        # 仅当为“今天”的消息才进内存实时流，避免历史日污染当前会话
        if date_str == datetime.now().strftime("%Y-%m-%d"):
            STATE_REF.setdefault("chat", []).insert(0, e)
            STATE_REF["chat"] = STATE_REF["chat"][:2000]


def _resample_30m(df_5m):
    """把 5 分钟 K 线聚合成 30 分钟 K 线（OHLCV），供 30 分钟 ATR 计算。"""
    if df_5m is None or len(df_5m) == 0:
        return None
    if not isinstance(df_5m.index, pd.DatetimeIndex):
        return None
    try:
        ohlc = (
            df_5m.resample("30min")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        return ohlc if len(ohlc) else None
    except Exception:
        return None


def _compute_stop_atr(sym, df_5m, atr_daily, now=None):
    """止损基准 ATR 计算（与信号口径一致）：
    - 日盘(09-15) & 非夜盘时段：用日线 ATR（含隔夜跳空，偏粗）
    - 夜盘(21:00-23:00)：用 30 分钟 ATR（中间档，根治 5m 的 3 点秒扫）
        · 30min K 不足（夜盘刚开盘）→ 日线ATR×0.4 经验估计
        · 最小地板：止损基准 ATR 不低于 日线ATR×FLOOR，防个别品种异常窄
    返回 (stop_atr, atr_src)。atr_daily 非法时返回 (None, None)。"""
    if atr_daily is None or pd.isna(atr_daily) or atr_daily <= 0:
        return None, None
    if now is None:
        now = datetime.now()
    stop_atr = atr_daily
    atr_src = "daily"
    if 21 <= now.hour < 23 and df_5m is not None and len(df_5m) >= 6:
        try:
            df_30m = _resample_30m(df_5m)
            n_k = len(df_30m) if df_30m is not None else 0
            if n_k >= ATR_MIN_K:
                win = min(ATR_WINDOW, n_k - 1)
                a30 = strat_atr(df_30m, win).iloc[-1]
                if a30 and not pd.isna(a30) and a30 > 0:
                    stop_atr = a30
                    atr_src = "30m"
                else:
                    stop_atr = atr_daily * ATR_30M_FALLBACK
                    atr_src = "30m_fallback"
            else:
                stop_atr = atr_daily * ATR_30M_FALLBACK
                atr_src = "30m_fallback"
        except Exception:
            stop_atr, atr_src = atr_daily, "daily"
    # 最小地板：止损基准 ATR 不低于 日线ATR×FLOOR，防个别品种异常窄
    floor = atr_daily * ATR_FLOOR
    if stop_atr < floor:
        stop_atr = floor
        atr_src = atr_src + "_floor"
    return stop_atr, atr_src


def _auto_levels(sym, direction, price):
    """开仓时自动算止损/止盈/t1/t2（30min ATR 规则）。price 为 0/None 时回退实时价。
    返回 (stop, t1, t2, atr_src, used_price, tail_enabled) 或失败元组。"""
    dir_T = 1 if direction == "多" else (-1 if direction == "空" else 0)
    if dir_T == 0 or not sym:
        return None, None, None, None, price, False
    if FEED is None:
        return None, None, None, None, price, False
    _user_price_valid = price is not None and price != 0
    used_price = price
    if _user_price_valid:
        print(f"[_auto_levels] {sym}: 使用用户价格 {price}")
    elif not used_price:
        try:
            p = FEED.price(sym)
            if p and p > 0:
                used_price = p
                print(f"[_auto_levels] {sym}: 使用实时价 {p} (用户未提供有效价格)")
        except Exception:
            used_price = None
        if not used_price:
            try:
                d = load_daily_refreshed(sym)
                if d is not None and len(d):
                    c = d["close"].iloc[-1]
                    if c and c > 0:
                        used_price = float(c)
                        print(f"[_auto_levels] {sym}: 实时价不可用, 回退日线收盘 {used_price}")
            except Exception:
                used_price = None
    if not used_price:
        return None, None, None, None, price, False
    if _user_price_valid and used_price != price:
        print(f"[_auto_levels] ⚠️⚠️ {sym}: 用户价{price} 被替换为 {used_price} — 强制还原!")
        used_price = price
    try:
        df_daily = load_daily_refreshed(sym)
        atr_daily = strat_atr(df_daily).iloc[-1] if (df_daily is not None and len(df_daily)) else None
        if atr_daily is None or pd.isna(atr_daily) or atr_daily <= 0:
            return None, None, None, None, used_price, False
        df_5m = FEED.get_5m(sym, n_bars=120)
        stop_atr, atr_src = _compute_stop_atr(sym, df_5m, atr_daily)
        if not stop_atr:
            return None, None, None, None, used_price, False
        try:
            # P-F (2026-08-14): 与主信号路径一致，用分品种 regime 阈值，避免两条 live 路径
            # regime 判错导致 tail_enabled 标记不一致（高/低波动品种尤甚）。
            rreg = fd.classify_regime(df_daily, fd.regime_params_for(sym, _STRAT_CFG, fmg.get_manager()))[0]
        except Exception:
            rreg = "波动"
        ep = exit_plan(
            sym, used_price, dir_T, stop_atr, rreg, _STRAT_CFG, fmg.get_manager(), sr_result=sra.get_cached(sym)
        )
        # 防御性校验：自动算出的止损/止盈方向绝不可错误，否则宁可失败也不落盘
        _bad = []
        if dir_T > 0:
            if ep["stop"] >= used_price:
                _bad.append(f"stop={ep['stop']}>=price={used_price}")
            if ep["t1"] <= used_price:
                _bad.append(f"t1={ep['t1']}<=price={used_price}")
            if ep["t2"] <= used_price:
                _bad.append(f"t2={ep['t2']}<=price={used_price}")
        else:
            if ep["stop"] <= used_price:
                _bad.append(f"stop={ep['stop']}<=price={used_price}")
            if ep["t1"] >= used_price:
                _bad.append(f"t1={ep['t1']}>=price={used_price}")
            if ep["t2"] >= used_price:
                _bad.append(f"t2={ep['t2']}>=price={used_price}")
        if _bad:
            print(f"   ⚠️ _auto_levels 方向校验失败 {sym} {direction}: {', '.join(_bad)}；不落盘")
            return None, None, None, None, used_price, False
        return ep["stop"], ep["t1"], ep["t2"], atr_src, used_price, ep.get("tail_enabled", False)
    except Exception:
        return None, None, None, None, used_price, False


def _sig_signature(sym, dir_T, price=None):
    """信号签名：品种 + 方向。同品种同方向在 60min 内只推一次；
    反向=真实反转，立即推送；价格微动不再当作"新信号"刷屏。"""
    return f"{sym}|{int(dir_T)}"


def _rebuild_dedup_from_chat(last_fire):
    """当磁盘 dedup 状态为空/损坏时，从今日聊天流反推 last_fire 与 _SIG_PREV_DIR。
    避免进程重启后把 60min 内刚发过的信号再发一遍（用户最反感的重复）。"""
    global _SIG_PREV_DIR
    try:
        p = chat_feed_path()
        if not os.path.exists(p):
            return
        # name -> sym 反向查找表（缓存一次）
        name_to_sym = {v.get("name", k): k for k, v in SYMBOLS.items()}
        latest = {}  # sym -> (time_str, dir_T, price)
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("kind") != "signal" or e.get("price") is None:
                continue
            t = e.get("time", "")
            if len(t) >= 10 and t[:10] != datetime.now().strftime("%Y-%m-%d"):
                continue
            sym = name_to_sym.get(e.get("name"))
            if sym is None:
                continue
            d = e.get("direction")
            dir_T = 1 if d == "多" else (-1 if d == "空" else 0)
            if dir_T == 0:
                continue
            # 保留每个品种最新一条
            cur = latest.get(sym)
            if cur is None or t > cur[0]:
                latest[sym] = (t, dir_T, float(e["price"]))
        for sym, (t, dir_T, price) in latest.items():
            try:
                t_ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                t_ts = datetime.now().timestamp()
            day = t[:10] if len(t) >= 10 else datetime.now().strftime("%Y-%m-%d")
            last_fire[sym] = {"dir": dir_T, "t": t_ts, "day": day, "sig": _sig_signature(sym, dir_T, price)}
            _SIG_PREV_DIR[sym] = dir_T
    except Exception:
        pass


def is_trading_now(now=None):
    now = now or datetime.now()
    t = now.time()
    day = dtime(9, 0) <= t <= dtime(15, 0)
    night = dtime(21, 0) <= t <= dtime(23, 0)
    return day or night


def session_label(now=None):
    now = now or datetime.now()
    t = now.time()
    if dtime(9, 0) <= t <= dtime(15, 0):
        return "日盘"
    if dtime(21, 0) <= t <= dtime(23, 0):
        return "夜盘"
    return "休市"


# ============ 账户级日亏硬限额（P2）：期货交易日基准权益 ============
# 主数据源改为「账户动态权益回撤」(含浮亏、不依赖成交记录器手动录入)，
# 交易时段划分按期货交易日(昨夜21:00~今15:00 为一交易日)。
DAY_OPEN_EQUITY = None  # 当前期货交易日开盘基准权益
DAY_OPEN_LABEL = ""  # 当前期货交易日标签(YYYY-MM-DD，15:00 后归属次日)


def _trading_day_label(now=None):
    """期货交易日标签：15:00 之后归属下一个自然日交易日。
    例：周一夜盘(21:00)+周二日盘 = '周二'交易日；周二夜盘+周三日盘 = '周三'。"""
    now = now or datetime.now()
    d = now.date()
    if now.hour >= 15:
        d = d + timedelta(days=1)
    return d.isoformat()


def ensure_F(today):
    """盘前刷新 F；失败则保持现状（pipeline 对缺失→中性）。"""
    try:
        stale = True
        if os.path.exists(FUNDAMENTALS):
            age = datetime.now().timestamp() - os.path.getmtime(FUNDAMENTALS)
            if age < 3600 * 20:  # 20h 内认为当日已刷
                stale = False
        if stale:
            print(f"[F] 刷新基本面(akshare) {today} …")
            ff.refresh(basis_start="20240101")
        else:
            print("[F] 已有当日缓存，跳过刷新")
    except Exception as e:
        print(f"[F] 刷新失败，继续(中性): {e}")


# ---------------------------------------------------------------------------
# 通知（独立：macOS 原生横幅 + 语音 + 日志）
# ---------------------------------------------------------------------------
def notify(sig, voice=True, banner=True):
    at = sig.get("alert_type")
    # —— 非交易时段门控：休市/午休/夜盘后/周末 不推送通知（仅控制台打印），
    # 避免基于价格快照的持仓触价/移动止损/规则引擎在休市误报刷屏。
    # 真正紧急的例外始终推送：缺口击穿止损（sig["force_close"]）、配置异常拦截；
    # 硬熔断走 notify_killswitch 独立路径（不经此门）。
    if _should_suppress(sig):
        print(f"   (非交易时段静默) {sig.get('name')} {sig.get('direction')} {sig.get('alert_type') or ''}")
        return
    if at == "配置异常":
        # 配置异常：用专门标题，避免"触及...位"这种触价文案误导
        line = (
            f"{sig['name']} {sig['direction']} 配置异常：持仓 {sig['lots']}手，"
            f"止损/止盈位方向需检查（实盘价 {sig.get('price') or '—'}）"
        )
    elif at:
        # 持仓触价报警（止损/止盈）
        label = sig.get("alert_label", "止损")
        level = sig.get("alert_level")
        line = (
            f"{sig['name']} {sig['direction']} {at}！持仓 {sig['lots']}手，"
            f"实盘价 {sig.get('price') or '—'} 触及你的{label}位 {level}"
        )
    else:
        line = (
            f"{sig['name']} {sig['direction']} 触发，建议 {sig['lots']} 手，"
            f"开仓 {sig.get('price') or '—'} / 止损 {sig['stop']} / "
            f"t1 {sig.get('t1') or '—'}(平半) / t2 {sig.get('target') or '—'}(全平)"
        )
    print(f"\n🔔 [四维信号 {datetime.now():%H:%M:%S}] {line}")
    print(f"   {sig['reason']}")
    # 可选通知：默认关闭（NOTIFY_CFG.enabled=False → 仅控制台，不弹窗/不语音）
    if not NOTIFY_CFG.get("enabled"):
        return
    v = voice and NOTIFY_CFG.get("voice", True)
    b = banner and NOTIFY_CFG.get("native", True)
    # 顺序：先弹横幅，再播语音。
    # 原因：macOS 通知横幅出现时会短暂压低其他音频(ducking)，若语音与横幅同时出，
    # 语音会被压没；多条信号连发时横幅一个接一个把语音全压掉。改为「横幅先到→
    # 压音窗口结束→再播报」，保证每条语音完整可闻。
    if b:
        sc = (
            f'display notification "{sh_escape(line)}" with title "四维策略信号" '
            f'subtitle "{sh_escape(sig["name"])} {sh_escape(sig["direction"])}"'
        )
        subprocess.run(["osascript", "-e", sc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if v:
        time.sleep(1.0)  # 等通知的短暂压音窗口结束，再开语音
        txt = sh_escape(line)
        # 优先中文语音 Tingting；若该语音包未下载(macOS 常用语音按需下载)，
        # 回退系统默认语音(一定已装)保证有声音；并在终端提示去下载 Tingting。
        for voice in ("Tingting", None):
            cmd = ["say"]
            if voice:
                cmd += ["-v", voice]
            cmd.append(txt)
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
                if r.returncode == 0:
                    if voice is None:
                        print(
                            "   ℹ️ Tingting 未装，已用系统默认语音播报(中文可能不准)；"
                            "建议：系统设置→辅助功能→语音→嗓音→中文(中国)→Tingting→下载"
                        )
                    break
                err = (r.stderr or b"").decode("utf-8", "replace")[:160]
                print(f"   ⚠️ say 语音[{voice or '默认'}]失败(rc={r.returncode}): {err}")
            except Exception as e:
                print(f"   ⚠️ say 语音[{voice or '默认'}]异常: {e}")
        else:
            print("   ⚠️ 所有语音均失败，无语音播报")
    # —— #15 手机推送：与横幅/语音并行，配置了 Telegram/Bark/企业微信就会推 ——
    _suppressed = sig.get("push_suppressed", False)
    if _suppressed:
        _hc = sig.get("hold_context") or {}
        _reason_key = "未知"
        if _hc.get("dedup"):
            _reason_key = "信号去重"
        elif _hc.get("whipsaw"):
            _reason_key = "Whipsaw噪声"
        elif _hc.get("rate_limited"):
            _reason_key = "频率限制"
        elif _hc.get("cooldown"):
            _reason_key = "平仓冷却期"
        elif _hc.get("conflict"):
            _reason_key = "反向冲突"
        elif _hc.get("held") and _hc.get("lots", 0) >= POSITION_SATURATION_LOTS:
            _reason_key = "仓位饱和"
        elif _hc.get("min_lots"):
            _reason_key = "手数过小"
        print(f"   📱 手机推送已抑制 [{_reason_key}]（持仓感知：{sig.get('signal_type', '?')}）")
    try:
        has_channel = (
            pn.channels_status().get("telegram")
            or pn.channels_status().get("bark")
            or pn.channels_status().get("wecom")
        )
        if not _suppressed and has_channel:
            pn.push_alert(sig)
        elif _suppressed and has_channel:
            hc = sig.get("hold_context") or {}
            title = "持仓提示"
            if hc.get("cooldown"):
                msg = f"⚠️ {sig.get('symbol', '')} {sig.get('direction', '')}信号已抑制：平仓冷却期"
            elif hc.get("held") and hc.get("conflict"):
                msg = f"⚠️ {sig.get('symbol', '')} {sig.get('direction', '')}信号已抑制：反向持仓冲突"
            elif hc.get("held"):
                msg = f"⚠️ {sig.get('symbol', '')} {sig.get('direction', '')}信号已抑制：仓位饱和"
            else:
                msg = f"⚠️ {sig.get('symbol', '')} 信号已抑制"
            pn.push(msg, title=title)
    except Exception as e:
        print(f"   ⚠️ 手机推送异常: {e}")


def notify_recover(sym, info):
    """自适应恢复通知：借用 notify 通道（横幅+语音，受 NOTIFY_CFG 开关约束）。"""
    name = SYMBOLS.get(sym, {}).get("name", sym)
    sig = {
        "name": name,
        "direction": "↻自适应恢复",
        "reason": f"近期walk-forward转正(expR={info.get('expR')}/胜{info.get('win_rate')}/n={info.get('trades')})，"
        f"自动解除屏蔽·恢复交易",
        "lots": 0,
        "price": None,
        "stop": None,
        "t1": None,
        "target": None,
    }
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)


def build_batch_orders(mode="flatten", symbol=None):
    """#8 一键全平 / 一键反手：生成可直接照着敲的委托清单。

    mode="flatten"  平掉全部（或指定品种）持仓
    mode="reverse"  平掉现有 + 反向重开（手数走 risk_gate，不是简单等量翻）
    只出「清单」不自动下单 —— 半自动系统的底线是手指最后按下去的人是你。
    """
    prices = {s: FEED.price(s) for s in SYMBOLS} if FEED else {}
    snap = at.snapshot(prices)
    rows = []
    for p in snap.get("positions", []):
        sym = p.get("symbol")
        lots = int(p.get("lots") or 0)
        if lots <= 0:
            continue
        if symbol and sym != symbol:
            continue
        px = p.get("price") or prices.get(sym)
        d = p.get("direction")
        close_side = "卖平" if d == "多" else "买平"
        item = {
            "symbol": sym,
            "name": p.get("name") or sym,
            "contract": ml.normalize_contract_code(p.get("contract") or sym),
            "step": "平仓",
            "side": close_side,
            "direction": d,
            "lots": lots,
            "ref_price": px,
            "float_pnl": p.get("float_pnl"),
            "exec": None,
        }
        try:
            item["exec"] = exp.plan_exit(sym, lots, px, d, panic=(mode == "flatten"))
        except Exception:
            pass
        rows.append(item)
        if mode == "reverse":
            new_dir = "空" if d == "多" else "多"
            new_lots = lots
            try:
                df = load_daily_refreshed(sym)
                atrd = float(strat_atr(df).iloc[-1]) if df is not None and len(df) else None
                if atrd and px:
                    rg = risk_gate(sym, px, atrd, _STRAT_CFG)
                    if rg.get("passed") and rg.get("lots"):
                        new_lots = int(rg["lots"])
                    ev_gate = ec.gate(lookahead_hours=4)
                    ev_scale = ec.scale_factor(ev_gate)
                    scale = rsm.RISK_FSM.scale()
                    dd_scale = ddg.scale_factor()  # #119 回撤水位线渐变降险
                    _combined = round(
                        min(scale, dd_scale, ev_scale), 3
                    )  # 整改：取较严者而非连乘（含#13事件闸门软减速）
                    if _combined < 1.0:
                        new_lots = max(1, int(round(new_lots * _combined)))
            except Exception:
                pass
            ritem = {
                "symbol": sym,
                "name": p.get("name") or sym,
                "contract": ml.normalize_contract_code(p.get("contract") or sym),
                "step": "反手开仓",
                "side": "买开" if new_dir == "多" else "卖开",
                "direction": new_dir,
                "lots": new_lots,
                "ref_price": px,
                "exec": None,
            }
            try:
                ritem["exec"] = exp.plan_execution(sym, new_lots, px, new_dir, urgency="fast")
            except Exception:
                pass
            rows.append(ritem)
    blocked = ""
    if mode == "reverse" and rsm.is_locked():
        blocked = "风控锁定/熔断中：反手的「开仓腿」被禁止，清单里的平仓腿照做、开仓腿先别打"
    txt = (
        "\n".join(
            f"{i + 1}. {r['name']}({r['contract']}) {r['step']} {r['side']} {r['lots']}手"
            f" @参考{r['ref_price'] if r['ref_price'] is not None else '市价'}"
            for i, r in enumerate(rows)
        )
        or "（当前无持仓）"
    )
    return {
        "ok": True,
        "mode": mode,
        "count": len(rows),
        "orders": rows,
        "text": txt,
        "blocked": blocked,
        "equity": snap.get("equity"),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def apply_batch_orders(mode="flatten", symbol=None):
    """#8 执行确认：把清单落到 account_tracker（平仓记已实现盈亏；反手再开新仓）。
    仅在用户在面板点「已执行」后调用 —— 系统不代下单，只代记账。"""
    plan = build_batch_orders(mode, symbol)
    done, errs = [], []
    for r in plan["orders"]:
        sym = r["symbol"]
        px = r.get("ref_price")
        if px is None:
            errs.append(f"{sym} 无参考价，跳过")
            continue
        try:
            if r["step"] == "平仓":
                ok, msg, _ = at.record_trade(sym, "close", r["direction"], r["lots"], px)
            else:
                if rsm.is_locked():
                    errs.append(f"{sym} 风控锁定，反手开仓腿未记账")
                    continue
                ok, msg, _ = at.record_trade(sym, "open", r["direction"], r["lots"], px)
            (done if ok else errs).append(f"{sym} {r['step']}{'' if ok else '失败:' + msg}")
        except Exception as e:
            errs.append(f"{sym} {r['step']} 异常: {repr(e)[:50]}")
    try:
        dr.log_event(
            "batch_order", state=mode, reason=f"{len(done)}腿已记账" + (f"；{len(errs)}腿异常" if errs else "")
        )
    except Exception:
        pass
    return {"ok": not errs, "mode": mode, "done": done, "errors": errs, "count": len(done)}


def notify_killswitch(ks):
    """组合级硬熔断播报（#5）：最高优先级，强制弹窗+语音（不受 no_voice 影响）。
    内容 = 触发原因 + 一键全平清单，用户照单执行即可。"""
    plan = ks.get("flatten_plan") or []
    if plan:
        legs = "，".join(f"{p['name']}{p['action']}{p['lots']}手" for p in plan[:6])
    else:
        legs = "当前无持仓，仅停止新开仓"
    line = f"账户硬熔断！{ks.get('reason', '')}。立即全平：{legs}"
    print("\n" + "=" * 64)
    print(f"🛑 [硬熔断 {datetime.now():%H:%M:%S}] {ks.get('reason', '')}")
    for p in plan:
        print(f"   → {p['name']}({p['symbol']}) {p['action']} {p['lots']} 手")
    print("   ⚠️ 已禁止全部新开仓，且不会自动恢复；平完在面板点「解除熔断」")
    print("=" * 64)
    try:
        sc = f'display notification "{sh_escape(line)}" with title "🛑 账户硬熔断" subtitle "立即全平并停止交易"'
        subprocess.run(["osascript", "-e", sc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        # 熔断是保命播报：无论 NOTIFY_CFG 如何都出声（连播两遍确保听到）
        for _ in range(2):
            subprocess.run(
                ["say", "-v", "Tingting", sh_escape(line)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
    except Exception:
        pass
    # —— #15 熔断强制推手机（最高优先级，无需在意 NOTIFY 开关）——
    try:
        pn.push(line, title="🛑 账户硬熔断")
    except Exception:
        pass


def recovery_tick():
    """周期性检查 AUTO_RECOVER_SYMBOLS 中被禁品种，近期 walk-forward 转正则自动解禁。
    返回恢复了的品种列表（用于日志）。"""
    recovered = []
    for sym in sorted(RUNTIME_DISABLED & AUTO_RECOVER_SYMBOLS):
        try:
            info = fd.recovery_check(sym)
        except Exception as e:
            print(f"[恢复检查] {sym} 异常(跳过): {repr(e)[:80]}")
            continue
        if info.get("recover"):
            RUNTIME_DISABLED.discard(sym)
            recovered.append(sym)
            print(
                f"\n🔓 [自适应恢复] {SYMBOLS.get(sym, {}).get('name', sym)} {sym} "
                f"近期walk-forward转正(expR={info.get('expR')}/胜{info.get('win_rate')}/n={info.get('trades')})"
                f"→ 自动解除屏蔽，恢复交易"
            )
            notify_recover(sym, info)
        else:
            print(f"[恢复检查] {sym} 维持屏蔽：{info.get('note')}")
    return recovered


def log_signal(sig):
    arr = []
    if os.path.exists(SIGNAL_LOG):
        try:
            arr = json.load(open(SIGNAL_LOG))
        except Exception:
            arr = []
    arr.insert(0, sig)
    json.dump(arr[:200], open(SIGNAL_LOG, "w"), ensure_ascii=False, indent=2, default=_json_default)


# ---------------------------------------------------------------------------
# 持仓触价报警（止损 / t1 平半 / t2 全平）：每轮 _update_aux 调用，复用 notify() 弹窗+语音
# ---------------------------------------------------------------------------
_POS_ALERT_GUARD = {}  # sym -> {"stop":{"fired":bool,"ts":float}, "t1":{...}, "t2":{...}, "gap_stop":{...}}
# 时间窗口+持久化去重：30min 内同 level 不重推，跨进程/重启保留记忆
# 缺口击穿告警（gap_stop）：5分钟冷却期，价格返回安全区后允许再次告警
# 用户修改止盈止损时自动重置守卫
_POS_LEVEL_GUARD = {}  # sym -> True，止损/止盈「方向配置异常」提醒每品种每轮持仓只发一次
_POS_INSURE_GUARD = {}  # sym -> True，浮盈保险拦截落盘去抖（持续期间只落一次，回安全区复位）
_SIG_PREV_DIR = {}  # sym -> 上一轮 dir_T（持续性去抖用，单轮临界抖动不触发）


def _fmt_price(v):
    if v is None:
        return "—"
    try:
        return str(round(float(v), 2))
    except (TypeError, ValueError):
        return str(v)


_ALERT_LABELS = {
    "stop": "止损",
    "t1": "t1（1R 平半）",
    "t2": "t2（2R 全平）",
}


def _fire_position_alert(p, px, kind, level):
    label = _ALERT_LABELS.get(kind, kind)
    _lots = int(p.get("lots") or 1)
    if kind == "stop":
        action_tip = "建议立即评估是否止损离场"
    elif kind == "t1":
        if _lots <= 1:
            action_tip = "建议全部止盈离场（仅1手，无法平半）"
        elif _lots == 2:
            action_tip = "建议平掉1手（持仓一半），剩余挂移动止损"
        else:
            half = _lots // 2
            action_tip = f"建议平掉{half}手（持仓一半），剩余{_lots - half}手挂移动止损"
    elif kind == "t2":
        if _lots <= 1:
            action_tip = "建议全部止盈离场"
        else:
            action_tip = f"建议全部止盈离场（共{_lots}手）"
    else:
        action_tip = "请自行判断"
    sig = {
        "name": p.get("name", p.get("symbol", "?")),
        "symbol": p.get("symbol"),
        "direction": p["direction"],
        "lots": p["lots"],
        "price": px,
        "stop": p.get("stop"),
        "target": p.get("target"),
        "t1": p.get("t1"),
        "t2": p.get("t2"),
        "alert_type": f"持仓{label}",
        "alert_label": label,
        "alert_level": _fmt_price(level),
        "reason": (
            f"你的{p.get('name', p.get('symbol', '?'))} {p['direction']} {p['lots']}手持仓，"
            f"实盘价 {_fmt_price(px)} 已触及你设的{label}位 {_fmt_price(level)}"
            f"（开仓均价 {_fmt_price(p.get('avg'))}），{action_tip}。"
        ),
    }
    # 2026-08-20: add main contract code
    if not sig.get("contract") and sig.get("symbol"):
        try:
            _auth = ml._authoritative_contracts()
            _code = _auth.get(sig["symbol"])
            if _code:
                sig["contract"] = ml.normalize_contract_code(_code)
        except Exception:
            pass
    # 记录平仓/减仓冷却期（T1减仓、T2全平都触发）
    if kind in ("t1", "t2"):
        _LAST_POS_CLOSE[p.get("symbol")] = time.time()
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
    sig["kind"] = "alert"
    append_chat(sig)
    # C4 报警历史：触价三档统一落盘，供「预警」页回看
    log_alert("触价", p.get("symbol"), sig["name"], sig["reason"], {"level_kind": kind, "level": level, "price": px})


# ── P1-3 止损冷静期 / P0-2 缺口击穿硬规则 相关 ──
STOP_COOLDOWN_SEC = 1800  # 止损后同品种禁止开仓冷静期（秒=30min；震荡市防反复止损，可调）
CLOSE_COOLDOWN_SEC = 900  # 平仓/减仓后同品种禁止开新仓冷静期（秒=15min；防刚平就追单）
ALERT_REFIRE_SEC = 1800  # 持仓触价告警重推窗口（秒=30min）：同 sym+同 level 在窗口内只推一次，防震荡刷屏
_LAST_STOP_EXIT = {}  # sym -> 最近止损离场时间戳（冷静期判定用）
_LAST_POS_CLOSE = {}  # sym -> 最近平仓/减仓时间戳（防刚平就开新仓）

# ── v6.0 市场状态缓存 ──
market_state_cache = {}  # symbol -> {state, tech_score, perf_score, ...}

# ── v6.0 Phase 3: 参数自优化全局状态 ──
# NOTE: _load_auto_opt_params() 定义在 L6062，这里先给安全默认值，main() 里再调一次
auto_opt_params = {k: dict(v) for k, v in AUTO_OPTIMIZE_PARAMS.items()}
if AUTO_OPTIMIZE_ENABLED and os.path.exists(AUTO_OPT_LOG_FILE):
    try:
        with open(AUTO_OPT_LOG_FILE, encoding="utf-8") as _f:
            _data = json.load(_f)
            if "params" in _data:
                for _k, _v in _data["params"].items():
                    if _k in auto_opt_params:
                        for _sk, _sv in _v.items():
                            auto_opt_params[_k][_sk] = _sv
    except Exception:
        pass
auto_opt_adjustment_logs = []
try:
    if os.path.exists(AUTO_OPT_LOG_FILE):
        with open(AUTO_OPT_LOG_FILE, encoding="utf-8") as _f:
            _data = json.load(_f)
            if "logs" in _data:
                auto_opt_adjustment_logs = _data["logs"]
except Exception as _e:
    print(f"[v6.0] 加载自优化日志失败: {_e}")

# ── v6.0 Phase 4: 知识增强全局状态 ──
# 模块A: 认知偏差状态
cognitive_bias_state = {
    "win_streak": 0,
    "loss_streak": 0,
    "last_trade_time": 0,
    "overconfidence_active": False,
    "revenge_cooldown_until": 0,
}
# 模块D: 交易者状态
trader_state = {
    "session_start_time": time.time(),
    "daily_trade_count": 0,
    "daily_date": time.strftime("%Y-%m-%d"),
    "emotion_streak": 0,
    "fatigue_level": 0,
    "scarcity_detected": False,
}
# 模块E: 决策日记
decision_diary = []
try:
    if os.path.exists(DECISION_DIARY_FILE):
        with open(DECISION_DIARY_FILE, encoding="utf-8") as _f:
            _dd = json.load(_f)
            decision_diary = _dd.get("entries", [])
except Exception:
    pass
# 模块F: 共识状态
consensus_state = {
    "extreme_high": False,
    "extreme_low": False,
    "consensus_score": 50,
    "last_update": 0,
}

_POS_DIRECTION_HISTORY = {}  # sym -> [(direction, timestamp), ...] 记录最近方向变化（防来回打脸）
_SIGNAL_PUSH_LOG = {}  # sym -> {"timestamps": [], "daily_count": date->int} 信号推送频率控制
_LAST_POSITION_SNAPSHOT = {}  # sym -> last known position state (防过期持仓警报)

# 持仓饱和阈值：已有持仓占账户权益比例超此值时，新同向信号降级为"持仓管理"而非"交易信号"
POSITION_SATURATION_VAPCT = 0.3  # 单个品种风险占权益上限
POSITION_SATURATION_LOTS = 5  # 单个品种持仓手数上限（超此值新同向信号抑制）

# 🔒 新增：信号推送保护阈值
WHIPSAW_WINDOW_SEC = 600  # 同一品种方向反转检测窗口（10分钟内反向=噪声）
MAX_SIGNALS_PER_HOUR = 3  # 单品种每小时最大信号数（防轰炸）
MAX_SIGNALS_PER_DAY = 12  # 单品种每日最大信号数（防疲劳推送）
SIGNAL_DEDUP_WINDOW_SEC = 180  # 同向信号去重窗口（3分钟内同向重复=去重）
SIGNAL_CROSS_DIR_LOCK_SEC = 1800  # 跨方向信号锁定窗口（30分钟内同品种不推反向信号）
MIN_SUGGESTED_LOTS = 1  # 最小建议手数（低于此值不推信号）

# 跨方向信号锁定记录：sym -> {direction, timestamp}
_SIGNAL_DIR_LOCK = {}  # 记录每个品种最近一次推送的信号方向和时间


def _check_gap_stop_triggered(ds, px, stop, entry_price):
    """[DEPRECATED] 请使用 gap_stop_utils.check_gap_stop_triggered
    保留此包装函数以兼容旧代码，实际逻辑已迁移到 gap_stop_utils 模块。"""
    from gap_stop_utils import check_gap_stop_triggered

    return check_gap_stop_triggered(ds, px, stop, entry_price)


def _fire_gap_stop_alert(p, px, stop, pen, oneR):
    """P0-2：缺口/滑点击穿止损的硬规则告警。半自动系统不代下单，改发最高优先级
    "必须立即市价平仓"告警，并明确提示：若开盘封同向停板平不掉，须承认敞口仍在并
    挂停板价排队，不可假装已离场（risk-manager 校验结论）。"""
    sym = p.get("symbol")
    name = p.get("name", sym)
    text = (
        f"⚠️ 缺口/滑点击穿止损：{name} {p['direction']} {p['lots']}手，"
        f"实盘价 {_fmt_price(px)} 已穿破止损位 {_fmt_price(stop)} 达 {pen:.1f}点"
        f"（>{0.5 * oneR:.1f}点≈0.5R），疑似跳空/滑点击穿而非干净触价。"
        f"半自动系统无法代下单——请立即市价平仓；若开盘封同向停板平不掉，"
        f"须承认敞口仍在并挂停板价排队，不可假装已离场。"
    )
    sig = {
        "name": name,
        "direction": p["direction"],
        "lots": p["lots"],
        "price": px,
        "symbol": sym,
        "stop": stop,
        "target": None,
        "t1": None,
        "t2": None,
        "alert_type": "缺口击穿止损",
        "alert_label": "必须平仓",
        "alert_level": "紧急",
        "reason": text,
        "force_close": True,
    }
    # 2026-08-20: add main contract code
    if not sig.get("contract") and sig.get("symbol"):
        try:
            _auth = ml._authoritative_contracts()
            _code = _auth.get(sig["symbol"])
            if _code:
                sig["contract"] = ml.normalize_contract_code(_code)
        except Exception:
            pass
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
    sig["kind"] = "alert"
    append_chat(sig)
    try:
        log_alert("缺口击穿止损", sym, name, text, {"pen": round(pen, 2), "oneR": round(oneR, 2)})
    except Exception:
        pass


def _levels_sane(p):
    """校验持仓 stop/t1/t2 是否落在方向正确的一侧（防配置错位造成误报）。
    返回 {"stop": True/False/None, "t1": ..., "t2": ...}：
      True  = 方向正确；False = 方向有误；None = 无法校验（avg 缺失/为 0、方向未知或该档未设）。
    规则：空单 stop 应 > avg、t1/t2 应 < avg；多单 stop 应 < avg、t1/t2 应 > avg。"""
    res = {"stop": None, "t1": None, "t2": None}
    avg = p.get("avg")
    try:
        avg = float(avg)
    except (TypeError, ValueError):
        avg = None  # 非数值 avg（如字符串脏数据）→ 无法校验，不拦截
    ds = 1 if p.get("direction") == "多" else (-1 if p.get("direction") == "空" else 0)
    if not avg or ds == 0:
        return res
    for k in ("stop", "t1", "t2"):
        v = p.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if ds > 0:  # 多：止损在均价下方，止盈在上方
            res[k] = (v < avg) if k == "stop" else (v > avg)
        else:  # 空：止损在均价上方，止盈在下方
            res[k] = (v > avg) if k == "stop" else (v < avg)
    return res


def check_position_alerts(positions):
    """遍历持仓，检测实盘价是否触及止损/t1/t2；触发则弹窗+语音。带防重复守卫。
    内含方向一致性校验：止损/止盈位落在错误一侧（或浮盈下「触止损」）一律拦截，
    配置异常每品种只提醒一次，并尝试用 _auto_levels 自动重算修正。"""
    # D1：平仓后 account_tracker 直接 del 持仓，snapshot 不再含该 sym，
    # 「无 lots 复位」分支执行不到 → 按当前持仓集合差集清理所有守卫残留，
    # 保证平仓重开后「配置异常」提醒与止损弹窗不被旧守卫吞掉。
    # ── v6.0 新增：分级止盈状态机初始化 + 检查 ──
    for _tp_init in positions:
        if not _tp_init.get("lots"):
            continue
        if "tp_level" not in _tp_init:
            _tp_init["tp_level"] = TAKE_PROFIT_LEVEL_NONE
            _tp_init["tp_targets"] = None
            _tp_init["trailing_stop"] = None
            _tp_init["tp_history"] = []
        if _tp_init.get("tp_targets") is None:
            try:
                _dir = "long" if _tp_init.get("direction") in ("多", "long") else "short"
                # ATR 缺失时从 stop 反推 stop_dist（保留旧持仓风险等级时无 atr）
                _atr_val = _tp_init.get("atr", _tp_init.get("stop_dist", 0))
                if (not _atr_val or float(_atr_val) <= 0) and _tp_init.get("stop") and _tp_init.get("avg"):
                    _atr_val = abs(float(_tp_init["stop"]) - float(_tp_init["avg"]))
                # 兜底：全无风险数据时用 2% ATR 替代，保证 tp_targets 可计算
                if (not _atr_val or float(_atr_val) <= 0) and _tp_init.get("avg"):
                    _atr_val = float(_tp_init["avg"]) * 0.02  # 2% 作为默认 ATR
                    # 同步补 stop 值，使后续推送/显示都能用上
                    if not _tp_init.get("stop"):
                        _avg_v = float(_tp_init["avg"])
                        _stop_v = round(_avg_v - _atr_val, 2) if _dir == "long" else round(_avg_v + _atr_val, 2)
                        _tp_init["stop"] = _stop_v
                        # 回写持久化
                        try:
                            import account_tracker as _at_mod

                            _st_tmp = _at_mod.load_state()
                            _st_tmp["positions"][_tp_init.get("symbol", "")] = {
                                **_st_tmp["positions"].get(_tp_init.get("symbol", ""), {}),
                                "stop": _stop_v,
                            }
                            _at_mod.save_state(_st_tmp)
                        except Exception:
                            pass
                if _atr_val and float(_atr_val) > 0:
                    _entry = float(_tp_init.get("avg") or _tp_init.get("price", 0))
                    if _entry > 0:
                        _tp_init["tp_targets"] = calc_take_profit_targets(
                            _entry,
                            float(_atr_val),
                            _dir,
                            market_state_cache.get(_tp_init.get("symbol", ""), {}).get("state"),
                        )
                        _tp_init["init_qty"] = int(_tp_init.get("lots", 0))
            except Exception:
                pass
    # ── v5 新增：时间止损检查（持仓超过5天未达T1 → 建议减仓）──
    _now_ts = time.time()
    for _tp in positions:
        if not _tp.get("lots"):
            continue
        _ot = _tp.get("open_time", "")
        if _ot and isinstance(_ot, str):
            try:
                _ot_ts = datetime.strptime(_ot, "%Y-%m-%d %H:%M:%S").timestamp()
                _days_held = (_now_ts - _ot_ts) / 86400.0
                if _days_held >= get_effective_param("time_stop_days", TIME_STOP_MAX_DAYS):
                    _t1_hit = _tp.get("t1_hit", False) or _tp.get("t1_state", "") == "hit"
                    if not _t1_hit:
                        log_alert(
                            "时间止损检查",
                            _tp.get("sym"),
                            "持仓",
                            f"{_tp.get('sym')} 已持仓{_days_held:.1f}天未达T1，建议减仓或止损",
                            {"days_held": round(_days_held, 1), "lots": _tp.get("lots", 0)},
                        )
            except Exception:
                pass

    # ── v6.0 新增：分级止盈状态机检查 ──
    for _tp in positions:
        if not _tp.get("lots") or not _tp.get("tp_targets"):
            continue
        _sym = _tp.get("symbol", "")
        _dir_str = _tp.get("direction", "多")
        _dir = "long" if _dir_str in ("多", "long") else "short"
        _px = _tp.get("price")
        if _px is None:
            continue
        try:
            _atr_v = float(_tp.get("atr", _tp.get("stop_dist", 0)) or 0)
        except (TypeError, ValueError):
            _atr_v = 0
        if _atr_v <= 0:
            continue
        _pi = {
            "entry_price": float(_tp.get("avg") or _tp.get("price", 0)),
            "direction": _dir,
            "tp_level": _tp.get("tp_level", TAKE_PROFIT_LEVEL_NONE),
            "tp_targets": _tp.get("tp_targets", {}),
            "current_qty": int(_tp.get("lots", 0)),
            "init_qty": _tp.get("init_qty", int(_tp.get("lots", 0))),
            "trailing_stop": _tp.get("trailing_stop", 0),
        }
        _tp_result = update_take_profit_level(_pi, float(_px), _atr_v)
        if _tp_result["action"] != "none":
            _tp["tp_level"] = _tp_result["new_level"]
            if _tp_result["new_stop_price"] is not None:
                _tp["stop"] = _tp_result["new_stop_price"]
            if _tp_result["trailing_stop"] is not None:
                _tp["trailing_stop"] = _tp_result["trailing_stop"]
            if _tp_result["action"] in ("reduce_t1", "reduce_t2"):
                _tp["t1_hit"] = True
                _tp["t1_state"] = "hit" if _tp_result["action"] == "reduce_t1" else "t2_hit"
            _action_labels = {
                "reduce_t1": "T1减仓",
                "reduce_t2": "T2减仓",
                "close_t1_full": "T1全平",
                "close_t2_full": "T2全平",
                "close_t3": "T3止盈平仓",
            }
            log_alert(
                "分级止盈",
                _sym,
                _dir_str,
                f"{_sym} {_action_labels.get(_tp_result['action'], _tp_result['action'])} "
                f"@{_px}, 减仓{_tp_result['reduce_qty']}手, "
                f"止损{'上移' if _tp_result['new_stop_price'] else '不变'}",
                {
                    "tp_level": _tp_result["new_level"],
                    "action": _tp_result["action"],
                    "reduce_qty": _tp_result["reduce_qty"],
                },
            )
            # ★ 2026-08-28: TP state machine fires alerts AND sets guard
            #   This connects the TP state machine with the alert guard:
            #   when a TP level transition is detected, fire the alert ONCE
            #   and mark the guard as fired to prevent duplicate raw-price alerts below.
            _tp_g = _POS_ALERT_GUARD.setdefault(
                _sym,
                {
                    "stop": {"fired": False, "ts": 0.0},
                    "t1": {"fired": False, "ts": 0.0},
                    "t2": {"fired": False, "ts": 0.0},
                    "gap_stop": {"fired": False, "ts": 0.0},
                },
            )
            _tp_now = time.time()
            _tp_px = float(_tp.get("price") or _px)
            _tp_t1v = _tp.get("t1")
            _tp_t2v = _tp.get("t2")
            _tp_act = _tp_result["action"]
            if _tp_act in ("reduce_t1", "close_t1_full"):
                _tp_g["t1"] = {"fired": True, "ts": _tp_now}
                _fire_position_alert(_tp, _tp_px, "t1", _tp_t1v)
                save_pos_alert_dedup()
            elif _tp_act in ("reduce_t2", "close_t2_full"):
                _tp_g["t2"] = {"fired": True, "ts": _tp_now}
                _fire_position_alert(_tp, _tp_px, "t2", _tp_t2v)
                save_pos_alert_dedup()
            elif _tp_act == "close_t3":
                _tp_g["t2"] = {"fired": True, "ts": _tp_now}
                _fire_position_alert(_tp, _tp_px, "t2", _tp_t2v)
                save_pos_alert_dedup()

    cur_syms = {p.get("symbol") for p in positions if p.get("lots")}
    for guard in (_POS_ALERT_GUARD, _POS_LEVEL_GUARD, _POS_INSURE_GUARD):
        for old in list(guard.keys()):
            if old not in cur_syms:
                guard.pop(old, None)
    for p in positions:
        sym = p.get("symbol")
        if not p.get("lots"):
            _POS_ALERT_GUARD.pop(sym, None)
            _POS_LEVEL_GUARD.pop(sym, None)
            _POS_INSURE_GUARD.pop(sym, None)
            continue
        stop = p.get("stop")
        target = p.get("target")
        t1 = p.get("t1")
        t2 = p.get("t2")
        if stop is None and t1 is None and t2 is None:
            continue
        px = p.get("price")
        if px is None:
            continue
        ds = 1 if p["direction"] == "多" else (-1 if p["direction"] == "空" else 0)
        if ds == 0:
            continue
        # D3：avg 统一转 float（非数值脏数据如 "abc" → None），供浮盈保险/缺口击穿
        # 等所有数值比较使用，避免 str-float 运算抛 TypeError 中断当轮剩余品种。
        avg = p.get("avg")
        try:
            avg_f = float(avg)
        except (TypeError, ValueError):
            avg_f = None
        g = _POS_ALERT_GUARD.setdefault(
            sym,
            {
                "stop": {"fired": False, "ts": 0.0},
                "t1": {"fired": False, "ts": 0.0},
                "t2": {"fired": False, "ts": 0.0},
                "gap_stop": {"fired": False, "ts": 0.0},  # ★ 缺口击穿去重守卫
            },
        )
        # ★ 2026-08-27: 确保已有守卫（从磁盘加载的旧版本）也包含 gap_stop 字段
        #   否则 get("gap_stop", default) 每次都返回默认值，去重机制完全失效
        if "gap_stop" not in g:
            g["gap_stop"] = {"fired": False, "ts": 0.0}
        # ── 方向一致性校验：stop/t1/t2 落在错误一侧 → 尝试自动重算修正并落盘；
        # 修正成功后静默（不弹窗），修正失败或无法重算才弹窗通知用户。──
        sane = _levels_sane(p)
        bad = [k for k in ("stop", "t1", "t2") if sane[k] is False]
        if bad:
            fix_note = ""
            try:
                res = _auto_levels(sym, p["direction"], p.get("avg"))
                vals = list(res) + [None] * (6 - len(res))  # 兼容失败 5 元组 / 成功 6 元组
                nstop, nt1, nt2 = vals[0], vals[1], vals[2]
                if nstop is not None and nt1 is not None and nt2 is not None:
                    for k in list(bad):
                        nv = {"stop": nstop, "t1": nt1, "t2": nt2}[k]
                        p[k] = nv
                        bad.remove(k)
                        fix_note += f"；{_ALERT_LABELS[k]}已自动修正为 {_fmt_price(nv)}"
                    stop = p.get("stop")
                    t1 = p.get("t1")
                    t2 = p.get("t2")
                    # 持久化到 account_state，避免下次轮询再次命中同一错误
                    try:
                        at.set_levels(sym, stop=stop, t1=t1, t2=t2)
                        # ★ 2026-08-27: 自动修正价位后重置告警守卫，防止旧价位告警
                        _POS_ALERT_GUARD.pop(sym, None)
                        save_pos_alert_dedup()
                    except Exception:
                        pass
            except Exception:
                pass
            if bad:
                # 自动修正失败：必须通知人工检查
                if sym not in _POS_LEVEL_GUARD:
                    _POS_LEVEL_GUARD[sym] = True
                    name = p.get("name", sym)
                    dir_tip = "空单止损应在开仓价上方、止盈在下方" if ds < 0 else "多单止损应在开仓价下方、止盈在上方"
                    text = (
                        f"⚠️ 配置异常：{name} {p['direction']} {p['lots']}手持仓的"
                        f"{'/'.join(bad) if bad else '止损/止盈'}位方向有误（{dir_tip}；"
                        f"开仓均价 {_fmt_price(p.get('avg'))}），已拦截误报{fix_note}。"
                        "仍有档位方向异常未能自动修正，请人工检查持仓配置。"
                    )
                    sig = {
                        "name": name,
                        "direction": p["direction"],
                        "lots": p["lots"],
                        "price": px,
                        "stop": p.get("stop"),
                        "target": p.get("target"),
                        "t1": p.get("t1"),
                        "t2": p.get("t2"),
                        "alert_type": "配置异常",
                        "alert_label": "方向校验",
                        "alert_level": "—",
                        "reason": text,
                    }
                    # 2026-08-20: add main contract code
                    if not sig.get("contract") and sig.get("symbol"):
                        try:
                            _auth = ml._authoritative_contracts()
                            _code = _auth.get(sig["symbol"])
                            if _code:
                                sig["contract"] = ml.normalize_contract_code(_code)
                        except Exception:
                            pass
                    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
                    sig["kind"] = "alert"
                    append_chat(sig)
                    try:
                        log_alert("方向校验", sym, name, text, {"avg": p.get("avg"), "fixed": fix_note or "无"})
                    except Exception:
                        pass
            else:
                # 已自动修正：仅落盘日志，不弹窗打扰用户
                try:
                    log_alert(
                        "方向校验自动修正",
                        sym,
                        p.get("name", sym),
                        f"检测到持仓止损/止盈方向错误{fix_note}，已静默修正并落盘",
                        {"avg": p.get("avg"), "fixed": fix_note},
                    )
                except Exception:
                    pass
        stop_hit = (
            (ds > 0 and px <= stop) or (ds < 0 and px >= stop) if stop is not None and "stop" not in bad else False
        )
        t1_hit = (ds > 0 and px >= t1) or (ds < 0 and px <= t1) if t1 is not None and "t1" not in bad else False
        t2_hit = (ds > 0 and px >= t2) or (ds < 0 and px <= t2) if t2 is not None and "t2" not in bad else False
        # ── 保险：浮盈状态下绝不报止损（空单 px<avg / 多单 px>avg 时 stop_hit 视为异常）──
        insured = bool(stop_hit and avg_f and ((ds < 0 and px < avg_f) or (ds > 0 and px > avg_f)))
        if insured:
            stop_hit = False
            # D2：去抖——条件持续期间只落盘一次，回安全区复位后允许再次落盘，
            # 防每个轮询周期刷屏 alert_history.json
            if not _POS_INSURE_GUARD.get(sym):
                _POS_INSURE_GUARD[sym] = True
                try:
                    log_alert(
                        "方向校验拦截",
                        sym,
                        p.get("name", sym),
                        f"浮盈状态下触发止损信号，判定为配置/数据异常已静默拦截"
                        f"（方向={p['direction']} avg={_fmt_price(avg)} "
                        f"px={_fmt_price(px)} stop={_fmt_price(stop)}）",
                        {"direction": p["direction"], "avg": avg, "px": px, "stop": stop},
                    )
                except Exception:
                    pass
        else:
            _POS_INSURE_GUARD.pop(sym, None)
        # 去重复位（收紧）：仅当价格明显远离该 level（>0.5R）才复位 fired，防震荡反复触发
        # 单纯 not hit 不复位——仍在警戒区就保持 fired 锁，防止小幅反复触发刷屏
        # ★ 2026-08-28: For TP-managed positions (tp_targets exists), DON'T reset
        #   the guard based on price movement alone. The TP state machine manages
        #   transitions, and guard reset only happens when user modifies levels
        #   or executes a trade. This prevents whip-saw re-firing.
        now_ts = time.time()
        _has_tp = bool(p.get("tp_targets"))
        if stop is not None and not stop_hit:
            stop_oneR = abs(avg_f - stop) if avg_f is not None else 0.0
            if stop_oneR > 0 and abs(px - stop) > 0.5 * stop_oneR:
                g["stop"] = {"fired": False, "ts": 0.0}
        if t1 is not None and not t1_hit:
            t1_oneR = abs(avg_f - t1) if avg_f is not None else 0.0
            # For TP-managed positions, use 1.5R threshold (3x more conservative)
            # This prevents whip-saw re-firing around T1
            _reset_threshold = 0.5 if not _has_tp else 1.5
            if t1_oneR > 0 and abs(px - t1) > _reset_threshold * t1_oneR:
                g["t1"] = {"fired": False, "ts": 0.0}
        if t2 is not None and not t2_hit:
            t2_oneR = abs(avg_f - t2) if avg_f is not None else 0.0
            _reset_threshold2 = 0.5 if not _has_tp else 1.5
            if t2_oneR > 0 and abs(px - t2) > _reset_threshold2 * t2_oneR:
                g["t2"] = {"fired": False, "ts": 0.0}
        # ★ 2026-08-27: 缺口击穿去重策略——彻底修复反复推送问题
        #   之前的"pen < 0.2R 自动复位"逻辑存在竞态：minishare 快照价精度不足，
        #   每轮轮询计算的 pen 值随机波动，导致守卫被反复复位 → 冷却期失效 → 反复推送。
        #
        #   新策略：gap_stop 采用"锁死式去重"——
        #   1. 一旦击穿告警推送，30 分钟内（或直到止损价被修改前）绝不重复推送
        #   2. 不再使用 pen < 0.2R 自动复位（避免竞态）
        #   3. 仅在以下情况复位：用户手动修改止损价 / 品种平仓 / 服务器启动后首次检测
        # 优先报止损，再 t2（全平），最后 t1（平半）——同一轮若多个同时触发，最紧急的先报
        stop_alerted = g.get("stop", {"fired": False, "ts": 0.0})
        t2_alerted = g.get("t2", {"fired": False, "ts": 0.0})
        t1_alerted = g.get("t1", {"fired": False, "ts": 0.0})

        # 止损：★ 一次性告警机制 —— 同 t1/t2
        if stop_hit:
            can_fire = not stop_alerted.get("fired", False)
            if can_fire:
                g["stop"] = {"fired": True, "ts": now_ts}
                _fire_position_alert(p, px, "stop", stop)
                # P1-3：记录止损离场时刻，触发同品种冷静期（防震荡市反复止损）
                _LAST_STOP_EXIT[sym] = time.time()
        else:
            if stop_alerted.get("fired", False):
                g["stop"] = {"fired": False, "ts": 0.0}
            # P0-2：缺口/滑点击穿硬规则——穿破>0.5R 视为跳空/滑点击穿，发最高优先级平仓告警
            # ★ 去重改为 30 分钟锁死式：一次推送后 30 分钟内绝不重复，无论价格如何波动
            #   不再使用 pen 阈值自动复位，彻底杜绝反复推送竞态
            # ★ 2026-08-28: 增加方向检查——价格必须在不利方向才触发（多单price<stop/空单price>stop）
            #   修复价格在有利方向时错误触发"缺口击穿"的假阳性告警
            # ★ 2026-08-28: 核心逻辑提取为 _check_gap_stop_triggered 纯函数，便于单元测试
            _gap_result = _check_gap_stop_triggered(ds, px, stop, avg_f)
            if _gap_result["triggered"]:
                oneR = _gap_result["oneR"]
                pen = _gap_result["pen"]
                _gap_alerted = g.get("gap_stop", {"fired": False, "ts": 0.0})
                _gap_cooldown = 1800  # 30 分钟冷却期
                if not _gap_alerted.get("fired", False) or (now_ts - _gap_alerted.get("ts", 0)) > _gap_cooldown:
                    g["gap_stop"] = {"fired": True, "ts": now_ts}
                    _fire_gap_stop_alert(p, px, stop, pen, oneR)

        # ★ 2026-08-28: Check if TP state machine manages this position
        _has_tp_targets = bool(p.get("tp_targets"))
        # t2（全平）：★ 一次性告警机制 —— 同 t1
        # ★ 2026-08-28: Skip raw alert for positions with tp_targets where
        #   the TP state machine already fired the alert.
        _tp_t2_handled = bool(p.get("t2_hit", False)) or (
            p.get("tp_level", "") in (TAKE_PROFIT_LEVEL_T2, TAKE_PROFIT_LEVEL_T3, TAKE_PROFIT_LEVEL_DONE)
        )
        if t2_hit:
            can_fire = not t2_alerted.get("fired", False)
            if _has_tp_targets and _tp_t2_handled:
                can_fire = False
            if can_fire:
                g["t2"] = {"fired": True, "ts": now_ts}
                _fire_position_alert(p, px, "t2", t2)
        else:
            if not (_has_tp_targets and _tp_t2_handled):
                if t2_alerted.get("fired", False):
                    g["t2"] = {"fired": False, "ts": 0.0}

        # t1（平半）：★ 一次性告警机制 —— 推过一次后，价格仍在警戒区就不再推。
        # 只有当价格离开警戒区（t1_hit=False）后才允许下次重新触发。
        # ★ 2026-08-28: Skip raw alert for positions with tp_targets where
        #   the TP state machine already fired the alert (t1_hit flag or guard fired).
        _tp_t1_handled = bool(p.get("t1_hit", False)) or (p.get("tp_level", "") not in (TAKE_PROFIT_LEVEL_NONE, ""))
        if t1_hit:
            can_fire = not t1_alerted.get("fired", False)
            # Don't re-fire if TP state machine already handled this transition
            if _has_tp_targets and _tp_t1_handled:
                can_fire = False
            if can_fire:
                g["t1"] = {"fired": True, "ts": now_ts}
                _fire_position_alert(p, px, "t1", t1)
        else:
            # 价格离开警戒区，允许下次重新触发
            # But if TP state machine handled it, keep the guard locked
            if not (_has_tp_targets and _tp_t1_handled):
                if t1_alerted.get("fired", False):
                    g["t1"] = {"fired": False, "ts": 0.0}


# ---------------------------------------------------------------------------
# 第二批能力：组合相关性矩阵 / 价差套利监控 / 情景压力测试 / 移动止损自动
# ---------------------------------------------------------------------------

# 价差监控预设对（均为 SYMBOLS 内品种；load_daily_refreshed 可逐合约/主连取日线）
SPREAD_PAIRS = [
    ("FG", "SA", "玻璃-纯碱 价差"),
    ("JM", "J", "焦煤-焦炭 价差"),
]

# 情景压力测试（adverse = 对持仓不利方向的变动幅度，正数）：
#   多头不利 = 价格下跌 adverse；空头不利 = 价格上涨 adverse
STRESS_SCENARIOS = [
    ("系统性急跌 -3%", 0.03, "all"),
    ("系统性急跌 -5%", 0.05, "all"),
    ("黑色板块急跌 -4%", 0.04, "black"),
    ("农产品急跌 -3%", 0.03, "ag"),
    ("单品极端(跌停 -4%)", 0.04, "single"),
]
_BLACK = {"FG", "SA", "JM", "J", "rb", "i", "hc", "SF", "SM", "v", "pp", "l", "pg", "eg", "bu", "FU", "ZC"}
_AG = {"OI", "CF", "AP", "lh", "jd", "SR", "c", "a", "m", "RM", "Y", "P", "PK", "CJ", "rs", "wh"}


def correlation_matrix():
    """组合相关性矩阵：基于持仓品种的日收益率相关系数（inner join 对齐交易日）。"""
    st = at.load_state()
    syms = [s for s, p in st["positions"].items() if p.get("lots")]
    if len(syms) < 2:
        return {"ok": False, "reason": "持仓不足 2 个，无法计算相关性", "labels": [], "names": {}, "matrix": []}
    rets = {}
    for sym in syms:
        try:
            df = load_daily_refreshed(sym)
            if df is None or len(df) < 30:
                continue
            r = df["close"].pct_change().dropna().rename(sym)
            if len(r) >= 20:
                rets[sym] = r
        except Exception:
            continue
    if len(rets) < 2:
        return {
            "ok": False,
            "reason": "有效日线不足 2 个品种",
            "labels": list(rets.keys()),
            "names": {s: SYMBOLS.get(s, {}).get("name", s) for s in rets},
            "matrix": [],
        }
    aligned = pd.concat(rets, axis=1).dropna()
    if isinstance(aligned.columns, pd.MultiIndex):
        aligned.columns = aligned.columns.get_level_values(0)
    if len(aligned) < 20:
        return {
            "ok": False,
            "reason": "对齐后样本不足",
            "labels": list(aligned.columns),
            "names": {s: SYMBOLS.get(s, {}).get("name", s) for s in aligned.columns},
            "matrix": [],
        }
    corr = aligned.corr()
    labels = list(corr.columns)
    names = {s: SYMBOLS.get(s, {}).get("name", s) for s in labels}
    matrix = [[round(float(corr.iloc[i, j]), 2) for j in range(len(labels))] for i in range(len(labels))]
    return {
        "ok": True,
        "labels": labels,
        "names": names,
        "matrix": matrix,
        "note": f"基于 {len(aligned)} 个对齐交易日的日收益率相关系数",
    }


# ----------------------------------------------------------------------------
# #123 组合 VaR / CVaR：参数法（正态、均值=0）1 日在险价值 + 期望短缺(ES)
# 与 #92 相关性矩阵同源（持仓日收益率协方差），但与「计划风险R(基于止损)」互补：
# 此处是市值暴露的逐日盯市 VaR，回答「一天内组合最多可能亏多少」。
# ----------------------------------------------------------------------------
_VAR_CACHE = {"t": 0.0, "v": None}
# ── ①②（2026-08-16）VaR 升级：250 日历史模拟 + EVT 尾部压力 + 协方差/收益率缓存 ──
# ② 两层数据缓存：逐品种日收益率（_VAR_RETS_CACHE）+ 对齐矩阵/协方差（_VAR_DATA_CACHE，
# 按品种集合+窗口做 key）；candidate_combined_var_pct 高频逐仓调用时复用，不再每次全量重算。
_VAR_RETS_CACHE = {}
_VAR_DATA_CACHE = {"t": 0.0, "key": None, "aligned": None, "cov": None, "valid": []}


def _var_cfg():
    """①② VaR 配置：trade_config.json 顶层键覆盖，缺省回退代码常量。"""
    try:
        _m = str((_load_tc() or {}).get("var_method", "hist") or "hist").lower()
    except Exception:
        _m = "hist"
    return {
        "method": _m,  # hist | param（回退开关）
        "window": int(_tc_num("var_hist_window", 250)),  # 历史模拟窗口（交易日）
        "min_samples": int(_tc_num("var_hist_min_samples", 120)),  # 对齐样本不足则回退参数法
        "evt": bool(_tc_num("var_evt_enabled", 1)),  # EVT 尾部压力测试开关
        "evt_q": float(_tc_num("var_evt_threshold_q", 0.90)),  # GPD 阈值分位
        "cache_sec": float(_tc_num("var_data_cache_sec", 300)),  # ② 数据缓存 TTL（秒）
    }


def _var_symbol_returns(sym, ttl):
    """② 逐品种日收益率（TTL 缓存；底层 load_daily_refreshed 自带 30min 日线缓存）。"""
    _now = datetime.now().timestamp()
    _ent = _VAR_RETS_CACHE.get(sym)
    if _ent is not None and (_now - _ent["t"]) < ttl:
        return _ent["r"]
    try:
        df = load_daily_refreshed(sym)
        if df is None or len(df) < 30:
            return None
        r = df["close"].pct_change().dropna()
        if len(r) < 20:
            return None
    except Exception:
        return None
    _VAR_RETS_CACHE[sym] = {"t": _now, "r": r}
    return r


def _var_aligned_cov(symbols, window, ttl):
    """② 对齐收益率矩阵（尾部 window 日）+ 协方差；同品种集合+窗口在 TTL 内直接复用。
    返回 (aligned, cov, valid_syms)；无有效数据返回 (None, None, [])。"""
    global _VAR_DATA_CACHE
    _now = datetime.now().timestamp()
    key = (tuple(sorted(symbols)), int(window))
    _c = _VAR_DATA_CACHE
    if _c["aligned"] is not None and _c["key"] == key and (_now - _c["t"]) < ttl:
        return _c["aligned"], _c["cov"], list(_c["valid"])
    rets = {}
    for sym in symbols:
        r = _var_symbol_returns(sym, ttl)
        if r is not None:
            rets[sym] = r
    valid = [s for s in symbols if s in rets]
    if not valid:
        return None, None, []
    aligned = pd.concat({s: rets[s] for s in valid}, axis=1).dropna()
    if isinstance(aligned.columns, pd.MultiIndex):
        aligned.columns = aligned.columns.get_level_values(0)
    aligned = aligned.tail(int(window))
    _VAR_DATA_CACHE = {"t": _now, "key": key, "aligned": aligned, "cov": aligned.cov(), "valid": list(valid)}
    return aligned, _VAR_DATA_CACHE["cov"], valid


def _hist_quantile(sorted_vals, q):
    """① 经验分位（线性插值，与 numpy.quantile 默认 method='linear' 同口径）。sorted_vals 升序。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * frac


def _evt_gpd_fit(losses_sorted, q_u=0.90):
    """① EVT/GPD 尾部拟合（矩估计 MoM，免 scipy）。
    losses_sorted: 升序损失序列（正=亏损，金额）。返回 (info, var_fn, es_fn)；拟合退化返回 (None, None, None)。
    GPD 超阈值尾部分布 P(Y>y)=(1+xi·y/sig)^(-1/xi)：
      VaR_cl = u + (sig/xi)·[((1-cl)·N/Nu)^(-xi) − 1]；ES_cl = (VaR_cl + sig − xi·u)/(1−xi)（需 xi<1）。"""
    ls = [float(x) for x in losses_sorted]
    N = len(ls)
    if N < 30:
        return None, None, None
    u = ls[min(N - 1, max(0, int(math.ceil(q_u * N)) - 1))]
    exc = sorted(x - u for x in ls if x > u)
    Nu = len(exc)
    if Nu < 5:
        return None, None, None
    m = sum(exc) / Nu
    v = sum((x - m) ** 2 for x in exc) / Nu
    if v <= 0 or m <= 0:
        return None, None, None
    xi = 0.5 * (1.0 - (m * m) / v)
    sig = 0.5 * m * ((m * m) / v + 1.0)
    if sig <= 0 or xi >= 1.0:
        return None, None, None

    def _var(cl):
        if abs(xi) < 1e-12:
            return u + sig * math.log(Nu / (N * (1.0 - cl)))
        return u + (sig / xi) * ((((1.0 - cl) * N) / Nu) ** (-xi) - 1.0)

    def _es(cl):
        return (_var(cl) + sig - xi * u) / (1.0 - xi)

    info = {
        "xi": round(xi, 4),
        "beta": round(sig, 2),
        "u": round(u, 2),
        "n": N,
        "n_exceed": Nu,
        "threshold_q": q_u,
        "heavy_tail": bool(xi >= 0.5),
    }
    return info, _var, _es


def portfolio_var(conf=(0.95, 0.99), force=False, cache_sec=60, positions=None):
    """组合 VaR / CVaR（① 2026-08-16 升级为 250 日历史模拟法 + EVT 尾部压力测试；
    var_method=param 或样本不足 var_hist_min_samples 时回退旧参数法正态）。
    返回 ok / equity / var_95 / var_99 / cvar_95 / cvar_99（金额+占权益%）/ contrib_var95(成分VaR)
    / exposures / method / evt（*_hist 键为纯历史经验值）。"""
    global _VAR_CACHE
    _now = datetime.now().timestamp()
    _injected = positions is not None
    if not _injected and not force and _VAR_CACHE["v"] is not None and (_now - _VAR_CACHE["t"]) < cache_sec:
        return _VAR_CACHE["v"]
    if not _injected:
        try:
            st = at.load_state()
        except Exception as e:
            return {"ok": False, "reason": f"账户状态读取失败: {e}"}
        positions = {s: p for s, p in st.get("positions", {}).items() if p.get("lots")}
    if not positions:
        r = {"ok": False, "reason": "当前无持仓，无法计算组合 VaR"}
        if not _injected:
            _VAR_CACHE = {"t": _now, "v": r}
        return r
    # 持仓市值暴露（带符号，元）：方向 × 手数 × 现价 × 乘数
    X, info = {}, {}
    for sym, p in positions.items():
        px = p.get("price") or p.get("avg")
        if not px:
            continue
        mult = _spec_mult(sym)
        sign = 1 if p.get("direction") == "多" else -1
        X[sym] = sign * p["lots"] * float(px) * mult
        info[sym] = {
            "name": SYMBOLS.get(sym, {}).get("name", sym),
            "direction": p.get("direction"),
            "lots": p.get("lots"),
            "price": float(px),
            "exposure": round(X[sym], 2),
        }
    if not X:
        r = {"ok": False, "reason": "持仓无可用价格"}
        if not _injected:
            _VAR_CACHE = {"t": _now, "v": r}
        return r
    # ①② 日收益率对齐 + 协方差（缓存复用：逐品种收益率 + 矩阵两层缓存，窗口尾部 var_hist_window 日）
    vcfg = _var_cfg()
    aligned, cov_df, valid = _var_aligned_cov(list(X.keys()), vcfg["window"], vcfg["cache_sec"])
    if not valid:
        r = {"ok": False, "reason": "有效日线不足（需 ≥20 个交易日）"}
        if not _injected:
            _VAR_CACHE = {"t": _now, "v": r}
        return r
    if aligned is None or len(aligned) < 20:
        r = {"ok": False, "reason": "对齐样本不足 20 日"}
        if not _injected:
            _VAR_CACHE = {"t": _now, "v": r}
        return r
    xr = pd.Series({s: X[s] for s in valid})
    var_pnl = float(xr @ cov_df @ xr)  # 组合 P&L 方差（元²）
    if var_pnl <= 0:
        r = {"ok": False, "reason": "协方差非正，无法计算"}
        if not _injected:
            _VAR_CACHE = {"t": _now, "v": r}
        return r
    sigma = math.sqrt(var_pnl)  # 组合日 P&L 标准差（元）
    prices_eq = {s: p.get("price") for s, p in positions.items() if p.get("price")}
    eq = at.snapshot(prices_eq).get("equity", 0) or _account_equity()
    # 标准正态分位数 + 正态 pdf（避免依赖 scipy；参数法回退路径 + 成分VaR 用）
    Z = {0.95: 1.6448536269514722, 0.99: 2.3263478740408408}

    def _es_norm(alpha):
        za = Z[alpha]
        phi = math.exp(-0.5 * za * za) / math.sqrt(2 * math.pi)
        return phi / (1 - alpha)

    out = {
        "ok": True,
        "equity": round(eq, 2),
        "horizon_days": 1,
        "n_positions": len(valid),
        "symbols": valid,
        "info": info,
        "exposures": {s: round(X[s], 2) for s in valid},
        "sample_days": int(len(aligned)),
    }
    use_hist = vcfg["method"] in ("hist", "historical") and len(aligned) >= vcfg["min_samples"]
    if use_hist:
        # ① 历史模拟法：组合逐日 P&L 经验分布取分位；EVT/GPD 尾部压力取严叠加（只加不减）
        _x = xr.values.astype(float)
        _pnl = aligned[valid].values.astype(float) @ _x  # 组合逐日 P&L（元）
        losses = sorted((-float(v)) for v in _pnl)
        evt_info, evt_var, evt_es = (None, None, None)
        if vcfg["evt"]:
            evt_info, evt_var, evt_es = _evt_gpd_fit(losses, vcfg["evt_q"])
        out["method"] = "hist%d%s" % (len(aligned), "+evt" if evt_info else "")
        if evt_info:
            out["evt"] = evt_info
        for a in conf:
            ka = int(a * 100)
            var_h = _hist_quantile(losses, a)
            _tail = [x for x in losses if x >= var_h]
            cvar_h = (sum(_tail) / len(_tail)) if _tail else var_h
            out[f"var_{ka}_hist"] = round(var_h, 2)
            out[f"cvar_{ka}_hist"] = round(cvar_h, 2)
            var_amt, cvar_amt = var_h, cvar_h
            if evt_info is not None and a > vcfg["evt_q"]:
                _ev, _ee = evt_var(a), evt_es(a)
                if _ev > 0:
                    var_amt = max(var_h, _ev)  # EVT 压力测试取严：历史为底、GPD 尾部只加不减
                if _ee > 0:
                    cvar_amt = max(cvar_h, _ee)
            out[f"var_{ka}"] = round(var_amt, 2)
            out[f"cvar_{ka}"] = round(cvar_amt, 2)
            out[f"var_{ka}_pct"] = round(100 * var_amt / eq, 2) if eq else None
            out[f"cvar_{ka}_pct"] = round(100 * cvar_amt / eq, 2) if eq else None
    else:
        # 参数法（正态、均值=0）回退：var_method=param 或对齐样本不足 var_hist_min_samples 时
        out["method"] = "param"
        for a in conf:
            za = Z[a]
            var_amt = za * sigma
            cvar_amt = _es_norm(a) * sigma
            ka = int(a * 100)
            out[f"var_{ka}"] = round(var_amt, 2)
            out[f"cvar_{ka}"] = round(cvar_amt, 2)
            out[f"var_{ka}_pct"] = round(100 * var_amt / eq, 2) if eq else None
            out[f"cvar_{ka}_pct"] = round(100 * cvar_amt / eq, 2) if eq else None
    # 成分 VaR（对 VaR_95 的边际贡献，求和≈总 VaR_95；协方差近似口径，与 method 无关）
    sx = cov_df @ xr
    sigma_inv = 1.0 / sigma if sigma else 0.0
    contrib = {s: round(Z[0.95] * xr[s] * sx[s] * sigma_inv, 2) for s in valid}
    out["contrib_var95"] = contrib
    if use_hist:
        out["note"] = (
            f"历史模拟法 1 日 VaR：{len(aligned)} 个对齐交易日组合逐日P&L经验分布分位；"
            f"EVT/GPD 尾部压力（阈值分位 {vcfg['evt_q']}，矩估计）取严叠加，"
            f"纯经验值见 *_hist 键；CVaR=超过 VaR 的条件期望损失。"
            f"与「计划风险R(基于止损)」互补：此处是市值暴露逐日盯市 VaR。"
            f"var_method=param 可回退参数法。"
        )
    else:
        out["note"] = (
            f"参数法（正态、均值=0）1 日 VaR，基于 {len(aligned)} 个对齐交易日日收益率协方差；"
            f"CVaR=期望短缺(ES)=超过 VaR 的条件期望损失。与「计划风险R(基于止损)」互补："
            f"此处是市值暴露逐日盯市 VaR。"
            f"（var_method=param 或对齐样本不足 var_hist_min_samples={vcfg['min_samples']} → 参数法回退）"
        )
    if not _injected:
        _VAR_CACHE = {"t": _now, "v": out}
    return out


# #123-P2B 逐仓增量 VaR：估算「候选持仓叠加到当前真实持仓」后组合 1日95% VaR 占权益%，用于预交易闸。
# 无法计算（候选无日线/异常）时返回 None → 不拦截，避免误杀正常新仓。
# ②（2026-08-16）：收益率/协方差走 _VAR_RETS_CACHE/_VAR_DATA_CACHE 两层缓存，高频逐仓不再全量重算。
def candidate_combined_var_pct(sym, direction, lots, price):
    """sym 拟以 direction(多/空) / lots 手 / price 入场，叠加到当前真实持仓后组合 VaR(95%)占权益%。"""
    try:
        if not lots or lots <= 0 or not price:
            return None
        st = at.load_state()
        base = {s: p for s, p in st.get("positions", {}).items() if p.get("lots")}
        cand = {"direction": direction, "lots": int(lots), "price": float(price), "avg": float(price)}
        combined = dict(base)
        combined[sym] = cand
        pv = portfolio_var(force=True, positions=combined)
        if pv.get("ok") and isinstance(pv.get("var_95_pct"), (int, float)):
            return pv["var_95_pct"]
    except Exception:
        return None
    return None


def spread_monitor():
    """价差套利监控：预设价差对的当前价差 / 60日均值 / z-score / 趋势(扩大·收窄)。"""
    out = []
    for a, b, name in SPREAD_PAIRS:
        try:
            da = load_daily_refreshed(a)
            db = load_daily_refreshed(b)
            if da is None or db is None or len(da) < 30 or len(db) < 30:
                out.append({"pair": name, "name": name, "error": "日线不足"})
                continue
            sa = da["close"].rename(a)
            sb = db["close"].rename(b)
            join = pd.concat([sa, sb], axis=1).dropna()
            if len(join) < 30:
                out.append({"pair": name, "name": name, "error": "对齐不足"})
                continue
            spread = join[a] - join[b]
            cur = float(spread.iloc[-1])
            win = spread.iloc[-60:]
            mean = float(win.mean())
            std = float(win.std())
            z = (cur - mean) / std if std and std > 0 else 0.0
            recent5 = float(spread.iloc[-5:].mean())
            prev5 = float(spread.iloc[-10:-5].mean()) if len(spread) >= 10 else float(spread.iloc[:-5].mean())
            trend = "扩大" if recent5 > prev5 else ("收窄" if recent5 < prev5 else "持平")
            out.append(
                {
                    "pair": f"{SYMBOLS.get(a, {}).get('name', a)}-{SYMBOLS.get(b, {}).get('name', b)}",
                    "name": name,
                    "legA": a,
                    "legB": b,
                    "current": round(cur, 1),
                    "mean": round(mean, 1),
                    "std": round(std, 1),
                    "z": round(z, 2),
                    "trend": trend,
                    "recent": round(float(spread.iloc[-1] - spread.iloc[-2]), 1) if len(spread) >= 2 else 0.0,
                }
            )
        except Exception as e:
            out.append({"pair": name, "name": name, "error": str(e)[:60]})
    return {"pairs": out}


# ── 增量缺口 #129：板块强弱轮动排序 ────────────────────────────────────────
_SECTOR_CACHE = {"t": 0.0, "v": None}


def _symbol_momentum(sym):
    """取品种日线，算多周期动量 (5d/20d/60d) + 20d 波动率。失败返回 None。"""
    try:
        df = load_daily_refreshed(sym)
        if df is None or len(df) < 65:
            return None
        c = df["close"].dropna()
        if len(c) < 65:
            return None
        ret5 = c.iloc[-1] / c.iloc[-6] - 1.0
        ret20 = c.iloc[-1] / c.iloc[-21] - 1.0
        ret60 = c.iloc[-1] / c.iloc[-61] - 1.0
        r = c.pct_change().dropna()
        vol20 = float(r.iloc[-20:].std()) if len(r) >= 20 else float(r.std())
        return (ret5, ret20, ret60, vol20)
    except Exception:
        return None


def sector_rotation(force=False, cache_sec=180):
    """板块强弱轮动排序：全市场品种（主连，剔除交割合约）按 SYMBOLS[sym]['group'] 分板块，
    逐品种算多周期动量 → 板块聚合风险调整相对强度(RS) → 排名 + 轮动象限
    （领涨加速 / 高位钝化 / 筑底回升 / 领跌走弱）。只读、不接券商 API。"""
    global _SECTOR_CACHE
    _now = datetime.now().timestamp()
    if not force and _SECTOR_CACHE["v"] is not None and (_now - _SECTOR_CACHE["t"]) < cache_sec:
        return _SECTOR_CACHE["v"]
    focus = set(FOCUS_SYMS)
    data = {}
    for sym, meta in SYMBOLS.items():
        if any(ch.isdigit() for ch in sym):
            continue  # 交割合约（SA01 等）剔除，避免板块内重复计数
        m = _symbol_momentum(sym)
        if m is None:
            continue
        ret5, ret20, ret60, vol20 = m
        rs = (ret20 / vol20) if vol20 and vol20 > 0 else 0.0  # 风险调整相对强度
        data[sym] = {
            "sym": sym,
            "name": meta.get("name", sym),
            "group": meta.get("group", "其他"),
            "ret5": ret5,
            "ret20": ret20,
            "ret60": ret60,
            "vol20": vol20,
            "rs": rs,
        }
    if not data:
        r = {"ok": False, "reason": "有效日线不足（需 ≥65 个交易日）"}
        _SECTOR_CACHE = {"t": _now, "v": r}
        return r
    # 分板块聚合
    groups = {}
    for sym, d in data.items():
        groups.setdefault(d["group"], []).append(d)
    focus_groups = set()
    for sym in focus:
        g = SYMBOLS.get(sym, {}).get("group")
        if g:
            focus_groups.add(g)
    sectors = []
    for g, members in groups.items():
        if not members:
            continue
        n = len(members)
        rs_vals = [m["rs"] for m in members]
        mean_rs = sum(rs_vals) / n
        # 板块级多周期平均（成员等权）
        gr5 = sum(m["ret5"] for m in members) / n
        gr20 = sum(m["ret20"] for m in members) / n
        gr60 = sum(m["ret60"] for m in members) / n
        up = sum(1 for m in members if m["ret20"] > 0)
        accel = gr20 - gr60  # 中期动能 − 长期动能：正=上行加速，负=动能衰减
        if gr20 > 0 and accel > 0:
            quad = "领涨加速"
        elif gr20 > 0 and accel <= 0:
            quad = "高位钝化"
        elif gr20 <= 0 and accel > 0:
            quad = "筑底回升"
        else:
            quad = "领跌走弱"
        members_sorted = sorted(members, key=lambda m: abs(m["rs"]), reverse=True)
        sectors.append(
            {
                "group": g,
                "n": n,
                "up": up,
                "down": n - up,
                "mean_rs": round(mean_rs, 3),
                "mean_ret5": round(gr5, 4),
                "mean_ret20": round(gr20, 4),
                "mean_ret60": round(gr60, 4),
                "accel": round(accel, 4),
                "quadrant": quad,
                "focus": g in focus_groups,
                "members": [
                    {"sym": m["sym"], "name": m["name"], "ret20": round(m["ret20"], 4), "rs": round(m["rs"], 3)}
                    for m in members_sorted[:6]
                ],
            }
        )
    sectors.sort(key=lambda s: s["mean_rs"], reverse=True)
    for i, s in enumerate(sectors):
        s["rank"] = i + 1
    # 全市场广度 + 聚焦板块
    total = len(data)
    total_up = sum(1 for d in data.values() if d["ret20"] > 0)
    focus_sectors = [s for s in sectors if s["focus"]]
    leading = [s["group"] for s in sectors if s["quadrant"] == "领涨加速"]
    lagging = [s["group"] for s in sectors if s["quadrant"] == "领跌走弱"]
    out = {
        "ok": True,
        "n_symbols": total,
        "n_groups": len(sectors),
        "breadth_up": total_up,
        "breadth_pct": round(100.0 * total_up / total, 1) if total else 0,
        "leading": leading,
        "lagging": lagging,
        "strongest": sectors[0]["group"] if sectors else None,
        "weakest": sectors[-1]["group"] if sectors else None,
        "focus_sectors": [
            {
                "group": s["group"],
                "rank": s["rank"],
                "mean_rs": s["mean_rs"],
                "quadrant": s["quadrant"],
                "mean_ret20": s["mean_ret20"],
                "accel": s["accel"],
            }
            for s in focus_sectors
        ],
        "sectors": sectors,
        "note": (
            "板块强弱轮动：逐品种多周期动量(5/20/60日)→风险调整相对强度 RS=20日收益/20日波动→"
            "板块 RS 均值排名；轮动象限按「中期动能(20d)正负 × 加速(20d−60d)正负」分 4 类。"
            "聚焦板块（含持仓关注的 黑系/农产品）加★标记。只读 minishare/akshare 日线，不接券商 API。"
        ),
    }
    _SECTOR_CACHE = {"t": _now, "v": out}
    return out


_VOLTGT_CACHE = {"t": 0.0, "v": None}


def vol_target_position(vol_target_pct=1.0, force=False, cache_sec=120):
    """波动率目标化头寸（#130）：给定单品种目标「日度盈亏波动率占权益比例」，
    反推每个品种的目标手数，并与当前持仓对比给出调仓建议。半自动、只读、不代下单。

    模型（每只独立 sizing，方向无关）：
      · 单品种每日 P&L 波动率(元) = 现价 × 日收益率波动率σ × 乘数 × 手数
      · 目标：单品种日度 P&L 波动率 = 权益 × vol_target_pct%
      · 目标手数 = (权益×vol_target_pct%/100) / (现价×σ×乘数)，向下取整、≥0
      · 与当前手数比较 → delta_lots → 加仓/减仓/建仓/持平 建议
    组合层面补充（仅当前持仓有方向，可算相关口径）：
      · 独立口径(忽略相关, sqrt(Σ²)) 与 相关口径(日收益率协方差) 两种组合日度波动，
        相关口径因对冲/低相关而更小，独立口径为上界。"""
    global _VOLTGT_CACHE
    _now = datetime.now().timestamp()
    if not force and _VOLTGT_CACHE["v"] is not None and (_now - _VOLTGT_CACHE["t"]) < cache_sec:
        return _VOLTGT_CACHE["v"]
    try:
        st = at.load_state()
    except Exception as e:
        return {"ok": False, "reason": f"账户状态读取失败: {e}"}
    snap = at.snapshot()
    equity = snap.get("equity") or 0
    if equity <= 0:
        r = {"ok": False, "reason": "权益无效(≤0)，无法计算目标头寸"}
        _VOLTGT_CACHE = {"t": _now, "v": r}
        return r
    positions = [p for p in snap.get("positions", []) if p.get("lots")]
    focus = list(FOCUS_SYMS) if "FOCUS_SYMS" in globals() else []
    syms, seen = [], set()
    for p in positions:
        s = p["symbol"]
        if s not in seen:
            syms.append(s)
            seen.add(s)
    # 持仓 + 聚焦品种一并给出目标手数建议（聚焦未持仓品种显示「建仓建议」）
    for s in focus:
        if s not in seen:
            syms.append(s)
            seen.add(s)
    if not syms:
        r = {"ok": False, "reason": "无持仓也无聚焦品种，无法计算"}
        _VOLTGT_CACHE = {"t": _now, "v": r}
        return r
    target_pnl_vol = equity * vol_target_pct / 100.0  # 单品种目标日度 P&L 波动(元)
    rows = []
    per_sym_vol = {}  # sym -> 当前日度 P&L 波动(元, 带符号暴露)
    per_sym_target_vol = {}  # sym -> 目标日度 P&L 波动(元)
    signed_X = {}  # sym -> 带符号暴露(元) 用于相关口径
    for sym in syms:
        px, cur_lots, direction = None, 0, None
        for p in snap.get("positions", []):
            if p.get("symbol") == sym and p.get("lots"):
                px = p.get("price") or p.get("avg")
                cur_lots = p.get("lots")
                direction = p.get("direction")
                break
        if px is None:
            if FEED:
                try:
                    pp = FEED.price(sym)
                    if pp and pp > 0:
                        px = pp
                except Exception:
                    pass
            if px is None:
                try:
                    d = load_daily_refreshed(sym)
                    if d is not None and len(d):
                        px = float(d["close"].iloc[-1])
                except Exception:
                    pass
        if not px or px <= 0:
            rows.append({"symbol": sym, "ok": False, "reason": "无价格"})
            continue
        mult = _spec_mult(sym)
        sigma = None
        try:
            d = load_daily_refreshed(sym)
            if d is not None and len(d) >= 25:
                rr = d["close"].pct_change().dropna()
                if len(rr) >= 20:
                    sigma = float(rr.iloc[-20:].std())
        except Exception:
            sigma = None
        if sigma is None or sigma <= 0:
            rows.append(
                {"symbol": sym, "ok": False, "reason": "日线波动不足", "current_lots": cur_lots, "price": round(px, 2)}
            )
            continue
        vol_per_lot = px * sigma * mult  # 每手日度 P&L 波动(元)
        target_lots = int(round(target_pnl_vol / vol_per_lot)) if vol_per_lot > 0 else 0
        target_lots = max(target_lots, 0)
        cur_vol = vol_per_lot * cur_lots
        delta = target_lots - cur_lots
        if cur_lots == 0:
            adj = "建仓" if target_lots > 0 else "无需建仓"
        elif delta > 0:
            adj = "加仓"
        elif delta < 0:
            adj = "减仓"
        else:
            adj = "持平"
        rows.append(
            {
                "symbol": sym,
                "name": SYMBOLS.get(sym, {}).get("name", sym),
                "ok": True,
                "direction": direction,
                "price": round(px, 2),
                "daily_sigma_pct": round(sigma * 100, 3),
                "mult": mult,
                "current_lots": cur_lots,
                "current_daily_vol": round(cur_vol, 2),
                "current_vol_pct": round(100 * cur_vol / equity, 3) if equity else None,
                "target_lots": target_lots,
                "target_daily_vol": round(vol_per_lot * target_lots, 2),
                "target_vol_pct": round(100 * vol_per_lot * target_lots / equity, 3) if equity else None,
                "delta_lots": delta,
                "adj": adj,
            }
        )
        if cur_lots > 0:
            ds = 1 if direction == "多" else (-1 if direction == "空" else 0)
            per_sym_vol[sym] = abs(cur_vol)
            signed_X[sym] = ds * cur_lots * px * mult
        per_sym_target_vol[sym] = vol_per_lot * target_lots
    # 组合日度波动
    cur_indep = math.sqrt(sum(v * v for v in per_sym_vol.values())) if per_sym_vol else 0.0
    target_indep = math.sqrt(sum(v * v for v in per_sym_target_vol.values())) if per_sym_target_vol else 0.0
    cur_corr = None
    if len(signed_X) >= 1:
        try:
            rets = {}
            valid = []
            for s in signed_X:
                d = load_daily_refreshed(s)
                if d is not None and len(d) >= 30:
                    rr = d["close"].pct_change().dropna()
                    rr = rr.iloc[-20:]  # 与逐品种σ口径一致（最后20日）
                    if len(rr) >= 10:
                        rets[s] = rr
                        valid.append(s)
            if valid:
                aligned = pd.concat({s: rets[s] for s in valid}, axis=1).dropna()
                if isinstance(aligned.columns, pd.MultiIndex):
                    aligned.columns = aligned.columns.get_level_values(0)
                if len(aligned) >= 5:
                    cov = aligned.cov()
                    xv = pd.Series({s: signed_X[s] for s in valid})
                    cur_corr = math.sqrt(max(float(xv @ cov @ xv), 0.0))
        except Exception:
            cur_corr = None
    out = {
        "ok": True,
        "equity": round(equity, 2),
        "vol_target_pct": vol_target_pct,
        "target_pnl_vol": round(target_pnl_vol, 2),
        "n_symbols": len(syms),
        "current_lots_total": sum(r.get("current_lots", 0) for r in rows if r.get("ok")),
        "rows": rows,
        "cur_portfolio_vol_indep": round(cur_indep, 2),
        "cur_portfolio_vol_corr": round(cur_corr, 2) if cur_corr is not None else None,
        "cur_portfolio_vol_corr_pct": round(100 * cur_corr / equity, 3) if cur_corr else None,
        "cur_portfolio_vol_indep_pct": round(100 * cur_indep / equity, 3) if equity else None,
        "target_portfolio_vol_indep": round(target_indep, 2),
        "target_portfolio_vol_indep_pct": round(100 * target_indep / equity, 3) if equity else None,
        "note": (
            f"波动率目标化：每品种目标日度盈亏波动={vol_target_pct}%权益(≈{round(target_pnl_vol, 0)}元)，"
            f"按 现价×日收益率σ×乘数 反推目标手数。当前组合日波动：相关口径"
            f"{('≈%.2f元/%.2f%%' % (cur_corr, 100 * cur_corr / equity)) if cur_corr else 'N/A'}"
            f"（含对冲更低）、独立口径≈{cur_indep:.2f}元/{100 * cur_indep / equity:.2f}%。"
            f"目标独立口径组合波动≈Σ=N×{vol_target_pct}%。仅建议、不代下单。"
        ),
    }
    _VOLTGT_CACHE["t"] = _now
    _VOLTGT_CACHE["v"] = out
    return out


# ---- #131 流动性风险 Liquidity-at-Risk ----
# 行情冲击（平方根冲击模型）：清仓成本 ≈ 名义金额 × IMPACT_BETA × √(参与率) × 日波动，
# 参与率 = 平仓手数 / 20 日均量(ADV)。参与率越高、日波动越大、流动性越差 → 冲击成本越高。
# 另给出「按 ≤MAX_PCT×ADV/日 减仓」需要的清仓天数，识别无法当日出清的品种。
_IMPACT_BETA = 0.15  # 冲击常数（占日波动比例），经验值 0.1~0.3
_LAR_MAX_PCT = 0.20  # 单日最多交易 ADV 的该比例（超过即视为冲击不可忽略）
_LAR_CAP_FRAC = 0.50  # 单笔冲击成本上限（名义金额占比）
_LAR_CACHE = {"t": 0.0, "v": None}


def liquidity_at_risk(force=False, cache_sec=180):
    _now = time.time()
    if not force and _LAR_CACHE["v"] is not None and (_now - _LAR_CACHE["t"]) < cache_sec:
        return _LAR_CACHE["v"]
    try:
        at = sys.modules.get("account_tracker") or __import__("account_tracker")
        snap = at.snapshot()
        equity = float(snap.get("equity", 0) or 0)
        positions = [p for p in snap.get("positions", []) if p.get("lots")]
        cfg = at.load_config() if hasattr(at, "load_config") else {}
        cfc = cfg.get("contract_specs", {}) if isinstance(cfg, dict) else {}
    except Exception:
        positions, equity, cfc = [], 0.0, {}

    rows = []
    for p in positions:
        sym = p.get("symbol", "")
        lots = float(p.get("lots", 0) or 0)
        px = float(p.get("price") or p.get("avg") or 0)
        mult = float((cfc.get(sym, {}) or {}).get("multiplier", 10) or 10)
        notional = lots * px * mult
        rec = {
            "symbol": sym,
            "direction": p.get("direction", ""),
            "lots": int(lots),
            "price": round(px, 2),
            "multiplier": int(mult),
            "notional": round(notional, 2),
            "ok": False,
            "reason": "",
        }
        try:
            d = load_daily_refreshed(sym)
            if d is None or len(d) < 5:
                rec["reason"] = "日线不足"
                rows.append(rec)
                continue
            vol = d["volume"].dropna()
            if len(vol) >= 5:
                adv = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())
            else:
                adv = float(vol.mean())
            oi = float(d["oi"].dropna().iloc[-1]) if "oi" in d and len(d["oi"].dropna()) else 0.0
            close = d["close"].dropna()
            if len(close) >= 20:
                sigma = float(close.pct_change().dropna().iloc[-20:].std())
            elif len(close) >= 2:
                sigma = float(close.pct_change().dropna().std())
            else:
                sigma = 0.0
            if adv <= 0:
                rec["adv"] = 0
                rec["reason"] = "无成交量数据"
                rows.append(rec)
                continue
            participation = lots / adv  # 单日清仓占 ADV 比例
            # 平方根冲击成本（名义占比）
            cost_frac = _IMPACT_BETA * math.sqrt(max(participation, 0.0)) * sigma
            cost_frac = min(cost_frac, _LAR_CAP_FRAC)
            laR = notional * cost_frac
            # 清仓天数：单日最多交易 MAX_PCT×ADV
            max_per_day = max(adv * _LAR_MAX_PCT, 1.0)
            days = int(math.ceil(lots / max_per_day)) if lots > 0 else 0
            # 流动性分层
            if participation > 0.5 or days > 3:
                tier = "低流动性 ⚠️"
            elif participation > 0.15:
                tier = "中等"
            else:
                tier = "高流动性"
            rec.update(
                {
                    "ok": True,
                    "adv": round(adv, 0),
                    "oi": round(oi, 0),
                    "sigma_daily_pct": round(100 * sigma, 3),
                    "participation_pct": round(100 * participation, 2),
                    "cost_fraction_pct": round(100 * cost_frac, 3),
                    "laR": round(laR, 2),
                    "laR_pct_equity": round(100 * laR / equity, 3) if equity else None,
                    "days_to_exit": days,
                    "impact_warn": participation > _LAR_MAX_PCT,
                    "tier": tier,
                }
            )
        except Exception as e:
            rec["reason"] = "计算异常:%s" % str(e)[:40]
            rows.append(rec)
            continue
        rows.append(rec)

    ok_rows = [r for r in rows if r.get("ok")]
    total_laR = sum(r["laR"] for r in ok_rows)
    worst = max(ok_rows, key=lambda r: r.get("participation_pct", 0)) if ok_rows else None
    max_leg = max(ok_rows, key=lambda r: r.get("laR_pct_equity") or 0) if ok_rows else None
    n_illiquid = sum(1 for r in ok_rows if r.get("tier", "").startswith("低"))
    n_warn = sum(1 for r in ok_rows if r.get("impact_warn"))

    out = {
        "ok": True,
        "equity": round(equity, 2),
        "n_symbols": len(rows),
        "impact_beta": _IMPACT_BETA,
        "max_pct_per_day": _LAR_MAX_PCT,
        "total_laR": round(total_laR, 2),
        "total_laR_pct_equity": round(100 * total_laR / equity, 3) if equity else None,
        "n_illiquid": n_illiquid,
        "n_impact_warn": n_warn,
        "worst_symbol": worst["symbol"] if worst else None,
        "worst_participation_pct": worst["participation_pct"] if worst else None,
        "max_leg_symbol": max_leg["symbol"] if max_leg else None,
        "max_leg_pct_equity": max_leg["laR_pct_equity"] if max_leg else None,
        "rows": rows,
        "note": (
            f"流动性风险(LaR)：基于平方根冲击模型，清仓冲击成本≈名义×{_IMPACT_BETA}×√(平仓量/ADV)×日波动。"
            f"组合 LaR≈{round(total_laR, 0)}元(占权益{round(100 * total_laR / equity, 2) if equity else 0}%)；"
            f"低流动性品种{n_illiquid}个、单日冲击超阈值{n_warn}个。"
            f"仅度量退出成本，不代下单。"
        ),
    }
    _LAR_CACHE["t"] = _now
    _LAR_CACHE["v"] = out
    return out


def stress_test():
    """情景压力测试：对当前持仓施加不利冲击，算冲击后动态权益 / 亏损 / 击穿止损数。
    返回 base_equity + 各情景结果（含每持仓冲击明细）。"""
    prices = {}
    if FEED:
        for sym in SYMBOLS:
            prices[sym] = FEED.price(sym)
    snap = at.snapshot(prices)
    base_equity = snap.get("equity") or 0
    base_float = snap.get("float_total", 0) or 0
    cfg = at.load_config()
    specs = cfg.get("contract_specs", {})
    held = [p for p in snap["positions"] if p.get("lots")]
    # 当前每持仓浮动盈亏 + 1R
    cur_pnl = {}
    r1 = {}
    for p in held:
        sym = p["symbol"]
        mult = specs.get(sym, {}).get("multiplier", 10)
        px = p.get("price")
        avg = p["avg"]
        ds = 1 if p["direction"] == "多" else -1
        cur_pnl[sym] = (px - avg) * mult * p["lots"] * ds if px is not None else 0
        t1 = p.get("t1")
        stop = p.get("stop")
        r1[sym] = abs(avg - t1) if t1 else (abs(avg - stop) if stop else None)
    results = []
    for sname, shock, scope in STRESS_SCENARIOS:
        if scope == "single":
            worst_loss = 0.0
            worst_sym = None
            worst_new = 0.0
            for p in held:
                sym = p["symbol"]
                mult = specs.get(sym, {}).get("multiplier", 10)
                px = p.get("price") or p["avg"]
                avg = p["avg"]
                ds = 1 if p["direction"] == "多" else -1
                newpx = px * (1 - shock * ds)  # 不利方向
                new = (newpx - avg) * mult * p["lots"] * ds
                loss = cur_pnl.get(sym, 0) - new
                if loss > worst_loss:
                    worst_loss = loss
                    worst_sym = sym
                    worst_new = new
            new_float_total = base_float - worst_loss
            eq_after = round(base_equity + (new_float_total - base_float), 2)
            results.append(
                {
                    "name": sname,
                    "scope": scope,
                    "equity_after": eq_after,
                    "loss": round(worst_loss, 2),
                    "breached": [worst_sym] if worst_sym else [],
                    "detail": f"最脆弱单品: {worst_sym or '—'}（冲击后该笔盈亏 {round(worst_new, 0)}）",
                }
            )
            continue
        new_float_total = 0.0
        breached = []
        for p in held:
            sym = p["symbol"]
            mult = specs.get(sym, {}).get("multiplier", 10)
            px = p.get("price") or p["avg"]
            avg = p["avg"]
            ds = 1 if p["direction"] == "多" else -1
            if scope == "black" and sym not in _BLACK:
                shk = 0.0
            elif scope == "ag" and sym not in _AG:
                shk = 0.0
            else:
                shk = shock
            newpx = px * (1 - shk * ds)  # 不利方向
            new = (newpx - avg) * mult * p["lots"] * ds
            new_float_total += new
            stop = p.get("stop")
            if stop is not None and ((ds > 0 and newpx <= stop) or (ds < 0 and newpx >= stop)):
                breached.append(sym)
        eq_after = round(base_equity + (new_float_total - base_float), 2)
        total_loss = round(base_float - new_float_total, 2)
        results.append(
            {
                "name": sname,
                "scope": scope,
                "equity_after": eq_after,
                "loss": total_loss,
                "breached": breached,
                "detail": f"击穿止损: {', '.join(breached) if breached else '无'}",
            }
        )
    return {
        "base_equity": round(base_equity, 2),
        "base_float": round(base_float, 2),
        "positions": len(held),
        "scenarios": results,
    }


# ----------------------------------------------------------------------------
# #128 相关性崩溃 / 危机趋同 stress 专项
# 在 #123 portfolio_var(正常观察协方差) 之外，把全部非对角相关性抬升至危机相关 ρ，
# 重算 VaR，量化「分散化保护在危机中蒸发多少」；并给出波动率放大+对冲失效的
# 危机趋同情景(美元)。与 #92 stress_test(均匀百分比冲击) 互补——此处是「相关性维度」的崩溃。
# 不接券商 API、纯只读；默认 ρ=0.7(尾部 0.9)、趋同 z=3.0σ。
# ----------------------------------------------------------------------------
_CORRBRK_CACHE = {"t": 0.0, "v": None}


def correlation_breakdown_stress(force=False, rho_crisis=0.7, rho_tail=0.9, z_crisis=3.0, cache_sec=60):
    """相关性崩溃 / 危机趋同 stress：危机相关 ρ 下重算 VaR + 对冲失效趋同情景。"""
    global _CORRBRK_CACHE
    _now = datetime.now().timestamp()
    if not force and _CORRBRK_CACHE["v"] is not None and (_now - _CORRBRK_CACHE["t"]) < cache_sec:
        return _CORRBRK_CACHE["v"]
    try:
        st = at.load_state()
    except Exception as e:
        return {"ok": False, "reason": f"账户状态读取失败: {e}"}
    positions = {s: p for s, p in st.get("positions", {}).items() if p.get("lots")}
    if len(positions) < 2:
        r = {"ok": False, "reason": "持仓不足 2 个，无法评估相关性崩溃"}
        _CORRBRK_CACHE = {"t": _now, "v": r}
        return r
    # 暴露向量 X（带符号，元）
    X, info = {}, {}
    for sym, p in positions.items():
        px = p.get("price") or p.get("avg")
        if not px:
            continue
        mult = _spec_mult(sym)
        sign = 1 if p.get("direction") == "多" else -1
        X[sym] = sign * p["lots"] * float(px) * mult
        info[sym] = {
            "name": SYMBOLS.get(sym, {}).get("name", sym),
            "direction": p.get("direction"),
            "lots": p["lots"],
            "price": float(px),
            "exposure": round(X[sym], 2),
        }
    if len(X) < 2:
        r = {"ok": False, "reason": "持仓无可用价格"}
        _CORRBRK_CACHE = {"t": _now, "v": r}
        return r
    # 日收益率对齐
    rets = {}
    for sym in X:
        try:
            df = load_daily_refreshed(sym)
            if df is None or len(df) < 30:
                continue
            r = df["close"].pct_change().dropna()
            if len(r) >= 20:
                rets[sym] = r
        except Exception:
            continue
    valid = [s for s in X if s in rets]
    if len(valid) < 2:
        r = {"ok": False, "reason": "有效日线不足（需 ≥20 个交易日）"}
        _CORRBRK_CACHE = {"t": _now, "v": r}
        return r
    aligned = pd.concat({s: rets[s] for s in valid}, axis=1).dropna()
    if isinstance(aligned.columns, pd.MultiIndex):
        aligned.columns = aligned.columns.get_level_values(0)
    if len(aligned) < 20:
        r = {"ok": False, "reason": "对齐样本不足 20 日"}
        _CORRBRK_CACHE = {"t": _now, "v": r}
        return r
    cov = aligned.cov().loc[valid, valid]
    xr = pd.Series({s: X[s] for s in valid})
    var_n = float(xr @ cov @ xr)
    if var_n <= 0:
        r = {"ok": False, "reason": "协方差非正，无法计算"}
        _CORRBRK_CACHE = {"t": _now, "v": r}
        return r
    sigma_n = math.sqrt(var_n)
    s_i = {s: math.sqrt(max(cov.loc[s, s], 0.0)) for s in valid}
    # 现状平均两两相关系数
    off = []
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            si, sj = s_i[valid[i]], s_i[valid[j]]
            if si > 0 and sj > 0:
                off.append(cov.loc[valid[i], valid[j]] / (si * sj))
    avg_corr_obs = float(sum(off) / len(off)) if off else 0.0

    # 危机协方差（非对角相关→ρ·sign(X_i·X_j)：与每对持仓买卖方向一致的最不利相关——
    # 同侧持仓取 +ρ(同涨同跌放大损失)，对侧持仓(对冲)取 -ρ(多空同时向不利方向发散)，
    # 二者都使组合方差增大，保证危机 VaR≥正常 VaR，代表「所有腿同时向不利方向趋同」。）
    def _crisis_sigma(rho):
        n = len(valid)
        tot = 0.0
        for i in range(n):
            for j in range(n):
                a_, b_ = valid[i], valid[j]
                if a_ == b_:
                    cij = cov.loc[a_, b_]
                else:
                    sg = 1.0 if xr[a_] * xr[b_] >= 0 else -1.0
                    cij = rho * sg * s_i[a_] * s_i[b_]
                tot += xr[a_] * xr[b_] * cij
        return math.sqrt(max(tot, 0.0))

    sigma_c = _crisis_sigma(rho_crisis)
    sigma_t = _crisis_sigma(rho_tail)
    prices_eq = {s: p.get("price") for s, p in positions.items() if p.get("price")}
    eq = at.snapshot(prices_eq).get("equity", 0) or _account_equity()
    Z = {0.95: 1.6448536269514722, 0.99: 2.3263478740408408}
    sum_standalone = sum(Z[0.95] * s_i[s] * abs(xr[s]) for s in valid)  # 名义独立 VaR(未计相关)
    out = {
        "ok": True,
        "equity": round(eq, 2),
        "n_positions": len(valid),
        "symbols": valid,
        "info": info,
        "exposures": {s: round(X[s], 2) for s in valid},
        "sample_days": int(len(aligned)),
        "rho_crisis": rho_crisis,
        "rho_tail": rho_tail,
        "avg_corr_obs": round(avg_corr_obs, 3),
        "z_crisis": z_crisis,
    }
    # Lens A：相关性崩溃 VaR
    for a in (0.95, 0.99):
        za = Z[a]
        ka = int(a * 100)
        vn = za * sigma_n
        vc = za * sigma_c
        vt = za * sigma_t
        out[f"var_{ka}"] = round(vn, 2)
        out[f"var_{ka}_pct"] = round(100 * vn / eq, 2) if eq else None
        out[f"var_{ka}_crisis"] = round(vc, 2)
        out[f"var_{ka}_crisis_pct"] = round(100 * vc / eq, 2) if eq else None
        out[f"var_{ka}_tail"] = round(vt, 2)
        out[f"amplify_{ka}"] = round(sigma_c / sigma_n, 2) if sigma_n else None
        out[f"amplify_tail_{ka}"] = round(sigma_t / sigma_n, 2) if sigma_n else None

    # 分散化保护（正常 vs 危机）
    def _div_benefit(sigma):
        port_var = Z[0.95] * sigma
        return (sum_standalone - port_var) / sum_standalone if sum_standalone else 0.0

    div_n = _div_benefit(sigma_n)
    div_c = _div_benefit(sigma_c)
    out["div_benefit_normal"] = round(div_n, 3)
    out["div_benefit_crisis"] = round(div_c, 3)
    out["div_erosion"] = round(div_n - div_c, 3)
    # Lens B：危机趋同情景（波动率放大 + 对冲失效）
    per_leg = {s: round(z_crisis * s_i[s] * abs(xr[s]), 2) for s in valid}
    loss_convergence = z_crisis * sum(s_i[s] * abs(xr[s]) for s in valid)
    loss_hedged_normal = z_crisis * sigma_n
    hedge_unwind = loss_convergence - loss_hedged_normal
    out["loss_convergence"] = round(loss_convergence, 2)
    out["loss_convergence_pct"] = round(100 * loss_convergence / eq, 2) if eq else None
    out["loss_hedged_normal"] = round(loss_hedged_normal, 2)
    out["hedge_unwind"] = round(hedge_unwind, 2)
    out["hedge_unwind_pct"] = round(100 * hedge_unwind / eq, 2) if eq else None
    out["per_leg_adverse"] = per_leg
    # 危机边际 VaR（对危机 VaR95 的贡献）
    inv_c = 1.0 / sigma_c if sigma_c else 0.0
    crisis_cov = pd.DataFrame(index=valid, columns=valid, dtype=float)
    for i in range(len(valid)):
        for j in range(len(valid)):
            a_, b_ = valid[i], valid[j]
            if a_ == b_:
                crisis_cov.loc[a_, b_] = cov.loc[a_, b_]
            else:
                sg = 1.0 if xr[a_] * xr[b_] >= 0 else -1.0
                crisis_cov.loc[a_, b_] = rho_crisis * sg * s_i[a_] * s_i[b_]
    xc = crisis_cov @ xr
    out["contrib_crisis_var95"] = {s: round(Z[0.95] * xr[s] * xc[s] * inv_c, 2) for s in valid}
    out["note"] = (
        f"相关性崩溃 stress：把全部非对角相关抬至危机相关 ρ={rho_crisis}(尾部 {rho_tail})，"
        f"且按每对持仓买卖方向取最不利符号(同侧+ρ/对冲-ρ)，即「所有腿同时向不利方向趋同」后重算 VaR。"
        f"现状平均两两相关 {avg_corr_obs:.2f}；危机放大倍数 {out['amplify_95']}×。"
        f"分散化保护由正常 {div_n * 100:.0f}% 降至危机 {div_c * 100:.0f}%（蒸发 {out['div_erosion'] * 100:.0f} 个百分点）。"
        f"危机趋同情景(每腿 z={z_crisis}σ 不利、对冲失效)组合损失约 {round(loss_convergence, 0)}，"
        f"较正常相关对冲版(约 {round(loss_hedged_normal, 0)}) 多亏 {round(hedge_unwind, 0)}（对冲消失）。非投资建议。"
    )
    _CORRBRK_CACHE = {"t": _now, "v": out}
    return out


def _fire_trail_alert(sym, pos, px, state, new_stop):
    """移动止损状态切换时通知（保本/跟踪/达t2/尾仓），与触价报警同款弹窗+语音+聊天流。"""
    # P-G：尾仓提示文案动态引用配置(tail_pct/min_profit_R/tail_trail_R)，让"平X%留Y%尾仓"真实反映参数
    tt = _STRAT_CFG.get("trailing_tail", {})
    tail_pct = float(tt.get("tail_pct", 0.25))
    close_pct = int(round((1 - tail_pct) * 100))
    keep_pct = int(round(tail_pct * 100))
    min_profit_R = float(tt.get("min_profit_R", 2.0))
    tail_trail_R = float(tt.get("tail_trail_R", 2.0))
    tip = {
        "保本": "t1(1R)已触及，止损已自动上移至开仓价(保本)，后续不再亏本金",
        "跟踪": "盈利扩大，移动止损已上移锁定利润",
        "达t2": "已达 t2(2R)全平目标，建议全部止盈离场",
        "尾仓": (
            f"已达 t{min_profit_R:.0f}R，建议平掉 {close_pct}%、留 {keep_pct}% 尾仓用 "
            f"{tail_trail_R:.0f}×R 宽移动止损跟出，让利润奔跑（回撤触新止损即离场）"
        ),
    }.get(state, "")
    sig = {
        "name": pos.get("name", sym),
        "direction": pos["direction"],
        "lots": pos["lots"],
        "symbol": sym,
        "price": px,
        "stop": new_stop,
        "t1": pos.get("t1"),
        "t2": pos.get("t2"),
        "alert_type": f"移动止损·{state}",
        "alert_label": state,
        "reason": (
            f"{pos.get('name', sym)} {pos['direction']} {pos['lots']}手：{tip}"
            f"（新止损 {_fmt_price(new_stop)}，现价 {_fmt_price(px)}）"
        ),
    }
    # 2026-08-20: add main contract code
    if not sig.get("contract") and sig.get("symbol"):
        try:
            _auth = ml._authoritative_contracts()
            _code = _auth.get(sig["symbol"])
            if _code:
                sig["contract"] = ml.normalize_contract_code(_code)
        except Exception:
            pass
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
    sig["kind"] = "alert"
    append_chat(sig)
    # C4 报警历史：移动止损状态切换统一落盘
    log_alert("移动止损", sym, sig["name"], sig["reason"], {"state": state, "new_stop": new_stop, "price": px})


def calc_tiered_stop_loss(position, entry_price, atr, sigma_stop_price):
    """
    分级止损计算（v5.1 新增）

    根据持仓浮盈状态，自动判断当前应使用哪一级止损：
    - 初始止损：入场时设定的止损价（风险1%）
    - 保本止损：浮盈≥1R时，止损上移至成本价
    - 移动止损：浮盈≥2R时，使用ATR移动止损追踪
    - 硬止损：3σ标准差兜底（极端行情）

    Args:
        position: 持仓对象（含 direction, entry_price, current_price 等）
        entry_price: 入场均价
        atr: 当前ATR值
        sigma_stop_price: 3σ硬止损价格

    Returns:
        dict: {
            "stop_level": 当前止损级别,
            "stop_price": 当前止损价,
            "stop_distance_pct": 止损距离百分比
        }
    """
    direction = position.get("direction", "long")
    current_price = position.get("current_price", entry_price)

    # 计算1R的价格距离（基于ATR*2作为初始止损距离）
    initial_stop_dist = atr * 2.0 if atr and atr > 0 else (entry_price * 0.02)

    # 计算初始止损价
    if direction in ("多", "long"):
        initial_stop = entry_price - initial_stop_dist
    else:
        initial_stop = entry_price + initial_stop_dist

    # 计算当前浮盈（以R为单位）
    if direction in ("多", "long"):
        profit_pips = current_price - entry_price
    else:
        profit_pips = entry_price - current_price
    profit_r = profit_pips / initial_stop_dist if initial_stop_dist > 0 else 0

    # 分级判断
    if profit_r >= 2.0:
        # 第三级：移动止损（ATR追踪）
        if direction in ("多", "long"):
            trailing_stop = current_price - atr * TRAILING_STOP_ATR_MULT
        else:
            trailing_stop = current_price + atr * TRAILING_STOP_ATR_MULT
        # 移动止损不低于保本
        if direction in ("多", "long"):
            stop_price = max(trailing_stop, entry_price)
        else:
            stop_price = min(trailing_stop, entry_price)
        stop_level = STOP_LOSS_LEVEL_TRAILING
    elif profit_r >= BREAKEVEN_TRIGGER_R:
        # 第二级：保本止损
        stop_price = entry_price
        stop_level = STOP_LOSS_LEVEL_BREAKEVEN
    else:
        # 第一级：初始止损
        stop_price = initial_stop
        stop_level = STOP_LOSS_LEVEL_INITIAL

    # 第四级：硬止损兜底（3σ更严格时使用）
    if sigma_stop_price and sigma_stop_price > 0:
        if direction in ("多", "long"):
            if sigma_stop_price > stop_price:
                stop_price = sigma_stop_price
                stop_level = STOP_LOSS_LEVEL_HARD
        else:
            if sigma_stop_price < stop_price:
                stop_price = sigma_stop_price
                stop_level = STOP_LOSS_LEVEL_HARD

    # 计算止损距离百分比
    stop_distance_pct = abs(current_price - stop_price) / current_price * 100 if current_price > 0 else 0

    return {
        "stop_level": stop_level,
        "stop_price": round(stop_price, 4),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "profit_r": round(profit_r, 2),
    }


def calc_signal_quality_score(signal, klines, volume_ma):
    """
    信号质量评分（v5.1 新增）
    对突破类信号进行多维度质量评估，过滤低质量假突破信号。
    """
    score = 0
    details = {}

    direction = signal.get("direction", "long")
    signal_type = signal.get("signal_type", "breakout")

    # 只对突破类信号做质量过滤
    if signal_type not in ["breakout", "trend_break", "resistance_break"]:
        return {"score": 100, "passed": True, "details": {"note": "非突破类信号，默认通过"}}

    if not klines or len(klines) < 3:
        return {"score": SIGNAL_QUALITY_MIN_SCORE, "passed": True, "details": {"note": "K线数据不足，默认通过"}}

    # 获取突破K线
    breakout_candle = klines[-2]
    open_p = breakout_candle.get("open", 0)
    close_p = breakout_candle.get("close", 0)
    high_p = breakout_candle.get("high", 0)
    low_p = breakout_candle.get("low", 0)
    volume = breakout_candle.get("volume", 0)

    if open_p == 0 or volume_ma == 0:
        return {"score": SIGNAL_QUALITY_MIN_SCORE, "passed": True, "details": {"note": "数据异常，默认通过"}}

    # 维度1：收盘价确认（30分）
    body_pct = abs(close_p - open_p) / open_p * 100
    body_score = min(30, body_pct / BREAKOUT_BODY_MIN_PCT * 30)
    details["body_confirm"] = {
        "score": round(body_score, 1),
        "max": 30,
        "body_pct": round(body_pct, 2),
        "threshold": BREAKOUT_BODY_MIN_PCT,
    }
    score += body_score

    # 维度2：成交量确认（30分）
    if volume_ma > 0:
        volume_ratio = volume / volume_ma
        volume_score = min(30, volume_ratio / BREAKOUT_VOLUME_MULT * 30)
    else:
        volume_ratio = 1.0
        volume_score = 15
    details["volume_confirm"] = {
        "score": round(volume_score, 1),
        "max": 30,
        "volume_ratio": round(volume_ratio, 2),
        "threshold": BREAKOUT_VOLUME_MULT,
    }
    score += volume_score

    # 维度3：回踩确认（20分）
    pullback_score = 0
    if len(klines) >= 3:
        if direction == "long":
            upper_wick = high_p - max(open_p, close_p)
            body_size = abs(close_p - open_p)
            if body_size > 0 and upper_wick / body_size < 0.5:
                pullback_score = 20
            else:
                pullback_score = 10
        else:
            lower_wick = min(open_p, close_p) - low_p
            body_size = abs(close_p - open_p)
            if body_size > 0 and lower_wick / body_size < 0.5:
                pullback_score = 20
            else:
                pullback_score = 10

    if BREAKOUT_PULLBACK_CONFIRM and len(klines) < 4:
        pullback_score = max(pullback_score, 10)

    details["pullback_confirm"] = {"score": round(pullback_score, 1), "max": 20}
    score += pullback_score

    # 维度4：位置合理性（20分）
    entry_price = signal.get("entry_price", close_p)
    if direction == "long":
        breakout_dist = (entry_price - high_p) / high_p * 100 if high_p > 0 else 0
    else:
        breakout_dist = (low_p - entry_price) / low_p * 100 if low_p > 0 else 0

    if 0.3 <= breakout_dist <= 2.0:
        position_score = 20
    elif breakout_dist < 0.3:
        position_score = 10
    elif breakout_dist <= 5:
        position_score = 15
    else:
        position_score = 5

    details["position_quality"] = {
        "score": round(position_score, 1),
        "max": 20,
        "breakout_dist_pct": round(breakout_dist, 2),
    }
    score += position_score

    score = round(score, 1)
    passed = score >= SIGNAL_QUALITY_MIN_SCORE

    return {"score": score, "passed": passed, "details": details}


def get_higher_tf_trend(higher_tf_klines):
    """
    大周期趋势判断（v5.1 新增）
    使用双均线（快线+慢线）判断大周期趋势方向。
    """
    if not higher_tf_klines or len(higher_tf_klines) < HIGHER_TF_MA_SLOW + 5:
        return {"trend": "sideways", "strength": 0, "ma_fast": 0, "ma_slow": 0}

    closes = [k.get("close", 0) for k in higher_tf_klines if k.get("close", 0) > 0]

    if len(closes) < HIGHER_TF_MA_SLOW:
        return {"trend": "sideways", "strength": 0, "ma_fast": 0, "ma_slow": 0}

    ma_fast = sum(closes[-HIGHER_TF_MA_FAST:]) / HIGHER_TF_MA_FAST
    ma_slow = sum(closes[-HIGHER_TF_MA_SLOW:]) / HIGHER_TF_MA_SLOW

    if ma_slow == 0:
        return {"trend": "sideways", "strength": 0, "ma_fast": ma_fast, "ma_slow": ma_slow}

    divergence_pct = (ma_fast - ma_slow) / ma_slow * 100
    strength = min(100, abs(divergence_pct) * 20)

    if divergence_pct > 0.5:
        trend = "bullish"
    elif divergence_pct < -0.5:
        trend = "bearish"
    else:
        trend = "sideways"

    return {
        "trend": trend,
        "strength": round(strength, 1),
        "ma_fast": round(ma_fast, 4),
        "ma_slow": round(ma_slow, 4),
        "divergence_pct": round(divergence_pct, 2),
    }


def apply_multi_tf_filter(signal, higher_tf_trend):
    """
    应用多周期过滤（v5.1 新增）
    根据大周期趋势对信号进行调整。
    """
    if not MULTI_TIMEFRAME_ENABLED:
        return {"passed": True, "pos_scale": 1.0, "rr_required": MIN_RR_RATIO, "reason": "多周期确认未启用"}

    trend = higher_tf_trend.get("trend", "sideways")
    strength = higher_tf_trend.get("strength", 0)
    direction = signal.get("direction", "long")

    is_bullish = trend == "bullish" and direction == "long"
    is_bearish = trend == "bearish" and direction == "short"
    is_with_trend = is_bullish or is_bearish

    if trend == "sideways":
        return {
            "passed": True,
            "pos_scale": 0.8,
            "rr_required": get_effective_param("min_rr_ratio", MIN_RR_RATIO),
            "reason": f"大周期震荡，仓位×0.8（趋势强度: {strength}）",
        }

    if is_with_trend:
        return {
            "passed": True,
            "pos_scale": 1.0,
            "rr_required": get_effective_param("min_rr_ratio", MIN_RR_RATIO),
            "reason": f"顺大周期趋势（{trend}），趋势强度: {strength}",
        }
    else:
        new_rr = MIN_RR_RATIO * COUNTER_TREND_RR_BOOST
        return {
            "passed": True,
            "pos_scale": COUNTER_TREND_POS_SCALE,
            "rr_required": round(new_rr, 2),
            "reason": f"逆大周期趋势（{trend}），仓位×{COUNTER_TREND_POS_SCALE}，盈亏比要求≥{new_rr:.1f}:1",
        }


def _compute_graded_stop_levels(entry, stop, t1, t2, atr, direction):
    """计算分级止损的各档位价格。
    返回: {initial, breakeven, trailing, hard}
    """
    ds = 1 if direction == "多" else -1
    oneR = abs(entry - t1) if t1 else abs(entry - stop)

    levels = {}
    # 初始止损
    levels[STOP_LOSS_LEVEL_INITIAL] = stop

    # 保本止损（1R触发）
    levels[STOP_LOSS_LEVEL_BREAKEVEN] = entry

    # 移动止损（当前价 -/+ ATR倍数）
    if atr and atr > 0:
        if ds > 0:
            levels[STOP_LOSS_LEVEL_TRAILING] = entry - TRAILING_STOP_ATR_MULT * atr
        else:
            levels[STOP_LOSS_LEVEL_TRAILING] = entry + TRAILING_STOP_ATR_MULT * atr
    else:
        levels[STOP_LOSS_LEVEL_TRAILING] = stop

    # 硬止损（3σ）
    if atr and atr > 0:
        if ds > 0:
            levels[STOP_LOSS_LEVEL_HARD] = entry - SIGMA_STOP_MULT * atr
        else:
            levels[STOP_LOSS_LEVEL_HARD] = entry + SIGMA_STOP_MULT * atr
    else:
        levels[STOP_LOSS_LEVEL_HARD] = stop

    return levels


def _get_stop_level_state(profit_R, cur_state, direction, px, entry, oneR, atr):
    """根据浮盈R倍数确定当前止损状态。
    返回: (new_stop, new_state)
    """
    ds = 1 if direction == "多" else -1

    if profit_R < BREAKEVEN_TRIGGER_R:
        # 未达1R：保持初始止损
        return None, STOP_LOSS_LEVEL_INITIAL

    elif profit_R < 2.0:
        # 1R-2R：保本止损
        return entry, STOP_LOSS_LEVEL_BREAKEVEN

    else:
        # ≥2R：进入移动止损
        if atr and atr > 0:
            if ds > 0:
                trailing_stop = max(entry, px - TRAILING_STOP_ATR_MULT * atr)
            else:
                trailing_stop = min(entry, px + TRAILING_STOP_ATR_MULT * atr)
        else:
            if ds > 0:
                trailing_stop = max(entry, px - oneR)
            else:
                trailing_stop = min(entry, px + oneR)
        return trailing_stop, STOP_LOSS_LEVEL_TRAILING


def manage_trailing_stops():
    """移动止损自动管理（仅实时行情可用时跑，避免非交易时段误调）：
      - 价格触及 t1(1R) → 止损上移至开仓价(保本)
      - 盈利继续扩大 → 按 1R 跟踪上移锁定利润（多头止损=max(开仓价, 现价-1R)）
      - 触及 t2(2R) → 标记达t2，提示全平
    只更新 account_state 的 stop + trail_state（不向经纪商下单）。"""
    if FEED is None or not FEED_AVAILABLE:
        return
    st = at.load_state()
    for sym, pos in st["positions"].items():
        if not pos.get("lots"):
            continue
        px = FEED.price(sym)
        if px is None:
            continue
        ds = 1 if pos["direction"] == "多" else -1
        entry = pos.get("avg")
        t1 = pos.get("t1")
        t2 = pos.get("t2")
        stop = pos.get("stop")
        if entry is None or t1 is None:
            continue  # 无 t1 无法定义 1R，跳过
        oneR = abs(entry - t1)
        if oneR <= 0:
            continue
        profit = ds * (px - entry)  # 以 1R 为单位的盈利
        cur_state = pos.get("trail_state")
        profit_R = profit / oneR  # 以 1R 为单位的盈利
        if profit_R < 1:
            continue  # 未达 t1：保持初始止损不动
        # ── P-G 尾仓（trailing_tail）：参数统一从 _STRAT_CFG 读取；min_profit_R 驱动入尾仓阈值 ──
        tt = _STRAT_CFG.get("trailing_tail", {})
        # 开关优先级：特性开关 > 旧配置 > 默认关闭
        _tail_sw = None
        try:
            _tail_sw = fmg.get_manager().is_enabled("trailing_stop")
        except Exception:
            pass
        if _tail_sw is None:
            _tail_sw = bool(tt.get("enabled", False))
        tail_enabled_cfg = bool(_tail_sw)
        tail_trail_R = float(tt.get("tail_trail_R", 2.0))
        min_profit_R = float(tt.get("min_profit_R", 2.0))
        if cur_state == "尾仓":
            # 已达 t2，用更宽(tail_trail_R×1R)移动止损跟出，回撤触新止损即离场
            tail_stop_dist = tail_trail_R * oneR
            if ds > 0:
                cand = max(entry, px - tail_stop_dist)  # 不低于开仓价(保本底)
                new_stop = cand if cand > stop else stop
            else:
                cand = min(entry, px + tail_stop_dist)
                new_stop = cand if cand < stop else stop
            new_state = "尾仓"
        elif cur_state in (None, "", "初始"):
            # 首次触及 1R → 保本（止损=开仓价，相对初始止损可能变宽，属正常保本动作）
            new_stop = entry
            new_state = "保本"
        elif profit_R >= min_profit_R and t2 is not None and bool(pos.get("tail_enabled")) and tail_enabled_cfg:
            # P-G：进入尾仓态——平掉 (1-tail_pct) 锁 min_profit_R×R，留 tail_pct 用宽 trail 跟出（不锁全平）
            tail_stop_dist = tail_trail_R * oneR
            if ds > 0:
                base = t2 - tail_stop_dist
                new_stop = base if base > stop else stop  # 尾仓基线比 t2 宽松（允许回撤）
            else:
                base = t2 + tail_stop_dist
                new_stop = base if base < stop else stop
            new_state = "尾仓"
        elif profit_R >= 2 and t2 is not None:
            # 原逻辑：已达 t2(2R)全平目标 → 把止损锁到 t2（锁定 2R 利润），提示全平（波动/震荡或非尾仓）
            # P0-3 修正（2026-08-14）：对空头，t2 在开仓价下方是止盈目标，不能把止损下移到 t2，
            # 否则止损位会跑到开仓价下方、盈利区被错误报为止损。空头达 t2 应锁定保本。
            new_state = "达t2"
            if ds > 0:
                # 多头：止损上移至 t2 锁定 2R 利润
                new_stop = max(stop, t2) if t2 > stop else stop
            else:
                # 空头：止损不下移；若尚未保本，则上移至开仓价锁定本金
                new_stop = min(stop, entry) if entry < stop else stop
        else:
            # 跟踪收紧：多头止损上移(>旧)、空头止损下移(<旧)，且不低于开仓价(保本底)
            if ds > 0:
                cand = max(entry, px - oneR)
                new_stop = cand if cand > stop else stop
            else:
                cand = min(entry, px + oneR)
                new_stop = cand if cand < stop else stop
            new_state = "跟踪"
        changed = False
        if (stop is None and new_stop is not None) or (stop is not None and abs(new_stop - stop) > 1e-6):
            changed = True
        if new_state != cur_state:
            changed = True
        if not changed:
            continue
        at.advance_trailing(sym, new_stop, new_state)
        if new_state != cur_state:
            _fire_trail_alert(sym, pos, px, new_state, new_stop)


def compute_heat(prices=None):
    """组合风险热度（A1/B4）：汇总所有持仓的「计划风险R合计 + 当前未实现R」，对比账户风险预算。
    坚持不自动下单，仅用于面板提示与硬上限预警（风险超标→红字勿加仓）。"""
    if prices is None:
        prices = {}
        if FEED:
            for sym in SYMBOLS:
                prices[sym] = FEED.price(sym)
    snap = at.snapshot(prices)
    cfg = at.load_config()
    specs = cfg.get("contract_specs", {})
    # 组合总风险预算 = 权益 × portfolio_risk_pct(默认 4.5%)。红线：绝不能拿「单笔风险% 1.5」当组合预算
    # ——那是单笔止损上限，5 笔合计计划风险去除会虚高（曾误报 91.8%，而实际保证金仓位仅 ~10%）。
    risk_pct = _tc_num("portfolio_risk_pct", PORTFOLIO_RISK_PCT)
    equity = snap.get("equity", 0) or 0
    budget = equity * risk_pct / 100.0 if equity > 0 else 0
    margin_cap_pct = snap.get("margin_cap_pct", 30)
    rows = []
    total_planned = 0.0
    total_unreal_r = 0.0
    single_over = False
    for p in snap.get("positions", []):
        if not p.get("lots"):
            continue
        sym = p["symbol"]
        mult = (specs.get(sym) or {}).get("multiplier", 10)
        stop = p.get("stop")
        avg = p.get("avg")
        lots = p["lots"]
        planned = None
        unreal_r = None
        if stop is not None and avg is not None:
            sd = abs(stop - avg) * mult * lots
            if sd > 0:
                planned = sd
                fp = p.get("float_pnl")
                if fp is not None:
                    unreal_r = fp / sd
        if planned:
            total_planned += planned
            if unreal_r is not None:
                total_unreal_r += unreal_r
        mp = p.get("margin_pct", 0) or 0
        if mp > margin_cap_pct:
            single_over = True
        rows.append(
            {
                "symbol": sym,
                "name": p.get("name", sym),
                "lots": lots,
                "direction": p.get("direction"),
                "planned_r": round(planned, 2) if planned else None,
                "unreal_r": round(unreal_r, 3) if unreal_r is not None else None,
                "margin_pct": mp,
            }
        )
    heat_pct = round(total_planned / budget * 100, 1) if budget > 0 else 0.0
    over = (heat_pct > 100) or single_over
    status = "超标" if over else ("警戒" if heat_pct > 80 else "正常")
    return {
        "budget": round(budget, 2),
        "equity": round(equity, 2),
        "risk_pct": risk_pct,
        "portfolio_risk_pct": risk_pct,  # risk_pct 此处=组合总风险预算%
        "total_planned_r": round(total_planned, 2),
        "total_unreal_r": round(total_unreal_r, 2),
        "heat_pct": heat_pct,
        "status": status,
        "over": bool(over),
        "margin_cap_pct": margin_cap_pct,
        "rows": rows,
    }


_HEAT_ALERT_STATE = {"status": None}


def check_heat_alert(prices=None):
    """风险预算硬上限报警（B4 + C4）：热度状态由 正常/警戒 → 超标 时提醒一次并落报警历史。
    状态回落后可再次触发，避免每轮重复弹窗。"""
    try:
        h = compute_heat(prices)
    except Exception:
        return 0
    status = h.get("status")
    if status == _HEAT_ALERT_STATE.get("status"):
        return 0
    prev = _HEAT_ALERT_STATE.get("status")
    _HEAT_ALERT_STATE["status"] = status
    if status != "超标" or prev is None:
        return 0
    text = (
        f"组合风险超标：计划风险合计 {h.get('total_planned_r')} 元 / 预算 "
        f"{h.get('budget')} 元 = 热度 {h.get('heat_pct')}%，已超风险预算上限——"
        f"勿再加仓，优先减仓或收紧止损。"
    )
    sig = {
        "name": "组合风险",
        "direction": "风控",
        "lots": 0,
        "price": None,
        "stop": None,
        "target": None,
        "t1": None,
        "t2": None,
        "alert_type": "风险超标",
        "alert_label": "风险预算",
        "alert_level": f"{h.get('heat_pct')}%",
        "reason": text,
    }
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
    sig["kind"] = "alert"
    append_chat(sig)
    log_alert("风险超标", None, "组合风险", text, {"heat_pct": h.get("heat_pct"), "budget": h.get("budget")})
    return 1


# ---------------------------------------------------------------------------
# 第四批能力：报警历史(C4) / 自选到价提醒(B1) / 换月预警(B2) / 跳空风险(B3) /
#            分品种 edge(C2) / 波动率 regime(C3)
#   共同约束：只提醒 / 只调面板，绝不自动下单。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 增量 #132 组合预警规则引擎
#   把散落在各模块的风险信号（风险热度 / 仓位状态机 / 回撤水位线 / 硬熔断 / 日亏 /
#   连亏 / 单品集中度 / 持仓数 / 资金使用率 等）统一收口到「一套可配置规则」：
#   逐条评估 → 分级告警（🔵提示 / 🟡关注 / 🟠警告 / 🔴危险）→ 给出动作建议。
#   规则可在 trade_config.json 的 risk_rules 段覆盖（开/关、阈值、严重度、动作）；
#   引擎只读、只建议、绝不自动下单/减仓（与全系统半自动原则一致）。
# ---------------------------------------------------------------------------
RULE_SEVERITY_RANK = {"info": 0, "notice": 1, "warning": 2, "danger": 3}
RULE_SEVERITY_LABEL = {"info": "提示", "notice": "关注", "warning": "警告", "danger": "危险"}
RULE_SEVERITY_COLOR = {"info": "#3498db", "notice": "#f1c40f", "warning": "#e6a23c", "danger": "#e74c3c"}

DEFAULT_RISK_RULES = [
    {
        "id": "heat_over",
        "name": "组合风险预算超标",
        "category": "仓位",
        "metric": "heat_pct",
        "op": ">",
        "threshold": 100,
        "unit": "pct",
        "severity": "danger",
        "notify": False,
        "action": "立即停止加仓；优先减仓或把止损收紧至 1.2R 内，使计划风险回到预算内。",
    },
    {
        "id": "heat_watch",
        "name": "组合风险预算接近上限",
        "category": "仓位",
        "metric": "heat_pct",
        "op": ">",
        "threshold": 80,
        "unit": "pct",
        "severity": "warning",
        "notify": True,
        "action": "风险已到警戒线（>80%），新开仓手数减半，预留安全垫。",
    },
    {
        "id": "kill_halted",
        "name": "组合级硬熔断触发",
        "category": "熔断",
        "metric": "halted",
        "op": "==",
        "threshold": True,
        "unit": "bool",
        "severity": "danger",
        "notify": False,
        "action": "硬熔断已触发：立即按系统生成的一键全平清单平仓，停机等待人工解除。",
    },
    {
        "id": "fsm_locked",
        "name": "仓位状态机锁定",
        "category": "熔断",
        "metric": "fsm_state",
        "op": "in",
        "threshold": ["LOCKED", "HALTED"],
        "unit": "str",
        "severity": "danger",
        "notify": False,
        "action": "状态机已锁死，禁止一切新开仓；若 HALTED 须人工按清单全平并解除熔断。",
    },
    {
        "id": "fsm_warning",
        "name": "仓位状态机预警",
        "category": "熔断",
        "metric": "fsm_state",
        "op": "==",
        "threshold": "WARNING",
        "unit": "str",
        "severity": "warning",
        "notify": True,
        "action": "状态机预警（WARNING），新开仓手数按 0.5× 缩放，控制节奏。",
    },
    {
        "id": "daily_loss",
        "name": "当日亏损逼近停机线",
        "category": "纪律",
        "metric": "daily_loss_pct",
        "op": ">",
        "threshold": 0.05,
        "unit": "frac",
        "severity": "danger",
        "notify": False,
        "action": "当日亏损已破 5% 软停机线，状态机锁定；减仓或停手，勿摊平。",
    },
    {
        "id": "daily_loss_warn",
        "name": "当日亏损预警线",
        "category": "纪律",
        "metric": "daily_loss_pct",
        "op": ">",
        "threshold": 0.03,
        "unit": "frac",
        "severity": "warning",
        "notify": True,
        "action": "当日亏损超 3%，收紧止损、降低新开仓手数。",
    },
    {
        "id": "consec_losses",
        "name": "连续止损笔数过多",
        "category": "纪律",
        "metric": "consec_losses",
        "op": ">=",
        "threshold": 2,
        "unit": "int",
        "severity": "warning",
        "notify": True,
        "action": "连续止损≥2 笔，软警告；≥3 笔当日冻结（禁新开，跨日自动解除）。",
    },
    {
        "id": "drawdown",
        "name": "账户回撤触及水位线",
        "category": "回撤",
        "metric": "dd_pct",
        "op": ">=",
        "threshold": 5.0,
        "unit": "pct",
        "severity": "warning",
        "notify": True,
        "action": "账户自峰值回撤≥5%，新开仓手数按水位线系数缩放（5%→0.7 / 10%→0.5 / 15%→0）。",
    },
    {
        "id": "single_concentration",
        "name": "单品保证金占比超限",
        "category": "暴露",
        "metric": "max_margin_pct",
        "op": ">",
        "threshold": 30,
        "unit": "pct",
        "severity": "warning",
        "notify": True,
        "action": "单一品种保证金占比超 30%，过度集中；分散或减仓。",
    },
    {
        "id": "usage_high",
        "name": "资金使用率偏高",
        "category": "仓位",
        "metric": "usage_rate",
        "op": ">",
        "threshold": 60,
        "unit": "pct",
        "severity": "warning",
        "notify": True,
        "action": "组合资金使用率>60%，接近组合上限；控制新开仓规模。",
    },
    {
        "id": "position_count",
        "name": "持仓品种过多",
        "category": "暴露",
        "metric": "n_positions",
        "op": ">",
        "threshold": 8,
        "unit": "int",
        "severity": "notice",
        "notify": True,
        "action": "持仓≥8 个品种，注意力分散、相关性难控；聚焦核心 3–5 个。",
    },
    # ── v5 新增：7大量化策略研究结论 ──
    {
        "id": "dd_circuit_breaker",
        "name": "10%回撤硬熔断",
        "category": "熔断",
        "metric": "dd_pct",
        "op": ">=",
        "threshold": 10.0,
        "unit": "pct",
        "severity": "danger",
        "notify": False,
        "action": "10%回撤触发硬熔断：全平所有仓位 + 强制休息24小时 + 重启风险评估。",
    },
    {
        "id": "consec_loss_hard",
        "name": "连亏5次硬冻结",
        "category": "纪律",
        "metric": "consec_losses",
        "op": ">=",
        "threshold": 5,
        "unit": "int",
        "severity": "danger",
        "notify": False,
        "action": "连亏5次触发硬冻结：当日禁止所有新开仓，次日自动解除。",
    },
    {
        "id": "per_trade_risk",
        "name": "单笔风险超1%上限",
        "category": "仓位",
        "metric": "risk_pct",
        "op": ">",
        "threshold": 1.0,
        "unit": "pct",
        "severity": "warning",
        "notify": True,
        "action": "单笔风险超1%上限，应缩手数或调整止损，控制单笔风险在1%以内。",
    },
    {
        "id": "time_stop_check",
        "name": "持仓超时检查",
        "category": "纪律",
        "metric": "position_count",
        "op": ">",
        "threshold": 0,
        "unit": "int",
        "severity": "notice",
        "notify": True,
        "action": "有持仓超过5天未达T1，应检查是否继续持有或减仓。",
    },
    # ── v5.1 新增：分级止损规则 ──
    {
        "id": "tiered_stop_loss",
        "name": "分级止损联动",
        "category": "止损",
        "metric": "stop_level",
        "op": "auto",
        "threshold": None,
        "unit": "level",
        "severity": "info",
        "notify": True,
        "action": "根据浮盈自动调整止损级别：初始→保本→移动→硬止损，实现止损级联保护。",
    },
]


def _load_risk_rules():
    """内置 DEFAULT_RISK_RULES 与 trade_config.json 的 risk_rules 段合并。
    覆盖字段：on(开关)/threshold/severity/action/notify；支持 extra 自定义规则列表。"""
    cfg = {}
    try:
        cfg = at.load_config().get("risk_rules", {}) or {}
    except Exception:
        cfg = {}
    rules = []
    overrides = cfg.get("overrides", {}) or {}
    for d in DEFAULT_RISK_RULES:
        rd = dict(d)
        ov = overrides.get(d["id"])
        if isinstance(ov, dict):
            for k in ("on", "threshold", "severity", "action", "notify"):
                if k in ov and ov[k] is not None:
                    rd[k] = ov[k]
        if rd.get("on", True):
            rules.append(rd)
    for c in cfg.get("extra") or []:
        if isinstance(c, dict) and c.get("id") and c.get("metric") and c.get("op"):
            c.setdefault("name", c["id"])
            c.setdefault("category", "自定义")
            c.setdefault("severity", "warning")
            c.setdefault("unit", "num")
            c.setdefault("notify", True)
            c.setdefault("action", "")
            c.setdefault("on", True)
            if c["on"]:
                rules.append(c)
    return rules


def _fmt_rule_value(v, unit):
    try:
        if unit == "pct":
            return f"{float(v):.1f}%"
        if unit == "frac":
            return f"{float(v) * 100:.1f}%"
        if unit == "int":
            return str(int(round(float(v))))
        if unit == "bool":
            return "是" if v else "否"
        if unit == "str":
            return str(v)
        return str(v)
    except Exception:
        return "—"


def _risk_rule_context(prices=None):
    """采集规则引擎所需的全部上下文指标（复用既有模块，不重复计算口径）。"""
    ctx = {}
    try:
        snap = at.snapshot(prices)
    except Exception:
        snap = {}
    ctx["equity"] = snap.get("equity") or 0
    ctx["total_margin"] = snap.get("total_margin") or 0
    ctx["usage_rate"] = snap.get("usage_rate")
    ctx["risk_pct"] = snap.get("risk_pct", 1.5)
    ctx["margin_cap_pct"] = snap.get("margin_cap_pct", 30)
    positions = [p for p in (snap.get("positions") or []) if p.get("lots")]
    ctx["n_positions"] = len(positions)
    ctx["max_margin_pct"] = max([(p.get("margin_pct") or 0) for p in positions], default=0)
    try:
        h = compute_heat(prices)
    except Exception:
        h = {}
    ctx["heat_pct"] = h.get("heat_pct")
    ctx["heat_status"] = h.get("status")
    try:
        fsm = rsm.RISK_FSM.summary()
    except Exception:
        fsm = {}
    ctx["fsm_state"] = fsm.get("state")
    ctx["scale"] = fsm.get("scale")
    ctx["consec_losses"] = fsm.get("consec_losses")
    ctx["daily_loss_pct"] = fsm.get("daily_loss_pct")
    try:
        dd = ddg.current()
    except Exception:
        dd = {}
    ctx["dd_pct"] = dd.get("dd_pct")
    ctx["dd_tier"] = dd.get("tier")
    try:
        ks = rsm.KILL.summary()
    except Exception:
        ks = {}
    ctx["halted"] = bool(ks.get("halted"))
    ctx["kill_triggers"] = ks.get("triggers", [])
    return ctx


def _eval_rule(rule, ctx):
    """单条规则评估：返回带展示字段的 dict，或 None（指标不可得）。"""
    m = rule.get("metric")
    if m not in ctx or ctx[m] is None:
        return None
    v = ctx[m]
    op = rule.get("op")
    thr = rule.get("threshold")
    try:
        if op in (">", "gt"):
            hit = float(v) > float(thr)
        elif op in (">=", "ge"):
            hit = float(v) >= float(thr)
        elif op in ("<", "lt"):
            hit = float(v) < float(thr)
        elif op in ("<=", "le"):
            hit = float(v) <= float(thr)
        elif op in ("==", "eq"):
            hit = v == thr
        elif op in ("!=", "ne"):
            hit = v != thr
        elif op == "in":
            hit = v in (thr or [])
        elif op == "not_in":
            hit = v not in (thr or [])
        else:
            hit = False
    except Exception:
        hit = False
    return {
        "id": rule["id"],
        "name": rule["name"],
        "category": rule.get("category"),
        "metric": m,
        "op": op,
        "threshold": thr,
        "threshold_disp": _fmt_rule_value(thr, rule.get("unit", "num")),
        "value": v,
        "value_disp": _fmt_rule_value(v, rule.get("unit", "num")),
        "severity": rule.get("severity", "warning"),
        "severity_label": RULE_SEVERITY_LABEL.get(rule.get("severity", "warning")),
        "action": rule.get("action", ""),
        "notify": rule.get("notify", True),
        "triggered": bool(hit),
    }


_RULE_NOTIFY_STATE = {}


def _risk_rules_notify(triggered):
    """节流写报警历史：规则「未触发→触发」且 notify=True 时记录一次（防每轮刷屏）。
    已存在专用播报的致命规则（heat_over/kill_halted/fsm_locked/daily_loss）notify=False，不重复。"""
    cur = {e["id"]: True for e in triggered if e.get("notify", True)}
    for e in triggered:
        rid = e["id"]
        was = _RULE_NOTIFY_STATE.get(rid, False)
        if e.get("notify", True) and not was:
            try:
                log_alert(
                    "规则引擎",
                    None,
                    e["name"],
                    f"[{e['severity_label']}] {e['name']}：当前 {e['value_disp']} "
                    f"触发阈值 {e['threshold_disp']}。建议：{e['action']}",
                    {"rule_id": rid, "severity": e["severity"], "value": e["value"], "threshold": e["threshold"]},
                )
            except Exception:
                pass
    for rid in list(_RULE_NOTIFY_STATE.keys()):
        if rid not in cur:
            _RULE_NOTIFY_STATE.pop(rid, None)
    _RULE_NOTIFY_STATE.update(cur)


_RISK_RULES_CACHE = {"t": 0, "v": None}


# ---------------------------------------------------------------------------
# #133 交互式历史回放（时间机器）
# 基于交易记录 / 信号历史 / 报警历史，重建 [最早事件, 最晚事件] 区间内每一自然日的组合状态帧，
# 供前端滑块逐日回放 + 播放动画。市值重估(mark-to-market)用日线收盘，best-effort（离线则仅算已实现）。
# ---------------------------------------------------------------------------
_PLAYBACK_CACHE = {"t": 0, "v": None}
_PLAYBACK_BASE_EQUITY = 500000.0  # 可由 trade_config.json 的 playback_base_equity 覆盖


def _pb_parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _pb_load_trades():
    out = []
    try:
        data = json.load(open(tj.JOURNAL_FILE, encoding="utf-8")) or {}
        for t in data.get("trades", []):
            out.append(
                {
                    "id": t.get("id"),
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction"),
                    "lots": t.get("lots"),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "exit_reason": t.get("exit_reason"),
                    "pnl": t.get("pnl"),
                    "signal_id": t.get("signal_id", ""),
                    "entry_dt": _pb_parse_dt(t.get("time")),
                    "exit_dt": _pb_parse_dt(t.get("exit_time") or ""),
                }
            )
    except Exception:
        pass
    return out


def _pb_load_events():
    evs = []
    for t in _pb_load_trades():
        if t["entry_dt"]:
            evs.append(
                {
                    "dt": t["entry_dt"],
                    "date": t["entry_dt"].strftime("%Y-%m-%d"),
                    "type": "entry",
                    "symbol": t["symbol"],
                    "dir": t["direction"],
                    "detail": "开仓 %s%s手 @%s（信号:%s）"
                    % (t["direction"], t["lots"], t["entry_price"], (t["signal_id"] or "")[:22]),
                }
            )
        if t["exit_dt"]:
            evs.append(
                {
                    "dt": t["exit_dt"],
                    "date": t["exit_dt"].strftime("%Y-%m-%d"),
                    "type": "exit",
                    "symbol": t["symbol"],
                    "dir": t["direction"],
                    "detail": "平仓 %s%s手 @%s 盈亏=%s（%s）"
                    % (t["direction"], t["lots"], t["exit_price"], t["pnl"], t["exit_reason"] or ""),
                }
            )
    try:
        for s in json.load(open(SIGNAL_LOG, encoding="utf-8")) or []:
            st = _pb_parse_dt(s.get("time"))
            if st:
                evs.append(
                    {
                        "dt": st,
                        "date": st.strftime("%Y-%m-%d"),
                        "type": "signal",
                        "symbol": s.get("symbol"),
                        "dir": s.get("direction"),
                        "detail": "信号 %s/%s %s 参考价%s 止损%s"
                        % (s.get("name", ""), s.get("symbol"), s.get("direction"), s.get("entry_ref"), s.get("stop")),
                    }
                )
    except Exception:
        pass
    try:
        for a in json.load(open(ALERT_HISTORY_FILE, encoding="utf-8")) or []:
            at_ = _pb_parse_dt(a.get("time"))
            if at_:
                evs.append(
                    {
                        "dt": at_,
                        "date": at_.strftime("%Y-%m-%d"),
                        "type": "alert",
                        "symbol": a.get("symbol"),
                        "dir": None,
                        "detail": "[%s] %s" % (a.get("kind", ""), (a.get("text") or "")[:70]),
                    }
                )
    except Exception:
        pass
    evs.sort(key=lambda e: e["dt"])
    return evs


def build_playback_timeline(force=False, cache_sec=60):
    """重建历史组合状态帧序列（时间机器核心）。"""
    now = time.time()
    if not force and _PLAYBACK_CACHE["v"] is not None and now - _PLAYBACK_CACHE["t"] < cache_sec:
        return _PLAYBACK_CACHE["v"]
    cfg = at.load_config()
    base = float(cfg.get("playback_base_equity", _PLAYBACK_BASE_EQUITY))
    events = _pb_load_events()
    if not events:
        out = {
            "ok": True,
            "base_equity": base,
            "symbols": [],
            "date_range": [None, None],
            "n_dates": 0,
            "frames": [],
            "note": "暂无交易/信号/报警历史",
        }
        _PLAYBACK_CACHE["t"] = now
        _PLAYBACK_CACHE["v"] = out
        return out
    d0 = min(e["date"] for e in events)
    d1 = max(e["date"] for e in events)
    start = datetime.strptime(d0, "%Y-%m-%d").date()
    end = datetime.strptime(d1, "%Y-%m-%d").date()
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    by_date = {}
    for e in events:
        by_date.setdefault(e["date"], []).append(e)
    trades = _pb_load_trades()
    specs = cfg.get("contract_specs", {})
    close_map = {}
    for sym in set(t["symbol"] for t in trades if t.get("symbol")):
        try:
            df = load_daily_refreshed(sym)
            if df is not None and len(df):
                cm = {}
                for idx, row in df.iterrows():
                    d = str(idx)[:10]
                    c = row.get("close")
                    if c is not None:
                        cm[d] = float(c)
                close_map[sym] = cm
        except Exception:
            pass
    frames = []
    for date in dates:
        realized = 0.0
        open_pos = []
        for t in trades:
            if not t["entry_dt"]:
                continue
            if t["entry_dt"].strftime("%Y-%m-%d") <= date:
                if t["exit_dt"] and t["exit_dt"].strftime("%Y-%m-%d") <= date:
                    if t["pnl"] is not None:
                        realized += float(t["pnl"])
                else:
                    mult = specs.get(t["symbol"], {}).get("multiplier", 10)
                    ds = 1 if t["direction"] == "多" else -1
                    close_at = close_map.get(t["symbol"], {}).get(date)
                    run = None
                    if close_at is not None and t["entry_price"] is not None:
                        run = round((close_at - float(t["entry_price"])) * mult * int(t["lots"]) * ds, 2)
                    open_pos.append(
                        {
                            "symbol": t["symbol"],
                            "dir": t["direction"],
                            "lots": t["lots"],
                            "entry": t["entry_price"],
                            "close_at_date": close_at,
                            "running_pnl": run,
                        }
                    )
        running_mtm = (
            round(sum(p["running_pnl"] for p in open_pos if p["running_pnl"] is not None), 2) if open_pos else 0.0
        )
        evs = by_date.get(date, [])
        frames.append(
            {
                "date": date,
                "realized_pnl_to_date": round(realized, 2),
                "running_mtm_pnl": running_mtm,
                "equity_to_date": round(base + realized + running_mtm, 2),
                "n_open": len(open_pos),
                "open_positions": open_pos,
                "events": [
                    {"type": e["type"], "symbol": e["symbol"], "dir": e["dir"], "detail": e["detail"]} for e in evs
                ],
                "n_signals": sum(1 for e in evs if e["type"] == "signal"),
                "n_entries": sum(1 for e in evs if e["type"] == "entry"),
                "n_exits": sum(1 for e in evs if e["type"] == "exit"),
                "n_alerts": sum(1 for e in evs if e["type"] == "alert"),
            }
        )
    symbols = sorted(set(e["symbol"] for e in events if e.get("symbol")))
    out = {
        "ok": True,
        "base_equity": base,
        "symbols": symbols,
        "date_range": [d0, d1],
        "n_dates": len(dates),
        "frames": frames,
    }
    _PLAYBACK_CACHE["t"] = now
    _PLAYBACK_CACHE["v"] = out
    return out


def playback_kline(symbol, asof=None):
    """返回某品种截至 asof 的日线(OHLC) + 交易记录开/平仓标记，best-effort。"""
    try:
        df = load_daily_refreshed(symbol)
        if df is None or not len(df):
            return {"ok": False, "reason": "no_daily_data"}
        bars = []
        for idx, row in df.iterrows():
            d = str(idx)[:10]
            if asof and d > asof:
                continue
            bars.append(
                {
                    "date": d,
                    "o": float(row.get("open")),
                    "h": float(row.get("high")),
                    "l": float(row.get("low")),
                    "c": float(row.get("close")),
                    "v": float(row.get("volume") or 0),
                }
            )
        marks = []
        for t in _pb_load_trades():
            if t["symbol"] != symbol:
                continue
            if t["entry_dt"]:
                marks.append(
                    {
                        "type": "entry",
                        "date": t["entry_dt"].strftime("%Y-%m-%d"),
                        "price": t["entry_price"],
                        "dir": t["direction"],
                    }
                )
            if t["exit_dt"]:
                marks.append(
                    {
                        "type": "exit",
                        "date": t["exit_dt"].strftime("%Y-%m-%d"),
                        "price": t["exit_price"],
                        "dir": t["direction"],
                    }
                )
        return {
            "ok": True,
            "symbol": symbol,
            "asof": asof,
            "bars": bars,
            "marks": marks,
            "events": _pb_load_events(symbol, asof),
        }
    except Exception as e:
        return {"ok": False, "reason": repr(e)[:80]}


def _pb_load_events(symbol, asof, state=None):
    """F7 事件驱动标记：聚合与某品种相关的事件（消息流 / 异动 / 信号）按日对齐 K 线。
    best-effort，单项缺数据不崩。返回 [{date, symbol, type, label, detail}]。"""
    events = []
    try:
        _asof_day = (asof or "")[:10]
        # 修复：回放历史日时聚合截至 asof 的所有有数据日的消息流（原仅读今天，历史事件全丢）
        _days = list_chat_days()
        if _asof_day:
            _days = [d for d in _days if d <= _asof_day]
        if not _days:
            _days = [None]  # 回退今天
        _name2sym = {str(v.get("name", "")): k for k, v in SYMBOLS.items()}
        for _day in _days:
            for it in load_chat_feed(_day, limit=None):
                sym = (it.get("symbol") or "").strip()
                # symbol 字段精确匹配；缺 symbol 时按 name 反查品种（兼容历史无 symbol 字段的条目）
                if symbol:
                    if sym and sym != symbol:
                        continue
                    if not sym and _name2sym.get(str(it.get("name", ""))) != symbol:
                        continue
                t = (it.get("time") or "")[:10]
                if not t:
                    continue
                if asof and t > asof:
                    continue
                kind = it.get("kind") or "news"
                label = it.get("name") or it.get("alert_label") or it.get("alert_type") or kind
                events.append(
                    {
                        "date": t,
                        "symbol": sym or symbol,
                        "type": kind,
                        "label": str(label),
                        "detail": it.get("reason") or "",
                    }
                )
    except Exception:
        pass
    if state is not None:
        try:
            _an = state.get("anomaly", {}) or {}
            for a in _an.get("anomalies") or []:
                if symbol and (a.get("symbol") or "") != symbol:
                    continue
                t = (a.get("time") or a.get("date") or "")[:10]
                if not t or (asof and t > asof):
                    continue
                events.append(
                    {
                        "date": t,
                        "symbol": a.get("symbol") or symbol,
                        "type": "anomaly",
                        "label": a.get("type") or "异动",
                        "detail": a.get("detail") or "",
                    }
                )
        except Exception:
            pass
    # 按日期去重（同日同标签只留一条）
    seen = set()
    uniq = []
    for e in events:
        k = (e["date"], e["type"], e["label"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    uniq.sort(key=lambda x: x["date"])
    return uniq


# ---------------------------------------------------------------------------
# #134 参数鲁棒性敏感性地图（OAT 单因子敏感性）
# 对关键风控/仓位参数做「一次一变」扫描，跑 walk-forward 回测，量化每参数对组合期望R/累计R/胜率/笔数的
# 敏感性，输出敏感性排序 + 逐参数逐值指标曲线，供前端热力/曲线展示。回测较重 → 后台线程计算，结果缓存。
# ---------------------------------------------------------------------------
_SENS_CACHE = {"t": 0.0, "v": None, "running": False, "since": 0.0}
_SENS_LOCK = threading.Lock()

SENS_PARAMS = [
    {"key": "risk_gate.stop_atr_mult", "label": "止损ATR倍数", "base": 1.5, "values": [1.0, 1.25, 1.5, 2.0, 2.5]},
    {"key": "risk_gate.rr_ratio", "label": "盈亏比RR", "base": 2.0, "values": [1.0, 1.5, 2.0, 2.5, 3.0]},
    {"key": "account.risk_pct", "label": "单笔风险%", "base": 1.5, "values": [0.5, 1.0, 1.5, 2.0, 3.0]},
    {"key": "risk_gate.kelly_slope", "label": "Kelly斜率", "base": 2.0, "values": [1.0, 1.5, 2.0, 2.5, 3.0]},
]
SENS_FOCUS_DEFAULT = ["FG", "SA", "JM", "jd"]
FOCUS_SYMS = [
    "jd",
    "lh",
    "FG",
    "SA",
    "JM",
    "J",
]  # 固定关注的 6 品种（2026-08-19 22:35 恢复：15:17 部署时误删定义，导致 evaluate 全部 NameError 跳过、夜盘零信号）


def _sens_deepcopy(cfg):
    try:
        return json.loads(json.dumps(cfg))
    except Exception:
        return dict(cfg)


def _sens_set_path(cfg, key, val):
    parts = key.split(".")
    cur = cfg
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = val


def _sens_agg_backtest(symbols, cfg, tail):
    """对一组品种跑 walk-forward，聚合组合期望R/累计R/笔数/胜率。"""
    total_R = 0.0
    n_trades = 0
    wins = 0
    for s in symbols:
        try:
            r = fd.walk_forward_backtest(s, cfg, tail=tail)
        except Exception:
            r = {"trades": 0}
        if r.get("trades"):
            total_R += r["expR"] * r["trades"]
            n_trades += r["trades"]
            wins += int(round(r["win_rate"] * r["trades"]))
    expR = total_R / n_trades if n_trades else 0.0
    win_rate = wins / n_trades if n_trades else 0.0
    return {"expR": round(expR, 4), "total_R": round(total_R, 2), "n_trades": n_trades, "win_rate": round(win_rate, 3)}


def _sens_run_thread(symbols, tail):
    """后台执行 OAT 敏感性扫描，结果写入 _SENS_CACHE。"""
    try:
        cfg0 = _sens_deepcopy(fd.DEFAULT_CONFIG)
        try:
            live = at.load_config() or {}
            for k, v in live.items():
                if isinstance(v, dict) and isinstance(cfg0.get(k), dict):
                    cfg0[k].update(v)
                else:
                    cfg0[k] = v
        except Exception:
            pass
        base_metrics = _sens_agg_backtest(symbols, cfg0, tail)
        params_out = []
        for p in SENS_PARAMS:
            series = []
            for val in p["values"]:
                cfg = _sens_deepcopy(cfg0)
                _sens_set_path(cfg, p["key"], val)
                m = _sens_agg_backtest(symbols, cfg, tail)
                series.append(
                    {
                        "value": val,
                        "is_base": val == p["base"],
                        "expR": m["expR"],
                        "total_R": m["total_R"],
                        "n_trades": m["n_trades"],
                        "win_rate": m["win_rate"],
                    }
                )
            exprs = [s["expR"] for s in series]
            rng = max(exprs) - min(exprs)
            base_s = next((s for s in series if s["is_base"]), None)
            base_expR = base_s["expR"] if base_s else 0.0
            sens = rng / max(0.01, abs(base_expR)) if base_expR else 0.0
            robust = "稳健" if sens < 0.3 else ("中等" if sens < 0.8 else "敏感")
            params_out.append(
                {
                    "key": p["key"],
                    "label": p["label"],
                    "base": p["base"],
                    "sensitivity": round(sens, 3),
                    "robust": robust,
                    "series": series,
                }
            )
        params_out.sort(key=lambda x: -x["sensitivity"])
        result = {
            "ok": True,
            "status": "done",
            "focus_symbols": symbols,
            "base_metrics": base_metrics,
            "params": params_out,
        }
        _SENS_CACHE["v"] = result
        _SENS_CACHE["t"] = time.time()
    except Exception as e:
        _SENS_CACHE["v"] = {"ok": False, "status": "error", "error": repr(e)[:120]}
    finally:
        _SENS_CACHE["running"] = False


def parameter_sensitivity(force=False, symbols=None, tail=200, cache_sec=1800):
    """参数鲁棒性敏感性地图主入口（#134）。首次调用后台计算，前端轮询到 done。"""
    now = time.time()
    with _SENS_LOCK:
        if _SENS_CACHE["running"]:
            return {"ok": True, "status": "running", "elapsed": round(now - _SENS_CACHE["since"], 1)}
        if not force and _SENS_CACHE["v"] is not None and now - _SENS_CACHE["t"] < cache_sec:
            return _SENS_CACHE["v"]
        syms = [s for s in (symbols or SENS_FOCUS_DEFAULT) if s in fd.SYMBOLS]
        if not syms:
            return {"ok": False, "status": "error", "error": "无有效品种"}
        _SENS_CACHE["running"] = True
        _SENS_CACHE["since"] = now
        t = threading.Thread(target=_sens_run_thread, args=(syms, tail), daemon=True)
        t.start()
        return {"ok": True, "status": "running", "elapsed": 0.0}


def evaluate_risk_rules(force=False, prices=None, cache_sec=30):
    """组合预警规则引擎主入口（#132）。返回分级评估 dict，供 /api/risk_rules 与面板。"""
    now = time.time()
    if not force and _RISK_RULES_CACHE["v"] is not None and now - _RISK_RULES_CACHE["t"] < cache_sec:
        return _RISK_RULES_CACHE["v"]
    ctx = _risk_rule_context(prices)
    rules = _load_risk_rules()
    evals, triggered = [], []
    worst_rank = -1
    top_action = ""
    for r in rules:
        e = _eval_rule(r, ctx)
        if e is None:
            continue
        evals.append(e)
        if e["triggered"]:
            triggered.append(e)
            rk = RULE_SEVERITY_RANK.get(e["severity"], 2)
            if rk > worst_rank:
                worst_rank = rk
                top_action = e["action"]
    grade, grade_color = "正常", "#39b54a"
    if worst_rank >= 3:
        grade, grade_color = "危险", RULE_SEVERITY_COLOR["danger"]
    elif worst_rank == 2:
        grade, grade_color = "警告", RULE_SEVERITY_COLOR["warning"]
    elif worst_rank == 1:
        grade, grade_color = "关注", RULE_SEVERITY_COLOR["notice"]
    elif worst_rank == 0:
        grade, grade_color = "提示", RULE_SEVERITY_COLOR["info"]
    out = {
        "ok": True,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "grade": grade,
        "grade_color": grade_color,
        "n_rules": len(evals),
        "n_triggered": len(triggered),
        "context": {
            "equity": round(ctx.get("equity") or 0, 2),
            "usage_rate": ctx.get("usage_rate"),
            "heat_pct": ctx.get("heat_pct"),
            "fsm_state": ctx.get("fsm_state"),
            "dd_pct": ctx.get("dd_pct"),
            "daily_loss_pct": ctx.get("daily_loss_pct"),
            "consec_losses": ctx.get("consec_losses"),
            "n_positions": ctx.get("n_positions"),
            "max_margin_pct": ctx.get("max_margin_pct"),
            "halted": ctx.get("halted"),
        },
        "rules": evals,
        "triggered_rules": triggered,
        "top_action": top_action,
    }
    try:
        _risk_rules_notify(triggered)
    except Exception:
        pass
    _RISK_RULES_CACHE["t"] = now
    _RISK_RULES_CACHE["v"] = out
    return out


ALERT_HISTORY_FILE = os.path.join(HERE, "alert_history.json")
ALERT_HISTORY_MAX = 300
ALERT_KINDS = ["触价", "移动止损", "自选到价", "跳空风险", "风险超标", "规则引擎"]


def log_alert(kind, symbol=None, name=None, text="", extra=None):
    """统一报警落盘（C4）：所有报警（触价三档 / 移动止损 / 自选到价 / 跳空 / 风险超标）
    都写入 alert_history.json（最多保留 300 条），供面板「预警」页回看。"""
    rec = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "symbol": symbol,
        "name": name or (SYMBOLS.get(symbol, {}).get("name", symbol) if symbol else None),
        "text": text,
    }
    if extra:
        rec.update(extra)
    try:
        arr = []
        if os.path.exists(ALERT_HISTORY_FILE):
            try:
                arr = json.load(open(ALERT_HISTORY_FILE, encoding="utf-8")) or []
            except Exception:
                arr = []
        arr.insert(0, rec)
        tmp = ALERT_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(arr[:ALERT_HISTORY_MAX], f, ensure_ascii=False, default=_json_default, indent=1)
        os.replace(tmp, ALERT_HISTORY_FILE)
    except Exception:
        pass
    return rec


def load_alerts(kind=None, limit=120):
    """读取报警历史（C4）：可按类型过滤，并返回各类型计数供面板筛选。"""
    try:
        arr = json.load(open(ALERT_HISTORY_FILE, encoding="utf-8")) or []
    except Exception:
        arr = []
    kinds = {}
    for a in arr:
        k = a.get("kind") or "其他"
        kinds[k] = kinds.get(k, 0) + 1
    total_all = len(arr)
    if kind and kind not in ("", "全部"):
        arr = [a for a in arr if a.get("kind") == kind]
    return {
        "items": arr[:limit],
        "total": total_all,
        "shown": min(len(arr), limit),
        "kinds": kinds,
        "all_kinds": ALERT_KINDS,
    }


# —— B1 自选到价提醒（非持仓品种也能挂条件）——
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")


def load_watchlist():
    try:
        d = json.load(open(WATCHLIST_FILE, encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def save_watchlist(wl):
    try:
        tmp = WATCHLIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, default=_json_default, indent=1)
        os.replace(tmp, WATCHLIST_FILE)
        return True
    except Exception:
        return False


def watch_add(symbol, op, price, note=""):
    """新增一条到价提醒：op=above(上破) / below(下破)。"""
    symbol = (symbol or "").strip()
    if symbol not in SYMBOLS:
        return False, f"未知品种 {symbol}"
    if op not in ("above", "below"):
        return False, "方向须为 above(上破)/below(下破)"
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False, "价格无效"
    if price <= 0:
        return False, "价格须大于 0"
    wl = load_watchlist()
    if len(wl["items"]) >= 60:
        return False, "到价提醒最多 60 条，请先删除部分"
    nm = SYMBOLS.get(symbol, {}).get("name", symbol)
    wl["items"].append(
        {
            "id": f"w{int(time.time() * 1000) % 1000000000}",
            "symbol": symbol,
            "name": nm,
            "op": op,
            "price": price,
            "note": (note or "")[:40],
            "enabled": True,
            "fired": False,
            "fired_time": None,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_watchlist(wl)
    return True, f"已添加：{nm} {'上破' if op == 'above' else '下破'} {price}"


def watch_update(wid, action):
    """对某条提醒执行 remove(删除) / reset(清除已触发·重新生效) / toggle(启停)。"""
    wl = load_watchlist()
    items = wl.get("items") or []
    idx = next((i for i, it in enumerate(items) if it.get("id") == wid), None)
    if idx is None:
        return False, "未找到该提醒"
    it = items[idx]
    if action == "remove":
        items.pop(idx)
        msg = f"已删除 {it.get('name')} 的提醒"
    elif action == "reset":
        it["fired"] = False
        it["fired_time"] = None
        it["enabled"] = True
        msg = f"{it.get('name')} 提醒已重新生效"
    elif action == "toggle":
        it["enabled"] = not it.get("enabled", True)
        msg = f"{it.get('name')} 提醒已{'启用' if it['enabled'] else '暂停'}"
    else:
        return False, f"未知动作 {action}"
    save_watchlist(wl)
    return True, msg


_WATCH_LAST_CHECK = 0.0


def check_watch_alerts():
    """自选到价提醒（B1）：每轮检查实盘价是否触及条件；命中即弹窗+语音+消息+报警历史。
    触发后置 fired 不再重复，可在面板点「重置」后再次生效；非该品种交易时段不误报。"""
    wl = load_watchlist()
    items = wl.get("items") or []
    if not items:
        return 0
    fired_n = 0
    dirty = False
    for it in items:
        if not it.get("enabled", True) or it.get("fired"):
            continue
        sym = it.get("symbol")
        if sym not in SYMBOLS or not _in_session(sym):
            continue
        px = FEED.price(sym) if FEED else None
        if px is None:
            continue
        try:
            px = float(px)
            lvl = float(it["price"])
        except (TypeError, ValueError):
            continue
        hit = (px >= lvl) if it["op"] == "above" else (px <= lvl)
        if not hit:
            continue
        it["fired"] = True
        it["fired_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dirty = True
        fired_n += 1
        nm = it.get("name") or sym
        arrow = "上破" if it["op"] == "above" else "下破"
        note = f"（备注：{it['note']}）" if it.get("note") else ""
        text = (
            f"自选到价提醒触发：{nm} 实盘价 {_fmt_price(px)} 已{arrow}你设的 "
            f"{_fmt_price(lvl)}{note}。仅到价提醒，是否入场仍看四维信号与风控。"
        )
        sig = {
            "name": nm,
            "direction": "关注",
            "lots": 0,
            "price": px,
            "stop": None,
            "target": None,
            "t1": None,
            "t2": None,
            "alert_type": "自选到价",
            "alert_label": f"{arrow}价位",
            "alert_level": _fmt_price(lvl),
            "reason": text,
        }
        # 2026-08-20: add main contract code
        if not sig.get("contract") and sig.get("symbol"):
            try:
                _auth = ml._authoritative_contracts()
                _code = _auth.get(sig["symbol"])
                if _code:
                    sig["contract"] = ml.normalize_contract_code(_code)
            except Exception:
                pass
        notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
        sig["kind"] = "alert"
        append_chat(sig)
        log_alert("自选到价", sym, nm, text, {"price": px, "level": lvl, "op": it["op"]})
    if dirty:
        save_watchlist(wl)
    return fired_n


def watch_view():
    """带当前价 / 距目标% / 是否在交易时段的自选列表，供面板展示。"""
    rows = []
    for it in load_watchlist().get("items") or []:
        sym = it.get("symbol")
        px = FEED.price(sym) if (FEED and sym in SYMBOLS) else None
        d = None
        if px is not None:
            try:
                d = round((float(px) - float(it["price"])) / float(it["price"]) * 100, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                d = None
        r = dict(it)
        r["price_now"] = px
        r["dist_pct"] = d
        r["in_session"] = _in_session(sym) if sym in SYMBOLS else False
        rows.append(r)
    rows.sort(key=lambda r: (r.get("fired") or False, r.get("symbol") or ""))
    return {
        "items": rows,
        "count": len(rows),
        "pending": sum(1 for r in rows if not r.get("fired") and r.get("enabled", True)),
    }


# —— B2 换月预警 ——
ROLL_WARN_DAYS = 15  # 距交割月首日 < 15 天：开始留意换月
ROLL_URGENT_DAYS = 7  # < 7 天：须换月（流动性下降 + 交割风险）


def _parse_contract_month(code):
    """从合约代码尾部 4 位 YYMM 解析交割年月：FG2609 → (2026, 9)。"""
    if not code:
        return None
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) < 4:
        return None
    try:
        yy = int(digits[-4:-2])
        mm = int(digits[-2:])
    except ValueError:
        return None
    if not 1 <= mm <= 12:
        return None
    return 2000 + yy, mm


def rollover_info(sym, contract=None):
    """换月预警（B2）：算持仓合约距交割月首日的天数，临近则提示换主力合约。"""
    code = ml.normalize_contract_code(contract or CONTRACT_MAP.get(sym) or sym)
    ym = _parse_contract_month(code)
    if not ym:
        return None
    y, m = ym
    try:
        first = datetime(y, m, 1).date()
    except ValueError:
        return None
    days = (first - datetime.now().date()).days
    if days <= 0:
        level, msg = "urgent", f"{code} 已进入交割月，务必立即换月/离场（个人客户不可交割）"
    elif days <= ROLL_URGENT_DAYS:
        level, msg = "urgent", f"{code} 距交割月仅 {days} 天，须尽快换月（流动性下降+交割风险）"
    elif days <= ROLL_WARN_DAYS:
        level, msg = "warn", f"{code} 距交割月 {days} 天，建议开始留意换到下一主力"
    else:
        level, msg = "ok", f"{code} 距交割月 {days} 天，无需换月"
    return {"contract": code, "delivery": f"{y}-{m:02d}", "days_left": days, "level": level, "msg": msg}


def rollover_overview():
    """全部持仓的换月状态汇总（B2），供预警页表格展示。
    附带「系统在用主力 vs 交易所真实主力」主动比对（mismatches）。"""
    st = at.load_state()
    rows = []
    for sym, p in (st.get("positions") or {}).items():
        if not p.get("lots"):
            continue
        info = rollover_info(sym) or {}
        rows.append(
            {
                "symbol": sym,
                "name": SYMBOLS.get(sym, {}).get("name", sym),
                "direction": p.get("direction"),
                "lots": p.get("lots"),
                **info,
            }
        )
    rows.sort(key=lambda r: r.get("days_left") if r.get("days_left") is not None else 999)
    urgent = [r for r in rows if r.get("level") == "urgent"]
    warn = [r for r in rows if r.get("level") == "warn"]
    mm = {}
    try:
        mm = rollover_mismatch_check()
    except Exception:
        mm = {"mismatches": [], "count": 0}
    return {
        "rows": rows,
        "urgent": len(urgent),
        "warn": len(warn),
        "note": f"距交割月 <{ROLL_URGENT_DAYS} 天须换月 · <{ROLL_WARN_DAYS} 天开始留意",
        "mismatches": mm.get("mismatches", []),
        "mm_count": mm.get("count", 0),
        "ak_checked_at": mm.get("checked_at"),
        "ak_error": mm.get("error"),
    }


# —— B3 跳空风险预警 ——
_GAP_CACHE = {}  # sym -> (ts, stats)  历史跳空统计缓存 30min
_GAP_ALERT_GUARD = set()  # "日期|窗口" 每个收盘前窗口只提醒一次


def _gap_stats(sym, lookback=120):
    """历史隔夜跳空幅度统计：|今开 − 昨收| / 昨收（%），取近 120 个交易日。"""
    now = time.time()
    c = _GAP_CACHE.get(sym)
    if c and now - c[0] < 1800:
        return c[1]
    out = None
    try:
        df = load_daily_refreshed(sym)
        if df is not None and len(df) > 30 and "open" in df.columns:
            d = df.iloc[-lookback:]
            prev_close = d["close"].shift(1)
            gap = ((d["open"] - prev_close).abs() / prev_close * 100).dropna()
            if len(gap) >= 20:
                out = {
                    "avg": round(float(gap.mean()), 2),
                    "p90": round(float(gap.quantile(0.9)), 2),
                    "max": round(float(gap.max()), 2),
                    "n": int(len(gap)),
                }
    except Exception:
        out = None
    _GAP_CACHE[sym] = (now, out)
    return out


def gap_risk(prices=None):
    """跳空风险预警（B3）：持仓过夜/过周末时，用历史跳空幅度对比止损距离，
    判断止损是否可能被跳空直接越过 → 提示减仓或收紧。仅提示，不自动下单。"""
    if prices is None:
        prices = {}
        if FEED:
            for sym in SYMBOLS:
                prices[sym] = FEED.price(sym)
    snap = at.snapshot(prices)
    now = datetime.now()
    t = now.hour * 60 + now.minute
    wd = now.weekday()
    overnight = "过周末" if (wd >= 5 or (wd == 4 and t >= 870)) else "过夜"
    if 870 <= t <= 900:
        phase = "日盘收盘前"
    elif 1350 <= t <= 1380:
        phase = "夜盘收盘前"
    elif is_trading_now(now):
        phase = "盘中"
    else:
        phase = "休市"
    rows = []
    counts = {"高": 0, "中": 0, "低": 0}
    for p in snap.get("positions", []):
        if not p.get("lots"):
            continue
        sym = p["symbol"]
        px = p.get("price")
        stop = p.get("stop")
        g = _gap_stats(sym)
        stop_pct = None
        if px and stop:
            try:
                stop_pct = round(abs(float(stop) - float(px)) / float(px) * 100, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                stop_pct = None
        level, tip = "—", "日线或止损缺失，无法评估"
        if g and stop_pct is not None:
            if g["p90"] >= stop_pct:
                level = "高"
                tip = f"90 分位跳空 {g['p90']}% ≥ 止损距 {stop_pct}%，{overnight}可能直接跳过止损，建议减仓或收紧止损"
            elif g["avg"] >= stop_pct * 0.6:
                level = "中"
                tip = f"平均跳空 {g['avg']}% 已接近止损距 {stop_pct}%，注意{overnight}风险"
            else:
                level = "低"
                tip = f"止损距 {stop_pct}% 明显大于常见跳空 {g['avg']}%，{overnight}风险可控"
            counts[level] = counts.get(level, 0) + 1
        rows.append(
            {
                "symbol": sym,
                "name": p.get("name", sym),
                "direction": p.get("direction"),
                "lots": p.get("lots"),
                "price": px,
                "stop": stop,
                "stop_pct": stop_pct,
                "gap_avg": (g or {}).get("avg"),
                "gap_p90": (g or {}).get("p90"),
                "gap_max": (g or {}).get("max"),
                "level": level,
                "tip": tip,
                "night": sym not in NO_NIGHT,
            }
        )
    order = {"高": 0, "中": 1, "低": 2, "—": 3}
    rows.sort(key=lambda r: order.get(r["level"], 9))
    if counts["高"]:
        summary = f"{counts['高']} 个持仓{overnight}跳空风险偏高，建议减仓或收紧止损"
    elif counts["中"]:
        summary = f"{counts['中']} 个持仓{overnight}跳空风险中等，留意隔夜缺口"
    elif rows:
        summary = f"当前持仓{overnight}跳空风险可控"
    else:
        summary = "当前无持仓，无跳空风险"
    return {
        "phase": phase,
        "overnight": overnight,
        "rows": rows,
        "counts": counts,
        "summary": summary,
        "note": "跳空统计=近120交易日 |今开−昨收|/昨收；p90=90分位（较极端情形）",
    }


def check_gap_alerts():
    """收盘前窗口（14:30–15:00 / 22:30–23:00）若有高跳空风险持仓，提醒一次（每窗口一次）。"""
    g = gap_risk()
    if g.get("phase") not in ("日盘收盘前", "夜盘收盘前"):
        return 0
    key = f"{datetime.now():%Y-%m-%d}|{g['phase']}"
    if key in _GAP_ALERT_GUARD:
        return 0
    highs = [r for r in g.get("rows", []) if r.get("level") == "高"]
    if not highs:
        return 0
    _GAP_ALERT_GUARD.add(key)
    rep = max(highs, key=lambda r: r.get("gap_p90") or 0)
    names = "、".join(r["name"] for r in highs[:4])
    text = (
        f"{g['phase']}提醒：{len(highs)} 个持仓{g['overnight']}跳空风险偏高（{names}）——"
        f"历史跳空幅度可能直接越过止损，建议减仓或收紧止损。"
    )
    sig = {
        "name": "跳空风险",
        "direction": "风控",
        "symbol": rep.get("symbol"),
        "lots": rep.get("lots"),
        "price": rep.get("price"),
        "stop": rep.get("stop"),
        "target": None,
        "t1": None,
        "t2": None,
        "alert_type": "跳空风险",
        "alert_label": g["overnight"],
        "alert_level": rep.get("stop"),
        "reason": text,
    }
    # 2026-08-20: add main contract code
    if not sig.get("contract") and sig.get("symbol"):
        try:
            _auth = ml._authoritative_contracts()
            _code = _auth.get(sig["symbol"])
            if _code:
                sig["contract"] = ml.normalize_contract_code(_code)
        except Exception:
            pass
    notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
    sig["kind"] = "alert"
    append_chat(sig)
    log_alert(
        "跳空风险",
        rep.get("symbol"),
        "跳空风险",
        text,
        {"count": len(highs), "phase": g["phase"], "overnight": g["overnight"]},
    )
    return len(highs)


def _build_gated_notices():
    """P1-②：为关注品种里被动态门控暂停发信号的生成定性提示卡（供 /api/state 下发前端）。"""
    out = []
    for sym in FOCUS_SYMS:
        try:
            g = sexp.explain_gated(sym)
        except Exception:
            g = None
        if g:
            out.append(g)
    return out


def _load_calib():
    if not os.path.exists(CALIB_FILE):
        return {}
    with open(CALIB_FILE, encoding="utf-8") as f:
        return json.load(f) or {}


# 波动率 regime 缓存（重构时遗漏定义，补回；vol_regime() 依赖）
_VOL_CACHE: dict = {}


def vol_regime(sym, lookback=120):
    """波动率 regime（C3）：当前日线 ATR% 在近 120 日分布中的分位 → 低/中/高/极高。
    高波动期同样的手数风险更大，用于提示是否该降手数。"""
    now = time.time()
    c = _VOL_CACHE.get(sym)
    if c and now - c[0] < 900:
        return c[1]
    out = None
    try:
        df = load_daily_refreshed(sym)
        if df is not None and len(df) >= 40:
            a = strat_atr(df, 14)
            atr_pct = (a / df["close"] * 100).dropna()
            if len(atr_pct) >= 30:
                win = atr_pct.iloc[-lookback:]
                cur = float(win.iloc[-1])
                pct = float((win <= cur).mean() * 100)
                label = "极高" if pct >= 85 else ("高" if pct >= 60 else ("中" if pct >= 25 else "低"))
                out = {
                    "atr_pct": round(cur, 2),
                    "percentile": round(pct, 0),
                    "regime": label,
                    "median": round(float(win.median()), 2),
                    "n": int(len(win)),
                }
    except Exception:
        out = None
    _VOL_CACHE[sym] = (now, out)
    return out


def variety_edge(with_vol=True):
    """分品种 edge（C2）+ 波动率 regime（C3）。
    edge 取校准文件里的滚动样本外期望 R（mean_oos）；波动率只对持仓+关注品种实算（控开销）。"""
    calib = _load_calib()
    st = at.load_state()
    held = {s for s, p in (st.get("positions") or {}).items() if p.get("lots")}
    focus = held | set(FOCUS_SYMS)
    rows = []
    for sym, meta in SYMBOLS.items():
        c = calib.get(sym) or {}
        mo = c.get("mean_oos")
        row = {
            "symbol": sym,
            "name": meta.get("name", sym),
            "mean_oos": (round(float(mo), 4) if mo is not None else None),
            "full_expR": c.get("full_expR"),
            "cur_full_expR": c.get("cur_full_expR"),
            "trades": c.get("total_trades"),
            "folds": c.get("folds"),
            "T_thresh": c.get("T_thresh"),
            "robust": bool(c.get("robust")),
            "note": c.get("note", ""),
            "disabled": sym in RUNTIME_DISABLED,
            "recoverable": sym in AUTO_RECOVER_SYMBOLS,
            "slip_pts": round(float(fd.get_slip_pts(sym, DEFAULT_CONFIG)), 2),
            "recalibrated": ("重校准" in (c.get("note", "") or "")),
            "held": sym in held,
            "focus": sym in focus,
            "vol": None,
        }
        if with_vol and sym in focus:
            row["vol"] = vol_regime(sym)
        rows.append(row)
    rows.sort(key=lambda r: (not r["held"], not r["focus"], -(r["mean_oos"] if r["mean_oos"] is not None else -9)))
    pos_edge = [r for r in rows if (r["mean_oos"] or 0) > 0 and not r["disabled"]]
    return {
        "rows": rows,
        "focus": sorted(focus),
        "held": sorted(held),
        "disabled": sorted(RUNTIME_DISABLED),
        "pos_edge_count": len(pos_edge),
        "note": "mean_oos=滚动样本外平均期望R（>0 才有正统计优势）；"
        "robust=false 或已屏蔽品种不出信号；波动率仅算持仓+关注 6 品种",
    }


# ---------------------------------------------------------------------------
# 龙虎榜 C_pos（席位持仓定位组）—— 对接 long_hu_bang.py 落盘的 cpos_cache.json
# ---------------------------------------------------------------------------
_CPOS_CACHE = None
_CPOS_MTIME = 0


def get_cpos_cache():
    """加载龙虎榜缓存；文件被每日自动化覆盖(mtime 变化)时自动重载。"""
    global _CPOS_CACHE, _CPOS_MTIME
    path = os.path.join(HERE, "cpos_cache.json")
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0
    if _CPOS_CACHE is None or mtime != _CPOS_MTIME:
        try:
            _CPOS_CACHE = json.load(open(path, encoding="utf-8"))
        except Exception:
            _CPOS_CACHE = {}
        _CPOS_MTIME = mtime
    return _CPOS_CACHE


def cpos_for(sym):
    """某品种龙虎榜 C_pos 摘要（无数据则 available=False）。"""
    cache = get_cpos_cache()
    rec = cache.get(sym.upper()) or cache.get(sym.lower())
    if not rec:
        return {"available": False}
    c = float(rec.get("C_score", 0.0) or 0.0)
    net = float(rec.get("net", 0.0) or 0.0)
    direction = "多" if net > 0 else ("空" if net < 0 else "中性")
    return {
        "available": True,
        "c_score": c,
        "net": net,
        "net_chg": float(rec.get("net_chg", 0.0) or 0.0),
        "direction": direction,
        "long_oi": int(rec.get("long_oi", 0) or 0),
        "short_oi": int(rec.get("short_oi", 0) or 0),
        "exchange": rec.get("exchange", ""),
        "trade_date": cache.get("_meta", {}).get("trade_date", ""),
    }


# 逐合约龙虎榜键（SA01→SA701）：让 01 取真实逐合约数据，不再用品种级 SA 失真
_CONTRACT_CPOS_KEY = {"SA01": "SA701"}


def contract_cpos_key(sym):
    return _CONTRACT_CPOS_KEY.get(sym, variety_of(sym))


def cpos_ranking(top_n=8, date=None):
    """席位态度榜：按 C_score 把有龙虎榜的品种排成偏多/偏空两榜。

    直接读 cpos_cache.json（不依赖 evaluate 是否已跑），且随文件重载。
    date=None → 用 _meta.trade_date（最新）；date='YYYYMMDD' → 回看该交易日历史。
    返回含 available_dates（可选历史交易日下拉）与 by_sym（按品种定位，供面板覆盖卡片）。
    bullish 按 C_score 降序（最偏多在前）；bearish 按 C_score 升序（最偏空在前）。
    """
    cache = get_cpos_cache()
    meta = cache.get("_meta", {})

    # 收集所有可用交易日（当前 + 各品种 history）
    avail = set()
    for sym in SYMBOLS:
        ckey = contract_cpos_key(sym).upper()
        rec = cache.get(ckey) or cache.get(ckey.lower())
        if not rec:
            continue
        if rec.get("date"):
            avail.add(str(rec["date"]))
        for h in rec.get("history", []):
            if h.get("date"):
                avail.add(str(h["date"]))
    avail_dates = sorted(avail, reverse=True)

    target = date or meta.get("trade_date", "")
    is_latest = (not date) or (str(target) == str(meta.get("trade_date", "")))

    rows = []
    for sym in SYMBOLS:
        ckey = contract_cpos_key(sym).upper()
        rec = cache.get(ckey) or cache.get(ckey.lower())
        if not rec:
            continue
        c = net = net_chg = None
        if str(rec.get("date", "")) == str(target):
            c = float(rec.get("C_score", 0.0) or 0.0)
            net = float(rec.get("net", 0.0) or 0.0)
            net_chg = float(rec.get("net_chg", 0.0) or 0.0)
        else:
            for h in rec.get("history", []):
                if str(h.get("date", "")) == str(target):
                    c = float(h.get("C_score", 0.0) or 0.0)
                    net = float(h.get("net", 0.0) or 0.0)
                    net_chg = float(h.get("net_chg", 0.0) or 0.0)
                    break
        if c is None:
            continue
        rows.append(
            {
                "sym": sym,
                "name": SYMBOLS[sym]["name"],
                "c_score": c,
                "net": net,
                "net_chg": net_chg,
                "direction": "多" if net > 0 else ("空" if net < 0 else "中性"),
                "exchange": rec.get("exchange", ""),
            }
        )
    rows.sort(key=lambda r: r["c_score"], reverse=True)
    bullish = [r for r in rows if r["c_score"] > 0][:top_n]
    bearish = [r for r in rows if r["c_score"] < 0]
    bearish.sort(key=lambda r: r["c_score"])  # 最偏空（最负）在前
    bearish = bearish[:top_n]
    by_sym = {
        r["sym"]: {
            "available": True,
            "c_score": r["c_score"],
            "net": r["net"],
            "net_chg": r["net_chg"],
            "direction": r["direction"],
            "trade_date": target,
        }
        for r in rows
    }
    # 2026-08-15：补充 is_today / stale_days，前端据此标注「数据日期·每日收盘更新」
    try:
        _td = datetime.strptime(str(target), "%Y%m%d").date() if target else None
    except Exception:
        _td = None
    _today = datetime.now().date()
    _is_today = (_td == _today) if _td else False
    _stale_days = (_today - _td).days if _td else None
    return {
        "trade_date": target,
        "is_latest": is_latest,
        "is_today": _is_today,
        "stale_days": _stale_days,
        "updated_at": meta.get("updated_at", ""),
        "total": len(rows),
        "available_dates": avail_dates,
        "bullish": bullish,
        "bearish": bearish,
        "by_sym": by_sym,
    }


# ---------------------------------------------------------------------------
# 主评估（单次）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 组合层风险约束（P0-1）：相关性同向 + 总风险预算
# ---------------------------------------------------------------------------
PAIR_CORR_CACHE = {}  # frozenset({a,b}) -> (ts, corr|None)


def _spec_mult(sym):
    """品种合约乘数（手风险计算用）。"""
    try:
        return at.load_config()["contract_specs"].get(sym, {}).get("multiplier", 10)
    except Exception:
        return 10


def _reconcile_journal_vs_account(held):
    """只读对账：journal 开仓记录(pnl=None)反推「应持仓」，与 account_state 持仓(held)比对。
    半自动系统下两本账可能漂移（漏记/未回灌/手动改），此检查拦住不一致。
    比对的维度（此前对账的盲区，已补）：
      - 持仓数量/手数/方向（原有）
      - 开仓均价 avg（journal entry_price vs account avg）
      - 已实现盈亏 realized_pnl（journal 已平仓盈亏合计 vs account realized_pnl）
    """
    try:
        data = tj._load()
        jopen = {}
        javg_acc = {}
        jlevels = {}
        for t in data.get("trades", []):
            if t.get("pnl") is not None:
                continue
            k = (t.get("symbol"), t.get("direction"))
            jopen[k] = jopen.get(k, 0) + (t.get("lots") or 0)
            qty = abs(t.get("quantity") or t.get("lots") or 0) or 1
            if k not in javg_acc:
                javg_acc[k] = {"sum": 0.0, "qty": 0.0}
                jlevels[k] = {
                    "stop": t.get("stop"),
                    "stop_dist": t.get("stop_dist"),
                    "t1": t.get("t1"),
                    "t2": t.get("t2"),
                }
            javg_acc[k]["sum"] += (t.get("entry_price") or 0) * qty
            javg_acc[k]["qty"] += qty
        javg = {k: round(v["sum"] / v["qty"], 2) if v["qty"] else 0.0 for k, v in javg_acc.items()}
        aopen = {}
        for p in held:
            k = (p.get("symbol"), p.get("direction"))
            aopen[k] = aopen.get(k, 0) + (p.get("lots") or 0)
        issues = []
        avg_issues = []
        for k in set(jopen) | set(aopen):
            jl = jopen.get(k, 0)
            al = aopen.get(k, 0)
            if jl == 0 and al > 0:
                issues.append(f"{k[0]} {k[1]}：账户有 {al} 手但 journal 无对应开仓记录")
            elif al == 0 and jl > 0:
                issues.append(f"{k[0]} {k[1]}：journal 开仓 {jl} 手但账户未同步（漏记/未回灌）")
            elif jl != al:
                issues.append(f"{k[0]} {k[1]}：手数不一致（journal {jl} vs 账户 {al}）")
        # 开仓均价漂移（journal entry_price vs account avg）
        for p in held:
            k = (p.get("symbol"), p.get("direction"))
            je = javg.get(k)
            if je is not None and abs((p.get("avg") or 0) - je) > 0.01:
                msg = f"{k[0]} 开仓均价不一致（journal {je} vs 账户 {p.get('avg')}）"
                avg_issues.append(msg)
                issues.append(msg)
        # 已实现盈亏漂移（journal 已平仓盈亏合计 vs account realized_pnl）
        jrealized = round(sum((t.get("pnl") or 0) for t in data.get("trades", []) if t.get("pnl") is not None), 2)
        arealized = round(at.load_state().get("realized_pnl", 0.0), 2)
        realized_issues = []
        if abs(jrealized - arealized) > 0.01:
            msg = f"已实现盈亏不一致（journal {jrealized} vs 账户 {arealized}）"
            realized_issues.append(msg)
            issues.append(msg)
        return {
            "match": len(issues) == 0,
            "issues": issues,
            "journal_open": len(jopen),
            "account_open": len(aopen),
            "realized_pnl_journal": jrealized,
            "realized_pnl_account": arealized,
            "realized_issues": realized_issues,
            "avg_issues": avg_issues,
        }
    except Exception as e:
        return {
            "match": None,
            "issues": [f"对账异常：{e}"],
            "journal_open": None,
            "account_open": None,
            "realized_pnl_journal": None,
            "realized_pnl_account": None,
            "realized_issues": [],
            "avg_issues": [],
        }


_ACCSYNC_CACHE = {"ts": 0.0, "data": None}
_ACCSYNC_TTL = 8


def positions_reconcile(positions):
    """A（2026-08-18）：手动对账端点。用户提交 CTP 真实持仓清单，重建 account_state + journal，
    消除「用户在 CTP 客户端手动平仓/开仓导致 runner 账户残留幽灵持仓」的漂移。
    positions: [{"symbol","direction","lots","avg"}, ...]
    返回 {ok, removed, added, adjusted, message}。"""
    try:
        if not isinstance(positions, list):
            return {"ok": False, "message": "positions 必须是数组"}
        specs = at.load_config().get("contract_specs", {})
        want = {}
        for it in positions:
            sym = it.get("symbol")
            if sym not in specs:
                return {"ok": False, "message": f"未知品种 {sym}"}
            d = it.get("direction")
            lots = int(it.get("lots") or 0)
            avg = it.get("avg")
            if d not in ("多", "空") or lots <= 0 or avg is None:
                return {"ok": False, "message": f"{sym} 手数/方向/均价非法"}
            want[sym] = {"direction": d, "lots": lots, "avg": float(avg)}
        cur = at.load_state().get("positions", {})
        removed, added, adjusted = [], [], []

        def _feed_price(sym, fallback):
            px = None
            if FEED:
                try:
                    px = FEED.price(sym)
                except Exception:
                    px = None
            return px or fallback

        # 1) 平仓：account 有、清单没有
        for sym in list(cur.keys()):
            if sym in want:
                continue
            p = cur[sym]
            px = _feed_price(sym, p.get("avg"))
            at.record_trade(sym, "close", p["direction"], p["lots"], px)
            tj.record_exit(sym, p["direction"], p["lots"], px, reason="CTP 手动平仓对账")
            removed.append({"symbol": sym, "direction": p["direction"], "lots": p["lots"], "exit_price": px})
        # 2) 开仓/调整：保留旧持仓的 stop/t1/t2/tp_targets 等风险控制字段
        for sym, w in want.items():
            p = cur.get(sym)
            if p is None:
                # 全新持仓（旧账户没有）：无历史风险等级可保留
                at.record_trade(sym, "open", w["direction"], w["lots"], w["avg"])
                tj.record_entry(sym, w["direction"], w["lots"], w["avg"], signal_id="对账")
                added.append({"symbol": sym, "direction": w["direction"], "lots": w["lots"], "avg": w["avg"]})
            elif p["lots"] != w["lots"] or p["direction"] != w["direction"]:
                # 同品种但手数/方向变了：先平后开，保留旧 stop/t1/t2
                px = _feed_price(sym, p.get("avg"))
                _old_stop = p.get("stop")
                _old_t1 = p.get("t1")
                _old_t2 = p.get("t2")
                at.record_trade(sym, "close", p["direction"], p["lots"], px)
                tj.record_exit(sym, p["direction"], p["lots"], px, reason="CTP 手动平仓对账")
                at.record_trade(
                    sym, "open", w["direction"], w["lots"], w["avg"], stop=_old_stop, t1=_old_t1, t2=_old_t2
                )
                tj.record_entry(sym, w["direction"], w["lots"], w["avg"], signal_id="对账")
                adjusted.append({"symbol": sym, "from": [p["direction"], p["lots"]], "to": [w["direction"], w["lots"]]})
        return {
            "ok": True,
            "removed": removed,
            "added": added,
            "adjusted": adjusted,
            "message": f"对账完成：平仓 {len(removed)}、新开 {len(added)}、调整 {len(adjusted)}",
        }
    except Exception as e:
        return {"ok": False, "message": f"对账异常：{e}"}


def account_marketsync(force=False, heal=False):
    """只读账户同步（minishare 实时盯市 + 对账漂移检测 + 自愈）。
    仅用 minishare 实时行情对 journal 已记录持仓逐笔盯市；不接券商 API、不代下单。
    heal=True 时以 journal 为真相源修正 account_state（已实现盈亏/开仓均价）；
    未显式 heal 时若检测到漂移也会自动自愈一次（journal 权威，幂等）。
    返回只读快照 + 每持仓盯市状态 + 漂移告警 + 自愈记录 + journal 账户一致性。"""
    global _ACCSYNC_CACHE
    now = time.time()
    if (
        (not force)
        and (not heal)
        and _ACCSYNC_CACHE["data"] is not None
        and (now - _ACCSYNC_CACHE["ts"]) < _ACCSYNC_TTL
    ):
        return _ACCSYNC_CACHE["data"]
    # 显式自愈（journal 为真相源，仅偏差时写盘，安全幂等）
    healed = []
    if heal:
        try:
            ok, healed, _ = at.heal_from_journal()
        except Exception as e:
            healed = [f"自愈失败：{e}"]
    prices = {}
    if FEED:
        for sym in SYMBOLS:
            try:
                prices[sym] = FEED.price(sym)
            except Exception:
                prices[sym] = None
    snap = at.snapshot(prices)
    # 行情时效
    feed_status = "正常" if FEED_AVAILABLE else "离线"
    feed_last = datetime.fromtimestamp(FEED_LAST_UPDATE).strftime("%H:%M:%S") if FEED_LAST_UPDATE else ""
    age = (datetime.now().timestamp() - FEED_LAST_UPDATE) / 60.0 if FEED_LAST_UPDATE else None
    data_age_min = round(age, 1) if age is not None else None
    # 每持仓盯市状态
    held = [p for p in snap.get("positions", []) if p.get("lots")]
    priced_n = 0
    for p in held:
        sym = p.get("symbol")
        ok = p.get("price") is not None
        if ok:
            priced_n += 1
            p["mtm_ok"] = True
            p["mtm_reason"] = "minishare 实时盯市"
        else:
            if sym not in SYMBOLS:
                p["mtm_reason"] = "未订阅该品种"
            elif not FEED_AVAILABLE:
                p["mtm_reason"] = "行情离线"
            else:
                p["mtm_reason"] = "非交易时段/行情缺口"
            p["mtm_ok"] = False
    # 漂移告警
    drift = []
    if FEED_AVAILABLE and data_age_min is not None and data_age_min > 5:
        drift.append({"level": "warn", "msg": f"行情数据偏旧（{data_age_min} 分钟前），只读快照可能滞后"})
    unpriced = [p["symbol"] for p in held if not p.get("mtm_ok")]
    if unpriced:
        drift.append({"level": "warn", "msg": "以下持仓当前无 minishare 实时价、无法盯市：" + "、".join(unpriced)})
    # B：持仓超期提醒（可能在 CTP 客户端手动平仓但 runner 未同步 → 提示对账）
    try:
        _st = at.load_state()
        for _sym, _pos in (_st.get("positions") or {}).items():
            _ot = _pos.get("open_time")
            if not _ot:
                continue
            try:
                _days = (datetime.now() - datetime.strptime(_ot, "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                continue
            if _days >= 7:
                drift.append(
                    {
                        "level": "info",
                        "msg": f"持仓超期提醒：{_sym} 已持 {_days} 天，若已在 CTP 客户端手动平仓，请用 /api/positions_reconcile 对账",
                    }
                )
    except Exception:
        pass
    # 一致性对账（含已实现盈亏/开仓均价盲区）
    consist = _reconcile_journal_vs_account(held)
    # 检测到漂移且非显式 heal → 自动自愈一次（journal 权威），再重算快照与一致性
    if consist.get("issues") and not heal:
        try:
            ok, healed2, _ = at.heal_from_journal()
            if healed2:
                healed = healed2
                snap = at.snapshot(prices)
                held = [p for p in snap.get("positions", []) if p.get("lots")]
                priced_n = 0
                for p in held:
                    if p.get("price") is not None:
                        priced_n += 1
                        p["mtm_ok"] = True
                        p["mtm_reason"] = "minishare 实时盯市"
                    else:
                        if p.get("symbol") not in SYMBOLS:
                            p["mtm_reason"] = "未订阅该品种"
                        elif not FEED_AVAILABLE:
                            p["mtm_reason"] = "行情离线"
                        else:
                            p["mtm_reason"] = "非交易时段/行情缺口"
                        p["mtm_ok"] = False
                consist = _reconcile_journal_vs_account(held)
        except Exception:
            pass
    if consist.get("issues"):
        for iss in consist["issues"]:
            drift.append({"level": "error", "msg": "对账不一致 · " + iss})
    out = {
        "read_only": True,
        "source": "minishare（行情）",
        "feed_status": feed_status,
        "feed_last": feed_last,
        "data_age_min": data_age_min,
        "equity_dynamic": snap.get("equity"),
        "float_total": snap.get("float_total"),
        "margin_used": snap.get("total_margin"),
        "usage_pct": snap.get("usage_rate"),
        "held_count": len(held),
        "priced_count": priced_n,
        "positions": [
            {
                "symbol": p["symbol"],
                "name": p.get("name"),
                "direction": p.get("direction"),
                "lots": p.get("lots"),
                "avg": p.get("avg"),
                "price": p.get("price"),
                "float_pnl": p.get("float_pnl"),
                "mtm_ok": p.get("mtm_ok"),
                "mtm_reason": p.get("mtm_reason"),
            }
            for p in held
        ],
        "drift": drift,
        "consistency": consist,
        "healed": healed,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _ACCSYNC_CACHE = {"ts": now, "data": out}
    return out


def _market_phase():
    """当前整体交易时段：日盘交易中 / 夜盘交易中 / 休市。
    周六/周日 全天休市。"""
    now = datetime.now()
    # 周末全天休市
    if now.weekday() >= 5:
        return "休市"
    t = now.hour * 60 + now.minute
    day = (540 <= t <= 615) or (630 <= t <= 690) or (810 <= t <= 900)
    night = 1260 <= t <= 1380
    if day:
        return "日盘交易中"
    if night:
        return "夜盘交易中"
    return "休市"


def _chk(name, passed, ok="", warn="", lvl="warn"):
    """构造一条开盘前检查项。status: ok / warn / error / info。"""
    if passed:
        return {"name": name, "status": "ok", "msg": ok or "正常"}
    return {"name": name, "status": (lvl if lvl in ("warn", "error", "info") else "warn"), "msg": warn or ok}


def _recent_signals_for_premkt(limit=60):
    """近期信号（含 TTL 过期标注），供盘前清单关注品种与时效检查。"""
    try:
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            arr = json.load(f)
    except Exception:
        arr = []
    arr = sorted(arr, key=lambda s: s.get("time", ""), reverse=True)[:limit]
    return [annotate_signal_ttl(s) for s in arr]


def _premkt_focus(sig_list):
    """盘前关注品种 + 关键价位（持仓优先，其次近期未过期信号）。"""
    focus = {}
    try:
        st = at.load_state()
        for sym, pos in st.get("positions", {}).items():
            lots = pos.get("lots") or 0
            if lots <= 0:
                continue
            focus[sym] = {
                "symbol": sym,
                "name": pos.get("name") or sym,
                "direction": pos.get("direction"),
                "lots": lots,
                "avg": pos.get("avg"),
                "stop": pos.get("stop"),
                "t1": pos.get("t1"),
                "target": pos.get("target"),
                "src": "持仓",
            }
    except Exception:
        pass
    for s in sig_list:
        sym = s.get("symbol") or s.get("name")
        if not sym or sym in focus or s.get("expired"):
            continue
        focus[sym] = {
            "symbol": sym,
            "name": s.get("name") or sym,
            "direction": s.get("direction"),
            "lots": s.get("lots"),
            "avg": s.get("price"),
            "stop": s.get("stop"),
            "t1": s.get("t1"),
            "target": s.get("target"),
            "src": "近期信号",
        }
    return list(focus.values())


_PREMK_CACHE = {"ts": 0, "data": None}
_PREMK_TTL = 30


# ----------------------------------------------------------------------------
# #126 多源数据交叉校验：minishare 实时价 vs 日线收盘 vs 持仓均价(journal) vs 信号开仓价
#   四源互相印证，检测跨源矛盾：移仓漏更 / 合约错 / 行情卡 / 复权错。
#   与 #14 单源数据质量、#124 journal↔账户对账 区分：此处是「多源数值一致性」。
#   不接券商 API，纯半自动只读。
# ----------------------------------------------------------------------------
_XSRC_CACHE = {"t": 0.0, "v": None}
_XSRC_TTL = 60
RT_DAILY_GAP_WARN = 6.0  # 实时价 vs 日线收盘阈值(%)
RT_AVG_GAP_WARN = 8.0  # 实时价 vs 持仓均价阈值(%)（仅交易时段）
AVG_DAILY_GAP_WARN = 8.0  # 持仓均价 vs 日线收盘阈值(%)（实时不可用/休市时的替代校验）
SIGNAL_DAILY_GAP_WARN = 8.0  # 信号开仓价 vs 日线收盘阈值(%)


def _xs_recent_signal_price(sym):
    """该品种最近一条【未过期】信号的开仓价。
    过期/历史信号的价格已随行情漂移，与最新日线收盘的偏离属正常现象，
    不应再判为'信号时间错/合约错'，故跳过（返回 None）。
    无 valid_minutes 的老信号（#122 TTL 功能上线前的历史记录）用默认 TTL 判定过期，
    避免 2 天前的快照持续误报合约错（P2-4：JM 1323@08-12 误比对 JM2609 最新收盘 1515）。"""
    try:
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            arr = json.load(f)
    except Exception:
        return None, None
    cands = [s for s in arr if (s.get("symbol") or s.get("name")) == sym]
    if not cands:
        return None, None
    cands.sort(key=lambda s: s.get("time", ""), reverse=True)
    now = datetime.now()
    for s in cands:
        tstr = s.get("time") or s.get("created_at")
        vmin = s.get("valid_minutes") or 0
        if vmin <= 0:
            vmin = DEFAULT_SIGNAL_TTL_MIN  # 老信号用默认 TTL 作过期基准
        try:
            ct = datetime.strptime(tstr, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ct = None
        if ct is None:
            continue  # 时间无法解析的信号不安全，跳过
        if (now - ct).total_seconds() / 60.0 > vmin:
            continue  # 已过期，跳过（历史快照不应再比对最新日线）
        return s.get("price"), s.get("time")
    return None, None


def cross_source_check(force=False):
    """多源数据交叉校验（#126）。对持仓∪关注品种，逐合约比对四源数值并标记矛盾。"""
    global _XSRC_CACHE
    now = time.time()
    if not force and _XSRC_CACHE["v"] is not None and (now - _XSRC_CACHE["t"]) < _XSRC_TTL:
        return _XSRC_CACHE["v"]
    phase = _market_phase()
    trading = phase != "休市"
    st = at.load_state()
    held = {s for s, p in st.get("positions", {}).items() if (p.get("lots") or 0) > 0}
    syms = sorted(set(held) | set(FOCUS_SYMS))
    rows = []
    all_flags = []
    ok_n = warn_n = error_n = 0
    with_rt = 0
    for sym in syms:
        meta = SYMBOLS.get(sym, {})
        name = meta.get("name", sym)
        rt = FEED.price(sym) if (FEED and sym in SYMBOLS) else None
        if rt is not None:
            with_rt += 1
        dl = dhi = dlo = None
        try:
            df = load_daily_refreshed(sym)
            if df is not None and len(df):
                dl = float(df["close"].iloc[-1])
                dhi = float(df["high"].iloc[-1])
                dlo = float(df["low"].iloc[-1])
        except Exception:
            pass
        pos = st.get("positions", {}).get(sym, {})
        avg = pos.get("avg")
        sp, spt = _xs_recent_signal_price(sym)
        flags = []
        rt_vs_daily = avg_vs_daily = rt_vs_avg = sig_vs_daily = None
        # 校验1：实时 vs 日线收盘（仅交易时段、实时可用）
        if rt is not None and dl is not None and trading:
            gap = (rt - dl) / dl * 100.0
            rt_vs_daily = round(gap, 2)
            amp = (dhi - dlo) / dlo * 100.0 if (dhi and dlo and dlo > 0) else 0.0
            thr = max(RT_DAILY_GAP_WARN, 1.5 * amp, 3.0)
            if abs(gap) > thr:
                flags.append(
                    f"实时价({rt:.0f})与日线收盘({dl:.0f})偏离 {gap:+.1f}%（>动态阈值 {thr:.1f}%），疑似行情卡/合约错/复权错"
                )
        # 校验2：实时 vs 持仓均价（持仓 + 交易时段 + 实时可用）
        if rt is not None and avg is not None and (pos.get("lots") or 0) > 0 and trading:
            gap = (rt - avg) / avg * 100.0
            rt_vs_avg = round(gap, 2)
            if abs(gap) > RT_AVG_GAP_WARN:
                flags.append(f"实时价({rt:.0f})与持仓均价({avg:.0f})偏离 {gap:+.1f}%，疑似移仓漏更/合约错/账户同步错")
        # 校验2'：持仓均价 vs 日线（实时不可用/休市时仍有效，与校验2互斥避免重复报警）
        elif avg is not None and dl is not None and (pos.get("lots") or 0) > 0:
            gap = (avg - dl) / dl * 100.0
            avg_vs_daily = round(gap, 2)
            if abs(gap) > AVG_DAILY_GAP_WARN:
                flags.append(f"持仓均价({avg:.0f})与日线收盘({dl:.0f})偏离 {gap:+.1f}%，疑似移仓漏更")
        # 校验3：信号开仓价 vs 日线收盘
        if sp is not None and dl is not None:
            gap = (sp - dl) / dl * 100.0
            sig_vs_daily = round(gap, 2)
            if abs(gap) > SIGNAL_DAILY_GAP_WARN:
                flags.append(f"信号开仓价({sp:.0f}@{spt})与日线收盘({dl:.0f})偏离 {gap:+.1f}%，疑似信号时间错/合约错")
        # 状态：持仓均价相关矛盾→error（最危险，影响盯市/止损）；其余→warn
        status = "ok"
        if any("持仓均价" in f for f in flags):
            status = "error"
        elif flags:
            status = "warn"
        if status == "ok":
            ok_n += 1
        elif status == "warn":
            warn_n += 1
        else:
            error_n += 1
        for f in flags:
            all_flags.append(f"{sym}: {f}")
        rows.append(
            {
                "symbol": sym,
                "name": name,
                "rt": round(rt, 2) if rt is not None else None,
                "daily_close": round(dl, 2) if dl is not None else None,
                "daily_high": round(dhi, 2) if dhi is not None else None,
                "daily_low": round(dlo, 2) if dlo is not None else None,
                "avg": round(avg, 2) if avg is not None else None,
                "signal_price": round(sp, 2) if sp is not None else None,
                "signal_time": spt,
                "rt_vs_daily_pct": rt_vs_daily,
                "rt_vs_avg_pct": rt_vs_avg,
                "avg_vs_daily_pct": avg_vs_daily,
                "sig_vs_daily_pct": sig_vs_daily,
                "held": sym in held,
                "flags": flags,
                "status": status,
            }
        )
    out = {
        "ok": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "trading": trading,
        "symbols": rows,
        "summary": {
            "checked": len(rows),
            "with_realtime": with_rt,
            "ok": ok_n,
            "warn": warn_n,
            "error": error_n,
            "flags": all_flags,
        },
        "note": "跨源校验：minishare 实时价 / 日线收盘 / 持仓均价(journal) / 信号开仓价 四源互相印证。报警即疑似数据矛盾（移仓漏更/合约错/行情卡/复权错），与 #14 单源质量、#124 对账互补。",
    }
    _XSRC_CACHE["t"] = now
    _XSRC_CACHE["v"] = out
    return out


# ---------------------------------------------------------------------------
# #127 实盘盈亏 F/T/C 维度归因：包装 trade_journal.pnl_attribution，加缓存。
# ---------------------------------------------------------------------------
_ATTR_CACHE = {"ts": 0.0, "data": None}
_ATTR_TTL = 30


def pnl_attribution(force=False):
    """实盘盈亏 F/T/C 维度归因（#127）：包装 tj.pnl_attribution，30s 缓存。"""
    global _ATTR_CACHE
    now = time.time()
    if not force and _ATTR_CACHE["data"] is not None and (now - _ATTR_CACHE["ts"]) < _ATTR_TTL:
        return _ATTR_CACHE["data"]
    import trade_journal as tj

    try:
        out = tj.pnl_attribution()
    except Exception as e:
        out = {"ok": False, "reason": "归因计算失败: " + repr(e)[:160]}
    _ATTR_CACHE["ts"] = now
    _ATTR_CACHE["data"] = out
    return out


def premarket_brief(force=False):
    """盘前自动作战清单（#125）：聚合只读账户盯市 / 风险热度 / 回撤水位线 /
    状态机 / 硬熔断 / 行情时效 / 近期信号过期 → 开盘前检查清单 + 关注品种关键价位。
    纯只读、不接券商 API，与系统半自动定位一致。"""
    global _PREMK_CACHE
    now = time.time()
    if not force and _PREMK_CACHE["data"] is not None and (now - _PREMK_CACHE["ts"]) < _PREMK_TTL:
        return _PREMK_CACHE["data"]
    # —— 聚合各数据源 ——
    sync = account_marketsync(force=force)
    heat = compute_heat()
    dd = ddg.current()
    fsm = rsm.RISK_FSM.summary()
    halted = rsm.is_halted()
    phase = _market_phase()
    sig_list = _recent_signals_for_premkt()
    expired_n = sum(1 for s in sig_list if s.get("expired"))
    focus = _premkt_focus(sig_list)
    feed_status = sync.get("feed_status", "离线")
    feed_age = sync.get("data_age_min")
    eq = sync.get("equity_dynamic") or 0
    held = sync.get("held_count", 0)
    priced = sync.get("priced_count", 0)
    consist = sync.get("consistency", {}) or {}
    drift = sync.get("drift", []) or []
    # —— 检查清单 ——
    checks = []
    checks.append(
        _chk("账户权益已同步", eq > 0, ok=f"已同步 · 动态权益 ¥{eq:,.0f}", warn="未同步或权益为 0，开盘前请先同步账户")
    )
    if phase == "休市":
        checks.append(_chk("实时行情", True, ok="当前休市 · 行情待开盘后恢复", lvl="info"))
    elif feed_status == "正常" and (feed_age is None or feed_age <= 5):
        checks.append(_chk("实时行情", True, ok=f"正常 · {feed_age} 分钟前更新"))
    elif feed_status == "正常":
        checks.append(_chk("实时行情", False, warn=f"行情偏旧（{feed_age} 分钟前），开盘前请确认连接", lvl="warn"))
    else:
        checks.append(_chk("实时行情", False, warn="行情离线，无法盯市", lvl="warn"))
    dd_scale = dd.get("scale", 1.0)
    if dd_scale < 1.0:
        checks.append(
            _chk(
                "回撤水位线降险",
                False,
                warn=f"已触发 · 回撤 {dd.get('dd_pct')}% · 新开仓系数 {dd_scale}（自动降仓）",
                lvl="warn",
            )
        )
    else:
        checks.append(_chk("回撤水位线降险", True, ok=f"正常 · 回撤 {dd.get('dd_pct')}% · 系数 1.0"))
    if halted:
        checks.append(
            _chk(
                "硬熔断状态",
                False,
                warn="已激活 · " + str(fsm.get("killswitch", {}).get("reason", "") or "未知"),
                lvl="error",
            )
        )
    else:
        checks.append(_chk("硬熔断状态", True, ok="未激活"))
    hpct = heat.get("heat_pct", 0) or 0
    if heat.get("over"):
        checks.append(_chk("组合风险热度", False, warn=f"超标 {hpct}% · 红字勿加仓", lvl="error"))
    elif hpct > 80:
        checks.append(_chk("组合风险热度", False, warn=f"警戒 {hpct}%", lvl="warn"))
    else:
        checks.append(_chk("组合风险热度", True, ok=f"正常 {hpct}% / 预算 ¥{heat.get('budget') or 0:,.0f}"))
    if consist.get("match"):
        checks.append(_chk("journal↔账户一致性", True, ok="一致 · 无漏记"))
    else:
        issues = consist.get("issues", []) or []
        checks.append(
            _chk(
                "journal↔账户一致性", False, warn="不一致 · " + ("；".join(issues[:3]) or "请回灌 journal"), lvl="error"
            )
        )
    if expired_n > 0:
        checks.append(
            _chk(
                "近期信号时效",
                False,
                warn=f"{expired_n} 条信号已过期（>{signal_ttl_minutes()}min），跟单前请重评",
                lvl="warn",
            )
        )
    else:
        checks.append(_chk("近期信号时效", True, ok="无过期信号"))
    if held == 0:
        checks.append(_chk("当前持仓盯市", True, ok="无持仓", lvl="info"))
    elif priced < held:
        checks.append(
            _chk(
                "当前持仓盯市",
                False,
                warn=f"{priced}/{held} 持仓有实时价，{held - priced} 个无实时价（非交易时段/行情缺口）",
                lvl="warn",
            )
        )
    else:
        checks.append(_chk("当前持仓盯市", True, ok=f"{priced}/{held} 持仓全部实时盯市"))
    # —— 汇总 ——
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "read_only": True,
        "source": "minishare（行情）+ journal（持仓）+ 风控状态机",
        "account": {
            "equity_dynamic": eq,
            "usage_pct": sync.get("usage_pct"),
            "float_total": sync.get("float_total"),
            "held": held,
            "priced": priced,
        },
        "heat": {
            "heat_pct": hpct,
            "status": heat.get("status"),
            "over": heat.get("over"),
            "budget": heat.get("budget"),
        },
        "drawdown": {"dd_pct": dd.get("dd_pct"), "tier": dd.get("tier"), "scale": dd_scale},
        "fsm": {
            "state": fsm.get("state"),
            "consec_losses": fsm.get("consec_losses"),
            "halted": halted,
            "kill_reason": fsm.get("killswitch", {}).get("reason"),
        },
        "feed": {"status": feed_status, "age_min": feed_age, "last": sync.get("feed_last")},
        "signals": {"total": len(sig_list), "expired": expired_n, "recent": sig_list[:8]},
        "focus": focus,
        "checks": checks,
        "drift_count": len(drift),
        "drift": drift,
    }
    _PREMK_CACHE = {"ts": now, "data": out}
    return out


def _account_equity():
    """当前账户权益（同步权益，保守基准）。"""
    try:
        return float(at.load_state().get("equity") or 0)
    except Exception:
        return 0.0


_POS_CACHE = {"time": 0, "data": None}
_POS_CACHE_TTL = 2  # 缓存2秒


def _load_open_positions():
    """当前真实持仓 → 敞口列表 [{sym, direction, lots, risk}]。"""
    global _POS_CACHE
    _now = time.time()
    if _POS_CACHE["data"] is not None and (_now - _POS_CACHE["time"]) < _POS_CACHE_TTL:
        return _POS_CACHE["data"]
    try:
        st = at.load_state()
        out = []
        for sym, pos in st.get("positions", {}).items():
            lots = pos.get("lots") or 0
            if lots <= 0:
                continue
            stop = pos.get("stop") or pos.get("t1")
            avg = pos.get("avg")
            if stop is None or avg is None:
                continue
            try:
                risk = abs(float(avg) - float(stop)) * _spec_mult(sym) * lots
            except Exception:
                continue
            out.append(
                {
                    "sym": sym,
                    "direction": pos.get("direction"),
                    "lots": lots,
                    "risk": risk,
                    "avg": avg,
                    "group": _sym_group(sym),
                }
            )
        _POS_CACHE = {"time": time.time(), "data": out}
        return out
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
# v6.0 市场状态引擎 — 核心函数（阶段一）
# ═══════════════════════════════════════════════════════════


def _calc_ma_alignment_score(klines):
    """技术面指标1：均线排列评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_MA_SLOW + 5:
        return 50
    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    if len(closes) < TECH_MA_SLOW:
        return 50
    ma_fast_now = sum(closes[-TECH_MA_FAST:]) / TECH_MA_FAST
    ma_slow_now = sum(closes[-TECH_MA_SLOW:]) / TECH_MA_SLOW
    prev_closes = closes[:-5] if len(closes) > 5 else closes
    ma_fast_prev = sum(prev_closes[-TECH_MA_FAST:]) / TECH_MA_FAST if len(prev_closes) >= TECH_MA_FAST else ma_fast_now
    ma_slow_prev = sum(prev_closes[-TECH_MA_SLOW:]) / TECH_MA_SLOW if len(prev_closes) >= TECH_MA_SLOW else ma_slow_now
    if ma_slow_now == 0:
        return 50
    divergence_now = (ma_fast_now - ma_slow_now) / ma_slow_now * 100
    divergence_prev = (ma_fast_prev - ma_slow_prev) / ma_slow_prev * 100 if ma_slow_prev != 0 else 0
    divergence_change = divergence_now - divergence_prev
    abs_div = abs(divergence_now)
    if abs_div < 0.3:
        base_score = 10 + abs_div / 0.3 * 20
    elif abs_div < 1.5:
        base_score = 30 + (abs_div - 0.3) / 1.2 * 40
    elif abs_div < 3.0:
        base_score = 70 + (abs_div - 1.5) / 1.5 * 20
    else:
        base_score = 90 + min(10, (abs_div - 3.0) * 5)
    if divergence_change < -0.1 and abs_div > 1.0:
        base_score = max(base_score, 75)
    return round(min(100, max(0, base_score)), 1)


def _calc_vol_level_score(klines):
    """技术面指标2：波动率水平评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_ATR_PERIOD + 5:
        return 50
    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    if len(closes) < TECH_ATR_PERIOD:
        return 50
    tr_values = []
    for i in range(1, len(klines)):
        high = klines[i].get("high", 0)
        low = klines[i].get("low", 0)
        prev_close = klines[i - 1].get("close", 0)
        if high > 0 and low > 0 and prev_close > 0:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
    if len(tr_values) < TECH_ATR_PERIOD:
        return 50
    atr = sum(tr_values[-TECH_ATR_PERIOD:]) / TECH_ATR_PERIOD
    current_price = closes[-1]
    if current_price == 0:
        return 50
    atr_pct = atr / current_price * 100
    if atr_pct < 0.8:
        score = 10 + atr_pct / 0.8 * 20
    elif atr_pct < 2.0:
        score = 30 + (atr_pct - 0.8) / 1.2 * 40
    elif atr_pct < 4.0:
        score = 70 + (atr_pct - 2.0) / 2.0 * 25
    else:
        score = 95 + min(5, (atr_pct - 4.0) * 2)
    return round(min(100, max(0, score)), 1)


def _calc_vol_change_score(klines):
    """技术面指标3：波动率变化评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_ATR_PERIOD + TECH_LOOKBACK_BARS + 5:
        return 50
    tr_values = []
    for i in range(1, len(klines)):
        high = klines[i].get("high", 0)
        low = klines[i].get("low", 0)
        prev_close = klines[i - 1].get("close", 0)
        if high > 0 and low > 0 and prev_close > 0:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
    if len(tr_values) < TECH_ATR_PERIOD + TECH_LOOKBACK_BARS:
        return 50
    atr_recent = sum(tr_values[-TECH_ATR_PERIOD:]) / TECH_ATR_PERIOD
    atr_prev = sum(tr_values[-TECH_ATR_PERIOD - TECH_LOOKBACK_BARS : -TECH_LOOKBACK_BARS]) / TECH_ATR_PERIOD
    if atr_prev == 0:
        return 50
    change_pct = (atr_recent - atr_prev) / atr_prev * 100
    if change_pct > 30:
        score = 75 + min(25, (change_pct - 30) * 0.5)
    elif change_pct > 10:
        score = 55 + (change_pct - 10) / 20 * 20
    elif change_pct > -10:
        score = 45 + (change_pct + 10) / 20 * 10
    elif change_pct > -30:
        score = 25 + (change_pct + 30) / 20 * 20
    else:
        score = max(5, 25 - (abs(change_pct) - 30) * 0.5)
    return round(min(100, max(0, score)), 1)


def _calc_volume_price_score(klines, volumes=None):
    """技术面指标4：量价配合评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_VOLUME_MA_PERIOD + 5:
        return 50
    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    if not volumes or len(volumes) < TECH_VOLUME_MA_PERIOD:
        if len(closes) < 10:
            return 50
        price_changes = [
            abs(closes[i] - closes[i - 1]) / closes[i - 1] * 100 for i in range(1, len(closes)) if closes[i - 1] > 0
        ]
        if len(price_changes) < 5:
            return 50
        avg_change = sum(price_changes[-10:]) / 10
        if avg_change < 0.3:
            return 30
        elif avg_change < 1.0:
            return 55
        elif avg_change < 2.0:
            return 70
        else:
            return 85
    vol_ma = sum(volumes[-TECH_VOLUME_MA_PERIOD:]) / TECH_VOLUME_MA_PERIOD
    recent_scores = []
    for i in range(-3, 0):
        if len(klines) + i < 1 or len(volumes) + i < 1:
            continue
        close = klines[i].get("close", 0)
        prev_close = klines[i - 1].get("close", 0) if i > -len(klines) else close
        vol = volumes[i] if i < len(volumes) else vol_ma
        if prev_close == 0 or vol_ma == 0:
            continue
        price_change_pct = (close - prev_close) / prev_close * 100
        vol_ratio = vol / vol_ma
        if abs(price_change_pct) < 0.2:
            if vol_ratio < 0.8:
                recent_scores.append(20)
            else:
                recent_scores.append(40)
        elif price_change_pct > 0:
            if vol_ratio > 1.2:
                recent_scores.append(70)
            elif vol_ratio > 0.8:
                recent_scores.append(55)
            else:
                recent_scores.append(85)
        else:
            if vol_ratio > 1.2:
                recent_scores.append(75)
            elif vol_ratio > 0.8:
                recent_scores.append(50)
            else:
                recent_scores.append(80)
    if not recent_scores:
        return 50
    return round(sum(recent_scores) / len(recent_scores), 1)


def _calc_trend_strength_score(klines):
    """技术面指标5：趋势强度评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_ADX_PERIOD + 5:
        return 50
    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    if len(closes) < TECH_ADX_PERIOD:
        return 50
    ma_fast = sum(closes[-TECH_MA_FAST:]) / TECH_MA_FAST
    ma_slow = sum(closes[-TECH_MA_SLOW:]) / TECH_MA_SLOW
    if ma_slow == 0:
        return 50
    divergence_pct = abs(ma_fast - ma_slow) / ma_slow * 100
    if divergence_pct < 0.5:
        score = 10 + divergence_pct / 0.5 * 20
    elif divergence_pct < 1.5:
        score = 30 + (divergence_pct - 0.5) / 1.0 * 30
    elif divergence_pct < 3.0:
        score = 60 + (divergence_pct - 1.5) / 1.5 * 30
    else:
        score = 90 + min(10, (divergence_pct - 3.0) * 3)
    return round(min(100, max(0, score)), 1)


def calc_tech_market_state(klines, volumes=None):
    """技术面市场状态综合评分（v6.0 新增）"""
    if not klines or len(klines) < TECH_MA_SLOW + 10:
        return {
            "total_score": 50,
            "state_candidate": MARKET_STATE_SIDEWAYS,
            "indicators": {},
            "ma_fast": 0,
            "ma_slow": 0,
        }
    ma_score = _calc_ma_alignment_score(klines)
    vol_level_score = _calc_vol_level_score(klines)
    vol_change_score = _calc_vol_change_score(klines)
    vp_score = _calc_volume_price_score(klines, volumes)
    trend_strength_score = _calc_trend_strength_score(klines)
    total_score = (
        ma_score * TECH_WEIGHT_MA_ALIGNMENT
        + vol_level_score * TECH_WEIGHT_VOL_LEVEL
        + vol_change_score * TECH_WEIGHT_VOL_CHANGE
        + vp_score * TECH_WEIGHT_VOLUME_PRICE
        + trend_strength_score * TECH_WEIGHT_TREND_STRENGTH
    ) / 100
    total_score = round(total_score, 1)
    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    ma_fast = sum(closes[-TECH_MA_FAST:]) / TECH_MA_FAST if len(closes) >= TECH_MA_FAST else 0
    ma_slow = sum(closes[-TECH_MA_SLOW:]) / TECH_MA_SLOW if len(closes) >= TECH_MA_SLOW else 0
    trend_direction = "long" if ma_fast > ma_slow else "short"
    if total_score < TECH_SCORE_SIDEWAYS_MAX:
        state_candidate = MARKET_STATE_SIDEWAYS
    elif total_score < TECH_SCORE_TREND_MID_MIN:
        state_candidate = MARKET_STATE_TREND_EARLY
    elif total_score < TECH_SCORE_TREND_LATE_MIN:
        state_candidate = MARKET_STATE_TREND_MID
    else:
        state_candidate = MARKET_STATE_TREND_LATE
    if total_score >= 70 and vol_change_score < 40:
        state_candidate = MARKET_STATE_TREND_LATE
    return {
        "total_score": total_score,
        "state_candidate": state_candidate,
        "trend_direction": trend_direction,
        "indicators": {
            "ma_alignment": ma_score,
            "vol_level": vol_level_score,
            "vol_change": vol_change_score,
            "volume_price": vp_score,
            "trend_strength": trend_strength_score,
        },
        "ma_fast": round(ma_fast, 4),
        "ma_slow": round(ma_slow, 4),
    }


def calc_performance_score(recent_trades):
    """表现面反馈统计（v6.0 新增）"""
    if not recent_trades or len(recent_trades) == 0:
        return {"total_score": 50, "level": "mid", "metrics": {}}
    trades = recent_trades[-PERF_WINDOW_MID:]
    n = len(trades)
    if n < 3:
        return {"total_score": 50, "level": "mid", "metrics": {"note": f"交易数不足({n}笔)"}}
    wins = sum(1 for t in trades if t.get("win", t.get("r_result", 0) > 0))
    win_rate = wins / n * 100
    if win_rate > 60:
        winrate_score = 80 + min(20, (win_rate - 60) * 2)
    elif win_rate > 45:
        winrate_score = 50 + (win_rate - 45) / 15 * 30
    elif win_rate > 30:
        winrate_score = 20 + (win_rate - 30) / 15 * 30
    else:
        winrate_score = max(0, 20 - (30 - win_rate) * 1.5)
    winning_r = [t.get("r_result", 0) for t in trades if t.get("r_result", 0) > 0]
    losing_r = [abs(t.get("r_result", 0)) for t in trades if t.get("r_result", 0) < 0]
    profit_factor = sum(winning_r) / sum(losing_r) if losing_r and sum(losing_r) > 0 else (3.0 if winning_r else 1.0)
    if profit_factor > 2.5:
        pf_score = 80 + min(20, (profit_factor - 2.5) * 10)
    elif profit_factor > 1.5:
        pf_score = 50 + (profit_factor - 1.5) * 30
    elif profit_factor > 0.8:
        pf_score = 20 + (profit_factor - 0.8) / 0.7 * 30
    else:
        pf_score = max(0, 20 - (0.8 - profit_factor) * 30)
    current_streak = 0
    streak_type = "none"
    for t in reversed(trades):
        is_win = t.get("win", t.get("r_result", 0) > 0)
        if is_win:
            if streak_type in ("win", "none"):
                current_streak += 1
                streak_type = "win"
            else:
                break
        else:
            if streak_type in ("lose", "none"):
                current_streak += 1
                streak_type = "lose"
            else:
                break
    if streak_type == "win":
        streak_score = (
            90 if current_streak >= 5 else (75 if current_streak >= 3 else (60 if current_streak >= 2 else 55))
        )
    elif streak_type == "lose":
        streak_score = (
            10 if current_streak >= 5 else (25 if current_streak >= 3 else (40 if current_streak >= 2 else 45))
        )
    else:
        streak_score = 50
    recent_short = trades[-PERF_WINDOW_SHORT:] if len(trades) >= PERF_WINDOW_SHORT else trades
    total_r = sum(t.get("r_result", 0) for t in recent_short)
    r_score = max(0, min(100, 50 + (total_r / 3.0) * 50))
    total_score = (
        winrate_score * PERF_WEIGHT_WINRATE
        + pf_score * PERF_WEIGHT_PROFIT_FACTOR
        + streak_score * PERF_WEIGHT_STREAK
        + r_score * PERF_WEIGHT_RECENT_R
    ) / 100
    total_score = round(total_score, 1)
    level = "good" if total_score >= PERF_SCORE_GOOD_MIN else ("mid" if total_score >= PERF_SCORE_MID_MIN else "poor")
    return {
        "total_score": total_score,
        "level": level,
        "metrics": {
            "win_rate": round(win_rate, 1),
            "winrate_score": round(winrate_score, 1),
            "profit_factor": round(profit_factor, 2),
            "pf_score": round(pf_score, 1),
            "current_streak": current_streak,
            "streak_type": streak_type,
            "streak_score": round(streak_score, 1),
            "recent_total_r": round(total_r, 2),
            "r_score": round(r_score, 1),
            "trade_count": n,
        },
    }


def determine_market_state(tech_result, perf_result, prev_state=None, confirm_counter=0):
    """市场状态最终判定 - 双因子交叉+确认机制（v6.0 新增）"""
    tech_state = tech_result.get("state_candidate", MARKET_STATE_SIDEWAYS)
    perf_level = perf_result.get("level", "mid")
    switch_matrix = {
        MARKET_STATE_TREND_EARLY: {
            "good": (MARKET_STATE_TREND_EARLY, "high"),
            "mid": (MARKET_STATE_TREND_EARLY, "mid"),
            "poor": (MARKET_STATE_SIDEWAYS, "low"),
        },
        MARKET_STATE_TREND_MID: {
            "good": (MARKET_STATE_TREND_MID, "high"),
            "mid": (MARKET_STATE_TREND_MID, "mid"),
            "poor": (MARKET_STATE_TREND_LATE, "low"),
        },
        MARKET_STATE_TREND_LATE: {
            "good": (MARKET_STATE_TREND_LATE, "mid"),
            "mid": (MARKET_STATE_TREND_LATE, "mid"),
            "poor": (MARKET_STATE_TREND_LATE, "high"),
        },
        MARKET_STATE_SIDEWAYS: {
            "good": (MARKET_STATE_SIDEWAYS, "mid"),
            "mid": (MARKET_STATE_SIDEWAYS, "mid"),
            "poor": (MARKET_STATE_SIDEWAYS, "high"),
        },
    }
    if tech_state in switch_matrix and perf_level in switch_matrix[tech_state]:
        candidate_state, confidence = switch_matrix[tech_state][perf_level]
    else:
        candidate_state = tech_state
        confidence = "low"
    switched = False
    if prev_state is None or prev_state == candidate_state:
        new_state = candidate_state
        new_counter = 0
    else:
        new_counter = confirm_counter + 1
        if new_counter >= STATE_CONFIRM_BARS:
            new_state = candidate_state
            switched = True
            new_counter = 0
        else:
            new_state = prev_state
    return {
        "state": new_state,
        "confidence": confidence,
        "tech_state": tech_state,
        "perf_level": perf_level,
        "tech_score": tech_result.get("total_score", 50),
        "perf_score": perf_result.get("total_score", 50),
        "confirm_counter": new_counter,
        "switched": switched,
        "trend_direction": tech_result.get("trend_direction", "long"),
    }


def get_dynamic_params(market_state):
    """获取当前市场状态下的动态参数（v6.0 新增）"""
    if not DYNAMIC_PARAMS_ENABLED:
        return {
            "single_trade_risk_mult": 1.0,
            "min_rr_ratio_mult": 1.0,
            "quality_score_mult": 1.0,
            "atr_stop_mult": 1.0,
            "time_stop_days_mult": 1.0,
            "max_positions_mult": 1.0,
            "take_profit_mult": 1.0,
            "note": "动态参数未启用（阶段一观察期）",
        }
    if market_state in STATE_PARAM_MAPPING:
        return STATE_PARAM_MAPPING[market_state].copy()
    return {
        "single_trade_risk_mult": 1.0,
        "min_rr_ratio_mult": 1.0,
        "quality_score_mult": 1.0,
        "atr_stop_mult": 1.0,
        "time_stop_days_mult": 1.0,
        "max_positions_mult": 1.0,
        "take_profit_mult": 1.0,
        "note": "未知状态",
    }


def get_state_label(state):
    """获取市场状态中文标签（v6.0 新增）"""
    labels = {
        MARKET_STATE_TREND_EARLY: "趋势初期",
        MARKET_STATE_TREND_MID: "趋势中期",
        MARKET_STATE_TREND_LATE: "趋势末期",
        MARKET_STATE_SIDEWAYS: "震荡市",
    }
    return labels.get(state, state)


# ═══════════════════════════════════════════════════════════
# v6.0 分级止盈状态机 — 核心函数
# ═══════════════════════════════════════════════════════════


def calc_take_profit_targets(entry_price, atr, direction, market_state=None):
    """计算分级止盈目标位"""
    t1_mult = TP_T1_ATR_MULT
    t2_mult = TP_T2_ATR_MULT
    t3_mult = TP_T3_TRAILING_ATR_MULT
    skip_t3 = False
    if DYNAMIC_PARAMS_ENABLED and market_state:
        params = get_dynamic_params(market_state)
        tp_mult = params.get("take_profit_mult", 1.0)
        t1_mult = TP_T1_ATR_MULT * tp_mult
        t2_mult = TP_T2_ATR_MULT * tp_mult
        if TP_SIDEWAYS_SKIP_T3 and market_state == MARKET_STATE_SIDEWAYS:
            skip_t3 = True
    if direction == "long":
        t1_price = entry_price + atr * t1_mult
        t2_price = entry_price + atr * t2_mult
    else:
        t1_price = entry_price - atr * t1_mult
        t2_price = entry_price - atr * t2_mult
    return {
        "t1_price": round(t1_price, 4),
        "t2_price": round(t2_price, 4),
        "t1_atr_mult": round(t1_mult, 2),
        "t2_atr_mult": round(t2_mult, 2),
        "t3_trailing_atr_mult": round(t3_mult, 2),
        "skip_t3": skip_t3,
    }


def update_take_profit_level(position_info, current_price, atr):
    """更新分级止盈状态（T1→T2→T3状态机）"""
    entry_price = position_info.get("entry_price", 0)
    direction = position_info.get("direction", "long")
    current_level = position_info.get("tp_level", TAKE_PROFIT_LEVEL_NONE)
    tp_targets = position_info.get("tp_targets", {})
    current_qty = position_info.get("current_qty", 0)
    init_qty = position_info.get("init_qty", current_qty)

    result = {
        "new_level": current_level,
        "action": "none",
        "reduce_qty": 0,
        "new_stop_price": None,
        "trailing_stop": None,
    }

    if entry_price <= 0 or current_price <= 0 or not tp_targets:
        return result

    t1_price = tp_targets.get("t1_price", 0)
    t2_price = tp_targets.get("t2_price", 0)
    skip_t3 = tp_targets.get("skip_t3", False)

    def hit_target(target):
        if direction == "long":
            return current_price >= target
        else:
            return current_price <= target

    # 状态1: T1未达
    if current_level == TAKE_PROFIT_LEVEL_NONE:
        if hit_target(t1_price):
            _raw_reduce = max(1, int(init_qty * TP_T1_REDUCE_RATIO))
            # Bug fix: 减仓量≥持仓量时直接全平，避免"平半变全平"或下单失败
            if _raw_reduce >= current_qty:
                result["new_level"] = TAKE_PROFIT_LEVEL_DONE
                result["action"] = "close_t1_full"
                result["reduce_qty"] = current_qty
            else:
                result["new_level"] = TAKE_PROFIT_LEVEL_T1
                result["action"] = "reduce_t1"
                result["reduce_qty"] = _raw_reduce
            if TP_T1_STOP_MOVE_TO_BREAKEVEN:
                result["new_stop_price"] = entry_price
        return result

    # 状态2: T1已达
    elif current_level == TAKE_PROFIT_LEVEL_T1:
        if hit_target(t2_price):
            _raw_reduce2 = max(1, int(init_qty * TP_T2_REDUCE_RATIO))
            # Bug fix: T2减仓量≥剩余持仓时全平
            if _raw_reduce2 >= current_qty:
                result["new_level"] = TAKE_PROFIT_LEVEL_DONE
                result["action"] = "close_t2_full"
                result["reduce_qty"] = current_qty
            else:
                result["new_level"] = TAKE_PROFIT_LEVEL_T2
                result["action"] = "reduce_t2"
                result["reduce_qty"] = _raw_reduce2
            if TP_T2_STOP_MOVE_TO_T1:
                result["new_stop_price"] = t1_price
        return result

    # 状态3: T2已达 → 进入T3或全平
    elif current_level == TAKE_PROFIT_LEVEL_T2:
        if skip_t3:
            result["new_level"] = TAKE_PROFIT_LEVEL_DONE
            result["action"] = "close_t3"
            result["reduce_qty"] = current_qty
        else:
            result["new_level"] = TAKE_PROFIT_LEVEL_T3
            result["action"] = "none"
            if direction == "long":
                result["trailing_stop"] = current_price - atr * TP_T3_TRAILING_ATR_MULT
            else:
                result["trailing_stop"] = current_price + atr * TP_T3_TRAILING_ATR_MULT
        return result

    # 状态4: T3追踪中
    elif current_level == TAKE_PROFIT_LEVEL_T3:
        prev_trailing = position_info.get("trailing_stop", 0)
        if direction == "long":
            new_trailing = current_price - atr * TP_T3_TRAILING_ATR_MULT
            if prev_trailing and new_trailing > prev_trailing:
                result["trailing_stop"] = new_trailing
            else:
                result["trailing_stop"] = prev_trailing or new_trailing
            if current_price <= result["trailing_stop"]:
                result["new_level"] = TAKE_PROFIT_LEVEL_DONE
                result["action"] = "close_t3"
                result["reduce_qty"] = current_qty
        else:
            new_trailing = current_price + atr * TP_T3_TRAILING_ATR_MULT
            if prev_trailing and new_trailing < prev_trailing:
                result["trailing_stop"] = new_trailing
            else:
                result["trailing_stop"] = prev_trailing or new_trailing
            if current_price >= result["trailing_stop"]:
                result["new_level"] = TAKE_PROFIT_LEVEL_DONE
                result["action"] = "close_t3"
                result["reduce_qty"] = current_qty
        return result

    return result


# ═══════════════════════════════════════════════════════════
# v6.0 止盈状态机结束
# ═══════════════════════════════════════════════════════════


def _load_state_log():
    """加载状态切换日志"""
    if not STATE_LOG_ENABLED:
        return []
    try:
        if os.path.exists(STATE_LOG_FILE):
            with open(STATE_LOG_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[v6.0] 加载状态日志失败: {e}")
    return []


def _save_state_log(log_list):
    """保存状态切换日志"""
    if not STATE_LOG_ENABLED:
        return
    try:
        trimmed = log_list[-STATE_LOG_MAX_RECORDS:]
        with open(STATE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[v6.0] 保存状态日志失败: {e}")


def log_state_transition(symbol, prev_state, new_state, tech_result, perf_result, confidence):
    """记录一次状态切换"""
    if not STATE_LOG_ENABLED:
        return
    log_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "ts_unix": int(time.time()),
        "symbol": symbol,
        "from_state": prev_state,
        "to_state": new_state,
        "from_label": get_state_label(prev_state),
        "to_label": get_state_label(new_state),
        "confidence": confidence,
        "tech": {
            "total_score": tech_result.get("total_score", 0),
            "state_candidate": tech_result.get("state_candidate", ""),
            "indicators": tech_result.get("indicators", {}),
            "trend_direction": tech_result.get("trend_direction", ""),
            "ma_fast": tech_result.get("ma_fast", 0),
            "ma_slow": tech_result.get("ma_slow", 0),
        },
        "perf": {
            "total_score": perf_result.get("total_score", 0),
            "level": perf_result.get("level", ""),
            "metrics": perf_result.get("metrics", {}),
        },
    }
    log_list = _load_state_log()
    log_list.append(log_entry)
    _save_state_log(log_list)
    print(
        f"[v6.0] 状态切换: {symbol} {get_state_label(prev_state)} → {get_state_label(new_state)} "
        f"(技术{tech_result.get('total_score', 0)}分 / 表现{perf_result.get('total_score', 0)}分)"
    )


def get_state_log(symbol=None, limit=20):
    """获取状态切换历史日志"""
    log_list = _load_state_log()
    if symbol:
        log_list = [l for l in log_list if l.get("symbol") == symbol]
    log_list.reverse()
    return log_list[:limit]


# ═══════════════════════════════════════════
# v6.0 Phase 3: 参数自优化核心函数
# ═══════════════════════════════════════════


def _load_auto_opt_params():
    """加载自优化参数配置（从本地JSON文件持久化）"""
    if not AUTO_OPTIMIZE_ENABLED:
        return {k: dict(v) for k, v in AUTO_OPTIMIZE_PARAMS.items()}
    try:
        if os.path.exists(AUTO_OPT_LOG_FILE):
            with open(AUTO_OPT_LOG_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if "params" in data:
                    result = {k: dict(v) for k, v in AUTO_OPTIMIZE_PARAMS.items()}
                    for key, saved in data["params"].items():
                        if key in result:
                            for sk, sv in saved.items():
                                result[key][sk] = sv
                    return result
    except Exception as e:
        print(f"[v6.0] 加载自优化参数失败: {e}")
    return {k: dict(v) for k, v in AUTO_OPTIMIZE_PARAMS.items()}


def _save_auto_opt_params(params, adjustment_logs=None):
    """保存自优化参数到本地JSON文件"""
    if not AUTO_OPTIMIZE_ENABLED:
        return
    try:
        data = {"params": params, "last_save_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}
        if adjustment_logs is not None:
            data["logs"] = adjustment_logs[-AUTO_OPT_LOG_MAX_RECORDS:]
        with open(AUTO_OPT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[v6.0] 保存自优化参数失败: {e}")


def _add_adjustment_log(logs, param_key, action, old_val, new_val, reason, metrics):
    """添加一条参数调整记录"""
    log_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "ts_unix": int(time.time()),
        "param_key": param_key,
        "param_label": AUTO_OPTIMIZE_PARAMS.get(param_key, {}).get("label", param_key),
        "action": action,
        "old_value": old_val,
        "new_value": new_val,
        "reason": reason,
        "metrics": metrics,
    }
    logs.append(log_entry)
    if len(logs) > AUTO_OPT_LOG_MAX_RECORDS:
        logs = logs[-AUTO_OPT_LOG_MAX_RECORDS:]
    return logs


def _get_trade_r(t):
    """获取交易的R值（兼容多种数据源字段）"""
    if "r_result" in t:
        return float(t.get("r_result", 0))
    if "pnl_r" in t:
        return float(t.get("pnl_r", 0))
    if "pnl" in t:
        pnl = float(t.get("pnl", 0))
        entry = float(t.get("entry_price", 0))
        stop = float(t.get("stop_dist", t.get("stop", 0)))
        if stop > 0 and entry > 0:
            return pnl / stop
        return pnl / max(1, abs(entry)) if entry else pnl
    return 0.0


def _calc_performance_metrics(recent_trades, stop_trades=None):
    """计算近期表现指标"""
    if not recent_trades or len(recent_trades) == 0:
        return {
            "win_rate": 50,
            "profit_factor": 1.0,
            "streak_count": 0,
            "streak_type": "none",
            "stop_rate": 50,
            "recent_total_r": 0,
            "trade_count": 0,
        }
    trades = recent_trades[-AUTO_OPT_WINDOW_MID:]
    n = len(trades)
    wins = sum(1 for t in trades if (_get_trade_r(t)) > 0)
    win_rate = wins / n * 100 if n > 0 else 50
    winning_r = sum(max(0, _get_trade_r(t)) for t in trades)
    losing_r = sum(abs(min(0, _get_trade_r(t))) for t in trades)
    profit_factor = winning_r / losing_r if losing_r > 0 else (3.0 if winning_r > 0 else 1.0)
    streak_count = 0
    streak_type = "none"
    for t in reversed(trades):
        is_win = _get_trade_r(t) > 0
        if is_win:
            if streak_type in ("win", "none"):
                streak_count += 1
                streak_type = "win"
            else:
                break
        else:
            if streak_type in ("lose", "none"):
                streak_count += 1
                streak_type = "lose"
            else:
                break
    if stop_trades is not None and len(stop_trades) > 0:
        stop_count = sum(1 for t in stop_trades[-AUTO_OPT_WINDOW_STOP:] if t.get("exit_type") == "stop_loss")
        total = min(AUTO_OPT_WINDOW_STOP, len(stop_trades))
        stop_rate = stop_count / total * 100 if total > 0 else 50
    else:
        stop_count = sum(1 for t in trades[-AUTO_OPT_WINDOW_STOP:] if _get_trade_r(t) < -0.1)
        total = min(AUTO_OPT_WINDOW_STOP, len(trades))
        stop_rate = stop_count / total * 100 if total > 0 else 50
    short_trades = (
        recent_trades[-AUTO_OPT_WINDOW_SHORT:] if len(recent_trades) >= AUTO_OPT_WINDOW_SHORT else recent_trades
    )
    recent_total_r = sum(_get_trade_r(t) for t in short_trades)
    return {
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "streak_count": streak_count,
        "streak_type": streak_type,
        "stop_rate": round(stop_rate, 1),
        "recent_total_r": round(recent_total_r, 2),
        "trade_count": n,
    }


def check_and_adjust_params(params, recent_trades, adjustment_logs):
    """检查并调整参数"""
    if not AUTO_OPTIMIZE_ENABLED:
        return params, adjustment_logs, []
    if not recent_trades or len(recent_trades) < AUTO_OPT_WINDOW_SHORT:
        return params, adjustment_logs, []
    adjustments = []
    now = time.time()
    metrics = _calc_performance_metrics(recent_trades)
    pending = []

    # 1. 信号质量门槛
    p = params.get("quality_threshold", {})
    if not p.get("locked", False):
        cd = (now - p.get("last_adjust_time", 0)) > (AUTO_OPT_COOLDOWN_HOURS * 3600)
        if cd:
            if metrics["streak_type"] == "win" and metrics["streak_count"] >= AUTO_OPT_STREAK_THRESHOLD:
                pending.append(
                    {
                        "param_key": "quality_threshold",
                        "direction": "decrease",
                        "priority": 10,
                        "reason": f"连盈{metrics['streak_count']}次，降低信号质量门槛",
                    }
                )
            elif metrics["streak_type"] == "lose" and metrics["streak_count"] >= AUTO_OPT_STREAK_THRESHOLD:
                pending.append(
                    {
                        "param_key": "quality_threshold",
                        "direction": "increase",
                        "priority": 10,
                        "reason": f"连亏{metrics['streak_count']}次，提高信号质量门槛",
                    }
                )

    # 2. 单笔风险比例
    p = params.get("single_trade_risk_pct", {})
    if not p.get("locked", False):
        cd = (now - p.get("last_adjust_time", 0)) > (AUTO_OPT_COOLDOWN_HOURS * 3600)
        if cd and metrics["trade_count"] >= AUTO_OPT_WINDOW_MID:
            if metrics["win_rate"] > AUTO_OPT_WINRATE_HIGH:
                pending.append(
                    {
                        "param_key": "single_trade_risk_pct",
                        "direction": "increase",
                        "priority": 8,
                        "reason": f"胜率{metrics['win_rate']}% > {AUTO_OPT_WINRATE_HIGH}%，提高单笔风险",
                    }
                )
            elif metrics["win_rate"] < AUTO_OPT_WINRATE_LOW:
                pending.append(
                    {
                        "param_key": "single_trade_risk_pct",
                        "direction": "decrease",
                        "priority": 8,
                        "reason": f"胜率{metrics['win_rate']}% < {AUTO_OPT_WINRATE_LOW}%，降低单笔风险",
                    }
                )

    # 3. 盈亏比要求
    p = params.get("min_rr_ratio", {})
    if not p.get("locked", False):
        cd = (now - p.get("last_adjust_time", 0)) > (AUTO_OPT_COOLDOWN_HOURS * 3600)
        if cd and metrics["trade_count"] >= AUTO_OPT_WINDOW_MID:
            if metrics["profit_factor"] > AUTO_OPT_PF_HIGH:
                pending.append(
                    {
                        "param_key": "min_rr_ratio",
                        "direction": "decrease",
                        "priority": 6,
                        "reason": f"盈亏比{metrics['profit_factor']} > {AUTO_OPT_PF_HIGH}，降低入场要求",
                    }
                )
            elif metrics["profit_factor"] < AUTO_OPT_PF_LOW:
                pending.append(
                    {
                        "param_key": "min_rr_ratio",
                        "direction": "increase",
                        "priority": 6,
                        "reason": f"盈亏比{metrics['profit_factor']} < {AUTO_OPT_PF_LOW}，提高入场要求",
                    }
                )

    # 4. ATR止损倍数
    p = params.get("atr_stop_mult", {})
    if not p.get("locked", False):
        cd = (now - p.get("last_adjust_time", 0)) > (AUTO_OPT_COOLDOWN_HOURS * 3600)
        if cd and metrics["trade_count"] >= AUTO_OPT_WINDOW_STOP:
            if metrics["stop_rate"] > AUTO_OPT_STOP_RATE_HIGH:
                pending.append(
                    {
                        "param_key": "atr_stop_mult",
                        "direction": "increase",
                        "priority": 7,
                        "reason": f"止损触发率{metrics['stop_rate']}% > {AUTO_OPT_STOP_RATE_HIGH}%，放宽止损",
                    }
                )
            elif metrics["stop_rate"] < AUTO_OPT_STOP_RATE_LOW:
                pending.append(
                    {
                        "param_key": "atr_stop_mult",
                        "direction": "decrease",
                        "priority": 7,
                        "reason": f"止损触发率{metrics['stop_rate']}% < {AUTO_OPT_STOP_RATE_LOW}%，收紧止损",
                    }
                )

    # 5. 时间止损天数
    p = params.get("time_stop_days", {})
    if not p.get("locked", False):
        cd = (now - p.get("last_adjust_time", 0)) > (AUTO_OPT_COOLDOWN_HOURS * 3600)
        if cd and metrics["trade_count"] >= AUTO_OPT_WINDOW_MID:
            if metrics["win_rate"] > 55 and metrics["profit_factor"] > 2.0:
                pending.append(
                    {
                        "param_key": "time_stop_days",
                        "direction": "increase",
                        "priority": 4,
                        "reason": f"胜率{metrics['win_rate']}%+盈亏比{metrics['profit_factor']}，延长持仓时间",
                    }
                )
            elif metrics["win_rate"] < 45 and metrics["profit_factor"] < 1.5:
                pending.append(
                    {
                        "param_key": "time_stop_days",
                        "direction": "decrease",
                        "priority": 4,
                        "reason": f"胜率{metrics['win_rate']}%+盈亏比{metrics['profit_factor']}，缩短持仓时间",
                    }
                )

    if pending:
        pending.sort(key=lambda x: x["priority"], reverse=True)
        adj = pending[0]
        param_key = adj["param_key"]
        direction = adj["direction"]
        param_info = params[param_key]
        current_val = param_info.get("current_value", param_info["base"])
        step = param_info["step"]
        if direction == "increase":
            new_val = min(param_info["max"], current_val + step)
        else:
            new_val = max(param_info["min"], current_val - step)
        if new_val != current_val:
            old_val = current_val
            params[param_key]["current_value"] = new_val
            params[param_key]["last_adjust_time"] = now
            params[param_key]["adjust_count"] = params[param_key].get("adjust_count", 0) + 1
            adjustment_logs = _add_adjustment_log(
                adjustment_logs, param_key, direction, old_val, new_val, adj["reason"], metrics
            )
            adjustments.append(
                f"{param_info['label']}: {old_val}{param_info['unit']} → {new_val}{param_info['unit']} ({adj['reason']})"
            )
            _save_auto_opt_params(params, adjustment_logs)
            print(f"[v6.0] 参数自优化: {param_info['label']} {old_val} → {new_val} ({adj['reason']})")
    return params, adjustment_logs, adjustments


def check_rollback_needed(params, recent_trades, adjustment_logs):
    """回退机制检查"""
    if not AUTO_OPTIMIZE_ENABLED:
        return params, adjustment_logs, False
    if not recent_trades or len(recent_trades) < AUTO_OPT_ROLLBACK_TRADES:
        return params, adjustment_logs, False
    last_adjust_time = 0
    last_adjusted_param = None
    last_adjusted_val = None
    for log in reversed(adjustment_logs):
        if log.get("action") in ("increase", "decrease"):
            last_adjust_time = log.get("ts_unix", 0)
            last_adjusted_param = log.get("param_key")
            last_adjusted_val = log.get("old_value")
            break
    if last_adjust_time == 0 or last_adjusted_param is None:
        return params, adjustment_logs, False
    trades_after = [t for t in recent_trades if t.get("exit_time", 0) > last_adjust_time]
    if len(trades_after) < AUTO_OPT_ROLLBACK_TRADES:
        return params, adjustment_logs, False
    recent_after = trades_after[-AUTO_OPT_ROLLBACK_TRADES:]
    total_r = sum(_get_trade_r(t) for t in recent_after)
    if total_r < AUTO_OPT_ROLLBACK_THRESHOLD:
        param_info = params[last_adjusted_param]
        current_val = param_info.get("current_value", param_info["base"])
        rollback_val = last_adjusted_val
        rollback_val = max(param_info["min"], min(param_info["max"], rollback_val))
        if rollback_val != current_val:
            params[last_adjusted_param]["current_value"] = rollback_val
            adjustment_logs = _add_adjustment_log(
                adjustment_logs,
                last_adjusted_param,
                "rollback",
                current_val,
                rollback_val,
                f"调整后{AUTO_OPT_ROLLBACK_TRADES}笔累积R={total_r:.2f} < {AUTO_OPT_ROLLBACK_THRESHOLD}，自动回退",
                {"total_r_after": total_r, "trade_count": len(recent_after)},
            )
            _save_auto_opt_params(params, adjustment_logs)
            print(f"[v6.0] 参数回退: {param_info['label']} {current_val} → {rollback_val} (调整后累积R={total_r:.2f})")
            return params, adjustment_logs, True
    return params, adjustment_logs, False


def get_effective_param(param_key, default_val):
    """获取实际生效的参数值"""
    if AUTO_OPTIMIZE_ENABLED and param_key in auto_opt_params:
        return auto_opt_params[param_key].get("current_value", default_val)
    return default_val


# Phase 4 Modules A-F


def update_cognitive_bias(result_r):
    if not COGNITIVE_BIAS_ENABLED:
        return
    if result_r > 0:
        cognitive_bias_state["win_streak"] += 1
        cognitive_bias_state["loss_streak"] = 0
    else:
        cognitive_bias_state["loss_streak"] += 1
        cognitive_bias_state["win_streak"] = 0
    cognitive_bias_state["last_trade_time"] = time.time()
    if cognitive_bias_state["win_streak"] >= OVERCONFIDENCE_WIN_STREAK:
        cognitive_bias_state["overconfidence_active"] = True
        print(f"[v6.0 Phase4] overconfidence: {cognitive_bias_state['win_streak']} wins")
    if cognitive_bias_state["loss_streak"] >= REVENGE_TRADING_LOSS_STREAK:
        if time.time() > cognitive_bias_state.get("revenge_cooldown_until", 0):
            cognitive_bias_state["revenge_cooldown_until"] = time.time() + REVENGE_TRADING_COOLDOWN_MIN * 60
            print(f"[v6.0 Phase4] revenge cooldown: {REVENGE_TRADING_COOLDOWN_MIN}min")


def get_cognitive_bias_overlay():
    if not COGNITIVE_BIAS_ENABLED:
        return {}
    overlay = {}
    if cognitive_bias_state.get("overconfidence_active"):
        overlay["quality_threshold_boost"] = OVERCONFIDENCE_RAISE_THRESHOLD
        overlay["position_shrink_pct"] = OVERCONFIDENCE_SHRINK_PCT
        overlay["reason"] = f"win_streak={cognitive_bias_state['win_streak']}"
    if time.time() < cognitive_bias_state.get("revenge_cooldown_until", 0):
        overlay["block_new_signals"] = True
        overlay["reason"] = "revenge_trading_cooldown"
    return overlay


def calc_expected_value_signal(signal, market_state=None, position_info=None):
    if not EXPECTED_VALUE_ENGINE_ENABLED:
        return {"ev_score": 0, "ev_passed": True, "ev_details": {}}
    dims = {}
    trend_score = 50
    if signal.get("trend_strength"):
        trend_score = min(100, max(0, int(signal["trend_strength"])))
    elif market_state and market_state.get("state") in ("trend_early", "trend_mid"):
        trend_score = 70
    elif market_state and market_state.get("state") == "trend_late":
        trend_score = 55
    elif market_state and market_state.get("state") == "sideways":
        trend_score = 30
    dims["trend_strength"] = trend_score
    vol_score = signal.get("volume_score", 50)
    dims["volume_confirmation"] = min(100, max(0, int(vol_score)))
    if market_state:
        state_score_map = {"trend_early": 75, "trend_mid": 65, "trend_late": 45, "sideways": 30}
        state_score = state_score_map.get(market_state.get("state", "sideways"), 40)
        if market_state.get("confirm_count", 0) >= 3:
            state_score += 10
    else:
        state_score = 50
    dims["market_state"] = min(100, state_score)
    rr = signal.get("rr_ratio", 2.0)
    rr_score = min(100, int(rr * 30)) if rr > 0 else 20
    dims["risk_reward"] = rr_score
    timing_score = 50
    if signal.get("momentum_score"):
        timing_score = int(signal["momentum_score"])
    dims["timing"] = min(100, max(0, timing_score))
    total_weight = sum(EV_DIMENSIONS.values())
    weighted_score = sum(dims[k] * v for k, v in EV_DIMENSIONS.items()) / total_weight
    values = list(dims.values())
    if all(v >= 60 for v in values):
        weighted_score = min(100, weighted_score * (1 + EV_CONFIDENCE_BOOST))
    est_win_rate = weighted_score / 100
    avg_rr = max(1.5, rr)
    expected_value = est_win_rate * avg_rr - (1 - est_win_rate)
    passed = expected_value >= EV_MIN_THRESHOLD
    return {
        "ev_score": round(weighted_score, 1),
        "ev_expected_value": round(expected_value, 2),
        "ev_passed": passed,
        "ev_details": dims,
        "ev_min_threshold": EV_MIN_THRESHOLD,
    }


def calc_antifragile_adjustment(equity, peak_equity, signal_quality, position_info=None):
    if not ANTIFRAGILE_ENABLED:
        return {"action": "none", "reason": ""}
    if equity <= 0 or peak_equity <= 0:
        return {"action": "none", "reason": ""}
    drawdown = (peak_equity - equity) / peak_equity * 100
    if drawdown < ANTIFRAGILE_DRAWDOWN_TRIGGER:
        return {"action": "none", "reason": f"drawdown={drawdown:.1f}% < {ANTIFRAGILE_DRAWDOWN_TRIGGER}%"}
    if signal_quality >= ANTIFRAGILE_MIN_QUALITY:
        add_pct = min(ANTIFRAGILE_MAX_ADD_PCT, int(drawdown * 10))
        return {
            "action": "antifragile_add",
            "reason": f"dd={drawdown:.1f}%, q={signal_quality}, add_{add_pct}%",
            "add_pct": add_pct,
        }
    else:
        shrink_pct = min(50, int(drawdown * 5))
        return {
            "action": "antifragile_shrink",
            "reason": f"dd={drawdown:.1f}%, q={signal_quality}, shrink_{shrink_pct}%",
            "shrink_pct": shrink_pct,
        }


def update_trader_state(trade_result=None):
    if not TRADER_STATE_ENABLED:
        return
    today = time.strftime("%Y-%m-%d")
    if trader_state.get("daily_date") != today:
        trader_state["daily_date"] = today
        trader_state["daily_trade_count"] = 0
        trader_state["emotion_streak"] = 0
    trader_state["daily_trade_count"] += 1
    if trade_result is not None:
        if trade_result > 0:
            trader_state["emotion_streak"] = 0
        else:
            trader_state["emotion_streak"] += 1
    session_hours = (time.time() - trader_state.get("session_start_time", time.time())) / 3600
    trader_state["fatigue_level"] = min(100, int(session_hours / TRADER_FATIGUE_HOURS * 50))
    if trader_state.get("daily_trade_count", 0) > TRADER_DAILY_MAX_TRADES:
        trader_state["scarcity_detected"] = True


def save_decision_diary_entry(entry):
    if not STRATEGY_REVIEW_ENABLED:
        return
    entry["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["ts_unix"] = time.time()
    decision_diary.append(entry)
    try:
        data = {"entries": decision_diary[-500:]}
        with open(DECISION_DIARY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[v6.0 Phase4] save diary failed: {e}")


def calc_review_quality(trade_result, decision_quality, execution_quality=None):
    if not STRATEGY_REVIEW_ENABLED:
        return {"total_score": 0, "grade": "N/A"}
    exec_q = execution_quality or decision_quality
    total = (
        decision_quality * REVIEW_QUALITY_WEIGHTS["decision_quality"]
        + exec_q * REVIEW_QUALITY_WEIGHTS["execution_quality"]
        + (min(100, max(0, 50 + trade_result * 20)) * REVIEW_QUALITY_WEIGHTS["result_quality"])
    )
    total = round(total, 1)
    if total >= 80:
        grade = "A"
    elif total >= 70:
        grade = "B"
    elif total >= 60:
        grade = "C"
    else:
        grade = "D"
    return {"total_score": total, "grade": grade}


def update_consensus_state(market_states):
    if not SECOND_LEVEL_THINKING_ENABLED:
        return consensus_state
    if not market_states:
        return consensus_state
    states = list(market_states.values())
    if len(states) < CONSENSUS_MIN_SAMPLES // 5:
        return consensus_state
    state_counts = {}
    for s in states:
        st = s.get("state", "unknown")
        state_counts[st] = state_counts.get(st, 0) + 1
    total = len(states)
    trend_pct = sum(v for k, v in state_counts.items() if k in ("trend_early", "trend_mid", "trend_late")) / total * 100
    consensus_score = min(100, int(trend_pct))
    consensus_state["consensus_score"] = consensus_score
    consensus_state["last_update"] = time.time()
    consensus_state["extreme_high"] = consensus_score >= CONSENSUS_EXTREME_HIGH
    consensus_state["extreme_low"] = consensus_score <= CONSENSUS_EXTREME_LOW
    return consensus_state


def check_second_level_thinking(signal, market_state=None):
    if not SECOND_LEVEL_THINKING_ENABLED:
        return {"pass": True, "reason": "", "barrier_boost": 0}
    cs = consensus_state
    if cs.get("extreme_high"):
        return {
            "pass": True,
            "reason": f"extreme_high({cs['consensus_score']}%), barrier +{SECOND_LEVEL_BARRIER_BOOST}",
            "barrier_boost": SECOND_LEVEL_BARRIER_BOOST,
            "warning": "extreme_bullish",
        }
    if cs.get("extreme_low"):
        signal_dir = signal.get("direction", "")
        if signal_dir == "long":
            return {
                "pass": True,
                "reason": f"extreme_low({cs['consensus_score']}%), contrarian",
                "barrier_boost": 0,
                "opportunity": "contrarian",
            }
    return {"pass": True, "reason": "", "barrier_boost": 0}


def apply_phase4_filters(signal, market_state=None, position_info=None, equity=None, peak_equity=None):
    result = {"passed": True, "reason": "", "adjustments": {}, "warnings": [], "boosts": []}
    if not PHASE4_ENABLED:
        return result
    overlay = get_cognitive_bias_overlay()
    if overlay.get("block_new_signals"):
        result["passed"] = False
        result["reason"] = overlay.get("reason", "blocked")
        result["warnings"].append(f"[A]{overlay.get('reason', '')}")
        return result
    if overlay.get("position_shrink_pct"):
        result["adjustments"]["position_shrink_pct"] = overlay["position_shrink_pct"]
        result["warnings"].append(f"[A]shrink_{overlay['position_shrink_pct']}%")
    if overlay.get("quality_threshold_boost"):
        result["adjustments"]["quality_threshold_boost"] = overlay["quality_threshold_boost"]
    ev = calc_expected_value_signal(signal, market_state, position_info)
    if not ev.get("ev_passed"):
        result["passed"] = False
        result["reason"] = f"ev={ev['ev_expected_value']:.2f} < {EV_MIN_THRESHOLD}"
        result["warnings"].append(f"[B]{result['reason']}")
        return result
    if ev.get("ev_expected_value", 0) > 1.0:
        result["boosts"].append(f"[B]ev={ev['ev_expected_value']:.2f}")
    if equity and peak_equity:
        af = calc_antifragile_adjustment(equity, peak_equity, signal.get("quality_score", 50), position_info)
        if af.get("action") == "antifragile_add":
            result["boosts"].append(f"[C]{af['reason']}")
            result["adjustments"]["antifragile_add_pct"] = af["add_pct"]
        elif af.get("action") == "antifragile_shrink":
            result["adjustments"]["antifragile_shrink_pct"] = af["shrink_pct"]
            result["warnings"].append(f"[C]{af['reason']}")
    t_overlay = get_trader_state_overlay()
    if t_overlay.get("emotion_block"):
        result["passed"] = False
        result["reason"] = t_overlay.get("reason", "emotion_blocked")
        result["warnings"].append(f"[D]{result['reason']}")
        return result
    if t_overlay.get("fatigue_shrink_pct"):
        result["adjustments"]["fatigue_shrink_pct"] = t_overlay["fatigue_shrink_pct"]
    slt = check_second_level_thinking(signal, market_state)
    if slt.get("barrier_boost"):
        result["adjustments"]["second_level_barrier"] = slt["barrier_boost"]
        result["warnings"].append(f"[F]{slt.get('reason', '')}")
    if slt.get("opportunity"):
        result["boosts"].append(f"[F]contrarian:{slt['opportunity']}")
    return result


def get_trader_state_overlay():
    if not TRADER_STATE_ENABLED:
        return {}
    overlay = {}
    if trader_state.get("fatigue_level", 0) >= 50:
        overlay["fatigue_shrink_pct"] = min(50, trader_state["fatigue_level"])
        overlay["reason"] = f"fatigue={trader_state['fatigue_level']}%"
    if trader_state.get("emotion_streak", 0) >= TRADER_EMOTION_STREAK:
        overlay["emotion_block"] = True
        overlay["reason"] = f"emotion_streak={trader_state['emotion_streak']}"
    if trader_state.get("scarcity_detected"):
        overlay["max_risk_boost"] = -50
        overlay["reason"] = "scarcity_detected"
    return overlay


def _update_market_states(feed, state):
    """v6.0: 遍历所有品种更新市场状态"""
    # Phase 4: 更新共识状态
    update_consensus_state(market_state_cache)
    for sym in SYMBOLS.keys():
        try:
            # 获取K线数据
            klines_data = state.get("klines_data", {})
            sym_klines = klines_data.get(sym, [])
            if not sym_klines or len(sym_klines) < TECH_MA_SLOW + 10:
                continue

            # 获取成交量数据
            volumes_list = []
            for k in sym_klines:
                v = k.get("volume") or k.get("vol")
                if v:
                    volumes_list.append(float(v))

            # 1. 技术面识别
            tech_result = calc_tech_market_state(sym_klines, volumes_list or None)

            # 2. 表现面反馈（从state的交易历史获取）
            trade_log = state.get("discipline", {}).get("trade_log", [])
            sym_trades = [t for t in trade_log if t.get("symbol") == sym]
            perf_result = calc_performance_score(sym_trades)

            # 3. 获取之前的状态
            prev_info = market_state_cache.get(sym, {})
            prev_state = prev_info.get("state", MARKET_STATE_SIDEWAYS)
            prev_counter = prev_info.get("confirm_counter", 0)

            # 4. 状态判定
            state_result = determine_market_state(tech_result, perf_result, prev_state, prev_counter)

            # 5. 更新缓存
            market_state_cache[sym] = {
                "state": state_result["state"],
                "prev_state": prev_state,
                "confirm_counter": state_result["confirm_counter"],
                "tech_score": state_result["tech_score"],
                "perf_score": state_result["perf_score"],
                "confidence": state_result["confidence"],
                "trend_direction": state_result["trend_direction"],
                "tech_indicators": tech_result.get("indicators", {}),
                "last_update": time.time(),
                "switched": state_result["switched"],
            }

            # 6. 状态切换日志 + 记录
            if state_result["switched"]:
                log_state_transition(
                    symbol=sym,
                    prev_state=prev_state,
                    new_state=state_result["state"],
                    tech_result=tech_result,
                    perf_result=perf_result,
                    confidence=state_result["confidence"],
                )
        except Exception as _e:
            pass


# ═══════════════════════════════════════════════════════════
# v6.0 函数结束
# ═══════════════════════════════════════════════════════════


def _position_aware_advice(sig, open_positions, price):
    """P-持仓感知 v4：信号发出前与用户真实持仓比对。
    核心原则：信号推送必须服从持仓逻辑 + 信号必须经过噪声过滤。
    v4 新增保护：Whipsaw检测/频率限制/去重/过期快照/手数下限。
    """
    sym = sig.get("symbol")
    sig_lots = sig.get("lots", 0)
    sig_dir = sig.get("direction")
    if not sym:
        return False

    if "reason" not in sig:
        sig["reason"] = ""

    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    # ── 状态清理：定期清理过期的追踪记录（防内存泄漏）──
    _EXPIRE_SEC = 86400  # 24小时过期
    # 清理过期的Whipsaw方向历史
    for _s, _hist in list(_POS_DIRECTION_HISTORY.items()):
        _cleaned = [(d, t) for d, t in _hist if now - t < _EXPIRE_SEC]
        if len(_cleaned) != len(_hist):
            if _cleaned:
                _POS_DIRECTION_HISTORY[_s] = _cleaned
            else:
                del _POS_DIRECTION_HISTORY[_s]
    # 清理过期的平仓冷却期记录
    for _s in list(_LAST_POS_CLOSE.keys()):
        if now - _LAST_POS_CLOSE[_s] > CLOSE_COOLDOWN_SEC * 2:
            del _LAST_POS_CLOSE[_s]
    # 清理过期的持仓快照
    for _s in list(_LAST_POSITION_SNAPSHOT.keys()):
        _snap = _LAST_POSITION_SNAPSHOT[_s]
        if now - _snap.get("time", 0) > _EXPIRE_SEC:
            del _LAST_POSITION_SNAPSHOT[_s]
    # 清理过期的信号推送日志
    for _s in list(_SIGNAL_PUSH_LOG.keys()):
        _log = _SIGNAL_PUSH_LOG[_s]
        _log["timestamps"] = [t for t in _log.get("timestamps", []) if now - t < 3600]
        _daily = _log.get("daily_count", {})
        _log["daily_count"] = {
            d: c
            for d, c in _daily.items()
            if (datetime.strptime(d, "%Y-%m-%d").timestamp() if isinstance(d, str) else 0) > now - 7 * 86400
        }
        if not _log.get("timestamps") and not _log.get("daily_count"):
            if _log.get("last_sig_time", 0) < now - _EXPIRE_SEC:
                del _SIGNAL_PUSH_LOG[_s]
                continue
        _SIGNAL_PUSH_LOG[_s] = _log
    # ── 清理结束 ──

    # 保护8：跨方向信号锁定（30分钟内同品种只推一个方向，避免矛盾信号）
    _dir_lock = _SIGNAL_DIR_LOCK.get(sym)
    if _dir_lock and (now - _dir_lock.get("time", 0)) < SIGNAL_CROSS_DIR_LOCK_SEC:
        _locked_dir = _dir_lock.get("direction", "?")
        if _locked_dir != sig_dir:
            _lock_remain = int((SIGNAL_CROSS_DIR_LOCK_SEC - (now - _dir_lock["time"])) // 60)
            sig["hold_context"] = {"cross_dir_locked": True, "locked_dir": _locked_dir}
            sig["signal_type"] = "信号整合·方向锁定"
            sig["push_suppressed"] = True
            sig["action_advice"] = (
                f"{sym} 30分钟内已推{_locked_dir}方向信号，当前{sig_dir}方向锁定中({_lock_remain}分钟后解锁)，不推矛盾信号。"
            )
            sig["advice_type"] = "cross_dir_locked"
            sig["reason"] += f"【整合·方向锁定】{sym} 近期已推{_locked_dir}方向信号，不推反向{sig_dir}信号。"
            return "blocked"

    # 保护7：信号去重（3分钟内同向重复→抑制）
    _sig_log = _SIGNAL_PUSH_LOG.get(sym, {"timestamps": [], "daily_count": {}, "last_dir": None, "last_sig_time": 0})
    if _sig_log.get("last_dir") == sig_dir and (now - _sig_log.get("last_sig_time", 0)) < SIGNAL_DEDUP_WINDOW_SEC:
        remaining = int(SIGNAL_DEDUP_WINDOW_SEC - (now - _sig_log["last_sig_time"]))
        sig["hold_context"] = {"dedup": True}
        sig["signal_type"] = "信号去重·同向重复"
        sig["push_suppressed"] = True
        sig["action_advice"] = f"{sym} {sig_dir}信号{remaining}秒内已推送过，去重处理。"
        sig["reason"] += f"【去重】{SIGNAL_DEDUP_WINDOW_SEC}秒内同向重复信号已抑制。"
        return "blocked"

    # 保护5：Whipsaw检测（短时间方向反转→噪声）
    _dir_history = _POS_DIRECTION_HISTORY.get(sym, [])
    recent_flip = False
    for d, ts in _dir_history:
        if d != sig_dir and (now - ts) < WHIPSAW_WINDOW_SEC:
            recent_flip = True
            break
    _POS_DIRECTION_HISTORY.setdefault(sym, []).append((sig_dir, now))
    if len(_POS_DIRECTION_HISTORY[sym]) > 6:
        _POS_DIRECTION_HISTORY[sym] = _POS_DIRECTION_HISTORY[sym][-6:]

    if recent_flip and len(_dir_history) >= 2:
        sig["hold_context"] = {"whipsaw": True}
        sig["signal_type"] = "信号噪声·来回打脸"
        sig["push_suppressed"] = True
        sig["action_advice"] = f"{sym} 近期{WHIPSAW_WINDOW_SEC // 60}分钟内方向反复切换(Whipsaw)，建议观望。"
        sig["reason"] += f"【Whipsaw】{sym} 近期{WHIPSAW_WINDOW_SEC // 60}分钟内出现方向反转，信号可信度下降。"
        return "blocked"

    # 保护6：信号频率控制
    _timestamps = [t for t in _sig_log.get("timestamps", []) if now - t < 3600]
    if len(_timestamps) >= MAX_SIGNALS_PER_HOUR:
        sig["hold_context"] = {"rate_limited": True}
        sig["signal_type"] = "频率限制·每小时上限"
        sig["push_suppressed"] = True
        sig["action_advice"] = f"{sym} 本小时已推送{len(_timestamps)}条信号，达到上限{MAX_SIGNALS_PER_HOUR}/小时。"
        sig["reason"] += f"【限频】单品种每小时最多{MAX_SIGNALS_PER_HOUR}条信号。"
        return "blocked"

    _daily = _sig_log.get("daily_count", {})
    daily_count = _daily.get(today, 0)
    if daily_count >= MAX_SIGNALS_PER_DAY:
        sig["hold_context"] = {"rate_limited": True}
        sig["signal_type"] = "频率限制·每日上限"
        sig["push_suppressed"] = True
        sig["action_advice"] = f"{sym} 今日已推送{daily_count}条信号，达到上限{MAX_SIGNALS_PER_DAY}/日。"
        sig["reason"] += f"【限频】单品种每日最多{MAX_SIGNALS_PER_DAY}条信号。"
        return "blocked"

    _SIGNAL_PUSH_LOG[sym] = {
        "timestamps": _timestamps + [now],
        "daily_count": {**_daily, today: daily_count + 1},
        "last_dir": sig_dir,
        "last_sig_time": now,
    }
    # 记录跨方向锁定：该品种30分钟内不再推反向信号
    _SIGNAL_DIR_LOCK[sym] = {"direction": sig_dir, "time": now}

    total_positions = len(open_positions)
    total_lots = sum(int(p.get("lots") or 0) for p in open_positions)

    # 保护3：平仓冷却期
    _lpc = _LAST_POS_CLOSE.get(sym)
    if _lpc is not None and (now - _lpc) < CLOSE_COOLDOWN_SEC:
        sig["hold_context"] = {"cooldown": True}
        sig["signal_type"] = "持仓提示·平仓冷却期"
        sig["push_suppressed"] = True
        remain = int((CLOSE_COOLDOWN_SEC - (now - _lpc)) // 60)
        sig["action_advice"] = f"{sym} 近期有平仓/减仓操作，建议等待{remain}分钟冷却期后再考虑开仓。"
        sig["advice_type"] = "cooldown"
        sig["reason"] += f"【冷却期】{remain}分钟内不推新信号。"
        return "blocked"

    for pos in open_positions:
        if pos.get("sym") != sym:
            continue
        pos_dir = pos.get("direction")
        lots = int(pos.get("lots") or 0)
        mult = _spec_mult(sym)
        avg = float(pos.get("avg") or 0)
        if price and mult and lots:
            float_pnl = (
                ((float(price) - avg) * mult * lots) if pos_dir == "多" else ((avg - float(price)) * mult * lots)
            )
        else:
            float_pnl = None

        _LAST_POSITION_SNAPSHOT[sym] = {"lots": lots, "direction": pos_dir, "avg": avg, "time": now}

        sig["hold_context"] = {
            "held": True,
            "conflict": pos_dir != sig_dir,
            "direction": pos_dir,
            "lots": lots,
            "avg": avg,
            "price": price,
            "float_pnl": round(float_pnl, 1) if float_pnl is not None else None,
            "sig_dir": sig_dir,
            "total_positions": total_positions,
            "total_lots": total_lots,
        }

        # 保护1：反向信号严格抑制
        if pos_dir != sig_dir:
            sig["signal_type"] = "持仓提示·反向信号"
            sig["push_suppressed"] = True
            if float_pnl is not None and float_pnl < 0:
                advice = (
                    "⚠️ 你持有 %s %d手、均价%.1f、浮亏约%.0f元。"
                    "模型现翻向%s。请勿盲目反手或锁仓！"
                    "应先检查止损是否触发——未触发则持有观察，触发则按纪律离场。"
                    % (pos_dir, lots, avg, abs(float_pnl), sig_dir)
                )
            else:
                pnl_txt = ("浮盈约%.0f元" % float_pnl) if float_pnl is not None else "持仓中"
                advice = (
                    "⚠️ 你持有 %s %d手、均价%.1f、%s。"
                    "模型现翻向%s。反向信号不建议操作！"
                    "盈利单可继续持有，若离场优先平仓了结，勿反向开仓。" % (pos_dir, lots, avg, pnl_txt, sig_dir)
                )
            sig["action_advice"] = advice
            sig["advice_type"] = "reverse_blocked"
            sig["reason"] = f"【反向抑制】{advice}"
            return "blocked"

        # 保护2：同向大仓位饱和
        if lots >= POSITION_SATURATION_LOTS:
            sig["signal_type"] = "持仓提示·大仓位维持"
            sig["push_suppressed"] = True
            advice = (
                f"你已持有 {pos_dir} {lots}手（均价{avg}），仓位已较大。"
                f"模型确认{sig_dir}方向，但不建议继续加仓。"
                f"建议：维持现有仓位，设好移动止损。"
            )
            sig["action_advice"] = advice
            sig["advice_type"] = "saturated"
            sig["reason"] += "【仓位饱和】" + advice
            return "blocked"

        new_total = lots + sig_lots
        if new_total > POSITION_SATURATION_LOTS:
            allowed = POSITION_SATURATION_LOTS - lots
            if allowed <= 0:
                return "blocked"
            sig_lots = allowed
            sig["lots"] = allowed

        if lots >= sig_lots * 2:
            advice_type = "hold"
            advice = (
                f"你已持有 {pos_dir} {lots}手（均价{avg}），模型再次确认{sig_dir}方向。"
                f"当前仓位已不小，建议：维持为主，移动止损保护利润。"
            )
        elif float_pnl is not None and float_pnl > 0:
            advice_type = "add_profit"
            advice = (
                f"你已持有 {pos_dir} {lots}手（均价{avg}，浮盈{float_pnl}元），"
                f"模型确认{sig_dir}方向延续。建议：可加{sig_lots}手→总{new_total}手。"
            )
        elif float_pnl is not None and float_pnl < 0:
            advice_type = "caution"
            advice = (
                f"你已持有 {pos_dir} {lots}手（均价{avg}，浮亏{abs(float_pnl)}元），"
                f"模型确认{sig_dir}方向未变。谨慎加仓！"
            )
        else:
            advice_type = "add"
            advice = (
                f"你已持有 {pos_dir} {lots}手（均价{avg}），"
                f"模型触发{sig_dir}信号。建议：可加{sig_lots}手→总{new_total}手。"
            )

        sig["signal_type"] = "持仓提示·同向加仓信号"
        sig["action_advice"] = advice
        sig["advice_type"] = advice_type
        sig["reason"] += f"【持仓·建议】{advice}"
        return True

    # 无持仓：检查组合上下文
    if sig_lots < MIN_SUGGESTED_LOTS:
        sig["hold_context"] = {"min_lots": True}
        sig["signal_type"] = "信号过滤·手数过小"
        sig["push_suppressed"] = True
        sig["action_advice"] = f"{sym} 建议手数{sig_lots}低于最小下单单位{MIN_SUGGESTED_LOTS}，信号静默。"
        sig["reason"] += f"【手数过滤】建议手数{sig_lots} < 最小单位{MIN_SUGGESTED_LOTS}。"
        return "blocked"

    for s in list(_LAST_POSITION_SNAPSHOT.keys()):
        snap = _LAST_POSITION_SNAPSHOT.get(s)
        if snap and (now - snap.get("time", 0)) > 86400:
            del _LAST_POSITION_SNAPSHOT[s]

    if total_positions > 0:
        sig["hold_context"] = {
            "held": False,
            "conflict": False,
            "total_positions": total_positions,
            "total_lots": total_lots,
        }
        long_count = sum(1 for p in open_positions if p.get("direction") == sig_dir)
        short_count = total_positions - long_count

        if (sig_dir == "多" and long_count > 0) or (sig_dir == "空" and short_count > 0):
            advice = (
                f"当前已有{long_count if sig_dir == '多' else short_count}笔{sig_dir}头持仓，"
                f"本信号为新{sig_dir}方向——注意整体敞口。"
            )
            sig["signal_type"] = "持仓提示·同向开仓信号"
            sig["advice_type"] = "same_dir_add"
        elif long_count > 0 and short_count > 0:
            advice = f"当前有多{long_count}空{short_count}笔持仓，本信号为新开仓方向。"
            sig["signal_type"] = "持仓提示·新开仓信号"
            sig["advice_type"] = "new_position"
        else:
            advice = f"当前空仓状态，本信号为{sig_dir}方向新开仓。"
            sig["signal_type"] = "持仓提示·新开仓信号"
            sig["advice_type"] = "new_position"
        sig["action_advice"] = advice
        sig["reason"] += f"【组合·{sig_dir}】{advice}"
    else:
        sig["hold_context"] = {"held": False, "total_positions": 0}
        sig["signal_type"] = "持仓提示·空仓新信号"
        sig["action_advice"] = f"当前空仓，本信号为{sig_dir}方向开仓。"
        sig["advice_type"] = "empty_new"
        sig["reason"] += f"【空仓·{sig_dir}】信号为新方向开仓。"

    _LAST_POSITION_SNAPSHOT[sym] = {"lots": 0, "direction": None, "avg": None, "time": now}
    return False


def _limit_locked_stress(open_positions, feed, pre_close_map=None):
    """P2（2026-08-14）：涨跌停锁死头寸尾部应力加计。
    目的：某亏损头寸因封板平不掉时，真实尾部风险比账面浮亏更大（次日可能再穿一个
    停板）。在日亏线上提前计提该应力，避免「账面浮亏未到 8% 熔断线、实则已临近
    爆雷」的盲区。仅对**亏损**头寸计提，盈利封板风险小、不计。
    判定：
      - 精确模式：feed.last_snap[sym]["pre_close"] 存在 → 推算涨停/跌停价，判定不利
        方向封板（多单封跌停 / 空单封涨停）；
      - 启发式兜底：pre_close 缺失时，浮亏/名义 ≥ STRESS_TRIGGER_MULT×limit_pct
        视为「接近封板尾部」；
      - 两者都只对亏损头寸加计应力 = limit_pct × 名义 × STRESS_BUFFER_MULT。
    返回 (total_stress:float, details:list)。STRESS_LIMIT_LOCKED=False 时直接返回 (0, [])。
    """
    if not STRESS_LIMIT_LOCKED:
        return 0.0, []
    total = 0.0
    details = []
    for p in open_positions:
        sym = p.get("sym")
        direction = p.get("direction")
        lots = p.get("lots") or 0
        avg = p.get("avg")
        if not sym or lots <= 0 or avg is None:
            continue
        spec = SYMBOLS.get(sym, {})
        mult = spec.get("multiplier") or spec.get("mult") or 1
        limit_pct = spec.get("limit_pct") or 0.05
        try:
            avg_f = float(avg)
            lots_f = float(lots)
            mult_f = float(mult)
        except Exception:
            continue
        notional = avg_f * mult_f * lots_f  # 名义金额
        # 实时价（优先 feed.price，其次 last_snap.close）
        px = None
        try:
            px = feed.price(sym) if feed is not None else None
        except Exception:
            px = None
        if px is None and feed is not None and hasattr(feed, "last_snap"):
            _sn = feed.last_snap.get(sym) or {}
            px = _sn.get("close")
        if px is None:
            continue
        try:
            px_f = float(px)
        except Exception:
            continue
        dir_sign = 1 if str(direction) in ("多", "long", "buy", "B", "1") else -1
        unreal = (px_f - avg_f) * mult_f * lots_f * dir_sign  # 浮动盈亏（正=盈）
        if unreal >= 0:
            continue  # 盈利头寸封板风险小，跳过
        loss_ratio = (-unreal / notional) if notional > 0 else 0.0
        # 封板判定
        pre = (pre_close_map or {}).get(sym)
        if pre is None and feed is not None and hasattr(feed, "last_snap"):
            pre = (feed.last_snap.get(sym) or {}).get("pre_close")
        locked = False
        mode = "heuristic"
        if pre:
            try:
                pre_f = float(pre)
                upper = pre_f * (1 + limit_pct)
                lower = pre_f * (1 - limit_pct)
                if dir_sign > 0 and px_f <= lower * 1.002:
                    locked = True
                    mode = "exact"  # 多单封跌停
                elif dir_sign < 0 and px_f >= upper * 0.998:
                    locked = True
                    mode = "exact"  # 空单封涨停
            except Exception:
                pass
        if not locked and loss_ratio < STRESS_TRIGGER_MULT * limit_pct:
            continue  # 启发式也未达门槛
        stress = notional * limit_pct * STRESS_BUFFER_MULT
        total += stress
        details.append(
            {
                "sym": sym,
                "direction": direction,
                "lots": lots_f,
                "unreal": round(unreal, 1),
                "lock_mode": mode,
                "stress": round(stress, 1),
            }
        )
    return total, details


def _sym_group(sym):
    """品种所属板块（化工/黑系/农产品/有色/贵金属/能源/航运）。"""
    try:
        return SYMBOLS.get(sym, {}).get("group") or "其他"
    except Exception:
        return "其他"


def get_pair_corr(a, b, ttl=PAIR_CORR_TTL):
    """任意两品种日收益率滚动相关（带缓存）。返回 float 或 None（数据不足/异常）。"""
    if a == b:
        return 1.0
    key = frozenset({a, b})
    c = PAIR_CORR_CACHE.get(key)
    if c is not None and (time.time() - c[0]) < ttl:
        return c[1]
    try:
        da = load_daily_refreshed(a)
        db = load_daily_refreshed(b)
        if da is None or db is None or len(da) < 30 or len(db) < 30:
            PAIR_CORR_CACHE[key] = (time.time(), None)
            return None
        ra = da["close"].pct_change().dropna().rename(a)
        rb = db["close"].pct_change().dropna().rename(b)
        join = pd.concat([ra, rb], axis=1).dropna()
        if len(join) < 20:
            PAIR_CORR_CACHE[key] = (time.time(), None)
            return None
        corr = float(join[a].corr(join[b]))
        PAIR_CORR_CACHE[key] = (time.time(), corr)
        return corr
    except Exception:
        PAIR_CORR_CACHE[key] = (time.time(), None)
        return None


def portfolio_risk_check(sym, direction, lots, price, stop, equity, open_positions, round_exposures):
    """组合层约束：①总风险预算 ②相关性同向桶上限。
    返回 {ok, lots(调整后), action, reason, corr_hits}。"""
    orig = lots
    mult = _spec_mult(sym)
    per_hand = abs(price - stop) * mult if stop else 0.0
    if per_hand <= 0 or lots <= 0:
        return {"ok": True, "lots": lots, "action": "allow", "reason": "", "corr_hits": []}
    cand_risk = per_hand * lots
    # ── v5 新增：单笔1%风险铁律（趋势跟踪核心规则）──
    per_trade_budget = equity * get_effective_param("single_trade_risk_pct", MAX_SINGLE_TRADE_RISK_PCT) / 100.0
    if cand_risk > per_trade_budget:
        per_trade_fit = max(0, int(per_trade_budget // per_hand)) if per_hand > 0 else 0
        if per_trade_fit < 1:
            return {
                "ok": False,
                "lots": 0,
                "action": "blocked",
                "reason": f"单笔风险{cand_risk:.0f}超1%上限({per_trade_budget:.0f})，缩手数或调整止损",
                "corr_hits": [],
                "risk_level": "per_trade",
            }
        lots = min(lots, per_trade_fit)
        cand_risk = per_hand * lots
    # 现有敞口 = 持仓 + 本轮回已发信号
    exposures = (open_positions or []) + (round_exposures or [])
    open_risk = sum(e["risk"] for e in exposures)
    # ① 总风险预算
    budget = equity * _tc_num("portfolio_risk_pct", PORTFOLIO_RISK_PCT) / 100.0
    remaining = budget - open_risk
    if remaining <= 0:
        return {
            "ok": False,
            "lots": 0,
            "action": "blocked",
            "reason": f"组合风险预算已用尽({open_risk:.0f}/{budget:.0f})",
            "corr_hits": [],
        }
    if cand_risk > remaining:
        fit = max(1, int(remaining // per_hand))
        if fit < lots:
            lots = fit
            cand_risk = per_hand * lots
    # ② 相关性同向桶上限
    corr_hits = []
    for e in exposures:
        if e["sym"] == sym:
            continue
        c = get_pair_corr(sym, e["sym"])
        if c is None or c < PORTFOLIO_CORR_THRESHOLD:
            continue
        if e["direction"] == direction:  # 同向 + 高相关 = 双倍暴露风险
            corr_hits.append((e["sym"], round(c, 2)))
            cap = equity * PORTFOLIO_CORR_BUCKET_PCT / 100.0
            if e["risk"] + cand_risk > cap:
                allow_cand = max(0.0, cap - e["risk"])
                fit = max(0, min(int(allow_cand // per_hand) if per_hand > 0 else 0, lots))
                if fit < lots:
                    lots = fit
                    cand_risk = per_hand * lots
                if lots < 1:
                    return {
                        "ok": False,
                        "lots": 0,
                        "action": "blocked",
                        "reason": f"与{e['sym']}(相关{c:.2f})同向·组合风险超限",
                        "corr_hits": corr_hits,
                    }
    # ③ 板块集中度（#6）：同板块风险总额上限 + 同板块最多 N 个品种
    grp = _sym_group(sym)
    same_grp = [e for e in exposures if e["sym"] != sym and (e.get("group") or _sym_group(e["sym"])) == grp]
    grp_risk = sum(e["risk"] for e in same_grp)
    if len(same_grp) >= PORTFOLIO_SECTOR_MAX_N:
        return {
            "ok": False,
            "lots": 0,
            "action": "blocked",
            "reason": f"{grp}板块已有{len(same_grp)}个敞口(上限{PORTFOLIO_SECTOR_MAX_N})",
            "corr_hits": corr_hits,
        }
    grp_cap = equity * PORTFOLIO_SECTOR_PCT / 100.0
    if grp_risk + cand_risk > grp_cap:
        allow = max(0.0, grp_cap - grp_risk)
        fit = int(allow // per_hand) if per_hand > 0 else 0
        fit = max(0, min(fit, lots))
        if fit < 1:
            return {
                "ok": False,
                "lots": 0,
                "action": "blocked",
                "reason": f"{grp}板块风险已满({grp_risk:.0f}/{grp_cap:.0f})",
                "corr_hits": corr_hits,
            }
        if fit < lots:
            lots = fit
            cand_risk = per_hand * lots
    # ④ 单边净敞口上限（#6）：Σ同方向风险 ≤ 权益 × 1.5%，防「清一色做多」被一根系统性大阴线穿
    same_dir_risk = sum(e["risk"] for e in exposures if e["sym"] != sym and e["direction"] == direction)
    dir_cap = equity * PORTFOLIO_NET_DIR_PCT / 100.0
    if same_dir_risk + cand_risk > dir_cap:
        allow = max(0.0, dir_cap - same_dir_risk)
        fit = int(allow // per_hand) if per_hand > 0 else 0
        fit = max(0, min(fit, lots))
        if fit < 1:
            return {
                "ok": False,
                "lots": 0,
                "action": "blocked",
                "reason": f"单边{direction}净敞口已满({same_dir_risk:.0f}/{dir_cap:.0f})",
                "corr_hits": corr_hits,
            }
        if fit < lots:
            lots = fit
            cand_risk = per_hand * lots
    reasons = []
    if corr_hits and lots < orig:
        reasons.append("相关性同向")
    if grp_risk > 0 and lots < orig:
        reasons.append(f"{grp}板块集中度")
    if same_dir_risk > 0 and lots < orig:
        reasons.append(f"单边{direction}敞口")
    return {
        "ok": lots >= 1,
        "lots": lots,
        "action": ("blocked" if lots < 1 else ("reduced" if lots < orig else "allow")),
        "reason": (("%s降仓至%s手·" % ("+".join(reasons), lots)) if (reasons and lots < orig) else ""),
        "corr_hits": corr_hits,
        "sector": grp,
        "sector_risk": round(grp_risk, 1),
        "dir_risk": round(same_dir_risk, 1),
    }


def _portfolio_recommend(signals, open_positions, state):
    """组合级智能推荐：收集所有信号后，只推最优的1-2个。

    排名依据：
    1. 信号质量分（quality_score）— 越高越好
    2. 盈亏比（rr_ratio）— 越高越好
    3. 持仓相关性 — 有持仓的品种优先（加仓/管理优于新开）
    4. 板块分散 — 避免同板块重复推荐

    推送规则：
    - 0个信号：不推送
    - 1个信号：直接推送
    - 2+个信号：只推排名第1的，其余标记为"暂缓"
    """
    if not signals:
        return

    _MAX_PUSH = 1  # 每轮回最多推送1个信号（避免轰炸）

    # 排序：综合评分 = 质量分*0.4 + 盈亏比*0.3 + 持仓相关*0.3
    def _rank_score(sig):
        qs = sig.get("quality_score", 50) or 50
        rr = float(sig.get("rr_ratio", 2.0) or 2.0)
        rr_norm = min(rr / 3.0, 1.0)  # 归一化到0-1

        # 持仓相关加分：有持仓的品种信号优先
        hc = sig.get("hold_context") or {}
        pos_bonus = 0
        if hc.get("held"):
            pos_bonus = 1.0  # 有持仓的品种加满分
        elif hc.get("total_positions", 0) > 0:
            pos_bonus = 0.5  # 组合有持仓但非本品种

        score = qs * 0.4 + rr_norm * 100 * 0.3 + pos_bonus * 100 * 0.3
        return score

    # 按综合评分排序
    ranked = sorted(signals, key=_rank_score, reverse=True)

    # 选出要推送的信号
    to_push = ranked[:_MAX_PUSH]
    to_suppress = ranked[_MAX_PUSH:]

    # 推送选中的信号
    for sig in to_push:
        sym = sig.get("symbol", "?")
        qs = sig.get("quality_score", "?")
        rr = sig.get("rr_ratio", "?")

        # 构建组合推荐上下文
        total_pos = len(open_positions)
        pos_info = ""
        if total_pos > 0:
            held = [p for p in open_positions if p.get("sym") == sym]
            if held:
                p = held[0]
                pos_info = f"（当前持有{p.get('direction')} {p.get('lots')}手@{p.get('avg')}）"
            else:
                pos_info = f"（当前组合有{total_pos}笔持仓，本品种为新开仓）"
        else:
            pos_info = "（当前空仓，建议新开仓）"

        sig["portfolio_context"] = {
            "total_positions": total_pos,
            "pos_info": pos_info,
            "rank_score": round(_rank_score(sig), 1),
            "is_top_pick": True,
        }

        # 在action_advice中补充组合建议
        _orig_advice = sig.get("action_advice", "") or ""
        sig["action_advice"] = (
            f"📊 组合智能推荐 · {sym} {sig.get('direction', '')} | 质量{qs}分 · 盈亏比{rr} | {pos_info}\n{_orig_advice}"
        )

        notify(sig, voice=not getattr(ARGS, "no_voice", False), banner=True)
        append_chat(sig)
        print(f"\n🎯 [组合推荐] 最优信号: {sym} {sig.get('direction', '')} 质量{qs} 盈亏比{rr} → 已推送")

    # 暂缓的信号
    for sig in to_suppress:
        sym = sig.get("symbol", "?")
        sig["portfolio_deprioritized"] = True
        sig["push_suppressed"] = True
        sig["hold_context"] = sig.get("hold_context", {})
        sig["hold_context"]["deprioritized"] = True
        sig["action_advice"] = (
            f"本轮回优先推荐其他品种，{sym}信号暂缓推送。"
            f"（质量{sig.get('quality_score', '?')} 盈亏比{sig.get('rr_ratio', '?')}）"
        )
        # 仍记录到前端，但标记为已抑制
        append_chat(sig)
        print(f"   📋 [组合推荐·暂缓] {sym} {sig.get('direction', '')} → 优先级不足")


def evaluate(feed, today, last_fire, state, corr_histories):
    fired = []
    _round_signal_buffer = []  # 组合级智能推荐：收集本轮回所有可推送信号，延迟notify
    open_positions = _load_open_positions()  # 组合层：当前真实持仓敞口（整轮不变）
    # P1-4：组合 VaR 预交易闸（risk-manager 校验：1日95% VaR≤3.3%）——当前组合 VaR 已超上限则本轮回禁止新增信号
    _var_hot = False
    _var_cap = _tc_num("portfolio_var_pct_cap", PORTFOLIO_VAR_PCT_CAP)
    try:
        _pv = portfolio_var()
        if _pv.get("ok") and isinstance(_pv.get("var_95_pct"), (int, float)) and _pv["var_95_pct"] > _var_cap:
            _var_hot = True
            print(f"[P1-4 VaR闸] 组合1日95%VaR={_pv['var_95_pct']}% 超上限{_var_cap}%，本轮回禁止新增信号")
    except Exception as _e:
        print(f"[P1-4 VaR闸] 计算异常(忽略): {repr(_e)[:60]}")
    round_exposures = []  # 组合层：本轮回已发信号（供后续品种比对叠加）
    load_dedup_state(last_fire)  # 载入磁盘去重记忆（重启/多进程共享，杜绝连环重发）
    load_pos_alert_dedup()  # 载入持仓触价告警去重记忆（跨重启保留 30min 窗口）
    state["cpos_trade_date"] = get_cpos_cache().get("_meta", {}).get("trade_date", "")
    # #8 市场情绪系统：用上一轮全品种快照计算综合情绪 → 本轮 pipeline 调制
    _sent_band = None
    try:
        _snaps = senteng.build_snapshots_from_runner(state.get("symbols", {}), feed, SYMBOLS)
        _sent_res = senteng.compute(_snaps)
        _sent_band = _sent_res.get("band")
        state["sentiment"] = {
            "score": _sent_res["score"],
            "label": _sent_res["label"],
            "band": _sent_band,
            "bias": _sent_res["bias"],
            "scale": _sent_res["scale"],
            "factors": _sent_res["factors"],
        }
        if _sent_band not in ("neutral",):
            print(f"[#8 情绪] score={_sent_res['score']} label={_sent_res['label']} scale={_sent_res['scale']}")
    except Exception as _e:
        print(f"[#8 情绪] 计算异常(忽略): {repr(_e)[:80]}")
    for sym in SYMBOLS:
        if sym in RUNTIME_DISABLED:  # 自适应恢复：硬禁或待恢复品种，禁止出信号（保留卡片占位）
            recoverable = sym in AUTO_RECOVER_SYMBOLS
            state["symbols"][sym] = {
                "name": SYMBOLS[sym]["name"],
                "price": feed.price(sym),
                "pipe": {
                    "disabled": True,
                    "recoverable": recoverable,
                    "reason": (
                        "自适应恢复候选·近期walk-forward转正后自动解禁" if recoverable else "校准OOS负期望·硬禁"
                    ),
                },
                "last_signal": None,
                "cpos": cpos_for(contract_cpos_key(sym)),
            }
            continue
        gate = GATE_CACHE["gates"].get(sym)
        if gate and gate.get("gated"):  # 动态表现门控：近期真实回测转负，自动暂停发信号（卡片保留）
            state["symbols"][sym] = {
                "name": SYMBOLS[sym]["name"],
                "price": feed.price(sym),
                "pipe": {"gated": True, "reason": f"动态门控·近期真实回测负({gate.get('reason', '')})"},
                "last_signal": None,
                "cpos": cpos_for(contract_cpos_key(sym)),
            }
            continue
        try:
            df_daily = load_daily_refreshed(sym)  # 日线层刷新到当前（akshare 兜底，minishare 无日线权限）
            if df_daily is None or len(df_daily) < 60:
                state["symbols"][sym] = {
                    "name": SYMBOLS[sym]["name"],
                    "price": feed.price(sym),
                    "pipe": {},
                    "last_signal": None,
                    "cpos": cpos_for(contract_cpos_key(sym)),
                }
                continue
            df_5m = feed.get_5m(sym, n_bars=120)
            F = score_F(variety_of(sym), today)
            F2, _info_adj = idim.f_override_for(variety_of(sym), F)  # #1 信息维度定量→F覆盖层
            C = feed.c_flow(sym)
            # #7 HMM 市场状态（仅关注6品种，模型缓存避免重训；非关注品种 hmm_label=None）
            hmm_lbl = None
            if sym in FOCUS_SYMS:
                try:
                    hmm_lbl = rhmm.compute_label(sym, df_daily)
                except Exception as _e:
                    hmm_lbl = None
                    print(f"[HMM] {sym} 标注失败(忽略): {repr(_e)[:60]}")
            # #7 (续) GBM/GARCH 波动率动力学 + 前向情景（同仅关注6品种；非关注 garch_label=None）
            garch_lbl = None
            gbm_res = None
            if sym in FOCUS_SYMS:
                try:
                    gbm_res = gg.compute(sym, df_daily)
                    garch_lbl = gbm_res["vol_state"] if gbm_res else None
                except Exception as _e:
                    garch_lbl = None
                    gbm_res = None
                    print(f"[GBM/GARCH] {sym} 计算失败(忽略): {repr(_e)[:60]}")
            ch = corr_histories.get(sym, [])
            # #6 跨资产宏观语境（全局，独立于品种；无数据→0.0 不影响信号）
            mc = mctx.compute()
            mb = mc.get("macro_bias")
            # #9 支撑压力位识别（从日线算结构位，缓存供 pipeline + exit_plan 消费）
            _sr_res = None
            try:
                _cur_px = feed.price(sym)
                _sr_res = sra.compute_and_cache(sym, df_daily, _cur_px)
            except Exception as _e:
                _sr_res = None
            # #10 GA 权重加载（每品种设置优化权重，无缓存则用默认）
            fd.set_ga_weights_for_symbol(sym)
            pipe = pipeline(
                sym,
                df_daily,
                df_5m,
                _STRAT_CFG,
                corr_hist=ch if len(ch) >= 10 else None,
                c_override=C,
                date=today,
                F_override=F2,
                hmm_label=hmm_lbl,
                macro_label=mb,
                garch_label=garch_lbl,
                gbm_garch=gbm_res,
                feat_mgr=fmg.get_manager(),
                sentiment_label=_sent_band,
                sr_result=_sr_res,
            )
            # #1 信息维度：把资讯/新闻/情绪/另类数据对 F 的调整写入 pipe，供面板/信号解释消费
            try:
                pipe["F_raw"] = round(float(F), 3)
                pipe["F_info"] = F2
                pipe["info_adj"] = _info_adj
                pipe["F_source"] = "info_override" if abs(F2 - float(F)) > 1e-6 else "base"
            except Exception:
                pass
            # 更新 corr_hist（滚动窗口，最多 20 个点）
            ch.append([pipe.get("T_5m", 0), pipe.get("C", 0)])
            while len(ch) > 20:
                ch.pop(0)
            corr_histories[sym] = ch
            # 方向源偏差监控（红线①）：TD vs T5m 一致性滚动统计 + SA 专项敏感度
            if dsm is not None:
                try:
                    pipe["dsm"] = dsm.alert_level(sym, pipe.get("T_D", 0), pipe.get("T_5m", 0))
                except Exception:
                    pass
            price = feed.price(sym)
            # 合约类品种的实时价：minishare 尚未映射/非交易时段无快照时，回退 akshare 该合约日线最新收盘
            if price is None and df_daily is not None and len(df_daily):
                price = float(df_daily["close"].iloc[-1])
            state["symbols"][sym] = {
                "name": SYMBOLS[sym]["name"],
                "price": price,
                "pipe": pipe,
                "last_signal": state["symbols"].get(sym, {}).get("last_signal"),
                "cpos": cpos_for(contract_cpos_key(sym)),
                "gbm_garch": gbm_res,
                "garch_label": garch_lbl,
            }
        except Exception as e:
            # 单品种计算异常不应拖垮整块面板：记日志并跳过本轮该品种
            print(f"[evaluate] 跳过 {sym}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

        # 交易时段门控：无夜盘品种（生猪/鸡蛋等）收盘后(尤其 21:00 起)不再推送信号
        if not _in_session(sym):
            continue
        # P1-3 止损冷静期：同品种近期止损离场后 N 秒内禁止再发新信号（防震荡市反复止损）
        _lse = _LAST_STOP_EXIT.get(sym)
        if _lse is not None and (time.time() - _lse) < _tc_num("stop_cooldown_sec", STOP_COOLDOWN_SEC):
            _remain = int((STOP_COOLDOWN_SEC - (time.time() - _lse)) // 60)
            state["symbols"][sym] = {
                "name": SYMBOLS[sym]["name"],
                "price": price,
                "pipe": {"cooldown": True, "reason": f"止损冷静期(剩{_remain}min禁开)"},
                "last_signal": None,
                "cpos": cpos_for(contract_cpos_key(sym)),
            }
            continue
        if pipe["triggered"] and pipe["dir_T"] != 0 and price:
            # P1-4：组合 VaR 已超上限→本轮回不再新增信号（卡片仍更新展示）
            if _var_hot:
                pipe["var_blocked"] = True
                pipe["var_reason"] = "组合VaR超上限，暂停新增"
                continue
            try:
                # 状态机锁死 / 组合级硬熔断：禁止新开仓（卡片标 🔒锁定 / 🛑熔断）
                if rsm.is_locked():
                    pipe["risk_locked"] = True
                    if rsm.is_halted():
                        pipe["kill_halted"] = True
                        pipe["kill_reason"] = rsm.KILL.reason
                    continue
                dir_T = int(pipe["dir_T"])
                today_key = datetime.now().strftime("%Y-%m-%d")
                # ① 持续性去抖：dir_T 须同号连续 2 轮，单轮临界抖动不触发（根治连环弹窗）
                prev_d = _SIG_PREV_DIR.get(sym)
                _SIG_PREV_DIR[sym] = dir_T
                if prev_d is None or prev_d != dir_T:
                    continue
                # ② 签名去重：相同签名(同方向+同价位桶)在窗口内抑制；不同签名=真实新信号立即推
                sig_hash = _sig_signature(sym, dir_T, price)
                last = last_fire.get(sym)
                is_flip = bool(last and last.get("dir") != dir_T)
                if last and last.get("sig") == sig_hash:
                    # last["t"] 持久化为 Unix 时间戳(float)，避免重启后 datetime→str 比较报错
                    if (datetime.now().timestamp() - float(last["t"])) < DEDUPE_SAME_SIG_MIN * 60:
                        continue
                # 不同签名(反向/新价位)或超出抑制窗口 → 放行（即时推送真实新信号）
                # ★ 立即登记去重记忆（早于 notify/append/log），即使后续调用异常也不会丢失，
                #   避免“写了聊天流却没记去重”导致下一轮又重发（用户最反感重复）。
                last_fire[sym] = {"dir": dir_T, "t": datetime.now().timestamp(), "day": today_key, "sig": sig_hash}
                # 算风控 + 出场 + 信号
                atr_daily = strat_atr(df_daily).iloc[-1]
                if atr_daily is None or pd.isna(atr_daily) or atr_daily <= 0:
                    continue
                # 止损基准 ATR：夜盘用 30 分钟 ATR（中间档），日盘维持日线 ATR（详见 _compute_stop_atr）
                stop_atr, atr_src = _compute_stop_atr(sym, df_5m, atr_daily)
                # P2b：该品种已有持仓手数（open_positions 为 evaluate 开头已加载的真实持仓）
                _held = next((int(p.get("lots") or 0) for p in open_positions if p.get("sym") == sym), 0)
                # P1/P2b（2026-08-19）：T 强度缩放仓位 + 净持仓扣减；手数按日线ATR（稳定）
                rg = risk_gate(
                    sym,
                    price,
                    atr_daily,
                    _STRAT_CFG,
                    t_strength=abs(pipe.get("T_5m") or 0.0),
                    t_thresh=pipe.get("T_thresh_used"),
                    held_lots=_held,
                )
                if not rg["passed"]:
                    # 仍记录触发但风控未过（温和提示）
                    pipe["risk_blocked"] = True
                    continue
                ep = exit_plan(
                    sym,
                    price,
                    pipe["dir_T"],
                    stop_atr,
                    pipe["regime"],
                    _STRAT_CFG,
                    fmg.get_manager(),
                    sr_result=sra.get_cached(sym),
                )
                sig = build_signal(sym, pipe, rg, ep, _STRAT_CFG, entry_ref=price)
                sig["regime_hmm"] = pipe.get("regime_hmm")
                # ★ P-持仓感知 v3：与真实持仓比对，根据持仓逻辑智能处理
                _pa_result = _position_aware_advice(sig, open_positions, price)
                # "blocked" = 反向信号/仓位饱和/冷却期 → 不推送为交易信号
                if _pa_result == "blocked":
                    sig["push_suppressed"] = True
                _sig_conflict = (sig.get("hold_context") or {}).get("conflict", False)
                sig["garch_label"] = pipe.get("garch_label")
                sig["gbm_garch"] = pipe.get("gbm_garch")
                sig["gbm_risk_scale"] = pipe.get("risk_scale")
                sig["macro_ctx"] = mc  # #6 跨资产宏观语境分项+bias（全局，所有品种共用同一宏观背景）
                # 状态机手数缩放（WARNING 0.5×；连续止损 0.8^n 封底 0.2）
                # + #119 回撤水位线渐变降险（graduated，非二值）
                # + #13 事件/数据日历闸门（此前仅 state 展示，本次接入手数缩放）：
                #   重磅数据前 1h 禁新开仓(no_new_open→阻断信号)，临近减仓(reduce→0.5×)
                ev_gate = ec.gate(lookahead_hours=4)
                ev_scale = ec.scale_factor(ev_gate)
                if ev_scale <= 0.0:
                    pipe["event_blocked"] = True
                    pipe["event_reason"] = ev_gate.get("msg", "临近重磅数据，禁止新开仓")
                    continue
                scale = rsm.RISK_FSM.scale()
                dd_scale = ddg.scale_factor()
                sig["dd_scale"] = dd_scale
                sig["event_scale"] = ev_scale
                _gbm_scale = pipe.get("risk_scale") or 1.0
                _combined = round(
                    min(scale, dd_scale, ev_scale, _gbm_scale), 3
                )  # 整改：取较严者而非连乘（含#13事件闸门+GBM高波动降仓）
                if _combined < 1.0:
                    sig["lots"] = max(1, int(round(sig["lots"] * _combined)))
                    sig["risk_scale"] = _combined
                # 组合层约束：相关性同向降仓/否决 + 总风险预算（日亏含浮亏由 P0-1 主源=动态权益回撤负责，非此处）
                pchk = portfolio_risk_check(
                    sym,
                    sig["direction"],
                    sig["lots"],
                    price,
                    sig.get("stop"),
                    _account_equity(),
                    open_positions,
                    round_exposures,
                )
                if not pchk["ok"] and not _sig_conflict:
                    pipe["portfolio_blocked"] = True
                    pipe["portfolio_reason"] = pchk["reason"]
                    continue
                if pchk["lots"] != sig["lots"] and not _sig_conflict:
                    sig["lots"] = pchk["lots"]
                    sig["portfolio_reduced"] = True
                    sig["reason"] += f"（组合层降仓至{pchk['lots']}手·{pchk['reason']}）"
                # ── v5 新增：2:1盈亏比检查（趋势跟踪核心规则）──
                _rr_price = sig.get("price") or price
                _rr_stop = sig.get("stop")
                _rr_t2 = sig.get("t2")
                if _rr_stop and _rr_t2 and float(_rr_stop) != float(_rr_t2):
                    risk_dist = abs(float(_rr_price) - float(_rr_stop))
                    reward_dist = abs(float(_rr_t2) - float(_rr_price))
                    if risk_dist > 0:
                        rr_ratio = reward_dist / risk_dist
                        sig["rr_ratio"] = round(rr_ratio, 2)
                        if rr_ratio < MIN_RR_RATIO:
                            sig["push_suppressed"] = True
                            sig["hold_context"] = {"rr_too_low": True}
                            sig["action_advice"] = (
                                f"{sym} 盈亏比 {rr_ratio:.1f}:1 低于要求 {MIN_RR_RATIO:.0f}:1，"
                                f"潜在盈利不足风险的{MIN_RR_RATIO:.0f}倍，建议观望或缩小目标。"
                            )
                            sig["reason"] += f"【盈亏比不足】{rr_ratio:.1f}:1 < {MIN_RR_RATIO:.0f}:1。"
                        elif not sig.get("push_suppressed"):
                            sig["action_advice"] = (
                                f"盈亏比 {rr_ratio:.1f}:1，符合≥{MIN_RR_RATIO:.0f}:1 要求，风险回报合理。"
                            )

                # ── v5.1 新增：信号质量评分过滤 ──
                if not sig.get("push_suppressed"):
                    _vols = sig.get("_volumes", [])
                    if _vols and len(_vols) >= VOLUME_MA_PERIOD:
                        _vol_ma = sum(_vols[-VOLUME_MA_PERIOD:]) / VOLUME_MA_PERIOD
                    else:
                        _vol_ma = 0
                    _kls = sig.get("_klines", [])
                    _qq = calc_signal_quality_score(sig, _kls, _vol_ma)
                    sig["quality_score"] = _qq["score"]
                    sig["quality_details"] = _qq["details"]
                    if not _qq["passed"]:
                        sig["push_suppressed"] = True
                        sig["hold_context"] = sig.get("hold_context", {})
                        sig["hold_context"]["quality_too_low"] = True
                        sig["action_advice"] = (
                            f"{sym} 信号质量 {_qq['score']}分 < {get_effective_param('quality_threshold', SIGNAL_QUALITY_MIN_SCORE)}分，"
                            f"假突破风险较高，建议观望。"
                        )
                        # v6.0 Phase 4: 知识增强过滤
                        _p4_signal = dict(sig)
                        _p4_signal.setdefault("quality_score", _qq["score"])
                        _p4_result = apply_phase4_filters(_p4_signal, market_state=market_state_cache.get(sym))
                        if not _p4_result.get("passed", True):
                            sig["reason"] += f"【Phase4过滤】{_p4_result.get('reason', '')}。"
                            sig["blocked"] = True
                        for _w in _p4_result.get("warnings", []):
                            sig["reason"] += f"【{_w}】"
                        for _b in _p4_result.get("boosts", []):
                            sig["reason"] += f"✨{_b}"
                        # 应用调整参数
                        if _p4_result.get("adjustments", {}).get("position_shrink_pct"):
                            sig["position_shrink_pct"] = _p4_result["adjustments"]["position_shrink_pct"]
                        sig["reason"] += (
                            f"【信号质量不足】{_qq['score']}分 < {get_effective_param('quality_threshold', SIGNAL_QUALITY_MIN_SCORE)}分。"
                        )

                # ── v5.1 新增：多周期趋势确认 ──
                if not sig.get("push_suppressed"):
                    _htf = sig.get("_higher_tf_klines", [])
                    if _htf and len(_htf) >= HIGHER_TF_MA_SLOW:
                        _tf = get_higher_tf_trend(_htf)
                        sig["higher_tf_trend"] = _tf
                        _tfr = apply_multi_tf_filter(sig, _tf)
                        sig["tf_filter_result"] = _tfr
                        if _tfr.get("pos_scale", 1.0) != 1.0:
                            _orig = sig.get("lots", 1)
                            sig["lots"] = max(1, int(_orig * _tfr["pos_scale"]))
                            sig["reason"] += f"【多周期调整】{_tfr['reason']}"
                        _req_rr = _tfr.get("rr_required", MIN_RR_RATIO)
                        if _req_rr > MIN_RR_RATIO:
                            _cur_rr = sig.get("rr_ratio", 0)
                            if _cur_rr < _req_rr:
                                sig["push_suppressed"] = True
                                sig["hold_context"] = sig.get("hold_context", {})
                                sig["hold_context"]["counter_trend_rr"] = True
                                sig["action_advice"] = (
                                    f"{sym} 逆大周期（{_tf.get('trend')}），"
                                    f"盈亏比要求≥{_req_rr:.1f}:1，当前{_cur_rr:.1f}:1不足，建议观望。"
                                )
                                sig["reason"] += f"【逆周期盈亏比不足】{_cur_rr:.1f}:1 < {_req_rr:.1f}:1。"

                # P2-B（2026-08-14 整改）：逐仓增量 VaR 预交易闸
                # 当前组合 VaR 未超上限时，校验「加入本品种最终计划手数」后组合 VaR 是否越界；
                # 越界则仅否决该品种（不再整轮回禁），避免粗闸误杀其他低风险新仓。
                if not _var_hot and not _sig_conflict:
                    _cvar = candidate_combined_var_pct(sym, sig["direction"], pchk["lots"], price)
                    if _cvar is not None and _cvar > _var_cap:
                        pipe["var_blocked"] = True
                        pipe["var_reason"] = f"加仓后组合VaR≈{_cvar}%超上限{_var_cap}%，本品种暂停"
                        continue
                # 记录本轮回已发信号，供后续品种比对相关性/预算叠加
                if not _sig_conflict:
                    _mult = _spec_mult(sym)
                    round_exposures.append(
                        {
                            "sym": sym,
                            "direction": sig["direction"],
                            "lots": sig["lots"],
                            "risk": abs(price - (sig.get("stop") or 0)) * _mult * sig["lots"],
                            "group": _sym_group(sym),
                        }
                    )
                sig["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sig["price"] = price
                if not _sig_conflict:
                    sig["signal_type"] = "翻转" if is_flip else "建仓"
                sig["atr_src"] = atr_src
                # #122 信号时效 TTL：记录时效基准与有效期
                sig["created_at"] = sig["time"]
                sig["valid_minutes"] = signal_ttl_minutes()
                # #7 执行计划：手数定了还得说清「怎么打进去」（薄盘口大单自伤最贵）
                try:
                    sig["exec_plan"] = exp.plan_execution(
                        sym, sig["lots"], price, sig["direction"], urgency="fast" if is_flip else "normal"
                    )
                except Exception as _e:
                    print(f"[执行计划] {sym} 异常(忽略): {repr(_e)[:60]}")
                sig["reason"] += f"（止损基准:{atr_src}ATR）"
                # 2026-08-20: ensure signal has main contract code
                if not sig.get("contract"):
                    try:
                        _auth = ml._authoritative_contracts()
                        _code = _auth.get(sym)
                        if _code:
                            sig["contract"] = ml.normalize_contract_code(_code)
                    except Exception:
                        pass
                # #4 信号解释：确定性 driver 解释（必出）；若配置了 DEEPSEEK_API_KEY 再叠加 LLM 增强层
                try:
                    _exp = sexp.explain_signal(sig, pipe)
                    if os.environ.get("DEEPSEEK_API_KEY"):
                        _llm_txt = sexp.llm_explain(_exp.get("llm_prompt", ""))
                        if _llm_txt:
                            _exp["llm"] = _llm_txt
                    sig["explanation"] = _exp
                except Exception as _e:
                    sig["explanation"] = {
                        "summary": sig.get("reason", ""),
                        "bullets": [sig.get("reason", "")],
                        "llm_prompt": "",
                    }
                sig["kind"] = "signal"
                _hc = sig.get("hold_context") or {}
                if not _hc.get("cross_dir_locked"):
                    _round_signal_buffer.append(sig)
                else:
                    print(f"   📌 {sym} 方向锁定中({_hc.get('locked_dir')}向)，信号静默不展示")
                log_signal(sig)
                state["signals"].insert(
                    0,
                    {
                        k: sig.get(k)
                        for k in (
                            "time",
                            "created_at",
                            "name",
                            "direction",
                            "signal_type",
                            "lots",
                            "price",
                            "stop",
                            "target",
                            "t1",
                            "t2",
                            "reason",
                            "atr_src",
                            "valid_minutes",
                            "explanation",
                            "regime_hmm",
                            "macro_ctx",
                            "garch_label",
                            "gbm_garch",
                            "risk_scale",
                            "contract",
                            "push_suppressed",
                            "hold_context",
                            "action_advice",
                            "advice_type",
                        )
                    },
                )
                state["signals"] = state["signals"][:30]
                state["symbols"][sym]["last_signal"] = {
                    "direction": sig["direction"],
                    "lots": sig["lots"],
                    "price": sig.get("price"),
                    "stop": sig["stop"],
                    "target": sig["target"],
                    "t1": sig.get("t1"),
                    "t2": sig.get("t2"),
                    "created_at": sig.get("created_at"),
                    "valid_minutes": sig.get("valid_minutes"),
                    "time": sig.get("time"),
                    "explanation": sig.get("explanation"),
                    "macro_ctx": sig.get("macro_ctx"),
                    "garch_label": sig.get("garch_label"),
                    "gbm_garch": sig.get("gbm_garch"),
                    "risk_scale": sig.get("risk_scale"),
                    "push_suppressed": sig.get("push_suppressed"),
                    "hold_context": sig.get("hold_context"),
                    "action_advice": sig.get("action_advice"),
                    "advice_type": sig.get("advice_type"),
                    "contract": sig.get("contract") or _get_main_contract(sym),
                }
            except Exception as e:
                print(f"[evaluate] 信号生成异常 {sym}: {type(e).__name__}: {e}")
                traceback.print_exc()
                continue
            fired.append(sym)
    # ── 组合级智能推荐：收集所有信号后，只推最优的1-2个 ──
    _portfolio_recommend(_round_signal_buffer, open_positions, state)
    save_dedup_state(last_fire)  # 写回磁盘去重记忆（重启/多进程共享）
    save_pos_alert_dedup()  # 写回持仓触价告警去重记忆
    return fired


# ---------------------------------------------------------------------------
# Web 面板
# ---------------------------------------------------------------------------
def start_dashboard(state):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _handle_journal_import(self):
            # F2 交易记录导入：接收 {csv:"..."} 或 {rows:[...]}，合并进 trade_journal.json
            # 抽成方法供 do_GET（兼容）与 do_POST（前端实际用法）共用分发
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length).decode("utf-8", "ignore") if length else "{}"
                body_json = json.loads(raw) if raw.strip() else {}
                rows = body_json.get("rows")
                if rows is None and "csv" in body_json:
                    import csv as _csv

                    _buf = io.StringIO(body_json["csv"])
                    rd = _csv.reader(_buf)
                    header = next(rd, [])
                    keymap = {h.strip(): i for i, h in enumerate(header)}
                    rows = []
                    for line in rd:
                        if not line or not any(c.strip() for c in line):
                            continue
                        rows.append({k: (line[i] if i < len(line) else "") for k, i in keymap.items()})
                if not rows:
                    raise ValueError("未提供 rows 或 csv 数据")
                ok, msg, stat = tj.import_trades(rows)
                body = json.dumps({"ok": ok, "msg": msg, "stat": stat}, ensure_ascii=False, default=str)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def _handle_paper(self):
            # F8 模拟盘沙盒：GET 查状态；POST {action:open|close, symbol, direction, lots, price, ...}
            # 抽成方法供 do_GET（GET 状态查询）与 do_POST（开/平仓）共用分发
            try:
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length).decode("utf-8", "ignore") if length else "{}"
                    body_json = json.loads(raw) if raw.strip() else {}
                    act = body_json.get("action")
                    if act == "open":
                        ok, msg, st = paper_open(
                            body_json.get("symbol"),
                            body_json.get("direction"),
                            body_json.get("lots"),
                            body_json.get("price"),
                            body_json.get("signal_id", ""),
                            body_json.get("strategy", "模拟"),
                        )
                    elif act == "close":
                        ok, msg, st = paper_close(
                            body_json.get("symbol"),
                            body_json.get("price"),
                            body_json.get("lots"),
                            body_json.get("reason", "手动"),
                        )
                    else:
                        ok, msg, st = False, "未知 action", None
                    body = json.dumps(
                        {"ok": ok, "msg": msg, "state": st} if st is not None else {"ok": ok, "msg": msg},
                        ensure_ascii=False,
                        default=str,
                    )
                else:
                    body = json.dumps(paper_state(), ensure_ascii=False, default=str)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self):
            # v6.0: 声明全局变量（避免赋值前引用导致 F823 / UnboundLocalError）
            global AUTO_OPTIMIZE_ENABLED, auto_opt_params, auto_opt_adjustment_logs
            # F5 PWA 静态资源：manifest / service worker / 图标（不走 /api/）
            _asset = self.path.split("?")[0].lstrip("/")
            if _asset in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
                try:
                    _fp = os.path.join(HERE, _asset)
                    if os.path.isfile(_fp):
                        _ct = {
                            "manifest.webmanifest": "application/manifest+json",
                            "sw.js": "application/javascript",
                            "icon-192.png": "image/png",
                            "icon-512.png": "image/png",
                        }[_asset]
                        with open(_fp, "rb") as _f:
                            _data = _f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", _ct)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        self.wfile.write(_data)
                        return
                except Exception:
                    pass
            if self.path.startswith("/api/market-state"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _ms_payload = {
                    "states": market_state_cache,
                    "engine_enabled": MARKET_STATE_ENGINE_ENABLED,
                    "dynamic_params_enabled": DYNAMIC_PARAMS_ENABLED,
                    "timestamp": time.time(),
                    "state_count": len(market_state_cache),
                }
                _ms_body = json.dumps(_ms_payload, ensure_ascii=False, default=str)
                self.wfile.write(_ms_body.encode("utf-8"))
            elif self.path.startswith("/api/market-state/log"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _symbol_param = ""
                _limit_param = 20
                try:
                    _qs = self.path.split("?")[1] if "?" in self.path else ""
                    for _p in _qs.split("&"):
                        if "symbol=" in _p:
                            _symbol_param = _p.split("=")[1]
                        elif "limit=" in _p:
                            _limit_param = int(_p.split("=")[1])
                except Exception:
                    pass
                _logs = get_state_log(symbol=_symbol_param or None, limit=_limit_param)
                _log_payload = {"logs": _logs, "total": len(_logs), "symbol": _symbol_param or "all"}
                self.wfile.write(json.dumps(_log_payload, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # #8 市场情绪系统 API
            elif self.path.split("?")[0] == "/api/sentiment":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _sent_payload = senteng.get_snapshot()
                self.wfile.write(json.dumps(_sent_payload, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # #9 支撑压力位 API
            elif self.path.split("?")[0] == "/api/sr":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _sr_payload = {}
                for _sym in sra._CACHE:
                    _r = sra._CACHE[_sym]
                    _levels = _r.get("levels", [])
                    _sr_payload[_sym] = {
                        "current_price": _r.get("current_price"),
                        "nearest_support": _r.get("nearest_support"),
                        "nearest_resistance": _r.get("nearest_resistance"),
                        "at_support": _r.get("at_support", False),
                        "at_resistance": _r.get("at_resistance", False),
                        "zone": _r.get("zone", "far"),
                        "zone_label": _r.get("zone_label", "无数据"),
                        "nearest_dist_pct": _r.get("nearest_dist_pct", 99.0),
                        "levels": [
                            {
                                "price": l["price"],
                                "role": l["role"],
                                "strength": l["strength"],
                                "touches": l["touches"],
                                "distance_pct": l["distance_pct"],
                            }
                            for l in _levels[:5]
                        ],
                    }
                self.wfile.write(json.dumps(_sr_payload, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # #10 GA 因子挖掘/权重优化 API
            elif self.path.split("?")[0] == "/api/ga_weights":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _ga_payload = {}
                try:
                    _cache = gfm.load_weights("") or {}
                    # load_weights 按 symbol 查；这里全部返回
                    if os.path.exists(gfm.WEIGHTS_FILE):
                        _cache = json.load(open(gfm.WEIGHTS_FILE, encoding="utf-8"))
                    for _sym, _data in _cache.items():
                        _ga_payload[_sym] = {
                            "best_weights": _data.get("best_weights", {}),
                            "best_expR": _data.get("best_expR", 0),
                            "best_calmar": _data.get("best_calmar", 0),
                            "robust_score": _data.get("robust_score", 1.0),
                        }
                except Exception:
                    pass
                self.wfile.write(json.dumps(_ga_payload, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # #11 全市场扫描 API
            elif self.path.split("?")[0] == "/api/scan":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _scan = mscan.scan_all(SYMBOLS, use_cache=True)
                _scan_out = {
                    "summary": _scan.get("summary", {}),
                    "results": _scan.get("results", [])[:20],
                    "elapsed": _scan.get("elapsed", 0),
                }
                self.wfile.write(json.dumps(_scan_out, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # #11 品种筛选 API
            elif self.path.split("?")[0] == "/api/screener":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    _pos = state.get("positions", []) if state else []
                    _held = []
                    for _p in _pos:
                        if isinstance(_p, dict):
                            _held.append(_p.get("symbol"))
                        elif isinstance(_p, str):
                            _held.append(_p)
                    _screen = sscreener.screen(SYMBOLS, held_symbols=_held if _held else None)
                    _payload = json.dumps(_screen, ensure_ascii=False, default=str).encode("utf-8")
                    self.wfile.write(_payload)
                except Exception as _e:
                    import traceback

                    _tb = traceback.format_exc()
                    print(f"[screener error] {_tb}", flush=True)
                    _err = json.dumps(
                        {"error": str(_e), "tb": _tb, "passed": [], "summary": {"n_passed": 0, "n_total": 0}},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.wfile.write(_err)
                return
            # #11 回测可视化 API
            elif self.path.split("?")[0] == "/api/backtest_viz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _viz = viz.data()
                self.wfile.write(json.dumps(_viz, ensure_ascii=False, default=str).encode("utf-8"))
                return
            # v6.0 Phase 3: 参数自优化 API
            elif self.path.split("?")[0] == "/api/auto-optimize":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _ao_payload = {
                    "enabled": AUTO_OPTIMIZE_ENABLED,
                    "params": auto_opt_params,
                    "adjustment_count": len(auto_opt_adjustment_logs),
                    "recent_logs": auto_opt_adjustment_logs[-20:] if auto_opt_adjustment_logs else [],
                }
                self.wfile.write(json.dumps(_ao_payload, ensure_ascii=False, default=str).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/auto-optimize/toggle":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                AUTO_OPTIMIZE_ENABLED = not AUTO_OPTIMIZE_ENABLED
                self.wfile.write(json.dumps({"enabled": AUTO_OPTIMIZE_ENABLED}).encode("utf-8"))
                return
            elif self.path.startswith("/api/auto-optimize/lock"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                _param_key = ""
                _locked = True
                try:
                    _qs = self.path.split("?")[1] if "?" in self.path else ""
                    for _p in _qs.split("&"):
                        if "param_key=" in _p:
                            _param_key = _p.split("=")[1]
                        elif "locked=" in _p:
                            _locked = _p.split("=")[1].lower() == "true"
                except Exception:
                    pass
                if _param_key in auto_opt_params:
                    auto_opt_params[_param_key]["locked"] = _locked
                    _save_auto_opt_params(auto_opt_params, auto_opt_adjustment_logs)
                    self.wfile.write(
                        json.dumps({"success": True, "param": _param_key, "locked": _locked}).encode("utf-8")
                    )
                else:
                    self.wfile.write(json.dumps({"success": False, "error": "param not found"}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/auto-optimize/reset":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                auto_opt_params = {k: dict(v) for k, v in AUTO_OPTIMIZE_PARAMS.items()}
                auto_opt_adjustment_logs = []
                _save_auto_opt_params(auto_opt_params, auto_opt_adjustment_logs)
                self.wfile.write(json.dumps({"success": True, "message": "所有参数已重置为基准值"}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/health":
                # #16 进程看门狗健康检查：崩溃/卡死自检用
                try:
                    _last_age = (time.time() - LAST_CYCLE_TS) if LAST_CYCLE_TS else None
                    body = json.dumps(
                        {
                            "ok": True,
                            "pid": os.getpid(),
                            "uptime_sec": round(time.time() - START_TIME, 1),
                            "last_cycle_ago_sec": round(_last_age, 1) if _last_age is not None else None,
                            "feed_available": FEED_AVAILABLE,
                        },
                        ensure_ascii=False,
                    )
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.startswith("/api/state"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                # 合约以 akshare 权威覆盖为真相源（绕开 OI 滞后/卡月），每次请求实时解析，
                # 不依赖 300s 的 CONTRACT_MAP 刷新，避免重启后读到 trade_config 陈旧初值。
                _auth_contracts = ml._authoritative_contracts()
                for _s, _d in state.get("symbols", {}).items():
                    _d["contract"] = ml.normalize_contract_code(_auth_contracts.get(_s, _s))
                # 2026-08-21: 回填 chat 和 signals 中的主力合约代码，
                # 解决历史条目 contract 字段缺失导致前端不显示主力合约的问题
                for _c in state.get("chat", []):
                    if not _c.get("contract") and _c.get("symbol"):
                        _ct = _auth_contracts.get(_c["symbol"]) or CONTRACT_MAP.get(_c["symbol"])
                        if _ct:
                            _c["contract"] = ml.normalize_contract_code(_ct)
                for _s2 in state.get("signals", []):
                    if not _s2.get("contract") and _s2.get("symbol"):
                        _ct = _auth_contracts.get(_s2["symbol"]) or CONTRACT_MAP.get(_s2["symbol"])
                        if _ct:
                            _s2["contract"] = ml.normalize_contract_code(_ct)
                # 版本号随 /api/state 实时下发，前端侧栏动态渲染（方案 B）
                state["version"] = APP_VERSION
                # ★ 注入自动模拟交易状态
                try:
                    state["paper_trading"] = pti.get_state()
                except Exception:
                    state["paper_trading"] = {"enabled": False, "error": "获取失败"}
                # P1-②：门控品种定性提示（lh/JM 等被动态门控暂停发信号的，让面板露出覆盖缺口）
                try:
                    state["gated_notices"] = _build_gated_notices()
                except Exception:
                    state["gated_notices"] = []
                # ★ 注入账户实际持仓：确保 /api/state 的 positions 与 account_state.json 同步，
                # 使所有书签页（基本面/F、风控/回撤、归因等）都能看到用户真实持仓
                try:
                    _acc_st = at.load_state()
                    _acc_pos = _acc_st.get("positions", {})
                    # 完全替换 state["positions"]（账户持仓为权威源）
                    state["positions"] = {}
                    for _sym, _pos in _acc_pos.items():
                        if isinstance(_pos, dict) and _pos.get("lots", 0) > 0:
                            state["positions"][_sym] = _pos
                except Exception:
                    pass
                self.wfile.write(json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"))
            elif self.path.split("?")[0] == "/api/account":
                try:
                    # 每次刷新账户总览前，先把实际持仓品种钉死到其开仓合约，
                    # 防止 auto_main 动态换月导致盯市价错挂非持仓合约。
                    if FEED:
                        try:
                            FEED._pin_account_positions()
                        except Exception:
                            pass
                    # 以 journal 为真相源自愈 account_state（已实现盈亏/开仓均价），幂等、仅偏差时写盘
                    try:
                        at.heal_from_journal()
                    except Exception:
                        pass
                    prices = {}
                    if FEED:
                        for sym in SYMBOLS:
                            prices[sym] = FEED.price(sym)
                    snap = at.snapshot(prices)
                    # 规范化持仓合约代码显示（FG609 -> FG2609）
                    for _p in snap.get("positions", []):
                        if _p.get("contract"):
                            _p["contract"] = ml.normalize_contract_code(_p["contract"])
                    # CTP 数据源状态（是否连接到真实账户）
                    try:
                        _ctp_acc = am.get_account()
                        snap["ctp_connected"] = _ctp_acc is not None
                        if _ctp_acc:
                            snap["ctp_balance"] = _ctp_acc.get("balance", 0)
                        else:
                            snap["ctp_balance"] = None
                    except Exception:
                        snap["ctp_connected"] = False
                        snap["ctp_balance"] = None
                    # 行情健康指示
                    snap["feed_status"] = "正常" if FEED_AVAILABLE else "离线"
                    snap["feed_last"] = (
                        datetime.fromtimestamp(FEED_LAST_UPDATE).strftime("%H:%M:%S") if FEED_LAST_UPDATE else ""
                    )
                    _age = (datetime.now().timestamp() - FEED_LAST_UPDATE) / 60.0 if FEED_LAST_UPDATE else None
                    snap["data_age_min"] = round(_age, 1) if _age is not None else None
                    # 组合风险热度（A1/B4）
                    snap["heat"] = compute_heat(prices)
                    # 累计手续费（交易所基础费率重算后的全量已平仓手续费合计，与 /api/journal 同源）
                    try:
                        snap["total_fee"] = tj.summary().get("total_fee", 0)
                    except Exception:
                        snap["total_fee"] = 0
                    # 换月预警（B2）：给每个持仓挂上距交割月天数与等级，持仓表内联显示
                    try:
                        for _p in snap.get("positions", []):
                            if _p.get("lots"):
                                _p["rollover"] = rollover_info(_p.get("symbol"), _p.get("contract"))
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8"))
                except Exception as _e:
                    import traceback

                    print(f"[/api/account] ERROR: {type(_e).__name__}: {_e}")
                    traceback.print_exc()
                    # 出错时返回空账户数据，而不是让框架吞掉响应
                    _empty = {"equity": 0, "positions": [], "error": str(_e)}
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(_empty, ensure_ascii=False).encode("utf-8"))
            elif self.path.split("?")[0] == "/api/account_sync":
                # 只读账户同步（minishare 实时盯市 + 对账漂移检测 + 自愈，不接券商 API）
                force = "force=1" in self.path
                heal = "heal=1" in self.path
                try:
                    body = json.dumps(account_marketsync(force=force, heal=heal), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"read_only": True, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/account_heal":
                # 以 journal 为真相源自愈 account_state（已实现盈亏/开仓均价），返回修正明细
                try:
                    ok, changes, _ = at.heal_from_journal()
                    body = json.dumps(
                        {"ok": ok, "changes": changes, "account": at.load_state()}, ensure_ascii=False, default=str
                    )
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/premarket":
                # #125 盘前自动作战清单：聚合只读账户盯市/风险热度/回撤水位线/状态机/硬熔断/行情时效/信号过期
                force = "force=1" in self.path
                try:
                    body = json.dumps(premarket_brief(force=force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"read_only": True, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/crosscheck":
                # #126 多源数据交叉校验：minishare 实时价 / 日线 / 持仓均价 / 信号价 四源比对
                force = "force=1" in self.path
                try:
                    body = json.dumps(cross_source_check(force=force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/pnl_attribution":
                # #127 实盘盈亏 F/T/C 维度归因
                force = "force=1" in self.path
                try:
                    body = json.dumps(pnl_attribution(force=force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/journal":
                # 成交记录器：返回 summary + compare_to_papertrack + 绩效(净值曲线/回撤/Sharpe) + R 倍数
                try:
                    s = tj.summary()
                    c = tj.compare_to_papertrack()
                    all_trades = tj.get_all_trades()
                    prices = {}
                    if FEED:
                        for sym in SYMBOLS:
                            prices[sym] = FEED.price(sym)
                    perf = tj.performance_metrics(prices)
                    curve = tj.equity_curve(prices)
                    intraday = tj.intraday_equity()
                    body = json.dumps(
                        {
                            "summary": s,
                            "compare": c,
                            "performance": perf,
                            "equity_curve": curve,
                            "intraday": intraday,
                            "trades": all_trades,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                except Exception as _e:
                    import traceback

                    traceback.print_exc()
                    body = json.dumps(
                        {
                            "summary": tj.summary(),
                            "compare": {"error": str(_e)},
                            "performance": {},
                            "equity_curve": [],
                            "trades": [],
                            "error": f"journal partial failure: {_e}",
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/journal_strategy":
                # F1 多策略 / 多账户视图：按 strategy 或 account 聚合盈亏
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _gb = _q.get("group_by", ["strategy"])[0]
                    body = json.dumps(tj.by_strategy(group_by=_gb), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/anomaly":
                # 异动扫描结果
                try:
                    body = json.dumps(state.get("anomaly", {}), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/risk":
                # 仓位状态机快照
                try:
                    body = json.dumps(rsm.RISK_FSM.summary(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"state": "NORMAL", "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/killswitch":
                # 组合级硬熔断（#5）：GET 查状态；?action=ack 确认已全平；
                # ?action=reset 人工解除（唯一出口，可带 &peak= 重置峰值权益）
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _act = _p.get("action", "")
                    if _act == "ack":
                        body = json.dumps(rsm.KILL.acknowledge(), ensure_ascii=False, default=str)
                    elif _act == "reset":
                        _peak = _p.get("peak")
                        body = json.dumps(
                            rsm.KILL.reset("面板人工解除", reset_peak_to=float(_peak) if _peak else None),
                            ensure_ascii=False,
                            default=str,
                        )
                        # #119 同步重置回撤水位线峰值（解除即视为新起点，避免旧峰值秒杀）
                        try:
                            ddg.reset_peak(float(_peak)) if _peak else ddg.reset_peak()
                        except Exception:
                            pass
                        print("[熔断] 已人工解除（面板操作）")
                    else:
                        body = json.dumps(rsm.KILL.summary(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"halted": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/drawdown":
                # #119 回撤水位线：返回当前回撤 / 档位 / 降险系数 / 水位线配置
                try:
                    _d = ddg.current()
                    _d["halted"] = rsm.is_halted()
                    body = json.dumps(_d, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/calibration":
                # #120 概率校准 + 置信度分层命中率：方向命中率按 |bias_G| 分桶 + 可靠性图 + Brier
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _force = _p.get("force") == "1"
                    _wh = _p.get("window_h")
                    _wh = int(_wh) if _wh and _wh.isdigit() else 4
                    body = json.dumps(cal.evaluate(window_h=_wh, force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/consistency":
                # #5 训练/服务一致性看门狗：view 最近一次报告；?action=refresh 立即重算
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    if _p.get("action") == "refresh":
                        _cw = cw.check_consistency(focus_symbols=FOCUS_SYMS, disabled_set=RUNTIME_DISABLED)
                        _STATE_CONSISTENCY["report"] = _cw
                        _STATE_CONSISTENCY["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        body = json.dumps(
                            {
                                "ok": True,
                                "refreshed": True,
                                "last_run": _STATE_CONSISTENCY["last_run"],
                                "report": _STATE_CONSISTENCY["report"],
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    else:
                        body = json.dumps(
                            {
                                "ok": True,
                                "last_run": _STATE_CONSISTENCY["last_run"],
                                "report": _STATE_CONSISTENCY["report"],
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/self_check":
                # 系统自检：全链路数据一致性校验
                try:
                    import self_check as sc

                    _report = sc.run_all_checks()
                    body = json.dumps(_report, ensure_ascii=False, default=str)
                except Exception as _e:
                    import traceback

                    traceback.print_exc()
                    body = json.dumps({"ok": False, "error": str(_e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/recalibrate":
                # #3 漂移闭环：GET 查看漂移报告 + 已 staged 候选；?action=stage 后台异步跑扫描并 staging
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _action = _p.get("action", "view")
                    if _action == "stage":
                        if _RECAL_STAGE["running"]:
                            body = json.dumps(
                                {"ok": False, "msg": "staging 进行中，请稍候", "running": True}, ensure_ascii=False
                            )
                        else:
                            import threading

                            threading.Thread(target=_recalibrate_stage_async, daemon=True).start()
                            body = json.dumps(
                                {
                                    "ok": True,
                                    "msg": "已后台启动漂移检测+候选staging（约1-3分钟，稍后GET查看）",
                                    "running": True,
                                },
                                ensure_ascii=False,
                            )
                    else:  # view
                        try:
                            _d = json.load(open(os.path.join(HERE, "calibration_drift.json"), encoding="utf-8"))
                        except Exception:
                            _d = None
                        body = json.dumps(
                            {
                                "ok": True,
                                "drift_report": _d,
                                "staging": _RECAL_STAGE,
                                "note": "view=当前漂移报告；?action=stage 后台产出候选T；"
                                "POST /api/recalibrate?action=apply&symbol=J 或 __all__ 一键落盘",
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/tools":
                # #121 已接入 CLI 工具箱：?tool=calibrate|recalibrate|papertrack_html&action=run|status
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _tool = _p.get("tool", "")
                    _act = _p.get("action", "status")
                    if _act == "run":
                        if _tool not in _TOOL_DEFS:
                            body = json.dumps({"ok": False, "msg": f"未知工具: {_tool}"}, ensure_ascii=False)
                        else:
                            ok, msg = run_tool(_tool)
                            body = json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False)
                    else:
                        with _TOOL_LOCK:
                            body = json.dumps(
                                {"running": _TOOL_STATE["running"], "last": _TOOL_STATE["last"]},
                                ensure_ascii=False,
                                default=str,
                            )
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/broker":
                # #9 经纪商成交回灌：?action=scan(默认预览) | apply | file=<路径>&apply=1
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _apply = _p.get("apply") == "1" or _p.get("action") == "apply"
                    _f = _p.get("file")
                    if _f:
                        from urllib.parse import unquote

                        res = bi.import_file(unquote(_f), apply=_apply)
                    else:
                        res = bi.scan(apply=_apply)
                    if _apply:
                        state["broker_pending"] = bi.scan(apply=False)
                    body = json.dumps(res, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/batch":
                # #8 一键全平 / 一键反手：?mode=flatten|reverse [&symbol=XX] [&apply=1]
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _mode = _p.get("mode", "flatten")
                    _sym = _p.get("symbol") or None
                    if _p.get("apply") == "1":
                        res = apply_batch_orders(_mode, _sym)
                    else:
                        res = build_batch_orders(_mode, _sym)
                    body = json.dumps(res, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/notify":
                # 通知配置（GET 只读）
                try:
                    body = json.dumps(NOTIFY_CFG, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"enabled": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/decompose":
                # 模型健康分解（#2）：F/T/C 留一消融，定位退化维度。带内存缓存。
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _sym = dict(q.split("=", 1) for q in _q.split("&") if "=" in q).get("symbol", "")
                    if not _sym:
                        raise ValueError("缺少 symbol 参数")
                    _cache = getattr(self.server, "decompose_cache", None)
                    if _cache is None:
                        _cache = {}
                        self.server.decompose_cache = _cache
                    if _sym in _cache:
                        _res = _cache[_sym]
                    else:
                        _res = fd.decompose_model_health(_sym, tail=250)
                        _cache[_sym] = _res
                    body = json.dumps(_res, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/chat_days":
                # 所有有消息的日期（降序），供复盘下拉
                try:
                    body = json.dumps(list_chat_days(), ensure_ascii=False)
                except Exception:
                    body = json.dumps([], ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/chat":
                # 指定日期的全部消息（默认今天），用于逐日复盘
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _d = _q.get("date", [None])[0]
                    _feed = load_chat_feed(_d, limit=None)
                    # 2026-08-21: 回填历史消息的主力合约代码（与 /api/state 一致）
                    _auth_contracts = ml._authoritative_contracts()
                    for _m in _feed:
                        if not _m.get("contract") and _m.get("symbol"):
                            _ct = _auth_contracts.get(_m["symbol"]) or CONTRACT_MAP.get(_m["symbol"])
                            if _ct:
                                _m["contract"] = ml.normalize_contract_code(_ct)
                    body = json.dumps(
                        {"date": _d or datetime.now().strftime("%Y-%m-%d"), "count": len(_feed), "messages": _feed},
                        ensure_ascii=False,
                        default=str,
                    )
                except Exception as e:
                    body = json.dumps({"date": "", "count": 0, "messages": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/discipline":
                # 管住手复盘卡（日/周/月纪律评分）+ 收盘快照回看
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    if "list" in _q:
                        # 返回 日/周/月 三套记录列表，供复盘页下拉填充
                        body = json.dumps(
                            {
                                "daily": dr.list_records(),
                                "weekly": dr.list_weekly_records(),
                                "monthly": dr.list_monthly_records(),
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    elif "date" in _q:
                        _rec = dr.get_record(_q["date"][0])
                        if _rec is None:
                            body = json.dumps({"error": "no_record", "date": _q["date"][0]}, ensure_ascii=False)
                        else:
                            body = json.dumps(_rec, ensure_ascii=False, default=str)
                    elif "week" in _q:
                        _rec = dr.get_weekly_record(_q["week"][0])
                        if _rec is None:
                            body = json.dumps({"error": "no_record", "week": _q["week"][0]}, ensure_ascii=False)
                        else:
                            body = json.dumps(_rec, ensure_ascii=False, default=str)
                    elif "month" in _q:
                        _rec = dr.get_monthly_record(_q["month"][0])
                        if _rec is None:
                            body = json.dumps({"error": "no_record", "month": _q["month"][0]}, ensure_ascii=False)
                        else:
                            body = json.dumps(_rec, ensure_ascii=False, default=str)
                    else:
                        body = json.dumps(state.get("discipline", {}), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"daily": {}, "weekly": {}, "monthly": {}, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/signals":
                # 全量信号日志：供成交记录器按「品种+方向」过滤勾选，杜绝串品种误选
                try:
                    _sigs = []
                    if os.path.exists(SIGNAL_LOG):
                        try:
                            _sigs = json.load(open(SIGNAL_LOG, encoding="utf-8"))
                        except Exception:
                            _sigs = []
                    _sigs = sorted(_sigs, key=lambda s: s.get("time", ""), reverse=True)[:500]
                    # #122 信号时效 TTL：为每个信号补算过期状态
                    _sigs = [annotate_signal_ttl(s) for s in _sigs]
                    body = json.dumps(_sigs, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.startswith("/api/cpos_rank"):
                # 席位态度榜（龙虎榜 C_pos 排序：偏多/偏空）
                # 支持 ?date=YYYYMMDD 回看历史交易日
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _d = _q.get("date", [None])[0]
                    body = json.dumps(cpos_ranking(date=_d), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"total": 0, "bullish": [], "bearish": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/export":
                # CSV 导出：type=journal|performance|discipline&kind=daily|weekly|monthly
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _type = _q.get("type", ["journal"])[0]
                    _kind = _q.get("kind", ["daily"])[0]
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    if _type == "journal":
                        trades = tj._load()["trades"]
                        w.writerow(
                            [
                                "id",
                                "time",
                                "symbol",
                                "direction",
                                "lots",
                                "entry_price",
                                "exit_price",
                                "exit_time",
                                "exit_reason",
                                "pnl",
                                "signal_id",
                                "stop_dist",
                                "strategy",
                                "account",
                                "note",
                            ]
                        )
                        for t in trades:
                            w.writerow(
                                [
                                    t.get("id"),
                                    t.get("time"),
                                    t.get("symbol"),
                                    t.get("direction"),
                                    t.get("lots"),
                                    t.get("entry_price"),
                                    t.get("exit_price"),
                                    t.get("exit_time"),
                                    t.get("exit_reason"),
                                    t.get("pnl"),
                                    t.get("signal_id"),
                                    t.get("stop_dist"),
                                    t.get("strategy", ""),
                                    t.get("account", "主账户"),
                                    t.get("note", ""),
                                ]
                            )
                        fname = "trade_journal.csv"
                    elif _type == "performance":
                        s = tj.summary()
                        p = tj.performance_metrics()
                        w.writerow(["指标", "数值"])
                        for k, v in p.items():
                            if isinstance(v, dict):
                                v = json.dumps(v, ensure_ascii=False)
                            w.writerow([k, v])
                        w.writerow([])
                        w.writerow(["--- 成交统计 ---", ""])
                        for k, v in s.items():
                            if isinstance(v, dict):
                                v = json.dumps(v, ensure_ascii=False)
                            w.writerow([k, v])
                        fname = "performance.csv"
                    else:  # discipline
                        if _kind == "weekly":
                            recs = dr.list_weekly_records()
                        elif _kind == "monthly":
                            recs = dr.list_monthly_records()
                        else:
                            recs = dr.list_records()
                        w.writerow(
                            [
                                "date",
                                "score",
                                "grade",
                                "trades_opened",
                                "signal_trades",
                                "impulse_trades",
                                "manual_records",
                                "lock_violations",
                                "period_pnl",
                                "ops_count",
                            ]
                        )
                        for r in recs:
                            w.writerow(
                                [
                                    r.get("date") or r.get("friday") or r.get("month"),
                                    r.get("score"),
                                    r.get("grade"),
                                    r.get("trades_opened"),
                                    r.get("signal_trades"),
                                    r.get("impulse_trades", r.get("manual_trades", 0)),
                                    r.get("lock_violations"),
                                    r.get("period_pnl"),
                                    r.get("ops_count"),
                                ]
                            )
                        fname = f"discipline_{_kind}.csv"
                    csv_text = buf.getvalue()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(csv_text.encode("utf-8-sig"))
                    return
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/journal_import":
                self._handle_journal_import()
                return
            elif self.path.split("?")[0] in ("/api/report_daily", "/api/report_weekly"):
                # F3 自动化日报 / 周报（Markdown）
                try:
                    _kind = "weekly" if self.path.startswith("/api/report_weekly") else "daily"
                    rep = generate_report(_kind)
                    body = json.dumps(rep, ensure_ascii=False, default=str)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body.encode("utf-8"))
                    return
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body.encode("utf-8"))
                    return
            elif self.path.split("?")[0] == "/api/paper":
                self._handle_paper()
                return
            elif self.path.split("?")[0] == "/api/paper-trading":
                # 自动模拟交易引擎 API
                if self.command == "OPTIONS":
                    pti.handle_options(self)
                else:
                    pti.handle_api(self)
                return
            elif self.path.split("?")[0] == "/api/holdings_kline":
                # 持仓K线 + SR位 + 止损止盈标注
                try:
                    from urllib.parse import parse_qs, urlparse

                    q = parse_qs(urlparse(self.path).query)
                    sym = q.get("sym", [None])[0]
                    if not sym or sym not in SYMBOLS:
                        body = json.dumps({"ok": False, "error": "无效品种"}, ensure_ascii=False)
                    else:
                        body = json.dumps(_holdings_kline(sym), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/correlation":
                # 组合相关性矩阵（持仓品种日收益率相关系数）
                try:
                    body = json.dumps(correlation_matrix(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/spread":
                # 价差套利监控
                try:
                    body = json.dumps(spread_monitor(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"pairs": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/stress":
                # 情景压力测试
                try:
                    body = json.dumps(stress_test(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"base_equity": 0, "scenarios": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/stress_corr":
                # #128 相关性崩溃 / 危机趋同 stress 专项
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(correlation_breakdown_stress(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/sector_rotation":
                # #129 板块强弱轮动排序
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(sector_rotation(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/signal_feed":
                # 信号瀑布流：最近 N 条信号时间线
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    limit = int((_q.get("limit", ["30"]))[0])
                    since = _q.get("since", [None])[0]  # 增量拉取：只返回此时间之后的
                    body = json.dumps(_signal_feed(limit=limit, since=since), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/vol_target":
                # #130 波动率目标化头寸：?vol_target_pct=N(默认1.0) &force=1
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    _vt = float((_q.get("vol_target_pct") or ["1.0"])[0])
                    body = json.dumps(
                        vol_target_position(vol_target_pct=_vt, force=_force), ensure_ascii=False, default=str
                    )
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/liquidity_risk":
                # #131 流动性风险 Liquidity-at-Risk：?force=1
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(liquidity_at_risk(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/risk_rules":
                # #132 组合预警规则引擎：?force=1 绕过缓存
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(evaluate_risk_rules(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/playback":
                # #133 交互式历史回放（时间机器）：返回 [最早事件,最晚事件] 逐日组合状态帧序列
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(build_playback_timeline(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/playback_kline":
                # #133 单品种截至 asof 的日线(OHLC) + 交易记录开/平仓标记
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _sym = _q.get("symbol", [""])[0]
                    _asof = _q.get("asof", [None])[0]
                    if not _sym:
                        body = json.dumps({"ok": False, "error": "缺少 symbol"}, ensure_ascii=False)
                    else:
                        body = json.dumps(playback_kline(_sym, _asof), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/sensitivity":
                # #134 参数鲁棒性敏感性地图：?force=1&symbols=FG,SA,JM,jd
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    _sym_str = _q.get("symbols", [""])[0]
                    _symbols = [s for s in _sym_str.split(",") if s] or None
                    body = json.dumps(
                        parameter_sensitivity(force=_force, symbols=_symbols), ensure_ascii=False, default=str
                    )
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/var":
                # #123 组合 VaR / CVaR（参数法 1 日）
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _force = _q.get("force", ["0"])[0] in ("1", "true")
                    body = json.dumps(portfolio_var(force=_force), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/heat":
                # 组合风险热度（A1/B4）：计划风险R合计 + 当前未实现R + 预算对比 + 超标预警
                try:
                    body = json.dumps(compute_heat(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"heat_pct": 0, "status": "正常", "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/watch":
                # 自选到价提醒（B1）：列表 + 当前价 + 距目标%
                try:
                    body = json.dumps(watch_view(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"items": [], "count": 0, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/rollover":
                # 换月预警（B2）：持仓合约距交割月天数 + 换月等级 + 系统主力 vs 交易所真主力比对
                try:
                    body = json.dumps(rollover_overview(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"rows": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/rollover_mismatch":
                # B2 主动提醒：系统在用主力 vs 交易所真实主力（akshare）实时比对
                try:
                    body = json.dumps(rollover_mismatch_check(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"mismatches": [], "count": 0, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/gap":
                # 跳空风险预警（B3）：历史跳空幅度 vs 止损距离
                try:
                    body = json.dumps(gap_risk(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"rows": [], "summary": "计算异常", "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/edge":
                # 分品种 edge（C2）+ 波动率 regime（C3）
                try:
                    body = json.dumps(variety_edge(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"rows": [], "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/alerts":
                # 报警历史（C4）：可按类型过滤
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    kind = (_q.get("kind") or [""])[0]
                    limit = int((_q.get("limit") or ["120"])[0])
                    body = json.dumps(load_alerts(kind, limit), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"items": [], "total": 0, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/montecarlo":
                # #11 蒙特卡洛权益曲线置信区间（重抽样模拟）
                try:
                    body = json.dumps(mc.simulate(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "reason": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/blunder":
                # #12 纪律自动体检
                try:
                    body = json.dumps(state.get("blunder", bc.check()), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/calendar":
                # #13 事件日历闸门：?hours=N
                try:
                    from urllib.parse import parse_qs, urlparse

                    _q = parse_qs(urlparse(self.path).query)
                    _h = int((_q.get("hours") or ["24"])[0])
                    body = json.dumps(ec.upcoming(lookahead_hours=_h), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/dataq":
                # #14 数据质量/陈旧监控
                try:
                    body = json.dumps(state.get("data_quality", dq.check()), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/push":
                # #15 手机推送：GET 查通道状态；?action=test 发测试
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    if "test" in _q:
                        body = json.dumps(pn.test(), ensure_ascii=False, default=str)
                    else:
                        body = json.dumps({"channels": pn.channels_status()}, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"channels": {}, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/backtest_viz":
                # #17 回测可视化（水下曲线/逐笔散点）
                try:
                    body = json.dumps(bv.data(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/fund_metrics":
                # G1 基本面指标（利润/比价/价差）
                try:
                    body = json.dumps(fm.fund_metrics(force=("force=1" in self.path)), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/focus":
                # G2/G4 关注品种盯盘板（评分 + 关键字段）
                try:
                    body = json.dumps(focus_board(force=("force=1" in self.path)), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/journal_session":
                # G4 日夜盘盈亏分解
                try:
                    body = json.dumps(tj.session_performance(), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/journal_list":
                # G2 成交明细列表（含 id / note，供前端备注编辑）
                try:
                    _tj = tj._load()
                    body = json.dumps({"ok": True, "trades": _tj.get("trades", [])}, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] == "/api/symbols_meta":
                # 2026-08-14：返回每个品种的「生效可交割合约代码」（动态主力/次主力），
                # 供前端在所有书签页的商品名后补上合约名（如 螺纹 RB2610）。
                # 2026-08-20 修复：**始终优先使用 _authoritative_contracts()**，而非 FEED.contract_of()
                # 因为 FEED.contract_of() 对有持仓的品种返回「开仓合约」（被 _pin_account_positions 钉死），
                # 而非当前主力合约。展示层一律显示主力合约，持仓合约由账户接口单独展示。
                try:
                    _feed0 = FEED if FEED is not None else ml.feed()
                    _auth_map = ml._authoritative_contracts()
                    _meta = []
                    for _s, _info in SYMBOLS.items():
                        # 优先级：权威主力映射 > FEED 合约映射 > CONTRACT_MAP 兜底
                        _auth_code = _auth_map.get(_s)
                        if _auth_code:
                            _code = _auth_code
                        else:
                            try:
                                _code = _feed0.contract_of(_s)
                            except Exception:
                                _code = None
                            if not _code or _code == _s:
                                _code = CONTRACT_MAP.get(_s, _s)
                        _meta.append(
                            {
                                "symbol": _s,
                                "name": _info.get("name", _s),
                                "code": ml.normalize_contract_code(_code),
                                "mode": ml._contract_mode(_s),
                            }
                        )
                    body = json.dumps({"ok": True, "symbols": _meta}, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.split("?")[0] in ("/", "/index.html", "/four_dim_live.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                # P1-② 注入门控卡片内容（服务端渲染，首屏可见）
                _html = open(HTML_FILE, "rb").read().decode("utf-8")
                _gn = _build_gated_notices()
                if _gn:
                    _gated_html = "".join(
                        '<div style="background:var(--card);border:0.5px solid var(--border);border-left:3px solid var(--amber);border-radius:8px;padding:10px;margin-bottom:8px">'
                        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                        '<span style="font-weight:600">%s</span><span style="font-size:12px;color:var(--mut)">%s</span>'
                        '<span style="margin-left:auto;font-size:11px;padding:1px 7px;border-radius:10px;background:var(--amber)22;color:var(--amber);border:0.5px solid var(--amber)55">门控·暂停发信号</span>'
                        "</div>"
                        '<div style="font-size:12px;color:var(--mut);line-height:1.6">'
                        '模型判负向：期望R <b style="color:var(--amber)">%s</b> / 胜率 <b>%s</b>%s<br>'
                        '<b style="color:var(--fg)">定性建议：</b>%s'
                        "</div></div>"
                        % (
                            g.get("symbol", ""),
                            g.get("name", ""),
                            g.get("expR", "?"),
                            ("%.0f%%" % (g["win_rate"] * 100)) if g.get("win_rate") is not None else "?",
                            (" / 校准OOS %s" % g["calibrated_oos"]) if g.get("calibrated_oos") is not None else "",
                            g.get("advice", ""),
                        )
                        for g in _gn
                    )
                else:
                    _gated_html = '<div class="stamp">当前无门控品种（关注品种均正常发信号）</div>'
                _html = _html.replace("__GATED_CONTENT__", _gated_html)
                self.wfile.write(_html.encode("utf-8"))
            elif self.path.split("?")[0] == "/chart.umd.js":
                _chart_js = os.path.join(HERE, "chart.umd.js")
                if os.path.exists(_chart_js):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    with open(_chart_js, "rb") as _f:
                        self.wfile.write(_f.read())
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path.split("?")[0] == "/api/features":
                # 特性开关：GET 查询所有开关或单个开关状态
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _name = _p.get("name", "")
                    if _name:
                        _feat = fmg.get_manager().get_feature(_name)
                        body = json.dumps(_feat, ensure_ascii=False, default=str)
                    else:
                        _cats = fmg.get_manager().list_by_category()
                        _logs = fmg.get_manager().get_change_log(limit=10)
                        body = json.dumps(
                            {
                                "features": _cats,
                                "change_log": _logs,
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                except Exception as e:
                    body = json.dumps({"error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            elif self.path.split("?")[0].startswith("/paper_dashboard/"):
                # ★ 模拟交易仪表盘静态文件
                _req_path = self.path.split("?")[0]
                # 安全：规范化路径，防止目录穿越
                _safe_path = _req_path.replace("/paper_dashboard/", "", 1)
                _safe_path = _safe_path.lstrip("/").replace("..", "")
                _file_path = os.path.join(HERE, "paper_dashboard", _safe_path)
                _file_path = os.path.normpath(_file_path)
                # 确保在 paper_dashboard 目录内
                if not _file_path.startswith(os.path.join(HERE, "paper_dashboard")):
                    self.send_response(403)
                    self.end_headers()
                    return
                if os.path.isfile(_file_path):
                    # 根据扩展名设置 Content-Type
                    _ext = os.path.splitext(_file_path)[1].lower()
                    _ct = {
                        ".html": "text/html; charset=utf-8",
                        ".js": "application/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8",
                        ".ttf": "font/ttf",
                        ".woff": "font/woff",
                        ".woff2": "font/woff2",
                        ".svg": "image/svg+xml",
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".json": "application/json; charset=utf-8",
                    }.get(_ext, "application/octet-stream")
                    self.send_response(200)
                    self.send_header("Content-Type", _ct)
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    with open(_file_path, "rb") as _f:
                        self.wfile.write(_f.read())
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        # 全局异常保护：任何 handler 抛异常都返回 500 JSON，不让框架吞掉响应
        def handle_one_request(self):
            try:
                super().handle_one_request()
            except Exception as _e:
                import traceback

                print(f"[HTTP] handle_one_request ERROR: {type(_e).__name__}: {_e}")
                traceback.print_exc()
                try:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(_e)}, ensure_ascii=False).encode("utf-8"))
                except Exception:
                    pass

        def log_message(self, *a):
            pass

        def do_POST(self):
            # F2/F8 POST 路由修复：原误置于 do_GET，前端 POST 不可达；此处补齐分发，
            # 逻辑复用 _handle_journal_import / _handle_paper（与 GET 同源，避免漂移）
            if self.path.split("?")[0] == "/api/recalibrate":
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _act = _p.get("action", "")
                    _sym = _p.get("symbol", "__all__")
                    if _act == "apply":
                        ok, msg = _recalibrate_apply(_sym)
                        body = json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False)
                    else:
                        body = json.dumps({"ok": False, "msg": f"unknown action: {_act}"}, ensure_ascii=False)
                except Exception as e:
                    body = json.dumps({"ok": False, "msg": f"apply 异常: {e}"}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            if self.path.split("?")[0] == "/api/journal_import":
                self._handle_journal_import()
                return
            if self.path.split("?")[0] == "/api/positions_reconcile":
                # A：手动对账。用户提交 CTP 真实持仓清单，重建 account_state + journal。
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    req = json.loads(self.rfile.read(n) or b"{}")
                    body = json.dumps(positions_reconcile(req.get("positions")), ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "message": f"请求解析失败：{e}"}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            if self.path.split("?")[0] == "/api/paper":
                self._handle_paper()
                return
            if self.path.split("?")[0] == "/api/paper-trading":
                # 自动模拟交易引擎 API
                pti.handle_api(self)
                return
            if self.path.split("?")[0] == "/api/journal":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    act = body.get("action")
                    if act == "entry":
                        sym = body.get("symbol")
                        direction = body.get("direction")
                        lots = body.get("lots")
                        price = body.get("price")
                        _user_provided_price = price is not None and price != 0
                        print(
                            f"[journal] 开仓请求: sym={sym} dir={direction} lots={lots} price={price} (用户提供={_user_provided_price})"
                        )

                        # ★★ 2026-08-26: 开仓前风控检查 - 熔断/锁定时禁止开仓
                        try:
                            _lock_info = rsm.get_combined_risk_scale()
                            if _lock_info.get("locked") or _lock_info.get("halted"):
                                _reason = (
                                    _lock_info.get("reasons", ["风控锁定中"])[0]
                                    if _lock_info.get("reasons")
                                    else "风控锁定中"
                                )
                                print(f"[journal] 🚫 开仓被风控拦截: {_reason}")
                                body = json.dumps(
                                    {"ok": False, "msg": f"开仓被风控拦截: {_reason}", "risk": _lock_info},
                                    ensure_ascii=False,
                                )
                                self.send_response(200)
                                self.send_header("Content-Type", "application/json; charset=utf-8")
                                self.send_header("Access-Control-Allow-Origin", "*")
                                self.end_headers()
                                self.wfile.write(body.encode("utf-8"))
                                return
                            # 风控缩放：手数按风控系数缩放
                            _scale = _lock_info.get("combined", 1.0)
                            if _scale < 1.0:
                                _orig_lots = lots
                                lots = max(1, int(lots * _scale))
                                print(f"[journal] ⚠️ 风控缩放: 手数从 {_orig_lots} 缩放到 {lots} (系数={_scale})")
                        except Exception as _risk_e:
                            print(f"[journal] ⚠️ 风控检查异常(放行): {_risk_e}")

                        stop = body.get("stop")
                        target = body.get("target")
                        t1 = body.get("t1")
                        t2 = body.get("t2")
                        stop_dist = body.get("stop_dist")
                        auto_note = ""
                        a_tail = None
                        # 开仓未带止损止盈 → 自动按 30min ATR 算好（与账户总览表单一致，避免无止损裸奔）
                        # ★★ 2026-08-26: 强制保护用户价格，永不被自动计算覆盖
                        _original_user_price = price  # 保存用户原始价格
                        if stop is None and target is None and t1 is None and t2 is None:
                            try:
                                a_stop, a_t1, a_t2, a_src, used_px, a_tail = _auto_levels(sym, direction, price)
                                if a_stop is not None:
                                    stop, t1, t2, target = a_stop, a_t1, a_t2, a_t2
                                    if not _user_provided_price and used_px:
                                        price = used_px
                                        print(f"[journal] ⚠️ 价格回退: 用户未提供价格，使用自动检测价 {price}")
                                    else:
                                        # ★ 强制还原用户价格，确保不被 _auto_levels 内部修改
                                        if _user_provided_price and used_px != _original_user_price:
                                            print(
                                                f"[journal] 🔒 价格保护: 恢复用户价 {_original_user_price} (内部计算值 {used_px})"
                                            )
                                        price = _original_user_price
                                    auto_note = (
                                        f"；已自动算止损/止盈(基准{a_src}ATR)：止损{a_stop}/t1平半{a_t1}/t2全平{a_t2}"
                                    )
                            except Exception as _e:
                                print(f"[journal] _auto_levels 异常({sym}): {_e}")
                                a_tail = None
                                # ★ 异常时也要保护用户价格
                                price = _original_user_price
                        print(f"[journal] 最终成交: sym={sym} price={price} stop={stop}")
                        ok, msg, tid = tj.record_entry(
                            sym,
                            direction,
                            lots,
                            price,
                            body.get("signal_id", ""),
                            stop=stop,
                            stop_dist=stop_dist,
                            strategy=body.get("strategy", ""),
                            account=body.get("account", "主账户"),
                        )
                        msg = (msg or "") + auto_note
                        # ★ 打通：同步写 account_tracker，让账户总览持仓表立即可见
                        if ok:
                            try:
                                # 检查账户追踪器中是否已有该品种持仓
                                _st = at.load_state() if hasattr(at, "load_state") else {}
                                _existing_pos = _st.get("positions", {}).get(sym) if isinstance(_st, dict) else None

                                if _existing_pos and _existing_pos.get("direction") != direction:
                                    # 方向冲突：先平掉旧仓，再开新仓
                                    _old_dir = _existing_pos.get("direction")
                                    _old_lots = _existing_pos.get("lots", 0)
                                    _avg_price = _existing_pos.get("avg", price)
                                    print(f"[journal] ⚠️ {sym} 方向冲突：旧仓 {_old_dir} {_old_lots}手，先平后开")
                                    if _old_lots > 0:
                                        _ok_close, _msg_close, _ = at.record_trade(
                                            sym, "close", _old_dir, _old_lots, _avg_price
                                        )
                                        print(f"[journal] 平旧仓: ok={_ok_close} msg={_msg_close}")
                                    _ok2, _msg2, _ = at.record_trade(
                                        sym,
                                        "open",
                                        direction,
                                        lots,
                                        price,
                                        stop=stop,
                                        target=target,
                                        t1=t1,
                                        t2=t2,
                                        tail_enabled=a_tail,
                                    )
                                    print(f"[journal] 开新仓: {sym} open ok={_ok2} msg={_msg2}")
                                else:
                                    _action = "open" if not _existing_pos else "add"
                                    _ok2, _msg2, _ = at.record_trade(
                                        sym,
                                        _action,
                                        direction,
                                        lots,
                                        price,
                                        stop=stop if _action == "open" else None,
                                        target=target if _action == "open" else None,
                                        t1=t1 if _action == "open" else None,
                                        t2=t2 if _action == "open" else None,
                                        tail_enabled=a_tail if _action == "open" else None,
                                    )
                                    print(f"[journal] 账户同步: {sym} {_action} ok={_ok2} msg={_msg2}")

                                if not _ok2:
                                    print(f"[journal] 账户持仓未同步（已静默）: {_msg2}")
                            except Exception as e:
                                import traceback

                                traceback.print_exc()
                                print(f"[journal] 账户持仓同步失败（已静默）: {e}")
                        # 记录开仓纪律事件（含当时状态机状态，供锁死判定）
                        try:
                            dr.log_event(
                                "entry", symbol=sym, direction=direction, lots=lots, risk_state=rsm.RISK_FSM.state
                            )
                        except Exception:
                            pass
                    elif act == "exit":
                        sym = body.get("symbol")
                        direction = body.get("direction")
                        lots = body.get("lots")
                        price = body.get("price")
                        # ★ 2026-08-27: 价格保护 - 记录用户提交的价格
                        print(
                            f"[journal] 平仓请求: sym={sym} dir={direction} lots={lots} price={price} (type={type(price).__name__})"
                        )
                        _user_price = float(price) if price is not None and price != 0 else 0
                        print(f"[journal] 平仓价格验证: 用户价={price} 转换后={_user_price}")
                        ok, msg, pnl = tj.record_exit(sym, direction, lots, _user_price, body.get("reason", "手动"))
                        msg = f"{msg} (pnl={pnl})"
                        # ★ 打通：同步写 account_tracker（自动判平仓/减仓）
                        if ok:
                            try:
                                _ok2, _msg2, _ = at.record_trade(sym, "close", direction, lots, price)
                                if not _ok2:
                                    print(f"[journal] 账户持仓未同步（已静默）: {_msg2}")
                            except Exception as e:
                                print(f"[journal] 账户持仓同步失败（已静默）: {e}")
                            # ★ 2026-08-28: Reset alert guard after user executes a trade
                            #   This ensures the system recognizes the new position state
                            #   and doesn't keep pushing alerts for already-executed levels
                            if sym:
                                _POS_ALERT_GUARD.pop(sym, None)
                                _POS_LEVEL_GUARD.pop(sym, None)
                                _POS_INSURE_GUARD.pop(sym, None)
                                save_pos_alert_dedup()
                                print(f"[journal] 平仓后重置告警守卫: sym={sym}")
                    elif act == "note":
                        # G2：给某条成交补备注（经 trade_journal.update_trade 白名单）
                        _tid = body.get("id")
                        _note = body.get("note", "")
                        ok, msg = tj.update_trade(_tid, {"note": _note})
                    else:
                        ok, msg = False, f"未知 action: {act}"
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/watch":
                # 自选到价提醒（B1）增删改：add / remove / reset / toggle
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    act = body.get("action")
                    if act == "add":
                        ok, msg = watch_add(body.get("symbol"), body.get("op"), body.get("price"), body.get("note", ""))
                    elif act in ("remove", "reset", "toggle"):
                        ok, msg = watch_update(body.get("id"), act)
                    else:
                        ok, msg = False, f"未知 action: {act}"
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/trade":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    act = body.get("action")
                    sym = body.get("symbol")
                    if act in ("open", "add", "close", "reduce"):
                        # ★★ 2026-08-26: 开仓前风控检查 - 仅 open/add 需要检查，close/reduce 不受限
                        if act in ("open", "add"):
                            try:
                                _lock_info = rsm.get_combined_risk_scale()
                                if _lock_info.get("locked") or _lock_info.get("halted"):
                                    _reason = (
                                        _lock_info.get("reasons", ["风控锁定中"])[0]
                                        if _lock_info.get("reasons")
                                        else "风控锁定中"
                                    )
                                    print(f"[trade] 🚫 开仓被风控拦截: {_reason}")
                                    body = json.dumps(
                                        {"ok": False, "msg": f"开仓被风控拦截: {_reason}", "risk": _lock_info},
                                        ensure_ascii=False,
                                    )
                                    self.send_response(200)
                                    self.send_header("Content-Type", "application/json; charset=utf-8")
                                    self.send_header("Access-Control-Allow-Origin", "*")
                                    self.end_headers()
                                    self.wfile.write(body.encode("utf-8"))
                                    return
                                # 风控缩放：手数按风控系数缩放
                                _scale = _lock_info.get("combined", 1.0)
                                if _scale < 1.0:
                                    _orig_lots = body.get("lots")
                                    body["lots"] = max(1, int(int(_orig_lots) * _scale))
                                    print(
                                        f"[trade] ⚠️ 风控缩放: 手数从 {_orig_lots} 缩放到 {body['lots']} (系数={_scale})"
                                    )
                            except Exception as _risk_e:
                                print(f"[trade] ⚠️ 风控检查异常(放行): {_risk_e}")

                        a_tail = None
                        _raw_price = body.get("price")
                        _user_provided_price = _raw_price is not None and _raw_price != 0
                        _original_user_price = _raw_price  # ★ 保存用户原始价格
                        print(
                            f"[trade] 请求: sym={sym} act={act} dir={body.get('direction')} price={_raw_price} (用户提供={_user_provided_price})"
                        )
                        stop = body.get("stop")
                        target = body.get("target")
                        t1 = body.get("t1")
                        t2 = body.get("t2")
                        auto_note = ""
                        if act == "open" and all(v is None for v in (stop, target, t1, t2)):
                            try:
                                a_stop, a_t1, a_t2, a_src, used_px, a_tail = _auto_levels(
                                    sym, body.get("direction"), _raw_price
                                )
                                if a_stop is not None:
                                    stop, t1, t2, target = a_stop, a_t1, a_t2, a_t2
                                    if not _user_provided_price and used_px:
                                        body["price"] = used_px
                                        print(f"[trade] ⚠️ 价格回退: 用户未提供价格，使用自动检测价 {used_px}")
                                    else:
                                        # ★ 强制还原用户价格，确保不被 _auto_levels 内部修改
                                        if _user_provided_price and used_px != _original_user_price:
                                            print(
                                                f"[trade] 🔒 价格保护: 恢复用户价 {_original_user_price} (内部计算值 {used_px})"
                                            )
                                        body["price"] = _original_user_price
                                    auto_note = (
                                        f"；已自动算止损/止盈(基准{a_src}ATR)："
                                        f"止损{a_stop} / t1平半{a_t1} / t2全平{a_t2}"
                                    )
                                else:
                                    # ★ 即使自动算失败，也要保护用户价格
                                    if _user_provided_price:
                                        body["price"] = _original_user_price
                                    auto_note = "；自动算止损止盈失败，请稍后在持仓卡手动补"
                            except Exception as _e:
                                # ★ 异常时也要保护用户价格
                                if _user_provided_price:
                                    body["price"] = _original_user_price
                                auto_note = f"；自动算止损止盈异常({_e})，请稍后手动补"
                        else:
                            # ★ 非 open 操作（add/close/reduce），也要保护用户价格
                            if _user_provided_price:
                                body["price"] = _original_user_price
                        print(f"[trade] 最终: sym={sym} act={act} price={body.get('price')} stop={stop}")
                        ok, msg, _ = at.record_trade(
                            sym,
                            act,
                            body.get("direction"),
                            body.get("lots"),
                            body.get("price"),
                            stop,
                            target,
                            t1,
                            t2,
                            tail_enabled=a_tail if act == "open" else None,
                        )
                        msg = (msg or "") + auto_note
                        # ★ 打通：账户总览的交易记录同步写 trade_journal，使成交明细/绩效/对账/F.T.C 归因实时联动
                        if ok and act in ("open", "add", "close", "reduce"):
                            try:
                                _dir = body.get("direction")
                                _lots = body.get("lots")
                                _px = body.get("price")
                                _signal_id = body.get("signal_id") or "manual"
                                _strategy = body.get("strategy") or "手动"
                                _account = body.get("account") or "主账户"
                                if act in ("open", "add"):
                                    _ok2, _msg2, _tid = tj.record_entry(
                                        sym,
                                        _dir,
                                        _lots,
                                        _px,
                                        signal_id=_signal_id,
                                        stop=stop,
                                        strategy=_strategy,
                                        account=_account,
                                    )
                                    if not _ok2:
                                        msg = f"{msg}（⚠️ 成交记录未同步：{_msg2}）"
                                else:  # close / reduce
                                    _ok2, _msg2, _pnl = tj.record_exit(sym, _dir, _lots, _px, reason=_strategy)
                                    if not _ok2:
                                        msg = f"{msg}（⚠️ 成交记录未同步：{_msg2}）"
                                    # v6.0 Phase 4: 知识增强更新（每笔交易后）
                                    if PHASE4_ENABLED and _ok2:
                                        try:
                                            _trade_r = float(_pnl or 0)
                                            update_cognitive_bias(_trade_r)
                                            update_trader_state(_trade_r)
                                            # v6.0 Phase 4-E: 策略进化复盘记录
                                            _dir_emo = "贪婪" if _trade_r > 0 else ("恐惧" if _trade_r < 0 else "中性")
                                            save_decision_diary_entry(
                                                {
                                                    "symbol": _sym,
                                                    "direction": _dir,
                                                    "trade_result": _trade_r,
                                                    "decision_quality": 60,
                                                    "execution_quality": 70,
                                                    "emotion_label": _dir_emo,
                                                    "reason": "auto_settlement",
                                                }
                                            )
                                            _rq = calc_review_quality(_trade_r, 60)
                                            if _rq.get("total_score", 0) >= 80:
                                                print(
                                                    f"[v6.0 Phase4-E] {_sym} review={_rq.get('grade')} score={_rq.get('total_score')}"
                                                )
                                        except Exception:
                                            pass
                                    # v6.0 Phase 3: 参数自优化检查（每笔平仓/减仓后）
                                    if AUTO_OPTIMIZE_ENABLED and _ok2:
                                        try:
                                            _all_trades = tj.get_all_trades()
                                            if _all_trades and len(_all_trades) >= AUTO_OPT_WINDOW_SHORT:
                                                auto_opt_params, auto_opt_adjustment_logs, _rb = check_rollback_needed(
                                                    auto_opt_params, _all_trades, auto_opt_adjustment_logs
                                                )
                                                auto_opt_params, auto_opt_adjustment_logs, _adj = (
                                                    check_and_adjust_params(
                                                        auto_opt_params, _all_trades, auto_opt_adjustment_logs
                                                    )
                                                )
                                                if _rb or _adj:
                                                    _rb_msg = "⚠️ 参数回退" if _rb else ""
                                                    _adj_msg = " | ".join(_adj) if _adj else ""
                                                    notify(
                                                        {
                                                            "name": "⚙️参数自优化",
                                                            "direction": "",
                                                            "alert_type": "参数自优化",
                                                            "reason": f"{_rb_msg} {_adj_msg}".strip(),
                                                        },
                                                        voice=False,
                                                        banner=False,
                                                    )
                                        except Exception as _aoe:
                                            pass
                            except Exception as _e:
                                msg = f"{msg}（⚠️ 成交记录同步失败：{_e}）"
                    elif act == "equity":
                        # 同步权益前先以 journal 为真相源自愈，确保 realized_pnl 与开仓均价一致；
                        # 并传入当前实时价，使 float_at_sync 用同步瞬间真实行情计算。
                        try:
                            at.heal_from_journal()
                        except Exception:
                            pass
                        prices = {}
                        if FEED:
                            for _sym in SYMBOLS:
                                prices[_sym] = FEED.price(_sym)
                        eq = body.get("equity")
                        ok, msg, st = at.set_equity(eq, prices=prices)
                        if ok:
                            # snapshot 一次给出同步后的动态权益，供前端直观确认
                            try:
                                snap = at.snapshot(prices)
                                msg = (
                                    f"同步权益 {float(eq):,.0f} · "
                                    f"动态权益 {snap.get('equity', 0):,.0f} · "
                                    f"浮动盈亏 {snap.get('float_total', 0):+,.2f}"
                                )
                            except Exception:
                                msg = f"同步权益 {float(eq):,.0f} 成功"
                    else:
                        ok, msg = False, "unknown action"
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/position_levels":
                # 给已有持仓设置/清除止损止盈位（用于触价报警）
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    sym = body.get("symbol")
                    ok, msg, _ = at.set_levels(
                        sym, body.get("stop"), body.get("target"), body.get("t1"), body.get("t2")
                    )
                    # ★ 2026-08-27: 用户修改止盈止损后，立即重置告警守卫
                    #   防止基于旧价位的告警持续触发
                    if ok and sym:
                        _POS_ALERT_GUARD.pop(sym, None)
                        _POS_LEVEL_GUARD.pop(sym, None)
                        _POS_INSURE_GUARD.pop(sym, None)
                        save_pos_alert_dedup()
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/notify":
                # 运行时改写通知配置（并持久化到 trade_config.json）
                global NOTIFY_CFG
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    for k in ("enabled", "native", "voice", "sound"):
                        if k in body:
                            NOTIFY_CFG[k] = bool(body[k])
                    # 持久化
                    try:
                        cfg_path = os.path.join(HERE, "trade_config.json")
                        tc = json.load(open(cfg_path, encoding="utf-8"))
                        tc["notify"] = dict(NOTIFY_CFG)
                        json.dump(tc, open(cfg_path, "w"), ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    ok, msg = True, f"通知配置已更新: {NOTIFY_CFG}"
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg, "cfg": NOTIFY_CFG}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/discipline/snapshot":
                # 手动触发收盘复盘落账：
                #   {"kind":"daily","date":"YYYY-MM-DD"}（默认今天）
                #   {"kind":"weekly","week":"YYYY-MM-DD"}（周五日期）
                #   {"kind":"monthly","month":"YYYY-MM"}
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    _kind = body.get("kind", "daily")
                    if _kind == "weekly":
                        _k = body.get("week") or dr._friday_of(datetime.now()).strftime("%Y-%m-%d")
                        _rec = dr.snapshot_week(_k)
                        ok, msg = True, f"已落账周 {_k}（评分 {_rec.get('score')} {_rec.get('grade')}）"
                    elif _kind == "monthly":
                        _now = datetime.now()
                        _k = body.get("month") or f"{_now.year}-{_now.month:02d}"
                        _rec = dr.snapshot_month(_k)
                        ok, msg = True, f"已落账月 {_k}（评分 {_rec.get('score')} {_rec.get('grade')}）"
                    else:
                        _ds = body.get("date") or datetime.now().strftime("%Y-%m-%d")
                        _rec = dr.snapshot_day(_ds)
                        ok, msg = True, f"已落账 {_ds}（评分 {_rec.get('score')} {_rec.get('grade')}）"
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg}).encode("utf-8"))
                return
            elif self.path.split("?")[0] == "/api/push":
                # #15 手机推送配置：{"section":"telegram|bark|wecom", ...字段} 或 {"action":"test"}
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if body.get("action") == "test":
                        res = pn.test()
                        ok, msg = bool(res.get("sent")), f"已推送通道 {res.get('sent')}"
                    else:
                        sec = body.get("section", "")
                        if sec not in ("telegram", "bark", "wecom"):
                            ok, msg = False, "section 须为 telegram|bark|wecom"
                        else:
                            kw = {k: v for k, v in body.items() if k not in ("section", "action")}
                            ok, msg = pn.configure(sec, **kw)
                except Exception as e:
                    ok, msg = False, str(e)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok, "msg": msg, "channels": pn.channels_status()}).encode("utf-8"))
                return
            if self.path.split("?")[0] == "/api/features/toggle":
                # 特性开关切换：POST /api/features/toggle?name=XXX
                try:
                    _q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    _p = dict(x.split("=", 1) for x in _q.split("&") if "=" in x)
                    _name = _p.get("name", "")
                    if not _name:
                        raise ValueError("缺少 name 参数")
                    _length = int(self.headers.get("Content-Length", 0) or 0)
                    _raw = self.rfile.read(_length).decode("utf-8", "ignore") if _length else "{}"
                    _body = json.loads(_raw) if _raw.strip() else {}
                    _enabled = _body.get("enabled", True)
                    _reason = _body.get("reason", "")
                    _op = _body.get("operator", "manual")
                    _mgr = fmg.get_manager()
                    _result = _mgr.toggle_feature(_name, _enabled, reason=_reason, operator=_op)
                    if _result.get("ok") and _result.get("dangerous"):
                        try:
                            import push_notify as _pn

                            _pn.bark(f"⚠️ 特性开关变更: {_name} → {'ON' if _enabled else 'OFF'}")
                        except Exception:
                            pass
                    body = json.dumps(_result, ensure_ascii=False, default=str)
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return

            else:
                self.send_response(404)
                self.end_headers()

    srv = ThreadingHTTPServer(("0.0.0.0", ARGS.port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[面板] http://localhost:{ARGS.port}")
    return srv


# ---------------------------------------------------------------------------
_LAST_SNAP_CHECK = 0  # 收盘复盘快照调度节流时间戳
_LAST_BROKER_SCAN = 0  # #9 经纪商成交回灌扫描节流时间戳
_LAST_BLUNDER_SCAN = 0  # #12 纪律体检节流时间戳
_LAST_EVENT_CHECK = 0  # #13 事件日历闸门节流时间戳


def _refresh_main_contracts_external():
    """换月期全市场主力合约自动核对/强制更新（#6 换月闭环，2026-08-17 落地）。
    外部采集器(系统 python3.9 + akshare) refresh_main_contracts.py --apply 扫描全市场
    5 大交易所近月主力，对换月滞后品种写 forced=True 锁近月主力；带自愈（主力出交割月清 forced）。
    本进程(3.13, 无 akshare)通过 subprocess 调用系统 python3，只读其写出的 json；
    minishare_live 在 _refresh/_apply 时保留 forced，二者互不冲突（无前视/无竞态）。
    """
    script = os.path.join(HERE, "refresh_main_contracts.py")
    if not os.path.exists(script):
        return
    py = "/usr/bin/python3"  # 系统 python3.9 + akshare；本进程 venv 无 akshare
    try:
        print(f"[换月核对] 调用 {py} {script} --apply (全市场扫描)")
        r = subprocess.run([py, script, "--apply"], cwd=HERE, timeout=300, capture_output=True, text=True)
        for line in (r.stdout or "").strip().splitlines()[-6:]:
            print(f"[换月核对] {line}")
        if r.returncode != 0:
            print(f"[换月核对] 非零退出 {r.returncode}: {(r.stderr or '').strip()[:200]}")
    except Exception as e:
        print(f"[换月核对] 异常: {repr(e)[:120]}")


# ---------------------------------------------------------------------------
# B2 换月主动提醒：运行时「系统在用主力 vs 交易所真实主力」比对
# runner(3.13) 无 akshare，经 subprocess 调系统 python3.9+akshare 的
# refresh_main_contracts.py --dump-akmap 取权威主力映射；与系统真实在用
# CONTRACT_MAP 比对，不一致即记 mismatch（主动弹窗+语音+面板上红条）。
# 设计要点：
#  - CONTRACT_MAP 每 5min 由 refresh_contract_map() 从 _authoritative_contracts()
#    刷新（该函数每次重读 main_overrides.json），即「系统当前真实在用的合约」。
#  - akshare 真主力为「交易所当前真实主力」。二者不等 = 系统在过期/错误合约上
#    跑信号（昨天 2609 滞后坑的根因）。此检查同时覆盖「文件滞后」与「runner
#    未重启读到旧内存」两类场景。
#  - best-effort：网络/采集器失败则保留旧缓存、不误报。
_AK_MAIN_CACHE = {"t": 0.0, "v": {}}  # akshare 真主力映射缓存（节流 30min）
_AK_MAIN_TS = 0.0  # refresh_ak_main 节流时间戳
_AK_MAIN_DONE = False  # 启动后首次立即核对（同 _MAIN_REFRESH_DONE 模式）
_AK_MAIN_LOCK = threading.Lock()  # 防 30s 轮询并发重复触发子进程


def _seed_from_overrides():
    """回退基线：akshare 不可用/瞬断时，用 main_overrides.json(权威覆盖层) 作为主力映射。
    键统一小写以匹配 rollover_mismatch_check 的 ak.get(sym.lower())。返回 {} 表示无兜底。"""
    try:
        mo = getattr(ml, "MAIN_OVERRIDE", None) or {}
        if mo:
            return {str(k).lower(): v for k, v in mo.items()}
    except Exception:
        pass
    return {}


def refresh_ak_main(force=False):
    """经 subprocess 取 akshare 全市场真主力（--dump-akmap，只读不写）。
    结果缓存 30min；best-effort，失败保留旧值并记录 error。带锁防并发双取。"""
    global _AK_MAIN_CACHE, _AK_MAIN_TS
    now = time.time()
    if not force and (now - _AK_MAIN_CACHE.get("t", 0.0)) < 1800 and _AK_MAIN_CACHE.get("v"):
        return _AK_MAIN_CACHE.get("v")
    script = os.path.join(HERE, "refresh_main_contracts.py")
    if not os.path.exists(script):
        return _AK_MAIN_CACHE.get("v")
    py = "/usr/bin/python3"  # 系统 python3.9 + akshare；本进程 venv 无 akshare
    with _AK_MAIN_LOCK:
        # 加锁后二次检查（等待期间可能已被别的线程刷新）
        if not force and (time.time() - _AK_MAIN_CACHE.get("t", 0.0)) < 1800 and _AK_MAIN_CACHE.get("v"):
            return _AK_MAIN_CACHE.get("v")
        try:
            r = subprocess.run([py, script, "--dump-akmap"], cwd=HERE, timeout=120, capture_output=True, text=True)
            txt = (r.stdout or "").strip()
            # 取最后一行 JSON（防偶发 stderr 混入）
            data = None
            for line in reversed(txt.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    data = json.loads(line)
                    break
            # 仅当 akshare 返回非空主力映射才覆盖缓存；空映射(瞬断)保留旧值，避免 B2 卡 pending
            if data and data.get("ok") and isinstance(data.get("main"), dict) and len(data.get("main", {})) > 0:
                _AK_MAIN_CACHE = {"t": now, "v": data.get("main", {})}
                print(f"[换月比对] akshare 真主力已刷新，覆盖 {len(_AK_MAIN_CACHE['v'])} 品种")
            else:
                # 瞬断/空映射：保留旧缓存(无旧缓存则回退 main_overrides 权威层)，不覆盖为空
                if not _AK_MAIN_CACHE.get("v"):
                    _AK_MAIN_CACHE["v"] = _seed_from_overrides()
                _AK_MAIN_CACHE["error"] = (data or {}).get("error") or (r.stderr or "")[:120]
                print(f"[换月比对] akshare 返回空/异常(保留旧缓存): {_AK_MAIN_CACHE['error']}")
        except Exception as e:
            if not _AK_MAIN_CACHE.get("v"):
                _AK_MAIN_CACHE["v"] = _seed_from_overrides()
            _AK_MAIN_CACHE["error"] = repr(e)[:120]
            print(f"[换月比对] 取真主力失败(保留旧缓存): {repr(e)[:120]}")
    return _AK_MAIN_CACHE.get("v")


def rollover_mismatch_check():
    """B2 主动提醒核心：系统真实在用合约 vs 交易所真实主力。
    仅当缓存为空（首次/重启后）才主动取一次；平时依赖后台 5.5 节流刷新，
    避免 30s 前端轮询在缓存过期时反复触发 akshare 子进程。
    返回 {mismatches:[{sym,name,current,authoritative}], checked_at, ak_available, error}。"""
    # 非阻塞：直接用后台 5.5 每 30min 刷新的 _AK_MAIN_CACHE（启动后首次立即跑）。
    # 缓存未就绪时先用 main_overrides.json 权威层兜底(立即可用)，不 force 触发 akshare 子进程，
    # 避免前端 30s 轮询阻塞超时。
    if not _AK_MAIN_CACHE.get("v"):
        seeded = _seed_from_overrides()
        if seeded:
            _AK_MAIN_CACHE["v"] = seeded
        else:
            return {
                "mismatches": [],
                "count": 0,
                "checked_at": None,
                "ak_available": False,
                "error": "akshare 缓存尚未就绪（后台刷新中）",
                "pending": True,
            }
    refresh_contract_map()  # 确保 CONTRACT_MAP 为最新在用合约
    ak = _AK_MAIN_CACHE.get("v", {}) or {}
    mismatches = []
    err = _AK_MAIN_CACHE.get("error") if isinstance(_AK_MAIN_CACHE, dict) else None
    for sym in SYMBOLS:
        cur = (CONTRACT_MAP.get(sym) or "").upper()
        akv = (ak.get(sym.lower()) or "").upper()
        # 仅当两边都是真实合约代码（含数字）且不一致才记；akshare 缺失/合成键跳过
        if not cur or not akv or not any(ch.isdigit() for ch in cur):
            continue
        if cur != akv:
            mismatches.append(
                {
                    "symbol": sym,
                    "name": SYMBOLS.get(sym, {}).get("name", sym),
                    "current": cur,
                    "authoritative": akv,
                }
            )
    return {
        "mismatches": mismatches,
        "count": len(mismatches),
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ak_available": bool(ak),
        "ak_count": len(ak),
        "error": err,
    }


def _update_aux(feed, state):
    """每轮更新：异动扫描 + 账户监控自动 papertrack + 仓位状态机。"""
    global DAY_OPEN_EQUITY, DAY_OPEN_LABEL, _LAST_HEAL_TS  # 修 UnboundLocalError：函数内赋值会令 Python 视为局部
    # —— 周期性自动对账（每 30 秒一次，确保 tp_targets 等数据实时同步）——
    global _LAST_HEAL_TS
    _now_ts = time.time()
    if _now_ts - _LAST_HEAL_TS >= _HEAL_INTERVAL:
        try:
            _ok, _healed, _ = at.heal_from_journal()
            if _healed:
                print(f"[自动对账] 修正 {len(_healed)} 项: {_healed[0][:80]}...")
            else:
                pass  # 无变化，静默
        except Exception as _e:
            print(f"[自动对账] 异常: {repr(_e)[:80]}")
        _LAST_HEAL_TS = _now_ts
    # ★ 每轮同步账户实际持仓到 runner state（确保所有书签页看到用户真实持仓）
    try:
        _acc_st = at.load_state()
        _acc_pos = _acc_st.get("positions", {})
        # 完全替换 state["positions"]（账户持仓为权威源）
        state["positions"] = {}
        for _sym, _pos in _acc_pos.items():
            if isinstance(_pos, dict) and _pos.get("lots", 0) > 0:
                state["positions"][_sym] = _pos
    except Exception:
        pass
    # 1) 异动扫描（基于 minishare 实时快照，全品种）
    try:
        snaps = {}
        pre_close_map = {}
        for s in SYMBOLS:
            snap = feed.last_snap.get(s)
            if snap:
                snaps[s] = snap
                # 提取昨收价供异动扫描使用
                pc = snap.get("pre_close")
                if pc:
                    pre_close_map[s] = float(pc)
        state["anomaly"] = asc.compute(snaps, pre_close_map=pre_close_map)
    except Exception as e:
        print(f"[异动扫描] 异常: {repr(e)[:80]}")
    # 2) 账户监控 → 自动驱动 papertrack（无接口则优雅降级为手动）
    try:
        acc = am.get_account()
        if acc:
            prices = {s: feed.price(s) for s in SYMBOLS}
            am.auto_sync(acc, prices)
    except Exception as e:
        print(f"[账户监控] 异常: {repr(e)[:80]}")
    # 3) 更新仓位状态机（权益/保证金/当日盈亏/连亏 → NORMAL/WARNING/LOCKED）
    try:
        prices = {s: feed.price(s) for s in SYMBOLS}
        snap = at.snapshot(prices)
        # 3.5) 持仓触价报警（止损/止盈）：复用 notify() 弹窗+语音
        #      全天候运行：tp_targets 计算（分级止盈目标价）不依赖交易时段；
        #      推送层面由 check_position_alerts 内部 _should_suppress 自门控。
        try:
            check_position_alerts(snap["positions"])
            save_pos_alert_dedup()  # 持仓触价告警去重状态落盘（原子写，防止连环重发）
        except Exception as e:
            print(f"[持仓触价报警] 异常: {repr(e)[:80]}")
        # —— 以下推送逻辑仅在市场开盘时执行 ——
        # 移动止损/到价提醒/跳空风险/风险热度均依赖实时行情，
        # 非交易时段价格静止，推送只会造成误导与打扰。
        if _market_open_now():
            # 3.6) 移动止损自动管理（t1→保本 / 盈利跟踪上移 / t2→全平提示）
            try:
                manage_trailing_stops()
            except Exception as e:
                print(f"[移动止损] 异常: {repr(e)[:80]}")
            # 3.7) 自选到价提醒（B1）：非持仓品种也能挂条件，触发即弹窗+语音+落报警历史
            try:
                check_watch_alerts()
            except Exception as e:
                print(f"[自选到价] 异常: {repr(e)[:80]}")
            # 3.8) 跳空风险预警（B3）：仅收盘前窗口提醒一次，避免盘中反复打扰
            try:
                check_gap_alerts()
            except Exception as e:
                print(f"[跳空风险] 异常: {repr(e)[:80]}")
            # 3.9) 风险预算硬上限（B4）：热度转为「超标」时提醒一次
            try:
                check_heat_alert(prices)
            except Exception as e:
                print(f"[风险热度] 异常: {repr(e)[:80]}")
        # 3.9.5) 组合预警规则引擎（#132）：每轮评估一次（内部 30s 缓存，成本极低），
        #        节流把「软预警」规则（>80% 热度 / 3% 日亏 / 连亏≥4 / 回撤≥5% / 集中度 等）
        #        写入报警历史；致命规则复用既有专用播报、此处不重复。
        try:
            evaluate_risk_rules(prices=prices)
        except Exception as e:
            print(f"[规则引擎] 评估异常: {repr(e)[:80]}")
        eq = snap.get("equity") or 0
        used = snap.get("total_margin") or 0
        # ---- 账户级日亏（主源=动态权益回撤，含浮亏、不依赖手动录入）----
        _td = _trading_day_label()
        if DAY_OPEN_EQUITY is None or DAY_OPEN_LABEL != _td:
            DAY_OPEN_EQUITY = eq
            DAY_OPEN_LABEL = _td
        account_daily_pnl = (eq - DAY_OPEN_EQUITY) if DAY_OPEN_EQUITY > 0 else 0.0  # 亏为负
        journal_daily = tj.today_pnl()  # 自然日已实现盈亏（交叉验证/展示）
        daily_pnl = min(account_daily_pnl, journal_daily)  # 取更亏者（更保守）
        # P2 涨跌停锁死头寸尾部应力加计（可配置，默认 OFF；见 STRESS_LIMIT_LOCKED）
        _ll_stress, _ll_details = 0.0, []
        if STRESS_LIMIT_LOCKED:
            try:
                _ll_stress, _ll_details = _limit_locked_stress(_load_open_positions(), feed)
                if _ll_stress:
                    daily_pnl -= _ll_stress  # 让日亏更亏，更早触发熔断
            except Exception as e:
                print(f"[P2 锁死应力] 计算异常: {repr(e)[:80]}")
        state["limit_locked_stress"] = {
            "amount": round(_ll_stress, 1),
            "details": _ll_details,
            "enabled": STRESS_LIMIT_LOCKED,
        }
        consec = tj.current_loss_streak()
        prev_state = state.get("risk_state", {}).get("state")
        # #119 回撤水位线：每轮用动态权益更新峰值/回撤/档位（持久化），取回峰值喂硬熔断
        try:
            _dd_state = ddg.update(eq)
            state["drawdown"] = _dd_state
            _dd_peak = _dd_state.get("peak_equity")
        except Exception as e:
            print(f"[#119 回撤水位线] 更新异常: {repr(e)[:80]}")
            _dd_peak = None
        # 组合级硬熔断（#5）：把持仓一并喂进去，触发时直接生成一键全平清单
        _res = rsm.update_risk_state(
            eq, used, daily_pnl, consec, positions=snap.get("positions") or [], peak_equity=_dd_peak
        )
        new_state = rsm.RISK_FSM.summary()  # 含 daily_loss_pct / daily_loss_stop / killswitch
        state["risk_state"] = new_state
        # ── v5 新增：10%回撤硬熔断检测 ──
        _current_dd = new_state.get("drawdown", 0)
        if _current_dd >= DRAWDOWN_FULL_STOP_PCT and new_state.get("state") != "HALTED":
            new_state["state"] = "HALTED"
            new_state["halted_at"] = time.time()
            new_state["halted_reason"] = f"10%回撤硬熔断（当前{_current_dd:.1f}%）"
            new_state["force_rest_until"] = time.time() + DRAWDOWN_FORCE_REST_SEC
            new_state["scale"] = 0.0
            log_alert(
                "硬熔断触发",
                None,
                "熔断",
                f"账户回撤{_current_dd:.1f}%≥{DRAWDOWN_FULL_STOP_PCT}%，强制全平+休息24小时",
                {"drawdown": _current_dd},
            )

        state["killswitch"] = new_state.get("killswitch", {})
        if _res.get("kill_newly"):
            _ks = new_state.get("killswitch", {})
            try:
                notify_killswitch(_ks)
            except Exception as e:
                print(f"[熔断] 播报异常: {repr(e)[:80]}")
            try:
                dr.log_event("killswitch", state="HALTED", reason=_ks.get("reason", ""))
            except Exception:
                pass
        # 状态机切换时记录事件（供「锁死时开仓」纪律判定）
        if prev_state is not None and new_state["state"] != prev_state:
            try:
                dr.log_event("risk", state=new_state["state"], reason=new_state.get("lock_reason", ""))
            except Exception:
                pass
    except Exception as e:
        print(f"[状态机] 更新异常: {repr(e)[:80]}")
    # 3.10) 经纪商成交回灌扫描（#9）：只发现不落账，发现新成交在面板挂待确认徽标
    global _LAST_BROKER_SCAN
    try:
        if time.time() - _LAST_BROKER_SCAN >= 300:
            _LAST_BROKER_SCAN = time.time()
            _bs = bi.scan(apply=False)
            state["broker_pending"] = _bs
            if _bs.get("new_fills"):
                print(f"[成交回灌] 发现 {_bs['new_fills']} 笔未导入成交（{_bs['files']} 个文件）→ 面板确认后落账")
    except Exception as e:
        print(f"[成交回灌] 扫描异常: {repr(e)[:80]}")
    # 3.11) 数据质量/陈旧监控（#14）：每轮观察行情新鲜度，检测断流/冻结/异常
    try:
        dq.observe(feed)
        state["data_quality"] = dq.check(trading=is_trading_now(datetime.now()))
    except Exception as e:
        print(f"[数据质量] 异常: {repr(e)[:80]}")
    # 3.12) 纪律自动体检（#12）：节流 600s 扫 journal 揪违规，结果进面板
    global _LAST_BLUNDER_SCAN
    try:
        if time.time() - _LAST_BLUNDER_SCAN >= 600:
            _LAST_BLUNDER_SCAN = time.time()
            state["blunder"] = bc.check()
    except Exception as e:
        print(f"[纪律体检] 异常: {repr(e)[:80]}")
    # 3.13) 事件/数据日历闸门（#13）：节流 300s 看未来事件，给减仓/禁开建议
    global _LAST_EVENT_CHECK
    try:
        if time.time() - _LAST_EVENT_CHECK >= 300:
            _LAST_EVENT_CHECK = time.time()
            state["event_gate"] = ec.gate(lookahead_hours=4)
    except Exception as e:
        print(f"[事件闸门] 异常: {repr(e)[:80]}")
    # 4) 管住手复盘卡（日/周/月纪律评分，纯本地文件读，安全降级）
    try:
        state["discipline"] = dr.get_all()
    except Exception as e:
        print(f"[管住手] 计算异常: {repr(e)[:80]}")
    # 5) 收盘复盘自动落账：每日 23:20 / 每周五 15:10 / 每月末最后交易日 15:10
    #    依次把 日→周→月 冻结成记录，供日后回看（节流 10 分钟；幂等）
    global _LAST_SNAP_CHECK
    try:
        if time.time() - _LAST_SNAP_CHECK >= 600:
            _LAST_SNAP_CHECK = time.time()
            dr.run_close_snapshots()
            dr.run_weekly_snapshots()
            dr.run_monthly_snapshots()
    except Exception as e:
        print(f"[复盘快照] 调度异常: {repr(e)[:80]}")
    # 5.1) G3 重校准自动调度：按间隔触发（默认 6h，可经 trade_config.json 配置）
    global _RECAL_LAST_TS
    try:
        _cfg = at.load_config()
        _auto = _cfg.get("auto_recalibrate", True)
        _iv = float(_cfg.get("recalibrate_interval_h", 6)) * 3600
        if _auto and (time.time() - _RECAL_LAST_TS) > _iv:
            _RECAL_LAST_TS = time.time()
            _recalibrate_tick()
    except Exception as _e:
        print(f"[重校准] 自动调度异常(忽略): {repr(_e)[:120]}")
    # 5.2) 2026-08-14：动态主力合约映射刷新（每 5 分钟），保证 CONTRACT_MAP/展示/换月预警
    #      跟随当前主力/次主力合约，避免写死月份价过时（rb 曾卡在 RB2609=2986，实际主力 3016）。
    global _CONTRACT_MAP_TS
    try:
        if time.time() - _CONTRACT_MAP_TS > 300:
            _CONTRACT_MAP_TS = time.time()
            refresh_contract_map()
    except Exception:
        pass
    # 5.3) #1 信息维度缓存刷新（每 30 分钟）：系统 python3.9+akshare 抓资讯/现货/宏观/快讯 →
    #      info_dimension.json；本进程(3.13)只读，复用 fundamentals.json 的「外部写/内部读」模式。
    global _INFO_TS
    try:
        if time.time() - _INFO_TS > 1800:
            _INFO_TS = time.time()
            idim.refresh()
            mctx.refresh()  # #6 跨资产宏观语境缓存刷新（重读 macro_context.json）
    except Exception:
        pass
    # 5.4) 换月期全市场主力合约自动核对/强制更新（外部采集器：系统 python3.9+akshare）：
    #      refresh_main_contracts.py --apply 扫描全市场 5 大交易所近月主力，换月滞后品种写 forced=True。
    #      本进程(3.13)只读 HOT_CACHE（forced 由 minishare_live 保留），复用「外部写/内部读」模式。
    #      节流 2h；重启后首次 aux 立即跑一次用于核对（见 globals _MAIN_REFRESH_*）。
    global _MAIN_REFRESH_TS, _MAIN_REFRESH_DONE
    try:
        if (time.time() - _MAIN_REFRESH_TS > 7200) or (not _MAIN_REFRESH_DONE):
            _MAIN_REFRESH_TS = time.time()
            _MAIN_REFRESH_DONE = True
            _refresh_main_contracts_external()
    except Exception:
        pass
    # 5.5) B2 换月主动提醒：刷新 akshare 交易所真主力缓存（每 30min；启动后首次立即跑）。
    #      供 rollover_mismatch_check() 与「系统在用 vs 交易所真实主力」比对（见 _AK_MAIN_*）。
    global _AK_MAIN_TS, _AK_MAIN_DONE
    try:
        if (time.time() - _AK_MAIN_TS > 1800) or (not _AK_MAIN_DONE):
            _AK_MAIN_TS = time.time()
            _AK_MAIN_DONE = True
            # 先用权威覆盖层(main_overrides.json)兜底，避免首次命中 B2 时空缓存卡 pending
            if not _AK_MAIN_CACHE.get("v"):
                _AK_MAIN_CACHE["v"] = _seed_from_overrides()
            refresh_ak_main(force=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# G3 重校准自动调度
# ---------------------------------------------------------------------------
_RECAL_LAST_TS = 0.0
_CONTRACT_MAP_TS = 0.0  # 5.2 动态合约映射刷新节流
_INFO_TS = 0.0  # 5.3 #1 信息维度缓存刷新节流（30min）
_MAIN_REFRESH_TS = 0.0  # 5.4 换月核对节流（2h）
_MAIN_REFRESH_DONE = False  # 5.4 重启后首次 aux 立即跑一次用于核对


def _recalibrate_tick():
    """G3：每轮由 _update_aux 按间隔触发。原样复用已接入 CLI 工具箱的真实入口：
    - four_dim_recalibrate.main(apply=False, stage=True) → 校准漂移检测 + 对高置信漂移/失效品种
      自动 staging 候选 T_thresh（仅 staged 到 calibration_drift.json，不覆盖线上 calibration_params.json）
    - four_dim_calibrate.recalibrate_report(...) → 重校准扫描报告（不自动落盘）
    均包裹 try/except，任何异常仅打印日志、不连累主循环。"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [重校准] 自动调度开始")
    # ① 漂移检测 + 自动 staging 候选 T（stage=True：仅对高置信 drift/broken 跑近期 WF 扫描；
    #    stage_symbols=FOCUS_SYMS 限定关注 6 品种，控开销，避免全市场扫描）
    try:
        fdr.main(apply=False, stage=True, stage_symbols=FOCUS_SYMS)
    except Exception as _e:
        print(f"[重校准] 漂移检测/ staging 异常(忽略): {repr(_e)[:160]}")
    # ② 重校准扫描报告（默认品种集，与 four_dim_calibrate.main 默认一致；不自动落盘）
    try:
        fdc.recalibrate_report(["JM", "hc", "zn", "eb", "al"], tail=250, min_trades=10)
    except Exception as _e:
        print(f"[重校准] 扫描报告异常(忽略): {repr(_e)[:160]}")
    # ③ P-H (2026-08-14): 回灌稳健池准入门槛(依赖 ① 产出的 calibration_drift.json)
    #    auto_adapt=False(默认) → 不写文件/不改动, walk_forward_gate 行为 = v12(可一键回退)。
    try:
        _rpg = _STRAT_CFG.get("robust_pool_gate", {})
        # auto_adapt 优先级：auto_optimize 主开关关闭 → False > 子配置 auto_adapt > 旧配置 > 默认 False
        _auto_adapt = _rpg.get("auto_adapt", False)
        try:
            _fm = fmg.get_manager()
            if _fm is not None:
                if _fm.is_enabled("auto_optimize"):
                    _feat_cfg = _fm.get_config("auto_optimize")
                    if _feat_cfg and "auto_adapt" in _feat_cfg:
                        _auto_adapt = bool(_feat_cfg["auto_adapt"])
                else:
                    _auto_adapt = False  # 主开关关闭 → 子配置失效
        except Exception:
            pass  # 特性开关读取失败，fallback 旧配置
        _r = strategy_layer.backfill_robust_pool_gate(auto_adapt=_auto_adapt, cfg=_rpg)
        if _r["written"]:
            print(
                f"[稳健池] 门槛回灌: OOS_expR={_r['oos_expR']:.3f} "
                f"(ensemble_recent={_r['ensemble_recent_expR']}, relaxed={_r['relaxed']})"
            )
    except Exception as _e:
        print(f"[稳健池] 门槛回灌异常(忽略): {repr(_e)[:160]}")
    # ④ #5 训练/服务一致性看门狗：报告 live 服务参数相对 OOS 校验基线的偏离/未校验/失效服务/陈旧
    try:
        _cw = cw.check_consistency(focus_symbols=FOCUS_SYMS, disabled_set=RUNTIME_DISABLED)
        _STATE_CONSISTENCY["report"] = _cw
        _STATE_CONSISTENCY["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _cs = _cw["summary"]
        print(
            f"[一致性] 偏离={_cs['divergences']} 未校验={_cs['unvalidated']} "
            f"失效服务={_cs['broken_serving']} 陈旧={_cs['stale']} "
            f"{'(✅一致)' if _cw['ok'] else '(⚠️存在不一致)'}"
        )
    except Exception as _e:
        print(f"[一致性] 看门狗异常(忽略): {repr(_e)[:160]}")


# ---------------------------------------------------------------------------
# #3 漂移闭环：已 staged 候选的「一键 apply」+ 异步 staging 调度
# ---------------------------------------------------------------------------
_RECAL_STAGE = {"running": False, "last": None, "log": []}
# #5 一致性看门狗最近一次报告缓存（供 /api/consistency 读取，避免每次重算）
_STATE_CONSISTENCY = {"report": None, "last_run": None}


def _recalibrate_apply(symbol):
    """一键 apply 已 staged 的候选 T_thresh 到 calibration_params.json。
    仅写 T_thresh 决策参数 + 标记 recalibrated_at；备份原文件；清 strategy 缓存使其即时生效(无需重启)。
    返回 (ok, msg)。symbol='__all__' 时提交全部已 staged 候选。"""
    import shutil

    drift_path = os.path.join(HERE, "calibration_drift.json")
    calib_path = os.path.join(HERE, "calibration_params.json")
    try:
        drift = json.load(open(drift_path, encoding="utf-8"))
    except Exception as e:
        return False, f"无漂移报告(先 stage/refresh): {e}"
    items = drift.get("items", [])
    if symbol == "__all__":
        targets = [it for it in items if it.get("proposed_T") is not None]
    else:
        targets = [it for it in items if it.get("symbol") == symbol and it.get("proposed_T") is not None]
    if not targets:
        return False, f"品种 {symbol} 无已 staged 的候选 T_thresh（先 stage/refresh）"
    try:
        calib = json.load(open(calib_path, encoding="utf-8"))
    except Exception as e:
        return False, f"读 calibration_params.json 失败: {e}"
    bak = calib_path + f".bak_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        shutil.copy(calib_path, bak)
    except Exception as e:
        return False, f"备份失败: {e}"
    applied = []
    warns = []
    for it in targets:
        sym = it["symbol"]
        pT = int(it["proposed_T"])
        entry = calib.setdefault(sym, {})
        entry["T_thresh"] = pT
        entry["recalibrated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _note = entry.get("note", "")
        entry["note"] = (
            (_note + f" | 漂移重校apply:T_thresh={pT}").strip(" |") if _note else f"漂移重校apply:T_thresh={pT}"
        )
        applied.append(sym)
        _pe = it.get("proposed_expR")
        if _pe is not None and float(_pe) < 0:
            warns.append(f"{sym}(T→{pT},近期期望仍为负{_pe:.3f}，建议优先靠动态门控暂停而非依赖该T)")
    try:
        with open(calib_path, "w", encoding="utf-8") as f:
            json.dump(calib, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"写入失败(已备份{bak}): {e}"
    # 即时生效：清 strategy 模块校准缓存（pipeline 走 _load_calib_params，读前会重新加载）
    try:
        fd._CALIB_CACHE.clear()
    except Exception:
        pass
    _warn = ("；⚠️ 注意：" + "；".join(warns)) if warns else ""
    return True, (f"已 apply {applied} 的候选 T_thresh 并备份{bak}；即时生效(strategy 校准缓存已清，无需重启){_warn}")


def _recalibrate_stage_async():
    """后台线程：跑漂移检测 + 对高置信漂移/失效品种自动产出候选 T_thresh（仅 staged）。"""
    global _RECAL_STAGE
    _RECAL_STAGE["running"] = True
    _RECAL_STAGE["log"] = []
    try:
        fdr.main(apply=False, stage=True, stage_symbols=FOCUS_SYMS)
        _RECAL_STAGE["last"] = "ok:" + datetime.now().strftime("%H:%M:%S")
    except Exception as e:
        _RECAL_STAGE["last"] = f"err:{e}"
        _RECAL_STAGE["log"].append(repr(e)[:240])
    _RECAL_STAGE["running"] = False


# ---------------------------------------------------------------------------
# G2/G4 关注品种盯盘板（focus_board）
# ---------------------------------------------------------------------------
def focus_board(force=False):
    """关注品种（FOCUS_SYMS）盯盘评分板：5 维打分（0–100）按权重合成。
    逐项 try/except 防缺数据；返回按 score 降序的列表。"""
    st = at.load_state()
    # 2026-08-14 修复：此前 focus_board 用持仓存储价(pos.price)/日线收盘算现价与盈亏，
    # 导致「我的品种」盯盘板不随行情实时刷新、当日盈亏恒为 +0.00。
    # 现统一用与 /api/account 同源的实时价 FEED.price 构建 prices，并传给 at.snapshot，
    # 使「现价/方向/当日盈亏」均走实时行情源。FEED 不可用时回退到无 prices（行为同旧）。
    live_prices = {}
    if FEED:
        try:
            for s in SYMBOLS:
                live_prices[s] = FEED.price(s)
        except Exception:
            live_prices = {}
    snap = at.snapshot(live_prices if live_prices else None)
    equity = (snap.get("equity") or 0) or 0
    positions = {s: p for s, p in st.get("positions", {}).items() if p.get("lots")}
    # 持仓实时价 / 浮动盈亏映射（来自与账户总览同源的 snapshot）
    _snap_pos = {p["symbol"]: p for p in snap.get("positions", [])}
    # 信号态（健壮取）
    last_sig = {}
    try:
        last_sig = st.get("last_signal", {}) or {}
    except Exception:
        last_sig = {}
    # 基本面指标（取相关品种 zscore 绝对值用于 spread 维）
    fm_cache = {}
    try:
        _fm = fm.fund_metrics(force=force)
        for _m in _fm.get("metrics", []):
            fm_cache[_m["id"]] = _m
    except Exception:
        fm_cache = {}
    # 热度状态
    heat_status = "正常"
    try:
        heat_status = (compute_heat() or {}).get("status", "正常")
    except Exception:
        heat_status = "正常"

    out = []
    for sym in FOCUS_SYMS:
        try:
            # 最新价：优先实时价(FEED.price，与 /api/account 同源)，其次持仓存储价，最后日线收盘
            pos = positions.get(sym)
            last_price = live_prices.get(sym)
            if last_price is None:
                if pos:
                    last_price = pos.get("price") or pos.get("avg")
                if last_price is None:
                    try:
                        last_price = float(load_daily_refreshed(sym)["close"].iloc[-1])
                    except Exception:
                        last_price = None
            dirn = pos.get("direction", "—") if pos else "—"
            # 信号态
            sig_state = "—"
            try:
                sig_state = last_sig.get(sym, "—") or "—"
            except Exception:
                sig_state = "—"
            # 当日盈亏：与账户总览同源——持仓实时浮动盈亏 + 当日已实现盈亏。
            # 2026-08-14 修复：此前仅用 tj.today_pnl（仅含当日已平仓，未平仓恒为 +0.00），
            # 现改用实时浮动盈亏(由 live FEED 价驱动) + 当日已实现，使盯盘板随行情实时变化。
            pnl_day = None
            try:
                _realized_today = round(tj.today_pnl(sym), 2)
                if pos:
                    _fp = _snap_pos.get(sym, {}).get("float_pnl")
                    pnl_day = round((_fp or 0) + _realized_today, 2) if _fp is not None else _realized_today
                else:
                    pnl_day = _realized_today
            except Exception:
                pnl_day = None
            # 持仓 VaR%（占权益）
            var_pct = None
            if pos:
                try:
                    _pv = portfolio_var(force=force)
                    if _pv.get("ok") and equity > 0:
                        _c = _pv.get("contrib_var95", {}).get(sym)
                        if _c is not None:
                            var_pct = round(100 * _c / equity, 2)
                except Exception:
                    var_pct = None
            # 波动率%（ATR%/close）
            atr_pct = None
            vol_score = 0
            try:
                df = load_daily_refreshed(sym)
                if df is not None and len(df) >= 15:
                    _close = float(df["close"].iloc[-1])
                    _atr = float(df["close"].rolling(14).std().iloc[-1])
                    if _close > 0:
                        atr_pct = round(100 * _atr / _close, 2)
                        vol_score = max(0, min(100, atr_pct * 20))
            except Exception:
                atr_pct = None
                vol_score = 0
            # spread 维：相关基本面指标 zscore 的最大绝对值（偏离均值越远→关注越高）
            spread_score = 0
            try:
                _zs = []
                for _mid in ("fg_sa_profit", "jm_j_profit", "fg_sa_ratio", "jm_j_ratio", "rb_hc_spread"):
                    _mm = fm_cache.get(_mid)
                    if _mm and _mm.get("value_ok") and _mm.get("zscore") is not None:
                        _zs.append(abs(_mm["zscore"]))
                if _zs:
                    spread_score = max(0, min(100, max(_zs) * 20))
            except Exception:
                spread_score = 0
            # 5 维打分
            signal_score = 100 if (pos and dirn in ("多", "空")) else 0
            position_score = 100 if pos else 0
            heat_map = {"正常": 0, "警戒": 50, "超标": 100}
            heat_score = heat_map.get(heat_status, 0)
            breakdown = {
                "signal": signal_score,
                "vol": vol_score,
                "position": position_score,
                "spread": spread_score,
                "heat": heat_score,
            }
            score = round(
                0.30 * signal_score + 0.20 * vol_score + 0.20 * position_score + 0.15 * spread_score + 0.15 * heat_score
            )
            # 2026-08-20 修复：优先使用 _authoritative_contracts() 获取主力合约代码，
            # 而非 FEED.contract_of()（后者对有持仓品种返回开仓合约，非主力合约）。
            contract = None
            try:
                _auth_map = ml._authoritative_contracts()
                _auth_code = _auth_map.get(sym)
                if _auth_code:
                    contract = ml.normalize_contract_code(_auth_code)
                else:
                    _feed0 = FEED if FEED is not None else ml.feed()
                    _code = _feed0.contract_of(sym) if _feed0 is not None else None
                    if _code:
                        contract = ml.normalize_contract_code(_code)
            except Exception:
                contract = None
            out.append(
                {
                    "symbol": sym,
                    "name": SYMBOLS.get(sym, {}).get("name", sym),
                    "contract": contract,
                    "score": score,
                    "breakdown": breakdown,
                    "last_price": last_price,
                    "dir": dirn,
                    "pnl_day": pnl_day,
                    "var_pct": var_pct,
                    "atr_pct": atr_pct,
                    "signal_state": sig_state,
                    "note": "",
                }
            )
        except Exception as _e:
            # 单项缺数据：仍返回占位，避免整板失败
            out.append(
                {
                    "symbol": sym,
                    "name": SYMBOLS.get(sym, {}).get("name", sym),
                    "score": 0,
                    "breakdown": {"signal": 0, "vol": 0, "position": 0, "spread": 0, "heat": 0},
                    "last_price": None,
                    "dir": "—",
                    "pnl_day": None,
                    "var_pct": None,
                    "atr_pct": None,
                    "signal_state": "—",
                    "note": f"数据缺失: {repr(_e)[:60]}",
                }
            )
    out.sort(key=lambda x: x["score"], reverse=True)
    return {
        "ok": True,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": out,
    }


# ---------------------------------------------------------------------------
# F3 自动化日报 / 周报：基于当日/当周交易与风险数据合成 Markdown 复盘报告。
#   调用既有 tj / dr / 风控 / 聚焦 数据函数，组合为一份可直接发布的复盘文案。
# ---------------------------------------------------------------------------
def generate_report(kind="daily"):
    """合成 Markdown 复盘报告。kind: daily|weekly。返回 {ok, kind, title, markdown}。"""
    now = datetime.now()
    is_weekly = kind == "weekly"
    s = tj.summary()
    pm = tj.performance_metrics()
    ss = tj.session_split()
    rr = evaluate_risk_rules(force=True)
    fb = focus_board(force=False)
    if is_weekly:
        disc = dr.list_weekly_records()
        period_label = "本周"
        title = f"期货交易周报 · {now.strftime('%Y-%m-%d')} 当周"
    else:
        disc = dr.list_records()
        period_label = "今日"
        title = f"期货交易日报 · {now.strftime('%Y-%m-%d')}"

    L = []
    L.append(f"# {title}")
    L.append("")
    L.append(f"> 自动生成 · 非投资建议 · 数据截至 {now.strftime('%Y-%m-%d %H:%M')}")
    L.append("")

    # 一、账户与权益
    L.append("## 一、账户与权益")
    L.append(f"- 总盈亏（估算）：**{pm.get('total_pnl', 0):.0f}**")
    L.append(
        f"- 胜率：{s.get('win_rate', 0):.1f}% ｜ 笔数：{s.get('total', 0)} ｜ 盈利 {s.get('win', 0)} / 亏损 {s.get('loss', 0)}"
    )
    L.append(
        f"- 最大回撤：{pm.get('max_dd', 0):.1f}% ｜ 收益风险比(Sharpe)：{pm.get('sharpe', 0):.2f} ｜ Calmar：{pm.get('calmar', 0):.2f}"
    )
    L.append(
        f"- 平均 R：{pm.get('avg_R', 0):.2f} ｜ 利润因子：{pm.get('profit_factor', 0):.2f} ｜ 累计手续费：{pm.get('total_fee', 0):.0f}"
    )
    L.append("")

    # 二、时段分解
    L.append("## 二、日盘 / 夜盘分解")
    day = ss.get("day", {})
    night = ss.get("night", {})
    L.append(
        f"- 日盘：净盈亏 {day.get('pnl', 0):.0f} ｜ 胜率 {day.get('win_rate', 0):.0f}% ｜ {day.get('trades', 0)} 笔"
    )
    L.append(
        f"- 夜盘：净盈亏 {night.get('pnl', 0):.0f} ｜ 胜率 {night.get('win_rate', 0):.0f}% ｜ {night.get('trades', 0)} 笔"
    )
    L.append("")

    # 三、风控状态
    L.append("## 三、风控状态")
    L.append(f"- 综合评级：**{rr.get('grade', '正常')}**（触发规则 {rr.get('n_triggered', 0)}/{rr.get('n_rules', 0)}）")
    ctx = rr.get("context", {})
    L.append(
        f"- 权益 {ctx.get('equity', 0):.0f} ｜ 持仓 {ctx.get('n_positions', 0)} ｜ 资金使用率 {ctx.get('usage_rate', 0) if ctx.get('usage_rate') is not None else '—'}% ｜ 回撤 {ctx.get('dd_pct', 0) if ctx.get('dd_pct') is not None else '—'}%"
    )
    if rr.get("triggered_rules"):
        L.append("- 触发项：")
        for t in rr["triggered_rules"][:6]:
            L.append(f"  - [{t.get('severity', '')}] {t.get('name', '')}：{t.get('detail', '')}")
    else:
        L.append("- 无触发规则，风控正常。")
    L.append("")

    # 四、交易纪律
    L.append("## 四、交易纪律")
    if disc:
        last = disc[0]
        L.append(f"- 最新{period_label}评分：**{last.get('score', '—')}**（{last.get('grade', '')}）")
        L.append(
            f"- 开仓 {last.get('trades_opened', 0)} ｜ 跟信号 {last.get('signal_trades', 0)} ｜ 冲动 {last.get('impulse_trades', last.get('manual_trades', 0))} ｜ 锁违 {last.get('lock_violations', 0)} ｜ 区间盈亏 {last.get('period_pnl', 0):.0f}"
        )
    else:
        L.append(f"- {period_label}暂无纪律评分记录。")
    L.append("")

    # 五、关注品种
    L.append("## 五、关注品种盯盘")
    for f in (fb.get("symbols", []) or [])[:8]:
        L.append(
            f"- {f.get('name', f.get('symbol', ''))}（{f.get('symbol', '')}）：盯盘评分 {f.get('score', 0)} ｜ 当日盈亏 {f.get('pnl_day') if f.get('pnl_day') is not None else '—'} ｜ 信号 {f.get('signal_state', '—')}"
        )
    L.append("")

    L.append("---")
    L.append("*本报由四维策略模型自动生成，仅供个人复盘参考，不构成投资建议。*")
    return {
        "ok": True,
        "kind": kind,
        "title": title,
        "markdown": "\n".join(L),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# F8 模拟盘 / 回测沙盒：在实盘面板上叠加一个虚拟账户，可手动/按信号模拟开平仓，
#   独立核算盈亏，不碰真实资金。虚拟资金初始 1,000,000，逐合约单仓（与真实账户同口径）。
# ---------------------------------------------------------------------------
PAPER_PATH = os.path.join(HERE, "paper_account.json")
PAPER_INIT_CASH = 1_000_000.0


def _paper_load():
    try:
        with open(PAPER_PATH) as f:
            d = json.load(f)
        d.setdefault("cash", PAPER_INIT_CASH)
        d.setdefault("realized", 0.0)
        d.setdefault("positions", {})
        d.setdefault("trades", [])
        return d
    except Exception:
        return {"cash": PAPER_INIT_CASH, "realized": 0.0, "positions": {}, "trades": [], "updated": ""}


def _paper_save(d):
    try:
        with open(PAPER_PATH, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _paper_mult(sym):
    try:
        return float((_TCFG.get("contract_specs", {}).get(sym, {}) or {}).get("multiplier", 10) or 10)
    except Exception:
        return 10.0


def _paper_mtm(d):
    """按当前实时价估算各持仓浮动盈亏（best-effort）。"""
    total = 0.0
    for sym, p in (d.get("positions") or {}).items():
        try:
            px = FEED.price(sym) if FEED else None
        except Exception:
            px = None
        if px is None:
            px = p.get("avg")
        if px is None:
            continue
        mult = _paper_mult(sym)
        sign = 1 if p.get("direction") == "多" else -1
        total += (px - p["avg"]) * mult * p["lots"] * sign
    return round(total, 2)


def paper_state():
    d = _paper_load()
    mtm = _paper_mtm(d)
    eq = round(d["cash"] + d["realized"] + mtm, 2)
    return {
        "ok": True,
        "cash": round(d["cash"], 2),
        "realized": round(d["realized"], 2),
        "mtm": mtm,
        "equity": eq,
        "positions": d.get("positions", {}),
        "trades": d.get("trades", []),
        "updated": d.get("updated", ""),
    }


def paper_open(symbol, direction, lots, price, signal_id="", strategy="模拟"):
    d = _paper_load()
    if symbol not in SYMBOLS:
        return False, f"未知品种 {symbol}"
    lots = int(lots)
    price = float(price)
    if lots <= 0 or price <= 0:
        return False, "手数/价格无效"
    pos = d["positions"].get(symbol)
    if pos and pos["direction"] == direction:  # 加仓：更新均价
        tot = pos["lots"] + lots
        pos["avg"] = round((pos["avg"] * pos["lots"] + price * lots) / tot, 2)
        pos["lots"] = tot
    elif pos and pos["direction"] != direction:  # 反手：先平旧仓再开新仓
        _paper_realize(d, symbol, price)
        d["positions"][symbol] = {
            "direction": direction,
            "lots": lots,
            "avg": price,
            "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        d["positions"][symbol] = {
            "direction": direction,
            "lots": lots,
            "avg": price,
            "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    d["trades"].append(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "action": "开",
            "direction": direction,
            "lots": lots,
            "price": price,
            "signal_id": signal_id,
            "strategy": strategy,
        }
    )
    d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _paper_save(d)
    return True, "模拟开仓成功", paper_state()


def paper_close(symbol, price, lots=None, reason="手动"):
    d = _paper_load()
    pos = d["positions"].get(symbol)
    if not pos:
        return False, f"{symbol} 无模拟持仓"
    price = float(price)
    close_lots = int(lots) if lots else pos["lots"]
    close_lots = min(close_lots, pos["lots"])
    _paper_realize(d, symbol, price, close_lots)
    if close_lots >= pos["lots"]:
        del d["positions"][symbol]
    else:
        pos["lots"] -= close_lots
    d["trades"].append(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "action": "平",
            "direction": pos["direction"],
            "lots": close_lots,
            "price": price,
            "reason": reason,
        }
    )
    d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _paper_save(d)
    return True, "模拟平仓成功", paper_state()


def _paper_realize(d, symbol, price, lots=None):
    pos = d["positions"].get(symbol)
    if not pos:
        return
    mult = _paper_mult(symbol)
    close_lots = int(lots) if lots else pos["lots"]
    sign = 1 if pos["direction"] == "多" else -1
    pnl = (price - pos["avg"]) * mult * close_lots * sign
    d["realized"] = round(d["realized"] + pnl, 2)
    d["cash"] = round(d["cash"] + pnl, 2)


def _holdings_kline(sym, bars=30):
    """获取持仓品种K线 + SR位 + 止损止盈线。
    返回 {ok, symbol, name, klines:[{date,open,high,low,close}],
           supports:[...], resistances:[...], entry_price, direction,
           stop_loss, take_profit_1, take_profit_2, current_price, atr}"""
    import sr_analyzer as sra
    from four_dim_strategy import exit_plan, risk_gate, strat_atr

    df = load_daily_refreshed(sym)
    if df is None or len(df) < 20:
        return {"ok": False, "error": "数据不足"}

    # 取最近 N 根K线
    tail = df.tail(bars)
    klines = []
    for idx, row in tail.iterrows():
        klines.append(
            {
                "date": idx.strftime("%m-%d"),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            }
        )

    # SR 位
    sr = sra.analyze(df.tail(80))
    levels = sr.get("levels", [])
    supports = [lv for lv in levels if lv["role"] == "support"][-5:]
    resistances = [lv for lv in levels if lv["role"] == "resistance"][-5:]

    # 当前价
    cur_price = float(df["close"].iloc[-1])
    if FEED:
        px = FEED.price(sym)
        if px and px > 0:
            cur_price = px

    # ATR
    atr_val = float(strat_atr(df).iloc[-1])

    # 查持仓信息（从 paper trading）
    entry_price = None
    direction = None
    stop_loss = None
    take_profit_1 = None
    take_profit_2 = None
    try:
        ps = paper_state()
        pos = (ps.get("positions") or {}).get(sym)
        if pos:
            entry_price = pos.get("avg")
            direction = pos.get("direction")
            dir_sign = 1 if direction == "多" else -1
            rg = risk_gate(sym, entry_price, atr_val, DEFAULT_CONFIG)
            if rg.get("passed"):
                ep = exit_plan(sym, entry_price, dir_sign, atr_val, "neutral", DEFAULT_CONFIG)
                stop_loss = round(ep.get("stop", 0), 2)
                take_profit_1 = round(ep.get("t1", 0), 2)
                take_profit_2 = round(ep.get("t2", 0), 2)
    except Exception:
        pass

    sp = DEFAULT_CONFIG["contract_specs"].get(sym, {})
    name = SYMBOLS.get(sym, {}).get("name", sym)

    return {
        "ok": True,
        "symbol": sym,
        "name": name,
        "klines": klines,
        "supports": [round(s["price"], 2) for s in supports],
        "resistances": [round(r["price"], 2) for r in resistances],
        "entry_price": entry_price,
        "direction": direction,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "current_price": round(cur_price, 2),
        "atr": round(atr_val, 2),
        "multiplier": sp.get("multiplier", 1),
    }


def _signal_feed(limit=30, since=None):
    """信号瀑布流：返回最近 N 条精简后的信号。
    since: 只返回时间严格大于此值的信号（用于增量拉取）"""
    try:
        if not os.path.exists(SIGNAL_LOG):
            return {"ok": True, "signals": [], "total": 0}
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            all_sigs = json.load(f)
    except Exception:
        return {"ok": False, "error": "读取信号日志失败"}

    # 按时间过滤（增量）
    if since:
        all_sigs = [s for s in all_sigs if s.get("time", "") > since]

    # 精简字段
    feed = []
    for s in all_sigs[:limit]:
        pipe = s.get("pipeline", {})
        risk = s.get("risk_gate", {})
        feed.append(
            {
                "time": s.get("time", ""),
                "symbol": s.get("symbol", ""),
                "name": s.get("name", s.get("symbol", "")),
                "direction": s.get("direction", ""),
                "price": s.get("price", s.get("entry_ref")),
                "stop": s.get("stop"),
                "t1": s.get("t1"),
                "t2": s.get("t2"),
                "rr_ratio": s.get("rr_ratio"),
                "regime": s.get("regime_hmm"),
                "T_5m": pipe.get("T_5m"),
                "F": pipe.get("F"),
                "C": pipe.get("C"),
                "sentiment_filter": pipe.get("sentiment_filter_note"),
                "sr_note": pipe.get("sr_note"),
                "signal_type": s.get("signal_type", ""),
                "reason": s.get("reason", ""),
            }
        )
    return {"ok": True, "signals": feed, "total": len(all_sigs)}


def refresh_gates():
    """刷新动态表现门控：增量重算 papertrack 真实回测 → 各品种近期表现门槛。
    某品种近期真实回测转负则暂停其信号，恢复正数后自动解除（自适应、不永久禁用）。"""
    global GATE_CACHE
    try:
        pt.main()  # 增量重算真实回测（数据缓存，开销小）
    except Exception as e:
        print(f"[门控] papertrack 重算异常(沿用旧门控): {repr(e)[:120]}")
    try:
        GATE_CACHE["gates"] = pt.compute_symbol_gates()
    except Exception as e:
        print(f"[门控] 计算异常: {repr(e)[:120]}")
    GATE_CACHE["ts"] = time.time()
    gated = [s for s, g in GATE_CACHE["gates"].items() if g.get("gated")]
    if gated:
        print(f"[门控] 当前暂停发信号的品种: {gated}")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次评估后退出（测试）")
    ap.add_argument("--no-voice", action="store_true", help="关闭语音")
    ap.add_argument("--port", type=int, default=PORT)
    ARGS = ap.parse_args()

    global FEED
    FEED = ml.feed()
    feed = FEED  # 兼容下方循环内 feed.* 引用（与 FEED 同一实例）
    feed_ok = FEED.available()
    if not feed_ok:
        print("[!] minishare 不可用，降级运行（面板照常，仅无实时信号）")

    state = {
        "updated": "",
        "session": "",
        "online": True,
        "order": list(SYMBOLS.keys()),
        "symbols": {},
        "signals": [],
        "chat": load_chat_feed(),
    }
    global STATE_REF
    STATE_REF = state

    # 面板先行启动：dashboard 在后台线程 serve，预热与否都不影响面板打开
    start_dashboard(state)
    print("[面板] 已先行启动（预热在后台进行，面板立即可用）")

    # #119 回撤水位线：加载水位线配置，并把持久化峰值权益喂给状态机（重启不洗白）
    try:
        ddg.init_from_config()
        _dd0 = ddg.current()
        if _dd0.get("peak_equity"):
            rsm.RISK_FSM.peak_equity = float(_dd0["peak_equity"])
        print(
            f"[#119 回撤水位线] 已加载，档位={[(round(t * 100, 1), s) for t, s in ddg.waterlines()]}"
            f" · 持久化峰值={_dd0.get('peak_equity')}"
        )
    except Exception as e:
        print(f"[#119 回撤水位线] 初始化异常(忽略): {repr(e)[:80]}")
    # #121 已接入 CLI 工具箱：载入上次的运行结果
    try:
        _load_tools_state()
    except Exception:
        pass
    # #10 GA 权重缓存：启动时加载（combine_bias 会自动读取）
    try:
        _ga_data = fd._load_ga_weights()
        if _ga_data:
            print(f"[#10 GA权重] 已加载 {len(_ga_data)} 个品种的优化权重")
    except Exception:
        pass

    # —— #3 盘口级订单流连接器（默认关闭，TICK_FEED_ENABLED=1 才启；无源时静默降级）——
    if TICK_FEED_ENABLED:
        threading.Thread(target=TICK_CONNECTOR.loop, daemon=True).start()
        print(f"[#3 订单流] 连接器已启动（源={'WS ' + TICK_WS_URL if TICK_WS_URL else TICK_STREAM_FILE}）")
    else:
        print("[#3 订单流] 未启用（TICK_FEED_ENABLED=0，C_flow 维持 minishare 驱动）")
    last_fire = {}
    last_F_day = None
    corr_histories = {}  # sym -> [(T, C), ...] 滚动窗口，喂 pipeline corr_hist 激活相关性闸门

    if ARGS.once:
        today = datetime.now().strftime("%Y%m%d")
        ensure_F(today)
        _poll_feed(feed)
        evaluate(feed, today, last_fire, state, corr_histories)
        _update_aux(feed, state)
        state["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["session"] = session_label()
        json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, default=str, indent=2)
        print("[once] 完成，状态已写", STATE_FILE)
        return

    refresh_gates()  # 启动即刷新动态门控（预热评估前，让卡片即刻反映门控状态）
    # 启动预热：先评估一次填 symbols，避免刚启动恰逢休市时信号卡全空
    if feed_ok:
        try:
            _today = datetime.now().strftime("%Y%m%d")
            ensure_F(_today)
            _poll_feed(feed)
            evaluate(feed, _today, last_fire, state, corr_histories)
            _update_aux(feed, state)
            state["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["session"] = session_label()
            print("[预热] 启动评估完成")
        except Exception as e:
            print(f"[预热] 评估异常(忽略): {repr(e)[:160]}")
    else:
        print("[预热] 跳过（feed 不可用）")
    print(f"[runner] 启动 · 轮询 {POLL_SEC}s · 去抖(同号连续2轮)+签名去重 · 时段内评估")

    # —— #16 卡死自检线程：主循环停摆超过阈值则自杀，交由看门狗重启 ——
    def _stall_watchdog():
        global LAST_CYCLE_TS
        while True:
            time.sleep(30)
            if LAST_CYCLE_TS and time.time() - LAST_CYCLE_TS > 180:
                print(f"[看门狗] 主循环已停摆 {int(time.time() - LAST_CYCLE_TS)}s，自杀退出交看门狗重启")
                os._exit(3)

    threading.Thread(target=_stall_watchdog, daemon=True).start()
    # ★ 启动 minishare rt_fut_k 主数据源轮询线程（5秒/次，不限次快照）
    # rt_fut_k 返回全市场 948 个期货品种实时行情，系统自动筛选持仓品种
    global _TS_SYMBOLS, _AK_SYMBOLS
    _TS_SYMBOLS = list(tl._TUSHARE_CONTRACTS.keys())  # 保留用于品种集合
    _AK_SYMBOLS = _TS_SYMBOLS  # 同步兼容（akshare 新浪实时监控品种）
    print(f"[行情] 监控品种数: _TS={len(_TS_SYMBOLS)}, _AK={len(_AK_SYMBOLS)}")
    # ★ 启动 akshare 新浪实时行情轮询线程（盘中实时数据源）
    # minishare rt_fut_k 仅在收盘更新，盘中需依赖新浪财经实时数据
    threading.Thread(target=_ak_poller, daemon=True).start()
    threading.Thread(target=_ts_poller, daemon=True).start()
    # ★ 2026-08-28: 启动 account_tracker 的 akshare 批量轮询（持仓品种实时价缓存）
    try:
        at.start_ak_poller(interval=5)
    except Exception as _e:
        print(f"[account_tracker] ak_poller 启动失败: {_e}")
    # ★ 启动自动模拟交易引擎
    try:
        pti.init(
            price_feed=feed if feed_ok else None,
            contract_specs=_TCFG.get("contract_specs", {}),
        )
    except Exception as _e:
        print(f"[PaperTrading] 初始化失败: {_e}")
    last_gate = time.time()
    global LAST_CYCLE_TS  # #16 心跳：main 内给模块全局赋值必须声明 global，否则只更新局部、看门狗读到永远 0.0
    global last_recover  # 修复 UnboundLocalError：函数内赋值会让 Python 视为局部
    while True:
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        # 跨日刷新 F
        if last_F_day != now.strftime("%Y-%m-%d"):
            ensure_F(today)
            last_F_day = now.strftime("%Y-%m-%d")
        if feed_ok and is_trading_now(now):
            _poll_feed(feed)
            evaluate(feed, today, last_fire, state, corr_histories)
            # ── v6.0 新增：市场状态引擎更新（阶段一） ──
            if MARKET_STATE_ENGINE_ENABLED:
                try:
                    _update_market_states(feed, state)
                except Exception:
                    pass  # 市场状态更新失败不影响主流程
        else:
            # 休市也轻量 poll 维持价格 / C_flow 快照，让账户表盘后也能显示浮动盈亏（不评估）
            try:
                _poll_feed(feed)
            except Exception:
                pass
        # —— 从 da龘 合并进来的三件事（每轮）——
        _update_aux(feed, state)
        # ★ 自动模拟交易：检查新信号 + 检查持仓 TP/SL
        try:
            pti.tick(state)
        except Exception as _pte:
            print(f"[PaperTrading] tick 异常: {repr(_pte)[:80]}")
        # 日内权益采样（每轮都跑，内部按分钟去重）
        try:
            prices = {}
            if feed_ok:
                for sym in SYMBOLS:
                    px = feed.price(sym)
                    if px:
                        prices[sym] = px
            tj.sample_equity(prices)
        except Exception:
            pass
        state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["session"] = session_label(now)
        json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, default=str)
        LAST_CYCLE_TS = time.time()  # #16 心跳：主循环活着就打点
        # 动态门控每 30 分钟刷新一次（重算真实回测 → 近期转负的品种自动暂停）
        if now.timestamp() - last_gate > 1800:
            refresh_gates()
            last_gate = now.timestamp()
        # 自适应恢复检查：被禁品种近期 walk-forward 转正→自动解禁（JM/hc 等保留定义不删除）
        if now.timestamp() - last_recover > RECOVERY_SEC:
            try:
                recovery_tick()
            except Exception as e:
                print(f"[恢复检查] 异常(忽略): {repr(e)[:120]}")
            last_recover = now.timestamp()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
# CI pipeline verification comment
