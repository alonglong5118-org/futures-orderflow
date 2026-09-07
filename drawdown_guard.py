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

多账户支持（2026-09-07）：
  · default 账户 → 沿用原 drawdown_state.json（向后兼容）
  · 其他账户  → 状态文件为 drawdown_state_{account_id}.json
  · 工厂函数 get_guard(account_id) 按需创建并缓存实例

用法（runner 调用）：
  import drawdown_guard as ddg
  ddg.init_from_config()                 # 启动加载水位线
  st = ddg.update(dynamic_equity)        # 每轮喂动态权益（默认账户）
  st = ddg.update(dynamic_equity, account_id="acc2")  # 指定账户
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
_CONFIG_LOCK = threading.RLock()  # 保护全局水位线配置


# ==========================================================================
# DrawdownGuard 类（单账户实例）
# ==========================================================================

class DrawdownGuard:
    """单账户回撤水位线管理器。

    每个账户独立的状态文件、峰值、档位，互不干扰。"""

    def __init__(self, state_path):
        self.state_path = state_path
        self._lock = threading.RLock()

    def _load(self):
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, encoding="utf-8") as f:
                    d = json.load(f) or {}
                return d
        except Exception:
            pass
        return {
            "peak_equity": None,
            "dd_pct": 0.0,
            "tier": 0,
            "scale": 1.0,
            "updated": "",
            "thresholds": waterlines(),
            "intraday_peak": None,
            "intraday_peak_date": None,
            "intraday_dd_pct": 0.0,
            "opening_equity": None,
        }

    def _save(self, d):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def update(self, equity):
        """每轮喂入动态权益，更新峰值 / 回撤 / 档位，返回当前状态 dict。"""
        with self._lock:
            st = self._load()
            peak = st.get("peak_equity")
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
            # 新的一天 → 初始化日内峰值
            if intraday_date != today:
                intraday_peak = eq
                st["intraday_peak_date"] = today
                st["opening_equity"] = eq
                st["intraday_peak"] = eq
            elif intraday_peak is None or eq > intraday_peak:
                intraday_peak = eq
                st["intraday_peak"] = eq
            # 全周期峰值
            if peak is None or eq > peak:
                peak = eq
            # 全周期回撤
            dd = (peak - eq) / peak if peak > 0 else 0.0
            # 日内回撤
            intraday_dd = (intraday_peak - eq) / intraday_peak if intraday_peak > 0 else 0.0
            # 档位查找取 max(全周期dd, 日内dd)
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
            st["intraday_dd_pct"] = round(intraday_dd * 100, 2)
            st["intraday_peak"] = intraday_peak
            st["opening_equity"] = st.get("opening_equity", eq)
            st["tier"] = tier
            st["scale"] = scale
            st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            st["thresholds"] = wls
            self._save(st)
            return st

    def scale_factor(self):
        try:
            return float(self._load().get("scale", 1.0))
        except Exception:
            return 1.0

    def current(self):
        st = self._load()
        st["thresholds"] = waterlines()
        return st

    def reset_peak(self, equity=None):
        """重置峰值权益，使回撤归零、降险系数回 1.0。"""
        with self._lock:
            st = self._load()
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
            self._save(st)
            return st


# ==========================================================================
# 多账户工厂 + 模块级函数（向后兼容）
# ==========================================================================

_REGISTRY_LOCK = threading.RLock()
_GUARD_REGISTRY = {}


def _state_file_for(account_id):
    """按账户返回回撤状态文件路径，default 沿用旧文件名。"""
    if account_id == "default":
        return STATE_FILE
    return os.path.join(HERE, f"drawdown_state_{account_id}.json")


def get_guard(account_id=None):
    """获取指定账户的 DrawdownGuard 实例，不传则用 default。

    default 账户的状态文件仍是 drawdown_state.json（确保向后兼容）；
    其他账户按需创建并缓存，状态文件按账户隔离。"""
    if account_id is None or account_id == "default":
        account_id = "default"
    with _REGISTRY_LOCK:
        g = _GUARD_REGISTRY.get(account_id)
        if g is None:
            g = DrawdownGuard(_state_file_for(account_id))
            _GUARD_REGISTRY[account_id] = g
        return g


