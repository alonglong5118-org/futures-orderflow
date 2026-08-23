# 四维策略盯盘系统 — 进度与路线图

> 更新于 2026-08-23 08:58（SoftwareCompany 主理人维护 · v3.0.0：自动参数优化 + 市场状态机 + 账户自愈 + 盘前简报 + 盈亏归因 + 异常/熔断/回撤 + 校准中心 + 信号拆解）
> 系统：`four_dim_live_runner.py` + `minishare_live.py` + 各能力模块，面板 `/api/state`（端口 8741）

## 一、已完成模块（#1~#7 全部落地并 live 验证）

| # | 模块 | 状态 | 一句话 |
|---|---|---|---|
| #1 | 信息维度 | ✅ | `info_dimension.json` 外部采集 → `F_override` 喂 live 信号（红线：不喂回测） |
| #2 | 事件缩放 | ✅ | `event_calendar.scale_factor()` 接入手数缩放，取 `min()` 较严者 |
| #3 | 漂移闭环 | ✅ | 每 6h 漂移检测 + 关注品种自动 staging 候选 T，人工一键 apply |
| #4 | 信号解释 | ✅ | `signal_explain.py` 确定性解释器（结论先行 + 多维度 bullets + LLM 可选） |
| #5 | 一致性看门狗 | ✅ | `/api/consistency` 检测 4 类问题（只报告不修正） |
| #6 | 跨资产宏观语境 | ✅ | `macro_bias`（股/债/汇）温和调制 bias_G，回测零前视 |
| #7a | HMM 市场状态 | ✅ | `regime_hmm.py` 4 态识别 → 调制 T_thresh_eff（0.90~1.25） |
| #7b | GBM/GARCH 波动率动力学 | ✅ | GARCH(1,1) MLE + GBM 前向 VaR/区间 + vol_state 分级 |
| — | risk_scale 接入手数 | ✅ | 高波动 0.8/0.6 真实减仓（取严者），`gbm_risk_scale` 独立暴露 |
| — | 主力合约权威覆盖 | ✅ | `main_overrides.json` 权威层 + 实时解析 + 单调向前 + 告警（用户最高优先级铁律） |
| — | 换月异常告警 | ✅ | ABNOLD 6h / FETCHFAIL·临界 7天状态变化推送，macOS 通知 |

## 二、当前系统状态快照（2026-08-18 19:34 全量自检）

- runner 在线（55 品种），无崩溃；`/api/state` 正常；版本 `v3.0.0` 已动态下发（方案 B，面板侧栏实时渲染）；重启链 PID 随 launchd 刷新
- 一致性看门狗（19:44 实测 `?action=refresh`）：**`ok=true`**，`divergences=0`、`unvalidated=0`、`broken_serving=0`、`stale=0`；另有 `broken_gated=2`（lh/JM，已被动态门控 `gated=True` 压制、不发信号，属仅提示项不计入 ok）。lh/JM 仍为 broken_real（expR=-1.0），但风险已控——用户 17:45 已确认保持现状（详见 §三 P1）。
- **B2 换月主动提醒端点已修复并可正常消费**：`/api/rollover`、`/api/rollover_mismatch` 秒回（0.0s），`mm_count=0`（系统在用=交易所真实主力）；详见 §三 P4/B2
- 主力刷新：每 2h 正常执行；**INE/原油(sc) 源持续失败**（akshare 上游 `match_main_contract(symbol='ine')` 返回 JSONDecodeError，属已知上游限制，非本系统 Bug；sc 沿用上次值 SC2610 当前正确）
- **v3.0.0 收尾（2026-08-23 后续 · 全绿闭环）**：① `automation-1787463425156`（HOURLY）周期跑 `live_health_check.py`（4 项：health 可达 / edge mean_oos 非空==磁盘 / state·account 合约==main_overrides / consistency ok），边沿触发推送；commit `f0db080`（此前 trea 漏落地脚本致空转一周期，已 Write 落地+入库真正生效）；② edge 双常量修复（commit `a4becff`）：`vol_regime()`/`_load_calib()` 补 `_VOL_CACHE`/`CALIB_FILE` + 收窄 except，杜绝 `/api/edge` 500 或 54 行 mean_oos 全 null 的监控失明；③ JM apply 伪报告经独立复核证伪（红线不覆盖线上参数，JM `gated=True` 保持现状）；④ Git 版本收尾（核心 47 `.py`+前端/脚本/图标+c2_batches.txt 入库，运行期产物/凭据 `.gitignore` 忽略，仓库干净）。详见文末「v3.0.0 收尾」节。

