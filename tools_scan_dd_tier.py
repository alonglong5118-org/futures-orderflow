#!/usr/bin/env python3
"""dd_tier 分段熔断阈值扫描（2026-09-07，状态分层版）。

问题：决策46/47 的 dd_tier 分级（normal<5% / soft5≥5% / only_mar8≥8% / fullstop10≥10% / halt15≥15%）
来自启发式拍板，未验证「回撤状态下新开仓是否更易亏」这一闸门前提。

方法（状态分层，无死锁）：
  · 取 live 品种组合 walk_forward_backtest 逐笔成交（含 entry_date/exit_date/entry_price/stop_dist/dir/R_adj）；
  · 按日盯市重建**无闸门参考权益曲线**，得每根交易日的组合回撤 dd；
  · 把每笔成交按其「入场时组合回撤 dd_entry」分层；
  · 直接检验前提：高 dd 时入场的中续 R 是否显著差于低 dd？（若否→闸门无前提，纯属枷锁）
  · 对每档阈值，统计「会被拦截的成交」数量与合计 R（负→闸门省钱；正→闸门误杀盈利）。

说明：R 空间未做资本约束（841 笔 R 直接累加，无杠杆/不出金/无强平），故组合 dd 可达 200%+，
这恰说明「固定 3-5% 组合回撤闸门」对本策略波动率是自杀式紧——但本扫描改用「入场回撤状态分层」
规避了该缩放死锁，专注检验闸门前提是否成立。
"""
import sys
import numpy as np
import pandas as pd

HERE = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, HERE)
from four_dim_strategy import walk_forward_backtest, DEFAULT_CONFIG, DISABLED_SYMBOLS, load_daily

SYMS = ["rb", "FG", "ag", "cu", "al", "JM", "jd", "SR", "TA", "y", "p", "ru", "c", "fu", "J"]


def collect_trades():
    trades = []
    closes = {}
    for sym in SYMS:
        if sym in DISABLED_SYMBOLS:
            print(f"  skip {sym}: 校准判死，不参与 live 组合")
            continue
        r = walk_forward_backtest(sym)
        if r.get("trades", 0) == 0:
            continue
        df = load_daily(sym)
        closes[sym] = df["close"]
        for t in r["trades_detail"]:
            trades.append({
                "sym": sym,
                "dir": int(t["dir"]),
                "R": float(t["R_adj"]),
                "entry": t["entry_date"],
                "exit": t["exit_date"],
                "entry_price": float(t["entry_price"]),
                "sd": float(t["stop_dist"]),
            })
    trades.sort(key=lambda x: x["entry"])
    return trades, closes


def build_reference(trades, closes):
    """无闸门参考权益曲线（日盯市），返回每日 dd 数组与全局日期索引。"""
    gmin = min(t["entry"] for t in trades)
    gmax = max(t["exit"] for t in trades)
    D = pd.date_range(gmin, gmax, freq="B")
    nD = len(D)
    ca = {sym: s.reindex(D).ffill().to_numpy(dtype=float) for sym, s in closes.items()}
    eq = np.zeros(nD)
    for t in trades:
        i0 = D.get_indexer([t["entry"]], method="backfill")[0]
        i1 = D.get_indexer([t["exit"]], method="pad")[0]
        if i0 < 0:
            i0 = 0
        if i1 < 0 or i1 < i0:
            i1 = min(i0, nD - 1)
        c = ca[t["sym"]]
        ep, sd, d = t["entry_price"], t["sd"], t["dir"]
        for j in range(i0, i1):
            cj = c[j] if not np.isnan(c[j]) else ep
            eq[j] += (cj - ep) / sd * d if sd > 0 else 0.0
        eq[i1] += t["R"]  # 平仓日锁定已实现
    # 每日 dd
    dd = np.zeros(nD)
    pk = 0.0
    for j in range(nD):
        if eq[j] > pk:
            pk = eq[j]
        dd[j] = (pk - eq[j]) / pk * 100.0 if pk > 1e-9 else 0.0
    return D, dd


