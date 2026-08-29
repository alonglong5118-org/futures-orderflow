"""
paper_trading_engine.py · 四维策略自动模拟交易引擎
=====================================================
自动监听信号 → 模拟开仓 → 实时跟踪 TP/SL → 自动平仓 → 统计盈亏

核心功能：
  1. 信号监听：检测 four_dim_signals.json 中的新信号
  2. 自动开仓：按信号参数自动模拟开仓
  3. 实时跟踪：每轮检查持仓价格是否触及止损/止盈
  4. 自动平仓：触及 TP/SL 自动平仓，支持分批止盈（t1 平半 + t2 全平）
  5. 移动止损：浮盈达到 t1 后启动移动止损
  6. 风控管理：最大持仓数、同品种不重复开仓、单笔风险限制
  7. 盈亏统计：已实现盈亏、浮动盈亏、权益曲线

数据持久化：paper_trading_state.json
  - positions: 当前持仓列表
  - trades: 历史交易记录（含开平仓信息）
  - equity_curve: 权益曲线（每日快照）
  - stats: 统计指标（胜率、盈亏比、最大回撤等）

使用方式：
  1. 独立运行：python paper_trading_engine.py --daemon
  2. 嵌入调用：from paper_trading_engine import PaperTradingEngine
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "paper_trading_state.json")
SIGNAL_LOG = os.path.join(HERE, "four_dim_signals.json")

# 默认配置
DEFAULT_CONFIG = {
    "enabled": True,  # 是否启用自动模拟交易
    "init_cash": 1_000_000.0,  # 初始资金
    "max_positions": 8,  # 最大同时持仓数
    "max_lots_per_trade": 5,  # 单笔最大手数
    "risk_per_trade_pct": 1.0,  # 单笔风险占资金比例（%）
    "default_lots": 1,  # 默认开仓手数（当信号未指定或为0时）
    "use_signal_lots": True,  # 是否使用信号推荐的手数
    "enable_trailing": True,  # 是否启用移动止损
    "trailing_start_R": 1.0,  # 浮盈达到多少 R 后启动移动止损
    "trailing_lock_R": 0.5,  # 移动止损锁定利润（R）
    "cooldown_minutes": 30,  # 同品种平仓后冷却时间（分钟）
    "slippage_pts": 0,  # 模拟滑点（点数）
}


def sig_hash(signal: dict) -> str:
    """生成信号唯一标识，用于去重。"""
    key = (
        signal.get("symbol"),
        signal.get("time"),
        signal.get("price") or signal.get("entry_ref"),
        signal.get("direction"),
        signal.get("stop"),
        signal.get("target"),
    )
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def direction_en(direction: str) -> str:
    """中文方向转英文。"""
    if direction in ("多", "long", "buy", "多单"):
        return "long"
    if direction in ("空", "short", "sell", "空单"):
        return "short"
    return direction or "long"


def direction_cn(direction: str) -> str:
    """英文方向转中文。"""
    if direction in ("long", "buy", "多"):
        return "多"
    if direction in ("short", "sell", "空"):
        return "空"
    return direction or "多"


class PaperTradingEngine:
    """自动模拟交易引擎。

    用法：
        engine = PaperTradingEngine(feed=my_price_feed)
        engine.start()  # 启动后台线程
        # ... 或手动调用：
        engine.check_new_signals()
        engine.check_positions(current_prices)
    """

    def __init__(self, config: dict | None = None, price_feed=None, contract_specs: dict | None = None):
        """
        Args:
            config: 配置字典，覆盖默认配置
            price_feed: 价格数据源，需实现 .price(symbol) 方法返回最新价
            contract_specs: 合约规格 {symbol: {multiplier, margin_rate, ...}}
        """
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.feed = price_feed
        self.contract_specs = contract_specs or {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signal_hash = ""
        self._processed_signals: set[str] = set()
        self._load_state()

    # ── 状态持久化 ──────────────────────────────────────────────────────

    def _load_state(self):
        """从磁盘加载状态。"""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding="utf-8") as f:
                    state = json.load(f)
                self.cash = float(state.get("cash", self.config["init_cash"]))
                self.realized_pnl = float(state.get("realized_pnl", 0.0))
                self.positions = state.get("positions", {})
                self.trades = state.get("trades", [])
                self.equity_curve = state.get("equity_curve", [])
                self.stats = state.get("stats", {})
                self._processed_signals = set(state.get("processed_signals", []))
                self._cooldowns = state.get("cooldowns", {})
                self.config = {**self.config, **state.get("config", {})}
            else:
                self._init_state()
        except Exception as e:
            print(f"[PaperEngine] 加载状态失败: {e}，使用初始状态")
            self._init_state()

    def _init_state(self):
        """初始化空白状态。"""
        self.cash = self.config["init_cash"]
        self.realized_pnl = 0.0
        self.positions = {}  # symbol -> position dict
        self.trades = []  # 已平仓交易记录
        self.equity_curve = []  # [{date, equity, realized, mtm}]
        self.stats = {}
        self._processed_signals = set()
        self._cooldowns = {}  # symbol -> cooldown_end_timestamp

    def _save_state(self):
        """保存状态到磁盘。"""
        try:
            state = {
                "cash": round(self.cash, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "positions": self.positions,
                "trades": self.trades[-500:],  # 保留最近 500 笔
                "equity_curve": self.equity_curve[-365:],  # 保留最近一年
                "stats": self.stats,
                "processed_signals": list(self._processed_signals),
                "cooldowns": self._cooldowns,
                "config": self.config,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f"[PaperEngine] 保存状态失败: {e}")

    # ── 价格获取 ──────────────────────────────────────────────────────

    def _get_price(self, symbol: str) -> float | None:
        """获取品种最新价。"""
        if self.feed is None:
            return None
        try:
            px = self.feed.price(symbol)
            if px and px > 0:
                return float(px)
        except Exception:
            pass
        return None

    def _get_multiplier(self, symbol: str) -> float:
        """获取合约乘数。"""
        try:
            spec = self.contract_specs.get(symbol, {})
            return float(spec.get("multiplier", 10))
        except Exception:
            return 10.0

    # ── 信号处理 ──────────────────────────────────────────────────────

    def check_new_signals(self) -> list[dict]:
        """检查是否有新信号，有则自动开仓。

        Returns:
            list: 本次新开仓的列表
        """
        new_trades = []
        try:
            if not os.path.exists(SIGNAL_LOG):
                return new_trades
            with open(SIGNAL_LOG, encoding="utf-8") as f:
                signals = json.load(f)
            if not signals:
                return new_trades

            # 只处理最新的（前 10 条内的新信号）
            for sig in signals[:10]:
                sig_id = sig_hash(sig)
                if sig_id in self._processed_signals:
                    continue
                # 只处理建仓信号
                if sig.get("signal_type") not in ("建仓", "翻转", "新仓", "突破"):
                    self._processed_signals.add(sig_id)
                    continue
                # 尝试开仓
                result = self._auto_open(sig, sig_id)
                if result:
                    new_trades.append(result)

        except Exception as e:
            print(f"[PaperEngine] 检查信号失败: {e}")
        return new_trades

    def _auto_open(self, signal: dict, sig_id: str) -> dict | None:
        """根据信号自动开仓。

        Returns:
            开仓成功返回 position dict，失败返回 None
        """
        with self._lock:
            symbol = signal.get("symbol")
            if not symbol:
                return None

            # 检查是否启用
            if not self.config["enabled"]:
                return None

            # 检查是否已有同品种持仓
            if symbol in self.positions:
                self._processed_signals.add(sig_id)
                return None

            # 检查冷却期
            now_ts = time.time()
            if symbol in self._cooldowns:
                if now_ts < self._cooldowns[symbol]:
                    self._processed_signals.add(sig_id)
                    return None
                del self._cooldowns[symbol]

            # 检查最大持仓数
            if len(self.positions) >= self.config["max_positions"]:
                return None  # 先不标记已处理，等有仓位了再处理

            # 获取入场价格
            entry_price = signal.get("price") or signal.get("entry_ref")
            if not entry_price:
                entry_price = self._get_price(symbol)
            if not entry_price or entry_price <= 0:
                return None

            # 模拟滑点
            direction = direction_en(signal.get("direction", "多"))
            slip = self.config.get("slippage_pts", 0)
            if direction == "long":
                fill_price = float(entry_price) + slip
            else:
                fill_price = float(entry_price) - slip

            # 确定手数
            if self.config["use_signal_lots"] and signal.get("lots"):
                lots = int(signal["lots"])
            else:
                lots = self.config["default_lots"]
            lots = min(lots, self.config["max_lots_per_trade"])
            if lots <= 0:
                lots = 1

            # 获取止损止盈
            stop = signal.get("stop")
            target = signal.get("target")
            t1 = signal.get("t1")
            t2 = signal.get("t2")

            # 如果有 exit_plan，优先使用
            ep = signal.get("exit_plan") or {}
            if ep:
                stop = stop or ep.get("stop")
                t1 = t1 or ep.get("t1")
                t2 = t2 or ep.get("t2")
                target = target or t2 or ep.get("target")

            if not stop or not target:
                return None

            stop_dist = abs(fill_price - stop)
            if stop_dist <= 0:
                return None

            # 计算 R 倍数
            target_R = round(abs(target - fill_price) / stop_dist, 2)

            # 构建持仓对象
            position = {
                "id": sig_id,
                "symbol": symbol,
                "name": signal.get("name", symbol),
                "direction": direction_cn(direction),
                "direction_en": direction,
                "lots": lots,
                "entry_price": round(fill_price, 4),
                "stop_price": round(float(stop), 4),
                "target_price": round(float(target), 4),
                "t1_price": round(float(t1), 4) if t1 else None,
                "t2_price": round(float(t2), 4) if t2 else None,
                "stop_dist": round(stop_dist, 4),
                "target_R": target_R,
                "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "open_ts": time.time(),
                "signal_time": signal.get("time"),
                "signal_reason": signal.get("reason", ""),
                "trailing_active": False,
                "trailing_stop": None,
                "peak_price": fill_price,
                "t1_filled": False,  # t1 是否已平半
                "remaining_lots": lots,
                "pipeline": signal.get("pipeline", {}),
                "source": "auto_signal",
            }

            self.positions[symbol] = position
            self._processed_signals.add(sig_id)

            # 记录开仓
            self.trades.append(
                {
                    "type": "open",
                    "time": position["open_time"],
                    "symbol": symbol,
                    "name": position["name"],
                    "direction": position["direction"],
                    "lots": lots,
                    "price": position["entry_price"],
                    "stop": position["stop_price"],
                    "target": position["target_price"],
                    "signal_id": sig_id,
                    "source": "auto_signal",
                }
            )

            self._save_state()
            print(f"[PaperEngine] 自动开仓: {symbol} {position['direction']} {lots}手 @ {fill_price}")
            return position

    # ── 持仓检查 ──────────────────────────────────────────────────────

    def check_positions(self, prices: dict | None = None) -> list[dict]:
        """检查所有持仓是否触发止损/止盈。

        Args:
            prices: 可选的价格字典 {symbol: price}，为 None 时从 feed 获取

        Returns:
            list: 本次平仓的记录
        """
        closed = []
        with self._lock:
            symbols_to_check = list(self.positions.keys())

            for symbol in symbols_to_check:
                pos = self.positions.get(symbol)
                if not pos:
                    continue

                # 获取当前价格
                if prices and symbol in prices:
                    cur_price = float(prices[symbol])
                else:
                    cur_price = self._get_price(symbol)

                if cur_price is None or cur_price <= 0:
                    continue

                result = self._check_position(pos, cur_price)
                if result:
                    closed.append(result)
                    # 如果全部平仓，移除持仓
                    if result.get("fully_closed"):
                        del self.positions[symbol]
                        # 设置冷却期
                        self._cooldowns[symbol] = time.time() + self.config["cooldown_minutes"] * 60

            if closed:
                self._update_stats()
                self._save_state()

        return closed

    def _check_position(self, pos: dict, cur_price: float) -> dict | None:
        """检查单个持仓是否触发条件。

        Returns:
            平仓记录 dict，未触发返回 None
        """
        symbol = pos["symbol"]
        direction = pos["direction_en"]
        remaining_lots = pos["remaining_lots"]
        entry = pos["entry_price"]
        stop = pos["stop_price"]
        target = pos["target_price"]
        stop_dist = pos["stop_dist"]

        # 更新峰值价格（用于移动止损）
        if direction == "long":
            pos["peak_price"] = max(pos["peak_price"], cur_price)
            profit_R = (cur_price - entry) / stop_dist
        else:
            pos["peak_price"] = min(pos["peak_price"], cur_price)
            profit_R = (entry - cur_price) / stop_dist

        # 检查止损
        if direction == "long" and cur_price <= stop:
            return self._close_position(pos, remaining_lots, cur_price, "止损")
        if direction == "short" and cur_price >= stop:
            return self._close_position(pos, remaining_lots, cur_price, "止损")

        # 检查 t1 止盈（平半）
        t1_price = pos.get("t1_price")
        if t1_price and not pos["t1_filled"] and remaining_lots >= 2:
            hit_t1 = (direction == "long" and cur_price >= t1_price) or (direction == "short" and cur_price <= t1_price)
            if hit_t1:
                half_lots = remaining_lots // 2
                result = self._partial_close(pos, half_lots, cur_price, "t1 平半")
                pos["t1_filled"] = True
                # t1 后启动移动止损
                if self.config["enable_trailing"]:
                    pos["trailing_active"] = True
                    pos["trailing_stop"] = self._calc_trailing_stop(pos)
                return result

        # 检查 t2 / 目标止盈（全平）
        hit_target = (direction == "long" and cur_price >= target) or (direction == "short" and cur_price <= target)
        if hit_target:
            return self._close_position(pos, remaining_lots, cur_price, "止盈")

        # 移动止损检查
        if pos.get("trailing_active") and self.config["enable_trailing"]:
            # 更新移动止损线
            new_stop = self._calc_trailing_stop(pos)
            if new_stop:
                current_ts = pos.get("trailing_stop")
                if current_ts is None:
                    # 首次设置移动止损
                    pos["trailing_stop"] = new_stop
                elif direction == "long":
                    # 多头：只上移不下移
                    pos["trailing_stop"] = max(current_ts, new_stop)
                else:
                    # 空头：只下移不上移
                    pos["trailing_stop"] = min(current_ts, new_stop)
                # 检查是否触发移动止损
                if direction == "long" and cur_price <= pos["trailing_stop"]:
                    return self._close_position(pos, remaining_lots, cur_price, "移动止损")
                if direction == "short" and cur_price >= pos["trailing_stop"]:
                    return self._close_position(pos, remaining_lots, cur_price, "移动止损")

        return None

    def _calc_trailing_stop(self, pos: dict) -> float | None:
        """计算移动止损价位。"""
        direction = pos["direction_en"]
        peak = pos["peak_price"]
        stop_dist = pos["stop_dist"]
        lock_R = self.config.get("trailing_lock_R", 0.5)

        if direction == "long":
            return round(peak - stop_dist * lock_R, 4)
        else:
            return round(peak + stop_dist * lock_R, 4)

    # ── 平仓处理 ──────────────────────────────────────────────────────

    def _close_position(self, pos: dict, lots: int, price: float, reason: str) -> dict:
        """全部平仓。"""
        symbol = pos["symbol"]
        mult = self._get_multiplier(symbol)
        direction = pos["direction_en"]
        sign = 1 if direction == "long" else -1
        pnl = (price - pos["entry_price"]) * mult * lots * sign
        pnl_R = (price - pos["entry_price"]) * sign / pos["stop_dist"]

        self.realized_pnl += pnl
        self.cash += pnl

        trade_record = {
            "type": "close",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": pos.get("name", symbol),
            "direction": pos["direction"],
            "lots": lots,
            "entry_price": pos["entry_price"],
            "exit_price": round(price, 4),
            "stop_price": pos["stop_price"],
            "target_price": pos["target_price"],
            "pnl": round(pnl, 2),
            "pnl_R": round(pnl_R, 3),
            "reason": reason,
            "holding_hours": round((time.time() - pos["open_ts"]) / 3600, 2),
            "signal_id": pos.get("id", ""),
            "peak_price": pos.get("peak_price"),
        }
        self.trades.append(trade_record)

        pos["remaining_lots"] = 0
        return {
            "symbol": symbol,
            "fully_closed": True,
            "lots": lots,
            "price": price,
            "reason": reason,
            "pnl": round(pnl, 2),
            "pnl_R": round(pnl_R, 3),
            "record": trade_record,
        }

    def _partial_close(self, pos: dict, lots: int, price: float, reason: str) -> dict:
        """部分平仓。"""
        symbol = pos["symbol"]
        mult = self._get_multiplier(symbol)
        direction = pos["direction_en"]
        sign = 1 if direction == "long" else -1
        pnl = (price - pos["entry_price"]) * mult * lots * sign
        pnl_R = (price - pos["entry_price"]) * sign / pos["stop_dist"]

        self.realized_pnl += pnl
        self.cash += pnl
        pos["remaining_lots"] -= lots

        trade_record = {
            "type": "close",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": pos.get("name", symbol),
            "direction": pos["direction"],
            "lots": lots,
            "entry_price": pos["entry_price"],
            "exit_price": round(price, 4),
            "stop_price": pos["stop_price"],
            "target_price": pos["target_price"],
            "pnl": round(pnl, 2),
            "pnl_R": round(pnl_R, 3),
            "reason": reason,
            "holding_hours": round((time.time() - pos["open_ts"]) / 3600, 2),
            "signal_id": pos.get("id", ""),
            "partial": True,
        }
        self.trades.append(trade_record)

        print(f"[PaperEngine] 部分平仓: {symbol} {lots}手 @ {price} 原因: {reason} PnL: {pnl:+.2f}")
        return {
            "symbol": symbol,
            "fully_closed": False,
            "lots": lots,
            "price": price,
            "reason": reason,
            "pnl": round(pnl, 2),
            "pnl_R": round(pnl_R, 3),
            "remaining_lots": pos["remaining_lots"],
            "record": trade_record,
        }

    # ── 手动操作 ──────────────────────────────────────────────────────

    def manual_open(
        self,
        symbol: str,
        direction: str,
        lots: int,
        price: float,
        stop: float | None = None,
        target: float | None = None,
        strategy: str = "手动",
    ) -> tuple[bool, str, dict | None]:
        """手动开仓。"""
        with self._lock:
            if symbol in self.positions:
                return False, f"{symbol} 已有持仓", None
            if len(self.positions) >= self.config["max_positions"]:
                return False, f"已达最大持仓数 {self.config['max_positions']}", None

            lots = int(lots)
            price = float(price)
            if lots <= 0 or price <= 0:
                return False, "手数/价格无效", None

            dir_en = direction_en(direction)
            stop_dist = abs(price - stop) if stop else price * 0.02
            target_R = round(abs(target - price) / stop_dist, 2) if target else 2.0

            position = {
                "id": f"manual_{int(time.time())}",
                "symbol": symbol,
                "name": symbol,
                "direction": direction_cn(dir_en),
                "direction_en": dir_en,
                "lots": lots,
                "entry_price": round(price, 4),
                "stop_price": round(float(stop), 4) if stop else None,
                "target_price": round(float(target), 4) if target else None,
                "t1_price": None,
                "t2_price": None,
                "stop_dist": round(stop_dist, 4),
                "target_R": target_R,
                "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "open_ts": time.time(),
                "trailing_active": False,
                "trailing_stop": None,
                "peak_price": price,
                "t1_filled": False,
                "remaining_lots": lots,
                "source": "manual",
                "strategy": strategy,
            }
            self.positions[symbol] = position
            self.trades.append(
                {
                    "type": "open",
                    "time": position["open_time"],
                    "symbol": symbol,
                    "direction": position["direction"],
                    "lots": lots,
                    "price": price,
                    "source": "manual",
                    "strategy": strategy,
                }
            )
            self._save_state()
            return True, "开仓成功", position

    def manual_close(
        self, symbol: str, price: float, lots: int | None = None, reason: str = "手动"
    ) -> tuple[bool, str, dict | None]:
        """手动平仓。"""
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return False, f"{symbol} 无持仓", None

            close_lots = int(lots) if lots else pos["remaining_lots"]
            close_lots = min(close_lots, pos["remaining_lots"])

            if close_lots >= pos["remaining_lots"]:
                result = self._close_position(pos, close_lots, float(price), reason)
                del self.positions[symbol]
                self._cooldowns[symbol] = time.time() + self.config["cooldown_minutes"] * 60
            else:
                result = self._partial_close(pos, close_lots, float(price), reason)

            self._update_stats()
            self._save_state()
            return True, "平仓成功", result

    # ── 状态查询 ──────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """获取完整状态（供前端展示）。"""
        with self._lock:
            positions_list = []
            total_mtm = 0.0

            for sym, pos in self.positions.items():
                cur_price = self._get_price(sym) or pos["entry_price"]
                mult = self._get_multiplier(sym)
                sign = 1 if pos["direction_en"] == "long" else -1
                mtm = (cur_price - pos["entry_price"]) * mult * pos["remaining_lots"] * sign
                mtm_R = (cur_price - pos["entry_price"]) * sign / pos["stop_dist"] if pos["stop_dist"] else 0

                total_mtm += mtm
                pos_copy = dict(pos)
                pos_copy["current_price"] = round(cur_price, 4)
                pos_copy["mtm"] = round(mtm, 2)
                pos_copy["mtm_R"] = round(mtm_R, 3)
                positions_list.append(pos_copy)

            equity = round(self.cash + self.realized_pnl + total_mtm, 2)

            return {
                "enabled": self.config["enabled"],
                "cash": round(self.cash, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "mtm": round(total_mtm, 2),
                "equity": equity,
                "init_cash": self.config["init_cash"],
                "total_return_pct": round((equity - self.config["init_cash"]) / self.config["init_cash"] * 100, 2),
                "positions": positions_list,
                "position_count": len(self.positions),
                "recent_trades": self.trades[-50:][::-1],  # 最近 50 笔，倒序
                "total_trades": len([t for t in self.trades if t["type"] == "close"]),
                "stats": self.stats,
                "config": {
                    "max_positions": self.config["max_positions"],
                    "max_lots_per_trade": self.config["max_lots_per_trade"],
                    "default_lots": self.config["default_lots"],
                    "cooldown_minutes": self.config["cooldown_minutes"],
                    "enable_trailing": self.config["enable_trailing"],
                    "trailing_lock_r": self.config["trailing_lock_R"],
                },
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    def _update_stats(self):
        """更新统计指标。"""
        closed_trades = [t for t in self.trades if t["type"] == "close" and not t.get("partial")]
        if not closed_trades:
            self.stats = {}
            return

        wins = [t for t in closed_trades if t["pnl"] > 0]
        losses = [t for t in closed_trades if t["pnl"] <= 0]

        total_pnl = sum(t["pnl"] for t in closed_trades)
        win_rate = len(wins) / len(closed_trades) if closed_trades else 0

        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 0

        avg_holding = sum(t.get("holding_hours", 0) for t in closed_trades) / len(closed_trades)

        # 最大回撤（基于已实现 PnL 序列）
        cumulative = []
        running = 0
        for t in closed_trades:
            running += t["pnl"]
            cumulative.append(running)
        max_dd = 0
        peak = 0
        for val in cumulative:
            peak = max(peak, val)
            dd = peak - val
            max_dd = max(max_dd, dd)

        self.stats = {
            "total_trades": len(closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_holding_hours": round(avg_holding, 2),
            "max_drawdown": round(max_dd, 2),
            "avg_R": round(sum(t.get("pnl_R", 0) for t in closed_trades) / len(closed_trades), 4),
        }

    # ── 后台运行 ──────────────────────────────────────────────────────

    def start(self, interval: float = 10.0):
        """启动后台线程。

        Args:
            interval: 检查间隔（秒）
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, args=(interval,), daemon=True)
        self._thread.start()
        print(f"[PaperEngine] 自动模拟交易引擎已启动（间隔 {interval}s）")

    def stop(self):
        """停止后台线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        print("[PaperEngine] 自动模拟交易引擎已停止")

    def _run_loop(self, interval: float):
        """后台主循环。"""
        while not self._stop_event.is_set():
            try:
                # 检查新信号
                new_trades = self.check_new_signals()
                if new_trades:
                    print(f"[PaperEngine] 新开仓 {len(new_trades)} 笔")

                # 检查持仓
                closed = self.check_positions()
                if closed:
                    for c in closed:
                        print(
                            f"[PaperEngine] 平仓: {c['symbol']} {c['reason']} PnL: {c['pnl']:+.2f} ({c['pnl_R']:+.2f}R)"
                        )

                # 每日快照（简单实现：每小时存一次权益曲线点）
                self._maybe_snapshot()

            except Exception as e:
                print(f"[PaperEngine] 循环异常: {e}")

            self._stop_event.wait(interval)

    def _maybe_snapshot(self):
        """每日权益曲线快照。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.equity_curve and self.equity_curve[-1].get("date") == today:
            return
        state = self.get_state()
        self.equity_curve.append(
            {
                "date": today,
                "equity": state["equity"],
                "realized": state["realized_pnl"],
                "mtm": state["mtm"],
                "position_count": state["position_count"],
            }
        )
        self._save_state()

    # ── 配置管理 ──────────────────────────────────────────────────────

    def update_config(self, **kwargs):
        """更新配置。"""
        with self._lock:
            self.config.update(kwargs)
            self._save_state()

    def toggle_enabled(self, enabled: bool | None = None) -> bool:
        """切换启用状态。"""
        with self._lock:
            if enabled is None:
                self.config["enabled"] = not self.config["enabled"]
            else:
                self.config["enabled"] = enabled
            self._save_state()
            return self.config["enabled"]

    def reset(self):
        """重置模拟账户。"""
        with self._lock:
            self._init_state()
            self._save_state()
            print("[PaperEngine] 模拟账户已重置")


