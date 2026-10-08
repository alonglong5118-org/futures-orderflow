#!/usr/bin/env python3
"""模拟交易仪表盘验证服务器。
提供 /paper_dashboard/ 静态文件 + /api/paper-trading 模拟 API，
用于独立验证仪表盘前端是否正常工作。
"""

import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(HERE, "paper_dashboard")
PORT = 8742

# 模拟状态数据
_state = {
    "enabled": True,
    "config": {
        "max_positions": 8,
        "max_lots_per_trade": 5,
        "default_lots": 1,
        "cooldown_minutes": 30,
        "enable_trailing": True,
        "trailing_lock_r": 0.5,
        "t1_r": 1.0,
        "t2_r": 2.0,
    },
    "positions": {
        "IF2409": {
            "symbol": "IF2409",
            "direction": "long",
            "lots": 2,
            "entry_price": 3950.0,
            "current_price": 3985.6,
            "stop_loss": 3920.0,
            "take_profit": 4010.0,
            "unrealized_pnl": 21300,
            "r_pnl": 1.18,
            "opened_at": time.time() - 3600,
            "trailing_active": False,
        },
        "IC2409": {
            "symbol": "IC2409",
            "direction": "short",
            "lots": 1,
            "entry_price": 5620.0,
            "current_price": 5598.4,
            "stop_loss": 5660.0,
            "take_profit": 5560.0,
            "unrealized_pnl": 4320,
            "r_pnl": 0.54,
            "opened_at": time.time() - 7200,
            "trailing_active": False,
        },
        "RB2410": {
            "symbol": "RB2410",
            "direction": "long",
            "lots": 3,
            "entry_price": 3480.0,
            "current_price": 3462.0,
            "stop_loss": 3450.0,
            "take_profit": 3540.0,
            "unrealized_pnl": -5400,
            "r_pnl": -0.3,
            "opened_at": time.time() - 1800,
            "trailing_active": False,
        },
    },
    "stats": {
        "equity": 1038750.0,
        "initial_equity": 1000000.0,
        "total_pnl": 38750.0,
        "total_trades": 27,
        "wins": 16,
        "losses": 11,
        "win_rate": 16 / 27,
        "profit_factor": 1.85,
        "max_drawdown": 28500.0,
        "expR": 0.42,
        "avg_hold_time": 2450,
        "max_win_streak": 4,
        "max_loss_streak": 3,
        "avg_win": 5200.0,
        "avg_loss": -2800.0,
        "sharpe": 1.24,
        "calmar": 2.15,
    },
    "equity_curve": [],
    "trade_history": [],
}


# 生成权益曲线
def _gen_equity_curve():
    now = time.time()
    equity = 1000000.0
    curve = []
    for i in range(60, 0, -1):
        equity += (random.random() - 0.42) * 8000
        equity = max(950000, min(1100000, equity))
        curve.append([now - i * 3600 * 4, round(equity, 2)])
    curve.append([now, 1038750.0])
    return curve


_state["equity_curve"] = _gen_equity_curve()


# 生成交易历史
def _gen_trade_history():
    now = time.time()
    trades = []
    symbols = ["IF2409", "IC2409", "IH2409", "M2409", "CU2409", "AU2410", "AG2409", "MA2409", "RB2410"]
    reasons_win = ["止盈T1", "止盈T2", "移动止损", "手动平仓"]
    reasons_loss = ["止损", "移动止损", "手动平仓"]

    for i in range(15):
        ts = now - i * 7200 - random.randint(0, 1800)
        sym = random.choice(symbols)
        direction = random.choice(["long", "short"])
        is_win = random.random() < 0.6
        entry = round(random.uniform(3000, 8000), 2)
        r = random.uniform(0.8, 2.5)
        sl_dist = entry * 0.008

        # 开仓记录
        trades.append(
            {
                "timestamp": ts + 3600,
                "symbol": sym,
                "direction": direction,
                "type": "open",
                "lots": random.randint(1, 3),
                "entry_price": entry,
                "exit_price": None,
                "pnl": None,
                "r_pnl": None,
                "reason": "信号建仓",
            }
        )

        # 平仓记录
        exit_price = entry + sl_dist * r if is_win else entry - sl_dist
        if direction == "short":
            exit_price = entry - sl_dist * r if is_win else entry + sl_dist
        pnl = (exit_price - entry) * 300 * 2 if direction == "long" else (entry - exit_price) * 300 * 2
        if not is_win:
            pnl = -abs(pnl)

        reason = random.choice(reasons_win) if is_win else random.choice(reasons_loss)
        trades.append(
            {
                "timestamp": ts,
                "symbol": sym,
                "direction": direction,
                "type": "close",
                "lots": random.randint(1, 3),
                "entry_price": entry,
                "exit_price": round(exit_price, 2),
                "pnl": round(pnl, 2),
                "r_pnl": round(r if is_win else -1.0, 2),
                "reason": reason,
            }
        )

    trades.sort(key=lambda x: x["timestamp"], reverse=True)
    return trades


