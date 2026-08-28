# -*- coding: utf-8 -*-
"""四维策略 · 成交记录器（真实成交 vs 引擎信号对比）
========================================================
把用户在模拟盘按信号手动下单的真实成交记录下来，与 four_dim_signals.json
的引擎信号做逐笔核对，算「真实 papertrack」（期望R / 胜率 / 回撤），
生成对比报告。

这是模拟盘回本 100 万作战规划 §6 的「成交记录器（待建）」。

用法：
  import trade_journal as tj
  tj.record_entry("FG", "空", 2, 901.0, signal_id="sig_20260811_...")  # 按信号开仓
  tj.record_exit("FG", "空", 2, 895.0, reason="止盈")                   # 平仓
  report = tj.summary()           # 成交统计
  cmp = tj.compare_to_papertrack() # 真实 vs 信号对比

Web API（已集成到 four_dim_live_runner.py）:
  POST /api/journal  {"action":"entry","symbol":"FG","direction":"空","lots":2,"price":901.0,"signal_id":"..."}
  POST /api/journal  {"action":"exit","symbol":"FG","direction":"空","lots":2,"price":895.0,"reason":"止盈"}
  GET  /api/journal  → 返回 summary + compare 报告
"""
from __future__ import annotations
import os, json, uuid, threading
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_FILE = os.path.join(HERE, "trade_journal.json")
SIGNAL_LOG = os.path.join(HERE, "four_dim_signals.json")
# 用 RLock 而非 Lock：update_trade 在持有锁时会调用 _load/_save（其内部也上锁），
# 非重入的 Lock 会造成同一线程自死锁。RLock 同线程可重入、跨线程仍互斥，行为安全。
_LOCK = threading.RLock()

# 防重复提交：记录最近一次请求的指纹和时间
# 格式: {fingerprint: timestamp}
_RECENT_REQUESTS = {}
_REQUEST_DEDUP_WINDOW = 3  # 秒，同一请求在此时间窗口内会被拒绝

# 合约乘数（与 four_dim_strategy DEFAULT_CONFIG.contract_specs 对齐）
_MULTIPLIERS = {
    # 上期所 SHFE
    "cu": 5, "al": 5, "zn": 5, "ni": 1, "sn": 1, "ao": 20,
    "au": 1000, "ag": 15, "rb": 10, "hc": 10, "ss": 5,
    "bu": 10, "fu": 10, "ru": 10, "sp": 10,
    # 上期能源 INE
    "sc": 1000, "ec": 50,
    # 大商所 DCE
    "i": 100, "J": 100, "JM": 60, "eb": 5, "eg": 10,
    "l": 5, "pp": 5, "v": 5, "pg": 20,
    "m": 10, "y": 10, "a": 10, "b": 10, "p": 10,
    "c": 10, "cs": 10, "jd": 10, "lh": 16, "rr": 10,
    # 郑商所 CZCE
    "FG": 20, "SA": 20, "SA01": 20, "MA": 10, "TA": 5, "PF": 5,
    "PX": 5, "SH": 30, "UR": 20, "PR": 5,
    "SR": 10, "CF": 5, "RM": 10, "OI": 10, "PK": 5, "AP": 10,
    # 广期所 GFEX
    "si": 5, "lc": 1,
}

# 交易所手续费（占名义金额比例，近似；用于把毛利变成净盈亏）。
# 仅作为「未纳入 _FEE_SCHEDULE 的品种」的兜底回退（避免其它品种/新上市合约突然算不出费）。
_FEE_RATE = {
    "jd": 0.00015, "lh": 0.0002, "FG": 0.0001, "SA": 0.0001,
    "JM": 0.0001, "J": 0.0001, "rb": 0.0001, "i": 0.0001,
    "m": 0.00015, "y": 0.00025, "a": 0.0002, "c": 0.00012,
}
_FEE_DEFAULT = 0.0001

# ============================================================================
# 交易所手续费「基础标准」（2026-08-14 复核，网验来源）：
#   - 期货公司公示的交易所标准表（2025-03-14）：
#     https://www.gldhqh.com.cn/main/a/20250314/50928.shtml
#   - 上期所官网 手续费一览（2025-01-02）：
#     https://www.shfe.com.cn/reports/businessdata/feeandcharges/202501/t20250102_824218.html
#
# 结构：每品种 {"mode":"fixed"|"pct", "open":X, "close":Y, "close_today":Z(可选)}
#   - fixed → X 为「元/手」；pct → X 为「名义金额比例」（price×mult×lots×X）。
#   - open/close 为各自腿费率；close_today 仅在「同日开平」时覆盖 close
#     （平今万6 / 平今万4.2 / 平今60元 等；close_today=0 即「平今免收」）。
#   - 省略 close_today 时，平仓统一用 close（无论是否同日）。
#
# 以上为交易所基础标准；实盘 = 交易所 + 期货公司佣金，本系统按交易所基础记账，
# 改本表即可调整。如需账户实际费率，可放 fee_schedule.override.json 覆盖。
#
# 注（2026-08-14 更正，依据 gldhqh.com.cn 2025-03-14 交易所基础标准表）：
#   OI 菜油 = 6 元/手（固定，开平今均收）→ {"mode":"fixed","open":6.0,"close":6.0}；
#   CF 棉花 = 12.9 元/手（固定，平今免收）→ {"mode":"fixed","open":12.9,"close":12.9,"close_today":0.0}。
#   此前为迁就「验收数字」曾用 pct 0.0006(OI)/默认0.0001(CF)，现按用户「真实交易所费率」硬指令改回真实值。
_FEE_SCHEDULE = {
    # 大商所
    "jd": {"mode": "pct", "open": 0.00045, "close": 0.00045},                 # 鸡蛋 万分之4.5，开平今均收
    "lh": {"mode": "pct", "open": 0.0003, "close": 0.0003, "close_today": 0.0006},  # 生猪 开万3 / 平今万6
    "c":  {"mode": "fixed", "open": 3.6, "close": 3.6},                       # 玉米 3.6 元/手，开平今均收
    # 郑商所
    "FG": {"mode": "fixed", "open": 18.0, "close": 18.0},                    # 玻璃 18 元/手（交易所基础）
    "SA": {"mode": "pct", "open": 0.0006, "close": 0.0006},                  # 纯碱 万分之6
    "SA01": {"mode": "pct", "open": 0.0006, "close": 0.0006},                # P1-6 fix: 纯碱连续合约(SA01) 万分之6
    "OI": {"mode": "fixed", "open": 6.0, "close": 6.0},                     # 菜油 6 元/手（固定，开平今均收）
    "CF": {"mode": "fixed", "open": 12.9, "close": 12.9, "close_today": 0.0}, # 棉花 12.9 元/手（固定，平今免收）
    "AP": {"mode": "fixed", "open": 15.0, "close": 15.0, "close_today": 60.0},  # 苹果 开平15 / 平今60
    # 上期所
    "JM": {"mode": "pct", "open": 0.0003, "close": 0.0003},                  # 焦煤 万分之3
    "J":  {"mode": "pct", "open": 0.0003, "close": 0.0003, "close_today": 0.00042},  # 焦炭 开万3 / 平今万4.2
    "rb": {"mode": "pct", "open": 0.0001, "close": 0.0001},                  # 螺纹钢 万分之1
}


def _leg_fee(symbol, price, lots, side="open", same_day=False):
    """单边（开或平）手续费。

    优先按 _FEE_SCHEDULE（交易所基础标准）计算；symbol 不在表中时回退旧
    _FEE_RATE / _FEE_DEFAULT 近似（避免其它品种/新上市合约突然算不出费）。

    - fixed 模式：fee = 对应腿费率(元/手) × 手数。
        side=="open" → open；side=="close" → (same_day 且表含 close_today ? close_today : close)。
        close_today==0 即「平今免收」→ 0 元。
    - pct 模式：fee = round(价格 × 乘数 × 手数 × 对应腿费率, 2)。
    对应腿费率：open 取 open；close 取 (same_day 且含 close_today ? close_today : close)。
    """
    mult = _MULTIPLIERS.get(symbol, 10)
    try:
        price = float(price); lots = int(lots)
    except (TypeError, ValueError):
        return 0.0
    sp = _FEE_SCHEDULE.get(symbol)
    if sp is not None:
        if side == "open":
            rate = float(sp.get("open", 0.0))
        else:
            # 平仓：同日且有 close_today 用 close_today，否则用 close（close_today=0 即平今免收）
            if same_day and "close_today" in sp:
                rate = float(sp["close_today"])
            else:
                rate = float(sp.get("close", 0.0))
        if sp.get("mode") == "fixed":
            return round(rate * lots, 2)
        return round(price * mult * lots * rate, 2)
    # 回退：旧近似（其余品种/未在表中的合约）
    rate = _FEE_RATE.get(symbol, _FEE_DEFAULT)
    try:
        return round(price * mult * lots * rate, 2)
    except Exception:
        return 0.0


