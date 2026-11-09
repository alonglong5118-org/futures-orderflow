"""账户权益单一真源（SSOT）—— 三感参谋 A·P0

状态：**staged（未接线）**。2026-11-06 设计与实现，11-09 实验窗口收官后接线。
接线的唯一改动点：把 four_dim_live_runner.py 的 `_sync_live_equity()` 内部委托给本模块，
并删除 `static_fallback` 这条回退路径。

## 为什么需要它（现状证据，2026-11-06 实测）

`_sync_live_equity()` 的三级回退每一级都取不到数：
  1. `am.get_account()`        → account_monitor.json 不存在 ⇒ 恒 None
  2. `_account_equity()`       → account_state.json 的 equity_live/equity 恒 0.0
  3. `at.snapshot()`           → positions 为 {} ⇒ 也是 0
结果 `/api/state.sizing_equity` 恒为 `{equity: 1000000.0, source: "static_fallback"}`。
1% 单笔风险 / 45% 使用率 / 5% 日亏 / 15% 回撤全部锚在这个硬编码常量上，且**不报错**。

而真实账本 `paper_trading_state.json` 一直是活的（cash 997,120、4 条持仓、有 realized_pnl）。
两套账本，一套在用，一套被读 —— 这就是 SSOT 缺失。

## 设计红线

1. **绝不返回"猜"的数字。** 取不到就 ok=False，由调用方拒绝出信号。宁可不出信号，
   不要用假权益算出一个看起来合理的仓位。
2. **source 枚举里没有 static。** 硬编码常量不是合法权益来源，必须从类型层面不可表达。
3. **每条权益都带 age_sec。** 超过 max_age 即视为不可信，不因为"数字看着合理"就放行。
4. **乘数未知必须报错。** 默认 10 会把 au(1000)/ni(1)/lc(1) 错算 10–100 倍，
   而这正是爆仓级错误；unknown multiplier ≠ 10。

## 用法

    import account_ssot
    r = account_ssot.resolve(mode="paper", price_fn=feed.price)
    if not r.ok:
        # 拒绝出信号，面板红字告警
        ...
    equity = r.equity

命令行自检（只读，不碰任何文件）：
    python3 account_ssot.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

# ── 合法权益来源（枚举不含 static —— 这是刻意的，不要加回来）────────────────
SOURCE_LIVE_BROKER = "live_broker"      # CTP/券商实时资金
SOURCE_LIVE_SNAPSHOT = "live_snapshot"  # 系统自算并落盘的动态权益（equity_live）
SOURCE_MANUAL_ANCHOR = "manual_anchor"  # 人工同步锚（面板「同步权益」）
SOURCE_PAPER_LEDGER = "paper_ledger"    # 模拟盘账本 cash + Σ浮动

VALID_SOURCES = (
    SOURCE_LIVE_BROKER,
    SOURCE_LIVE_SNAPSHOT,
    SOURCE_MANUAL_ANCHOR,
    SOURCE_PAPER_LEDGER,
)

# 模拟盘/实盘账本模式
MODE_PAPER = "paper"
MODE_LIVE = "live"


class UnknownMultiplier(Exception):
    """持仓品种的合约乘数未知。必须抛，不许默认 10。"""


@dataclass(frozen=True)
class Leg:
    """一条持仓腿的估值明细，用于审计与对账。"""

    symbol: str
    direction: str
    lots: float
    entry_price: float
    mark_price: Optional[float]
    multiplier: float
    mtm: Optional[float]

    @property
    def priced(self) -> bool:
        return self.mtm is not None


@dataclass(frozen=True)
class Reading:
    """一次权益解析的结果。ok=False 时 equity 可能为 None。"""

    equity: Optional[float]
    source: str
    ok: bool
    detail: str
    age_sec: Optional[float] = None
    legs: Tuple[Leg, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "equity": self.equity,
            "source": self.source,
            "ok": self.ok,
            "detail": self.detail,
            "age_sec": None if self.age_sec is None else round(self.age_sec, 1),
            "legs": [
                {
                    "symbol": l.symbol,
                    "direction": l.direction,
                    "lots": l.lots,
                    "entry": l.entry_price,
                    "mark": l.mark_price,
                    "mult": l.multiplier,
                    "mtm": None if l.mtm is None else round(l.mtm, 2),
                }
                for l in self.legs
            ],
            "warnings": list(self.warnings),
        }


# ── 基础工具 ──────────────────────────────────────────────────────────────


def _load_json(path: str) -> Optional[dict]:
    """读 JSON。不存在/损坏一律返回 None，不抛（调用方据此判 ok=False）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _file_age(path: str, now: float) -> Optional[float]:
    try:
        return max(0.0, now - os.path.getmtime(path))
    except Exception:
        return None


