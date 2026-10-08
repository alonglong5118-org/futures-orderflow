# M2 引擎侧可复现性定案（2026-10-08）

> P2 收口文档。针对「M2 `four_dim_strategy.py` 回测可复现性」与「孤儿长历史资产」两项的**最终定案**。
> 所有结论基于实测（读码 + 文件系统核查），非文字报告推断。

## §1 C（订单流）维度恒为 0 的根因（已实测）

`four_dim_strategy.py` 的 `precompute_C_array`（L1859）/ `score_C`（L1820）/ `get_kline_C`（L2004）
两源都「缺数据即返回 `0.0`」：

- **dragon 源（默认）**：依赖 `cpos_cache.json`（龙虎榜历史代理）。
  实测：`*dragon*` / `*longhu*` 数据文件**均不存在** → 全品种 C = 0。
- **kline 源**：依赖 `cflow_kline_cache.json`（17 年 K 线推导资金流）。
  实测：`cflow_kline_cache.json` **不存在**（No such file）→ 全品种 C = 0。
  （架构文档 `docs/architecture/orderflow-layering-and-arbitration.md:108-110` 同载：
   FG/SA/jd/lh/cu 两源非零个数全部 = 0。）

> 即：历史回测里 C 维度**物理上不可得**，不是参数 bug，是数据源缺失。

## §2 唯一「补 C」路径的代价与陷阱

`experiment_archive/build_kline_cflow_all.py` 可造 `cflow_kline_cache.json`，
但它依赖 **M4 桌面 17 年 1 分钟 K 线（8.3GB，仅 M4）**。

⚠️ 关键约束（记忆已载，2026-10-04 实测）：**该 1 分钟数据是 OHLC K 线，无盘口量** →
「订单流不可用（盘口量分母不存在）」。造出的 C 是 **K 线代理 imbalance**，并非真实盘口资金流，
与「订单流 edge」的定义不符，且会制造「C 已可用」的虚假安全感。

**定案**：**不**为补历史 C 而跑 `build_kline_cflow_all.py`。理由：
1. 产出是 K 线代理非真盘口，证据口径不成立；
2. 需跨机搬运 8.3GB + 重算，性价比低；
3. 与「证据一律实测、不可复现需先穷举口径」纪律相悖。

## §3 定案结论（M2 历史回测的边界）

- **M2 `four_dim_strategy.py` 历史回测 = F + T 两维**（F=基本面/fundamentals，T=技术/价格行为）。
  C（订单流）维度在历史回测中**恒为 0、不计入**，任何标榜「四维」的历史 OOS 数字实质是 F+T。
- **C 维度的验证职责，转移到前向实盘 edge 测量**（已建、已预注册）：
  - 主终点：FG+SA 真实 tick 源1（delta/吸收/失衡），45 交易日判定；
  - 次要终点 S1：源2 18 品种纯 imbalance，127 交易日判定。
  - 这恰是 edge 预注册要做的事，与 M2 引擎互不影响。

## §4 孤儿长历史资产处置（data_5m_long / data_daily_long，75 品种）

- 资产：`futures-orderflow/data_5m_long`(665M) + `data_daily_long`(13M)，各 75 品种，
  由 `build_ohlcv_long.py` 从 M4 17 年 1 分钟主力连续重建；已加 `.gitignore`（不入库）。
- 用途：喂 **M2 `four_dim_strategy.py` 引擎**做 F+T 长历史 OOS（54→75 品种、尾部至 2026-08-28）。
  **注意：与闸门引擎无关**（闸门引擎读 M4 `data/history/*.parquet`，已独立接入 17 年）。
- **定案**：保留为**可重建资产**（gitignore + 有 `build_ohlcv_long.py`），**当前不消费、不阻塞**。
  若未来要做 F+T 长历史 OOS，用它即可；但须先解决 F 维度 `fundamentals.json` 的快照复现（已落地
  `snapshot_inputs.py`）以确保可复现。

## §5 整体判定

- M2 引擎「四维回测」实为 **F+T 两维**，这是结构性事实，非待修 bug。
- 「四维」的 C 维度证据来自**前向实盘**（edge harness），不在历史回测。
- 4 份历史 OOS 报告「全部不可复现」的根因在此 + F 维度未快照（已补）；旧报告数字**一律作废**，
  不可作为任何上线/决策依据。新结论一律以「F+T 历史 + 前向 edge 测量」为准。

---
*定案：2026-10-08 · 基于读码（four_dim_strategy.py L1820-1905/2004-2024）+ 文件系统核查（cflow/cpos 缓存均缺失）。*
