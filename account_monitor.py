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

【白名单过滤 — 防止 auto_sync 污染非交易品种】
  CTP / TqSdk 可能返回账户里所有品种持仓（含已清仓的历史遗留），
  auto_sync 只同步 trade_config.json 的 contract_specs 里声明过的品种。
  这防止了 2026-09-11 遇到的问题：CTP 返回 al/p/ss 等非白名单品种，
  被错误写入 account_state，导致面板"持仓数"虚高。

【权益 Anchor 保护 — auto_sync 绝不覆盖用户手动值】
  account_state.equity 是 anchor（用户手动设置或截图同步的"基准权益"）。
  auto_sync 注释掉了 set_equity() 调用，只做持仓对账：
    ✅ 开/平/加/减仓 → journal + state 都更新
    ❌ set_equity()  → 只在用户手动点「同步权益」按钮时才调
  原因：CTP 快照可能延迟 / 口径不一致（CTP 用盯市权益，account_state 用用户截图权益）。
  2026-09-11 事故：auto_sync 用 TqSdk 模拟盘余额覆盖了 CTP 实盘 anchor。

【账户隔离】
  auto_sync(account, prices, account_id='live') 里的 account_id 参数
  通过 at.set_account(account_id) 切换到目标账户的 state/journal 文件。

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

# ★★★ 2026-11-02 两账户物理隔离（Ken 拍板：彻底停写）★★★
# False = 模拟盘引擎持仓【不再】写进实盘账户文件 account_state.json 的 positions。
#   模拟盘持仓只在 /api/paper-trading + 面板 F8「模拟盘沙盒」tab 展示，实盘账户文件里
#   一条模拟盘腿都不留。原「镜像腿」机制（2026-11-01 治幽灵腿引入）仅软隔离：
#   靠 source/book 标记把浮动与保证金排除在权益之外，一旦哪天代码回退或被覆盖，
#   模拟盘腿就会被当实盘腿计入 —— 属结构性隐患，故按 Ken 决定改为物理隔离。
# True = 恢复旧行为（仅应急回滚用，正常不要打开）。
MIRROR_PAPER_LEGS_TO_ACCOUNT = False


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"enabled": False, "backend": "manual", "broker_id": "", "account_id": "", "password": ""}
    try:
        return json.load(open(CONFIG_FILE, encoding="utf-8"))
    except Exception:
        return {"enabled": False, "backend": "manual"}


def _map_symbol(tqsdk_sym):
    """CTP/TqSdk 合约代码 -> 四维内部 sym。
    兼容 3 种格式：①带交易所前缀(CZCE.SA2701/DCE.m|JM2609) ②纯合约(zn2610) ③品种码(SA/JM)。
    SYMBOLS 是混合大小写字典(ru小写/SA大写)，所以逐轮尝试 upper/lower。"""
    try:
        import re

        import four_dim_strategy as fd

        s = str(tqsdk_sym).strip()
        # 去交易所前缀
        for sep in ["|", ".", "@"]:
            if sep in s:
                s = s.split(sep)[-1]
        code_upper = s.upper()
        code_lower = s.lower()
        code_stripped = code_upper.strip()

        # ① 逐合约精确映射
        if code_stripped in fd.CONTRACT_SYM_BY_CODE:
            return fd.CONTRACT_SYM_BY_CODE[code_stripped]

        # ② 剥月份数字取品种字母（同时尝试 upper 和 lower）
        m = re.match(r"([A-Za-z]+)", code_stripped)
        if m:
            key_upper = m.group(1).upper()
            key_lower = key_upper.lower()
            # 主连代码（仅 3+ 字母且以 M 结尾：JMM→JM, RBM→RB, 但不碰 JM/J/SA 等真实品种）
            if len(key_upper) >= 3 and key_upper.endswith("M"):
                key_upper = key_upper[:-1]
                key_lower = key_upper.lower()
            # 先试大写（SA/JM/J），再试小写（ru/zn/al）
            if key_upper in fd.SYMBOLS:
                return key_upper
            if key_lower in fd.SYMBOLS:
                return key_lower

        # ③ 兜底：去掉所有数字后剩余字母在 SYMBOLS 里吗
        letters = re.sub(r"\d+", "", code_stripped)
        if letters:
            if letters in fd.SYMBOLS:
                return letters
            if letters.upper() in fd.SYMBOLS:
                return letters.upper()
            if letters.lower() in fd.SYMBOLS:
                return letters.lower()
    except Exception:
        pass
    return None


def get_account(prices=None):
    """返回账户快照 dict 或 None（无接口）。
    dict = {"balance","available","profit","positions":[{symbol,pos,open_price,direction,margin}],"updated","backend"}"""
    cfg = load_config()
    if not cfg.get("enabled", False):
        return None
    backend = cfg.get("backend", "manual")
    if backend == "tqsdk":
        return _get_tqsdk_account(cfg)
    if backend in ("ctp", "snapshot"):
        return _get_snapshot_account(cfg)
    if backend == "sandbox":
        return _get_sandbox_account(cfg, prices)
    # 其他后端可在此扩展
    return None


def _get_tqsdk_account(cfg):
    """TqSdk 只读账户（da龘 同款逻辑）。失败/未装返回 None。"""
    global _last_account, _last_fetch
    try:
        from tqsdk import TqAccount, TqApi, TqAuth
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
            account=TqAccount(cfg.get("broker_id", ""), cfg.get("account_id", ""), cfg.get("password", "")),
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


