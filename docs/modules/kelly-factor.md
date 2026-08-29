# Kelly 因子模块（kelly_utils）

## 模块简介

Kelly 因子模块提供 fractional-Kelly 仓位缩放系数的计算，将纯数学逻辑从策略主文件中提取，便于单元测试，同时避免导入 strategy 时的全局副作用。

### 核心思想

根据策略的历史 edge（超额收益）动态调整仓位大小：

- **高 edge** → 放大仓位（最高 1.2x）
- **低 edge / 负 edge** → 缩小仓位（最低 0.6x）
- **近景负期望** → 封顶 1.0，禁止加杠杆

---

## 核心函数列表

### compute_kelly_factor

计算 fractional-Kelly 仓位缩放系数。

```python
def compute_kelly_factor(
    edge,
    kelly_min=0.6,
    kelly_max=1.2,
    target_edge=0.5,
    cur_full_expR=None,
)
```

**公式**：

$$
\text{mult} = \text{kelly\_min} + (\text{kelly\_max} - \text{kelly\_min}) \times \text{clip}\left(\frac{\text{edge}}{\text{target\_edge}}, 0, 1\right)
$$

**近景门槛**：仅当 `edge` 与近景期望收益（`cur_full_expR`）同为正时，才允许 >1.0 的杠杆放大；否则强制封顶 1.0。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `edge` | `float or None` | — | walk-forward edge（mean_oos 或 full_expR），None 表示无校准数据 → 返回 1.0 |
| `kelly_min` | `float` | `0.6` | 最小缩放系数 |
| `kelly_max` | `float` | `1.2` | 最大缩放系数 |
| `target_edge` | `float` | `0.5` | 归一化目标 edge |
| `cur_full_expR` | `float or None` | `None` | 近景期望收益（用于近景门槛），None 表示无近景数据 → 退回远 edge 符号 |

**返回值**：`float`，Kelly 缩放系数，范围：

- 正常情况：`[kelly_min, kelly_max]`
- 近景负时：`[kelly_min, 1.0]`（封顶 1.0）

---

## 计算流程

```
输入 edge
    │
    ├─ edge is None / 无效 → 返回 1.0（中性）
    │
    ├─ kelly_min > kelly_max → 自动交换（防御性编程）
    │
    ├─ edge_pos = max(edge, 0)  （负 edge 按 0 处理）
    │
    ├─ ratio = clip(edge_pos / target_edge, 0, 1)
    │
    ├─ mult = kelly_min + (kelly_max - kelly_min) * ratio
    │
    └─ 近景门槛检查
         ├─ cur_full_expR > 0  → 保持 mult（可 > 1.0）
         └─ cur_full_expR <= 0 → mult = min(mult, 1.0)
```

---

## 使用示例

```python
from kelly_utils import compute_kelly_factor

# 场景 1：高 edge 品种，近景正期望
mult = compute_kelly_factor(
    edge=0.6,           # 历史 edge 60%
    cur_full_expR=0.3,  # 近景期望收益 30%
)
# mult = 0.6 + (1.2 - 0.6) * min(0.6/0.5, 1.0)
#      = 0.6 + 0.6 * 1.0 = 1.2
# 近景正 → 允许 1.2x

# 场景 2：中等 edge 品种
mult = compute_kelly_factor(edge=0.25)
# mult = 0.6 + 0.6 * min(0.25/0.5, 1.0)
#      = 0.6 + 0.6 * 0.5 = 0.9

# 场景 3：负 edge 品种
mult = compute_kelly_factor(edge=-0.1)
# edge_pos = max(-0.1, 0) = 0
# mult = 0.6 + 0.6 * 0 = 0.6

# 场景 4：高 edge 但近景负期望（近景门槛生效）
mult = compute_kelly_factor(
    edge=0.6,
    cur_full_expR=-0.1,  # 近景亏损
)
# 计算得 mult = 1.2
# 近景负 → 封顶 1.0
# 最终 mult = 1.0

# 场景 5：无校准数据
mult = compute_kelly_factor(edge=None)
# mult = 1.0（中性）

# 场景 6：自定义参数
mult = compute_kelly_factor(
    edge=0.3,
    kelly_min=0.5,
    kelly_max=1.5,
    target_edge=1.0,
)
# mult = 0.5 + (1.5 - 0.5) * min(0.3/1.0, 1.0)
#      = 0.5 + 1.0 * 0.3 = 0.8
```

---

## 历史演进（决策 20）

### 原公式（问题）

```
mult = 0.6 + slope * edge
```

- edge=0.5 时冲到 **1.6x**，过度杠杆
- 弱/中置信品种风险过高

### 新公式（修复）

```
mult = kelly_min + (kelly_max - kelly_min) * clip(edge / target_edge, 0, 1)
```

- 标准化线性映射，高 edge 品种杠杆降低 **25%**（1.6x → 1.2x）
- `target_edge` 作为归一化基准，可控性更强
- 新增**近景门槛**：弱 edge 反向加杠杆被杜绝（P2-A 整改）

---

## 注意事项

1. **edge 取正处理**：负 edge 按 0 处理，即最小缩放 `kelly_min`。
2. **近景门槛是安全垫**：即使历史 edge 很高，只要近景期望收益为负，就禁止加杠杆。
3. **缺近景数据时退回远 edge**：用远 edge 的符号判断是否允许加杠杆。
4. **参数防御**：所有参数都做了类型转换和异常处理，无效输入返回 1.0（中性）。
5. **kelly_min > kelly_max 自动交换**：防止配置错误导致异常。
6. **target_edge 为 0 或负时直接拉满**：异常配置保护，ratio 设为 1.0。
