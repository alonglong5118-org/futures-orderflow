#!/usr/bin/env python3
"""移仓护卫 v1 历史 paper 回测（2026-09-07）。

数据：/Volumes/Expansion/归档-M4/期货数据/分钟线数据/1分钟/ — 逐合约 1 分钟历史（2010–2026，全交易所）。
此前本地 data_5m 仅主连 _XX0，缺逐合约历史→无法验证；现用 Expansion 上的逐合约 parquet 做真实换月成本回测。

方法：对每个 live 品种，汇集其所有历史合约（前缀+YYMM，如 FG2401），按交割月排序；
检测换月事件 = 次月合约持仓量(OI)首次超过旧合约之日(roll_date)。
比较两种处理在 [warn=交割月前15日, 交割月首日] 窗口的成本：
  · NAIVE（无视移仓护卫，死扛旧合约至交割）：旧合约进入交割月前流动性崩塌→点差/滑点恶化；
  · GUARDIAN（移仓护卫 warn 窗口平旧开新）：在正常流动性下完成换月。
量化：窗口内旧合约 vs 新合约的流动性(OI)与滑点代理(1分钟 bar 高低差/收盘)之差 = 移仓护卫"避免的流动性损耗"。
另报不可免的换月基差(roll_spread)作参照。

滑点代理 = 1分钟 bar 的 (high-low)/close 均值（%）——流动性越差，盘中区间越宽→滑点越大。
"""
import os
import zipfile
import io
import datetime as dt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = "/Volumes/Expansion/归档-M4/期货数据/分钟线数据/1分钟"
EXCH_OF = {  # 我们的 live 品种 -> (交易所代码, 合约前缀，前缀在逐合约 parquet 中为大写)
    "rb": ("SHFE", "RB"), "FG": ("CZCE", "FG"), "ag": ("SHFE", "AG"),
    "cu": ("SHFE", "CU"), "al": ("SHFE", "AL"), "JM": ("DCE", "JM"),
    "jd": ("DCE", "JD"), "SR": ("CZCE", "SR"), "TA": ("CZCE", "TA"),
    "y": ("DCE", "Y"), "p": ("DCE", "P"), "ru": ("SHFE", "RU"),
    "c": ("DCE", "C"), "fu": ("SHFE", "FU"), "J": ("DCE", "J"),
}
YEARS = range(2018, 2026)
ROLL_WARN_DAYS = 15


def load_contracts(sym):
    """返回 {contract_code: DataFrame(日级: date, close, oi, vol, slip_pct)}"""
    exch, prefix = EXCH_OF[sym]
    out = {}
    for y in YEARS:
        zp = os.path.join(ROOT, str(y), exch, f"期货1分钟历史行情_{y}_{exch}.zip")
        if not os.path.exists(zp):
            continue
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if not base.startswith(prefix) or not base.endswith(".parquet"):
                    continue
                # 合约代码：去前缀与 ".{EXCH}.parquet"
                code = base[len(prefix):].split(".")[0]   # e.g. 2401
                # 只接受 YYMM 标准合约（跳过主连/连续/延迟合约如 y0/yD1、期权等）
                if len(code) != 4 or not code.isdigit():
                    continue
                mm = int(code[2:])
                if mm < 1 or mm > 12:
                    continue
                contract = prefix + code                  # FG2401
                with z.open(name) as f:
                    tbl = pq.read_table(io.BytesIO(f.read()))
                df = tbl.to_pandas()
                # 解析时间
                df["t"] = pd.to_datetime(df["trade_time"])
                df = df.sort_values("t")
                df["date"] = df["t"].dt.normalize()
                # 日级聚合
                g = df.groupby("date")
                day = pd.DataFrame({
                    "close": g["close"].last(),
                    "oi": g["oi"].last().fillna(0),
                    "vol": g["vol"].sum().fillna(0),
                    "slip_pct": ((df["high"] - df["low"]) / df["close"]).groupby(df["date"]).mean() * 100.0,
                }).dropna(subset=["close"])
                day["contract"] = contract
                day["deliv"] = dt.date(2000 + int(code[:2]), int(code[2:]), 1)
                out[contract] = day
    return out


