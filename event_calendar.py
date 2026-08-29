"""四维策略 · 事件/数据日历闸门（#13）
=========================================
重大的宏观/产业数据公布前后，盘面常出现“流动性抽干 + 瞬时打脸”：
点差拉爆、滑点失控、假突破扫损。这种时候最该做的往往不是“更努力交易”，
而是“少做 / 不做 / 把尺寸压下来”。

本模块内置一份期货品种相关的关键事件日历（宏观 + 产业 + 交易所），
交易前自动检查未来 N 小时有没有重磅事件，给出闸门建议：
    reduce      → 把计划手数压到常态的 1/2（事件前减仓）
    no_new_open → 事件前 1 小时内禁止新开仓（只在场内的能平不能开）
    flat_hint   → 提醒注意已有持仓的跳空风险

日历是可维护的静态表（带周几/时刻/重要性），配合少量月度启发式。
真正的“某日某刻 exact”财经日历需联网抓取，本模块先做到“高频规律事件不漏”。

用法：
    import event_calendar as ec
    evs = ec.upcoming(lookahead_hours=24)   # 未来24h 的事件
    g   = ec.gate(lookahead_hours=4)         # 未来4h 的闸门建议
    ec.print_gate(g)
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 事件表：wd=None 表示每日；wd=0..6（周一=0）表示每周几；imp: 高/中/低
# hh/mm 为北京时间；window_h = 该事件影响半径（前后多少小时算“临近”）
EVENTS = [
    # —— 每日时段（提醒用，非风险）——
    {
        "name": "日盘开盘",
        "kind": "时段",
        "imp": "低",
        "wd": None,
        "hh": 9,
        "mm": 0,
        "win": 0.5,
        "note": "流动性恢复，但开盘前15分钟常跳空",
    },
    {
        "name": "日盘收盘",
        "kind": "时段",
        "imp": "低",
        "wd": None,
        "hh": 15,
        "mm": 0,
        "win": 0.5,
        "note": "收盘前注意减仓/平仓，避免隔夜跳空",
    },
    {
        "name": "夜盘开盘",
        "kind": "时段",
        "imp": "低",
        "wd": None,
        "hh": 21,
        "mm": 0,
        "win": 0.5,
        "note": "外盘联动时段，波动加大",
    },
    {
        "name": "夜盘收盘",
        "kind": "时段",
        "imp": "低",
        "wd": None,
        "hh": 23,
        "mm": 0,
        "win": 0.5,
        "note": "部分品种23:00收，注意持仓过夜风险",
    },
    # —— 宏观（美国时段，北京时间）——
    {
        "name": "EIA原油库存",
        "kind": "宏观",
        "imp": "高",
        "wd": 2,
        "hh": 22,
        "mm": 30,
        "win": 2.0,
        "note": "每周三22:30，原油/化工(燃油/PTA)瞬间波动大",
    },
    {
        "name": "美国非农就业",
        "kind": "宏观",
        "imp": "高",
        "wd": 4,
        "hh": 20,
        "mm": 30,
        "win": 2.0,
        "note": "每月第一周五20:30，全商品共振，金银/有色/股指最敏感",
    },
    {
        "name": "美国CPI",
        "kind": "宏观",
        "imp": "高",
        "wd": None,
        "hh": 20,
        "mm": 30,
        "win": 2.0,
        "note": "多在每月中旬某日20:30(夏令)/21:30(冬令)，需盯exact日历",
    },
    {
        "name": "美联储FOMC利率决议",
        "kind": "宏观",
        "imp": "高",
        "wd": None,
        "hh": 2,
        "mm": 0,
        "win": 3.0,
        "note": "每年8次(约6周一次)凌晨2:00公布+2:30鲍威尔，金银/股指巨震",
    },
    {
        "name": "美国初请失业金",
        "kind": "宏观",
        "imp": "中",
        "wd": 3,
        "hh": 20,
        "mm": 30,
        "win": 1.0,
        "note": "每周四20:30，影响有限但叠加其他数据会放大",
    },
    {
        "name": "美国GDP/耐用品订单",
        "kind": "宏观",
        "imp": "中",
        "wd": None,
        "hh": 20,
        "mm": 30,
        "win": 1.5,
        "note": "月底/季末不定期，美元系品种留意",
    },
    # —— 中国宏观/产业 ——
    {
        "name": "中国CPI/PPI(统计局)",
        "kind": "宏观",
        "imp": "中",
        "wd": None,
        "hh": 9,
        "mm": 30,
        "win": 1.5,
        "note": "每月9-15日某日09:30，黑色/农产品情绪影响",
    },
    {
        "name": "中国社融/信贷数据",
        "kind": "宏观",
        "imp": "中",
        "wd": None,
        "hh": 16,
        "mm": 0,
        "win": 1.5,
        "note": "每月10-15日盘后公布，次日开盘易跳",
    },
    {
        "name": "交易所持仓排名(龙虎榜)",
        "kind": "交易所",
        "imp": "中",
        "wd": 4,
        "hh": 15,
        "mm": 30,
        "win": 1.0,
        "note": "每日收盘后交易所公布，周五更受关注；本系统已有 cpos_rank 模块",
    },
    # —— 产业周度数据 ——
    {
        "name": "Mysteel钢材库存/产量",
        "kind": "产业",
        "imp": "高",
        "wd": 3,
        "hh": 11,
        "mm": 0,
        "win": 2.0,
        "note": "每周四约11:00，螺纹/热卷/焦煤焦炭定价锚",
    },
    {
        "name": "找钢网库存数据",
        "kind": "产业",
        "imp": "中",
        "wd": 2,
        "hh": 11,
        "mm": 30,
        "win": 1.5,
        "note": "每周三中午，黑色系情绪扰动",
    },
    {
        "name": "港口铁矿石库存",
        "kind": "产业",
        "imp": "中",
        "wd": 4,
        "hh": 15,
        "mm": 30,
        "win": 1.5,
        "note": "每周五下午，铁矿/黑色链",
    },
    {
        "name": "USDA出口销售/供需报告",
        "kind": "产业",
        "imp": "高",
        "wd": 4,
        "hh": 0,
        "mm": 30,
        "win": 2.0,
        "note": "周四半夜(北京时间周五00:30左右)，豆粕/豆油/玉米/棉花跳空",
    },
    {
        "name": "生猪周度出栏/屠宰数据",
        "kind": "产业",
        "imp": "中",
        "wd": 5,
        "hh": 12,
        "mm": 0,
        "win": 1.5,
        "note": "每周六机构汇总，影响周一生猪开盘预期",
    },
    {
        "name": "钢联/卓创化工开工率",
        "kind": "产业",
        "imp": "中",
        "wd": 3,
        "hh": 17,
        "mm": 0,
        "win": 1.5,
        "note": "周四盘后，纯碱/PTA/甲醇等",
    },
]

_IMP_RANK = {"高": 3, "中": 2, "低": 1}


def _next_occurrence(ev, now):
    """返回该事件下一次发生的 datetime（≥now）。wd=None 表示每日。"""
    hh, mm, wd = ev["hh"], ev["mm"], ev.get("wd")
    if wd is None:
        # 今天该时刻若已过，取明天
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand < now:
            cand += timedelta(days=1)
        return cand
    # 找到下一个 weekday==wd 的日期
    days_ahead = (wd - now.weekday()) % 7
    if days_ahead == 0:
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand < now:
            days_ahead = 7
    cand = (now + timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cand


def upcoming(lookahead_hours=24, now=None):
    now = now or datetime.now()
    horizon = now + timedelta(hours=lookahead_hours)
    out = []
    for ev in EVENTS:
        occ = _next_occurrence(ev, now)
        if occ <= horizon:
            in_h = round((occ - now).total_seconds() / 3600.0, 2)
            out.append(
                {
                    "name": ev["name"],
                    "kind": ev["kind"],
                    "imp": ev["imp"],
                    "time": occ.strftime("%Y-%m-%d %H:%M"),
                    "in_hours": in_h,
                    "win": ev["win"],
                    "note": ev["note"],
                }
            )
    out.sort(key=lambda x: x["in_hours"])
    return out


def gate(lookahead_hours=4, now=None):
    """未来 lookahead 小时内有没有重磅事件？给闸门建议。"""
    now = now or datetime.now()
    evs = upcoming(lookahead_hours=lookahead_hours, now=now)
    reduce = False
    no_new_open = False
    near = []
    for e in evs:
        # 临近程度：事件发生时刻落在 [now - win, now + win]
        eff = max(0.0, e["in_hours"] - e["win"] * 0.5)  # 距离“影响半径”还有多久触发关注
        within = e["in_hours"] <= e["win"]
        if not within:
            continue
        near.append(e)
        rank = _IMP_RANK.get(e["imp"], 1)
        if rank >= 2:
            reduce = True
        if rank >= 3 and e["in_hours"] <= 1.0:
            no_new_open = True
    msg = "无重大事件，按计划交易"
    if no_new_open:
        msg = "⛔ 1小时内有重磅数据，建议禁止新开仓，仅处理场内持仓"
    elif reduce:
        msg = "⚠️ 临近重要数据，建议把计划手数压到 1/2"
    return {
        "lookahead_hours": lookahead_hours,
        "reduce": reduce,
        "no_new_open": no_new_open,
        "events": near,
        "msg": msg,
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def scale_factor(g, normal=1.0):
    """把闸门建议转成手数缩放系数。no_new_open 时返回 0（禁开）。"""
    if g.get("no_new_open"):
        return 0.0
    if g.get("reduce"):
        return 0.5
    return normal


def print_gate(g):
    print("=" * 56)
    print(f"事件闸门 · 未来 {g['lookahead_hours']}h")
    print(f"  {g['msg']}")
    if g["events"]:
        for e in g["events"]:
            print(f"  [{e['imp']}] {e['name']} · {e['time']}（还有{e['in_hours']}h）")
    else:
        print("  无临近事件")
    print("=" * 56)


if __name__ == "__main__":
    print_gate(gate(lookahead_hours=24))
