"""6 因子样本外验证"""

import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest
from ga_six_factor import optimize_six_factor

symbol = "rb"
df = load_daily(symbol)
print(f"总数据: {len(df)} 根", flush=True)

df_train = df.iloc[:400]
df_test = df.iloc[400:600]
print(f"训练集: {len(df_train)} 根", flush=True)
print(f"验证集: {len(df_test)} 根", flush=True)
print(flush=True)

# 训练
print("开始 GA 训练...", flush=True)
result = optimize_six_factor(symbol, df_daily=df_train, pop_size=20, n_gen=10, verbose=True, tail=None)
best_w = result["best_weights"]
print(f"\n训练最优权重: {best_w}", flush=True)
print(f"训练集 expR: {result['best_expR']:+.4f} (trades={result['n_trades']})", flush=True)
print(flush=True)

# 验证
cfg_base = copy.deepcopy(DEFAULT_CONFIG)
r_base_test = walk_forward_backtest(symbol, cfg=cfg_base, window=300, min_bars=60, df_in=df_test)

cfg_sf = copy.deepcopy(DEFAULT_CONFIG)
cfg_sf["subfactor_weights"] = best_w
r_sf_test = walk_forward_backtest(symbol, cfg=cfg_sf, window=300, min_bars=60, df_in=df_test)

print(f"基准 - 验证集: expR={r_base_test['expR']:+.4f} trades={r_base_test['trades']}", flush=True)
print(f"6因子 - 验证集: expR={r_sf_test['expR']:+.4f} trades={r_sf_test['trades']}", flush=True)

if r_base_test["expR"] != 0:
    pct = (r_sf_test["expR"] - r_base_test["expR"]) / abs(r_base_test["expR"]) * 100
    print(f"样本外提升: {pct:+.1f}%", flush=True)
else:
    diff = r_sf_test["expR"] - r_base_test["expR"]
    print(f"样本外差值: {diff:+.4f}", flush=True)
