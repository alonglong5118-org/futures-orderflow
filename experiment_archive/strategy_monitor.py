"""
策略监控面板：实时监控 + 告警规则 + 健康度报告

功能：
  1. 单品种监控：expR / 胜率 / 回撤 / 触发频率
  2. 组合监控：组合收益 / 回撤 / 板块暴露 / 相关性风险
  3. 告警规则：连续亏损 / 回撤超限 / 胜率骤降 / 权重偏离
  4. 健康度报告：每日/每周生成健康度评分

用法：
  实时模式：从交易引擎接收持仓和交易记录，实时计算
  离线模式：从历史回测数据生成基准，用于对比
"""

import copy
import json
import os
import sys
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from four_dim_strategy import DEFAULT_CONFIG
from portfolio_manager import (
    check_correlation_risk,
    portfolio_diagnostic,
    rebalance_suggestion,
    sector_exposure,
    symbols_group,
    symbols_name,
)

# ── 告警规则定义 ────────────────────────────────────────────────────────────
ALERT_RULES = {
    # 单品种级别
    "symbol_consecutive_loss": {
        "name": "连续亏损",
        "level": "warning",
        "threshold": 3,  # 连续 3 笔亏损
        "description": "连续 {threshold} 笔亏损，检查策略是否失效",
    },
    "symbol_drawdown": {
        "name": "最大回撤超限",
        "level": "critical",
        "threshold": 10,  # 10R 回撤
        "description": "单品种回撤超过 {threshold}R，考虑暂停该品种",
    },
    "symbol_win_rate_drop": {
        "name": "胜率骤降",
        "level": "warning",
        "threshold": 0.15,  # 比基准低 15%
        "window": 20,  # 最近 20 笔
        "description": "最近 {window} 笔胜率比基准低 {threshold*100:.0f}% 以上",
    },
    "symbol_expr_drop": {
        "name": "期望收益骤降",
        "level": "critical",
        "threshold": -0.1,  # 滚动 expR < -0.1
        "window": 15,  # 最近 15 笔
        "description": "最近 {window} 笔 expR 低于 {threshold}，策略可能失效",
    },
    # 组合级别
    "portfolio_drawdown": {
        "name": "组合回撤超限",
        "level": "critical",
        "threshold": 5,  # 5% 净值回撤
        "description": "组合净值回撤超过 {threshold}%",
    },
    "sector_concentration": {
        "name": "板块集中度过高",
        "level": "warning",
        "threshold": 0.40,  # 40%
        "description": "单板块权重超过 {threshold*100:.0f}%",
    },
    "high_corr_risk": {
        "name": "高相关对同向持仓",
        "level": "warning",
        "threshold": 2,  # 2 对以上
        "description": "高相关同向持仓超过 {threshold} 对，集中度风险",
    },
    "rebalance_needed": {
        "name": "需要再平衡",
        "level": "info",
        "threshold": 0.05,  # 5% 偏离
        "description": "组合权重偏离目标超过 {threshold*100:.0f}%",
    },
}


