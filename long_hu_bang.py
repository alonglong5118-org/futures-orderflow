#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
long_hu_bang.py — 每日龙虎榜（前 20 会员持仓排名）抓取 → cpos_cache.json
=====================================================================
为四维策略 `four_dim_strategy.score_C` 的 **C_pos（持仓定位组）** 维度提供真实数据。

覆盖交易所：CZCE（郑商） / SHFE（上期） / DCE（大商） / GFEX（广期） / INE（上期能源）
抓取的每个品种产出：期货公司（前 20 会员）净持仓 net、净持仓日变化 net_chg，
并据此合成 **C_score ∈ [-100, 100]**（偏多为正），写入 cpos_cache.json。

设计要点：
- 龙虎榜每日 ~16:30 由交易所公布；本脚本默认抓「今天」，抓不到则自动回溯最近 retry_days 个交易日。
- 只抓四维策略实际覆盖的品种（见 *_SYMS），避免整所抓取导致 OOM。
- SHFE / INE 直接解析交易所官方 JSON（绕开 akshare 对 2025 版接口字段名的解析缺陷）。
- CZCE / DCE / GFEX 用 akshare；若未安装则自动 pip 安装。
- 任一交易所抓取失败仅跳过该所，不影响其它所与已写盘数据（优雅降级）。

用法：
    python long_hu_bang.py                 # 抓今天，失败回溯 4 天
    python long_hu_bang.py --date 20260811
    python long_hu_bang.py --retry-days 6
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CPOS_JSON = os.path.join(HERE, "cpos_cache.json")

# ── 四维策略实际覆盖的品种（与 four_dim_strategy.SYMBOLS 对齐，只抓这些）──
CZCE_SYMS = ["FG", "SA", "MA", "TA", "PF", "PX", "SH", "UR", "PR", "SR", "CF", "RM", "OI", "PK", "AP"]
SHFE_SYMS = ["cu", "al", "zn", "ni", "sn", "ao", "au", "ag", "rb", "hc", "ss", "bu", "fu", "ru", "sp"]
DCE_SYMS  = ["J", "JM", "jd", "lh", "i", "eb", "eg", "l", "pp", "v", "pg", "m", "y", "a", "b", "p", "c", "cs", "rr"]
GFEX_SYMS = ["si", "lc"]
INE_SYMS  = ["sc", "ec"]

# akshare DCE 用 j/jm 表示焦炭/焦煤；返回时按小写 variety 聚合，再映射回我的 symbol
DCE_VAR_MAP = {  # 小写 variety -> 我的 symbol
    "j": "J", "jm": "JM", "jd": "jd", "lh": "lh", "i": "i", "eb": "eb", "eg": "eg",
    "l": "l", "pp": "pp", "v": "v", "pg": "pg", "m": "m", "y": "y", "a": "a",
    "b": "b", "p": "p", "c": "c", "cs": "cs", "rr": "rr",
}


# ----------------------------------------------------------------------------
# 依赖与工具
# ----------------------------------------------------------------------------
def ensure(module, pip_name=None):
    """确保模块可用；缺失则尝试 pip 安装到当前解释器。"""
    try:
        return __import__(module)
    except Exception:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", pip_name or module], check=False)
        return __import__(module)


