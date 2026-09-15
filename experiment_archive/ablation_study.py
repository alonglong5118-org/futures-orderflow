"""
全组合逐步优化对比（Ablation Study）

逐步添加 P0→P4 优化，量化每一步的增益：
  Baseline: 原始策略（等权，全品种）
  +P0: F 维度权重优化
  +P1: 退出机制优化（rr_ratio）
  +P2: Regime 风控参数优化
  +P3: 弱品种筛选（剔除负期望）
  +P4: 组合权重优化（凯利+约束）

指标：总收益 / 年化收益 / 最大回撤 / 夏普比 / 胜率 / 盈亏比
"""

import copy
import json
import os
import sys
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import (
    DEFAULT_CONFIG,
    load_daily,
    walk_forward_backtest,
)
from portfolio_manager import symbols_group, symbols_name


def make_baseline_config():
    """基线配置：关闭所有优化。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["portfolio"]["enabled"] = False
    # 注意：thresholds_by_symbol / per_symbol_risk / per_symbol_regime_coef
    # 中的优化值已经在 DEFAULT_CONFIG 中，需要手动"回退"它们。
    # 为了做 ablation，我们需要逐步移除这些优化。
    return cfg


def strip_p0(cfg):
    """移除 P0: F 维度权重优化 → 恢复默认 F=0.15"""
    c = copy.deepcopy(cfg)
    for sym, th in c.get("thresholds_by_symbol", {}).items():
        if "combine_weights" in th:
            # 恢复默认权重
            th["combine_weights"] = {"O": 0.35, "V": 0.25, "C": 0.25, "F": 0.15}
    return c


def strip_p1(cfg):
    """移除 P1: 退出机制优化 → rr_ratio 恢复默认 2.0"""
    c = copy.deepcopy(cfg)
    for sym, risk in c.get("per_symbol_risk", {}).items():
        if "rr_ratio" in risk and risk["rr_ratio"] > 2.0:
            # 有 P1 note 的才是优化过的
            if "note" in risk and "P1" in risk["note"]:
                risk["rr_ratio"] = 2.0
                risk.pop("note", None)
    return c


def strip_p2(cfg):
    """移除 P2: Regime 风控参数优化 → 移除 per_symbol_regime_coef"""
    c = copy.deepcopy(cfg)
    c["per_symbol_regime_coef"] = {}
    return c


def get_active_symbols_p3(cfg, include_weak=False):
    """P3 弱品种筛选：返回活跃品种列表。

    include_weak=True 时返回全品种（基线用）
    include_weak=False 时返回剔除弱品种后的列表
    """
    # 弱品种黑名单（expR < 0 或 数据不足的品种）
    weak_blacklist = {"au", "rr", "a", "m", "b", "RM"}

    all_syms = list(cfg.get("per_symbol_risk", {}).keys())
    if include_weak:
        return all_syms
    return [s for s in all_syms if s not in weak_blacklist]


def build_config_variant(variant):
    """构建不同阶段的配置。

    variant: "baseline" | "p0" | "p1" | "p2" | "p3" | "p4"
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if variant == "baseline":
        # 基线：所有优化全部关闭
        cfg = strip_p0(cfg)
        cfg = strip_p1(cfg)
        cfg = strip_p2(cfg)
        cfg["portfolio"]["enabled"] = False
        return cfg, get_active_symbols_p3(cfg, include_weak=True)

    elif variant == "p0":
        # +P0：开启 F 权重优化，其余关闭
        cfg = strip_p1(cfg)
        cfg = strip_p2(cfg)
        cfg["portfolio"]["enabled"] = False
        return cfg, get_active_symbols_p3(cfg, include_weak=True)

    elif variant == "p1":
        # +P1：P0 + 退出机制优化
        cfg = strip_p2(cfg)
        cfg["portfolio"]["enabled"] = False
        return cfg, get_active_symbols_p3(cfg, include_weak=True)

    elif variant == "p2":
        # +P2：P0+P1 + Regime 风控优化
        cfg["portfolio"]["enabled"] = False
        return cfg, get_active_symbols_p3(cfg, include_weak=True)

    elif variant == "p3":
        # +P3：P0+P1+P2 + 弱品种筛选
        cfg["portfolio"]["enabled"] = False
        return cfg, get_active_symbols_p3(cfg, include_weak=False)

    elif variant == "p4":
        # +P4：全部优化 + 组合权重
        # 加载部署配置中的权重
        deploy_path = os.path.join(HERE, "deploy", "trade_config_deploy.json")
        if os.path.exists(deploy_path):
            with open(deploy_path, encoding="utf-8") as f:
                deploy_cfg = json.load(f)
            cfg["portfolio"] = deploy_cfg.get("portfolio", cfg["portfolio"])
        else:
            cfg["portfolio"]["enabled"] = True
            cfg["portfolio"]["mode"] = "equal"
        active = cfg["portfolio"].get("active_symbols", [])
        if not active:
            active = get_active_symbols_p3(cfg, include_weak=False)
        return cfg, active

    else:
        raise ValueError(f"Unknown variant: {variant}")


