# 止盈模块（take_profit_utils）

## 模块简介

止盈模块提供止盈止损参数计算和逐 bar 出场模拟的纯函数工具，从 `four_dim_strategy.py` 中提取。

### 核心功能

1. **`calc_exit_plan()`** — 计算 stop / t1 / t2 / 尾仓参数
2. **`sim_exit_bars()`** — 逐 bar 模拟出场（止损 / 止盈 / 尾仓移动止损）

### 设计原则

- **纯函数**：相同输入 → 相同输出
- **无副作用**：不读文件、不写全局、不抛非预期异常
- **可独立测试**：不依赖 pandas / 数据库 / 外部模块

---

## 核心函数列表

### calc_exit_plan

计算一笔交易的止损 / 止盈 / 尾仓参数。

```python
def calc_exit_plan(
    entry: float,
    dir_T: float,
    atr_val: float,
    stop_atr_mult: float = 1.5,
    rr_ratio: float = 2.0,
    regime_stop_coef: float = 1.0,
    tail_enabled: bool = False,
    tail_trail_R: float = 2.0,
    tail_pct: float = 0.25,
) -> Dict[str, Any]
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `entry` | `float` | — | 入场价 |
| `dir_T` | `float` | — | 方向（>0 多，<0 空） |
| `atr_val` | `float` | — | ATR 值 |
| `stop_atr_mult` | `float` | `1.5` | 止损 ATR 倍数（基础） |
| `rr_ratio` | `float` | `2.0` | 盈亏比（t2 = rr_ratio × stop_dist） |
| `regime_stop_coef` | `float` | `1.0` | regime 止损系数（趋势 1.0 / 波动 1.2 / 震荡 1.0） |
| `tail_enabled` | `bool` | `False` | 是否启用尾仓 |
| `tail_trail_R` | `float` | `2.0` | 尾仓跟踪距离（单位：1R） |
| `tail_pct` | `float` | `0.25` | 尾仓比例 |

**返回值**：

```python
{
    "stop": float,             # 止损价
    "t1": float,               # 第一止盈价（1R，平半）
    "t2": float,               # 第二止盈价（rr_ratio R，全平或进入尾仓）
    "stop_dist": float,        # 止损距离（绝对值，正数）
    "tail_enabled": bool,      # 尾仓是否启用
    "tail_stop_dist": float,   # 尾仓跟踪距离（绝对值 = tail_trail_R × stop_dist）
    "tail_pct": float,         # 尾仓比例
}
```

---

### 计算公式

**止损距离**：

$$
\text{stop\_dist} = \text{stop\_atr\_mult} \times \text{regime\_stop\_coef} \times \text{ATR}
$$

**多单**：

| 价位 | 公式 |
|------|------|
| 止损 | `entry - stop_dist` |
| t1（1R） | `entry + stop_dist` |
| t2（rr_ratio R） | `entry + rr_ratio × stop_dist` |

**空单**：

| 价位 | 公式 |
|------|------|
| 止损 | `entry + stop_dist` |
| t1（1R） | `entry - stop_dist` |
| t2（rr_ratio R） | `entry - rr_ratio × stop_dist` |

**尾仓跟踪距离**：

$$
\text{tail\_stop\_dist} = \text{tail\_trail\_R} \times \text{stop\_dist}
$$

---

### sim_exit_bars

逐 bar 模拟出场（止损 / 止盈 / 尾仓移动止损）。

```python
def sim_exit_bars(...)
```

逐 bar 模拟价格走势，判断何时触发止损、止盈或尾仓移动止损。支持分批止盈（t1 平半、t2 全平 / 进入尾仓）和尾仓跟踪止损。

---

## 使用示例

```python
from take_profit_utils import calc_exit_plan

