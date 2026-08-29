# API 参考

本页为 futures-orderflow 各核心模块的 API 索引，按功能分类列出主要类与函数。详细的参数说明与使用示例请参阅各模块文档及源码。

---

## 策略层

### four_dim_strategy — 四维策略核心

四维策略主模块，基于成交量、价格、资金、时间四个维度综合研判。

| 类/函数 | 说明 |
|---|---|
| `FourDimStrategy` | 四维策略主类 |
| `compute_T()` | 技术面触发计算（T 维度） |
| `score_C()` | 资金面确认（C 维度） |
| `compute_strategy()` | 综合策略计算入口 |

**相关文档**：[四维策略架构](../architecture/four-dim-strategy.md)

### strategy_layer — 策略层管理

多策略管理层，负责策略调度、状态管理与信号汇总。

| 类/函数 | 说明 |
|---|---|
| `StrategyLayer` | 策略层管理类 |
| `register_strategy()` | 注册策略 |
| `generate_signals()` | 生成所有策略信号 |

### sentiment_engine — 情绪引擎

基于订单流数据的市场情绪分析引擎。

| 类/函数 | 说明 |
|---|---|
| `SentimentEngine` | 情绪引擎主类 |
| `compute_sentiment()` | 计算市场情绪指标 |
| `sentiment_sr_combined_bt` | 情绪 + 支撑阻力联合回测 |

---

## 风控层

### risk_gate_utils — 风险门禁

多层风险门禁系统，开仓前的综合风险检查。

| 类/函数 | 说明 |
|---|---|
| `RiskGate` | 风险门禁主类 |
| `RiskGateConfig` | 门禁配置（dataclass） |
| `RiskGate.check()` | 执行风险检查，返回 `GateResult` |
| `GateResult` | 检查结果（passed / reasons） |
| `RiskGatePosition` | 持仓相关风控 |

**相关文档**：[风险门禁](../modules/risk-gate.md) · [风控体系](../architecture/risk-management.md)

### risk_state_machine — 风控状态机

状态机驱动的风控系统，根据市场状态动态调整风控参数。

| 类/函数 | 说明 |
|---|---|
| `RiskStateMachine` | 风控状态机 |
| `RiskState` | 风控状态枚举 |
| `transition()` | 状态转换 |

### kelly_utils — 凯利仓位管理

基于凯利公式的仓位管理工具。

| 类/函数 | 说明 |
|---|---|
| `kelly_fraction(win_rate, win_loss_ratio)` | 计算凯利比例（f*） |
| `kelly_position_size(...)` | 计算实际仓位手数 |
| `kelly_gap_signal()` | 凯利 + 缺口信号联合计算 |

**相关文档**：[凯利因子](../modules/kelly-factor.md)

### price_protection — 价格保护

开仓价格保护机制，避免在不利价位成交。

| 类/函数 | 说明 |
|---|---|
| `PriceProtection` | 价格保护类 |
| `check_price()` | 检查开仓价格是否合理 |

**相关文档**：[价格保护](../modules/price-protection.md)

### gap_stop_utils — 缺口止损

基于缺口的止损逻辑。

| 类/函数 | 说明 |
|---|---|
| `GapStop` | 缺口止损类 |
| `detect_gap()` | 检测缺口 |
| `compute_stop()` | 计算止损位 |

**相关文档**：[缺口止损](../modules/gap-stop.md)

### take_profit_utils — 止盈模块

动态止盈策略。

| 类/函数 | 说明 |
|---|---|
| `TakeProfit` | 止盈主类 |
| `compute_tp_level()` | 计算止盈位 |
| `trailing_stop()` | 移动止损 |

**相关文档**：[止盈模块](../modules/take-profit.md)

### corr_gate_utils — 相关性门禁

基于品种间相关性的风险控制。

| 类/函数 | 说明 |
|---|---|
| `CorrGate` | 相关性门禁类 |
| `check_correlation()` | 检查持仓相关性 |
| `compute_portfolio_corr()` | 计算组合相关性矩阵 |

**相关文档**：[相关性门禁](../modules/corr-gate.md)

### drawdown_guard — 回撤守护

组合回撤监控与限仓。

| 类/函数 | 说明 |
|---|---|
| `DrawdownGuard` | 回撤守护类 |
| `check_drawdown()` | 检查回撤水平 |
| `adjust_position_size()` | 根据回撤调整仓位 |

---

## 分析层

### sr_analyzer — 支撑阻力分析

支撑阻力位检测与分析。

| 类/函数 | 说明 |
|---|---|
| `SRAnalyzer` | 支撑阻力分析器 |
| `detect(bars)` | 检测支撑阻力位 |
| `SRAnalyzerResult` | 分析结果 |
| `sr_exit_adjust_backtest` | 支撑阻力止盈回测 |
| `sr_widen_sweep` | 支撑位拓宽扫描 |

### hidden_pivot — 隐藏枢轴

隐藏枢轴点检测算法。

