#!/usr/bin/env python3
"""金字塔影子模式验证（P2，2026-08-31）。

场景：
1. 三重门门控：全过 → 推荐；逐门拒绝（黑名单/灰名单/趋势末/均值标签）
2. 阶梯数学：触发价、止损上移、峰值手数、保本结构
3. 实盘信号回放：four_dim_signals.json 近期信号 × 当前状态引擎 → 哪些会触发推荐
4. 影子日志读写
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pyramid_addon as pyr


def show(rec, tag):
    if rec is None:
        print(f"  {tag}: 不推荐（走原 T1/T2）✓")
    else:
        l1, l2 = rec["ladder"]
        print(f"  {tag}: 推荐金字塔（{rec['market_state']} × {rec['strategy_label']}）")
        print(f"    P1: {l1['trigger_price']} 加 {l1['add_lots']} 手, 止损→{l1['new_stop_price']}")
        print(f"    P2: {l2['trigger_price']} 加 {l2['add_lots']} 手, 止损→{l2['new_stop_price']}")
        print(f"    峰值 {rec['peak_lots']} 手（首仓 {rec['base_lots']}）")


print("=" * 66)
print("场景 1：三重门门控")
print("=" * 66)
base = dict(direction="多", entry_price=4000.0, stop_dist=40.0, lots=4)

# 1a. 全过：l 塑料（v2 白名单） + 趋势中期 + 趋势标签
rec = pyr.evaluate(symbol="l", market_state="trend_mid", strategy_label="趋势", **base)
show(rec, "l+trend_mid+趋势（应推荐）")
assert rec is not None and rec["mode"] == "pyramid"

# 1b. 黑名单：UR（EV -0.071）
rec = pyr.evaluate(symbol="UR", market_state="trend_mid", strategy_label="趋势", **base)
show(rec, "UR+trend_mid+趋势（黑名单，应拒）")
assert rec is None

# 1b2. OOS 剔除：cu（模块级 2/5 折，2026-08-31 剔除）
rec = pyr.evaluate(symbol="cu", market_state="trend_mid", strategy_label="趋势", **base)
show(rec, "cu+trend_mid+趋势（OOS 剔除，应拒）")
assert rec is None

# 1c. 灰名单：rb（未验证）
rec = pyr.evaluate(symbol="rb", market_state="trend_mid", strategy_label="趋势", **base)
show(rec, "rb+trend_mid+趋势（灰名单，应拒）")
assert rec is None

# 1d. 行情门：趋势末
rec = pyr.evaluate(symbol="l", market_state="trend_late", strategy_label="趋势", **base)
show(rec, "l+trend_late+趋势（趋势末，应拒）")
assert rec is None

# 1e. 行情门：震荡
rec = pyr.evaluate(symbol="l", market_state="sideways", strategy_label="趋势", **base)
show(rec, "l+sideways+趋势（震荡，应拒）")
assert rec is None

# 1f. 标签门：均值回归
rec = pyr.evaluate(symbol="l", market_state="trend_mid", strategy_label="均值回归", **base)
show(rec, "l+trend_mid+均值回归（零边际，应拒）")
assert rec is None

# 1g. 标签门：背离（最强标签）
rec = pyr.evaluate(symbol="sp", market_state="trend_early", strategy_label="背离", **base)
show(rec, "sp+trend_early+背离（应推荐）")
assert rec is not None
print()

print("=" * 66)
print("场景 2：阶梯数学（l 塑料 多 @4000, stop_dist=40, 首仓 4 手）")
print("=" * 66)
rec = pyr.evaluate(symbol="l", market_state="trend_mid", strategy_label="趋势", **base)
l1, l2 = rec["ladder"]
print(f"  P1: 触发 {l1['trigger_price']}（4000+1.0R×40=4040 ✓）, 加 {l1['add_lots']} 手（4×50%=2 ✓）, 止损 {l1['new_stop_price']}（保本 4000 ✓）")
print(f"  P2: 触发 {l2['trigger_price']}（4000+1.5R×40=4060 ✓）, 加 {l2['add_lots']} 手（4×25%=1 ✓）, 止损 {l2['new_stop_price']}（+0.5R=4020 ✓）")
print(f"  峰值 {rec['peak_lots']} 手（4+2+1=7 ✓）")
print(f"  移动止损起点 {rec['trail']['start_price']}（+2.0R=4080）, 回撤距离 {rec['trail']['dist_r']}R")
assert l1["trigger_price"] == 4040.0 and l1["add_lots"] == 2 and l1["new_stop_price"] == 4000.0
assert l2["trigger_price"] == 4060.0 and l2["add_lots"] == 1 and l2["new_stop_price"] == 4020.0
assert rec["peak_lots"] == 7

# 空头镜像
rec_s = pyr.evaluate(symbol="SR", direction="空", entry_price=4000.0, stop_dist=40.0,
                     lots=4, market_state="trend_mid", strategy_label="趋势")
ls1, ls2 = rec_s["ladder"]
print(f"  空头镜像: P1 触发 {ls1['trigger_price']}（3960 ✓）, 止损 {ls1['new_stop_price']}（4000 ✓）")
assert ls1["trigger_price"] == 3960.0 and ls1["new_stop_price"] == 4000.0

# 保本数学：P2 后全打 +0.5R 止损
# 首仓 4 手 × +0.5R = +2.0R；P1 2 手 × (1.0-0.5)R = -1.0R；P2 1 手 × (1.5-0.5)R = -1.0R
breakeven_r = 4 * 0.5 - 2 * 0.5 - 1 * 1.0
print(f"  保本数学: P2 后全打 +0.5R 止损 → {breakeven_r:+.1f}R（≈0 保本 ✓）")
assert abs(breakeven_r) < 0.01
print()

print("=" * 66)
print("场景 3：实盘信号回放（four_dim_signals.json × 白名单）")
print("=" * 66)
with open(Path(__file__).parent / "four_dim_signals.json", encoding="utf-8") as f:
    sigs = json.load(f)
whitelist_hits = []
for s in sigs:
    sym = s.get("symbol")
    if sym not in pyr.PYRAMID_WHITELIST:
        continue
    whitelist_hits.append(s)
    # 信号侧信息（历史信号无 strategy 标签，用回算判断留待实盘验证）
    print(f"  {s.get('time', '?')[:16]} {sym:<4} {s.get('direction')} @ {s.get('price')} "
          f"stop={s.get('stop')} t1={s.get('t1')} t2={s.get('t2')}")
print(f"  白名单品种信号 {len(whitelist_hits)}/{len(sigs)} 条——这些是金字塔候选池的实盘信号")
print(f"  （历史信号无 strategy 标签与实时状态，实际推荐在实盘信号产生时判定）")
print()

print("=" * 66)
print("场景 4：影子日志")
print("=" * 66)
rec = pyr.evaluate(symbol="FG", market_state="trend_early", strategy_label="趋势", **base)
entry = pyr.log_shadow(rec, signal_time="2026-08-31 10:00:00")
print(f"  写入: {entry['symbol']} {entry['direction']} @ {entry['entry_ref']} ladder={len(entry['ladder'])} 档")
with open(pyr.SHADOW_LOG, encoding="utf-8") as f:
    logs = json.load(f)
print(f"  日志文件 {len(logs)} 条 ✓")
# 清理测试日志（保持影子日志干净，等实盘真实写入）
logs = [l for l in logs if l.get("signal_time") != "2026-08-31 10:00:00"]
with open(pyr.SHADOW_LOG, "w", encoding="utf-8") as f:
    json.dump(logs, f, ensure_ascii=False, indent=2)
print(f"  测试条目已清理，日志 {len(logs)} 条")
print()

print("全部场景通过 ✓")
