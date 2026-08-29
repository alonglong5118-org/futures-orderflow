# 变更日志

本文档记录 futures-orderflow 的版本历史与重要变更。详细的版本发布说明请参阅 [GitHub Releases](https://github.com/alonglong5118-org/futures-orderflow/releases)。

---

## 版本格式

项目遵循 [语义化版本（Semantic Versioning）](https://semver.org/lang/zh-CN/) 规范：

- **主版本号（MAJOR）** — 不兼容的 API 变更
- **次版本号（MINOR）** — 向下兼容的功能性新增
- **修订号（PATCH）** — 向下兼容的问题修正

---

## v3.0.0

> 发布日期：2026-08-23 · 主要版本

### 核心新功能

- **自动参数优化（auto-optimize）** — 一键扫描优化信号阈值，含 lock / toggle / reset 防护，auto-stage + 人工一键 apply，绝不自作主张覆盖线上参数
- **市场状态机（market-state）** — HMM 宏观状态实时可视化 + 切换日志
- **账户自愈（account_sync / account_heal）** — 与 CTP 账户对账同步 + 自动修复幽灵持仓 / 孤儿头寸
- **盘前简报（premarket）** — 盘前机会扫描与作战清单
- **交叉验证（crosscheck）** — 多源数据交叉核对，防前视 / 口径漂移
- **盈亏归因（pnl_attribution）** — 逐笔盈亏 F/T/C 维度归因
- **日志策略标注（journal_strategy）** — 交易日志 ↔ 策略信号关联标注
- **异常检测（anomaly）+ 熔断（killswitch）+ 回撤守护（drawdown）** — 实时异常监测 + 一键全局熔断 + 组合回撤限仓
- **校准中心（calibration）+ 券商/批量/工具箱（broker/batch/tools）+ 信号拆解（decompose）** — 参数可视化与一键应用、交易执行增强、信号多因子拆解

### v3.0.0 收尾修复

- 修复 `/api/edge` 双常量缺失问题（`_VOL_CACHE`、`CALIB_FILE`），杜绝接口 500 或数据全空的监控失明
- 新增 `live_health_check.py` 周期自检（四项检查 + 边沿触发推送）+ HOURLY 自动化调度
- 门控品种透明提示卡（前端「门控品种提示」卡片 + 30s 轮询兜底）
- 换月前瞻预警（已过期/交割月临界/下月进交割月 三级预警）
- 静默 except 硬化（6 处 loader 改为 exists 守卫 + 可见日志）
- 组合 F OOS 权重验证 harness（`oos_weight_validation.py`）
- Git 版本管理收尾（核心代码全入库，运行期产物全忽略）

### 破坏性变更

无。v3.0.0 所有新增功能均为可选模块，不影响既有接口。

---

## v2.5.0

> 发布日期：2026-08 · 次要版本

### 新增功能

- **信息维度（#1）** — `info_dimension.json` 外部采集 → `F_override` 喂 live 信号
- **事件缩放（#2）** — `event_calendar.scale_factor()` 接入手数缩放
- **漂移闭环（#3）** — 每 6h 漂移检测 + 自动 staging 候选 T + 人工一键 apply
- **信号解释（#4）** — 确定性解释器（结论先行 + 多维度 bullets + LLM 可选）
- **一致性看门狗（#5）** — `/api/consistency` 检测 4 类问题
- **跨资产宏观语境（#6）** — `macro_bias`（股/债/汇）温和调制 bias_G
- **HMM 市场状态（#7a）** — 4 态识别 → 调制 T_thresh_eff
- **GBM/GARCH 波动率动力学（#7b）** — GARCH(1,1) MLE + GBM 前向 VaR/区间

### 配套能力

- `risk_scale` 接入手数（高波动真实减仓）
- 主力合约权威覆盖（`main_overrides.json` 权威层）
- 换月异常告警（ABNOLD / FETCHFAIL 推送 + macOS 通知）

---

## v2.4.0

> 发布日期：2026-08 · 次要版本

### 新增功能

- **B2 换月主动提醒** — 运行时「系统在用主力 vs 交易所真实主力」主动比对
    - 面板 B2 卡红条列出不一致品种
    - `raiseToast` 主动弹窗 + 语音播报 + 预警角标
    - 新端点 `/api/rollover_mismatch`
    - 主循环每 30min 后台刷新

### 修复

- 采集器大小写键不匹配 Bug（`ak_main.get(v)` 大写查小写字典恒为 None）
- 采集器月份上界算术错误（`now_ym + 12` 数值相加而非月份进位）
- 运行时 `_authoritative_contracts()` 键名不匹配 Bug（大写品种查不到覆盖）
- `/api/rollover_mismatch` 端点阻塞 Bug（缓存未就绪时同步拉取导致前端超时）
- 空映射保护（瞬断时保留旧缓存，避免 B2 卡 pending）

---

## v1.x

> 初始版本系列 · 已发布

### 核心功能

- 四维策略基础框架（F/T/C + 时间维度）
- 风险门禁（RiskGate）
- 凯利仓位管理
- 价格保护机制
- 支撑阻力分析
- 缺口止损
- 止盈模块
- 相关性门禁
- 隐藏枢轴检测
- T 分数统计
- 异常扫描
- 基础回测框架

---

## 如何查看完整变更

详细的版本发布说明（包含每个 PR 的变更明细）请访问：

- [GitHub Releases 页面](https://github.com/alonglong5118-org/futures-orderflow/releases)
- [项目路线图](roadmap.md)

---

> 本变更日志最后更新于 2026 年 8 月
