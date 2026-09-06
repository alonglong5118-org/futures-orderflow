#!/usr/bin/env python3
"""实盘成交回填策略标签（策略标签体系 P1，2026-08-31）。

对 trade_journal.json 中 strategy 为空/缺失的成交，
按「品种 + 方向 + 时间窗」匹配 four_dim_signals.json 中的最近同向信号，
回填 signal_id + strategy + strat_evidence。
匹配不上 → strategy = "手动"。

用法：
    python tools_backfill_strategy_labels.py          # 预览模式（不写回）
    python tools_backfill_strategy_labels.py --write   # 实际写回 journal
"""

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
JOURNAL_FILE = HERE / "trade_journal.json"
SIGNALS_FILE = HERE / "four_dim_signals.json"
BACKUP_FILE = HERE / "trade_journal.json.bak"

# 回填窗口：成交前 N 分钟内找最近同向信号
BACKFILL_WINDOW_MIN = 120


def _parse_time(s):
    """解析时间字符串，支持多种格式。"""
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def _signal_direction(sig):
    """从信号 dict 提取方向（1=多, -1=空）。"""
    d = sig.get("direction") or sig.get("dir_T") or sig.get("dir")
    if isinstance(d, (int, float)):
        return 1 if d > 0 else (-1 if d < 0 else 0)
    if isinstance(d, str):
        if d in ("多", "long", "buy", "1"):
            return 1
        if d in ("空", "short", "sell", "-1"):
            return -1
    return 0


def _trade_direction(trade):
    """从成交 dict 提取方向。"""
    d = trade.get("direction") or trade.get("dir")
    if isinstance(d, (int, float)):
        return 1 if d > 0 else (-1 if d < 0 else 0)
    if isinstance(d, str):
        if d in ("多", "long", "buy", "1"):
            return 1
        if d in ("空", "short", "sell", "-1"):
            return -1
    return 0


def load_signals():
    """加载信号日志，按品种+方向建索引。"""
    if not SIGNALS_FILE.exists():
        return {}
    with open(SIGNALS_FILE, encoding="utf-8") as f:
        signals = json.load(f)
    # 索引: (symbol, direction) → [signal, ...]（按时间降序）
    idx = {}
    for sig in signals:
        sym = sig.get("symbol") or sig.get("name", "")
        direction = _signal_direction(sig)
        t = _parse_time(sig.get("time") or sig.get("created_at"))
        if not sym or direction == 0 or t is None:
            continue
        key = (sym, direction)
        idx.setdefault(key, []).append({"time": t, "signal": sig})
    # 按时间降序排列（最新的在前）
    for key in idx:
        idx[key].sort(key=lambda x: x["time"], reverse=True)
    return idx


def find_matching_signal(sig_idx, symbol, direction, trade_time):
    """在时间窗内找最近的同向信号。"""
    key = (symbol, direction)
    candidates = sig_idx.get(key, [])
    if not candidates:
        return None
    window_start = trade_time - timedelta(minutes=BACKFILL_WINDOW_MIN)
    for entry in candidates:
        if entry["time"] <= trade_time and entry["time"] >= window_start:
            return entry["signal"]
    return None


def recompute_signal_evidence(symbol, signal_time):
    """对历史信号回算簇证据（升级前的信号无 strat_evidence 字段）。

    用 pipeline 以信号时间重放当日计算，取簇贡献推导标签。
    pipeline 的 date 参数格式为 YYYYMMDD。
    """
    try:
        from four_dim_strategy import load_daily, pipeline
        date_str = str(signal_time)[:10].replace("-", "")
        if len(date_str) != 8:
            return None
        df = load_daily(symbol)
        if df is None or len(df) < 60:
            return None
        result = pipeline(symbol, df, None, date=date_str)
        ev = result.get("strat_evidence")
        if not ev:
            return None
        return {"strategy": result.get("strategy", "手动"), "strat_evidence": ev}
    except Exception:
        return None


def backfill(dry_run=True):
    # 加载数据
    with open(JOURNAL_FILE, encoding="utf-8") as f:
        journal = json.load(f)
    trades = journal.get("trades", [])
    sig_idx = load_signals()

    stats = {"total": 0, "already_labeled": 0, "backfilled": 0, "manual": 0, "no_time": 0}
    changes = []

    for i, trade in enumerate(trades):
        stats["total"] += 1

        # 已有标签的跳过
        existing = str(trade.get("strategy", "")).strip()
        if existing and existing != "手动":
            stats["already_labeled"] += 1
            continue

        sym = trade.get("symbol", "")
        direction = _trade_direction(trade)
        trade_time = _parse_time(
            trade.get("entry_time") or trade.get("time") or trade.get("created_at")
        )

        if not trade_time:
            stats["no_time"] += 1
            if not existing:
                trade["strategy"] = "手动"
            continue

        # 尝试匹配信号
        matched = find_matching_signal(sig_idx, sym, direction, trade_time)
        if matched:
            trade["signal_id"] = matched.get("time", "")
            matched_strategy = matched.get("strategy")
            # 历史信号（升级前生成）无 strategy/strat_evidence → 回算簇证据推导标签
            if not matched_strategy or not matched.get("strat_evidence"):
                recomputed = recompute_signal_evidence(sym, matched.get("time", ""))
                if recomputed:
                    matched["strategy"] = recomputed["strategy"]
                    matched["strat_evidence"] = recomputed["strat_evidence"]
                    matched_strategy = recomputed["strategy"]
            trade["strategy"] = matched_strategy or "手动"
            if matched.get("strat_evidence"):
                trade["strat_evidence"] = matched["strat_evidence"]
            stats["backfilled"] += 1
            changes.append({
                "idx": i,
                "symbol": sym,
                "direction": "多" if direction > 0 else "空",
                "trade_time": str(trade_time),
                "matched_signal_time": matched.get("time", ""),
                "strategy": trade["strategy"],
            })
        else:
            trade["strategy"] = "手动"
            stats["manual"] += 1

    # 输出统计
    print(f"{'='*50}")
    print(f"回填统计:")
    print(f"  总成交: {stats['total']} 笔")
    print(f"  已有标签: {stats['already_labeled']} 笔")
    print(f"  成功回填: {stats['backfilled']} 笔")
    print(f"  标为手动: {stats['manual']} 笔")
    print(f"  无时间戳: {stats['no_time']} 笔")

    if changes:
        print(f"\n回填明细:")
        for c in changes:
            print(
                f"  [{c['idx']}] {c['symbol']} {c['direction']} "
                f"@ {c['trade_time'][:16]} → {c['strategy']} "
                f"(信号 @ {c['matched_signal_time'][:16]})"
            )

    if dry_run:
        print(f"\n[预览模式] 未写回。加 --write 参数实际写入。")
    else:
        # 备份 + 原子写入
        shutil.copy2(JOURNAL_FILE, BACKUP_FILE)
        journal["trades"] = trades
        with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(journal, f, ensure_ascii=False, indent=2)
        print(f"\n已写回 {JOURNAL_FILE}（备份: {BACKUP_FILE}）")

    return stats


def main():
    dry_run = "--write" not in sys.argv
    backfill(dry_run=dry_run)


if __name__ == "__main__":
    main()
