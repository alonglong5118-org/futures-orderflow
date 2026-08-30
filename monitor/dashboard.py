"""
实盘表现监控看板 (Phase 7, Task 1)

功能：
- 每日生成 HTML 监控报告
- 跟踪各品种 expR、交易次数、胜率等指标
- 展示基线 vs 近期的对比
- 集成漂移检测告警
- 支持邮件摘要输出

用法：
    from monitor.dashboard import PerformanceDashboard
    db = PerformanceDashboard()
    report_path = db.generate(metrics_dict, output_path="monitor_dashboard.html")

输入数据格式：
    metrics_dict = {
        "zn": {
            "symbol": "zn",
            "baseline_expR": 0.720,
            "baseline_trades": 28,
            "baseline_win_rate": 0.45,
            "recent_expR": 0.650,
            "recent_trades": 8,
            "recent_win_rate": 0.42,
            "baseline_daily_expR": [0.01, -0.02, ...],  # 可选
            "recent_daily_expR": [0.005, ...],           # 可选
            "current_params": {"stop_atr_mult": 0.7, "rr_ratio": 2.0},
            "last_update": "2026-08-30",
        },
        ...
    }
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from monitor.drift_detector import DriftDetector, DriftAlert


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>参数表现监控看板 — {date}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f8fafc;
    color: #0f172a;
    font-size: 14px;
    line-height: 1.6;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 0 20px; }}

  /* Header */
  .header {{
    background: linear-gradient(135deg, #2563eb, #7c3aed);
    color: #fff;
    padding: 24px 0;
    margin-bottom: 24px;
  }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .subtitle {{ font-size: 13px; opacity: 0.9; }}
  .header .meta {{ display: flex; gap: 20px; margin-top: 12px; font-size: 12px; opacity: 0.85; }}

  /* KPI cards */
  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .kpi-card {{
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
  }}
  .kpi-value {{
    font-size: 28px;
    font-weight: 700;
    background: linear-gradient(135deg, #2563eb, #7c3aed);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    line-height: 1.2;
  }}
  .kpi-label {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
  .kpi-sub {{ font-size: 11px; color: #94a3b8; margin-top: 2px; }}

  /* Sections */
  .section {{
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 20px;
  }}
  .section h2 {{
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .section h2 .badge {{
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
  }}
  .badge-critical {{ background: #fef2f2; color: #dc2626; }}
  .badge-warning {{ background: #fffbeb; color: #d97706; }}
  .badge-ok {{ background: #f0fdf4; color: #059669; }}

  /* Alert items */
  .alert-list {{ list-style: none; }}
  .alert-item {{
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-left: 4px solid;
  }}
  .alert-critical {{ background: #fef2f2; border-left-color: #dc2626; }}
  .alert-warning {{ background: #fffbeb; border-left-color: #d97706; }}
  .alert-info {{ background: #eff6ff; border-left-color: #2563eb; }}
  .alert-symbol {{ font-weight: 700; font-size: 14px; }}
  .alert-message {{ color: #475569; font-size: 13px; margin-top: 2px; }}
  .alert-metric {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }}
  .alert-right {{ text-align: right; }}
  .alert-delta {{ font-family: monospace; font-weight: 700; font-size: 14px; }}
  .delta-down {{ color: #dc2626; }}
  .delta-up {{ color: #059669; }}

  /* Table */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{
    text-align: left;
    padding: 10px 12px;
    background: #f8fafc;
    border-bottom: 2px solid #e2e8f0;
    font-weight: 600;
    font-size: 12px;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }}
  td {{
    padding: 10px 12px;
    border-bottom: 1px solid #e2e8f0;
  }}
  tr:hover td {{ background: #f8fafc; }}
  .sym {{ font-weight: 700; }}
  .num {{ font-family: monospace; text-align: right; }}
  .pos {{ color: #059669; font-weight: 600; }}
  .neg {{ color: #dc2626; font-weight: 600; }}
  .status-dot {{
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 6px;
  }}
  .status-ok {{ background: #059669; }}
  .status-warn {{ background: #d97706; }}
  .status-crit {{ background: #dc2626; }}

  /* Chart container */
  .chart-container {{
    width: 100%;
    min-height: 300px;
    margin: 16px 0;
  }}

  /* Footer */
  .footer {{
    text-align: center;
    color: #94a3b8;
    font-size: 12px;
    padding: 20px 0;
  }}

  @media (max-width: 600px) {{
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .kpi-value {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <div class="container">
    <h1>📊 参数表现监控看板</h1>
    <div class="subtitle">13 个上线品种的实盘/回测表现跟踪与漂移检测</div>
    <div class="meta">
      <span>生成时间：{date}</span>
      <span>监控品种：{n_symbols} 个</span>
      <span>基线版本：{version}</span>
    </div>
  </div>
</div>

<div class="container">

  <!-- KPI -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-value">{avg_expr}</div>
      <div class="kpi-label">平均 expR（近期）</div>
      <div class="kpi-sub">基线 {baseline_expr}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value">{n_critical}</div>
      <div class="kpi-label">严重告警</div>
      <div class="kpi-sub">warning: {n_warning}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value">{n_ok}</div>
      <div class="kpi-label">表现正常</div>
      <div class="kpi-sub">占比 {ok_pct}%</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-value">{total_trades}</div>
      <div class="kpi-label">近期总交易</div>
      <div class="kpi-sub">{window} 天窗口</div>
    </div>
  </div>

  <!-- Alerts -->
  <div class="section">
    <h2>
      ⚠️ 漂移告警
      <span class="badge badge-critical">{n_critical} 严重</span>
      <span class="badge badge-warning">{n_warning} 警告</span>
    </h2>
    {alerts_html}
  </div>

  <!-- Per-symbol table -->
  <div class="section">
    <h2>📋 品种明细</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>品种</th>
            <th class="num">基线 expR</th>
            <th class="num">近期 expR</th>
            <th class="num">变化量</th>
            <th class="num">变化率</th>
            <th class="num">近期交易</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
  </div>

</div>

<div class="footer">
  四维策略模型 · 参数表现监控看板 · 生成于 {date} · 仅供内部参考
</div>

</body>
</html>"""


