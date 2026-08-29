# 风控闸门模块（risk_gate_utils）

## 模块简介

风控闸门模块是从 `four_dim_strategy.risk_gate` 中提取的核心计算逻辑，覆盖仓位计算的所有关键约束。该模块以纯函数形式提供，便于单元测试和独立验证。

### 仓位计算约束层级

1. **风险预算手数** — 基于 `risk_pct / stop_pts / multiplier`
2. **最小 1 手兜底** — 超风险标注（不裸奔）
3. **Kelly 因子缩放** — fractional-Kelly 仓位调整
4. **保证金约束手数** — 资金利用率上限
5. **单品种持仓上限** — 集中度控制
6. **T 强度缩放** — 弱过阈降仓
7. **已有持仓扣减** — 加仓不超配
8. **涨跌停闸门（gate3）** — 极端行情保护

---

## 核心函数列表

### calc_risk_lots

计算风险预算允许的手数（向下取整）。

```python
def calc_risk_lots(
    equity: float,
    risk_pct: float,
    stop_pts: float,
    multiplier: float,
) -> int
```

**公式**：

$$
N_{risk\_raw} = \left\lfloor \frac{\text{equity} \times (\text{risk\_pct}/100)}{\text{stop\_pts} \times \text{multiplier}} \right\rfloor
$$

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `equity` | `float` | 账户权益 |
| `risk_pct` | `float` | 单笔风险占比（%），如 1.5 表示 1.5% |
| `stop_pts` | `float` | 止损点数（价格单位） |
| `multiplier` | `float` | 合约乘数（每手多少单位） |

**返回值**：`int`，风险预算手数（0 表示风险预算不够一手）。

**边界处理**：当 `stop_pts * multiplier <= 0` 时返回 0，防止除零错误。

---

### calc_min_lot_floor

最小 1 手兜底处理。

```python
def calc_min_lot_floor(N_risk_raw: int, risk_per_hand: float) -> tuple
```

**规则**：

- `N_risk_raw < 1` 且有风险 → 强制 1 手，标注 `over_risk=True`（超风险预算）
- 否则 → 正常手数，`over_risk=False`

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `N_risk_raw` | `int` | 风险预算原始手数 |
| `risk_per_hand` | `float` | 每手风险金额 |

**返回值**：`(N_risk, over_risk)`

- `N_risk`：调整后手数
- `over_risk`：是否超出风险预算

**设计意图**：不裸奔，但不加仓（只开最小 1 手，超风险也认了）。

---

### apply_kelly_scaling

应用 Kelly 因子缩放。

```python
def apply_kelly_scaling(N_risk: int, kelly_mult: float) -> int
```

**规则**：

- `N_risk >= 1` → 乘以 `kelly_mult`，四舍五入取整，至少 1 手
- `N_risk < 1` → 保持 0（没风险预算就不开仓）

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `N_risk` | `int` | 风险预算手数 |
| `kelly_mult` | `float` | Kelly 缩放系数（来自 kelly_utils） |

**返回值**：`int`，缩放后的手数，至少 1 手（当原始手数 >= 1 时）。

**历史 bug 修复（P1-4）**：原公式 kelly 可达 1.6x，弱/中置信品种过度杠杆。修复后 `kelly_max=1.2`，且标准化映射。

---

### calc_margin_lots

计算保证金约束允许的手数（向下取整）。

```python
def calc_margin_lots(
    equity: float,
    margin_cap_pct: float,
    price: float,
    multiplier: float,
    margin_rate: float,
) -> int
```

**公式**：

$$
N_{margin} = \left\lfloor \frac{\text{equity} \times (\text{margin\_cap\_pct}/100)}{\text{price} \times \text{multiplier} \times \text{margin\_rate}} \right\rfloor
$$

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `equity` | `float` | 账户权益 |
| `margin_cap_pct` | `float` | 单品种保证金上限占比（%） |
| `price` | `float` | 当前价格 |
| `multiplier` | `float` | 合约乘数 |
| `margin_rate` | `float` | 保证金率 |

**返回值**：`int`，保证金约束手数。

---

## 仓位计算流程

完整的仓位计算遵循以下约束链条（从上到下，取最小值）：

```
风险预算手数
    ↓ (最小1手兜底)
风险手数 + over_risk 标记
    ↓ (Kelly 缩放)
Kelly 缩放后手数
    ↓ (取 min)
保证金约束手数
    ↓ (取 min)
单品种持仓上限
    ↓ (T强度缩放)
T 强度调整手数
    ↓ (已有持仓扣减)
最终开仓手数
```

---

## 使用示例

```python
from risk_gate_utils import (
    calc_risk_lots,
    calc_min_lot_floor,
    apply_kelly_scaling,
    calc_margin_lots,
)

# 1. 风险预算手数
equity = 100000       # 账户权益 10 万
risk_pct = 1.5        # 单笔风险 1.5%
stop_pts = 30         # 止损 30 点
multiplier = 10       # 合约乘数 10（每手 10 吨）

N_risk_raw = calc_risk_lots(equity, risk_pct, stop_pts, multiplier)
# N_risk_raw = 100000 * 0.015 // (30 * 10) = 5 手

# 2. 最小 1 手兜底（5 手 > 1，正常）
risk_per_hand = stop_pts * multiplier  # 300 元/手
N_risk, over_risk = calc_min_lot_floor(N_risk_raw, risk_per_hand)
# N_risk = 5, over_risk = False

# 3. Kelly 缩放（kelly_mult = 0.9）
N_kelly = apply_kelly_scaling(N_risk, kelly_mult=0.9)
# N_kelly = round(5 * 0.9) = 5 手（四舍五入）

# 4. 保证金约束
margin_cap_pct = 20   # 单品种保证金上限 20%
price = 3000          # 当前价格
margin_rate = 0.12    # 保证金率 12%

N_margin = calc_margin_lots(
    equity, margin_cap_pct, price, multiplier, margin_rate
)
# N_margin = 100000 * 0.2 // (3000 * 10 * 0.12) = 20000 // 3600 = 5 手

# 最终手数 = min(N_kelly, N_margin, 持仓上限, ...)
```

**超风险兜底场景**：

```python
N_risk_raw = calc_risk_lots(50000, 0.5, 100, 20)
# = 50000 * 0.005 // (100 * 20) = 250 // 2000 = 0 手

N_risk, over_risk = calc_min_lot_floor(0, risk_per_hand=2000)
# N_risk = 1, over_risk = True（强制开 1 手，超风险）
```

---

## 注意事项

1. **所有手数均为整数**：期货交易以手为单位，所有计算结果向下取整（Kelly 缩放除外，使用四舍五入）。
2. **最小 1 手兜底是超风险操作**：`over_risk=True` 时需在日志中标注，供事后复盘。
3. **Kelly 缩放仅放大已有风险预算**：风险预算为 0 时，Kelly 不会凭空创造仓位。
4. **分品种保证金上限不同**：JM/J 等低胜率品种保证金上限更紧（2026-08-16 整改）。
5. **T 强度随动缩放**：弱过阈降仓，`|T|≥1.5×阈值` 时满仓（2026-08-19 新增）。
6. **同品种持仓扣减**：加仓不超配（P2b 修复）。
7. **涨跌停闸门（gate3）**：极端行情下的前置否决（P1-16 风险锁定/熔断前置否决）。
