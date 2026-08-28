# -*- coding: utf-8 -*-
"""四维策略 · 执行计划器（#7 大单拆分 / 冰山 / TWAP 建议）
=============================================================
问题：策略只告诉你「做多 8 手」，但没告诉你 **怎么把这 8 手打进去**。
在生猪、苹果、纯碱远月这类薄盘口品种，一次性市价 8 手可能自己把价格打上去 3~5 跳，
一进场就先亏掉半个 R —— 这部分成本回测里根本看不到。

本模块按「品种流动性 × 委托手数」给出人话执行建议：
  · 手数相对盘口很小        → 一次性成交（市价/对价单）
  · 手数吃掉盘口一大口      → 拆 N 片 TWAP，给出每片手数与间隔秒数
  · 手数明显超过盘口承载    → 拆片 + 冰山（只显示一小部分），并降级为限价被动挂单

流动性口径（无 L2 盘口数据时的稳健近似）：
  每分钟成交量 ≈ 近 60 日日均成交量 / 225（一个交易日约 225 分钟连续竞价）
  单片上限 = 每分钟量 × PARTICIPATION（默认 8%，即不做「盘口里最吵的那个人」）

用法：
    import execution_planner as ep
    plan = ep.plan_execution("lh", lots=8, price=14500, direction="多")
    print(plan["headline"])   # 一句话建议
"""

from __future__ import annotations

import math
import time

try:
    import four_dim_strategy as fd
except Exception:  # 允许脱离主工程单测
    fd = None

# ── 参数 ──────────────────────────────────────────────────────────────────
# 单片上限 = min(每分钟成交量 × 参与率, 绝对帽)。
# 参与率按档位递减：越薄的盘口，能不动声色吃掉的比例越小。
# 绝对帽是关键 —— 每分钟成交量高估了「瞬时盘口深度」（挂单只有分钟量的一小部分），
# 没有绝对帽的话超流动品种算出 200+ 手单片，规则等于永不触发、形同虚设。
PARTICIPATION_BY_TIER = {1.0: 0.03, 1.5: 0.02, 2.0: 0.01}
SLICE_CAP_BY_TIER = {1.0: 40, 1.5: 15, 2.0: 6}
MINUTES_PER_DAY = 225  # 日盘连续竞价分钟数（近似）
MAX_SLICES = 6  # 最多拆 6 片（再多人工执行就烦了）
MIN_SLICE_LOTS = 1
ICEBERG_TRIGGER = 3  # 单片 ≥3 手且属低流动 → 建议冰山
_VOL_CACHE: dict = {}  # {sym: (ts, avg_daily_volume)}
_VOL_TTL = 3600


def _avg_daily_volume(symbol, tail=60):
    """近 tail 日日均成交量（手）。取不到返回 None（调用方降级为纯档位规则）。"""
    now = time.time()
    hit = _VOL_CACHE.get(symbol)
    if hit and now - hit[0] < _VOL_TTL:
        return hit[1]
    v = None
    try:
        if fd is not None:
            df = fd.load_daily(symbol)
            if df is not None and len(df) >= 20 and "volume" in df:
                v = float(df["volume"].tail(tail).mean())
                if not (v > 0):
                    v = None
    except Exception:
        v = None
    _VOL_CACHE[symbol] = (now, v)
    return v


def _tier(symbol):
    """流动性档位：1.0 超流动 / 1.5 中 / 2.0 薄。复用滑点表，口径一致。"""
    try:
        if fd is not None:
            return float(fd.get_slip_pts(symbol))
    except Exception:
        pass
    return 1.5


def _tick_size(symbol):
    try:
        if fd is not None:
            sp = fd.DEFAULT_CONFIG.get("contract_specs", {}).get(symbol, {})
            return float(sp.get("tick") or sp.get("tick_size") or 1)
    except Exception:
        pass
    return 1.0