class PerformanceDashboard:
    """实盘表现监控看板生成器"""

    def __init__(
        self,
        drift_detector: Optional[DriftDetector] = None,
        version: str = "v001",
    ):
        self.detector = drift_detector or DriftDetector()
        self.version = version

    def generate(
        self,
        metrics: Dict[str, Dict[str, Any]],
        output_path: str = "monitor_dashboard.html",
        window_days: int = 60,
    ) -> str:
        """
        生成监控看板 HTML。

        Args:
            metrics: 品种表现数据字典
            output_path: 输出 HTML 文件路径
            window_days: 近期窗口天数

        Returns:
            输出文件路径
        """
        # 运行漂移检测
        alerts = self.detector.detect(metrics)
        alert_symbols = self._get_alert_symbols(alerts)

        # 计算 KPI
        avg_expR, baseline_expR = self._compute_avg_expr(metrics)
        n_critical = sum(1 for a in alerts if a.severity == "critical")
        n_warning = sum(1 for a in alerts if a.severity == "warning")
        n_symbols = len(metrics)
        n_ok = n_symbols - len(set(a.symbol for a in alerts))
        ok_pct = round(n_ok / n_symbols * 100, 1) if n_symbols else 0
        total_trades = sum(m.get("recent_trades", 0) for m in metrics.values())

        # 生成告警 HTML
        alerts_html = self._render_alerts(alerts)
        if not alerts:
            alerts_html = '<div style="padding:20px;text-align:center;color:#059669;background:#f0fdf4;border-radius:8px;">✅ 所有品种表现正常，未检测到显著漂移</div>'

        # 生成表格
        table_rows = self._render_table_rows(metrics, alert_symbols)

        # 填充模板
        html = HTML_TEMPLATE.format(
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            n_symbols=n_symbols,
            version=self.version,
            avg_expr=f"{avg_expR:.3f}",
            baseline_expr=f"{baseline_expR:.3f}",
            n_critical=n_critical,
            n_warning=n_warning,
            n_ok=n_ok,
            ok_pct=ok_pct,
            total_trades=total_trades,
            window=window_days,
            alerts_html=alerts_html,
            table_rows=table_rows,
        )

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        return output_path

    def generate_email_summary(
        self,
        metrics: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        生成邮件摘要（适合邮件通知的精简版本）。

        Returns:
            {
                "subject": "邮件标题",
                "body": "纯文本正文",
                "n_critical": int,
                "n_warning": int,
                "critical_symbols": [...],
            }
        """
        alerts = self.detector.detect(metrics)
        critical = [a for a in alerts if a.severity == "critical"]
        warning = [a for a in alerts if a.severity == "warning"]

        subject_parts = []
        if critical:
            subject_parts.append(f"[严重] {len(critical)} 个品种漂移")
        if warning:
            subject_parts.append(f"[警告] {len(warning)} 个品种漂移")
        if not subject_parts:
            subject_parts.append("[正常] 所有品种表现稳定")

        subject = "参数监控 " + " ".join(subject_parts)

        body_lines = [
            f"参数表现监控日报",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"监控品种: {len(metrics)} 个",
            "",
        ]

        if critical:
            body_lines.append("【严重告警】")
            for a in critical:
                body_lines.append(f"  * {a.symbol} {a.message}")
            body_lines.append("")

        if warning:
            body_lines.append("【警告】")
            for a in warning:
                body_lines.append(f"  * {a.symbol} {a.message}")
            body_lines.append("")

        if not critical and not warning:
            body_lines.append("所有品种表现正常，未检测到显著漂移。")

        return {
            "subject": subject,
            "body": "\n".join(body_lines),
            "n_critical": len(critical),
            "n_warning": len(warning),
            "critical_symbols": [a.symbol for a in critical],
            "warning_symbols": [a.symbol for a in warning],
        }

    def _get_alert_symbols(self, alerts: List[DriftAlert]) -> Dict[str, str]:
        """获取有告警的品种及其最严重级别"""
        result = {}
        for a in alerts:
            if a.symbol not in result or a.severity == "critical":
                result[a.symbol] = a.severity
        return result

    def _compute_avg_expr(
        self, metrics: Dict[str, Dict[str, Any]]
    ) -> tuple:
        """计算平均 expR（简单平均）"""
        if not metrics:
            return 0.0, 0.0
        recent = [m.get("recent_expR", 0) for m in metrics.values()]
        baseline = [m.get("baseline_expR", 0) for m in metrics.values()]
        return sum(recent) / len(recent), sum(baseline) / len(baseline)

    def _render_alerts(self, alerts: List[DriftAlert]) -> str:
        """渲染告警列表 HTML"""
        if not alerts:
            return ""

        html = ['<ul class="alert-list">']
        for a in alerts:
            severity_class = f"alert-{a.severity}"
            delta_class = "delta-down" if a.delta < 0 else "delta-up"
            delta_sign = "+" if a.delta >= 0 else ""
            html.append(f"""
              <li class="alert-item {severity_class}">
                <div>
                  <div class="alert-symbol">{a.symbol}</div>
                  <div class="alert-message">{a.message}</div>
                  <div class="alert-metric">{a.metric} · {a.method}</div>
                </div>
                <div class="alert-right">
                  <div class="alert-delta {delta_class}">{delta_sign}{a.delta:.3f}</div>
                  <div style="font-size:11px;color:#64748b;">{a.delta_pct:+.1f}%</div>
                </div>
              </li>
            """)
        html.append("</ul>")
        return "\n".join(html)

    def _render_table_rows(
        self,
        metrics: Dict[str, Dict[str, Any]],
        alert_symbols: Dict[str, str],
    ) -> str:
        """渲染品种明细表格行"""
        rows = []
        # 按 expR 变化排序（下降最多的在前）
        sorted_syms = sorted(
            metrics.keys(),
            key=lambda s: metrics[s].get("recent_expR", 0) - metrics[s].get("baseline_expR", 0),
        )

        for sym in sorted_syms:
            m = metrics[sym]
            base_expR = m.get("baseline_expR", 0)
            recent_expR = m.get("recent_expR", 0)
            delta = recent_expR - base_expR
            delta_pct = (delta / abs(base_expR) * 100) if base_expR != 0 else 0
            trades = m.get("recent_trades", 0)

            # 状态
            severity = alert_symbols.get(sym, "ok")
            if severity == "critical":
                status_dot = '<span class="status-dot status-crit"></span>严重'
            elif severity == "warning":
                status_dot = '<span class="status-dot status-warn"></span>警告'
            else:
                status_dot = '<span class="status-dot status-ok"></span>正常'

            delta_class = "neg" if delta < 0 else "pos"
            delta_sign = "+" if delta >= 0 else ""

            rows.append(f"""
              <tr>
                <td class="sym">{sym}</td>
                <td class="num">{base_expR:.3f}</td>
                <td class="num">{recent_expR:.3f}</td>
                <td class="num {delta_class}">{delta_sign}{delta:.3f}</td>
                <td class="num {delta_class}">{delta_sign}{delta_pct:.1f}%</td>
                <td class="num">{trades}</td>
                <td>{status_dot}</td>
              </tr>
            """)

        return "\n".join(rows)


def generate_demo_dashboard(output_dir: str = ".") -> str:
    """
    生成演示用的监控看板（基于 Phase 6 数据模拟）。
    用于验证看板功能是否正常工作。
    """
    import random

    random.seed(42)

    # Phase 6 上线品种
    phase6_symbols = [
        "zn", "pp", "al", "c", "cs", "y", "hc", "l",
        "RM", "ss", "au", "OI", "CF",
    ]

    metrics = {}
    for sym in phase6_symbols:
        # 模拟基线 expR（0.1 ~ 0.8）
        base_expR = round(random.uniform(0.1, 0.8), 3)
        # 模拟近期 expR（基线 ±30% 随机波动）
        drift = random.uniform(-0.25, 0.1)
        recent_expR = round(base_expR + drift, 3)
        # 模拟交易数
        base_trades = random.randint(20, 100)
        recent_trades = random.randint(5, 30)

        metrics[sym] = {
            "symbol": sym,
            "baseline_expR": base_expR,
            "baseline_trades": base_trades,
            "baseline_win_rate": round(random.uniform(0.35, 0.55), 2),
            "recent_expR": recent_expR,
            "recent_trades": recent_trades,
            "recent_win_rate": round(random.uniform(0.30, 0.60), 2),
            "baseline_window_days": 250,
            "recent_window_days": 60,
        }

    # 故意设置几个明显漂移的品种
    metrics["zn"]["recent_expR"] = metrics["zn"]["baseline_expR"] - 0.15  # 严重下降
    metrics["pp"]["recent_expR"] = metrics["pp"]["baseline_expR"] - 0.08  # 警告
    metrics["cs"]["recent_trades"] = 5  # 交易频率下降

    db = PerformanceDashboard(version="v001 (Phase 6 基线)")
    output_path = os.path.join(output_dir, "monitor_dashboard_demo.html")
    report_path = db.generate(metrics, output_path)

    # 同时打印邮件摘要
    summary = db.generate_email_summary(metrics)
    print(f"\n📧 邮件摘要预览：")
    print(f"  标题: {summary['subject']}")
    print(f"  严重: {summary['n_critical']} 个, 警告: {summary['n_warning']} 个")

    return report_path


if __name__ == "__main__":
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(project_root, "logs")
    os.makedirs(output_dir, exist_ok=True)

    print("=== 生成演示监控看板 ===")
    path = generate_demo_dashboard(output_dir)
    print(f"\n✓ 看板已生成: {path}")
    print(f"  在浏览器中打开查看效果")
