# 路线图

本文档记录 futures-orderflow 四维策略盯盘系统的开发进度与未来规划。

> 更新于 2026-08-23 · 当前版本：**v3.0.0**

---

## 已完成模块

以下模块已全部落地并通过实盘验证：

| # | 模块 | 说明 |
|---|---|---|
| #1 | 信息维度 | `info_dimension.json` 外部采集 → `F_override` 喂 live 信号（红线：不喂回测） |
| #2 | 事件缩放 | `event_calendar.scale_factor()` 接入手数缩放，取 `min()` 较严者 |
| #3 | 漂移闭环 | 每 6h 漂移检测 + 关注品种自动 staging 候选 T，人工一键 apply |
| #4 | 信号解释 | `signal_explain.py` 确定性解释器（结论先行 + 多维度 bullets + LLM 可选） |
| #5 | 一致性看门狗 | `/api/consistency` 检测 4 类问题（只报告不修正） |
| #6 | 跨资产宏观语境 | `macro_bias`（股/债/汇）温和调制 bias_G，回测零前视 |
| #7a | HMM 市场状态 | `regime_hmm.py` 4 态识别 → 调制 T_thresh_eff（0.90~1.25） |
| #7b | GBM/GARCH 波动率动力学 | GARCH(1,1) MLE + GBM 前向 VaR/区间 + vol_state 分级 |

### 配套能力

- **risk_scale 接入手数** — 高波动 0.8/0.6 真实减仓（取严者），`gbm_risk_scale` 独立暴露
- **主力合约权威覆盖** — `main_overrides.json` 权威层 + 实时解析 + 单调向前 + 告警
- **换月异常告警** — ABNOLD 6h / FETCHFAIL·临界 7天状态变化推送，macOS 通知

---

## v3.0.0 升级要点

v3.0.0 为当前最新版本，相对 v2.5.0 新增以下能力：

### 核心新功能

- **自动参数优化 `auto-optimize`** — 一键扫描优化信号阈值，含 lock / toggle / reset 防护，auto-stage + 人工一键 apply
- **市场状态机 `market-state`** — HMM 宏观状态实时可视化 + 切换日志
- **账户自愈 `account_sync` / `account_heal`** — 与 CTP 账户对账同步 + 自动修复幽灵持仓 / 孤儿头寸
- **盘前简报 `premarket`** — 盘前机会扫描与作战清单
- **交叉验证 `crosscheck`** — 多源数据交叉核对，防前视 / 口径漂移
- **盈亏归因 `pnl_attribution`** — 逐笔盈亏 F/T/C 维度归因
- **日志策略标注 `journal_strategy`** — 交易日志 ↔ 策略信号关联标注
- **异常检测 `anomaly` + 熔断 `killswitch` + 回撤守护 `drawdown`** — 实时异常监测 + 一键全局熔断 + 组合回撤限仓
- **校准中心 `calibration` + 券商/批量/工具箱 `broker/batch/tools` + 信号拆解 `decompose`** — 参数可视化与一键应用、交易执行增强、信号多因子拆解

### v3.0.0 收尾（全绿闭环）

- **`/api/edge` 双常量缺失修复** — 修复 `vol_regime()` 和 `_load_calib()` 的常量缺失问题，杜绝接口 500 或数据全空的监控失明
- **JM apply 伪报告教训** — 确立独立复核铁律：trea 报「已修复」须经独立复核验证
- **live 周期自检自动化** — 新增 `live_health_check.py`（四项检查 + 边沿触发推送）+ HOURLY 周期调度
- **Git 版本管理收尾** — 核心 47 个 `.py` + 前端/脚本/图标全部入库，运行期产物全部 `.gitignore` 忽略

### v3.0.0 改进序列

- **P1-① DEEPSEEK 信号解释注入** — plist 注入 `DEEPSEEK_API_KEY/BASE_URL/MODEL`，`signal_explain.llm_explain()` 仅在 key 存在时叠加
- **P1-② 门控品种透明提示卡** — `signal_explain.explain_gated()` + 前端「门控品种提示」卡片
- **P1-③ 换月前瞻预警** — `live_health_check.py` 新增合约到期三级预警
- **P2-⑤ 静默 except 硬化** — 6 处 loader `except: return {}` → exists 守卫 + 可见日志
- **P2-④ 组合 F OOS 权重验证** — walk_forward_backtest assert 红线守卫 + 新 harness `oos_weight_validation.py`

---

## 当前系统状态

- runner 在线（55 品种），无崩溃；`/api/state` 正常
- 版本 `v3.0.0` 已动态下发（面板侧栏实时渲染）
- 一致性看门狗：`ok=true`，`divergences=0`、`unvalidated=0`、`broken_serving=0`、`stale=0`
- 换月主动提醒端点已修复并正常消费：`/api/rollover`、`/api/rollover_mismatch` 秒回
- 主力刷新：每 2h 正常执行
- live 周期自检自动化：HOURLY 周期运行 `live_health_check.py`

---

## 红线与铁律

以下为不可违反的红线规则：

1. `score_F` 量程是 [-100,100]；信息分只喂 live 不喂回测
2. 回测三处调用不传任何 live 专属参数（零前视污染）
3. 自动类功能必须验证终端效果被消费（`/api/state` 实时字段为准），不可假自动
4. 持仓品种（AP/FG/SA/c/rb）钉死开仓合约不滚、SA01/SA09 fixed 不滚——属正确设计
5. 改核心文件后必须 `launchctl kickstart -k gui/502/com.ken.futures-orderflow.live` 重启

---

## 下一步候选

### P1 — 看门狗暴露的问题（保持现状）

- **JM**：`broken_real`（expR=-1.0、mean_oos=-0.967），已被动态门控 `gated=True` 压制、不发信号。用户确认保持现状
- **lh**：同 JM `broken_real`，动态门控压制不发信号。保持现状

### P2 — 体验与展示完善（已完成）

- 面板展示 GBM 前向情景 ✅ 已落地
- risk_scale 进信号解释 ✅ 已落地

### P3 — 数据源增强（暂缓）

- **INE/原油源修复** — akshare 上游 INE 接口故障，免费源受阻、付费性价比低，暂缓
- **宏观数据补全** — 原油/南华商品指数/USDA 缺失。`macro_bias` 只用 hs300/cgb10/usdcny 三因子，缺失对 live 信号零影响，暂缓
- 采集器骨架 `fetch_macro_context_full.py` 已就位，未来充值 Tushare 或决定融合商品因子时可直接使用

### P4 — 换月期运维（例行）

- 大换月落地后核对 `main_overrides.json` 固化值（已完成 2026-08-18 核对）
- B2 换月主动提醒已上线运行

---

## 历史版本

| 版本 | 核心特性 | 状态 |
|---|---|---|
| v3.0.0 | 自动参数优化 + 市场状态机 + 账户自愈 + 盘前简报 + 盈亏归因 + 异常/熔断/回撤 + 校准中心 + 信号拆解 | ✅ 当前版本 |
| v2.5.0 | 信息维度 + 事件缩放 + 漂移闭环 + 信号解释 + 一致性看门狗 + 宏观语境 + HMM + GBM-GARCH + 主力合约权威覆盖 | ✅ 已发布 |
| v2.4.0 | B2 换月主动提醒 + 运行时修复 | ✅ 已发布 |
| v1.x | 基础四维策略 + 风控体系 | ✅ 已发布 |

---

> 路线图根据项目进展持续更新。如有功能建议，欢迎通过 [GitHub Issues](https://github.com/alonglong5118-org/futures-orderflow/issues) 提出。
