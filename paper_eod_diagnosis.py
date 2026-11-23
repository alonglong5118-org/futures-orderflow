#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三感参谋 · 模拟盘 auto 每日 EOD 诊断（纯标准库，无第三方依赖）

数据来源：
  1) paper_trading_state.json  —— 完整 trades 历史 / stats / run_start / config
  2) 实时 API GET /api/paper-trading —— 权威的 equity / mtm / 当前持仓（带 current_price）
  3) trade_config.json         —— contract_specs（multiplier / margin_rate / name）用于算保证金占用

产出：
  - paper_reports/YYYY-MM-DD.md   —— 当日 Markdown 日报
  - paper_reports/daily_history.csv —— 逐日累积（同日重跑自动覆盖当日行），用于趋势/回撤

指标：胜率 / 盈亏比(profit factor) / R 倍数分布 / 平均R / 期望R / 最大回撤 /
      保证金占用与使用率 / 按品种拆解 / EOD 持仓明细 / 最近平仓明细。

用法：
  python3 paper_eod_diagnosis.py [--date YYYY-MM-DD]
"""
import json
import os
import sys
import csv
import datetime
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "paper_trading_state.json")
TCFG_FILE = os.path.join(HERE, "trade_config.json")
OUTDIR = os.path.join(HERE, "paper_reports")
HIST_CSV = os.path.join(OUTDIR, "daily_history.csv")
API_URL = "http://localhost:8741/api/paper-trading"

# R 倍数分布分桶（左闭右开，最后一桶闭合）
R_BUCKETS = [
    ("<= -3R", None, -3.0),
    ("-3 ~ -2R", -3.0, -2.0),
    ("-2 ~ -1R", -2.0, -1.0),
    ("-1 ~ 0R", -1.0, 0.0),
    ("0 ~ 1R", 0.0, 1.0),
    ("1 ~ 2R", 1.0, 2.0),
    ("2 ~ 3R", 2.0, 3.0),
    (">= 3R", 3.0, None),
]


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_api():
    try:
        req = urllib.request.Request(API_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def fmt_money(x):
    try:
        return f"{x:,.0f}"
    except Exception:
        return str(x)


def fmt_pct(x, nd=2):
    try:
        return f"{x:.{nd}f}%"
    except Exception:
        return str(x)


def bucket_of(r):
    for label, lo, hi in R_BUCKETS:
        if lo is None and r <= hi:
            return label
        if hi is None and r >= lo:
            return label
        if lo is not None and hi is not None and lo <= r < hi:
            return label
    return ">= 3R"


def compute_position_results(closes):
    """按 signal_id 归并平仓记录 -> 头寸级结果（支持分批平仓）。"""
    pos = {}
    for t in closes:
        key = t.get("signal_id") or f"{t.get('symbol')}@{t.get('time')}"
        p = pos.setdefault(
            key,
            {
                "symbol": t.get("symbol"),
                "name": t.get("name") or t.get("symbol"),
                "direction": t.get("direction"),
                "pnl": 0.0,
                "R": 0.0,
                "lots": 0,
                "last_time": t.get("time"),
                "reasons": set(),
            },
        )
        p["pnl"] += float(t.get("pnl") or 0.0)
        p["R"] += float(t.get("pnl_R") or 0.0)
        p["lots"] += int(t.get("lots") or 0)
        p["reasons"].add(t.get("reason") or "")
        if (t.get("time") or "") >= (p["last_time"] or ""):
            p["last_time"] = t.get("time")
    return list(pos.values())


def main():
    date_override = None
    if "--date" in sys.argv:
        i = sys.argv.index("--date")
        if i + 1 < len(sys.argv):
            date_override = sys.argv[i + 1]

    os.makedirs(OUTDIR, exist_ok=True)

    st = load_json(STATE_FILE)
    specs = (load_json(TCFG_FILE).get("contract_specs") or {}) if os.path.exists(TCFG_FILE) else {}
    api = fetch_api()

    now = datetime.datetime.now()
    report_date = date_override or now.strftime("%Y-%m-%d")

    trades = st.get("trades", []) or []
    closes = [t for t in trades if t.get("type") == "close"]
    opens = [t for t in trades if t.get("type") == "open"]

    # ---- 账户总览（优先实时 API，回退落盘 state）----
    cash = float(st.get("cash", 0.0))
    realized = float(st.get("realized_pnl", 0.0))
    init_cash = float((st.get("config") or {}).get("init_cash", 1000000.0))
    if api:
        equity = float(api.get("equity", cash + realized) or 0.0)
        mtm = float(api.get("mtm", 0.0) or 0.0)
        positions = api.get("positions", []) or []
    else:
        equity = cash + realized
        mtm = 0.0
        positions = list((st.get("positions") or {}).values())

    total_return_pct = (equity - init_cash) / init_cash * 100 if init_cash else 0.0

    # ---- 保证金占用 / 使用率 ----
    total_margin = 0.0
    for p in positions:
        sym = p.get("symbol")
        spec = specs.get(sym, {})
        mult = float(spec.get("multiplier", 10))
        mr = float(spec.get("margin_rate", 0.10))
        total_margin += int(p.get("lots", 0)) * float(p.get("entry_price", 0)) * mult * mr
    usage_pct = total_margin / equity * 100 if equity else 0.0
    usage_cap = float((st.get("config") or {}).get("max_usage_pct", 45.0))

    # ---- 头寸级结果 ----
    presults = compute_position_results(closes)
    n_pos = len(presults)
    wins = [p for p in presults if p["pnl"] > 0]
    losses = [p for p in presults if p["pnl"] < 0]
    flats = [p for p in presults if p["pnl"] == 0]
    win_rate = (len(wins) / n_pos * 100) if n_pos else 0.0
    gross_profit = sum(p["pnl"] for p in wins)
    gross_loss = abs(sum(p["pnl"] for p in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    avg_R = (sum(p["R"] for p in presults) / n_pos) if n_pos else 0.0
    total_realized_closed = sum(p["pnl"] for p in presults)

    # R 分布（头寸级）
    r_dist = {label: 0 for label, _, _ in R_BUCKETS}
    for p in presults:
        r_dist[bucket_of(p["R"])] += 1

    # 持仓时长（从 close 记录 holding_hours 汇总，若无则用 0）
    hold_hours = [float(t.get("holding_hours") or 0.0) for t in closes if t.get("holding_hours") is not None]
    avg_hold = sum(hold_hours) / len(hold_hours) if hold_hours else 0.0

    max_dd = float((st.get("stats") or {}).get("max_drawdown", 0.0) or 0.0)

    # 运行天数 & 交易日
    run_start_ts = float(st.get("run_start_ts", 0.0) or 0.0)
    run_start_str = (
        datetime.datetime.fromtimestamp(run_start_ts).strftime("%Y-%m-%d %H:%M:%S") if run_start_ts else "—"
    )
    elapsed = (now - datetime.datetime.fromtimestamp(run_start_ts)) if run_start_ts else datetime.timedelta(0)
    cal_days = elapsed.days
    uptime_str = f"{cal_days} 天" if elapsed.days >= 1 else f"{elapsed.total_seconds() / 3600:.1f} 小时"
    trade_days = sorted({(t.get("time") or "")[:10] for t in trades if t.get("time")})

    # ---- 按品种拆解 ----
    per_symbol = {}
    for p in presults:
        s = per_symbol.setdefault(
            p["symbol"],
            {"name": p["name"], "n": 0, "w": 0, "l": 0, "pnl": 0.0, "R": 0.0},
        )
        s["n"] += 1
        s["pnl"] += p["pnl"]
        s["R"] += p["R"]
        if p["pnl"] > 0:
            s["w"] += 1
        elif p["pnl"] < 0:
            s["l"] += 1
    per_symbol_rows = []
    for sym, s in per_symbol.items():
        wr = (s["w"] / s["n"] * 100) if s["n"] else 0.0
        disp_name = (specs.get(sym, {}) or {}).get("name") or s["name"]
        per_symbol_rows.append(
            {
                "symbol": sym,
                "name": disp_name,
                "n": s["n"],
                "w": s["w"],
                "l": s["l"],
                "wr": wr,
                "pnl": s["pnl"],
                "avg_R": (s["R"] / s["n"]) if s["n"] else 0.0,
            }
        )
    per_symbol_rows.sort(key=lambda x: x["pnl"])

    # ---- 生成 Markdown ----
    L = []
    L.append(f"# 三感参谋 · 模拟盘 auto 日报（{report_date}）")
    L.append("")
    L.append(f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}　｜　运行起点：{run_start_str}　｜　已运行 {uptime_str} / {len(trade_days)} 个交易日")
    L.append("")

    L.append("## 一、账户总览")
    L.append("")
    L.append("| 项目 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 初始资金 | ¥{fmt_money(init_cash)} |")
    L.append(f"| 当前权益 | **¥{fmt_money(equity)}** |")
    L.append(f"| 已实现盈亏 | ¥{fmt_money(realized)} |")
    L.append(f"| 浮动盈亏(MTM) | ¥{fmt_money(mtm)} |")
    L.append(f"| 总收益率 | {fmt_pct(total_return_pct)} |")
    L.append(f"| 当前持仓 | {len(positions)} 笔 |")
    L.append(f"| 保证金占用 | ¥{fmt_money(total_margin)}（使用率 {fmt_pct(usage_pct)}，上限 {fmt_pct(usage_cap, 0)}） |")
    L.append(f"| 已平仓头寸 | {n_pos} 笔 |")
    L.append("")

    L.append("## 二、核心指标")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 胜率 | {fmt_pct(win_rate)}（{len(wins)}胜 / {len(losses)}负" + (f" / {len(flats)}平" if flats else "") + "） |")
    pf_str = "∞（无亏损）" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    L.append(f"| 盈亏比(Profit Factor) | {pf_str} |")
    L.append(f"| 平均盈利 / 平均亏损 | ¥{fmt_money(avg_win)} / ¥{fmt_money(avg_loss)} |")
    L.append(f"| 平均 R 倍数 | {avg_R:+.2f}R |")
    L.append(f"| 平均持仓时长 | {avg_hold:.2f} 小时 |")
    L.append(f"| 最大回撤 | ¥{fmt_money(max_dd)}（{fmt_pct(max_dd / init_cash * 100 if init_cash else 0)}） |")
    L.append("")

    L.append("## 三、R 倍数分布（头寸级）")
    L.append("")
    L.append("| 区间 | 笔数 | 占比 |")
    L.append("|---|---|---|")
    for label, _, _ in R_BUCKETS:
        c = r_dist[label]
        pct = (c / n_pos * 100) if n_pos else 0.0
        bar = "█" * c if c else ""
        L.append(f"| {label} | {c} | {fmt_pct(pct, 0)} {bar} |")
    L.append("")

    L.append("## 四、按品种拆解")
    L.append("")
    if per_symbol_rows:
        L.append("| 品种 | 笔数 | 胜/负 | 胜率 | 累计盈亏 | 平均R |")
        L.append("|---|---|---|---|---|---|")
        for r in per_symbol_rows:
            L.append(
                f"| {r['symbol']}({r['name']}) | {r['n']} | {r['w']}/{r['l']} | {fmt_pct(r['wr'], 0)} | ¥{fmt_money(r['pnl'])} | {r['avg_R']:+.2f}R |"
            )
    else:
        L.append("_暂无已平仓交易_")
    L.append("")

    L.append("## 五、持仓明细（EOD 未平）")
    L.append("")
    if positions:
        L.append("| 品种 | 方向 | 手数 | 开仓价 | 止损 | 目标 | 现价 | 浮动盈亏 | 浮盈R | 持仓(h) |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for p in positions:
            sym = p.get("symbol")
            spec = specs.get(sym, {})
            nm = spec.get("name", p.get("name", sym))
            cur = p.get("current_price")
            mtm_p = p.get("mtm")
            mtm_R = p.get("mtm_R")
            L.append(
                f"| {sym}({nm}) | {p.get('direction','')} | {p.get('lots',0)} | {p.get('entry_price')} | "
                f"{p.get('stop_price')} | {p.get('target_price')} | {cur if cur is not None else '—'} | "
                f"{('¥'+fmt_money(mtm_p)) if mtm_p is not None else '—'} | "
                f"{('%.2fR' % mtm_R) if mtm_R is not None else '—'} | {p.get('hours_held','—')} |"
            )
    else:
        L.append("_当前无持仓_")
    L.append("")

    L.append("## 六、最近平仓明细（最多 15 笔）")
    L.append("")
    if closes:
        L.append("| 时间 | 品种 | 方向 | 手数 | 开→平 | 盈亏 | R | 原因 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t in closes[-15:][::-1]:
            L.append(
                f"| {t.get('time','')} | {t.get('symbol','')} | {t.get('direction','')} | {t.get('lots',0)} | "
                f"{t.get('entry_price')}→{t.get('exit_price')} | ¥{fmt_money(t.get('pnl'))} | "
                f"{float(t.get('pnl_R') or 0):+.2f}R | {t.get('reason','')} |"
            )
    else:
        L.append("_暂无平仓交易_")
    L.append("")
    L.append("---")
    L.append("_本报告由 paper_eod_diagnosis.py 自动生成；只读诊断，不改动持仓、不触发交易。_")

    md = "\n".join(L)
    out_md = os.path.join(OUTDIR, f"{report_date}.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)

    # ---- 追加/更新 daily_history.csv（同日覆盖）----
    header = [
        "date", "generated_at", "equity", "cash", "realized_pnl", "mtm",
        "total_margin", "usage_pct", "positions_open", "closed_positions",
        "wins", "losses", "win_rate", "profit_factor", "avg_R", "max_drawdown",
    ]
    rows = []
    if os.path.exists(HIST_CSV):
        try:
            with open(HIST_CSV, encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for row in rd:
                    if row.get("date") != report_date:
                        rows.append(row)
        except Exception:
            rows = []
    rows.append(
        {
            "date": report_date,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "equity": f"{equity:.2f}",
            "cash": f"{cash:.2f}",
            "realized_pnl": f"{realized:.2f}",
            "mtm": f"{mtm:.2f}",
            "total_margin": f"{total_margin:.2f}",
            "usage_pct": f"{usage_pct:.2f}",
            "positions_open": str(len(positions)),
            "closed_positions": str(n_pos),
            "wins": str(len(wins)),
            "losses": str(len(losses)),
            "win_rate": f"{win_rate:.2f}",
            "profit_factor": ("inf" if profit_factor == float("inf") else f"{profit_factor:.4f}"),
            "avg_R": f"{avg_R:.4f}",
            "max_drawdown": f"{max_dd:.2f}",
        }
    )
    rows.sort(key=lambda r: r.get("date", ""))
    with open(HIST_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in header})

    # ---- 控制台摘要 ----
    print("=" * 60)
    print(f"三感参谋 · 模拟盘 auto 日报  {report_date}")
    print("=" * 60)
    print(f"权益       : ¥{fmt_money(equity)}  (收益 {fmt_pct(total_return_pct)})")
    print(f"已实现/浮动: ¥{fmt_money(realized)} / ¥{fmt_money(mtm)}")
    print(f"持仓/平仓  : {len(positions)} 笔在持 / {n_pos} 笔已平")
    print(f"胜率       : {fmt_pct(win_rate)}  盈亏比: {pf_str}  平均R: {avg_R:+.2f}")
    print(f"使用率     : {fmt_pct(usage_pct)} (上限 {fmt_pct(usage_cap,0)})  最大回撤: ¥{fmt_money(max_dd)}")
    print(f"报告       : {out_md}")
    print(f"历史       : {HIST_CSV}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
