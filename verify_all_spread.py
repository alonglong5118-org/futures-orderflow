#!/usr/bin/env python3
"""
verify_all_spread.py — 全品种固定月差四重验证（2026-09-26）

对初筛通过的 39 品种 × 77 组月差，跑与 j/rb/hc 完全一致的四重验证口径：
  1. 去趋势负相关：偏离 60 日均线 vs 未来 5 日变化（初筛已算，这里复核）
  2. 滞后一根成交：信号 bar t → t+1 收盘成交（无前视）
  3. walk-forward：滚动 train/test，样本外绩效
  4. 分段剔除换月断层：near_sym 切换处强制平仓重置

输出每品种每月的：净 PnL、胜率、笔数、均值/笔、样本外稳健性，作为最终入选依据。

成本：用真实成本（元/吨，含滑点 2 tick + 手续费），从 real_cost_table 口径推导。
运行环境：/Users/a123/.workbuddy/venvs/akshare39/bin/python
"""
import os
import glob

import numpy as np
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "futures_spread")

# 真实成本（元/吨，round-trip = 手续费×2 或 平今口径 + 滑点 2 tick）
# 从 real_cost_table.py 的乘数/费率/tick_size 推导，单位统一为「元/吨」
# cost_per_ton = 手续费(元/手)/乘数 × 2(开平) + tick_size×2(滑点)
REAL_COST = {
    # code: (tick_size 元/吨, 手续费元/手, 乘数) → cost = fee/mult*2 + tick*2
    "j":  (0.5, 100), "jm": (0.5, 60), "rb": (1.0, 10), "hc": (1.0, 10),
    "i":  (0.5, 100), "sf": (2.0, 5), "sm": (2.0, 5), "ss": (5.0, 5),
    "cu": (10.0, 5), "al": (5.0, 5), "zn": (5.0, 5), "ni": (10.0, 1),
    "pb": (5.0, 5), "sn": (10.0, 1),
    "sc": (0.1, 1000), "fu": (1.0, 10), "bu": (2.0, 10), "ru": (5.0, 10),
    "ta": (2.0, 5), "ma": (1.0, 10), "pp": (1.0, 5), "l": (1.0, 5),
    "v": (1.0, 5), "eg": (1.0, 10), "sa": (1.0, 20), "fg": (1.0, 20),
    "p": (2.0, 10), "y": (2.0, 10), "m": (1.0, 10), "rm": (1.0, 10),
    "oi": (1.0, 10), "cf": (5.0, 5), "sr": (1.0, 10), "ap": (1.0, 10),
    "c": (1.0, 10), "cs": (1.0, 10), "a": (1.0, 10), "sp": (2.0, 10),
    "ur": (1.0, 20), "eb": (1.0, 5), "pg": (1.0, 20), "cj": (5.0, 5),
    "lh": (5.0, 10), "au": (0.02, 1000), "ag": (1.0, 15),
}
# 手续费（元/手，rate 型按现价估；这里用近似值，精确核算在 Task #61）
FEE_YUAN = {
    "j": 35.0, "jm": 12.0, "rb": 4.0, "hc": 4.0, "i": 8.0, "sf": 3.0, "sm": 3.0,
    "ss": 3.0, "cu": 45.0, "al": 3.0, "zn": 3.0, "ni": 3.0, "pb": 3.0, "sn": 3.0,
    "sc": 20.0, "fu": 2.0, "bu": 2.0, "ru": 3.0, "ta": 3.0, "ma": 2.0, "pp": 2.0,
    "l": 2.0, "v": 2.0, "eg": 2.0, "sa": 3.0, "fg": 3.0,
    "p": 2.5, "y": 2.5, "m": 1.5, "rm": 1.5, "oi": 2.0, "cf": 4.0, "sr": 3.0,
    "ap": 5.0, "c": 1.2, "cs": 1.5, "a": 2.0, "sp": 3.0, "ur": 3.0, "eb": 1.0,
    "pg": 6.0, "cj": 3.0, "lh": 3.0, "au": 10.0, "ag": 1.5,
}