# ── 单品种监控器 ────────────────────────────────────────────────────────────
class SymbolMonitor:
    """单品种实时监控器。"""

    def __init__(self, symbol, baseline=None, cfg=DEFAULT_CONFIG):
        self.symbol = symbol
        self.cfg = cfg
        self.baseline = baseline or {}  # 基准指标（expR, win_rate, max_dd, trades）
        self.trades = []  # 交易记录
        self.daily_pnl = []  # 每日盈亏（R 单位）
        self.alerts = []  # 告警记录
        self._consecutive_losses = 0

    def add_trade(self, trade):
        """添加一笔交易。trade: {R_adj, entry_time, exit_time, direction, reason}"""
        self.trades.append(trade)

        r = trade.get("R_adj", 0)
        if r < 0:
            self._consecutive_losses += 1
            # 连续亏损告警
            rule = ALERT_RULES["symbol_consecutive_loss"]
            if self._consecutive_losses >= rule["threshold"]:
                self._alert(
                    "symbol_consecutive_loss",
                    f"连续 {self._consecutive_losses} 笔亏损",
                    f"最近 {self._consecutive_losses} 笔全部亏损",
                )
        else:
            self._consecutive_losses = 0

        # 滚动指标检查
        self._check_rolling_metrics()

    def _check_rolling_metrics(self):
        """检查滚动窗口内的指标。"""
        n = len(self.trades)
        if n < 5:
            return

        recent = self.trades[-min(n, 20) :]
        Rs = [t["R_adj"] for t in recent]

        # 回撤检查
        cumulative = np.cumsum(Rs)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = float(np.max(drawdowns))

        dd_rule = ALERT_RULES["symbol_drawdown"]
        if max_dd >= dd_rule["threshold"]:
            self._alert("symbol_drawdown", f"回撤 {max_dd:.1f}R 超限", f"最近 {len(Rs)} 笔最大回撤 {max_dd:.1f}R")

        # 胜率检查
        wr_rule = ALERT_RULES["symbol_win_rate_drop"]
        window = wr_rule["window"]
        if n >= window:
            recent_wr = sum(1 for t in self.trades[-window:] if t["R_adj"] > 0) / window
            base_wr = self.baseline.get("win_rate", 0.3)
            if base_wr - recent_wr >= wr_rule["threshold"]:
                self._alert(
                    "symbol_win_rate_drop",
                    f"胜率 {recent_wr * 100:.0f}%（基准 {base_wr * 100:.0f}%）",
                    f"最近 {window} 笔胜率骤降 {(base_wr - recent_wr) * 100:.0f}%",
                )

        # expR 检查
        expr_rule = ALERT_RULES["symbol_expr_drop"]
        expr_window = expr_rule["window"]
        if n >= expr_window:
            recent_expr = float(np.mean([t["R_adj"] for t in self.trades[-expr_window:]]))
            if recent_expr <= expr_rule["threshold"]:
                self._alert(
                    "symbol_expr_drop",
                    f"滚动 expR={recent_expr:.3f}",
                    f"最近 {expr_window} 笔期望收益为负，策略可能失效",
                )

    def _alert(self, rule_key, subject, detail):
        """记录告警。"""
        rule = ALERT_RULES.get(rule_key, {})
        alert = {
            "time": datetime.now().isoformat(),
            "symbol": self.symbol,
            "level": rule.get("level", "info"),
            "rule": rule_key,
            "rule_name": rule.get("name", rule_key),
            "subject": subject,
            "detail": detail,
        }
        # 去重：同一规则 24 小时内不重复告警
        for existing in self.alerts[-10:]:
            if existing["rule"] == rule_key:
                try:
                    t1 = datetime.fromisoformat(existing["time"])
                    t2 = datetime.fromisoformat(alert["time"])
                    if (t2 - t1).total_seconds() < 86400:
                        return  # 24h 内重复，跳过
                except Exception:
                    pass
        self.alerts.append(alert)

    def get_status(self):
        """获取品种状态摘要。"""
        n = len(self.trades)
        if n == 0:
            return {"symbol": self.symbol, "trades": 0, "status": "no_data"}

        Rs = [t["R_adj"] for t in self.trades]
        wins = [r for r in Rs if r > 0]
        expR = float(np.mean(Rs))
        win_rate = len(wins) / len(Rs)

        cumulative = np.cumsum(Rs)
        running_max = np.maximum.accumulate(cumulative)
        max_dd = float(np.max(running_max - cumulative))

        # 状态评级
        base_expR = self.baseline.get("expR", 0)
        if expR >= base_expR * 0.8:
            status = "healthy"
        elif expR >= 0:
            status = "watch"
        else:
            status = "warning"

        # 最近 10 笔
        recent_Rs = Rs[-min(10, len(Rs)) :]
        recent_expR = float(np.mean(recent_Rs)) if recent_Rs else 0

        return {
            "symbol": self.symbol,
            "name": symbols_name(self.symbol),
            "group": symbols_group(self.symbol),
            "trades": n,
            "total_R": round(sum(Rs), 2),
            "expR": round(expR, 4),
            "win_rate": round(win_rate, 3),
            "max_dd": round(max_dd, 2),
            "recent_10_expR": round(recent_expR, 4),
            "consecutive_losses": self._consecutive_losses,
            "status": status,
            "baseline_expR": base_expR,
        }


