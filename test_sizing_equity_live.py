# -*- coding: utf-8 -*-
"""自检：仓位 sizing 跟随账户权益 SSOT（2026-11-09 A 接线版）。

新契约（升级落地包 A·P0）：
  - _sync_live_equity 委托 account_ssot.resolve，唯一真源，无静态兜底
  - 取不到可信权益 → (0.0, "untrusted:<source>")，调用方拒绝新增开仓信号
  - 可运行：任意能 import four_dim_live_runner 的 python
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("runner_mod", os.path.join(HERE, "four_dim_live_runner.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
import account_ssot

PASS = FAIL = 0


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


orig_resolve = R.account_ssot.resolve
state = {}
try:
    # 1) paper 模式可信 → 权益跟随，detail 暴露，无 blocked
    R.account_ssot.resolve = lambda **kw: account_ssot.Reading(
        995190.0, "paper_ledger", True, "账本 cash + 3 腿浮动", 60.0, (), ())
    eq, src = R._sync_live_equity(state=state)
    chk("paper 权益跟随", eq == 995190.0 and src == "paper_ledger", f"{eq}/{src}")
    chk("_STRAT_CFG.equity 同步", R._STRAT_CFG["account"]["equity"] == 995190.0,
        R._STRAT_CFG["account"]["equity"])
    chk("sizing_equity_detail 暴露", "sizing_equity_detail" in state, list(state.keys()))
    chk("无 sizing_blocked", "sizing_blocked" not in state, state.get("sizing_blocked"))

    # 2) 不可信 → (0.0, untrusted:*)，blocked 暴露给面板
    R.account_ssot.resolve = lambda **kw: account_ssot.Reading(
        None, "paper_ledger", False, "账本不可读")
    eq, src = R._sync_live_equity(state=state)
    chk("untrusted 返回 0 权益", eq == 0.0, eq)
    chk("source=untrusted:*", src.startswith("untrusted:"), src)
    blk = state.get("sizing_blocked") or {}
    chk("sizing_blocked 暴露", blk.get("reason") == "账本不可读" and blk.get("source") == "paper_ledger", blk)

    # 3) resolve 抛异常 → 降级为 untrusted:error（不崩主循环）
    def boom(**kw):
        raise RuntimeError("模拟炸了")
    R.account_ssot.resolve = boom
    eq, src = R._sync_live_equity(state=state)
    chk("异常 → untrusted:error", eq == 0.0 and src == "untrusted:error", f"{eq}/{src}")

    # 4) 不传 state 也不崩（兼容旧调用形态）
    R.account_ssot.resolve = lambda **kw: account_ssot.Reading(
        995190.0, "paper_ledger", True, "ok", None, (), ())
    eq, src = R._sync_live_equity()
    chk("无 state 兼容", eq == 995190.0 and src == "paper_ledger", f"{eq}/{src}")

    # 5) 手数影响实测：真实权益 vs 旧静态锚（因 max_lots=2 硬顶）
    from four_dim_strategy import risk_gate
    rows = []
    for sym, atr, px in (("ru", 210.0, 20000.0), ("zn", 180.0, 23000.0),
                         ("cs", 25.0, 2685.0), ("jd", 45.0, 4045.0), ("cu", 300.0, 78000.0)):
        R._STRAT_CFG["account"]["equity"] = R._SIZING_EQUITY_STATIC
        a = risk_gate(sym, px, atr, R._STRAT_CFG)
        R._STRAT_CFG["account"]["equity"] = 995190.0
        b = risk_gate(sym, px, atr, R._STRAT_CFG)
        rows.append((sym, a.get("N_plan"), b.get("N_plan")))
    print(f"  [手数影响 静态→SSOT] {rows}")
    chk("SSOT 权益下所有品种 N_plan ≤2（铁律单合约≤2）",
        all((r[2] or 0) <= 2 for r in rows), rows)
    chk("SSOT 权益下 N_plan ≥ 静态 N_plan（不应缩仓）",
        all((r[2] or 0) >= (r[1] or 0) for r in rows), rows)

finally:
    R.account_ssot.resolve = orig_resolve
    R._STRAT_CFG["account"]["equity"] = R._SIZING_EQUITY_STATIC

print(f"\n结果：{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
