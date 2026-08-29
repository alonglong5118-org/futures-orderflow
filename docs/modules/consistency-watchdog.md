# 一致性看门狗模块（consistency_watchdog）

## 模块简介

一致性看门狗（consistency_watchdog）负责检查训练 / 服务一致性（train/serve parity），防止"你以为在跑校验过的模型，实际在跑一个偏离基线、未复验的参数"这种实盘埋雷情况。

### 问题背景

#3 让 live 的 `calibration_params.json` 可被"一键 apply"改写 T_thresh，也允许人工重校。但改写后 live 服务的参数可能已偏离"最后一次 OOS 校验基线"（`four_dim_strategy.DEFAULT_CONFIG` 的 `thresholds_by_symbol`），或某品种根本没有 `mean_oos`（从未被校验）却在服务。

### 设计原则

**只报告、不修正**（与 #3 红线一致：不擅自改线上参数），产出结构化差异清单供人工决策。

---

## 检查项

### 1. train_serve_divergence（训练/服务偏离）

每个关注品种的**校验基线 T**（`DEFAULT_CONFIG`）vs **服务 T**（`calibration_params`），偏离超过 `DEVIATE_PCT`（默认 35%）则标 `needs_revalidation`。

**例外**：近期（`RECAL_GRACE_DAYS` 天内，默认 7 天）已主动重校（apply）的品种，偏离基线属有意行为，不重复报 divergence。

### 2. unvalidated（未校验）

关注品种在 `calibration_params` 中缺 `mean_oos`（从未被 OOS 校验，用默认 T 在服务）。

### 3. broken_serving（漂移失效仍在服务）

漂移判 broken 且未禁用、且未被动态门控压制 → 真在服务一个失效模型（计入 `ok=false`）。

### 3b. broken_gated（漂移失效但已门控）

漂移判 broken 但已被动态门控 `papertrack_gated` 压制（不发信号）→ 风险已控，仅提示（不计入 `ok=false`）。

### 4. stale（参数陈旧）

`recalibrated_at` 超过 `STALE_DAYS` 天（默认 30 天）未刷新 → 建议重校。

---

## 核心函数列表

### check_consistency

返回一致性报告 dict。

```python
def check_consistency(focus_symbols=None, disabled_set=None) -> dict
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `focus_symbols` | `list or None` | `None` | 关注品种列表，缺省取 `fd.DEFAULT_CONFIG` 全部键 |
| `disabled_set` | `set or None` | `None` | 已禁用品种集合 |

**返回值**：结构化的一致性报告 dict，包含上述所有检查项的结果。

---

### 辅助函数

#### _load_calib

加载 `calibration_params.json` 文件。

```python
def _load_calib() -> dict
```

#### _load_drift

加载 `calibration_drift.json` 文件。

```python
def _load_drift() -> dict
```

---

## 配置常量

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `DEVIATE_PCT` | `0.35` | 服务 T 相对基线 T 偏离 >35% 视为需复验 |
| `STALE_DAYS` | `30` | `recalibrated_at` 超过 30 天未刷新视为陈旧 |
| `RECAL_GRACE_DAYS` | `7` | 近期已主动重校的品种，偏离基线属有意行为，不重复报 divergence |

---

## 文件依赖

| 文件 | 路径变量 | 作用 |
|------|----------|------|
| 校准参数 | `CALIB_FILE` | `calibration_params.json`，当前服务参数 |
| 漂移报告 | `DRIFT_FILE` | `calibration_drift.json`，漂移检测结果 |
| 基线配置 | `four_dim_strategy.DEFAULT_CONFIG` | OOS 校验基线 |

---

## 使用示例

```python
from consistency_watchdog import check_consistency

# 1. 全量检查（所有 DEFAULT_CONFIG 中的品种）
report = check_consistency()

# 2. 指定关注品种
report = check_consistency(
    focus_symbols=["rb", "FG", "SA", "M", "MA"],
    disabled_set={"JM"},  # JM 已禁用，不计入 broken_serving
)

# 3. 解读报告
if not report.get("ok"):
    print("存在风险，需要关注！")

if report.get("divergences"):
    print(f"训练/服务偏离: {len(report['divergences'])} 个品种")
    for item in report["divergences"]:
        print(
            f"  {item['symbol']}: 基线T={item['baseline_T']}, 服务T={item['served_T']}, 偏离={item['deviation_pct']}%"
        )

if report.get("unvalidated"):
    print(f"未校验品种: {report['unvalidated']}")

if report.get("broken_serving"):
    print(f"漂移失效仍在服务: {report['broken_serving']}")

if report.get("broken_gated"):
    print(f"漂移失效已门控: {report['broken_gated']}")

if report.get("stale"):
    print(f"参数陈旧: {len(report['stale'])} 个品种建议重校")
```

---

## 风险等级

| 等级 | 检查项 | 计入 ok=false | 说明 |
|------|--------|--------------|------|
| 严重 | `broken_serving` | 是 | 漂移失效仍在发信号 |
| 警告 | `divergences` | 否 | 参数偏离基线，需复验 |
| 警告 | `unvalidated` | 否 | 从未被 OOS 校验 |
| 提示 | `broken_gated` | 否 | 已门控，风险可控 |
| 提示 | `stale` | 否 | 参数陈旧，建议重校 |

---

## 注意事项

1. **只报告、不修正**：看门狗绝不修改线上参数，所有问题由人工决策处理。
2. **近期重校豁免**：7 天内主动 apply 的品种，偏离基线是有意为之，不报 divergence，避免重复干扰。
3. **broken_serving 是硬风险**：漂移判 broken 又没被门控、又没禁用，等于在实盘跑一个失效模型，计入 `ok=false`。
4. **broken_gated 风险可控**：虽然模型失效，但已被动态门控压制（papertrack 模式，不发实盘信号），仅作提示。
5. **文件加载容错**：配置文件不存在或解析失败时返回空 dict，不会崩溃。
6. **`__note_only__` 标记**：calib 中的 `__note_only__` 条目是纯备注，不计入未校验检查。
7. **与 #3 红线一致**：不擅自改线上参数，只做监控和告警。