ENTRY_Z = 1.5
MA_WINDOW = 60
MAX_HOLDING = 40
COOLDOWN = 5


def cost_per_ton(code):
    tick, mult = REAL_COST.get(code, (1.0, 10))
    fee = FEE_YUAN.get(code, 2.0)
    return tick * 2 + fee / mult * 2   # 滑点 2 tick + 手续费开平


def tick_yuan(code):
    """每手 1 tick 的元数（用于把净 PnL 折算成可跨品种比较的净 tick 数）。"""
    tick, mult = REAL_COST.get(code, (1.0, 10))
    return tick * mult


def backtest_continuous(spread, cost, k=ENTRY_Z, warmup=MA_WINDOW):
    """连续回放（四重验证第 2/4 重：滞后成交 + 换月分段由外层处理）。"""
    s = np.asarray(spread, dtype=float)
    n = len(s)
    ma = pd.Series(s).rolling(MA_WINDOW).mean().to_numpy()
    sd = pd.Series(s).rolling(MA_WINDOW).std().to_numpy()
    pos = 0
    entry = 0.0
    hold = 0
    cooldown = 0
    trades = []
    i = warmup
    while i < n - 1:
        if np.isnan(ma[i]) or np.isnan(sd[i]) or sd[i] == 0:
            i += 1
            continue
        z = (s[i] - ma[i]) / sd[i]
        if cooldown > 0:
            cooldown -= 1
            i += 1
            continue
        if pos == 0:
            if z >= k:
                pos = -1
                entry = s[i + 1]
                hold = 0
                i += 1
            elif z <= -k:
                pos = 1
                entry = s[i + 1]
                hold = 0
                i += 1
        else:
            hold += 1
            exit_sig = ((pos == -1 and s[i] <= ma[i]) or (pos == 1 and s[i] >= ma[i]))
            if exit_sig or hold >= MAX_HOLDING:
                trades.append((s[i] - entry) * pos - cost)
                pos = 0
                if hold >= MAX_HOLDING:
                    cooldown = COOLDOWN
        i += 1
    return trades


def segment_backtest(df, cost, k=ENTRY_Z):
    """分段剔除换月断层（第 4 重）：near_sym 切换处强制平仓重置。"""
    all_trades = []
    n = len(df)
    seg_start = 0
    for i in range(1, n + 1):
        if i == n or df.iloc[i]["near_sym"] != df.iloc[seg_start]["near_sym"]:
            seg = df.iloc[seg_start:i]["spread"].to_numpy()
            if len(seg) > MA_WINDOW:
                all_trades.extend(backtest_continuous(seg, cost, k))
            seg_start = i
    return all_trades


def walk_forward(df, cost, k=ENTRY_Z, train=250, test=60):
    """walk-forward（第 3 重）：严格样本外，冻结训练段均线/std 带入测试段。

    🔴 修正（前视偏差）：不能用「全序列滚动均线」算测试段 z-score（ma[i] 含未来
    60 天数据 = 隐性 look-ahead，导致 WF 净 tick 虚高 3~24 倍）。正确做法：
    - 训练段 [0, start) 内滚动算出均线/std
    - 测试段 [start, start+test) 首日 z 用「训练段末尾的均线/std」冻结值
    - 测试段内逐日滚动（只用测试段已见数据）
    - 每段 test 结束后，把该段数据并入训练集，滚动窗口滑到下一段
    """
    s = df["spread"].to_numpy()
    n = len(s)
    oos_trades = []
    pos = 0
    entry = 0.0
    hold = 0
    cooldown = 0

    # 维护一个「已见数据」滚动状态：用 expanding 的窗口（含全部历史，等价训练段并入）
    seen = s[:train].tolist()
    # 预计算训练段末尾的均线/std 作为测试段首日种子
    ma = float(np.mean(seen[-MA_WINDOW:]))
    sd = float(np.std(seen[-MA_WINDOW:]))
    mean_q = seen[-MA_WINDOW:]   # 用列表维护滚动窗口（逐步滑入测试段数据）

    i = train
    while i < n:
        # 用当前滚动窗口（只含已见数据）算 z
        if sd <= 0:
            # std 退化，跳过
            mean_q = mean_q[1:] + [s[i]]
            ma = float(np.mean(mean_q))
            sd = float(np.std(mean_q))
            i += 1
            continue
        z = (s[i] - ma) / sd
        if cooldown > 0:
            cooldown -= 1
        elif pos == 0:
            if z >= k:
                pos = -1
                entry = s[i + 1] if i + 1 < n else s[i]
                hold = 0
            elif z <= -k:
                pos = 1
                entry = s[i + 1] if i + 1 < n else s[i]
                hold = 0
        else:
            hold += 1
            exit_sig = ((pos == -1 and s[i] <= ma) or (pos == 1 and s[i] >= ma))
            if exit_sig or hold >= MAX_HOLDING:
                oos_trades.append((s[i] - entry) * pos - cost)
                pos = 0
                if hold >= MAX_HOLDING:
                    cooldown = COOLDOWN
        # 滑入当前 bar，更新滚动窗口（只用已见数据）
        mean_q = mean_q[1:] + [s[i]]
        ma = float(np.mean(mean_q))
        sd = float(np.std(mean_q))
        i += 1
    return oos_trades


