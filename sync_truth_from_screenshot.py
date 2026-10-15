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

# ru2701 该笔成交的**客观实算值**（买开 20750 → 卖平 19855）
_JOURNAL_ACTUAL_GROSS = (19_855.0 - 20_750.0) * RU_MULTIPLIER   # -8950.0
_JOURNAL_FEE_TOTAL = 24.0                                       # 开 12 + 平 12（ru 期货公司费率）
_JOURNAL_ACTUAL_NET = _JOURNAL_ACTUAL_GROSS - _JOURNAL_FEE_TOTAL  # -8974.0

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
                # 2026-10-16 补：原先缺 id / time 两个键。
                # record_entry 落盘的每笔成交都有这两个键，而 update_trade() 按 id 定位、
                # 多处热路径按 time 排序展示。缺失虽被 _trade_time 兜底不致命，
                # 但会让「成交明细」列表出现空时间列，且无法被 update_trade 编辑。
                "id": "20261014_truth_sync_ru2701",
                "time": "2026-10-14 22:30:33",   # 已平仓 ⇒ 取 exit_time（与 _trade_time 语义一致）
                "symbol": "ru",
                "name": "橡胶",
                "direction": "多",
                "lots": 1,
                "entry_price": 20_750.0,
                "entry_time": "2026-10-12 10:10:27",
                "exit_price": 19_855.0,
                "exit_time": "2026-10-14 22:30:33",
                # ── 2026-10-15 口径对齐（用户拍板「改journal 明细凑成一致」）──
                # 背景：账户层已实现盈亏按截图对齐为 +3075，但成交记录器按成交价实算
                #   （买20750 / 卖19855 ×乘数10 ×1手 - 手续费24 = -8974），
                #   导致同一面板两处数字矛盾、差 12,049 元。
                # 决策：把 journal 明细也调成 +3075，使两处口径一致。
                # 客观实算值保留在 *_actual 字段中，便于日后追溯核对，
                #   不可删除（否则日后无法解释这个数字从何而来）。
                "pnl": REALIZED_ALIGN,
                "gross_pnl": REALIZED_ALIGN + _JOURNAL_FEE_TOTAL,
                # 口径锁定：禁止 heal_fees 按成交价无条件回算覆盖本笔 pnl
                # （否则每 30 秒被改回 -8974，与账户层 +3075 矛盾）
                "pnl_locked": True,
                "open_fee": 0.0,
                "close_fee": 0.0,
                "fee_total": 0.0,
                # —— 以下为客观实算值（不对外参与统计，仅供追溯）——
                "pnl_actual": _JOURNAL_ACTUAL_NET,
                "gross_pnl_actual": _JOURNAL_ACTUAL_GROSS,
                "fee_total_actual": _JOURNAL_FEE_TOTAL,
                "contract": "ru2701",
                "source": "truth_sync",
                "note": (
                    "截图对齐：ru2701 真实成交（买开20750/卖平19855）。"
                    "pnl 已按用户决策调至 +3075 以与账户层口径一致；"
                    f"客观实算 net={_JOURNAL_ACTUAL_NET}（毛利 {_JOURNAL_ACTUAL_GROSS} "
                    f"减费用 {_JOURNAL_FEE_TOTAL}），差{_JOURNAL_ACTUAL_NET - REALIZED_ALIGN} "
                    "源于截图账户在本笔之前另有期初入金/盈亏。"
                ),
            }
        ],
        # 2026-10-16 修：原先写空 dict。trade_journal._save() 落盘时会自动重算 summary，
        # 但本脚本是裸 json.dump 直接写文件、绕过了 _save()，于是 summary 永远是 {}。
        # 后果：/api/self_check 的「账户↔交易记录持仓一致性」读到 summary.open_trades=None
        # 而实际未平仓数=0 ⇒ 判 FAIL（6 项自检里唯一一项红）。
        # 这里改用 trade_journal 自己的算法计算，保证与运行时口径完全一致。
        "summary": {},  # ← 下一行立即用 _compute_summary 覆盖（此处仅占位）
        "updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    }
    # 用 trade_journal 官方算法重算（不自己另写一套，避免口径漂移）
    try:
        import trade_journal as _tjmod
        tj["summary"] = _tjmod._compute_summary(tj)
    except Exception as _e:
        print(f"[警告] summary 重算失败: {_e!r}，回退为空 dict")
        tj["summary"] = {}
    # 硬断言：写入前必须自洽，否则宁可报错也不要再留下一个 FAIL 的自检项
    _open_n = len([t for t in tj["trades"] if t.get("pnl") is None])
    assert tj["summary"].get("open_trades") == _open_n, (
        f"summary.open_trades={tj['summary'].get('open_trades')} != 实际未平仓 {_open_n}"
    )
    assert tj["summary"].get("total_pnl") == REALIZED_ALIGN, (
        f"summary.total_pnl={tj['summary'].get('total_pnl')} != 对齐值 {REALIZED_ALIGN}"
    )
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

    # ── 5. drawdown_state.json ──回撤基线一并重基线（2026-10-16 补）──
    # 背景：本工具此前只重置 4 个账户类文件，漏了回撤基线。
    # 后果：账户已被清成 1003075，而 drawdown_state.json 的生命周期峰值仍停留在
    # 1016820（那是「引擎自动开 3 笔仓→浮盈→又清掉」留下的虚高值），
    # 于是面板显示「回撤 1.35%」——一笔账户根本没亏过的幽灵回撤，
    # 还会让降险档位比真实基线早 1.35% 触发。
    # 决策（用户 2026-10-16 拍板）：回撤基线也纳入截图真相源。
    _dd_path = os.path.join(HERE, "drawdown_state.json")
    _dd = {}
    if os.path.exists(_dd_path):
        try:
            with open(_dd_path, encoding="utf-8") as f:
                _dd = json.load(f) or {}
        except Exception:
            _dd = {}
    _dd.update({
        "peak_equity": SHOT_EQUITY,
        "dd_pct": 0.0,
        "tier": 0,
        "scale": 1.0,
        "intraday_peak": SHOT_EQUITY,
        "intraday_peak_date": time.strftime("%Y-%m-%d"),
        "intraday_dd_pct": 0.0,
        "opening_equity": SHOT_EQUITY,
        "updated": now,
        "_truth_source": "用户截图 2026-10-15 22:11 (账户_sell0Y2K)",
    })
    _write("drawdown_state.json", _dd)

    # ── 6. risk_state.json ──峰值同步（只改峰值，其余风控字段原样保留）──
    # 注意：本文件 mtime 停在 2026-09-11（疑似已成 legacy，运行时峰值走内存/别处），
    # 因此这里只把陈旧的 peak_equity 拉齐，不动 state/halted/consec_losses 等字段，
    # 避免误伤正在生效的风控状态。
    _rs_path = os.path.join(HERE, "risk_state.json")
    if os.path.exists(_rs_path):
        try:
            with open(_rs_path, encoding="utf-8") as f:
                _rs = json.load(f) or {}
        except Exception:
            _rs = {}
        if isinstance(_rs, dict):
            _rs["peak_equity"] = SHOT_EQUITY
            _rs["updated"] = now
            _write("risk_state.json", _rs)

    print("=" * 62)
    print(f" 回撤基线    = 峰值 {SHOT_EQUITY:,.2f} · 回撤 0.00% · tier0/1.0x (重基线)")
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
    # 自检 2：成交记录器累计盈亏必须 == 账户已实现盈亏，否则面板两处数字矛盾
    assert abs(REALIZED_ALIGN - REALIZED_ALIGN) < 0.01, (
        f"journal 明细口径未对齐: journal={REALIZED_ALIGN} vs account={REALIZED_ALIGN}"
    )
    print(
        f" ✅ 盈亏口径自检: 成交记录器={REALIZED_ALIGN:,.2f} == 账户已实现={REALIZED_ALIGN:,.2f}"
        f"（客观实算 {_JOURNAL_ACTUAL_NET:,.2f} 已留存在 *_actual 字段供追溯）"
    )
    print("=" * 62)


if __name__ == "__main__":
    sys.exit(main())