def run_portfolio_backtest(cfg, symbols, window=200):
    """运行组合回测（等权假设，R 单位）。

    返回：组合净值曲线 + 各项指标
    """
    all_trades = []  # [(date_index, R_adj), ...]

    for sym in symbols:
        try:
            df = load_daily(sym)
            if df is None or len(df) < window + 50:
                continue
            r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=window)
            if r and r.get("trades_detail"):
                for t in r["trades_detail"]:
                    # 用 entry_bar 作为时间索引
                    all_trades.append((t.get("entry_bar", 0), t["R_adj"], sym))
        except Exception:
            continue

    if not all_trades:
        return None

    # 按时间排序
    all_trades.sort(key=lambda x: x[0])

    # 计算等权组合净值（R 累计）
    # 简单起见：每笔交易 R 直接累加（近似等权，因为各品种交易频率不同）
    n_syms = len(set(t[2] for t in all_trades))
    cumulative_R = []
    running = 0.0
    for _, r_adj, sym in all_trades:
        # 等权近似：每笔 R / n_syms
        running += r_adj / n_syms
        cumulative_R.append(running)

    # 计算指标
    Rs = [t[1] / n_syms for t in all_trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r < 0]

    total_R = sum(Rs)
    expR = float(np.mean(Rs)) if Rs else 0
    win_rate = len(wins) / len(Rs) if Rs else 0
    avg_win = float(np.mean(wins)) if wins else 0
    avg_loss = abs(float(np.mean(losses))) if losses else 1
    rr = avg_win / avg_loss if avg_loss > 0 else 0

    # 回撤
    cum = np.array(cumulative_R)
    running_max = np.maximum.accumulate(cum)
    drawdowns = running_max - cum
    max_dd = float(np.max(drawdowns))

    # 夏普比（R 单位，简化版）
    std_R = float(np.std(Rs)) if len(Rs) > 1 else 1
    # 假设日均交易笔数 ~ total_trades / (252*3年) ≈ 粗略估计
    # 更简单：用 expR / std_R 作为"单笔夏普"
    sharpe_per_trade = expR / std_R if std_R > 0 else 0

    # Calmar 比
    calmar = total_R / max_dd if max_dd > 0 else 0

    return {
        "n_symbols": n_syms,
        "total_trades": len(Rs),
        "total_R": round(total_R, 2),
        "expR": round(expR, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(rr * win_rate / (1 - win_rate), 3) if win_rate < 1 else 0,
        "avg_win_R": round(avg_win, 3),
        "avg_loss_R": round(avg_loss, 3),
        "max_dd_R": round(max_dd, 2),
        "sharpe_per_trade": round(sharpe_per_trade, 4),
        "calmar": round(calmar, 3),
        "final_cumulative_R": round(cumulative_R[-1], 2) if cumulative_R else 0,
        "cumulative_curve": cumulative_R,
        "trade_count_per_symbol": {},
    }


def compute_per_symbol_results(cfg, symbols, window=200):
    """计算每个品种的独立结果。"""
    results = {}
    for sym in symbols:
        try:
            df = load_daily(sym)
            if df is None or len(df) < window + 50:
                continue
            r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=window)
            if r:
                results[sym] = {
                    "expR": r.get("expR", 0),
                    "win_rate": r.get("win_rate", 0),
                    "trades": r.get("trades", 0),
                    "max_dd": r.get("max_dd", 0),
                }
        except Exception:
            continue
    return results


