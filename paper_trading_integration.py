"""
paper_trading_integration.py · 模拟交易引擎与 live runner 集成层
=================================================================
在不侵入 four_dim_live_runner.py 主代码的前提下，提供：
  1. 引擎初始化
  2. 每轮 tick 调用（检查新信号 + 检查持仓 TP/SL）
  3. API 端点处理
  4. State 注入（供 /api/state 使用）

用法（在 four_dim_live_runner.py 中）：
  import paper_trading_integration as pti

  # 初始化（main 函数开头）
  pti.init(feed, contract_specs=_TCFG.get("contract_specs", {}))

  # 每轮循环调用（_update_aux 末尾或主循环中）
  pti.tick(state)

  # /api/state 中注入
  state["paper_trading"] = pti.get_state()

  # /api/paper-trading 端点处理
  if path == "/api/paper-trading":
      pti.handle_api(self)
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paper_trading_engine import PaperTradingEngine

_engine: PaperTradingEngine | None = None
_initialized = False


def init(price_feed=None, contract_specs: dict | None = None, config: dict | None = None):
    """初始化模拟交易引擎。

    Args:
        price_feed: 实时行情数据源（需实现 .price(symbol) 方法）
        contract_specs: 合约规格字典 {symbol: {multiplier, margin_rate, ...}}
        config: 额外配置覆盖
    """
    global _engine, _initialized
    if _initialized:
        return _engine

    _engine = PaperTradingEngine(
        config=config,
        price_feed=price_feed,
        contract_specs=contract_specs,
    )
    _initialized = True
    print(f"[PaperTrading] 模拟交易引擎已初始化（启用: {_engine.config['enabled']}）")
    return _engine


def get_engine() -> PaperTradingEngine | None:
    """获取引擎实例。"""
    return _engine


def tick(state: dict | None = None) -> dict:
    """每轮调用：检查新信号 + 检查持仓 TP/SL。

    Args:
        state: live runner 的 state 字典，用于注入 paper_trading 状态

    Returns:
        dict: {new_trades: [], closed: []}
    """
    if not _engine:
        return {"new_trades": [], "closed": []}

    result = {"new_trades": [], "closed": []}

    try:
        # 1. 检查新信号（自动开仓）
        if _engine.config["enabled"]:
            new_trades = _engine.check_new_signals()
            result["new_trades"] = new_trades
    except Exception as e:
        print(f"[PaperTrading] 检查新信号异常: {e}")

    try:
        # 2. 检查持仓（自动平仓）
        if _engine.config["enabled"] and _engine.positions:
            closed = _engine.check_positions()
            result["closed"] = closed
    except Exception as e:
        print(f"[PaperTrading] 检查持仓异常: {e}")

    # 3. 注入到 state
    if state is not None:
        try:
            state["paper_trading"] = get_state()
        except Exception:
            pass

    return result


def get_state() -> dict:
    """获取模拟交易状态（供前端展示）。"""
    if not _engine:
        return {"enabled": False, "error": "未初始化"}
    try:
        return _engine.get_state()
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def handle_api(handler) -> None:
    """处理 /api/paper-trading API 请求。

    支持的 action:
      - GET: 获取状态
      - POST {action: "toggle"}: 切换启用状态
      - POST {action: "open", symbol, direction, lots, price}: 手动开仓
      - POST {action: "close", symbol, price, lots}: 手动平仓
      - POST {action: "reset"}: 重置账户
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
            ok, msg, pos = _engine.manual_open(
                symbol=body_json.get("symbol", ""),
                direction=body_json.get("direction", "多"),
                lots=int(body_json.get("lots", 1)),
                price=float(body_json.get("price", 0)),
                stop=body_json.get("stop"),
                target=body_json.get("target"),
                strategy=body_json.get("strategy", "手动"),
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
            _engine.reset()
            resp = {"ok": True, "msg": "账户已重置", "state": get_state()}

        elif action == "config":
            # 更新配置
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
            updates = {k: body_json[k] for k in config_keys if k in body_json}
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
