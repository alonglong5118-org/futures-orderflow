# 异动扫描模块（anomaly_scan）

## 模块简介

异动扫描模块提供全市场品种的异动评分和涨跌榜排名功能，从 da龘 的 minishare 全市场扫描适配而来。

该模块直接复用四维策略已有的 minishare 实时快照（`feed.last_snap` 含 open/high/low/close/vol），对全部 53 个品种计算"日内异动评分"，与信号卡片互补，承担广度选品的使命。

### 评分公式

$$
\text{score} = 0.7 \times |\text{日内涨跌幅}| + 0.3 \times \text{振幅}
$$

| 权重 | 指标 | 说明 |
|------|------|------|
| 0.7 | 日内涨跌幅 | `(close - open) / open × 100`（有昨收时改用昨收基准） |
| 0.3 | 振幅 | `(high - low) / open × 100` |

---

## 核心函数列表

### compute

计算全市场异动扫描结果。

```python
def compute(snaps, pre_close_map=None, top_n=TOP_N) -> dict
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `snaps` | `dict` | — | 品种快照字典，`{sym: {"close", "open", "high", "low", "name", ...}}` |
| `pre_close_map` | `dict or None` | `None` | 昨收价字典，`{sym: pre_close}`，可选 |
| `top_n` | `int` | `12` | 涨跌榜各取前 N 名 |

**返回值**：

```python
{
    "ok": bool,  # 是否成功（有有效数据）
    "updated": str,  # 更新时间（HH:MM:SS）
    "total": int,  # 有效品种数量
    "by_symbol": dict,  # 按品种代码索引的详细数据
    "top_up": list,  # 涨幅榜前 top_n
    "top_down": list,  # 跌幅榜前 top_n
}
```

**单品种记录格式**（`by_symbol[sym]` 和 `top_up/top_down` 中的元素）：

```python
{
    "symbol": str,  # 品种代码
    "name": str,  # 品种名称
    "close": float,  # 最新价
    "pct": float,  # 涨跌幅（%）
    "amp": float,  # 振幅（%）
    "score": float,  # 异动评分
}
```

---

## 涨跌榜排序规则

- **涨幅榜（top_up）**：按 `pct` 从大到小排序（涨幅最高的在前）
- **跌幅榜（top_down）**：按 `pct` 从小到大排序（跌幅最大的在前，即最负的在前）

**注意**：异动评分（score）用于衡量"波动剧烈程度"，但涨跌榜仍按涨跌幅（pct）排序，保持市场习惯。

---

## 使用示例

```python
import anomaly_scan as asc

# 1. 基本用法（无昨收数据）
snaps = {
    "FG": {"close": 910, "open": 900, "high": 915, "low": 898, "name": "玻璃"},
    "SA": {"close": 980, "open": 1000, "high": 1005, "low": 975, "name": "纯碱"},
    "rb": {"close": 3300, "open": 3300, "high": 3320, "low": 3280, "name": "螺纹"},
}

result = asc.compute(snaps)

# FG:
#   pct = (910 - 900) / 900 * 100 = 1.11%
#   amp = (915 - 898) / 900 * 100 = 1.89%
#   score = 0.7 * 1.11 + 0.3 * 1.89 = 0.777 + 0.567 = 1.34
#
# SA:
#   pct = (980 - 1000) / 1000 * 100 = -2.0%
#   amp = (1005 - 975) / 1000 * 100 = 3.0%
#   score = 0.7 * 2.0 + 0.3 * 3.0 = 1.4 + 0.9 = 2.3
#
# rb:
#   pct = 0%
#   amp = (3320 - 3280) / 3300 * 100 = 1.21%
#   score = 0.7 * 0 + 0.3 * 1.21 = 0.36

print("异动品种数:", result["total"])  # 3
print("领涨:", [(x["name"], x["pct"]) for x in result["top_up"]])
# 领涨: [("玻璃", 1.11), ("螺纹", 0.0), ("纯碱", -2.0)]
print("领跌:", [(x["name"], x["pct"]) for x in result["top_down"]])
# 领跌: [("纯碱", -2.0), ("螺纹", 0.0), ("玻璃", 1.11)]

# 2. 带昨收数据（更准确的涨跌幅）
pre_close_map = {
    "FG": 895,
    "SA": 990,
    "rb": 3290,
}

result = asc.compute(snaps, pre_close_map=pre_close_map)
# FG pct = (910 - 895) / 895 * 100 = 1.68%（用昨收计算）
# SA pct = (980 - 990) / 990 * 100 = -1.01%

# 3. 自定义涨跌榜数量
result = asc.compute(snaps, top_n=5)  # 取前 5 名
```

### 在 runner 中调用

```python
import anomaly_scan as asc

# 从实时 feed 获取所有品种快照
snaps = {sym: feed.last_snap[sym] for sym in SYMBOLS if feed.last_snap.get(sym)}
result = asc.compute(snaps)

if result["ok"]:
    print(f"异动扫描完成，共 {result['total']} 个品种")
    print("涨幅前 5:", [(r["symbol"], r["pct"]) for r in result["top_up"][:5]])
    print("跌幅前 5:", [(r["symbol"], r["pct"]) for r in result["top_down"][:5]])
```

---

## 模块常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `W_PCT` | `0.7` | 涨跌幅权重 |
| `W_AMP` | `0.3` | 振幅权重 |
| `TOP_N` | `12` | 涨跌榜各取前 N 名 |

---

## 注意事项

1. **涨跌幅基准优先级**：有昨收（`pre_close`）时用昨收作基准，否则用开盘价。昨收数据异常时回退到开盘价。
2. **数据异常自动跳过**：`close / open / high / low` 任一缺失或无法转换为数值时，该品种被跳过。
3. **评分用绝对值**：异动评分取涨跌幅的绝对值，上涨和下跌都算异动。
4. **涨跌榜仍按方向排序**：涨幅榜按涨幅从高到低，跌幅榜按跌幅从深到浅，符合市场习惯。
5. **默认 12 名**：涨跌榜各取前 12 名，可通过 `top_n` 参数调整。
6. **时间戳格式**：`updated` 字段为 `HH:MM:SS` 格式的字符串（本地时间）。
7. **纯计算无副作用**：不依赖外部数据源，输入快照即可计算，便于单元测试。
