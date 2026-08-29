# CI/CD 配置说明

本文档介绍项目的持续集成与持续部署（CI/CD）配置，包括各工作流的触发条件、检查内容和本地等效命令。

---

## 工作流概览

| 工作流 | 触发时机 | 耗时 | 覆盖范围 |
|---|---|---|---|
| `test.yml` | PR + push main/master | ~2-5min | 单元测试 + 集成测试 + 覆盖率 |
| `code-quality.yml` | PR | ~1min | 语法 + 命名规范 + 发现一致性 |
| `nightly.yml` | 每日 03:00 + 手动 | ~10-15min | 全量测试 + 性能基准 + 不稳定检测 |
| `pr-automation.yml` | PR 事件 | <30s | 自动打 size 标签 |

---

## 工作流详情

### 1. 测试工作流（test.yml）

**触发条件：**

- PR 到 main/master → 3 个 Python 版本 × 单元测试 + 集成测试
- push 到 main/master → 同上 + 覆盖率报告
- 手动触发 → 可选测试范围

**矩阵：**

- Python 3.10 / 3.11 / 3.12
- 测试范围：unit / integration

**产物：**

- JUnit XML 测试报告
- HTML 覆盖率报告（仅 main 分支）

### 2. 代码质量（code-quality.yml）

**检查项：**

- Python 语法编译检查（所有 .py 文件）
- 测试文件命名规范（`test_*.py`）
- 基准文件 JSON 完整性
- 测试发现一致性（`discover_tests.py`）

### 3. 夜间全量（nightly.yml）

**触发：** 每天北京时间 03:00

**内容：**

- 3 个 Python 版本全量测试（含集成/属性/基准）
- 性能基准测试
- 可选：不稳定测试检测（`--retry 2`）

**产物：**

- 完整测试结果
- 性能基准数据

### 4. PR 自动化（pr-automation.yml）

**自动功能：**

根据变更行数自动打 size 标签：

| 标签 | 变更行数 |
|---|---|
| XS | < 30 行 |
| S | 30 - 100 行 |
| M | 100 - 500 行 |
| L | 500 - 2000 行 |
| XL | > 2000 行 |

---

## 其他工作流

### 安全检查（security.yml / codeql.yml）

项目配置了多重安全扫描：

- **CodeQL** — 代码语义分析，检测潜在安全漏洞
- **Gitleaks** — 密钥泄露检测
- **Scorecard** — 开源项目安全健康度评分
- **Dependabot** — 依赖安全更新自动提交 PR

### 发布相关

- **release-drafter.yml** — 自动维护 draft release 日志
- **sbom.yml** — 发布时生成软件物料清单（SBOM）
- **benchmark.yml** — 性能基准对比

### 运维类

- **cleanup-branches.yml** — 自动清理已合并分支
- **stale.yml** — 标记长期无活动的 Issue/PR

---

## 本地等效命令

```bash
# PR 提交前检查（等效于 code-quality + smoke test）
make smoke
python scripts/discover_tests.py

# 等效于 CI test.yml unit
make test

# 等效于 nightly 全量
make all

# 等效于性能基准
make perf
```

---

## 添加新 CI 检查

1. 在 `.github/workflows/` 下新建 `xxx.yml`
2. 参考现有工作流的结构
3. 确保有明确的触发条件和产物上传
4. 在本文档的「工作流概览」表格中添加一行

---

## 常见问题

### CI 测试失败但本地通过？

- 检查 Python 版本（CI 测 3.10/3.11/3.12）
- 检查是否有平台相关的代码
- 用 `--retry 2` 检测是否是不稳定测试

### 性能测试 CI 上波动大？

- 性能测试只在 nightly 运行，不阻塞 PR
- 看趋势不看单次结果
- 用 `_perf_baseline.json` 对比历史

### 怎么跳过 CI？

- 不建议跳过。如果必须，PR 描述加 `[skip ci]` 或在 commit message 中加
- 测试用例层面用 `@unittest.skip`

### CI 中的覆盖率门禁是怎样的？

CI 会对比当前 PR 与 main 分支的覆盖率差异，不允许覆盖率下降。如果新增代码需要时间补充测试，可以在 PR 中说明并与维护者协商。
