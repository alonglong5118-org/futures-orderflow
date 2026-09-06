#!/usr/bin/env python3
"""金字塔加仓模块（影子模式，P2，2026-08-31）。

三重门 + 保守阶梯 + 只推荐不执行（shadow mode）。

诊断依据（tools_pyramid_diagnosis.py v3，4498 笔首达博弈）：
- A1(+0.5R 加仓) EV +0.067R / A2(+1.0R) EV +0.104R / A3(+1.5R) EV +0.158R
  → "等更多确认再加仓"优于"早加仓"，故阶梯取 A2/A3 位置
- 三重门：
  1. 行情门：Layer 0 状态引擎 ∈ {趋势初, 趋势中}（趋势状态 A2 EV +0.123R 最强）
  2. 品种门：白名单 = 加仓 EV > +0.12R 的品种；黑名单(EV<0)与灰名单(未验证)均不开
  3. 标签门：策略标签 ∈ {趋势, 背离}（均值回归 EV +0.003 零边际；背离 +0.098 最强）

阶梯（保守版，最多 2 次加仓，与 T1/T2 互斥——门全过推荐金字塔，任一门不过走原 T1/T2）：
- P1：+1.0R 触发，加 50% 首仓手数，全仓止损上移至入场价（0R）
- P2：+1.5R 触发，加 25% 首仓手数，全仓止损上移至 +0.5R
- P2 后：全仓移动止损 = 峰值回撤 1.0R；峰值仓位 175%（首仓 100% + 50% + 25%）
- 保本数学：P2 加完后全打 +0.5R 止损 → 0.5×1 − 0.5×0.5 − 0.25×1 = 0R ≈ 保本
  （首仓 N 手盈 0.5R×N，P1 0.5N 亏 0.5R×0.5N，P2 0.25N 亏 1.0R×0.25N）

影子模式行为：evaluate() 只返回建议 dict 并写影子日志，不产生任何委托/仓位变更。
上线前须过 5 折 OOS（tools_oos_batch 系列同款纪律）。
"""

import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SHADOW_LOG = os.path.join(HERE, "pyramid_shadow_log.json")

# ── 门 2：品种白名单 v2（品种级 5 折 OOS 通过，2026-08-31）──
# v1 = 诊断层加仓 EV>0.12R 的 10 品种；v2 = 模块级同路径 A/B 重放逐品种 5 折验证后保留 6 个。
# 白名单 v2 复验：n=383, Δ合计 +60.89R, +0.159R/笔, 5/5 折全正（tools_oos_pyramid_by_symbol.py）
PYRAMID_WHITELIST = {
    "FG", "SR", "l", "ru", "sp", "ss",
}
# OOS 剔除（模块级重放不达标）：cu 2/5 折 -13.56R（诊断第一但保本止损掐赢单）、
# AP 2/5 折、p 3/5 折但合计为负、pp 2/5 折。教训：加仓单位孤立 EV ≠ 全模块 Δ
PYRAMID_OOS_REMOVED = {"cu", "AP", "p", "pp"}
# 黑名单（诊断层加仓 EV < 0）显式记录，防止后续误加入；灰名单（未验证）一律不开
PYRAMID_BLACKLIST = {"ag", "UR", "pg", "b", "rr"}

# ── 门 3：标签门 ──
PYRAMID_LABELS = {"趋势", "背离"}

# ── 门 1：行情门（Layer 0 状态引擎）──
PYRAMID_STATES = {"trend_early", "trend_mid"}

# ── 门 4：换月门（2026-08-31 移仓护卫联动）──
# 持仓合约进入换月 warn/urgent 窗口（距交割月 <15 天）时，老合约的价格体系即将作废，
# 在其上加仓等于把新仓建在失真趋势上。窗口内该品种暂停金字塔推荐，走原 T1/T2。
# 由 runner 在调用 evaluate() 前查 rollover_info 后置 True（pyramid_addon 保持零依赖，
# 不 import live_runner，避免循环引用）。
PYRAMID_ROLL_BLOCK = {"warn", "urgent"}

# ── 保守阶梯（最多 2 次加仓）──
PYRAMID_LADDER = [
    {"step": "P1", "trigger_r": 1.0, "add_ratio": 0.50, "new_stop_r": 0.0},
    {"step": "P2", "trigger_r": 1.5, "add_ratio": 0.25, "new_stop_r": 0.5},
]
PYRAMID_TRAIL_START_R = 2.0   # P2 后启动移动止损的起点
PYRAMID_TRAIL_DIST_R = 1.0    # 峰值回撤 1.0R 离场

_STATE_NAMES = {
    "trend_early": "趋势初", "trend_mid": "趋势中",
    "trend_late": "趋势末", "sideways": "震荡",
}


