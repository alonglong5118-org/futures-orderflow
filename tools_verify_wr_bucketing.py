#!/usr/bin/env python3
"""胜率反馈分桶（P2）验证脚本。

场景：
1. 分桶核心行为：趋势单 33.5%（基线）→ 应中性 ~50 分，不再是旧公式的 ~27 分（结构性错杀修复）
2. 均值单 60% vs 趋势单 33.5% 混桶 vs 分桶：旧公式混桶会互相污染，新公式各自按基线计分
3. 真实数据链路：load_journal_trades_for_perf() 读 14 笔带标签成交 → calc_performance_score
4. 回归：无标签成交 → 全局基线行为（与设计一致）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# live_runner 导入重（含 HTTP server 模块），只导入目标函数所需的定义环境
import importlib.util

spec = importlib.util.spec_from_file_location(
    "fdr", Path(__file__).parent / "four_dim_live_runner.py"
)


def load_target_funcs():
    """从 four_dim_live_runner.py 提取目标函数（避免完整启动 server）。"""
    import json, os, re
    import trade_journal as tj

    src = (Path(__file__).parent / "four_dim_live_runner.py").read_text(encoding="utf-8")

    ns = {"json": json, "os": os, "tj": tj}

    # 常量块：从 PERF_WINDOW_SHORT 到状态判定阈值前整块 exec（含多行 dict）
    c_start = src.index("PERF_WINDOW_SHORT =")
    c_end = src.index("# ── 状态判定阈值（技术面得分 0-100） ──")
    # 追加两个 level 阈值（在后面定义）
    m_good = re.search(r"^PERF_SCORE_GOOD_MIN\s*=\s*(\d+)", src, re.M)
    m_mid = re.search(r"^PERF_SCORE_MID_MIN\s*=\s*(\d+)", src, re.M)
    exec(compile(src[c_start:c_end], "<consts>", "exec"), ns)
    ns["PERF_SCORE_GOOD_MIN"] = int(m_good.group(1))
    ns["PERF_SCORE_MID_MIN"] = int(m_mid.group(1))

    # 函数体提取（calc_performance_score + load_journal_trades_for_perf）
    for fname in ["calc_performance_score", "load_journal_trades_for_perf"]:
        start = src.index(f"def {fname}(")
        rest = src[start:]
        lines = rest.split("\n")
        body = [lines[0]]
        for ln in lines[1:]:
            if ln.startswith("def ") or (ln and not ln[0].isspace()):
                break
            body.append(ln)
        code = "\n".join(body)
        exec(compile(code, f"<{fname}>", "exec"), ns)
    ns["HERE"] = str(Path(__file__).parent)
    return ns


ns = load_target_funcs()
calc_performance_score = ns["calc_performance_score"]
load_journal_trades_for_perf = ns["load_journal_trades_for_perf"]


def old_winrate_score(win_rate):
    """旧公式（绝对阈值）——对照用。"""
    if win_rate > 60:
        return 80 + min(20, (win_rate - 60) * 2)
    elif win_rate > 45:
        return 50 + (win_rate - 45) / 15 * 30
    elif win_rate > 30:
        return 20 + (win_rate - 30) / 15 * 30
    else:
        return max(0, 20 - (30 - win_rate) * 1.5)


def mk(n, wins, strategy, r_win=2.0, r_lose=-1.0):
    """造 n 笔成交，前 wins 笔盈利。"""
    out = []
    for i in range(n):
        out.append({
            "symbol": "test", "win": i < wins, "r_result": r_win if i < wins else r_lose,
            "strategy": strategy,
        })
    return out


print("=" * 66)
print("场景 1：趋势单基线表现（WR 33.3% / PF 1.33 / 间隔胜负）——不应触发防御")
print("=" * 66)
# 趋势基线结构：4 胜×2.2R + 8 亏×1R → PF=1.33（回测趋势标签基线），间隔排列避免连亏
trades = [
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": True, "r_result": 2.2, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": True, "r_result": 2.2, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": True, "r_result": 2.2, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": True, "r_result": 2.2, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
    {"symbol": "t", "win": False, "r_result": -1.0, "strategy": "趋势"},
]
res = calc_performance_score(trades)
print(f"趋势桶 12 笔 4 胜（33.3%，基线 33.5%），PF=1.33（基线 1.33）:")
print(f"  新 winrate_score = {res['metrics']['winrate_score']}（中性 ≈50）")
print(f"  旧公式 winrate_score = {old_winrate_score(33.3):.1f}（结构性错杀）")
print(f"  总分 {res['total_score']} → level = {res['level']}（基线表现应 mid，不应 poor）")
assert res["metrics"]["winrate_score"] > 45, "基线胜率应得中性分"
assert res["level"] != "poor", "趋势策略基线表现不应触发防御"
print()

print("=" * 66)
print("场景 2：均值单 60% vs 趋势单 33%——混桶 vs 分桶")
print("=" * 66)
mixed = mk(10, 6, "均值回归") + mk(10, 3, "趋势")  # 混合 45%
res_mixed = calc_performance_score(mixed)
mixed_wr = 9 / 20 * 100
print(f"混合 20 笔（均值 60% + 趋势 30%）:")
print(f"  旧公式（混桶 45%）winrate_score = {old_winrate_score(mixed_wr):.1f}")
print(f"  新公式 winrate_score = {res_mixed['metrics']['winrate_score']}")
for label, b in res_mixed["metrics"]["winrate_buckets"].items():
    print(f"  桶[{label}]: n={b['n']}, WR={b['win_rate']}%, 基线={b['baseline']}%, 偏离={b['dev']:+}pp, 分={b['score']}")
print()

print("=" * 66)
print("场景 3：真实数据链路（journal 14 笔带标签 → perf）")
print("=" * 66)
jt = load_journal_trades_for_perf()
print(f"journal 加载: {len(jt)} 笔")
by_strat = {}
for t in jt:
    by_strat.setdefault(t["strategy"], []).append(t)
for s, ts in by_strat.items():
    wr = sum(1 for t in ts if t["win"]) / len(ts) * 100
    rs = [t["r_result"] for t in ts]
    print(f"  [{s}] n={len(ts)}, 胜率={wr:.0f}%, R范围=[{min(rs):+.2f}, {max(rs):+.2f}]")
# 全品种合并算一次（状态引擎是按品种的，这里看整体）
res_real = calc_performance_score(jt)
print(f"  全体 {len(jt)} 笔已平仓（另 5 笔 pnl 缺失跳过）: total={res_real['total_score']}, level={res_real['level']}")
for label, b in res_real["metrics"]["winrate_buckets"].items():
    print(f"  桶[{label}]: n={b['n']}, WR={b['win_rate']}%, 基线={b['baseline']}%, 分={b['score']}")
print()

print("=" * 66)
print("场景 4：回归——无标签成交走全局基线")
print("=" * 66)
no_label = mk(10, 4, None)  # 40% 无标签
res_nl = calc_performance_score(no_label)
print(f"无标签 10 笔 4 胜（40%，全局基线 34%）:")
print(f"  winrate_score = {res_nl['metrics']['winrate_score']}（偏离 +6pp → 应 ≈65）")
assert 60 < res_nl["metrics"]["winrate_score"] < 70
print()

print("=" * 66)
print("场景 5：样本不足桶并入无标签（<3 笔）")
print("=" * 66)
sparse = mk(10, 4, "趋势") + mk(2, 0, "均值回归")  # 均值桶只有2笔 → 并入无标签
res_sp = calc_performance_score(sparse)
labels = list(res_sp["metrics"]["winrate_buckets"].keys())
print(f"桶列表: {labels}（均值回归不应单独成桶）")
assert "均值回归" not in labels
print()

print("全部场景通过 ✓")