def _load():
    """P0-3 加固：读取 journal，损坏时自动回退 .bak，都损坏则返回空结构。

    读取链路（优先级）：JOURNAL_FILE → JOURNAL_FILE.bak → 空结构
    杜绝 JSONDecodeError 导致系统无法启动、已实现盈亏/连亏计数清零的问题。"""
    _default = {"trades": [], "updated": ""}
    if not os.path.exists(JOURNAL_FILE):
        return dict(_default)
    with _LOCK:
        # 1. 先试主文件
        try:
            with open(JOURNAL_FILE, encoding="utf-8") as _f:
                return json.load(_f)
        except (json.JSONDecodeError, OSError) as _e:
            print(f"[trade_journal] 主文件损坏，尝试恢复 .bak: {_e}")
        # 2. 主文件损坏，试 .bak 备份
        _bak = JOURNAL_FILE + ".bak"
        if os.path.exists(_bak):
            try:
                with open(_bak, encoding="utf-8") as _f:
                    _res = json.load(_f)
                # 恢复成功：把 .bak 复制回主文件（下次直接读主文件）
                try:
                    import shutil as _su
                    _su.copy2(_bak, JOURNAL_FILE)
                    print(f"[trade_journal] 已从 .bak 恢复主文件")
                except Exception:
                    pass
                return _res
            except (json.JSONDecodeError, OSError) as _e2:
                print(f"[trade_journal] .bak 也损坏: {_e2}")
        # 3. 全部失败，返回空结构 + 留档备查
        try:
            _bad = JOURNAL_FILE + ".corrupt"
            if os.path.exists(JOURNAL_FILE):
                import shutil as _su
                _su.copy2(JOURNAL_FILE, _bad)
                print(f"[trade_journal] 损坏副本已另存为 {_bad}")
        except Exception:
            pass
        return dict(_default)


def _save(data):
    """保存成交记录；自动重算 summary 确保统计实时准确。
    P0-3 fix: 原子写盘 + .bak 备份，防止进程崩溃导致文件丢失。"""
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        # ★ 自动重算 summary（避免 record_entry/record_exit 后 summary 过期）
        data["summary"] = _compute_summary(data)
        # 备份当前好状态
        if os.path.exists(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, encoding="utf-8") as _f:
                    _cur = _f.read()
                if _cur.strip():
                    with open(JOURNAL_FILE + ".bak", "w", encoding="utf-8") as _f:
                        _f.write(_cur)
            except Exception:
                pass
        # 原子写：先写临时文件再 os.replace
        import tempfile as _tmp
        _fd, _tmp_path = _tmp.mkstemp(dir=os.path.dirname(JOURNAL_FILE), suffix=".tmp")
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                json.dump(data, _f, ensure_ascii=False, indent=2)
                _f.flush()
                os.fsync(_f.fileno())
            os.replace(_tmp_path, JOURNAL_FILE)
        except Exception:
            try:
                os.unlink(_tmp_path)
            except Exception:
                pass
            raise