def list_dd_accounts():
    """列出所有已知回撤账户（default + 有状态文件的账户）。"""
    accounts = {"default"}
    try:
        for fn in os.listdir(HERE):
            if fn.startswith("drawdown_state_") and fn.endswith(".json"):
                aid = fn[len("drawdown_state_"):-len(".json")]
                if aid and not aid.endswith(".tmp") and not aid.endswith(".bak"):
                    accounts.add(aid)
    except Exception:
        pass
    return sorted(accounts)


# ==========================================================================
# 模块级函数（向后兼容：默认操作 default 账户，可传 account_id 指定账户）
# ==========================================================================

def init_from_config():
    """从 trade_config.json 读取 risk_gate.drawdown_waterlines（可选覆盖）。
    格式：[[阈值百分比, 系数], ...] 或 [[阈值小数, 系数], ...]（>1 自动视为百分比）。

    注意：水位线是全局配置，不按账户隔离（所有账户用同一套阈值）。"""
    with _CONFIG_LOCK:
        try:
            from account_tracker import load_config

            cfg = load_config()
            wl = cfg.get("risk_gate", {}).get("drawdown_waterlines")
            if wl:
                parsed = []
                for row in wl:
                    th, sc = row[0], row[1]
                    th = float(th)
                    if th > 1.0:  # 形如 5 / 10 → 视为百分比
                        th = th / 100.0
                    parsed.append((th, float(sc)))
                parsed.sort(key=lambda x: x[0])
                if parsed:
                    _CONFIG["waterlines"] = parsed
        except Exception:
            pass
        return _CONFIG["waterlines"]


def waterlines():
    return list(_CONFIG["waterlines"])


def update(equity, account_id=None):
    """每轮喂入动态权益，更新峰值 / 回撤 / 档位。

    参数:
      - equity: 当前动态权益
      - account_id: 账户 ID（None 则用 default）
    """
    return get_guard(account_id).update(equity)


def scale_factor(account_id=None):
    """获取当前缩放系数。"""
    return get_guard(account_id).scale_factor()


def current(account_id=None):
    """获取当前状态 dict（供面板展示）。"""
    return get_guard(account_id).current()


def reset_peak(equity=None, account_id=None):
    """重置峰值权益，使回撤归零、降险系数回 1.0。"""
    return get_guard(account_id).reset_peak(equity)


# ==========================================================================
# 自测
# ==========================================================================

if __name__ == "__main__":
    import tempfile

    # 用临时状态文件，不污染真实文件
    tmp_path = os.path.join(tempfile.gettempdir(), "ddg_selftest.json")
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    guard = DrawdownGuard(tmp_path)
    init_from_config()
    print("水位线:", [(round(t * 100, 1), s) for t, s in waterlines()])
    print("100万:", guard.update(1_000_000)["scale"], "(应=1.0)")
    print("97万 :", guard.update(970_000)["scale"], "(应=1.0, 未触线)")
    print("94万 :", guard.update(940_000)["scale"], "(应=0.7, 触5%线)")
    print("89万 :", guard.update(890_000)["scale"], "(应=0.5, 触10%线)")
    print("84万 :", guard.update(840_000)["scale"], "(应=0.0, 触15%线)")
    print("105万:", guard.update(1_050_000)["scale"], "(应=1.0, 新高归零)")
    print("reset:", guard.reset_peak(990_000)["scale"], "(应=1.0)")

    # 测试多账户隔离
    print()
    print("=== 多账户隔离测试 ===")
    tmp_a = os.path.join(tempfile.gettempdir(), "ddg_accA.json")
    tmp_b = os.path.join(tempfile.gettempdir(), "ddg_accB.json")
    for p in [tmp_a, tmp_b]:
        if os.path.exists(p):
            os.remove(p)

    gA = DrawdownGuard(tmp_a)
    gB = DrawdownGuard(tmp_b)
    gA.update(1_000_000)
    gB.update(500_000)
    gA.update(900_000)  # A 跌 10%
    gB.update(480_000)  # B 跌 4%
    print(f"账户A scale={gA.scale_factor()} (应=0.5, 跌10%)")
    print(f"账户B scale={gB.scale_factor()} (应=1.0, 跌4%未触线)")
    print(f"隔离测试: {'PASS' if gA.scale_factor() != gB.scale_factor() else 'FAIL'}")

    try:
        for p in [tmp_path, tmp_a, tmp_b]:
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass
