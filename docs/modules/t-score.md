# T 分数模块（t_score_utils）

## 模块简介

T 评分合成模块提供 T 分数（趋势维度得分）的核心计算逻辑，从 `four_dim_strategy.compute_T` 中提取。该模块覆盖 P-A 整改的三大去相关机制，旨在解决 T 分数容易"顶满 100"的问题，提高信号区分度。

### 三大去相关机制（P-A 整改）

| 机制 | 函数 | 作用 |
|------|------|------|
| ① 簇投票 | `cluster_vote_and_consensus` | 同簇共线策略坍缩为一票，避免重复加权 |
| ② 拥挤降权 | `crowd_penalty_factor` | 趋势簇一致度过高 → 打折，抑制追高杀低 |
| ③ 反向阻尼 | `contrarian_damping` | 趋势与均值回归背离 → T 幅值整体打折 |

---

## 核心函数列表

### cluster_vote_and_consensus

计算各策略簇的投票均值和一致度。

```python
def cluster_vote_and_consensus(
    sig: Dict[str, float],
    clusters: Dict[str, list],
) -> Tuple[Dict[str, float], Dict[str, float]]
```

**簇投票（P-A ①）**：

同簇共线策略先坍缩为"簇投票"（簇内 mean signal ∈ [-1, 1]），避免 5 个趋势策略同向 = "5 次投同一方向" 导致 T 顶满 100。

**一致度**：

簇内与均值同向的策略占比（0~1），用于拥挤降权的输入。

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `sig` | `Dict[str, float]` | 各策略信号值，如 `{"ma_break": 1, "boll": -1, ...}` |
| `clusters` | `Dict[str, list]` | 簇定义，如 `{"trend": ["ma_break", ...], "mean": ["boll", ...]}` |

**返回值**：`(cluster_vote, cluster_consensus)`

- `cluster_vote`：dict，各簇投票均值，范围 `[-1, 1]`
- `cluster_consensus`：dict，各簇一致度，范围 `[0, 1]`

---

### crowd_penalty_factor

计算拥挤降权系数（P-A ②）。

```python
def crowd_penalty_factor(
    consensus: float,
    crowd_thresh: float = 0.8,
    crowd_pen: float = 0.35,
) -> float
```

**规则**：

- 一致度 <= 阈值 → 不降权（factor = 1.0）
- 一致度 > 阈值 → 线性降权，一致度越高降权越多
- 最大降权幅度 = `crowd_pen`（如 0.35 表示最多打 65 折）
- factor 范围：`[1 - crowd_pen, 1.0]`

**公式**：

$$
\text{over} = \min\left(1.0, \frac{\text{consensus} - \text{crowd\_thresh}}{1 - \text{crowd\_thresh}}\right)
$$

$$
\text{factor} = \max(0, 1 - \text{crowd\_pen} \times \text{over})
$$

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `consensus` | `float` | — | 簇一致度，范围 `[0, 1]` |
| `crowd_thresh` | `float` | `0.8` | 拥挤阈值，超过此值开始降权 |
| `crowd_pen` | `float` | `0.35` | 最大降权幅度 |

**返回值**：`float`，拥挤降权系数，范围 `[1 - crowd_pen, 1.0]`。

**设计意图**：趋势簇内部一致度过高 → 可能处于趋势末端（"所有人都看多就该跌了"），对趋势簇贡献打折，抑制追高杀低。

---

## 使用示例

```python
from t_score_utils import cluster_vote_and_consensus, crowd_penalty_factor

# 1. 簇投票 + 一致度计算
sig = {
    "ma_break": 1,  # 均线突破：看多
    "macd": 1,  # MACD：看多
    "kdj": -1,  # KDJ：看空
    "boll": 1,  # 布林带：看多
    "rsi": -1,  # RSI：看空
}

clusters = {
    "trend": ["ma_break", "macd", "boll"],  # 趋势簇（3 个策略）
    "mean": ["kdj", "rsi"],  # 均值回归簇（2 个策略）
}

vote, consensus = cluster_vote_and_consensus(sig, clusters)

# 趋势簇：
#   votes = [1, 1, 1]
#   mean_v = 1.0
#   agree = 3/3 = 1.0（全部同向）
# 均值回归簇：
#   votes = [-1, -1]
#   mean_v = -1.0
#   agree = 2/2 = 1.0

# vote = {"trend": 1.0, "mean": -1.0}
# consensus = {"trend": 1.0, "mean": 1.0}

# 2. 拥挤降权
# 趋势簇一致度 1.0，超过阈值 0.8
factor = crowd_penalty_factor(
    consensus=1.0,
    crowd_thresh=0.8,
    crowd_pen=0.35,
)
# over = min(1.0, (1.0 - 0.8) / (1 - 0.8)) = min(1.0, 1.0) = 1.0
# factor = max(0, 1 - 0.35 * 1.0) = 0.65
# 趋势簇贡献打 65 折

# 3. 一致度刚好等于阈值 → 不降权
factor = crowd_penalty_factor(consensus=0.8)
# factor = 1.0

# 4. 一致度低于阈值 → 不降权
factor = crowd_penalty_factor(consensus=0.6)
# factor = 1.0

# 5. 中等拥挤（一致度 0.9）
factor = crowd_penalty_factor(consensus=0.9)
# over = (0.9 - 0.8) / 0.2 = 0.5
# factor = 1 - 0.35 * 0.5 = 1 - 0.175 = 0.825
# 打 82.5 折
```

---

## T 分数合成流程（P-A 整改）

```
各策略信号 (sig ∈ [-1, 1])
    │
    ▼
簇投票（cluster_vote）
→ 同簇策略坍缩为一票（簇内均值）
→ 同时计算簇一致度
    │
    ▼
拥挤降权（crowd_penalty_factor）
→ 趋势簇一致度过高 → 打折
→ 抑制追高杀低
    │
    ▼
反向阻尼（contrarian_damping）
→ 趋势与均值回归背离 → 整体打折
→ 降低分歧时的信号强度
    │
    ▼
加权求和 → 归一化到 [0, 100]
→ 最终 T 分数
```

---

## 注意事项

1. **簇投票解决共线问题**：旧逻辑 5 个趋势策略各投 1 票，同向时 T 轻易顶满 100，簇投票将其坍缩为 1 票，提高区分度。
2. **拥挤降权是反指思维**：一致度越高反而降权，基于"所有人都看多就该跌了"的经验规律。
3. **最大降权幅度可控**：`crowd_pen=0.35` 表示最多打 65 折，不会完全抹掉趋势信号。
4. **阈值以下不降权**：一致度 ≤ `crowd_thresh`（默认 0.8）时正常计权，只有过度一致才打折。
5. **反向阻尼处理簇间分歧**：趋势簇和均值回归簇方向相反时，整体 T 幅值打折，避免在分歧市场中给出假信号。
6. **纯函数设计**：所有计算逻辑不依赖外部状态，便于单元测试覆盖各种边界条件。
