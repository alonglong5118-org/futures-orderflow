"""
飞书告警快速测试脚本

用法：
    # 1. 测试文本消息
    python -m monitor.feishu_alert_test --text "测试消息"

    # 2. 测试漂移告警卡片
    python -m monitor.feishu_alert_test --drift

    # 3. 测试灰度评估卡片
    python -m monitor.feishu_alert_test --gray

    # 4. 测试日报卡片
    python -m monitor.feishu_alert_test --daily

    # 指定 chat_id 或 chat_name
    python -m monitor.feishu_alert_test --drift --chat-id oc_xxx
    python -m monitor.feishu_alert_test --drift --chat-name "策略监控群"
"""

import argparse
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from monitor.feishu_alert import FeishuAlert
from monitor.drift_detector import DriftAlert


def test_text(alert: FeishuAlert, text: str):
    """测试文本消息"""
    print(f"发送文本消息: {text}")
    ok = alert.send_text(text)
    print(f"结果: {'成功' if ok else '失败'}")


def test_drift(alert: FeishuAlert):
    """测试漂移告警卡片"""
    alerts = [
        DriftAlert(
            symbol="cu",
            metric="expR",
            severity="critical",
            method="ttest",
            baseline_value=0.85,
            current_value=0.32,
            delta=-0.53,
            delta_pct=-62.4,
            p_value=0.0031,
            message="expR 从 0.85 降至 0.32（-62%），统计显著（p=0.003）",
        ),
        DriftAlert(
            symbol="pp",
            metric="expR",
            severity="warning",
            method="cusum",
            baseline_value=0.78,
            current_value=0.55,
            delta=-0.23,
            delta_pct=-29.5,
            p_value=0.078,
            message="expR 从 0.78 降至 0.55（-30%），CUSUM 触发上界",
        ),
        DriftAlert(
            symbol="TA",
            metric="trades",
            severity="warning",
            method="ttest",
            baseline_value=12.5,
            current_value=8.2,
            delta=-4.3,
            delta_pct=-34.4,
            p_value=0.056,
            message="月均交易数从 12.5 降至 8.2（-34%）",
        ),
    ]

    print(f"发送漂移告警卡片 ({len(alerts)} 条)")
    ok = alert.send_drift_alerts(alerts, {"source": "测试数据"})
    print(f"结果: {'成功' if ok else '失败'}")


def test_gray(alert: FeishuAlert):
    """测试灰度评估卡片"""
    checks = [
        {"name": "expR 比例", "value": "85%", "threshold": "≥ 70%", "pass": True},
        {"name": "最大回撤", "value": "1.2R", "threshold": "≤ 2.0R", "pass": True},
        {"name": "胜率变化", "value": "-3%", "threshold": "≥ -5%", "pass": True},
        {"name": "交易频率", "value": "12笔/月", "threshold": "≥ 8笔/月", "pass": True},
    ]
    print("发送灰度评估卡片 (PASS)")
    ok = alert.send_gray_alert("Batch1 (pp, pg)", "pass", checks, {"days": 21})
    print(f"结果: {'成功' if ok else '失败'}")


def test_daily(alert: FeishuAlert):
    """测试日报卡片"""
    summary = {
        "total_R": 2.45,
        "n_trades": 8,
        "win_rate": 0.625,
        "per_symbol": {
            "cu": {"expR": 1.23, "trades": 2},
            "al": {"expR": 0.85, "trades": 1},
            "zn": {"expR": -0.42, "trades": 1},
            "TA": {"expR": 0.56, "trades": 1},
            "pp": {"expR": 0.23, "trades": 2},
            "y": {"expR": -0.15, "trades": 1},
        },
    }
    print("发送每日日报卡片")
    ok = alert.send_daily_report(summary)
    print(f"结果: {'成功' if ok else '失败'}")


def main():
    parser = argparse.ArgumentParser(description="飞书告警测试")
    parser.add_argument("--chat-id", default="", help="飞书群聊 ID (oc_xxx)")
    parser.add_argument("--chat-name", default="", help="群聊名称（用于搜索）")
    parser.add_argument("--text", default="", help="发送纯文本消息")
    parser.add_argument("--drift", action="store_true", help="测试漂移告警卡片")
    parser.add_argument("--gray", action="store_true", help="测试灰度评估卡片")
    parser.add_argument("--daily", action="store_true", help="测试日报卡片")
    args = parser.parse_args()

    if not args.chat_id and not args.chat_name:
        print("请指定 --chat-id 或 --chat-name")
        print("示例:")
        print("  python -m monitor.feishu_alert_test --drift --chat-id oc_xxx")
        print("  python -m monitor.feishu_alert_test --daily --chat-name '策略监控群'")
        sys.exit(1)

    alert = FeishuAlert(
        chat_id=args.chat_id,
        chat_name=args.chat_name,
        alert_cooldown_sec=0,  # 测试时禁用冷却
    )

    if args.text:
        test_text(alert, args.text)

    if args.drift:
        test_drift(alert)

    if args.gray:
        test_gray(alert)

    if args.daily:
        test_daily(alert)

    if not any([args.text, args.drift, args.gray, args.daily]):
        print("请指定测试类型: --text / --drift / --gray / --daily")
        sys.exit(1)


if __name__ == "__main__":
    main()