| 类/函数 | 说明 |
|---|---|
| `HiddenPivot` | 隐藏枢轴检测器 |
| `detect_pivots()` | 检测枢轴点 |
| `divergence_hidden_pivot` | 背离 + 隐藏枢轴分析 |

**相关文档**：[隐藏枢轴](../modules/hidden-pivot.md)

### t_score_utils — T 分数统计

统计显著性检验，用于验证策略效果。

| 类/函数 | 说明 |
|---|---|
| `compute_t_score()` | 计算 T 分数 |
| `t_test_significance()` | T 检验显著性 |

**相关文档**：[T 分数](../modules/t-score.md)

### gbm_garch — GBM/GARCH 波动率模型

几何布朗运动 + GARCH 波动率动力学模型。

| 类/函数 | 说明 |
|---|---|
| `GBMGarchModel` | GBM-GARCH 模型 |
| `garch_fit()` | GARCH(1,1) 参数拟合（MLE） |
| `gbm_forward_var()` | GBM 前向 VaR 计算 |
| `vol_state` | 波动率状态分级 |
| `gbm_risk_scale` | 基于波动率的风险缩放系数 |

### regime_hmm — HMM 市场状态识别

隐马尔可夫模型的市场状态识别。

| 类/函数 | 说明 |
|---|---|
| `HMMRegime` | HMM 状态识别类 |
| `fit()` | 模型拟合 |
| `predict_regime()` | 预测当前市场状态 |
| `regime_hmm.py` | 4 态识别 → 调制 T_thresh_eff |

### anomaly_scan — 异常扫描

市场异常检测与扫描。

| 类/函数 | 说明 |
|---|---|
| `AnomalyScanner` | 异常扫描器 |
| `scan_anomalies()` | 扫描市场异常 |
| `anomaly_calibration` | 异常检测校准 |

**相关文档**：[异常扫描](../modules/anomaly-scan.md)

### calibration — 校准工具

策略参数校准与优化工具。

| 类/函数 | 说明 |
|---|---|
| `calibrate_params()` | 参数校准入口 |
| `CalibrationResult` | 校准结果 |
| `calibration_cache` | 校准缓存管理 |

---

## 信号与触发

### signal_trigger_utils — 信号触发

信号触发与过滤逻辑。

| 类/函数 | 说明 |
|---|---|
| `SignalTrigger` | 信号触发器 |
| `generate_signal()` | 生成交易信号 |
| `filter_signal()` | 信号过滤 |

**相关文档**：[信号触发](../modules/signal-trigger.md)

### signal_explain — 信号解释

交易信号的可解释性模块。

| 类/函数 | 说明 |
|---|---|
| `SignalExplainer` | 信号解释器 |
| `explain_signal()` | 确定性信号解释 |
| `llm_explain()` | LLM 辅助解释（需配置 API Key） |
| `explain_gated()` | 门控品种解释 |

---

## 监控层

### consistency_watchdog — 一致性看门狗

系统一致性监控，检测漂移、未验证、服务中断、过期等问题。

| 类/函数 | 说明 |
|---|---|
| `ConsistencyWatchdog` | 一致性看门狗 |
| `check_consistency()` | 执行一致性检查 |
| `/api/consistency` | REST API 端点 |

**相关文档**：[一致性看门狗](../modules/consistency-watchdog.md) · [方向源监控](../architecture/direction-monitor.md)

### live_health_check — 实盘健康检查

实盘运行健康度检查，边沿触发推送。

| 类/函数 | 说明 |
|---|---|
| `live_health_check.py` | 健康检查脚本 |
| 检查项 ① | `/api/health` 可达性 |
| 检查项 ② | `/api/edge` mean_oos 非空校验 |
| 检查项 ③ | 合约与 `main_overrides.json` 一致性 |
| 检查项 ④ | `/api/consistency` ok 状态 |
| 检查项 ⑤ | 换月前瞻预警 |

### account_tracker — 账户追踪

账户资金与持仓追踪。

| 类/函数 | 说明 |
|---|---|
| `AccountTracker` | 账户追踪器 |
| `update_account()` | 更新账户信息 |
| `get_position()` | 获取持仓信息 |

### trade_journal — 交易日志

交易记录与日志标注。

| 类/函数 | 说明 |
|---|---|
| `TradeJournal` | 交易日志类 |
| `record_trade()` | 记录交易 |
| `journal_strategy()` | 策略信号关联标注 |

---

## 数据层

### minishare_live / minishare_feed — 迷你行情源

轻量级行情数据源，提供实时 K 线数据。

| 类/函数 | 说明 |
|---|---|
| `MiniShareLive` | 迷你行情实时类 |
| `get_kline()` | 获取 K 线数据 |
| `_authoritative_contracts()` | 权威合约获取 |

### akshare_live — AkShare 数据源

基于 AkShare 的行情数据接入。

| 类/函数 | 说明 |
|---|---|
| `akshare_live.py` | AkShare 数据模块 |
| `fetch_kline()` | 获取 K 线 |
| `match_main_contract()` | 主力合约匹配 |

