# -*- coding: utf-8 -*-
"""四维策略 · 仓位状态机（从 da龘 移植并适配）
================================================
da龘 的 RiskStateMachine 是账户级「会认输」的风控：NORMAL→WARNING→LOCKED，
由 risk_guard 快照（保证金率红线 / 日亏停机线）推进，叠加连续止损降档，
LOCKED 时禁止新开仓。

★ 双轨风控架构（2026-08-25 P1-15 定稿）：
  轨1（本模块 + KillSwitch）：保证金红线/日亏停机/连亏 → 状态机软降档 + 硬熔断
  轨2（drawdown_guard）：    动态权益峰值 → 多档渐变回撤降险（5%/10%/15%）
  合流：get_combined_risk_scale() → min(轨1_scale, 轨2_scale) 取较严者

用法（runner 调用）：
  import risk_state_machine as rsm
  rsm.init_dual_track()                          # 启动时调用一次
  rsm.update_risk_state(equity, used_margin, daily_pnl, consec_losses)
  info = rsm.get_combined_risk_scale()            # 每轮取合并缩放
  if info['locked']: ...
  scale = info['combined']
"""
from __future__ import annotations

import json
import os
import threading
import time

# ============ 风控闸参数（da哥 原值） ============
RED_LINE = 0.45          # 保证金使用率红线（禁新开）
DAILY_LOSS_STOP = 0.05   # 当日亏损停机线（强制冻结）
WARN_LINE = 0.40         # 预警线
SINGLE_LEG = 0.30        # 单笔保证金占比上限
DRAWDOWN_BASE = 0.10      # 回撤基准
DRAWDOWN_TRIGGER = 0.08   # 回撤 0.8×基准 触发降档（=0.8×10%）
LOCK_RELEASE_SEC = 300    # LOCKED 解锁冷却（秒）
WARN_RELEASE_SEC = 120    # WARNING 回 NORMAL 冷却（秒）
LOSS_DECAY = 0.8          # 连续止损手数缩放
LOSS_FLOOR = 0.2          # 手数缩放封底
CONSEC_WARN = 2           # 连续止损 ≥2 笔 → WARNING 软警告（手数降档）
CONSEC_LOCK = 3           # 连续止损 ≥3 笔 → 当日冻结（LOCKED，禁新开，跨日解除）

# 可配置化：从 trade_config.json 读取 consec_loss_gate，缺省回退上述常量
def _load_consec_gate():
    """从 trade_config.json 读取连续止损阈值配置，缺省用 CONSEC_WARN/CONSEC_LOCK。"""
    global CONSEC_WARN, CONSEC_LOCK
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            gate = cfg.get("consec_loss_gate") or {}
            w = int(gate.get("warn", CONSEC_WARN))
            l = int(gate.get("lock", CONSEC_LOCK))
            if w >= 1 and l > w:
                CONSEC_WARN = w
                CONSEC_LOCK = l
                print(f"[risk_fsm] 加载 consec_loss_gate: warn={w}, lock={l}")
    except Exception:
        pass

_load_consec_gate()

# ============ 分品种专项风控（回测结论 2026-08-16） ============
# 回测(全市场5m出场 + 红线①探针)显示：
#   · JM/J 实盘 on 胜率低于 off（焦煤27.2% / 焦炭34.2%），靠 R 乘数盈利
#     → 单笔保证金占比收紧 + 强制止损，防单笔大亏侵蚀期望R。
#   · SA 对方向源(T_D vs T_5m)最敏感 → 方向质量由 direction_source_monitor 专项盯。
PER_SYMBOL_RISK = {
    "JM": {"single_leg": 0.18, "strict_stop": True,
           "note": "焦煤低胜率(27%)：单笔≤18%、强制止损不可放宽"},
    "J":  {"single_leg": 0.18, "strict_stop": True,
           "note": "焦炭低胜率(34%)：单笔≤18%、强制止损不可放宽"},
}
SA_SENSITIVE = True       # SA 方向源最敏感（红线①）


def per_symbol_override(symbol):
    """返回该品种的风控覆盖参数；无覆盖返回 None。供 runner/面板读取。"""
    return PER_SYMBOL_RISK.get(symbol)

