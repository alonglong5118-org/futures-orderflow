# 信号触发模块（signal_trigger_utils）

## 模块简介

信号触发模块是从 `four_dim_strategy.py` pipeline 的触发判断逻辑中提取的纯函数工具集，覆盖 threshold 模式下的核心决策链路。该模块负责将 F（基本面）、C（资金面）背景偏置与 T（技术面）阈值结合，完成最终的信号触发判断。

### 核心设计原则

- **纯函数**：相同输入产生相同输出，无副作用
- **可独立测试**：不依赖 pandas / 数据库 / 外部模块
- **历史 bug 修复覆盖**：P-C 硬否决阈值过高、P-B 同向确认空转、T 方向为 0 误触发

---

## 核心函数列表

### compute_bias_FC

计算 F/C 合成背景偏置（非技术面背景偏置）。

```python
def compute_bias_FC(F: float, C: float) -> float
```

**公式**：

$$
\text{bias\_FC} = 0.25 \times F + 0.15 \times C
$$

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `F` | `float` | 基本面得分 |
| `C` | `float` | 资金面得分 |

**返回值**：`float`，合成背景偏置值，保留 1 位小数。

**设计意图**：让 F/C 真正参与触发决策，而不是原来的"只看 T，F/C 形同虚设"。

---

### check_hard_veto

F/C 反向硬否决判断。

```python
def check_hard_veto(
    bias_FC: float,
    dir_T: int,
    fc_hard: float = 25.0,
) -> Tuple[bool, str]
```

**规则**：

1. `bias_FC` 的绝对值 >= `fc_hard` 阈值
2. 且 `bias_FC` 的符号与 `dir_T` 相反
3. → 触发硬否决，信号被抑制

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `bias_FC` | `float` | — | F/C 合成背景偏置 |
| `dir_T` | `int` | — | T 方向（1 多 / -1 空 / 0 无方向） |
| `fc_hard` | `float` | `25.0` | 硬否决阈值 |

**返回值**：`(hard_veto: bool, reason: str)`

- `hard_veto`：是否触发硬否决
- `reason`：否决原因描述（未否决时为空字符串）

**历史 bug 修复（P-C）**：原硬否决用 `bias_G≥60`，几乎永远达不到，F/C 形同虚设。修复后改用 `bias_FC + fc_hard=25`，阈值可达，F/C 真正有否决权。

---

### check_fc_confirmation

F/C 同向确认判断。

```python
def check_fc_confirmation(
    bias_FC: float,
    dir_T: int,
    fc_confirm: float = 25.0,
) -> bool
```

**规则**：

1. `bias_FC` 的符号与 `dir_T` 相同
2. 且 `abs(bias_FC) >= fc_confirm` 阈值
3. → 同向确认成立，T 阈值可以降低（正向加成）

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `bias_FC` | `float` | — | F/C 合成背景偏置 |
| `dir_T` | `int` | — | T 方向 |
| `fc_confirm` | `float` | `25.0` | 同向确认阈值 |

**返回值**：`bool`，同向确认是否成立。

**历史 bug 修复（P-B）**：原逻辑 F/C 强同向但没有实际降阈值，属于"空转"。

---

### compute_effective_threshold

计算有效 T 阈值（根据同向确认结果调整）。

```python
def compute_effective_threshold(
    T_thresh_eff: float,
    fc_confirmed: bool,
    confirm_relief: float = 0.85,
) -> float
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `T_thresh_eff` | `float` | — | 基础 T 阈值 |
| `fc_confirmed` | `bool` | — | F/C 同向确认是否成立 |
| `confirm_relief` | `float` | `0.85` | 确认后的阈值折扣系数 |

**返回值**：`float`，调整后的有效阈值。

- 同向确认成立时：`T_thresh_eff * confirm_relief`
- 未确认时：保持原阈值

---

## 使用示例

```python
from signal_trigger_utils import (
    compute_bias_FC,
    check_hard_veto,
    check_fc_confirmation,
    compute_effective_threshold,
)

# 1. 计算 F/C 背景偏置
bias_FC = compute_bias_FC(F=60.0, C=40.0)
# bias_FC = 0.25*60 + 0.15*40 = 21.0

# 2. 检查硬否决
vetoed, reason = check_hard_veto(bias_FC=21.0, dir_T=1, fc_hard=25.0)
# vetoed = False（21.0 < 25，未达硬否决阈值）

# 3. 检查同向确认
confirmed = check_fc_confirmation(bias_FC=21.0, dir_T=1, fc_confirm=25.0)
# confirmed = False（21.0 < 25，未达确认阈值）

# 4. 计算有效阈值
eff_thresh = compute_effective_threshold(
    T_thresh_eff=70.0,
    fc_confirmed=confirmed,
    confirm_relief=0.85,
)
# eff_thresh = 70.0（未确认，保持原阈值）
```

**强同向场景**：

```python
bias_FC = compute_bias_FC(F=80.0, C=60.0)  # = 29.0
confirmed = check_fc_confirmation(bias_FC=29.0, dir_T=1)  # True
eff_thresh = compute_effective_threshold(70.0, confirmed)
# eff_thresh = 70.0 * 0.85 = 59.5（阈值降低，更容易触发）
```

**反向硬否决场景**：

```python
bias_FC = compute_bias_FC(F=-80.0, C=-60.0)  # = -29.0
vetoed, reason = check_hard_veto(bias_FC=-29.0, dir_T=1)
# vetoed = True
# reason = "F/C反向硬否决(|bias_FC|=29.0≥25)"
```

---

## 注意事项

1. **方向为 0 时安全**：`dir_T=0` 时，硬否决和同向确认均返回 `False`，不会误触发。
2. **bias_FC 为 0 时安全**：同向确认返回 `False`，硬否决也不会触发。
3. **阈值参数可调**：`fc_hard` 和 `fc_confirm` 默认均为 25.0，可根据品种特性调整。
4. **confirm_relief 默认 0.85**：即同向确认时阈值打 85 折，约降低 15%。
5. **不包含 combined 方向模式**：该模式用得少且逻辑复杂，未纳入纯函数工具集。
6. **不包含 sentiment 情绪过滤**：依赖 `sentiment_engine`，属于非核心路径。