def _gate_reasons(symbol, market_state, strategy_label, roll_level=None):
    """逐门检查，返回 (全过?, 明细 dict)。roll_level: 换月预警级别（ok/warn/urgent/None）。"""
    g = {"regime": False, "symbol": False, "label": False, "roll": False}
    detail = {}

    g["regime"] = market_state in PYRAMID_STATES
    if not g["regime"]:
        detail["regime"] = f"行情门未过：{_STATE_NAMES.get(market_state, market_state or '未知状态')}（仅趋势初/中期开放）"

    if symbol in PYRAMID_WHITELIST:
        g["symbol"] = True
    elif symbol in PYRAMID_BLACKLIST:
        detail["symbol"] = f"品种门未过：{symbol} 在黑名单（加仓 EV 为负）"
    else:
        detail["symbol"] = f"品种门未过：{symbol} 未通过 OOS 验证（灰名单）"

    g["label"] = (strategy_label or "") in PYRAMID_LABELS
    if not g["label"]:
        detail["label"] = f"标签门未过：{strategy_label or '无标签'}（仅趋势/背离开放，均值回归加仓零边际）"

    g["roll"] = roll_level not in PYRAMID_ROLL_BLOCK
    if not g["roll"]:
        detail["roll"] = f"换月门未过：{symbol} 持仓合约距交割月不足 15 天（{roll_level}），老合约趋势失真，暂停加仓推荐"

    return all(g.values()), {"passed": g, "failed_reasons": detail}


def evaluate(symbol, direction, entry_price, stop_dist, lots,
             market_state=None, strategy_label=None, roll_level=None):
    """三重门评估。全过 → 返回金字塔推荐 dict；任一门不过 → 返回 None（走原 T1/T2）。

    direction: "多"/"空"（或 ±1）
    entry_price: 信号参考入场价
    stop_dist: 止损距离（价格单位，来自信号卡）
    lots: 首仓手数
    roll_level: 换月预警级别（"ok"/"warn"/"urgent"，来自 rollover_info；None 视为 ok）
    """
    if not entry_price or not stop_dist or stop_dist <= 0 or not lots:
        return None
    d = 1 if str(direction) in ("多", "1", "long", "buy") else -1

    passed, gates = _gate_reasons(symbol, market_state, strategy_label, roll_level)
    if not passed:
        return None

    ladder = []
    peak_lots = int(lots)
    for spec in PYRAMID_LADDER:
        add_lots = max(1, int(lots * spec["add_ratio"] + 0.5)) if lots >= 2 else 1
        trigger_price = round(entry_price + d * spec["trigger_r"] * stop_dist, 1)
        new_stop_price = round(entry_price + d * spec["new_stop_r"] * stop_dist, 1)
        ladder.append({
            "step": spec["step"],
            "trigger_r": spec["trigger_r"],
            "trigger_price": trigger_price,
            "add_lots": add_lots,
            "add_ratio": spec["add_ratio"],
            "new_stop_r": spec["new_stop_r"],
            "new_stop_price": new_stop_price,
            "note": f"{'涨至' if d > 0 else '跌至'} {trigger_price} 加 {add_lots} 手，全仓止损上移至 {new_stop_price}",
        })
        peak_lots += add_lots

    trail_price = round(entry_price + d * PYRAMID_TRAIL_START_R * stop_dist, 1)
    state_name = _STATE_NAMES.get(market_state, market_state)

    rationale = (
        f"三重门全过：{state_name} × 白名单品种 × {strategy_label}标签 × 换月窗口外。"
        f"阶梯：+1.0R 加 50%（止损保本）→ +1.5R 加 25%（止损 +0.5R）→ 峰值 {peak_lots} 手（首仓 {lots}），"
        f"P2 后移动止损 1.0R。最坏情况（P2 后全打 +0.5R 止损）≈ 保本。"
        f"参考期望：A2 +0.104R / A3 +0.158R（4498 笔首达博弈）。"
    )

    return {
        "mode": "pyramid",
        "shadow": True,  # 影子模式标记：仅推荐，不执行
        "symbol": symbol,
        "direction": "多" if d > 0 else "空",
        "entry_ref": entry_price,
        "stop_dist": round(stop_dist, 2),
        "base_lots": int(lots),
        "market_state": market_state,
        "strategy_label": strategy_label,
        "gates": gates,
        "ladder": ladder,
        "trail": {
            "start_r": PYRAMID_TRAIL_START_R,
            "start_price": trail_price,
            "dist_r": PYRAMID_TRAIL_DIST_R,
        },
        "peak_lots": peak_lots,
        "rationale": rationale,
        "ev_ref": "A2 +0.104R, A3 +0.158R (tools_pyramid_diagnosis v3)",
    }


def log_shadow(rec, signal_time=None):
    """影子日志：记录每次推荐，供后续回放验证与 OOS 检验。"""
    entry = {
        "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_time": signal_time or rec.get("logged_at"),
        "symbol": rec.get("symbol"),
        "direction": rec.get("direction"),
        "entry_ref": rec.get("entry_ref"),
        "stop_dist": rec.get("stop_dist"),
        "base_lots": rec.get("base_lots"),
        "market_state": rec.get("market_state"),
        "strategy_label": rec.get("strategy_label"),
        "ladder": rec.get("ladder"),
        "peak_lots": rec.get("peak_lots"),
    }
    try:
        logs = []
        if os.path.exists(SHADOW_LOG):
            with open(SHADOW_LOG, encoding="utf-8") as f:
                logs = json.load(f)
        logs.append(entry)
        with open(SHADOW_LOG, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 影子日志失败不影响信号主流程
    return entry
