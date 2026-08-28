# -*- coding: utf-8 -*-
"""
four_dim_recalibrate.py · 四维策略「在线自适应重校准」之漂移检测（P1-4）
=========================================================================
问题背景：calibration_params.json 是**静态**校准（某历史窗口的 OOS 期望）。
Regime 漂移后参数逐渐失效（JM 焦煤实盘崩的根因之一：校准是历史某段，
近期 regime 变了却没重校）。本脚本做**漂移检测 + 候选新参数产出**，
但不直接覆盖线上 calibration_params.json（需人工确认才落盘，避免自作主张）。

数据源（关键修复 v2）：
  1. **papertrack 真实近期表现**（首选）：从 papertrack_report.json 的冻结交易里，
     取每个品种最近 PT_WINDOW 笔已判定交易，算真实 expR / 胜率。这是金标准——
     样本充足（JM 26/hc 21/FG 14…）、确定性强（来自已固化的回测快照）。
  2. **walk_forward 回测**（补充）：仅对 papertrack 缺覆盖的品种，跑 tail=WF_TAIL
     日线 walk-forward 得模型近期 expR。P0-2 换月跳空已修复，回测干净。
  原 v1 只用了 walk-forward 且硬性要求 ≥10 笔，而 walk-forward(tail=120) 每品种仅
  出 1-7 笔 → 全部被判 insufficient，P1-4 形同虚设。v2 改为真实优先 + 门槛降到
  CONF_MIN=5（低于则仍给估计但标低置信）。

判定逻辑：
  · cur_expR < 0                         → broken（近期期望为负，必须重校）
  · cur_expR < mean_oos * DRIFT_FACTOR   → drift（衰减超半）
  · 校准本身为负且 cur < mean_oos        → drift（比已负校准更差）
  · papertrack 真实门控已触发(gated)     → 至少 drift（真实亏佐证）
  · 其余                                  → healthy
  gated 且 cur<0 升级 broken；gated 但 cur 正 → 强制 drift（真实近期失败）。

用法：
  python3 four_dim_recalibrate.py            # 漂移检测 + 产出建议(不写回)
  python3 four_dim_recalibrate.py --apply    # 仅对高置信品种更新 cur_full_expR 观测字段(备份原文件)
（默认不写回线上决策参数 T_thresh 等；--apply 只更新观测字段并备份）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CALIB_JSON = os.path.join(HERE, "calibration_params.json")
DRIFT_JSON = os.path.join(HERE, "calibration_drift.json")
PAPERTRACK_JSON = os.path.join(HERE, "papertrack_report.json")

# papertrack 取最近 N 笔实判交易算近期 expR
PT_WINDOW = 15
# 近期样本≥此数才用于正式判定（否则标 insufficient 但仍给估计）
CONF_MIN = 5
# walk-forward 回测窗口（日线根数，~1 年）：足够累积 ≥5 笔交易
WF_TAIL = 250
# 漂移判定系数：当前 expR < mean_oos * 此值 → 视为漂移/失效
DRIFT_FACTOR = 0.5


def load_calib():
    if not os.path.exists(CALIB_JSON):
        return {}
    try:
        with open(CALIB_JSON, "r", encoding="utf-8") as _f:
            return json.load(_f)
    except Exception as e:
        print(f"[recalibrate] 加载 CALIB_JSON 失败(非缺失): {e}", flush=True)
        return {}


def load_papertrack_trades():
    """读 papertrack 已判定交易（冻结、确定性），供近期真实表现计算。失败返回 []。"""
    if not os.path.exists(PAPERTRACK_JSON):
        return []
    try:
        with open(PAPERTRACK_JSON, "r", encoding="utf-8") as _f:
            rep = json.load(_f)
        return [t for t in rep.get("trades", []) if t.get("status") == "done"]
    except Exception as e:
        print(f"[recalibrate] 加载 PAPERTRACK_JSON 失败(非缺失): {e}", flush=True)
        return []


def load_papertrack_gates():
    """读 papertrack 近期真实门控状态，作交叉验证。失败返回 {}。"""
    try:
        import four_dim_papertrack as pt

        return pt.compute_symbol_gates()
    except Exception as e:
        print(f"[recalibrate] 加载 papertrack gates 失败: {e}", flush=True)
        return {}


def papertrack_recent(trades_all: list, sym: str, window: int = PT_WINDOW) -> dict | None:
    """取某品种最近 window 笔已判定交易，算真实 expR / 胜率 / 累计R。无样本返回 None。"""
    ts = [t for t in trades_all if t.get("symbol") == sym]
    ts = sorted(ts, key=lambda x: x.get("time") or "")
    recent = ts[-window:]
    n = len(recent)
    if n == 0:
        return None
    Rs = [float(t.get("R", 0.0) or 0.0) for t in recent]
    expR = sum(Rs) / n
    wr = sum(1 for r in Rs if r > 0) / n
    return {"n": n, "expR": expR, "win_rate": wr, "cum_R": sum(Rs)}


def _status_of(cur_expR, mean_oos, gated):
    """按判定逻辑返回 (status, conf_broken_flag)。cur_expR 可能为 None。"""
    if cur_expR is None:
        return "insufficient"
    if cur_expR < 0:
        return "broken"
    if mean_oos > 0 and cur_expR < mean_oos * DRIFT_FACTOR:
        return "drift"
    if mean_oos <= 0 and cur_expR < mean_oos:
        return "drift"
    if gated:
        return "drift"
    return "healthy"


# —— #3 漂移闭环：对高置信漂移/失效品种自动产出候选 T_thresh（不自动落盘，需人工一键apply）——
STAGE_TAIL = 250  # walk-forward 扫描窗口（日线根数，~1年）
STAGE_MIN_TRADES = 10  # 候选 T 需达到的最小交易数（防过拟合/稀疏误判）


def compute_proposed_T(sym, tail=STAGE_TAIL, min_trades=STAGE_MIN_TRADES):
    """对单品种跑近期窗口 walk-forward T 扫描，产出提议新 T_thresh。
    成功返回 (proposed_T:int, proposed_expR:float|None)；无有效候选返回 None。
    仅在漂移/失效高置信品种上调用，避免全量开销。"""
    try:
        import four_dim_calibrate as fdc

        rep = fdc.recalibrate_report([sym], tail=tail, min_trades=min_trades)
        items = rep.get("items") or []
        if not items:
            return None
        pT = items[0].get("proposed_T")
        if pT is None:
            return None
        return (int(pT), items[0].get("proposed_expR"))
    except Exception as e:
        print(f"[staging] {sym} 候选扫描异常(忽略): {repr(e)[:120]}")
        return None


def stage_candidates(results, calib, tail=STAGE_TAIL, symbols=None):
    """就地给高置信 drift/broken 品种补 proposed_T 候选（写入 results 对应项的 proposed_* 字段）。
    symbols 为非空集合时仅对该集合内品种 staging（控开销，避免全市场扫描）。返回 staged 数量。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allow = set(symbols) if symbols else None
    staged = 0
    for r in results:
        if allow and r["symbol"] not in allow:
            continue
        if r.get("status") not in ("drift", "broken"):
            continue
        if r.get("confidence") != "high" or r.get("status") == "skip":
            continue
        if r.get("proposed_T") is not None:
            continue  # 已 staged
        cand = compute_proposed_T(r["symbol"], tail=tail)
        if not cand:
            continue
        pT, pExpR = cand
        cur_T = (calib.get(r["symbol"], {}) or {}).get("T_thresh")
        r["proposed_T"] = pT
        r["proposed_expR"] = round(pExpR, 4) if pExpR is not None else None
        r["proposed_note"] = (
            f"漂移候选：当前T={cur_T}→提议{pT}（近期walk-forward期望≈{pExpR:.3f}）；需人工一键apply落盘"
        )
        r["staged_at"] = now
        staged += 1
    return staged


