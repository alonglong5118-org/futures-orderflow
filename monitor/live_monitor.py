"""
实盘监控编排器 (Phase 7 实盘接入)

功能：
- 从配置的 PnL 数据源拉取实盘交易数据
- 运行参数漂移检测（t 检验 + CUSUM）
- 生成 HTML 监控看板
- 触发告警（控制台 / 邮件 / Webhook）
- 记录监控历史，支持趋势追踪
- 定时运行模式（cron 友好）

用法：
    # 单次运行（生成看板 + 检测漂移 + 输出摘要）
    python -m monitor.live_monitor --config monitor/live_monitor_config.json

    # 仅检测漂移（返回非零退出码表示有 critical 告警）
    python -m monitor.live_monitor --check

    # 仅生成看板
    python -m monitor.live_monitor --dashboard

    # 查看历史趋势
    python -m monitor.live_monitor --history

    # 守护模式（每小时运行一次）
    python -m monitor.live_monitor --daemon --interval 3600

配合 crontab 使用：
    # 每天收盘后 15:30 运行一次
    30 15 * * 1-5 cd /path/to/project && python -m monitor.live_monitor >> monitor/logs/live_monitor.log 2>&1
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from monitor.pnl_data_source import PnLDataSource
from monitor.drift_detector import DriftDetector, DriftAlert
from monitor.dashboard import PerformanceDashboard


# ============================================================================
# 监控历史记录
# ============================================================================

class MonitorHistory:
    """
    监控历史记录器。

    每次运行保存一个快照，用于追踪指标变化趋势。
    """

    def __init__(self, history_dir: str):
        self.history_dir = history_dir
        os.makedirs(history_dir, exist_ok=True)
        self._index_path = os.path.join(history_dir, "history_index.json")

    def _load_index(self) -> Dict[str, Any]:
        if os.path.exists(self._index_path):
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"runs": []}

    def _save_index(self, data: Dict[str, Any]):
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._index_path)

    def record_run(
        self,
        metrics: Dict[str, Dict[str, Any]],
        alerts: List[DriftAlert],
        source_summary: Dict[str, Any],
    ) -> str:
        """记录一次监控运行的快照"""
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 保存详细快照
        snapshot = {
            "run_id": run_id,
            "timestamp": timestamp,
            "metrics": metrics,
            "alerts": [a.to_dict() if hasattr(a, "to_dict") else {
                "symbol": a.symbol,
                "metric": a.metric,
                "severity": a.severity,
                "message": a.message,
                "delta": a.delta,
                "p_value": getattr(a, "p_value", None),
            } for a in alerts],
            "source_summary": source_summary,
            "n_critical": sum(1 for a in alerts if a.severity == "critical"),
            "n_warning": sum(1 for a in alerts if a.severity == "warning"),
        }

        snap_path = os.path.join(self.history_dir, f"snapshot_{run_id}.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # 更新索引
        idx = self._load_index()
        idx["runs"].append({
            "run_id": run_id,
            "timestamp": timestamp,
            "n_critical": snapshot["n_critical"],
            "n_warning": snapshot["n_warning"],
            "total_trades": source_summary.get("total_trades", 0),
            "snapshot_file": os.path.basename(snap_path),
        })
        # 最多保留 100 条索引
        if len(idx["runs"]) > 100:
            idx["runs"] = idx["runs"][-100:]
        self._save_index(idx)

        return run_id

    def get_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        """获取历史运行记录（最近 N 次）"""
        idx = self._load_index()
        runs = idx.get("runs", [])
        return runs[-limit:] if limit else runs

    def get_trend(self, symbol: str, metric: str = "recent_expR", limit: int = 30) -> List[Dict[str, Any]]:
        """获取某品种某指标的历史趋势"""
        idx = self._load_index()
        runs = idx.get("runs", [])[-limit:]
        trend = []
        for run in runs:
            snap_path = os.path.join(self.history_dir, run["snapshot_file"])
            if not os.path.exists(snap_path):
                continue
            try:
                with open(snap_path, "r") as f:
                    snap = json.load(f)
                sym_metrics = snap.get("metrics", {}).get(symbol, {})
                if metric in sym_metrics:
                    trend.append({
                        "timestamp": run["timestamp"],
                        "value": sym_metrics[metric],
                    })
            except Exception:
                pass
        return trend


# ============================================================================
# 告警通知
# ============================================================================

class AlertNotifier:
    """
    告警通知器。

    支持多种通知渠道：
    - console: 控制台输出（默认）
    - file:    写入告警日志文件
    - email:   邮件通知（需要配置 SMTP）
    - webhook: Webhook 回调（如飞书、企业微信、钉钉）
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.channels = config.get("channels", ["console"])
        self.log_file = config.get("log_file", "monitor/logs/alerts.log")

    def send(self, alerts: List[DriftAlert], context: Dict[str, Any] = None) -> bool:
        """发送告警到所有配置的渠道"""
        if not alerts:
            return True

        context = context or {}
        success = True

        for channel in self.channels:
            try:
                if channel == "console":
                    self._send_console(alerts, context)
                elif channel == "file":
                    self._send_file(alerts, context)
                elif channel == "email":
                    self._send_email(alerts, context)
                elif channel == "webhook":
                    self._send_webhook(alerts, context)
                elif channel == "feishu":
                    self._send_feishu(alerts, context)
                else:
                    print(f"[AlertNotifier] 未知通知渠道: {channel}")
            except Exception as e:
                print(f"[AlertNotifier] 渠道 {channel} 发送失败: {e}")
                success = False

        return success

    def _send_console(self, alerts: List[DriftAlert], context: Dict[str, Any]):
        print("\n" + "=" * 60)
        print(f"⚠️  参数漂移告警 ({len(alerts)} 条)")
        print("=" * 60)
        for a in alerts:
            icon = "🔴" if a.severity == "critical" else "🟡"
            print(f"  {icon} [{a.severity.upper()}] {a.symbol} - {a.metric}")
            print(f"     {a.message}")
            if hasattr(a, "p_value") and a.p_value is not None:
                print(f"     p-value: {a.p_value:.4f}")
        print("=" * 60 + "\n")

    def _send_file(self, alerts: List[DriftAlert], context: Dict[str, Any]):
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for a in alerts:
                f.write(f"[{ts}] [{a.severity}] {a.symbol} {a.metric}: {a.message}\n")

    def _send_email(self, alerts: List[DriftAlert], context: Dict[str, Any]):
        """邮件通知（需要配置 SMTP）"""
        email_cfg = self.config.get("email", {})
        if not email_cfg:
            print("[AlertNotifier] 邮件配置为空，跳过")
            return

        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        critical = [a for a in alerts if a.severity == "critical"]
        subject = f"[参数漂移告警] {len(critical)} 个严重 / {len(alerts)} 个总计"

        body_lines = [
            f"监控时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"数据源: {context.get('source', 'unknown')}",
            "",
            "告警详情:",
        ]
        for a in alerts:
            body_lines.append(f"  [{a.severity.upper()}] {a.symbol} - {a.metric}")
            body_lines.append(f"    {a.message}")
            body_lines.append("")

        body = "\n".join(body_lines)

        msg = MIMEMultipart()
        msg["From"] = email_cfg.get("from", "monitor@localhost")
        msg["To"] = email_cfg.get("to", "")
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp_host = email_cfg.get("smtp_host", "localhost")
        smtp_port = email_cfg.get("smtp_port", 587)
        smtp_user = email_cfg.get("smtp_user", "")
        smtp_pass = email_cfg.get("smtp_pass", "")

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_user:
                server.starttls()
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)

    def _send_webhook(self, alerts: List[DriftAlert], context: Dict[str, Any]):
        """Webhook 通知（飞书 / 企业微信 / 钉钉 通用）"""
        webhook_cfg = self.config.get("webhook", {})
        url = webhook_cfg.get("url", "")
        if not url:
            print("[AlertNotifier] Webhook URL 为空，跳过")
            return

        import urllib.request

        critical = [a for a in alerts if a.severity == "critical"]
        warning = [a for a in alerts if a.severity == "warning"]

        # 飞书富文本格式
        content_lines = [
            f"**参数漂移告警**",
            f"监控时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"严重: {len(critical)} 条 | 警告: {len(warning)} 条",
            "",
        ]
        for a in alerts:
            emoji = "🔴" if a.severity == "critical" else "🟡"
            content_lines.append(f"{emoji} **{a.symbol}** - {a.metric}")
            content_lines.append(f"   {a.message}")

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"参数漂移告警 ({len(critical)} 严重 / {len(alerts)} 总计)",
                    },
                    "template": "red" if critical else "orange",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(content_lines),
                    }
                ],
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    def _send_feishu(self, alerts: List[DriftAlert], context: Dict[str, Any]):
        """飞书通知（通过 lark-cli 发送交互式卡片）"""
        feishu_cfg = self.config.get("feishu", {})
        if not feishu_cfg.get("enabled", False):
            print("[AlertNotifier] 飞书通知未启用，跳过")
            return

        chat_id = feishu_cfg.get("chat_id", "")
        if not chat_id:
            print("[AlertNotifier] 飞书 chat_id 为空，跳过")
            return

        try:
            from monitor.feishu_alert import FeishuAlert
        except ImportError as e:
            print(f"[AlertNotifier] 导入 FeishuAlert 失败: {e}")
            return

        cooldown = feishu_cfg.get("alert_cooldown_sec", 3600)
        as_identity = feishu_cfg.get("as_identity", "user")
        alert = FeishuAlert(
            chat_id=chat_id,
            alert_cooldown_sec=cooldown,
            as_identity=as_identity,
        )
        ok = alert.send_drift_alerts(alerts, context)
        if not ok:
            print("[AlertNotifier] 飞书消息发送失败")


