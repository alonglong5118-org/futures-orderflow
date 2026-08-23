#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase C2：全市场 45+ 品种 5m 出场批量回测（日线定信号 + 5m 出场，验证 P-G 尾仓）。

on  = make_cfg(True)  (整改后 DEFAULT_CONFIG, P-A~P-H 全开)
off = make_cfg(False) (v12, 五块开关全 False)
复用 four_dim_oos_compare 的 make_cfg/run_one/summarize/_build_out。

输出：oos_5m_45_result.json（结构同 oos_5m_result.json，扩展到全市场）。
单品种超时保护；可重入（先恢复已完成）。
"""
import json
import os
import sys
import signal
import gc
import argparse
import four_dim_strategy as fd
import four_dim_oos_compare as oc

HERE = os.path.dirname(os.path.abspath(__file__))
PER_SYM_TIMEOUT = 600

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=os.path.join(HERE, "oos_5m_45_result.json"),
                help="本分块结果文件(并行分块时各任务独立落盘, 避免互相覆盖)")
ap.add_argument("--only", default="", help="仅跑这些品种, 逗号分隔(分块用)")
args = ap.parse_args()
OUT = args.out
only_set = {s.strip() for s in args.only.split(",") if s.strip()}

rep = json.load(open(f"{HERE}/_convert_min5_report.json"))["report"]
targets = [k for k, v in rep.items() if v.get("ok")]
if only_set:
    targets = [t for t in targets if t in only_set]
print(f"=== Phase C2 分块：{len(targets)} 品种 (out={os.path.basename(OUT)}) on/off ===\n", flush=True)

def _on_alarm(signum, frame):
    raise TimeoutError("per-symbol timeout")

# 恢复已完成
rows = []
completed = set()
if os.path.exists(OUT):
    try:
        prev = json.load(open(OUT, encoding="utf-8"))
        for rr in prev.get("rows", []):
            rows.append((rr["symbol"], rr["on"], rr["off"]))
            completed.add(rr["symbol"])
        print(f"[续跑] 已恢复 {len(completed)} 个\n", flush=True)
    except Exception as e:
        print(f"[续跑] 失败: {e}", flush=True)

cfg_on, cfg_off = oc.make_cfg(True), oc.make_cfg(False)
todo = [s for s in targets if s not in completed]
print(f"[待跑] 剩余 {len(todo)} 个\n", flush=True)

signal.signal(signal.SIGALRM, _on_alarm)
for sym in todo:
    # 5m 可用性快速校验
    if fd.load_min5(sym, fetch_if_missing=False) is None:
        print(f"  {sym:4} 跳过(无5m)", flush=True)
        continue
    signal.alarm(PER_SYM_TIMEOUT)
    try:
        r_on = oc.run_one(sym, cfg_on, fd.walk_forward_backtest_5m_exit)
        r_off = oc.run_one(sym, cfg_off, fd.walk_forward_backtest_5m_exit)
        a, b = oc.summarize(r_on), oc.summarize(r_off)
        name = fd.SYMBOLS.get(sym, {}).get("name", sym)
        grp = fd.SYMBOLS.get(sym, {}).get("group", "?")
        rows.append((sym, a, b))
        print(f"  {sym:4} {name:4}({grp}) on笔={a['trades']:>4} on期望R={a['expR']:>7} "
              f"off笔={b['trades']:>4} off期望R={b['expR']:>7} Δ={a['expR']-b['expR']:>7}", flush=True)
        # 增量落盘
        out = oc._build_out(targets, "5m_exit_45", rows)
        json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2, default=str)
    except TimeoutError:
        print(f"  {sym:4} 超时跳过", flush=True)
    except Exception as e:
        print(f"  {sym:4} 异常跳过: {type(e).__name__}: {str(e)[:80]}", flush=True)
    finally:
        signal.alarm(0)
        # 每品种强制释放，避免跨符号内存累积撑爆 sandbox
        try:
            del r_on, r_off, a, b
        except NameError:
            pass
        gc.collect()

out = oc._build_out(targets, "5m_exit_45", rows)
json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2, default=str)
s = out["summary"]
print(f"\n=== 完成：{s['n_valid']}/{s['n_total']} 有效 | 改善 {s['n_improve']} / 退化 {s['n_degrade']} / 持平 {s['n_flat']} | 平均Δ期望R={s['avg_delta_expR']} ===")
print(f"报告已写 {OUT}")
