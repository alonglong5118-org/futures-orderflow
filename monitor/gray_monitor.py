"""
Phase 8 灰度上线监控工具

专门用于灰度期的每日监控和批次评估。
比常规监控更严格的告警阈值，支持批次级通过/失败判断。

用法：
    # 每日运行（放在 crontab 里）
    python -m monitor.gray_monitor --config monitor/gray_rollout/batch1_monitor_config.json

    # 批次评估（灰度结束时运行）
    python -m monitor.gray_monitor --config monitor/gray_rollout/batch1_monitor_config.json --evaluate

    # 生成回滚报告
    python -m monitor.gray_monitor --config monitor/gray_rollout/batch1_monitor_config.json --rollback-report
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def load_config(config_path: str) -> Dict[str, Any]:
    """加载灰度监控配置"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_trade_journal(path: str) -> List[Dict[str, Any]]:
    """加载交易流水"""
    full_path = os.path.join(SCRIPT_DIR, path) if not os.path.isabs(path) else path
    if not os.path.exists(full_path):
        return []
    with open(full_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("trades", data.get("closed_trades", []))


def filter_gray_period(trades: List[Dict[str, Any]], start_date: str, end_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """过滤灰度期内的交易"""
    result = []
    for t in trades:
        t_date = str(t.get("close_date", t.get("exit_date", t.get("date", ""))))[:10]
        if t_date < start_date:
            continue
        if end_date and t_date > end_date:
            continue
        result.append(t)
    return result


def calc_metrics(trades: List[Dict[str, Any]], symbol: Optional[str] = None) -> Dict[str, Any]:
    """计算交易指标"""
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]

    if not trades:
        return {"trades": 0, "expR": 0, "win_rate": 0, "max_drawdown": 0, "total_R": 0}

    rs = [float(t.get("R_adj", t.get("R", 0))) for t in trades]
    wins = [r for r in rs if r > 0]

    # 计算回撤（R 单位）
    cumulative = np.cumsum(rs)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

    return {
        "trades": len(trades),
        "expR": round(float(np.mean(rs)), 4),
        "win_rate": round(len(wins) / len(rs), 3),
        "max_drawdown": round(max_dd, 4),
        "total_R": round(float(np.sum(rs)), 4),
        "wins": len(wins),
        "losses": len(rs) - len(wins),
    }


def check_drift(
    gray_metrics: Dict[str, Any],
    baseline_metrics: Dict[str, Any],
    drift_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """检查漂移，返回告警列表"""
    alerts = []

    if gray_metrics["trades"] == 0:
        return [{"level": "warning", "type": "no_trades", "message": "灰度期内暂无交易"}]

    # expR 漂移
    base_expR = baseline_metrics.get("expR", 0)
    gray_expR = gray_metrics["expR"]
    if base_expR != 0:
        pct_change = (gray_expR - base_expR) / abs(base_expR) * 100
        if pct_change <= drift_config.get("expr_critical_pct", -30):
            alerts.append({
                "level": "critical",
                "type": "expr_critical",
                "message": f"expR 严重漂移: 基线{base_expR:+.3f} → 灰度{gray_expR:+.3f} ({pct_change:+.1f}%)",
            })
        elif pct_change <= drift_config.get("expr_warning_pct", -15):
            alerts.append({
                "level": "warning",
                "type": "expr_warning",
                "message": f"expR 警告漂移: 基线{base_expR:+.3f} → 灰度{gray_expR:+.3f} ({pct_change:+.1f}%)",
            })

    # 交易频率漂移
    base_n = baseline_metrics.get("trades", 0)
    gray_n = gray_metrics["trades"]
    if base_n > 0:
        freq_pct = (gray_n - base_n) / base_n * 100
        if freq_pct <= drift_config.get("freq_critical_pct", -50):
            alerts.append({
                "level": "critical",
                "type": "freq_critical",
                "message": f"交易频率严重下降: 基线{base_n}笔 → 灰度{gray_n}笔 ({freq_pct:+.1f}%)",
            })
        elif freq_pct <= drift_config.get("freq_warning_pct", -30):
            alerts.append({
                "level": "warning",
                "type": "freq_warning",
                "message": f"交易频率下降: 基线{base_n}笔 → 灰度{gray_n}笔 ({freq_pct:+.1f}%)",
            })

    # 样本量判断
    min_trades = drift_config.get("min_trades_for_critical", 5)
    if gray_n < min_trades:
        for a in alerts:
            if a["level"] == "critical":
                a["message"] += " [样本不足，置信度低]"
                a["low_confidence"] = True

    return alerts


def check_immediate_rollback(
    trades: List[Dict[str, Any]],
    rollback_config: Dict[str, Any],
) -> List[str]:
    """检查是否触发立即回滚条件，返回触发原因列表"""
    reasons = []

    if len(trades) < 2:
        return reasons

    rs = [float(t.get("R_adj", t.get("R", 0))) for t in trades]
    dates = [str(t.get("close_date", t.get("date", ""))) for t in trades]

    # 连续亏损
    consec_losses = 0
    max_consec = 0
    for r in rs:
        if r < 0:
            consec_losses += 1
            max_consec = max(max_consec, consec_losses)
        else:
            consec_losses = 0

    min_consec = rollback_config.get("consecutive_losses", 3)
    min_r = rollback_config.get("consecutive_loss_r", 1.0)
    if max_consec >= min_consec:
        # 检查最近的连续亏损是否每笔都 ≥ min_r
        consec_seq = []
        for i, r in enumerate(rs):
            if r < 0:
                consec_seq.append(r)
            else:
                if len(consec_seq) >= min_consec and all(abs(x) >= min_r for x in consec_seq):
                    reasons.append(f"连续 {len(consec_seq)} 笔亏损 ≥ {min_r}R")
                consec_seq = []
        # 末尾检查
        if len(consec_seq) >= min_consec and all(abs(x) >= min_r for x in consec_seq):
            reasons.append(f"连续 {len(consec_seq)} 笔亏损 ≥ {min_r}R")

    # 单周回撤
    weekly_dd = rollback_config.get("weekly_drawdown_r", 3.0)
    total_r = sum(rs)
    if total_r <= -weekly_dd:
        reasons.append(f"灰度期累计亏损 {total_r:+.2f}R，超过 {weekly_dd}R 阈值")

    return reasons


def evaluate_batch(
    gray_metrics: Dict[str, Any],
    baseline_metrics: Dict[str, Any],
    pass_criteria: Dict[str, Any],
    rollback_reasons: List[str],
    critical_alerts: int,
) -> Dict[str, Any]:
    """批次评估：通过 / 失败 / 待定"""
    checks = []

    # 1. expR 不低于基线的一定比例
    min_ratio = pass_criteria.get("min_expr_ratio", 0.7)
    base_expR = baseline_metrics.get("expR", 0)
    gray_expR = gray_metrics.get("expR", 0)
    if base_expR > 0:
        ratio = gray_expR / base_expR
        pass_expR = ratio >= min_ratio
        checks.append({
            "name": "expR 比例",
            "value": f"{ratio*100:.0f}%",
            "threshold": f"≥ {min_ratio*100:.0f}%",
            "pass": pass_expR,
        })
    elif base_expR < 0:
        # 基线就是负的，灰度后更好就算过
        pass_expR = gray_expR > base_expR
        checks.append({
            "name": "expR 改善",
            "value": f"{gray_expR:+.3f} vs {base_expR:+.3f}",
            "threshold": "优于基线",
            "pass": pass_expR,
        })
    else:
        pass_expR = True
        checks.append({"name": "expR 检查", "value": "基线为0，跳过", "threshold": "-", "pass": True})

    # 2. 严重告警次数
    max_alerts = pass_criteria.get("max_critical_alerts_per_week", 2)
    pass_alerts = critical_alerts <= max_alerts
    checks.append({
        "name": "严重告警数",
        "value": str(critical_alerts),
        "threshold": f"≤ {max_alerts}",
        "pass": pass_alerts,
    })

    # 3. 回撤
    max_dd_ratio = pass_criteria.get("max_drawdown_ratio", 1.2)
    base_dd = baseline_metrics.get("max_drawdown", 0)
    gray_dd = gray_metrics.get("max_drawdown", 0)
    if base_dd > 0:
        dd_ratio = gray_dd / base_dd
        pass_dd = dd_ratio <= max_dd_ratio
        checks.append({
            "name": "回撤比例",
            "value": f"{dd_ratio:.1f}x",
            "threshold": f"≤ {max_dd_ratio}x",
            "pass": pass_dd,
        })
    else:
        pass_dd = True
        checks.append({"name": "回撤检查", "value": "基线无回撤，跳过", "threshold": "-", "pass": True})

    # 4. 立即回滚条件
    pass_rollback = len(rollback_reasons) == 0
    checks.append({
        "name": "立即回滚条件",
        "value": "未触发" if pass_rollback else f"触发: {rollback_reasons[0]}",
        "threshold": "不触发",
        "pass": pass_rollback,
    })

    # 综合判断
    all_pass = all(c["pass"] for c in checks)
    n_pass = sum(1 for c in checks if c["pass"])

    # 样本量不足 → 待定
    if gray_metrics.get("trades", 0) < 5:
        verdict = "pending"
        verdict_text = "待定（样本不足）"
    elif all_pass:
        verdict = "pass"
        verdict_text = "通过"
    else:
        verdict = "fail"
        verdict_text = "未通过"

    return {
        "verdict": verdict,
        "verdict_text": verdict_text,
        "checks": checks,
        "n_pass": n_pass,
        "n_total": len(checks),
    }


def generate_gray_dashboard(
    config: Dict[str, Any],
    per_symbol: Dict[str, Dict[str, Any]],
    overall: Dict[str, Any],
    alerts: List[Dict[str, Any]],
    rollback_reasons: List[str],
    evaluation: Optional[Dict[str, Any]] = None,
) -> str:
    """生成灰度监控看板 HTML"""
    gray_symbols = config.get("gray_symbols", [])
    batch = config.get("batch", "")
    start_date = config.get("gray_start_date", "")
    period_days = config.get("gray_period_days", 14)

    # 计算已运行天数
    today = datetime.now().strftime("%Y-%m-%d")
    days_passed = 0
    if start_date:
        d1 = datetime.strptime(start_date, "%Y-%m-%d")
        d2 = datetime.strptime(today, "%Y-%m-%d")
        days_passed = (d2 - d1).days

    # 逐品种卡片
    sym_cards_html = ""
    for sym in gray_symbols:
        gm = per_symbol.get(sym, {}).get("gray", {"trades": 0, "expR": 0})
        bm = per_symbol.get(sym, {}).get("baseline", {"trades": 0, "expR": 0})
        sym_alerts = per_symbol.get(sym, {}).get("alerts", [])

        n_critical = sum(1 for a in sym_alerts if a["level"] == "critical")
        n_warning = sum(1 for a in sym_alerts if a["level"] == "warning")

        expR_delta = gm.get("expR", 0) - bm.get("expR", 0)
        if expR_delta > 0.05:
            delta_class = "pos"
            delta_icon = "▲"
        elif expR_delta < -0.05:
            delta_class = "neg"
            delta_icon = "▼"
        else:
            delta_class = "neutral"
            delta_icon = "—"

        alert_badges = ""
        if n_critical > 0:
            alert_badges += f'<span style="background:#fee2e2;color:#991b1b;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">严重 {n_critical}</span> '
        if n_warning > 0:
            alert_badges += f'<span style="background:#fef9c3;color:#854d0e;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">警告 {n_warning}</span>'

        sym_cards_html += f"""
        <div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <strong style="font-size:16px">{sym}</strong>
            {alert_badges}
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px">
            <div style="text-align:center;padding:8px;background:#f8fafc;border-radius:6px">
              <div style="font-size:11px;color:#64748b">交易数</div>
              <div style="font-size:16px;font-weight:700">{gm.get('trades',0)} 笔</div>
            </div>
            <div style="text-align:center;padding:8px;background:#f8fafc;border-radius:6px">
              <div style="font-size:11px;color:#64748b">灰度 expR</div>
              <div style="font-size:16px;font-weight:700;{('color:#059669' if gm.get('expR',0)>0 else 'color:#dc2626')}">{gm.get('expR',0):+.3f}</div>
            </div>
            <div style="text-align:center;padding:8px;background:#f8fafc;border-radius:6px">
              <div style="font-size:11px;color:#64748b">Δ vs 基线</div>
              <div style="font-size:16px;font-weight:700" class="{delta_class}">{delta_icon} {expR_delta:+.3f}</div>
            </div>
          </div>
          <div style="font-size:12px;color:#64748b">
            基线 expR: {bm.get('expR',0):+.3f} | 胜率: {gm.get('win_rate',0)*100:.0f}% | 累计: {gm.get('total_R',0):+.2f}R
          </div>
        </div>
        """

    # 告警列表
    if alerts:
        alerts_html = ""
        for a in alerts:
            color = "#dc2626" if a["level"] == "critical" else "#d97706"
            bg = "#fef2f2" if a["level"] == "critical" else "#fffbeb"
            alerts_html += f'<div style="padding:10px 12px;background:{bg};border-left:3px solid {color};border-radius:0 6px 6px 0;margin-bottom:6px;font-size:13px">{a["message"]}</div>'
    else:
        alerts_html = '<div style="padding:14px;background:#f0fdf4;border-radius:8px;text-align:center;color:#065f46;font-size:13px">✅ 暂无告警，一切正常</div>'

    # 评估结果
    eval_html = ""
    if evaluation:
        verdict = evaluation["verdict"]
        if verdict == "pass":
            v_color = "#059669"
            v_bg = "#f0fdf4"
            v_icon = "✅"
        elif verdict == "fail":
            v_color = "#dc2626"
            v_bg = "#fef2f2"
            v_icon = "❌"
        else:
            v_color = "#d97706"
            v_bg = "#fffbeb"
            v_icon = "⏳"

        checks_html = ""
        for c in evaluation["checks"]:
            status = "✅" if c["pass"] else "❌"
            checks_html += f"<tr><td>{c['name']}</td><td>{c['value']}</td><td>{c['threshold']}</td><td>{status}</td></tr>"

        eval_html = f"""
        <div style="padding:18px;background:{v_bg};border:1px solid {v_color}33;border-radius:10px;margin-bottom:16px">
          <h3 style="color:{v_color};margin:0 0 10px 0">{v_icon} 批次评估结果：{evaluation['verdict_text']}</h3>
          <div style="font-size:13px;color:#334155">
            通过 {evaluation['n_pass']} / {evaluation['n_total']} 项检查
          </div>
          <table style="margin-top:10px;font-size:12px">
            <thead><tr><th>检查项</th><th>实际值</th><th>阈值</th><th>结果</th></tr></thead>
            <tbody>{checks_html}</tbody>
          </table>
        </div>
        """

    # 进度条
    progress_pct = min(days_passed / period_days * 100, 100) if period_days else 0

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Phase 8 灰度监控 — {batch}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC","WenQuanYi Micro Hei",sans-serif; background:#f8fafc; color:#0f172a; font-size:14px; line-height:1.65; }}
  .container {{ max-width:900px; margin:0 auto; padding:0 20px; }}
  .header {{ background:linear-gradient(135deg,#059669,#0891b2); color:#fff; padding:24px 0; margin-bottom:20px; }}
  .header h1 {{ font-size:22px; font-weight:700; margin-bottom:4px; }}
  .header .sub {{ font-size:13px; opacity:0.9; }}
  .header .meta {{ display:flex; gap:20px; margin-top:12px; font-size:11px; opacity:0.85; flex-wrap:wrap; }}
  section {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:18px; margin-bottom:16px; }}
  section h2 {{ font-size:16px; font-weight:600; margin-bottom:12px; color:#1e293b; }}
  .pos {{ color:#059669; }}
  .neg {{ color:#dc2626; }}
  .neutral {{ color:#64748b; }}
  .progress-bar {{ height:8px; background:#e2e8f0; border-radius:4px; overflow:hidden; margin:8px 0; }}
  .progress-fill {{ height:100%; background:linear-gradient(90deg,#059669,#10b981); transition:width 0.3s; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th, td {{ padding:6px 10px; text-align:left; border-bottom:1px solid #f1f5f9; }}
  th {{ background:#f8fafc; font-weight:600; color:#475569; }}
  .footnote {{ font-size:11px; color:#94a3b8; margin-top:12px; padding-top:10px; border-top:1px solid #e2e8f0; }}
</style>
</head>
<body>
<div class="header">
  <div class="container">
    <h1>Phase 8 灰度监控 — {batch}</h1>
    <div class="sub">品种：{', '.join(gray_symbols)}</div>
    <div class="meta">
      <span>📅 开始：{start_date}</span>
      <span>⏱ 已运行：{days_passed} / {period_days} 天</span>
      <span>🏷 v001 → v002</span>
    </div>
    <div class="progress-bar">
      <div class="progress-fill" style="width:{progress_pct:.1f}%"></div>
    </div>
  </div>
</div>
<div class="container">
  {eval_html}
  <section>
    <h2>逐品种表现</h2>
    {sym_cards_html}
  </section>
  <section>
    <h2>告警汇总</h2>
    {alerts_html}
  </section>
  <div class="footnote">
    生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 
    数据来源：trade_journal.json | 
    灰度批次：{batch}
  </div>
</div>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="Phase 8 灰度上线监控")
    parser.add_argument("--config", type=str, required=True, help="灰度监控配置文件路径")
    parser.add_argument("--evaluate", action="store_true", help="执行批次评估")
    parser.add_argument("--baseline-path", type=str, default=None,
                        help="基线交易数据路径（用于对比），默认用配置里的")
    parser.add_argument("--output", type=str, default=None, help="HTML 看板输出路径")

    args = parser.parse_args()

    config = load_config(args.config)
    batch = config.get("batch", "灰度批次")
    gray_symbols = config.get("gray_symbols", [])
    start_date = config.get("gray_start_date", "")

    print(f"Phase 8 灰度监控 — {batch}")
    print(f"品种: {', '.join(gray_symbols)}")
    print(f"开始日期: {start_date}")
    print("-" * 60)

    # 加载交易数据
    data_sources = config.get("data_sources", [])
    all_trades = []
    for ds in data_sources:
        if ds.get("type") == "trade_journal":
            trades = load_trade_journal(ds.get("path", "../trade_journal.json"))
            all_trades.extend(trades)

    # 去重
    seen = set()
    unique_trades = []
    for t in all_trades:
        key = (t.get("symbol"), str(t.get("entry_date", t.get("date", ""))))
        if key not in seen:
            seen.add(key)
            unique_trades.append(t)

    print(f"总交易数: {len(unique_trades)}")

    # 过滤灰度期
    gray_trades = filter_gray_period(unique_trades, start_date)
    print(f"灰度期交易: {len(gray_trades)} 笔")

    # 基线数据（灰度期之前的同品种交易，作为同期参考）
    # 简化：用灰度期之前 3 个月的数据做基线
    if start_date:
        d = datetime.strptime(start_date, "%Y-%m-%d")
        baseline_end = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        baseline_start = (d - timedelta(days=90)).strftime("%Y-%m-%d")
        baseline_trades = filter_gray_period(unique_trades, baseline_start, baseline_end)
    else:
        baseline_trades = []

    print(f"基线参考交易: {len(baseline_trades)} 笔（灰度前 90 天）")

    # 逐品种分析
    per_symbol = {}
    all_alerts = []
    all_rollback = []

    for sym in gray_symbols:
        sym_gray = [t for t in gray_trades if t.get("symbol") == sym]
        sym_baseline = [t for t in baseline_trades if t.get("symbol") == sym]

        gray_metrics = calc_metrics(sym_gray)
        baseline_metrics = calc_metrics(sym_baseline)

        # 漂移检测
        drift_cfg = config.get("alerts", {}).get("drift", {})
        alerts = check_drift(gray_metrics, baseline_metrics, drift_cfg)
        for a in alerts:
            a["symbol"] = sym
        all_alerts.extend(alerts)

        # 立即回滚检查
        rb_cfg = config.get("alerts", {}).get("immediate_rollback", {})
        rollback_reasons = check_immediate_rollback(sym_gray, rb_cfg)
        for r in rollback_reasons:
            all_rollback.append(f"{sym}: {r}")

        per_symbol[sym] = {
            "gray": gray_metrics,
            "baseline": baseline_metrics,
            "alerts": alerts,
            "rollback_reasons": rollback_reasons,
        }

        # 打印
        n_crit = sum(1 for a in alerts if a["level"] == "critical")
        n_warn = sum(1 for a in alerts if a["level"] == "warning")
        status = "🔴" if n_crit > 0 else ("🟡" if n_warn > 0 else "🟢")
        print(f"  {status} {sym:4s}: 灰度{gray_metrics['trades']}笔 expR={gray_metrics['expR']:+.3f} | "
              f"基线{baseline_metrics['trades']}笔 expR={baseline_metrics['expR']:+.3f} | "
              f"告警: {n_crit}严 {n_warn}警")

    # 整体指标
    overall_gray = calc_metrics(gray_trades)
    overall_baseline = calc_metrics(baseline_trades)

    print("-" * 60)
    print(f"整体: 灰度{overall_gray['trades']}笔 expR={overall_gray['expR']:+.3f} | "
          f"基线{overall_baseline['trades']}笔 expR={overall_baseline['expR']:+.3f}")

    # 立即回滚
    if all_rollback:
        print(f"\n⚠️  触发立即回滚:")
        for r in all_rollback:
            print(f"  - {r}")

    # 批次评估
    evaluation = None
    if args.evaluate:
        print(f"\n=== 批次评估 ===")
        critical_count = sum(1 for a in all_alerts if a["level"] == "critical")
        evaluation = evaluate_batch(
            overall_gray, overall_baseline,
            config.get("pass_criteria", {}),
            all_rollback, critical_count,
        )
        print(f"结果: {evaluation['verdict_text']} ({evaluation['n_pass']}/{evaluation['n_total']} 通过)")
        for c in evaluation["checks"]:
            status = "✅" if c["pass"] else "❌"
            print(f"  {status} {c['name']}: {c['value']} (阈值: {c['threshold']})")

    # 生成 HTML 看板
    output_dir = config.get("output_dir", "monitor/gray_rollout")
    output_file = args.output or os.path.join(
        SCRIPT_DIR, output_dir,
        config.get("dashboard_filename", "gray_dashboard.html")
    )
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    html = generate_gray_dashboard(
        config, per_symbol,
        {"gray": overall_gray, "baseline": overall_baseline},
        all_alerts, all_rollback, evaluation,
    )
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n监控看板: {output_file}")


if __name__ == "__main__":
    main()
