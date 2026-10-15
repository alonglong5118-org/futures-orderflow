#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以用户提供的券商模拟盘截图为唯一真相源，重置三感参谋模拟盘状态。

截图事实（2026-10-15 22:11 用户提供，两张图）：
  账户         : 模拟盘 账户_sell0Y2K
  权益         : 1,003,075.00
  可用资金     : 1,003,075.00
  资金使用率   : 0.00%
  盯市浮盈     : 0.00
  平仓盈亏     : 0.00
  当前持仓     : 空仓（可开多1手/ 可开空1手 / 可平 0 手）
  1手保证金    : 22,114 元（ru2701 @19910）
  历史成交     : 仅 2 笔，全为 ru2701(nz2701) 全仓各 1 手
      10-14 22:30:33  卖出平仓  19855
      10-12 10:10:27  买入开仓  20750
  行情         : ru2701 = 19910

用户两项决策：
  1) 截图即真相 —— 清空系统历史测试单（fu/J/zn/pg/ru@18830 共 5 笔、94 笔旧成交、
     al 多2手@23870 脏数据），只保留截图这 2 笔 ru2701 成交。
  2) 初始资金保留 1,000,000，差额 3,075.00 计入已实现盈亏（用户明确选择
     「不管差异，直接对齐截图数字」）。

幂等：可重复执行。执行前自动备份到 _backup_before_truth_sync_*。
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 截图真相常量 ────────────────────────────────────────────────
SHOT_EQUITY = 1_003_075.00
SHOT_AVAILABLE = 1_003_075.00
INIT_EQUITY = 1_000_000.00          # 用户决策：保留 100 万初始
REALIZED_ALIGN = 3_075.00           # 差额计入已实现盈亏 = 1003075 - 1000000
# 账户口径 account_monitor: equity = cash + realized + mtm
# 因此 cash 必须是「初始金」而非「可用资金」，否则 realized 会被重复计入。
# （2026-10-15 首版误把 cash 写成可用资金 1003075，导致 equity 显示 1006150，虚高 3075。）
CASH_BASE = INIT_EQUITY
RU_MULTIPLIER = 10
SHOT_PRICE = 19_910.0
SHOT_MARGIN_1LOT = 22_114.0

# 截图历史成交（按时间正序，开仓在前）
SHOT_TRADES = [
    {
        "symbol": "ru",
        "name": "橡胶",
        "direction": "多",
        "lots": 1,
        "price": 20_750.0,              # 买入开仓价
        "open_time": "2026-10-12 10:10:27",
        "contract": "ru2701",
    },
    {
        "symbol": "ru",
        "name": "橡胶",
        "direction": "多",
        "lots": 1,
        "price": 19_855.0,              # 卖出平仓价
        "close_time": "2026-10-14 22:30:33",
        "contract": "ru2701",
    },
]


def _backup():
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(HERE, f"_backup_before_truth_sync_{ts}")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for f in (
        "paper_account.json",
        "paper_trading_state.json",
        "account_state.json",
        "trade_journal.json",
    ):
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dst, f))
            n += 1
    print(f"[备份] {n} 个文件 -> {os.path.basename(dst)}")
    return dst