def auto_sync(account, prices=None, account_id=None):
    """把账户权益/持仓自动同步进 account_tracker + trade_journal。
    修正版：同时处理「新开仓 / 已平仓 / 手数变化」三种情况。

    Args:
        account: get_account() 返回的快照 dict
        prices: {sym: price} 实时价 dict
        account_id: 可选，指定同步到哪个账户（default/live）。None = 当前线程默认
    """
    if not account:
        return
    prices = prices or {}
    try:
        import account_tracker as at
        import trade_journal as tj

        # 账户隔离：切换到目标账户
        _prev_account = None
        if account_id:
            _prev_account = at.set_account(account_id)

        # 1) 权益同步：已禁用！auto_sync 只做持仓对账，绝不覆盖用户手动设的 anchor
        #    用户手动点「同步权益」按钮（调 set_equity）时才更新 anchor
        # at.set_equity(account["balance"])  # ← 注释掉，防止 TqAccount 模拟盘余额覆盖 CTP 实盘 anchor

        # 2) 持仓镜像对账（完整重建：支持加仓/减仓/调仓/反手）
        current = {}  # (sym, direction) -> {lots, price}
        # ★ 白名单过滤：只同步 contract_specs 里声明过的品种
        #   防止 CTP/TqSdk 返回的非交易品种（如历史遗留空仓）污染 state
        _allowed_syms = set()
        try:
            _cfg = json.load(open(os.path.join(os.path.dirname(__file__), "trade_config.json")))
            _allowed_syms = set((_cfg.get("contract_specs") or {}).keys())
        except Exception:
            pass
        if not _allowed_syms:
            _allowed_syms = None  # 没配置白名单 → 不过滤
        for p in account.get("positions", []):
            _psym = str(p.get("symbol", ""))
            if _allowed_syms is not None and _psym.lower() not in {s.lower() for s in _allowed_syms}:
                continue  # 跳过非白名单品种
            key = (p["symbol"], p["direction"])
            current[key] = {"lots": p["pos"], "price": p["open_price"]}

        # 也拉 account_state 当前持仓（用于比对和保留风控参数）
        cur_state = {}
        try:
            _st = at.load_state()
            for _sym, _p in (_st.get("positions") or {}).items():
                _d = _p.get("direction", "多")
                cur_state[(_sym, _d)] = _p
        except Exception:
            pass

        # 3) 生成对账计划：按品种分组，处理手数变化
        all_keys = set(current.keys()) | set(cur_state.keys())
        actions = []  # [(action, key, old_lots, new_lots, info)]
        for key in all_keys:
            sym, direction = key
            in_account = key in current
            in_state = key in cur_state
            acct_lots = current.get(key, {}).get("lots", 0)
            state_lots = cur_state.get(key, {}).get("lots", 0)

            if in_account and not in_state:
                actions.append(("OPEN", key, 0, acct_lots, f"{sym} {direction} 新开 {acct_lots}手"))
            elif not in_account and in_state:
                px = prices.get(sym) or cur_state[key].get("avg", 0)
                actions.append(("CLOSE", key, state_lots, 0, f"{sym} {direction} 全平 {state_lots}手 @{px}"))
            elif in_account and in_state and acct_lots != state_lots:
                diff = acct_lots - state_lots
                px = prices.get(sym) or cur_state[key].get("avg", 0)
                if diff > 0:
                    actions.append(("ADD", key, state_lots, acct_lots, f"{sym} {direction} 加仓 +{diff}手 @{px}"))
                else:
                    actions.append(("REDUCE", key, state_lots, acct_lots, f"{sym} {direction} 减仓 {diff}手 @{px}"))
            # else: 完全一致，跳过

        # 4) 执行对账（先平后开，避免保证金不足）
        closed_count = opened_count = adjusted_count = 0
        for action in actions:
            act, key, old_lots, new_lots, info = action
            sym, direction = key
            px = prices.get(sym)
            if px is None and act in ("CLOSE", "REDUCE"):
                px = cur_state.get(key, {}).get("avg", 0)
            if act == "CLOSE":
                if px:
                    tj.record_exit(sym, direction, old_lots, px, reason="账户同步平仓")
                    at.record_trade(sym, "close", direction, old_lots, px)
                    closed_count += 1
            elif act == "OPEN":
                new_price = current[key]["price"]
                tj.record_entry(sym, direction, new_lots, new_price, signal_id="auto_account")
                at.record_trade(sym, "open", direction, new_lots, new_price)
                opened_count += 1
            elif act == "ADD":
                diff = new_lots - old_lots
                new_price = current[key]["price"]
                tj.record_entry(sym, direction, diff, new_price, signal_id="auto_account加仓")
                at.record_trade(sym, "open", direction, diff, new_price)
                adjusted_count += 1
            elif act == "REDUCE":
                diff = old_lots - new_lots
                if px:
                    tj.record_exit(sym, direction, diff, px, reason="账户同步减仓")
                    at.record_trade(sym, "close", direction, diff, px)
                    adjusted_count += 1

        if actions:
            _account_tag = f"[{account_id or at.get_account()}] "
            print(
                f"[账户监控] {_account_tag}对账: 新开{opened_count}/平{closed_count}/调整{adjusted_count} ({len(actions)}项)"
            )
            for a in actions:
                print(f"    {a[4]}")

        # 5) 更新镜像跟踪（_synced_open）
        with _lock:
            _synced_open.clear()
            for key, val in current.items():
                _synced_open[key] = val

        # 恢复原账户
        if _prev_account is not None:
            at.set_account(_prev_account)

    except Exception as e:
        import traceback

        print(f"[账户监控] 自动同步异常: {repr(e)[:160]}")
        traceback.print_exc()


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
            if "volume_long" in p or "volume_short" in p:
                lv = int(p.get("volume_long") or 0)
                sv = int(p.get("volume_short") or 0)
                if lv > 0:
                    k = (sym, "多")
                    px2 = float(p.get("open_price_long") or 0)
                    mg2 = float(p.get("margin_long") or 0)
                    agg.setdefault(k, {"lots": 0, "cost": 0.0, "margin": 0.0})
                    agg[k]["cost"] += px2 * lv
                    agg[k]["lots"] += lv
                    agg[k]["margin"] += mg2
                if sv > 0:
                    k = (sym, "空")
                    px2 = float(p.get("open_price_short") or 0)
                    mg2 = float(p.get("margin_short") or 0)
                    agg.setdefault(k, {"lots": 0, "cost": 0.0, "margin": 0.0})
                    agg[k]["cost"] += px2 * sv
                    agg[k]["lots"] += sv
                    agg[k]["margin"] += mg2
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
        positions.append(
            {
                "symbol": sym,
                "pos": v["lots"],
                "open_price": round(avg, 2),
                "direction": direction,
                "margin": round(v["margin"], 2),
            }
        )
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