_state["trade_history"] = _gen_trade_history()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path):
        safe_path = path.replace("/paper_dashboard/", "", 1).lstrip("/").replace("..", "")
        if not safe_path:
            safe_path = "index.html"
        file_path = os.path.normpath(os.path.join(DASH_DIR, safe_path))

        if not file_path.startswith(DASH_DIR):
            self.send_response(403)
            self.end_headers()
            return

        if not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        ext = os.path.splitext(file_path)[1].lower()
        ct = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".ttf": "font/ttf",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")

        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            self.wfile.write(f.read())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._send_static("/paper_dashboard/index.html")
        elif path.startswith("/paper_dashboard/"):
            self._send_static(path)
        elif path == "/api/paper-trading":
            # 模拟价格波动
            for sym, pos in _state["positions"].items():
                drift = (random.random() - 0.5) * pos["entry_price"] * 0.002
                pos["current_price"] = round(pos["current_price"] + drift, 2)
                # 更新浮盈
                mult = 300 if "IF" in sym or "IC" in sym or "IH" in sym else 10
                if pos["direction"] == "long":
                    pos["unrealized_pnl"] = round((pos["current_price"] - pos["entry_price"]) * pos["lots"] * mult, 2)
                else:
                    pos["unrealized_pnl"] = round((pos["entry_price"] - pos["current_price"]) * pos["lots"] * mult, 2)
                sl_dist = abs(pos["entry_price"] - pos["stop_loss"])
                if sl_dist > 0:
                    pos["r_pnl"] = round(pos["unrealized_pnl"] / (sl_dist * pos["lots"] * mult), 2)
            self._send_json(_state)
        elif path == "/api/state":
            self._send_json(
                {
                    "paper_trading": _state,
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "session": "验证模式",
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/paper-trading":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            action = body.get("action", "")

            if action == "toggle":
                _state["enabled"] = not _state["enabled"]
                self._send_json({"success": True, "enabled": _state["enabled"]})
            elif action == "config":
                if "config" in body:
                    _state["config"].update(body["config"])
                self._send_json({"success": True, "config": _state["config"]})
            elif action == "reset":
                _state["positions"] = {}
                _state["stats"] = {
                    "equity": 1000000.0,
                    "initial_equity": 1000000.0,
                    "total_pnl": 0,
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0,
                    "profit_factor": 0,
                    "max_drawdown": 0,
                    "expR": 0,
                    "avg_hold_time": 0,
                    "max_win_streak": 0,
                    "max_loss_streak": 0,
                    "avg_win": 0,
                    "avg_loss": 0,
                    "sharpe": 0,
                    "calmar": 0,
                }
                _state["equity_curve"] = [[time.time(), 1000000.0]]
                _state["trade_history"] = []
                self._send_json({"success": True})
            elif action == "open":
                sym = body.get("symbol", "TEST")
                _state["positions"][sym] = {
                    "symbol": sym,
                    "direction": body.get("direction", "long"),
                    "lots": body.get("lots", 1),
                    "entry_price": body.get("price", 3000.0),
                    "current_price": body.get("price", 3000.0),
                    "stop_loss": body.get("price", 3000.0) * 0.98,
                    "take_profit": body.get("price", 3000.0) * 1.04,
                    "unrealized_pnl": 0,
                    "r_pnl": 0,
                    "opened_at": time.time(),
                }
                self._send_json({"success": True})
            elif action == "close":
                sym = body.get("symbol", "")
                if sym in _state["positions"]:
                    del _state["positions"][sym]
                self._send_json({"success": True})
            else:
                self._send_json({"success": False, "error": f"未知 action: {action}"})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {format % args}")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 50)
    print("  模拟交易仪表盘验证服务器")
    print(f"  端口: {PORT}")
    print(f"  仪表盘: http://localhost:{PORT}/paper_dashboard/")
    print(f"  API:    http://localhost:{PORT}/api/paper-trading")
    print("=" * 50)
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
