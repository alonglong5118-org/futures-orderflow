"""回测可视化增强（Viz Upgrade）
=================================================================
用 Plotly 生成交互式回测报告，替代纯 JSON 数据流。
输出自包含 HTML 文件，可在任何浏览器打开。

图表：
  1. 权益曲线 + 水下曲线（drawdown underwater）
  2. 逐笔 R 散点图（盈亏分布）
  3. 月度热力图（月度收益分布）
  4. 盈亏直方图
  5. 回测统计摘要表

参考 Kara说量化 的数据可视化思路，适配到四维策略框架。
数据来源：backtest_viz.data() + papertrack_report.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_JSON = os.path.join(HERE, "papertrack_report.json")
OUTPUT_HTML = os.path.join(HERE, "backtest_report_viz.html")

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    from plotly.subplots import make_subplots

    _HAVE_PLOTLY = True
except Exception:
    _HAVE_PLOTLY = False

DARK_THEME = {
    "bg": "#0d1117",
    "text": "#c9d1d9",
    "grid": "#21262d",
    "green": "#3fb950",
    "red": "#f85149",
    "gold": "#d4af37",
    "blue": "#58a6ff",
}


def _load_report():
    """加载回测报告数据。"""
    try:
        if os.path.exists(REPORT_JSON):
            with open(REPORT_JSON, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _load_r_series():
    """从 backtest_viz 获取 R 序列。"""
    try:
        import backtest_viz as bv

        return bv.data()
    except Exception:
        return {}


def _try_journal_r():
    """从 trade_journal 获取实盘 R 序列。"""
    try:
        import backtest_viz as bv

        return bv._journal_r_series()
    except Exception:
        return []


def generate_report(output_path=None):
    """生成交互式回测报告 HTML。

    返回生成的文件路径，失败返回 None。
    """
    if not _HAVE_PLOTLY:
        return _generate_fallback()

    output_path = output_path or OUTPUT_HTML
    report = _load_report()
    viz_data = _load_r_series()

    trades = report.get("trades", [])
    if not trades:
        trades = viz_data.get("r_series", [])
        if not trades:
            trades = _try_journal_r()

    if not trades:
        return _generate_fallback()

    # 计算权益曲线
    equity_curve = []
    dd_curve = []
    peak = 0.0
    cum_R = 0.0
    for t in trades:
        R = float(t.get("R", 0))
        cum_R += R
        equity = cum_R
        equity_curve.append(
            {
                "idx": len(equity_curve),
                "R": round(R, 4),
                "equity": round(equity, 2),
                "symbol": t.get("symbol", ""),
                "time": t.get("time", ""),
            }
        )
        if equity > peak:
            peak = equity
        dd = equity - peak
        dd_curve.append({"idx": len(dd_curve) - 1, "dd": round(dd, 2)})

    # 月度统计
    monthly = {}
    for t in trades:
        tm = t.get("time", "")
        if not tm:
            continue
        try:
            dt = datetime.strptime(tm[:10], "%Y-%m-%d")
            key = f"{dt.year}-{dt.month:02d}"
        except Exception:
            continue
        R = float(t.get("R", 0))
        monthly[key] = monthly.get(key, 0) + R

    # 生成 HTML
    fig = make_subplots(
        rows=3,
        cols=2,
        subplot_titles=(
            "权益曲线",
            "水下曲线 (Drawdown)",
            "逐笔 R 散点",
            "盈亏分布直方图",
            "月度收益热力图",
            "统计摘要",
        ),
        specs=[[{"colspan": 2}, None], [{"colspan": 2}, None], [{"type": "heatmap"}, {"type": "table"}]],
        vertical_spacing=0.08,
    )

    # 1. 权益曲线
    xs = [e["idx"] for e in equity_curve]
    ys = [e["equity"] for e in equity_curve]
    fig.add_trace(
        go.Scatter(x=xs, y=ys, mode="lines", name="累计R", line=dict(color=DARK_THEME["gold"], width=2)), row=1, col=1
    )

    # 2. 水下曲线
    dd_ys = [d["dd"] for d in dd_curve]
    fig.add_trace(
        go.Scatter(
            x=xs, y=dd_ys, mode="lines", name="回撤", fill="tozeroy", line=dict(color=DARK_THEME["red"], width=1)
        ),
        row=2,
        col=1,
    )

    # 3. R 散点
    colors = [DARK_THEME["green"] if e["R"] > 0 else DARK_THEME["red"] for e in equity_curve]
    fig.add_trace(
        go.Scatter(
            x=xs, y=[e["R"] for e in equity_curve], mode="markers", name="单笔R", marker=dict(color=colors, size=5)
        ),
        row=3,
        col=1,
    )

    # 4. 盈亏直方图
    r_vals = [e["R"] for e in equity_curve]
    fig.add_trace(go.Histogram(x=r_vals, name="R分布", marker_color=DARK_THEME["blue"], nbinsx=30), row=3, col=2)

    # 5. 月度热力图
    if monthly:
        months = sorted(monthly.keys())
        fig.add_trace(
            go.Heatmap(
                z=[[monthly[m]] for m in months],
                x=months,
                y=["月度R"],
                colorscale="RdYlGn",
            ),
            row=3,
            col=1,
        )

    # 6. 统计摘要表
    n_trades = len(trades)
    wins = [t for t in trades if float(t.get("R", 0)) > 0]
    losses = [t for t in trades if float(t.get("R", 0)) <= 0]
    win_rate = len(wins) / n_trades * 100 if n_trades else 0
    avg_win = sum(float(t.get("R", 0)) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(float(t.get("R", 0)) for t in losses) / len(losses) if losses else 0
    total_R = sum(float(t.get("R", 0)) for t in trades)
    max_dd = min(d["dd"] for d in dd_curve) if dd_curve else 0
    profit_factor = (
        sum(float(t.get("R", 0)) for t in wins) / abs(sum(float(t.get("R", 0)) for t in losses)) if losses else 0
    )

    fig.add_trace(
        go.Table(
            header=dict(
                values=["指标", "值"], fill=dict(color=DARK_THEME["grid"]), font=dict(color=DARK_THEME["text"])
            ),
            cells=dict(
                values=[
                    ["交易笔数", "胜率", "平均盈利R", "平均亏损R", "总R", "最大回撤R", "盈亏比"],
                    [
                        f"{n_trades}",
                        f"{win_rate:.1f}%",
                        f"{avg_win:.2f}",
                        f"{avg_loss:.2f}",
                        f"{total_R:.2f}",
                        f"{max_dd:.2f}",
                        f"{profit_factor:.2f}",
                    ],
                ],
                fill=dict(color=DARK_THEME["bg"]),
                font=dict(color=DARK_THEME["text"]),
            ),
        ),
        row=3,
        col=2,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=DARK_THEME["bg"],
        plot_bgcolor=DARK_THEME["bg"],
        font=dict(color=DARK_THEME["text"], size=12),
        height=1200,
        showlegend=False,
        title=dict(text="四维策略回测报告", font=dict(size=20, color=DARK_THEME["gold"])),
    )

    html = pio.to_html(fig, full_html=True, include_plotlyjs="cdn")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


def _generate_fallback():
    """无 Plotly 时的兜底。"""
    return None


def data():
    """返回可视化数据（JSON 格式，供 API 消费）。"""
    report = _load_report()
    viz_data = _load_r_series()
    trades = report.get("trades", []) or viz_data.get("r_series", [])

    if not trades:
        trades = _try_journal_r()

    equity = []
    peak = 0.0
    cum = 0.0
    for t in trades:
        R = float(t.get("R", 0))
        cum += R
        if cum > peak:
            peak = cum
        equity.append(
            {"R": round(R, 2), "cum_R": round(cum, 2), "dd": round(cum - peak, 2), "symbol": t.get("symbol", "")}
        )

    n = len(trades)
    wins = [t for t in trades if float(t.get("R", 0)) > 0]
    return {
        "equity_curve": equity,
        "n_trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0,
        "total_R": round(sum(float(t.get("R", 0)) for t in trades), 2),
        "max_dd_R": round(min(e["dd"] for e in equity), 2) if equity else 0,
    }
