"""
trisense_replay_integration.py · 三感参谋实盘跟踪账户
=====================================================
第二个 PaperTradingEngine 实例，用于跟踪真实实盘持仓。
- 不跟随策略信号自动开仓（只手动管理）
- 启用 TP/SL 检查 + 移动止损（盈亏和风控实时计算）
- 独立状态文件：trisense_replay_state.json
- 独立 API 路由：/api/trisense-replay
- 独立仪表盘：/trisense_dashboard/

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

STATE_FILE = os.path.join(HERE, "trisense_replay_state.json")

_engine: PaperTradingEngine | None = None
_initialized = False


def init(price_feed=None, contract_specs: dict | None = None, config: dict | None = None):
    """初始化三感参谋实盘跟踪引擎。

    默认配置：
    - enabled: False（不跟随策略信号自动开仓，只做手动持仓跟踪）
    - max_positions: 20（足够容纳实盘所有持仓）
    - enable_trailing: True（移动止损照常工作）
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
        "trailing_lock_R": 0.3,
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
    )

    _initialized = True
    print(f"[TriSense] 三感参谋实盘跟踪账户已初始化（自动开仓: {_engine.config['enabled']}）")
    return _engine


def get_engine() -> PaperTradingEngine | None:
    """获取引擎实例。"""
    return _engine


def tick(state: dict | None = None) -> dict:
    """每轮调用：只检查持仓 TP/SL + 移动止损，不跟随信号。

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

    # 注入到 state
    if state is not None:
        try:
            state["trisense_replay"] = get_state()
        except Exception:
            pass

    return result


def get_state() -> dict:
    """获取三感参谋实盘跟踪状态（供前端展示）。"""
    if not _engine:
        return {"enabled": False, "error": "未初始化", "account": "trisense_replay"}
    try:
        s = _engine.get_state()
        s["account"] = "trisense_replay"
        s["account_name"] = "三感参谋实盘账户"
        return s
    except Exception as e:
        return {"enabled": False, "error": str(e), "account": "trisense_replay"}


def handle_api(handler) -> None:
    """处理 /api/trisense-replay API 请求。

    支持的 action:
      - GET: 获取状态
      - POST {action: "toggle"}: 切换启用状态（是否跟信号，默认关）
      - POST {action: "open", symbol, direction, lots, price, stop, target}: 手动开仓
      - POST {action: "close", symbol, price, lots}: 手动平仓
      - POST {action: "reset", init_cash}: 重置账户（可指定初始资金）
      - POST {action: "config", ...}: 更新配置
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
                ok, msg, pos = _engine.manual_open(
                    symbol=sym_open,
                    direction=dir_open,
                    lots=int(body_json.get("lots", 1)),
                    price=float(body_json.get("price", 0)),
                    stop=body_json.get("stop"),
                    target=body_json.get("target"),
                    strategy=body_json.get("strategy", "实盘导入"),
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
