# 提交规范

本项目遵循 **Conventional Commits** 提交规范，配合 Git 钩子自动检查，确保提交历史清晰可读。

---

## 快速上手

### 提交格式

```
<type>(<scope>): <subject>
```

- **type**：提交类型（必填）
- **scope**：影响范围（可选）
- **subject**：简短描述（必填，中文/英文均可，≤ 72 字符）

### 类型说明

| type | 说明 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat(strategy): 新增四维策略止损逻辑` |
| `fix` | Bug 修复 | `fix(risk): 修复风控模块浮盈计算错误` |
| `perf` | 性能优化 | `perf(orderbook): 优化订单簿计算速度` |
| `refactor` | 代码重构（非功能、非修复） | `refactor(utils): 抽取通用工具函数` |
| `style` | 代码风格/格式（不影响逻辑） | `style: ruff format 全量格式化` |
| `docs` | 文档更新 | `docs(readme): 更新安装说明` |
| `test` | 测试补充/修改 | `test(strategy): 补充止损逻辑单元测试` |
| `ci` | CI/CD 配置变更 | `ci(coverage): 添加覆盖率门禁` |
| `build` | 构建系统/依赖变更 | `build: 升级 numpy 到 2.0` |
| `chore` | 杂项（工具、配置、其他） | `chore(git): 更新 .gitignore` |
| `revert` | 回滚提交 | `revert: 回滚止损逻辑变更` |

### 更多示例

```bash
# 带 scope
git commit -m "feat(strategy): 新增四维策略止损逻辑"

# 不带 scope
git commit -m "fix: 修复风控模块浮盈计算错误"

# 破坏性变更（加 ! 标记）
git commit -m "feat(api)!: 重构策略接口，不兼容旧版"

# CI 配置变更
git commit -m "ci(coverage): 添加覆盖率门禁"
```

---

## 破坏性变更

如果提交包含破坏性变更（Breaking Change），在 type 后加 `!`，并在 body 中说明：

```
feat!(api): 修改风险门禁接口

BREAKING CHANGE: RiskGate.check() 返回值从 bool 改为 GateResult 对象
迁移方式：将 if gate.check() 改为 if gate.check().passed
```

---

## 自动检查机制

项目内置了 **3 层提交检查**，通过 Git 钩子（Hook）自动运行：

### 1. pre-commit — 格式化检查 + 冒烟测试

**触发时机**：`git commit` 提交前

**检查内容**：

- 暂存的 Python 文件是否符合 Ruff 格式规范
- 冒烟测试（快速验证，< 3s）

**未通过怎么办**：

```bash
# 自动格式化
make format

# 或只格式化单个文件
ruff format path/to/file.py

# 重新暂存后提交
git add path/to/file.py
git commit -m "..."
```

### 2. commit-msg — 提交信息规范检查

**触发时机**：提交信息编写完成后

**检查内容**：提交信息是否符合 Conventional Commits 规范

**未通过怎么办**：

- 按规范修改提交信息格式
- 参考上方「类型说明」选择合适的 type

### 3. pre-push — 全量测试

**触发时机**：`git push` 推送前

**检查内容**：全量单元测试 + 集成测试

**未通过怎么办**：

- 修复失败的测试
- 确保本地 `make test` 通过后再推送

### 跳过检查（不建议）

```bash
# 跳过所有 pre-commit 检查
git commit --no-verify

# 跳过所有 pre-push 检查
git push --no-verify
```

!!! warning
    跳过检查可能导致不规范的代码或提交进入仓库，请谨慎使用。CI 中仍会运行完整检查。

---

## 安装钩子

```bash
# 安装所有钩子（推荐）
make hooks

# 或单独安装
./scripts/install_hooks.sh install pre-commit
./scripts/install_hooks.sh install commit-msg
./scripts/install_hooks.sh install pre-push
```

### 查看钩子状态

```bash
./scripts/install_hooks.sh status
```

### 卸载钩子

```bash
./scripts/install_hooks.sh uninstall
```

---

## CI 中的检查

除了本地钩子，GitHub Actions 中也有对应的检查：

| 检查项 | Workflow | 说明 |
|--------|----------|------|
| PR 标题规范 | PR Automation | 确保 PR 标题符合规范（squash merge 后即 commit message） |
| PR 模板完整性 | PR Automation | 检查 PR 描述是否按模板填写 |
| 代码格式 | Code Quality | `ruff format --check` 全量检查 |
| Lint 检查 | Code Quality | `ruff check` 阻塞级问题检查 |
| 单元测试 | Test | 多版本 Python 全量测试 |
| 覆盖率门禁 | Test | 覆盖率不退化检查 |

---

## 最佳实践

1. **小步提交**：每个 commit 只做一件事，方便回滚和 Code Review
2. **描述清晰**：subject 用动词开头，说明「做了什么」而非「怎么做的」
3. **先格式化再提交**：养成 `make format` 的习惯，避免被钩子拦截
4. **本地跑测试**：提交前至少跑过冒烟测试，push 前跑全量测试
5. **合理使用 type**：
    - 改了功能 → `feat` / `fix`
    - 改了性能但不改功能 → `perf`
    - 改了代码结构但不改行为 → `refactor`
    - 只改格式/空格/排序 → `style`
    - 只改文档 → `docs`
    - 只改测试 → `test`
    - 改 CI/工作流 → `ci`
    - 改依赖/构建脚本 → `build`
    - 其他杂项 → `chore`

---

## 相关资源

- [Conventional Commits 官方文档（中文）](https://www.conventionalcommits.org/zh-cn/)
- `make help` — 查看所有可用命令
- `make hooks` — 安装 Git 钩子
