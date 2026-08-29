# 测试体系总览

## 概述

四维策略（futures-orderflow）采用分层测试体系，确保从核心纯函数到完整 pipeline 的各级逻辑都经过充分验证。测试体系以 **纯函数优先** 为设计原则——尽量将核心逻辑提取为无副作用的纯函数，便于单元测试和快速迭代。

---

## 测试分层

### 第 1 层：单元测试（Unit Tests）

针对单个纯函数的测试，验证输入输出的正确性。

**特点**：
- 运行速度快（毫秒级）
- 覆盖边界条件和异常输入
- 不依赖外部数据或服务
- 可独立运行

**覆盖模块**：gap_stop、kelly_factor、price_protection、corr_gate、take_profit、signal_trigger、risk_gate、t_score、hidden_pivot 等。

### 第 2 层：集成测试（Integration Tests）

验证多个模块组合后的交互是否正确。

**特点**：
- 测试模块间的接口和数据流
- 覆盖 pipeline 串联逻辑
- 运行速度中等（秒级）

**覆盖模块**：pipeline、compute_strategy、sim_exit_5m、flow_aggregator 等。

### 第 3 层：属性测试（Property-based Tests）

使用 Hypothesis 进行基于属性的随机测试，发现边界情况。

**特点**：
- 自动生成大量测试用例
- 验证不变量（invariants）
- 发现手工用例遗漏的 corner case

### 第 4 层：回归测试与基准测试（Regression & Benchmark）

确保代码变更不破坏已有功能，性能不出现明显退化。

**特点**：
- 与基准输出对比
- 性能基线监控
- 覆盖完整策略链路

---

## 测试模块清单（Python）

以下是 `run_tests.py` 中注册的 Python 测试模块：

| 模块名 | 测试文件 | 说明 |
|--------|----------|------|
| `gap_stop` | `tests/test_gap_stop.py` | 缺口止损 |
| `kelly_factor` | `tests/test_kelly_factor.py` | Kelly 因子 |
| `price_protection` | `tests/test_price_protection.py` | 价格保护 |
| `corr_gate` | `tests/test_corr_gate.py` | 相关性闸门 |
| `take_profit` | `tests/test_take_profit.py` | 止盈止损 |
| `signal_trigger` | `tests/test_signal_trigger.py` | 信号触发 |
| `risk_gate` | `tests/test_risk_gate.py` | 风控闸门 |
| `regime` | `tests/test_regime.py` | Regime 判断 |
| `t_score` | `tests/test_t_score.py` | T 分数合成 |
| `sr_analyzer` | `tests/test_sr_analyzer.py` | 支撑阻力分析 |
| `bias_and_slip` | `tests/test_bias_and_slip.py` | 偏置与滑点 |
| `params` | `tests/test_params.py` | 参数系统 |
| `strategies` | `tests/test_strategies.py` | 策略基类 |
| `config` | `tests/test_config.py` | 配置管理 |
| `weights` | `tests/test_weights.py` | 权重系统 |
| `flow_aggregator` | `tests/test_flow_aggregator.py` | 流量聚合 |
| `compute_strategy` | `tests/test_compute_strategy.py` | 策略计算 |
| `pipeline` | `tests/test_pipeline.py` | Pipeline 串联 |
| `risk_exit_main` | `tests/test_risk_exit_main.py` | 风控出场主逻辑 |
| `subfactors_buildsignal` | `tests/test_subfactors_buildsignal.py` | 子因子构建信号 |
| `sim_exit_5m` | `tests/test_sim_exit_5m.py` | 5 分钟出场模拟 |
| `metrics_utils` | `tests/test_metrics_utils.py` | 指标工具 |
| `risk_lock_wf_gate` | `tests/test_risk_lock_wf_gate.py` | 风险锁定 WF 闸门 |
| `ema_robust_gate` | `tests/test_ema_robust_gate.py` | EMA 稳健性闸门 |
| `risk_state_machine` | `tests/test_risk_state_machine.py` | 风险状态机 |
| `util_functions` | `tests/test_util_functions.py` | 工具函数 |
| `risk_sm_class` | `tests/test_risk_sm_class.py` | 风险状态机类 |
| `montecarlo` | `tests/test_montecarlo.py` | 蒙特卡洛 |
| `discipline_utils` | `tests/test_discipline_utils.py` | 纪律工具 |
| `trade_journal_utils` | `tests/test_trade_journal_utils.py` | 交易日志工具 |
| `gbm_garch` | `tests/test_gbm_garch.py` | GBM-GARCH 模型 |
| `technical_analysis` | `tests/test_technical_analysis.py` | 技术分析 |
| `sentiment_engine` | `tests/test_sentiment_engine.py` | 情绪引擎 |
| `info_screener` | `tests/test_info_screener.py` | 信息筛选器 |
| `account_tracker_utils` | `tests/test_account_tracker_utils.py` | 账户跟踪工具 |
| `anomaly_calibration` | `tests/test_anomaly_calibration.py` | 异动校准 |
| `long_hu_bang` | `tests/test_long_hu_bang.py` | 龙虎榜 |
| `hidden_pivot` | `tests/test_hidden_pivot.py` | 隐秘枢轴 |
| `regime_hmm` | `tests/test_regime_hmm.py` | HMM Regime |

---

## 测试运行方式

### 快速运行

```bash
# 全部单元测试
python run_tests.py

# 只跑某个模块
python run_tests.py gap_stop

# 冒烟测试（<10s，核心功能快速验证）
python run_tests.py smoke
```

### 分类运行

```bash
python run_tests.py unit         # 只跑单元测试
python run_tests.py integration  # 只跑集成测试
python run_tests.py advanced     # 属性+基准+性能
python run_tests.py all          # 全部（含性能测试）
```

### 常用选项

```bash
python run_tests.py -v               # 详细输出
python run_tests.py -f               # 快速失败（第一个失败就停止）
python run_tests.py -c               # 生成覆盖率报告
python run_tests.py -r               # 随机测试顺序
python run_tests.py --slow 500       # 标记慢测试
python run_tests.py --junit report.xml  # JUnit XML 报告
python run_tests.py --retry 3        # 失败重跑
python run_tests.py --list           # 列出所有测试模块
python run_tests.py --py-only        # 只跑 Python 测试
python run_tests.py --js-only        # 只跑 JS 测试
```

---

## 测试设计原则

### 1. 纯函数优先

核心计算逻辑尽量提取为纯函数，避免副作用，便于测试。

### 2. 历史 bug 回归覆盖

每个修复的 bug 都要有对应的测试用例，防止回归。

### 3. 边界条件全覆盖

- 零值、负值、极值
- None、空列表、类型错误
- 边界值（等于阈值、刚好触发等）

### 4. 测试与实现分离

测试文件集中在 `tests/` 目录，与源码分开。

### 5. 可独立运行

每个测试模块都可以单独运行，不依赖其他测试模块的状态。

---

## 相关文档

- [测试框架说明](test-framework.md) — pytest、hypothesis、运行方式详解
- [覆盖率说明](coverage.md) — coverage.py、Codecov、.coveragerc 配置
- [回归测试与基准测试](regression.md) — 回归测试策略、基准测试方法
