"""
实盘 PnL 数据源适配器 (Phase 7 实盘接入)

功能：
- 支持多种 PnL 数据源：trade_journal.json、模拟盘引擎、CSV 文件、自定义接口
- 统一转换为漂移检测 / 监控看板所需的 symbol_metrics 格式
- 自动加载基线版本参数与 OOS 指标
- 支持按日期范围、账户、策略标签筛选

数据源类型：
  1. trade_journal - 从 trade_journal.json 读取手动/半自动交易记录
  2. paper_trading - 从 paper_trading_engine 读取模拟盘成交
  3. csv           - 从 CSV 文件导入（兼容通用交易导出格式）
  4. custom        - 自定义数据源（通过回调函数注入）

输出格式（与 drift_detector / dashboard 一致）：
    {
        "zn": {
            "symbol": "zn",
            "baseline_expR": 0.72,
            "baseline_trades": 28,
            "baseline_win_rate": 0.45,
            "recent_expR": 0.65,
            "recent_trades": 8,
            "recent_win_rate": 0.42,
            "baseline_daily_expR": [...],
            "recent_daily_expR": [...],
            "current_params": {"stop_atr_mult": 0.7, "rr_ratio": 2.0},
            "last_update": "2026-08-30",
        },
        ...
    }

用法：
    from monitor.pnl_data_source import PnLDataSource
    src = PnLDataSource.from_config("monitor/live_monitor_config.json")
    metrics = src.get_symbol_metrics()
"""

import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from monitor.param_versions import ParamVersionManager


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class TradeRecord:
    """标准化的单笔交易记录（已平仓）"""
    symbol: str
    entry_time: str          # "YYYY-MM-DD HH:MM:SS"
    exit_time: str           # "YYYY-MM-DD HH:MM:SS"
    direction: str           # "long" / "short"
    lots: int
    entry_price: float
    exit_price: float
    pnl: float               # 盈亏金额（含手续费）
    fee: float               # 手续费总额
    r_multiple: float = 0.0  # R 倍数（= pnl / 风险预算）
    strategy: str = ""       # 策略标签
    account: str = "主账户"  # 账户标签
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始数据


@dataclass
class SymbolDailyStats:
    """品种每日统计"""
    date: str
    expR: float = 0.0        # 当日 R 倍数净值（sum of R-multiples）
    trades: int = 0
    wins: int = 0


# ============================================================================
# 基类
# ============================================================================