def _safe_read_json(path, default):
    """P0-3 加固：安全读取任意 JSON 文件，不抛异常、不泄漏句柄。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as _f:
            return json.load(_f)
    except (json.JSONDecodeError, OSError):
        return default


def get_all_trades():
    """获取所有交易记录（供自优化模块使用）"""
    data = _load()
    return data.get("trades", [])


def _dir_sign(direction):
    return 1 if direction == "多" else (-1 if direction == "空" else 0)


def _validate_entry_stop(direction, entry_price, stop):
    """校验开仓时止损方向；若错误则以 entry_price 为轴镜像修正。
    返回 (stop_or_none, fix_note_or_empty)。"""
    if stop is None:
        return None, ""
    ds = _dir_sign(direction)
    if ds == 0:
        return stop, ""
    try:
        ep = float(entry_price); sv = float(stop)
    except Exception:
        return stop, ""
    ok = (sv < ep) if ds > 0 else (sv > ep)
    if ok:
        return stop, ""
    nv = round(ep + (ep - sv), 2)
    if abs(nv - ep) < 1e-9:
        nv = round(ep + 0.01 if ds > 0 else ep - 0.01, 2)
    return nv, f"；止损方向已自动修正为 {nv}"


def _normalize_sym(sym):
    """品种名标准化：支持大小写（AO/ao/jm/JM 均可）。"""
    if sym in _MULTIPLIERS:
        return sym
    lower = sym.lower()
    if lower in _MULTIPLIERS:
        return lower
    upper = sym.upper()
    if upper in _MULTIPLIERS:
        return upper
    return sym  # 原样返回，让下游报错



def _check_duplicate_request(fingerprint: str) -> bool:
    """检查是否为重复请求。返回 True 表示是重复请求，应拒绝。"""
    import time
    now = time.time()
    # 清理过期记录
    expired = [k for k, v in _RECENT_REQUESTS.items() if now - v > _REQUEST_DEDUP_WINDOW]
    for k in expired:
        del _RECENT_REQUESTS[k]
    # 检查重复
    if fingerprint in _RECENT_REQUESTS:
        return True
    # 记录新请求
    _RECENT_REQUESTS[fingerprint] = now
    return False

def record_entry(symbol, direction, lots, price, signal_id="", stop=None, stop_dist=None,
                  strategy="", account="主账户"):
    """记录一笔按信号开仓。返回 (ok, msg, trade_id)。
    stop_dist: 该笔计划止损距离（点），用于后续 R 倍数追踪；缺则回退 risk_pct 风险预算推算。
    strategy: F1 多策略视图标签（如 趋势/波段/日内/套利/手动…），缺省空串→归入『未分类』。
    account : F1 多账户标签（默认『主账户』），为后续多账户视图预留维度。"""
    symbol = _normalize_sym(symbol)
    if symbol not in _MULTIPLIERS:
        return False, f"未知品种 {symbol}", ""
    data = _load()
    tid = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    try:
        _sd = float(stop_dist) if stop_dist is not None else None
    except Exception:
        _sd = None
    # ★★ 2026-08-26: 价格保护 - 验证用户价格不被篡改
    _original_price = float(price) if price is not None and price != 0 else 0
    if _original_price <= 0:
        return False, f"非法价格 {price}，必须大于0", ""
    print(f"[record_entry] 保存价格: {_original_price} (原始输入: {price}, 类型: {type(price).__name__})")
    # 止损方向校验：防止空单止损落在开仓价下方等错误配置落盘
    stop, fix_note = _validate_entry_stop(direction, _original_price, stop)
    trade = {
        "id": tid,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "direction": direction,
        "lots": int(lots),
        "entry_price": _original_price,  # ★ 使用验证后的价格，防止篡改
        "signal_id": str(signal_id),
        "strategy": (str(strategy).strip() or ""),   # F1：策略标签
        "account": (str(account).strip() or "主账户"),  # F1：账户标签
        "stop": (float(stop) if stop is not None else None),
        "stop_dist": _sd,
        "open_fee": _leg_fee(symbol, price, lots),
        "close_fee": None,
        "fee_total": None,
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "pnl": None,
        "note": "",            # G2：成交备注（默认空，可经 update_trade 补）
    }
    data["trades"].append(trade)
    _save(data)
    return True, f"已记录 {symbol} {direction} {lots}手 @{_original_price}{fix_note}", tid  # ★ 返回验证后的价格


def record_exit(symbol, direction, lots, price, reason="手动"):
    """平仓/减仓。按 FIFO 匹配该品种同方向的最近未平仓记录。返回 (ok, msg, pnl)。

    支持部分平仓（半自动系统常见：t1 平半 / 手动减仓）。lots 为本次平仓手数：
      - close_lots >= trade["lots"]：整笔平，保持原有逻辑（回归安全）。
      - close_lots <  trade["lots"]：部分平（拆分法）——原 trade 保持未平、仅减手数，
        同时追加一条新「已平记录」，保证 journal 与账户侧不再脱钩。
    """
    symbol = _normalize_sym(symbol)
    if symbol not in _MULTIPLIERS:
        return False, f"未知品种 {symbol}", 0.0
    # 防重复提交检查
    import time
    _fp = f"exit:{symbol}:{direction}:{lots}:{price}:{reason}"
    if _check_duplicate_request(_fp):
        print(f"[record_exit] 重复请求已拒绝: {_fp}")
        return False, "请求过于频繁，请稍候再试", 0.0
    data = _load()
    # 找该品种同方向、未平仓的最近一条
    candidates = [t for t in data["trades"]
                  if t["symbol"] == symbol and t["direction"] == direction and t.get("exit_price") is None]
    if not candidates:
        return False, f"{symbol} {direction} 无未平仓记录", 0.0
    trade = candidates[-1]  # 最近一条
    # 同日判定（开平同日历日 → 平今费率生效）：用于 close 腿费率选择
    _now = datetime.now()
    _entry_date = (trade.get("time") or "")[:10]
    _exit_date = _now.strftime("%Y-%m-%d")
    same_day = bool(_entry_date and _entry_date == _exit_date)
    mult = _MULTIPLIERS[symbol]
    ds = 1 if direction == "多" else -1

    close_lots = int(lots) if lots else trade["lots"]
    if close_lots <= 0:
        return False, f"{symbol} {direction} 平仓手数非法（{close_lots}）", 0.0

    if close_lots >= trade["lots"]:
        # 整笔平：保持原有逻辑不变，回归安全
        trade_lots = trade["lots"]
        gross = round((float(price) - trade["entry_price"]) * mult * trade_lots * ds, 2)
        close_fee = _leg_fee(symbol, price, trade_lots, "close", same_day)
        # 开仓手续费：缺失时按开仓价补算，保证 fee_total/open_fee 完整（避免净盈亏口径漂移）
        open_fee = trade.get("open_fee")
        if open_fee is None:
            open_fee = _leg_fee(symbol, trade["entry_price"], trade_lots)
        fee_total = round(float(open_fee) + float(close_fee), 2)
        pnl = round(gross - fee_total, 2)  # 净盈亏（已扣手续费）
        trade["exit_price"] = float(price)
        trade["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_reason"] = reason
        trade["open_fee"] = round(float(open_fee), 2)
        trade["close_fee"] = close_fee
        trade["fee_total"] = fee_total
        trade["gross_pnl"] = gross
        trade["pnl"] = pnl
        _save(data)
        return True, f"{symbol} {direction} {reason}平仓 @{price}，净盈亏 {pnl:+.0f} 元（含手续费 {fee_total:.0f}）", pnl
    else:
        # 部分平：拆分法——原 trade 保持未平、仅减手数；新生成一条已平记录
        trade_lots = close_lots
        gross = round((float(price) - trade["entry_price"]) * mult * trade_lots * ds, 2)
        close_fee = _leg_fee(symbol, price, trade_lots, "close", same_day)
        # 该部分对应的开仓手续费（按开仓价 + 平掉手数），与整笔平口径一致（fee_total = open_fee + close_fee）
        # P1-10 fix: 直接按平仓手数重算开仓手续费，避免比例分摊舍入误差累积
        open_fee = _leg_fee(symbol, trade["entry_price"], trade_lots)
        fee_total = round(float(open_fee) + float(close_fee), 2)
        partial_pnl = round(gross - fee_total, 2)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        closed_record = dict(trade)  # 复制当前 trade 为新「已平记录」
        closed_record["lots"] = trade_lots
        closed_record["exit_price"] = float(price)
        closed_record["exit_time"] = now_str
        closed_record["exit_reason"] = reason
        closed_record["open_fee"] = round(float(open_fee), 2)
        closed_record["close_fee"] = close_fee
        closed_record["fee_total"] = fee_total
        closed_record["gross_pnl"] = gross
        closed_record["pnl"] = partial_pnl
        data["trades"].append(closed_record)
        # 原 trade 保持未平（不设 exit_price / pnl），仅减手数
        trade["lots"] -= trade_lots
        _save(data)
        return True, f"{symbol} {direction} 平 {trade_lots}手 @{price}（余 {trade['lots']}手），本次净盈亏 {partial_pnl:+.0f} 元", partial_pnl


def heal_fees():
    """修复已平仓成交缺失/不一致的手续费与净盈亏字段（#journal-account-mismatch）。

    逐笔检查每笔已平仓成交（exit_price 非 None）：
      - 若 open_fee / close_fee / gross_pnl / fee_total / pnl 任一缺失或不一致，
        用 _leg_fee + 存储的 entry_price / exit_price / lots 重新计算：
            gross      = (exit - entry) * mult * lots * dir_sign
            open_fee   = 已存 open_fee；缺失则按 _leg_fee(symbol, entry, lots) 补算
            close_fee  = _leg_fee(symbol, exit, lots)
            fee_total  = open_fee + close_fee
            pnl        = gross - fee_total
    未平仓成交（exit_price 为 None / pnl 为 None）保持原样，pnl 仍为 null。
    返回人类可读的变更说明列表；仅存在偏差时才写盘，幂等、安全。

    用途：配合 account_tracker.heal_from_journal() 在自愈时调用，
    确保 journal 自身手续费/盈亏口径正确，进而使 account_state.realized_pnl
    与 journal 已平仓盈亏合计一致。
    """
    data = _load()
    trades = data.get("trades", [])
    changes = []
    modified = False
    for t in trades:
        if t.get("exit_price") is None:
            continue  # 未平仓（exit_price 为 None）：保持 pnl=null，不动
        # 注：已平仓但 pnl 为 None（如导入抹除）属异常态，须下方重算自愈，不得跳过
        sym = t.get("symbol")
        if sym not in _MULTIPLIERS:
            continue
        mult = _MULTIPLIERS[sym]
        ds = 1 if t.get("direction") == "多" else -1
        entry = t.get("entry_price")
        exit_px = t.get("exit_price")
        lots = t.get("lots")
        if entry is None or exit_px is None or lots is None:
            continue
        gross = round((float(exit_px) - float(entry)) * mult * int(lots) * ds, 2)
        # 同日判定（开平同日历日 → 平今费率生效）
        _entry_date = (t.get("time") or "")[:10]
        _exit_date = (t.get("exit_time") or "")[:10]
        same_day = bool(_entry_date and _exit_date and _entry_date == _exit_date)
        # 按交易所基础费率（_FEE_SCHEDULE）重算双边手续费，确保与实时记账口径一致
        open_fee = float(_leg_fee(sym, entry, lots, "open", False))
        close_fee = float(_leg_fee(sym, exit_px, lots, "close", same_day))
        fee_total = round(open_fee + close_fee, 2)
        pnl = round(gross - fee_total, 2)
        # 对比现有值，仅偏差时更正
        need = {}
        if t.get("gross_pnl") != gross:
            need["gross_pnl"] = gross
        if t.get("open_fee") != round(open_fee, 2):
            need["open_fee"] = round(open_fee, 2)
        if t.get("close_fee") != close_fee:
            need["close_fee"] = close_fee
        if t.get("fee_total") != fee_total:
            need["fee_total"] = fee_total
        if t.get("pnl") != pnl:
            need["pnl"] = pnl
        if need:
            detail = "；".join(f"{k}: {t.get(k)}→{v}" for k, v in need.items())
            changes.append(
                f"{sym} {t.get('direction')} {lots}手 @{entry}→{exit_px}："
                f"毛利{gross}，开费{round(open_fee,2)}，平费{close_fee}，"
                f"费合计{fee_total}，净盈亏{pnl:+.2f}（{detail}）"
            )
            t.update(need)
            modified = True
    if modified:
        _save(data)
    return changes


def update_trade(trade_id, fields):
    """G2：更新某条成交的可编辑字段（白名单：note / exit_reason）。
    用 _LOCK 包住 读-改-写，避免并发损坏。返回 (ok, msg)。"""
    if not trade_id:
        return False, "缺少成交 id"
    allowed = {"note", "exit_reason", "strategy", "account"}
    clean = {k: fields[k] for k in allowed if k in fields}
    if not clean:
        return False, "无允许修改的字段（仅支持 note / exit_reason / strategy / account）"
    with _LOCK:
        data = _load()
        for t in data.get("trades", []):
            if str(t.get("id")) == str(trade_id):
                for k, v in clean.items():
                    t[k] = v
                _save(data)
                return True, "ok"
    return False, "未找到成交"


def _compute_summary(data):
    """从 data dict 直接计算 summary（供 _save 内部使用，避免重复 I/O）。"""
    trades = data.get("trades", [])
    closed = [t for t in trades if t["pnl"] is not None]
    open_trades = [t for t in trades if t["pnl"] is None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]

    total_pnl = sum(t["pnl"] for t in closed)
    total_fee = sum((t.get("fee_total") or 0) for t in closed)
    gross_pnl = sum((t.get("gross_pnl") if t.get("gross_pnl") is not None else t["pnl"]) for t in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    expect_pnl = total_pnl / len(closed) if closed else 0

    # 最大连胜/连亏
    max_streak = {"win": 0, "loss": 0}
    cur_win = cur_loss = 0
    for t in sorted(closed, key=lambda t: t.get("time", "")):
        if t["pnl"] > 0:
            cur_win += 1; cur_loss = 0
            max_streak["win"] = max(max_streak["win"], cur_win)
        else:
            cur_loss += 1; cur_win = 0
            max_streak["loss"] = max(max_streak["loss"], cur_loss)

    # 按品种统计
    by_symbol = {}
    for t in closed:
        s = t.get('symbol','')
        if s not in by_symbol:
            by_symbol[s] = {"count": 0, "wins": 0, "pnl": 0.0, "fee": 0.0}
        by_symbol[s]["count"] += 1
        by_symbol[s]["pnl"] += t["pnl"]
        by_symbol[s]["fee"] += (t.get("fee_total") or 0)
        if t["pnl"] > 0:
            by_symbol[s]["wins"] += 1
    for s in by_symbol:
        b = by_symbol[s]
        b["win_rate"] = round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0

    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "total_pnl": round(total_pnl, 2),
        "realized": round(total_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        "total_fee": round(total_fee, 2),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expect_pnl": round(expect_pnl, 2),
        "max_win_streak": max_streak["win"],
        "max_loss_streak": max_streak["loss"],
        "by_symbol": by_symbol,
    }


def summary():
    """成交统计：总笔数 / 胜率 / 总盈亏 / 期望值 / 最大连胜/连亏。"""
    data = _load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]
    open_trades = [t for t in data["trades"] if t["pnl"] is None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]

    total_pnl = sum(t["pnl"] for t in closed)
    total_fee = sum((t.get("fee_total") or 0) for t in closed)
    gross_pnl = sum((t.get("gross_pnl") if t.get("gross_pnl") is not None else t["pnl"]) for t in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    expect_pnl = total_pnl / len(closed) if closed else 0

    # 最大连胜/连亏
    max_streak = {"win": 0, "loss": 0}
    cur_win = cur_loss = 0
    for t in sorted(closed, key=lambda t: t.get("time", "")):
        if t["pnl"] > 0:
            cur_win += 1; cur_loss = 0
            max_streak["win"] = max(max_streak["win"], cur_win)
        else:
            cur_loss += 1; cur_win = 0
            max_streak["loss"] = max(max_streak["loss"], cur_loss)

    # 按品种统计
    by_symbol = {}
    for t in closed:
        s = t.get('symbol','')
        if s not in by_symbol:
            by_symbol[s] = {"count": 0, "wins": 0, "pnl": 0.0, "fee": 0.0}
        by_symbol[s]["count"] += 1
        by_symbol[s]["pnl"] += t["pnl"]
        by_symbol[s]["fee"] += (t.get("fee_total") or 0)
        if t["pnl"] > 0:
            by_symbol[s]["wins"] += 1
    for s in by_symbol:
        b = by_symbol[s]
        b["win_rate"] = round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0

    # R 倍数追踪：每笔实际吃到的 R = pnl / 计划风险金额（有真实止损距优先，否则按 risk_pct 风险预算回退）
    base_equity = _base_equity()
    risk_pct = _risk_pct()
    r_list = []
    cum = 0.0
    for t in sorted(closed, key=lambda t: t.get("time", "")):
        equity_before = base_equity - total_pnl + cum
        cum += t["pnl"]
        sd = t.get("stop_dist")
        if sd and sd > 0:
            actual_risk = sd * _MULTIPLIERS.get(t.get("symbol",""), 1) * t.get("lots", 1)
            R = t["pnl"] / actual_risk if actual_risk > 0 else 0.0
        else:
            planned_risk = max(1.0, equity_before * risk_pct / 100)
            R = t["pnl"] / planned_risk
        r_list.append(R)
    avg_R = round(sum(r_list) / len(r_list), 3) if r_list else 0.0
    r_dist = {"R>=2": 0, "1<=R<2": 0, "0<R<1": 0, "R<=0": 0}
    for R in r_list:
        if R >= 2:
            r_dist["R>=2"] += 1
        elif R >= 1:
            r_dist["1<=R<2"] += 1
        elif R > 0:
            r_dist["0<R<1"] += 1
        else:
            r_dist["R<=0"] += 1

    # ★ 今日统计（按开仓日期 OR 平仓日期过滤），供总览驾驶舱「交易执行」板块联动
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades_list = [t for t in data.get("trades", [])
                         if (t.get("time") or "")[:10] == today
                         or (t.get("exit_time") or "")[:10] == today]
    today_closed = [t for t in today_trades_list if t.get("pnl") is not None]
    today_pnl = sum(t["pnl"] for t in today_closed)
    today_fee = sum((t.get("fee_total") or 0) for t in today_closed)

    # ★ 2026-08-26: 添加持仓浮动盈亏和风险统计
    # 加载账户状态用于计算持仓风险
    _acct_file = os.path.join(HERE, "account_state.json")
    _positions = {}
    if os.path.exists(_acct_file):
        try:
            with open(_acct_file, "r") as _f:
                _acct = json.load(_f)
                _positions = _acct.get("positions", {})
        except Exception:
            pass
    
    # 计算持仓最大风险（基于止损价）
    _total_risk = 0.0
    _floating_details = []
    for _t in open_trades:
        _sym = _t["symbol"]
        _dir = _t.get('direction','?')
        _lots = _t.get('lots', 0)
        _entry = _t.get('entry_price', 0)
        _stop = _t.get("stop")
        _mult = _MULTIPLIERS.get(_sym, 10)
        
        # 计算单笔风险
        if _stop and _dir == "多":
            _risk = (_entry - _stop) * _lots * _mult
        elif _stop and _dir == "空":
            _risk = (_stop - _entry) * _lots * _mult
        else:
            _risk = 0
        
        _total_risk += _risk
        _floating_details.append({
            "symbol": _sym,
            "direction": _dir,
            "lots": _lots,
            "entry": _entry,
            "stop": _stop,
            "risk": round(_risk, 2),
        })
    
    return {
        "total_trades": len(data.get("trades", [])),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "today_trades": len(today_trades_list),
        "today_pnl": round(today_pnl, 2),
        "today_fee": round(today_fee, 2),
        # 已实现盈亏
        "total_pnl": round(total_pnl, 2),
        "realized": round(total_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        # 持仓风险统计
        "total_risk": round(_total_risk, 2),
        "floating_count": len(open_trades),
        # 胜率统计
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expect_pnl": round(expect_pnl, 2),
        "max_win_streak": max_streak["win"],
        "max_loss_streak": max_streak["loss"],
        "avg_R": avg_R,
        "r_dist": r_dist,
        "r_list": [round(x, 2) for x in r_list],
        "by_symbol": by_symbol,
        "session_split": session_split(),
        "floating_details": _floating_details,
        "updated": data.get("updated", ""),
    }


# ---------------------------------------------------------------------------
# F1 多策略 / 多账户视图：按 strategy（或 account）标签聚合盈亏，对比各策略表现。
#   缺省/空标签归入『未分类』。多账户维度同法，前端可切换分组键。
# ---------------------------------------------------------------------------
def by_strategy(group_by="strategy"):
    """按 strategy 或 account 标签聚合盈亏（F1）。返回各组 笔数/胜率/盈亏/手续费。"""
    data = _load()
    trades = data.get("trades", [])
    if group_by not in ("strategy", "account"):
        group_by = "strategy"

    def _key(t):
        v = (t.get(group_by) or "").strip()
        return v or ("未分类" if group_by == "strategy" else "主账户")

    groups = {}
    for t in trades:
        groups.setdefault(_key(t), []).append(t)

    out = []
    for name, ts in groups.items():
        closed = [t for t in ts if t.get("pnl") is not None]
        wins = [t for t in closed if t["pnl"] > 0]
        pnl = sum(t["pnl"] for t in closed)
        fee = sum((t.get("fee_total") or 0) for t in closed)
        out.append({
            "group": name,
            "total": len(ts),
            "closed": len(closed),
            "open": len(ts) - len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "pnl": round(pnl, 2),
            "fee": round(fee, 2),
            "net_pnl": round(pnl, 2),
        })
    out.sort(key=lambda x: x["pnl"], reverse=True)
    return {
        "ok": True,
        "group_by": group_by,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": out,
        "note": f"按成交记录 {group_by} 字段聚合；多策略/多账户视图可对比各组盈亏与胜率（非投资建议）。",
    }


# ---------------------------------------------------------------------------
# F2 交易记录导入：将导出的 CSV（或手工整理的同结构 CSV）合并回 trade_journal.json。
#   按 id 去重——已存在的 id 更新字段，缺 id 的行生成新 id 追加。返回合并统计。
# ---------------------------------------------------------------------------
_IMPORT_FIELDS = ["time", "symbol", "direction", "lots", "entry_price", "exit_price",
                  "exit_time", "exit_reason", "pnl", "signal_id", "stop_dist",
                  "strategy", "account", "note"]


def import_trades(rows):
    """rows: list[dict]，键与导出 CSV 列名一致。合并进 trade_journal.json。"""
    if not isinstance(rows, list):
        return False, "rows 必须是数组", {}
    data = _load()
    trades = data.setdefault("trades", [])
    by_id = {t.get("id"): t for t in trades if t.get("id")}
    added = 0; updated = 0; skipped = 0
    for raw in rows:
        if not isinstance(raw, dict):
            skipped += 1; continue
        sym = (raw.get("symbol") or "").strip()
        if not sym:
            skipped += 1; continue
        try:
            # 规范化：缺失项置 None（不填默认值），便于下方「选择性合并」识别 CSV 实际提供了哪些列
            norm = {
                "symbol": sym,
                "direction": (raw.get("direction") or "").strip() or None,
                "lots": int(float(raw.get("lots"))) if raw.get("lots") not in (None, "", "None") else None,
                "entry_price": float(raw.get("entry_price")) if raw.get("entry_price") not in (None, "", "None") else None,
                "exit_price": (None if raw.get("exit_price") in (None, "", "None") else float(raw.get("exit_price"))),
                "exit_time": (raw.get("exit_time") or None),
                "exit_reason": (raw.get("exit_reason") or None),
                "pnl": (None if raw.get("pnl") in (None, "", "None") else float(raw.get("pnl"))),
                "time": (raw.get("time") or None),
                "signal_id": (raw.get("signal_id") or None),
                "stop_dist": (None if raw.get("stop_dist") in (None, "", "None") else float(raw.get("stop_dist"))),
                "strategy": (raw.get("strategy") or None),
                "account": (raw.get("account") or None),
                "note": (raw.get("note") or None),
            }
        except Exception:
            skipped += 1; continue
        rid = (raw.get("id") or "").strip()
        if rid and rid in by_id:
            ex = by_id[rid]
            # 选择性合并：仅用 CSV 实际提供的非空值覆盖，杜绝部分列 CSV 抹掉既有字段
            # （曾因全量 update 把 pnl/exit_time/stop_dist 抹成 None 导致 total_fee 归零）
            for k, v in norm.items():
                if v is None or v == "":
                    continue
                ex[k] = v
            updated += 1
        else:
            # 新增成交：补默认值
            norm["time"] = norm["time"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            norm["direction"] = norm["direction"] or "多"
            norm["lots"] = norm["lots"] or 1
            norm["entry_price"] = norm["entry_price"] or 0.0
            norm["signal_id"] = norm["signal_id"] or ""
            norm["strategy"] = norm["strategy"] or ""
            norm["account"] = norm["account"] or "主账户"
            norm["note"] = norm["note"] or ""
            norm["id"] = rid or (datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6])
            trades.append(norm)
            by_id[norm["id"]] = norm
            added += 1
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save(data)
    return True, f"新增 {added} · 更新 {updated} · 跳过 {skipped}", {
        "added": added, "updated": updated, "skipped": skipped, "total": len(trades)}


# ---------------------------------------------------------------------------
# C1 日夜盘盈亏分解：按平仓时刻把成交归入 日盘 / 夜盘 / 其他，分别统计净盈亏与胜率。
#   （你的 6 个关注品种里鸡蛋/生猪无夜盘，玻璃/纯碱/焦煤/焦炭有夜盘，
#     分解后可看出自己在哪个时段更赚钱、夜盘是否在乱操作。）
# ---------------------------------------------------------------------------
def _session_of(ts):
    """按时间字符串判定交易时段：日盘 09:00-15:00 / 夜盘 21:00-次日02:30 / 其他。"""
    if not ts or len(ts) < 16:
        return "其他"
    try:
        hh = int(ts[11:13]); mm = int(ts[14:16])
    except ValueError:
        return "其他"
    t = hh * 60 + mm
    if 540 <= t <= 900:
        return "日盘"
    if t >= 1260 or t <= 150:
        return "夜盘"
    return "其他"


def session_split():
    """日夜盘分解（C1）：各时段的笔数 / 胜率 / 净盈亏 / 平均每笔。净盈亏均已扣手续费。"""
    data = _load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]
    buckets = {k: {"count": 0, "wins": 0, "pnl": 0.0} for k in ("日盘", "夜盘", "其他")}
    for t in closed:
        s = _session_of(t.get("exit_time") or t.get("time"))
        b = buckets.setdefault(s, {"count": 0, "wins": 0, "pnl": 0.0})
        b["count"] += 1
        b["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            b["wins"] += 1
    rows = []
    for k in ("日盘", "夜盘", "其他"):
        b = buckets[k]
        rows.append({
            "session": k, "count": b["count"],
            "win_rate": round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0.0,
            "pnl": round(b["pnl"], 2),
            "avg": round(b["pnl"] / b["count"], 2) if b["count"] else 0.0,
        })
    best = max([r for r in rows if r["count"]], key=lambda r: r["pnl"], default=None)
    return {"rows": rows, "best": (best["session"] if best else None),
            "note": "净盈亏已扣双边手续费；夜盘含次日 00:00-02:30"}


def session_performance():
    """G4：按平仓时段（日盘/夜盘）分解盈亏，含 per-symbol。
    仅统计已平仓成交（pnl 非 None）。「其他」时段不计入 day/night 汇总，但进 by_symbol。
    返回 {"ok":True,"day":{...},"night":{...},"by_symbol":{sym:{"day":{...},"night":{...}}}}。"""
    data = _load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]

    def _agg(bucket):
        cnt = len(bucket)
        wins = sum(1 for t in bucket if t["pnl"] > 0)
        pnl = sum(t["pnl"] for t in bucket)
        return {
            "pnl": round(pnl, 2),
            "trades": cnt,
            "wins": wins,
            "win_rate": round(wins / cnt, 4) if cnt else 0.0,
            "avg_pnl": round(pnl / cnt, 2) if cnt else 0.0,
        }

    day_bucket, night_bucket = [], []
    by_sym = {}
    for t in closed:
        s = _session_of(t.get("exit_time") or t.get("time"))
        sym = t.get("symbol")
        bs = by_sym.setdefault(sym, {"day": [], "night": []})
        if s == "日盘":
            day_bucket.append(t)
            bs["day"].append(t)
        elif s == "夜盘":
            night_bucket.append(t)
            bs["night"].append(t)
        # 「其他」时段：不进 day/night 汇总；by_symbol 里也不单列（保持 day/night 二分）
    by_symbol_out = {}
    for sym, bs in by_sym.items():
        by_symbol_out[sym] = {"day": _agg(bs["day"]), "night": _agg(bs["night"])}
    return {
        "ok": True,
        "day": _agg(day_bucket),
        "night": _agg(night_bucket),
        "by_symbol": by_symbol_out,
    }


# ---------------------------------------------------------------------------
# #127 实盘盈亏 F/T/C 维度归因：每笔已平仓成交按关联信号的
#   F(基本面) / T(触发) / C(资金确认) 三维得分，拆解实盘净盈亏的维度来源。
#   两种口径：
#     (1) 主导维度分桶：每笔计入「与交易方向对齐后强度最大」的单一维度；
#     (2) 比例贡献分解：pnl 按三维度「对齐交易方向后的强度」归一化权重拆分，
#         三项之和 = 信号驱动盈亏（可加分解）。
#   另给「该维度强信号时」(|score|≥阈值) 的胜率/盈亏，检验维度有效性。
#   无信号成交(账户同步/手动)计入『无信号驱动』，不参与 F/T/C 拆分。
# ---------------------------------------------------------------------------
def _attrib_match_signal(t, signals=None, sig_by_time=None, window_min=180):
    """把一笔成交绑定到引擎信号（精确 id 或 品种+方向+时间窗模糊）。回 (sig, method)。"""
    if signals is None:
        signals = []
        signals = _safe_read_json(SIGNAL_LOG, [])
    if sig_by_time is None:
        sig_by_time = {s.get("time", ""): s for s in signals}
    sid = (t.get("signal_id") or "")
    sig = sig_by_time.get(sid) if sid and sid not in ("账户同步", "manual", "") else None
    if sig:
        return sig, "id"
    # 模糊匹配：品种+方向相同、时间差≤窗口，取最近信号
    def _tt(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    ttime = _tt(t.get("time", ""))
    best, best_dt = None, None
    for s in signals:
        if s.get("symbol") != t.get("symbol") or s.get("direction") != t.get("direction"):
            continue
        st = _tt(s.get("time", ""))
        if not st or not ttime:
            continue
        d = abs((st - ttime).total_seconds()) / 60.0
        if d <= window_min and (best is None or d < best_dt):
            best, best_dt = s, d
    if best:
        return best, "fuzzy"
    return None, "none"


def pnl_attribution(strong_thresh=20):
    """实盘盈亏 F/T/C 维度归因（#127）。详见模块顶部说明。"""
    data = _load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]
    signals = _safe_read_json(SIGNAL_LOG, [])
    sig_by_time = {s.get("time", ""): s for s in signals}

    buckets = {k: {"label": L, "count": 0, "wins": 0, "pnl": 0.0}
               for k, L in (("F", "基本面主导"), ("T", "触发主导"),
                            ("C", "资金确认主导"), ("none", "无信号驱动"))}
    prop = {"F": 0.0, "T": 0.0, "C": 0.0}
    strong = {k: {"label": L, "count": 0, "wins": 0, "pnl": 0.0}
              for k, L in (("F", "基本面强信号"), ("T", "触发强信号"),
                           ("C", "资金确认强信号"))}
    detail = []
    matched_n = 0

    for t in sorted(closed, key=lambda x: x.get("exit_time") or x.get("time")):
        pnl = t["pnl"]
        dir_sign = 1 if t["direction"] == "多" else -1
        sig, method = _attrib_match_signal(t, signals, sig_by_time)
        scores = None
        if sig and sig.get("pipeline"):
            pipe = sig["pipeline"]
            F = float(pipe.get("F_bias") or 0)
            T = float(pipe.get("T_D") or 0)
            C = float(pipe.get("C_score") or 0)
            T5 = float(pipe.get("T_5m") or 0)
            scores = {"F": F, "T": T, "C": C, "T_5m": T5}
            matched_n += 1
        if scores is None:
            buckets["none"]["count"] += 1
            buckets["none"]["pnl"] += pnl
            if pnl > 0:
                buckets["none"]["wins"] += 1
            detail.append({"symbol": t["symbol"], "direction": t["direction"],
                           "pnl": pnl, "matched": False, "dom": "无信号驱动",
                           "method": method, "scores": None, "aligned": None})
            continue
        # 与交易方向对齐后的强度（正数=支持该笔交易，负数=反向）
        aligned = {k: scores[k] * dir_sign for k in ("F", "T", "C")}
        # 主导维度：对齐后（带符号）强度最大者 = 最支持该笔交易的维度
        dom = max(aligned, key=lambda k: aligned[k])
        buckets[dom]["count"] += 1
        buckets[dom]["pnl"] += pnl
        if pnl > 0:
            buckets[dom]["wins"] += 1
        # 比例贡献分解：按三维度「带符号对齐强度」归一化 → 三项之和严格=该笔盈亏；
        # 反向维度得到负贡献（真实归因），无净信号(三者和≈0)则均分。
        tot_signed = aligned["F"] + aligned["T"] + aligned["C"]
        if abs(tot_signed) > 1e-9:
            prop["F"] += pnl * aligned["F"] / tot_signed
            prop["T"] += pnl * aligned["T"] / tot_signed
            prop["C"] += pnl * aligned["C"] / tot_signed
        else:
            prop["F"] += pnl / 3.0
            prop["T"] += pnl / 3.0
            prop["C"] += pnl / 3.0
        # 强信号维度统计
        for k in ("F", "T", "C"):
            if abs(scores[k]) >= strong_thresh:
                strong[k]["count"] += 1
                strong[k]["pnl"] += pnl
                if pnl > 0:
                    strong[k]["wins"] += 1
        detail.append({"symbol": t["symbol"], "direction": t["direction"],
                       "pnl": pnl, "matched": True, "method": method,
                       "dom": dom, "scores": scores,
                       "aligned": {k: round(aligned[k], 2) for k in aligned}})

    total_pnl = sum(t["pnl"] for t in closed)
    none_pnl = buckets["none"]["pnl"]
    attributable = round(total_pnl - none_pnl, 2)
    for k in buckets:
        b = buckets[k]
        b["win_rate"] = round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0.0
        b["pnl"] = round(b["pnl"], 2)
    for k in strong:
        s = strong[k]
        s["win_rate"] = round(s["wins"] / s["count"] * 100, 1) if s["count"] else 0.0
        s["pnl"] = round(s["pnl"], 2)
    prop = {k: round(v, 2) for k, v in prop.items()}
    # 比例占比：相对总盈亏（F+T+C 占比 + 无信号占比 = 100%）
    prop_share = {}
    none_share = round(none_pnl / total_pnl * 100, 1) if abs(total_pnl) > 1e-9 else 0.0
    if abs(total_pnl) > 1e-9:
        prop_share = {k: round(prop[k] / total_pnl * 100, 1) for k in prop}
    else:
        prop_share = {k: 0.0 for k in prop}

    return {
        "ok": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strong_thresh": strong_thresh,
        "total_closed": len(closed),
        "matched": matched_n,
        "unmatched": len(closed) - matched_n,
        "total_pnl": round(total_pnl, 2),
        "attributable_pnl": attributable,
        "none_pnl": round(none_pnl, 2),
        "buckets": buckets,                       # 主导维度分桶
        "proportional": prop,                     # 比例贡献（元，信号驱动部分）
        "proportional_share": prop_share,         # 比例贡献占比（% 总盈亏）
        "none_share": none_share,                 # 无信号占比（% 总盈亏）
        "strong": strong,                         # 各维度强信号时表现
        "detail": detail,                         # 逐笔归因
        "note": ("实盘净盈亏按信号 F(基本面)/T(触发)/C(资金确认) 三维拆解："
                 "主导维度=与交易方向对齐后强度最大者；比例贡献=按三维度对齐强度归一化拆分"
                 "(三者之和=信号驱动盈亏)。强信号阈值 |score|≥%d。"
                 "无信号成交(账户同步/手动)计入『无信号驱动』，不参与 F/T/C 拆分（非投资建议）。"
                 % strong_thresh),
    }


