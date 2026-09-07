"""四维策略 · 账户监控（驱动 papertrack 自动化，从 da龘 移植并适配）
==============================================================
da龘 用 TqSdk 的 TqAccount 只读读取真实账户（权益/持仓），四维原本只有
「手动记账」(account_tracker / trade_journal)。本模块把 da龘 的账户读取能力
接进四维：**若模拟盘/实盘账户有可读取接口，自动同步权益与持仓 → 自动驱动
papertrack 评分与状态机**，否则优雅降级为手动记账（不影响现有流程）。

后端可插拔（account_monitor.json 配 backend）：
  - "tqsdk"  : 天勤/快期 TqAccount（da龘 同款只读账户监控，需 pip install tqsdk）
              ★ 天勤免费版拦截实盘账户查询（报"需要专业版"），查实盘需专业版 ¥9988/年
  - "ctp"/"snapshot" : 只读 account_monitor_ctp.json 快照（0 元，绕开天勤付费墙）。
              快照可由两种 feed 写入，格式一致：
                · ctp_account_feed.py   —— 直连期货公司 CTP 柜台（需期货公司参数+Linux环境，你目前拿不到参数）
                · cfmmc_statement_parser.py —— 解析「中国期货市场监控中心 cfmmc.com 每日结算单」
                  ★ 推荐：完全免费、无需任何期货公司参数、一处查全你名下所有期货公司账户
  - "manual" / 未配置 : 不读取，四维维持手动记账

自动同步逻辑（auto_sync）：
  - 账户权益 → account_tracker.set_equity()
  - 账户持仓 → 与已镜像持仓对比：
      新开仓(持仓出现且未镜像) → trade_journal.record_entry + account_tracker open
      已平仓(镜像中存在但账户无) → trade_journal.record_exit(现价) + account_tracker close
  用模块级 _synced_open 去重，避免重复记成交。

用法（runner 后台线程调用）：
  import account_monitor as am
  acc = am.get_account()          # None = 无接口（手动模式）
  if acc:
      am.auto_sync(acc, prices={sym: feed.price(sym) for sym in SYMBOLS})
"""

from __future__ import annotations

import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "account_monitor.json")

_lock = threading.Lock()
_synced_open = {}  # (sym, direction) -> {"lots","price"} 已镜像到成交记录器的持仓
_last_account = None  # 最近一次账户快照（供状态机/面板读取）
_last_fetch = 0


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"enabled": False, "backend": "manual", "broker_id": "", "account_id": "", "password": ""}
    try:
        return json.load(open(CONFIG_FILE, encoding="utf-8"))
    except Exception:
        return {"enabled": False, "backend": "manual"}


def _map_symbol(tqsdk_sym):
    """tqsdk 合约代码 -> 四维内部 sym。
    规则：① 优先按完整合约代码映射（CZCE.SA2701 -> 'SA01'，
            实现逐合约持仓拆卡）；② 否则剥掉月份数字取品种字母
            （CZCE.FG2609 -> 'FG'、CZCE.SA -> 'SA'、DCE.m|JM2609 -> 'JM'）。"""
    try:
        import re

        import four_dim_strategy as fd

        s = str(tqsdk_sym).upper()
        # 去交易所前缀 (如 DCE.m| / CZCE. / KQ.m@)
        if "|" in s:
            s = s.split("|")[-1]
        if "." in s:
            s = s.split(".")[-1]
        code = s.strip()  # e.g. SA2609 / SA / FG2609
        # ① 逐合约优先：SA2701 -> SA01
        if code in fd.CONTRACT_SYM_BY_CODE:
            return fd.CONTRACT_SYM_BY_CODE[code]
        # ② 品种级（剥月份数字）
        m = re.match(r"([A-Za-z]+)", code)
        if m:
            key = m.group(1).upper()
            if key in fd.SYMBOLS:
                return key
            # 主连代码（如 JMM->JM, FGM->FG）
            if key.endswith("M") and key[:-1] in fd.SYMBOLS:
                return key[:-1]
    except Exception:
        pass
    return None


