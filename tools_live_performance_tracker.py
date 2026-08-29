#!/usr/bin/env python3
"""
实盘表现追踪器 - GA优化参数验证监控

功能：
1. 从 trade_journal.json 读取实盘成交记录
2. 计算实盘 vs 回测(OOS) 的表现偏差
3. 按品种统计表现，监控是否触发验证失败条件
4. 生成每日追踪报告
5. 判断是否达到进阶条件（小资金→正式资金）

用法：
    python3 live_performance_tracker.py            # 显示当前状态
    python3 live_performance_tracker.py --report   # 生成详细报告
    python3 live_performance_tracker.py --check    # 只检查是否触发告警
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = "/Users/ken/WorkBuddy/futures-orderflow"
sys.path.insert(0, SCRIPT_DIR)

OUTPUT_DIR = "/Users/ken/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a8cc690838fade9dcdef78c"
JOURNAL_PATH = os.path.join(SCRIPT_DIR, "trade_journal.json")
ACCOUNT_PATH = os.path.join(SCRIPT_DIR, "account_state.json")
TRADE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "trade_config.json")
OOS_RESULT_PATTERN = os.path.join(SCRIPT_DIR, "ga_v5_{symbol}_result/{symbol}_phase35_oos_result.json")


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_oos_metrics(symbol):
    """获取某品种的OOS回测指标"""
    path = OOS_RESULT_PATTERN.format(symbol=symbol)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        # 取最佳候选的OOS指标
        oos_val = data.get("oos_validation", {})
        # 优先找expR最高的
        best_key = None
        best_expR = -999
        for key in ["balanced", "stable", "aggressive"]:
            if key in oos_val:
                expR = oos_val[key]["oos"].get("expR", -999)
                if expR > best_expR:
                    best_expR = expR
                    best_key = key
        if best_key:
            return oos_val[best_key]["oos"]
    except Exception:
        pass
    return None


def load_journal_trades():
    """从trade_journal加载已平仓交易"""
    try:
        import trade_journal as tj

        data = tj._load()
        closed = [t for t in data.get("trades", []) if t.get("pnl") is not None]
        return closed, data
    except Exception as e:
        print(f"无法加载trade_journal: {e}")
        return [], {}


def compute_R_multipliers(trades, base_equity=100000, risk_pct=0.5):
    """计算每笔交易的R倍数"""
    # 获取合约乘数
    multipliers = {}
    tc = load_json(TRADE_CONFIG_PATH) or {}
    specs = tc.get("contract_specs", {})
    for sym, spec in specs.items():
        multipliers[sym] = spec.get("multiplier", 10)

    r_list = []
    cum_pnl = 0.0

    for t in sorted(trades, key=lambda t: t.get("exit_time") or t.get("time", "")):
        sym = t.get("symbol", "")
        pnl = t.get("pnl", 0)
        lots = t.get("lots", 1)
        stop_dist = t.get("stop_dist")
        mult = multipliers.get(sym, 10)

        cum_pnl += pnl
        equity_before = base_equity - cum_pnl + pnl

        if stop_dist and stop_dist > 0:
            actual_risk = stop_dist * mult * lots
            R = pnl / actual_risk if actual_risk > 0 else 0
        else:
            planned_risk = max(1.0, equity_before * risk_pct / 100)
            R = pnl / planned_risk

        r_list.append(
            {
                "symbol": sym,
                "time": t.get("exit_time") or t.get("time", ""),
                "pnl": pnl,
                "R": R,
                "direction": t.get("direction", ""),
                "exit_reason": t.get("exit_reason", ""),
            }
        )

    return r_list


def compute_metrics(r_list):
    """从R列表计算绩效指标"""
    if not r_list:
        return None

    R_values = [t["R"] for t in r_list]
    total_R = sum(R_values)
    n = len(R_values)
    wins = [r for r in R_values if r > 0]
    losses = [r for r in R_values if r <= 0]

    win_rate = len(wins) / n if n > 0 else 0
    avg_R = total_R / n if n > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pf = (avg_win * len(wins)) / (avg_loss * len(losses)) if avg_loss > 0 and len(losses) > 0 else float("inf")

    # 最大回撤
    cumulative = []
    cum = 0
    for r in R_values:
        cum += r
        cumulative.append(cum)

    peak = 0
    max_dd = 0
    for c in cumulative:
        peak = max(peak, c)
        dd = peak - c
        max_dd = max(max_dd, dd)

    calmar = total_R / max_dd if max_dd > 0 else float("inf")

    # 夏普
    if n > 1:
        import math

        mean = avg_R
        std = (sum((r - mean) ** 2 for r in R_values) / (n - 1)) ** 0.5
        sharpe = math.sqrt(n) * mean / std if std > 0 else 0
    else:
        sharpe = 0

    return {
        "total_trades": n,
        "total_R": total_R,
        "avg_R": avg_R,
        "win_rate": win_rate,
        "avg_win_R": avg_win,
        "avg_loss_R": avg_loss,
        "profit_factor": pf,
        "max_drawdown_R": max_dd,
        "calmar_ratio": calmar,
        "sharpe_ratio": sharpe,
        "cumulative": cumulative,
    }


def analyze_by_symbol(r_list):
    """按品种分析"""
    by_sym = {}
    for t in r_list:
        s = t["symbol"]
        if s not in by_sym:
            by_sym[s] = []
        by_sym[s].append(t)

    result = {}
    for sym, trades in by_sym.items():
        result[sym] = compute_metrics(trades)
        result[sym]["trades_list"] = trades

    return result


def compare_with_oos(by_sym_metrics):
    """实盘 vs OOS回测对比"""
    comparison = []

    for sym, live_m in sorted(by_sym_metrics.items()):
        oos_m = get_oos_metrics(sym)
        if not oos_m:
            continue

        live_expR = live_m["avg_R"]
        oos_expR = oos_m.get("expR", 0)

        # 偏差率
        if oos_expR != 0:
            deviation = (live_expR - oos_expR) / abs(oos_expR) * 100
        else:
            deviation = 0

        # 状态判定
        if live_expR >= oos_expR * 0.5:  # 实盘达到回测50%以上算通过
            status = "✅ 达标"
        elif live_expR > 0:
            status = "⚠️ 偏低"
        else:
            status = "❌ 亏损"

        comparison.append(
            {
                "symbol": sym,
                "live_trades": live_m["total_trades"],
                "live_expR": live_expR,
                "oos_expR": oos_expR,
                "deviation_pct": deviation,
                "status": status,
                "live_win_rate": live_m["win_rate"],
                "oos_win_rate": oos_m.get("win_rate", 0),
            }
        )

    return comparison


def check_validation_status(metrics, small_cap_config):
    """检查小资金验证状态"""
    targets = small_cap_config["validation_targets"]
    grad = small_cap_config["graduation_criteria"]

    if not metrics:
        return {"phase": "未开始", "progress": 0, "checks": {}}

    checks = {
        "交易笔数": {
            "current": metrics["total_trades"],
            "target": targets["min_trades"],
            "passed": metrics["total_trades"] >= targets["min_trades"],
        },
        "胜率": {
            "current": metrics["win_rate"],
            "target": targets["min_win_rate"],
            "passed": metrics["win_rate"] >= targets["min_win_rate"],
        },
        "期望收益(R)": {
            "current": metrics["avg_R"],
            "target": targets["min_expR"],
            "passed": metrics["avg_R"] >= targets["min_expR"],
        },
    }

    passed = sum(1 for c in checks.values() if c["passed"])
    progress = passed / len(checks) * 100

    # 是否达到进阶条件
    grad_checks = {
        "交易笔数": metrics["total_trades"] >= grad["trades"],
        "胜率": metrics["win_rate"] >= grad["win_rate"],
        "期望收益": metrics["avg_R"] >= grad["expR"],
        "Calmar": metrics["calmar_ratio"] >= grad["calmar"],
        "最大回撤": metrics["max_drawdown_R"] <= grad["max_drawdown_pct"] / 100 * 100,  # 粗略换算
    }

    grad_passed = sum(grad_checks.values())
    can_graduate = grad_passed >= 4  # 至少4项达标

    phase = "验证中"
    if can_graduate:
        phase = "🎉 可进阶"
    elif progress >= 66:
        phase = "进展良好"

    return {
        "phase": phase,
        "progress": progress,
        "checks": checks,
        "graduation_checks": grad_checks,
        "can_graduate": can_graduate,
    }


def load_small_cap_config():
    """加载小资金验证配置"""
    tc = load_json(TRADE_CONFIG_PATH) or {}
    return tc.get(
        "_small_cap_mode",
        {
            "validation_targets": {
                "min_trades": 30,
                "min_win_rate": 0.35,
                "min_expR": 0.1,
                "max_drawdown_pct": 5,
            },
            "graduation_criteria": {
                "trades": 50,
                "win_rate": 0.38,
                "expR": 0.2,
                "calmar": 2.0,
                "max_drawdown_pct": 3,
            },
        },
    )


def print_status():
    """打印当前状态"""
    trades, raw_data = load_journal_trades()

    if not trades:
        print("⚠️  暂无成交记录")
        return

    small_cap = load_small_cap_config()

    # 计算R倍数（用小资金权益）
    base_equity = small_cap.get("account", {}).get("equity", 100000)
    risk_pct = small_cap.get("account", {}).get("risk_pct", 0.5)

    r_list = compute_R_multipliers(trades, base_equity=base_equity, risk_pct=risk_pct)
    metrics = compute_metrics(r_list)
    by_sym = analyze_by_symbol(r_list)
    comparison = compare_with_oos(by_sym)
    status = check_validation_status(metrics, small_cap)

    print("=" * 70)
    print("  实盘表现追踪 - GA优化参数验证")
    print("=" * 70)
    print(f"  验证阶段: {status['phase']}")
    print(f"  总成交笔数: {metrics['total_trades']}")
    print(f"  累计收益: {metrics['total_R']:+.4f} R")
    print(f"  平均收益: {metrics['avg_R']:+.4f} R/笔")
    print(f"  胜率: {metrics['win_rate']:.1%}")
    print(f"  盈亏比: {metrics['profit_factor']:.2f}")
    print(f"  最大回撤: {metrics['max_drawdown_R']:.4f} R")
    print(f"  Calmar: {metrics['calmar_ratio']:.2f}")
    print(f"  夏普: {metrics['sharpe_ratio']:.2f}")

    print("\n--- 验证进度 ---")
    for name, check in status["checks"].items():
        icon = "✅" if check["passed"] else "⬜"
        if isinstance(check["target"], float):
            current_pct = check["current"]
            target_pct = check["target"]
            print(f"  {icon} {name}: {current_pct:.3f} / {target_pct:.3f}")
        else:
            print(f"  {icon} {name}: {check['current']} / {check['target']}")
    print(f"  进度: {status['progress']:.0f}%")

    if status.get("can_graduate"):
        print("\n🎉 达到进阶条件！可以考虑扩大资金规模")

    # 品种对比
    if comparison:
        print("\n--- 品种表现 vs OOS回测 ---")
        print(f"  {'品种':6s} {'实盘笔数':>8s} {'实盘expR':>10s} {'OOS expR':>10s} {'偏差':>8s} 状态")
        print(f"  {'-' * 55}")
        for c in sorted(comparison, key=lambda x: x["live_expR"], reverse=True):
            dev_str = f"{c['deviation_pct']:+.1f}%"
            print(
                f"  {c['symbol']:6s} {c['live_trades']:>8d} "
                f"{c['live_expR']:>+10.4f} {c['oos_expR']:>+10.4f} "
                f"{dev_str:>8s} {c['status']}"
            )


def main():
    parser = argparse.ArgumentParser(description="实盘表现追踪器 - GA优化参数验证")
    parser.add_argument("--report", action="store_true", help="生成详细报告")
    parser.add_argument("--check", action="store_true", help="只检查验证状态")
    args = parser.parse_args()

    print_status()


if __name__ == "__main__":
    main()