def main():
    trades, closes = collect_trades()
    D, dd = build_reference(trades, closes)
    # 每笔入场回撤
    for t in trades:
        i0 = D.get_indexer([t["entry"]], method="backfill")[0]
        t["dd_entry"] = float(dd[i0]) if i0 >= 0 else 0.0

    allR = [t["R"] for t in trades]
    print(f"dd_tier 入场回撤状态分层（{len(trades)} 笔 live 成交，参考曲线最大回撤 {dd.max():.0f}%）")
    print("=" * 86)
    print(f"{'分层(入场dd)':16} {'成交':>6} {'占比':>7} {'avgR':>8} {'sumR':>9} {'胜率':>7}")
    buckets = [("0–5%", 0, 5), ("5–8%", 5, 8), ("8–10%", 8, 10),
               ("10–15%", 10, 15), ("≥15%", 15, 1e9)]
    for name, lo, hi in buckets:
        grp = [t for t in trades if lo <= t["dd_entry"] < hi]
        if not grp:
            print(f"{name:16} {0:6d} {'--':>7} {'--':>8} {'--':>9} {'--':>7}")
            continue
        Rs = [t["R"] for t in grp]
        wr = sum(1 for r in Rs if r > 0) / len(Rs)
        print(f"{name:16} {len(grp):6d} {len(grp)/len(trades)*100:6.1f}% "
              f"{np.mean(Rs):+8.3f} {sum(Rs):+9.2f} {wr*100:6.1f}%")
    base = np.mean(allR)
    wr = sum(1 for r in allR if r > 0) / len(allR)
    print(f"{'全样本':16} {len(allR):6d} {'100%':>7} {base:+8.3f} {sum(allR):+9.2f} {wr*100:6.1f}%")
    print("-" * 86)
    print("前提检验：高 dd 层 avgR 是否显著低于全样本？")
    print("  · 若 10–15% / ≥15% 层 avgR ≈ 全样本 → 回撤不预测下一笔亏损，闸门无前提（纯枷锁）")
    print("  · 若高 dd 层 avgR 明显更负 → 闸门前提成立，分级有价值")
    print("=" * 86)
    # 各档闸门会拦截的成交（用无闸门参考曲线的 dd_entry 标签，无反馈死锁）
    print("各档闸门「会拦截的成交」（入场 dd ≥ 阈值才拦；级联按档位判定）：")
    print(f"{'规则':16} {'放行':>6} {'拦截':>6} {'拦截合计R':>10} {'拦截avgR':>9} {'含义'}")
    # 单一硬阈 Hx：入场 dd ≥ x*0.6 才拦截（滞回：回落至 60% 解除≈不拦）
    for x in [5, 8, 10, 15]:
        blk = [t for t in trades if t["dd_entry"] >= x]
        if blk:
            bR = [t["R"] for t in blk]
            bar = f"高dd层 avgR {np.mean(bR):+.3f}（{'省亏' if np.mean(bR)<0 else '误杀盈利'}）"
        else:
            bar = "无成交达此回撤→闸门从未触发"
        nblk = len(blk)
        sblk = sum(t["R"] for t in blk)
        print(f"{'H%d 硬阈'%x:16} {len(trades)-nblk:6d} {nblk:6d} {sblk:+10.2f} "
              f"{(np.mean([t['R'] for t in blk]) if blk else 0):+9.3f}  {bar}")
    # 生产级 5 档级联
    def casc(t):
        d = t["dd_entry"]
        if d >= 15 or d >= 10:
            return False
        if d >= 8:
            return t["sym"] == "J"
        if d >= 5:
            return "size0.7"
        return True
    blk = [t for t in trades if casc(t) is False]
    sblk = sum(t["R"] for t in blk)
    avg_blk = (np.mean([t["R"] for t in blk]) if blk else 0.0)
    avg_str = ("%+.3f" % avg_blk) if blk else "无"
    print(f"{'C 生产级5档':16} {len(trades) - len(blk):6d} {len(blk):6d} {sblk:+10.2f} "
          f"{avg_blk:+9.3f}  拦截层 avgR {avg_str}")
    print("=" * 86)


if __name__ == "__main__":
    main()