# ============ 组合级硬熔断 kill-switch（#5） ============
# 与上面的「软降档」不同：硬熔断是最后一道保险 —— 触发即 **全平 + 停机**，
# 且 **不自动恢复**（必须人工在面板点「解除熔断」），跨重启持久化。
KILL_DRAWDOWN = 0.15       # 账户权益自峰值回撤 ≥15% → 硬熔断
KILL_DAILY_LOSS = 0.08     # 当日亏损占权益 ≥8% → 硬熔断（软停机线是 5%）
KILL_CONSEC_LOSSES = 6     # 连续止损 ≥6 笔 → 硬熔断
_HERE = os.path.dirname(os.path.abspath(__file__))
KILL_STATE_FILE = os.path.join(_HERE, "killswitch_state.json")


def risk_guard(equity, used_margin=0.0, daily_pnl=0.0, proposed_margin=0.0,
               red_line=RED_LINE, daily_loss_stop=DAILY_LOSS_STOP,
               single_leg=SINGLE_LEG, warn_line=WARN_LINE, symbol=None,
               opening_equity=None):
    """da哥 风控闸: 45%红线 / 日亏5%停机 / 单笔30% / 回撤预警。
    返回 {status:'OK'|'WARN'|'LOCK', usage, daily_loss_pct, reasons}。
    proposed_margin 计入红线评估(破线前预警)。
    symbol: 传入时分品种应用 PER_SYMBOL_RISK 覆盖（JM/J 收紧单笔上限 + 强制止损）。"""
    # 分品种覆盖：低胜率品种收紧单笔保证金占比
    ov = per_symbol_override(symbol) if symbol else None
    if ov:
        single_leg = min(single_leg, ov.get("single_leg", single_leg))
    total = used_margin + proposed_margin
    usage = total / equity if equity > 0 else 0.0
    _base_eq = opening_equity if opening_equity and opening_equity > 0 else equity
    daily_loss_pct = max(0.0, -daily_pnl) / _base_eq if _base_eq > 0 else 0.0
    reasons = []
    status = "OK"
    if usage >= red_line:
        status = "LOCK"
        reasons.append(f"保证金使用率 {usage*100:.0f}% 破 {red_line*100:.0f}% 红线，禁止新开仓")
    if daily_loss_pct >= daily_loss_stop:
        status = "LOCK"
        reasons.append(f"当日亏损 {daily_loss_pct*100:.1f}% 达 {daily_loss_stop*100:.0f}% 停机线，强制冻结")
    if status == "OK":
        if usage >= warn_line:
            status = "WARN"
            reasons.append(f"保证金使用率 {usage*100:.0f}% 接近红线，谨慎")
        if equity > 0 and proposed_margin / equity >= single_leg:
            status = "WARN" if status == "OK" else status
            reasons.append(f"单笔保证金占比 {proposed_margin/equity*100:.0f}% 超 {single_leg*100:.0f}%"
                           + (f"（{symbol}低胜率专项收紧）" if ov else ""))
        if ov and ov.get("strict_stop"):
            reasons.append(f"{symbol} 强制止损(低胜率专项)：止损位不可放宽，单笔≤{single_leg*100:.0f}%")
    return {"status": status, "usage": round(usage, 3),
            "daily_loss_pct": round(daily_loss_pct, 3), "reasons": reasons,
            "symbol_override": ov}


