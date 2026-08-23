#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维策略 OOS 对比脚本（整改后 on vs 整改前 off=v12）
====================================================
在同一份本地数据上，对品种分别跑两遍回测：
  · on  = DEFAULT_CONFIG（P-A~P-H 全部 enabled，即现状整改后）
  · off = 把 decorrelate/seasonal_boost/regime_params/trailing_tail/
           robust_pool_gate 的 enabled 设 False（= 旧 v12 行为，A-B 对照基线）

支持三种模式：
  [日线]  默认 / 指定品种  → walk_forward_backtest（日线 bar 出场）
  --all               → 全市场：自动发现所有有日线数据的品种
  --5m                → 5m 出场细化：仅 5 个有 5m 数据的品种(FG/J/JM/SA/lh)，
                        日线定信号 + 5m bar 做出场，验证 P-G 尾仓在细粒度下的真实表现
  --1h                → 1h 出场细化：FIVE_M_TARGETS 同上，但把 5m 先 resample 为 1h
                        再跑同一套出场逻辑，作为 日线→1h→5m 粒度阶梯的中间档（无原生1h数据）

对比指标（逐品种并列）：笔数 / 期望R / 胜率 / 最大回撤(R) / t2达成率 / 尾仓占比 /
各 regime 期望R；并给出 Δ 与判定（改善/退化/持平）。

⚠️ 关于 P-H：robust_pool_gate 是"稳健池准入闸门"，回测直接对指定品种跑、不查 gate，
因此 P-H 的 on/off 在单品种回测层面无差异；其 A-B 应另看"被准入交易的品种集合"变化。

⚠️ 关于 --5m：5m 数据仅近 ~3 周(7/21-8/11)、5 个品种，属"近期机制验证"（确认尾仓在
细粒度下确实让利润奔跑、回撤触止损离场），样本较小，非全样本 OOS 期望估计。