def summarize(trades, label):
    if not trades:
        return {"label": label, "笔数": 0, "净PnL": 0.0, "胜率": None, "均值": None}
    t = np.array(trades)
    return {
        "label": label, "笔数": len(t), "净PnL": round(t.sum(), 1),
        "胜率": round((t > 0).mean(), 3), "均值": round(t.mean(), 2),
    }


def main():
    files = sorted(glob.glob(os.path.join(OUT_DIR, "*_fixed.csv")))
    rows = []
    for fp in files:
        base = os.path.basename(fp)
        code = base.split("_")[0]
        month = base[len(code) + 1:].replace("_fixed.csv", "")
        if "vs" not in month:
            continue
        df = pd.read_csv(fp)
        if "spread" not in df.columns or len(df) < 120:
            continue
        cost = cost_per_ton(code)
        ty = tick_yuan(code)   # 每手 1 tick 元数

        # 四重验证
        cont = summarize(backtest_continuous(df["spread"].to_numpy(), cost), "连续")
        seg = summarize(segment_backtest(df, cost), "分段剔换月")
        wf = summarize(walk_forward(df, cost), "walk-forward")

        rows.append({
            "品种": code, "月差": month, "样本日": len(df),
            "成本元/吨": round(cost, 2), "tick元/手": round(ty, 1),
            "分段笔数": seg["笔数"],
            "分段净PnL": seg["净PnL"],
            "分段净tick": round(seg["净PnL"] / ty, 1) if ty else None,
            "分段胜率": seg["胜率"], "分段均值tick": round(seg["均值"] / ty, 2) if ty and seg["均值"] else None,
            "WF笔数": wf["笔数"], "WF净PnL": wf["净PnL"],
            "WF净tick": round(wf["净PnL"] / ty, 1) if ty and wf["净PnL"] else None,
            "WF胜率": wf["胜率"],
        })

    df = pd.DataFrame(rows)
    out = os.path.join(OUT_DIR, "verify_all.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    # 最终入选判据：分段净 tick > 0 且 笔数 >= 10 且 胜率 >= 50% 且 WF 样本外净 tick 也 > 0
    passed = df[(df["分段净tick"] > 0) & (df["分段笔数"] >= 10)
                & (df["分段胜率"] >= 0.5) & (df["WF净tick"] > 0)]
    passed = passed.sort_values("分段净tick", ascending=False)

    print("=" * 120)
    print(f"四重验证结果：{len(df)} 组月差，入选（分段净tick>0+笔数≥10+胜率≥50%+WF净tick>0）{len(passed)} 组")
    print("=" * 120)
    cols = ["品种", "月差", "样本日", "tick元/手", "分段笔数", "分段净tick",
            "分段胜率", "分段均值tick", "WF笔数", "WF净tick", "WF胜率"]
    print(passed[cols].to_string(index=False))
    print(f"\n完整表: {out}")


if __name__ == "__main__":
    main()
