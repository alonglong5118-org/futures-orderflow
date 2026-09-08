#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ctp_account_feed.py — 直连期货公司 CTP 柜台，查询实盘资金/持仓，写入 account_monitor_ctp.json
===============================================================================================
【为什么有这个文件】
  天勤免费版拦截实盘账户查询（报"需要专业版"），专业版 ¥9988/年。但「查实盘持仓/资金」
  这件事本身绕开天勤、直连期货公司 CTP 柜台是 0 元（CTP 接口 2026 年主流 AA 级期货公司
  已全面免费）。本脚本用开源 ctp-python 直连，彻底绕开天勤付费墙。

【在哪跑】（重要·Mac 原生跑不了 CTP）
  CTP 官方只提供 Linux(.so) / Windows(.dll) 的底层库，没有 macOS 版。所以本脚本必须跑在
  Linux 环境：
    · 你的 AutoDL GPU 实例（Linux，最省事，复用已有资源），或
    · 本地 Docker(Linux 容器)，把 fourd_run 目录 volume 挂进去，JSON 直接写回 Mac 本地
  跑完把 account_monitor_ctp.json 放到 fourd_run/ 目录（与 account_monitor.py 同目录）。
  Mac 上的 account_monitor.py(ctp 后端) 只【读】这个 JSON，零编译依赖。

【依赖】
  pip install openctp-ctp-python        # 提供 ctpapi 模块（TraderApi 等）
  （若导入名不同，把 `from ctpapi import TraderApi` 改为 `from openctp_ctp import TraderApi`）

【配置】
  同级目录放 ctp_account_feed.json（参考 ctp_account_feed.json.example）：
    {
      "output_path": "account_monitor_ctp.json",   # 写到哪（Docker 挂载则直接落 Mac 本地）
      "poll_seconds": 30,                            # 轮询间隔；<=0 只查一次就退出（适合 cron）
      "accounts": [
        {"name":"西南期货","broker_id":"<西南BrokerID>","investor_id":"<你的资金账号>","password":"<交易密码>",
         "td_front":"tcp://<西南期货实盘交易前置IP:端口>","app_id":"<期货公司给的AppID>","auth_code":"<AuthCode>"},
        {"name":"中信期货","broker_id":"<中信BrokerID>","investor_id":"<你的资金账号>","password":"<交易密码>",
         "td_front":"tcp://<中信实盘交易前置IP:端口>","app_id":"<AppID>","auth_code":"<AuthCode>"}
      ]
    }
  broker_id / td_front / app_id / auth_code 全部来自期货公司，网上查不到真实值。
  注：示例中的资金账号已脱敏为占位符 —— 真实 ctp_account_feed.json 不入版本库。

【运行】
  python ctp_account_feed.py            # 常驻：每 poll_seconds 秒刷新一次快照
  python ctp_account_feed.py once       # 只查一次就退出（配合 cron / 手动触发）
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "ctp_account_feed.json")
OUT_DEFAULT = os.path.join(HERE, "account_monitor_ctp.json")

# openctp-ctp-python 的导入名通常是 ctpapi；若你的环境是 openctp_ctp 请改下面这行
try:
    from ctpapi import TraderApi
except Exception:
    try:
        from openctp_ctp import TraderApi
    except Exception as e:
        print("[ctp_feed] 未安装 ctp-python：pip install openctp-ctp-python  （须在 Linux/Windows 环境）")
        raise


def _val(data, key, default=0.0):
    """兼容 data 是对象(属性)或 dict 两种形态。"""
    if data is None:
        return default
    if isinstance(data, dict):
        v = data.get(key, default)
    else:
        v = getattr(data, key, default)
    try:
        return float(v) if isinstance(default, float) else (int(v) if isinstance(default, int) else v)
    except Exception:
        return default


