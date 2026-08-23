#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase A：把全市场 5m 多源合并为 pipeline 可用的 data_5m/_XX0_min5.csv。

源（按日期去重合并，取并集 -> 最长连续覆盖）：
  1) 量化回测/全品种策略定位/_min5_cache/XX_5min.csv  (中文列, 主连5m, 止于 2026-07-24)
  2) data_5m/_XX0_min5.csv                              (已有 sina/天勤, 英文列)
  3) data_5m/tq_free/_XX0_min5.csv                      (天勤免费, 英文列, 止于 08-14)

不改动 tq_free/ 与任何源文件，只写 data_5m/_XX0_min5.csv。
加性数据工程，不在四红线内。
"""
import os, json, glob
import pandas as pd
import four_dim_strategy as fd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = "/Users/ken/WorkBuddy/2026-07-28-12-52-27/管住手_实盘工作区_Ken/量化回测/全品种策略定位/_min5_cache"
DATA_5M = os.path.join(HERE, "data_5m")
TQ_FREE = os.path.join(DATA_5M, "tq_free")
os.makedirs(DATA_5M, exist_ok=True)
COLMAP = fd.COLMAP  # 中文->英文
STD = ["date", "open", "high", "low", "close", "volume", "oi"]

def load_any(path, src_label):
    """读一个 5m csv（中/英文列皆可），返回带 date DatetimeIndex 的 DataFrame(英文列)。"""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.rename(columns=COLMAP)
    # 兜底日期列名
    for s in ("日期", "时间", "datetime", "Datetime", "time", "Time"):
        if s in df.columns and "date" not in df.columns:
            df = df.rename(columns={s: "date"}); break
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    keep = [c for c in STD if c in df.columns]
    df = df[keep].copy()
    df = df.sort_values("date").drop_duplicates(subset=["date"]).set_index("date")
    return df

def sym_from_cache(fname):
    return os.path.basename(fname).replace("_5min.csv", "")

def sym_from_std(fname):
    # _FG0_min5.csv -> FG ; _jd0_min5.csv -> jd （"0" 是主连连续标记，非符号一部分）
    b = os.path.basename(fname)
    assert b.startswith("_") and b.endswith("_min5.csv"), b
    s = b[1:-len("_min5.csv")]
    if s.endswith("0"):
        s = s[:-1]
    return s

# ---- 1. 收集所有源文件，按品种归组 ----
groups = {}  # sym -> list of (path, label)
for f in glob.glob(os.path.join(CACHE, "*_5min.csv")):
    s = sym_from_cache(f)
    groups.setdefault(s, []).append((f, "cache"))
for f in glob.glob(os.path.join(DATA_5M, "_*0_min5.csv")):
    s = sym_from_std(f)
    groups.setdefault(s, []).append((f, "data_5m"))
for f in glob.glob(os.path.join(TQ_FREE, "_*0_min5.csv")):
    s = sym_from_std(f)
    groups.setdefault(s, []).append((f, "tq_free"))

# 把 sym 对齐到 SYMBOLS 键（大小写）
def to_key(sym):
    for k in fd.SYMBOLS:
        if k.lower() == sym.lower():
            return k
    return sym  # 不在 SYMBOLS（如 bz）保留原样

print(f"=== Phase A：{len(groups)} 个品种待合并 ===\n")
report = {}
n_total_rows = 0
for sym in sorted(groups):
    frames = []
    srcs = []
    for path, label in groups[sym]:
        d = load_any(path, label)
        if d is not None and len(d) > 0:
            frames.append(d)
            srcs.append(f"{label}({len(d)})")
    if not frames:
        report[sym] = dict(ok=False, note="无可用源")
        continue
    merged = pd.concat(frames)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    # 补齐缺失列(不含 date，date 是索引)
    for c in ["open", "high", "low", "close", "volume", "oi"]:
        if c not in merged.columns:
            merged[c] = float("nan")
    key = to_key(sym)
    out_df = merged.reset_index()          # date 变回普通列
    out_df = out_df[STD]                   # [date,open,high,low,close,volume,oi]
    # 同时写 data_5m 与 BACKTEST_DIR 根目录（load_min5 先查 BACKTEST_DIR，
    # 否则旧 sina _JD0/_RM0 会盖住新合并数据）
    out_paths = [os.path.join(DATA_5M, f"_{key}0_min5.csv"),
                 os.path.join(fd.BACKTEST_DIR, f"_{key}0_min5.csv")]
    for p in out_paths:
        out_df.to_csv(p, index=False)
    # 完整性校验
    nan_close = int(out_df["close"].isna().sum())
    report[key] = dict(ok=True, n=len(out_df), srcs=srcs,
                       first=str(out_df["date"].iloc[0]), last=str(out_df["date"].iloc[-1]),
                       nan_close=nan_close, paths=out_paths)
    n_total_rows += len(merged)
    print(f"  {key:4} 行={len(merged):5d} 源=[{', '.join(srcs)}] 首={merged.index[0]} 尾={merged.index[-1]} nan_close={nan_close}")

# ---- 2. 日线可用性（决定 Phase C 5m出场回测能否跑） ----
BACKTEST_DIR = fd.BACKTEST_DIR
have_daily, miss_daily = [], []
for key in report:
    if not report[key].get("ok"):
        continue
    found = False
    for c in (key, key.upper(), key.lower()):
        if os.path.exists(os.path.join(BACKTEST_DIR, f"_{c}0_daily.csv")):
            found = True; break
    (have_daily if found else miss_daily).append(key)

print(f"\n=== 合并完成：{sum(1 for v in report.values() if v.get('ok'))} 个文件，总行数={n_total_rows} ===")
print(f"有日线(可跑5m出场回测): {len(have_daily)} 个")
print(f"无日线: {len(miss_daily)} 个 -> {miss_daily}")
print(f"\nDISABLED_SYMBOLS(校准判死, 回测结果仅作数据参考):", sorted(set(have_daily) & fd.DISABLED_SYMBOLS))

with open(os.path.join(HERE, "_convert_min5_report.json"), "w") as f:
    json.dump({"report": report, "have_daily": have_daily, "miss_daily": miss_daily,
               "disabled_in_have": sorted(set(have_daily) & fd.DISABLED_SYMBOLS)},
              f, ensure_ascii=False, indent=2, default=str)
print("\n报告已写 _convert_min5_report.json")