def plan_execution(symbol, lots, price=None, direction="多", urgency="normal"):
    """生成执行建议。

    urgency: "normal"（默认，均衡）/ "fast"（追信号，容忍冲击）/ "patient"（挂被动）
    返回 dict：
      {symbol, lots, tier, per_min_vol, max_slice, slices, slice_lots[],
       interval_sec, style, iceberg, iceberg_show, limit_offset_tick,
       impact_note, headline}
    """
    lots = max(0, int(lots or 0))
    if lots <= 0:
        return {"symbol": symbol, "lots": 0, "slices": 0, "headline": "无需执行"}

    tier = _tier(symbol)
    adv = _avg_daily_volume(symbol)
    per_min = (adv / MINUTES_PER_DAY) if adv else None

    # 单片可承载手数 = min(分钟量×档位参与率, 档位绝对帽)
    part = PARTICIPATION_BY_TIER.get(tier, 0.01)
    cap = SLICE_CAP_BY_TIER.get(tier, 6)
    if per_min:
        max_slice = max(MIN_SLICE_LOTS, min(int(per_min * part), cap))
    else:
        # 无量数据时按档位给保守经验值（取绝对帽的一半）
        max_slice = max(MIN_SLICE_LOTS, cap // 2)

    # 紧急度调整：追信号可放宽 1.5×，被动可收紧 0.6×
    if urgency == "fast":
        max_slice = max(MIN_SLICE_LOTS, int(max_slice * 1.5))
    elif urgency == "patient":
        max_slice = max(MIN_SLICE_LOTS, int(max_slice * 0.6))

    slices = min(MAX_SLICES, max(1, math.ceil(lots / max_slice)))
    base = lots // slices
    rem = lots - base * slices
    slice_lots = [base + (1 if i < rem else 0) for i in range(slices)]
    slice_lots = [s for s in slice_lots if s > 0]
    slices = len(slice_lots)

    # 间隔：越薄盘口拉越开；追信号压缩
    interval = {1.0: 20, 1.5: 45}.get(tier, 90)
    if urgency == "fast":
        interval = max(10, int(interval * 0.5))
    elif urgency == "patient":
        interval = int(interval * 1.6)
    if slices == 1:
        interval = 0

    # 下单方式
    if slices == 1 and tier <= 1.0:
        style = "市价/对价一次性成交"
    elif urgency == "fast":
        style = "对价限价分片（超价1跳保成交）"
    elif tier >= 2.0:
        style = "盘口被动限价分片（不追价，等对手来）"
    else:
        style = "限价分片（挂本方最优价，30秒不成再对价）"

    limit_offset = 1 if urgency == "fast" else 0

    # 冰山：薄盘口 + 单片手数不小 → 藏单量
    biggest = max(slice_lots) if slice_lots else 0
    iceberg = bool(tier >= 1.5 and biggest >= ICEBERG_TRIGGER)
    iceberg_show = max(1, biggest // 3) if iceberg else 0

    # 冲击成本估算（点数）：档位滑点 × 分片折减
    tick = _tick_size(symbol)
    raw_impact = tier * (1 + math.log(max(1, lots / max(1, max_slice)), 2))
    split_impact = tier * (1 + 0.25 * (slices - 1))
    saved_pts = max(0.0, raw_impact - split_impact)
    impact_note = (
        f"一次性打约滑 {raw_impact:.1f} 跳，拆 {slices} 片约 {split_impact:.1f} 跳，"
        f"省约 {saved_pts:.1f} 跳（≈{saved_pts * tick:.1f} 点/手）"
    )

    if slices == 1:
        headline = f"{lots} 手一次性{style}即可（盘口吃得下，不用拆）"
    else:
        seq = "+".join(str(s) for s in slice_lots)
        headline = f"{lots} 手别一把打 —— 拆 {slices} 片（{seq}），每片间隔 {interval}s，{style}" + (
            f"，冰山只露 {iceberg_show} 手" if iceberg else ""
        )

    return {
        "symbol": symbol,
        "lots": lots,
        "direction": direction,
        "tier": tier,
        "tier_label": {1.0: "A超流动", 1.5: "B中流动"}.get(tier, "C薄盘口"),
        "avg_daily_volume": round(adv) if adv else None,
        "per_min_vol": round(per_min, 1) if per_min else None,
        "max_slice": max_slice,
        "slices": slices,
        "slice_lots": slice_lots,
        "interval_sec": interval,
        "style": style,
        "iceberg": iceberg,
        "iceberg_show": iceberg_show,
        "limit_offset_tick": limit_offset,
        "impact_note": impact_note,
        "headline": headline,
        "urgency": urgency,
    }


def plan_exit(symbol, lots, price=None, direction="多", panic=False):
    """离场执行建议。止损离场默认 urgency=fast（保命优先，不为省滑点拖着不跑）。"""
    p = plan_execution(symbol, lots, price, direction, urgency="fast" if panic else "normal")
    if panic:
        p["headline"] = "🚨 止损离场：" + p["headline"] + "（宁可多滑一跳也要走干净）"
    return p


if __name__ == "__main__":
    for sym, lots in [("rb", 5), ("rb", 60), ("lh", 8), ("AP", 6), ("jd", 3), ("FG", 30)]:
        p = plan_execution(sym, lots)
        print(f"\n[{sym} {lots}手] 档位={p['tier_label']} 每分钟量={p['per_min_vol']} 单片上限={p['max_slice']}")
        print("  →", p["headline"])
        print("  ", p["impact_note"])
    print("\n[止损离场 lh 8手]", plan_exit("lh", 8, panic=True)["headline"])
