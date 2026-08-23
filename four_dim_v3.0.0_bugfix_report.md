# 四维策略模型 v3.0.0 只读审计 · Bug 报告 + 修复 Diff

> 审计方式：只读（未改任何文件、未重启 runner）。范围：18/18 模块 `py_compile` 通过；8 个新端点实测 `ok=True`；精读 `snapshot/record_trade/heal_from_journal`、`build_batch_orders/apply_batch_orders`、`broker_import.py` 全量、`fd.decompose_model_health`、`calibration.evaluate`、`compute_heat`；runner 日志无 traceback/error。
> **总评：系统整体健康，无崩溃级 bug。** 发现 1 个确凿逻辑/一致性 bug（违反合约一致性红线）+ 2 个可选加固项。
> **交付物**：`four_dim_v3.0.0_bugfix.patch`（确凿 bug 的干净统一 diff，trea 一键应用）+ 本报告（含可选加固代码）。

---

## 一、确凿 Bug：账户总览空仓品种合约用了陈旧值（违反合约一致性红线）

- **位置**：`account_tracker.py` → `snapshot()` → 空仓分支（约 477 行）
- **现象**：账户总览里**没持仓**的品种（约 48 个）显示的合约来自 `contract_specs`（手工维护、换月后陈旧），而**有持仓**的品种（约 6 个）走 `_authoritative_contract`（main_overrides 权威源，正确）。两边不一致即此 bug——空仓品种会显示旧月合约（如玉米已换 2611，空仓分支仍显示 2609）。
- **根因**：同一 `snapshot()` 内，持仓分支用了 `_authoritative_contract`，空仓分支漏了，直接 `sp.get("contract", sym)`。
- **修复**：见 `four_dim_v3.0.0_bugfix.patch`（已生成，trea 直接 `git apply` 即可）。

```python
# 修复前（account_tracker.py 约 477 行，空仓分支）：
            positions.append({
                "symbol": sym, "name": sp.get("name", sym),
                "contract": sp.get("contract", sym),          # ← BUG：contract_specs 陈旧合约
                "direction": "—", "lots": 0, "avg": None, "price": px,
                ...

# 修复后：与持仓分支保持一致，走 _authoritative_contract
            positions.append({
                "symbol": sym, "name": sp.get("name", sym),
                "contract": _authoritative_contract(sym, sp.get("contract", sym)),  # ← 修复
                "direction": "—", "lots": 0, "avg": None, "price": px,
                ...
```
> `_authoritative_contract` 在本文件 31 行已定义、持仓分支 460 行已在用，无需新增 import。

---

## 二、可选加固项（非 bug，低优先级，按需让 trea 做）

### 加固 1：`cross_source_check` 极端低波动日阈值偏松
- **位置**：`four_dim_live_runner.py` → `cross_source_check()` 约 5333 行
- **现状**：`thr = max(RT_DAILY_GAP_WARN, 2.0 * amp)`，当当日振幅 `amp` 极小时只取 `RT_DAILY_GAP_WARN`（默认 6%），可能漏报小幅但持续的合约错。
- **修复代码**（加一个绝对下限 3%）：

```python
# 修复前：
            thr = max(RT_DAILY_GAP_WARN, 2.0 * amp)
# 修复后：
            thr = max(RT_DAILY_GAP_WARN, 1.5 * amp, 3.0)
```

### 加固 2：`heal_from_journal` 开仓均价只取首笔，忽略加仓
- **位置**：`account_tracker.py` → `heal_from_journal()` 约 388–401 行
- **现状**：`if k not in javg:` 逻辑下，同一 (品种,方向) 有多笔加仓(open)时只保留第一笔 `entry_price`，后续加仓均价不被纳入自愈。
- **修复代码**（按手数加权均价，且保持 `javg` 为数值型以兼容 414 行下游消费）：

```python
# 修复前（约 388–401 行）：
    javg = {}
    jlevels = {}  # (sym, direction) -> {"stop":..., "stop_dist":...}
    for t in jdata.get("trades", []):
        if t.get("pnl") is not None:
            continue
        k = (t.get("symbol"), t.get("direction"))
        if k not in javg:
            javg[k] = t.get("entry_price")
            jlevels[k] = {
                "stop": t.get("stop"),
                "stop_dist": t.get("stop_dist"),
                "t1": t.get("t1"),
                "t2": t.get("t2"),
            }

# 修复后（按手数加权均价，javg 仍为数值型，下游 414 行无需改动）：
    javg_acc = {}  # (sym, direction) -> {"sum": 价格*手数, "qty": 手数}
    jlevels = {}  # (sym, direction) -> {"stop":..., "stop_dist":...}
    for t in jdata.get("trades", []):
        if t.get("pnl") is not None:
            continue
        k = (t.get("symbol"), t.get("direction"))
        qty = abs(t.get("quantity") or t.get("lots") or 0) or 1
        if k not in javg_acc:
            javg_acc[k] = {"sum": 0.0, "qty": 0.0}
            jlevels[k] = {
                "stop": t.get("stop"),
                "stop_dist": t.get("stop_dist"),
                "t1": t.get("t1"),
                "t2": t.get("t2"),
            }
        javg_acc[k]["sum"] += (t.get("entry_price") or 0) * qty
        javg_acc[k]["qty"] += qty
    javg = {k: round(v["sum"] / v["qty"], 2) if v["qty"] else 0.0
            for k, v in javg_acc.items()}
```

---

## 三、应用与验证（交给 trea）

1. 应用 patch：`cd /Users/ken/WorkBuddy/futures-orderflow && git apply four_dim_v3.0.0_bugfix.patch`（可选加固项按需在单独文件/手动编辑落地）。
2. 重启 runner：`launchctl kickstart -k gui/502/com.ken.futures-orderflow.live`。
3. 验证：打 `GET /api/account`，确认**所有品种**（含空仓）的 `contract` 字段 == `main_overrides.json` 中对应主力；与 `/api/state` 的 `symbols[].contract` 完全一致。
4. 回归：打 `GET /api/health`、`/api/consistency` 确认无新异常。
