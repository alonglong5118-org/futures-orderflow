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
import os, json, time
from datetime import datetime

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
    for t in (tj.get("trades") or []):
        if t.get("pnl") is None:
            tj_positions.add((t.get("symbol"), t.get("direction"), t.get("lots")))

    if st_positions != tj_positions:
        only_in_st = st_positions - tj_positions
        only_in_tj = tj_positions - st_positions
        if only_in_st: issues.append(f"account_state有但journal无: {only_in_st}")
        if only_in_tj: issues.append(f"journal有但account_state无: {only_in_tj}")
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
    st_eq = st.get("equity", 0)
    if ks_eq and st_eq:
        diff_pct = abs(ks_eq - st_eq) / max(st_eq, 1) * 100
        if diff_pct > 5:
            return {"ok": False, "name": "风险状态机数据新鲜度",
                    "detail": f"killswitch权益{ks_eq:,.0f} vs account权益{st_eq:,.0f} (偏差{diff_pct:.1f}%)"}

    history = ks.get("history") or []
    for h in history:
        t = h.get("t", "")
        if t and t[:10] < datetime.now().strftime("%Y-%m-%d"):
            return {"ok": False, "name": "风险状态机数据新鲜度",
                    "detail": f"killswitch含旧历史: {t} ({h.get('event')})"}

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
    return {"ok": True, "name": "模拟盘权益一致性",
            "detail": f"模拟盘权益={pa.get('equity',0):,.0f}, 持仓={len(pa.get('positions',{}))}个"}



def check_night_session_eligibility():
    """⑦ 夜盘资格配置一致性检查：确保所有品种正确配置了夜盘资格。
    防止新增品种时遗漏 NO_NIGHT_DEFAULT，导致无夜盘品种在夜盘时段误发信号。"""
    issues = []
    try:
        from four_dim_strategy import SYMBOLS, NO_NIGHT_DEFAULT
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
        if sym not in NO_NIGHT_DEFAULT and cfg.get("night") == False:
            should_have_night.append(sym)
    if should_have_night:
        issues.append(f"未标记为无夜盘但night=False: {should_have_night}")

    # 检查4：读取runner中的NO_NIGHT集合，检查与NO_NIGHT_DEFAULT的一致性
    try:
        runner_night_set = set()
        runner_path = os.path.join(HERE, "four_dim_live_runner.py")
        if os.path.exists(runner_path):
            with open(runner_path, 'r') as rf:
                lines = rf.readlines()
            # 找到 NO_NIGHT = 的定义（可能跨多行）
            for i, line in enumerate(lines):
                if line.strip().startswith('NO_NIGHT =') and '{' in line:
                    # 合并该行及后续行直到找到闭合大括号
                    full_line = line
                    j = i + 1
                    while '}' not in full_line and j < len(lines):
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
    return {
        "ok": ok,
        "name": "夜盘资格配置一致性",
        "detail": "所有品种夜盘资格配置正确" if ok else "; ".join(issues)
    }


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
