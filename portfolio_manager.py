"""
组合管理模块：权重分配 + 风险预算 + 相关性监控

功能：
  1. portfolio_weight(symbol, cfg) - 获取某品种的组合权重
  2. portfolio_risk_budget(symbol, cfg) - 计算某品种的风险预算（占总风险的比例）
  3. sector_exposure_check(symbol, current_positions, cfg) - 板块集中度检查
  4. correlation_watch - 高相关对持仓监控
  5. rebalance_suggestion - 再平衡建议
"""

import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS


def symbols_group(sym):
    return SYMBOLS.get(sym, {}).get("group", "其他")


def symbols_name(sym):
    return SYMBOLS.get(sym, {}).get("name", sym)


# ── 权重解析 ────────────────────────────────────────────────────────────────
def portfolio_weight(symbol, cfg=DEFAULT_CONFIG):
    """获取某品种的组合权重。

    优先级：
      1. portfolio.weights[symbol] - 显式配置
      2. portfolio.mode == "equal" - 等权
      3. portfolio.mode == "kelly" - 凯利（需 per_symbol_risk 中 expR/win_rate）
      4. 默认 1/N（N = 活跃品种数）

    返回 float（0~1）
    """
    pf = cfg.get("portfolio", {})
    if not pf.get("enabled", False):
        return 1.0 / max(len(cfg.get("per_symbol_risk", {})), 1)  # 近似等权

    # 显式权重优先
    weights = pf.get("weights", {})
    if symbol in weights:
        return float(weights[symbol])

    mode = pf.get("mode", "equal")
    active_syms = pf.get("active_symbols", list(cfg.get("per_symbol_risk", {}).keys()))
    n = max(len(active_syms), 1)

    if mode == "equal":
        return 1.0 / n if symbol in active_syms else 0.0

    # 其他模式回落等权
    return 1.0 / n if symbol in active_syms else 0.0


def portfolio_risk_mult(symbol, cfg=DEFAULT_CONFIG):
    """组合权重换算为风险倍率。

    基准：等权时 risk_mult = 1.0（即每品种风险预算相同）
    若某品种权重是等权的 1.5 倍 → risk_mult = 1.5 → 仓位 × 1.5

    返回 float，默认 1.0
    """
    pf = cfg.get("portfolio", {})
    if not pf.get("enabled", False):
        return 1.0

    w = portfolio_weight(symbol, cfg)
    active_syms = pf.get("active_symbols", list(cfg.get("per_symbol_risk", {}).keys()))
    n = max(len(active_syms), 1)
    equal_w = 1.0 / n

    if equal_w <= 0:
        return 1.0

    # 权重倍率 = 实际权重 / 等权权重
    mult = w / equal_w

    # 限制范围 [0.3, 2.0]，避免极端
    max_mult = pf.get("max_weight_mult", 2.0)
    min_mult = pf.get("min_weight_mult", 0.3)
    return max(min_mult, min(max_mult, mult))


# ── 板块集中度检查 ──────────────────────────────────────────────────────────
def sector_exposure(current_positions, cfg=DEFAULT_CONFIG):
    """计算当前持仓的板块暴露度。

    current_positions: dict {symbol: {"lots": int, "direction": int, "notional": float}}

    返回: {
        "by_sector": {sector: {"long_lots", "short_lots", "net_lots", "notional", "weight"}},
        "total_notional": float,
        "max_sector_weight": float,
        "max_sector": str,
        "concentration_ok": bool,
    }
    """
    pf = cfg.get("portfolio", {})
    max_sector_w = pf.get("max_sector_weight", 0.35)  # 默认 35%

    by_sector = {}
    total_notional = 0.0

    for sym, pos in current_positions.items():
        sec = symbols_group(sym)
        notional = pos.get("notional", 0)
        direction = pos.get("direction", 0)
        lots = pos.get("lots", 0)

        if sec not in by_sector:
            by_sector[sec] = {"long_lots": 0, "short_lots": 0, "net_lots": 0, "notional": 0.0}

        if direction > 0:
            by_sector[sec]["long_lots"] += lots
        elif direction < 0:
            by_sector[sec]["short_lots"] += lots

        by_sector[sec]["net_lots"] += lots * direction
        by_sector[sec]["notional"] += notional
        total_notional += notional

    # 计算权重
    for sec in by_sector:
        by_sector[sec]["weight"] = by_sector[sec]["notional"] / total_notional if total_notional > 0 else 0

    max_sector = max(by_sector.keys(), key=lambda s: by_sector[s]["notional"]) if by_sector else ""
    max_sector_w_actual = by_sector[max_sector]["weight"] if max_sector else 0

    return {
        "by_sector": by_sector,
        "total_notional": total_notional,
        "max_sector_weight": round(max_sector_w_actual, 4),
        "max_sector": max_sector,
        "concentration_ok": max_sector_w_actual <= max_sector_w,
        "threshold": max_sector_w,
    }


