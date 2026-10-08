"""
equity_history.py — 日终权益归档（P0）
================================================
背景：trade_journal.sample_equity() 每分钟把动态权益写进 intraday_equity.json，
      但该文件只保留最近 7 天（滚动删除，见 trade_journal.py:1420-1425），
      历史权益留不下来，无法做跨月/跨季的权益曲线与回撤复盘。

本模块：每天日盘收盘（15:00）后，把当日日内曲线压缩成一条「日终快照」永久归档：
      开盘权益 / 收盘权益 / 日内最高 / 日内最低 / 当日盈亏 / 日内最大回撤 / 采样点数，
      并旁挂 drawdown_guard 的回撤档位（若可用）。

设计原则：
  · 只读 intraday_equity.json，绝不改动它；归档写入独立文件 equity_history.json。
  · 幂等：同一天重复调用只归档一次（除非 force=True）。
  · 可补归档：某日已过 15:00 但当时未归档（如 runner 没跑），下次调用自动补齐。
  · 纯只读 + 独立文件，不触碰任何交易数据、不下单、不写交易接口。

接入 runner 主循环（four_dim_live_runner.py 约 14545 行 tj.sample_equity(prices) 之后）：
    try:
        import equity_history as eh
        eh.maybe_snapshot()
    except Exception:
        pass

命令行：
  python equity_history.py --check                 # 只报告「哪些日期待归档」，不写文件
  python equity_history.py --snapshot              # 归档所有可归档日期（幂等）
  python equity_history.py --snapshot --force      # 强制重算今天
  python equity_history.py --snapshot --date 2026-09-11
  python equity_history.py --show --days 30        # 查看最近 30 天归档
  python equity_history.py --audit                 # 每日 journal 前置自检（打印告警）
  python equity_history.py --audit --gate          # 前置自检门：命中可疑盈亏则 exit 1
  python equity_history.py --backfill              # 从 trade_journal 反推逐日权益（只读预演）
  python equity_history.py --backfill --apply      # 真正回填（幂等，冲突保留真实归档）
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HIST_FILE = os.path.join(HERE, "equity_history.json")

# 日盘收盘时刻（期货日盘 15:00 收盘）——归档判定基准
CLOSE_HOUR = 15
# 归档保留上限（0 = 永久保留）
MAX_DAYS = 0

_LOCK = threading.RLock()

# 归档文件说明（每次保存都刷成最新，保证说明与字段同步）
_NOTE = (
    "日终权益归档。每条 = 某个自然日的日内曲线汇总（15:00 日盘收盘后归档）。"
    "数据源：intraday_equity.json；本文件为永久留存，不参与 7 天滚动删除。"
    "equity_base = 动态权益的基准来源（dynamic_equity = anchor + Δ已实现 + Δ浮动）；"
    "journal_suspicious = 扫描到的物理不可能盈亏（只读上报，不改交易数据）。"
    "⚠️ 2026-09-12 发现：anchor 被 journal 里一笔 al 假盈亏（+907,435）抬到 ≈99 万，"
    "真实口径应远低于此，读取本文件时务必先看 equity_base 与 journal_suspicious。"
)


# ============================================================
# 内部：读写
# ============================================================


def _intraday_file():
    """复用 trade_journal 的日内文件定位逻辑（含多账户后缀）。"""
    try:
        import trade_journal as tj

        return tj._intraday_file_for()
    except Exception:
        return os.path.join(HERE, "intraday_equity.json")


def _load_intraday():
    """读日内采样数据：{date: [{t:'HH:MM', equity, floating}, ...]}"""
    path = _intraday_file()
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f) or {}
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


# 口径断点：发生账户重置 / 本金变更时登记在此。
# 作用：跨断点直接比较权益、算连续收益率都会得出错误结论（数值可能接近但含义不同）。
_REGIME_BREAKS = [
    {
        "at": "2026-09-12 21:01:14",
        "kind": "account_reset",
        "detail": (
            "以模拟盘截图为准重置账户：本金 100,600 → 1,000,000；"
            "权益锚 993,685 → 984,415（已实现 -3,245 / 浮盈 -12,340）；"
            "journal 同步清至 8 笔并新增 _opening_adjustment，旧口径的 al 假盈亏记录随之作废。"
        ),
        "impact": (
            "该时刻（含）之前的归档条目属旧本金口径。与之后的数值虽可能接近，"
            "但含义不同——禁止跨此点做趋势比较、连续收益或回撤统计。"
        ),
    }
]


def _load_hist():
    """读归档文件；不存在或格式异常则返回空骨架。"""
    try:
        if os.path.exists(HIST_FILE):
            with open(HIST_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                d["_note"] = _NOTE  # 说明始终与代码同步
                d["_regime_breaks"] = _REGIME_BREAKS  # 口径断点始终与代码同步
                return d
    except Exception:
        pass
    return {
        "_version": "1.2",
        "_note": _NOTE,
        "_regime_breaks": _REGIME_BREAKS,
        "days": {},
    }


def _save_hist(d):
    """原子写：先写 .tmp 再 replace，避免中途崩溃写坏文件。"""
    tmp = HIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HIST_FILE)


def _session_points(points):
    """取当日日盘收盘（15:00）之前窗口的采样点。

    日内文件按自然日分键，但 15:00 之后是非交易时段（采样仍在继续、
    权益不变），且 21:00 起属于下一个期货交易日。因此「日终」口径
    只取 t <= '15:00' 的点，保证 close 是真正的日盘收盘权益。
    """
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        t = p.get("t") or ""
        if t and t <= "15:00":
            out.append(p)
    return out or points


def _clean_equities(eqs, rel_thresh=0.30):
    """剔除明显异常（尖刺/塌陷）的权益采样点。

    实测 intraday_equity.json 存在脏点（如 2026-09-08 21:55-22:04 出现
    权益中位数 143% 的尖刺），若不过滤会把日内最大回撤算到 30%+。

    以中位数为基准，偏离超过 rel_thresh 的点视为脏数据。
    返回 (清洗后列表, 剔除数, 中位数)。
    """
    if len(eqs) < 5:
        return list(eqs), 0, (eqs[0] if eqs else 0.0)
    s = sorted(eqs)
    med = s[len(s) // 2]
    if med <= 0:
        return list(eqs), 0, med
    lo, hi = med * (1 - rel_thresh), med * (1 + rel_thresh)
    clean = [v for v in eqs if lo <= v <= hi]
    if not clean:
        return list(eqs), 0, med
    return clean, len(eqs) - len(clean), med


# ============================================================
# 审计：权益基准来源 + journal 物理不可能盈亏
# ============================================================


def _anchor():
    """读 account_tracker 的「权益锚」三元组。

    dynamic_equity = equity_anchor + (journal已实现 - realized_pnl_at_sync)
                                          + (当前浮动 - float_at_sync)

    归档必须记录 anchor 来源，否则权益曲线看着漂亮却可能整条建立在
    不可靠的基准上（2026-09-12 实测踩中，见下）。
    """
    st = None
    try:
        import account_tracker as at

        st = at.load_state()
    except Exception:
        try:
            import account_tracker as _at2

            p = _at2.state_file_for()
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    st = json.load(f)
        except Exception:
            st = None
    if not isinstance(st, dict):
        return {}
    init = float(st.get("initial_balance", 0) or 0)
    anchor = float(st.get("equity", 0) or 0)
    ra = float(st.get("realized_pnl_at_sync", 0) or 0)
    fa = float(st.get("float_at_sync", 0) or 0)
    out = {
        "anchor_equity": round(anchor, 2),
        "initial_balance": round(init, 2),
        "realized_pnl_at_sync": round(ra, 2),
        "float_at_sync": round(fa, 2),
        "equity_synced_at": st.get("equity_synced") or st.get("updated"),
    }
    # anchor 是否 = 本金 + 已实现 + 浮动（即「由 journal 推导」而非券商同步）
    if init:
        derived = init + ra + fa
        out["anchor_derived_from_journal"] = abs(derived - anchor) <= max(1.0, abs(anchor) * 0.001)
        out["anchor_implied_growth_pct"] = round((anchor - init) / init * 100, 2)
    return out


def journal_sanity(max_move_ratio=0.50):
    """扫描 journal，标出「物理上不可能」的已实现盈亏。

    单笔盈亏上限 = multiplier * lots * entry_price * max_move_ratio，
    即隐含价格波动不得超过开仓价的 max_move_ratio 倍。超出即为脏数据。

    2026-09-12 实测：journal 里 al 2手 @23870（乘数5）记 pnl=907435，
    隐含平仓价 114613.5（+380%），物理不可能；而它单独撑起了
    realized_pnl_at_sync=914229 的 99.3%，把 anchor 抬到 99 万。

    返回 {"checked": n, "suspicious": [{...}]}；任何异常都返回空结果。
    """
    out = {"checked": 0, "suspicious": []}
    try:
        specs = {}
        for fn in ("trade_config.json", "contract_specs.json"):
            p = os.path.join(HERE, fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                c = json.load(f) or {}
            specs = c.get("contract_specs") or {}
            if specs:
                break
        try:
            import trade_journal as tj

            jp = tj._journal_file_for()
        except Exception:
            jp = os.path.join(HERE, "trade_journal.json")
        if not os.path.exists(jp):
            return out
        with open(jp, encoding="utf-8") as f:
            trades = (json.load(f) or {}).get("trades") or []
    except Exception:
        return out

    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("pnl")
        if not isinstance(pnl, (int, float)):
            continue
        out["checked"] += 1
        sym = str(t.get("symbol") or "").lower()
        lots = float(t.get("lots", 0) or 0)
        entry = t.get("entry_price") or t.get("entry")
        mult = float((specs.get(sym) or {}).get("multiplier", 0) or 0)
        if not (lots and entry and mult):
            continue
        cap = mult * lots * float(entry) * max_move_ratio
        if abs(float(pnl)) > cap:
            out["suspicious"].append(
                {
                    "time": t.get("time"),
                    "symbol": sym,
                    "lots": lots,
                    "entry_price": entry,
                    "multiplier": mult,
                    "pnl": pnl,
                    "max_plausible_pnl": round(cap, 2),
                    "implied_exit_price": round(float(entry) + float(pnl) / (mult * lots), 2),
                    "excess_x": round(abs(float(pnl)) / cap, 1) if cap else None,
                }
            )
    return out


# ============================================================
# 核心：归档
# ============================================================


def snapshot(date=None, force=False):
    """把某个自然日的日内曲线归档成一条日终快照。

    参数:
      - date: 'YYYY-MM-DD'；None 则取今天
      - force: True 时即使已归档也重算覆盖

    返回: 归档条目 dict；无数据可归档时返回 None。
    """
    with _LOCK:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        hist = _load_hist()
        if date in hist["days"] and not force:
            return hist["days"][date]

        raw_points = _load_intraday().get(date, []) or []
        points = _session_points(raw_points)  # 只取 15:00 收盘前
        raw_eqs = [
            float(p["equity"]) for p in points if isinstance(p, dict) and isinstance(p.get("equity"), (int, float))
        ]
        if not raw_eqs:
            return None

        eqs, outliers, median_eq = _clean_equities(raw_eqs)
        if not eqs:
            eqs = list(raw_eqs)

        open_eq = eqs[0]
        close_eq = eqs[-1]
        high = max(eqs)
        low = min(eqs)
        pnl = close_eq - open_eq
        pnl_pct = (pnl / open_eq * 100) if open_eq else 0.0

        # 日内最大回撤：从开盘起的 running peak 到其后最低
        peak = eqs[0]
        max_dd = 0.0
        for e in eqs:
            if e > peak:
                peak = e
            if peak > 0:
                dd = (peak - e) / peak
                if dd > max_dd:
                    max_dd = dd

        entry = {
            "date": date,
            "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "open_equity": round(open_eq, 2),
            "close_equity": round(close_eq, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 3),
            "intraday_max_dd_pct": round(max_dd * 100, 3),
            "range_pct": round((high - low) / open_eq * 100, 3) if open_eq else 0.0,
            "samples": len(eqs),
            "outliers_removed": outliers,
            "median_equity": round(median_eq, 2),
            "raw_high": round(max(raw_eqs), 2),
            "raw_low": round(min(raw_eqs), 2),
            "first_sample": (points[0].get("t") if isinstance(points[0], dict) else None),
            "last_sample": (points[-1].get("t") if isinstance(points[-1], dict) else None),
            "window": "00:00-15:00",
        }

        # 审计：权益基准来源（动态权益 = anchor + Δ已实现 + Δ浮动）
        base = _anchor()
        if base:
            entry["equity_base"] = base

        # 审计：journal 物理不可能盈亏（读了就报，不改任何交易数据）
        san = journal_sanity()
        if san.get("suspicious"):
            entry["journal_suspicious"] = san["suspicious"]

        # 旁挂回撤档位（drawdown_guard 可用时）。
        # ⚠️ 注意语义：drawdown_guard.current() 返回的是「调用时刻」的档位，
        # 不是「date 那一天收盘时」的档位。因此补归档的历史日会带上今天的档位。
        # 为避免误读，收进 dd_at_archive 子键并标明语义。
        try:
            import drawdown_guard as ddg

            st = ddg.current() or {}
            entry["dd_at_archive"] = {
                "dd_pct": float(st.get("dd_pct", 0) or 0),
                "tier": int(st.get("tier", 0) or 0),
                "scale": float(st.get("scale", 1.0) or 1.0),
                "note": "归档调用时刻的实时档位；历史补归档日不代表当日档位",
            }
        except Exception:
            pass

        hist["days"][date] = entry
        hist["_updated"] = entry["archived_at"]

        if MAX_DAYS and len(hist["days"]) > MAX_DAYS:
            for d in sorted(hist["days"].keys())[: len(hist["days"]) - MAX_DAYS]:
                del hist["days"][d]

        _save_hist(hist)
        return entry


def pending(now=None):
    """列出「已过 15:00 收盘、有日内数据、但尚未归档」的日期（升序）。"""
    now = now or datetime.now()
    intraday = _load_intraday()
    done = _load_hist()["days"]
    out = []
    for date in sorted(intraday.keys()):
        if date in done:
            continue
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            continue
        if now >= d.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0):
            out.append(date)
    return out


def maybe_snapshot(now=None):
    """幂等自动归档入口（供 runner 主循环调用）。

    扫描所有「已过收盘但未归档」的日期并补齐，返回本次新归档的条目列表。
    盘中调用不会产生任何写入；已归档日期不会重复写入。
    """
    done = []
    for date in pending(now):
        e = snapshot(date)
        if e:
            done.append(e)
    return done


# ============================================================
# 每日 journal 巡检（供 21:00 日报等做「前置自检」门）
# ============================================================

AUDIT_FILE = os.path.join(HERE, "journal_audit.json")


def _load_audit():
    try:
        if os.path.exists(AUDIT_FILE):
            with open(AUDIT_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            if isinstance(d, dict):
                d.setdefault("days", {})
                return d
    except Exception:
        pass
    return {"_version": "1.0", "_note": "每日 journal 物理不可能盈亏巡检记录（只读诊断，不改交易数据）", "days": {}}


def _save_audit(d):
    tmp = AUDIT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AUDIT_FILE)


def journal_audit(date=None, force=False):
    """跑一次 journal 物理校验并落盘（幂等，一天一条）。

    返回 {"date","ok","checked","suspicious","checked_at"}；
    ok=False 表示命中物理不可能盈亏，调用方（日报）应当红字告警、不得静默通过。
    """
    with _LOCK:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        d = _load_audit()
        if date in d["days"] and not force:
            return d["days"][date]
        san = journal_sanity()
        rec = {
            "date": date,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": not san.get("suspicious"),
            "checked": san.get("checked", 0),
            "suspicious": san.get("suspicious", []),
            "anchor": _anchor(),
        }
        d["days"][date] = rec
        d["_updated"] = rec["checked_at"]
        # 只留最近 180 天
        keys = sorted(d["days"].keys())
        for k in keys[:-180]:
            del d["days"][k]
        _save_audit(d)
        return rec


_AUDIT_RETRY_MIN = 10  # 未通过时的重检间隔（分钟），避免主循环每轮都写盘


def maybe_audit(now=None):
    """幂等自动巡检入口（供 runner 主循环 / 日报前置调用）。

    幂等策略：**不缓存失败**。当天已有记录且为 ok=True 才跳过；
    若当天记录是 ok=False，则按 _AUDIT_RETRY_MIN 的间隔重跑，直到问题修好、
    记录转绿为止 —— 否则用户修完 journal 后当天告警仍会挂着，误导人。
    （重检有节流：主循环每轮都调用也不会反复写盘。）
    """
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    prev = _load_audit()["days"].get(date)
    if prev is not None:
        if prev.get("ok", True):
            return None  # 已通过：当天不再重跑
        try:
            last = datetime.strptime(prev["checked_at"], "%Y-%m-%d %H:%M:%S")
            if (now - last).total_seconds() < _AUDIT_RETRY_MIN * 60:
                return None  # 未通过但还没到重检间隔
        except Exception:
            pass
    return journal_audit(date, force=True)


def audit_alerts(rec):
    """把巡检结果转成可直接贴进日报顶部的告警行（无问题返回空列表）。"""
    if not rec or rec.get("ok", True):
        return []
    out = ["🔴 【权益数据告警】journal 存在物理上不可能的盈亏，权益数字不可信，勿据此定仓位："]
    for s in rec.get("suspicious", []):
        out.append(
            "  · %s %s %s手 @%s（乘数%s）记 pnl=%s，隐含平仓价 %s（超合理上限 %s 倍）"
            % (
                s.get("time"),
                s.get("symbol"),
                s.get("lots"),
                s.get("entry_price"),
                s.get("multiplier"),
                s.get("pnl"),
                s.get("implied_exit_price"),
                s.get("excess_x"),
            )
        )
    a = rec.get("anchor") or {}
    if a:
        out.append(
            "  · 权益锚 %s（本金 %s，隐含增长 %s%%），上次同步 %s"
            % (
                a.get("anchor_equity"),
                a.get("initial_balance"),
                a.get("anchor_implied_growth_pct"),
                a.get("equity_synced_at"),
            )
        )
    return out


# ============================================================
# 查询
# ============================================================


def history(days=60, clean_only=False):
    """返回最近 days 天的归档条目（升序）。days<=0 表示全部。

    clean_only=True 时跳过 pre_reset_polluted（09-12 账户重置前旧口径）条目，
    供 σ_port 等需要「同口径干净序列」的消费方使用——否则跨口径算波动率会被污染放大。
    """
    d = _load_hist()["days"]
    keys = sorted(d.keys())
    if days and days > 0:
        keys = keys[-days:]
    rows = [d[k] for k in keys]
    if clean_only:
        rows = [r for r in rows if not r.get("pre_reset_polluted")]
    return rows


def latest():
    h = history(1)
    return h[-1] if h else None


def summary(clean_only=False):
    """汇总：归档天数、区间、累计盈亏、最大单日回撤。clean_only 语义同 history()。"""
    rows = history(0, clean_only=clean_only)
    if not rows:
        return {"days": 0}
    total = sum(r.get("pnl", 0) or 0 for r in rows)
    worst = min(rows, key=lambda r: r.get("pnl", 0) or 0)
    best = max(rows, key=lambda r: r.get("pnl", 0) or 0)
    return {
        "days": len(rows),
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "cum_pnl": round(total, 2),
        "best_day": {"date": best["date"], "pnl": best.get("pnl")},
        "worst_day": {"date": worst["date"], "pnl": worst.get("pnl")},
        "max_intraday_dd_pct": max((r.get("intraday_max_dd_pct", 0) or 0) for r in rows),
    }


# ============================================================
# 回填：从 trade_journal 已平仓成交反推逐日权益
# ============================================================


def backfill_from_journal(account_id="default", apply=False, now=None):
    """从 trade_journal 已平仓成交反推逐日权益，回填 equity_history.json 的 days。

    口径（与 trade_journal.equity_curve() 完全一致）：
      base  = 账户权益锚（account_state 的 equity）
      start = base - 全部已平仓盈亏之和（即「首笔成交前」的权益）
      再按平仓日期逐笔累计已实现盈亏，聚成日频 open/close/high/low/pnl。

    规则：
      · 只读 trade_journal（显式传 account_id，默认 default=模拟盘；绝不碰 live）。
      · 幂等：days 里已有该日期的条目视为「真实归档」，一律保留，进冲突清单。
      · apply=False 只预演（返回派生结果，不写盘）；apply=True 才把新增日写盘。
      · 回填条目打 source='journal_backfill' 标记，与真实日内归档可区分。
      · 口径断点（_REGIME_BREAKS）之前的历史日属旧本金口径，回填值仅供参考。

    返回 {"account","base_equity","closed_trades","total_closed_pnl","start_equity",
          "derived","added","conflicts"}。
    """
    now = now or datetime.now()
    # equity_history.json 非账户隔离（固定文件，锚也读 default），回填只允许 default=模拟盘，
    # 禁止把 live 等其它账户混入同一份归档。
    if account_id != "default":
        return {"error": "回填仅支持 default（模拟盘）；equity_history.json 非账户隔离，禁止混入账户 %s" % account_id}
    try:
        import account_tracker as at
        import trade_journal as tj
    except Exception as e:
        return {"error": "import 失败: %s" % e}

    # 显式切到指定账户读 journal 与锚（线程局部，退出自动恢复），确保不串到 live
    with at.account_context(account_id):
        data = tj._load()
        trades = data.get("trades") or []
        closed = sorted(
            [t for t in trades if isinstance(t, dict) and t.get("pnl") is not None],
            key=lambda t: t.get("exit_time") or t.get("time") or "",
        )
        base = tj._base_equity()

    total = sum(float(t["pnl"]) for t in closed)
    start = base - total

    def _date(ts):
        ts = ts or ""
        return ts[:10] if len(ts) >= 10 else None

    # 按平仓日期聚合，逐笔累计当日权益轨迹（算 high/low/日内回撤）
    from collections import OrderedDict

    by_day = OrderedDict()
    for t in closed:
        d = _date(t.get("exit_time") or t.get("time"))
        if not d:
            continue
        by_day.setdefault(d, []).append(float(t["pnl"]))

    prev_close = start
    derived = []
    for d, pnls in by_day.items():
        day_open = prev_close
        running = day_open
        high = running
        low = running
        peak = running
        max_dd = 0.0
        for p in pnls:
            running += p
            high = max(high, running)
            low = min(low, running)
            if running > peak:
                peak = running
            if peak > 0:
                dd = (peak - running) / peak
                max_dd = max(max_dd, dd)
        day_close = running
        pnl = day_close - day_open
        derived.append(
            {
                "date": d,
                "archived_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "open_equity": round(day_open, 2),
                "close_equity": round(day_close, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / day_open * 100, 3) if day_open else 0.0,
                "range_pct": round((high - low) / day_open * 100, 3) if day_open else 0.0,
                "intraday_max_dd_pct": round(max_dd * 100, 3),
                "samples": len(pnls),
                "source": "journal_backfill",
                "base_equity": round(base, 2),
            }
        )
        prev_close = day_close

    hist = _load_hist()
    added = []
    conflicts = []
    for e in derived:
        if e["date"] in hist["days"]:
            real = hist["days"][e["date"]]
            conflicts.append(
                {
                    "date": e["date"],
                    "real_close": real.get("close_equity"),
                    "derived_close": e["close_equity"],
                    "kept": "real",
                }
            )
        else:
            if apply:
                hist["days"][e["date"]] = e
            added.append(e)

    if apply and added:
        hist["_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
        _save_hist(hist)

    return {
        "account": account_id,
        "base_equity": round(base, 2),
        "closed_trades": len(closed),
        "total_closed_pnl": round(total, 2),
        "start_equity": round(start, 2),
        "derived": derived,
        "added": added,
        "conflicts": conflicts,
    }


# ============================================================
# CLI
# ============================================================


def _main():
    ap = argparse.ArgumentParser(description="日终权益归档")
    ap.add_argument("--check", action="store_true", help="只报告待归档日期，不写文件")
    ap.add_argument("--snapshot", action="store_true", help="执行归档（幂等）")
    ap.add_argument("--force", action="store_true", help="强制重算（配合 --date 或今天）")
    ap.add_argument("--date", type=str, default=None, help="指定日期 YYYY-MM-DD")
    ap.add_argument("--show", action="store_true", help="展示最近归档")
    ap.add_argument("--days", type=int, default=30, help="--show 的天数，默认 30")
    ap.add_argument("--audit", action="store_true", help="跑 journal 物理校验（前置自检门）")
    ap.add_argument("--gate", action="store_true", help="配合 --audit：命中则 exit 1（供日报管线拦截）")
    ap.add_argument("--backfill", action="store_true", help="从 trade_journal 反推逐日权益并回填（缺省只预演）")
    ap.add_argument("--apply", action="store_true", help="配合 --backfill：真正写盘")
    ap.add_argument("--account", type=str, default="default", help="--backfill 的账户 ID（默认 default=模拟盘）")
    args = ap.parse_args()

    if args.backfill:
        res = backfill_from_journal(account_id=args.account, apply=args.apply)
        if "error" in res:
            print("❌ %s" % res["error"])
            return 2
        print(
            "回填（账户=%s）：已平仓 %d 笔，总盈亏 %+.2f，锚 %s，起点权益 %s"
            % (res["account"], res["closed_trades"], res["total_closed_pnl"], res["base_equity"], res["start_equity"])
        )
        print(
            "派生 %d 条；新增 %d 条；冲突 %d 条（冲突以真实归档为准）"
            % (len(res["derived"]), len(res["added"]), len(res["conflicts"]))
        )
        for c in res["conflicts"]:
            print(
                "  ⚠️ 冲突 %s：真实收盘 %s vs 反推收盘 %s → 保留真实" % (c["date"], c["real_close"], c["derived_close"])
            )
        if not args.apply:
            print("【只读预演】未写任何文件。确认无误后加 --apply 回填。")
        else:
            print("✅ 已回填 %d 条。" % len(res["added"]))
        return 0

    if args.audit:
        rec = journal_audit(force=True)
        lines = audit_alerts(rec)
        print(
            "journal 巡检 %s：检查 %d 笔可结算成交，可疑 %d 笔"
            % (rec["date"], rec["checked"], len(rec.get("suspicious", [])))
        )
        if lines:
            print("\n" + "\n".join(lines))
        else:
            print("✅ 未发现物理不可能盈亏")
        if args.gate and not rec.get("ok", True):
            return 1
        return 0

    if args.check:
        p = pending()
        print("待归档日期: %s" % (", ".join(p) if p else "（无）"))
        print("已归档天数: %d" % len(_load_hist()["days"]))
        return 0

    if args.snapshot:
        if args.date:
            e = snapshot(args.date, force=args.force)
            print(json.dumps(e, ensure_ascii=False, indent=2) if e else "无数据可归档：%s" % args.date)
        else:
            done = (
                [snapshot(d, force=(args.force and d == datetime.now().strftime("%Y-%m-%d"))) for d in pending()]
                if not args.force
                else [snapshot(None, force=True)]
            )
            done = [e for e in done if e]
            if done:
                for e in done:
                    print(
                        "已归档 %s  收盘 %.2f  盈亏 %+.2f (%.3f%%)  样本 %d"
                        % (e["date"], e["close_equity"], e["pnl"], e["pnl_pct"], e["samples"])
                    )
            else:
                print("无新增归档（可能均已归档，或当日尚未收盘）")
        return 0

    if args.show:
        rows = history(args.days)
        if not rows:
            print("暂无归档记录")
            return 0
        print("%-12s %12s %12s %10s %8s %6s" % ("日期", "收盘权益", "当日盈亏", "盈亏%", "日内DD%", "样本"))
        for r in rows:
            print(
                "%-12s %12.2f %+12.2f %9.3f%% %7.3f%% %6d"
                % (
                    r["date"],
                    r["close_equity"],
                    r["pnl"],
                    r["pnl_pct"],
                    r.get("intraday_max_dd_pct", 0) or 0,
                    r["samples"],
                )
            )
        print("\n汇总: %s" % json.dumps(summary(), ensure_ascii=False))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
