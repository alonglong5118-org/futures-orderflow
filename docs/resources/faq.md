# 常见问题

本文档整理了 futures-orderflow 项目的常见问题与解答。如果你在这里找不到答案，欢迎通过 [GitHub Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 或 [Discussions](https://github.com/alonglong5118-org/futures-orderflow/discussions) 提问。

---

## 安装与环境

### 如何安装项目？

请参考 [安装配置指南](../getting-started/installation.md)。简而言之：

```bash
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 支持哪些 Python 版本？

项目支持 Python **3.10 及以上**版本，CI 中测试 3.10 / 3.11 / 3.12 三个版本。

### 依赖安装失败怎么办？

```bash
# 1. 升级 pip
python -m pip install --upgrade pip

# 2. 使用国内镜像源（中国大陆用户推荐）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

如果是特定包安装失败，尝试单独安装并查看详细日志：`pip install 包名 -v`。

---

## 开发与贡献

### 我是新手，可以贡献什么？

非常欢迎！可以从以下方面入手：

- 补充测试用例（关注标注 `good first issue` 的 Issue）
- 完善文档（翻译、纠错、补充说明）
- 修复简单的 Bug
- 代码风格优化

更多信息请参阅 [贡献指南](../dev-guide/contributing.md)。

### 如何设置开发环境？

```bash
pip install -r requirements-dev.txt
make hooks    # 安装 Git 钩子
make smoke    # 冒烟测试，验证环境
```

完整步骤请参阅 [贡献指南 - 开发环境搭建](../dev-guide/contributing.md)。

### 提交代码有什么规范？

项目遵循 **Conventional Commits** 规范。提交格式为：

```
<type>(<scope>): <subject>
```

常用 type 包括 `feat`、`fix`、`perf`、`refactor`、`test`、`docs`、`ci`、`chore` 等。

详细说明请参阅 [提交规范](../dev-guide/commit-conventions.md)。

### 我的 PR 一直没人 review 怎么办？

可以在 PR 中 @ 维护者，或者在讨论区礼貌地提醒。维护者可能在忙，请耐心等待。

---

## 策略与功能

### 可以添加自己的策略吗？

当然可以！请确保：

1. 策略有清晰的逻辑说明
2. 有配套的测试用例
3. 有回测结果验证
4. 遵循项目的代码风格

提交 PR 时附上回测报告和策略说明，维护者会进行 review。

### 四维策略的四个维度是什么？

四维策略基于以下四个维度综合研判：

1. **成交量维度（F）** — 基于订单流数据的量能分析
2. **价格维度（T）** — 技术面触发与支撑阻力分析
3. **资金维度（C）** — 持仓量与资金流向确认
4. **时间维度** — 时间衰减与周期分析

更多详情请参阅 [四维策略架构](../architecture/four-dim-strategy.md)。

### 系统支持哪些经纪商？

项目通过 TqSdk 后端接入交易，支持大部分国内期货经纪商。你也可以自行扩展其他交易后端。

---

## 测试与质量

### 如何运行测试？

```bash
# 全部测试
make test

# 单个测试文件
python -m pytest tests/test_risk_gate.py -v

# 覆盖率报告
make coverage
```

更多信息请参阅 [测试体系](../testing/overview.md)。

### 如何运行完整的 CI 检查？

本地可以运行以下命令模拟 CI 检查：

```bash
make test            # 测试
make quality         # 质量检查
make security        # 安全检查
make coverage-check  # 覆盖率检查
```

如果 CI 中有个别检查本地没有工具（如 CodeQL），提交 PR 后会自动运行。

### CI 失败但本地测试通过怎么办？

可能的原因：

1. **Python 版本差异** — CI 测试 3.10/3.11/3.12，本地可能只测了一个版本
2. **平台差异** — CI 在 Ubuntu 上运行，本地可能是 macOS/Windows
3. **测试顺序** — 测试之间可能有隐藏的依赖关系
4. **环境变量** — CI 环境与本地不同

排查方法：仔细阅读 CI 日志中的错误信息，在本地用同样的 Python 版本测试。

---

## 安全与风险

### 实盘交易安全吗？

本项目是策略研究和回测框架，实盘功能需要自行配置经纪商接口和参数。**实盘交易有风险，请充分回测和验证后谨慎使用**。

建议：

- 先用模拟盘验证策略
- 从小资金开始实盘
- 严格执行风控规则
- 定期检查系统运行状态

### 如何报告安全漏洞？

请**不要**在公开的 Issue 中报告安全漏洞，而是通过以下方式私下联系：

- 通过 GitHub [发起安全咨询](https://github.com/alonglong5118-org/futures-orderflow/security/advisories/new)（推荐）
- 发送邮件至安全联系人邮箱

更多信息请参阅 [安全策略](security.md)。

### API 密钥怎么保存才安全？

**绝不要**将 API 密钥、交易密码等敏感信息提交到代码仓库。建议：

- 使用环境变量存储敏感信息
- 使用 `.env` 文件（已加入 `.gitignore`）
- 为交易 API 密钥配置最小权限
- 定期轮换密钥

---

## 文档与支持

### 文档在哪里？

项目文档站点包含以下内容：

- **快速开始** — 安装与上手指南
- **架构设计** — 系统架构与核心模块设计
- **核心模块** — 各功能模块的详细说明
- **测试与质量** — 测试体系与质量保障
- **开发指南** — 贡献流程与规范
- **API 参考** — 接口文档
- **资源** — FAQ、路线图、安全策略等

### 遇到问题怎么求助？

1. 先检查本文档是否有相关解答
2. 搜索 [Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 看是否有类似问题
3. 在 [Discussions](https://github.com/alonglong5118-org/futures-orderflow/discussions) 中提问
4. 提交新的 Issue 并附上详细的错误信息

提问时请尽量提供：问题描述、复现步骤、环境信息、错误日志等。
