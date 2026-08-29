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
- [新手入门：第一次贡献指南](#-新手入门第一次贡献指南)
- [Code Review 礼仪](#-code-review-礼仪)
- [故障排查](#-故障排查)
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

## 🌱 新手入门：第一次贡献指南

如果你是第一次为开源项目做贡献，别担心！本节带你一步步完成你的第一次贡献。

### 第一步：找一个适合的 Issue

1. 浏览 [Issues 列表](https://github.com/alonglong5118-org/futures-orderflow/issues)
2. 寻找标注了 **`good first issue`** 或 **`help wanted`** 的 Issue
3. 在 Issue 下评论 "我想做这个"，让大家知道你在处理
4. 如果不确定，随时提问，维护者会耐心解答

> 💡 即使最终没有提交代码，参与讨论本身也是宝贵的贡献！

### 第二步：开发你的改动

```bash
# 1. 同步上游（确保从最新代码开始）
git checkout main
git pull upstream main

# 2. 创建功能分支
git checkout -b fix/issue-123-some-bug

# 3. 编写代码
# ... 你的改动 ...

# 4. 运行测试（确保没有破坏任何东西）
make test
make quality

# 5. 提交
git add .
git commit -m "fix(scope): 修复 xxx 问题

Closes #123"
```

### 第三步：提交 PR

1. 推送你的分支：`git push origin fix/issue-123-some-bug`
2. 点击 GitHub 上的 "Compare & pull request" 按钮
3. 填写 PR 描述模板
4. 提交 PR，等待 CI 检查和 Review

### 第四步：响应 Code Review

1. 维护者可能会提出修改建议
2. 在同一分支上继续提交修改，然后推送
3. 回复每条 review 意见（已修改 / 说明原因）
4. PR 会自动更新，无需重新创建

> 🎉 恭喜！你的 PR 合并后，你就是这个项目的贡献者了！

---

## 🤝 Code Review 礼仪

Code Review 是提升代码质量和知识共享的重要环节。以下是一些建议：

### 作者指南

- **主动说明** — PR 描述中清楚说明「做了什么」和「为什么这样做」
- **拆小 PR** — 大改动尽量拆成多个小 PR，方便 review
- **虚心接受** — Review 意见是对代码不对人，不要有防御心理
- **及时响应** — 尽量在 2-3 天内回复 review 意见
- **主动标记** — 改好了可以在评论里说 "已修改，请再看"
- **提问而非辩解** — 不同意某条意见时，先问清楚原因，再讨论

### Reviewer 指南

- **尊重作者** — 评论代码，不评论人
- **明确区分** — 明确标注哪些是「必须改」哪些是「建议改」
  - 🔴 **必须修改**（阻塞合并的问题）
  - 🟡 **建议修改**（可以讨论，不强制）
  - 💡 **想法建议**（仅供参考）
- **给出理由** — 提建议时说明为什么这样更好
- **提供替代方案** — 不只是说不好，也给出更好的写法
- **肯定优点** — 看到好的代码不要吝啬表扬
- **及时 Review** — 尽量在 2-3 天内完成 review

### Review 中的常见讨论点

| 主题 | 优先级 | 说明 |
|---|---|---|
| 正确性 Bug | 🔴 必须改 | 逻辑错误必须修复 |
| 性能问题 | 🔴/🟡 | 显著退化必须改，微小优化是建议 |
| 测试覆盖 | 🔴 必须改 | 新增代码应有测试 |
| 命名风格 | 🟡 建议 | 不符合规范时建议修改 |
| 代码结构 | 🟡 建议 | 可读性和可维护性 |
| 注释文档 | 💡 建议 | 公共 API 应该有 docstring |
| 个人偏好 | 💡 想法 | 仅供讨论，不阻塞合并 |

---

## 🔧 故障排查

### 依赖安装失败

**现象**：`pip install -r requirements.txt` 报错

**解决方法**：
```bash
# 1. 升级 pip
python -m pip install --upgrade pip

# 2. 使用国内镜像源（中国大陆用户推荐）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 如果是某个特定包失败，单独安装调试
pip install 包名 -v
```

### 测试运行失败

**现象**：运行 `make test` 有测试失败

**排查步骤**：
```bash
# 1. 单独运行失败的测试，看详细错误
python -m pytest tests/test_xxx.py::test_xxx -v --tb=long

# 2. 进入调试模式（失败时自动启动 pdb）
python -m pytest tests/test_xxx.py::test_xxx --pdb

# 3. 确认是你的改动导致的吗？
git stash   # 暂存你的改动
make test   # 跑一下原始代码
git stash pop  # 恢复你的改动
```

### 格式化检查失败

**现象**：`make format-check` 报错

**解决方法**：
```bash
# 自动格式化
make format

# 然后重新提交
git add .
git commit --amend --no-edit
```

### Lint 检查失败

**现象**：`make lint-critical` 有错误

**常见问题与解决**：
| 错误码 | 含义 | 解决方法 |
|---|---|---|
| F401 | 导入未使用 | 删除未使用的导入，或加 `# noqa: F401` |
| F821 | 未定义的名字 | 检查是否拼写错误或缺少导入 |
| I001 | 导入顺序不对 | 运行 `make format` 自动修复 |
| E722 | 裸 except | 改为捕获具体异常类型 |

### Git Hooks 不生效

**现象**：安装了 hooks 但提交时没有触发

**排查**：
```bash
# 检查 hooks 是否安装
ls -la .git/hooks/

# 重新安装
make hooks

# 手动测试 hook
bash .git/hooks/pre-commit
```

### CI 失败但本地通过

**现象**：本地测试通过，GitHub Actions 上失败

**可能原因**：
1. **平台差异** — CI 在 Ubuntu 上运行，本地可能是 macOS
2. **Python 版本** — CI 跑多版本，本地只跑了一个版本
3. **测试顺序** — 测试之间有隐藏的依赖关系
4. **环境变量** — CI 环境与本地不同

**排查方法**：
- 仔细阅读 CI 日志中的错误信息
- 在本地用同样的 Python 版本测试
- 用 `make flake` 检测不稳定测试
- 检查 CI 配置的环境变量

### 其他问题

如果以上都解决不了你的问题：
1. 搜索 [Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 看是否有类似问题
2. 在 [Discussions](https://github.com/alonglong5118-org/futures-orderflow/discussions) 中提问
3. 提交新的 Issue 并附上详细的错误信息

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