def _get_sandbox_account(cfg, prices=None):
    """本地模拟盘沙盒后端（根治「手动记账模式」警告，零外部依赖、不碰任何券商实盘）。
    读 paper_account.json —— 即 F8 沙盒，也就是用户在券商模拟终端操作的「模拟盘」。
    该文件由 /api/paper 实时维护，所以面板账户区会随沙盒自动刷新，无需再手动喂截图。
    同时把沙盒镜像进 account_state（保留既有止损/目标位），作为账户区真相源。"""
    global _last_account, _last_fetch
    try:
        paper_path = os.path.join(HERE, cfg.get("sandbox_path", "paper_account.json"))
        if not os.path.exists(paper_path):
            print(f"[账户监控] 沙盒文件 {paper_path} 不存在（维持手动记账）")
            return None
        d = json.load(open(paper_path, encoding="utf-8"))
        cash = float(d.get("cash", 0) or 0)
        realized = float(d.get("realized", 0) or 0)
        # 合约乘数（ru=10）
        mult_map = {}
        try:
            _tc = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
            for _s, _v in _tc.get("contract_specs", {}).items():
                mult_map[_s.lower()] = float(_v.get("multiplier", 10) or 10)
        except Exception:
            pass
        positions = []
        mtm_total = 0.0
        margin_total = 0.0
        for sym, p in (d.get("positions") or {}).items():
            try:
                lots = int(p.get("lots", 0) or 0)
                if lots <= 0:
                    continue
                avg = float(p.get("avg", 0) or 0)
                direction = p.get("direction", "多")
                margin = float(p.get("margin", 0) or 0)
                margin_total += margin
                mult = mult_map.get(sym.lower(), 10)
                price = (prices or {}).get(sym)
                if price is None:
                    price = p.get("mark")  # 价真空期（收盘后/开盘前）回落标记价
                if price is None:
                    price = avg
                if direction == "空":
                    pl = (avg - price) * lots * mult
                else:
                    pl = (price - avg) * lots * mult
                mtm_total += pl
                positions.append(
                    {
                        "symbol": sym,
                        "pos": lots,
                        "open_price": round(avg, 2),
                        "direction": direction,
                        "margin": round(margin, 2),
                        "profit": round(pl, 2),
                    }
                )
            except Exception:
                continue
        # ── 2026-10-15：合并「引擎仓」进沙盒镜像 ──────────────────────────────
        # 背景：paper_trading_engine 只写 paper_trading_state.json，而本函数只读
        # paper_account.json，两者互不相通 ⇒ 引擎开的仓在面板完全不可见、使用率恒 0.00%
        # （实测3 腿引擎仓被整体漏掉，铁律「使用率≤45%」形同虚设）。
        # 在此就地合并（而非事后补写）的原因：_sync_sandbox_to_account_state 每轮都会
        # 整份覆盖 snapshot，任何"事后补"的写法都会被下一轮冲掉，形成 0→3→0 抖动。
        # 只补不删：paper_account.json 自己的腿优先，引擎腿仅在其缺席时补入。
        try:
            _eng_path = os.path.join(HERE, "paper_trading_state.json")
            if os.path.exists(_eng_path):
                with open(_eng_path, encoding="utf-8") as _f:
                    _epos = (json.load(_f) or {}).get("positions") or {}
                _have = {str(x.get("symbol")) for x in positions}
                for _esym, _ep in _epos.items():
                    if not isinstance(_ep, dict):
                        continue
                    # 实时手数：t1 平半后 remaining_lots 才减，lots 不变
                    _elots = int(_ep.get("remaining_lots") or 0)
                    if _elots <= 0:
                        _elots = int(_ep.get("lots") or 0)
                    _eavg = float(_ep.get("entry_price") or _ep.get("price") or 0)
                    if _elots <= 0 or _eavg <= 0 or str(_esym) in _have:
                        continue
                    _emult = mult_map.get(str(_esym).lower(), 10)
                    _emargin = round(_eavg * _emult * _elots * 0.10, 2)
                    _edir = _ep.get("direction") or "多"
                    _eprice = (prices or {}).get(_esym) or _eavg
                    _epl = (
                        (_eavg - _eprice) * _elots * _emult
                        if _edir == "空"
                        else (_eprice - _eavg) * _elots * _emult
                    )
                    # 实时止损：移动止损激活后以 tracking stop 为准（面板「止损」列 + 触价红标同读 stop）
                    _etrail = _ep.get("trailing_stop")
                    _etrail_act = bool(_ep.get("trailing_active"))
                    _eff_stop = _etrail if (_etrail_act and _etrail) else _ep.get("stop_price")
                    margin_total += _emargin
                    mtm_total += _epl
                    positions.append(
                        {
                            "symbol": _esym,
                            "pos": _elots,
                            "open_price": round(_eavg, 2),
                            "direction": _edir,
                            "margin": _emargin,
                            "profit": round(_epl, 2),
                            # 全量引擎腿 schema（与面板持仓表 four_dim_live.html:7754 字段对齐）
                            "stop": _eff_stop,
                            "t1": _ep.get("t1_price"),
                            "t2": _ep.get("t2_price"),
                            "trailing_stop": _etrail,
                            "trail_state": ("跟踪" if _etrail_act else None),
                            "peak_price": _ep.get("peak_price"),
                            "t1_filled": bool(_ep.get("t1_filled")),
                            "tp_targets": _ep.get("tp_targets"),
                            "open_time": _ep.get("open_time"),
                            "source": "engine",
                        }
                    )
        except Exception as e:
            print(f"[沙盒镜像] 引擎仓合并跳过: {repr(e)[:80]}")
        equity = round(cash + realized + mtm_total, 2)
        avail = round(equity - margin_total, 2)
        snap = {
            "balance": equity,
            "available": avail,
            "profit": round(mtm_total, 2),
            "positions": positions,
            "updated": time.strftime("%H:%M:%S"),
            "backend": "sandbox",
        }
        # 镜像进 account_state，作为账户区真相源（保留止损/目标位）；内容不变则不写盘
        _sync_sandbox_to_account_state(d, positions, equity, avail, mtm_total, margin_total, prices)
        with _lock:
            _last_account = snap
            _last_fetch = time.time()
        return snap
    except Exception as e:
        print(f"[账户监控] 沙盒读取失败: {repr(e)[:120]}（维持手动记账）")
        return None