def get_account():
    """返回账户快照 dict 或 None（无接口）。
    dict = {"balance","available","profit","positions":[{symbol,pos,open_price,direction,margin}],"updated"}"""
    cfg = load_config()
    if not cfg.get("enabled", False):
        return None
    backend = cfg.get("backend", "manual")
    if backend == "tqsdk":
        return _get_tqsdk_account(cfg)
    if backend in ("ctp", "snapshot"):
        return _get_snapshot_account(cfg)
    # 其他后端可在此扩展
    return None


def _get_tqsdk_account(cfg):
    """TqSdk 只读账户（da龘 同款逻辑）。失败/未装返回 None。"""
    global _last_account, _last_fetch
    try:
        from tqsdk import TqAccount, TqAuth, TqApi
    except Exception:
        print("[账户监控] tqsdk 未安装，跳过自动读取（维持手动记账）")
        return None
    try:
        # ★ 2026-09-07 修复：TqSdk 3.x 必须先 TqAuth（天勤行情账号）再 TqAccount（期货公司实盘）
        # 从天勤配置文件读取行情账号（存在 tq_config.json 里）
        _tq_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tq_config.json")
        _tq_username = None
        _tq_password = None
        if os.path.exists(_tq_cfg_path):
            try:
                _tqc = json.load(open(_tq_cfg_path))
                _tq_username = _tqc.get("tq_username")
                _tq_password = _tqc.get("tq_password")
            except Exception:
                pass
        if not _tq_username or not _tq_password:
            # 回退：从 cfg 本身读
            _tq_username = cfg.get("tq_username")
            _tq_password = cfg.get("tq_password")
        if not _tq_username or not _tq_password:
            raise Exception("缺少 TqAuth 认证：tq_config.json 里没有 tq_username/tq_password")
        
        api = TqApi(
            auth=TqAuth(_tq_username, _tq_password),
            account=TqAccount(cfg.get("broker_id", ""), cfg.get("account_id", ""), cfg.get("password", ""))
        )
        acc = api.get_account()
        pos_obj = api.get_position()
        balance = float(acc.get("balance", 0) or 0)
        available = float(acc.get("available", 0) or 0)
        profit = float(acc.get("profit", 0) or 0)
        positions = []
        # pos_obj 是 dict-like：key=合约代码, value=持仓对象
        for k in pos_obj or {}:
            try:
                p = pos_obj[k]
                sym = _map_symbol(k)
                if sym is None:
                    continue
                long_vol = int(p.get("volume_long", 0) or 0)
                short_vol = int(p.get("volume_short", 0) or 0)
                if long_vol > 0:
                    positions.append(
                        {
                            "symbol": sym,
                            "pos": long_vol,
                            "open_price": float(p.get("open_price_long", 0) or 0),
                            "direction": "多",
                            "margin": float(p.get("margin_long", 0) or 0),
                        }
                    )
                if short_vol > 0:
                    positions.append(
                        {
                            "symbol": sym,
                            "pos": short_vol,
                            "open_price": float(p.get("open_price_short", 0) or 0),
                            "direction": "空",
                            "margin": float(p.get("margin_short", 0) or 0),
                        }
                    )
            except Exception:
                continue
        api.close()
        snap = {
            "balance": balance,
            "available": available,
            "profit": profit,
            "positions": positions,
            "updated": time.strftime("%H:%M:%S"),
            "backend": "tqsdk",
        }
        with _lock:
            _last_account = snap
            _last_fetch = time.time()
        return snap
    except Exception as e:
        print(f"[账户监控] TqAccount 读取失败: {repr(e)[:120]}（维持手动记账）")
        return None