# ── 高相关对监控 ────────────────────────────────────────────────────────────
def check_correlation_risk(current_positions, cfg=DEFAULT_CONFIG):
    """检查高相关对同向持仓风险。

    返回: {
        "high_risk_pairs": [{"pair": (s1, s2), "corr": float, "s1_dir": int, "s2_dir": int, "risk": "high"|"medium"}],
        "total_pairs": int,
        "risk_level": "high"|"medium"|"low",
    }
    """
    pf = cfg.get("portfolio", {})
    high_corr_threshold = pf.get("high_corr_threshold", 0.7)
    corr_matrix = pf.get("corr_matrix", {})

    if not corr_matrix:
        return {"high_risk_pairs": [], "total_pairs": 0, "risk_level": "low"}

    pos_syms = list(current_positions.keys())
    high_risk = []

    for i, s1 in enumerate(pos_syms):
        for s2 in pos_syms[i + 1 :]:
            corr = corr_matrix.get(s1, {}).get(s2, corr_matrix.get(s2, {}).get(s1, 0))
            if abs(corr) >= high_corr_threshold:
                d1 = current_positions[s1].get("direction", 0)
                d2 = current_positions[s2].get("direction", 0)

                # 正相关且同向 → 集中度风险
                if corr > 0 and d1 * d2 > 0:
                    risk = "high" if abs(corr) >= 0.85 else "medium"
                    high_risk.append(
                        {
                            "pair": (s1, s2),
                            "corr": corr,
                            "s1_dir": d1,
                            "s2_dir": d2,
                            "risk": risk,
                            "type": "concentration",  # 同向高相关 = 集中度风险
                        }
                    )
                # 负相关且反向 → 对冲（低风险）
                elif corr < 0 and d1 * d2 < 0:
                    pass  # 对冲，无风险

    risk_level = "low"
    high_count = sum(1 for p in high_risk if p["risk"] == "high")
    if high_count >= 3:
        risk_level = "high"
    elif high_count >= 1 or len(high_risk) >= 3:
        risk_level = "medium"

    return {
        "high_risk_pairs": high_risk,
        "total_pairs": len(high_risk),
        "risk_level": risk_level,
    }


# ── 再平衡建议 ──────────────────────────────────────────────────────────────
def rebalance_suggestion(current_positions, cfg=DEFAULT_CONFIG):
    """生成再平衡建议。

    比较当前持仓权重 vs 目标权重，输出需要调仓的品种。
    """
    pf = cfg.get("portfolio", {})
    if not pf.get("enabled", False):
        return {"needs_rebalance": False, "suggestions": []}

    target_weights = pf.get("weights", {})
    rebalance_threshold = pf.get("rebalance_threshold", 0.05)  # 偏离 5% 触发

    # 计算当前权重
    total_notional = sum(p.get("notional", 0) for p in current_positions.values())
    if total_notional <= 0:
        return {"needs_rebalance": False, "suggestions": [], "reason": "无持仓"}

    suggestions = []
    for sym, target_w in target_weights.items():
        current_w = current_positions.get(sym, {}).get("notional", 0) / total_notional
        deviation = current_w - target_w

        if abs(deviation) > rebalance_threshold:
            action = "减仓" if deviation > 0 else "加仓"
            suggestions.append(
                {
                    "symbol": sym,
                    "name": symbols_name(sym),
                    "current_weight": round(current_w, 4),
                    "target_weight": round(target_w, 4),
                    "deviation": round(deviation, 4),
                    "action": action,
                    "adjust_pct": round(abs(deviation) * 100, 2),
                }
            )

    # 检查不在目标中但有持仓的品种
    for sym in current_positions:
        if sym not in target_weights:
            suggestions.append(
                {
                    "symbol": sym,
                    "name": symbols_name(sym),
                    "current_weight": round(current_positions[sym].get("notional", 0) / total_notional, 4),
                    "target_weight": 0,
                    "deviation": round(current_positions[sym].get("notional", 0) / total_notional, 4),
                    "action": "清仓",
                    "adjust_pct": round(current_positions[sym].get("notional", 0) / total_notional * 100, 2),
                }
            )

    suggestions.sort(key=lambda x: abs(x["deviation"]), reverse=True)

    return {
        "needs_rebalance": len(suggestions) > 0,
        "suggestions": suggestions,
        "total_deviation": round(sum(abs(s["deviation"]) for s in suggestions) / 2, 4),
    }


# ── 组合风险预算 → 仓位调整 ──────────────────────────────────────────────────
def apply_portfolio_risk(symbol, base_lots, cfg=DEFAULT_CONFIG):
    """将组合风险预算应用到基础仓位。

    base_lots: risk_gate 计算出的基础手数
    返回: 调整后的手数（int）
    """
    mult = portfolio_risk_mult(symbol, cfg)
    return max(0, int(round(base_lots * mult)))