# ============================================================================
# 主监控器
# ============================================================================

class LiveMonitor:
    """
    实盘监控主类。

    编排流程：
    1. 从 PnL 数据源拉取交易数据
    2. 计算各品种指标
    3. 运行漂移检测
    4. 生成监控看板
    5. 触发告警
    6. 记录历史
    """

    def __init__(
        self,
        config_path: str,
        output_dir: Optional[str] = None,
    ):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.config_path = config_path
        base_dir = os.path.dirname(os.path.abspath(config_path))

        # 输出目录
        self.output_dir = output_dir or os.path.join(base_dir, "dashboard_output")
        os.makedirs(self.output_dir, exist_ok=True)

        # PnL 数据源
        self.pnl_source = PnLDataSource.from_config(config_path)

        # 漂移检测器
        drift_cfg = self.config.get("drift_detection", {})
        self.detector = DriftDetector(
            warning_delta=drift_cfg.get("warning_delta", 0.05),
            critical_delta=drift_cfg.get("critical_delta", 0.10),
            p_value_threshold=drift_cfg.get("p_value_threshold", 0.10),
            min_trades=drift_cfg.get("min_recent_trades", 5),
            window_size=drift_cfg.get("window_size", 60),
        )

        # 告警通知
        alert_cfg = self.config.get("alerts", {})
        # 解析相对路径
        if "log_file" in alert_cfg:
            log_file = alert_cfg["log_file"]
            if not os.path.isabs(log_file):
                alert_cfg["log_file"] = os.path.join(base_dir, log_file)
        self.notifier = AlertNotifier(alert_cfg)

        # 历史记录
        history_dir = os.path.join(base_dir, self.config.get("history_dir", "history"))
        self.history = MonitorHistory(history_dir)

        # 监控看板（传入同一个检测器实例，确保参数一致）
        self.dashboard = PerformanceDashboard(
            drift_detector=self.detector,
            version=self.config.get("baseline_version", "v001"),
        )

    # ------------------------------------------------------------------
    # 核心运行
    # ------------------------------------------------------------------

    def run(self, generate_dashboard: bool = True, send_alerts: bool = True) -> Dict[str, Any]:
        """
        执行一次完整的监控流程。

        Returns:
            {
                "timestamp": "...",
                "metrics": {...},
                "alerts": [...],
                "dashboard_path": "...",
                "n_critical": N,
                "n_warning": N,
            }
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] 开始实盘监控...")

        # 1. 获取数据源摘要
        source_summary = self.pnl_source.summary()
        print(f"  数据源: {', '.join(source_summary['available_sources'])}")
        print(f"  总交易笔数: {source_summary['total_trades']}")
        print(f"  品种数: {source_summary['total_symbols']}")

        # 2. 计算各品种指标
        metrics = self.pnl_source.get_symbol_metrics()
        active_symbols = [s for s, m in metrics.items() if m["recent_trades"] > 0]
        print(f"  有交易的品种: {len(active_symbols)} 个")

        # 3. 漂移检测
        alerts = self.detector.detect(metrics)
        n_critical = sum(1 for a in alerts if a.severity == "critical")
        n_warning = sum(1 for a in alerts if a.severity == "warning")
        print(f"  漂移告警: {n_critical} 个严重 / {n_warning} 个警告")

        # 4. 生成看板
        dashboard_path = None
        if generate_dashboard:
            dashboard_path = os.path.join(self.output_dir, "live_monitor_dashboard.html")
            self.dashboard.generate(metrics, dashboard_path, window_days=self.pnl_source.recent_window_days)
            print(f"  监控看板: {dashboard_path}")

        # 5. 告警通知
        if send_alerts and alerts:
            self.notifier.send(alerts, context={"source": source_summary["available_sources"]})

        # 6. 记录历史
        self.history.record_run(metrics, alerts, source_summary)

        result = {
            "timestamp": timestamp,
            "metrics": metrics,
            "alerts": alerts,
            "dashboard_path": dashboard_path,
            "n_critical": n_critical,
            "n_warning": n_warning,
            "source_summary": source_summary,
        }

        print(f"[{timestamp}] 监控完成。")
        return result

    def check_only(self) -> int:
        """
        仅检测漂移，不生成看板。

        Returns:
            退出码：0=无告警，1=警告，2=严重
        """
        metrics = self.pnl_source.get_symbol_metrics()
        alerts = self.detector.detect(metrics)

        n_critical = sum(1 for a in alerts if a.severity == "critical")
        n_warning = sum(1 for a in alerts if a.severity == "warning")

        if alerts:
            self.notifier.send(alerts, context={
                "source": self.pnl_source.summary()["available_sources"]
            })

        if n_critical > 0:
            return 2
        elif n_warning > 0:
            return 1
        return 0

    def show_history(self, limit: int = 20):
        """显示历史运行记录"""
        runs = self.history.get_history(limit)
        if not runs:
            print("暂无历史记录")
            return

        print(f"\n{'时间':<20} {'严重':<6} {'警告':<6} {'交易数':<8}")
        print("-" * 45)
        for r in runs:
            print(f"{r['timestamp']:<20} {r['n_critical']:<6} {r['n_warning']:<6} {r['total_trades']:<8}")
        print()

    # ------------------------------------------------------------------
    # 守护模式
    # ------------------------------------------------------------------

    def daemon(self, interval_seconds: int = 3600):
        """
        守护模式：定期运行监控。

        Args:
            interval_seconds: 运行间隔（秒），默认 1 小时
        """
        print(f"[守护模式] 启动，每 {interval_seconds} 秒运行一次")
        print(f"[守护模式] 按 Ctrl+C 停止")

        while True:
            try:
                self.run()
            except Exception as e:
                print(f"[守护模式] 运行出错: {e}")
                import traceback
                traceback.print_exc()

            try:
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                print("\n[守护模式] 已停止")
                break


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="实盘参数漂移监控器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m monitor.live_monitor                        # 完整运行一次
  python -m monitor.live_monitor --check                # 仅检测漂移（退出码表示告警级别）
  python -m monitor.live_monitor --dashboard            # 仅生成看板
  python -m monitor.live_monitor --history              # 查看历史记录
  python -m monitor.live_monitor --daemon --interval 1800  # 守护模式，每30分钟运行一次
  python -m monitor.live_monitor --config my_config.json  # 指定配置文件
        """,
    )
    parser.add_argument("--config", default="monitor/live_monitor_config.json",
                        help="配置文件路径 (默认: monitor/live_monitor_config.json)")
    parser.add_argument("--check", action="store_true",
                        help="仅检测漂移（退出码: 0=正常 1=警告 2=严重）")
    parser.add_argument("--dashboard", action="store_true",
                        help="仅生成监控看板")
    parser.add_argument("--history", action="store_true",
                        help="查看历史运行记录")
    parser.add_argument("--daemon", action="store_true",
                        help="守护模式（定期运行）")
    parser.add_argument("--interval", type=int, default=3600,
                        help="守护模式运行间隔（秒），默认 3600")
    parser.add_argument("--output", type=str, default=None,
                        help="看板输出目录")
    parser.add_argument("--limit", type=int, default=20,
                        help="历史记录显示条数")

    args = parser.parse_args()

    monitor = LiveMonitor(args.config, output_dir=args.output)

    if args.check:
        sys.exit(monitor.check_only())

    elif args.dashboard:
        monitor.run(generate_dashboard=True, send_alerts=False)

    elif args.history:
        monitor.show_history(args.limit)

    elif args.daemon:
        monitor.daemon(args.interval)

    else:
        # 默认：完整运行
        result = monitor.run()
        if result["n_critical"] > 0:
            sys.exit(2)
        elif result["n_warning"] > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
