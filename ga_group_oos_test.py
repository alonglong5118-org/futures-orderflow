"""板块级 6 因子样本外验证（前 N 训练 / 后 M 验证）"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np

from four_dim_strategy import DEFAULT_CONFIG, walk_forward_backtest
from ga_group_six_factor import load_group_data

GROUP = "黑系"
TRAIN_BARS = 350  # 训练集 N 根
TEST_BARS = 200  # 验证集 M 根
POP = 20
GEN = 10

print(f"=== 板块级 6 因子 OOS 验证 ===", flush=True)
print(f"板块: {GROUP}", flush=True)
print(f"训练集: 最近-{TRAIN_BARS + TEST_BARS} ~ 最近-{TEST_BARS}", flush=True)
print(f"验证集: 最近-{TEST_BARS} ~ 最新", flush=True)
print(flush=True)

# 加载全量数据
group_data = load_group_data(GROUP, min_bars=TRAIN_BARS + TEST_BARS, tail=0)
print(f"有效品种: {len(group_data)} 个", flush=True)
for sym in sorted(group_data.keys()):
    print(f"  {sym}: {len(group_data[sym])} 根", flush=True)
print(flush=True)

# 切分训练/验证
train_data = {}
test_data = {}
for sym, df in group_data.items():
    total = len(df)
    train_end = total - TEST_BARS
    train_data[sym] = df.iloc[max(0, train_end - TRAIN_BARS) : train_end]
    test_data[sym] = df.iloc[train_end:]
    print(f"  {sym}: train={len(train_data[sym])} test={len(test_data[sym])}", flush=True)

print(flush=True)
print("=== 训练集 GA 优化 ===", flush=True)

# 直接用 optimize_group 但喂训练数据——需要改造一下
# 先把训练数据存到临时变量，手动调用评估
import random

from deap import algorithms, base, creator, tools

from ga_group_six_factor import (
    GROUP_CXPB,
    GROUP_MUTPB,
    SF_NAMES,
    _evaluate,
    _ind_to_weights,
)

n_genes = len(SF_NAMES)
for _name in ["FitnessGroup", "IndividualGroup"]:
    if _name in dir(creator):
        delattr(creator, _name)

creator.create("FitnessGroup", base.Fitness, weights=(1.0,))
creator.create("IndividualGroup", list, fitness=creator.FitnessGroup)

toolbox = base.Toolbox()
toolbox.register("attr_float", random.uniform, 0, 1)
toolbox.register("individual", tools.initRepeat, creator.IndividualGroup, toolbox.attr_float, n=n_genes)
toolbox.register("population", tools.initRepeat, list, toolbox.individual)


def _eval_ind(ind):
    return _evaluate(ind, train_data)


toolbox.register("evaluate", _eval_ind)
toolbox.register("mate", tools.cxBlend, alpha=0.3)
toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.3)
toolbox.register("select", tools.selTournament, tournsize=3)

pop = toolbox.population(n=POP)
hof = tools.HallOfFame(5)
stats = tools.Statistics(lambda ind: ind.fitness.values)
stats.register("avg", lambda x: float(np.mean(x)))
stats.register("max", lambda x: float(np.max(x)))

for gen in range(GEN):
    offspring = algorithms.varAnd(pop, toolbox, cxpb=GROUP_CXPB, mutpb=GROUP_MUTPB)
    fits = list(map(toolbox.evaluate, offspring))
    for ind, fit in zip(offspring, fits):
        ind.fitness.values = fit
    hof.update(offspring)
    pop = toolbox.select(offspring, k=len(pop))
    record = stats.compile(pop)
    print(f"  Gen {gen + 1:2d}: best={record['max']:+.4f}  avg={record['avg']:+.4f}", flush=True)
    if gen >= 5 and all(h["max"] >= record["max"] - 0.001 for h in [{"max": stats.compile(pop)["max"]}] + []):
        pass  # 简单起见不做早停

best = hof[0]
best_w = _ind_to_weights(best)
print(f"\n训练最优权重: {best_w}", flush=True)


# 训练集基准
def avg_expR(data, cfg_override=None):
    import copy

    expRs = []
    for sym, df in data.items():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if cfg_override:
            cfg.update(cfg_override)
        r = walk_forward_backtest(sym, cfg=cfg, window=300, min_bars=60, df_in=df)
        if r["trades"] >= 5:
            expRs.append(r["expR"])
    return float(np.mean(expRs)) if expRs else 0.0


train_base = avg_expR(train_data)
train_sf = avg_expR(train_data, {"subfactor_weights": best_w})
test_base = avg_expR(test_data)
test_sf = avg_expR(test_data, {"subfactor_weights": best_w})

print(flush=True)
print("=== 结果对比 ===", flush=True)
print(f"{'':12s} {'基准':>10s} {'6因子':>10s} {'变化':>10s}", flush=True)
print("-" * 46, flush=True)
print(f"{'训练集':12s} {train_base:+.4f}    {train_sf:+.4f}    ", end="", flush=True)
if train_base != 0:
    print(f"{(train_sf - train_base) / abs(train_base) * 100:+.1f}%", flush=True)
else:
    print(f"{train_sf - train_base:+.3f}", flush=True)
print(f"{'验证集':12s} {test_base:+.4f}    {test_sf:+.4f}    ", end="", flush=True)
if test_base != 0:
    print(f"{(test_sf - test_base) / abs(test_base) * 100:+.1f}%", flush=True)
else:
    print(f"{test_sf - test_base:+.3f}", flush=True)

# 过拟合度：训练提升 vs 验证提升的比值
train_gain = train_sf - train_base
test_gain = test_sf - test_base
print(flush=True)
print(f"训练集提升: {train_gain:+.4f}", flush=True)
print(f"验证集提升: {test_gain:+.4f}", flush=True)
if abs(train_gain) > 0.001:
    print(f"过拟合系数: {test_gain / train_gain:.2f} (1.0=无过拟合, <0=完全失效)", flush=True)
