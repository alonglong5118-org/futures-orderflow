# 回归测试与基准测试

## 概述

回归测试（Regression Testing）和基准测试（Benchmark Testing）是确保代码质量的重要手段。回归测试确保代码变更不破坏已有功能，基准测试确保性能不出现明显退化。

---

## 回归测试

### 什么是回归测试

回归测试验证代码变更后，已有功能是否仍然正常工作。每次提交代码后运行完整测试套件，确保：

- 修复一个 bug 没有引入新的 bug
- 新增功能没有破坏旧功能
- 重构没有改变行为

### 回归测试策略

#### 1. 全量回归

每次重大变更后运行全部测试：

```bash
python run_tests.py all
```

覆盖所有 Python + JS 测试模块，确保整体功能正常。

#### 2. 快速回归（冒烟测试）

日常开发中，每次小变更后运行冒烟测试：

```bash
python run_tests.py smoke
```

冒烟测试 < 10 秒完成，覆盖核心功能，用于快速验证没有严重破坏。

#### 3. 定向回归

修改特定模块后，只跑相关测试：

```bash
# 修改了 kelly_utils.py
python run_tests.py kelly_factor

# 修改了 risk_gate_utils.py
python run_tests.py risk_gate
```

### 历史 bug 回归覆盖

项目中每个修复的 bug 都有对应的测试用例，防止回归。以下是已覆盖的历史 bug：

| Bug 编号 | 模块 | 问题描述 | 修复内容 |
|----------|------|----------|----------|
| P-C | signal_trigger | 硬否决阈值太高（bias_G≥60 几乎不可达） | 改为 bias_FC + fc_hard(25) |
| P-B | signal_trigger | 同向确认没生效（F/C 强同向没降阈值） | 接上阈值降低逻辑 |
| P1-4 | risk_gate / kelly | Kelly 可达 1.6x，过度杠杆 | kelly_max=1.2，标准化映射 |
| P2b | risk_gate | 同品种持仓扣减缺失（加仓超配） | 已有持仓扣减逻辑 |
| 决策 24 | price_protection | 用户价被 _auto_levels 修改 | 3 层价格保护防线 |
| 决策 26 | corr_gate | corr_gate 空转（只改描述不降权） | 真正降权较弱维度 |
| P-A ① | t_score | 5 个趋势策略 = 5 票，T 顶满 100 | 簇投票坍缩共线策略 |
| P-A ② | t_score | 一致度过高时信号过强 | 拥挤降权 |
| P-A ③ | t_score | 趋势/均值回归背离无约束 | 反向阻尼 |
| 2026-08-28 | gap_stop | gap_stop 假阳性（有利方向也报击穿） | 增加方向检查 |

### 回归测试最佳实践

1. **每次提交都跑测试**：CI/CD 自动运行，不通过不合并。
2. **修复 bug 先写测试**：先写一个复现 bug 的测试（确认失败），再修复代码（确认通过）。
3. **保持测试独立性**：测试之间不共享状态，顺序不影响结果。
4. **随机测试顺序**：使用 `-r` 选项随机打乱测试顺序，发现隐藏依赖。
5. **不稳定测试标记**：使用 `--retry` 检测不稳定测试（flaky tests）。

---

## 基准测试

### 什么是基准测试

基准测试测量关键函数的执行时间和资源消耗，确保代码优化不会导致性能退化。

### 运行基准测试

```bash
python run_tests.py advanced
```

`advanced` 模式包含属性测试 + 基准测试 + 性能测试。

### 基准测试场景

| 场景 | 说明 | 基准指标 |
|------|------|----------|
| 信号计算 | 单次 compute_T 调用耗时 | < 1ms |
| 全品种扫描 | anomaly_scan.compute 53 品种 | < 10ms |
| 出场模拟 | sim_exit_bars 100 根 K 线 | < 5ms |
| Kelly 计算 | 单次 compute_kelly_factor | < 0.1ms |
| 风控计算 | 完整 risk_gate 计算 | < 1ms |

### 性能监控

- **慢测试标记**：使用 `--slow 500` 标记耗时超过 500ms 的测试
- **性能回归检测**：对比基准版本和当前版本的性能差异
- **CI 性能门禁**：性能退化超过阈值时阻断合并

---

## 属性测试（Hypothesis）

属性测试是回归测试的补充，通过自动生成大量随机输入来发现边界情况。

### 适用场景

1. **数学计算函数**：验证不变量（如输出范围、单调性）
2. **编解码函数**：验证编解码可逆性
3. **排序/聚合函数**：验证结果的结构性质

### 示例：Kelly 因子属性

```python
from hypothesis import given
from hypothesis.strategies import floats, one_of, none

@given(
    edge=one_of(floats(min_value=-1, max_value=2), none()),
    kelly_min=floats(min_value=0.1, max_value=1.0),
    kelly_max=floats(min_value=1.0, max_value=2.0),
    target_edge=floats(min_value=0.1, max_value=2.0),
)
def test_kelly_factor_properties(edge, kelly_min, kelly_max, target_edge):
    result = compute_kelly_factor(edge, kelly_min, kelly_max, target_edge)
    # 属性 1：返回值始终是 float
    assert isinstance(result, float)
    # 属性 2：返回值在合理范围内
    assert result > 0
    # 属性 3：edge=None 时返回 1.0
    if edge is None:
        assert result == 1.0
```

### Hypothesis 优势

- **发现边界 bug**：自动生成手工想不到的输入组合
- **失败简化（shrinking）**：失败时自动找到最小复现用例
- **确定性重放**：使用 `@settings(deterministic=True)` 确保可复现

---

## 回归测试工作流

### 开发阶段

```
开发新功能 / 修复 bug
    │
    ▼
编写/更新单元测试
    │
    ▼
运行定向回归（相关模块）
    │
    ▼
通过 → 提交代码
    │
    ▼
CI 运行全量回归 + 覆盖率
    │
    ▼
通过 → 代码合并
```

### 发布阶段

```
发布前
    │
    ▼
运行完整测试套件（all）
    │
    ▼
运行基准测试
    │
    ▼
检查覆盖率趋势
    │
    ▼
生成发布报告
```

---

## 不稳定测试（Flaky Tests）

### 识别不稳定测试

```bash
python run_tests.py --retry 3
```

失败重跑 3 次，如果有时通过有时失败，说明是不稳定测试。

### 常见原因

1. **时间依赖**：测试依赖当前时间或超时
2. **随机数依赖**：使用了非确定性随机源
3. **文件系统依赖**：测试间共享文件状态
4. **顺序依赖**：测试 B 依赖测试 A 的副作用

### 修复策略

1. **注入时间**：将时间作为参数传入，测试中使用固定时间
2. **固定随机种子**：使用固定 seed 确保可复现
3. **隔离测试环境**：每个测试使用独立的临时目录
4. **纯函数化**：将有状态的逻辑改为纯函数

---

## 相关文档

- [测试体系总览](overview.md) — 测试分层、模块清单
- [测试框架说明](test-framework.md) — pytest、hypothesis、运行方式
- [覆盖率说明](coverage.md) — coverage.py、Codecov、.coveragerc 配置
