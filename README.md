# Futures OrderFlow

期货订单流策略系统。

## 状态

| 类别 | 状态 |
|---|---|
| **测试** | [![Test](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/test.yml) |
| **代码质量** | [![Code Quality](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/code-quality.yml/badge.svg?branch=main)](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/code-quality.yml) |
| **安全扫描** | [![Security](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/security.yml) |
| **CodeQL** | [![CodeQL](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/alonglong5118-rgb/futures-orderflow/actions/workflows/codeql.yml) |
| **覆盖率** | [![codecov](https://codecov.io/gh/alonglong5118-rgb/futures-orderflow/branch/main/graph/badge.svg)](https://codecov.io/gh/alonglong5118-rgb/futures-orderflow) |
| **Scorecard** | [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/alonglong5118-rgb/futures-orderflow/badge)](https://scorecard.dev/viewer/?uri=github.com/alonglong5118-rgb/futures-orderflow) |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 运行测试
python run_tests.py unit
```

## 项目结构

```
├── four_dim_strategy.py     # 四维策略核心
├── risk_gate_utils.py       # 风控工具
├── price_protection.py      # 价格保护
├── consistency_watchdog.py  # 一致性监控
├── sr_analyzer.py           # 支撑阻力分析
├── execution_planner.py     # 执行规划
├── run_tests.py             # 测试运行器
├── tests/                   # 测试用例
├── scripts/                 # 工具脚本
└── .github/workflows/       # CI 配置
```

## 开发

```bash
# 安装 pre-commit hooks
pre-commit install

# 运行所有检查
make lint
make test
make coverage
```
