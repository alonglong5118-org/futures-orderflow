# Futures OrderFlow · 期货订单流策略系统

> 基于订单流数据的多维度期货量化交易策略框架，集成四维策略、风控管理、回测验证与实盘执行。

<!-- 项目状态徽章 -->
<p align="center">
  <a href="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/test.yml">
    <img src="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/test.yml/badge.svg?branch=main" alt="Test">
  </a>
  <a href="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/code-quality.yml">
    <img src="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/code-quality.yml/badge.svg?branch=main" alt="Code Quality">
  </a>
  <a href="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/security.yml">
    <img src="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/security.yml/badge.svg?branch=main" alt="Security">
  </a>
  <a href="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/codeql.yml">
    <img src="https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/codeql.yml/badge.svg?branch=main" alt="CodeQL">
  </a>
  <a href="https://codecov.io/gh/alonglong5118-org/futures-orderflow">
    <img src="https://codecov.io/gh/alonglong5118-org/futures-orderflow/branch/main/graph/badge.svg" alt="codecov">
  </a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/alonglong5118-org/futures-orderflow">
    <img src="https://api.scorecard.dev/projects/github.com/alonglong5118-org/futures-orderflow/badge" alt="OpenSSF Scorecard">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  </a>
</p>

---

## 📑 目录