# ── 单例 ────────────────────────────────────────────────────────────

_engine_instance: PaperTradingEngine | None = None


def get_engine(price_feed=None, contract_specs=None) -> PaperTradingEngine:
    """获取全局单例引擎。"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = PaperTradingEngine(price_feed=price_feed, contract_specs=contract_specs)
    return _engine_instance


# ── 命令行运行 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="四维策略自动模拟交易引擎")
    parser.add_argument("--daemon", action="store_true", help="后台运行模式")
    parser.add_argument("--interval", type=float, default=10.0, help="检查间隔（秒）")
    parser.add_argument("--status", action="store_true", help="查看当前状态")
    parser.add_argument("--reset", action="store_true", help="重置模拟账户")
    args = parser.parse_args()

    engine = get_engine()

    if args.status:
        state = engine.get_state()
        print("=== 模拟交易账户状态 ===")
        print(f"权益: {state['equity']:,.2f}")
        print(f"已实现盈亏: {state['realized_pnl']:+,.2f}")
        print(f"浮动盈亏: {state['mtm']:+,.2f}")
        print(f"持仓数: {state['position_count']}")
        print(f"总交易数: {state['total_trades']}")
        if state["stats"]:
            s = state["stats"]
            print(f"胜率: {s['win_rate']:.1%}")
            print(f"盈亏比: {s['profit_factor']:.2f}")
        print("\n当前持仓:")
        for p in state["positions"]:
            print(
                f"  {p['symbol']:>5} {p['direction']} {p['remaining_lots']}手 "
                f"@ {p['entry_price']}  浮盈: {p['mtm']:+,.2f} ({p['mtm_R']:+.2f}R)"
            )
    elif args.reset:
        engine.reset()
        print("账户已重置")
    elif args.daemon:
        engine.start(interval=args.interval)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            engine.stop()
    else:
        # 默认：单次检查
        new_trades = engine.check_new_signals()
        closed = engine.check_positions()
        state = engine.get_state()
        print(f"新信号开仓: {len(new_trades)} 笔")
        print(f"触发平仓: {len(closed)} 笔")
        print(f"当前权益: {state['equity']:,.2f}")
        print(f"持仓数: {state['position_count']}")
