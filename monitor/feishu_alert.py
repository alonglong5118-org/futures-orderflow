"""
飞书告警通知模块

功能：
- 通过飞书 Bot 发送告警消息到指定群聊
- 支持交互式卡片（富文本）
- 支持漂移告警、灰度监控告警、日报等多种模板
- 告警去重（相同告警 1 小时内不重复发送）
- 告警分级（critical / warning / info）

用法：
    from monitor.feishu_alert import FeishuAlert
    alert = FeishuAlert(chat_id="oc_xxx")
    alert.send_drift_alerts(alerts_list, context)

依赖：
    lark-cli（飞书命令行工具）
    需先配置飞书应用：lark-cli config init
"""

import json
import os
import subprocess
import hashlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FeishuAlert:
    """
    飞书告警发送器。

    使用 lark-cli 发送消息，支持 bot 身份。
    """

    def __init__(
        self,
        chat_id: str = "",
        chat_name: str = "",
        alert_cooldown_sec: int = 3600,
        log_dir: str = "",
        as_identity: str = "user",
    ):
        """
        Args:
            chat_id: 飞书群聊 ID（oc_ 开头），优先用 chat_id
            chat_name: 群聊名称（用于搜索 chat_id）
            alert_cooldown_sec: 相同告警的冷却时间（秒），默认 1 小时
            log_dir: 告警日志目录
            as_identity: 发送身份，"user" 或 "bot"，默认 user
        """
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.cooldown = alert_cooldown_sec
        self.log_dir = log_dir or os.path.join(SCRIPT_DIR, "monitor", "logs")
        self.as_identity = as_identity
        self._dedup_file = os.path.join(self.log_dir, "feishu_alert_dedup.json")
        self._dedup_cache: Dict[str, float] = {}
        self._load_dedup()

        # 如果有 chat_name 但没有 chat_id，尝试搜索
        if chat_name and not chat_id:
            self.chat_id = self._find_chat_by_name(chat_name)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def send_drift_alerts(
        self,
        alerts: List[Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        发送参数漂移告警。

        Args:
            alerts: DriftAlert 对象列表
            context: 上下文信息（数据源、监控时间等）

        Returns:
            是否发送成功
        """
        if not alerts:
            return True

        context = context or {}

        # 去重检查
        dedup_key = self._make_dedup_key("drift", alerts)
        if not self._check_dedup(dedup_key):
            print(f"[FeishuAlert] 漂移告警在冷却期内，跳过 ({len(alerts)} 条)")
            return True

        # 构建卡片
        critical = [a for a in alerts if getattr(a, "severity", "") == "critical"]
        warning = [a for a in alerts if getattr(a, "severity", "") == "warning"]

        title = f"参数漂移告警：{len(critical)} 严重 / {len(warning)} 警告"
        template = "red" if critical else "orange"

        elements = []
        # 概览
        elements.append({
            "tag": "markdown",
            "content": (
                f"**监控时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"**数据源**：{context.get('source', '实盘')}\n"
                f"**告警总数**：{len(alerts)} 条（🔴 {len(critical)} / 🟡 {len(warning)}）"
            ),
        })

        # 告警详情（分等级）
        if critical:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": "**🔴 严重告警**",
            })
            for a in critical:
                elements.append(self._alert_markdown(a, "🔴"))

        if warning:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": "**🟡 警告**",
            })
            for a in warning:
                elements.append(self._alert_markdown(a, "🟡"))

        # 底部操作
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "📊 期货策略监控系统 · 自动告警"},
            ],
        })

        success = self._send_card(title, template, elements)
        if success:
            self._mark_dedup(dedup_key)
        return success

    def send_gray_alert(
        self,
        batch_name: str,
        verdict: str,
        checks: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        发送灰度监控告警。

        Args:
            batch_name: 批次名称
            verdict: pass / fail / warning
            checks: 检查项列表
            context: 上下文
        """
        context = context or {}

        dedup_key = f"gray_{batch_name}_{verdict}"
        if not self._check_dedup(dedup_key):
            print(f"[FeishuAlert] 灰度告警在冷却期内，跳过: {batch_name}")
            return True

        if verdict == "pass":
            title = f"✅ 灰度批次通过：{batch_name}"
            template = "green"
        elif verdict == "fail":
            title = f"❌ 灰度批次失败：{batch_name}"
            template = "red"
        else:
            title = f"⚠️ 灰度批次警告：{batch_name}"
            template = "orange"

        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"**批次**：{batch_name}\n"
                    f"**评估时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"**结果**：{verdict.upper()}"
                ),
            },
            {"tag": "hr"},
        ]

        for c in checks:
            status = "✅" if c.get("pass") else "❌"
            elements.append({
                "tag": "markdown",
                "content": f"{status} **{c.get('name', '')}**：{c.get('value', '')} (阈值: {c.get('threshold', '')})",
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "📊 灰度上线监控 · 自动评估"},
            ],
        })

        success = self._send_card(title, template, elements)
        if success:
            self._mark_dedup(dedup_key)
        return success

    def send_daily_report(
        self,
        summary: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        发送每日日报。

        Args:
            summary: 日报数据（总收益、交易数、各品种表现等）
        """
        title = f"📊 每日策略日报 - {datetime.now().strftime('%Y-%m-%d')}"
        template = "blue"

        total_r = summary.get("total_R", 0)
        n_trades = summary.get("n_trades", 0)
        win_rate = summary.get("win_rate", 0)

        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"**日期**：{datetime.now().strftime('%Y-%m-%d')}\n"
                    f"**总收益**：{total_r:+.2f} R\n"
                    f"**交易笔数**：{n_trades} 笔\n"
                    f"**胜率**：{win_rate*100:.0f}%"
                ),
            },
            {"tag": "hr"},
        ]

        # 各品种表现
        per_symbol = summary.get("per_symbol", {})
        if per_symbol:
            lines = ["**各品种表现**："]
            # 按收益排序
            sorted_syms = sorted(per_symbol.items(), key=lambda x: -x[1].get("expR", 0))
            for sym, data in sorted_syms[:8]:
                expR = data.get("expR", 0)
                n = data.get("trades", 0)
                emoji = "🟢" if expR > 0 else ("🔴" if expR < 0 else "⚪")
                lines.append(f"{emoji} **{sym}**：{expR:+.2f}R ({n}笔)")
            elements.append({
                "tag": "markdown",
                "content": "\n".join(lines),
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "📈 期货策略监控系统 · 每日自动推送"},
            ],
        })

        # 日报不做去重（每天一次）
        return self._send_card(title, template, elements)

    def send_text(self, text: str) -> bool:
        """发送纯文本消息"""
        return self._send_text(text)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _alert_markdown(self, alert: Any, icon: str) -> Dict[str, Any]:
        """构建单条告警的 markdown 元素"""
        sym = getattr(alert, "symbol", "?")
        metric = getattr(alert, "metric", "?")
        msg = getattr(alert, "message", "")
        delta = getattr(alert, "delta", 0)
        p_val = getattr(alert, "p_value", None)

        line = f"{icon} **{sym}** - {metric}\n   {msg}"
        if p_val is not None:
            line += f"\n   p-value: {p_val:.4f}"

        return {"tag": "markdown", "content": line}

    def _send_card(self, title: str, template: str, elements: List[Dict]) -> bool:
        """发送交互式卡片消息（通过 lark-cli）"""
        if not self.chat_id:
            print("[FeishuAlert] 未配置 chat_id，跳过发送")
            return False

        payload = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        }

        # 用 lark-cli 发送
        try:
            cmd = [
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.chat_id,
                "--msg-type", "interactive",
                "--content", json.dumps(payload, ensure_ascii=False),
                "--as", self.as_identity,
                "--format", "json",
            ]
            env = os.environ.copy()
            env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
            env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
                cwd=SCRIPT_DIR,
            )

            if result.returncode == 0:
                print(f"[FeishuAlert] 卡片消息发送成功: {title}")
                return True
            else:
                err = result.stderr.strip() or result.stdout.strip()
                print(f"[FeishuAlert] 卡片消息发送失败: {err[:200]}")
                return False

        except subprocess.TimeoutExpired:
            print("[FeishuAlert] 发送超时")
            return False
        except Exception as e:
            print(f"[FeishuAlert] 发送异常: {e}")
            return False

    def _send_text(self, text: str) -> bool:
        """发送纯文本消息"""
        if not self.chat_id:
            print("[FeishuAlert] 未配置 chat_id，跳过发送")
            return False

        try:
            content = json.dumps({"text": text}, ensure_ascii=False)
            cmd = [
                "lark-cli", "im", "+messages-send",
                "--chat-id", self.chat_id,
                "--msg-type", "text",
                "--content", content,
                "--as", self.as_identity,
                "--format", "json",
            ]
            env = os.environ.copy()
            env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
            env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, env=env, cwd=SCRIPT_DIR,
            )

            if result.returncode == 0:
                print(f"[FeishuAlert] 文本消息发送成功")
                return True
            else:
                err = result.stderr.strip() or result.stdout.strip()
                print(f"[FeishuAlert] 文本发送失败: {err[:200]}")
                return False
        except Exception as e:
            print(f"[FeishuAlert] 发送异常: {e}")
            return False

    def _find_chat_by_name(self, name: str) -> str:
        """根据群名搜索 chat_id"""
        try:
            cmd = [
                "lark-cli", "im", "+chat-search",
                "--query", name,
                "--as", self.as_identity,
                "--format", "json",
            ]
            env = os.environ.copy()
            env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
            env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, env=env, cwd=SCRIPT_DIR,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                items = data.get("data", {}).get("items", [])
                if items:
                    chat_id = items[0].get("chat_id", "")
                    print(f"[FeishuAlert] 找到群聊: {name} -> {chat_id}")
                    return chat_id
        except Exception as e:
            print(f"[FeishuAlert] 搜索群聊失败: {e}")
        return ""

    # ------------------------------------------------------------------
    # 去重机制
    # ------------------------------------------------------------------

    def _make_dedup_key(self, alert_type: str, alerts: List[Any]) -> str:
        """生成告警去重 key"""
        # 按品种+指标排序，确保相同告警集合生成相同 key
        keys = sorted(
            f"{getattr(a, 'symbol', '')}_{getattr(a, 'metric', '')}_{getattr(a, 'severity', '')}"
            for a in alerts
        )
        raw = f"{alert_type}_{'|'.join(keys)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _load_dedup(self):
        """加载去重缓存"""
        if os.path.exists(self._dedup_file):
            try:
                with open(self._dedup_file, "r") as f:
                    self._dedup_cache = json.load(f)
            except Exception:
                self._dedup_cache = {}

    def _check_dedup(self, key: str) -> bool:
        """检查是否在冷却期内。True=可以发送，False=冷却中"""
        now = time.time()
        last = self._dedup_cache.get(key, 0)
        return (now - last) >= self.cooldown

    def _mark_dedup(self, key: str):
        """标记已发送"""
        self._dedup_cache[key] = time.time()
        self._save_dedup()

    def _save_dedup(self):
        """保存去重缓存（清理过期条目）"""
        now = time.time()
        # 清理超过 24 小时的条目
        self._dedup_cache = {
            k: v for k, v in self._dedup_cache.items()
            if now - v < 86400 * 7  # 保留 7 天
        }
        os.makedirs(os.path.dirname(self._dedup_file), exist_ok=True)
        try:
            with open(self._dedup_file, "w") as f:
                json.dump(self._dedup_cache, f)
        except Exception:
            pass
