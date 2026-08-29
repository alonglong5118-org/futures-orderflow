# 代码风格指南

本项目使用 [Ruff](https://github.com/astral-sh/ruff) 进行代码格式化与静态检查，使用 [mypy](https://mypy-lang.org/) 进行类型检查。本文档详细介绍相关配置与使用方法。

---

## 快速命令

```bash
# 自动格式化
make format

# 格式检查（不修改）
make format-check

# 全量 lint
make lint

# 阻塞级 lint（CI 同款）
make lint-critical

# 类型检查
mypy 模块名.py
```

---

## Ruff 配置

项目的 Ruff 配置定义在 `pyproject.toml` 中。

### 基础配置

```toml
[tool.ruff]
target-version = "py310"
line-length = 120
```

- **目标版本**：Python 3.10+
- **行宽**：120 字符

### 排除目录

以下目录不参与 Ruff 检查：

| 目录 | 说明 |
|---|---|
| `ponytail/` | 外部项目 |
| `ga_v4_*` | 回测结果目录 |
| `ga_v5_*` | 回测结果目录 |
| `.git/` | Git 仓库 |
| `__pycache__/` | Python 缓存 |
| `.venv/` / `venv/` | 虚拟环境 |

### Lint 规则集

项目启用的规则集：

| 前缀 | 规则集 | 说明 |
|---|---|---|
| `E` / `W` | pycodestyle | 风格与格式错误 |
| `F` | Pyflakes | 逻辑错误（未使用导入、未定义变量等） |
| `I` | isort | 导入排序 |
| `B` | flake8-bugbear | Bug 检测 |
| `UP` | pyupgrade | Python 版本升级建议 |
| `PTH` | flake8-use-pathlib | 建议使用 pathlib |

### 忽略的规则

| 规则码 | 说明 | 原因 |
|---|---|---|
| `E501` | line too long | 由 `line-length` 配置统一控制 |
| `F403` | `from module import *` | 策略文件中常见用法 |
| `F405` | name may be undefined | `import *` 带来的正常现象 |
| `B008` | function call does not bind | 装饰器中常见 |
| `UP007` | `X | Y` 类型联合 | 渐进迁移中 |
| `UP006` | `list/dict` 泛型 | 渐进迁移中 |

### 测试文件特殊规则

测试文件（`tests/*.py`）额外放宽以下规则：

| 规则码 | 说明 |
|---|---|
| `E402` | module level import not at top |
| `F841` | unused variable |
| `S101` | assert used |

### 格式化配置

```toml
[tool.ruff.format]
quote-style = "double"       # 双引号
indent-style = "space"       # 空格缩进
skip-magic-trailing-comma = false
line-ending = "auto"
```

---

## mypy 配置

项目的 mypy 配置定义在 `pyproject.toml` 中。

### 基础配置

```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = false
warn_unused_configs = true
disallow_untyped_defs = false
check_untyped_defs = false
ignore_missing_imports = true
follow_imports = "silent"
warn_redundant_casts = true
show_error_codes = true
```

### 配置说明

| 配置项 | 值 | 说明 |
|---|---|---|
| `python_version` | `3.10` | 目标 Python 版本 |
| `disallow_untyped_defs` | `false` | 不强制要求所有函数有类型注解 |
| `check_untyped_defs` | `false` | 不对无类型注解的函数做类型检查 |
| `ignore_missing_imports` | `true` | 忽略缺少类型存根的第三方库 |
| `warn_redundant_casts` | `true` | 警告冗余的类型转换 |
| `show_error_codes` | `true` | 显示错误码 |

### 排除目录

- `ponytail/` — 外部项目
- `ga_v4_*/` / `ga_v5_*/` — 回测结果目录
- `tests/` — 测试文件不做类型检查

---

## 编码规范速查

### 命名规范

| 元素 | 规范 | 示例 |
|---|---|---|
| 模块/文件 | `snake_case` | `risk_gate_utils.py` |
| 函数/方法 | `snake_case` | `calculate_kelly()` |
| 变量 | `snake_case` | `win_rate` |
| 常量 | `UPPER_SNAKE_CASE` | `MAX_POSITION` |
| 类 | `PascalCase` | `RiskGate` |
| 异常类 | `PascalCase` + `Error` | `CalibrationError` |

### 导入规范

导入按以下顺序排列，由 Ruff I 规则自动管理：

1. 标准库导入
2. 第三方库导入
3. 本地项目导入

每组之间空一行，组内按字母顺序排列。

```python
# ✅ 正确示例
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from risk_gate_utils import RiskGate
from kelly_utils import kelly_fraction
```

### 函数定义

- 公共函数添加 docstring
- 核心函数添加完整的类型注解
- 参数较多时使用 dataclass 封装

```python
# ✅ 好的示例
@dataclass
class RiskGateConfig:
    """风险门禁配置。"""
    max_position: int = 10
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10


def check_risk(
    position: Position,
    market_data: MarketData,
    config: RiskGateConfig | None = None,
) -> GateResult:
    """执行风险门禁检查。

    Args:
        position: 当前持仓对象
        market_data: 最新市场数据
        config: 门禁配置，为 None 时使用默认配置

    Returns:
        检查结果，包含是否通过及原因列表
    """
    ...
```

---

## 常见 Lint 错误处理

| 错误码 | 含义 | 解决方法 |
|---|---|---|
| `F401` | 导入未使用 | 删除未使用的导入，或加 `# noqa: F401` |
| `F821` | 未定义的名字 | 检查是否拼写错误或缺少导入 |
| `I001` | 导入顺序不对 | 运行 `make format` 自动修复 |
| `E722` | 裸 except | 改为捕获具体异常类型 |
| `B006` | 可变默认参数 | 使用 `None` 作为默认值，函数内初始化 |
| `UP028` | 可改用 `YAML` 注释 | 视情况采纳 |

---

## Make 命令速查

```bash
make format           # 自动格式化所有 Python 文件
make format-check     # 检查格式（不修改文件）
make lint             # 全量 lint 检查
make lint-critical    # 阻塞级 lint（CI 同款）
make quality          # 格式 + lint + 冒烟测试
make smoke            # 冒烟测试（快速验证）
make test             # 运行全部测试
make coverage         # 生成覆盖率报告
make security         # 安全检查（bandit）
make all              # 全量检查（等同 nightly）
```

更多命令请查看项目根目录的 `Makefile`，或运行 `make help`。
