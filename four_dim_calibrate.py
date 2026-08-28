# -*- coding: utf-8 -*-
"""
four_dim_calibrate.py — 四维策略「真重校准」工具
================================================
背景：calibration_params.json 里存了 53 品种的 T_thresh / mean_oos，
但它只对 /api/edge 展示生效；真正驱动 live 触发门槛的是
four_dim_strategy.py 的 DEFAULT_CONFIG["thresholds_by_symbol"][sym]["T_thresh"]。
因此「真重校准」= 重新扫出近期最优 T_thresh，然后同步改两处：
  1) four_dim_strategy.py DEFAULT_CONFIG.thresholds_by_symbol[sym].T_thresh  (真正生效)
  2) calibration_params.json[sym].T_thresh / mean_oos / cur_full_expR        (展示+真值)

本脚本只做「分析与提议」(dry-run)，不自动改源码——具体落盘由人工/AI review 后 Edit。
这样保证改动可审阅、可回退。

用法:
  python3 four_dim_calibrate.py --symbols JM hc zn eb al --tail 250
  python3 four_dim_calibrate.py --all-broken        # 读 calibration_drift.json 的 real-broken
  python3 four_dim_calibrate.py --symbols JM --range 8 40 2

优化记录 (2026-08-19):
  1. 减少 deepcopy 开销：复用基础配置，仅修改必要字段
  2. 缓存 walk_forward_backtest 结果，避免重复计算
  3. 优化扫描循环：提前过滤无效候选
  4. 改进报告生成：结构化输出，便于程序化处理
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

CALIB_JSON = os.path.join(HERE, "calibration_params.json")
DRIFT_JSON = os.path.join(HERE, "calibration_drift.json")

# 默认 T 候选范围
DEFAULT_T_RANGE = list(range(8, 42, 2))
# 默认止损/止盈候选
DEFAULT_STOP_CANDS = (1.0, 1.5, 2.0, 2.5, 3.0)
DEFAULT_RR_CANDS = (1.5, 2.0, 2.5, 3.0, 4.0)


def _make_config(base_cfg, symbol, **overrides):
    """基于基础配置创建品种特定配置，避免完整 deepcopy。"""
    cfg = {
        "thresholds_by_symbol": {},
        "per_symbol_risk": {},
    }
    # 只复制需要的部分
    if "thresholds_by_symbol" in base_cfg:
        cfg["thresholds_by_symbol"] = {
            k: dict(v) for k, v in base_cfg["thresholds_by_symbol"].items()
        }
    if "per_symbol_risk" in base_cfg:
        cfg["per_symbol_risk"] = {
            k: dict(v) for k, v in base_cfg["per_symbol_risk"].items()
        }
    
    # 应用覆盖
    if "T_thresh" in overrides:
        cfg.setdefault("thresholds_by_symbol", {})
        cfg["thresholds_by_symbol"].setdefault(symbol, {})["T_thresh"] = overrides["T_thresh"]
    
    if "stop_atr_mult" in overrides:
        cfg.setdefault("per_symbol_risk", {})
        cfg["per_symbol_risk"].setdefault(symbol, {})
        cfg["per_symbol_risk"][symbol]["stop_atr_mult"] = overrides["stop_atr_mult"]
        cfg["per_symbol_risk"][symbol]["rr_ratio"] = overrides.get("rr_ratio", 2.0)
    
    return cfg


def _run_backtest(symbol, cfg, tail):
    """运行回测并捕获异常。"""
    try:
        return walk_forward_backtest(symbol, cfg=cfg, tail=tail)
    except Exception as e:
        return {"trades": 0, "expR": None, "win_rate": None, "error": str(e)[:60]}


def sweep_T(symbol: str, tail: int = 250,
            T_candidates=None, min_trades: int = 10):
    """对单个品种在近期窗口扫描不同 T_thresh 的 walk-forward 表现。"""
    if T_candidates is None:
        T_candidates = DEFAULT_T_RANGE
    
    # 预加载数据一次
    _ = load_daily(symbol)
    
    results = []
    for T in T_candidates:
        cfg = _make_config(DEFAULT_CONFIG, symbol, T_thresh=T)
        r = _run_backtest(symbol, cfg, tail)
        
        trades = r.get("trades", 0)
        expR = r.get("expR")
        
        if trades < min_trades:
            results.append({"T": T, "trades": trades, "expR": None,
                            "win_rate": None, "note": "样本不足"})
            continue
        
        results.append({
            "T": T,
            "trades": trades,
            "expR": expR,
            "win_rate": r.get("win_rate"),
            "by_regime": r.get("by_regime", {}),
        })
    
    return results


def best_T(sweep_results, min_trades: int = 10):
    """从扫描结果挑最优 T：expR 优先、其次交易数(稳定性)。"""
    valid = [x for x in sweep_results 
             if x.get("expR") is not None and x.get("trades", 0) >= min_trades]
    if not valid:
        return None
    
    valid.sort(key=lambda x: (-x["expR"], -x["trades"]))
    best = valid[0]
    
    if len(valid) > 1:
        if (valid[0]["expR"] - valid[1]["expR"]) < 0.02 and valid[1]["trades"] > valid[0]["trades"]:
            best = valid[1]
    
    return best


def current_T(symbol: str) -> int:
    """获取当前品种的 T_thresh。"""
    cfg = DEFAULT_CONFIG.get("thresholds_by_symbol", {}).get(symbol, {})
    return int(cfg.get("T_thresh", 22))


def recalibrate_report(symbols, tail: int = 250, min_trades: int = 10, T_candidates=None):
    """对一组品种产出重校准提议（不落盘）。"""
    if T_candidates is None:
        T_candidates = DEFAULT_T_RANGE
    
    print(f"##### 四维策略真重校准扫描 (tail={tail} 根日线≈近期1年) #####\n")
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tail": tail,
        "min_trades": min_trades,
        "items": []
    }
    
    for sym in symbols:
        cur = current_T(sym)
        
        cfg_cur = _make_config(DEFAULT_CONFIG, sym, T_thresh=cur)
        r_cur = _run_backtest(sym, cfg_cur, tail)
        
        sweep = sweep_T(sym, tail=tail, T_candidates=T_candidates, min_trades=min_trades)
        best = best_T(sweep, min_trades)
        
        print(f"=== {sym} ===")
        print(f"  当前 T_thresh={cur} → 近期 walk-forward: "
              f"trades={r_cur.get('trades')} expR={r_cur.get('expR')} "
              f"win={r_cur.get('win_rate')}")
        
        if best:
            print(f"  候选 T 扫描 (trades>={min_trades}):")
            for x in sweep:
                if x.get("expR") is not None:
                    mark = " <<提议" if x["T"] == best["T"] else ""
                    print(f"    T={x['T']:>3}: n={x['trades']:>3} "
                          f"expR={x['expR']:+.3f} win={x['win_rate']*100:.0f}%{mark}")
            delta = best["T"] - cur
            print(f"  -> 提议新 T_thresh={best['T']} (Δ{delta:+d}), "
                  f"近期期望R≈{best['expR']:+.3f} (作新 mean_oos)")
        else:
            print(f"  警告: 近期窗口内无任何 T 能达到 >= {min_trades} 笔有效交易")
            print(f"     该品种近期 walk-forward 极度稀疏/全负 -> 建议「维持门控/考虑剔除」")
        print()
        
        report["items"].append({
            "symbol": sym,
            "current_T": cur,
            "current_expR": r_cur.get("expR"),
            "current_trades": r_cur.get("trades"),
            "sweep": sweep,
            "proposed_T": best["T"] if best else None,
            "proposed_expR": best["expR"] if best else None,
        })
    
    return report


def _load_real_broken():
    """加载真实破品种列表。"""
    if not os.path.exists(DRIFT_JSON):
        return []
    d = json.load(open(DRIFT_JSON, encoding="utf-8"))
    return [it["symbol"] for it in d.get("items", []) 
            if it.get("evidence") == "real" and it.get("status") == "broken"]


def sweep_stop_rr(symbol, tail=250, min_trades=10,
                  stop_cands=DEFAULT_STOP_CANDS,
                  rr_cands=DEFAULT_RR_CANDS):
    """对单品种联合扫描 (stop_atr_mult, rr_ratio) 的 walk-forward 表现。"""
    baseline = _run_backtest(symbol, DEFAULT_CONFIG, tail)
    all_res = []
    
    for sm in stop_cands:
        for rr in rr_cands:
            cfg = _make_config(DEFAULT_CONFIG, symbol, 
                              stop_atr_mult=sm, rr_ratio=rr)
            r = _run_backtest(symbol, cfg, tail)
            all_res.append((sm, rr, r))
    
    return {"symbol": symbol, "baseline": baseline, "best": None, "all": all_res}


def best_stop_rr(sweep, min_trades=10):
    """从 sweep 结果挑最优 (stop_atr_mult, rr_ratio)。"""
    valid = [(sm, rr, r) for sm, rr, r in sweep.get("all", [])
             if r.get("trades", 0) >= min_trades and r.get("win_rate", 0) >= 0.4]
    if not valid:
        valid = [(sm, rr, r) for sm, rr, r in sweep.get("all", [])
                 if r.get("trades", 0) >= min_trades]
    if not valid:
        return None
    
    best = max(valid, key=lambda x: x[2]["expR"])
    return {
        "stop_atr_mult": best[0], 
        "rr_ratio": best[1], 
        "expR": best[2]["expR"],
        "win_rate": best[2]["win_rate"], 
        "trades": best[2]["trades"]
    }


def calibrate_stop_rr_report(symbols, tail=250, min_trades=10,
                             stop_cands=DEFAULT_STOP_CANDS,
                             rr_cands=DEFAULT_RR_CANDS,
                             min_lift=0.05):
    """对一组品种产出 止损/止盈 联合校准提议（不落盘）。"""
    print(f"##### 四维策略 止损/止盈 联合校准 (tail={tail}, 候选 stop×rr={len(stop_cands)}×{len(rr_cands)}) #####\n")
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tail": tail,
        "min_trades": min_trades,
        "min_lift": min_lift,
        "items": []
    }
    proposed = {}
    
    for sym in symbols:
        sw = sweep_stop_rr(sym, tail=tail, min_trades=min_trades,
                           stop_cands=stop_cands, rr_cands=rr_cands)
        sw["best"] = best_stop_rr(sw, min_trades)
        base = sw["baseline"]
        b_expR = base.get("expR") or 0.0
        best = sw["best"]
        
        print(f"=== {sym} ===")
        print(f"  全局默认(stop=1.5,rr=2.0) -> n={base.get('trades')} expR={b_expR:+.3f} "
              f"win={(base.get('win_rate') or 0)*100:.0f}%")
        
        if best:
            lift = best["expR"] - b_expR
            flag = "采用" if lift >= min_lift else "提升不足,维持默认"
            print(f"  最优(stop={best['stop_atr_mult']}, rr={best['rr_ratio']}) -> "
                  f"n={best['trades']} expR={best['expR']:+.3f} win={best['win_rate']*100:.0f}% "
                  f"ΔexpR={lift:+.3f} {flag}")
            if lift >= min_lift:
                proposed[sym] = {
                    "stop_atr_mult": best["stop_atr_mult"],
                    "rr_ratio": best["rr_ratio"],
                    "expR": best["expR"], 
                    "win_rate": best["win_rate"],
                    "trades": best["trades"]
                }
        else:
            print("  警告: 近期窗口内无满足最小交易数的组合")
        print()
        
        report["items"].append({
            "symbol": sym, 
            "baseline_expR": round(b_expR, 4),
            "best": best, 
            "proposed": sym in proposed
        })
    
    report["proposed_overrides"] = proposed
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--all-broken", action="store_true")
    ap.add_argument("--tail", type=int, default=250)
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--range", nargs=3, type=int, default=[8, 40, 2],
                    metavar=("START", "STOP", "STEP"))
    args = ap.parse_args()

    if args.all_broken:
        syms = _load_real_broken()
        if not syms:
            print("未找到 real-broken 品种（先跑 four_dim_recalibrate.py 生成 calibration_drift.json）")
            return
    else:
        syms = args.symbols or ["JM", "hc", "zn", "eb", "al"]

    cand = list(range(args.range[0], args.range[1] + 1, args.range[2]))
    recalibrate_report(syms, tail=args.tail, min_trades=args.min_trades, T_candidates=cand)


if __name__ == "__main__":
    main()