def _base_equity():
    """用于 R/净值曲线的基准权益：优先取已同步权益，否则回退常量。"""
    try:
        import account_tracker as at
        st = at.load_state()
        eq = st.get("equity")
        if eq:
            return float(eq)
    except Exception:
        pass
    return 613090.0


def _risk_pct():
    try:
        import account_tracker as at
        cfg = at.load_config()
        return float(cfg.get("account", {}).get("risk_pct", 1.5))
    except Exception:
        return 1.5


def equity_curve(prices=None):
    """基于已平仓成交按时间累计构建净值曲线；末点追加当前动态权益（含未平仓浮动）。
    返回 {points:[{t,equity}], max_dd_pct, base_equity,
          peak_equity, current_dd_pct, dd_days, is_new_high}。"""
    data = _load()
    closed = sorted([t for t in data["trades"] if t.get("pnl") is not None],
                    key=lambda t: t.get("exit_time") or t["time"])
    base = _base_equity()
    total_pnl = sum(t["pnl"] for t in closed)
    start_equity = base - total_pnl if closed else base
    pts = [{"t": "起点", "equity": round(start_equity, 2)}]
    peak_equity = start_equity
    peak_date = None
    max_dd = 0.0
    eq = start_equity
    today = datetime.now().strftime("%Y-%m-%d")

    def _date(ts):
        ts = ts or ""
        return ts[:10] if len(ts) >= 10 else None

    for t in closed:
        eq += t["pnl"]
        d = _date(t.get("exit_time") or t["time"])
        if eq > peak_equity:
            peak_equity = eq
            peak_date = d
        dd = (peak_equity - eq) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)
        pts.append({"t": (t.get("exit_time") or t["time"]), "equity": round(eq, 2)})
    # 末点：当前动态权益（含未平仓浮动），让曲线收在今日真实权益
    live_eq = None
    try:
        import account_tracker as at
        snap = at.snapshot(prices or {})
        live_eq = snap.get("equity")
        if live_eq:
            d = today
            if live_eq > peak_equity:
                peak_equity = live_eq
                peak_date = d
            dd = (peak_equity - live_eq) / peak_equity * 100 if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)
            pts.append({"t": "当前(含浮动)", "equity": round(live_eq, 2)})
    except Exception:
        pass
    # 当前回撤 / 持续天数 / 新高状态（基于末点动态权益）
    if live_eq is not None and peak_equity > 0:
        if live_eq >= peak_equity:
            current_dd_pct = 0.0
            dd_days = 0
            is_new_high = True
        else:
            current_dd_pct = round((peak_equity - live_eq) / peak_equity * 100, 2)
            try:
                if peak_date:
                    dd_days = max(0, (datetime.strptime(today, "%Y-%m-%d")
                                      - datetime.strptime(peak_date, "%Y-%m-%d")).days)
                else:
                    dd_days = 0
            except Exception:
                dd_days = 0
            is_new_high = False
    else:
        current_dd_pct = 0.0
        dd_days = 0
        is_new_high = False
    return {
        "points": pts, "max_dd_pct": round(max_dd, 2), "base_equity": round(base, 2),
        "peak_equity": round(peak_equity, 2), "current_dd_pct": current_dd_pct,
        "dd_days": dd_days, "is_new_high": is_new_high,
    }