class BasePnLSource:
    """PnL 数据源基类"""

    def name(self) -> str:
        return "base"

    def fetch_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> List[TradeRecord]:
        """获取指定条件的已平仓交易记录"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """检查数据源是否可用"""
        return True


# ============================================================================
# 数据源 1: trade_journal.json
# ============================================================================

class TradeJournalSource(BasePnLSource):
    """
    从 trade_journal.json 读取交易记录。

    适用于：手动下单 + trade_journal 记账的场景。
    """

    def __init__(self, journal_path: str, risk_per_trade_pct: float = 0.5, base_equity: float = 100000.0):
        """
        Args:
            journal_path: trade_journal.json 文件路径
            risk_per_trade_pct: 每笔风险占资金比例（%），用于计算 R 倍数
            base_equity: 基准资金，用于推算 R 倍数
        """
        self.journal_path = journal_path
        self.risk_per_trade_pct = risk_per_trade_pct
        self.base_equity = base_equity
        self._risk_per_trade = base_equity * (risk_per_trade_pct / 100.0)

    def name(self) -> str:
        return f"trade_journal:{os.path.basename(self.journal_path)}"

    def is_available(self) -> bool:
        return os.path.exists(self.journal_path)

    def fetch_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> List[TradeRecord]:
        if not self.is_available():
            return []

        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []

        trades_raw = data.get("trades", [])
        records = []

        for t in trades_raw:
            # 只取已平仓
            if t.get("pnl") is None or t.get("exit_time") is None:
                continue

            sym = t.get("symbol", "")
            exit_time = t.get("exit_time", "")
            account_t = t.get("account", "主账户")
            strategy_t = t.get("strategy", "")

            # 日期筛选
            if start_date and exit_time < start_date:
                continue
            if end_date and exit_time > end_date + " 23:59:59":
                continue

            # 账户 / 策略筛选
            if account and account_t != account:
                continue
            if strategy and strategy_t != strategy:
                continue

            pnl = float(t.get("pnl", 0))
            fee = float(t.get("fee_total", 0) or 0)

            # 计算 R 倍数
            stop_dist = t.get("stop_dist")
            if stop_dist and stop_dist > 0:
                # 用实际止损距离计算 R
                from trade_journal import _MULTIPLIERS
                mult = _MULTIPLIERS.get(sym, 1)
                risk = float(stop_dist) * mult * t.get("lots", 1)
                r_mult = pnl / risk if risk > 0 else 0.0
            else:
                # 用固定风险预算推算
                r_mult = pnl / self._risk_per_trade if self._risk_per_trade > 0 else 0.0

            records.append(TradeRecord(
                symbol=sym,
                entry_time=t.get("time", ""),
                exit_time=exit_time,
                direction=t.get("direction", ""),
                lots=int(t.get("lots", 1)),
                entry_price=float(t.get("entry_price", 0)),
                exit_price=float(t.get("exit_price", 0)),
                pnl=pnl,
                fee=fee,
                r_multiple=r_mult,
                strategy=strategy_t,
                account=account_t,
                raw=t,
            ))

        return records


# ============================================================================
# 数据源 2: 模拟盘引擎 (paper_trading_engine)
# ============================================================================

class PaperTradingSource(BasePnLSource):
    """
    从 paper_trading_engine 读取模拟盘成交记录。

    适用于：天勤 TqSdk 模拟盘自动交易场景。
    """

    def __init__(
        self,
        engine_module: str = "paper_trading_engine",
        risk_per_trade_pct: float = 0.5,
        base_equity: float = 100000.0,
    ):
        self.engine_module = engine_module
        self.risk_per_trade_pct = risk_per_trade_pct
        self.base_equity = base_equity
        self._risk_per_trade = base_equity * (risk_per_trade_pct / 100.0)
        self._engine = None

    def name(self) -> str:
        return f"paper_trading:{self.engine_module}"

    def is_available(self) -> bool:
        try:
            self._get_engine()
            return True
        except Exception:
            return False

    def _get_engine(self):
        if self._engine is None:
            import importlib
            mod = importlib.import_module(self.engine_module)
            self._engine = mod
        return self._engine

    def fetch_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> List[TradeRecord]:
        try:
            engine = self._get_engine()
        except Exception:
            return []

        # 尝试从引擎获取成交历史
        trades_raw = []
        if hasattr(engine, "get_closed_trades"):
            trades_raw = engine.get_closed_trades()
        elif hasattr(engine, "closed_trades"):
            trades_raw = engine.closed_trades
        else:
            # 退而求其次：从 trade_journal 读（模拟盘也可能写入 journal）
            return []

        records = []
        for t in trades_raw:
            sym = t.get("symbol", "")
            exit_time = t.get("exit_time", t.get("close_time", ""))

            if start_date and exit_time < start_date:
                continue
            if end_date and exit_time > end_date + " 23:59:59":
                continue

            pnl = float(t.get("pnl", t.get("profit", 0)))
            fee = float(t.get("fee", t.get("commission", 0) or 0))

            r_mult = pnl / self._risk_per_trade if self._risk_per_trade > 0 else 0.0

            records.append(TradeRecord(
                symbol=sym,
                entry_time=t.get("entry_time", t.get("open_time", "")),
                exit_time=exit_time,
                direction=t.get("direction", ""),
                lots=int(t.get("lots", t.get("volume", 1))),
                entry_price=float(t.get("entry_price", t.get("open_price", 0))),
                exit_price=float(t.get("exit_price", t.get("close_price", 0))),
                pnl=pnl,
                fee=fee,
                r_multiple=r_mult,
                strategy=t.get("strategy", ""),
                account=t.get("account", "模拟盘"),
                raw=t,
            ))

        return records


# ============================================================================
# 数据源 3: CSV 文件
# ============================================================================

class CsvPnLSource(BasePnLSource):
    """
    从 CSV 文件读取交易记录。

    CSV 格式（表头必须包含）：
        symbol, entry_time, exit_time, direction, lots, entry_price, exit_price, pnl, fee, r_multiple, strategy, account

    最少只需：symbol, exit_time, pnl 三列
    """

    def __init__(
        self,
        csv_path: str,
        risk_per_trade_pct: float = 0.5,
        base_equity: float = 100000.0,
    ):
        self.csv_path = csv_path
        self.risk_per_trade_pct = risk_per_trade_pct
        self.base_equity = base_equity
        self._risk_per_trade = base_equity * (risk_per_trade_pct / 100.0)

    def name(self) -> str:
        return f"csv:{os.path.basename(self.csv_path)}"

    def is_available(self) -> bool:
        return os.path.exists(self.csv_path)

    def fetch_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> List[TradeRecord]:
        if not self.is_available():
            return []

        records = []
        try:
            with open(self.csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get("symbol", row.get("品种", "")).strip().lower()
                    exit_time = row.get("exit_time", row.get("平仓时间", "")).strip()

                    if start_date and exit_time < start_date:
                        continue
                    if end_date and exit_time > end_date + " 23:59:59":
                        continue

                    acc = row.get("account", row.get("账户", "主账户")).strip()
                    strat = row.get("strategy", row.get("策略", "")).strip()

                    if account and acc != account:
                        continue
                    if strategy and strat != strategy:
                        continue

                    pnl = float(row.get("pnl", row.get("盈亏", 0)) or 0)
                    fee = float(row.get("fee", row.get("手续费", 0)) or 0)

                    r_str = row.get("r_multiple", row.get("r倍数", ""))
                    if r_str:
                        r_mult = float(r_str)
                    else:
                        r_mult = pnl / self._risk_per_trade if self._risk_per_trade > 0 else 0.0

                    records.append(TradeRecord(
                        symbol=sym,
                        entry_time=row.get("entry_time", row.get("开仓时间", "")).strip(),
                        exit_time=exit_time,
                        direction=row.get("direction", row.get("方向", "")).strip().lower(),
                        lots=int(row.get("lots", row.get("手数", 1)) or 1),
                        entry_price=float(row.get("entry_price", row.get("开仓价", 0)) or 0),
                        exit_price=float(row.get("exit_price", row.get("平仓价", 0)) or 0),
                        pnl=pnl,
                        fee=fee,
                        r_multiple=r_mult,
                        strategy=strat,
                        account=acc,
                        raw=dict(row),
                    ))
        except Exception:
            pass

        return records


# ============================================================================
# 数据源 4: 自定义回调
# ============================================================================

class CustomPnLSource(BasePnLSource):
    """
    自定义数据源，通过回调函数获取交易记录。

    适用于：接入券商 API、数据库、或其他内部系统。
    """

    def __init__(
        self,
        fetch_callback: Callable[..., List[TradeRecord]],
        name: str = "custom",
    ):
        self._fetch = fetch_callback
        self._name = name

    def name(self) -> str:
        return f"custom:{self._name}"

    def fetch_trades(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> List[TradeRecord]:
        return self._fetch(
            start_date=start_date,
            end_date=end_date,
            account=account,
            strategy=strategy,
        )


# ============================================================================
# 主适配器：PnLDataSource
# ============================================================================

class PnLDataSource:
    """
    实盘 PnL 数据源主适配器。

    负责：
    1. 从配置的数据源拉取交易记录
    2. 从参数版本库加载基线参数与验证指标
    3. 聚合计算各品种的 symbol_metrics（供 drift_detector / dashboard 使用）
    """

    def __init__(
        self,
        sources: List[BasePnLSource],
        version_manager: Optional[ParamVersionManager] = None,
        versions_dir: Optional[str] = None,
        baseline_version: Optional[str] = None,
        oos_result_pattern: Optional[str] = None,
        recent_window_days: int = 60,
    ):
        """
        Args:
            sources: PnL 数据源列表（可多个，结果自动合并去重）
            version_manager: 参数版本管理器实例
            versions_dir: 参数版本目录（如果不传 version_manager 则用此路径创建）
            baseline_version: 基线版本 ID，None 表示使用当前版本
            oos_result_pattern: OOS 结果文件路径模式，如 "ga_v5_{symbol}_result/{symbol}_phase35_oos_result.json"
            recent_window_days: 近期表现统计窗口（天）
        """
        self.sources = sources
        self.recent_window_days = recent_window_days
        self.baseline_version = baseline_version
        self.oos_result_pattern = oos_result_pattern

        if version_manager:
            self.vm = version_manager
        elif versions_dir:
            self.vm = ParamVersionManager(versions_dir)
        else:
            self.vm = None

        # 缓存
        self._trades_cache: Optional[List[TradeRecord]] = None
        self._cache_date: Optional[str] = None

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_path: str) -> "PnLDataSource":
        """
        从 JSON 配置文件创建数据源。

        配置格式：
        {
            "sources": [
                {"type": "trade_journal", "path": "trade_journal.json", "risk_pct": 0.5},
                {"type": "csv", "path": "trades.csv", "base_equity": 100000},
                {"type": "paper_trading", "module": "paper_trading_engine"}
            ],
            "versions_dir": "monitor/versions",
            "baseline_version": "v001",
            "oos_result_pattern": "ga_v5_{symbol}_result/{symbol}_phase35_oos_result.json",
            "recent_window_days": 60
        }
        """
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        sources: List[BasePnLSource] = []
        base_dir = os.path.dirname(os.path.abspath(config_path))

        for src_cfg in cfg.get("sources", []):
            stype = src_cfg.get("type", "")
            risk_pct = src_cfg.get("risk_pct", 0.5)
            base_equity = src_cfg.get("base_equity", 100000.0)

            if stype == "trade_journal":
                path = src_cfg["path"]
                if not os.path.isabs(path):
                    path = os.path.join(base_dir, path)
                sources.append(TradeJournalSource(path, risk_pct, base_equity))

            elif stype == "paper_trading":
                sources.append(PaperTradingSource(
                    engine_module=src_cfg.get("module", "paper_trading_engine"),
                    risk_per_trade_pct=risk_pct,
                    base_equity=base_equity,
                ))

            elif stype == "csv":
                path = src_cfg["path"]
                if not os.path.isabs(path):
                    path = os.path.join(base_dir, path)
                sources.append(CsvPnLSource(path, risk_pct, base_equity))

            else:
                print(f"[PnLDataSource] 未知数据源类型: {stype}，已跳过")

        versions_dir = cfg.get("versions_dir")
        if versions_dir and not os.path.isabs(versions_dir):
            versions_dir = os.path.join(base_dir, versions_dir)

        oos_pattern = cfg.get("oos_result_pattern")
        if oos_pattern and not os.path.isabs(oos_pattern):
            # 相对于配置文件所在目录
            oos_pattern = os.path.join(base_dir, oos_pattern)

        return cls(
            sources=sources,
            versions_dir=versions_dir,
            baseline_version=cfg.get("baseline_version"),
            oos_result_pattern=oos_pattern,
            recent_window_days=cfg.get("recent_window_days", 60),
        )

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------

    def get_all_trades(self, force_refresh: bool = False) -> List[TradeRecord]:
        """获取所有可用数据源的交易记录（合并去重）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not force_refresh and self._trades_cache and self._cache_date == today:
            return self._trades_cache

        all_trades: List[TradeRecord] = []
        seen_keys = set()

        for src in self.sources:
            if not src.is_available():
                print(f"[PnLDataSource] 数据源 {src.name()} 不可用，已跳过")
                continue
            try:
                trades = src.fetch_trades()
                for t in trades:
                    # 去重键：symbol + exit_time + pnl
                    key = (t.symbol, t.exit_time, round(t.pnl, 2))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_trades.append(t)
            except Exception as e:
                print(f"[PnLDataSource] 数据源 {src.name()} 读取失败: {e}")

        # 按退出时间排序
        all_trades.sort(key=lambda t: t.exit_time)
        self._trades_cache = all_trades
        self._cache_date = today
        return all_trades

    def get_trades_in_window(self, days: int) -> List[TradeRecord]:
        """获取最近 N 天的交易记录"""
        all_trades = self.get_all_trades()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return [t for t in all_trades if t.exit_time >= cutoff]

    # ------------------------------------------------------------------
    # 基线数据加载
    # ------------------------------------------------------------------

    def _load_baseline_params(self) -> Dict[str, Dict[str, float]]:
        """加载基线版本的参数"""
        if not self.vm:
            return {}
        try:
            if self.baseline_version:
                return self.vm.load_version(self.baseline_version)
            else:
                return self.vm.load_current()
        except Exception:
            return {}

    def _load_baseline_metrics(self, symbol: str) -> Dict[str, Any]:
        """
        加载某品种的基线 OOS 指标。

        优先从 OOS 结果文件加载，其次从参数版本的 validation_summary 推算。
        """
        # 方法 1: 从 OOS 结果文件加载（最准确）
        if self.oos_result_pattern:
            path = self.oos_result_pattern.format(symbol=symbol)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    oos_val = data.get("oos_validation", {})
                    # 取最佳候选（expR 最高的）
                    best = None
                    best_expR = -999
                    for key in ["balanced", "stable", "aggressive"]:
                        if key in oos_val:
                            expR = oos_val[key]["oos"].get("expR", -999)
                            if expR > best_expR:
                                best_expR = expR
                                best = oos_val[key]["oos"]
                    if best:
                        return {
                            "baseline_expR": best.get("expR", 0),
                            "baseline_trades": best.get("trades", 0),
                            "baseline_win_rate": best.get("win_rate", 0),
                        }
                except Exception:
                    pass

        # 方法 2: 从版本验证摘要推算（粗略）
        if self.vm and self.baseline_version:
            try:
                info = self.vm.get_version_info(self.baseline_version)
                vs = info.get("validation_summary", {})
                if "avg_oos_delta" in vs:
                    # 粗略估计：用平均 delta 作为所有品种的基线
                    return {
                        "baseline_expR": vs["avg_oos_delta"],
                        "baseline_trades": 20,
                        "baseline_win_rate": 0.45,
                    }
            except Exception:
                pass

        return {
            "baseline_expR": 0.0,
            "baseline_trades": 0,
            "baseline_win_rate": 0.0,
        }

    # ------------------------------------------------------------------
    # 聚合计算
    # ------------------------------------------------------------------

    def _compute_daily_expR(self, trades: List[TradeRecord]) -> List[float]:
        """计算每日 R 倍数净值序列"""
        daily: Dict[str, float] = defaultdict(float)
        for t in trades:
            date = t.exit_time[:10] if t.exit_time else "unknown"
            daily[date] += t.r_multiple

        # 按日期排序后返回值序列
        sorted_dates = sorted(daily.keys())
        return [round(daily[d], 4) for d in sorted_dates]

    def _compute_symbol_metrics(
        self,
        symbol: str,
        recent_trades: List[TradeRecord],
    ) -> Dict[str, Any]:
        """计算单品种的完整指标（基线 + 近期）"""
        # 近期统计
        n_recent = len(recent_trades)
        if n_recent > 0:
            recent_expR = sum(t.r_multiple for t in recent_trades) / n_recent
            recent_wins = sum(1 for t in recent_trades if t.r_multiple > 0)
            recent_win_rate = recent_wins / n_recent
            recent_daily = self._compute_daily_expR(recent_trades)
        else:
            recent_expR = 0.0
            recent_win_rate = 0.0
            recent_daily = []

        # 基线
        baseline = self._load_baseline_metrics(symbol)
        baseline_params = self._load_baseline_params()

        return {
            "symbol": symbol,
            "baseline_expR": round(baseline.get("baseline_expR", 0), 4),
            "baseline_trades": int(baseline.get("baseline_trades", 0)),
            "baseline_win_rate": round(baseline.get("baseline_win_rate", 0), 4),
            "recent_expR": round(recent_expR, 4),
            "recent_trades": n_recent,
            "recent_win_rate": round(recent_win_rate, 4),
            "recent_daily_expR": recent_daily,
            "baseline_daily_expR": [],  # 基线日序列暂不填充（OOS 不一定有日数据）
            "current_params": baseline_params.get(symbol, {}),
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": ",".join(s.name() for s in self.sources if s.is_available()),
        }

    def get_symbol_metrics(
        self,
        symbols: Optional[List[str]] = None,
        recent_window_days: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        获取各品种的完整指标字典（漂移检测 / 看板的标准输入格式）。

        Args:
            symbols: 指定品种列表，None 表示用所有有交易的品种 + 基线参数中的品种
            recent_window_days: 近期窗口天数，None 表示用配置值

        Returns:
            {symbol: metrics_dict}
        """
        window = recent_window_days or self.recent_window_days
        recent_trades = self.get_trades_in_window(window)

        # 按品种分组
        by_symbol: Dict[str, List[TradeRecord]] = defaultdict(list)
        for t in recent_trades:
            by_symbol[t.symbol].append(t)

        # 确定要展示的品种列表
        if symbols is None:
            # 基线参数中的品种 + 实际有交易的品种
            baseline_params = self._load_baseline_params()
            symbols = sorted(set(baseline_params.keys()) | set(by_symbol.keys()))

        result = {}
        for sym in symbols:
            result[sym] = self._compute_symbol_metrics(sym, by_symbol.get(sym, []))

        return result

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """获取数据源总体摘要"""
        all_trades = self.get_all_trades()
        symbols = set(t.symbol for t in all_trades)
        total_pnl = sum(t.pnl for t in all_trades)
        total_r = sum(t.r_multiple for t in all_trades)

        return {
            "sources": [s.name() for s in self.sources],
            "available_sources": [s.name() for s in self.sources if s.is_available()],
            "total_trades": len(all_trades),
            "total_symbols": len(symbols),
            "symbols": sorted(symbols),
            "total_pnl": round(total_pnl, 2),
            "total_r_multiple": round(total_r, 4),
            "recent_window_days": self.recent_window_days,
            "baseline_version": self.baseline_version or "current",
        }


# ============================================================================
# 命令行入口
# ============================================================================

def _main():
    import argparse

    parser = argparse.ArgumentParser(description="实盘 PnL 数据源适配器")
    parser.add_argument("--config", default="monitor/live_monitor_config.json", help="配置文件路径")
    parser.add_argument("--summary", action="store_true", help="显示数据源摘要")
    parser.add_argument("--metrics", action="store_true", help="显示各品种指标")
    parser.add_argument("--trades", action="store_true", help="列出所有交易")
    parser.add_argument("--days", type=int, default=60, help="近期窗口天数")
    parser.add_argument("--symbol", type=str, default=None, help="指定品种")

    args = parser.parse_args()

    src = PnLDataSource.from_config(args.config)

    if args.summary:
        s = src.summary()
        print(json.dumps(s, ensure_ascii=False, indent=2))

    elif args.trades:
        trades = src.get_all_trades()
        if args.symbol:
            trades = [t for t in trades if t.symbol == args.symbol]
        print(f"共 {len(trades)} 笔交易")
        for t in trades[-20:]:
            print(f"  {t.exit_time}  {t.symbol:6s} {t.direction:5s} {t.lots}手 "
                  f"R={t.r_multiple:+.3f}  PnL={t.pnl:+,.0f}")

    elif args.metrics:
        symbols = [args.symbol] if args.symbol else None
        metrics = src.get_symbol_metrics(symbols=symbols, recent_window_days=args.days)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))

    else:
        # 默认：显示摘要
        s = src.summary()
        print(f"数据源: {', '.join(s['available_sources'])}")
        print(f"总交易笔数: {s['total_trades']}")
        print(f"品种数: {s['total_symbols']} ({', '.join(s['symbols'])})")
        print(f"总盈亏: {s['total_pnl']:+,.0f} 元")
        print(f"总R倍数: {s['total_r_multiple']:+.3f}")
        print(f"近期窗口: {s['recent_window_days']} 天")
        print(f"基线版本: {s['baseline_version']}")


if __name__ == "__main__":
    _main()