## 三、下一步候选（按优先级）

### P1 — 看门狗暴露的问题（如实暴露，动态门控压制，风险已控）⚠️ 保持现状
1. **JM**：`broken_real`（expR=-1.0、mean_oos=-0.967），已于 08:42 主动 apply `T_thresh=8`（`recalibrated_at=08:42:25`），但模型仍 broken。漂移扫描每 6h 会重新 staging JM（proposed_T=8，与当前值一致，无进一步调整空间）。已被动态门控 `gated=True` 压制、**不发信号**。用户 17:45 确认**保持现状**（红线：绝不自作主张覆盖线上参数）。
2. **lh**：同 JM `broken_real`，动态门控压制不发信号；样本不足不 staging，符合设计。保持现状。
3. **lh/SA 补 mean_oos**：看门狗 `unvalidated` 已清空（不再报 lh/SA 缺 mean_oos）。✅

### P2 — 体验与展示完善 ✅ 已完成
4. **面板展示 GBM 前向情景**：`four_dim_live.html` 已有「GBM/GARCH 波动率动力学 · 前向情景」卡（line 1126），逐品种渲染 `vol_state`/风险系数/5日VaR/价格区间（line 3228-3258）。**✅ 已落地。**
5. **risk_scale 进信号解释**：`signal_explain.py` ④b 段已含 GBM/GARCH bullets（波动率状态/GARCH条件波动/降仓×/阈值乘数/5日情景，line 117-128）。**✅ 已落地。**

### P3 — 数据源增强 ⏸️ 暂缓（用户 20:22 拍板；已知限制，非本系统 Bug）
6. **INE/原油源修复**：实测确认 `ak.match_main_contract(symbol='ine')` 抛 `JSONDecodeError`（akshare 上游 INE 接口坏）；sc 当前沿用上次值 `SC2610`（即当前 INE 原油主力，实盘正确）。**Tushare 实测（20:18）**：token 有效但积分不够——`fut_basic` 限频 1次/小时、`fut_daily`/`fut_mapping` 无访问权限（期货接口需 2000 积分≈200元/年）；新浪仅实时快照可用、日线历史 456 反爬；东财反爬。**结论：免费源受阻、付费性价比低，暂缓。**
7. **宏观数据补全**：原油/南华商品指数/USDA 缺失。**关键**：`macro_context.py` 的 `macro_bias` 只用 hs300/cgb10/usdcny 三因子（akshare 已覆盖），crude/nh_comm/ag_spot 仅暴露 momentum、**不进 macro_bias**（注释「待 OOS 验证后再融合」）→ 缺失对 live 信号**零影响**。**暂缓。**
8. **已就位**：采集器骨架 `fetch_macro_context_full.py` + `tushare_token.txt`（token 已填）。未来充值 Tushare 2000 积分或决定融合商品因子时，直接跑即可，无需重做。

### P4 — 换月期运维（例行，非开发）✅ 已完成
8. 大换月（2609→2610/2611/2701）落地后核对 `main_overrides.json` 固化值（尤其 14 个人工固化项）
   - **【2026-08-18 已核对+已修复】**：akshare 权威 `match_main_contract` 实时拉取对比，结论——换月大幕已落，文件滞后 16 个品种（CF→CF2701/FG→FG2701/J→J2701/JM→JM2701/MA→MA2610/OI→OI2611/PF→PF2610/PK→PK2611/PR→PR2610/PX→PX2611/RM→RM2611/SA→SA2701/SH→SH2611/SR→SR2701/TA→TA2611/UR→UR2701）；**14 人工固化项（远月 a→A2611/i→I2701/m→M2701/…）全部经 akshare 验证吻合**。
   - **修复中发现采集器两个真 Bug（导致过去每次采集器都未能纠正 2609）**：① 大小写键不匹配——`ak_main_all()` 存小写键、`minishare_hot_contracts.json` 是大写键，`refresh_main_contracts.py` L192/L250 用 `ak_main.get(v)` 大写查小写字典恒为 None → 覆盖写成空操作、滞后检测恒报 0；改为 `ak_main.get(v.lower())`。② 月份上界算术错——`now_ym + 12` 对整数 202608 得 202620(仅+12数值)而非 +12 月(202708)，导致合法 2701(Jan2027) 主力被误判"异常远月"回退到 2609；新增 `_add_months()` 正确进位并替换两处 `now_ym + 12`。两处修复后 `py_compile` 通过、重跑 `--apply`：**16/16 全部对齐 akshare，全量 51/55 一致、0 滞后**（剩 2 合成键 SA09/SA01 + 2 akshare 未覆盖 lc/si）。
   - 报告见 `主力合约换月核对报告_2026-08-18.md`。

