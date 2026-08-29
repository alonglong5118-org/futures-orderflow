# 贡献指南

感谢你有兴趣为 futures-orderflow 做贡献！本文档介绍了参与项目贡献的方式、开发流程和规范。

无论你是提交 Bug 报告、提出新功能建议，还是直接贡献代码，我们都非常欢迎。

---

## 📋 目录

- [行为准则](#行为准则)
- [可以贡献什么](#可以贡献什么)
- [开发环境搭建](#开发环境搭建)
- [开发工作流](#开发工作流)
- [代码规范](#代码规范)
- [测试规范](#测试规范)
- [提交规范](#提交规范)
- [PR 规范](#pr-规范)
- [发布流程](#发布流程)
- [问题反馈](#问题反馈)
- [常见问题](#常见问题)

---

## 行为准则

参与本项目请遵守以下行为准则：

- **友善耐心** — 尊重不同的经验水平和背景
- **包容开放** — 欢迎不同的观点和想法
- **专业负责** — 对自己的言论和行为负责
- **聚焦技术** — 讨论聚焦于技术问题，避免人身攻击

不适行为可以通过安全策略中列出的方式联系维护者。

---

## 可以贡献什么

### 代码贡献
- 🐛 Bug 修复
- ✨ 新功能 / 新策略
- ⚡ 性能优化
- 🧪 测试用例补充
- 📝 文档完善
- ♻️ 代码重构
- 🔒 安全改进

### 非代码贡献
- 📖 文档翻译 / 改进
- 🎨 设计建议
- 💡 功能建议
- 🐛 Bug 报告
- 📣 社区推广

---

## 开发环境搭建

### 前置要求

- Python **3.10** 或更高版本
- Git
- （可选）make — 用于快捷命令
- （可选）gitleaks — 密钥泄露检测
- （可选）actionlint — GitHub Actions 语法检查

### 步骤

```bash
# 1. Fork 仓库（在 GitHub 网页上操作）

# 2. 克隆你的 fork
git clone https://github.com/你的用户名/futures-orderflow.git
cd futures-orderflow

# 3. 添加上游仓库（用于同步更新）
git remote add upstream https://github.com/alonglong5118-org/futures-orderflow.git

# 4. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 5. 安装开发依赖
make deps
# 或手动：pip install -r requirements-dev.txt

# 6. 安装 Git Hooks（推荐）
make hooks

# 7. 验证环境
make smoke   # 冒烟测试，确认环境正常
make quality # 质量检查，确认工具链正常
```

### 同步上游更新

```bash
# 同步 main 分支
git checkout main
git fetch upstream
git merge upstream/main
git push origin main
```

---

## 开发工作流

### 标准流程

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  创建分支    │ →  │  开发代码    │ →  │  本地测试    │
└─────────────┘    └─────────────┘    └──────┬──────┘
                                              │
                                              ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  提交 PR    │ ←  │  提交代码    │ ←  │  质量检查    │
└─────────────┘    └─────────────┘    └─────────────┘
       │
       ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  CI 检查     │ →  │  Code Review │ →  │  合并       │
└─────────────┘    └─────────────┘    └─────────────┘
```

### 详细步骤

1. **创建功能分支**
   ```bash
   git checkout main
   git pull upstream main
   git checkout -b feat/你的功能名
   ```

2. **开发代码**
   - 遵循代码规范（见下文）
   - 适当添加注释
   - 新增功能需补充测试

3. **本地测试**
   ```bash
   make test           # 运行测试
   make quality        # 质量检查
   make coverage       # 覆盖率检查（确保未下降）
   ```

4. **提交代码**
   - 遵循提交规范（见下文）
   - 一个 PR 尽量只做一件事

5. **推送并创建 PR**
   - 填写 PR 模板
   - 关联相关 Issue

---

## 代码规范

### 编码风格

项目使用 [Ruff](https://github.com/astral-sh/ruff) 进行代码风格管理：

```bash
make format          # 自动格式化
make format-check    # 格式检查（不修改）
make lint            # 全量 lint
make lint-critical   # 阻塞级 lint（CI 同款）
```

### 核心规则

| 项目 | 规范 |
|---|---|
| 行宽 | 120 字符 |
| 引号 | 双引号 `"` |
| 缩进 | 4 空格 |
| 命名 | `snake_case`（函数/变量）/ `PascalCase`（类） |
| 导入排序 | Ruff I 规则自动管理 |
| Python 版本 | 3.10+ 语法兼容 |

### 类型注解

- 鼓励使用类型注解，尤其是公共 API
- 核心模块的函数建议添加完整的类型注解
- 测试文件不强制要求类型注解

```python
# ✅ 好的示例
def calculate_kelly(win_rate: float, payout_ratio: float) -> float:
    """计算凯利公式仓位比例。"""
    ...
```

### 文档字符串

公共函数和类建议添加 docstring，使用 Google 风格或 reStructuredText 风格均可：

```python
def risk_gate(position: Position, market_data: MarketData) -> bool:
    """风险门禁检查。

    根据当前仓位和市场数据，判断是否允许开新仓。

    Args:
        position: 当前持仓对象
        market_data: 最新市场数据

    Returns:
        True 表示允许开仓，False 表示被风险门禁拦截

    Raises:
        ValueError: 当市场数据不完整时
    """
    ...
```

---

## 测试规范

### 测试框架

- 测试框架：pytest
- 属性测试：Hypothesis
- 性能基准：pytest-benchmark / 自定义

### 测试目录结构

```
tests/
├── test_*.py               # 单元测试（按模块划分）
├── test_integration_*.py   # 集成测试
├── test_performance.py     # 性能基准测试
├── test_baseline_*.py      # 基准回归测试
└── test_property_*.py      # 属性测试（Hypothesis）
```

### 编写测试

```python
# 测试文件命名：test_模块名.py
# 测试函数命名：test_功能_场景

def test_risk_gate_blocks_high_risk():
    """高风险场景下风险门禁应拦截。"""
    gate = RiskGate(max_position=10)
    result = gate.check(position=Position(size=20))
    assert result.passed is False
    assert "max_position" in result.reasons
```

### 运行测试

```bash
# 全部测试
make test

# 单个测试文件
python -m pytest tests/test_risk_gate.py -v

# 单个测试用例
python -m pytest tests/test_risk.py::test_risk_gate_blocks_high_risk -v

# 失败时进入调试
python -m pytest tests/test_risk_gate.py --pdb

# 覆盖率
make coverage
```

### 覆盖率要求

- 新增代码应尽量覆盖测试
- 不允许因为你的修改导致整体覆盖率下降
- 使用 `make coverage-check` 检查覆盖率是否达标

---

## 提交规范

### Conventional Commits

项目使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 规范。

**格式**：
```
<type>(<scope>): <description>

<body>

<footer>
```

### Type 类型

| type | 说明 | 示例 |
|---|---|---|
| `feat` | 新功能 | `feat(strategy): 新增四维策略第五维度` |
| `fix` | Bug 修复 | `fix(risk): 修复风险门禁计算错误` |
| `perf` | 性能优化 | `perf(sr): 优化支撑阻力检测算法` |
| `refactor` | 代码重构 | `refactor(utils): 提取公共校准函数` |
| `test` | 测试补充 | `test(kelly): 增加凯利公式边界测试` |
| `docs` | 文档更新 | `docs(readme): 完善快速开始指南` |
| `ci` | CI/CD 配置 | `ci(benchmark): 添加性能基准对比` |
| `chore` | 杂项 | `chore(deps): 更新依赖版本` |
| `style` | 代码风格 | `style(format): 统一格式化策略文件` |
| `build` | 构建系统 | `build(makefile): 新增 benchmark 目标` |
| `revert` | 回滚 | `revert: 回滚 "feat(strategy): ..."` |

### 破坏性变更

如果提交包含破坏性变更（Breaking Change），在 type 后加 `!`，并在 body 中说明：

```
feat!(api): 修改风险门禁接口

BREAKING CHANGE: RiskGate.check() 返回值从 bool 改为 GateResult 对象
迁移方式：将 if gate.check() 改为 if gate.check().passed
```

### 提交信息要求

- 标题不超过 **72** 字符
- 使用中文描述（与项目语言一致）
- 标题用动词开头，简洁明了
- Body 可选，用于解释复杂变更的原因和细节

---

## PR 规范

### PR 标题

PR 标题遵循 Conventional Commits 规范（同提交规范）。CI 会自动检查。

### PR 描述

创建 PR 时请填写模板中的内容，包括：

- **变更类型** — feat / fix / refactor / test / docs / ci / chore
- **变更描述** — 做了什么，为什么这样做
- **关联 Issue** — `Closes #123` / `Related to #456`
- **测试说明** — 如何验证改动的正确性
- **截图/日志** — （可选）可视化的改动效果

### 分支命名

推荐使用以下前缀：

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat/` | 新功能 | `feat/fifth-dimension` |
| `fix/` | Bug 修复 | `fix/gate-calculation` |
| `perf/` | 性能优化 | `perf/sr-detection` |
| `refactor/` | 重构 | `refactor/calib-utils` |
| `test/` | 测试 | `test/kelly-edge-cases` |
| `docs/` | 文档 | `docs/contributing-guide` |
| `ci/` | CI/CD | `ci/benchmark-compare` |

### Code Review

- **耐心等待** — 维护者可能需要时间 review
- **积极响应** — 及时回复 review 意见
- **小步提交** — 大的改动尽量拆分成多个小 PR
- **尊重建议** — 对 review 意见有不同看法可以友好讨论

### 合并方式

- 所有 PR 需要至少 **1 位维护者 approve**
- 所有 **CI 检查必须通过**
- 使用 **Squash and merge** 合并（保持 main 分支历史整洁）
- 合并后会自动更新 draft release 日志

---

## 发布流程

项目使用 Release Drafter 自动管理发布：

1. PR 合并时，Release Drafter 自动更新 draft release
2. draft release 随时可在 GitHub Releases 页面预览
3. 版本号根据 PR type 自动递增（feat→minor, fix→patch 等）
4. 准备好发布时，打一个 `vX.Y.Z` tag 即可自动发布：

```bash
# 打 tag 并推送（自动触发发布流程）
git tag v1.0.0
git push origin v1.0.0
```

5. 发布时会自动生成 SBOM + 漏洞扫描报告并附加到 Release Assets

---

## 问题反馈

### Bug 报告

发现 Bug 时，请通过 GitHub Issue 反馈，并提供以下信息：

- **问题描述** — 清楚描述问题是什么
- **复现步骤** — 如何复现这个问题
- **预期行为** — 你期望的正确行为
- **实际行为** — 实际发生了什么
- **环境信息** — Python 版本、操作系统、依赖版本
- **最小复现** — 如果可能，提供最小可复现代码

### 功能建议

欢迎提出新功能建议，请说明：

- **功能描述** — 你想要什么功能
- **使用场景** — 在什么场景下需要这个功能
- **实现思路** — 你有什么实现想法（可选）

---

## 常见问题

### Q: 我是新手，可以贡献什么？

A: 非常欢迎！可以从以下方面入手：
- 补充测试用例（标注 "good first issue" 的 Issue）
- 完善文档
- 修复简单的 Bug
- 代码风格优化

### Q: 我的 PR 一直没人 review 怎么办？

A: 可以在 PR 中 @ 维护者，或者在讨论区礼貌地提醒。维护者可能在忙，请耐心等待。

### Q: 可以加我自己的策略吗？

A: 当然可以！请确保：
1. 策略有清晰的逻辑说明
2. 有配套的测试用例
3. 有回测结果验证
4. 遵循项目的代码风格

### Q: 如何运行完整的 CI 检查？

A: 本地可以运行以下命令模拟 CI 检查：

```bash
make test            # 测试
make quality         # 质量检查
make security        # 安全检查
make coverage-check  # 覆盖率检查
```

如果 CI 中有个别检查本地没有工具（如 CodeQL），提交 PR 后会自动运行。

---

## 致谢

感谢每一位贡献者的努力！你的每一个贡献都让项目变得更好。

---

> 本文档最后更新于 2026 年 8 月
> 如有疑问，欢迎通过 Issue 或 Discussion 交流