# ── 组合监控器 ──────────────────────────────────────────────────────────────
class PortfolioMonitor:
    """组合级监控器。"""

    def __init__(self, cfg=DEFAULT_CONFIG):
        self.cfg = cfg
        self.symbol_monitors = {}  # {symbol: SymbolMonitor}
        self.positions = {}  # {symbol: {lots, direction, notional, entry_price}}
        self.equity_history = []  # 净值历史
        self.daily_returns = []
        self.alerts = []

    def add_trade(self, symbol, trade):
        """添加一笔交易。"""
        if symbol not in self.symbol_monitors:
            self.symbol_monitors[symbol] = SymbolMonitor(symbol, cfg=self.cfg)
        self.symbol_monitors[symbol].add_trade(trade)

        # 传播告警
        mon = self.symbol_monitors[symbol]
        if mon.alerts and mon.alerts[-1]["time"] == datetime.now().isoformat():
            self.alerts.append(mon.alerts[-1])

    def update_position(self, symbol, position):
        """更新持仓。position: {lots, direction, notional, entry_price}"""
        self.positions[symbol] = position

    def check_portfolio_alerts(self):
        """检查组合级告警。"""
        if not self.positions:
            return

        # 板块集中度
        sec = sector_exposure(self.positions, self.cfg)
        if not sec["concentration_ok"]:
            rule = ALERT_RULES["sector_concentration"]
            self._portfolio_alert(
                "sector_concentration",
                f"{sec['max_sector']} 占比 {sec['max_sector_weight'] * 100:.1f}%",
                f"板块 {sec['max_sector']} 权重超过 {sec['threshold'] * 100:.0f}% 上限",
            )

        # 相关性风险
        corr = check_correlation_risk(self.positions, self.cfg)
        if corr["risk_level"] in ("medium", "high"):
            rule = ALERT_RULES["high_corr_risk"]
            if corr["total_pairs"] >= rule["threshold"]:
                self._portfolio_alert(
                    "high_corr_risk", f"{corr['total_pairs']} 对高相关同向持仓", f"风险等级: {corr['risk_level']}"
                )

        # 再平衡检查
        rebal = rebalance_suggestion(self.positions, self.cfg)
        if rebal["needs_rebalance"] and rebal["total_deviation"] > 0.05:
            self._portfolio_alert(
                "rebalance_needed",
                f"总偏离 {rebal['total_deviation'] * 100:.1f}%",
                f"{len(rebal['suggestions'])} 个品种需要调仓",
            )

    def _portfolio_alert(self, rule_key, subject, detail):
        """组合级告警。"""
        rule = ALERT_RULES.get(rule_key, {})
        alert = {
            "time": datetime.now().isoformat(),
            "level": rule.get("level", "info"),
            "rule": rule_key,
            "rule_name": rule.get("name", rule_key),
            "subject": subject,
            "detail": detail,
            "scope": "portfolio",
        }
        # 去重
        for existing in self.alerts[-20:]:
            if existing.get("rule") == rule_key and existing.get("scope") == "portfolio":
                try:
                    t1 = datetime.fromisoformat(existing["time"])
                    t2 = datetime.fromisoformat(alert["time"])
                    if (t2 - t1).total_seconds() < 43200:  # 12h
                        return
                except Exception:
                    pass
        self.alerts.append(alert)

    def get_report(self):
        """生成完整监控报告。"""
        # 品种状态
        symbol_statuses = []
        for sym, mon in self.symbol_monitors.items():
            symbol_statuses.append(mon.get_status())
        symbol_statuses.sort(key=lambda x: x["expR"], reverse=True)

        # 组合诊断
        diag = portfolio_diagnostic(self.positions, self.cfg) if self.positions else {}

        # 告警统计
        critical = sum(1 for a in self.alerts if a.get("level") == "critical")
        warning = sum(1 for a in self.alerts if a.get("level") == "warning")
        info = sum(1 for a in self.alerts if a.get("level") == "info")

        # 汇总指标
        all_trades = sum(s["trades"] for s in symbol_statuses)
        total_R = sum(s["total_R"] for s in symbol_statuses)

        return {
            "timestamp": datetime.now().isoformat(),
            "n_symbols": len(self.symbol_monitors),
            "n_positions": len(self.positions),
            "total_trades": all_trades,
            "total_R": round(total_R, 2),
            "symbol_statuses": symbol_statuses,
            "portfolio_diagnostic": diag,
            "alerts": {
                "total": len(self.alerts),
                "critical": critical,
                "warning": warning,
                "info": info,
                "recent": self.alerts[-10:],
            },
            "health_score": diag.get("health_score", 100) if diag else 100,
            "health_level": diag.get("health_level", "健康") if diag else "健康",
        }

    def print_dashboard(self):
        """打印文字版监控面板。"""
        report = self.get_report()

        print("=" * 70)
        print(f"  策略监控面板  {report['timestamp']}")
        print("=" * 70)

        # 健康度
        score = report["health_score"]
        level = report["health_level"]
        bar_len = int(score / 2)
        bar = "█" * bar_len + "░" * (50 - bar_len)
        print(f"\n  健康度: {score:>3}/100 ({level})")
        print(f"  [{bar}]")

        # 组合概览
        print("\n  组合概览:")
        print(f"    监控品种: {report['n_symbols']} 个")
        print(f"    当前持仓: {report['n_positions']} 个")
        print(f"    总交易数: {report['total_trades']} 笔")
        print(f"    累计盈亏: {report['total_R']:+.2f} R")

        # 告警
        alerts = report["alerts"]
        print(f"\n  告警: 🔴{alerts['critical']}  🟡{alerts['warning']}  ℹ️{alerts['info']}")
        if alerts["recent"]:
            print("  最近告警:")
            for a in alerts["recent"][-5:]:
                icon = "🔴" if a["level"] == "critical" else ("🟡" if a["level"] == "warning" else "ℹ️")
                print(f"    {icon} [{a.get('symbol', '组合')}] {a['subject']}")

        # 品种状态
        statuses = report["symbol_statuses"]
        if statuses:
            print("\n  品种表现（按 expR 排序）:")
            print(f"  {'品种':>5} {'名称':>6} {'expR':>7} {'胜率':>6} {'DD':>6} {'近10笔':>7} {'状态':>6}")
            print("  " + "-" * 50)
            for s in statuses:
                icon = "🟢" if s["status"] == "healthy" else ("🟡" if s["status"] == "watch" else "🔴")
                print(
                    f"  {s['symbol']:>5} {s['name']:>6} {s['expR']:>+7.3f} "
                    f"{s['win_rate'] * 100:>5.1f}% {s['max_dd']:>6.2f} "
                    f"{s['recent_10_expR']:>+7.3f} {icon}"
                )

        # 组合诊断（如果有持仓）
        diag = report.get("portfolio_diagnostic", {})
        if diag and "sector_exposure" in diag:
            sec = diag["sector_exposure"]
            print("\n  板块暴露:")
            for s, info in sorted(sec["by_sector"].items(), key=lambda x: -x[1]["notional"]):
                w = info.get("weight", 0)
                bar = "█" * int(w * 40)
                print(f"    {s:<6} {w * 100:>5.1f}%  {bar}")

        print("\n" + "=" * 70)


