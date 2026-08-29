# 价格保护模块（price_protection）

## 模块简介

价格保护模块提供价格有效性校验、止损方向校验和用户价保护等纯计算逻辑，从 `four_dim_live_runner.py` / `trade_journal.py` / `account_tracker.py` 中提取，便于单元测试。

### 三层价格保护防线（决策 24）

| 层级 | 位置 | 作用 |
|------|------|------|
| 第 1 层 | Handler 层 | 保存原始价，调用 `_auto_levels` 后强制还原 |
| 第 2 层 | `record_entry` 层 | 价格验证 + 非法价格拦截 |
| 第 3 层 | `record_trade` 层 | 价格验证 + 日志 |

**历史问题**：用户输入价格被 `_auto_levels` 内部修改（如 4830→4829.7，8732.3→8732.8），导致实际成交价与用户预期不符。

---

## 核心函数列表

### validate_price

校验价格是否合法（必须 > 0）。

```python
def validate_price(price) -> dict
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `price` | `int / float / str / None` | 待校验价格 |

**返回值**：

```python
{
    "valid": bool,  # 是否合法
    "price": float,  # 转换后的价格（不合法时为 0.0）
    "reason": str,  # 不合法的原因（合法时为空串）
}
```

**校验规则**：

1. `price is None` → 不合法，原因："价格不能为空"
2. 类型转换失败 → 不合法，原因："价格格式错误: {price}"
3. `price <= 0` → 不合法，原因："非法价格 {price}，必须大于0"
4. 以上均通过 → 合法

---

### _dir_sign

方向符号转换（内部辅助函数）。

```python
def _dir_sign(direction) -> int
```

将各种方向表示统一转换为符号：

| 输入 | 输出 |
|------|------|
| `"多"`, `"long"`, `"duo"`, `"buy"` | `1` |
| `"空"`, `"short"`, `"kong"`, `"sell"` | `-1` |
| 正数 | `1` |
| 负数 | `-1` |
| 0 / 其他 | `0` |

---

### validate_entry_stop

校验开仓时止损方向是否正确；若错误则以入场价为轴镜像修正。

```python
def validate_entry_stop(direction, entry_price, stop) -> dict
```

**规则**：

- **多单**：止损必须 < 入场价（在下方）
- **空单**：止损必须 > 入场价（在上方）
- 方向错误时：以 `entry_price` 为对称轴，镜像修正止损价

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `direction` | `str / int / float` | 方向（"多"/"空"/"long"/"short"/1/-1 等） |
| `entry_price` | `float` | 入场价 |
| `stop` | `float or None` | 止损价（None 表示未设止损） |

**返回值**：dict 结构，包含修正后的止损价和是否被修正。

---

## 使用示例

```python
from price_protection import validate_price, validate_entry_stop

# 1. 价格有效性校验
result = validate_price(3500)
# result = {"valid": True, "price": 3500.0, "reason": ""}

result = validate_price(-100)
# result = {"valid": False, "price": 0.0, "reason": "非法价格 -100，必须大于0"}

result = validate_price("abc")
# result = {"valid": False, "price": 0.0, "reason": "价格格式错误: abc"}

result = validate_price(None)
# result = {"valid": False, "price": 0.0, "reason": "价格不能为空"}

# 2. 止损方向校验 — 正确方向（多单止损在下方）
result = validate_entry_stop(direction="多", entry_price=3500, stop=3450)
# 多单止损在入场下方 → 正确，无需修正

# 3. 止损方向校验 — 错误方向（多单止损在上方，自动镜像修正）
result = validate_entry_stop(direction=1, entry_price=3500, stop=3550)
# 多单止损在入场上方 → 方向错误，镜像修正为 3450
# entry_price - (stop - entry_price) = 3500 - 50 = 3450

# 4. 空单止损方向校验
result = validate_entry_stop(direction="short", entry_price=3500, stop=3550)
# 空单止损在入场上方 → 正确

result = validate_entry_stop(direction=-1, entry_price=3500, stop=3450)
# 空单止损在入场下方 → 错误，镜像修正为 3550
```

---

## 注意事项

1. **价格类型兼容**：支持 `int`、`float`、`str`、`None` 多种输入，统一转换为 `float`。
2. **止损镜像修正**：当止损方向错误时，不是直接报错拒绝，而是自动修正并记录，保证交易不中断。
3. **未设止损安全**：`stop=None` 时直接返回，不做方向校验。
4. **方向表示多样**：支持中文（多/空）、英文（long/short/buy/sell）、拼音（duo/kong）、数字（1/-1）等多种表示。
5. **三层防线协同**：本模块提供第 2、3 层的校验逻辑，第 1 层 Handler 层的原始价保存由调用方负责。
6. **纯函数设计**：所有函数无副作用，相同输入产生相同输出，便于单元测试覆盖边界情况。
