# 覆盖率说明

## 概述

四维策略使用 **coverage.py** 进行代码覆盖率统计，通过 `.coveragerc` 文件进行配置。覆盖率报告帮助识别未被测试覆盖的代码路径，指导测试补充方向。

---

## 快速开始

### 生成覆盖率报告

```bash
# 使用 run_tests.py 生成覆盖率
python run_tests.py -c

# 或直接使用 pytest-cov
pytest --cov=. --cov-report=term --cov-report=html
```

### 查看报告

```bash
# 终端摘要（默认）
coverage report

# HTML 报告（可交互，查看每行覆盖情况）
open htmlcov/index.html

# XML 报告（CI/CD 用）
coverage xml
```

---

## .coveragerc 配置详解

配置文件位于项目根目录 `.coveragerc`，分为以下几个部分。

### [run] — 运行配置

```ini
[run]
source = .
```

- `source = .`：统计当前目录下所有 Python 文件的覆盖率

#### 排除的文件（omit）

`.coveragerc` 中通过 `omit` 配置排除不需要统计覆盖率的文件，分为以下类别：

| 类别 | 模式 | 说明 |
|------|------|------|
| 测试文件 | `tests/*`、`test_*.py` | 测试代码本身不计入覆盖率 |
| 缓存文件 | `*/__pycache__/*`、`*.pyc` | Python 字节码缓存 |
| 入口脚本 | `run_tests.py` | 测试入口脚本 |
| 备份文件 | `*.bak_*` | 备份文件 |
| 临时/探针脚本 | `_*.py` | 下划线开头的临时诊断脚本 |
| GA 优化脚本 | `ga_*.py` | 遗传算法一次性脚本 |
| 工具脚本 | `scripts/*`、`verify_*.py`、`*_backtest.py` 等 | 非核心库代码 |
| 回测/分析脚本 | `regression_test.py`、`perf_breakdown.py` 等 | 非库代码，显式列出 |
| 数据源/经纪商/实盘 | `minishare_*.py`、`tushare_live.py`、`akshare_live.py` 等 | 依赖外部 API，不适合单元测试 |
| 第三方代码 | `ponytail/*`、`node_modules/*`、`.venv/*`、`venv/*` | 第三方依赖 |

**完整排除列表**包括 60+ 个文件/模式，涵盖：
- 回测类脚本（`four_dim_backtest`、`four_dim_oos_compare`、`ga_*` 等）
- 实盘运行类（`four_dim_live_runner`、`four_dim_papertrack` 等）
- 数据接入类（`minishare_feed`、`tushare_live`、`akshare_live`、`backend_tqsdk` 等）
- 工具类（`sentiment_engine`、`feature_manager`、`calibration`、`gbm_garch` 等）

### [report] — 报告配置

```ini
[report]
precision = 1
skip_covered = false
sort = Cover
```

| 配置 | 值 | 说明 |
|------|-----|------|
| `precision` | `1` | 显示精度（保留 1 位小数） |
| `skip_covered` | `false` | 不跳过 100% 覆盖的文件（显示所有文件） |
| `sort` | `Cover` | 按覆盖率排序（从低到高，便于找短板） |

#### 排除的代码行（exclude_lines）

```ini
exclude_lines =
    pragma: no cover
    def __repr__
    if __name__ == .__main__.:
    raise AssertionError
    raise NotImplementedError
    if 0:
    if __debug__:
    pass
```

这些行模式不会计入未覆盖：

- `pragma: no cover` — 手动标记无需覆盖的代码
- `def __repr__` — repr 方法通常不测试
- `if __name__ == "__main__":` — 主入口代码
- `raise AssertionError` / `raise NotImplementedError` — 异常抛出
- `if 0:` — 永假分支
- `if __debug__:` — 调试分支
- `pass` — 空语句

### [html] — HTML 报告

```ini
[html]
directory = htmlcov
```

HTML 报告输出到 `htmlcov/` 目录，可在浏览器中打开查看每行的覆盖情况。

### [xml] — XML 报告

```ini
[xml]
output = coverage.xml
```

XML 报告输出到 `coverage.xml`，供 CI/CD 系统（如 Codecov、Jenkins）解析。

---

## 覆盖率使用指南

### 1. 查看低覆盖率文件

```bash
coverage report | sort -t' ' -k3 -n | head -20
```

按覆盖率从低到高排序，找出最需要补充测试的模块。

### 2. 查看具体文件的未覆盖行

```bash
coverage report -m signal_trigger_utils.py
```

`-m` 参数显示缺失的行号。

### 3. HTML 交互式查看

```bash
python run_tests.py -c
open htmlcov/index.html
```

在浏览器中点击文件名，可以看到红绿标注的源码，直观了解哪些行未被覆盖。

### 4. 只跑特定模块并查看覆盖率

```bash
pytest tests/test_gap_stop.py --cov=gap_stop_utils --cov-report=term-missing
```

---

## 覆盖率提升策略

### 优先级 1：核心纯函数模块

优先覆盖核心计算逻辑，这些模块：
- 纯函数，容易测试
- 影响交易决策，正确性至关重要
- 已有测试模板可参考

**目标模块**：kelly_utils、gap_stop_utils、price_protection、corr_gate_utils、signal_trigger_utils、risk_gate_utils、take_profit_utils、t_score_utils、hidden_pivot

### 优先级 2：边界条件和异常路径

覆盖率提升后期，重点关注：
- 错误处理分支
- 边界值判断
- None / 空值 / 零值处理
- 类型转换异常

### 优先级 3：集成测试覆盖

通过集成测试覆盖模块间交互路径。

---

## 注意事项

1. **覆盖率不是唯一指标**：100% 覆盖率不等于没有 bug，只是说明所有代码路径都被执行过。
2. **重点关注核心逻辑**：数据源接入、第三方 API 封装等模块覆盖率低是正常的，因为它们依赖外部服务。
3. **`pragma: no cover` 合理使用**：对于确实不需要测试的代码（如调试辅助函数），可以用 `# pragma: no cover` 标记排除。
4. **排除列表维护**：新增非库脚本时，记得在 `.coveragerc` 的 `omit` 中添加，避免拉低整体覆盖率。
5. **HTML 报告是最好的工具**：终端报告只能看到文件级覆盖率，HTML 报告可以看到具体哪些行没覆盖。

---

## 相关文档

- [测试体系总览](overview.md) — 测试分层、模块清单
- [测试框架说明](test-framework.md) — pytest、hypothesis、运行方式
- [回归测试与基准测试](regression.md) — 回归测试策略、基准测试方法
