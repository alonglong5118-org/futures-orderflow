"""四维策略 · 手机推送通道（#15）
=================================
电脑上的横幅/语音你不一定在旁边。关键时刻（信号触发、风控熔断、硬停机）
把消息推到手机，才是真正“管得住”。

本模块封装三种常用手机推送，统一入口 push()：
    1) Telegram  Bot API（最稳，跨平台）
    2) Bark（iOS 专用，秒到，支持自定义服务器）
    3) 企业微信机器人 webhook（国内稳，无需翻墙）

配置来源（env 覆盖文件）：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    BARK_KEY            （Bark 设备 key；自建服务器用 BARK_SERVER，默认 api.day.app）
    WECOM_WEBHOOK       （企业微信机器人 webhook 完整 URL）
或写 push_config.json：
    {"telegram":{"token":..,"chat_id":..},"bark":{"key":..,"server":..},"wecom":{"webhook":..}}

用法：
    import push_notify as pn
    pn.push("玻璃 FG 多单触发，建议 30 手")        # 推所有已启用通道
    pn.push_alert(sig)                            # 推送一条信号（自动格式化）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("push_notify")
log.setLevel(logging.INFO)

_HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(_HERE, "push_config.json")
_TIMEOUT = 8


def _load_cfg():
    cfg = {"telegram": {}, "bark": {}, "wecom": {}}
    if os.path.exists(CFG_FILE):
        try:
            f = json.load(open(CFG_FILE, encoding="utf-8"))
            for k in cfg:
                if isinstance(f.get(k), dict):
                    cfg[k].update(f[k])
        except Exception:
            pass
    # env 覆盖
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram"]["token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("BARK_KEY"):
        cfg["bark"]["key"] = os.environ["BARK_KEY"]
    if os.environ.get("BARK_SERVER"):
        cfg["bark"]["server"] = os.environ["BARK_SERVER"]
    if os.environ.get("WECOM_WEBHOOK"):
        cfg["wecom"]["webhook"] = os.environ["WECOM_WEBHOOK"]
    return cfg


def _save_cfg(cfg):
    try:
        json.dump(cfg, open(CFG_FILE, "w"), ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def channels_status():
    cfg = _load_cfg()
    return {
        "telegram": bool(cfg["telegram"].get("token") and cfg["telegram"].get("chat_id")),
        "bark": bool(cfg["bark"].get("key")),
        "wecom": bool(cfg["wecom"].get("webhook")),
    }


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "da-ge/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _http_post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "da-ge/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _send_telegram(cfg, title, text):
    tok = cfg["telegram"].get("token")
    cid = cfg["telegram"].get("chat_id")
    if not tok or not cid:
        return False, "未配置"
    msg = f"【{title}】\n{text}"
    url = (
        f"https://api.telegram.org/bot{tok}/sendMessage"
        f"?chat_id={urllib.parse.quote(str(cid))}"
        f"&text={urllib.parse.quote(msg)}"
    )
    _http_get(url)
    return True, "ok"


def _send_bark(cfg, title, text):
    key = cfg["bark"].get("key")
    if not key:
        return False, "未配置"
    server = cfg["bark"].get("server") or "https://api.day.app"
    server = server.rstrip("/")
    url = f"{server}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(text)}"
    _http_get(url)
    return True, "ok"


def _send_wecom(cfg, title, text):
    wh = cfg["wecom"].get("webhook")
    if not wh:
        return False, "未配置"
    payload = {"msgtype": "text", "text": {"content": f"【{title}】\n{text}"}}
    _http_post(wh, payload)
    return True, "ok"


def push(text, title="四维策略"):
    """推送到所有已启用通道。返回 {sent:[], failed:[], at}。"""
    cfg = _load_cfg()
    sent, failed = [], []
    dispatchers = [("telegram", _send_telegram), ("bark", _send_bark), ("wecom", _send_wecom)]
    for name, fn in dispatchers:
        try:
            ok, info = fn(cfg, title, text)
            if ok:
                sent.append(name)
            else:
                # 未配置不算失败，静默跳过
                pass
        except Exception as e:
            failed.append(f"{name}:{e}")
    return {"sent": sent, "failed": failed, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def push_alert(sig):
    """推送一条信号/报警（与 notify() 文案对齐）。
    持仓感知v4：如果信号被抑制，仅记录日志不推送。
    """
    # ── 持仓感知v4：信号推送抑制检查 ──
    if sig.get("push_suppressed"):
        hc = sig.get("hold_context") or {}
        reason_map = {
            "cross_dir_locked": "方向锁定",
            "dedup": "信号去重",
            "whipsaw": "Whipsaw噪声",
            "rate_limited": "频率限制",
            "cooldown": "平仓冷却期",
            "conflict": "反向冲突",
            "min_lots": "手数过小",
        }
        reason = "仓位饱和" if hc.get("held") else "未知原因"
        for k, v in reason_map.items():
            if hc.get(k):
                reason = v
                break
        advice = sig.get("action_advice", "")
        log.info(f"[SUPPRESSED] {sig.get('symbol', '?')} {sig.get('direction', '?')} | 原因: {reason} | 建议: {advice}")
        return {"suppressed": True, "reason": reason, "advice": advice}

    at = sig.get("alert_type")
    if at:
        line = (
            f"{sig['name']} {sig['direction']} {at}！持仓 {sig['lots']}手，"
            f"价 {sig.get('price') or '—'} 触及{sig.get('alert_label', '止损')}位 {sig.get('alert_level')}"
        )
    else:
        line = (
            f"{sig['name']} {sig['direction']} 触发，建议 {sig['lots']}手，"
            f"开 {sig.get('price') or '—'} / 损 {sig.get('stop')} / "
            f"t1 {sig.get('t1') or '—'} / t2 {sig.get('target') or '—'}"
        )
    # 持仓感知增强：附加持仓上下文和行动建议
    extra = ""
    hc = sig.get("hold_context") or {}
    if hc.get("held"):
        pnl_val = hc.get("float_pnl", 0)
        pnl_str = f" 浮盈{pnl_val}元" if pnl_val > 0 else (f" 浮亏{abs(pnl_val)}元" if pnl_val < 0 else "")
        extra = f"\n📍 持仓：{hc.get('direction')} {hc.get('lots')}手@{hc.get('avg')}{pnl_str}"
        if hc.get("conflict"):
            extra += " ⚠️方向冲突！"
    elif hc.get("total_positions", 0) > 0:
        extra += f"\n📊 组合：{hc['total_positions']}笔持仓中"
    advice = sig.get("action_advice", "")
    if advice:
        extra += f"\n💡 {advice[:150]}"
    log.info(
        f"[PUSH] {sig.get('symbol', '?')} {sig.get('direction', '?')} | {sig.get('signal_type', '信号')} | {sig.get('lots', 0)}手"
    )
    return push(f"{line}{extra}\n{sig.get('reason', '')}", title="四维信号")


def configure(section, **kw):
    """更新某通道配置并落盘。section ∈ telegram|bark|wecom。"""
    cfg = _load_cfg()
    if section not in cfg:
        return False, "未知通道"
    cfg[section].update(kw)
    ok = _save_cfg(cfg)
    return ok, ("已保存" if ok else "保存失败")


def test():
    """向所有已启用通道发一条测试。"""
    return push("这是一条来自 da龘 的测试推送 ✅", title="推送测试")


if __name__ == "__main__":
    st = channels_status()
    print("通道状态:", st)
    r = push("push_notify 模块自测 ✅")
    print("推送结果:", r)
