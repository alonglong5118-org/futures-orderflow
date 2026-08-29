# 快速上手指南

本指南将帮助你在几分钟内完成 Futures OrderFlow 的安装配置，并运行第一个策略示例。

---

## 环境要求

在开始之前，请确保你的系统满足以下最低要求：

| 项目 | 要求 | 说明 |
|---|---|---|
| **Python** | 3.10+ | 推荐使用 3.11 或 3.12 |
| **操作系统** | Linux / macOS / Windows | 全平台支持 |
| **内存** | 4 GB+ | 回测时建议 8 GB 以上 |
| **磁盘空间** | 1 GB+ | 源码与依赖占用 |
| **网络** | 稳定连接 | 实盘与数据获取需要 |

!!! tip "Python 版本检查"
    在终端运行以下命令确认 Python 版本：
    ```bash
    python3 --version
    ```
    如果版本低于 3.10，请先升级 Python。

---

## 安装步骤

### 1. 克隆仓库

```bash
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow
```

### 2. 创建虚拟环境

=== "venv（推荐）"

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate  # macOS / Linux
    # Windows: .venv\Scripts\activate
    ```

=== "conda"

    ```bash
    conda create -n futures-orderflow python=3.11
    conda activate futures-orderflow
    ```

!!! success "虚拟环境激活"
    激活成功后，终端提示符前会显示环境名称（如 `(.venv)` 或 `(futures-orderflow)`）。

### 3. 安装依赖

```bash
# 安装运行时依赖（必需）
pip install -r requirements.txt

# 安装开发与测试依赖（推荐）
pip install -r requirements-dev.txt
```

!!! info "依赖说明"
    - `requirements.txt` 包含核心运行时依赖（numpy、pandas、scipy、hmmlearn、plotly、tqdm）
    - `requirements-dev.txt` 包含测试、代码质量、安全扫描等开发工具
    - 可选数据源（akshare、tqsdk）和遗传算法（deap）按需单独安装

---

## 第一个策略示例

让我们通过一个完整的例子，快速体验风险门禁 + 凯利仓位管理的核心功能。

### 示例：构建一个简单的交易决策流程

```python
"""
快速示例：风险门禁 + 凯利仓位管理
演示如何使用核心模块构建基础交易决策流程
"""

from risk_gate_utils import RiskGate, RiskGateConfig
from kelly_utils import kelly_position_size


def main():
    # ── 1. 配置风险门禁 ──────────────────────────────
    config = RiskGateConfig(
        max_position=5,  # 最大持仓 5 手
        max_daily_loss_pct=0.02,  # 日最大亏损 2%
        max_drawdown_pct=0.08,  # 最大回撤 8%
        corr_threshold=0.6,  # 相关性阈值 0.6
    )
    gate = RiskGate(config)

    # ── 2. 模拟交易信号与账户数据 ────────────────────
    signal = {
        "direction": "long",
        "symbol": "SHFE.rb2501",
        "strength": 0.75,
    }

    portfolio = {
        "balance": 100_000,
        "daily_pnl": -500,  # 当日盈亏 -500
        "max_drawdown_pct": 0.03,  # 当前回撤 3%
    }

    position = {
        "symbol": "SHFE.rb2501",
        "volume": 2,  # 当前持仓 2 手
        "direction": "long",
    }

    # ── 3. 风险门禁检查 ──────────────────────────────
    result = gate.check(
        signal=signal,
        position=position,
        portfolio=portfolio,
        market_data={},  # 可传入行情数据用于更多检查
    )

    if not result.passed:
        print(f"⚠️  风险门禁拦截: {result.reasons}")
        return

    # ── 4. 计算凯利仓位 ──────────────────────────────
    position_size = kelly_position_size(
        account_balance=portfolio["balance"],
        win_rate=0.58,  # 历史胜率 58%
        win_loss_ratio=1.6,  # 盈亏比 1.6:1
        contract_value=50_000,  # 每手合约价值
        kelly_multiplier=0.5,  # 半凯利（保守策略）
    )

    print(f"✅ 信号通过风控检查")
    print(f"📊 建议仓位: {position_size:.1f} 手")
    print(f"📈 策略胜率: 58% | 盈亏比: 1.6:1")


if __name__ == "__main__":
    main()
```

### 运行示例

将上述代码保存为 `quick_demo.py`，然后运行：

```bash
python quick_demo.py
```

预期输出：

```
✅ 信号通过风控检查
📊 建议仓位: 1.2 手
📈 策略胜率: 58% | 盈亏比: 1.6:1
```

---

## 运行测试

项目拥有完善的测试体系，你可以通过运行测试来验证安装是否成功。

### 快速验证：冒烟测试

冒烟测试在 1 秒内完成，用于快速验证环境是否正常：

```bash
make smoke
```

!!! success "通过标准"
    如果看到 `OK` 且所有测试通过，说明安装成功。

### 单元测试

运行全部单元测试（272+ 用例）：

```bash
make test
# 或
make unit
```

### 集成测试

运行集成测试，验证模块间协作是否正常：

```bash
make integration
```

### 查看测试覆盖率

```bash
make coverage
```

运行后会在终端显示覆盖率报告，并生成 HTML 报告。

??? info "更多测试命令"
    | 命令 | 说明 |
    |---|---|
    | `make all` | 全部测试（含属性/基准/性能） |
    | `make advanced` | 属性测试 + 基准回归 + 性能测试 |
    | `make perf` | 性能基准测试 |
    | `make flake` | 不稳定测试检测（重跑 3 次） |
    | `make slow` | 列出慢测试（> 500ms） |

---

## 开发工具速览

### 代码质量检查

```bash
make quality   # lint + format + typecheck 全套检查
make lint      # Ruff 代码检查
make format    # 自动格式化代码
make typecheck # Mypy 类型检查
```

### 安全扫描

```bash
make security  # 全部安全检查
make bandit    # 代码安全扫描
make depscan   # 依赖漏洞扫描
```

---

## 常见问题

### Q: 安装依赖时出现编译错误怎么办？

**A:** 某些依赖（如 numpy、scipy）在部分平台上需要编译。建议：

1. 升级 pip 和 setuptools：
   ```bash
   pip install --upgrade pip setuptools wheel
   ```
2. 使用 Conda 安装科学计算库：
   ```bash
   conda install numpy scipy pandas
   ```

### Q: 运行测试时提示找不到模块？

**A:** 确保你在项目根目录下运行命令，并且虚拟环境已正确激活。可以尝试：

```bash
# 确认当前目录
pwd

# 确认 Python 路径
which python3
```

### Q: `make` 命令不可用怎么办？

**A:** Windows 用户可以直接使用 Python 脚本运行测试：

```bash
python run_tests.py unit --py-only    # 单元测试
python run_tests.py smoke --py-only   # 冒烟测试
```

### Q: 如何查看所有可用的 make 命令？

**A:** 运行 `make help` 查看完整的命令列表和说明。

### Q: 可选依赖如何安装？

**A:** 根据需要手动安装：

```bash
# AkShare 数据源
pip install akshare

# TqSdk 交易接口
pip install tqsdk

# 遗传算法优化
pip install deap
```

---

## 下一步

- 阅读 [安装配置指南](installation.md) 了解更详细的安装选项和配置说明
- 浏览 [架构设计](../architecture/overview.md) 深入理解系统分层设计
- 查看 [核心模块文档](../modules/risk-gate.md) 学习各模块的详细用法
- 阅读 [测试体系](../testing/overview.md) 了解项目的测试方法论