依赖：four_dim_strategy.walk_forward_backtest / walk_forward_backtest_5m_exit（需 pandas/numpy + 本地数据）。
"""
import copy
import json
import os
import sys
import signal
import fcntl
import numpy as np
import four_dim_strategy as fd

PER_SYM_TIMEOUT = 900  # 单品种(on+off两遍)超时秒数，超时跳过防止永久卡死（2026-08-16 放宽至900s以容纳FG等慢品种≈505s）

def _on_alarm(signum, frame):
    raise TimeoutError("per-symbol timeout")

# 用户固定关注的 6 个品种（省略命令行参数时默认）
DEFAULT_TARGETS = ["jd", "lh", "FG", "SA", "JM", "J"]

# 有本地 5m 数据的品种（用于 --5m 出场细化）
FIVE_M_TARGETS = ["FG", "J", "JM", "SA", "lh"]

# 受本轮整改控制的开关块（enabled 决定生效与否）
SWITCH_BLOCKS = ["decorrelate", "seasonal_boost", "regime_params",
                 "trailing_tail", "robust_pool_gate"]


def make_cfg(enabled: bool):
    """基于 DEFAULT_CONFIG 深拷贝；enabled=True 用整改后(现状)，False 退回 v12。"""
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    for blk in SWITCH_BLOCKS:
        if isinstance(cfg.get(blk), dict):
            cfg[blk]["enabled"] = bool(enabled)
    return cfg


def max_drawdown(Rs):
    """逐笔 R_adj 累积权益曲线的峰谷最大回撤（单位 R）。"""
    if not Rs:
        return 0.0
    eq = np.cumsum(np.array(Rs, dtype=float))
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def summarize(r):
    """从回测返回抽取指标（含回撤）。"""
    trades = r.get("trades_detail") or []
    Rs = [t["R_adj"] for t in trades]
    n = len(Rs)
    expR = float(np.mean(Rs)) if n else 0.0
    wins = [x for x in Rs if x > 0]
    win = len(wins) / n if n else 0.0
    reasons = r.get("exit_reasons", {})
    t2 = reasons.get("止盈2R", 0) + reasons.get("尾仓离场", 0)
    tail = reasons.get("尾仓离场", 0)
    return {
        "trades": n,
        "expR": round(expR, 4),
        "win_rate": round(win, 3),
        "max_dd_R": round(max_drawdown(Rs), 3),
        "t2_rate": round(t2 / n, 3) if n else 0.0,
        "tail_share": round(tail / n, 3) if n else 0.0,
        "by_regime": r.get("by_regime", {}),
    }


def run_one(symbol, cfg, fn):
    try:
        return fn(symbol, cfg)
    except Exception as e:
        return {"symbol": symbol, "trades": 0,
                "note": f"异常:{repr(e)[:60]}", "trades_detail": []}


# 中金所(CFFEX)金融期货代码——非商品，全市场扫描时默认排除
CFFEX = {"IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"}


def discover_all():
    """自动发现所有有本地日线数据的商品品种（全市场，排除中金所金融期货）。

    只返回 BACKTEST_DIR 下存在 _<SYM>0_daily.csv 且非 CFFEX 的品种。
    """
    bt_dir = getattr(fd, "BACKTEST_DIR", None)
    if not bt_dir:
        return [s for s in fd.SYMBOLS.keys() if s not in CFFEX]
    avail = []
    for sym in fd.SYMBOLS.keys():
        if sym in CFFEX:
            continue
        f = os.path.join(bt_dir, f"_{sym.upper()}0_daily.csv")
        if os.path.exists(f):
            avail.append(sym)
    return avail


def _build_out(targets, mode_str, rows):
    """由已完成品种的 (sym, r_on, r_off) 行构造输出 dict（含汇总）。"""
    out = {
        "targets": targets,
        "mode": mode_str,
        "note": "on=整改后(DEFAULT_CONFIG) off=全部开关False(v12)",
        "rows": [
            {"symbol": s, "on": a, "off": b,
             "delta_expR": round(a["expR"] - b["expR"], 4),
             "delta_win": round(a["win_rate"] - b["win_rate"], 3),
             "delta_dd": round(a["max_dd_R"] - b["max_dd_R"], 3)}
            for s, a, b in rows
        ],
    }
    rs = out["rows"]
    valid = [x for x in rs if x["on"]["trades"] > 0 and x["off"]["trades"] > 0]
    n_imp = sum(1 for x in valid if x["delta_expR"] > 0.02)
    n_dec = sum(1 for x in valid if x["delta_expR"] < -0.02)
    n_flat = len(valid) - n_imp - n_dec
    avg_de = round(sum(x["delta_expR"] for x in valid) / len(valid), 4) if valid else 0.0
    out["summary"] = {"n_total": len(rs), "n_valid": len(valid),
                      "n_improve": n_imp, "n_degrade": n_dec, "n_flat": n_flat,
                      "avg_delta_expR": avg_de}
    return out


def main():
    flags = sys.argv[1:]
    use_all = "--all" in flags
    use_5m = "--5m" in flags
    use_1h = "--1h" in flags
    args = [a for a in flags if not a.startswith("--")]

    # 单实例锁：防止看门狗/自愈重启时双实例写同一结果文件
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".oos_lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[单实例] 已有回测实例运行中，本进程退出（不双跑）", flush=True)
        sys.exit(0)

    if use_1h:
        targets = args if args else FIVE_M_TARGETS
        fn_label = "walk_forward_backtest_5m_exit(1h)"
        backtest_fn = lambda s, c: fd.walk_forward_backtest_5m_exit(s, c, tf="1h")
        out_name = "oos_1h_result.json"
        print("[1h 出场细化] 品种 {} 个（5m resample→1h，日线定信号 + 1h 出场，验证 P-G 尾仓）".format(len(targets)))
    elif use_5m:
        targets = args if args else FIVE_M_TARGETS
        fn_label = fd.walk_forward_backtest_5m_exit.__name__
        backtest_fn = fd.walk_forward_backtest_5m_exit
        out_name = "oos_5m_result.json"
        print("[5m 出场细化] 品种 {} 个（日线定信号 + 5m 出场，验证 P-G 尾仓）".format(len(targets)))
    elif use_all:
        targets = discover_all()
        backtest_fn = fd.walk_forward_backtest
        fn_label = fd.walk_forward_backtest.__name__
        out_name = "oos_compare_all_result.json"
        print(f"[全市场自动发现] 可用品种 {len(targets)} 个（有本地 _XX0_daily.csv）")
    elif args:
        targets = args
        backtest_fn = fd.walk_forward_backtest
        fn_label = fd.walk_forward_backtest.__name__
        out_name = "oos_compare_result.json"
    else:
        targets = DEFAULT_TARGETS
        backtest_fn = fd.walk_forward_backtest
        fn_label = fd.walk_forward_backtest.__name__
        out_name = "oos_compare_result.json"

    # 断点续跑：恢复已完成品种，崩溃/重启后从断点继续，不从头重跑、不卡死循环
    rows = []
    failures = []
    completed_syms = set()
    if os.path.exists(out_name):
        try:
            with open(out_name, encoding="utf-8") as f:
                prev = json.load(f)
            for rr in prev.get("rows", []):
                rows.append((rr["symbol"], rr["on"], rr["off"]))
                completed_syms.add(rr["symbol"])
            print(f"[续跑] 已从 {out_name} 恢复 {len(completed_syms)} 个已完成品种", flush=True)
        except Exception as e:
            print(f"[续跑] 读历史失败，从头跑: {e}", flush=True)
    todo = [s for s in targets if s not in completed_syms]
    print(f"[待跑] 剩余 {len(todo)} 个品种", flush=True)

    cfg_on, cfg_off = make_cfg(True), make_cfg(False)
    print("=" * 80)
    print("四维策略 OOS 对比：整改后(on) vs 整改前(off=v12)")
    print(f"（数据：本地 ｜ 回测：{fn_label} ｜ 品种数：{len(targets)}）")
    print("=" * 80)
    hdr = (f"{'品种':4} {'模式':4} {'笔':>5} {'期望R':>8} {'胜率':>7} "
           f"{'最大回撤R':>9} {'t2率':>6} {'尾仓占':>6}")
    print(hdr)
    print("-" * 80)
    mode_str = "1h_exit" if use_1h else ("5m_exit" if use_5m else ("all" if use_all else "default"))
    signal.signal(signal.SIGALRM, _on_alarm)
    for sym in todo:
        signal.alarm(PER_SYM_TIMEOUT)
        try:
            r_on = summarize(run_one(sym, cfg_on, backtest_fn))
            r_off = summarize(run_one(sym, cfg_off, backtest_fn))
        except TimeoutError:
            print(f"[超时跳过] {sym} 超过 {PER_SYM_TIMEOUT}s 未完成，继续下一品种", flush=True)
            failures.append(sym)
            continue
        except Exception as e:
            print(f"[异常跳过] {sym}: {repr(e)[:200]}，继续下一品种", flush=True)
            failures.append(sym)
            continue
        finally:
            signal.alarm(0)
        rows.append((sym, r_on, r_off))
        for tag, r in (("on ", r_on), ("off", r_off)):
            print(f"{sym:4} {tag:4} {r['trades']:>5} {r['expR']:>8} "
                  f"{r['win_rate']*100:>6.1f}% {r['max_dd_R']:>9} "
                  f"{r['t2_rate']*100:>5.1f}% {r['tail_share']*100:>5.1f}%", flush=True)
        de = r_on['expR'] - r_off['expR']
        dw = (r_on['win_rate'] - r_off['win_rate']) * 100
        dd = r_on['max_dd_R'] - r_off['max_dd_R']
        verdict = "改善▲" if de > 0.02 else ("退化▼" if de < -0.02 else "持平=")
        print(f"   └ ΔexpR={de:+.3f}  Δ胜率={dw:+.1f}pp  Δ回撤={dd:+.2f}R   → {verdict}", flush=True)
        print(flush=True)
        # 增量落盘：每完成一个品种就覆盖写一次，避免卡死/崩溃丢失已完成结果
        out = _build_out(targets, mode_str, rows)
        out["failures"] = failures
        with open(out_name, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    out = _build_out(targets, mode_str, rows)
    out["failures"] = failures
    print("=" * 80, flush=True)
    print(f"结果已写入 {out_name}", flush=True)
    valid = [x for x in out["rows"] if x["on"]["trades"] > 0 and x["off"]["trades"] > 0]
    if valid:
        print(f"汇总（{len(valid)} 个有效品种）：{out['summary']['n_improve']} 改善 / "
              f"{out['summary']['n_degrade']} 退化 / {out['summary']['n_flat']} 持平 "
              f"｜ 平均 ΔexpR={out['summary']['avg_delta_expR']:+.4f}", flush=True)
    if failures:
        print(f"[失败/跳过] {len(failures)} 个: {failures}", flush=True)


if __name__ == "__main__":
    main()
