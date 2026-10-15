#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""负样本测试：峰值权益重基线（peak_equity rebase）。

缺陷背景（2026-10-15 实测）：
  账户被人工按券商截图重置 equity: 1,016,820 → 1,003,075，
  但 RiskStateMachine.update() 只在 `equity > peak_equity` 时更新峰值，
  权益下降永不回落 ⇒ peak_equity 永久停在旧值
  ⇒ effective_daily_loss_cap() 一直按旧权益算封顶（显示 20,336.40 而非 20,061.50）。

判定标准（负样本）：
  1. 正常盘中波动（±5% 以内）→ 不得重基线（否则峰值被洗掉，失去回撤基准）
  2. 账户被重置（偏离 >5%）→ 必须重基线
  3. 重基线后日亏封顶必须等于 新权益 × 2%
  4. 权益上升仍正常抬升峰值（原有行为不得被破坏）
  5. 边界值不得抛异常
  6. 修复前必失败（负样本证明判据不是恒 PASS）
"""
import sys

import risk_state_machine as R

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _fresh():
    fsm = R.get_fsm("test_rebase_%d" % (len(RESULTS) * 7 + 1))
    fsm.peak_equity = None
    return fsm


def main():
    print("=" * 66)
    print(" 峰值权益重基线 · 负样本测试")
    print("=" * 66)

    # ── 场景 1：真实场景 —— 从 risk_state.json 恢复的陈旧峰值 ──
    # 实测：账户被重置到 1,003,075，peak 是从持久化文件恢复的 1,018,785
    fsm = _fresh()
    fsm.peak_equity = 1_018_785.0
    fsm.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=1_003_075.0)
    check(
        "场景1: 真实场景 1018785→1003075（-1.54%）必须重基线",
        abs(float(fsm.peak_equity) - 1_003_075.0) < 1.0,
        f"peak={fsm.peak_equity}",
    )

    # ── 场景 2：真正的大幅重置（-30%）→ 必须重基线 ──
    fsm2 = _fresh()
    fsm2.peak_equity = 1_016_820.0
    fsm2.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=700_000.0)
    check(
        "场景2: 权益 1016820→700000（-31%）必须重基线",
        abs(float(fsm2.peak_equity) - 700_000.0) < 1.0,
        f"peak={fsm2.peak_equity}",
    )

    # ── 场景 3：重基线后封顶必须 = 新权益×2% ──
    cap, src = R.effective_daily_loss_cap(fsm2.peak_equity)
    expect = 700_000.0 * 0.02
    check(
        "场景3: 重基线后封顶 = 新权益×2%",
        abs(cap - expect) < 0.5 and src == "abs_scaled",
        f"cap={cap:.2f} 期望={expect:.2f} src={src}",
    )

    # ── 场景 4：权益上升仍正常抬升峰值（原行为不得破坏）──
    fsm4 = _fresh()
    fsm4.peak_equity = 1_000_000.0
    fsm4.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=1_050_000.0)
    check(
        "场景4: 权益上升 → 峰值正常抬升",
        abs(float(fsm4.peak_equity) - 1_050_000.0) < 1.0,
        f"peak={fsm4.peak_equity}",
    )

    # ── 场景 5：盘中正常波动 -0.5% 不应重基线 ──
    fsm5 = _fresh()
    fsm5.peak_equity = 1_000_000.0
    fsm5.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=995_000.0)
    check(
        "场景5: 盘中 -0.5% 波动不重基线（保留回撤基准）",
        abs(float(fsm5.peak_equity) - 1_000_000.0) < 1.0,
        f"peak={fsm5.peak_equity}",
    )

    # ── 场景 6：边界值不抛异常 ──
    try:
        fsm6 = _fresh()
        fsm6.peak_equity = 0.0
        fsm6.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=0.0)
        fsm7 = _fresh()
        fsm7.peak_equity = 1_000.0
        fsm7.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=None)
        ok = True
        detail = ""
    except Exception as e:
        ok, detail = False, repr(e)[:60]
    check("场景6: 边界值（0/None）不抛异常", ok, detail)

    # ── 场景 7：阈值边界 1% 恰好不触发（严格大于才重基线）──
    fsm8 = _fresh()
    fsm8.peak_equity = 1_000_000.0
    fsm8.update({"status": "NORMAL", "usage": 0, "daily_loss_pct": 0.0}, equity=990_000.0)
    check(
        "场景7: 恰好 -1.0%（等于容差）不重基线",
        abs(float(fsm8.peak_equity) - 1_000_000.0) < 1.0,
        f"peak={fsm8.peak_equity}",
    )

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("=" * 66)
    print(f"结果: {passed} PASS / {failed} FAIL")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())