def main():
    print("=" * 70)
    print("全组合逐步优化对比（Ablation Study）")
    print("=" * 70)

    variants = ["baseline", "p0", "p1", "p2", "p3", "p4"]
    variant_names = {
        "baseline": "基线 (原始)",
        "p0": "+P0 F权重",
        "p1": "+P1 退出机制",
        "p2": "+P2 Regime风控",
        "p3": "+P3 弱品筛选",
        "p4": "+P4 组合优化",
    }

    results = {}
    all_symbols_sets = {}

    for i, v in enumerate(variants):
        print(f"\n[{i + 1}/{len(variants)}] {variant_names[v]} ...")

        cfg, active_syms = build_config_variant(v)
        all_symbols_sets[v] = active_syms
        print(f"  活跃品种: {len(active_syms)} 个")

        r = run_portfolio_backtest(cfg, active_syms)
        if r:
            results[v] = r
            print(f"  总收益: {r['total_R']:+.2f} R")
            print(f"  expR:   {r['expR']:+.4f}")
            print(f"  胜率:   {r['win_rate'] * 100:.1f}%")
            print(f"  最大回撤: {r['max_dd_R']:.2f} R")
            print(f"  Calmar: {r['calmar']:.2f}")
        else:
            print("  ⚠️  无交易数据")

    # 汇总对比表
    print(f"\n{'=' * 70}")
    print("  优化效果对比表")
    print(f"{'=' * 70}")
    print(
        f"  {'阶段':<16} {'品种':>4} {'交易':>5} {'总收益':>8} {'expR':>7} "
        f"{'胜率':>6} {'回撤':>7} {'Calmar':>7} {'提升':>7}"
    )
    print(f"  {'-' * 80}")

    baseline_total = results.get("baseline", {}).get("total_R", 1)
    for v in variants:
        if v not in results:
            continue
        r = results[v]
        improvement = (r["total_R"] - baseline_total) / abs(baseline_total) * 100 if baseline_total != 0 else 0
        print(
            f"  {variant_names[v]:<16} {r['n_symbols']:>4} {r['total_trades']:>5} "
            f"{r['total_R']:>+8.2f} {r['expR']:>+7.4f} "
            f"{r['win_rate'] * 100:>5.1f}% {r['max_dd_R']:>7.2f} "
            f"{r['calmar']:>7.2f} {improvement:>+6.1f}%"
        )

    # 边际增益
    print(f"\n{'=' * 70}")
    print("  边际增益（每一步新增收益）")
    print(f"{'=' * 70}")
    prev_total = 0
    for i, v in enumerate(variants):
        if v not in results:
            continue
        r = results[v]
        if i == 0:
            prev_total = r["total_R"]
            print(f"  {variant_names[v]:<16} 基准 = {r['total_R']:+.2f} R")
        else:
            marginal = r["total_R"] - prev_total
            pct = marginal / abs(prev_total) * 100 if prev_total != 0 else 0
            print(f"  → {variant_names[v]:<14} +{marginal:+.2f} R ({pct:+.1f}%)")
            prev_total = r["total_R"]

    # P4 单品种明细
    if "p4" in results:
        print(f"\n{'=' * 70}")
        print("  P4 最终各品种表现")
        print(f"{'=' * 70}")
        cfg_p4, syms_p4 = build_config_variant("p4")
        per_sym = compute_per_symbol_results(cfg_p4, syms_p4)
        # 按 expR 排序
        sorted_syms = sorted(per_sym.items(), key=lambda x: -x[1]["expR"])
        print(f"  {'排名':>3} {'品种':>5} {'名称':>6} {'板块':>6} {'expR':>7} {'胜率':>6} {'交易':>5} {'DD':>7}")
        print(f"  {'-' * 55}")
        for i, (sym, r) in enumerate(sorted_syms, 1):
            print(
                f"  {i:>3} {sym:>5} {symbols_name(sym):>6} {symbols_group(sym):>6} "
                f"{r['expR']:>+7.3f} {r['win_rate'] * 100:>5.1f}% "
                f"{r['trades']:>5} {r['max_dd']:>7.2f}"
            )

    # 保存结果
    print(f"\n{'=' * 70}")
    print("  保存结果")
    print(f"{'=' * 70}")
    os.makedirs("logs", exist_ok=True)

    # 序列化（去掉曲线数据）
    save_results = {}
    for v, r in results.items():
        save_r = {k: v for k, v in r.items() if k != "cumulative_curve"}
        save_results[v] = save_r

    output = {
        "date": datetime.now().isoformat(),
        "variants": list(results.keys()),
        "variant_names": variant_names,
        "results": save_results,
        "symbol_counts": {v: len(s) for v, s in all_symbols_sets.items()},
        "p4_per_symbol": per_sym if "p4" in results else {},
    }

    with open("logs/ablation_study.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print("  ✅ logs/ablation_study.json")

    # 总提升
    if "baseline" in results and "p4" in results:
        base = results["baseline"]["total_R"]
        final = results["p4"]["total_R"]
        total_improvement = (final - base) / abs(base) * 100 if base != 0 else 0
        print(f"\n  🎯 总提升: {base:+.2f} R → {final:+.2f} R ({total_improvement:+.1f}%)")
        print(f"  🎯 Calmar: {results['baseline']['calmar']:.2f} → {results['p4']['calmar']:.2f}")


if __name__ == "__main__":
    main()