def to_num(x):
    """把可能是 '157,909' 字符串 / Series 转成数值（去千分位逗号）。

    注意：字符串列的 .sum() 在 pandas 里是「拼接」而非求和，
    因此调用方必须传整列 Series（不要先 .sum()），由本函数逐元素转数后求和。
    """
    import pandas as pd
    if isinstance(x, pd.Series):
        s = x.astype(str).str.replace(",", "", regex=False).str.strip()
        return float(pd.to_numeric(s, errors="coerce").fillna(0.0).sum())
    s = str(x).replace(",", "").strip()
    if s in ("", "nan", "None"):
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def patch_calendar(date):
    """akshare 内部交易日历可能不含近期日期，给目标日打补丁避免被拒。"""
    try:
        import akshare.futures.cot as cot
        cal = list(getattr(cot, "calendar", []))
        if date not in cal:
            cot.calendar = cal + [date]
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 各交易所抓取：统一返回 list[dict(symbol, exchange, long_oi, short_oi, long_chg, short_chg)]
# ----------------------------------------------------------------------------
def fetch_czce(ak, date):
    recs = []
    try:
        d = ak.get_rank_table_czce(date=date)
    except Exception as e:
        print(f"  [CZCE] 抓取失败: {e}")
        return recs
    if not d:
        return recs
    for sym in CZCE_SYMS:
        df = d.get(sym)
        if df is None or not hasattr(df, "columns") or len(df) == 0:
            continue
        try:
            recs.append(dict(
                symbol=sym, exchange="CZCE",
                long_oi=int(to_num(df["long_open_interest"])),
                short_oi=int(to_num(df["short_open_interest"])),
                long_chg=int(to_num(df["long_open_interest_chg"])),
                short_chg=int(to_num(df["short_open_interest_chg"])),
            ))
        except Exception as e:
            print(f"  [CZCE] {sym} 解析失败: {e}")
    # 逐合约龙虎榜（如 SA609 / SA701 / UR703），供远月 / 次主力独立分析。
    # 品种级 "SA" 会把远近月互相抵消而失真，逐合约才能看出 2609 空头回补 vs 2701 空头增仓 的分化。
    import re as _re
    for key in d:
        m = _re.match(r"^([A-Z]{1,3})(\d{3})$", str(key))
        if not m:
            continue
        if m.group(1) not in CZCE_SYMS:
            continue
        dfc = d.get(key)
        if dfc is None or not hasattr(dfc, "columns") or len(dfc) == 0:
            continue
        try:
            recs.append(dict(
                symbol=str(key), exchange="CZCE",
                long_oi=int(to_num(dfc["long_open_interest"])),
                short_oi=int(to_num(dfc["short_open_interest"])),
                long_chg=int(to_num(dfc["long_open_interest_chg"])),
                short_chg=int(to_num(dfc["short_open_interest_chg"])),
            ))
        except Exception as e:
            print(f"  [CZCE] 逐合约 {key} 解析失败: {e}")
    return recs


def _top20_net_from_oc(oc, syms):
    """从交易所 o_cursor 解析每个品种的前 20 会员净持仓。

    注意：交易所「期货公司会员」汇总行的多/空总量必然相等（市场多空对冲），净=0，
    不可用。正确口径 = 按多头持仓 CJ2 取前 20 会员合计 − 按空头持仓 CJ3 取前 20 会员合计。
    返回 {symbol: {long_oi, short_oi, long_chg, short_chg}}。
    """
    out = {}
    for sym in syms:
        prefix = sym.lower()
        rows = []
        for r in oc:
            if not isinstance(r, dict):
                continue
            inst = str(r.get("INSTRUMENTID", "")).lower()
            if not inst.startswith(prefix):
                continue
            if inst.endswith("all"):          # 跳过 rball 等汇总行
                continue
            rank = r.get("RANK")
            if rank in (-1, 0, 999):          # 跳过汇总/others
                continue
            rows.append(r)
        if not rows:
            continue
        longs = sorted(rows, key=lambda x: float(x.get("CJ2", 0) or 0), reverse=True)[:20]
        shorts = sorted(rows, key=lambda x: float(x.get("CJ3", 0) or 0), reverse=True)[:20]
        try:
            out[sym] = dict(
                long_oi=sum(int(float(x.get("CJ2", 0) or 0)) for x in longs),
                short_oi=sum(int(float(x.get("CJ3", 0) or 0)) for x in shorts),
                long_chg=sum(int(float(x.get("CJ2_CHG", 0) or 0)) for x in longs),
                short_chg=sum(int(float(x.get("CJ3_CHG", 0) or 0)) for x in shorts),
            )
        except Exception:
            pass
    return out