def detect_rollovers(contracts):
    """按交割月排序相邻合约，找 roll_date（次月 OI 首超旧月）。返回 list of dict。"""
    items = sorted(contracts.items(), key=lambda kv: kv[1]["deliv"].iloc[0])
    rolls = []
    for (ca, da), (cb, db) in zip(items, items[1:]):
        if db["deliv"].iloc[0] <= da["deliv"].iloc[0]:
            continue
        # 共同交易日区间
        common = da.index.intersection(db.index)
        if len(common) < 5:
            continue
        roll_day = None
        for d in common:
            if db.loc[d, "oi"] > da.loc[d, "oi"] and db.loc[d, "oi"] > 0:
                roll_day = d
                break
        if roll_day is None:
            continue
        rolls.append({"old": ca, "new": cb, "roll_day": roll_day,
                      "deliv_start": da["deliv"].iloc[0],
                      "old_day": da, "new_day": db})
    return rolls


def _align_ts(ts, idx):
    """把 naive date 对齐成与 idx（可能为 tz-aware）同 tz 的 Timestamp。"""
    if getattr(idx, "tz", None) is not None:
        return pd.Timestamp(ts).tz_localize(idx.tz)
    return pd.Timestamp(ts)


def eval_roll(r):
    deliv = r["deliv_start"]
    warn = deliv - dt.timedelta(days=ROLL_WARN_DAYS)
    da, db = r["old_day"], r["new_day"]
    # 窗口 [warn, deliv_start] 内两合约都有数据的交易日（时区对齐）
    tw = _align_ts(warn, da.index)
    td = _align_ts(deliv, da.index)
    idx = da.index[(da.index >= tw) & (da.index <= td)]
    idx = idx.intersection(db.index)
    if len(idx) < 3:
        return None
    old_oi = da.loc[idx, "oi"].mean()
    new_oi = db.loc[idx, "oi"].mean()
    old_slip = da.loc[idx, "slip_pct"].mean()
    new_slip = db.loc[idx, "slip_pct"].mean()
    # 不可免换月基差：warn 日新旧收盘差
    wd = idx[0]
    roll_spread = abs(da.loc[wd, "close"] - db.loc[wd, "close"]) / da.loc[wd, "close"] * 100.0
    avoided = old_slip - new_slip  # %点/日，旧合约更宽=移仓护卫避免的滑点损耗
    return {
        "old": r["old"], "new": r["new"], "warn": str(warn),
        "old_oi": old_oi, "new_oi": new_oi,
        "old_slip": old_slip, "new_slip": new_slip,
        "liq_ratio": (new_oi / old_oi) if old_oi > 0 else np.nan,
        "avoided_slip_pct": avoided,
        "roll_spread_pct": roll_spread,
    }


def main():
    print("移仓护卫 v1 历史 paper 回测（逐合约 1 分钟，Expansion 数据）")
    print("=" * 96)
    grand = []
    for sym in EXCH_OF:
        contracts = load_contracts(sym)
        if len(contracts) < 2:
            continue
        rolls = detect_rollovers(contracts)
        evals = [e for e in (eval_roll(r) for r in rolls) if e]
        if not evals:
            continue
        av_avoid = np.mean([e["avoided_slip_pct"] for e in evals])
        av_liq = np.nanmedian([e["liq_ratio"] for e in evals])
        av_spread = np.mean([e["roll_spread_pct"] for e in evals])
        n_benef = sum(1 for e in evals if e["avoided_slip_pct"] > 0)
        print(f"{sym:4} 合约数={len(contracts):3d} 换月事件={len(evals):3d} | "
              f"新/旧流动性比={av_liq:6.1f}x | 避免滑点损耗(日均)={av_avoid:+5.2f}% | "
              f"换月基差(不可免)={av_spread:4.2f}% | 移仓护卫有益占比={n_benef/len(evals)*100:4.1f}%")
        for e in evals:
            grand.append((sym, e))
    print("=" * 96)
    if grand:
        ga = np.mean([e["avoided_slip_pct"] for _, e in grand])
        gl = np.nanmedian([e["liq_ratio"] for _, e in grand])
        gs = np.mean([e["roll_spread_pct"] for _, e in grand])
        gb = sum(1 for _, e in grand if e["avoided_slip_pct"] > 0) / len(grand) * 100
        print(f"全样本: 换月事件={len(grand)} | 新/旧流动性比={gl:6.1f}x | "
              f"避免滑点损耗(日均)={ga:+5.2f}% | 换月基差={gs:4.2f}% | 移仓护卫有益占比={gb:4.1f}%")
        print("结论: 若新/旧流动性比 >>1 且避免滑点损耗>0 的占比高 → 移仓护卫提前换月确实避免流动性损耗，护栏有效；")
        print("      若两者均接近中性 → 移仓护卫 mainly 防『误持旧合约进交割』的操作风险，成本节省有限（仍值得，因交割风险是硬止损）。")
    print("=" * 96)


if __name__ == "__main__":
    main()
