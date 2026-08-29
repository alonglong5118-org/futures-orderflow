# 缺口止损模块（gap_stop_utils）

## 模块简介

缺口止损（gap_stop）模块提供缺口击穿告警的纯判断逻辑，从 `four_dim_live_runner.py` 中提取，便于单元测试，同时避免导入 runner 时的全局副作用。

### 核心功能

监测价格是否以缺口形式快速击穿止损位，当穿透距离超过 0.5R 时触发告警，提醒交易者注意极端行情下的滑点风险。

---

## 核心函数列表

### check_gap_stop_triggered

检查缺口击穿止损是否触发（纯函数，无副作用）。

```python
def check_gap_stop_triggered(ds, px, stop, entry_price) -> dict
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `ds` | `int` | 方向，1=多，-1=空，0=未知 |
| `px` | `float` | 当前价格 |
| `stop` | `float or None` | 止损价 |
| `entry_price` | `float or None` | 入场价（用于计算 1R） |

**返回值**：

```python
{
    "triggered": bool,  # 是否触发缺口击穿告警
    "is_adverse": bool,  # 是否为不利方向
    "oneR": float,  # 1R 风险（入场价到止损价的距离）
    "pen": float,  # 当前价格到止损价的穿透距离
    "pen_ratio": float,  # 穿透比例（pen / oneR）
}
```

---

## 触发条件

需**全部满足**以下条件才触发缺口击穿告警：

1. **方向有效**：`ds != 0`
2. **止损有效**：`stop is not None`
3. **入场价有效**：`entry_price is not None` 且 `oneR > 0`
4. **不利方向**：价格穿越止损且方向不利
5. **穿透足够深**：穿透距离 **严格大于** 0.5R（等于 0.5R 是边界，不触发）

### 方向规则

| 持仓方向 | 不利方向判定 | 说明 |
|----------|-------------|------|
| 多单（`ds > 0`） | `px < stop` | 价格从上方跌到止损下方 |
| 空单（`ds < 0`） | `px > stop` | 价格从下方涨到止损上方 |

### 穿透计算

$$
\text{oneR} = |\text{entry\_price} - \text{stop}|
$$

$$
\text{pen} = |\text{px} - \text{stop}|
$$

$$
\text{pen\_ratio} = \frac{\text{pen}}{\text{oneR}}
$$

**触发条件**：`is_adverse and pen > 0.5 * oneR`

---

## 使用示例

```python
from gap_stop_utils import check_gap_stop_triggered

# 场景 1：多单，缺口击穿止损（触发）
result = check_gap_stop_triggered(
    ds=1,  # 多单
    px=2450,  # 当前价格 2450
    stop=2500,  # 止损价 2500
    entry_price=2600,  # 入场价 2600
)
# oneR = |2600 - 2500| = 100
# pen = |2450 - 2500| = 50
# is_adverse = True（多单，px < stop）
# pen = 50，0.5R = 50 → 50 > 50? 否，等于边界，不触发
# pen_ratio = 0.5
# triggered = False

# 场景 2：多单，深度击穿（触发）
result = check_gap_stop_triggered(
    ds=1,
    px=2400,  # 当前价 2400，击穿更深
    stop=2500,
    entry_price=2600,
)
# oneR = 100
# pen = 100
# pen_ratio = 1.0
# 100 > 50 → 触发
# triggered = True

# 场景 3：空单，缺口向上击穿止损（触发）
result = check_gap_stop_triggered(
    ds=-1,  # 空单
    px=3100,  # 当前价跳空上涨
    stop=3000,  # 止损价 3000
    entry_price=2800,  # 入场价 2800
)
# oneR = |2800 - 3000| = 200
# pen = |3100 - 3000| = 100
# is_adverse = True（空单，px > stop）
# 100 > 100? 否，等于 0.5R 边界 → 不触发

# 场景 4：有利方向穿越（不触发）
result = check_gap_stop_triggered(
    ds=1,  # 多单
    px=2700,  # 价格上涨，有利方向
    stop=2500,
    entry_price=2600,
)
# is_adverse = False（多单，px > stop 是有利方向）
# triggered = False

# 场景 5：无效输入（不触发）
result = check_gap_stop_triggered(ds=0, px=2400, stop=2500, entry_price=2600)
# triggered = False（方向无效）

result = check_gap_stop_triggered(ds=1, px=2400, stop=None, entry_price=2600)
# triggered = False（止损无效）
```

---

## 注意事项

1. **严格大于 0.5R**：等于 0.5R 是边界值，不触发告警，避免边界抖动。
2. **必须是不利方向**：有利方向的价格穿越（如多单价格涨过止损上方）不触发。
3. **历史 bug 修复（2026-08-28）**：修复了 gap_stop 假阳性 bug——原逻辑未检查方向，导致有利方向的穿越也会误报缺口击穿。
4. **类型防御**：所有数值参数都做了 `float()` 转换和异常捕获，脏数据不会导致崩溃。
5. **除零保护**：入场价等于止损价（`oneR <= 0`）时直接返回，避免除零错误。
6. **纯函数设计**：无副作用，相同输入产生相同输出，便于单元测试覆盖各种边界情况。
7. **告警不等于自动平仓**：gap_stop 是告警机制，提醒交易者注意滑点风险，具体操作由人工或上层策略决定。