class FeedTrader(TraderApi):
    """单个期货账户的 CTP 查询器。查询完成(is_last 两路到齐)后置 _done。"""

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.col = {
            "name": cfg.get("name"),
            "broker_id": cfg.get("broker_id"),
            "investor_id": cfg.get("investor_id"),
            "balance": 0.0, "available": 0.0, "profit": 0.0,
            "positions_raw": [],
            "_account_done": False, "_position_done": False,
        }
        self._done = threading.Event()

    # ---- 连接 / 登录 / 穿透式认证 ----
    def OnFrontConnected(self):
        req = {
            "BrokerID": self.cfg["broker_id"],
            "UserID": self.cfg["investor_id"],
            "ProductInfo": "openctp",
            "AuthCode": self.cfg["auth_code"],
            "AppID": self.cfg["app_id"],
        }
        self.ReqAuthenticate(req, 0)

    def OnRspAuthenticate(self, data, error, request_id, is_last):
        err = error.get("ErrorID") if isinstance(error, dict) else getattr(error, "ErrorID", None)
        if err:
            print(f"  [{self.cfg.get('name')}] 穿透式认证失败: {error}")
            self._done.set()
            return
        req = {
            "BrokerID": self.cfg["broker_id"],
            "UserID": self.cfg["investor_id"],
            "Password": self.cfg["password"],
        }
        self.ReqUserLogin(req, 0)

    def OnRspUserLogin(self, data, error, request_id, is_last):
        err = error.get("ErrorID") if isinstance(error, dict) else getattr(error, "ErrorID", None)
        if err:
            print(f"  [{self.cfg.get('name')}] 登录失败: {error}")
            self._done.set()
            return
        print(f"  [{self.cfg.get('name')}] 登录成功，开始查询账户/持仓…")
        self.ReqQryTradingAccount(
            {"BrokerID": self.cfg["broker_id"], "InvestorID": self.cfg["investor_id"]}, 1)
        self.ReqQryInvestorPosition(
            {"BrokerID": self.cfg["broker_id"], "InvestorID": self.cfg["investor_id"], "InstrumentID": ""}, 2)

    def OnRspQryTradingAccount(self, data, error, request_id, is_last):
        if data is not None:
            self.col["balance"] = _val(data, "Balance", 0.0)
            self.col["available"] = _val(data, "Available", 0.0)
            self.col["profit"] = _val(data, "PositionProfit", 0.0)
        if is_last:
            self.col["_account_done"] = True
            self._check_done()

    def OnRspQryInvestorPosition(self, data, error, request_id, is_last):
        if data is not None:
            inst = _val(data, "InstrumentID", "")
            if inst:
                pd = _val(data, "PosiDirection", "")
                direction = "多" if str(pd) == "2" else ("空" if str(pd) == "3" else "?")
                self.col["positions_raw"].append({
                    "instrument": inst,
                    "direction": direction,
                    "volume": int(_val(data, "Position", 0)),
                    "open_price": _val(data, "OpenPrice", 0.0),
                    "margin": _val(data, "Margin", 0.0),
                })
        if is_last:
            self.col["_position_done"] = True
            self._check_done()

    def _check_done(self):
        if self.col["_account_done"] and self.col["_position_done"]:
            self._done.set()

    def run(self, timeout: float = 25.0) -> dict:
        """连接→登录→查询→等待完成，返回收集到的 col。"""
        try:
            self.RegisterFront(self.cfg["td_front"])
            self.Init()
            self._done.wait(timeout=timeout)
        except Exception as e:
            print(f"  [{self.cfg.get('name')}] 异常: {repr(e)[:120]}")
        finally:
            try:
                self.Release()
            except Exception:
                pass
        return self.col


def query_account(cfg: dict) -> dict:
    return FeedTrader(cfg).run()


def main():
    once = len(sys.argv) > 1 and sys.argv[1] == "once"

    if not os.path.exists(CFG_PATH):
        example = {
            "output_path": "account_monitor_ctp.json",
            "poll_seconds": 30,
            "accounts": [
                {"name": "西南期货", "broker_id": "<西南BrokerID>", "investor_id": "<你的资金账号>", "password": "<交易密码>",
                 "td_front": "tcp://<西南期货实盘交易前置IP:端口>", "app_id": "<期货公司给的AppID>", "auth_code": "<AuthCode>"},
                {"name": "中信期货", "broker_id": "<中信BrokerID>", "investor_id": "<你的资金账号>", "password": "<交易密码>",
                 "td_front": "tcp://<中信实盘交易前置IP:端口>", "app_id": "<AppID>", "auth_code": "<AuthCode>"},
            ],
        }
        with open(CFG_PATH + ".example", "w", encoding="utf-8") as f:
            json.dump(example, f, ensure_ascii=False, indent=2)
        print(f"[ctp_feed] 未找到 {CFG_PATH}，已生成示例 {CFG_PATH}.example，填好参数后重命名运行")
        return

    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    out_path = cfg.get("output_path") or OUT_DEFAULT
    if not os.path.isabs(out_path):
        out_path = os.path.join(HERE, out_path)
    poll = int(cfg.get("poll_seconds") or 30)

    print(f"[ctp_feed] 启动，账户数={len(cfg.get('accounts', []))}，输出={out_path}")

    while True:
        accounts = []
        for ac in cfg.get("accounts", []):
            try:
                accounts.append(query_account(ac))
            except Exception as e:
                print(f"  [{ac.get('name')}] 查询失败: {repr(e)[:120]}")
        snap = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "ctp_direct",
            "accounts": accounts,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        ok = sum(1 for a in accounts if a.get("_account_done"))
        print(f"[ctp_feed] 已写入 {out_path}（成功 {ok}/{len(accounts)} 个账户）")
        if once or poll <= 0:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