def fetch_shfe(requests, date):
    """SHFE 官方 JSON 直解（绕开 akshare 解析缺陷），前 20 会员净持仓口径。"""
    url = f"https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{date}.dat"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "utf-8"
        ctx = r.json()
    except Exception as e:
        print(f"  [SHFE] 抓取失败: {e}")
        return []
    net = _top20_net_from_oc(ctx.get("o_cursor", []), SHFE_SYMS)
    return [dict(symbol=s, exchange="SHFE", **v) for s, v in net.items()]


def fetch_dce(ak, date):
    recs = []
    patch_calendar(date)
    vars_list = list(DCE_SYMS)  # akshare 会内部 lower()
    try:
        d = ak.get_dce_rank_table(date=date, vars_list=vars_list)
    except Exception as e:
        print(f"  [DCE] 抓取失败: {e}")
        return recs
    if not d:
        return recs
    frames = [v for v in (d.values() if isinstance(d, dict) else [d]) if hasattr(v, "columns")]
    for df in frames:
        varcol = "variety" if "variety" in df.columns else ("var" if "var" in df.columns else None)
        if varcol is None:
            continue
        for var, sub in df.groupby(varcol):
            mysym = DCE_VAR_MAP.get(str(var).lower())
            if mysym is None:
                continue
            try:
                recs.append(dict(
                    symbol=mysym, exchange="DCE",
                    long_oi=int(to_num(sub["long_open_interest"])),
                    short_oi=int(to_num(sub["short_open_interest"])),
                    long_chg=int(to_num(sub["long_open_interest_chg"])),
                    short_chg=int(to_num(sub["short_open_interest_chg"])),
                ))
            except Exception:
                pass
    return recs


def fetch_gfex(ak, date):
    recs = []
    patch_calendar(date)
    try:
        d = ak.futures_gfex_position_rank(date=date, vars_list=list(GFEX_SYMS))
    except Exception as e:
        print(f"  [GFEX] 抓取失败: {e}")
        return recs
    if not d:
        return recs
    for v in d.values():
        if not hasattr(v, "columns"):
            continue
        varcol = "variety" if "variety" in v.columns else ("var" if "var" in v.columns else None)
        if varcol is None:
            continue
        for var, sub in v.groupby(varcol):
            sym = str(var).lower()
            if sym not in GFEX_SYMS:
                continue
            try:
                recs.append(dict(
                    symbol=sym, exchange="GFEX",
                    long_oi=int(to_num(sub["long_open_interest"])),
                    short_oi=int(to_num(sub["short_open_interest"])),
                    long_chg=int(to_num(sub["long_open_interest_chg"])),
                    short_chg=int(to_num(sub["short_open_interest_chg"])),
                ))
            except Exception:
                pass
    return recs


def fetch_ine(requests, date):
    """INE 尽力直连（与 SHFE 同源风格，前 20 会员净持仓口径）。失败则跳过。"""
    hosts = [
        f"https://www.ine.cn/data/tradedata/future/dailydata/pm{date}.dat",
        f"https://www.ine.cn/data/dailydata/pm{date}.dat",
    ]
    for url in hosts:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.encoding = "utf-8"
            ctx = r.json()
            net = _top20_net_from_oc(ctx.get("o_cursor", []), INE_SYMS)
            if net:
                return [dict(symbol=s, exchange="INE", **v) for s, v in net.items()]
        except Exception:
            continue
    return []


# ----------------------------------------------------------------------------
# 超时保护：任一交易所网络挂起时不拖垮整次抓取
# ----------------------------------------------------------------------------
import threading


def timed(fn, timeout, *a, **k):
    box = {"v": []}

    def _w():
        try:
            box["v"] = fn(*a, **k)
        except Exception as e:
            print(f"  [{getattr(fn, '__name__', 'fn')}] 异常: {e}")

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"  [{getattr(fn, '__name__', 'fn')}] 超时({timeout}s)，跳过该交易所")
        return []
    return box["v"]


