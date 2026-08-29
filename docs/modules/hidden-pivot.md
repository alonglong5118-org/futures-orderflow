# 隐秘枢轴模块（hidden_pivot）

## 模块简介

隐秘枢轴（Hidden Pivot）模块基于 Peter Lakos 的理论，实现摆动点检测、ABC 结构识别和目标位计算。该模块为纯函数、数据无关设计，输入摆动点序列，推导 a-b-c 结构与 p 目标位 / c 止损锚。

### 国内期货适配

- **tick 取整**：价格按合约最小变动价位取整
- **涨跌停板校验**：目标位超出涨跌停板时标记不可达
- **跳空跳过**：夜盘 / 午休 / 隔夜缺口处重置摆动锚点，避免把缺口误判为 swing 高低点

---

## 核心函数列表

### find_swings

ZigZag 摆动点检测。

```python
def find_swings(
    highs,
    lows,
    closes,
    opens=None,
    deviation=0.004,
    depth=3,
    gap_pct=0.0,
) -> list
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `highs` | `list` | — | 最高价序列 |
| `lows` | `list` | — | 最低价序列 |
| `closes` | `list` | — | 收盘价序列 |
| `opens` | `list` | `None` | 可选开盘价序列，配合 `gap_pct` 做跳空跳过 |
| `deviation` | `float` | `0.004` | 相对摆动阈值（相对价格，如 0.004 = 0.4%），过滤毛刺 |
| `depth` | `int` | `3` | 确认一个极值所需的两侧根数 |
| `gap_pct` | `float` | `0.0` | 相对跳空阈值（如 0.0025 = 0.25%）；>0 时启用跳空跳过 |

**返回值**：`[(idx, 'high'/'low', price), ...]`，按时间升序排列的摆动点列表。

**跳空跳过逻辑**：当 `gap_pct > 0` 且 `opens` 不为空时，如果本根开盘相对前收的跳空幅度超过 `gap_pct`，则重置摆动锚点（`last = None`），避免把缺口误判为 swing 高低点。

---

### latest_abc

找最近合法 a-b-c 结构。

```python
def latest_abc(swings, direction=None) -> tuple or None
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `swings` | `list` | — | 摆动点列表（`find_swings` 的输出） |
| `direction` | `int or None` | `None` | 方向过滤：`1` 只找多头，`-1` 只找空头，`None` 自动检测 |

**返回值**：`(a, b, c, dir)` 或 `None`

- `a, b, c`：摆动点元组 `(idx, type, price)`
- `dir`：方向，`1`（偏多）或 `-1`（偏空）

### 结构判定规则

**多头结构**：
- `a` = 摆动低点（low）
- `b` = 反弹高点（high）
- `c` = 回调低点（low）
- `c.price > a.price`（higher low，抬升底）

**空头结构**：
- `a` = 摆动高点（high）
- `b` = 回落低点（low）
- `c` = 反弹高点（high）
- `c.price < a.price`（lower high，降低顶）

---

### round_tick

价格按 tick 取整。

```python
def round_tick(price, tick) -> float
```

**公式**：`round(round(price / tick) * tick, 6)`

---

### hidden_pivot

计算 p 目标位 + c 止损锚，tick 取整 + 停板校验。

```python
def hidden_pivot(abc, tick, limit_up=None, limit_down=None) -> dict or None
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `abc` | `tuple` | — | ABC 结构（`latest_abc` 的输出） |
| `tick` | `float` | — | 合约最小变动价位 |
| `limit_up` | `float or None` | `None` | 涨停价 |
| `limit_down` | `float or None` | `None` | 跌停价 |

**返回值**：`dict` 或 `None`（abc 为 None 时返回 None）

```python
{
    "direction": int,          # 方向：1 / -1
    "direction_text": str,     # 方向描述："偏多" / "偏空"
    "a": float,                # a 点价格（tick 取整）
    "b": float,                # b 点价格
    "c": float,                # c 点价格（止损锚）
    "p": float,                # p 目标位（tick 取整）
    "stop": float,             # 止损锚 = c 点
    "reachable": bool,         # 目标位是否可达（未超涨跌停）
    "gain_pts": float,         # 盈利空间（p - c，tick 取整后）
}
```

---

## 目标位计算

### 多头结构

$$
p = b + (b - a) \times 0.618
$$

止损锚 = c 点（结构有效性锚点，被破则逻辑失效）

### 空头结构

$$
p = b - (a - b) \times 0.618
$$

止损锚 = c 点（结构有效性锚点，被破则逻辑失效）

---

## 使用示例

```python
from hidden_pivot import find_swings, latest_abc, hidden_pivot

# 1. 摆动点检测
highs = [100, 102, 105, 103, 101, 98, 100, 104, 106, ...]
lows  = [98,  100, 103, 101, 99,  96, 97,  102, 104, ...]
closes = [99, 101, 104, 102, 100, 97, 99, 103, 105, ...]
opens = [...]

swings = find_swings(
    highs, lows, closes,
    opens=opens,
    deviation=0.004,   # 0.4% 摆动阈值
    depth=3,           # 两侧各 3 根确认
    gap_pct=0.0025,    # 0.25% 跳空阈值
)
# swings = [
#   (2, 'high', 105),
#   (5, 'low', 96),
#   (8, 'high', 106),
#   ...
# ]

# 2. 找最近 ABC 结构
abc = latest_abc(swings, direction=None)  # 自动检测方向
# 假设找到多头结构：
# abc = ((5, 'low', 96), (8, 'high', 106), (10, 'low', 99), 1)
# a=96（低点）, b=106（高点）, c=99（回调低点，>a=96 → higher low）

# 3. 计算目标位和止损
result = hidden_pivot(
    abc,
    tick=1.0,              # 最小变动价位 1 元
    limit_up=115.0,        # 涨停价
    limit_down=90.0,       # 跌停价
)
# 多头结构：
#   p = 106 + (106 - 96) * 0.618 = 106 + 6.18 = 112.18 → 112.0（tick 取整）
#   stop = c = 99.0
#   gain_pts = 112.0 - 99.0 = 13.0
#   reachable = True（112 < 115，未超涨停）
#
# result = {
#     "direction": 1,
#     "direction_text": "偏多",
#     "a": 96.0, "b": 106.0, "c": 99.0,
#     "p": 112.0,
#     "stop": 99.0,
#     "reachable": True,
#     "gain_pts": 13.0,
# }
```

---

## 注意事项

1. **数据量要求**：`find_swings` 需要至少 `depth * 2 + 2` 根 K 线，否则返回空列表。
2. **摆动点交替**：高低点交替出现，同方向连续摆动时只保留极值（更高的高 / 更低的低）。
3. **跳空跳过**：缺口处不是真实市场摆动结构，启用 `gap_pct` 可避免误判。
4. **ABC 结构从后往前找**：`latest_abc` 从最近的摆动点往前搜索，返回第一个合法结构。
5. **c 点是止损锚**：c 点被跌破（多头）或涨破（空头）意味着 ABC 结构失效。
6. **涨跌停校验**：目标位超出涨跌停板时 `reachable=False`，提醒交易者注意流动性风险。
7. **0.618 黄金分割**：Hidden Pivot 使用 0.618 比例计算目标位，源于斐波那契比率。