def _sync_sandbox_to_account_state(paper, positions, equity, avail, mtm, margin, prices):
    """把沙盒（模拟盘）镜像进 account_state.json，面板账户区即显示模拟盘数据。
    设计要点（针对 2026-09-12 账户混淆事故）：
      · 仅当内容哈希变化才写盘，避免每次轮询空写；
      · 既有止损/目标位(stop/t1/t2/tp_targets)一律保留，不覆盖用户手动风控设定；
      · 沙盒就是模拟盘真相源，不存在「模拟盘快照误标实盘」的错位。"""
    try:
        import account_tracker as at

        st = at.load_state()
        new_hash = (
            round(equity, 0),
            round(mtm, 0),
            # 含每笔浮盈：浮盈变化即需写盘，否则持仓行浮盈会停在上一次的值
            tuple(sorted((p["symbol"], p["pos"], round(p["open_price"], 0),
                          round(p.get("profit") or 0.0, 0)) for p in positions)),
        )
        if st.get("_sandbox_hash") == new_hash:
            return
        prev_positions = st.get("positions") or {}
        snap_positions = []
        new_positions = {}
        for p in positions:
            sym = p["symbol"]
            mult = 10
            try:
                _tc = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
                mult = float(_tc.get("contract_specs", {}).get(sym.upper(), {}).get("multiplier", 10) or 10)
            except Exception:
                pass
            # 价格优先级：实时价 → 标记价（paper 持仓 mark 字段）→ 开仓均价
            _mark = (prices or {}).get(sym) or prev_positions.get(sym, {}).get("mark") or p.get("mark") or p["open_price"]
            price = round(_mark, 2)
            # 2026-10-15 修复浮盈口径不一致（实测：面板浮盈 +0.00 而账户总览 +12350）：
            #   上游 _get_sandbox_account 已按同一价格优先级算好每笔 profit，此处却丢弃它、
            #   用「价格→均价」重算 —— 价真空期（盘后/开盘前）价格回落到 open_price，
            #   重算结果恒为 0，而 snapshot.float_total 用的是上游 mtm（仍为收盘值 12350），
            #   于是同一份快照内部自相矛盾：总览 12350 / 持仓行 0。
            # 修法：优先采用上游 profit（同源，保证 sum(float_pnl) == float_total）。
            _p_profit = p.get("profit")
            if isinstance(_p_profit, (int, float)):
                float_pnl = round(float(_p_profit), 2)
            else:
                float_pnl = round((price - p["open_price"]) * p["pos"] * mult, 2)
            margin_pct = round(p["margin"] / equity * 100, 2) if equity else 0
            prev = prev_positions.get(sym, {})
            contract = prev.get("contract") or (sym.upper() + "2701")
            leg = {
                "symbol": sym,
                "name": sym.upper(),
                "contract": contract,
                "direction": p["direction"],
                "lots": p["pos"],
                "avg": p["open_price"],
                "price": price,
                "float_pnl": float_pnl,
                "margin_used": p["margin"],
                "margin_pct": margin_pct,
            }
            snap_leg = dict(leg)
            # 引擎腿：携带全量实时字段（止损/移动止损/分级止盈），与面板持仓表对齐；
            # 每轮从 paper_trading_state.json 重新合并（line 524-553），故移动止损/t1 平半
            # 等推进天然实时刷新，引擎平仓后 paper 池不再含该腿 → 本轮不补入 → 自动清除（无孤儿）。
            if p.get("source") == "engine":
                for _k in ("stop", "t1", "t2", "trailing_stop", "trail_state",
                          "peak_price", "t1_filled", "tp_targets", "open_time"):
                    if p.get(_k) is not None:
                        leg[_k] = p.get(_k)
                        snap_leg[_k] = p.get(_k)
                leg["source"] = "engine"
                snap_leg["source"] = "engine"
            snap_positions.append(snap_leg)
            # 保留既有风控字段 + 写入引擎实时字段
            new_positions[sym] = dict(prev, **leg)
        # 2026-10-15 自洽性护栏：float_total 必须等于各持仓 float_pnl 之和。
        # 两者一旦背离，账户总览与持仓行会显示互相矛盾的数字（历史真实发生过：
        # 总览 +12350 / 持仓行 +0.00），排查极难。故在此强制对齐并留日志。
        _sum_fp = round(sum(float(x.get("float_pnl") or 0) for x in snap_positions), 2)
        _mtm_final = round(mtm, 2)
        if _sum_fp and abs(_sum_fp - _mtm_final) > 0.01:
            print(
                f"[沙盒镜像] 浮盈口径背离，已以持仓行合计为准: "
                f"float_total={_mtm_final} vs sum(float_pnl)={_sum_fp}"
            )
            _mtm_final = _sum_fp
        # ── 2026-11-01 拆雷（三处破坏性写入已移除）──────────────────────────────
        # 原实现会写 st["snapshot_mode"]=True + snapshot_until="2099-12-31"，
        # 而 account_tracker.snapshot() 一见 snapshot_mode 就**直接返回存盘快照、
        # 永不重算**（account_tracker.py L1117）⇒ 一旦本函数被激活，面板权益/浮盈
        # 会永久冻结在那一刻，且极难排查。同时原实现把 st["equity"] 覆盖成沙盒
        # 权益（引擎口径 100 万级，与真实账户 10 万级差 10 倍），会污染
        # 「equity / realized_pnl_at_sync / float_at_sync」这条被冻结的锚点三元组，
        # 直接影响 sizing 的 1% 风险预算。
        # 现在改为：只镜像持仓明细 + 浮动盈亏，**绝不改锚点、绝不开 snapshot_mode**；
        # 权益真值统一由 equity_live（每轮动态权益落盘）承载。
        st["snapshot"] = {
            "equity": equity,
            "available": avail,
            "total_margin": round(margin, 2),
            "float_total": _mtm_final,
            "mtm_pnl": _mtm_final,
            "usage_rate": round(margin / equity * 100, 2) if equity else 0,
            "positions": snap_positions,
        }
        st["positions"] = new_positions
        st["margin_occupied"] = round(margin, 2)
        st["available"] = avail
        st["_sandbox_hash"] = new_hash
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        at.save_state(st)
    except Exception as e:
        print(f"[账户监控] 沙盒→账户态同步跳过: {repr(e)[:120]}")