# 场景 1：多单，基础参数
plan = calc_exit_plan(
    entry=3000.0,
    dir_T=1.0,          # 多单
    atr_val=40.0,       # ATR = 40
    stop_atr_mult=1.5,  # 止损 = 1.5 ATR
    rr_ratio=2.0,       # 盈亏比 2:1
)
# stop_dist = 1.5 * 1.0 * 40 = 60
# stop = 3000 - 60 = 2940.0
# t1   = 3000 + 60 = 3060.0（1R，平半）
# t2   = 3000 + 2*60 = 3120.0（2R，全平）

# 场景 2：空单，启用尾仓
plan = calc_exit_plan(
    entry=3000.0,
    dir_T=-1.0,         # 空单
    atr_val=40.0,
    stop_atr_mult=1.5,
    rr_ratio=2.0,
    tail_enabled=True,
    tail_trail_R=2.0,   # 尾仓跟踪 2R
    tail_pct=0.25,      # 留 25% 尾仓
)
# stop = 3000 + 60 = 3060.0
# t1   = 3000 - 60 = 2940.0
# t2   = 3000 - 120 = 2880.0
# tail_stop_dist = 2.0 * 60 = 120.0
# tail_pct = 0.25

# 场景 3：波动 regime，止损放宽
plan = calc_exit_plan(
    entry=3000.0,
    dir_T=1.0,
    atr_val=40.0,
    stop_atr_mult=1.5,
    rr_ratio=2.0,
    regime_stop_coef=1.2,  # 波动市，止损放大 20%
)
# stop_dist = 1.5 * 1.2 * 40 = 72
# stop = 3000 - 72 = 2928.0
# t2   = 3000 + 2*72 = 3144.0
```

---

## 分批止盈与尾仓策略

### 标准分批止盈（无尾仓）

```
入场 → t1 触发 → 平 50% 仓位
     → t2 触发 → 平剩余 50%（全部离场）
```

### 尾仓策略（启用 tail）

```
入场 → t1 触发 → 平 50% 仓位
     → t2 触发 → 平 25%，留 25% 尾仓
              → 尾仓止损 = t2 ± tail_stop_dist（方向取决于多空）
              → 尾仓止损随价格移动（跟踪止损）
              → 价格回撤触及尾仓止损 → 全部离场
```

**多单尾仓止损初始值**：`t2 - tail_stop_dist`

**空单尾仓止损初始值**：`t2 + tail_stop_dist`

---

## 历史 bug 覆盖

| Bug | 描述 | 修复 |
|-----|------|------|
| 方向搞反 | 多单止盈在入场下方 / 空单止损在入场下方 | 方向判断统一用 `is_long = dir_T > 0` |
| regime 系数漏乘 | `stop_atr_mult` 没有 × `regime_coef.stop` | 止损距离 = `stop_atr_mult × regime_stop_coef × ATR` |
| 尾仓跟踪方向搞反 | 多单用 `min` 而不是 `max` 更新尾仓止损 | 多单尾仓止损上移用 `max`，空单用 `min` |
| t2 触发后未进入尾仓态 | 直接全平，尾仓逻辑空转 | t2 触发后进入尾仓跟踪模式 |
| 尾仓止损初始值算错 | 应该是 t2 ± tail_stop_dist | 修正初始值计算 |

---

## 注意事项

1. **所有价格保留 2 位小数**：返回的 stop / t1 / t2 均经过 `round(..., 2)` 处理。
2. **方向由 `dir_T` 符号决定**：`dir_T > 0` 为多，`dir_T < 0` 为空。
3. **regime 止损系数影响盈亏比**：止损放大时，t2 距离也同比例放大，保持 `rr_ratio` 不变。
4. **尾仓比例默认 25%**：即 t2 触发后平 75%，留 25% 做尾仓。
5. **尾仓跟踪距离以 1R 为单位**：`tail_trail_R=2.0` 表示跟踪距离为 2 倍初始止损距离。
6. **纯函数可测试**：所有计算逻辑不依赖外部状态，便于单元测试覆盖边界条件。
