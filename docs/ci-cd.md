# CI/CD 配置说明

## 工作流概览

| 工作流 | 触发时机 | 耗时 | 覆盖范围 |
|---|---|---|---|
| `test.yml` | PR + push main/master | ~2-5min | 单元测试 + 集成测试 + 覆盖率 |
| `code-quality.yml` | PR | ~1min | 语法 + 命名规范 + 发现一致性 |
| `nightly.yml` | 每日 03:00 + 手动 | ~10-15min | 全量测试 + 性能基准 + 不稳定检测 |
| `pr-automation.yml` | PR 事件 | <30s | 自动打 size 标签 |

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
- 根据变更行数自动打 size 标签：
  - XS: < 30 行
  - S: 30 - 100 行
  - M: 100 - 500 行
  - L: 500 - 2000 行
  - XL: > 2000 行

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

## 添加新 CI 检查

1. 在 `.github/workflows/` 下新建 `xxx.yml`
2. 参考现有工作流的结构
3. 确保有明确的触发条件和产物上传
4. 在本文件的"工作流概览"表格中添加一行

## 常见问题

**Q: CI 测试失败但本地通过？**
- 检查 Python 版本（CI 测 3.10/3.11/3.12）
- 检查是否有平台相关的代码
- 用 `--retry 2` 检测是否是不稳定测试

**Q: 性能测试 CI 上波动大？**
- 性能测试只在 nightly 运行，不阻塞 PR
- 看趋势不看单次结果
- 用 `_perf_baseline.json` 对比历史

**Q: 怎么跳过 CI？**
- 不建议跳过。如果必须，PR 描述加 `[skip ci]` 或在 commit message 中加
- 测试用例层面用 `@unittest.skip`