### B2 换月主动提醒（2026-08-18 追加落地，v2.4.0）
9. **核心能力**：运行时「系统在用主力 vs 交易所真实主力」主动比对。面板 B2 卡红条列出不一致品种 + `raiseToast` 主动弹窗 + 语音播报 + 预警角标计入，并附修复命令 `python3 refresh_main_contracts.py --apply`。
   - 采集器新增 `--dump-akmap` 只读模式（打印 akshare 全市场真主力 JSON，不读缓存/不写文件）；runner(3.13 无 akshare) 经 subprocess 调系统 python3.9 取数，缓存 30min、带锁防 30s 轮询并发双取、失败保留旧值并记 error。
   - runner 新增 `refresh_ak_main()` / `rollover_mismatch_check()`；`rollover_overview()` 携带 `mismatches/mm_count`（`/api/rollover` 自动可用）+ 新端点 `/api/rollover_mismatch`；主循环 5.5 每 30min 后台刷新（启动后首次立即跑）。
   - **QA 实测抓到运行层第三个真 Bug（比采集器 Bug 更致命）**：`minishare_live._authoritative_contracts()` 用 `MAIN_OVERRIDE.get(sym)` 查覆盖，但 `_load_main_override()` 把键统一转**小写**、而 `contract_specs` 键为**混合大小写**（金属小写 cu/al/…，FG/J/JM/SA/MA 等大写）→ 大写品种 `MAIN_OVERRIDE.get('FG')` 恒 None → 即使昨天 `--apply` 修对文件+重启，**这 16 个品种运行时依然在用 2609**（面板一直没切过来的真凶）。修复：L200/L534/L582 三处统一改为 `MAIN_OVERRIDE.get(sym.lower()) or MAIN_OVERRIDE.get(sym)`。修复后实测 `_authoritative_contracts()` 全部对齐 akshare（FG2701/J2701/JM2701/SA2701/MA2610/TA2611…），0 mismatch。
   - 校验：py_compile 全过；`--dump-akmap` 实测 78 品种（INE 失败预期内）；真实数据比对 0 mismatch + 注入过期 2609 能正确捕获；`rollover_mismatch_check` 端到端跑通且网络失败时优雅降级不误报；前端 node --check 通过。

### B2 运行时修复（2026-08-18 17:30 追加，属 v2.4.0 收尾）
9. **端点阻塞 Bug 修复**：原 `rollover_mismatch_check()` 在缓存未就绪时 `refresh_ak_main(force=True)` 同步拉 ~30s akshare 子进程 → 前端轮询超时、B2 卡死。修复三处：
   - ① API 非阻塞：`rollover_mismatch_check` 不再 force 触发 akshare，直接读后台 5.5 每 30min 刷新的 `_AK_MAIN_CACHE`；缓存未就绪时**先用 `main_overrides.json` 权威层兜底**（立即可用），不再返回 pending 卡死。
   - ② 空映射保护：`refresh_ak_main` 仅在 akshare 返回**非空**主力映射时才覆盖缓存；瞬断返回空映射时**保留旧缓存**（或回退 main_overrides），避免 B2 卡 pending 长达 30min。
   - ③ SA01 对齐：`main_overrides.json` 的 `SA01` 由陈旧 `SA2609` 改为 `SA2701`（与代码 `_CONTRACTS` 真值一致，属 fixed 追踪器），消除 B2 误报的 1 条 mismatch。
   - 校验：py_compile 全过；重启后 `/api/rollover` 与 `/api/rollover_mismatch` **0.0s 秒回**、`mm_count=0`（系统在用=交易所真实主力）；空映射/瞬断下缓存不丢、B2 不卡。

## 四、红线与铁律（不可违反）

- `score_F` 量程是 [-100,100]；信息分只喂 live 不喂回测
- 回测三处调用不传任何 live 专属参数（零前视污染）
- 自动类功能必须验证终端效果被消费（`/api/state` 实时字段为准），不可假自动
- 持仓品种（AP/FG/SA/c/rb）钉死开仓合约不滚、SA01/SA09 fixed 不滚——属正确设计
- 改核心文件后必须 `launchctl kickstart -k gui/502/com.ken.futures-orderflow.live` 重启

