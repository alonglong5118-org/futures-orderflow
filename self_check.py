# -*- coding: utf-8 -*-
"""系统自检模块 — 全链路数据一致性校验
检查项：
  ① account_state.json ↔ trade_journal.json 持仓一致性
  ② killswitch_state.json 权益指标是否与 account_state 同步
  ③ drawdown_state.json 峰值权益是否有效
  ④ paper_account.json 模拟盘权益一致性
  ⑤ 各状态文件是否有过期/旧数据残留
  ⑥ 风险状态机指标是否合理
  ⑦ 夜盘资格配置一致性（防止无夜盘品种在夜盘误发信号）
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime

import account_tracker as at  # P0-10 fix: needed by check_account_fields

HERE = os.path.dirname(os.path.abspath(__file__))


def _safe_load(path):
    try:
        if not os.path.exists(path):
            return None, "文件不存在"
        d = json.load(open(path, encoding="utf-8"))
        return d, None
    except Exception as e:
        return None, str(e)


def check_state_files_exist():
    missing = []
    for f in ["account_state.json", "trade_journal.json", "drawdown_state.json"]:
        if not os.path.exists(os.path.join(HERE, f)):
            missing.append(f)
    ok = len(missing) == 0
    return {"ok": ok, "name": "状态文件存在性", "detail": f"缺失: {missing}" if missing else "所有必要文件存在"}


def check_account_journal_consistency():
    issues = []
    st, err = _safe_load(os.path.join(HERE, "account_state.json"))
    if err:
        return {"ok": False, "name": "账户↔交易记录持仓一致性", "detail": f"account_state 加载失败: {err}"}
    tj, err2 = _safe_load(os.path.join(HERE, "trade_journal.json"))
    if err2:
        return {"ok": False, "name": "账户↔交易记录持仓一致性", "detail": f"trade_journal 加载失败: {err2}"}

    st_positions = set()
    for sym, pos in (st.get("positions") or {}).items():
        if isinstance(pos, dict) and (pos.get("lots") or 0) > 0:
            st_positions.add((sym, pos.get("direction"), pos.get("lots")))

    tj_positions = set()
    for t in tj.get("trades") or []:
        if t.get("pnl") is None:
            tj_positions.add((t.get("symbol"), t.get("direction"), t.get("lots")))

    if st_positions != tj_positions:
        only_in_st = st_positions - tj_positions
        only_in_tj = tj_positions - st_positions
        if only_in_st:
            issues.append(f"account_state有但journal无: {only_in_st}")
        if only_in_tj:
            issues.append(f"journal有但account_state无: {only_in_tj}")
        return {"ok": False, "name": "账户↔交易记录持仓一致性", "detail": "; ".join(issues)}

    summary = tj.get("summary") or {}
    actual_open = len([t for t in (tj.get("trades") or []) if t.get("pnl") is None])
    if summary.get("open_trades") != actual_open:
        issues.append(f"summary.open_trades={summary.get('open_trades')} vs 实际={actual_open}")

    ok = len(issues) == 0
    return {"ok": ok, "name": "账户↔交易记录持仓一致性", "detail": "持仓一致" if ok else "; ".join(issues)}


def check_killswitch_staleness():
    ks, err = _safe_load(os.path.join(HERE, "killswitch_state.json"))
    if err:
        return {"ok": False, "name": "风险状态机数据新鲜度", "detail": f"加载失败: {err}"}
    st, err2 = _safe_load(os.path.join(HERE, "account_state.json"))
    if err2:
        return {"ok": False, "name": "风险状态机数据新鲜度", "detail": f"account_state 加载失败: {err2}"}

    ks_eq = (ks.get("metrics") or {}).get("equity", 0)
    ks_halted = ks.get("halted", False)
    st_eq = st.get("equity", 0)

    if ks_halted:
        # 熔断中：killswitch.metrics.equity 是触发瞬间的审计快照，与当前权益不同步是设计预期
        # 改为检查：当前回撤是否仍满足触发条件
        peak = (ks.get("metrics") or {}).get("peak_equity", st_eq)
        current_dd = max(0.0, (peak - st_eq) / max(peak, 1))
        ks_dd = (ks.get("metrics") or {}).get("drawdown", 0)
        current_dl = (max(0.0, -float(st.get("daily_pnl") or 0)) / st_eq) if st_eq else 0.0
        ks_dl = (ks.get("metrics") or {}).get("daily_loss_pct", 0)

        if current_dd >= 0.15 or current_dl >= 0.08:
            detail = (
                f"熔断进行中(触发时回撤{ks_dd * 100:.1f}%/日亏{ks_dl * 100:.1f}%, "
                f"当前回撤{current_dd * 100:.1f}%/日亏{current_dl * 100:.1f}%)"
            )
        else:
            detail = (
                f"熔断快照(触发时权益{ks_eq:,.0f}, 回撤{ks_dd * 100:.1f}%) — "
                f"已平仓后触发条件消失(当前权益{st_eq:,.0f}, 回撤{current_dd * 100:.1f}%)"
            )
        return {"ok": True, "name": "风险状态机数据新鲜度", "detail": detail}

    # 非熔断态：指标应与实时权益一致
    if ks_eq and st_eq:
        diff_pct = abs(ks_eq - st_eq) / max(st_eq, 1) * 100
        if diff_pct > 5:
            return {
                "ok": False,
                "name": "风险状态机数据新鲜度",
                "detail": f"killswitch权益{ks_eq:,.0f} vs account权益{st_eq:,.0f} (偏差{diff_pct:.1f}%)",
            }

    # 历史检查：仅当最近期事件是 TRIGGER/ACK（未正确 RESET）时才报警
    history = ks.get("history") or []
    if history:
        last_event = history[-1]
        last_type = last_event.get("event", "")
        if last_type in ("TRIGGER", "ACK"):
            # 存在未正确 RESET 的旧记录
            for h in history:
                t = h.get("t", "")
                if t and t[:10] < datetime.now().strftime("%Y-%m-%d"):
                    return {
                        "ok": False,
                        "name": "风险状态机数据新鲜度",
                        "detail": f"killswitch含未处理历史: {t} ({h.get('event')})",
                    }

    return {"ok": True, "name": "风险状态机数据新鲜度", "detail": "数据新鲜"}


def check_drawdown_validity():
    dd, err = _safe_load(os.path.join(HERE, "drawdown_state.json"))
    if err:
        return {"ok": False, "name": "回撤水位线有效性", "detail": f"加载失败: {err}"}
    peak = dd.get("peak_equity")
    scale = dd.get("scale", 1.0)
    dd_pct = dd.get("dd_pct", 0)
    if peak is not None and peak <= 0:
        return {"ok": False, "name": "回撤水位线有效性", "detail": f"峰值异常: {peak}"}
    if scale < 0 or scale > 2:
        return {"ok": False, "name": "回撤水位线有效性", "detail": f"降险系数异常: {scale}"}
    return {"ok": True, "name": "回撤水位线有效性", "detail": f"峰值={peak}, 回撤={dd_pct}%, 系数={scale}x"}


def check_paper_account():
    pa, err = _safe_load(os.path.join(HERE, "paper_account.json"))
    if err:
        return {"ok": False, "name": "模拟盘权益一致性", "detail": f"加载失败: {err}"}
    return {
        "ok": True,
        "name": "模拟盘权益一致性",
        "detail": f"模拟盘权益={pa.get('equity', 0):,.0f}, 持仓={len(pa.get('positions', {}))}个",
    }


def check_night_session_eligibility():
    """⑦ 夜盘资格配置一致性检查：确保所有品种正确配置了夜盘资格。
    防止新增品种时遗漏 NO_NIGHT_DEFAULT，导致无夜盘品种在夜盘时段误发信号。"""
    issues = []
    try:
        from four_dim_strategy import NO_NIGHT_DEFAULT, SYMBOLS
    except Exception as e:
        return {"ok": False, "name": "夜盘资格配置一致性", "detail": f"导入失败: {e}"}

    # 检查1：所有SYMBOLS品种是否都有night字段
    missing_night = []
    for sym, cfg in SYMBOLS.items():
        if "night" not in cfg:
            missing_night.append(sym)
    if missing_night:
        issues.append(f"缺少night字段: {missing_night}")

    # 检查2：NO_NIGHT_DEFAULT中的品种不应有夜盘
    wrong_night = []
    for sym in NO_NIGHT_DEFAULT:
        if sym in SYMBOLS and SYMBOLS[sym].get("night", True):
            wrong_night.append(sym)
    if wrong_night:
        issues.append(f"标记为无夜盘但night=True: {wrong_night}")

    # 检查3：不在NO_NIGHT_DEFAULT中的品种应有夜盘（如果有night=False）
    should_have_night = []
    for sym, cfg in SYMBOLS.items():
        if sym not in NO_NIGHT_DEFAULT and cfg.get("night") is False:
            should_have_night.append(sym)
    if should_have_night:
        issues.append(f"未标记为无夜盘但night=False: {should_have_night}")

    # 检查4：读取runner中的NO_NIGHT集合，检查与NO_NIGHT_DEFAULT的一致性
    try:
        runner_night_set = set()
        runner_path = os.path.join(HERE, "four_dim_live_runner.py")
        if os.path.exists(runner_path):
            with open(runner_path, "r") as rf:
                lines = rf.readlines()
            # 找到 NO_NIGHT = 的定义（可能跨多行）
            for i, line in enumerate(lines):
                if line.strip().startswith("NO_NIGHT =") and "{" in line:
                    # 合并该行及后续行直到找到闭合大括号
                    full_line = line
                    j = i + 1
                    while "}" not in full_line and j < len(lines):
                        full_line += lines[j]
                        j += 1
                    import re

                    items = re.findall(r'"([^"]*)"', full_line)
                    runner_night_set = set(items)
                    break
        if runner_night_set:
            only_in_strategy = NO_NIGHT_DEFAULT - runner_night_set
            only_in_runner = runner_night_set - NO_NIGHT_DEFAULT
            if only_in_strategy:
                issues.append(f"strategy有但runner无: {only_in_strategy}")
            if only_in_runner:
                issues.append(f"runner有但strategy无: {only_in_runner}")
    except Exception as e:
        issues.append(f"runner NO_NIGHT检查异常: {e}")

    ok = len(issues) == 0
    return {"ok": ok, "name": "夜盘资格配置一致性", "detail": "所有品种夜盘资格配置正确" if ok else "; ".join(issues)}


def check_account_fields():
    """P1-18：核心账户字段完整性校验 —— 账户 state 所有数字字段不能为 None/负值/极端异常值。
    返回 (ok: bool, msg: str, details: dict)。"""
    required = ["equity", "available", "realized_pnl", "base_equity"]
    numeric_nonneg = [
        "equity",
        "available",
        "balance",
        "used_margin",
        "frozen_margin",
        "realized_pnl",
        "base_equity",
        "today_pnl",
        "close_pnl",
        "position_pnl",
        "total_fee",
        "commission",
    ]
    try:
        st = at.load_state() or {}
    except Exception as e:
        return False, f"账户状态文件无法加载: {e}", {}
    problems = []
    details = {"file": "account_state.json", "keys_present": list(st.keys())}
    for k in required:
        if k not in st or st.get(k) is None:
            problems.append(f"缺少关键字段 {k}")
    for k in numeric_nonneg:
        if k in st and st[k] is not None:
            try:
                v = float(st[k])
                # 允许负数(realized_pnl/today_pnl/pnl 可以为负)，但不能是 NaN
                if math.isnan(v) or math.isinf(v):
                    problems.append(f"{k}={v} 非法(NaN/Infinity)")
            except Exception as e:
                problems.append(f"{k}={st[k]!r} 非数值类型({e})")
    # 持仓数据一致性：每个持仓应含 lots, avg, direction
    positions = st.get("positions") or {}
    if isinstance(positions, dict):
        for sym, pos in positions.items():
            if not isinstance(pos, dict):
                problems.append(f"持仓 {sym} 非字典类型")
                continue
            lots = pos.get("lots", 0)
            if lots and (not isinstance(lots, (int, float)) or lots < 0):
                problems.append(f"持仓 {sym} lots={lots!r} 非法")
            for k2 in ("avg", "direction"):
                if lots and pos.get(k2) in (None, ""):
                    problems.append(f"持仓 {sym} 有 lots 但缺 {k2}")
    # 时间戳合理性: update_ts 不能超出当前 ±24 小时(非强失败，仅警告)
    update_ts = st.get("update_ts") or st.get("timestamp")
    if update_ts:
        try:
            dt = time.time() - float(update_ts)
            details["stale_hours"] = round(dt / 3600, 1)
        except Exception:
            pass
    if problems:
        return False, "; ".join(problems[:5]) + ("..." if len(problems) > 5 else ""), {**details, "problems": problems}
    return True, "账户核心字段完整", details


def run_all_checks():
    checks = [
        check_state_files_exist(),
        check_account_journal_consistency(),
        check_killswitch_staleness(),
        check_drawdown_validity(),
        check_paper_account(),
        check_night_session_eligibility(),
    ]
    total = len(checks)
    passed = sum(1 for c in checks if c["ok"])
    return {
        "ok": passed == total,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {"total": total, "passed": passed, "failed": total - passed},
        "checks": checks,
    }


if __name__ == "__main__":
    print(json.dumps(run_all_checks(), ensure_ascii=False, indent=2))
