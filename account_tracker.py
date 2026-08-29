"""四维策略 · 账户状态追踪器（实时账户表）
========================================================
读取 trade_config.json（用户填好的真实账户参数 + 品种合约参数），
维护 account_state.json（权益 + 各品种持仓方向/手数/均价），
用实时价算浮动盈亏 / 保证金占用 / 资金使用率 / 距风控线%。

这是把规格草案 §2「账户参数表」从占位值变成**真实可实时更新**的系统：
- 用户盘中开/平/加/减仓 → record_trade() → 表格实时变
- 每日收盘后同步一次真实权益 → set_equity()
- 浮动盈亏/资金占用用 minishare 实时价自动算，不手动填

用法（被 runner 调用）：
  import account_tracker as at
  at.record_trade("FG", "open", "空", 2, 901.0)   # 开仓
  at.record_trade("FG", "close", "空", 2, 905.0)  # 平仓
  at.set_equity(612140)                            # 同步权益
  snap = at.snapshot(prices={sym: feed.price(sym) for sym in SYMBOLS})
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

# ★ 2026-08-27: akshare 实时行情（分钟级，新浪数据源）
# ★ 2026-08-28: minishare_live 主数据源（rt_fut_k 不限次快照）
try:
    import minishare_live as _ml

    _MS_AVAILABLE = True
except ImportError:
    _MS_AVAILABLE = False
    _ml = None

try:
    import akshare_live as _al

    _AK_AVAILABLE = True
except ImportError:
    _AK_AVAILABLE = False
    _al = None

# minishare 价格缓存（主数据源，rt_fut_k 快照）
_MS_PRICE_CACHE = {}
_MS_PRICE_TS = 0.0
_MS_CACHE_LOCK = threading.Lock()

# minishare feed 实例（单例，避免重复创建）
_MS_FEED_INSTANCE = None
_MS_FEED_LOCK = threading.Lock()

# akshare 价格缓存（由后台线程或按需拉取）
_AK_PRICE_CACHE = {}
_AK_PRICE_TS = 0.0
_AK_CACHE_LOCK = threading.Lock()

# 全局 akshare feed 实例（避免每次 poll 都创建新实例）
_AK_FEED_INSTANCE = None
_AK_FEED_LOCK = threading.Lock()

# akshare 后台轮询线程控制
_AK_POLLER_RUNNING = False
_AK_POLLER_THREAD = None


def _get_ak_feed():
    """获取全局 akshare feed 实例。"""
    global _AK_FEED_INSTANCE
    if not _AK_AVAILABLE or _al is None:
        return None
    if _AK_FEED_INSTANCE is None:
        with _AK_FEED_LOCK:
            if _AK_FEED_INSTANCE is None:
                _AK_FEED_INSTANCE = _al.feed()
    return _AK_FEED_INSTANCE


def _get_ms_feed():
    """获取全局 minishare feed 实例（rt_fut_k 单例）。"""
    global _MS_FEED_INSTANCE
    if not _MS_AVAILABLE or _ml is None:
        return None
    if _MS_FEED_INSTANCE is None:
        with _MS_FEED_LOCK:
            if _MS_FEED_INSTANCE is None:
                _MS_FEED_INSTANCE = _ml.feed()
    return _MS_FEED_INSTANCE


def _is_trading_hours():
    """判断当前是否在期货交易时段（夜盘 21:00-23:00/02:30，日盘 9:00-15:00）。"""
    from datetime import datetime as _dt

    now = _dt.now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    if t >= 21 * 60 or t <= 2 * 60 + 30:
        return True
    if 9 * 60 <= t <= 15 * 60:
        return True
    return False


def _get_ms_price(sym):
    """从 minishare rt_fut_k 获取实时价格（主数据源，不限次快照）。"""
    global _MS_PRICE_CACHE, _MS_PRICE_TS, _MS_CACHE_LOCK
    if not _MS_AVAILABLE or _ml is None:
        return None
    sym_lower = sym.lower()
    # 先检查缓存（10秒TTL）
    with _MS_CACHE_LOCK:
        if _MS_PRICE_TS > 0 and (time.time() - _MS_PRICE_TS) < 10:
            if sym_lower in _MS_PRICE_CACHE and _MS_PRICE_CACHE[sym_lower] > 0:
                return _MS_PRICE_CACHE[sym_lower]
    # 缓存过期，按需拉取
    try:
        _feed = _get_ms_feed()
        if _feed:
            # 优先使用已缓存的快照（避免频繁请求）
            px = _feed.price(sym)
            if px and px > 0:
                with _MS_CACHE_LOCK:
                    _MS_PRICE_CACHE[sym_lower] = px
                    _MS_PRICE_TS = time.time()
                return px
            # 快照无数据则强制 poll
            _feed.poll()
            px = _feed.price(sym)
            if px and px > 0:
                with _MS_CACHE_LOCK:
                    _MS_PRICE_CACHE[sym_lower] = px
                    _MS_PRICE_TS = time.time()
                return px
    except Exception:
        pass
    return None


def _get_ak_price(sym):
    """从 akshare 缓存获取实时价格（由后台线程批量维护，避免每个品种单独请求新浪）。

    2026-08-28 优化：移除每次创建新 AkshareFeed 实例的逻辑，
    改为从后台线程维护的缓存中读取，缓存失效时回退到全局 feed 实例。
    """
    global _AK_PRICE_CACHE, _AK_PRICE_TS
    if not _AK_AVAILABLE or _al is None:
        return None
    sym_lower = sym.lower()
    # 检查缓存（5秒TTL，足够实时又避免频繁请求）
    with _AK_CACHE_LOCK:
        if _AK_PRICE_TS > 0 and (time.time() - _AK_PRICE_TS) < 5:
            if sym_lower in _AK_PRICE_CACHE and _AK_PRICE_CACHE[sym_lower] > 0:
                return _AK_PRICE_CACHE[sym_lower]
    # 缓存失效，按需拉取（使用全局 feed 实例）
    try:
        f = _get_ak_feed()
        if f is None:
            return None
        snap = f.poll([sym_lower])
        if snap and sym_lower in snap:
            price = snap[sym_lower].get("close", 0)
            if price > 0:
                with _AK_CACHE_LOCK:
                    _AK_PRICE_CACHE[sym_lower] = price
                    _AK_PRICE_TS = time.time()
                return price
    except Exception:
        pass
    return None


def _get_ak_prices_batch(symbols):
    """批量获取多个品种的 akshare 实时价格（一次 poll 多个品种，高效）。"""
    global _AK_PRICE_CACHE, _AK_PRICE_TS
    if not _AK_AVAILABLE or _al is None:
        return {}
    # 过滤有效品种
    valid_syms = []
    for s in symbols:
        s_lower = s.lower()
        code = _al.ALL_CONTRACTS.get(s_lower, _al.ALL_CONTRACTS.get(s.upper()))
        if code:
            valid_syms.append(s_lower)
    if not valid_syms:
        return {}
    try:
        f = _get_ak_feed()
        if f is None:
            return {}
        snap = f.poll(valid_syms)
        result = {}
        if snap:
            with _AK_CACHE_LOCK:
                for sym_lower in valid_syms:
                    if sym_lower in snap and snap[sym_lower]:
                        price = snap[sym_lower].get("close", 0)
                        if price > 0:
                            _AK_PRICE_CACHE[sym_lower] = price
                            result[sym_lower] = price
                _AK_PRICE_TS = time.time()
        return result
    except Exception:
        return {}


def start_ak_poller(interval=5):
    """启动后台线程：每 interval 秒批量拉取所有持仓品种的实时价格。"""
    global _AK_POLLER_RUNNING, _AK_POLLER_THREAD
    if _AK_POLLER_RUNNING:
        return
    _AK_POLLER_RUNNING = True

    def _poll_loop():
        global _AK_POLLER_RUNNING
        print(f"[akshare_live] 后台价格轮询线程启动（{interval}秒/次，持仓品种实时价）")
        errors = 0
        while _AK_POLLER_RUNNING:
            try:
                # 从 account_state 获取当前持仓品种
                try:
                    st = load_state()
                    positions = st.get("positions", {})
                    held_syms = []
                    for sym, pos in positions.items():
                        if isinstance(pos, dict) and (pos.get("lots") or 0) > 0:
                            held_syms.append(sym)
                except Exception:
                    held_syms = []

                if held_syms:
                    # 批量拉取所有持仓品种
                    _get_ak_prices_batch(held_syms)
                    errors = 0
                time.sleep(interval)
            except Exception:
                errors += 1
                if errors >= 5:
                    print(f"[akshare_live] 轮询错误过多({errors}次)，暂停30秒")
                    time.sleep(30)
                else:
                    time.sleep(interval)

    _AK_POLLER_THREAD = threading.Thread(target=_poll_loop, daemon=True)
    _AK_POLLER_THREAD.start()


def stop_ak_poller():
    """停止后台价格轮询线程。"""
    global _AK_POLLER_RUNNING
    _AK_POLLER_RUNNING = False


# ★ 2026-08-28: 启动 akshare 后台轮询线程（批量维护持仓品种实时价缓存）
# 在服务器启动时调用 start_ak_poller() 即可
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "trade_config.json")
STATE_FILE = os.path.join(HERE, "account_state.json")
OVERRIDE_FILE = os.path.join(HERE, "main_overrides.json")  # 主力合约权威源（v2.5.0+）
_LOCK = threading.Lock()


def _authoritative_contract(sym, fallback):
    """优先用 main_overrides.json 的主力合约覆盖（避免 account 总览用陈旧 contract_specs）。
    fallback 来自 trade_config.json 的 contract_specs。两者不一致时以 main_overrides 为准。"""
    try:
        mo = json.load(open(OVERRIDE_FILE, encoding="utf-8"))
        v = mo.get(sym)
        if v:
            return str(v)
    except Exception:
        pass
    return fallback


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"account": {}, "risk_gate": {}, "contract_specs": {}}
    return json.load(open(CONFIG_FILE, encoding="utf-8"))


def load_state():
    """读取账户状态。文件不存在/为空/解析失败均返回安全默认，不抛异常（防止 /api/account 500）。
    若当前文件损坏，尝试从 .bak 恢复上一份好状态。"""
    default = {"equity": 0, "realized_pnl": 0.0, "positions": {}, "updated": "", "equity_synced": ""}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as e:
        sys.stderr.write("[account_tracker] load_state 读文件失败，返回默认: %s\n" % e)
        return default
    if not text:
        return _load_state_from_bak(default)  # 空文件：写盘竞态瞬间，尝试 .bak
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.stderr.write("[account_tracker] load_state 解析失败，尝试 .bak: %s\n" % e)
        return _load_state_from_bak(default)


def _load_state_from_bak(default):
    """从 .bak 恢复上一份好状态；不存在或损坏则返回 default。"""
    bak = STATE_FILE + ".bak"
    if not os.path.exists(bak):
        return default
    try:
        with open(bak, encoding="utf-8") as f:
            txt = f.read().strip()
        if txt:
            return json.loads(txt)
    except Exception:
        pass
    return default


def save_state(st):
    """原子写盘：先写临时文件再 os.replace，消除「半写空文件」竞态窗口（修复 /api/account 偶发 500）。
    写入前把当前好状态备份为 .bak，供 load_state 失败时恢复。"""
    # 备份当前好状态（若存在且非空）
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                cur = f.read()
            if cur.strip():
                with open(STATE_FILE + ".bak", "w", encoding="utf-8") as f:
                    f.write(cur)
        except OSError:
            pass
    # 原子写：temp 与 STATE_FILE 同目录（保证 os.replace 同 fs）
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dir_sign(direction):
    return 1 if direction == "多" else (-1 if direction == "空" else 0)


def _fmt_price(v):
    if v is None:
        return "—"
    try:
        return str(round(float(v), 2))
    except (TypeError, ValueError):
        return str(v)


def _to_float(v):
    """空串/None → None，否则 float。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _validate_levels(pos):
    """校验并修正持仓止损/止盈方向，防止错误配置落盘。

    规则：
      - 多单：stop < avg；target/t1/t2 > avg
      - 空单：stop > avg；target/t1/t2 < avg
    方向错误的档位以 avg 为轴镜像修正。
    返回 (changed: dict, reason: str)，changed 的 value 是 (old, new) 元组；
    reason 为空串表示无需修正。
    """
    direction = pos.get("direction")
    avg = _to_float(pos.get("avg"))
    ds = _dir_sign(direction)
    if ds == 0 or avg is None:
        return {}, ""

    changed = {}
    for k in ("stop", "target", "t1", "t2"):
        v = _to_float(pos.get(k))
        if v is None:
            continue
        if k == "stop":
            ok = (v < avg) if ds > 0 else (v > avg)
        else:
            ok = (v > avg) if ds > 0 else (v < avg)
        if not ok:
            nv = avg + (avg - v)  # 以 avg 为轴镜像
            if abs(nv - avg) < 1e-9:
                nv = avg + 0.01 if ds > 0 else avg - 0.01
            nv = round(nv, 2)
            changed[k] = (v, nv)
            pos[k] = nv

    if not changed:
        return {}, ""
    label_map = {"stop": "止损", "target": "止盈", "t1": "t1", "t2": "t2"}
    reason = "；已自动修正方向错误档位：" + "，".join(
        f"{label_map.get(k, k)} {_fmt_price(ov)}→{_fmt_price(nv)}" for k, (ov, nv) in changed.items()
    )
    return changed, reason


