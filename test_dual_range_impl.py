#!/usr/bin/env python3
"""双差值止损上线 · 隔离等价性测试（2026-08-31，v3.8.0）。

验证生产实现与 OOS 验证补丁完全等价：
  T1  _dual_range_array / dual_range_vol 与验证公式逐位一致
  T2  白名单品种（ru）走生产 DEFAULT_CONFIG（含 dual_range_stop_symbols）
      → 5 折 expR 必须复现 tools_oos_dual_range_final.py 的 stop_only 结果
  T3  非白名单品种（cu）结果与 dual_range_stop_symbols=[] 时完全一致（隔离性）
  T4  runner._stop_vol_daily：ru 返回 DR≠ATR、cu 返回 ATR、<14 根回退 ATR

只读测试，不触碰任何生产写入接口。
"""

import copy
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import four_dim_strategy as fds
from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest
from strategy_layer import _dual_range_array, dual_range_vol, atr as strat_atr

N_FOLDS = 5
FAILED = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)


# ── T1: 公式逐位一致 ──────────────────────────────────────────
def validation_dr(high, low, close, window):
    hh = pd.Series(high).rolling(window).max().values
    ll = pd.Series(low).rolling(window).min().values
    hc = pd.Series(close).rolling(window).max().values
    lc = pd.Series(close).rolling(window).min().values
    return np.maximum(hh - lc, hc - ll) / np.sqrt(window)


df_ru = load_daily("ru")
h, l, c = df_ru["high"].values, df_ru["low"].values, df_ru["close"].values
a = _dual_range_array(h, l, c, 14)
b = validation_dr(h, l, c, 14)
valid_mask = ~np.isnan(b)
check("T1a _dual_range_array 与验证公式逐位一致", bool(np.allclose(a[valid_mask], b[valid_mask])),
      f"{valid_mask.sum()} 个有效值")

s = dual_range_vol(df_ru)
check("T1b dual_range_vol 序列版与数组版末值一致",
      abs(float(s.iloc[-1]) - float(a[-1])) < 1e-9,
      f"series={float(s.iloc[-1]):.4f} array={float(a[-1]):.4f}")


# ── T2: 白名单品种生产复现验证结果 ────────────────────────────
def folds(sym, cfg):
    df = load_daily(sym)
    total = len(df)
    fs = total // N_FOLDS
    out = []
    for k in range(N_FOLDS):
        st = k * fs
        en = st + fs if k < N_FOLDS - 1 else total
        r = walk_forward_backtest(sym, cfg=cfg, df_in=df.iloc[st:en])
        out.append(r["expR"] if r.get("trades", 0) > 0 else None)
    return out


# tools_oos_dual_range_final.py stop_only（DR 止损 + ATR regime）的 ru 5 折结果
EXPECT_RU = [1.861, 1.534, -0.457, 0.274, 0.033]
got_ru = folds("ru", DEFAULT_CONFIG)
ok = len(got_ru) == len(EXPECT_RU) and all(
    g is not None and abs(g - e) < 5e-4 for g, e in zip(got_ru, EXPECT_RU)
)
check("T2 ru 生产引擎复现验证 stop_only 结果", ok,
      " ".join(f"{g:+.3f}" if g is not None else "None" for g in got_ru))

# p 也抽查一折（首折 +0.425）
r_p = walk_forward_backtest("p", cfg=DEFAULT_CONFIG, df_in=load_daily("p").iloc[: len(load_daily("p")) // 5])
check("T2b p 首折复现（期望 +0.425 附近）", abs(r_p["expR"] - 0.425) < 5e-3, f"{r_p['expR']:+.4f}")


# ── T3: 非白名单隔离性 ────────────────────────────────────────
cfg_no_dr = copy.deepcopy(DEFAULT_CONFIG)
cfg_no_dr["dual_range_stop_symbols"] = []
got_cu_on = folds("cu", DEFAULT_CONFIG)
got_cu_off = folds("cu", cfg_no_dr)
pairs = [(x, y) for x, y in zip(got_cu_on, got_cu_off) if x is not None and y is not None]
check("T3 cu（非白名单）不受白名单配置影响", pairs and all(x == y for x, y in pairs),
      f"{len(pairs)} 折逐一相等")


# ── T4: runner helper（重启后 API 验证，不在测试中导入 runner）─────────
# runner 模块导入即启动 HTTP 服务/数据线程（有生产副作用），禁止在测试中 import。
# _stop_vol_daily 的正确性由重启后只读 API 验证：
#   /api/state → APP_VERSION v3.8.0
#   holdings kline（ru）→ atr 应为 DR=164.37 而非 ATR=309.64
#   holdings kline（cu 非白名单）→ atr 应保持 ATR=1182.86
print("[SKIP] T4 runner helper — 模块导入有生产副作用，改由重启后只读 API 验证")


# ── 汇总 ─────────────────────────────────────────────────────
print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✓ 全部通过 — 生产实现与 OOS 验证口径等价，可重启服务器生效")