# ── 诊断报告 ────────────────────────────────────────────────────────────────
def portfolio_diagnostic(current_positions, cfg=DEFAULT_CONFIG):
    """生成组合诊断报告。"""
    pf = cfg.get("portfolio", {})
    enabled = pf.get("enabled", False)

    report = {
        "enabled": enabled,
        "mode": pf.get("mode", "equal"),
        "n_active": len(pf.get("active_symbols", [])),
    }

    if not enabled or not current_positions:
        return report

    # 板块暴露
    sector_info = sector_exposure(current_positions, cfg)
    report["sector_exposure"] = sector_info

    # 相关性风险
    corr_info = check_correlation_risk(current_positions, cfg)
    report["correlation_risk"] = corr_info

    # 再平衡建议
    rebal = rebalance_suggestion(current_positions, cfg)
    report["rebalance"] = rebal

    # 综合评分
    score = 100
    if not sector_info["concentration_ok"]:
        score -= 20
    if corr_info["risk_level"] == "high":
        score -= 25
    elif corr_info["risk_level"] == "medium":
        score -= 10
    if rebal["needs_rebalance"] and rebal["total_deviation"] > 0.15:
        score -= 15

    report["health_score"] = max(0, score)
    report["health_level"] = "健康" if score >= 80 else ("注意" if score >= 60 else "警告")

    return report


# ── 快速验证 ────────────────────────────────────────────────────────────────
def main():
    """快速验证组合管理模块功能。"""
    print("=" * 60)
    print("组合管理模块功能验证")
    print("=" * 60)

    # 测试配置
    test_cfg = copy.deepcopy(DEFAULT_CONFIG)
    test_cfg["portfolio"] = {
        "enabled": True,
        "mode": "kelly",
        "max_sector_weight": 0.35,
        "high_corr_threshold": 0.7,
        "rebalance_threshold": 0.05,
        "max_weight_mult": 2.0,
        "min_weight_mult": 0.3,
        "active_symbols": ["cu", "al", "rb", "MA", "CF", "y", "pp"],
        "weights": {
            "cu": 0.15,
            "al": 0.12,
            "rb": 0.10,
            "MA": 0.08,
            "CF": 0.10,
            "y": 0.10,
            "pp": 0.08,
            "jd": 0.07,
            "ru": 0.10,
            "v": 0.10,
        },
        "corr_matrix": {
            "rb": {"hc": 0.93, "i": 0.74, "J": 0.65},
            "hc": {"rb": 0.93, "i": 0.73},
            "pp": {"l": 0.91, "MA": 0.79, "TA": 0.68},
            "l": {"pp": 0.91, "MA": 0.76},
            "y": {"p": 0.75, "OI": 0.73},
        },
    }

    # 1. 权重测试
    print("\n1. 组合权重测试:")
    for sym in ["cu", "al", "rb", "MA", "CF", "au"]:
        w = portfolio_weight(sym, test_cfg)
        m = portfolio_risk_mult(sym, test_cfg)
        print(f"   {sym:>5}: weight={w * 100:>5.1f}%  risk_mult={m:.2f}x")

    # 2. 板块暴露测试
    print("\n2. 板块暴露测试:")
    positions = {
        "cu": {"lots": 2, "direction": 1, "notional": 50000},
        "al": {"lots": 3, "direction": 1, "notional": 30000},
        "rb": {"lots": 5, "direction": -1, "notional": 25000},
        "MA": {"lots": 4, "direction": 1, "notional": 15000},
        "CF": {"lots": 2, "direction": 1, "notional": 30000},
        "pp": {"lots": 3, "direction": 1, "notional": 20000},
    }
    sec = sector_exposure(positions, test_cfg)
    print(f"   总名义市值: {sec['total_notional']:,.0f}")
    print(f"   最大板块: {sec['max_sector']} ({sec['max_sector_weight'] * 100:.1f}%)")
    print(f"   集中度达标: {'是' if sec['concentration_ok'] else '否'}（阈值{sec['threshold'] * 100:.0f}%）")
    for s, info in sorted(sec["by_sector"].items(), key=lambda x: -x[1]["notional"]):
        print(f"     {s}: {info['weight'] * 100:>5.1f}%  多{info['long_lots']}手 / 空{info['short_lots']}手")

    # 3. 相关性风险测试
    print("\n3. 相关性风险测试:")
    corr_risk = check_correlation_risk(positions, test_cfg)
    print(f"   风险等级: {corr_risk['risk_level']}")
    print(f"   高风险对: {corr_risk['total_pairs']} 对")
    for p in corr_risk["high_risk_pairs"]:
        s1, s2 = p["pair"]
        print(f"     {s1}-{s2}: r={p['corr']:.2f}  {p['type']} ({p['risk']})")

    # 4. 再平衡建议测试
    print("\n4. 再平衡建议:")
    rebal = rebalance_suggestion(positions, test_cfg)
    print(f"   需要再平衡: {'是' if rebal['needs_rebalance'] else '否'}")
    print(f"   总偏离度: {rebal['total_deviation'] * 100:.1f}%")
    for s in rebal["suggestions"][:5]:
        print(
            f"     {s['symbol']}: {s['action']} {s['adjust_pct']:.1f}%  "
            f"({s['current_weight'] * 100:.1f}% → {s['target_weight'] * 100:.1f}%)"
        )

    # 5. 综合诊断
    print("\n5. 综合诊断:")
    diag = portfolio_diagnostic(positions, test_cfg)
    print(f"   健康度评分: {diag['health_score']}/100 ({diag['health_level']})")

    print("\n" + "=" * 60)
    print("验证完成 ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
