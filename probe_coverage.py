#!/usr/bin/env python3
"""
probe_coverage.py — 跨期套利全品种数据源覆盖度探针（2026-09-26）

目的：摸清新浪 futures_zh_daily_sina 通道对跨板块候选品种 1/5/9 主力月
已到期合约的历史回溯深度，作为「扩大品种范围」的入选依据。

探针口径：
  - 对每个品种，拉 2015~2026 共 12 年 × 3 个月（01/05/09）的合约
  - 统计：可拉到的合约数、每个合约的行数（≥50 才有效，排除挂牌即死）
  - 输出覆盖度矩阵：品种 | 板块 | 可回溯起始年 | 合约数 | 平均行数 | 数据量评分

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""
import os
import sys
import time
from datetime import datetime

import akshare as ak
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 跨板块候选品种清单：code -> (新浪前缀, 板块, 主力月集合)
# 新浪前缀与郑商所 3 位年份的特殊处理在 fetch 时按交易所规则展开
CANDIDATES = {
    # 黑色系
    "j":  ("J",  "黑色系", ["01", "05", "09"]),
    "jm": ("JM", "黑色系", ["01", "05", "09"]),
    "rb": ("RB", "黑色系", ["01", "05", "09"]),
    "hc": ("HC", "黑色系", ["01", "05", "09"]),
    "i":  ("I",  "黑色系", ["01", "05", "09"]),
    "sf": ("SF", "黑色系", ["01", "05", "09"]),
    "sm": ("SM", "黑色系", ["01", "05", "09"]),
    "ss": ("SS", "黑色系", ["01", "05", "09"]),
    # 有色金属
    "cu": ("CU", "有色", ["01", "05", "09"]),
    "al": ("AL", "有色", ["01", "05", "09"]),
    "zn": ("ZN", "有色", ["01", "05", "09"]),
    "ni": ("NI", "有色", ["01", "05", "09"]),
    "pb": ("PB", "有色", ["01", "05", "09"]),
    "sn": ("SN", "有色", ["01", "05", "09"]),
    "au": ("AU", "贵金属", ["06", "12"]),
    "ag": ("AG", "贵金属", ["06", "12"]),
    # 能化
    "sc": ("SC", "能化", ["01", "05", "09"]),
    "fu": ("FU", "能化", ["01", "05", "09"]),
    "bu": ("BU", "能化", ["01", "05", "09"]),
    "ru": ("RU", "能化", ["01", "05", "09"]),
    "ta": ("TA", "能化", ["01", "05", "09"]),
    "ma": ("MA", "能化", ["01", "05", "09"]),
    "pp": ("PP", "能化", ["01", "05", "09"]),
    "l":  ("L",  "能化", ["01", "05", "09"]),
    "v":  ("V",  "能化", ["01", "05", "09"]),
    "eg": ("EG", "能化", ["01", "05", "09"]),
    "eb": ("EB", "能化", ["01", "05", "09"]),
    "pg": ("PG", "能化", ["01", "05", "09"]),
    "p":  ("P",  "农产品", ["01", "05", "09"]),
    "y":  ("Y",  "农产品", ["01", "05", "09"]),
    "m":  ("M",  "农产品", ["01", "05", "09"]),
    "rm": ("RM", "农产品", ["01", "05", "09"]),
    "oi": ("OI", "农产品", ["01", "05", "09"]),
    "cf": ("CF", "农产品", ["01", "05", "09"]),
    "sr": ("SR", "农产品", ["01", "05", "09"]),
    "ap": ("AP", "农产品", ["01", "05", "10"]),
    "c":  ("C",  "农产品", ["01", "05", "09"]),
    "cs": ("CS", "农产品", ["01", "05", "09"]),
    "a":  ("A",  "农产品", ["01", "05", "09"]),
    # 其他
    "sa": ("SA", "能化", ["01", "05", "09"]),
    "fg": ("FG", "能化", ["01", "05", "09"]),
    "ur": ("UR", "能化", ["01", "05", "09"]),
    "cj": ("CJ", "农产品", ["01", "05", "09"]),
    "lh": ("LH", "农产品", ["01", "05", "09"]),
    "sp": ("SP", "农产品", ["01", "05", "09"]),
}


def build_symbols(prefix, months, years=range(15, 27)):
    """展开合约代码。郑商所（纯碱 SA 等）与上期/大商/能源不同：
    新浪对郑商所品种用 3 位年份（如 SA510=2025年10月），但前缀需大写。
    这里统一用 2 位年份 4 位代码（如 SA2510），新浪 futures_zh_daily_sina
    实际接受 SA2510 这种标准 4 位代码（已在 collect_spread.py 验证 SA2610 有效）。
    """
    syms = []
    for y in years:
        yy = f"{y:02d}"
        for m in months:
            syms.append(f"{prefix}{yy}{m}")
    return syms


def probe(code, prefix, months):
    """探测单品种覆盖度。返回 (合约数, 有效合约数, 起始年, 平均行数, 详情)。"""
    syms = build_symbols(prefix, months)
    valid = {}
    for sym in syms:
        try:
            df = ak.futures_zh_daily_sina(symbol=sym)
            if df is not None and len(df) >= 50:
                valid[sym] = len(df)
        except Exception:
            pass
        time.sleep(0.3)  # 限流保护
    if not valid:
        return 0, 0, None, 0, {}
    start_years = sorted({2000 + int(s[-4:-2]) for s in valid})
    avg = sum(valid.values()) / len(valid)
    return len(valid), len(valid), start_years[0], avg, valid


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 78)
    print(f"跨期套利全品种覆盖度探针 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)

    rows = []
    for code, (prefix, sector, months) in CANDIDATES.items():
        sys.stdout.write(f"[{code:>3}] {prefix:>3} {sector} 探测中...")
        sys.stdout.flush()
        n_contracts, n_valid, start_year, avg_len, detail = probe(code, prefix, months)
        # 估算可对齐交易日（近似 = 平均行数 × 有效合约数 / 月数跨度）
        status = "✅" if start_year is not None and start_year <= 2020 else "⚠️"
        print(f" {status} 有效合约={n_valid} 起始={start_year} 平均行数={avg_len:.0f}")
        rows.append({
            "品种": code,
            "前缀": prefix,
            "板块": sector,
            "有效合约数": n_valid,
            "回溯起始年": start_year if start_year else "",
            "平均行数": round(avg_len, 0) if avg_len else 0,
            "覆盖评分": "A" if (start_year and start_year <= 2018 and n_valid >= 20)
                       else "B" if (start_year and start_year <= 2020 and n_valid >= 12)
                       else "C" if start_year else "D",
        })

    df = pd.DataFrame(rows)
    df = df.sort_values(["板块", "覆盖评分", "有效合约数"], ascending=[True, True, False])
    out = os.path.join(OUT_DIR, "coverage_matrix.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print("覆盖度矩阵（按板块 × 覆盖评分排序）:")
    print(df.to_string(index=False))
    print(f"\n已落盘: {out}")
    print("\n评分口径：A=起始≤2018且合约≥20（深度好，优先入选）；")
    print("          B=起始≤2020且合约≥12（可做但样本偏短）；")
    print("          C=有数据但深度不足；D=无有效数据（新浪不覆盖/已摘牌）。")


if __name__ == "__main__":
    main()
