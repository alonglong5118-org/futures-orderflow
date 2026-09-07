#!/usr/bin/env python3
"""Layer0 市场状态引擎校准（2026-09-07）。

验证驱动金字塔「行情门」的 regime 分类器（classify_regime_array）是否真能预测后续收益。
方法：全品种日线 → 逐 bar 因果分类 regime（无参数、只用过去 SMA20/ATR，天然防前视）
→ 按 regime 聚后续 5/10/20 日收益率 → 看「趋势」态是否显著优于「震荡/波动」。

判定：趋势态后续收益明显高于其他态 → 行情门（仅趋势态放行加仓）有依据；
      趋势态与其他态无分离 → 状态信号是噪声，Layer0 需重校准。
"""
import sys
import numpy as np
import pandas as pd

HERE = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, HERE)
from four_dim_strategy import load_daily, SYMBOLS
from strategy_layer import classify_regime_array, _atr_array, _sma_array

CODE_NAME = {0: "未知", 1: "波动", 2: "震荡", 3: "趋势", 4: "过渡"}


def sma20_slope_prev_arr(close):
    sma20 = _sma_array(close, 20)
    return np.concatenate([np.full(4, np.nan), sma20[:-4]])


def main():
    horizons = [5, 10, 20]
    # 聚合：regime -> 各 horizon 的 [sum_ret, n, wins]
    agg = {c: {h: [0.0, 0, 0] for h in horizons} for c in CODE_NAME}
    n_sym = 0
    for sym in SYMBOLS:
        df = load_daily(sym)
        if df is None or len(df) < 120:
            continue
        close = df["close"].values.astype(float)
        n = len(close)
        atr14 = _atr_array(df["high"].values.astype(float), df["low"].values.astype(float), close, 14)
        sma20 = _sma_array(close, 20)
        slope_prev = sma20_slope_prev_arr(close)
        codes = classify_regime_array(close, atr14, sma20, slope_prev)
        n_sym += 1
        rets = np.diff(close) / close[:-1]
        for h in horizons:
            fwd = np.concatenate([rets[: n - 1 - h + 1] if False else np.zeros(h - 1),
                                  close[h:] / close[:-h] - 1])
            # 更稳妥：逐 bar 前推
            fwd = np.full(n, np.nan)
            for t in range(n - h):
                fwd[t] = close[t + h] / close[t] - 1
            for t in range(n):
                c = codes[t]
                if np.isnan(c):
                    continue
                ci = int(c)
                fr = fwd[t]
                if np.isnan(fr):
                    continue
                bucket = agg[ci][h]
                bucket[0] += fr
                bucket[1] += 1
                if fr > 0:
                    bucket[2] += 1

    print(f"Layer0 状态引擎校准（{n_sym} 品种，因果 regime 分类，按态聚后续收益）")
    print("=" * 78)
    for h in horizons:
        print(f"\n── 后续 {h} 日收益率 ──")
        print(f"{'状态':6} {'n':>8} {'均值收益':>10} {'胜率':>8}")
        rows = []
        for c in sorted(CODE_NAME):
            b = agg[c][h]
            if b[1] == 0:
                continue
            mean = b[0] / b[1]
            wr = b[2] / b[1]
            rows.append((c, b[1], mean, wr))
            print(f"{CODE_NAME[c]:6} {b[1]:8d} {mean*100:9.3f}% {wr*100:7.1f}%")
        # 趋势 vs 非趋势分离度
        trend = next((r for r in rows if r[0] == 3), None)
        others = [r for r in rows if r[0] != 3 and r[0] != 0]
        if trend and others:
            om = np.mean([r[2] for r in others])
            sep = trend[2] - om
            best = max(rows, key=lambda r: r[2])
            print(f"  趋势态均值 {trend[2]*100:.3f}% vs 其他态均值 {om*100:.3f}% → 分离 {sep*100:+.3f}% "
                  f"| 最高收益态={CODE_NAME[best[0]]}({best[2]*100:.3f}%)")
            verdict = "✅趋势态预测力成立" if sep > 0.1 else ("⚠️趋势态无超额预测力" if sep < 0.05 else "➖弱分离")
            print(f"  判定: {verdict}")
    print("=" * 78)


if __name__ == "__main__":
    main()