class RiskStateMachine:
    """da哥 仓位状态机: NORMAL→WARNING→LOCKED 流转 + 回撤降档 + 连续止损降档 + 恢复冷却。

    - 状态由账户级 risk_guard 快照推进(每轮 runner 评估一次)。
    - scale(): 当前仓位缩放因子 —— LOCKED=0(禁新开), WARNING=0.5, 叠加连续止损×0.8^n(封底0.2)。
    - 回撤0.8底: 权益自峰值回撤 > 8% 触发降档至 WARNING。
    - 恢复: LOCKED 需红线/日亏解除且冷却>300s 才解锁到 WARNING; WARNING 冷却>120s 回 NORMAL。
    """
    NORMAL, WARNING, LOCKED = "NORMAL", "WARNING", "LOCKED"

    def __init__(self):
        self.state = self.NORMAL
        self.entered_at = time.time()
        self.consec_losses = 0
        self.peak_equity = None
        self.lock_reason = ""
        self.last_update = 0
        self.daily_loss_pct = 0.0   # 当日亏损占权益比（由 risk_guard 快照写入）
        # 必须初始化：否则「红线触锁」路径下 state==LOCKED 后，update() L158 读
        # self.daily_loss_locked 会 AttributeError（该属性原仅在 reset_daily / 日亏触锁分支赋值；
        # live runner 从不调用 reset_daily_if_new_day → 上线后属性恒未定义 → 红线锁无法自动释放）。
        self.daily_loss_locked = False
        self.consec_lock = False   # 当日连续止损冻结标记（跨日解除，类似 daily_loss_locked）
        self._lock = threading.RLock()  # 可重入：update() 内调用 summary() 不会死锁

    def mark_loss(self):
        with self._lock:
            self.consec_losses += 1

    def reset_daily(self):
        with self._lock:
            self.consec_losses = 0
            self.daily_loss_locked = False  # 跨日重置：当日日亏锁解除，新交易日可正常交易
            self.consec_lock = False   # 跨日解除当日连续止损冻结

    def scale(self):
        if _kill_halted() or self.state == self.LOCKED:
            return 0.0
        base = 0.5 if self.state == self.WARNING else 1.0
        loss_factor = max(LOSS_FLOOR, LOSS_DECAY ** self.consec_losses)
        return round(base * loss_factor, 3)

    def update(self, rg, equity=None):
        with self._lock:
            now = time.time()
            status = rg.get("status")
            usage = rg.get("usage", 0)
            dlp = rg.get("daily_loss_pct", 0)
            if equity:
                if self.peak_equity is None or equity > self.peak_equity:
                    self.peak_equity = equity
            if status == "LOCK":
                if self.state != self.LOCKED:
                    self.state = self.LOCKED
                    self.entered_at = now
                    self.lock_reason = "；".join(rg.get("reasons", [])) or "风控触发"
                # P0-1：若本次锁由「当日亏损」触发，标记当日锁（跨日才解除，不因浮亏回吐解锁）
                if any("日亏" in r or "daily" in r.lower() for r in (rg.get("reasons") or [])):
                    self.daily_loss_locked = True
            elif self.state == self.LOCKED:
                # P0-1：当日日亏触发的软锁，跨日才解除（不因盘中浮亏回吐自动解锁）；
                # 红线触发的锁仍按原条件(usage<红线+冷却)释放。
                _can_release = (usage < RED_LINE and (now - self.entered_at) > LOCK_RELEASE_SEC
                                and not self.daily_loss_locked and not self.consec_lock)
                if _can_release:
                    self.state = self.WARNING
                    self.entered_at = now
                    self.lock_reason = ""
            elif status == "WARN":
                if self.state == self.NORMAL:
                    self.state = self.WARNING
                    self.entered_at = now
            elif status == "OK":
                if self.state == self.WARNING and (now - self.entered_at) > WARN_RELEASE_SEC:
                    self.state = self.NORMAL
                    self.entered_at = now
            # 注意：回撤降档已移交给 drawdown_guard（#119 多档渐变 + 跨重启持久化），
            # P-连损：连续止损 3 笔 → 当日冻结；2 笔 → 软警告（用户口径 2026-08-19）
            # 红线/日亏 LOCK 优先：不覆盖其 lock_reason，只设置 consec_lock 锁标记
            if self.consec_losses >= CONSEC_LOCK:
                if not self.consec_lock:
                    self.consec_lock = True
                if status != "LOCK" and self.state != self.LOCKED:
                    self.state = self.LOCKED
                    self.entered_at = now
                    self.lock_reason = f"连续止损{self.consec_losses}笔（≥{CONSEC_LOCK}），当日冻结"
            elif self.consec_losses >= CONSEC_WARN and self.state == self.NORMAL and status != "LOCK":
                self.state = self.WARNING
                self.entered_at = now
                self.lock_reason = f"连续止损{self.consec_losses}笔，软警告"

            # 此处不再自行按 0.8×基准降档，避免与 drawdown_guard 双重惩罚。
            # 硬熔断 KILL_DRAWDOWN 仍由 KillSwitch 负责，二者在 15% 处对齐。
            self.daily_loss_pct = dlp
            self.last_update = now
            return self.summary()

    def summary(self):
        with self._lock:
            d = {
                "state": self.state,
                "scale": self.scale(),
                "consec_losses": self.consec_losses,
                "peak_equity": self.peak_equity,
                "lock_reason": self.lock_reason,
                "daily_loss_pct": round(self.daily_loss_pct, 4),
                "daily_loss_stop": DAILY_LOSS_STOP,
                "updated": time.strftime("%H:%M:%S"),
            }
            try:
                d["killswitch"] = KILL.summary()
                if d["killswitch"].get("halted"):
                    d["state"] = "HALTED"      # 面板显示最高优先级状态
            except Exception:
                pass
            return d