def _write(name, obj):
    p = os.path.join(HERE, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    print(f"[写入] {name}")


def main():
    print("=" * 62)
    print(" 以截图为真相源 · 重置模拟盘状态")
    print("=" * 62)
    _backup()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. paper_account.json ──空仓 + 权益对齐截图 ──
    paper_account = {
        "init_equity": INIT_EQUITY,
        # 截图：当前空仓
        "positions": {},
        "last_updated": now,
        "cash": CASH_BASE,
        "realized": REALIZED_ALIGN,
        "trades": [
            {
                "time": t.get("open_time") or t.get("close_time"),
                "symbol": t["symbol"],
                "action": "开" if "open_time" in t else "平",
                "direction": t["direction"],
                "lots": t["lots"],
                "price": t["price"],
                "contract": t["contract"],
                "reason": "截图对齐(ru2701)",
            }
            for t in SHOT_TRADES
        ],
        "updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    }
    _write("paper_account.json", paper_account)

    # ── 2. paper_trading_state.json ──空仓 + 只留截图 2 笔成交 ──
    pts = {
        "cash": CASH_BASE,
        "realized_pnl": REALIZED_ALIGN,
        # 截图：空仓
        "positions": {},
        "trades": [
            {
                "type": "open",
                "time": "2026-10-12 10:10:27",
                "symbol": "ru",
                "name": "橡胶",
                "direction": "多",
                "lots": 1,
                "price": 20_750.0,
                "stop": None,
                "target": None,
                "contract": "ru2701",
                "source": "truth_sync",
                "strategy": "截图对齐：ru2701 买入开仓 1 手",
            },
            {
                "type": "close",
                "time": "2026-10-14 22:30:33",
                "symbol": "ru",
                "name": "橡胶",
                "direction": "多",
                "lots": 1,
                "entry_price": 20_750.0,
                "exit_price": 19_855.0,
                "stop_price": None,
                "reason": "截图对齐：ru2701 卖出平仓 1 手",
                "contract": "ru2701",
                "source": "truth_sync",
            },
        ],
        "equity_curve": [
            {"time": "2026-10-12 10:10:27", "equity": SHOT_EQUITY},
            {"time": "2026-10-14 22:30:33", "equity": SHOT_EQUITY},
        ],
        "stats": {
            "total_trades": 2,
            "win_trades": 0,
            "lose_trades": 1,
            "win_rate": 0.0,
        },
        "processed_signals": {},
        "cooldowns": {},
        "config": {},
        "last_updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    }
    _write("paper_trading_state.json", pts)

    # ── 3. trade_journal.json ──清空脏数据（含 al/ru 未平仓）──
    # 这两条 pnl=null 的未平仓记录正是自动对账死循环的燃料，必须清除
    tj = {
        "trades": [
            {
                "symbol": "ru",
                "name": "橡胶",
                "direction": "多",
                "lots": 1,
                "entry_price": 20_750.0,
                "entry_time": "2026-10-12 10:10:27",
                "exit_price": 19_855.0,
                "exit_time": "2026-10-14 22:30:33",
                # 价差 -895 × 乘数 10 × 1 手
                "pnl": (19_855.0 - 20_750.0) * RU_MULTIPLIER,
                "open_fee": 0.0,
                "close_fee": 0.0,
                "fee_total": 0.0,
                "contract": "ru2701",
                "source": "truth_sync",
                "note": "截图对齐：真实成交已平仓，非当前持仓",
            }
        ],
        "summary": {},
        "updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    }
    _write("trade_journal.json", tj)

    # ── 4. account_state.json ──空仓 + 权益对齐 ──
    acct = {
        "positions": {},
        "realized_pnl": REALIZED_ALIGN,
        "realized_pnl_at_sync": REALIZED_ALIGN,
        "float_at_sync": 0.0,
        "margin_occupied": 0.0,
        "available": SHOT_AVAILABLE,
        "equity": SHOT_EQUITY,
        "snapshot": {
            "equity": SHOT_EQUITY,
            "available": SHOT_AVAILABLE,
            "total_margin": 0.0,
            "float_total": 0.0,
            "mtm_pnl": 0.0,
            "usage_rate": 0.0,
            "positions": [],
        },
        "snapshot_mode": True,
        "snapshot_until": "2099-12-31 23:59:59",
        "updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    }
    _write("account_state.json", acct)

    print("=" * 62)
    print(f" 权益        = {SHOT_EQUITY:,.2f}   (截图一致)")
    print(f" 可用资金    = {SHOT_AVAILABLE:,.2f}   (截图一致)")
    print(f" 资金使用率  = 0.00%      (截图一致)")
    print(f" 持仓        = 空仓        (截图一致)")
    print(f" 历史成交    = 2 笔 ru2701 (截图一致)")
    print(f" 初始资金    = {INIT_EQUITY:,.2f}")
    print(f" 已实现盈亏  = {REALIZED_ALIGN:,.2f}  (= 权益 - 初始)")
    print(f" cash 基数   = {CASH_BASE:,.2f}  (= 初始金，非可用资金)")
    # 自检：账户口径 equity = cash + realized，必须等于截图权益
    _equity_check = CASH_BASE + REALIZED_ALIGN
    assert abs(_equity_check - SHOT_EQUITY) < 0.01, (
        f"权益口径自检失败: cash({CASH_BASE}) + realized({REALIZED_ALIGN}) "
        f"= {_equity_check} !=截图 {SHOT_EQUITY}"
    )
    print(f" ✅ 权益口径自检: {CASH_BASE:,.2f} + {REALIZED_ALIGN:,.2f} = {_equity_check:,.2f} == 截图")
    print("=" * 62)


if __name__ == "__main__":
    sys.exit(main())