# ---- 日内权益分钟级采样（实时权益曲线） ----
INTRADAY_FILE = os.path.join(HERE, "intraday_equity.json")
_INTRADAY_LOCK = threading.Lock()


def _load_intraday():
    """加载日内采样数据。格式: {date: [{t: 'HH:MM', equity: float, floating: float}]"""
    try:
        if os.path.exists(INTRADAY_FILE):
            with open(INTRADAY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_intraday(data):
    with open(INTRADAY_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def sample_equity(prices=None):
    """采样当前权益并写入日内曲线。每自然分钟最多写一条。
    返回当前采样点 {t, equity, floating} 或 None。"""
    try:
        import account_tracker as at
        snap = at.snapshot(prices or {})
        live_eq = snap.get("equity")
        if live_eq is None:
            return None
        floating = snap.get("floating_pnl", 0.0) or 0.0
    except Exception:
        return None

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    with _INTRADAY_LOCK:
        data = _load_intraday()
        day_data = data.get(date_str, [])
        # 同一分钟内只保留最后一条（覆盖）
        if day_data and day_data[-1]["t"] == time_str:
            day_data[-1] = {"t": time_str, "equity": round(live_eq, 2),
                            "floating": round(floating, 2)}
        else:
            day_data.append({"t": time_str, "equity": round(live_eq, 2),
                             "floating": round(floating, 2)})
        data[date_str] = day_data
        # 只保留最近 7 天
        if len(data) > 7:
            sorted_dates = sorted(data.keys())
            for d in sorted_dates[:-7]:
                del data[d]
        _save_intraday(data)

    return {"t": time_str, "equity": round(live_eq, 2),
            "floating": round(floating, 2)}


def intraday_equity(date=None):
    """获取指定日期的日内权益曲线。date=None 则取今日。
    返回 {date, points:[{t, equity, floating}], day_start_equity, day_pnl, day_pnl_pct}"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    with _INTRADAY_LOCK:
        data = _load_intraday()
        points = data.get(date, [])

    # 找当日起始权益 = 昨日收盘权益（或当日第一笔已平仓前的权益）
    day_start = None
    try:
        # 从 equity_curve 里找最接近今日开盘的点
        curve = equity_curve()
        pts = curve.get("points", [])
        # 找最后一个非"当前"的点作为昨日收盘
        closed_pts = [p for p in pts if p["t"] != "当前(含浮动)"]
        if closed_pts:
            day_start = closed_pts[-1]["equity"]
    except Exception:
        pass

    # 如果有日内数据，用第一分钟前的权益算日盈亏
    current_eq = points[-1]["equity"] if points else (day_start or 0)
    day_pnl = (current_eq - day_start) if day_start else 0
    day_pnl_pct = (day_pnl / day_start * 100) if day_start and day_start > 0 else 0

    return {
        "date": date,
        "points": points,
        "day_start_equity": round(day_start, 2) if day_start else None,
        "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": round(day_pnl_pct, 2),
    }


def performance_metrics(prices=None):
    """估算绩效指标（基于成交记录，非严格日频）。标注为估算。"""
    data = _load()
    closed = sorted([t for t in data["trades"] if t.get("pnl") is not None],
                    key=lambda t: t.get("exit_time") or t["time"])
    s = summary()
    if not closed:
        return {"note": "暂无平仓记录", "total_return_pct": 0, "max_dd_pct": 0,
                "sharpe_est": 0, "calmar": 0, "profit_factor": 0,
                "avg_R": s.get("avg_R", 0), "win_rate": 0, "trades": 0}
    base = _base_equity()
    total_pnl = sum(t["pnl"] for t in closed)
    total_return_pct = round(total_pnl / base * 100, 2) if base else 0
    curve = equity_curve(prices)
    max_dd_pct = curve["max_dd_pct"]
    # 逐笔收益风险比
    rets = []
    cum = 0.0
    for t in closed:
        equity_before = base - total_pnl + cum
        cum += t["pnl"]
        denom = max(1.0, equity_before * _risk_pct() / 100)
        rets.append(t["pnl"] / denom)
    n = len(rets)
    mean_r = sum(rets) / n
    std_r = (sum((x - mean_r) ** 2 for x in rets) / n) ** 0.5 if n > 1 else 0.0
    ann = (250.0 / max(n, 1)) ** 0.5 if std_r > 0 else 0.0
    sharpe_est = round(mean_r / std_r * ann, 2) if std_r > 0 else 0.0
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    profit_factor = round(gross_w / gross_l, 2) if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)
    calmar = round(total_return_pct / max(max_dd_pct, 0.01), 2) if max_dd_pct > 0 else (total_return_pct if total_return_pct > 0 else 0.0)
    return {
        "trades": n, "total_return_pct": total_return_pct, "max_dd_pct": max_dd_pct,
        "sharpe_est": sharpe_est, "calmar": calmar, "profit_factor": profit_factor,
        "avg_R": s.get("avg_R", 0), "win_rate": s.get("win_rate", 0),
        "expect_pnl": s.get("expect_pnl", 0),
        "total_fee": s.get("total_fee", 0), "total_pnl": s.get("total_pnl", 0),
        "r_dist": s.get("r_dist", {}),
    }


def compare_to_papertrack(window_min=120):
    """真实成交 vs 引擎信号对比：哪些信号被采纳、实际盈亏 vs 信号建议。
    匹配策略：
      1) 精确匹配：成交 signal_id == 信号 time（前端录入时勾选了真实信号）
      2) 模糊匹配：账户同步/手动单 按 品种+方向+时间窗(±window_min) 回溯绑定当时活跃信号
         —— 解决“账户自动同步来的成交 signal_id='账户同步' 无法计入采纳率”的盲区
    """
    data = _load()
    closed = [t for t in data["trades"] if t.get("pnl") is not None]

    # 读信号日志
    signals = _safe_read_json(SIGNAL_LOG, [])

    def _tt(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    sig_by_time = {s.get("time", ""): s for s in signals}
    used_sig = set()

    def _get_slip(sym):
        """流动性敏感滑点（懒导入 four_dim_strategy，避免模块加载耦合）。"""
        try:
            from four_dim_strategy import get_slip_pts
            return get_slip_pts(sym)
        except Exception:
            return 1.0

    def _fill_quality(t, sig):
        """执行层成交质量（P2）：参考入场价 vs 实际成交 / 时机 / 手数 / 出场质量。"""
        out = {"adverse_slip_pts": None, "expected_slip_pts": None,
               "excess_slip_pts": None, "slip_in_R": None, "timing_min": None,
               "size_ratio": None, "exit_quality": None, "exec_flag": "缺参考价"}
        ref = sig.get("entry_ref")
        if ref is None or t.get("entry_price") is None:
            return out
        try:
            ref = float(ref); fill = float(t.get("entry_price", ref))
        except Exception:
            return out
        dir_sign = 1 if t["direction"] == "多" else -1
        # 不利滑点：多单买更高 / 空单卖更低 = 正值
        slippage_signed = (fill - ref) * dir_sign
        adverse = abs(slippage_signed)
        sd = sig.get("stop_dist") or t.get("stop_dist")
        expected = _get_slip(t.get("symbol",""))
        out["adverse_slip_pts"] = round(adverse, 2)
        out["expected_slip_pts"] = round(expected, 2)
        out["excess_slip_pts"] = round(adverse - expected, 2)
        if sd:
            try:
                out["slip_in_R"] = round(slippage_signed / float(sd), 3)
            except Exception:
                pass
        st = _tt(sig.get("time", "")); ft = _tt(t.get("time", ""))
        if st and ft:
            out["timing_min"] = round(abs((ft - st).total_seconds()) / 60.0, 1)
        sl = sig.get("lots")
        if sl:
            try:
                out["size_ratio"] = round(t.get("lots", 1) / float(sl), 2)
            except Exception:
                pass
        pnl = t.get("pnl")
        if pnl is not None:
            tgt = sig.get("target"); stp = sig.get("stop")
            if pnl > 0 and tgt and sd:
                out["exit_quality"] = ("达标止盈" if abs(float(t.get("exit_price", 0)) - float(tgt)) / float(sd) <= 0.25
                                       else "盈利出场(偏离目标)")
            elif pnl <= 0 and stp and sd:
                out["exit_quality"] = ("触止损" if abs(float(t.get("exit_price", 0)) - float(stp)) / float(sd) <= 0.25
                                       else "亏损出场(偏离止损)")
        # 判定：超流动预期滑点 >1.5倍，或占止损距 >5%，或严重偏离计划手数 → 执行偏差
        bad = []
        if out["excess_slip_pts"] is not None and out["excess_slip_pts"] > expected * 0.5:
            bad.append("滑点超预算")
        if out["slip_in_R"] is not None and abs(out["slip_in_R"]) > 0.05:
            bad.append("滑点>5%止损距")
        if out["size_ratio"] is not None and abs(out["size_ratio"] - 1.0) > 0.25:
            bad.append("手数偏离计划")
        out["exec_flag"] = "；".join(bad) if bad else "执行良好"
        return out

    def _mk(t, sig, method):
        _safe_id = t.get("id") or f"legacy_{t.get('symbol','?')}_{t.get('time','')}"
        rec = {
            "trade_id": _safe_id, "symbol": t.get("symbol","?"), "direction": t.get("direction","?"),
            "entry": t.get("entry_price"), "exit": t.get("exit_price"), "pnl": t.get("pnl"),
            "exit_reason": t.get("exit_reason"),
            "signal_time": sig.get("time"), "match_method": method,
            "signal_stop": sig.get("stop"), "signal_target": sig.get("target"),
            "signal_lots": sig.get("lots"), "signal_type": sig.get("signal_type"),
            "signal_entry_ref": sig.get("entry_ref"),
        }
        rec.update(_fill_quality(t, sig))
        return rec

    matched = []
    remain = []          # 精确未匹配的成交，进入模糊匹配
    for t in closed:
        sid = t.get("signal_id", "")
        sig = sig_by_time.get(sid) if sid and sid not in ("账户同步", "manual", "") else None
        if sig and id(sig) not in used_sig:
            used_sig.add(id(sig))
            matched.append(_mk(t, sig, "id"))
        else:
            remain.append(t)

    # 模糊匹配：品种+方向相同、时间差≤窗口，取最近未用信号
    for t in remain:
        ttime = _tt(t.get("time", ""))
        best, best_dt = None, None
        for s in signals:
            if id(s) in used_sig:
                continue
            if s.get("symbol") != t.get("symbol") or s.get("direction") != t.get("direction"):
                continue
            st = _tt(s.get("time", ""))
            if not st or not ttime:
                continue
            d = abs((st - ttime).total_seconds()) / 60.0
            if d <= window_min and (best is None or d < best_dt):
                best, best_dt = s, d
        if best:
            used_sig.add(id(best))
            matched.append(_mk(t, best, "fuzzy"))

    # 仍未匹配上的 = 真正手动/冲动交易
    unmatched = []
    for t in remain:
        _tid2 = t.get("id") or ""
        if any(m["trade_id"] == _tid2 for m in matched):
            continue
        unmatched.append({
            "trade_id": t.get("id") or "", "symbol": t.get("symbol","?"), "direction": t.get("direction","?"),
            "entry": t.get("entry_price"), "exit": t.get("exit_price"), "pnl": t.get("pnl"),
            "exit_reason": t.get("exit_reason"),
            "note": "无匹配信号（可能为冲动/手动交易）",
        })

    total_signals = len(signals)
    acted_on = len(matched)
    closed_n = len(closed)
    adoption_rate = round(acted_on / total_signals * 100, 1) if total_signals else 0
    follow_rate = round(acted_on / closed_n * 100, 1) if closed_n else 0
    m_wins = [m for m in matched if (m["pnl"] or 0) > 0]
    matched_win_rate = round(len(m_wins) / acted_on * 100, 1) if acted_on else 0
    matched_pnl = round(sum((m["pnl"] or 0) for m in matched), 2)

    # ── 执行层成交质量聚合（P2）──
    fq = [m for m in matched if m.get("adverse_slip_pts") is not None]
    fill_quality = {
        "n_scored": len(fq),
        "avg_adverse_slip_pts": round(sum(m["adverse_slip_pts"] for m in fq) / len(fq), 2) if fq else None,
        "avg_excess_slip_pts": round(sum(m["excess_slip_pts"] for m in fq) / len(fq), 2) if fq else None,
        "max_excess_slip_pts": round(max((m["excess_slip_pts"] for m in fq), default=0), 2),
        "avg_timing_min": round(sum(m["timing_min"] for m in fq if m["timing_min"] is not None)
                                / max(1, sum(1 for m in fq if m["timing_min"] is not None)), 1) if fq else None,
        "avg_size_ratio": round(sum(m["size_ratio"] for m in fq if m["size_ratio"] is not None)
                                / max(1, sum(1 for m in fq if m["size_ratio"] is not None)), 2) if fq else None,
        "poor_execution": sum(1 for m in fq if m.get("exec_flag") not in (None, "执行良好")),
        "poor_list": [{"symbol": m["symbol"], "trade_id": m["trade_id"],
                       "excess_slip_pts": m["excess_slip_pts"], "slip_in_R": m["slip_in_R"],
                       "size_ratio": m["size_ratio"], "flag": m["exec_flag"]}
                      for m in fq if m.get("exec_flag") not in (None, "执行良好")],
    }

    return {
        "total_signals": total_signals,
        "acted_on": acted_on,
        "adoption_rate": adoption_rate,
        "follow_rate": follow_rate,
        "closed_trades": closed_n,
        "matched_trades": matched,
        "unmatched_trades": unmatched,
        "matched_win_rate": matched_win_rate,
        "matched_pnl": matched_pnl,
        "fill_quality": fill_quality,
    }


def today_pnl(sym=None):
    """当日已实现盈亏（驱动状态机日亏停机线）。
    sym 省略→全品种合计；传入 sym→仅该品种当日已实现盈亏（供 focus_board 使用）。"""
    data = _load()
    today = datetime.now().strftime("%Y-%m-%d")
    closed = [t for t in data["trades"]
              if t["pnl"] is not None and (t.get("exit_time") or "")[:10] == today]
    if sym is not None:
        closed = [t for t in closed if t.get("symbol") == sym]
    return sum(t["pnl"] for t in closed)


def current_loss_streak():
    """当前连续亏损笔数（驱动状态机连续止损降档）。

    2026-08-27 整改：当存在历史熔断解除记录时，只统计解除后的交易，
    避免被熔断前的亏损'卡'住，导致系统永远无法恢复。
    熔断强平交易本身计入统计（视为正常交易结果）。
    """
    import os
    import json as _json
    data = _load()
    # 查找最近的熔断解除时间戳
    ks_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'killswitch_state.json')
    reset_time = None
    try:
        if os.path.exists(ks_path):
            with open(ks_path, 'r', encoding='utf-8') as _f:
                _ks = _json.load(_f)
            reset_time = _ks.get('reset_at')
    except Exception:
        pass
    # 过滤：只统计 reset_at 之后的交易（如果存在）
    closed = sorted(
        [t for t in data['trades']
         if t.get('pnl') is not None],
        key=lambda t: t['time']
    )
    if reset_time:
        closed = [t for t in closed if (t.get('time') or '') >= reset_time]
    streak = 0
    for t in reversed(closed):
        if t['pnl'] < 0:
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# CLI：独立跑看报告
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("四维策略 · 成交记录器 — 真实 papertrack 报告")
    print("=" * 60)
    s = summary()
    print(f"\n📊 成交统计")
    print(f"  已平仓: {s['total_trades']} 笔 | 未平仓: {s['open_trades']} 笔")
    print(f"  总盈亏: {s['total_pnl']:+,.0f} 元 | 胜率: {s['win_rate']:.1f}%")
    print(f"  平均盈利: {s['avg_win']:+,.0f} | 平均亏损: {s['avg_loss']:+,.0f} | 期望: {s['expect_pnl']:+,.0f}")
    print(f"  最大连胜: {s['max_win_streak']} | 最大连亏: {s['max_loss_streak']}")
    if s["by_symbol"]:
        print(f"\n  按品种:")
        for sym, b in s["by_symbol"].items():
            print(f"    {sym}: {b['count']}笔, 盈亏 {b['pnl']:+,.0f}, 胜率 {b['win_rate']:.1f}%")

    cmp = compare_to_papertrack()
    print(f"\n🔗 信号采纳")
    print(f"  总信号: {cmp['total_signals']} | 已采纳: {cmp['acted_on']} | 采纳率: {cmp['adoption_rate']}%")
    print(f"  匹配成交: {len(cmp['matched_trades'])} | 无匹配: {len(cmp['unmatched_trades'])}")
    if cmp["matched_trades"]:
        print(f"\n  逐笔对比（成交 vs 信号建议）:")
        for m in cmp["matched_trades"]:
            print(f"    {m['symbol']} {m['direction']}: 入{m['entry']}→出{m['exit']} "
                  f"盈亏{m['pnl']:+.0f} | 信号止损{m['signal_stop']}/目标{m['signal_target']} "
                  f"({m['exit_reason']})")

    print(f"\n---\n*非投资建议，模拟盘验证数据。*")