def _engine_opened_leg(sym, leg, direction=None):
    """判定账户层某腿是否「由交易引擎开过」——用成交流水做证据，而不是只信 source 字段。

    背景（2026-10-31 幽灵腿事故）：
      remove_engine_position 旧护栏只认 `source == "engine"`，但账户层的引擎腿常常
      source 为空（account_tracker.heal_from_journal 从 journal 恢复持仓时不写 source），
      于是引擎平仓后被判为「手动腿」永久保留 ⇒ 面板挂着一条早已止损平掉的腿，
      现价继续盯市、浮亏无限放大（实测 l 于 10-30 09:00:46 已止损平仓，
      面板却挂到 10-31 10:16，浮亏 -4420，并把账户权益压低了 4420）。

    判据（双源，任一命中即认定引擎腿）：
      ① paper_trading_state.json 的 trades 里有同品种/同方向/同开仓价的 open 记录；
      ② trade_journal.json 里有同品种/同方向/同开仓价、strategy 含「引擎」的未平记录。
    查无记录（手动腿/真实腿/券商同步腿）→ False，一律不动，护栏本意不破。
    """
    try:
        _sym = str(sym).strip().lower()
        _dir = str(direction or leg.get("direction") or "").strip()
        try:
            _avg = float(leg.get("avg") or leg.get("open_price") or 0)
        except (TypeError, ValueError):
            _avg = 0.0
        _tol = 0.51  # 价格容差：允许 1 个最小变动价位内的浮点/取整偏差

        # ① 引擎自己的成交流水
        _p = os.path.join(HERE, "paper_trading_state.json")
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                _eng = json.load(_f) or {}
            for _t in (_eng.get("trades") or []):
                if not isinstance(_t, dict) or _t.get("type") != "open":
                    continue
                if str(_t.get("symbol") or "").strip().lower() != _sym:
                    continue
                if _dir and str(_t.get("direction") or "").strip() != _dir:
                    continue
                try:
                    _px = float(_t.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if _avg <= 0 or abs(_px - _avg) <= _tol:
                    return True

        # ② journal 的引擎开仓记录（兜底，防 trades 被裁剪）
        _jp = os.path.join(HERE, "trade_journal.json")
        if os.path.exists(_jp):
            with open(_jp, encoding="utf-8") as _f:
                _j = json.load(_f) or {}
            for _t in (_j.get("trades") or []):
                if not isinstance(_t, dict):
                    continue
                if str(_t.get("symbol") or "").strip().lower() != _sym:
                    continue
                if _dir and str(_t.get("direction") or "").strip() != _dir:
                    continue
                if "引擎" not in str(_t.get("strategy") or ""):
                    continue
                try:
                    _px = float(_t.get("entry_price") or 0)
                except (TypeError, ValueError):
                    continue
                if _avg <= 0 or abs(_px - _avg) <= _tol:
                    return True
        return False
    except Exception:
        return False


def remove_engine_position(symbol, direction=None, lots=0, fully_closed=True):
    """引擎平仓 → 立即清理账户层对应引擎腿（与 _sync_sandbox_to_account_state 的
    每轮重建互补：重建负责常规清除，本函数负责「平仓当轮」内即时清除，避免面板残留红标）。

    护栏（保住账户层真相源，绝不误删手动/实盘腿）：
      1. source=="engine" 直接放行；source 为空/其它时，必须能在引擎成交流水里
         查到同品种/同方向/同开仓价的 open 记录才放行（见 _engine_opened_leg）；
      2. 方向不一致跳过；
      3. 全平 → 删除；部分平 → 按手数扣减，扣完即删；
      4. 原子写盘（account_tracker.save_state），异常只返回文本。
    """
    sym = str(symbol).strip()
    try:
        import account_tracker as _at
        st = _at.load_state()
        pos_map = st.get("positions")
        if not isinstance(pos_map, dict) or sym not in pos_map:
            return ""  # 账户层本无该腿 → 无需清理（幂等）
        leg = pos_map[sym]
        if not isinstance(leg, dict):
            return f"跳过 {sym}：账户层腿结构异常"
        _src = str(leg.get("source") or "")
        if _src not in ("engine", "engine_sync", "auto_signal") and not _engine_opened_leg(sym, leg, direction):
            return f"跳过 {sym}：非引擎腿(source={_src or '空'}，成交流水无对应开仓记录)，保住账户层真相未改动"
        if direction and str(leg.get("direction") or "") and str(leg.get("direction")) != str(direction):
            return f"跳过 {sym}：方向不符(账户层{leg.get('direction')} vs 引擎{direction})，未改动"
        cur = int(leg.get("lots") or 0)
        left = 0 if fully_closed else max(cur - int(lots or 0), 0)
        if cur <= 0 or left <= 0:
            del pos_map[sym]
            action = f"全平清理 {sym}（原 {cur} 手）"
        else:
            leg["lots"] = left
            pos_map[sym] = leg
            action = f"部分平 {sym} {lots} 手，账户层余 {left} 手"
        st["positions"] = pos_map
        snap = st.get("snapshot")
        if isinstance(snap, dict) and isinstance(snap.get("positions"), list):
            if left <= 0:
                snap["positions"] = [x for x in snap["positions"] if str(x.get("symbol")) != sym]
            else:
                for x in snap["positions"]:
                    if str(x.get("symbol")) == sym:
                        x["lots"] = left
        st["snapshot"] = snap
        _at.save_state(st)
        return action
    except Exception as e:
        return f"清理异常: {repr(e)[:80]}"


# 引擎 close 回调复用同一签名（paper_trading_engine 内 `import account_monitor as _sea; _sea.remove_account_position(...)`）
def remove_account_position(symbol, direction=None, lots=0, fully_closed=True):
    return remove_engine_position(symbol, direction=direction, lots=lots, fully_closed=fully_closed)


# 引擎腿实时字段（移动止损/t1 平半推进时必须跟着变，见 2026-10-28 铁律「auto 全部状态联动实时更新」）
_ENGINE_LIVE_FIELDS = (
    "lots", "direction", "avg", "stop", "t1", "t2",
    "trailing_stop", "trail_state", "peak_price", "t1_filled", "open_time", "source",
)


def _engine_positions_now():
    """读引擎当前持仓 → {sym: leg}（仅 lots>0）。文件不存在/损坏返回 {}。"""
    out = {}
    try:
        _p = os.path.join(HERE, "paper_trading_state.json")
        if not os.path.exists(_p):
            return out
        with open(_p, encoding="utf-8") as _f:
            _eng = json.load(_f) or {}
        for _sym, _ep in ((_eng.get("positions") or {}) if isinstance(_eng.get("positions"), dict) else {}).items():
            if not isinstance(_ep, dict):
                continue
            _lots = int(_ep.get("remaining_lots") or 0) or int(_ep.get("lots") or 0)
            _avg = float(_ep.get("entry_price") or _ep.get("price") or 0)
            if _lots <= 0 or _avg <= 0:
                continue
            _trail = _ep.get("trailing_stop")
            _trail_act = bool(_ep.get("trailing_active"))
            out[str(_sym)] = {
                "symbol": str(_sym),
                "direction": _ep.get("direction") or "多",
                "lots": _lots,
                "avg": round(_avg, 4),
                # 移动止损激活后以跟踪止损为准（面板「止损」列 + 触价红标同读这一个值）
                "stop": _trail if (_trail_act and _trail) else _ep.get("stop_price"),
                "t1": _ep.get("t1_price"),
                "t2": _ep.get("t2_price"),
                "trailing_stop": _trail,
                "trail_state": ("跟踪" if _trail_act else None),
                "peak_price": _ep.get("peak_price"),
                "t1_filled": bool(_ep.get("t1_filled")),
                "open_time": _ep.get("open_time"),
                "source": "engine",
                "last_price": _ep.get("last_price"),
            }
    except Exception as _e:
        print(f"[引擎对齐] 读取引擎持仓失败: {repr(_e)[:80]}")
    return out


def _journal_engine_open_leg(sym, avg, direction=None):
    """journal 里是否还有这条「未平仓的引擎记录」（pnl is None 且 strategy 含「引擎」）。

    用途：reconcile 删除账户层腿之前的最后一道护栏。
      account_tracker.heal_from_journal() 每 30s 会从 journal 未平记录**重建**账户层腿，
      所以如果 journal 说这笔还开着，这里硬删就是在跟自愈打乒乓（删→加→删→加，
      每 30s 一次写盘、面板来回跳）。这种情况不该删腿，而该把 journal 的陈旧记录
      暴露出来（打印 WARNING），交给人工/引擎结清。
    返回命中的 journal 记录 dict 或 None。
    """
    try:
        _sym = str(sym).strip().lower()
        _dir = str(direction or "").strip()
        try:
            _avg = float(avg or 0)
        except (TypeError, ValueError):
            _avg = 0.0
        _jp = os.path.join(HERE, "trade_journal.json")
        if not os.path.exists(_jp):
            return None
        with open(_jp, encoding="utf-8") as _f:
            _j = json.load(_f) or {}
        for _t in (_j.get("trades") or []):
            if not isinstance(_t, dict):
                continue
            if _t.get("pnl") is not None:
                continue  # 已平仓
            if str(_t.get("symbol") or "").strip().lower() != _sym:
                continue
            if _dir and str(_t.get("direction") or "").strip() != _dir:
                continue
            if "引擎" not in str(_t.get("strategy") or ""):
                continue
            try:
                _px = float(_t.get("entry_price") or 0)
            except (TypeError, ValueError):
                continue
            if _avg <= 0 or abs(_px - _avg) <= 0.51:
                return _t
        return None
    except Exception:
        return None


def _purge_paper_mirror_legs():
    """清掉 account_state.positions 里所有模拟盘引擎腿（物理隔离的清理侧）。

    判据（任一成立即视为模拟盘腿）：
      ① book == "paper"（镜像时代打的标记）
      ② source ∈ {engine, engine_sync, auto_signal}（镜像时代写的来源）
      ③ 该品种此刻在 paper_trading_state.json 里仍有持仓，**且手数+开仓均价与引擎腿一致**
         （本质判据，防标记被覆盖丢失；加指纹比对是为了不误伤「同名品种的真实腿」——
          镜像腿是引擎腿的逐字段复制，手数与均价必然相同；真实开仓几乎不可能两者同时相同）
    手动腿 / 真实腿 / 无来源的无主腿一律不碰，权益锚点三元组绝不改写。

    幂等：没东西可清就不写盘（避免每轮空写抖动）。
    """
    res = {"added": [], "updated": [], "removed": [], "skipped": [], "mode": "purge-only"}
    try:
        import account_tracker as _at

        _eng = _engine_positions_now()  # 模拟盘引擎当前持仓（含手数/均价指纹）
        _atlock = getattr(_at, "_LOCK", _lock)
        with _atlock:
            st = _at.load_state() or {}
            pos_map = st.get("positions")
            if not isinstance(pos_map, dict) or not pos_map:
                return res
            _drop = []
            for _sym, _leg in list(pos_map.items()):
                if not isinstance(_leg, dict):
                    continue
                _src = str(_leg.get("source") or "")
                _is_paper = (
                    str(_leg.get("book") or "") == "paper"
                    or _src in ("engine", "engine_sync", "auto_signal")
                )
                if not _is_paper:
                    # ③ 指纹判据：品种在引擎里 + 手数相同 + 均价相差 < 0.02
                    _pe = _eng.get(str(_sym))
                    if isinstance(_pe, dict):
                        try:
                            _l1 = float(_leg.get("lots") or 0)
                            _l2 = float(_pe.get("lots") or 0)
                            _a1 = float(_leg.get("avg") or _leg.get("price") or 0)
                            _a2 = float(_pe.get("avg") or 0)
                            _is_paper = (
                                _l1 > 0 and abs(_l1 - _l2) < 1e-9
                                and _a1 > 0 and abs(_a1 - _a2) < 0.02
                            )
                        except (TypeError, ValueError):
                            _is_paper = False
                if _is_paper:
                    _drop.append(_sym)
                else:
                    res["skipped"].append(f"{_sym}({_leg.get('book') or 'live'})")
            if not _drop:
                return res
            for _sym in _drop:
                pos_map.pop(_sym, None)
                res["removed"].append(_sym)
            st["positions"] = pos_map
            _sn = st.get("snapshot")
            if isinstance(_sn, dict) and isinstance(_sn.get("positions"), list):
                _sn["positions"] = [x for x in _sn["positions"] if str(x.get("symbol")) not in _drop]
            _at.save_state(st)
        return res
    except Exception as e:
        print(f"[引擎对齐] 清理模拟盘镜像腿异常(跳过本轮): {repr(e)[:120]}")
        return res


def reconcile_engine_positions(prices=None):
    """把引擎持仓每轮幂等地对齐进 account_state.positions —— 引擎是 auto 腿的唯一真相源。

    为什么必须有这个函数（2026-10-31 幽灵腿事故的治本项）：
      · `_get_sandbox_account` 需要 paper_account.json（不存在）+ account_monitor.json
        （不存在 → enabled=False），整条「每轮重建账户层」的链路**从未执行**；
      · 账户层实际只靠 `heal_from_journal` 自愈 + 引擎平仓回调两条腿走路，
        于是出现「引擎已止损平仓、面板还挂着腿」「止损被 2% 兜底值冒充」两类事故。
    本函数直接以引擎 state 为源做全量对账，不依赖任何沙盒/账户后端。

    ★ 2026-11-02 改造：`MIRROR_PAPER_LEGS_TO_ACCOUNT = False` 时本函数退化为
      「只清不写」——不再把模拟盘引擎腿写进实盘账户，只负责把历史上已写进去的
      模拟盘腿清干净（幂等）。模拟盘持仓改由 /api/paper-trading + F8 沙盒 tab 独立展示。
      理由：模拟盘与实盘必须两套账、两套文件，不能靠标记做软隔离。

    护栏（绝不污染非引擎腿）：
      1. 只写/改/删「引擎腿」：source∈{engine,engine_sync} 或成交流水可证（_engine_opened_leg）；
      2. 手动腿/真实腿/无来源无流水的孤儿腿一律不碰；
      3. 绝不触碰权益锚点三元组（equity / realized_pnl_at_sync / float_at_sync）——
         账户权益永远来自真实账户口径，不因引擎持仓变化而改写；
      4. 无变化不写盘（防每轮空写抖动）。

    返回 {"added":[...], "updated":[...], "removed":[...], "skipped":[...]}
    """
    if not MIRROR_PAPER_LEGS_TO_ACCOUNT:
        return _purge_paper_mirror_legs()
    res = {"added": [], "updated": [], "removed": [], "skipped": []}
    try:
        import account_tracker as _at

        eng = _engine_positions_now()
        # 与 account_tracker.snapshot() 的写盘共用同一把锁，避免「读-改-写」被 API 线程
        # 的 equity_live 落盘覆盖（丢更新 → 面板权益短暂停在旧值）。
        _atlock = getattr(_at, "_LOCK", _lock)
        with _atlock:
            st = _at.load_state() or {}
            pos_map = st.get("positions")
            if not isinstance(pos_map, dict):
                pos_map = {}
            changed = False

            # ① 引擎有腿 → 账户层必须有一致的镜像腿
            for _sym, _want in eng.items():
                _cur = pos_map.get(_sym)
                if _cur is None:
                    _leg = dict(_want)
                    # ★ 2026-11-02 两账户独立：镜像腿打 book="paper" 标记，
                    #   供前端/后续计算区分「模拟盘腿」与「实盘腿」。
                    #   account_tracker.snapshot() 已按 source 排除其浮动与保证金，
                    #   此处标记是给展示层与审计用的第二道标识。
                    _leg["book"] = "paper"
                    if prices and isinstance(prices, dict):
                        _px = prices.get(_sym) or prices.get(str(_sym).upper())
                        if isinstance(_px, (int, float)) and _px > 0:
                            _leg["price"] = round(float(_px), 2)
                    pos_map[_sym] = _leg
                    res["added"].append(_sym)
                    changed = True
                    continue
                _src = str(_cur.get("source") or "")
                if _src not in ("engine", "engine_sync", "auto_signal") and not _engine_opened_leg(_sym, _cur, _cur.get("direction")):
                    res["skipped"].append(f"{_sym}(非引擎腿,source={_src or '空'})")
                    continue
                _diff = {k: (_cur.get(k), _want.get(k)) for k in _ENGINE_LIVE_FIELDS if _cur.get(k) != _want.get(k)}
                if _diff:
                    for k in _ENGINE_LIVE_FIELDS:
                        if _want.get(k) is not None or k in ("trailing_stop", "trail_state", "peak_price", "t1_filled"):
                            _cur[k] = _want.get(k)
                    _cur["source"] = "engine"
                    _cur["book"] = "paper"  # 2026-11-02：与新增分支一致，供展示/审计区分
                    _cur["levels_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    pos_map[_sym] = _cur
                    res["updated"].append(f"{_sym}:{','.join(sorted(_diff))}")
                    changed = True

            # ② 账户层有腿但引擎已无 → 只删可证实的引擎腿（引擎已平仓）
            for _sym in list(pos_map.keys()):
                if _sym in eng:
                    continue
                _leg = pos_map.get(_sym)
                if not isinstance(_leg, dict):
                    continue
                _src = str(_leg.get("source") or "")
                if _src in ("engine", "engine_sync", "auto_signal") or _engine_opened_leg(_sym, _leg, _leg.get("direction")):
                    # 最后一道护栏：journal 仍认为未平 → 不删（否则与 heal_from_journal 打乒乓）
                    _jo = _journal_engine_open_leg(_sym, _leg.get("avg"), _leg.get("direction"))
                    if _jo:
                        res["skipped"].append(f"{_sym}(journal 仍为未平仓记录,疑陈旧)")
                        print(
                            f"[引擎对齐] ⚠ {_sym} 引擎已无此腿，但 journal 仍有未平仓记录"
                            f"(entry={_jo.get('entry_price')} pnl=None) → 保留账户层腿，"
                            f"请核对是否历史残留（否则每 30s 会与自愈互相覆盖）"
                        )
                        continue
                    pos_map.pop(_sym, None)
                    res["removed"].append(_sym)
                    changed = True
                else:
                    res["skipped"].append(f"{_sym}(无流水证据,保留)")

            if not changed:
                return res

            st["positions"] = pos_map
            # snapshot 内的持仓列表同步裁剪（stale 腿不留在快照里）
            _sn = st.get("snapshot")
            if isinstance(_sn, dict) and isinstance(_sn.get("positions"), list):
                _sn["positions"] = [x for x in _sn["positions"] if str(x.get("symbol")) in pos_map]
            _at.save_state(st)
        return res
    except Exception as e:
        print(f"[引擎对齐] 异常(跳过本轮): {repr(e)[:120]}")
        return res


def get_last():
    with _lock:
        return _last_account, _last_fetch


if __name__ == "__main__":
    acc = get_account()
    if acc:
        print("账户:", acc["balance"], "持仓:", len(acc["positions"]))
    else:
        print("无账户接口（手动记账模式）")