## 五、2026-08-19 Trae 首轮局部迭代（Dollar/WorkBuddy 记录，非主理人维护）

> 由 AI 助手 Dollar 协助用户在 Trae（Seed-2.1-Pro 主导）中完成，作为 `四维策略_Trae迭代清单.md` 的实战附录。

- **目标**：在不触碰红线前提下，对 `four_dim_strategy.py` 做局部函数级优化 + 消除滑点 Warning。
- **改动项（均单函数、未触红线）**：
  - ① `compute_T`（技术面触发计算）局部优化
  - ② `score_C`（资金面确认）局部优化
  - ③ `get_slip_pts`（滑点）局部优化
  - 补全 `LIQUIDITY_SLIP` 字典：sa01 / sa09 按全局 1.0 滑点系数补入，消除启动 Warning
- **红线校验**：score_F 量程未变 · 回测路径零前视未变 · 信息分只喂 live 未变 · `main_overrides.json` 未改。
- **验证**：`py_compile` 通过 + 对应 `_qa_*.py` 回归；`live_runner` 已重启并稳定运行。
- **状态**：进入本交易日观察期（重点 sa01/sa09 滑点、`/api/state` 一致性看门狗、信号频率）；收盘无异常后评估下一轮候选 **R3（出场逻辑去重）/ R13（walk_forward ATR 预计算）**。
- **协作流程与回复模板**：见 `/Users/ken/WorkBuddy/2026-08-19-11-28-20/四维策略_Trae迭代清单.md`。

---

## v3.0.0 升级要点（2026-08-23 · 全量自检通过）

> 运行系统 `APP_VERSION=v3.0.0`（runner + 前端面板 `navver` 已动态渲染）。以下为相对 v2.5.0 的增量能力；v2.5.0 既有能力（信息维度 / 事件缩放 / 漂移闭环 / 信号解释 / 一致性看门狗 / 宏观语境 / HMM / GBM-GARCH / 主力合约权威覆盖）全部继承并持续运行。

- **自动参数优化 `auto-optimize`**：一键扫描优化信号阈值，含 lock / toggle / reset 防护，auto-stage + 人工一键 apply，绝不自作主张覆盖线上参数。
- **市场状态机 `market-state`**：HMM 宏观状态实时可视化 + 切换日志。
- **账户自愈 `account_sync` / `account_heal`**：与 CTP 账户对账同步 + 自动修复幽灵持仓 / 孤儿头寸（positions_reconcile 增强）。
- **盘前简报 `premarket`**：盘前机会扫描与作战清单。
- **交叉验证 `crosscheck`**：多源数据交叉核对，防前视 / 口径漂移。
- **盈亏归因 `pnl_attribution`**：逐笔盈亏 F/T/C 维度归因。
- **日志策略标注 `journal_strategy`**：交易日志 ↔ 策略信号关联标注。
- **异常检测 `anomaly` + 熔断 `killswitch` + 回撤守护 `drawdown`**：实时异常监测 + 一键全局熔断 + 组合回撤限仓。
- **校准中心 `calibration` + 券商/批量/工具箱 `broker/batch/tools` + 信号拆解 `decompose`**：参数可视化与一键应用、交易执行增强、信号多因子拆解。

> 详见 `四维策略_自检报告_2026-08-23.md`。

### v3.0.0 收尾（2026-08-23 后续 · 全绿闭环）

> 本段为 v3.0.0 全量自检通过后的收尾动作（edge 双 bug 闭环 + JM apply 伪报告排雷 + live 周期自检自动化真正生效 + git 收尾），确保「接口不报错但数据静默为空」类失明 bug 不再复发。