# ===========================================================================
# 组合级硬熔断 KillSwitch（#5）
# ===========================================================================
def _pos_lots(p):
    for k in ("lots", "lot", "qty", "volume", "手数", "size"):
        v = p.get(k)
        if v not in (None, ""):
            try:
                return abs(int(round(float(v))))
            except Exception:
                continue
    return 0


def _pos_dir(p):
    """返回 +1 多 / -1 空 / 0 未知。"""
    for k in ("direction", "dir", "side", "方向"):
        v = p.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return 1 if v > 0 else (-1 if v < 0 else 0)
        s = str(v).strip().lower()
        if s in ("long", "buy", "多", "做多", "多头", "1", "+1"):
            return 1
        if s in ("short", "sell", "空", "做空", "空头", "-1"):
            return -1
    return 0


def build_flatten_plan(positions):
    """把当前持仓翻译成「一键全平」指令清单（供人工在交易软件执行 / 未来对接下单）。"""
    plan = []
    for p in (positions or []):
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol") or p.get("sym") or p.get("代码") or ""
        lots = _pos_lots(p)
        if not sym or lots <= 0:
            continue
        d = _pos_dir(p)
        plan.append({
            "symbol": sym,
            "name": p.get("name") or p.get("名称") or sym,
            "lots": lots,
            "action": "平多（卖平）" if d > 0 else ("平空（买平）" if d < 0 else "全平"),
            "side": d,
            "price": p.get("price") or p.get("last") or p.get("现价"),
        })
    return plan