# ── 离线模式：从回测数据生成基准报告 ──────────────────────────────────────────
def generate_baseline_report(cfg=DEFAULT_CONFIG):
    """从回测数据生成基准监控报告（离线模式）。

    用于：
      1. 建立基准指标（供实时监控对比）
      2. 部署前健康度评估
    """
    from four_dim_strategy import load_daily, walk_forward_backtest

    pf = cfg.get("portfolio", {})
    active_syms = pf.get("active_symbols", []) or list(cfg.get("per_symbol_risk", {}).keys())

    print("生成基准监控报告 ...")
    monitor = PortfolioMonitor(cfg)

    for i, sym in enumerate(active_syms):
        try:
            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue
            r = walk_forward_backtest(sym, cfg=cfg, df_in=df, window=200)
            if r and r.get("trades_detail"):
                # 设置基准
                baseline = {
                    "expR": r.get("expR", 0),
                    "win_rate": r.get("win_rate", 0.3),
                    "max_dd": r.get("max_dd", 10),
                    "trades": r.get("trades", 0),
                }
                mon = SymbolMonitor(sym, baseline=baseline, cfg=cfg)
                for t in r["trades_detail"]:
                    mon.trades.append(t)  # 直接填充，不触发告警
                monitor.symbol_monitors[sym] = mon
            print(f"  [{i + 1}/{len(active_syms)}] {sym:>5}", end="\r", flush=True)
        except Exception:
            continue
    print()

    return monitor


# ── 主函数：演示 ────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("策略监控面板 - 基准模式（离线演示）")
    print("=" * 70)

    # 加载部署配置
    deploy_cfg_path = os.path.join(HERE, "deploy", "trade_config_deploy.json")
    if os.path.exists(deploy_cfg_path):
        with open(deploy_cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        print("\n  已加载部署配置: deploy/trade_config_deploy.json")
    else:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["portfolio"]["enabled"] = True
        print("\n  使用默认配置（部署配置不存在）")

    # 生成基准报告
    monitor = generate_baseline_report(cfg)

    # 打印面板
    monitor.print_dashboard()

    # 保存报告
    os.makedirs("logs", exist_ok=True)
    report = monitor.get_report()
    with open("logs/monitor_baseline_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print("\n  报告已保存 → logs/monitor_baseline_report.json")


if __name__ == "__main__":
    main()
