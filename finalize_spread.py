#!/usr/bin/env python3
"""
finalize_spread.py — 全品种价差策略最终核算 + 流动性闸门 + 入选清单（2026-09-26）

三件事：
  1. 真实成本核算：从 symbols/symbol_config.yaml（银河官方费率）+ 乘数/tick_size
     计算每品种 round-trip 成本（元/吨），替换初筛/验证里的近似值。
  2. 流动性闸门：ADV250（按 trading_date 聚合日均成交量）≥5000 OK / 500~5000 差 / <500 死。
  3. 最终入选清单：分段口径净 tick > 0 且 笔数≥10 且 胜率≥50% 且 流动性 OK/差，排除死合约。

sn/ni 专项：额外输出保证金占用（每手保证金 vs 每笔止损 tick），供 Ken 决定是否保留。

运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python（读 csv/yaml 无需 akshare）
"""
import os
import sys
import glob
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")
ZZ = Path(os.path.expanduser("~/Desktop/日内交易系统/intraday_trading_system_zz"))

# 近期近月价（元/吨，从 data/history/<品种>.parquet 近 500 根中位价，2026-09-26）
# 🔴 用于算 rate 型手续费与保证金（rate 手续费 = 费率‰ × 价 × 乘数，必须用真实价
#   不能用价差均值——价差是「近月-远月」的差，sn 价差才 2011 而锡价 42 万，差 200 倍）
PRICE = {
    "j": 2142.5, "jm": 1615.5, "rb": 3104.0, "hc": 3364.0, "i": 721.2,
    "sf": 6024.0, "sm": 6022.0, "ss": 13975.0,
    "cu": 108550.0, "al": 23935.0, "zn": 26195.0, "ni": 128270.0,
    "pb": 16270.0, "sn": 422245.0,
    "sc": 595.2, "fu": 3680.0, "bu": 4573.0, "ru": 18760.0,
    "ta": 5562.0, "ma": 2812.0, "pp": 7912.0, "l": 7644.5,
    "v": 4486.5, "eg": 5043.0, "sa": 1013.0, "fg": 914.0,
    "p": 10080.0, "y": 8858.5, "m": 3344.0, "rm": 2343.0,
    "oi": 10324.0, "cf": 17090.0, "sr": 5378.0, "ap": 7456.0,
    "c": 2282.0, "cs": 2583.0, "a": 4941.0, "sp": 4775.0,
    "ur": 1754.0, "eb": 8628.0, "pg": 5772.0, "cj": 8065.0,
    "lh": 11905.0, "au": 994.2, "ag": 16844.5,
}

# 乘数（吨/手 或 单位/手）+ tick_size（元/吨）。缺省品种用公开合约规格补全。
# (multiplier, tick_size元/吨)
CONTRACT_SPEC = {
    "j":  (100, 0.5), "jm": (60, 0.5), "rb": (10, 1.0), "hc": (10, 1.0),
    "i":  (100, 0.5), "sf": (5, 2.0), "sm": (5, 2.0), "ss": (5, 5.0),
    "cu": (5, 10.0), "al": (5, 5.0), "zn": (5, 5.0), "ni": (1, 10.0),
    "pb": (5, 5.0), "sn": (1, 10.0),
    "sc": (1000, 0.1), "fu": (10, 1.0), "bu": (10, 2.0), "ru": (10, 5.0),
    "ta": (5, 2.0), "ma": (10, 1.0), "pp": (5, 1.0), "l": (5, 1.0),
    "v": (5, 1.0), "eg": (10, 1.0), "sa": (20, 1.0), "fg": (20, 1.0),
    "p": (10, 2.0), "y": (10, 2.0), "m": (10, 1.0), "rm": (10, 1.0),
    "oi": (10, 1.0), "cf": (5, 5.0), "sr": (10, 1.0), "ap": (10, 1.0),
    "c": (10, 1.0), "cs": (10, 1.0), "a": (10, 1.0), "sp": (10, 2.0),
    "ur": (20, 1.0), "eb": (5, 1.0), "pg": (20, 1.0), "cj": (5, 5.0),
    "lh": (16, 5.0), "au": (1000, 0.02), "ag": (15, 1.0),
}