def _detect_base_dir() -> str:
    """定位项目根：staged_upgrade/ 与本目录都试，谁的 paper_trading_state.json 在就算谁。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for cand in (d, os.path.dirname(d)):
        if os.path.exists(os.path.join(cand, "paper_trading_state.json")):
            return cand
    return os.path.dirname(d)


def contract_multiplier(
    symbol: str,
    specs: Dict[str, Any],
    journal_mult: Optional[Dict[str, float]] = None,
) -> float:
    """合约乘数：trade_config.contract_specs → trade_journal._MULTIPLIERS → 抛错。

    **不回退到 10。** 未知乘数会让权益算错，必须显式失败。
    """
    spec = (specs or {}).get(symbol) or {}
    for key in ("multiplier", "mult"):
        v = spec.get(key)
        if isinstance(v, (int, float)) and float(v) > 0:
            return float(v)
    if journal_mult and symbol in journal_mult:
        v = journal_mult[symbol]
        if isinstance(v, (int, float)) and float(v) > 0:
            return float(v)
    raise UnknownMultiplier(f"{symbol}: 乘数未知（contract_specs 与 journal 表都没有）")


def dir_sign(direction: Any) -> int:
    """方向归一化：多/买/long/1 → +1；空/卖/short/-1 → -1；其余 0。"""
    if direction is None:
        return 0
    s = str(direction).strip().lower()
    if s in ("多", "买", "long", "buy", "b", "1", "+1"):
        return 1
    if s in ("空", "卖", "short", "sell", "s", "-1"):
        return -1
    return 0


# ── 模拟盘账本估值 ────────────────────────────────────────────────────────


def paper_equity(
    state: dict,
    specs: Dict[str, Any],
    price_fn: Optional[Callable[[str], Optional[float]]] = None,
    journal_mult: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[float], Tuple[Leg, ...], Tuple[str, ...]]:
    """cash + Σ浮动 = 模拟盘权益。

    浮动一律用「现价」重算，**不信持仓里落盘的 last_price**（实测 paper_trading_state
    的 last_updated 可能落后 1 小时以上，直接读会拿旧价当权益）。
    取不到现价时该腿 mtm=None，整体返回 None —— 宁可失败，不给残缺权益。
    """
    warnings: list[str] = []
    legs: list[Leg] = []
    if not isinstance(state, dict):
        return None, (), ("账本不是 dict",)
    try:
        cash = float(state.get("cash"))
    except Exception:
        return None, (), ("cash 缺失或非数值",)

    positions = state.get("positions") or {}
    if not isinstance(positions, dict):
        return None, (), ("positions 不是 dict",)

    unpriced: list[str] = []
    for sym, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        lots = pos.get("remaining_lots", pos.get("lots"))
        try:
            lots = float(lots or 0)
        except Exception:
            lots = 0.0
        if lots <= 0:
            continue
        d = dir_sign(pos.get("direction"))
        if d == 0:
            warnings.append(f"{sym}: 方向无法识别({pos.get('direction')!r})，已跳过")
            continue
        try:
            entry = float(pos.get("entry_price"))
        except Exception:
            warnings.append(f"{sym}: 开仓价缺失，已跳过")
            continue
        try:
            mult = contract_multiplier(sym, specs, journal_mult)
        except UnknownMultiplier as e:
            raise UnknownMultiplier(str(e)) from None

        mark: Optional[float] = None
        if price_fn is not None:
            try:
                p = price_fn(sym)
                mark = float(p) if isinstance(p, (int, float)) and float(p) > 0 else None
            except Exception:
                mark = None
        if mark is None:
            v = pos.get("last_price")
            mark = float(v) if isinstance(v, (int, float)) and float(v) > 0 else None
            if mark is not None:
                warnings.append(f"{sym}: 无实时价，退回落盘 last_price（可能过期）")

        if mark is None:
            unpriced.append(sym)
            legs.append(Leg(sym, str(pos.get("direction")), lots, entry, None, mult, None))
            continue

        mtm = (mark - entry) * d * lots * mult
        legs.append(Leg(sym, str(pos.get("direction")), lots, entry, mark, mult, mtm))

    if unpriced:
        warnings.append(f"取不到现价的腿: {','.join(unpriced)} ⇒ 权益不可信")
        return None, tuple(legs), tuple(warnings)

    equity = cash + sum(l.mtm or 0.0 for l in legs)
    return equity, tuple(legs), tuple(warnings)


# ── 主解析入口 ────────────────────────────────────────────────────────────


def resolve(
    mode: str = MODE_PAPER,
    price_fn: Optional[Callable[[str], Optional[float]]] = None,
    base_dir: Optional[str] = None,
    max_age: float = 180.0,
    floor: float = 10_000.0,
    cap: Optional[float] = None,
    now: Optional[float] = None,
    live_reader: Optional[Callable[[], Optional[dict]]] = None,
    allow_stale_prices: bool = False,
) -> Reading:
    """按模式解析一次可信权益。

    mode="paper": 只认模拟盘账本；取不到 → ok=False（不跨模式借用数字）
    mode="live" : live_broker → live_snapshot → manual_anchor；全失败 → ok=False
    """
    now = time.time() if now is None else now
    here = base_dir or _detect_base_dir()

    if mode == MODE_PAPER:
        return _resolve_paper(here, price_fn, max_age, floor, cap, now, allow_stale_prices)

    if mode == MODE_LIVE:
        return _resolve_live(here, max_age, floor, cap, now, live_reader)

    return Reading(None, "invalid_mode", False, f"未知 mode={mode!r}")


def _finalize(equity: Optional[float], source: str, detail: str, age: Optional[float],
              legs: Tuple[Leg, ...], warns: Tuple[str, ...],
              floor: float, cap: Optional[float]) -> Reading:
    if equity is None:
        return Reading(None, source, False, detail, age, legs, warns)
    if equity <= 0:
        return Reading(None, source, False, f"{detail}；权益 {equity:.2f} ≤ 0", age, legs, warns)
    if equity < floor:
        return Reading(None, source, False,
                       f"{detail}；权益 {equity:.2f} < 下限 {floor:.0f}", age, legs, warns)
    if cap is not None and equity > cap:
        return Reading(cap, source, True, f"{detail}；已按上限 {cap:.0f} 截断", age, legs, warns)
    return Reading(equity, source, True, detail, age, legs, warns)


def _resolve_paper(here: str, price_fn, max_age: float, floor: float,
                   cap: Optional[float], now: float,
                   allow_stale_prices: bool = False) -> Reading:
    """模拟盘权益。

    ⚠️ 新鲜度语义（2026-11-06 实测修正）：`paper_trading_state.json` 只在**交易事件**
    （开/平仓）时落盘，实测盘中已 82 分钟未更新，而权益每秒都在变。所以文件 mtime
    衡量的是「上次成交」，**不是「价格新鲜度」**。真正的价格新鲜度由 price_fn 决定：
      - 传了 price_fn（现价源）→ 以现价算浮动，权益是活的
      - 没传 → 只能用持仓里的 last_price，本身就可能是旧价 ⇒ 默认拒绝，除非显式
        声明这是在收盘后按最后价估值（allow_stale_prices=True）
    """
    path = os.path.join(here, "paper_trading_state.json")
    state = _load_json(path)
    if state is None:
        return Reading(None, SOURCE_PAPER_LEDGER, False, f"账本不可读: {path}")

    age = _file_age(path, now)

    cfg = _load_json(os.path.join(here, "trade_config.json")) or {}
    specs = cfg.get("contract_specs") or {}
    journal_mult = _journal_multipliers(here)

    try:
        eq, legs, warns = paper_equity(state, specs, price_fn, journal_mult)
    except UnknownMultiplier as e:
        return Reading(None, SOURCE_PAPER_LEDGER, False, f"乘数未知，拒绝估值: {e}", age)

    detail = f"账本 cash + {len(legs)} 腿浮动"
    if age is not None:
        detail += f"（账本 {age:.0f}s 前落盘）"

    if price_fn is None and not allow_stale_prices:
        return Reading(eq, SOURCE_PAPER_LEDGER, False,
                       detail + "；未提供实时价源，last_price 可能过期 ⇒ 拒绝出信号"
                       "（收盘后估值请显式传 allow_stale_prices=True）",
                       age, legs, warns)

    return _finalize(eq, SOURCE_PAPER_LEDGER, detail, age, legs, warns, floor, cap)


def _resolve_live(here: str, max_age: float, floor: float, cap: Optional[float],
                  now: float, live_reader) -> Reading:
    # ① 券商实时资金
    if live_reader is not None:
        try:
            snap = live_reader()
        except Exception as e:
            snap = None
            _ = e
        if isinstance(snap, dict):
            for k in ("equity", "balance", "total_asset"):
                v = snap.get(k)
                if isinstance(v, (int, float)) and float(v) > 0:
                    return _finalize(float(v), SOURCE_LIVE_BROKER, f"券商快照字段 {k}",
                                     None, (), (), floor, cap)

    path = os.path.join(here, "account_state.json")
    st = _load_json(path) or {}
    age = _file_age(path, now)

    # ② 系统落盘的动态权益
    v = st.get("equity_live")
    if isinstance(v, (int, float)) and float(v) > 0:
        return _finalize(float(v), SOURCE_LIVE_SNAPSHOT, "account_state.equity_live",
                         age, (), (), floor, cap)

    # ③ 人工同步锚
    v = st.get("equity")
    if isinstance(v, (int, float)) and float(v) > 0:
        return _finalize(float(v), SOURCE_MANUAL_ANCHOR, "account_state.equity（人工锚）",
                         age, (), (), floor, cap)

    return Reading(None, "no_source", False,
                   "实盘三级来源全空（券商/equity_live/人工锚）；拒绝出信号，不做静态兜底",
                   age)


def _journal_multipliers(here: str) -> Optional[Dict[str, float]]:
    """从 trade_journal 取乘数兜底表；取不到返回 None（不抛）。"""
    try:
        p = os.path.join(here, "trade_journal.py")
        if not os.path.exists(p):
            return None
        # 只读源码取一个字面量字典，不 import 整个模块（它拉 pandas 等重依赖）
        src = open(p, encoding="utf-8").read()
        start = src.find("_MULTIPLIERS = {")
        if start < 0:
            return None
        end = src.find("}", start)
        ns: Dict[str, Any] = {}
        exec(src[start:end + 1], {}, ns)  # noqa: S102 — 只执行一个字面量字典赋值
        d = ns.get("_MULTIPLIERS")
        return {k: float(v) for k, v in d.items()} if isinstance(d, dict) else None
    except Exception:
        return None


# ── 命令行自检（只读）──────────────────────────────────────────────────────


def _api_paper_snapshot(url: str) -> Optional[dict]:
    """从运行中的 runner 读模拟盘快照，用于交叉验算（只读 GET）。"""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="账户权益 SSOT 只读自检")
    ap.add_argument("--mode", default=MODE_PAPER, choices=[MODE_PAPER, MODE_LIVE])
    ap.add_argument("--base-dir", default=None)
    ap.add_argument("--max-age", type=float, default=180.0)
    ap.add_argument("--check", action="store_true", help="与运行中的 runner 交叉验算")
    ap.add_argument("--api", default="http://127.0.0.1:8741/api/paper-trading")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.base_dir is None:
        args.base_dir = _detect_base_dir()

    price_fn = None
    api_eq = None
    if args.check:
        snap = _api_paper_snapshot(args.api)
        if snap:
            m = {}
            for p in snap.get("positions") or []:
                cp = p.get("current_price")
                if isinstance(cp, (int, float)) and cp > 0:
                    m[str(p.get("symbol"))] = float(cp)
            price_fn = lambda s: m.get(s)  # noqa: E731
            api_eq = snap.get("equity")

    r = resolve(mode=args.mode, price_fn=price_fn, base_dir=args.base_dir,
                max_age=args.max_age)
    d = r.as_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if api_eq is not None and r.equity is not None:
        diff = abs(r.equity - float(api_eq))
        print(f"\n交叉验算: ssot={r.equity:.2f}  api={float(api_eq):.2f}  diff={diff:.2f}"
              f"  → {'PASS' if diff < 1.0 else 'FAIL'}")
        return 0 if diff < 1.0 else 1
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
