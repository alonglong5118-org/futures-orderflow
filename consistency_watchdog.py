# -*- coding: utf-8 -*-
"""
consistency_watchdog.py · #5 训练/服务一致性看门狗（train/serve parity）
=========================================================================
问题：#3 让 live 的 calibration_params.json 可被「一键 apply」改写 T_thresh，也允许人工重校。
但改写后 live 服务的参数可能已偏离「最后一次 OOS 校验基线」(four_dim_strategy.DEFAULT_CONFIG
的 thresholds_by_symbol)，或某品种根本没有 mean_oos(从未被校验) 却在服务。这种「训练-服务
不一致」是实盘埋雷：你以为在跑校验过的模型，实际在跑一个偏离基线、未复验的参数。

本看门狗只「报告、不修正」（与 #3 红线一致：不擅自改线上参数），产出结构化差异清单：
  1. train_serve_divergence：每个关注品种的 校验基线T(DEFAULT_CONFIG) vs 服务T(calibration_params)，
     偏离超 DEVIATE_PCT 则标 needs_revalidation。
  2. unvalidated：关注品种在 calibration_params 中缺 mean_oos（从未被 OOS 校验，用默认 T 在服务）。
  3. broken_serving：漂移判 broken 且未禁用、且未被动态门控压制 → 真在服务一个失效模型（计入 ok=false）。
  3b. broken_gated：漂移判 broken 但已被动态门控 papertrack_gated 压制（不发信号）→ 风险已控，仅提示（不计入 ok=false）。
  4. stale：recalibrated_at 超过 STALE_DAYS 天未刷新 → 建议重校。
"""
import json
import os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CALIB_FILE = os.path.join(HERE, "calibration_params.json")
DRIFT_FILE = os.path.join(HERE, "calibration_drift.json")

DEVIATE_PCT = 0.35      # 服务T 相对基线T 偏离 >35% 视为需复验
STALE_DAYS = 30         # recalibrated_at 超过 30 天未刷新视为陈旧
RECAL_GRACE_DAYS = 7    # 近期已主动重校(apply)的品种，偏离基线属有意行为，不重复报 divergence


def _load_calib():
    if not os.path.exists(CALIB_FILE):
        return {}
    try:
        with open(CALIB_FILE, encoding="utf-8") as _f:
            return json.load(_f) or {}
    except Exception as e:
        print(f"[consistency_watchdog] 加载 CALIB_FILE 失败(非缺失): {e}", flush=True)
        return {}


def _load_drift():
    if not os.path.exists(DRIFT_FILE):
        return {}
    try:
        with open(DRIFT_FILE, encoding="utf-8") as _f:
            return json.load(_f) or {}
    except Exception as e:
        print(f"[consistency_watchdog] 加载 DRIFT_FILE 失败(非缺失): {e}", flush=True)
        return {}


def check_consistency(focus_symbols=None, disabled_set=None):
    """返回一致性报告 dict。focus_symbols 缺省取 fd.DEFAULT_CONFIG 全部键。"""
    import four_dim_strategy as fd
    focus = focus_symbols or list(fd.DEFAULT_CONFIG.get("thresholds_by_symbol", {}).keys())
    disabled = disabled_set or set()
    calib = _load_calib()
    drift = _load_drift()
    drift_map = {it.get("symbol"): it for it in drift.get("items", [])}

    base_ts = fd.DEFAULT_CONFIG.get("thresholds_by_symbol", {})
    divergences, unvalidated, broken_serving, broken_gated, stale = [], [], [], [], []

    for sym in focus:
        base_cfg = base_ts.get(sym, {})
        base_T = base_cfg.get("T_thresh")
        served = calib.get(sym, {})
        served_T = served.get("T_thresh", base_T)
        mean_oos = served.get("mean_oos")

        # 1) 训练/服务偏离（仅对「未近期主动重校」者报：近期 apply 的偏离是有意为之，
        #    由 broken_serving 覆盖失效风险，此处不再重复报 needs_revalidation）
        _recently_recal = False
        _ra = served.get("recalibrated_at")
        if _ra:
            try:
                _dt = datetime.strptime(_ra, "%Y-%m-%d %H:%M:%S")
                _recently_recal = (datetime.now() - _dt) <= timedelta(days=RECAL_GRACE_DAYS)
            except Exception:
                pass
        if (not _recently_recal) and base_T is not None and served_T is not None and base_T != 0:
            dev = abs(served_T - base_T) / abs(base_T)
            if dev > DEVIATE_PCT:
                divergences.append({
                    "symbol": sym, "baseline_T": base_T, "served_T": served_T,
                    "deviation_pct": round(dev * 100, 1),
                    "needs_revalidation": True,
                })

        # 2) 未校验（无 mean_oos）
        if mean_oos is None and sym not in calib.get("__note_only__", {}):
            # 仅对确实有 calib 条目但缺 mean_oos 的关注品种报警（纯默认占位不算）
            if sym in calib:
                unvalidated.append({"symbol": sym, "served_T": served_T,
                                     "note": "calibration_params 有条目但缺 mean_oos，未做 OOS 校验"})

        # 3) 失效却在服务。已被动态门控 papertrack_gated 压制 → 归入 broken_gated（风险已控，仅提示）；
        #    未门控且未禁用 → 真在服务失效模型，归入 broken_serving（计入 ok=false）。
        d = drift_map.get(sym)
        if d and d.get("status") == "broken" and sym not in disabled:
            _gated = bool(d.get("papertrack_gated"))
            _entry = {"symbol": sym,
                      "current_expR": d.get("current_expR"),
                      "evidence": d.get("evidence"),
                      "papertrack_gated": _gated,
                      "note": ("漂移判 broken 且已被动态门控压制（不发信号），风险已控"
                               if _gated else
                               "漂移判 broken 但未禁用，仍在服务该模型")}
            if _gated:
                broken_gated.append(_entry)
            else:
                broken_serving.append(_entry)

        # 4) 陈旧重校
        ra = served.get("recalibrated_at")
        if ra:
            try:
                dt = datetime.strptime(ra, "%Y-%m-%d %H:%M:%S")
                if datetime.now() - dt > timedelta(days=STALE_DAYS):
                    stale.append({"symbol": sym, "recalibrated_at": ra,
                                  "days_ago": (datetime.now() - dt).days})
            except Exception:
                pass

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"deviate_pct": DEVIATE_PCT, "stale_days": STALE_DAYS},
        "summary": {
            "divergences": len(divergences),
            "unvalidated": len(unvalidated),
            "broken_serving": len(broken_serving),
            "broken_gated": len(broken_gated),
            "stale": len(stale),
            "focus_count": len(focus),
        },
        "divergences": divergences,
        "unvalidated": unvalidated,
        "broken_serving": broken_serving,
        "broken_gated": broken_gated,
        "stale": stale,
        "ok": (len(divergences) + len(unvalidated) + len(broken_serving) + len(stale)) == 0,
    }
