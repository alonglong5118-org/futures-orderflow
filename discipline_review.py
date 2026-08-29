"""四维策略 · 管住手复盘卡（每日 / 每周 / 每月 trading-discipline review）
================================================================================
从模型已有数据自动算「管住手」纪律评分，帮交易员复盘：今天 / 本周 / 本月
有没有管住手。这是把「风控四闸 + 仓位状态机」从“机器约束”补成“人也能看见
自己守没守纪律”的闭环。

数据源（全部本地 JSON，零网络依赖）：
  - trade_journal.json    : 真实成交（含 signal_id / exit_reason）
  - four_dim_signals.json : 引擎信号（含 time / symbol / direction）
  - account_state.json    : 当前权益 + 持仓（算仓位纪律）
  - trade_config.json     : 合约参数 + 风控上限（单品/组合保证金上限）
  - discipline_events.json: runner 每轮追加的状态机锁死/开仓事件（无则锁违=0）

评分维度（满分 100，逐项扣分，封底 0）：
  C1 无冲动开仓    manual_trades==0          -> -15/笔（封顶 -40）
  C2 锁死不开仓    lock_violations==0        -> -30/笔
  C3 单品不超上限  max_single_leg<=cap       -> -15
  C4 组合不超上限  portfolio_pct<=cap         -> -15
  C5 未破日亏线    period_loss_pct<=5%       -> -25
  C6 止损不扛单    closed_loss_manual==0      -> -8/笔（封顶 -20）
评级：A 90-100（严守） / B 75-89（良好） / C 60-74（有瑕疵） / D <60（失控）

用法（被 runner 调用）：
  import discipline_review as dr
  cards = dr.get_all()   # {"daily":{...}, "weekly":{...}, "monthly":{...}}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# 复盘回看起始日：此前数据无记录/无意义，下拉不显示（数据不删除）
_RECORD_CUTOFF_DATE = "2026-08-11"

JOURNAL_FILE = os.path.join(HERE, "trade_journal.json")
SIGNAL_LOG = os.path.join(HERE, "four_dim_signals.json")
ACCOUNT_FILE = os.path.join(HERE, "account_state.json")
CONFIG_FILE = os.path.join(HERE, "trade_config.json")
EVENTS_FILE = os.path.join(HERE, "discipline_events.json")

_EVENT_LOCK = threading.Lock()

# 评分权重
W_MANUAL = 15  # 每笔冲动/手动开仓
W_MANUAL_CAP = 40  # 冲动扣分封顶
W_LOCK = 30  # 每笔锁死时开仓
W_SINGLE = 15  # 单品超上限
W_PORTFOLIO = 15  # 组合超上限
W_LOSS = 25  # 破日亏线
W_LOSSMANUAL = 8  # 每笔手动砍亏
W_LOSSMANUAL_CAP = 20

_DAILY_LOSS_STOP = 0.05  # 与 risk_state_machine.DAILY_LOSS_STOP 对齐


def _load(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _parse_time(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except Exception:
            return None


def _period_bounds(kind, now=None):
    now = now or datetime.now()
    d = now.date()
    if kind == "daily":
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1)
    elif kind == "weekly":
        monday = d - timedelta(days=d.weekday())  # 周一为周起点
        start = datetime(monday.year, monday.month, monday.day)
        end = start + timedelta(days=7)
    else:  # monthly
        start = datetime(d.year, d.month, 1)
        if d.month == 12:
            end = datetime(d.year + 1, 1, 1)
        else:
            end = datetime(d.year, d.month + 1, 1)
    return start, end


# ---------------------------------------------------------------------------
# 事件日志（runner 调用，记录锁死/开仓事件以便精确判定“锁死时开仓”）
# ---------------------------------------------------------------------------
def log_event(etype, state=None, reason="", symbol="", direction="", lots=0, risk_state=""):
    """追加一条纪律事件。etype: 'risk' | 'entry'。"""
    rec = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": etype,
    }
    if etype == "risk":
        rec["state"] = state
        rec["reason"] = reason
    else:  # entry
        rec["symbol"] = symbol
        rec["direction"] = direction
        rec["lots"] = int(lots)
        rec["risk_state"] = risk_state or ""
    with _EVENT_LOCK:
        arr = _load(EVENTS_FILE, [])
        if not isinstance(arr, list):
            arr = []
        arr.append(rec)
        # 只保留近 180 天，防无限增长
        cutoff = (datetime.now() - timedelta(days=180)).timestamp()
        arr = [e for e in arr if (_parse_time(e.get("time", "")) or datetime.now()).timestamp() >= cutoff]
        json.dump(arr[-5000:], open(EVENTS_FILE, "w"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 信号匹配：一笔成交是否由真实引擎信号驱动
# ---------------------------------------------------------------------------
def _signal_map():
    sigs = _load(SIGNAL_LOG, [])
    m = {}
    for s in sigs:
        t = s.get("time", "")
        if t:
            m[t] = s
    return m


def _is_signal_backed(trade, sig_map):
    sid = (trade.get("signal_id") or "").strip()
    if not sid or sid.lower().startswith("manual"):
        return False
    sig = sig_map.get(sid)
    if not sig:
        return False
    # 同品种才认（避免不同品种同名信号串号）
    return sig.get("symbol") == trade.get("symbol")


def _is_manual_record(trade):
    """判断交易是否为'手动记录'（非冲动开仓）：
    - signal_id 为空 → 用户直接记账，未关联引擎信号
    - signal_id 以 'manual' 开头 → 用户明确标记为手动记录
    这两种情况都不应自动扣分，因为只是如实记录交易，并非冲动开仓。"""
    sid = (trade.get("signal_id") or "").strip()
    return not sid or sid.lower().startswith("manual")


# ---------------------------------------------------------------------------
# 仓位纪律（来自 account_state + trade_config 合约参数）
# ---------------------------------------------------------------------------
def _position_metrics():
    st = _load(ACCOUNT_FILE, {})
    cfg = _load(CONFIG_FILE, {})
    specs = cfg.get("contract_specs", {})
    acc = cfg.get("account", {})
    equity = float(st.get("equity") or acc.get("equity") or 0)
    single_cap = float(acc.get("margin_cap_pct", 30))
    port_cap = float(acc.get("portfolio_margin_cap_pct", 60))
    max_single = 0.0
    total_margin = 0.0
    positions = []
    # 从 trade_journal 匹配当前未平持仓的来源
    trades = _load(JOURNAL_FILE, {}).get("trades", [])
    open_source = {}
    for t in trades:
        if t.get("exit_time"):
            continue
        sym = t.get("symbol")
        if sym:
            open_source[sym] = _source_label(t.get("signal_id"))
    for sym, pos in (st.get("positions") or {}).items():
        sp = specs.get(sym)
        if not sp:
            continue
        mult = sp.get("multiplier", 1)
        mrate = sp.get("margin_rate", 0.1)
        lots = int(pos.get("lots", 0))
        avg = float(pos.get("avg") or 0)
        margin = lots * avg * mult * mrate
        pct = margin / equity * 100 if equity > 0 else 0
        max_single = max(max_single, pct)
        total_margin += margin
        positions.append(
            {
                "symbol": sym,
                "name": sp.get("name", sym),
                "contract": sp.get("contract", sym),
                "direction": pos.get("direction"),
                "lots": lots,
                "avg": avg,
                "margin_pct": round(pct, 2),
                "open_time": pos.get("open_time"),
                "source": open_source.get(sym, "账户同步"),
            }
        )
    port_pct = total_margin / equity * 100 if equity > 0 else 0
    return {
        "equity": equity,
        "single_cap": single_cap,
        "port_cap": port_cap,
        "max_single_leg_pct": round(max_single, 2),
        "portfolio_pct": round(port_pct, 2),
        "positions": positions,
    }


# ---------------------------------------------------------------------------
# 单周期复盘卡
# ---------------------------------------------------------------------------
def _card(kind, now=None):
    now = now or datetime.now()
    start, end = _period_bounds(kind, now)
    trades = _load(JOURNAL_FILE, {}).get("trades", [])
    sig_map = _signal_map()
    events = _load(EVENTS_FILE, [])
    if not isinstance(events, list):
        events = []

    opened, closed_in_period = [], []
    for t in trades:
        et = _parse_time(t.get("time", ""))
        xt = _parse_time(t.get("exit_time", ""))
        if et and start <= et < end:
            opened.append(t)
        if xt and start <= xt < end:
            closed_in_period.append(t)

    # 信号采纳（周期内）：只在“你实际做过的品种”范围内统计采纳率，
    # 避免被 53 个全市场信号稀释成恒为 0（你不可能跟全市场信号）。
    traded_symbols = set(t.get("symbol") for t in trades if t.get("symbol"))
    signals_in_period = [
        s
        for s in sig_map.values()
        if (s.get("symbol") in traded_symbols if traded_symbols else True)
        and (lambda p: p and start <= p < end)(_parse_time(s.get("time", "")))
    ]

    # 2026-08-21: 重构分类逻辑 — 区分冲动开仓 vs 手动记录
    # external_trades: 账户同步/历史持仓等非主动开仓 → 不扣分
    # 2026-08-21: 扩展外部交易识别 — 账户总览等手动记账来源也不应扣分
    _EXT_PREFIXES = ("账户同步", "历史持仓", "对账补录", "对账", "补录", "账户总览")
    external_trades = [t for t in opened if (t.get("signal_id") or "").startswith(_EXT_PREFIXES)]
    # manual_records: 用户手动记录（空 signal_id 或 manual 前缀）→ 不扣分，只是如实记账
    manual_records = [t for t in opened if _is_manual_record(t) and t not in external_trades]
    # impulse_trades: 有 signal_id 但不是引擎信号也不是外部/手动 → 冲动开仓，扣分
    impulse_trades = [
        t for t in opened if not _is_signal_backed(t, sig_map) and not _is_manual_record(t) and t not in external_trades
    ]
    signal_trades = [t for t in opened if _is_signal_backed(t, sig_map)]
    acted_on = len(signal_trades)
    adoption = round(acted_on / len(signals_in_period) * 100, 1) if signals_in_period else (0.0 if opened else 0.0)

    # 锁死时开仓（来自事件）
    lock_violations = [
        e
        for e in events
        if e.get("type") == "entry"
        and e.get("risk_state") == "LOCKED"
        and (lambda p: p and start <= p < end)(_parse_time(e.get("time", "")))
    ]
    warning_entries = [
        e
        for e in events
        if e.get("type") == "entry"
        and e.get("risk_state") == "WARNING"
        and (lambda p: p and start <= p < end)(_parse_time(e.get("time", "")))
    ]

    # 仓位纪律
    pm = _position_metrics()
    single_breach = pm["max_single_leg_pct"] > pm["single_cap"]
    port_breach = pm["portfolio_pct"] > pm["port_cap"]

    # 亏损 / 止损纪律（周期内平仓）
    closed_pnl = sum(t.get("pnl") or 0 for t in closed_in_period)
    closed_loss_manual = [t for t in closed_in_period if (t.get("pnl") or 0) < 0 and t.get("exit_reason") == "手动"]
    # 周期内止损平仓（纪律好，正面计数）
    stopped_out = [t for t in closed_in_period if t.get("exit_reason") == "止损"]
    period_loss_pct = (max(0.0, -closed_pnl) / pm["equity"] * 100) if pm["equity"] > 0 else 0.0
    loss_breach = period_loss_pct >= _DAILY_LOSS_STOP * 100

    # 周期内胜率
    cw = [t for t in closed_in_period if (t.get("pnl") or 0) > 0]
    cwr = round(len(cw) / len(closed_in_period) * 100, 1) if closed_in_period else 0.0

    # ---- 评分 ----
    penalty = 0
    checks = []
    # C1
    m_pen = min(W_MANUAL * len(impulse_trades), W_MANUAL_CAP)
    penalty += m_pen
    checks.append(
        {
            "key": "C1",
            "name": "无冲动开仓",
            "pass": len(impulse_trades) == 0,
            "detail": (
                f"无冲动开仓（手动记录 {len(manual_records)} 笔已豁免）"
                if not impulse_trades
                else f"{len(impulse_trades)} 笔冲动开仓（非信号、非手动记录，扣 {m_pen}），手动记录 {len(manual_records)} 笔已豁免"
            ),
        }
    )
    # C2
    l_pen = W_LOCK * len(lock_violations)
    penalty += l_pen
    checks.append(
        {
            "key": "C2",
            "name": "锁死不开仓",
            "pass": len(lock_violations) == 0,
            "detail": (
                "状态机锁死期间未开仓"
                if not lock_violations
                else f"锁死期间开仓 {len(lock_violations)} 笔（扣 {l_pen}）"
            ),
        }
    )
    # C3
    penalty += W_SINGLE if single_breach else 0
    checks.append(
        {
            "key": "C3",
            "name": "单品不超上限",
            "pass": not single_breach,
            "detail": f"单品最大 {pm['max_single_leg_pct']}% / 上限 {pm['single_cap']}%"
            + (" ⚠️ 超上限" if single_breach else " ✓"),
        }
    )
    # C4
    penalty += W_PORTFOLIO if port_breach else 0
    checks.append(
        {
            "key": "C4",
            "name": "组合不超上限",
            "pass": not port_breach,
            "detail": f"组合占用 {pm['portfolio_pct']}% / 上限 {pm['port_cap']}%"
            + (" ⚠️ 超上限" if port_breach else " ✓"),
        }
    )
    # C5
    penalty += W_LOSS if loss_breach else 0
    checks.append(
        {
            "key": "C5",
            "name": "未破日亏线",
            "pass": not loss_breach,
            "detail": f"区间亏损 {period_loss_pct:.2f}% / 红线 {_DAILY_LOSS_STOP * 100:.0f}%"
            + (" ⚠️ 破线" if loss_breach else " ✓"),
        }
    )
    # C6
    lm_pen = min(W_LOSSMANUAL * len(closed_loss_manual), W_LOSSMANUAL_CAP)
    penalty += lm_pen
    checks.append(
        {
            "key": "C6",
            "name": "止损不扛单",
            "pass": len(closed_loss_manual) == 0,
            "detail": (
                "无手动砍亏" if not closed_loss_manual else f"{len(closed_loss_manual)} 笔亏损手动平仓（扣 {lm_pen}）"
            ),
        }
    )

    score = max(0, 100 - penalty)
    grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))

    # ---- 复盘要点（自动文案）----
    notes = []
    if not opened:
        notes.append(
            "本周期无新开仓，按兵不动（管住手 ✓）。" if kind != "daily" else "今日无新开仓，按兵不动（管住手 ✓）。"
        )
    else:
        period_word = "今日" if kind == "daily" else "本周期"
        parts = []
        if signal_trades:
            parts.append(f"{len(signal_trades)} 笔跟信号")
        if impulse_trades:
            parts.append(f"{len(impulse_trades)} 笔冲动开仓")
        if manual_records:
            parts.append(f"{len(manual_records)} 笔手动记录")
        if external_trades:
            parts.append(f"{len(external_trades)} 笔来自账户同步/历史持仓")
        if len(parts) == 1 and signal_trades:
            notes.append(f"{period_word} {len(signal_trades)} 笔全部按引擎信号开仓，纪律好。")
        elif len(parts) == 1 and impulse_trades:
            notes.append(f"{period_word} {len(impulse_trades)} 笔冲动开仓，未参考四维信号，属高风险冲动交易。")
        elif len(parts) == 1 and external_trades:
            notes.append(f"{period_word} {len(external_trades)} 笔均为账户同步/历史持仓，无新增主动开仓。")
        else:
            notes.append(f"{period_word}开仓 {len(opened)} 笔：{'、'.join(parts)}。")
    if signals_in_period:
        if adoption >= 80:
            notes.append(f"引擎发出 {len(signals_in_period)} 个信号，采纳率 {adoption:.0f}%，跟单到位。")
        elif adoption == 0:
            notes.append(
                f"引擎发出 {len(signals_in_period)} 个信号但你未采纳（仅手动交易），确认是刻意空仓还是漏看信号。"
            )
        else:
            notes.append(
                f"引擎发出 {len(signals_in_period)} 个信号，采纳率 {adoption:.0f}%，可复盘未跟单的信号是否该跟。"
            )
    if lock_violations:
        notes.append(f"⚠️ 状态机 LOCKED 期间仍有 {len(lock_violations)} 笔开仓，严重违规——锁死即应禁手。")
    if warning_entries:
        notes.append(f"WARNING 期间开仓 {len(warning_entries)} 笔，已自动按 0.5× 缩手数，仍在可控范围。")
    if single_breach or port_breach:
        notes.append("⚠️ 仓位突破上限，回撤风险升高，下周期务必降仓。")
    if stopped_out:
        notes.append(f"区间 {len(stopped_out)} 笔按止损离场，止损执行到位（不扛单 ✓）。")
    if closed_loss_manual:
        notes.append(f"区间 {len(closed_loss_manual)} 笔亏损手动平仓，留意是否提前砍在止损前（情绪化操作）。")
    if not notes:
        notes.append("本周期数据较少，无法充分评估，继续积累。")

    label = {"daily": "今日", "weekly": "本周", "monthly": "本月"}[kind]
    period_text = start.strftime("%Y-%m-%d")
    if kind == "weekly":
        period_text += f" 周{['一', '二', '三', '四', '五', '六', '日'][start.weekday()]}"
    elif kind == "monthly":
        period_text += f" ({start.month}月)"

    return {
        "label": label,
        "kind": kind,
        "period_start": start.strftime("%Y-%m-%d"),
        "period_text": period_text,
        "score": score,
        "grade": grade,
        "trades_opened": len(opened),
        "impulse_trades": len(impulse_trades),
        "manual_records": len(manual_records),
        "signal_trades": len(signal_trades),
        "external_trades": len(external_trades),
        "signals_in_period": len(signals_in_period),
        "adoption_rate": adoption,
        "lock_violations": len(lock_violations),
        "warning_entries": len(warning_entries),
        "max_single_leg_pct": pm["max_single_leg_pct"],
        "single_leg_cap": pm["single_cap"],
        "portfolio_pct": pm["portfolio_pct"],
        "portfolio_cap": pm["port_cap"],
        "period_pnl": round(closed_pnl, 2),
        "period_loss_pct": round(period_loss_pct, 2),
        "closed_count": len(closed_in_period),
        "win_rate": cwr,
        "stopped_out": len(stopped_out),
        "closed_loss_manual": len(closed_loss_manual),
        "checks": checks,
        "open_positions": pm["positions"],
        "notes": notes,
    }


def get_all(now=None):
    """返回 日/周/月 三张复盘卡。"""
    return {
        "daily": _card("daily", now),
        "weekly": _card("weekly", now),
        "monthly": _card("monthly", now),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 收盘快照（每日自动落账，供日后回看）：把某一天的复盘卡 + 当天所有操作
# 冻结成一条持久记录，写进 discipline_records.json（按日期索引，幂等覆盖）。
# ---------------------------------------------------------------------------
RECORDS_FILE = os.path.join(HERE, "discipline_records.json")
_CLOSE_SNAP_HOUR = 23  # 收盘快照时点：夜盘 23:00 结束后
_CLOSE_SNAP_MIN = 20
_BACKFILL_DAYS = 7  # 启动时最多补做近 N 天（已过收盘且未记录）


def _sym_name(sym):
    cfg = _load(CONFIG_FILE, {})
    sp = (cfg.get("contract_specs") or {}).get(sym)
    return sp.get("name", sym) if sp else (sym or "")


def _source_label(sig):
    """根据 signal_id 判断操作来源。"""
    if not sig:
        return ""
    if sig.startswith(("账户同步", "历史持仓")):
        return "账户同步"
    if sig == "手动" or sig.lower().startswith("manual_"):
        return "手动"
    # 时间格式如 2026-08-12 14:29:24 视为引擎信号
    if len(sig) >= 19 and sig[4] == "-" and sig[10] == " " and sig[13] == ":":
        return "信号"
    return "其他"


def _duration(t1, t2):
    """返回 t1→t2 的持仓时长（如 '2小时35分'）。"""
    try:
        a = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
        secs = int((b - a).total_seconds())
        if secs < 0:
            return ""
        mins = secs // 60
        if mins < 60:
            return f"{mins}分钟"
        h, m = divmod(mins, 60)
        if m == 0:
            return f"{h}小时"
        return f"{h}小时{m}分"
    except Exception:
        return ""


def _contract(sym):
    cfg = _load(CONFIG_FILE, {})
    sp = (cfg.get("contract_specs") or {}).get(sym)
    return sp.get("contract", sym) if sp else (sym or "")


def _day_operations(date_str):
    """返回 date_str 当天发生的所有操作（开仓 / 平仓），按时间升序。"""
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return []
    trades = _load(JOURNAL_FILE, {}).get("trades", [])
    ops = []
    for t in trades:
        sym = t.get("symbol")
        contract = _contract(sym)
        et = _parse_time(t.get("time", ""))
        xt = _parse_time(t.get("exit_time", ""))
        if et and et.date() == d0:
            ops.append(
                {
                    "time": t.get("time", ""),
                    "action": "开仓",
                    "symbol": sym,
                    "contract": contract,
                    "name": _sym_name(sym),
                    "direction": t.get("direction"),
                    "lots": t.get("lots"),
                    "price": t.get("entry_price"),
                    "signal": (t.get("signal_id") or "手动"),
                    "source": _source_label(t.get("signal_id")),
                    "reason": "",
                    "pnl": None,
                }
            )
        if xt and xt.date() == d0:
            dur = _duration(t.get("time", ""), t.get("exit_time", ""))
            ops.append(
                {
                    "time": t.get("exit_time", ""),
                    "action": "平仓",
                    "symbol": sym,
                    "contract": contract,
                    "name": _sym_name(sym),
                    "direction": t.get("direction"),
                    "lots": t.get("lots"),
                    "price": t.get("exit_price"),
                    "signal": "",
                    "source": _source_label(t.get("signal_id")),
                    "reason": (t.get("exit_reason") or ""),
                    "pnl": t.get("pnl"),
                    "duration": dur,
                }
            )
    ops.sort(key=lambda o: o["time"] or "")
    return ops


def snapshot_day(date_str):
    """计算并冻结某天的复盘记录（幂等，重复调用覆盖）。"""
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        d0 = datetime.now()
    now_end = datetime(d0.year, d0.month, d0.day, 23, 30)
    card = _card("daily", now=now_end)
    ops = _day_operations(date_str)
    rec = dict(card)
    rec["date"] = date_str
    rec["operations"] = ops
    rec["ops_count"] = len(ops)
    rec["snapshot_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _load(RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    records[date_str] = rec
    try:
        json.dump(records, open(RECORDS_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[复盘快照] 写文件失败: {repr(e)[:80]}")
    return rec


def get_record(date_str):
    records = _load(RECORDS_FILE, {})
    return records.get(date_str) if isinstance(records, dict) else None


def _after_cutoff(date_str):
    """日期是否 >= _RECORD_CUTOFF_DATE（字符串比较即可，ISO 格式）。"""
    if not date_str:
        return False
    try:
        return date_str >= _RECORD_CUTOFF_DATE
    except Exception:
        return False


def list_records():
    records = _load(RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    out = []
    for dt, rec in records.items():
        if not _after_cutoff(dt):
            continue
        out.append(
            {
                "date": dt,
                "score": rec.get("score"),
                "grade": rec.get("grade"),
                "trades_opened": rec.get("trades_opened"),
                "signal_trades": rec.get("signal_trades"),
                "impulse_trades": rec.get("impulse_trades", rec.get("manual_trades", 0)),
                "lock_violations": rec.get("lock_violations"),
                "period_pnl": rec.get("period_pnl"),
                "ops_count": rec.get("ops_count"),
                "snapshot_at": rec.get("snapshot_at"),
            }
        )
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _has_activity(date_str):
    """该日期是否有真实交易活动（开/平仓 或 纪律事件），用于避免为无操作旧日造空记录。"""
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    trades = _load(JOURNAL_FILE, {}).get("trades", [])
    for t in trades:
        et = _parse_time(t.get("time", ""))
        xt = _parse_time(t.get("exit_time", ""))
        if (et and et.date() == d0) or (xt and xt.date() == d0):
            return True
    events = _load(EVENTS_FILE, [])
    if isinstance(events, list):
        for e in events:
            et = _parse_time(e.get("time", ""))
            if et and et.date() == d0:
                return True
    return False


def pending_close_dates(now=None):
    """返回需要补做收盘快照的日期：今天及近 _BACKFILL_DAYS 天中，
    已过收盘时点（23:20）、records 中尚无记录、且当日确有交易活动的。
    （无活动的旧日不补，避免造空记录；严格从「今天」起有操作才落账。）"""
    now = now or datetime.now()
    records = _load(RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    pending = []
    for i in range(0, _BACKFILL_DAYS + 1):
        d = (now - timedelta(days=i)).date()
        snap_at = datetime(d.year, d.month, d.day, _CLOSE_SNAP_HOUR, _CLOSE_SNAP_MIN)
        if snap_at <= now:
            ds = d.strftime("%Y-%m-%d")
            if ds not in records and _has_activity(ds):
                pending.append(ds)
    return pending


def run_close_snapshots(now=None, verbose=True):
    """对当前待记录日期逐一落账（供 runner 每轮调用）。返回本次新生成的日期列表。"""
    done = []
    for ds in pending_close_dates(now):
        try:
            snapshot_day(ds)
            done.append(ds)
            if verbose:
                print(f"[复盘快照] 已落账 {ds}")
        except Exception as e:
            print(f"[复盘快照] {ds} 失败: {repr(e)[:80]}")
    return done


# ---------------------------------------------------------------------------
# 周 / 月 复盘快照（在每日快照之上做「聚合」，形成日→周→月三级闭环）
# ---------------------------------------------------------------------------
WEEKLY_RECORDS_FILE = os.path.join(HERE, "discipline_weekly_records.json")
MONTHLY_RECORDS_FILE = os.path.join(HERE, "discipline_monthly_records.json")


# 周/月聚合时各检查项的汇总文案模板
def _agg_detail(key, children):
    if key == "C1":
        tot = sum(c.get("impulse_trades", c.get("manual_trades", 0)) for c in children)
        return (
            "全程无冲动开仓 ✓"
            if tot == 0
            else f"{len(children)} 个子周期中 {sum(1 for c in children if c.get('impulse_trades', c.get('manual_trades', 0)) > 0)} 个有冲动开仓，共 {tot} 笔"
        )
    if key == "C2":
        tot = sum(c.get("lock_violations", 0) for c in children)
        return (
            "全程锁死期间未开仓 ✓"
            if tot == 0
            else f"{sum(1 for c in children if c.get('lock_violations', 0) > 0)} 个周期锁死期间共 {tot} 笔开仓（严重）"
        )
    if key == "C3":
        worst = max((c.get("max_single_leg_pct", 0) for c in children), default=0)
        cap = children[0].get("single_leg_cap", 30)
        return f"单品最大 {worst:.1f}% / 上限 {cap}%"
    if key == "C4":
        worst = max((c.get("portfolio_pct", 0) for c in children), default=0)
        cap = children[0].get("portfolio_cap", 60)
        return f"组合最大 {worst:.1f}% / 上限 {cap}%"
    if key == "C5":
        worst = max((c.get("period_loss_pct", 0) for c in children), default=0)
        red = _DAILY_LOSS_STOP * 100
        return "全程未破日亏线 ✓" if worst < red else f"最差单周期亏损 {worst:.2f}% / 红线 {red:.0f}%（曾破线）"
    if key == "C6":
        tot = sum(c.get("closed_loss_manual", 0) for c in children)
        return (
            "全程无情绪化砍亏 ✓"
            if tot == 0
            else f"{sum(1 for c in children if c.get('closed_loss_manual', 0) > 0)} 个周期共 {tot} 笔亏损手动平仓"
        )
    return ""


def _aggregate_card(children, kind, meta):
    """把若干子周期复盘卡（日卡或周卡）聚合成一张周卡/月卡。"""
    children = [c for c in children if c]
    if not children:
        return None
    scores = [c.get("score") or 0 for c in children]
    avg = round(sum(scores) / len(scores), 1)
    grade = "A" if avg >= 90 else ("B" if avg >= 75 else ("C" if avg >= 60 else "D"))
    trades_opened = sum(c.get("trades_opened", 0) for c in children)
    signal_trades = sum(c.get("signal_trades", 0) for c in children)
    impulse_trades = sum(c.get("impulse_trades", c.get("manual_trades", 0)) for c in children)
    external_trades = sum(c.get("external_trades", 0) for c in children)
    lock_violations = sum(c.get("lock_violations", 0) for c in children)
    warning_entries = sum(c.get("warning_entries", 0) for c in children)
    period_pnl = round(sum((c.get("period_pnl") or 0) for c in children), 2)
    adops = [c.get("adoption_rate") for c in children if c.get("adoption_rate") is not None]
    adoption = round(sum(adops) / len(adops), 1) if adops else 0.0
    closed = sum(c.get("closed_count", 0) for c in children)
    wins = sum(round((c.get("win_rate") or 0) / 100 * c.get("closed_count", 0)) for c in children)
    win_rate = round(wins / closed * 100, 1) if closed else 0.0
    stopped_out = sum(c.get("stopped_out", 0) for c in children)
    closed_loss_manual = sum(c.get("closed_loss_manual", 0) for c in children)
    max_single = round(max((c.get("max_single_leg_pct", 0) for c in children), default=0), 2)
    port = round(max((c.get("portfolio_pct", 0) for c in children), default=0), 2)
    # 检查项：以首张子卡的检查项结构为准，逐 key 聚合
    checks = []
    first = children[0]
    for ch in first.get("checks", []):
        key = ch["key"]
        name = ch["name"]
        total = sum(1 for c in children if any(x["key"] == key for x in c.get("checks", [])))
        ok = total > 0 and all(
            any(x["key"] == key and x["pass"] for x in c.get("checks", []))
            for c in children
            if any(x["key"] == key for x in c.get("checks", []))
        )
        checks.append({"key": key, "name": name, "pass": ok, "detail": _agg_detail(key, children)})
    # 操作：展平全部子周期操作（保留各自 time/date）
    ops = []
    for c in children:
        for o in c.get("operations") or []:
            ops.append(o)
    ops.sort(key=lambda o: o.get("time", "") or "")
    # 要点：汇总去重（保留顺序，最多 12 条）
    notes = []
    for c in children:
        for n in c.get("notes") or []:
            if n not in notes:
                notes.append(n)
    notes = notes[:12]
    rec = dict(meta)
    rec.update(
        {
            "kind": kind,
            "score": avg,
            "grade": grade,
            "trades_opened": trades_opened,
            "signal_trades": signal_trades,
            "impulse_trades": impulse_trades,
            "external_trades": external_trades,
            "lock_violations": lock_violations,
            "warning_entries": warning_entries,
            "adoption_rate": adoption,
            "period_pnl": period_pnl,
            "closed_count": closed,
            "win_rate": win_rate,
            "stopped_out": stopped_out,
            "closed_loss_manual": closed_loss_manual,
            "max_single_leg_pct": max_single,
            "single_leg_cap": first.get("single_leg_cap", 30),
            "portfolio_pct": port,
            "portfolio_cap": first.get("portfolio_cap", 60),
            "checks": checks,
            "operations": ops,
            "ops_count": len(ops),
            "children_scores": [
                {
                    "label": c.get("period_text") or c.get("date") or c.get("friday") or "",
                    "score": c.get("score"),
                    "grade": c.get("grade"),
                }
                for c in children
            ],
            "children_count": len(children),
        }
    )
    return rec


def _friday_of(date):
    """返回包含 date 的那一周的周五日期。"""
    d = date.date() if isinstance(date, datetime) else date
    return d + timedelta(days=(4 - d.weekday()))


def _is_last_trading_day(d):
    """d 是否为当月最后一个交易日（近似：月末最后一天，若落周末则取前一个周五）。"""
    if d.month == 12:
        nxt = datetime(d.year + 1, 1, 1)
    else:
        nxt = datetime(d.year, d.month + 1, 1)
    last = (nxt - timedelta(days=1)).date()
    wd = last.weekday()
    ltd = last - timedelta(days=(wd - 4)) if wd >= 5 else last
    return d == ltd


def snapshot_week(friday_str):
    """把某周五所在交易周（周一~周五）聚合成一张周复盘卡并冻结。
    会先确保该周 5 个交易日都有日卡（无操作的日子也会生成满分日卡，
    以保证「每周 5 张日卡」）。"""
    try:
        fri = datetime.strptime(friday_str, "%Y-%m-%d").date()
    except Exception:
        fri = _friday_of(datetime.now()).date()
    mon = fri - timedelta(days=4)
    weekdays = [(mon + timedelta(days=i)) for i in range(5)]
    # 先补齐 5 个交易日卡（含无操作日），但未来的交易日暂不补（待届时再落账）
    for wd in weekdays:
        ds = wd.strftime("%Y-%m-%d")
        if wd > datetime.now().date():
            continue
        if get_record(ds) is None:
            try:
                snapshot_day(ds)
            except Exception as e:
                print(f"[周复盘] 补日卡 {ds} 失败: {repr(e)[:60]}")
    day_recs = [get_record(wd.strftime("%Y-%m-%d")) for wd in weekdays]
    day_recs = [r for r in day_recs if r]
    if not day_recs:
        return None
    meta = {
        "friday": friday_str,
        "week_range": f"{weekdays[0].strftime('%Y-%m-%d')} ~ {weekdays[4].strftime('%Y-%m-%d')}",
        "period_text": f"{weekdays[0].strftime('%m-%d')} ~ {weekdays[4].strftime('%m-%d')} 周",
        "period_start": weekdays[0].strftime("%Y-%m-%d"),
    }
    rec = _aggregate_card(day_recs, "weekly", meta)
    rec["snapshot_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _load(WEEKLY_RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    records[friday_str] = rec
    try:
        json.dump(records, open(WEEKLY_RECORDS_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[周复盘] 写文件失败: {repr(e)[:80]}")
    return rec


def snapshot_month(month_str):
    """把某月的若干周复盘卡聚合成一张月复盘卡并冻结。
    month_str 形如 'YYYY-MM'。会先确保该月每个周五都有周卡。"""
    try:
        y, m = map(int, month_str.split("-"))
    except Exception:
        now = datetime.now()
        y, m = now.year, now.month
        month_str = f"{y}-{m:02d}"
    # 收集该月所有周五
    if m == 12:
        nxt = datetime(y + 1, 1, 1)
    else:
        nxt = datetime(y, m + 1, 1)
    days_in_month = (nxt - timedelta(days=1)).day
    fridays = []
    for d in range(1, days_in_month + 1):
        dt = datetime(y, m, d).date()
        if dt.weekday() == 4:
            fridays.append(dt.strftime("%Y-%m-%d"))
    # 先补齐各周卡
    for fr in fridays:
        recs = _load(WEEKLY_RECORDS_FILE, {})
        if not isinstance(recs, dict):
            recs = {}
        if fr not in recs:
            try:
                snapshot_week(fr)
            except Exception as e:
                print(f"[月复盘] 补周卡 {fr} 失败: {repr(e)[:60]}")
    week_recs = []
    for fr in fridays:
        r = _load(WEEKLY_RECORDS_FILE, {}).get(fr)
        if r:
            week_recs.append(r)
    if not week_recs:
        return None
    meta = {
        "month": month_str,
        "month_range": f"{month_str}-01 ~ {month_str}-{days_in_month:02d}",
        "period_text": f"{y}年{m}月",
        "period_start": f"{month_str}-01",
    }
    rec = _aggregate_card(week_recs, "monthly", meta)
    rec["snapshot_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _load(MONTHLY_RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    records[month_str] = rec
    try:
        json.dump(records, open(MONTHLY_RECORDS_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[月复盘] 写文件失败: {repr(e)[:80]}")
    return rec


def get_weekly_record(friday_str):
    records = _load(WEEKLY_RECORDS_FILE, {})
    return records.get(friday_str) if isinstance(records, dict) else None


def get_monthly_record(month_str):
    records = _load(MONTHLY_RECORDS_FILE, {})
    return records.get(month_str) if isinstance(records, dict) else None


def list_weekly_records():
    records = _load(WEEKLY_RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    out = []
    for fr, rec in records.items():
        if not _after_cutoff(fr):
            continue
        out.append(
            {
                "friday": fr,
                "week_range": rec.get("week_range"),
                "score": rec.get("score"),
                "grade": rec.get("grade"),
                "trades_opened": rec.get("trades_opened"),
                "impulse_trades": rec.get("impulse_trades", rec.get("manual_trades", 0)),
                "lock_violations": rec.get("lock_violations"),
                "period_pnl": rec.get("period_pnl"),
                "ops_count": rec.get("ops_count"),
                "children_count": rec.get("children_count"),
                "snapshot_at": rec.get("snapshot_at"),
            }
        )
    out.sort(key=lambda x: x["friday"], reverse=True)
    return out


def list_monthly_records():
    records = _load(MONTHLY_RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}
    out = []
    for mo, rec in records.items():
        if not _after_cutoff(mo + "-01"):
            continue
        out.append(
            {
                "month": mo,
                "month_range": rec.get("month_range"),
                "score": rec.get("score"),
                "grade": rec.get("grade"),
                "trades_opened": rec.get("trades_opened"),
                "impulse_trades": rec.get("impulse_trades", rec.get("manual_trades", 0)),
                "lock_violations": rec.get("lock_violations"),
                "period_pnl": rec.get("period_pnl"),
                "ops_count": rec.get("ops_count"),
                "children_count": rec.get("children_count"),
                "snapshot_at": rec.get("snapshot_at"),
            }
        )
    out.sort(key=lambda x: x["month"], reverse=True)
    return out


def pending_weekly(now=None):
    """待生成的周复盘：最近 10 天内、周五、且 15:10 已过、records 尚无记录的。"""
    now = now or datetime.now()
    recs = _load(WEEKLY_RECORDS_FILE, {})
    if not isinstance(recs, dict):
        recs = {}
    pending = []
    for i in range(0, 11):
        d = (now - timedelta(days=i)).date()
        if d.weekday() != 4:  # 仅周五
            continue
        snap_at = datetime(d.year, d.month, d.day, 15, 10)
        if snap_at <= now and d.strftime("%Y-%m-%d") not in recs:
            pending.append(d.strftime("%Y-%m-%d"))
    return pending


def pending_monthly(now=None):
    """待生成的月复盘：最近 40 天内、最后交易日、且 15:10 已过、records 尚无记录的。"""
    now = now or datetime.now()
    recs = _load(MONTHLY_RECORDS_FILE, {})
    if not isinstance(recs, dict):
        recs = {}
    pending = []
    seen = set()
    for i in range(0, 41):
        d = (now - timedelta(days=i)).date()
        if not _is_last_trading_day(d):
            continue
        snap_at = datetime(d.year, d.month, d.day, 15, 10)
        if snap_at <= now:
            m = f"{d.year}-{d.month:02d}"
            if m not in recs and m not in seen:
                seen.add(m)
                pending.append(m)
    return pending


def run_weekly_snapshots(now=None, verbose=True):
    done = []
    for fr in pending_weekly(now):
        try:
            snapshot_week(fr)
            done.append(fr)
            if verbose:
                print(f"[周复盘] 已落账 {fr}")
        except Exception as e:
            print(f"[周复盘] {fr} 失败: {repr(e)[:80]}")
    return done


def run_monthly_snapshots(now=None, verbose=True):
    done = []
    for mo in pending_monthly(now):
        try:
            snapshot_month(mo)
            done.append(mo)
            if verbose:
                print(f"[月复盘] 已落账 {mo}")
        except Exception as e:
            print(f"[月复盘] {mo} 失败: {repr(e)[:80]}")
    return done


# ---------------------------------------------------------------------------
# CLI：独立跑看三卡
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cards = get_all()
    for k in ("daily", "weekly", "monthly"):
        c = cards[k]
        print("=" * 60)
        print(f"【管住手 · {c['label']}复盘卡】 {c['period_text']}")
        print(f"  纪律评分 {c['score']} 分（{c['grade']}级）")
        print(
            f"  开仓 {c['trades_opened']} 笔 | 跟信号 {c['signal_trades']} | 冲动 {c['impulse_trades']} | 手动记录 {c['manual_records']} "
            f"| 锁违 {c['lock_violations']} | WARNING开仓 {c['warning_entries']}"
        )
        print(f"  信号采纳率 {c['adoption_rate']:.0f}%（周期信号 {c['signals_in_period']}）")
        print(
            f"  单品 {c['max_single_leg_pct']}%/{c['single_leg_cap']}% · "
            f"组合 {c['portfolio_pct']}%/{c['portfolio_cap']}% · 区间盈亏 {c['period_pnl']:+.0f}"
        )
        print(
            f"  区间平仓 {c['closed_count']} 笔 · 胜率 {c['win_rate']:.0f}% · "
            f"止损离场 {c['stopped_out']} · 手动砍亏 {c['closed_loss_manual']}"
        )
        print("  纪律检查：")
        for ch in c["checks"]:
            flag = "✓" if ch["pass"] else "✗"
            print(f"    [{flag}] {ch['key']} {ch['name']}：{ch['detail']}")
        print("  复盘要点：")
        for n in c["notes"]:
            print(f"    - {n}")
    print("\n*非投资建议，纪律复盘仅供自我约束参考。*")