# ----------------------------------------------------------------------------
# C_score 合成
# ----------------------------------------------------------------------------
def compute_c_score(rec):
    long_oi, short_oi = rec["long_oi"], rec["short_oi"]
    long_chg, short_chg = rec["long_chg"], rec["short_chg"]
    net = long_oi - short_oi
    net_chg = long_chg - short_chg
    total_oi = long_oi + short_oi
    # 参照尺度：净变化按总持仓 2% 封顶；绝对净持仓按 10% 封顶
    chg_ref = max(300.0, total_oi * 0.02)
    net_ref = max(1000.0, total_oi * 0.10)
    net_chg_score = max(-100.0, min(100.0, net_chg / chg_ref * 100))
    net_score = max(-100.0, min(100.0, net / net_ref * 100))
    # 主信号=净变化方向（规格 §1.3），绝对净持仓作 25% 倾斜
    c = round(0.75 * net_chg_score + 0.25 * net_score, 1)
    return dict(C_score=c, net=int(net), net_chg=int(net_chg),
                long_oi=long_oi, short_oi=short_oi,
                long_chg=long_chg, short_chg=short_chg, total_oi=int(total_oi))


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def load_cache():
    if os.path.exists(CPOS_JSON):
        try:
            return json.load(open(CPOS_JSON, encoding="utf-8"))
        except Exception:
            pass
    return {}


def run(date=None, retry_days=4):
    import pandas as pd  # noqa 确保在 to_num 前可用
    ak = ensure("akshare")
    requests = ensure("requests")

    if date is None:
        date = datetime.date.today().strftime("%Y%m%d")

    today = datetime.date.today()
    candidates = [date]
    dt = today
    for i in range(1, retry_days + 1):
        dt = dt - datetime.timedelta(days=i)
        candidates.append(dt.strftime("%Y%m%d"))

    recs = []
    used_date = None
    for ds in candidates:
        print(f"== 尝试抓取交易日 {ds} ==")
        recs = []
        # 可靠的所先抓；DCE/GFEX/INE 加超时保护（网络挂起时跳过，不拖垮整次）
        recs += timed(fetch_czce, 40, ak, ds)
        recs += timed(fetch_shfe, 40, requests, ds)
        recs += timed(fetch_dce, 35, ak, ds)
        recs += timed(fetch_gfex, 35, ak, ds)
        recs += timed(fetch_ine, 30, requests, ds)
        if recs:
            used_date = ds
            print(f"  抓取成功：{len(recs)} 个品种")
            break
        print(f"  {ds} 无数据，回溯前一天")

    if not recs:
        print("所有候选日均无数据，保留旧缓存退出。")
        return

    cache = load_cache()
    for rec in recs:
        sym = rec["symbol"].upper()
        cscore = compute_c_score(rec)
        hist = cache.get(sym, {}).get("history", [])
        hist = [h for h in hist if h.get("date") != used_date]
        hist.append({"date": used_date, "C_score": cscore["C_score"],
                     "net": cscore["net"], "net_chg": cscore["net_chg"]})
        hist = hist[-30:]
        cache[sym] = {
            "date": used_date, "exchange": rec["exchange"],
            "C_score": cscore["C_score"], "net": cscore["net"], "net_chg": cscore["net_chg"],
            "long_oi": cscore["long_oi"], "short_oi": cscore["short_oi"],
            "long_chg": cscore["long_chg"], "short_chg": cscore["short_chg"],
            "total_oi": cscore["total_oi"], "history": hist,
        }
    cache["_meta"] = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": used_date, "count": len(recs),
    }
    with open(CPOS_JSON, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"已写入 {CPOS_JSON}（{len(recs)} 品种，交易日 {used_date}）")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="每日龙虎榜抓取 → cpos_cache.json")
    p.add_argument("--date", help="指定交易日 YYYYMMDD（默认今天）")
    p.add_argument("--retry-days", type=int, default=4, help="抓不到时回溯天数")
    a = p.parse_args()
    run(a.date, a.retry_days)