def auto_sync(account, prices=None):
    """把账户权益/持仓自动同步进 account_tracker + trade_journal。"""
    if not account:
        return
    prices = prices or {}
    try:
        import account_tracker as at
        import trade_journal as tj

        # 1) 权益同步
        at.set_equity(account["balance"])
        # 2) 持仓镜像对账
        current = {}  # (sym, direction) -> {lots, price}
        for p in account.get("positions", []):
            key = (p["symbol"], p["direction"])
            current[key] = {"lots": p["pos"], "price": p["open_price"]}
        with _lock:
            synced = dict(_synced_open)
        # 新开仓：账户有、未镜像
        for key, val in current.items():
            sym, direction = key
            if key not in synced:
                tj.record_entry(sym, direction, val["lots"], val["price"], signal_id="auto_account")
                at.record_trade(sym, "open", direction, val["lots"], val["price"])
                synced[key] = val
        # 已平仓：镜像有、账户无
        for key, val in list(synced.items()):
            if key not in current:
                sym, direction = key
                px = prices.get(sym)
                if px is not None:
                    tj.record_exit(sym, direction, val["lots"], px, reason="账户同步平仓")
                    at.record_trade(sym, "close", direction, val["lots"], px)
                del synced[key]
        with _lock:
            _synced_open.clear()
            _synced_open.update(synced)
    except Exception as e:
        print(f"[账户监控] 自动同步异常: {repr(e)[:120]}")


def _get_snapshot_account(cfg):
    """快照读取（0 元方案，绕开天勤专业版付费墙）。
    读 account_monitor_ctp.json —— 该快照格式无关，可由两种 feed 写入：
      · ctp_account_feed.py        （直连期货公司 CTP 柜台，需参数+Linux）
      · cfmmc_statement_parser.py  （解析 cfmmc.com 每日结算单，免费、无需参数 ★）
    不依赖 ctp-python / 天勤，Mac 上零编译风险。找不到快照则降级 None。
    """
    global _last_account, _last_fetch
    ctp_path = cfg.get("ctp_snapshot") or os.path.join(HERE, "account_monitor_ctp.json")
    if not os.path.exists(ctp_path):
        print(f"[账户监控] 未找到 CTP 快照 {ctp_path}（ctp_account_feed.py 尚未运行？维持手动记账）")
        return None
    try:
        snap = json.load(open(ctp_path, encoding="utf-8"))
    except Exception as e:
        print(f"[账户监控] CTP 快照读取失败: {repr(e)[:120]}（维持手动记账）")
        return None
    accounts = snap.get("accounts", [])
    if not accounts:
        return None
    total_balance = total_avail = total_profit = 0.0
    agg = {}  # (sym, direction) -> {"lots","cost","margin"}
    for acc in accounts:
        total_balance += float(acc.get("balance") or 0)
        total_avail += float(acc.get("available") or 0)
        total_profit += float(acc.get("profit") or 0)
        for p in acc.get("positions_raw", []):
            sym = _map_symbol(p.get("instrument", ""))
            if not sym:
                continue
            direction = p.get("direction", "多")
            key = (sym, direction)
            lots = int(p.get("volume") or 0)
            price = float(p.get("open_price") or 0)
            margin = float(p.get("margin") or 0)
            if key not in agg:
                agg[key] = {"lots": 0, "cost": 0.0, "margin": 0.0}
            agg[key]["cost"] += price * lots
            agg[key]["lots"] += lots
            agg[key]["margin"] += margin
    positions = []
    for (sym, direction), v in agg.items():
        if v["lots"] <= 0:
            continue
        avg = (v["cost"] / v["lots"]) if v["lots"] else 0
        positions.append({
            "symbol": sym,
            "pos": v["lots"],
            "open_price": round(avg, 2),
            "direction": direction,
            "margin": round(v["margin"], 2),
        })
    snap_out = {
        "balance": round(total_balance, 2),
        "available": round(total_avail, 2),
        "profit": round(total_profit, 2),
        "positions": positions,
        "updated": snap.get("updated", ""),
        "backend": "ctp",
    }
    with _lock:
        _last_account = snap_out
        _last_fetch = time.time()
    return snap_out


def get_last():
    with _lock:
        return _last_account, _last_fetch


if __name__ == "__main__":
    acc = get_account()
    if acc:
        print("账户:", acc["balance"], "持仓:", len(acc["positions"]))
    else:
        print("无账户接口（手动记账模式）")
