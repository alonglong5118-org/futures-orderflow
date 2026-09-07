# -*- coding: utf-8 -*-
"""
被拦交易质检（oppose 门 live 复盘 / 调参支撑）
================================================
方法：
  - 对每个有 kline C 的品种，分别跑 基线(threshold, dragon, 门OFF) 与 oppose 门(kline, 门ON)。
  - 按 (entry_date, dir) 求差：基线有交易、门版没有 → 被门拦掉的交易。
  - 被拦交易的 expR（来自基线）即「若不拦本该的盈亏」：
      * mean(blocked expR) < 0  → 门正确移除了亏损交易（拦得对）
      * mean(blocked expR) > 0  → 门误伤了盈利交易（过度抑制）
  - 同时算 拦截率 = n_blocked / n_baseline，判断是否过激。
  - 扫 30/40/50 三档阈值，给调参直接证据。

输出：oos_c_gate_blockquality.json（聚合 + 逐品种 + 逐阈值）
"""
import os, json, copy
import numpy as np
import four_dim_strategy as fd

HERE = os.path.dirname(os.path.abspath(__file__))

def run_backtest(sym, c_source, c_gate=None, thr=30.0):
    cfg = copy.deepcopy(fd.DEFAULT_CONFIG)
    cfg.setdefault("bias_synthesis", {})["c_source"] = c_source
    if c_gate:
        cfg["bias_synthesis"]["c_gate"] = c_gate
        cfg["bias_synthesis"]["c_gate_threshold"] = thr
    return fd.walk_forward_backtest(sym, cfg=cfg)

def _key(t):
    return (t.get("entry_date"), t.get("dir"))

def main():
    cache = {}
    try:
        with open(os.path.join(HERE, "cflow_kline_cache.json"), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    # fd.SYMBOLS 键：jd/lh 小写，其它大写；跳过合成 SA01 与禁用
    disabled = set(getattr(fd, "DISABLED_SYMBOLS", []) or [])
    targets = []
    for s in fd.SYMBOLS:
        if s in ("SA01",) or s in disabled:
            continue
        if s.upper() in cache:
            targets.append(s)
    targets.sort()
    print(f"[质检] 门可用品种 {len(targets)} 个", flush=True)

    thr_grid = [30.0, 40.0, 50.0]
    agg = {str(int(t)): {"n_blocked": 0, "n_baseline": 0, "sum_blocked_expR": 0.0,
                          "sum_allowed_expR": 0.0, "n_allowed": 0,
                          "blocked_loser": 0, "blocked_winner": 0,
                          "symbols": []} for t in thr_grid}

    per_symbol = []
    for sym in targets:
        r_base = run_backtest(sym, "dragon", None)
        base_td = r_base.get("trades_detail") or []
        if not base_td:
            continue
        base_map = {_key(t): t for t in base_td}

        rec = {"symbol": sym, "n_baseline": len(base_td), "thr": {}}
        for thr in thr_grid:
            r_ot = run_backtest(sym, "kline", "oppose_threshold", thr)
            ot_td = r_ot.get("trades_detail") or []
            ot_set = set(_key(t) for t in ot_td)
            blocked = [base_map[k] for k in base_map if k not in ot_set]
            allowed = [t for t in ot_td]
            b_exp = [float(t.get("R") or t.get("expR") or 0.0) for t in blocked]
            a_exp = [float(t.get("R") or t.get("expR") or 0.0) for t in allowed]
            a = agg[str(int(thr))]
            a["n_blocked"] += len(blocked)
            a["n_baseline"] += len(base_td)
            a["sum_blocked_expR"] += sum(b_exp)
            a["sum_allowed_expR"] += sum(a_exp)
            a["n_allowed"] += len(allowed)
            a["blocked_loser"] += sum(1 for x in b_exp if x < 0)
            a["blocked_winner"] += sum(1 for x in b_exp if x > 0)
            rec["thr"][str(int(thr))] = {
                "n_blocked": len(blocked),
                "block_rate": round(len(blocked) / len(base_td), 3),
                "blocked_mean_expR": round(float(np.mean(b_exp)), 4) if b_exp else None,
                "blocked_sum_expR": round(float(sum(b_exp)), 3),
                "allowed_mean_expR": round(float(np.mean(a_exp)), 4) if a_exp else None,
                "blocked_loser_ratio": round(sum(1 for x in b_exp if x < 0) / len(b_exp), 3) if b_exp else None,
            }
        per_symbol.append(rec)

    # 聚合统计
    for tk, a in agg.items():
        nb, nbase = a["n_blocked"], a["n_baseline"]
        a["block_rate"] = round(nb / nbase, 3) if nbase else None
        a["blocked_mean_expR"] = round(a["sum_blocked_expR"] / nb, 4) if nb else None
        a["allowed_mean_expR"] = round(a["sum_allowed_expR"] / a["n_allowed"], 4) if a["n_allowed"] else None
        a["blocked_loser_ratio"] = round(a["blocked_loser"] / nb, 3) if nb else None
        # 净效果：若拦掉的交易总和 expR 为负 → 门在移除亏损（好）
        a["skipped_pnl_expR"] = round(a["sum_blocked_expR"], 3)
        del a["sum_blocked_expR"], a["sum_allowed_expR"], a["symbols"]

    out = {"agg_by_threshold": agg, "per_symbol": per_symbol,
           "notes": "blocked_mean_expR<0 且 blocked_loser_ratio>0.5 → 门拦得对；"
                    "block_rate 过高(>0.4) 提示阈值偏激进。skipped_pnl_expR<0 表示被拦交易整体是亏损的(好)。"}
    with open(os.path.join(HERE, "oos_c_gate_blockquality.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ── 控制台摘要 ──
    print("=" * 86, flush=True)
    print(f"{'阈值':>4} {'拦截率':>7} {'被拦均R':>8} {'放行均R':>8} {'被拦亏损占比':>11} {'被拦交易总R':>11}")
    for tk, a in agg.items():
        print(f"{tk:>4} {str(a['block_rate']):>7} {str(a['blocked_mean_expR']):>8} "
              f"{str(a['allowed_mean_expR']):>8} {str(a['blocked_loser_ratio']):>11} {a['skipped_pnl_expR']:>11}")
    print("=" * 86, flush=True)
    # 重点品种（live 已拦过）
    focus = [s for s in per_symbol if s["symbol"] in ("ss", "J", "SR", "bu", "JM", "FG")]
    print("\n[重点品种·被拦交易质量]")
    for s in focus:
        line = f"  {s['symbol']:4s} 基线{s['n_baseline']}笔 |"
        for tk in ("30", "40", "50"):
            d = s["thr"][tk]
            line += f"  thr{tk}:拦{d['n_blocked']}({d['block_rate']:.0%})均R={d['blocked_mean_expR']}"
        print(line, flush=True)
    print(f"\n[已写] oos_c_gate_blockquality.json", flush=True)

if __name__ == "__main__":
    main()