def load_symbol_config():
    """读 symbols/symbol_config.yaml 的 products 段。"""
    p = ZZ / "symbols" / "symbol_config.yaml"
    with open(p, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    return full.get("products", {})


def real_cost_per_ton(code, products, price=None):
    """真实 round-trip 成本（元/吨）= 手续费(开+平)/乘数 + 滑点 2 tick。

    rate 型：费率(‰/1000) × 价 × 乘数 / 乘数 = 费率‰ × 价（单位即元/吨，乘数抵消）
    fixed 型：费(元/手) × 2 / 乘数
    滑点：tick_size × 2
    """
    mult, tick = CONTRACT_SPEC.get(code, (10, 1.0))
    prod = products.get(code, {})
    ctype = prod.get("commission_type", "fixed")
    slip = tick * 2.0
    px = price if price else PRICE.get(code, 1000.0)
    if ctype == "rate":
        rate = prod.get("commission_rate", 0.0) / 1000.0  # ‰ → 比例
        fee_per_ton = rate * px * 2  # 开+平（rate×价 已是元/吨）
    else:
        fee = prod.get("commission", 0.0)
        fee_per_ton = fee * 2.0 / mult
    return slip + fee_per_ton


def margin_per_lot(code, products, price):
    """每手保证金 = 价 × 乘数 × 保证金率（银河 ×1.02 已在 margin_rate 内）。"""
    mult, _ = CONTRACT_SPEC.get(code, (10, 1.0))
    prod = products.get(code, {})
    mr = prod.get("margin_rate", 0.0)
    if mr <= 0:
        return None
    px = price if price else PRICE.get(code, 1000.0)
    return px * mult * mr


def adv250(code):
    """流动性：读 data/history/<品种>.parquet，按 trading_date 聚合日均成交量。"""
    p = ZZ / "data" / "history" / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["trading_date", "volume"])
        by_day = df.groupby("trading_date")["volume"].sum()
        return float(by_day.mean())
    except Exception:
        return None


def main():
    products = load_symbol_config()
    verify = pd.read_csv(os.path.join(OUT_DIR, "verify_all.csv"))

    rows = []
    for _, r in verify.iterrows():
        code = r["品种"]
        month = r["月差"]
        avg_price = PRICE.get(code, 1000.0)   # 用真实近月价（不是价差均值）
        cost = real_cost_per_ton(code, products, avg_price)
        adv = adv250(code)
        mult, tick = CONTRACT_SPEC.get(code, (10, 1.0))
        ty = tick * mult

        # 流动性判定
        if adv is None:
            liq = "无数据"
        elif adv < 500:
            liq = "死合约"
        elif adv < 5000:
            liq = "流动性差"
        else:
            liq = "OK"

        mg = margin_per_lot(code, products, avg_price)
        # 每手合约价值 + 每笔止损风险（价差 std ≈ 单笔潜在波动 × tick 元）
        lot_value = avg_price * mult

        rows.append({
            "品种": code, "月差": month,
            "近月价": round(avg_price, 1),
            "真实成本元/吨": round(cost, 3),
            "tick元/手": round(ty, 2),
            "分段净tick": r["分段净tick"],
            "分段胜率": r["分段胜率"],
            "分段笔数": r["分段笔数"],
            "WF净tick": r["WF净tick"],
            "ADV250": round(adv, 0) if adv else None,
            "流动性": liq,
            "每手保证金": round(mg, 0) if mg else None,
            "每手价值": round(lot_value, 0),
        })

    out = pd.DataFrame(rows)
    out = out.sort_values("分段净tick", ascending=False)
    out.to_csv(os.path.join(OUT_DIR, "finalize_spread.csv"), index=False, encoding="utf-8-sig")

    # 最终入选：分段净tick>0 且 笔数≥10 且 胜率≥50% 且 流动性非「死合约」
    passed = out[(out["分段净tick"] > 0) & (out["分段笔数"] >= 10)
                 & (out["分段胜率"] >= 0.5) & (out["流动性"] != "死合约")]
    print("=" * 120)
    print(f"最终核算：{len(out)} 组月差，入选（分段净tick>0+笔数≥10+胜率≥50%+非死合约）{len(passed)} 组")
    print("=" * 120)
    print(passed.to_string(index=False))
    print(f"\n完整表: {os.path.join(OUT_DIR, 'finalize_spread.csv')}")

    # sn/ni 专项提示
    snni = out[out["品种"].isin(["sn", "ni"])]
    if len(snni):
        print("\n=== sn/ni 专项（高edge高波动，需 Ken 决定）===")
        print(snni.to_string(index=False))


if __name__ == "__main__":
    main()
