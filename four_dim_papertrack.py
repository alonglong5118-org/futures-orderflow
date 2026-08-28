# -*- coding: utf-8 -*-
"""
four_dim_papertrack.py · 四维策略模拟盘「真实回测」复盘器
=================================================================
只读、不交易。对 four_dim_signals.json 中已发出的「建仓」信号，用信号时刻
之后的实际行情逐根 walk-forward 判定：价格先触目标(止盈)还是先触止损(止损)。
输出真实胜率 / 期望R / 连续亏损 / 持仓周期分布。

数据来源（按精度优先）：
  · 本地 5m 缓存（data_5m/_XX0_min5.csv，覆盖信号后近~3天）→ 5m 精度
  · 否则 akshare 主连日线（load_daily_refreshed，覆盖全部历史）→ 日线精度
未触达（仍持仓 / 数据不足）的信号标 pending，下一次运行（数据更充足后）自动重评。
不触达的信号不参与胜率/期望R 统计，避免虚高。

输出：papertrack_report.json
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

import pandas as pd

# 四维子分重建需要引擎的 pipeline / 配置（仅 backfill_subscores 用，lazy import 避免顶层循环依赖）
import four_dim_strategy as _fds

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_JSON = os.path.join(HERE, "four_dim_signals.json")
REPORT_JSON = os.path.join(HERE, "papertrack_report.json")

SIGNAL_TYPE = "建仓"
DIRECTION_MAP = {"多": "long", "long": "long", "buy": "long", "空": "short", "short": "short", "sell": "short"}

# ── 换月/跳空 跳空识别（P0-2）────────────────────────────────────────────
# 主连日线在合约换月处会出现巨大"展期缺口"（旧合约收盘→新合约开盘的跳变），
# 该缺口非真实价格运动，却会被误判"触止损/触止盈" → 假交易，污染真实回测成绩。
# 判定规则：某根 K 线的开盘相对前收跳变超过以下任一阈值 → 视为展期/涨跌停缺口，
# 跳过该根的止损/止盈判定（沿用上一根未平状态继续）。
#   · ROLL_GAP_PCT    : 相对前收价的跳变比例阈值（默认 1.0%，覆盖多数换月缺口）
#   · ROLL_GAP_MULT   : 相对止损距离的倍数阈值（默认 1.0 倍，即缺口超过整段风险距离）
ROLL_GAP_PCT = 0.010
ROLL_GAP_MULT = 1.0

# ── 回测确定性（可复现）────────────────────────────────────────────────────
# 问题：backtest_signal 每次都实时重拉行情(live_bars/本地5m/akshare日线)，
# 数据随时变化 → 同一信号在不同时间重算会得到不同 outcome，门控指标不可信。
# 修复：信号首次可判定时，把"实际用于判定的 K 线"快照固化进报告
# (backtest_bars + backtest_gran)；之后任何重算都从快照重放，结果 100% 可复现。
BARS_SNAPSHOT_MAX = 600  # 单笔快照最多保留的根数(覆盖绝大多数持仓周期)


def bars_to_records(df: "pd.DataFrame") -> list:
    """DataFrame(OHLCV, DatetimeIndex) → 可 JSON 序列化的 [[iso,o,h,l,c], ...]。"""
    if df is None or len(df) == 0:
        return []
    recs = []
    for ts, row in df.iterrows():
        recs.append(
            [
                pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                round(float(row["open"]), 4),
                round(float(row["high"]), 4),
                round(float(row["low"]), 4),
                round(float(row["close"]), 4),
            ]
        )
    return recs[:BARS_SNAPSHOT_MAX]


def records_to_bars(recs: list) -> "pd.DataFrame | None":
    """[[iso,o,h,l,c], ...] → DatetimeIndex 的 OHLCV DataFrame，供重放。"""
    if not recs:
        return None
    idx, o, h, l, c = [], [], [], [], []
    for r in recs:
        try:
            idx.append(pd.Timestamp(r[0]))
            o.append(float(r[1]))
            h.append(float(r[2]))
            l.append(float(r[3]))
            c.append(float(r[4]))
        except Exception:
            continue
    if not idx:
        return None
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=pd.DatetimeIndex(idx))


def sig_id(s: dict) -> str:
    key = (
        s.get("symbol"),
        s.get("time"),
        s.get("price"),
        s.get("direction"),
        s.get("stop"),
        s.get("target"),
        s.get("lots"),
    )
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def parse_signal(s: dict) -> dict | None:
    """解析单条建仓信号为可回测的几何结构；无效返回 None。"""
    if s.get("signal_type") != SIGNAL_TYPE:
        return None
    entry, stop, target = s.get("price"), s.get("stop"), s.get("target")
    if None in (entry, stop, target):
        return None
    direction = DIRECTION_MAP.get(str(s.get("direction", "")).strip().lower())
    if direction is None:
        return None
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None
    dist_to_target = abs(target - entry)
    if dist_to_target <= 0:
        return None
    if direction == "long" and target <= entry:
        return None
    if direction == "short" and target >= entry:
        return None
    pipe = s.get("pipeline", {}) or {}
    return {
        "id": sig_id(s),
        "symbol": s.get("symbol"),
        "name": s.get("name"),
        "direction": direction,
        "time": s.get("time"),
        "entry": round(float(entry), 4),
        "stop": round(float(stop), 4),
        "target": round(float(target), 4),
        "stop_dist": round(stop_dist, 4),
        "dist_to_target": round(dist_to_target, 4),
        "target_R": round(dist_to_target / stop_dist, 4),
        "lots": s.get("lots") or 1,
        "atr_src": s.get("atr_src"),
        "regime": pipe.get("regime"),
        "conv": pipe.get("conv"),
        "bias_G": pipe.get("bias_G"),
        # 四维子分（P1-3 盈亏归因用）：F 基本面 / T_5m 技术触发 / C 资金面(实盘含资金流Cflow)
        "F_bias": pipe.get("F_bias") if pipe.get("F_bias") is not None else pipe.get("F"),
        "T_D": pipe.get("T_D"),
        "T_5m": pipe.get("T_5m"),
        "C_score": pipe.get("C_score") if pipe.get("C_score") is not None else pipe.get("C"),
    }


def _load_live_bars(symbol):
    """读 runner 实时聚合的 5m 行情（live_bars.json，覆盖最近~2天真实 tick 聚合）。
    live_bars 键大小写不统一（主品种大写 FG/SA/JM/J，jd/lh 小写），尝试多大小写变体。
    返回 DatetimeIndex 的 OHLCV DataFrame 或 None。"""
    try:
        with open(os.path.join(HERE, "live_bars.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        bars = None
        for cand in (symbol, symbol.upper(), symbol.lower()):
            if cand in d and d[cand]:
                bars = d[cand]
                break
        if not bars:
            return None
        rows = []
        for b in bars:
            try:
                dt = pd.to_datetime(b["date"])
            except Exception:
                continue
            rows.append(
                (
                    dt,
                    float(b["open"]),
                    float(b["high"]),
                    float(b["low"]),
                    float(b["close"]),
                    float(b.get("volume", 0)),
                    float(b.get("oi", 0)),
                )
            )
        if not rows:
            return None
        idx = pd.DatetimeIndex([r[0] for r in rows])
        df = pd.DataFrame(
            [[r[1], r[2], r[3], r[4], r[5], r[6]] for r in rows],
            columns=["open", "high", "low", "close", "volume", "oi"],
            index=idx,
        ).sort_index()
        return df
    except Exception:
        return None


def _load_backtest_bars(symbol, signal_dt):
    """返回 signal_dt 之后的实际 K 线 (DatetimeIndex OHLCV)。
    数据源优先级：runner实时5m(live_bars) > 本地5m缓存 > akshare主连日线。
    返回 (df, granularity) 或 (None, None)。"""
    # 0) runner 实时聚合 5m（覆盖最近~2天真实行情，精度最高、无需等T+1）
    try:
        lb = _load_live_bars(symbol)
        if lb is not None and len(lb):
            after = lb[lb.index > signal_dt]
            if len(after) >= 3:
                return after, "live5m"
    except Exception:
        pass
    import four_dim_strategy as fd

    # 1) 本地 5m 缓存（code 用基础符号，load_min5 自动补 0 → _FG0_min5.csv）
    try:
        code = symbol.upper()
        df5 = fd.load_min5(code, fetch_if_missing=False)
        if df5 is not None and len(df5):
            after = df5[df5.index > signal_dt]
            if len(after) >= 3:
                return after, "5m"
    except Exception:
        pass
    # 2) 日线（akshare 主连，联网追加近期）
    try:
        dfd = fd.load_daily_refreshed(symbol)
        if dfd is not None and len(dfd):
            sdate = signal_dt.normalize()
            after = dfd[dfd.index >= sdate]
            if len(after) >= 1:
                return after, "daily"
    except Exception as e:
        print(f"  [backtest] {symbol} 日线失败: {e}")
    return None, None


def backtest_signal(p: dict, bars=None, gran=None) -> dict:
    """对单条已解析信号做真实 walk-forward 回测。
    返回 {outcome, R, holding_bars, gran, status}
      outcome: 'win' / 'loss'；status: 'done'(已判定) / 'pending'(数据不足或仍持仓)。
    bars/gran 若提供(确定性重放路径)：直接用该快照判定，不再实时拉取行情，
    保证同一信号每次重算结果一致。"""
    symbol = p["symbol"]
    try:
        sdt = pd.to_datetime(p["time"])
    except Exception:
        return {"outcome": None, "R": 0.0, "holding_bars": 0, "gran": None, "status": "pending"}
    if bars is None:
        bars, gran = _load_backtest_bars(symbol, sdt)
    if bars is None or len(bars) < 1:
        return {
            "outcome": None,
            "R": 0.0,
            "holding_bars": 0,
            "gran": gran if gran is not None else None,
            "status": "pending",
        }
    direction = p["direction"]
    target, stop = p["target"], p["stop"]
    stop_dist = abs(stop - p["entry"]) or 1e-9
    # 跳过信号所在根（含入场的同根不用于前视判定），从下一根起逐根判定
    seq = bars.iloc[1:] if len(bars) > 1 else bars.iloc[0:0]
    if len(seq) == 0:
        return {"outcome": None, "R": 0.0, "holding_bars": 0, "gran": gran, "status": "pending"}
    # 前收价（用于识别换月跳空）：从信号根收盘起算
    prev_close = float(bars.iloc[0]["close"]) if len(bars) > 0 else None
    roll_skipped = 0
    for i, (_, row) in enumerate(seq.iterrows()):
        o, hi, lo, c = (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
        # ── 换月跳空识别（P0-2）──
        # 首根(seq[0])即入场根，其开盘跳变是真实入場缺口，不跳過；
        # 后续根若开盘相对前收出现超阈值跳变，视为展期/涨跌停缺口，跳过本根判定。
        if i >= 1 and prev_close is not None:
            gap = abs(o - prev_close)
            thr = max(ROLL_GAP_PCT * prev_close, ROLL_GAP_MULT * stop_dist)
            if gap > thr:
                roll_skipped += 1
                prev_close = c
                continue
        if direction == "long":
            hit_t, hit_s = hi >= target, lo <= stop
        else:  # short
            hit_t, hit_s = lo <= target, hi >= stop
        if hit_t and hit_s:
            # 同根双触：保守判止损（止损更近者先触发）
            return {
                "outcome": "loss",
                "R": -1.0,
                "holding_bars": i + 1,
                "gran": gran,
                "status": "done",
                "roll_skipped": roll_skipped,
            }
        if hit_t:
            return {
                "outcome": "win",
                "R": p["target_R"],
                "holding_bars": i + 1,
                "gran": gran,
                "status": "done",
                "roll_skipped": roll_skipped,
            }
        if hit_s:
            return {
                "outcome": "loss",
                "R": -1.0,
                "holding_bars": i + 1,
                "gran": gran,
                "status": "done",
                "roll_skipped": roll_skipped,
            }
        prev_close = c
    # 遍历完仍未触达：仍持仓或数据不足
    return {
        "outcome": None,
        "R": 0.0,
        "holding_bars": len(seq),
        "gran": gran,
        "status": "pending",
        "roll_skipped": roll_skipped,
    }


def aggregate(trades, key_R, key_outcome) -> dict:
    total = len(trades)
    if total == 0:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "expected_R": 0.0,
            "max_consecutive_losses": 0,
            "consecutive_loss_warning": False,
            "final_cum_R": 0.0,
            "final_cum_R_lotweighted": 0.0,
        }
    wins = [t for t in trades if t[key_outcome] == "win"]
    losses = [t for t in trades if t[key_outcome] == "loss"]
    win_rate = len(wins) / total
    expected_R = sum(t[key_R] for t in trades) / total
    max_run = cur = 0
    for t in trades:
        if t[key_outcome] == "loss":
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 0
    cum_R = sum(t[key_R] for t in trades)
    cum_R_lw = sum(t[key_R] * (t["lots"] or 1) for t in trades)
    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "expected_R": round(expected_R, 4),
        "max_consecutive_losses": max_run,
        "consecutive_loss_warning": max_run >= 3,
        "final_cum_R": round(cum_R, 4),
        "final_cum_R_lotweighted": round(cum_R_lw, 4),
    }


def _by_symbol(trades):
    out = {}
    for t in trades:
        out.setdefault(t["symbol"], []).append(t)
    return {s: aggregate(v, "R", "outcome") for s, v in out.items()}


def compute_symbol_gates(window_n: int = 10, min_trades: int = 5, breakeven_wr: float = 1 / 3):
    """基于 papertrack_report.json 中已判定交易，算每个品种的「近期表现门槛」。
    取每个品种最近 window_n 笔已判定交易：
      · 笔数 < min_trades            → 样本不足，不门控（保守，允许发信号）
      · 胜率 < 盈亏平衡线 或 累计R<0 → 门控（暂停发信号）
      · 否则                         → 不门控
    返回 {sym: {gated, n, win_rate, cum_R, last_time}}。
    门控是动态的：某品种近期转负自动暂停，恢复正数后下次调用自动解除。"""
    try:
        with open(REPORT_JSON, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception:
        return {}
    trades = report.get("trades", [])
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    gates = {}
    for sym, ts in by_sym.items():
        ts = sorted(ts, key=lambda x: x.get("time") or "")
        recent = ts[-window_n:]
        n = len(recent)
        if n < min_trades:
            gates[sym] = {
                "gated": False,
                "n": n,
                "win_rate": None,
                "cum_R": None,
                "reason": "样本不足",
                "last_time": ts[-1].get("time"),
            }
            continue
        wins = sum(1 for t in recent if t.get("outcome") == "win")
        wr = wins / n
        cum_R = sum(t.get("R", 0.0) for t in recent)
        gated = (wr < breakeven_wr) or (cum_R < 0)
        reason = []
        if wr < breakeven_wr:
            reason.append(f"胜率{wr * 100:.0f}%<{breakeven_wr * 100:.0f}%")
        if cum_R < 0:
            reason.append(f"累计R{cum_R:+.2f}<0")
        gates[sym] = {
            "gated": gated,
            "n": n,
            "win_rate": round(wr, 3),
            "cum_R": round(cum_R, 3),
            "reason": "；".join(reason) if reason else "正常",
            "last_time": ts[-1].get("time"),
        }
    return gates


def backfill_snapshots(trades: list) -> int:
    """为「缺可靠快照」的历史交易补拍快照，使其可被确定性重放。
    使用与评分完全相同的 _load_backtest_bars(优先级 live5m>本地5m>日线)，
    保证取回的行情与原始判定所用一致 → 重放可完美复现。
    仅附快照、绝不改动已固化 outcome/R（历史成绩神圣不可变）。
    返回成功补拍的交易数。"""
    # 需要补拍：缺快照 / 快照不足(<3根) / 之前退化为近似(approx)
    need = [
        t
        for t in trades
        if t.get("status") == "done"
        and (not t.get("backtest_bars") or len(t.get("backtest_bars", [])) < 3 or t.get("snapshot_approx"))
    ]
    if not need:
        return 0
    done = 0
    for t in need:
        sym = t.get("symbol")
        try:
            sdt = pd.to_datetime(t["time"])
        except Exception:
            continue
        # 复用评分同款取数 → 粒度/数据一致
        bars, gran = _load_backtest_bars(sym, sdt)
        if bars is None or len(bars) < 2:
            t["reproducible"] = False  # 数据源已不可得(如超 live_bars 保留期), 仅保留冻结 outcome
            t["backtest_bars"] = t.get("backtest_bars")  # 不写垃圾快照
            continue
        recs = bars_to_records(bars)
        if not recs:
            continue
        t["backtest_bars"] = recs
        t["backtest_gran"] = gran
        t["snapshot_approx"] = False
        t["reproducible"] = True
        done += 1
    return done


# ── 四维子分重建（P1-3）──────────────────────────────────────────────────────
# 历史交易在「维度分未入库」时期已判定，trade 缺 F_bias/T_D/T_5m/C_score。
# 用信号时刻日线(向后窗口, ≤信号日)重跑引擎 pipeline() 重建三维子分：
#   · T 经诊断：必须用「向后日线窗口」重建 T_D(≈信号时刻技术偏置)；若用冻结的
#     前向5m窗口重建, T_5m 会变成"入场后"动量, 与真实信号时刻 T 相反(一致率仅22.8%)。
#     向后日线窗口重建 → T 一致率 85%(与 F/C 同级), 忠实还原。
#   · F(基本面)/C(资金面) 用当前快照重建（信号时刻基本面/龙虎榜已不可得）→ 近似, 标注 reconstructed
# 仅附维度分、绝不改动 outcome/R；无维度分的交易自然排除在归因之外。
def backfill_subscores(trades: list) -> int:
    need = [
        t
        for t in trades
        if t.get("status") == "done" and (t.get("F_bias") is None or t.get("T_5m") is None or t.get("C_score") is None)
    ]
    if not need:
        return 0
    daily_cache = {}
    done = 0
    for t in need:
        sym = t.get("symbol")
        try:
            sdt = pd.to_datetime(t["time"])
        except Exception:
            continue
        if sym not in daily_cache:
            try:
                d = _fds.load_daily(sym)
                daily_cache[sym] = d[d.index <= sdt] if (d is not None and len(d)) else None
            except Exception:
                daily_cache[sym] = None
        dfd = daily_cache[sym]
        # 关键：用向后日线窗口(≤信号日)重建 T_D；不传5m → pipeline 退化为 T_D，忠实还原信号时刻技术偏置
        try:
            date_str = sdt.strftime("%Y%m%d")
            pipe = _fds.pipeline(sym, dfd, None, _fds.DEFAULT_CONFIG, date=date_str, c_override=None)
        except Exception:
            continue
        t["F_bias"] = pipe.get("F")
        t["T_D"] = pipe.get("T_D")
        t["T_5m"] = pipe.get("T_5m")  # 重建下 == T_D（向后日线窗口）
        t["C_score"] = pipe.get("C")
        t["subscores_reconstructed"] = True
        done += 1
    return done


# ── 四维盈亏归因（P1-3）──────────────────────────────────────────────────────
# 引擎合成偏置 combine_bias = 0.6*T + 0.25*F + 0.15*C → 各维权重即其对决策的贡献度。
# 归因逻辑：每条 done trade，方向 D(多+1/空-1)，维度 d 投票 vote=sign(score_d)：
#   · 同意(agree)：vote==D 且非中性 → 该维「 commitment 了这笔交易的方向」
#   · 归因R：R * 权重_w_d * (1 if agree else 0)
#       - 盈利时，同意维获正信用；亏损时，同意维被记负（它撑了错误的方向）
#       - 中立/反对维记 0（没驱动这笔交易，不揽功也不背锅）
#     → 三维修正R之和 ≈ R*(同意维权重和) ≤ R，无重复计数。
# 另算「G 合成偏置」(sign(bias_G)) 作参考决策维（不入R归因和，避免双重计数）。
DIM_WEIGHTS = {"F": 0.25, "T": 0.60, "C": 0.15}
DIM_LABELS = {"F": "基本面(F)", "T": "技术面·触发(T)", "C": "资金面/资金流(C)"}


def _dim_vote(score):
    """返回投票: +1 偏多 / -1 偏空 / 0 中性(未表态)。None 表示该维无数据。"""
    if score is None:
        return None
    if score > 0:
        return 1
    if score < 0:
        return -1
    return 0


def compute_attribution(trades: list) -> dict:
    overall_wr = None
    done_n = sum(1 for t in trades if t.get("status") == "done")
    if done_n:
        wins = sum(1 for t in trades if t.get("status") == "done" and t.get("outcome") == "win")
        overall_wr = round(wins / done_n, 4)
    stats = {}
    for key in ("F", "T", "C"):
        w = DIM_WEIGHTS[key]
        n_voted = n_agree = aw = al = wo = wlo = 0
        attr_R = 0.0
        for t in trades:
            if t.get("status") != "done":
                continue
            sc = t.get(key + ("_bias" if key == "F" else ("_5m" if key == "T" else "_score")))
            vote = _dim_vote(sc)
            if vote is None or vote == 0:
                continue
            D = 1 if t["direction"] == "long" else -1
            n_voted += 1
            if vote == D:
                n_agree += 1
                attr_R += (t.get("R", 0.0) or 0.0) * w
                if t.get("outcome") == "win":
                    aw += 1
                else:
                    al += 1
            else:
                if t.get("outcome") == "win":
                    wo += 1
                else:
                    wlo += 1
        denom_ag = aw + al
        denom_op = wo + wlo
        stats[key] = {
            "label": DIM_LABELS[key],
            "weight": w,
            "n_voted": n_voted,
            "agreement_rate": round(n_agree / n_voted, 4) if n_voted else None,
            "win_if_agree": round(aw / denom_ag, 4) if denom_ag else None,
            "win_if_oppose": round(wo / denom_op, 4) if denom_op else None,
            "attr_R": round(attr_R, 3),
            "attr_wins": aw,
            "attr_losses": al,
            "attr_R_per_trade": round(attr_R / n_voted, 4) if n_voted else None,
        }
    # G 合成偏置（参考维）
    g_voted = g_agree = g_aw = g_al = 0
    for t in trades:
        if t.get("status") != "done":
            continue
        g = t.get("bias_G")
        if g is None or g == 0:
            continue
        vote = 1 if g > 0 else -1
        D = 1 if t["direction"] == "long" else -1
        g_voted += 1
        if vote == D:
            g_agree += 1
            if t.get("outcome") == "win":
                g_aw += 1
            else:
                g_al += 1
    g_denom = g_aw + g_al
    gstat = {
        "label": "合成偏置(G)",
        "weight": 1.0,
        "n_voted": g_voted,
        "agreement_rate": round(g_agree / g_voted, 4) if g_voted else None,
        "win_if_agree": round(g_aw / g_denom, 4) if g_denom else None,
        "attr_R": None,  # G 不计入R归因和
    }
    return {
        "overall_win_rate": overall_wr,
        "dims": stats,  # F / T / C
        "G": gstat,
    }


def attach_dim_votes(trades: list):
    """为每条 done trade 附 dim_votes（F/T/C 投票与是否同意交易方向），供面板明细展示。"""
    for t in trades:
        if t.get("status") != "done":
            continue
        D = 1 if t["direction"] == "long" else -1
        dv = {}
        for key, fld in (("F", "F_bias"), ("T", "T_5m"), ("C", "C_score")):
            sc = t.get(fld)
            vote = _dim_vote(sc)
            dv[key] = {
                "score": sc,
                "vote": vote,  # +1/-1/0
                "agree": (vote is not None and vote != 0 and vote == D),
            }
        t["dim_votes"] = dv


def main():
    if os.path.exists(REPORT_JSON):
        with open(REPORT_JSON, "r", encoding="utf-8") as f:
            report = json.load(f)
    else:
        report = {"trades": [], "scored_ids": []}

    already = set(report.get("scored_ids", []))  # 仅记录已判定(done)的信号 id
    trades = list(report.get("trades", []))

    with open(SIGNALS_JSON, "r", encoding="utf-8") as f:
        signals = json.load(f)

    new_trades = []
    skipped = 0
    pending_count = 0
    for s in signals:
        p = parse_signal(s)
        if p is None:
            skipped += 1
            continue
        if p["id"] in already:
            continue
        # 确定性：先取实际用于判定的 K 线快照，再重放(不再二次拉取) → 结果可复现
        bars, gran = _load_backtest_bars(p["symbol"], pd.to_datetime(p["time"]))
        if bars is None or len(bars) < 1:
            pending_count += 1
            continue  # 数据不足，下次自动重评
        bt = backtest_signal(p, bars=bars, gran=gran)
        if bt["status"] == "pending":
            pending_count += 1
            continue  # 不计入，下次数据更充足后自动重评
        p["outcome"] = bt["outcome"]
        p["R"] = bt["R"]
        p["holding_bars"] = bt["holding_bars"]
        p["gran"] = bt["gran"]
        p["status"] = "done"
        # 固化快照：之后任何重算从 backtest_bars 重放，杜绝行情漂移导致的结果跳动
        p["backtest_bars"] = bars_to_records(bars)
        p["backtest_gran"] = gran
        new_trades.append(p)
        already.add(p["id"])

    trades.extend(new_trades)
    trades.sort(key=lambda t: t["time"] or "")

    # 确定性：为历史缺快照的交易补拍(仅附快照,不改 outcome) → 全报告可复现
    n_backfill = backfill_snapshots(trades)

    # P1-3：历史缺维度分的交易重建四维子分（仅附维度分, 不改 outcome）
    n_sub_backfill = backfill_subscores(trades)

    # P1-3：为每条 done trade 附维度投票（须在 extend+backfill 之后）
    attach_dim_votes(trades)

    # P1-3：四维盈亏归因
    attribution = compute_attribution(trades)

    agg = aggregate(trades, "R", "outcome")
    by_sym = _by_symbol(trades)
    # 正确盈亏平衡胜率: 使期望R=0 所需胜率 = |avg_loss_R| / (avg_win_R + |avg_loss_R|)
    # (旧公式 1/(1+期望R) 错把期望值当平均盈利, 期望R<0 时会解出>100% 的荒谬值)
    _done = [t for t in trades if t.get("status") == "done"]
    _wins = [t["R"] for t in _done if t.get("outcome") == "win"]
    _losses = [t["R"] for t in _done if t.get("outcome") == "loss"]
    if _wins and _losses:
        avg_win_R = sum(_wins) / len(_wins)
        avg_loss_R = sum(_losses) / len(_losses)  # 恒为负
        denom = avg_win_R + abs(avg_loss_R)
        breakeven_wr = (abs(avg_loss_R) / denom) if denom > 0 else None
    else:
        breakeven_wr = None
    roll_skipped_total = sum(int(t.get("roll_skipped", 0) or 0) for t in trades)

    # 权益曲线（按时间累计真实 R）
    cum_R = cum_R_lw = 0.0
    equity_curve = []
    for i, t in enumerate(trades, 1):
        cum_R += t["R"]
        cum_R_lw += t["R"] * (t["lots"] or 1)
        equity_curve.append(
            {
                "idx": i,
                "time": t["time"],
                "symbol": t["symbol"],
                "outcome": t["outcome"],
                "R": t["R"],
                "lots": t["lots"],
                "target_R": t["target_R"],
                "holding_bars": t["holding_bars"],
                "gran": t.get("gran"),
                "cum_R": round(cum_R, 4),
                "cum_R_lotweighted": round(cum_R_lw, 4),
            }
        )

    report = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "four_dim_signals.json",
            "method": "walk_forward_real (信号后实际行情逐根判定 先触目标/先触止损)",
            "data_priority": "本地5m缓存 > akshare主连日线",
            "pending_note": "未触达信号标 pending，下次运行自动重评，不参与胜率统计",
            "breakeven_winrate_formula": "|avg_loss_R|/(avg_win_R+|avg_loss_R|)",
        },
        "summary": {
            "new_scored": len(new_trades),
            "skipped_invalid": skipped,
            "pending_count": pending_count,
            "backfilled_snapshots": n_backfill,
            "roll_skipped_total": roll_skipped_total,
            "cumulative_done": agg["total"],
            "headline": {
                "expected_R": agg["expected_R"],
                "win_rate": agg["win_rate"],
                "wins": agg["wins"],
                "losses": agg["losses"],
                "max_consecutive_losses": agg["max_consecutive_losses"],
                "consecutive_loss_warning": agg["consecutive_loss_warning"],
                "final_cum_R": agg["final_cum_R"],
                "final_cum_R_lotweighted": agg["final_cum_R_lotweighted"],
            },
            "breakeven_required_winrate": round(breakeven_wr, 4) if breakeven_wr is not None else None,
            "by_symbol": by_sym,
            "subscore_backfill": n_sub_backfill,
            "attribution": attribution,
        },
        "trades": trades,
        "equity_curve": equity_curve,
        "scored_ids": list(already),
    }

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    s = report["summary"]
    h = s["headline"]
    print("=== 四维模拟盘 · 真实 walk-forward 回测 ===")
    print(f"本次新增已判定 : {s['new_scored']}")
    print(f"跳过(无效)     : {s['skipped_invalid']}")
    print(f"仍 pending      : {s['pending_count']} (下次自动重评)")
    print(f"补拍K线快照   : {s['backfilled_snapshots']} 笔历史交易 (确定性固化, 不改outcome)")
    print(f"换月跳空跳过   : {s['roll_skipped_total']} 根 (P0-2 修复后剔除的展期伪触发)")
    print(f"累计已判定     : {s['cumulative_done']}")
    print(f"真实期望R      : {h['expected_R']:+.4f}")
    print(f"真实胜率       : {h['win_rate'] * 100:.1f}%")
    print(f"盈亏平衡胜率   : {s['breakeven_required_winrate'] * 100:.1f}%")
    print(f"连续亏损(最长) : {h['max_consecutive_losses']} -> 预警={h['consecutive_loss_warning']}")
    print(f"累计R(等权)    : {h['final_cum_R']:+.2f}")
    print(f"累计R(手数加权): {h['final_cum_R_lotweighted']:+.2f}")
    print("按品种:")
    for sym, b in sorted(by_sym.items()):
        print(
            f"  {sym:>4s}: n={b['total']:>3d} 胜率{b['win_rate'] * 100:5.1f}% 期望R{b['expected_R']:+.3f} 连亏{b['max_consecutive_losses']}"
        )
    # P1-3 四维盈亏归因
    att = s.get("attribution") or {}
    if att:
        dims = att.get("dims", {})
        print("=== 四维盈亏归因 (P1-3) ===")
        print(f"(总胜率 {(att.get('overall_win_rate') or 0):.1%}；维度权重 T0.6/F0.25/C0.15)")
        print(f"{'维度':<16s}{'投票数':>7s}{'一致率':>8s}{'同意时胜率':>11s}{'归因R':>9s}")
        for key in ("F", "T", "C"):
            d = dims.get(key, {})
            if not d:
                continue
            print(
                f"{d['label']:<14s}{d['n_voted']:>7d}{(d['agreement_rate'] or 0):>8.1%}"
                f"{(d['win_if_agree'] or 0):>11.1%}{d['attr_R']:>+9.2f}"
            )
        g = att.get("G", {})
        if g:
            print(
                f"{g['label']:<14s}{g['n_voted']:>7d}{(g['agreement_rate'] or 0):>8.1%}"
                f"{(g['win_if_agree'] or 0):>11.1%}{'—':>9s}(参考维)"
            )
        print(f"维度分重建     : {s.get('subscore_backfill', 0)} 笔历史交易 (T可复现, F/C近似)")
    print(f"报告已写入     : {REPORT_JSON}")


if __name__ == "__main__":
    main()
