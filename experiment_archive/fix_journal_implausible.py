#!/usr/bin/env python3
"""
fix_journal_implausible.py — 修正 trade_journal.json 里「物理上不可能」的盈亏
================================================================================
背景（2026-09-12 实测）：
  trade_journal.json 里一条 al（沪铝）2手 @23870（乘数5）记 pnl=907435.0，
  隐含平仓价 114613.5（+380%），物理不可能。该笔占 realized_pnl_at_sync 的 99.3%，
  把权益锚从约 8.6 万抬到 99 万 → 风控上限被放大 ~11.5 倍（系统性过度开仓）。

为什么修了 journal 权益就自动回落：
  dynamic_equity = anchor + (journal已实现 - realized_pnl_at_sync) + (浮动 - float_at_sync)
  anchor(993685) 本身 = 本金(100600) + 已实现(914229) + 浮动(-21150)，即「由 journal 推导」。
  把假盈亏去掉后 journal 总盈亏从 914229 → 6794，
  Δ已实现 = 6794 - 914229 = -907435，dynamic_equity 自动回落到 ≈ 86,244。
  —— 因此本脚本只改 journal，不碰 account_state、不重新同步权益。

🔴 安全设计（这是交易数据，必须由你手动执行）：
  · 默认【只读预演】，只打印将要做的改动，绝不写盘；
  · 必须显式加 --apply 才会写；
  · 写前自动备份 trade_journal.json.bak-fix-<时间戳>（不动已有 .bak）；
  · 原子写（临时文件 + os.replace），写后重算 summary。

用法：
  python3 fix_journal_implausible.py                              # 预演（只读）
  python3 fix_journal_implausible.py --remove --apply             # 删除可疑笔
  python3 fix_journal_implausible.py --set-pnl 9074.35 --apply    # 改成估计的真实值
  python3 fix_journal_implausible.py --set-pnl none --apply       # 置空（排除出已实现）

修完之后：请核对券商真实权益，若与脚本给出的预估不同，再用面板上的「同步权益」按钮校正。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(HERE, "trade_journal.json")
ACCOUNT_STATE = os.path.join(HERE, "account_state.json")


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _sanity():
    """复用 equity_history 的物理校验，保持口径一致。"""
    sys.path.insert(0, HERE)
    try:
        import equity_history as eh

        return eh.journal_sanity()
    except Exception as e:
        print(f"⚠️ 无法调用 equity_history.journal_sanity（{e}），退出")
        sys.exit(2)


def _recompute_summary(data):
    """尽力用 trade_journal 自己的算法重算 summary，保证口径一致。"""
    sys.path.insert(0, HERE)
    try:
        import trade_journal as tj

        data["summary"] = tj._compute_summary(data)
        return True
    except Exception as e:
        print(f"⚠️ 无法重算 summary（{e}）：字段会保持旧值，请随后手工核对")
        return False


def _estimate_equity(total_pnl_after):
    """按权益链公式预估修正后的动态权益（只读）。"""
    st = _load_json(ACCOUNT_STATE, {}) or {}
    if not st:
        return None
    anchor = float(st.get("equity", 0) or 0)
    ra = float(st.get("realized_pnl_at_sync", 0) or 0)
    fa = float(st.get("float_at_sync", 0) or 0)
    # 预估时假设当前浮动 ≈ 同步时浮动（无法预知未来价格）
    return {
        "anchor": anchor,
        "realized_at_sync": ra,
        "float_at_sync": fa,
        "est_dynamic_equity": round(anchor + (total_pnl_after - ra), 2),
    }


def main():
    ap = argparse.ArgumentParser(description="修正 journal 物理不可能盈亏")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--remove", action="store_true", help="删除可疑交易记录")
    g.add_argument("--set-pnl", type=str, metavar="VAL", help="把可疑笔 pnl 改成 VAL（或 none 置空）")
    ap.add_argument("--apply", action="store_true", help="真正写盘（缺省为只读预演）")
    args = ap.parse_args()

    if not os.path.exists(JOURNAL):
        print(f"找不到 {JOURNAL}")
        return 2

    san = _sanity()
    susp = san.get("suspicious") or []
    print(f"journal 物理校验：检查 {san.get('checked', 0)} 笔可结算成交，可疑 {len(susp)} 笔")
    if not susp:
        print("✅ 无可疑记录，无需修正")
        return 0

    data = _load_json(JOURNAL, {})
    trades = data.get("trades") or []

    # 用 (time, symbol, lots, pnl) 精确定位可疑笔在 trades 里的下标
    targets = []
    for s in susp:
        for i, t in enumerate(trades):
            if not isinstance(t, dict):
                continue
            if (
                str(t.get("time")) == str(s.get("time"))
                and str(t.get("symbol")) == str(s.get("symbol"))
                and float(t.get("lots", 0) or 0) == float(s.get("lots") or 0)
                and float(t.get("pnl", 0) or 0) == float(s.get("pnl") or 0)
            ):
                targets.append(i)
                break
    if not targets:
        print("⚠️ 未能把可疑记录定位到 trades 下标，退出（不改任何数据）")
        return 2

    old_total = sum(float(t.get("pnl") or 0) for t in trades if isinstance(t, dict))
    for i in targets:
        t = trades[i]
        print(
            f"\n将处理 trades[{i}]: {t.get('time')} {t.get('symbol')} "
            f"{t.get('lots')}手 @{t.get('entry_price')} pnl={t.get('pnl')}"
        )

    new_trades = list(trades)
    if args.remove:
        new_trades = [t for j, t in enumerate(new_trades) if j not in set(targets)]
        action_desc = f"删除 {len(targets)} 笔"
    else:
        val = args.set_pnl.strip().lower()
        new_pnl = None if val in ("none", "null", "") else float(val)
        for i in targets:
            new_trades[i].setdefault("_pnl_fixed", {})["was"] = new_trades[i].get("pnl")
            new_trades[i]["_pnl_fixed"]["fixed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_trades[i]["_pnl_fixed"]["reason"] = "物理不可能：隐含平仓价超出合理区间"
            new_trades[i]["pnl"] = new_pnl
        action_desc = f"把 {len(targets)} 笔 pnl 改为 {new_pnl}"

    new_total = sum(float(t.get("pnl") or 0) for t in new_trades if isinstance(t, dict))
    print(f"\n动作：{action_desc}")
    print(f"journal 总已实现盈亏：{old_total:,.2f} → {new_total:,.2f}（Δ {new_total - old_total:+,.2f}）")

    est = _estimate_equity(new_total)
    if est:
        print(
            f"预估动态权益：{est['anchor']:,.0f}（anchor） − Δ已实现 "
            f"{est['realized_at_sync'] - new_total:,.0f} = {est['est_dynamic_equity']:,.2f}"
        )
        print("⚠️ 该预估假设当前浮动 ≈ 同步时浮动；实际值以券商权益为准。")

    if not args.apply:
        print("\n【只读预演】未写任何文件。确认无误后加 --apply 重新执行。")
        return 0

    # ── 写盘 ──
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{JOURNAL}.bak-fix-{ts}"
    try:
        shutil.copy2(JOURNAL, bak)
        print(f"\n已备份：{bak}")
    except Exception as e:
        print(f"❌ 备份失败，中止（不写盘）：{e}")
        return 2

    data["trades"] = new_trades
    _recompute_summary(data)
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    tmp = JOURNAL + ".tmp-fix"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOURNAL)
    except Exception as e:
        print(f"❌ 写盘失败：{e}（备份仍在 {bak}，原文件未被破坏）")
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return 2

    # 复核：重新跑一次校验
    san2 = _sanity()
    ok = not san2.get("suspicious")
    print(f"写入完成。复核：可疑 {len(san2.get('suspicious') or [])} 笔 {'✅ 已清零' if ok else '⚠️ 仍有残留'}")
    print("\n下一步：")
    print("  1) 重启 runner 让内存态重载：launchctl kickstart -k gui/$(id -u)/com.a123.fourdim")
    print("  2) 核对券商真实权益；若与预估不符，用面板「同步权益」按钮校正（不要用 API 写）")
    print("  3) 跑 python3 equity_history.py --audit 确认告警消失")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