class KillSwitch:
    """账户级硬熔断：回撤/日亏/连亏任一击穿硬线 → 全平 + 停机 + 人工才能解除。

    设计要点（与软状态机的分工）：
      · 软层（RiskStateMachine）：降档、缩仓、可自动恢复 —— 处理「今天手气差」。
      · 硬层（KillSwitch）：全平、禁开、**永不自动恢复** —— 处理「模型或人已经失控」。
    状态落盘 killswitch_state.json，进程重启后仍然是熔断态（防「重启洗白」）。
    """

    def __init__(self, path=KILL_STATE_FILE):
        self.path = path
        self.halted = False
        self.reason = ""
        self.triggers = []          # 命中的硬线列表
        self.triggered_at = 0.0
        self.metrics = {}
        self.flatten_plan = []
        self.history = []           # 历次熔断/解除记录
        self.ack = False            # 用户是否已确认（已按清单全平）
        self._opening_equity = None  # P0-7 fix: 日初权益(固定值)，稳定风控阈值
        self.reset_at = None        # 最近一次人工解除熔断的时间戳
        self._lock = threading.RLock()
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f) or {}
                self.halted = bool(d.get("halted"))
                self.reason = d.get("reason", "")
                self.triggers = d.get("triggers", []) or []
                self.triggered_at = float(d.get("triggered_at") or 0)
                self.metrics = d.get("metrics", {}) or {}
                self.flatten_plan = d.get("flatten_plan", []) or []
                self.history = d.get("history", []) or []
                self.ack = bool(d.get("ack"))
                self._opening_equity = d.get("_opening_equity")
                self.reset_at = d.get("reset_at")
        except Exception as e:
            print(f"[熔断] 状态载入失败(忽略): {repr(e)[:80]}")

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "halted": self.halted, "reason": self.reason,
                    "triggers": self.triggers, "triggered_at": self.triggered_at,
                    "metrics": self.metrics, "flatten_plan": self.flatten_plan,
                    "history": self.history[-50:], "ack": self.ack,
                    "_opening_equity": self._opening_equity,
                    "reset_at": self.reset_at,
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[熔断] 状态落盘失败(忽略): {repr(e)[:80]}")

    # ---------- 核心判定 ----------
    def check(self, equity, peak_equity=None, daily_pnl=0.0,
              consec_losses=0, positions=None):
        """每轮调用。返回 {halted, newly, reason, triggers, plan, metrics}。"""
        with self._lock:
            dd = 0.0
            if equity and peak_equity and peak_equity > 0:
                dd = max(0.0, (peak_equity - equity) / peak_equity)
            _base_eq = self._opening_equity or equity
            dlp = (max(0.0, -float(daily_pnl or 0)) / _base_eq) if _base_eq else 0.0
            cl = int(consec_losses or 0)
            trig = []
            if dd >= KILL_DRAWDOWN:
                trig.append(f"账户自峰值回撤 {dd*100:.1f}% ≥ 硬线 {KILL_DRAWDOWN*100:.0f}%")
            if dlp >= KILL_DAILY_LOSS:
                trig.append(f"当日亏损 {dlp*100:.1f}% ≥ 硬线 {KILL_DAILY_LOSS*100:.0f}%")
            if cl >= KILL_CONSEC_LOSSES:
                trig.append(f"连续止损 {cl} 笔 ≥ 硬线 {KILL_CONSEC_LOSSES} 笔")
            self.metrics = {"equity": equity, "peak_equity": peak_equity,
                            "drawdown": round(dd, 4), "daily_loss_pct": round(dlp, 4),
                            "consec_losses": cl}
            newly = False
            if trig and not self.halted:
                self.halted = True
                self.ack = False
                self.triggers = trig
                self.reason = "；".join(trig)
                self.triggered_at = time.time()
                self.flatten_plan = build_flatten_plan(positions)
                self.history.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "event": "TRIGGER", "reason": self.reason,
                                     "metrics": dict(self.metrics)})
                self._save()
                newly = True
            elif trig and self.halted:
                # 持续熔断中：刷新全平清单（可能又被动成交/部分平仓）
                self.flatten_plan = build_flatten_plan(positions) or self.flatten_plan
            # P1-12 fix: 尝试自动恢复（熔断条件已解除 + 冷却足够）
            if not trig and self.halted and self.ack:
                self._maybe_recover(equity, peak_equity, daily_pnl, consec_losses)
            return {"halted": self.halted, "newly": newly, "reason": self.reason,
                    "triggers": list(self.triggers), "plan": list(self.flatten_plan),
                    "metrics": dict(self.metrics)}

    def _maybe_recover(self, equity, peak_equity, daily_pnl, consec_losses):
        """P1-12 深度重审补上：熔断自动恢复（原修复遗漏了方法体，属空转）。

        触发条件（AND 全部成立才解除熔断）：
        1. self.halted=True 且 self.ack=True（用户已确认全平）
        2. 冷却期已满（自触发时间 > _COOLDOWN_SEC）
        3. 当前权益 > 熔断峰值的一定比例（未继续恶化）
        4. 当日亏损已收敛（daily_pnl 不再触及原熔断阈值）
        5. 连亏计数已清零或降至阈值以下
        """
        RECOVER_COOLDOWN_SEC = 3600  # 1h 冷却
        RECOVER_EQUITY_RATIO = 0.92  # 权益需维持在熔断峰值 92% 以上
        RECOVER_CONSEC_MAX = 2       # 连亏不得超过 2 笔
        RECOVER_DAILY_PNL_TOLERANCE = 0.01  # 当日盈亏与熔断时的偏离容忍

        if not (self.halted and self.ack):
            return
        now = time.time()
        if self.triggered_at and (now - self.triggered_at) < RECOVER_COOLDOWN_SEC:
            return
        # 条件 3: 权益未继续恶化
        peak = peak_equity or self.metrics.get("peak_equity", equity)
        if equity < peak * RECOVER_EQUITY_RATIO:
            return
        # 条件 4: 当日盈亏未显著恶化
        orig_daily_pnl = self.metrics.get("orig_daily_pnl_at_kill", daily_pnl)
        if orig_daily_pnl < 0 and daily_pnl < orig_daily_pnl - abs(orig_daily_pnl) * RECOVER_DAILY_PNL_TOLERANCE:
            return
        # 条件 5: 连亏已收敛
        if consec_losses is not None and consec_losses > RECOVER_CONSEC_MAX:
            return
        # 所有条件满足 → 自动恢复
        self.halted = False
        self.ack = False
        self.reason = ""
        self.triggers = []
        self.triggered_at = 0.0
        self.flatten_plan = []
        self.history.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "event": "AUTO_RECOVER",
                             "note": f"熔断自动恢复（冷却{int(now - self.triggered_at)}s + 权益{equity:.0f} + 连亏{consec_losses}）"})
        self._save()

    def acknowledge(self):
        """用户确认「已按清单全平」——仍处于熔断态，只是消掉红色催办。"""
        with self._lock:
            if self.halted:
                self.ack = True
                self.history.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "event": "ACK"})
                self._save()
            return self.summary()

    def reset(self, note="人工解除", reset_peak_to=None):
        """人工解除熔断（唯一出口）。可顺便把峰值权益重置到当前，避免刚解除又被旧峰值秒杀。"""
        with self._lock:
            was = self.halted
            self.reset_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.halted = False
            self.ack = False
            self.reason = ""
            self.triggers = []
            self.flatten_plan = []
            # 清除旧 metrics 快照：防止非熔断态检查误报"数据不新鲜"
            self.metrics = {"equity": 0, "peak_equity": 0,
                            "drawdown": 0.0, "daily_loss_pct": 0.0,
                            "consec_losses": 0}
            if was:
                self.history.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "event": "RESET", "note": note})
            self._save()
            if reset_peak_to:
                try:
                    RISK_FSM.peak_equity = float(reset_peak_to)
                except Exception:
                    pass
            return self.summary()

    def summary(self):
        with self._lock:
            d = {
                "halted": self.halted,
                "reason": self.reason,
                "triggers": list(self.triggers),
                "ack": self.ack,
                "metrics": dict(self.metrics),
                "flatten_plan": list(self.flatten_plan),
                "thresholds": {"drawdown": KILL_DRAWDOWN,
                               "daily_loss": KILL_DAILY_LOSS,
                               "consec_losses": KILL_CONSEC_LOSSES},
                "history": self.history[-10:],
            }
            if self.triggered_at:
                d["triggered_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(self.triggered_at))
                d["halted_min"] = round((time.time() - self.triggered_at) / 60, 1)
            return d


# 全局单例（runner 全程共享）
RISK_FSM = RiskStateMachine()
KILL = KillSwitch()


def _kill_halted():
    try:
        return bool(KILL.halted)
    except Exception:
        return False


def update_risk_state(equity, used_margin=0.0, daily_pnl=0.0,
                      consec_losses=None, proposed_margin=0.0, positions=None,
                      peak_equity=None, symbol=None):
    """runner 每轮调用：用账户快照推进状态机 + 组合级硬熔断判定。
    - equity: 当前权益（来自 account_tracker 或账户监控）
    - used_margin: 已占用保证金
    - daily_pnl: 当日已实现盈亏（负值=亏损）
    - consec_losses: 当前连续止损笔数（来自成交记录器；None 则沿用现状）
    - positions: 当前持仓列表（用于生成硬熔断的一键全平清单）
    - peak_equity: 跨重启持久化的峰值权益（由 drawdown_guard 维护）；
                  传入可让硬熔断基于稳定峰值而非本进程内存峰值
    - symbol: 拟开仓品种（分品种专项风控 JM/J 收紧单笔上限 + 强制止损）
    - 返回 summary dict（含 killswitch 字段；新触发时带 kill_newly=True）
    """
    if consec_losses is not None:
        with RISK_FSM._lock:
            RISK_FSM.consec_losses = max(0, int(consec_losses))
    if peak_equity is not None:
        try:
            pe = float(peak_equity)
            with RISK_FSM._lock:
                if RISK_FSM.peak_equity is None or pe > RISK_FSM.peak_equity:
                    RISK_FSM.peak_equity = pe
        except Exception:
            pass
    rg = risk_guard(equity, used_margin, daily_pnl, proposed_margin, symbol=symbol)
    RISK_FSM.update(rg, equity=equity)
    ks = KILL.check(equity, RISK_FSM.peak_equity, daily_pnl,
                    RISK_FSM.consec_losses, positions)
    out = RISK_FSM.summary()
    out["kill_newly"] = ks.get("newly", False)
    if rg.get("symbol_override"):
        out["symbol_override"] = rg["symbol_override"]
        out["symbol"] = symbol
    return out



# ===========================================================================
# P1-15: 双轨风控统一接口 —— drawdown_guard + risk_state_machine 分工明确化
# ===========================================================================
# 分工原则（2026-08-25 定稿）：
#   · drawdown_guard (ddg)：唯一的「回撤降险」数据源 —— 多档渐变(5%/10%/15%) + 跨重启持久化
#     负责：账户动态权益峰值追踪 → 回撤分档 → 对应缩放系数
#   · risk_state_machine (rsm)：唯一的「状态机 + 硬熔断」数据源
#     负责：保证金红线(45%) / 日亏停机(5%) / 连续止损(2→3笔) → NORMAL→WARNING→LOCKED
#           + 硬熔断 KillSwitch(15%回撤/8%日亏/6连亏 → 全平+禁开)
#   · 组合规则：combined = min(rsm_scale, ddg_scale) —— 取较严者，杜绝双重惩罚
#     注意：15% 处二者对齐（ddg=0.0, KILL_DRAWDOWN 硬熔断），形成双保险而非重复惩罚
#
# 调用方式（runner 应统一使用此函数，不再手动 min(scale, dd_scale)）：
#   import risk_state_machine as rsm
#   rsm.init_dual_track()                    # 启动时调用一次
#   scale_info = rsm.get_combined_risk_scale()  # 每轮开仓前调用
#   if scale_info['locked']: 禁止开仓
#   sig['lots'] = int(sig['lots'] * scale_info['combined'])
# ---------------------------------------------------------------------------

_DUAL_TRACK_INIT = False


def init_dual_track():
    """P1-15: 启动时初始化双轨风控系统。
    
    必须在 runner 主循环开始前调用一次，确保 drawdown_guard 加载水位线配置。
    幂等：重复调用不会重复初始化。"""
    global _DUAL_TRACK_INIT
    if _DUAL_TRACK_INIT:
        return
    try:
        import drawdown_guard as _ddg
        _ddg.init_from_config()
        _DUAL_TRACK_INIT = True
    except Exception as e:
        print(f"[双轨风控] drawdown_guard 初始化失败: {repr(e)[:80]}")


def get_combined_risk_scale():
    """P1-15: 获取双轨风控合并后的手数缩放系数。
    
    返回 dict:
      - combined: 合并后的缩放系数（= min(rsm_scale, ddg_scale)，取较严者）
      - rsm_scale: risk_state_machine 侧缩放（0.0=LOCKED/HALTED, 0.5=WARNING, 1.0=NORMAL）
      - ddg_scale: drawdown_guard 侧缩放（1.0/0.70/0.50/0.00，多档渐变）
      - locked: 是否禁止新开仓（rsm LOCKED 或 KILL HALTED 或 ddg 0.0）
      - rsm_state: 状态机当前状态字符串
      - dd_tier: drawdown_guard 当前档位（0=正常, 1-3=对应水位线档）
      - dd_pct: 当前回撤百分比
      - halted: 是否处于硬熔断状态
      - reasons: 触发缩放的原因列表
    """
    global _DUAL_TRACK_INIT
    reasons = []
    
    # 1. risk_state_machine 侧
    rsm_scale = RISK_FSM.scale()
    rsm_state = RISK_FSM.state
    rsm_locked = (rsm_state == RiskStateMachine.LOCKED)
    if _kill_halted():
        rsm_locked = True
        rsm_scale = 0.0
        reasons.append(f"硬熔断(HALTED): {KILL.reason}")
    elif rsm_state == RiskStateMachine.LOCKED:
        reasons.append(f"状态机锁定(LOCKED): {RISK_FSM.lock_reason}")
    elif rsm_state == RiskStateMachine.WARNING:
        reasons.append("状态机预警(WARNING): 手数按 0.5x 缩放")
    
    # 2. drawdown_guard 侧
    ddg_scale = 1.0
    dd_tier = 0
    dd_pct = 0.0
    if _DUAL_TRACK_INIT:
        try:
            import drawdown_guard as _ddg
            _st = _ddg.current()
            ddg_scale = float(_st.get("scale", 1.0))
            dd_tier = int(_st.get("tier", 0))
            dd_pct = float(_st.get("dd_pct", 0))
            if ddg_scale < 1.0:
                reasons.append(f"回撤降险: {dd_pct:.1f}% -> 档位{dd_tier}(系数{ddg_scale})")
        except Exception:
            pass
    
    # 3. 市场情绪侧（#8）：极端情绪→缩仓
    sent_scale = 1.0
    sent_label = "中性"
    sent_score = 50.0
    try:
        import sentiment_engine as _se
        _snap = _se.get_snapshot()
        sent_scale = float(_snap.get("scale", 1.0))
        sent_label = _snap.get("label", "中性")
        sent_score = float(_snap.get("score", 50.0))
        if sent_scale < 1.0:
            reasons.append(f"市场情绪({sent_label}={sent_score:.0f}): 缩仓×{sent_scale}")
    except Exception:
        pass

    # 4. 合并：取较严者（三轨：rsm + ddg + sentiment）
    combined = round(min(rsm_scale, ddg_scale, sent_scale), 3)
    locked = rsm_locked or (ddg_scale <= 0.0)

    return {
        "combined": combined,
        "rsm_scale": rsm_scale,
        "ddg_scale": ddg_scale,
        "sent_scale": sent_scale,
        "sent_label": sent_label,
        "sent_score": sent_score,
        "locked": locked,
        "rsm_state": rsm_state,
        "dd_tier": dd_tier,
        "dd_pct": dd_pct,
        "halted": _kill_halted(),
        "reasons": reasons,
    }

def is_locked():
    """禁止新开仓（软锁 LOCKED 或 硬熔断 HALTED 任一成立）。"""
    return _kill_halted() or RISK_FSM.state == RiskStateMachine.LOCKED


def is_halted():
    return _kill_halted()


def reset_daily_if_new_day(last_day_str):
    """跨日重置连续止损计数（可选，配合 runner 跨日逻辑调用）。
    仅当 last_day_str 与今日日期不同时才重置。"""
    today_str = time.strftime("%Y-%m-%d")
    if last_day_str != today_str:
        RISK_FSM.reset_daily()
        # P0-7 fix: 同时记录日初权益
        try:
            import account_tracker as at
            st = at.load_state()
            eq = st.get("equity", 0) if st else 0
            if eq > 0:
                KILL._opening_equity = eq
                KILL._save()
        except Exception:
            pass
        return today_str
    return last_day_str


if __name__ == "__main__":
    # 自测：软层降档 + 硬熔断全流程（用临时状态文件，不污染真实熔断态）
    import tempfile
    KILL.path = os.path.join(tempfile.gettempdir(), "killswitch_selftest.json")
    KILL.reset("自测起始")
    print("初始:", RISK_FSM.summary()["state"], "scale=", RISK_FSM.scale())
    s = update_risk_state(1000000, used_margin=470000, daily_pnl=0, consec_losses=0)
    print("红线破:", s["state"], "scale=", s["scale"], "锁定?", is_locked())

    # 硬熔断：权益从 100 万峰值掉到 84 万（-16% > 15% 硬线）
    pos = [{"symbol": "FG", "name": "玻璃", "direction": "多", "lots": 3, "price": 1200},
           {"symbol": "jd", "name": "鸡蛋", "side": "short", "qty": 2, "price": 3400}]
    s2 = update_risk_state(840000, used_margin=100000, daily_pnl=-20000,
                           consec_losses=1, positions=pos)
    ks = s2["killswitch"]
    print("硬熔断?", ks["halted"], "| 原因:", ks["reason"])
    print("全平清单:", ks["flatten_plan"])
    print("状态:", s2["state"], "scale=", s2["scale"], "禁新开?", is_locked())

    # 权益回升也不自动解除（必须人工）
    s3 = update_risk_state(990000, used_margin=0, daily_pnl=0, consec_losses=0)
    print("回升后仍熔断?", s3["killswitch"]["halted"], "state=", s3["state"])
    print("人工解除:", KILL.reset("自测解除", reset_peak_to=990000)["halted"],
          "| 恢复 scale=", RISK_FSM.scale())

    # —— P-连损自测（2026-08-19 模板H）：consec=2→WARNING / consec=3→LOCKED+consec_lock / reset_daily 解锁 ——
    _consec_ok = True
    # 清场：模拟新进程/新交易日干净状态（reset_daily 只清 consec 计数，不清 state；此处显式重置避免被前序硬熔断测试污染）
    RISK_FSM.state = RiskStateMachine.NORMAL
    RISK_FSM.entered_at = time.time()
    RISK_FSM.consec_losses = 0
    RISK_FSM.consec_lock = False
    RISK_FSM.daily_loss_locked = False
    _w = update_risk_state(1000000, used_margin=0, daily_pnl=0, consec_losses=2)
    if _w["state"] != "WARNING":
        _consec_ok = False
        print(f"[FAIL] consec=2 应 WARNING，实际 {_w['state']}")
    _l = update_risk_state(1000000, used_margin=0, daily_pnl=0, consec_losses=3)
    if _l["state"] != "LOCKED":
        _consec_ok = False
        print(f"[FAIL] consec=3 应 LOCKED，实际 {_l['state']}")
    if not RISK_FSM.consec_lock:
        _consec_ok = False
        print("[FAIL] consec=3 应置 consec_lock=True")
    RISK_FSM.reset_daily()
    if RISK_FSM.consec_lock or RISK_FSM.consec_losses != 0:
        _consec_ok = False
        print("[FAIL] reset_daily 应清 consec_lock 且 consec_losses=0")
    print("P-连损自测:", "ALL PASS ✅" if _consec_ok else "有 FAIL ❌")

    try:
        os.remove(KILL.path)
    except Exception:
        pass
