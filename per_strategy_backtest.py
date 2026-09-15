#!/usr/bin/env python3
"""每策略 × 全市场品种独立回测矩阵（2026-09-16）

目标：回答「每个策略在全市场 54 个商品品种上独立表现如何」。
方法：单策略隔离——用 strat_blacklist 把目标策略以外的 7 个策略禁用，
      只留目标策略参与投票，再跑现有 walk_forward_backtest（日线口径，
      含 ATR 止损/2R 止盈/尾仓/扣费扣滑点/换月跳空识别），与实盘口径一致。

产出：
  1) 8×54 expR 矩阵（策略 × 品种）
  2) 每策略全市场统计：正期望品种数 / 负期望品种数 / 平均expR / 加权expR
  3) JSON 落盘 per_strategy_backtest_result.json

用法：
  env -u PYTHONHOME -u PYTHONPATH $PY per_strategy_backtest.py            # 日线口径
  env -u PYTHONHOME -u PYTHONPATH $PY per_strategy_backtest.py --with-5m  # 追加 5m 出场口径
"""
from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import datetime

import four_dim_strategy as fd
from strategy_layer import ALL_STRATS


def run_one_strategy(strat, syms, mode="daily"):
    """构造单策略隔离 cfg，回测该策略在所有品种上的表现。"""
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    others = [s for s in ALL_STRATS if s != strat]
    for sym, _name in syms:
        bl = set(cfg.get("strat_blacklist", {}).get(sym, []))
        bl.update(others)
        cfg["strat_blacklist"][sym] = list(bl)

    rows = []
    for sym, name in syms:
        try:
            if mode == "5m":
                r = fd.walk_forward_backtest_5m_exit(sym, cfg, long=True)
            else:
                r = fd.walk_forward_backtest(sym, cfg)
        except Exception as e:  # noqa: BLE001
            r = {"trades": 0, "expR": 0.0, "win_rate": 0.0, "note": f"异常:{repr(e)[:50]}"}
        rows.append(
            {
                "symbol": sym,
                "name": name,
                "trades": r.get("trades", 0),
                "expR": r.get("expR", 0.0),
                "win_rate": r.get("win_rate", 0.0),
            }
        )
    return rows


def strat_summary(rows):
    """单个策略的全市场统计。"""
    active = [r for r in rows if r["trades"] > 0]
    pos = sum(1 for r in active if r["expR"] > 0)
    neg = sum(1 for r in active if r["expR"] < 0)
    zero = sum(1 for r in active if r["expR"] == 0)
    nopos = sum(1 for r in rows if r["trades"] == 0)
    tot_trades = sum(r["trades"] for r in rows)
    avg_expR = sum(r["expR"] for r in active) / len(active) if active else 0.0
    wgt_expR = sum(r["expR"] * r["trades"] for r in rows) / tot_trades if tot_trades else 0.0
    return {
        "active": len(active),
        "pos": pos,
        "neg": neg,
        "zero": zero,
        "no_signal": nopos,
        "total_trades": tot_trades,
        "avg_expR": round(avg_expR, 4),
        "weighted_expR": round(wgt_expR, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-5m", action="store_true", help="追加 5m 出场口径（慢，每品种约12s）")
    ap.add_argument("--symbols", type=str, default=None, help="逗号分隔品种，默认全市场 54 品种")
    args = ap.parse_args()

    syms = [(s, fd.SYMBOLS.get(s, {}).get("name", "")) for s in fd.SYMBOLS]
    if args.symbols:
        want = {x.strip() for x in args.symbols.split(",") if x.strip()}
        syms = [(s, n) for s, n in syms if s in want]

    print(f"全市场品种: {len(syms)} 个 | 策略: {len(ALL_STRATS)} 个 | 共 {len(syms)*len(ALL_STRATS)} 次回测", flush=True)

    result = {"generated": datetime.now().isoformat(), "strategies": ALL_STRATS, "symbols": [s for s, _ in syms]}

    for mode in (["daily"] + (["5m"] if args.with_5m else [])):
        t0 = time.time()
        strat_rows = {}
        print(f"\n{'='*70}\n【{mode} 口径】每策略全市场回测\n{'='*70}", flush=True)
        for strat in ALL_STRATS:
            rows = run_one_strategy(strat, syms, mode)
            strat_rows[strat] = rows
            ss = strat_summary(rows)
            print(
                f"[{strat:10}] 有信号={ss['active']:2}/{len(syms)}  "
                f"正期望={ss['pos']:2} 负期望={ss['neg']:2} 零={ss['zero']:2} 无信号={ss['no_signal']:2}  "
                f"平均expR={ss['avg_expR']:+.4f} 加权expR={ss['weighted_expR']:+.4f}  "
                f"总笔数={ss['total_trades']}",
                flush=True,
            )
        result[mode] = {"rows_by_strategy": strat_rows, "elapsed_sec": round(time.time() - t0, 1)}

        # 打印矩阵
        print(f"\n----- {mode} 口径 expR 矩阵（策略 × 品种，+为盈 -为亏）-----")
        header = f"{'策略':10} " + " ".join(f"{s:>5}" for s, _ in syms)
        print(header)
        for strat in ALL_STRATS:
            cells = []
            for r in strat_rows[strat]:
                if r["trades"] == 0:
                    cells.append("  ·  ".rjust(5))
                else:
                    cells.append(f"{r['expR']:+.2f}".rjust(5))
            print(f"{strat:10} " + " ".join(cells))
        print("(· = 无信号/0笔)")

    with open("per_strategy_backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已落盘: per_strategy_backtest_result.json")


if __name__ == "__main__":
    main()