- [特性](#-特性)
- [快速开始](#-快速开始)
- [架构概览](#️-架构概览)
- [核心模块](#-核心模块)
- [快速示例](#-快速示例)
- [测试体系](#-测试体系)
- [开发工具](#-开发工具)
- [CI/CD 流水线](#-cicd-流水线)
- [性能基准](#-性能基准)
- [路线图](#-路线图)
- [常见问题](#-常见问题)
- [安全](#-安全)
- [贡献指南](#-贡献指南)
- [社区与支持](#-社区与支持)
- [许可证](#-许可证)

---

## ✨ 特性

- **四维策略引擎** — 基于成交量、持仓量、价格、时间四个维度的综合研判
- **多层风控体系** — 风险门禁、价格保护、一致性监控、仓位管理多重保障
- **遗传算法优化** — GA 因子挖掘、参数优化、OOS 验证一体化流程
- **多经纪商适配** — 支持 TqSdk、其他主流期货经纪商接入
- **回测验证系统** — 完整的回测框架 + 基准回归测试 + 性能基准
- **实盘监控** — 实时健康检查、账户追踪、异常告警
- **供应链安全** — SBOM + 漏洞扫描 + 依赖审计，保障软件供应链安全

---

## 🚀 快速开始

### 环境要求

- Python **3.10+**
- 推荐使用虚拟环境（venv / conda）

### 安装

```bash
# 克隆仓库
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt      # 运行时依赖
pip install -r requirements-dev.txt  # 开发依赖（测试、lint 等）
```

### 运行测试

```bash
# 运行全部单元测试
python run_tests.py unit --py-only

# 或使用 Makefile
make test

# 冒烟测试（快速验证，< 1 秒）
make smoke
```

### 快速体验

```bash
# 查看可用命令
make help

# 运行质量检查
make quality

# 生成覆盖率报告
make coverage
```

---

## 🏗️ 架构概览

```
futures-orderflow/
├── 🧠 策略层
│   ├── four_dim_strategy.py       # 四维策略核心
│   ├── strategy_layer.py          # 策略层管理
│   ├── sentiment_engine.py        # 情绪引擎
│   └── ga_*.py                    # 遗传算法优化系列
│
├── 🛡️  风控层
│   ├── risk_gate_utils.py         # 风险门禁
│   ├── risk_state_machine.py      # 风控状态机
│   ├── price_protection.py        # 价格保护
│   ├── kelly_utils.py             # 凯利公式仓位管理
│   └── gap_stop_utils.py          # 缺口止损
│
├── 📊 分析层
│   ├── sr_analyzer.py             # 支撑阻力分析
│   ├── hidden_pivot.py            # 隐藏枢轴
│   ├── t_score_utils.py           # T 分数统计
│   ├── calibration.py             # 校准工具
│   └── anomaly_scan.py            # 异常扫描
│
├── ⚡ 执行层
│   ├── execution_planner.py       # 执行规划
│   ├── backend_tqsdk.py           # TqSdk 后端
│   ├── tick_orderflow.py          # Tick 订单流处理
│   └── preflight_check.py         # 起飞前检查
│
├── 📡 数据层
│   ├── akshare_live.py            # AkShare 数据源
│   ├── tushare_live.py            # Tushare 数据源
│   ├── minishare_feed.py          # 迷你行情源
│   └── macro_context.py           # 宏观数据
│
├── 🔍 监控层
│   ├── consistency_watchdog.py    # 一致性监控
│   ├── live_health_check.py       # 实盘健康检查
│   ├── account_tracker.py         # 账户追踪
│   ├── trade_journal.py           # 交易日志
│   └── drawdown_guard.py          # 回撤守护
│
├── 🧪 测试
│   ├── tests/                     # 272+ 测试用例
│   ├── run_tests.py               # 测试运行器
│   └── scripts/                   # 工具脚本
│
└── ⚙️ CI/CD
    └── .github/workflows/         # 12 个 CI Workflow
```

---

## 📦 核心模块

### 四维策略

| 模块 | 说明 |
|---|---|
| `four_dim_strategy.py` | 四维策略核心引擎 |
| `four_dim_calibrate.py` | 策略参数校准 |
| `four_dim_papertrack.py` | 模拟盘运行器 |
| `four_dim_oos_compare.py` | OOS 样本外对比 |

### 风控体系

| 模块 | 说明 |
|---|---|
| `risk_gate_utils.py` | 风险门禁（多维度风险过滤） |
| `risk_state_machine.py` | 风控状态机（状态驱动风控） |
| `price_protection.py` | 价格保护（滑点/异常价格检测） |
| `kelly_utils.py` | 凯利公式仓位计算 |
| `gap_stop_utils.py` | 缺口止损逻辑 |

### 分析工具

| 模块 | 说明 |
|---|---|
| `sr_analyzer.py` | 支撑阻力分析 |
| `hidden_pivot.py` | 隐藏枢轴点检测 |
| `t_score_utils.py` | T 分数统计检验 |
| `calibration.py` | 参数校准工具 |
| `gbm_garch.py` | GBM-GARCH 波动率模型 |
| `regime_hmm.py` | 市场状态 HMM 识别 |

### 遗传算法优化

| 模块 | 说明 |
|---|---|
| `ga_factor_miner.py` | 因子挖掘 |
| `ga_six_factor.py` | 六因子模型 |
| `ga_tpsl_optimizer.py` | 止盈止损优化 |
| `ga_oos_validation.py` | OOS 验证 |
| `ga_quality_filter.py` | 质量过滤 |

---

## 💡 快速示例

### 风险门禁检查

```python
from risk_gate_utils import RiskGate, RiskGateConfig

# 配置风险门禁
config = RiskGateConfig(
    max_position=10,  # 最大持仓手数
    max_daily_loss_pct=0.03,  # 日最大亏损比例 3%
    max_drawdown_pct=0.10,  # 最大回撤 10%
    corr_threshold=0.7,  # 相关性阈值
)

gate = RiskGate(config)

# 检查是否允许开仓
result = gate.check(
    signal=my_signal,
    position=current_position,
    portfolio=portfolio,
    market_data=market_data,
)

if result.passed:
    execute_order(signal)
else:
    print(f"风险门禁拦截: {result.reasons}")
```

### 凯利仓位计算

```python
from kelly_utils import kelly_fraction, kelly_position_size

# 计算凯利比例
fraction = kelly_fraction(
    win_rate=0.55,  # 胜率 55%
    win_loss_ratio=1.5,  # 盈亏比 1.5:1
)
print(f"凯利仓位: {fraction:.2%}")  # 约 25%

# 计算实际仓位手数
position_size = kelly_position_size(
    account_balance=100_000,
    win_rate=0.55,
    win_loss_ratio=1.5,
    contract_value=50_000,
    kelly_multiplier=0.5,  # 半凯利（更保守）
)
print(f"建议仓位: {position_size:.1f} 手")
```

### 支撑阻力分析

```python
from sr_analyzer import SRAnalyzer

analyzer = SRAnalyzer(bar_count=200)
levels = analyzer.detect(bars=kline_data)

for level in levels:
    print(f"{level.type}: {level.price:.2f} (强度: {level.strength}/5)")
```

> 💡 更多示例请参考 `tests/` 目录下的测试用例，它们也是很好的用法参考。

---

## 🧪 测试体系

### 测试层级

```
┌─────────────────────────────────────────────────┐
│  性能测试（performance）                        │
│  基准回归（baseline regression）                │
│  属性测试（property-based / Hypothesis）        │
├─────────────────────────────────────────────────┤
│  集成测试（integration）                        │
│  · 回测集成 · 管道集成 · 深度集成               │
├─────────────────────────────────────────────────┤
│  单元测试（unit）                               │
│  · 90+ 测试模块 · 272+ 测试用例                 │
└─────────────────────────────────────────────────┘
```

### 常用测试命令

```bash
# 快速验证
make smoke           # 冒烟测试
make unit            # 单元测试

# 完整测试
make test            # 全部单元测试
make integration     # 集成测试
make all             # 全部测试（含属性/基准/性能）

# 质量相关
make coverage        # 覆盖率报告
make quality         # lint + format 检查
make flake           # 不稳定测试检测（重跑 3 次）
```

---

## 🔧 开发工具

### 代码质量

```bash
make lint            # Ruff 全量 lint
make lint-critical   # 阻塞级 lint（CI 同款）
make format          # 自动格式化
make format-check    # 格式检查
make typecheck       # Mypy 类型检查
make quality         # 全套质量检查
```

### 安全扫描

```bash
make secretscan      # 密钥泄露检测（gitleaks）
make bandit          # 代码安全扫描（高危）
make depscan         # 依赖漏洞扫描（pip-audit）
make security        # 全部安全检查
```

### Git Hooks

```bash
make hooks           # 安装 pre-commit / pre-push 钩子
```

pre-commit：单元测试 + 格式检查
pre-push：回归测试 + 质量检查

---

## 🏭 CI/CD 流水线

项目配置了 12 个 GitHub Actions Workflow：

| Workflow | 触发 | 说明 |
|---|---|---|
| **test.yml** | push / PR / merge_queue | 多平台 × 多 Python 版本测试矩阵 |
| **code-quality.yml** | push / PR | Ruff lint、格式检查、命名规范 |
| **security.yml** | push / PR | 密钥扫描、依赖审计、代码安全 |
| **codeql.yml** | push / schedule | CodeQL 语义分析 |
| **scorecard.yml** | schedule / push | OpenSSF Scorecard 安全评分 |
| **benchmark.yml** | push / 手动 / PR 评论 | 性能基准测试 + 趋势追踪 |
| **sbom.yml** | push / release | SBOM 生成 + 漏洞扫描 |
| **release-drafter.yml** | push / PR / tag | 自动发布日志 + 版本管理 |
| **pr-automation.yml** | PR | Size 标签、自动分配、标题检查 |
| **nightly.yml** | schedule | 夜间全量测试 + 覆盖率检查 |
| **stale.yml** | schedule | 过期 Issue/PR 清理 |
| **cleanup-branches.yml** | push | 已合并分支清理 |

在 PR 中评论 `/benchmark` 可手动触发性能基准对比。

---

## 📈 性能基准

项目内置性能基准测试，追踪核心函数性能变化：

```bash
# 运行性能基准
make perf

# 严格模式（退化超过阈值则失败）
make bench-strict

# 更新性能基线
make bench-update

# 对比两次基准结果
python scripts/compare_benchmarks.py \
  --results new.json --baseline old.json

# 查看性能趋势
python scripts/perf_trend.py chart --trend trend.json --benchmark "risk_gate"
```

---

## 🗺️ 路线图

以下是项目的发展规划，按优先级排列。欢迎通过 Issue/PR 参与讨论和贡献。

### ✅ 已完成

- [x] 四维策略核心框架
- [x] 多层风控体系（风险门禁 / 价格保护 / 一致性监控）
- [x] 遗传算法优化工具链
- [x] 回测系统 + 基准回归测试
- [x] TqSdk 实盘接入
- [x] 完整的 CI/CD 流水线
- [x] 性能基准测试与趋势追踪
- [x] SBOM + 供应链安全
- [x] OpenSSF Scorecard 安全评分

### 🚧 进行中 / 计划中

- [ ] **实盘 Web 监控面板** — 实时行情 + 账户 + 信号可视化
- [ ] **多策略组合管理** — 多品种/多策略资金分配与风控
- [ ] **因子库扩展** — 更多订单流因子与情绪因子
- [ ] **策略文档站** — MkDocs 搭建的完整文档站点
- [ ] **Docker 化部署** — 一键启动实盘环境
- [ ] **移动端通知** — 微信 / Telegram / 钉钉告警推送
- [ ] **机器学习模型** — LSTM / Transformer 辅助信号判断

### 💡 未来探索

- [ ] **多账户管理** — 多账户资金分配与风控隔离
- [ ] **社区策略市场** — 分享和订阅第三方策略
- [ ] **云原生部署** — K8s 集群化运行与弹性扩缩

> 有想法？欢迎通过 [Issue](https://github.com/alonglong5118-org/futures-orderflow/issues) 讨论！

---

## ❓ 常见问题

### 这是什么类型的项目？

这是一个期货订单流策略框架，基于成交量、持仓量、价格、时间四个维度进行交易决策。它包含策略引擎、风控系统、回测工具和实盘接入。

### 可以直接用于实盘交易吗？

本项目是策略研究和回测框架，实盘功能需要自行配置经纪商接口和参数。**实盘交易有风险，请充分回测和验证后谨慎使用**。

### 支持哪些期货经纪商？

目前主要适配 TqSdk（支持多家期货公司），架构上支持扩展其他经纪商后端。

### 运行环境有什么要求？

- Python 3.10+
- 操作系统：Linux / macOS / Windows 均可
- 内存：建议 4GB 以上（回测时需要更多）
- 实盘需要稳定的网络连接

### 如何贡献自己的策略？

欢迎贡献！请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发流程。策略类贡献需要提供：
1. 清晰的策略逻辑说明
2. 配套的单元测试
3. 回测结果验证
4. 遵循项目代码风格

### 项目使用什么许可证？

MIT 许可证，可自由使用、修改和分发。详见 [LICENSE](LICENSE)。

### 如何报告 Bug 或提出建议？

通过 [GitHub Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 提交，尽量提供详细的复现步骤和环境信息。

---

## 🔒 安全

- **SBOM** — 每次发布生成 SPDX + CycloneDX 双格式软件物料清单
- **漏洞扫描** — Grype + pip-audit 双重漏洞扫描，高危漏洞阻塞发布
- **代码安全** — CodeQL + Bandit 静态安全分析
- **密钥检测** — gitleaks 防止密钥泄露
- **Scorecard** — OpenSSF Scorecard 供应链安全评分
- **安全策略** — 参见 [SECURITY.md](SECURITY.md)

---

## 📝 贡献指南

我们欢迎所有形式的贡献！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发流程和规范。

### 快速开始贡献

```bash
# 1. Fork 并克隆
git clone your-fork-url
cd futures-orderflow

# 2. 安装开发依赖 + hooks
make deps
make hooks

# 3. 创建分支
git checkout -b feat/your-feature

# 4. 开发 + 测试
make test
make quality

# 5. 提交 PR
```

---

## 💬 社区与支持

### 讨论与交流

- **[GitHub Discussions](https://github.com/alonglong5118-org/futures-orderflow/discussions)** — 技术讨论、经验分享、问答
- **[GitHub Issues](https://github.com/alonglong5118-org/futures-orderflow/issues)** — Bug 报告、功能建议

### 寻求帮助

如果你遇到问题，可以按以下途径寻求帮助：

1. **先看文档** — 检查 README、CONTRIBUTING.md 和相关测试用例
2. **搜索 Issue** — 搜索是否有人遇到过类似问题
3. **发起讨论** — 在 Discussions 中发帖提问
4. **提交 Issue** — 确认是 Bug 后，提交详细的 Issue

提问时请尽量提供：
- 你在做什么（预期行为）
- 实际发生了什么
- 复现步骤（最小可复现示例）
- 环境信息（Python 版本、操作系统、依赖版本）

### 项目状态

- **维护状态**：活跃开发中
- **主要维护者**：[@alonglong5118-org](https://github.com/alonglong5118-org)
- **回复时间**：通常 1-3 个工作日内回复

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

## 🙏 致谢

感谢所有为这个项目做出贡献的开发者，以及开源社区提供的优秀工具和库。

---

<div align="center">
  <sub>Built with ❤️ by the futures-orderflow team</sub>
</div>
