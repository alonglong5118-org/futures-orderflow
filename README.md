# 三感参谋 · 期货信号决策系统（TriSense Advisor）

> 顾问式期货信号系统：对活跃品种做 **F / T / C 三维打分 → 四层风控**，
> 输出「方向 + 手数 + 止损 + T1/T2」，**但绝不自动下单**。

[![Test](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/test.yml)
[![Code Quality](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/code-quality.yml/badge.svg?branch=main)](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/code-quality.yml)
[![Security](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/alonglong5118-org/futures-orderflow/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

## ⚠️ 先读这段：它不是什么

**三感参谋不自动下单。** 主链路没有任何下单代码——`TqApi` 仅出现在取数与探针脚本中。
系统的定位是「顾问」：把三维打分和四层风控的结论摆在你面前，
**手指最后按下去的人是你**。这是半自动系统的底线，也是本项目的设计前提。

## 📖 命名约定

系统对外只有一个名字：**三感参谋（TriSense Advisor）**。

| 层次 | 用法 |
|---|---|
| 对外名 / UI / 文档标题 | 一律「三感参谋」 |
| 内部代号（`four_dim_*` 文件名、`four_dim` 标识符） | **保留不动**，仅作代码层标识 |
| 历史文档标题（`四维风控模型 v5/v6.0/v6.1`） | **保留原名不断链**，正文旧称不回改 |

> 说明：「三感参谋」是对外品牌名。代码内实际分层是 **F/T/C 三维打分 + 风控闸门**，
> 内部仍沿用 `four_dim` 代号——改名收益为零、风险实打实，故不动。

---

## 📑 目录

- [系统定位](#-系统定位)
- [核心链路](#-核心链路)
- [风控四层](#-风控四层)
- [运行形态](#-运行形态)
- [架构概览](#️-架构概览)
- [品种覆盖](#-品种覆盖)
- [快速开始](#-快速开始)
- [测试体系](#-测试体系)
- [🔒 策略改动铁律](#-策略改动铁律必读)
- [开发工具](#-开发工具)
- [CI/CD](#-cicd)
- [已知问题](#-已知问题)
- [贡献指南](#-贡献指南)
- [许可证](#-许可证)

---

## 🎯 系统定位

每 60 秒对全部活跃品种做一次三维打分与风控裁决，输出可执行的交易建议。

- **品种覆盖**：54 个品种定义 / 7 大板块，11 个被硬禁 → **43 个活跃**
- **输出**：方向（多/空）、建议手数、止损位、T1/T2 止盈位，附触发理由
- **不输出**：任何形式的自动委托

---

## 🔬 核心链路

```
品种日线/5m 数据
      │
      ├─ F  基本面偏置 ── 库存/基差/持仓结构等
      ├─ T  技术触发   ── T_5m 动量打分（主驱动）
      └─ C  资金确认   ── 成交持仓、资金流向
      │
      ▼
  bias_G = 0.6×T + 0.25×F + 0.15×C        ← 综合打分（combine_bias）
      │
      ▼
  触发判定：|T_5m| ≥ T_thresh_eff
      │        └─ 阈值经 6 段调制链：regime → HMM → GARCH → 情绪 → 支撑压力 → F/C 同向放宽(0.85)
      ▼
  风控四层裁决
      │
      ▼
  输出：方向 + 手数 + 止损 + T1/T2
```

两个设计值得留意：

1. **`bias_FC` 刻意排除 T**（`four_dim_strategy.py:3175`）——用纯 F/C 做独立否决，
   避免技术面自我循环论证。
2. **前视偏差防线干净**：HMM / GARCH / 情绪 / 宏观 / SR 五个调制器严格 live-only，
   回测时传 `None` 且**永不进入回测路径**（`walk_forward_backtest` 有 assert 锁死）。

---

## 🛡️ 风控四层

| 层次 | 触发条件 | 位置 | 后果 |
|---|---|---|---|
| **1. 品种级硬禁** | `DISABLED_SYMBOLS` | `four_dim_strategy.py:251` | 不参与信号评估 |
| **2. 信号级闸门** | `risk_gate()` 多维度否决 | `four_dim_strategy.py` | 拒绝该笔信号 |
| **3. 组合级** | VaR / 单笔 ≤1% / 相关性 / 板块集中度 | `portfolio_var` `portfolio_risk_check` `apply_phase4_filters` `dynamic_position_scale` | 缩减手数 |
| **4. 账户级** | 回撤 15% / 日亏 8% / 连亏 6 笔 | `risk_state_machine.py` `drawdown_guard.py` | 停机 + KillSwitch |

**KillSwitch**：自动触发，人工只能解除，**跨重启不洗白**。

止损/止盈由 `exit_plan()` 计算，受 regime、ATR、支撑阻力、跟踪止损等因子调制。

---

## ⚙️ 运行形态

| 组件 | 端口 | 说明 |
|---|---|---|
| **面板 + API** | **8741** | `four_dim_live_runner.py`，80 个 `/api/*` 端点，单端口 |
| **行情后端** | **8742** | `backend_tqsdk.py`（TqSdk 行情），独立服务 |
| **常驻** | — | launchd `com.a123.fourdim`，60s 评估周期 |

```bash
# 手动启动面板
python3 four_dim_live_runner.py --port 8741
```

**账户体系**（三套并行，无全局 paper/live 开关）：

| 文件 | 用途 |
|---|---|
| `account_state.json` | 真实持仓 |
| `paper_account.json` | 手动沙盒 |
| `pti` / `tri` | 模拟跟信号 / 三感参谋实盘跟踪 |

---

## 🏗️ 架构概览

```
fourd_run/
├── 🧠 策略层
│   ├── four_dim_strategy.py       # 核心引擎（内部代号，勿改文件名）
│   ├── strategy_layer.py          # 策略层管理
│   ├── sentiment_engine.py        # 情绪引擎
│   └── ga_*.py                    # 遗传算法优化系列
│
├── 🛡️ 风控层
│   ├── risk_state_machine.py      # 风控状态机（连亏/回撤/全平）
│   ├── risk_gate_utils.py         # 风险门禁
│   ├── drawdown_guard.py          # 回撤分档守卫
│   ├── price_protection.py        # 价格保护
│   ├── kelly_utils.py             # 凯利仓位
│   ├── corr_gate_utils.py         # 相关性闸门
│   └── gap_stop_utils.py          # 缺口止损
│
├── 🚀 运行层
│   ├── four_dim_live_runner.py    # 主运行器 + HTTP 面板（8741）
│   ├── backend_tqsdk.py           # TqSdk 行情后端（8742）
│   ├── minishare_live.py          # 迷你行情源
│   └── dir_utils.py               # 方向归一化唯一真源
│
├── 📊 分析层
│   ├── sr_analyzer.py             # 支撑阻力
│   ├── regime_hmm.py              # 市场状态 HMM
│   ├── gbm_garch.py               # 波动率模型
│   └── calibration.py             # 校准工具
│
├── 🔍 监控层
│   ├── consistency_watchdog.py    # 一致性监控
│   ├── live_health_check.py       # 实盘健康检查
│   ├── account_tracker.py         # 账户追踪
│   └── trade_journal.py           # 交易日志
│
├── 🔧 决策工具（tools_*.py）       # 见下方「策略改动铁律」
├── 🧪 tests/                      # 3187 项测试
└── ⚙️ .github/workflows/          # 12 个 CI Workflow
```

---

## 📈 品种覆盖

- 54 个品种定义，7 大板块
- `DISABLED_SYMBOLS` 11 个（au / i / eg / m / a / b / rr / RM / hc / MA / PR）→ **43 活跃（配置层）**
- 其中 40 个有足够本地日线数据可回测（2026-09-08 实测）
- 禁用依据是 `calibration_params.json` 的样本外 `mean_oos`

> 2026-09-08 已用 `tools_disabled_oos_recheck.py` 对 11 个禁用品种做同口径样本外复查，
> 结论为**全部维持禁用**（禁用品种 pooled +0.1050R vs 活跃 +0.4920R）。
> 详见 [`docs/禁用品种OOS复查_2026-09-08.md`](docs/禁用品种OOS复查_2026-09-08.md)。

---

## 🚀 快速开始

### 环境要求

- Python **3.10+**（本项目在 3.13 上跑测试）
- 推荐使用虚拟环境（venv / conda）

### 安装

```bash
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 常用命令

```bash
make help            # 查看全部命令
make smoke           # 冒烟测试（< 1 秒，pre-commit 同款）
make test            # 全部单元测试
make all             # 含属性/基准/性能测试
make quality         # lint + 格式检查
```

### 跑一次单品种回测

```python
from four_dim_strategy import DEFAULT_CONFIG, load_daily, walk_forward_backtest

r = walk_forward_backtest("cu", cfg=DEFAULT_CONFIG, df_in=load_daily("cu"))
print(r["trades"], r["expR"], r["win_rate"])
```

---

## 🧪 测试体系

**3187 项测试，全绿。**

```
┌─────────────────────────────────────────────────┐
│  性能测试 / 基准回归 / 属性测试（Hypothesis）    │
├─────────────────────────────────────────────────┤
│  集成测试（回测 / 管道 / 深度）                  │
├─────────────────────────────────────────────────┤
│  单元测试（90+ 模块）                            │
└─────────────────────────────────────────────────┘
```

```bash
make smoke           # 冒烟
make unit            # 单元
make integration     # 集成
make coverage        # 覆盖率
make flake           # 不稳定测试检测（重跑 3 次）
```

---

## 🔒 策略改动铁律（必读）

本项目**改策略/配置前必须遵守**。每一条都是踩过的坑：

1. **禁止用小窗口/单品种下结论**
   rb 全历史 4207 根 bar 仅 **12 笔**成交；`backtest_rb_tail500` 基线 **trades=1**。
   1–12 笔上的 expR 差异纯属噪声。

2. **任何策略行为改动，合入前必须跑 `tools_regime_ab.py`**
   全品种合并 A/B 台，输出 pooled expR + 逐品种胜负 + 符号检验。
   **p > 0.05 = 无证据 = 默认不改。**

3. **「某信号无预测力」≠「删掉它更好」**
   Layer0 审计只证明 regime 不预测后续收益，但 `regime_coef` 是长期调参的
   **触发灵敏度**而非预测性倾斜；一并拍平 → 成交数 1658→956（−42%），
   合并 expR +0.6140R→+0.2809R（**p=0.004 显著恶化**）。

4. **评估脚本必须显式构造双侧配置**
   不能靠「不打补丁就用模块默认」——曾因回滚后模块默认变回旧值，
   导致 V0=V1=V2=V3 全相同，A/B 静默失效。

5. **对照基线必须真的「无处理」**
   经典 bug：baseline 用 `DEFAULT_CONFIG`，而它自带被测特性 → 差值恒为 0 → 假结论。

6. **回撤类闸门不能用 R 空间无资本约束的曲线验证**
   会饱和到 495%，所有成交挤进最高档。

7. **评估口径必须与判死依据一致**
   禁用依据是 `mean_oos`（样本外），就不能拿全历史实测（含样本内）去比较。

### 决策工具

| 工具 | 用途 |
|---|---|
| `tools_regime_ab.py` | **全品种合并配置 A/B 台（改动必跑）** |
| `tools_ps_true_oos.py` | P-S 黑名单干净 OOS 三组对照 |
| `tools_disabled_oos_recheck.py` | 禁用品种解禁复查台 |
| `tools_oos_ps_revalidation.py` | P-S OOS 重验（已修基线污染 bug） |
| `tools_roll_guardian_paper.py` | 移仓护卫历史回测 |

---

## 🔧 开发工具

```bash
# 代码质量
make lint            # Ruff 全量 lint
make format          # 自动格式化
make typecheck       # Mypy

# 安全扫描
make secretscan      # 密钥泄露检测（gitleaks）
make bandit          # 代码安全扫描
make depscan         # 依赖漏洞扫描

# Git Hooks
make hooks           # 安装 pre-commit / pre-push
```

pre-commit：单元测试 + 格式检查　|　pre-push：回归测试 + 质量检查

---

## 🏭 CI/CD

12 个 GitHub Actions Workflow：`test` / `code-quality` / `security` / `codeql` /
`scorecard` / `benchmark` / `sbom` / `release-drafter` / `pr-automation` /
`nightly` / `stale` / `cleanup-branches`。

在 PR 中评论 `/benchmark` 可手动触发性能基准对比。

---

## ⚠️ 已知问题

| 问题 | 状态 |
|---|---|
| **`feature_flags.json` 半接线** — `drawdown_guard` / `kill_switch` 两个开关无人读取，UI 切换无效（模块本身硬生效） | 待修 |
| **`recovery_check` 自适应恢复形同虚设** — tail=250 窗口下 10/11 品种因 n<10 无法判定，且 `AUTO_RECOVER_SYMBOLS` 只有 `{"hc"}` | 待专项评估 |
| **`calibration_params.json` 值陈旧** — 与当前配置脱节，驱动 `/api/edge` 展示 | 待重跑（展示层） |
| **利润集中** — Top3 品种贡献约 42% 总利润 | 结构性风险 |

---

## 🤝 贡献指南

请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。**策略类改动请务必先读上方「策略改动铁律」**，
并附上 `tools_regime_ab.py` 的合并 A/B 结果。

```bash
git checkout -b feat/your-feature
make test && make quality
```

---

## ❓ 常见问题

**可以直接用于实盘交易吗？**
本项目是**顾问式决策系统**，不自动下单。信号仅供参考，实盘交易有风险，请充分验证后谨慎使用。

**为什么代码里到处是 `four_dim`？**
内部代号，见[命名约定](#-命名约定)。对外一律称「三感参谋」，代码层不改名。

**支持哪些数据源？**
TqSdk（`backend_tqsdk.py`）、AkShare（`akshare_live.py`）、Tushare（`tushare_live.py`）、
迷你行情源（`minishare_live.py`）。

**如何报告 Bug？**
通过 [GitHub Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 提交。

---

## 📄 许可证

MIT License，详见 [LICENSE](LICENSE)。
