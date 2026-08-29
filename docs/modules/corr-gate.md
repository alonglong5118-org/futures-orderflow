# 相关性闸门模块（corr_gate_utils）

## 模块简介

相关性闸门（corr_gate）模块提供 T/C 相关性降权的纯计算逻辑，从 `four_dim_strategy.py` 中提取，便于单元测试。

### 核心思想

当 T（趋势维度）和 C（资金维度）高度相关时，说明两个维度提供的信息冗余。此时将绝对值较小的那一维强制降为 0，避免冗余维度重复加权，提高信号熵纯度。

### 历史背景（决策 26）

**问题**：原修复只改了文本描述，权重并未实际降权（空转）。

**修复**：`|corr(T,C)| > gate` 时，把 T 和 C 中绝对值较小的一维强制降为 0。

---

## 核心函数列表

### _pearson_corr

计算皮尔逊相关系数（不依赖 numpy，纯 Python 实现）。

```python
def _pearson_corr(x, y) -> float or None
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `x` | `list` | 第一组数据序列 |
| `y` | `list` | 第二组数据序列 |

**返回值**：`float` 或 `None`

- 正常返回：相关系数，范围 `[-1.0, 1.0]`
- 无法计算时返回 `None`（数据不足、长度不一致、方差为 0）

**特点**：

- 纯 Python 实现，不依赖 numpy
- 浮点误差保护：结果限制在 `[-1.0, 1.0]`
- 方差为 0 时返回 `None`（避免除零）

---

### apply_corr_gate

应用相关性闸门：如果 T 和 C 高度相关，降权较弱的那一维。

```python
def apply_corr_gate(
    T_score,
    C_score,
    corr_hist=None,
    gate=0.70,
    min_history=10,
) -> dict
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `T_score` | `float` | — | 趋势维度得分（T_D） |
| `C_score` | `float` | — | 资金维度得分（C） |
| `corr_hist` | `list` | `None` | T 和 C 的历史序列，格式为 `[[T_val, C_val], ...]` |
| `gate` | `float` | `0.70` | 相关性阈值 |
| `min_history` | `int` | `10` | 最少历史样本数 |

**返回值**：

```python
{
    "T": float,            # 处理后的 T 得分
    "C": float,            # 处理后的 C 得分
    "corr": float or None, # 相关系数
    "action": str,         # 动作描述
    "applied": bool,       # 是否触发了降权
    "dropped": str,        # 被降权的维度："T"/"C"/"none"
}
```

---

## 处理规则

1. **历史数据不足**（`< min_history` 或 `corr_hist is None`）→ 不处理，正常计权
2. **计算相关系数**：使用皮尔逊相关系数
3. **`|corr| > gate`** → 降权（把绝对值较小的那一维设为 0）
4. **`|corr| <= gate`** → 正常计权

### 降权策略

比较 `|T_score|` 和 `|C_score|`：

- `|T| < |C|` → 降 T，`T = 0`
- `|C| <= |T|` → 降 C，`C = 0`

---

## 使用示例

```python
from corr_gate_utils import apply_corr_gate

# 构造历史数据（T 和 C 的历史序列）
corr_hist = [
    [80, 70], [75, 65], [90, 80], [85, 75], [70, 60],
    [60, 50], [95, 85], [78, 68], [82, 72], [88, 78],
    [76, 66], [84, 74],  # 12 条数据，> min_history=10
]

# 场景 1：高度正相关，T > C → 降 C
result = apply_corr_gate(
    T_score=85.0,
    C_score=70.0,
    corr_hist=corr_hist,
    gate=0.70,
    min_history=10,
)
# corr ≈ 0.98（高度正相关）
# |corr| > 0.70 → 触发降权
# |T|=85 > |C|=70 → 降 C
# result = {
#     "T": 85.0,
#     "C": 0.0,       # C 被降权
#     "corr": 0.98,
#     "action": "高度相关,降权 C",
#     "applied": True,
#     "dropped": "C",
# }

# 场景 2：低度相关 → 正常计权
result = apply_corr_gate(
    T_score=60.0,
    C_score=40.0,
    corr_hist=[[1, -1], [2, -2], [3, -3], ...],  # 负相关
    gate=0.70,
    min_history=10,
)
# corr ≈ -1.0，但 |corr| > 0.70 也触发
# 高度负相关也是冗余 → 同样降权较弱维度

# 场景 3：历史数据不足 → 跳过
result = apply_corr_gate(
    T_score=85.0,
    C_score=70.0,
    corr_hist=[[80, 70], [75, 65]],  # 只有 2 条
    gate=0.70,
    min_history=10,
)
# result["action"] = "历史数据不足,跳过corr_gate"
# result["applied"] = False
# T 和 C 保持原值

# 场景 4：无历史数据 → 跳过
result = apply_corr_gate(T_score=85.0, C_score=70.0)
# result["action"] = "无历史数据,跳过corr_gate"
```

---

## 注意事项

1. **正负相关都算冗余**：`|corr| > gate` 即触发，无论是高度正相关还是高度负相关，因为两者都代表信息冗余。
2. **降较弱的一维**：始终保留绝对值较大的维度，降掉较小的，确保强信号不被抑制。
3. **历史数据不足时不处理**：宁可漏降，不可误降——数据不足时保持原始权重。
4. **纯 Python 实现**：不依赖 numpy，减少外部依赖，便于独立测试。
5. **方差为 0 时返回 None**：某维度数据完全不变（方差为 0）时无法计算相关系数，跳过处理。
6. **浮点误差保护**：相关系数结果限制在 `[-1.0, 1.0]`，防止浮点计算溢出。
7. **空转 bug 已修复**：决策 26 修复了 corr_gate 只改描述不降权的空转问题，现在降权真正生效。
