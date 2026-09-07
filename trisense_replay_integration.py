"""
trisense_replay_integration.py · 三感参谋实盘跟踪账户
=====================================================
第二个 PaperTradingEngine 实例，用于跟踪真实实盘持仓。
- 不跟随策略信号自动开仓（只手动管理）
- 启用 TP/SL 检查 + 移动止损（盈亏和风控实时计算）
- 独立状态文件：trisense_replay_state.json
- 独立 API 路由：/api/trisense-replay
- 独立仪表盘：/trisense_dashboard/
- **独立风控状态**（2026-09-07）：账户级风控（状态机+回撤水位线+硬熔断）与主账户完全隔离

命名说明：
- 系统对外定名「三感参谋」（TriSense Advisor，决策 #40）
- 原四维策略模型是内部开发代号，代码底层变量名保留 four_dim 以减少改动
- 本模块是实盘跟踪账户的专用集成层

用法（在 four_dim_live_runner.py 中）：
  import trisense_replay_integration as tri

  # 初始化（main 函数开头，pti.init 之后）
  tri.init(feed, contract_specs=_TCFG.get("contract_specs", {}))

  # 每轮循环调用（只检查持仓 TP/SL，不跟信号）
  tri.tick(state)

  # /api/state 中注入
  state["trisense_replay"] = tri.get_state()

  # /api/trisense-replay 端点处理
  if path == "/api/trisense-replay":
      tri.handle_api(self)
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paper_trading_engine import PaperTradingEngine
import risk_state_machine as _rsm
import drawdown_guard as _ddg

STATE_FILE = os.path.join(HERE, "trisense_replay_state.json")
TRI_ACCOUNT_ID = "trisense_replay"  # 三感参谋独立账户 ID（风控/回撤状态按此隔离）

_engine: PaperTradingEngine | None = None
_initialized = False


def init(price_feed=None, contract_specs: dict | None = None, config: dict | None = None, stop_vol_fn=None):
    """初始化三感参谋实盘跟踪引擎。

    默认配置：
    - enabled: False（不跟随策略信号自动开仓，只做手动持仓跟踪）
    - max_positions: 20（足够容纳实盘所有持仓）
    - enable_trailing: True（移动止损照常工作）

    Args:
        stop_vol_fn: 可选 fn(symbol) -> float，返回日线波动量（ATR/DR），
                     用于自动计算默认止损距离；为 None 时用价格 2% 兜底
    """
    global _engine, _initialized
    if _initialized:
        return _engine

    default_cfg = {
        "enabled": False,  # 关键：禁用自动跟信号，只做手动持仓跟踪
        "max_positions": 20,
        "max_lots_per_trade": 50,
        "risk_per_trade_pct": 1.0,
        "default_lots": 1,
        "use_signal_lots": False,
        "enable_trailing": True,
        "trailing_start_R": 0.5,
        "trailing_lock_R": 0.3,  # T1 前：0.3R 锁定（紧）
        "trailing_tail_lock_R": 1.2,  # T1 后尾仓：1.2R 锁定（宽，抓趋势）
        "default_rr": 2.0,
        "enable_time_exit": True,
        "time_exit_hours": 120,  # 5 天，给趋势足够时间
        "time_exit_min_profit_R": 1.5,  # 至少 1.5R 才触发时间止盈
        "cooldown_minutes": 0,
        "slippage_pts": 0,
    }
    if config:
        default_cfg.update(config)

    _engine = PaperTradingEngine(
        config=default_cfg,
        price_feed=price_feed,
        contract_specs=contract_specs,
        state_file=STATE_FILE,
        stop_vol_fn=stop_vol_fn,
    )

    # 初始化独立风控：把当前权益作为峰值基准
    try:
        _st = _engine.get_state()
        eq = float(_st.get("equity", _engine.config["init_cash"]))
        _ddg.update(eq, account_id=TRI_ACCOUNT_ID)
        _dd_st = _ddg.current(account_id=TRI_ACCOUNT_ID)
        if _dd_st.get("peak_equity"):
            _rsm.get_fsm(TRI_ACCOUNT_ID).peak_equity = float(_dd_st["peak_equity"])
        print(
            f"[TriSense] 独立风控已就绪（账户: {TRI_ACCOUNT_ID}）· "
            f"权益={eq:,.0f} · 峰值={_dd_st.get('peak_equity', 0):,.0f}"
        )
    except Exception as e:
        print(f"[TriSense] 独立风控初始化异常: {e}")

    _initialized = True
    print(f"[TriSense] 三感参谋实盘跟踪账户已初始化（自动开仓: {_engine.config['enabled']}）")
    return _engine


def get_engine() -> PaperTradingEngine | None:
    """获取引擎实例。"""
    return _engine


def _calc_consec_losses() -> int:
    """从最近成交记录统计连续止损笔数（供风控状态机使用）。"""
    if not _engine:
        return 0
    try:
        trades = _engine.get_state().get("closed_trades", [])
        if not trades:
            return 0
        count = 0
        for t in reversed(trades[-20:]):  # 只看最近 20 笔
            pnl = float(t.get("pnl", 0))
            if pnl < 0:
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def tick(state: dict | None = None) -> dict:
    """每轮调用：检查持仓 TP/SL + 移动止损 + 更新独立风控状态。

    Args:
        state: live runner 的 state 字典，用于注入 trisense_replay 状态

    Returns:
        dict: {new_trades: [], closed: []}
    """
    if not _engine:
        return {"new_trades": [], "closed": []}

    result = {"new_trades": [], "closed": []}

    try:
        # 实盘跟踪盘：只检查持仓（自动平仓/移动止损），不检查新信号
        if _engine.positions:
            closed = _engine.check_positions()
            result["closed"] = closed
    except Exception as e:
        print(f"[TriSense] 检查持仓异常: {e}")

    # 更新独立风控状态（每轮同步权益/回撤/连亏）
    try:
        _st = _engine.get_state()
        eq = float(_st.get("equity", 0))
        daily_pnl = float(_st.get("daily_pnl", 0))
        used = float(_st.get("used_margin", 0))
        consec = _calc_consec_losses()
        positions = _st.get("positions", [])

        # 回撤水位线更新
        _dd_state = _ddg.update(eq, account_id=TRI_ACCOUNT_ID)
        _dd_peak = _dd_state.get("peak_equity")

        # 风控状态机 + 硬熔断更新
        _rsm.update_risk_state(
            eq, used, daily_pnl, consec,
            positions=positions, peak_equity=_dd_peak,
            account_id=TRI_ACCOUNT_ID,
        )
    except Exception as e:
        print(f"[TriSense] 独立风控更新异常: {e}")

    # 注入到 state
    if state is not None:
        try:
            state["trisense_replay"] = get_state()
        except Exception:
            pass

    return result


def get_risk_state() -> dict:
    """获取三感参谋独立风控状态（总览，含 fsm/killswitch/drawdown/combined）。"""
    try:
        fsm = _rsm.get_fsm(TRI_ACCOUNT_ID).summary()
        kill = _rsm.get_kill(TRI_ACCOUNT_ID).summary()
        dd = _ddg.current(account_id=TRI_ACCOUNT_ID)
        combined = _rsm.get_combined_risk_scale(TRI_ACCOUNT_ID)
        return {
            "fsm": fsm,
            "killswitch": kill,
            "drawdown": dd,
            "combined": combined,
            "account_id": TRI_ACCOUNT_ID,
        }
    except Exception as e:
        return {"error": str(e), "account_id": TRI_ACCOUNT_ID}


def get_risk_fsm() -> dict:
    """获取三感参谋风控状态机快照（与 /api/risk 结构一致）。"""
    try:
        return _rsm.get_fsm(TRI_ACCOUNT_ID).summary()
    except Exception as e:
        return {"state": "NORMAL", "error": str(e)}


def get_risk_killswitch() -> dict:
    """获取三感参谋硬熔断状态（与 /api/killswitch GET 结构一致）。"""
    try:
        return _rsm.get_kill(TRI_ACCOUNT_ID).summary()
    except Exception as e:
        return {"halted": False, "error": str(e)}


def get_risk_drawdown() -> dict:
    """获取三感参谋回撤水位线状态（与 /api/drawdown 结构一致）。"""
    try:
        d = _ddg.current(account_id=TRI_ACCOUNT_ID)
        d["halted"] = _rsm.is_halted(TRI_ACCOUNT_ID)
        return d
    except Exception as e:
        return {"error": str(e)}


def risk_kill_ack() -> dict:
    """确认三感参谋硬熔断告警（已手动全平，解除 halt 但保留锁定计数）。"""
    try:
        return _rsm.get_kill(TRI_ACCOUNT_ID).acknowledge()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def risk_kill_reset(peak_equity=None) -> dict:
    """人工解除三感参谋硬熔断，可指定新的峰值权益。"""
    try:
        result = _rsm.get_kill(TRI_ACCOUNT_ID).reset(
            "面板人工解除", reset_peak_to=peak_equity
        )
        # 同步重置回撤水位线峰值
        try:
            _ddg.reset_peak(peak_equity, account_id=TRI_ACCOUNT_ID)
        except Exception:
            pass
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def risk_reset_peak(peak_equity=None) -> dict:
    """重置三感参谋回撤峰值权益（解除降险档位）。"""
    try:
        _ddg.reset_peak(peak_equity, account_id=TRI_ACCOUNT_ID)
        if peak_equity is not None:
            _rsm.get_fsm(TRI_ACCOUNT_ID).peak_equity = float(peak_equity)
        return {"ok": True, "msg": "峰值已重置"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_state() -> dict:
    """获取三感参谋实盘跟踪状态（供前端展示）。"""
    if not _engine:
        return {"enabled": False, "error": "未初始化", "account": TRI_ACCOUNT_ID}
    try:
        s = _engine.get_state()
        s["account"] = TRI_ACCOUNT_ID
        s["account_name"] = "三感参谋实盘账户"
        # 注入独立风控状态
        s["risk"] = get_risk_state()
        return s
    except Exception as e:
        return {"enabled": False, "error": str(e), "account": TRI_ACCOUNT_ID}


def handle_api(handler) -> None:
    """处理 /api/trisense-replay API 请求。

    支持的 action:
      - GET: 获取状态
      - POST {action: "toggle"}: 切换启用状态（是否跟信号，默认关）
      - POST {action: "open", symbol, direction, lots, price, stop, target}: 手动开仓
      - POST {action: "close", symbol, price, lots}: 手动平仓
      - POST {action: "reset", init_cash}: 重置账户（可指定初始资金，同步重置风控）
      - POST {action: "config", ...}: 更新配置
      - POST {action: "risk_reset_peak", peak}: 重置风控峰值权益
      - POST {action: "risk_kill_ack"}: 确认熔断告警
      - POST {action: "risk_kill_reset", peak}: 解除硬熔断
    """
    if not _engine:
        body = json.dumps({"ok": False, "error": "引擎未初始化"}, ensure_ascii=False)
        _send_json(handler, body)
        return

    try:
        if handler.command == "GET":
            body = json.dumps(get_state(), ensure_ascii=False, default=str)
            _send_json(handler, body)
            return

        # POST
        length = int(handler.headers.get("Content-Length", 0) or 0)
        raw = handler.rfile.read(length).decode("utf-8", "ignore") if length else "{}"
        body_json = json.loads(raw) if raw.strip() else {}
        action = body_json.get("action", "")

        if action == "toggle":
            enabled = body_json.get("enabled")
            if enabled is None:
                result = _engine.toggle_enabled()
            else:
                result = _engine.toggle_enabled(bool(enabled))
            resp = {"ok": True, "enabled": result, "state": get_state()}

        elif action == "open":
            # 独立风控闸门：锁定/熔断时禁止开仓
            lock_info = _rsm.get_combined_risk_scale(TRI_ACCOUNT_ID)
            if lock_info.get("locked") or lock_info.get("halted"):
                reason = "；".join(lock_info.get("reasons", ["风控锁定"]))
                resp = {
                    "ok": False,
                    "msg": f"开仓被风控拦截: {reason}",
                    "risk": get_risk_state(),
                }
            else:
                # C 感知方向门检查（与主账户一致）
                sym_open = body_json.get("symbol", "")
                dir_open = body_json.get("direction", "多")
                _dir_val = 1 if dir_open == "多" else (-1 if dir_open == "空" else 0)
                _cg_blocked = False
                _cg_reason = ""
                try:
                    import four_dim_strategy as _fd_cg_tri
                    from four_dim_strategy import DEFAULT_CONFIG as _TRI_CFG
                    _cg_res = _fd_cg_tri.check_c_gate(sym_open, _dir_val, _TRI_CFG)
                    if not _cg_res["passed"]:
                        _cg_blocked = True
                        _cg_reason = _cg_res["reason"]
                except Exception:
                    pass  # 异常放行
                if _cg_blocked:
                    resp = {
                        "ok": False,
                        "msg": f"开仓被C感知门拦截: {_cg_reason}",
                        "c_gate": {"passed": False, "reason": _cg_reason},
                        "state": get_state(),
                    }
                else:
                    # 风控缩放：手数按 combined 系数调整（向下取整，至少 1 手）
                    lots = int(body_json.get("lots", 1))
                    scale = lock_info.get("combined", 1.0)
                    if scale < 1.0:
                        scaled_lots = max(1, int(lots * scale))
                        lots = scaled_lots

                    ok, msg, pos = _engine.manual_open(
                        symbol=sym_open,
                        direction=dir_open,
                        lots=lots,
                        price=float(body_json.get("price", 0)),
                        stop=body_json.get("stop"),
                        target=body_json.get("target"),
                        strategy=body_json.get("strategy", "手动开仓"),
                    )
                    resp = {"ok": ok, "msg": msg, "position": pos, "state": get_state()}

        elif action == "close":
            ok, msg, result = _engine.manual_close(
                symbol=body_json.get("symbol", ""),
                price=float(body_json.get("price", 0)),
                lots=body_json.get("lots"),
                reason=body_json.get("reason", "手动"),
            )
            resp = {"ok": ok, "msg": msg, "result": result, "state": get_state()}

        elif action == "reset":
            init_cash = body_json.get("init_cash")
            if init_cash is not None:
                _engine.config["init_cash"] = float(init_cash)
                _engine.cash = float(init_cash)
            _engine.reset()
            # 同步重置独立风控（峰值 + 回撤 + 连亏 + 熔断）
            try:
                _rsm.get_fsm(TRI_ACCOUNT_ID).reset_daily()
                _rsm.get_kill(TRI_ACCOUNT_ID).reset("账户重置")
                _ddg.reset_peak(_engine.config["init_cash"], account_id=TRI_ACCOUNT_ID)
            except Exception:
                pass
            resp = {"ok": True, "msg": "账户已重置", "state": get_state()}

        elif action == "config":
            config_keys = [
                "enabled",
                "max_positions",
                "max_lots_per_trade",
                "risk_per_trade_pct",
                "default_lots",
                "use_signal_lots",
                "enable_trailing",
                "trailing_start_R",
                "trailing_lock_R",
                "cooldown_minutes",
                "slippage_pts",
            ]
            source = body_json.get("config", body_json)
            updates = {k: source[k] for k in config_keys if k in source}
            _engine.update_config(**updates)
            resp = {"ok": True, "config": _engine.config, "state": get_state()}

        # ===== 独立风控专用接口 =====
        elif action == "risk_reset_peak":
            peak = body_json.get("peak")
            try:
                peak_val = float(peak) if peak else None
            except (TypeError, ValueError):
                peak_val = None
            _ddg.reset_peak(peak_val, account_id=TRI_ACCOUNT_ID)
            if peak_val:
                _rsm.get_fsm(TRI_ACCOUNT_ID).peak_equity = peak_val
            resp = {"ok": True, "msg": "风控峰值已重置", "risk": get_risk_state()}

        elif action == "risk_kill_ack":
            result = _rsm.get_kill(TRI_ACCOUNT_ID).acknowledge()
            resp = {"ok": True, "result": result, "risk": get_risk_state()}

        elif action == "risk_kill_reset":
            peak = body_json.get("peak")
            try:
                peak_val = float(peak) if peak else None
            except (TypeError, ValueError):
                peak_val = None
            result = _rsm.get_kill(TRI_ACCOUNT_ID).reset(
                "面板人工解除", reset_peak_to=peak_val
            )
            # 同步重置回撤水位线峰值
            try:
                _ddg.reset_peak(peak_val, account_id=TRI_ACCOUNT_ID)
            except Exception:
                pass
            resp = {"ok": True, "result": result, "risk": get_risk_state()}

        else:
            resp = {"ok": False, "msg": f"未知 action: {action}"}

        body = json.dumps(resp, ensure_ascii=False, default=str)
        _send_json(handler, body)

    except Exception as e:
        import traceback
        traceback.print_exc()
        body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        _send_json(handler, body)


def _send_json(handler, body: str):
    """发送 JSON 响应。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body.encode("utf-8"))


def handle_options(handler) -> None:
    """处理 OPTIONS 请求（CORS 预检）。"""
    handler.send_response(200)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