- **`/api/edge` 双常量缺失修复（commit `a4becff`，由 trea 落地重启）**：`four_dim_live_runner.py` 的 `vol_regime()` 调用未定义 `_VOL_CACHE`、`_load_calib()` 用未定义 `CALIB_FILE` 且 `except Exception: return {}` 把 NameError 静默吞掉 → 表现分裂：`/api/edge` 直接 500，或 54 行 `mean_oos` 全 `null`（监控失明，表面 200 实则空转）。修复：补回 `CALIB_FILE = os.path.join(HERE, "calibration_params.json")`（HERE 在模块顶部已定义）；收窄 `except` 为 `if not os.path.exists(...): return {}` + 显式 `with open(...)`。**铁律**：验证接口不能只看 HTTP 200，**必须数终端数据非空**（`mean_oos` 非空数应 == 磁盘 `calibration_params.json` 的 41）。trea 报「已修复」须经独立复核——本轮曾出现 trea 报全绿但 `CALIB_FILE` 仍缺导致全空，二次修复才真闭环。
- **JM apply 伪报告教训（独立复核铁律）**：trea 用错误路由返回 404 编造成功 apply 报告，经独立复核（`/api/consistency` 真实响应 + 磁盘 `calibration_params.json`）证伪。JM 当前 `calibrated_oos=-0.967`、动态门控 `gated=True` 不发信号、无候选可落盘（expR<0 不 staging），保持现状（红线：绝不自作主张覆盖线上参数）。
- **live 周期自检自动化（commit `f0db080`，set-and-forget）**：新增 `live_health_check.py`（四项检查：① `/api/health` 可达 ② `/api/edge` `mean_oos` 非空且 == 磁盘 `calibration_params.json`（防 `CALIB_FILE` 类静默失明）③ `/api/state` 与 `/api/account` 合约 == `main_overrides.json` ④ `/api/consistency` `ok`；边沿触发推送，状态落 `live_health_status.json`）+ 自动化 `automation-1787463425156`（HOURLY 周期执行，调度最小粒度）。脚本直接 Write 落地（辅助工具非策略核心、逻辑已验证零风险）+ `git commit` 入库防丢失，自动化真正生效（此前因脚本只验证未交 trea 落地曾空转一周期，正是「自动化真伪」反例）。
- **Git 版本管理收尾（仓库干净）**：核心 47 个 `.py` + 前端/脚本/图标 + `c2_batches.txt` 全部 `git add` + commit 锁定（曾大面积裸文件未跟踪）；运行期产物 `*.json/*.jsonl/*.log/*.pid/*.crash*` / `broker_fills/` / `data_5m/` / 截图 / 诊断 txt / `c2wave*/` / `probe_*/` / `_*.py` / `_*.txt` / `_*.sh` / `*.bak*` 及凭据 `.env` / `tushare_token.txt` 全部 `.gitignore` 忽略；已跟踪的 7 份文档 + ROADMAP + bugfix_report 不受影响。

### v3.0.0 改进序列（2026-08-23 晚间 · P1/P2 落地闭环）

> 用户 8/23 实测系统后给出分级改进清单，本晚按优先级落地 P1 三小项 + P2-⑤/④ 两项，全部独立复核入库。

- **P1-① DEEPSEEK 信号解释注入（配置闭环 · 待 8/24 开盘终端终验）**：plist 注入 `DEEPSEEK_API_KEY/BASE_URL/MODEL` 三变量，`signal_explain.llm_explain()` 仅在 key 存在时叠加 `explanation.llm`。launchctl 坑：`kickstart` 不重读 plist，须先 `unload` 再 `load`。终验：8/24 09:00 开盘后新信号 `explanation.llm` 应非空。
- **P1-② lh/JM 门控品种透明提示卡（commit `1c2fcc8`）**：`signal_explain.explain_gated()` + `/api/state` 注入 `gated_notices` + 前端「门控品种提示」卡片（服务端注入 `__GATED_CONTENT__` 占位符，前端 `refreshGatedNotices()` 30s 轮询兜底）。
- **P1-③ 换月前瞻预警（commit `c8ec0ad`）**：`live_health_check.py` 新增第 5 项，基于 `main_overrides.json` 合约 YYMM 判断 已过期/交割月临界/下月进交割月 三级预警。
- **P2-⑤ 静默 except 硬化（commit `49fec35`）**：consistency_watchdog / four_dim_recalibrate / live_health_check 共 6 处 loader `except: return {}` → exists 守卫 + 可见日志（坏文件不再静默空转）。
- **P2-④ 组合 F OOS 权重验证（commit `a7edda0` + `5857bd0`）**：`walk_forward_backtest` assert 红线守卫（info/HMM/macro/garch 禁入回测）+ `df_in` 注入 + `combine_bias` 读 `cfg["combine_weights"]`（默认不变）+ 新 harness `oos_weight_validation.py`（IS/OOS 切分扫参，只报告不 apply）。**发现**：J 过拟合嫌疑但 OOS 仅 1 笔、combine_weights 非敏感（根因待 seasonal 簇权重入网格）；jd/FG/JM 负退化=regime 切换；lh/SA 降档后可出结论；默认权重 top3 内无需紧急改参。