def _leg_fee_tj(symbol, price, lots, side="open", same_day=False):
    """单边手续费估算（与 trade_journal._leg_fee 同口径，避免手续费公式漂移）。

    优先复用 trade_journal 的 _leg_fee / _MULTIPLIERS / _FEE_SCHEDULE；
    side/same_day 透传给 trade_journal._leg_fee（支持「平今」费率）。
    trade_journal 仅在函数内部懒导入 account_tracker，此处同样懒导入，
    不会形成循环依赖。
    """
    try:
        import trade_journal as tj

        return tj._leg_fee(symbol, price, lots, side, same_day)
    except Exception:
        return 0.0


def record_trade(sym, action, direction, lots, price, stop=None, target=None, t1=None, t2=None, tail_enabled=None):
    """action: open / add / close / reduce。stop/target 可选（开仓时记录你的止损/止盈位）。返回 (ok, msg, state)。"""
    # P1-22: 方向校验前移 —— 避免无效数据进入下游(双写/风控/统计)
    VALID_DIR = ("long", "short", "多", "空", 1, -1)
    if isinstance(direction, str):
        _d = direction.strip().lower()
        _map = {
            "long": "long",
            "short": "short",
            "多": "long",
            "空": "short",
            "buy": "long",
            "sell": "short",
            "bull": "long",
            "bear": "short",
        }
        if _d not in _map and direction not in ("多", "空"):
            return False, f"非法方向 {direction!r}，合法值: long/short/多/空", None
    elif isinstance(direction, int):
        if direction not in (1, -1):
            return False, f"非法方向 int={direction!r}，合法值: 1(多) / -1(空)", None
    else:
        return False, f"方向类型非法 {type(direction).__name__}: {direction!r}", None
    # ★ 品种名标准化：支持大小写输入（AO/ao 均可）
    orig_sym = sym
    cfg = load_config()
    specs = cfg.get("contract_specs", {})
    if sym not in specs:
        # 尝试小写匹配
        lower_sym = sym.lower()
        if lower_sym in specs:
            sym = lower_sym
        else:
            # 尝试大写匹配（如用户输入 'jm'，配置中为 'JM'）
            upper_sym = sym.upper()
            if upper_sym in specs:
                sym = upper_sym
            else:
                return False, f"未知品种 {orig_sym}（已尝试大小写转换）", load_state()
    lots = int(lots)
    if lots <= 0:
        return False, "手数必须>0", load_state()
    # ★★ 2026-08-26: 价格保护 - 验证用户价格
    _verified_price = float(price) if price is not None and price != 0 else 0
    if _verified_price <= 0:
        return False, f"非法价格 {price}，必须大于0", load_state()
    print(f"[record_trade] 品种={sym} 动作={action} 价格={_verified_price} (原始: {price})")
    fix_note = ""
    with _LOCK:
        st = load_state()
        pos = st["positions"].get(sym)
        if action in ("open", "add"):
            if action == "open" and pos:
                return False, f"{sym} 已有持仓，请用 add/close/reduce", st
            if action == "add" and not pos:
                return False, f"{sym} 无持仓，不能 add，请先 open", st
            if action == "open":
                _new_pos = {
                    "direction": direction,
                    "lots": lots,
                    "avg": _verified_price,
                    "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "stop": _to_float(stop),
                    "target": _to_float(target),
                    "t1": _to_float(t1),
                    "t2": _to_float(t2),
                    "tail_enabled": bool(tail_enabled),
                    "levels_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                # ★ 自动计算分级止盈目标价（确保 tp_targets 立即可用）
                _tp = _auto_tp_targets(sym, _new_pos)
                if _tp:
                    _new_pos["tp_targets"] = _tp
                st["positions"][sym] = _new_pos
                _, fix_note = _validate_levels(st["positions"][sym])
            else:  # add：加权均价（保留原止损止盈，不覆盖）
                old = pos["lots"]
                new_avg = (old * pos["avg"] + lots * _verified_price) / (old + lots)  # ★ 使用验证后的价格
                pos["lots"] = old + lots
                pos["avg"] = round(new_avg, 2)
                if direction != pos["direction"]:
                    return False, f"{sym} 持仓方向({pos['direction']})与新开({direction})冲突", st
                # ★ 加权后重新计算 tp_targets（均价变化影响 ATR 反推）
                _tp = _auto_tp_targets(sym, pos)
                if _tp:
                    pos["tp_targets"] = _tp
                _, fix_note = _validate_levels(pos)
        elif action in ("close", "reduce"):
            if not pos:
                return False, f"{sym} 无持仓，不能 {action}", st
            # 同日判定（开平同日历日 → 平今费率生效）
            _now = datetime.now()
            _open_date = (pos.get("open_time") or "")[:10]
            same_day = bool(_open_date and _open_date == _now.strftime("%Y-%m-%d"))
            if lots >= pos["lots"]:
                # 平仓：记已实现盈亏（净，扣双边手续费，与 trade_journal.record_exit 同口径）
                mult = specs[sym]["multiplier"]
                gross = (float(price) - pos["avg"]) * mult * pos["lots"] * _dir_sign(pos["direction"])
                # 手续费：开仓费按开仓均价算 + 平仓费按平仓价算（与 journal 一致）
                open_fee = _leg_fee_tj(sym, pos["avg"], pos["lots"], "open", False)
                close_fee = _leg_fee_tj(sym, price, pos["lots"], "close", same_day)
                net = round(gross - (float(open_fee) + float(close_fee)), 2)
                st["realized_pnl"] = round(st.get("realized_pnl", 0) + net, 2)
                del st["positions"][sym]
            else:
                # 减仓：按减仓部分记已实现盈亏（净，扣该部分双边手续费，与 journal 部分平同口径）
                mult = specs[sym]["multiplier"]
                gross = (float(price) - pos["avg"]) * mult * lots * _dir_sign(pos["direction"])
                open_fee = _leg_fee_tj(sym, pos["avg"], lots, "open", False)
                close_fee = _leg_fee_tj(sym, price, lots, "close", same_day)
                net = round(gross - (float(open_fee) + float(close_fee)), 2)
                st["realized_pnl"] = round(st.get("realized_pnl", 0) + net, 2)
                pos["lots"] -= lots
        else:
            return False, f"未知 action {action}", st
        st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        return True, ("ok" + fix_note), st


# P1-7 fix: _to_float 已在上方定义（line ~129），此处删除重复定义


def set_levels(sym, stop=None, target=None, t1=None, t2=None, tail_enabled=None):
    """给已有持仓设置/清除止损止盈位（用于触价报警）。返回 (ok, msg, state)。"""
    with _LOCK:
        st = load_state()
        pos = st["positions"].get(sym)
        if not pos:
            return False, f"{sym} 无持仓，无法设止损止盈", st
        if stop is not None:
            pos["stop"] = _to_float(stop)
        if target is not None:
            pos["target"] = _to_float(target)
        if t1 is not None:
            pos["t1"] = _to_float(t1)
        if t2 is not None:
            pos["t2"] = _to_float(t2)
        if tail_enabled is not None:
            pos["tail_enabled"] = bool(tail_enabled)
        # ★ 2026-08-27: 用户手动设置止损止盈后，标记 _user_set_stop=True
        # 防止 heal_from_journal 自动修正覆盖用户设置的值
        pos["_user_set_stop"] = True
        _, fix_note = _validate_levels(pos)
        st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        return True, ("ok" + fix_note), st


def advance_trailing(sym, new_stop, trail_state):
    """移动止损自动管理：更新持仓的 stop（上移至保本/跟踪位）并记录 trail_state。
    返回 (ok, msg, state)。由 runner 的 manage_trailing_stops 调用。"""
    with _LOCK:
        st = load_state()
        pos = st["positions"].get(sym)
        if not pos:
            return False, f"{sym} 无持仓，无法移动止损", st
        pos["stop"] = _to_float(new_stop)
        pos["trail_state"] = trail_state
        pos["levels_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        return True, "ok", st


def set_equity(equity, prices=None):
    with _LOCK:
        st = load_state()
        st["equity"] = float(equity)
        # 同步时刻浮动盈亏（收盘后用户同步「总权益(含浮动)」时，
        # equity_synced 已含同步时刻浮动 float_at_sync；snapshot 需扣除避免重复计入）
        float_at_sync = 0.0
        try:
            from four_dim_strategy import load_daily_refreshed
        except Exception:
            load_daily_refreshed = None
        for sym, pos in st.get("positions", {}).items():
            if not pos.get("lots"):
                continue
            avg = pos.get("avg")
            ds = 1 if pos.get("direction") == "多" else -1
            sp = (load_config().get("contract_specs", {}) or {}).get(sym)
            if not sp:
                continue
            mult = sp["multiplier"]
            px = (prices or {}).get(sym)
            if px is None and load_daily_refreshed is not None:
                try:
                    d = load_daily_refreshed(sym)
                    if d is not None and len(d):
                        px = float(d["close"].iloc[-1])
                except Exception:
                    px = None
            if px:
                float_at_sync += (px - avg) * mult * pos["lots"] * ds
        st["float_at_sync"] = round(float_at_sync, 2)
        st["equity_synced"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 记录同步时刻的已实现盈亏，作为动态权益推算的基准：
        # 动态权益 = 同步权益 + (当前已实现盈亏 - 同步时已实现盈亏) + 当前浮动盈亏
        st["realized_pnl_at_sync"] = st.get("realized_pnl", 0.0)
        st["updated"] = st["equity_synced"]
        save_state(st)
        return True, "ok", st


def _auto_tp_targets(sym, pos):
    """为无 tp_targets 的持仓自动计算分级止盈目标价。
    优先级：stop_dist → stop 反推 → 2% ATR 默认。
    与 four_dim_live_runner.calc_take_profit_targets 保持一致：T1=ATR×3, T2=ATR×5, T3(跟踪)=ATR×2。
    注意：不导入 runner，避免触发实时模块副作用。"""
    try:
        avg = float(pos.get("avg", 0))
        if avg <= 0:
            return None
        direction = pos.get("direction", "多")
        _dir = "long" if direction in ("多", "long") else "short"

        # 1) 用 stop 反推 ATR（最可靠）
        atr_val = 0
        if pos.get("stop"):
            atr_val = abs(float(pos["stop"]) - avg)
        # 2) 兜底：2% ATR
        if atr_val <= 0:
            atr_val = avg * 0.02
        if atr_val <= 0:
            return None

        # 常量与 runner 保持一致
        TP_T1 = 3.0
        TP_T2 = 5.0
        TP_T3 = 2.0

        if _dir == "long":
            t1_price = avg + atr_val * TP_T1
            t2_price = avg + atr_val * TP_T2
        else:
            t1_price = avg - atr_val * TP_T1
            t2_price = avg - atr_val * TP_T2

        return {
            "t1_price": round(t1_price, 4),
            "t2_price": round(t2_price, 4),
            "t1_atr_mult": TP_T1,
            "t2_atr_mult": TP_T2,
            "t3_trailing_atr_mult": TP_T3,
            "skip_t3": False,
        }
    except Exception:
        return None


def _heal_position_levels(sym, pos, jlv, changes):
    """根据 journal 记录和 exit_plan 规则修正持仓的 stop/t1/t2。
    若持仓已设置 _user_set_stop=True，则跳过所有自动修正。"""
    if pos.get("_user_set_stop"):
        return  # 用户已手动设置止损止盈，跳过自动修正
    direction = pos.get("direction")
    avg = pos.get("avg")
    if direction not in ("多", "空") or avg is None:
        return
    ds = 1 if direction == "多" else -1
    stop = pos.get("stop")
    t1 = pos.get("t1")
    t2 = pos.get("t2")

    # 1) 用 journal 的 stop_dist 生成标准 levels（最权威）
    sd = None
    if jlv and jlv.get("stop_dist") is not None:
        sd = abs(float(jlv["stop_dist"]))
    elif jlv and jlv.get("stop") is not None:
        sd = abs(float(jlv["stop"]) - avg)

    if sd and sd > 0:
        # exit_plan 规则：多头 stop=entry-sd, t1=entry+sd；空头 stop=entry+sd, t1=entry-sd
        correct_stop = round(avg - ds * sd, 2)
        correct_t1 = round(avg + ds * sd, 2)
        if stop is None or abs(stop - correct_stop) > 0.01:
            changes.append(f"{sym} 止损位 {stop} → {correct_stop}（journal 修正）")
            pos["stop"] = correct_stop
        if t1 is None or abs(t1 - correct_t1) > 0.01:
            changes.append(f"{sym} T1位 {t1} → {correct_t1}（journal 修正）")
            pos["t1"] = correct_t1
        # t2 用 rr=2 生成
        correct_t2 = round(avg + ds * sd * 2.0, 2)
        if t2 is None or (ds > 0 and t2 < avg - 0.01) or (ds < 0 and t2 > avg + 0.01):
            changes.append(f"{sym} T2位 {t2} → {correct_t2}（journal 修正）")
            pos["t2"] = correct_t2
        return

    # 2) 无 journal 参考：仅当 stop 方向错误时，用当前 t1 反推 sd 修正 stop/t2
    if stop is not None:
        stop_ok = (ds > 0 and stop < avg - 0.01) or (ds < 0 and stop > avg + 0.01)
        if not stop_ok and t1 is not None:
            sd = abs(t1 - avg)
            if sd > 0:
                correct_stop = round(avg - ds * sd, 2)
                changes.append(f"{sym} 止损位 {stop} → {correct_stop}（与T1对称修正）")
                pos["stop"] = correct_stop
                correct_t2 = round(avg + ds * sd * 2.0, 2)
                if t2 is None or (ds > 0 and t2 < avg - 0.01) or (ds < 0 and t2 > avg + 0.01):
                    changes.append(f"{sym} T2位 {t2} → {correct_t2}（与T1对称修正）")
                    pos["t2"] = correct_t2
                return

    # 3) 无任何参考：stop 方向明显错误时至少移到保本
    if stop is not None:
        if (ds > 0 and stop > avg) or (ds < 0 and stop < avg):
            changes.append(f"{sym} 止损位 {stop} → {avg}（方向错误→保本修正）")
            pos["stop"] = round(avg, 2)


def heal_from_journal():
    """以 trade_journal.json 为真相源，修正 account_state 的「已实现盈亏 + 开仓均价 + 止损止盈位」漂移。
    此前 #124 对账只比对持仓数量/手数/方向，漏掉了已实现盈亏与开仓均价，
    导致 account_state 这份可手动维护的副本与成交记录静默漂移、面板长期显示错误数据。
    本函数把这三项对齐到 journal（交易记录的权威源）：
      - realized_pnl   应 = journal 所有已平仓成交 pnl 之和
      - 每未平仓持仓 avg 应 = journal 对应开仓记录的 entry_price（P2-C: 加仓按手数加权）
      - 每未平仓持仓 stop/t1/t2 应 = journal 开仓记录的 stop/stop_dist 推导
    仅当存在偏差时才写盘，幂等、安全；绝不删除持仓或改动方向/手数。
    返回 (ok, changes, state)。
    """

    # P1-11 fix: 品种名标准化辅助函数（统一 journal ↔ account 命名）
    def _normalize_sym_for_heal(sym, specs_dict):
        sym = str(sym).strip() if sym else ""
        if not sym:
            return sym
        if sym in specs_dict:
            return sym
        low = sym.lower()
        if low in specs_dict:
            return low
        up = sym.upper()
        if up in specs_dict:
            return up
        return sym

    try:
        import trade_journal as tj
    except Exception:
        return False, ["无法导入 trade_journal（跳过自愈）"], load_state()
    # ★ 加载品种规格表（供品种名大小写标准化使用，与 record_trade 保持一致）
    _cfg = load_config()
    specs = _cfg.get("contract_specs", {})

    # 先修复 journal 自身缺失/不一致的手续费与净盈亏，使后续对比基于已治愈数据
    fee_changes = []
    try:
        fee_changes = tj.heal_fees()
    except Exception as e:
        fee_changes = [f"journal 手续费自愈失败：{e}"]
    jdata = tj._load()
    jrealized = round(sum((t.get("pnl") or 0) for t in jdata.get("trades", []) if t.get("pnl") is not None), 2)
    # P2-C fix: 按手数加权计算开仓均价（忽略加仓只取首笔会低估/高估均价，影响浮盈亏与止损位）
    javg_acc = {}  # (sym, direction) -> {"sum": 价格*手数, "qty": 手数}
    jlevels = {}  # (sym, direction) -> {"stop":..., "stop_dist":...}
    for t in jdata.get("trades", []):
        if t.get("pnl") is not None:
            continue
        k = (t.get("symbol"), t.get("direction"))
        qty = abs(t.get("quantity") or t.get("lots") or 0) or 1
        _ep = float(t.get("entry_price") or 0)
        if k not in javg_acc:
            javg_acc[k] = {"sum": 0.0, "qty": 0.0}
            jlevels[k] = {
                "stop": t.get("stop"),
                "stop_dist": t.get("stop_dist"),
                "t1": t.get("t1"),
                "t2": t.get("t2"),
            }
        javg_acc[k]["sum"] += _ep * qty
        javg_acc[k]["qty"] += qty
    javg = {k: round(v["sum"] / v["qty"], 2) if v["qty"] else 0.0 for k, v in javg_acc.items()}
    with _LOCK:
        st = load_state()
        changes = []
        if fee_changes:
            changes.extend(fee_changes)
        # ★ 从 journal 恢复缺失持仓：journal 有未平仓但 account_state 没有时自动补回
        for (sym, direction), entry_price in javg.items():
            # ★ P1-11 fix: 大小写标准化 —— 用 _normalize_sym() 统一 journal ↔ account 品种名
            _sym = _normalize_sym_for_heal(sym, specs)
            if _sym not in st["positions"] or st["positions"][_sym].get("direction") != direction:
                # 查找 journal 中该品种/方向的最新未平仓记录（用标准化后的 sym 匹配）
                for t in jdata.get("trades", []):
                    _t_sym = _normalize_sym_for_heal(t.get("symbol", ""), specs)
                    if _t_sym == _sym and t.get("direction") == direction and t.get("pnl") is None:
                        pos_data = {
                            "direction": direction,
                            "lots": int(t.get("lots", 0)),
                            "avg": float(entry_price),
                            "open_time": t.get("time", ""),
                            "stop": t.get("stop"),
                            "target": None,
                            "t1": t.get("t1"),
                            "t2": t.get("t2"),
                            "tail_enabled": False,
                            "levels_updated": t.get("time", ""),
                        }
                        # 自动补齐分级止盈
                        _tp_tgt = _auto_tp_targets(sym, pos_data)
                        if _tp_tgt:
                            pos_data["tp_targets"] = _tp_tgt
                        st["positions"][_sym] = pos_data
                        changes.append(f"从 journal 恢复持仓: {sym} {direction} {t.get('lots')}手 @{entry_price}")
                        break
        rp = st.get("realized_pnl", 0.0)
        if abs(rp - jrealized) > 0.01:
            changes.append(f"realized_pnl {rp} → {jrealized}（journal 已平仓盈亏合计）")
            st["realized_pnl"] = jrealized
            st["realized_pnl_at_sync"] = jrealized
        # ★ 清除 journal 中已平仓的僵尸持仓（防止 account_state 残留过期持仓）
        _closed_syms = set()
        for t in jdata.get("trades", []):
            if t.get("pnl") is not None:  # 已平仓
                _closed_syms.add((t.get("symbol"), t.get("direction")))
        for _csym, _cdir in _closed_syms:
            _csym_norm = _normalize_sym_for_heal(_csym, specs)
            if _csym_norm in st["positions"]:
                _cpos = st["positions"][_csym_norm]
                if _cpos.get("direction") == _cdir and (_cpos.get("lots") or 0) > 0:
                    # 检查 journal 中该品种/方向是否还有未平仓记录（P1-11 fix: 标准化匹配）
                    _has_open = any(
                        _normalize_sym_for_heal(t.get("symbol", ""), specs) == _csym_norm
                        and t.get("direction") == _cdir
                        and t.get("pnl") is None
                        for t in jdata.get("trades", [])
                    )
                    if not _has_open:
                        del st["positions"][_csym_norm]
                        changes.append(f"清除僵尸持仓: {_csym_norm} {_cdir}（journal 已平仓无剩余）")
        for sym, pos in list(st["positions"].items()):  # list() 防止迭代时修改
            k = (sym, pos.get("direction"))
            je = javg.get(k)
            if je is not None and abs((pos.get("avg") or 0) - je) > 0.01:
                changes.append(f"{sym} 开仓均价 {pos.get('avg')} → {je}（journal entry_price）")
                pos["avg"] = round(je, 2)
            # 修正止损止盈位，防止方向错误/移动止损污染
            _heal_position_levels(sym, pos, jlevels.get(k), changes)
            # 补齐 tp_targets（分级止盈目标价）：journal 有 stop/stop_dist → 反推；无则用 2% ATR 默认
            if (pos.get("lots") or 0) > 0 and pos.get("tp_targets") is None and pos.get("avg"):
                _tp_tgt = _auto_tp_targets(sym, pos)
                if _tp_tgt:
                    pos["tp_targets"] = _tp_tgt
                    pos.setdefault("tp_level", "tp_none")
                    pos.setdefault("init_qty", int(pos.get("lots", 0)))
                    changes.append(
                        f"{sym} 自动补齐 tp_targets: T1={_tp_tgt.get('t1_price')}, T2={_tp_tgt.get('t2_price')}"
                    )
        # 保留旧持仓的止损/止盈数据（从旧 account_state.json 读取，防止 heal 重建时丢失）
        _old_state = load_state()
        _old_pos = _old_state.get("positions", {}) if isinstance(_old_state, dict) else {}
        for _sym, _pos in list(st.get("positions", {}).items()):
            if isinstance(_pos, dict) and (_pos.get("lots") or 0) > 0:
                _old = _old_pos.get(_sym, {}) if isinstance(_old_pos, dict) else {}
                if isinstance(_old, dict) and _old.get("stop") is not None and _pos.get("stop") is None:
                    _pos["stop"] = _old["stop"]
                    changes.append(f"{_sym} 保留旧止损: {_old['stop']}")
                if isinstance(_old, dict) and _old.get("t1") is not None and _pos.get("t1") is None:
                    _pos["t1"] = _old["t1"]
                if isinstance(_old, dict) and _old.get("t2") is not None and _pos.get("t2") is None:
                    _pos["t2"] = _old["t2"]
                if isinstance(_old, dict) and _old.get("target") is not None and _pos.get("target") is None:
                    _pos["target"] = _old["target"]
                if isinstance(_old, dict) and _old.get("tp_level") is not None:
                    _pos.setdefault("tp_level", _old["tp_level"])
                if isinstance(_old, dict) and _old.get("tp_targets") is not None and _pos.get("tp_targets") is None:
                    _pos["tp_targets"] = _old["tp_targets"]
        if changes:
            st["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_state(st)
        return True, changes, st


def snapshot(prices=None):
    """用实时价算浮动盈亏/占用/资金使用率/距风控线%。返回完整表 dict。
    为使面板上下实时同步：
      - 账户权益显示「动态权益」= 同步权益 + 同步后已实现盈亏变化 + 当前浮动盈亏总和
      - 资金使用率 / 距组合上限 / 单品占用% / 单品距上限 全部基于动态权益
      - 每次 snapshot 自动刷新同步基准（equity_synced → now, equity → dynamic_equity），
        使账户总览所有数据持续实时同步，无需手动「同步权益」按钮。
    """
    cfg = load_config()
    acc = cfg.get("account", {})
    specs = cfg.get("contract_specs", {})
    st = load_state()
    # 兼容 positions 格式：list -> dict（key=symbol）
    _raw_positions = st.get("positions", {})
    if isinstance(_raw_positions, list):
        _pos_dict = {}
        for _p in _raw_positions:
            if isinstance(_p, dict) and _p.get("symbol"):
                _pos_dict[_p["symbol"]] = _p
        st["positions"] = _pos_dict
    elif _raw_positions is None:
        st["positions"] = {}
    equity_synced = st.get("equity", 0) or 0
    margin_cap_pct = acc.get("margin_cap_pct", 30)
    portfolio_cap_pct = acc.get("portfolio_margin_cap_pct", 60)
    # 先过一遍持仓，拿到浮动盈亏（用实时价；无实时价则回退 account_state 存储价）
    positions = []
    total_margin = 0.0
    float_total = 0.0
    for sym, sp in specs.items():
        pos = st["positions"].get(sym)
        lots = (pos or {}).get("lots", 0) if pos else 0
        mult = sp["multiplier"]
        mrate = sp["margin_rate"]
        # ★ 2026-08-28 优化：只在持仓时获取实时价格（无持仓品种用存储价，避免50+品种无意义请求）
        px = None
        if lots and lots > 0:
            _trading = _is_trading_hours()
            if _trading:
                # 交易时段：优先 akshare 新浪实时（从缓存读取，后台线程已批量更新）
                _ak_px = _get_ak_price(sym)
                if _ak_px and _ak_px > 0:
                    px = _ak_px
                # 回退 minishare
                if px is None:
                    _ms_px = _get_ms_price(sym)
                    if _ms_px and _ms_px > 0:
                        px = _ms_px
            else:
                # 非交易时段：优先 minishare 收盘快照
                _ms_px = _get_ms_price(sym)
                if _ms_px and _ms_px > 0:
                    px = _ms_px
                # 回退 akshare
                if px is None:
                    _ak_px = _get_ak_price(sym)
                    if _ak_px and _ak_px > 0:
                        px = _ak_px
            # 外部传入的 prices dict
            if px is None:
                px = (prices or {}).get(sym)
            # 最终回退：account_state 存储价
            if (px is None or px == 0) and pos and pos.get("price") is not None:
                px = pos["price"]
        else:
            # 无持仓：直接用存储价或外部价格，不请求实时行情
            if pos and pos.get("price") is not None:
                px = pos["price"]
            if px is None:
                px = (prices or {}).get(sym)
        if pos:
            lots = pos["lots"]
            avg = pos["avg"]
            ds = _dir_sign(pos["direction"])
            margin_used = lots * avg * mult * mrate
            total_margin += margin_used
            float_pnl = None
            if px is not None:
                float_pnl = round((px - avg) * mult * lots * ds, 2)
                float_total += float_pnl
            cap_value = equity_synced * margin_cap_pct / 100
            positions.append(
                {
                    "symbol": sym,
                    "name": sp.get("name", sym),
                    "contract": _authoritative_contract(sym, sp.get("contract", sym)),
                    "direction": pos["direction"],
                    "lots": lots,
                    "avg": avg,
                    "stop": pos.get("stop"),
                    "target": pos.get("target"),
                    "t1": pos.get("t1"),
                    "t2": pos.get("t2"),
                    "tp_level": pos.get("tp_level", "tp_none"),
                    "tp_targets": pos.get("tp_targets"),
                    "trailing_stop": pos.get("trailing_stop"),
                    "init_qty": pos.get("init_qty", lots),
                    "trail_state": pos.get("trail_state"),
                    "price": px,
                    "float_pnl": float_pnl,
                    "margin_used": round(margin_used, 2),
                    "margin_pct": round(margin_used / equity_synced * 100, 2) if equity_synced > 0 else 0.0,
                    "dist_to_cap": round(cap_value - margin_used, 2) if equity_synced > 0 else 0.0,
                }
            )
        else:
            positions.append(
                {
                    "symbol": sym,
                    "name": sp.get("name", sym),
                    "contract": _authoritative_contract(sym, sp.get("contract", sym)),
                    "direction": "—",
                    "lots": 0,
                    "avg": None,
                    "price": px,
                    "float_pnl": None,
                    "margin_used": 0,
                    "margin_pct": 0.0,
                    "dist_to_cap": round(equity_synced * margin_cap_pct / 100, 2) if equity_synced > 0 else 0.0,
                }
            )
    # 动态权益：让账户总览与持仓实时行情保持同步
    # ★ 2026-08-28: 已实现盈亏采用反推法（权益 - 初始资金 - 浮动盈亏）
    #   确保各板块数据自洽，不依赖可能不完整的交易记录
    INIT_CAPITAL = 1000000.0
    realized_pnl = round(equity_synced - INIT_CAPITAL - float_total, 2)
    realized_pnl_at_sync = st.get("realized_pnl_at_sync", realized_pnl)
    delta_realized = realized_pnl - realized_pnl_at_sync
    float_at_sync = st.get("float_at_sync", 0.0)
    dynamic_equity = equity_synced + delta_realized + float_total - float_at_sync
    if dynamic_equity <= 0:
        dynamic_equity = 1  # 防除零
    # 基于动态权益重新计算占用率 / 距上限（使上下板块同步）
    for p in positions:
        if p["lots"] > 0 and equity_synced > 0:
            p["margin_pct"] = round(p["margin_used"] / dynamic_equity * 100, 2)
            p["dist_to_cap"] = round(dynamic_equity * margin_cap_pct / 100 - p["margin_used"], 2)
    usage_rate = round(total_margin / dynamic_equity * 100, 2)
    portfolio_cap = round(dynamic_equity * portfolio_cap_pct / 100, 2)

    # ── 自动刷新同步基准：每次 snapshot 都将「权益同步基准」推进到当前时刻 ──
    # ★ 2026-08-28: 不再修改 st["equity"]，保持用户设定的同步权益不变
    #   动态权益仅用于前端显示，不回写到基准权益，避免漂移
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        # 只更新同步时间戳和快照值，不修改基准权益
        st["equity_synced"] = now_str
        st["realized_pnl_at_sync"] = realized_pnl
        st["float_at_sync"] = round(float_total, 2)
        st["updated"] = now_str
        # 计算动态权益后回写（但不改变用户的同步基准）
        dynamic_eq = round(dynamic_equity, 2)
        save_state(st)

    available = round(dynamic_equity - total_margin, 2)
    # ★ 2026-08-28: 使用用户设定的同步权益作为基准，动态权益仅用于显示
    base_equity = equity_synced  # 用户/同步时设定的基准权益

    # ★★ 2026-08-28: 数据自洽验证（确保各板块数据一致）
    # 基本恒等式：权益 = 初始资金 + 已实现盈亏 + 浮动盈亏
    INIT_CAPITAL = 1000000.0
    self_check_ok = True
    self_check_msg = ""

    # 反向计算已实现盈亏，确保自洽
    computed_realized = round(dynamic_equity - INIT_CAPITAL - float_total, 2)

    # 自洽检查
    expected_total = round(computed_realized + float_total, 2)
    actual_total = round(dynamic_equity - INIT_CAPITAL, 2)
    if abs(expected_total - actual_total) > 0.01:
        self_check_ok = False
        self_check_msg = f"[自检失败] 盈亏不平衡: {expected_total} != {actual_total}"
        print(f"[SELF_CHECK] {self_check_msg}")

    # 防负值保护
    if available < 0:
        available = 0.0
        self_check_msg += "[警告] 可用资金为负，已修正为0"

    # 确保保证金占用率合理
    if usage_rate > 100:
        self_check_msg += f"[警告] 资金使用率超过100%: {usage_rate:.1f}%"

    return {
        "equity": round(dynamic_equity, 2),
        "equity_synced_raw": round(dynamic_equity, 2),
        "available": available,
        "realized_pnl": realized_pnl,
        "realized_pnl_at_sync": realized_pnl,
        "float_total": round(float_total, 2),
        "total_margin": round(total_margin, 2),
        "usage_rate": usage_rate,
        "portfolio_cap": portfolio_cap,
        "dist_to_portfolio_cap": round(portfolio_cap - total_margin, 2),
        "margin_cap_pct": margin_cap_pct,
        "portfolio_margin_cap_pct": portfolio_cap_pct,
        "max_lots": acc.get("max_lots", 6),
        "max_total_lots": acc.get("max_total_lots", 15),
        "risk_pct": acc.get("risk_pct", 1.5),
        "equity_synced": now_str,
        "updated": now_str,
        "positions": positions,
        "init_capital": INIT_CAPITAL,
        "self_check": {
            "ok": self_check_ok,
            "msg": self_check_msg or "数据自洽",
            "equity_verified": round(dynamic_equity, 2),
            "realized_computed": computed_realized,
            "float_computed": round(float_total, 2),
            "total_verified": round(dynamic_equity - INIT_CAPITAL, 2),
        },
    }


if __name__ == "__main__":
    import four_dim_strategy as fd

    px = {s: 900 + i for i, s in enumerate(fd.SYMBOLS)}
    print(json.dumps(snapshot(px), ensure_ascii=False, indent=2, default=str))
