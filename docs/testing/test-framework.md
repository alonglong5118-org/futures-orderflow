# 测试框架说明

## 概述

四维策略的测试框架基于 **pytest** 构建，辅以 **Hypothesis** 进行属性测试，通过 `run_tests.py` 提供统一的测试入口。

---

## 核心框架

### pytest

pytest 是主要的测试运行框架，提供：

- 自动发现测试（`test_*.py` 文件、`test_*` 函数）
- 丰富的断言（直接使用 `assert`）
- fixture 机制（测试前置/后置处理）
- 参数化测试（`@pytest.mark.parametrize`）
- 插件生态（coverage、hypothesis、xdist 等）

### Hypothesis

Hypothesis 用于属性测试（property-based testing），能够：

- 自动生成大量随机测试用例
- 发现手工用例遗漏的边界情况
- 失败时自动简化（shrinking）到最小复现用例
- 与 pytest 无缝集成

---

## 测试入口（run_tests.py）

`run_tests.py` 是统一的测试运行脚本，封装了 pytest 的调用细节，提供分类运行、覆盖率、性能监控等功能。

### 基本用法

```bash
# 跑全部单元测试（Python + JS）
python run_tests.py

# 只跑某个模块
python run_tests.py gap_stop

# 冒烟测试（<10s，核心功能快速验证）
python run_tests.py smoke
```

### 运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 默认 | `python run_tests.py` | 全部单元测试 |
| 单模块 | `python run_tests.py <module>` | 指定模块名 |
| 冒烟 | `python run_tests.py smoke` | 核心功能快速验证（<10s） |
| 单元 | `python run_tests.py unit` | 只跑单元测试 |
| 集成 | `python run_tests.py integration` | 只跑集成测试 |
| 高级 | `python run_tests.py advanced` | 属性 + 基准 + 性能 |
| 全部 | `python run_tests.py all` | 含性能测试的完整套件 |

### 常用选项

| 选项 | 说明 |
|------|------|
| `-v` | 详细输出（verbose） |
| `-f` | 快速失败（第一个失败就停止） |
| `-c` / `--coverage` | 生成覆盖率报告 |
| `-r` | 随机测试顺序（发现测试间依赖） |
| `--slow N` | 标记耗时超过 N 毫秒的慢测试 |
| `--junit FILE` | 生成 JUnit XML 报告（CI 用） |
| `--retry N` | 失败的测试最多重跑 N 次（检测不稳定测试） |
| `--list` | 列出所有可用测试模块 |
| `--py-only` | 只跑 Python 测试 |
| `--js-only` | 只跑 JS 测试 |

### 示例

```bash
# 详细模式 + 覆盖率 + 快速失败
python run_tests.py -v -c -f

# CI 环境：生成 JUnit 报告 + 覆盖率
python run_tests.py --junit report.xml -c

# 检测不稳定测试（失败重跑 3 次）
python run_tests.py --retry 3

# 随机顺序 + 慢测试标记（发现顺序依赖和性能问题）
python run_tests.py -r --slow 500
```

---

## 测试组织

### 目录结构

```
futures-orderflow/
├── run_tests.py          # 测试入口脚本
├── tests/                # 测试文件目录
│   ├── test_gap_stop.py
│   ├── test_kelly_factor.py
│   ├── test_price_protection.py
│   ├── test_corr_gate.py
│   ├── test_take_profit.py
│   ├── test_signal_trigger.py
│   ├── test_risk_gate.py
│   ├── test_t_score.py
│   ├── test_hidden_pivot.py
│   └── ...
├── .coveragerc           # coverage.py 配置
└── ...
```

### 测试模块注册

新增测试模块需要在 `run_tests.py` 的 `TEST_MODULES` 字典中注册：

```python
TEST_MODULES = {
    "gap_stop": "tests.test_gap_stop",
    "kelly_factor": "tests.test_kelly_factor",
    # ... 新增的测试模块在这里加一行
}
```

或者运行自动发现脚本：

```bash
python scripts/discover_tests.py --update
```

---

## 测试编写规范

### 1. 纯函数测试

测试纯函数时，直接传入参数，断言输出：

```python
def test_compute_bias_FC_basic():
    result = compute_bias_FC(F=60.0, C=40.0)
    assert result == 21.0  # 0.25*60 + 0.15*40
```

### 2. 参数化测试

使用 `@pytest.mark.parametrize` 覆盖多种输入：

```python
import pytest


@pytest.mark.parametrize(
    "F, C, expected",
    [
        (60.0, 40.0, 21.0),
        (0.0, 0.0, 0.0),
        (-60.0, -40.0, -21.0),
        (100.0, 100.0, 40.0),
    ],
)
def test_compute_bias_FC_parametrized(F, C, expected):
    assert compute_bias_FC(F, C) == expected
```

### 3. 边界条件测试

覆盖零值、极值、异常输入：

```python
def test_check_hard_veto_dir_zero():
    # dir_T=0 时不应触发硬否决
    vetoed, reason = check_hard_veto(bias_FC=100.0, dir_T=0)
    assert vetoed == False
    assert reason == ""
```

### 4. 历史 bug 回归测试

每个 bug 修复都要加测试，标注 bug 编号：

```python
def test_pc_hard_veto_threshold():
    """P-C 历史 bug：原硬否决用 bias_G≥60，几乎永远达不到。
    修复后改用 bias_FC + fc_hard=25，阈值可达。"""
    # bias_FC=30 应该触发硬否决（30 >= 25）
    vetoed, _ = check_hard_veto(bias_FC=30.0, dir_T=-1, fc_hard=25.0)
    assert vetoed == True
```

---

## Hypothesis 属性测试

### 基本用法

```python
from hypothesis import given
from hypothesis.strategies import floats, integers


@given(
    equity=floats(min_value=1000, max_value=10_000_000),
    risk_pct=floats(min_value=0.1, max_value=5.0),
    stop_pts=floats(min_value=1, max_value=1000),
    multiplier=floats(min_value=1, max_value=100),
)
def test_calc_risk_lots_non_negative(equity, risk_pct, stop_pts, multiplier):
    """属性：风险预算手数始终 >= 0"""
    result = calc_risk_lots(equity, risk_pct, stop_pts, multiplier)
    assert result >= 0
```

### 常用策略

| 策略 | 说明 |
|------|------|
| `floats(min_value, max_value)` | 浮点数 |
| `integers(min_value, max_value)` | 整数 |
| `lists(elements, min_size, max_size)` | 列表 |
| `one_of(strategy1, strategy2)` | 二选一 |
| `sampled_from([...])` | 从列表中取样 |

---

## CI/CD 集成

`run_tests.py` 支持生成 JUnit XML 报告，便于 CI/CD 系统（如 Jenkins、GitHub Actions）解析测试结果：

```bash
python run_tests.py --junit test-results.xml -c
```

JUnit XML 包含：
- 测试总数、通过数、失败数、跳过数
- 每个测试的耗时
- 失败测试的详细错误信息

---

## 相关文档

- [测试体系总览](overview.md) — 测试分层、模块清单
- [覆盖率说明](coverage.md) — coverage.py、Codecov、.coveragerc 配置
- [回归测试与基准测试](regression.md) — 回归测试策略、基准测试方法