def main(apply=False, stage=False, stage_symbols=None):
    import four_dim_strategy as fd

    calib = load_calib()
    if not calib:
        print("⚠️ 无 calibration_params.json，无法做漂移检测。")
        return

    trades_all = load_papertrack_trades()
    gates = load_papertrack_gates()
    print("=== 四维策略 · 校准漂移检测（P1-4 v2）===")
    print(
        f"papertrack 近期窗口={PT_WINDOW} 笔 | walk-forward tail={WF_TAIL} | 漂移系数={DRIFT_FACTOR} | 置信门槛={CONF_MIN}"
    )
    print(f"papertrack 可用实判交易: {len(trades_all)} 笔")
    print(f"报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    results = []
    for sym, c in calib.items():
        # 跳过真正的空占位项：既无校准(mean_oos/T_thresh) 又无 papertrack 真实交易
        pt_preview = papertrack_recent(trades_all, sym)
        if "note" in c and "mean_oos" not in c and "T_thresh" not in c and pt_preview is None:
            results.append(
                {
                    "symbol": sym,
                    "status": "skip",
                    "note": c.get("note", "无候选"),
                    "calibrated_oos": None,
                    "current_expR": None,
                    "current_win_rate": None,
                    "current_trades": 0,
                    "source": "none",
                    "confidence": "n/a",
                    "evidence": "n/a",
                    "papertrack_gated": False,
                    "suggestion": "无校准候选且无真实交易，跳过",
                }
            )
            continue
        # note-only 但确有真实交易(如实盘 SA/lh) → 仍评估(mean_oos 视为 0)
        if "mean_oos" not in c:
            c = dict(c)
            c["mean_oos"] = 0.0

        mean_oos = float(c.get("mean_oos", 0.0) or 0.0)

        # ① 真实近期表现（首选）
        pt = papertrack_recent(trades_all, sym)
        # ② walk-forward 仅对 papertrack 缺覆盖的品种补充
        wf = None
        if pt is None or pt["n"] < CONF_MIN:
            try:
                wf = fd.walk_forward_backtest(sym, fd.DEFAULT_CONFIG, tail=WF_TAIL)
            except Exception:
                wf = None

        # 选数据源
        if pt and pt["n"] >= CONF_MIN:
            cur_expR, cur_wr, cur_n, src = pt["expR"], pt["win_rate"], pt["n"], "papertrack"
        elif wf and int(wf.get("trades", 0) or 0) >= CONF_MIN:
            cur_expR, cur_wr, cur_n, src = (
                float(wf.get("expR") or 0.0),
                float(wf.get("win_rate") or 0.0),
                int(wf.get("trades", 0) or 0),
                "walk_forward",
            )
        elif pt:
            cur_expR, cur_wr, cur_n, src = pt["expR"], pt["win_rate"], pt["n"], "papertrack(低样本)"
        elif wf:
            cur_expR, cur_wr, cur_n, src = (
                float(wf.get("expR") or 0.0),
                float(wf.get("win_rate") or 0.0),
                int(wf.get("trades", 0) or 0),
                "walk_forward(低样本)",
            )
        else:
            cur_expR, cur_wr, cur_n, src = None, None, 0, "none"

        gate = gates.get(sym, {})
        gated = bool(gate.get("gated"))

        # 判定
        status = _status_of(cur_expR, mean_oos, gated)
        # gated 升级
        if gated and status == "healthy":
            status = "drift"
        if gated and cur_expR is not None and cur_expR < 0:
            status = "broken"

        conf = "high" if cur_n >= CONF_MIN else "low"

        suggestion = {
            "healthy": "维持当前参数（近期表现符合校准）",
            "drift": "近期衰减/真实门控触发，建议滚动重校（跑 four_dim_calibrate 近期窗口）",
            "broken": "近期期望已转负，尽快重校；过渡期靠动态门控暂停发信号",
            "insufficient": f"近期样本{cur_n}<{CONF_MIN}，结论不可靠，仅参考估计值",
            "skip": "无校准候选，跳过",
        }.get(status, "")

        results.append(
            {
                "symbol": sym,
                "status": status,
                "calibrated_oos": round(mean_oos, 4),
                "current_expR": round(cur_expR, 4) if cur_expR is not None else None,
                "current_win_rate": round(cur_wr, 3) if cur_wr is not None else None,
                "current_trades": cur_n,
                "source": src,
                "evidence": "real" if src.startswith("papertrack") else "model",
                "confidence": conf,
                "papertrack_gated": gated,
                "suggestion": suggestion,
                "proposed_T": None,
                "proposed_expR": None,
                "proposed_note": None,
                "staged_at": None,
            }
        )

    # 排序：broken > drift > insufficient > skip > healthy
    order = {"broken": 0, "drift": 1, "insufficient": 2, "skip": 3, "healthy": 4}
    results.sort(key=lambda x: (order.get(x["status"], 9), x["symbol"]))

    print(
        f"{'品种':5} {'状态':10} {'校准OOS':>8} {'当前expR':>9} {'胜率':>6} {'笔数':>4} {'证据':6} {'源':14} {'置':>3} {'门控':>5}"
    )
    for r in results:
        tag = {
            "healthy": "✅健康",
            "drift": "⚠️漂移",
            "broken": "❌失效",
            "insufficient": "⏳样本少",
            "skip": "⏭跳过",
        }.get(r["status"], r["status"])
        print(
            f"{r['symbol']:5} {tag:10} {str(r['calibrated_oos']):>8} "
            f"{str(r['current_expR']):>9} {str(r['current_win_rate']):>6} "
            f"{r['current_trades']:>4} {r['evidence']:6} {r['source']:14} "
            f"{r['confidence'][:1]:>3} {'🚫' if r.get('papertrack_gated') else '-':>5}"
        )
    print()
    for r in results:
        if r["status"] in ("drift", "broken", "insufficient"):
            print(f"  • {r['symbol']} [{r['confidence']}置信/{r['evidence']}证据/{r['source']}]: {r['suggestion']}")

    # #3 闭环：对高置信漂移/失效品种自动产出候选 T_thresh（仅 staged，不落盘）
    staged_count = 0
    if stage:
        staged_count = stage_candidates(results, calib, symbols=stage_symbols)
        if staged_count:
            _who = f"关注集{list(stage_symbols)}" if stage_symbols else "全部"
            print(f"\n[staging] 已为 {staged_count} 个品种({_who})自动产出候选 T_thresh（待人工一键apply）")

    # 落盘（仅产出建议，不覆盖线上决策参数）
    real_broken = [r["symbol"] for r in results if r["status"] == "broken" and r["evidence"] == "real"]
    model_broken = [r["symbol"] for r in results if r["status"] == "broken" and r["evidence"] == "model"]
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "pt_window": PT_WINDOW,
            "wf_tail": WF_TAIL,
            "drift_factor": DRIFT_FACTOR,
            "conf_min": CONF_MIN,
        },
        "summary": {
            "healthy": sum(1 for r in results if r["status"] == "healthy"),
            "drift": sum(1 for r in results if r["status"] == "drift"),
            "broken": sum(1 for r in results if r["status"] == "broken"),
            "broken_real": real_broken,
            "broken_model": model_broken,
            "insufficient": sum(1 for r in results if r["status"] == "insufficient"),
            "skip": sum(1 for r in results if r["status"] == "skip"),
            "staged": staged_count,
        },
        "items": results,
    }
    with open(DRIFT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n漂移报告已写入: {DRIFT_JSON}")

    if apply:
        # 仅更新观测字段 cur_full_expR（=近期高置信 expR），并备份原文件；不改 T_thresh 等决策参数
        bak = CALIB_JSON + f".bak_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy(CALIB_JSON, bak)
        updated = 0
        for r in results:
            sym = r["symbol"]
            if sym in calib and r["current_expR"] is not None and r["confidence"] == "high" and r["status"] != "skip":
                calib[sym]["cur_full_expR"] = r["current_expR"]
                updated += 1
        with open(CALIB_JSON, "w", encoding="utf-8") as f:
            json.dump(calib, f, ensure_ascii=False, indent=2)
        print(f"--apply: 已更新 {updated} 个品种 cur_full_expR 观测字段并备份原文件 -> {bak}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="仅更新高置信品种的观测字段 cur_full_expR（备份原文件）")
    args = ap.parse_args()
    main(apply=args.apply)