### macro_context — 宏观数据

跨资产宏观语境分析。

| 类/函数 | 说明 |
|---|---|
| `macro_bias` | 宏观偏向因子（股/债/汇） |
| `fetch_macro_context()` | 获取宏观数据 |
| `fetch_macro_context_full.py` | 全量宏观采集器 |

### info_dimension — 信息维度

外部信息采集与信号叠加。

| 类/函数 | 说明 |
|---|---|
| `info_dimension.py` | 信息维度模块 |
| `fetch_info_dimension.py` | 信息采集脚本 |
| `F_override` | 信息分覆盖（仅 live，不喂回测） |

### event_calendar — 事件日历

事件日历与仓位缩放。

| 类/函数 | 说明 |
|---|---|
| `EventCalendar` | 事件日历类 |
| `scale_factor()` | 事件缩放系数计算 |

---

## 执行层

### execution_planner — 执行规划

交易执行规划与下单管理。

| 类/函数 | 说明 |
|---|---|
| `ExecutionPlanner` | 执行规划器 |
| `plan_order()` | 规划下单 |
| `execute_order()` | 执行订单 |

### backend_tqsdk — TqSdk 后端

TqSdk 交易后端适配。

| 类/函数 | 说明 |
|---|---|
| `TqSdkBackend` | TqSdk 后端类 |
| `connect()` | 连接交易接口 |
| `send_order()` | 发送订单 |

### tick_orderflow — Tick 订单流

Tick 级订单流数据分析。

| 类/函数 | 说明 |
|---|---|
| `TickOrderflow` | Tick 订单流分析器 |
| `process_tick()` | 处理逐笔成交 |
| `compute_flow()` | 计算订单流指标 |

### preflight_check — 起飞前检查

交易前系统检查，确保运行环境正常。

| 类/函数 | 说明 |
|---|---|
| `PreflightCheck` | 起飞前检查类 |
| `run_checks()` | 运行全部检查 |
| `check_list` | 检查项清单 |

---

## 遗传算法优化

### ga_* — 遗传算法系列

GA 因子挖掘与参数优化系列模块。

| 模块 | 说明 |
|---|---|
| `ga_factor_miner.py` | 因子挖掘 |
| `ga_six_factor.py` | 六因子模型 |
| `ga_group_six_factor.py` | 分组六因子优化 |
| `ga_tpsl_optimizer.py` | 止盈止损优化 |
| `ga_oos_validation.py` | OOS 样本外验证 |
| `ga_quality_filter.py` | 质量过滤 |
| `ga_batch_optimize.py` | 批量优化 |
| `ga_blend_sweep.py` | 混合权重扫描 |
| `ga_robust_oos_compare.py` | 鲁棒性 OOS 对比 |

---

## 工具与辅助

### montecarlo — 蒙特卡洛模拟

蒙特卡洛模拟用于风险评估与策略测试。

| 类/函数 | 说明 |
|---|---|
| `MonteCarlo` | 蒙特卡洛模拟器 |
| `simulate()` | 运行模拟 |
| `compute_var()` | 计算 VaR |

### data_quality — 数据质量

数据质量检查与清洗。

| 类/函数 | 说明 |
|---|---|
| `DataQualityChecker` | 数据质量检查器 |
| `check_completeness()` | 完整性检查 |
| `clean_data()` | 数据清洗 |

### feature_manager — 特征管理

特征工程管理工具。

| 类/函数 | 说明 |
|---|---|
| `FeatureManager` | 特征管理器 |
| `register_feature()` | 注册特征 |
| `compute_features()` | 计算特征集 |

---

## 实盘运行器

### four_dim_live_runner — 四维实盘运行器

四维策略实盘主运行器，管理策略实盘运行。

| 功能 | 说明 |
|---|---|
| 主循环 | 策略实盘主循环 |
| `/api/state` | 系统状态面板（端口 8741） |
| `/api/consistency` | 一致性检查端点 |
| `/api/rollover` | 换月概览端点 |
| `/api/rollover_mismatch` | 换月不一致检查端点 |
| `/api/edge` | 边缘计算 / 校准参数端点 |
| `/api/health` | 健康检查端点 |
| 自动重启 | launchd 管理，崩溃自动重启 |

### four_dim_papertrack — 模拟盘

模拟盘（纸上交易）运行器。

| 功能 | 说明 |
|---|---|
| 模拟交易 | 基于实时行情的模拟交易 |
| 绩效统计 | 模拟盘绩效统计 |
| 对比基准 | 与实盘对比分析 |

---

## 浏览源码

所有源代码托管在 GitHub 上：

[:fontawesome-brands-github: alonglong5118-org/futures-orderflow](https://github.com/alonglong5118-org/futures-orderflow)

你也可以直接阅读项目中的 Python 源码文件，它们都包含详细的 docstring 和注释。测试文件（`tests/` 目录下 272+ 个测试用例）也是很好的用法参考。
