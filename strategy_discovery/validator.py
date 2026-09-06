"""验证引擎：把假设跑回测 + 对比基准 + OOS 过拟合检测 → 判定通过/失败。"""

import json
import os
import sys
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

# 确保能 import 项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import four_dim_strategy as fd
from .hypothesis import Hypothesis

# ── 基准品种（和回归测试一致，保持可比性）──
DEFAULT_SYMBOLS = ["cu", "rb", "JM", "i", "M", "y", "pp", "TA"]

# ── 过拟合判定阈值 ──
OOS_WARN_DEGRADE = 0.30   # IS→OOS expR 退化 >30% → 警告
OOS_FAIL_DEGRADE = 0.50   # IS→OOS expR 退化 >50% → 失败（过拟合）
WIN_DEGRADE_WARN = 0.10   # 胜率下降 >10% → 警告
WIN_DEGRADE_FAIL = 0.20   # 胜率下降 >20% → 失败
SIG_AGREE_WARN = 0.85     # 信号一致率 <85% → 警告
SIG_AGREE_FAIL = 0.70     # 信号一致率 <70% → 失败（逻辑变了）
TRADE_CHANGE_WARN = 0.50  # 交易数变化 >50% → 警告
TRADE_CHANGE_FAIL = 0.80  # 交易数变化 >80% → 失败

# ── 综合放行标准 ──
MIN_EXP = 0.02            # 最低 expR（正收益）
MIN_TRADES = 10           # 最少交易数（样本太小没意义）


def load_baseline(path: str = "regression_baseline.json") -> Optional[Dict]:
    """加载回归测试基准。"""
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def run_backtest(symbol: str, cfg: Dict, tail: Optional[int] = None,
                 df_in=None) -> Dict:
    """跑单个品种的 walk-forward 回测，标准化返回。"""
    try:
        r = fd.walk_forward_backtest(symbol, cfg, tail=tail, df_in=df_in)
    except Exception as e:
        return {
            "symbol": symbol,
            "name": fd.SYMBOLS.get(symbol, {}).get("name", symbol),
            "group": fd.SYMBOLS.get(symbol, {}).get("group", "未知"),
            "trades": 0,
            "expR": None,
            "win_rate": None,
            "error": str(e)[:120],
        }

    trades_detail = r.get("trades_detail", [])
    signatures = []
    for t in trades_detail[:50]:
        d = t.get("date", "")
        direction = t.get("direction", "")
        regime = t.get("regime", "")
        signatures.append(f"{d}_{direction}_{regime}")

    return {
        "symbol": symbol,
        "name": r.get("name", fd.SYMBOLS.get(symbol, {}).get("name", symbol)),
        "group": fd.SYMBOLS.get(symbol, {}).get("group", "未知"),
        "trades": r.get("trades", 0),
        "expR": r.get("expR"),
        "win_rate": r.get("win_rate"),
        "by_regime": r.get("by_regime", {}),
        "exit_reasons": r.get("exit_reasons", {}),
        "signatures": signatures,
        "error": r.get("note", None) if r.get("trades", 0) == 0 else None,
    }


def split_is_oos(df, oos_ratio: float = 0.3):
    """把 DataFrame 切成 IS（前 70%）和 OOS（后 30%）。"""
    n = len(df)
    split_idx = int(n * (1 - oos_ratio))
    return df.iloc[:split_idx], df.iloc[split_idx:]


def calc_signal_agreement(sigs_a: List[str], sigs_b: List[str]) -> float:
    """信号一致率：交集 / 基准集合大小。"""
    if not sigs_a and not sigs_b:
        return 1.0
    if not sigs_a or not sigs_b:
        return 0.0
    set_a = set(sigs_a)
    set_b = set(sigs_b)
    return len(set_a & set_b) / max(len(set_b), 1)


def validate_hypothesis(
    hypo: Hypothesis,
    baseline: Optional[Dict] = None,
    symbols: Optional[List[str]] = None,
    tail: Optional[int] = None,
    oos_ratio: float = 0.3,
) -> Dict[str, Any]:
    """验证一个策略假设。

    Args:
        hypo: 要验证的假设
        baseline: 回归测试基准（从 regression_baseline.json 加载）
        symbols: 测试品种列表（默认 DEFAULT_SYMBOLS）
        tail: 仅用尾部 N 根 K 线（None=全部）
        oos_ratio: OOS 数据占比

    Returns:
        验证结果字典，包含：
        - hypothesis: 假设信息
        - per_symbol: 各品种详细结果
        - summary: 汇总指标
        - verdict: 判定结果（pass / warn / fail）
        - reasons: 判定理由列表
    """
    symbols = symbols or hypo.target_symbols or DEFAULT_SYMBOLS
    base_cfg = fd.DEFAULT_CONFIG
    hypo_cfg = hypo.apply_to_config(base_cfg)

    # ── 1. 全量回测（假设配置）──
    hypo_results = []
    for sym in symbols:
        r = run_backtest(sym, hypo_cfg, tail=tail)
        hypo_results.append(r)

    # ── 2. OOS 验证（最后 oos_ratio 的数据）──
    oos_results = []
    for sym in symbols:
        try:
            df = fd.load_daily(sym)
            if df is None:
                oos_results.append({"symbol": sym, "trades": 0, "expR": None, "note": "无数据"})
                continue
            if tail:
                df = df.tail(tail)
            _, oos_df = split_is_oos(df, oos_ratio)
            if len(oos_df) < 60:
                oos_results.append({"symbol": sym, "trades": 0, "expR": None, "note": "OOS数据不足"})
                continue
            r = run_backtest(sym, hypo_cfg, df_in=oos_df)
            oos_results.append(r)
        except Exception as e:
            oos_results.append({"symbol": sym, "trades": 0, "expR": None, "error": str(e)[:80]})

    # ── 3. 基准对比（如果有基准数据）──
    baseline_syms = {}
    if baseline and "symbols" in baseline:
        baseline_syms = baseline["symbols"]

    # ── 4. 逐品种分析 ──
    per_symbol = []
    for i, sym in enumerate(symbols):
        h = hypo_results[i]
        o = oos_results[i] if i < len(oos_results) else {}
        b = baseline_syms.get(sym, {})

        sym_result = {
            "symbol": sym,
            "name": h.get("name", ""),
            "group": h.get("group", ""),
            "hypothesis": {
                "trades": h.get("trades", 0),
                "expR": h.get("expR"),
                "win_rate": h.get("win_rate"),
                "error": h.get("error"),
            },
            "oos": {
                "trades": o.get("trades", 0),
                "expR": o.get("expR"),
                "win_rate": o.get("win_rate"),
                "error": o.get("error") if "error" in o else o.get("note"),
            },
            "baseline": b,
            "deltas": {},
            "oos_degrade": {},
            "checks": {},
        }

        # 与基准的差异
        if b and b.get("expR") is not None and h.get("expR") is not None:
            sym_result["deltas"]["expR"] = round(h["expR"] - b["expR"], 4)
            sym_result["deltas"]["win_rate"] = round(h["win_rate"] - b["win_rate"], 3)
            sym_result["deltas"]["trades_pct"] = round(
                (h["trades"] - b["trades"]) / max(b["trades"], 1), 2
            )
            sym_result["deltas"]["sig_agreement"] = round(
                calc_signal_agreement(h.get("signatures", []), b.get("signatures", [])), 3
            )

        # IS→OOS 退化
        h_expR = h.get("expR")
        o_expR = o.get("expR")
        if h_expR is not None and o_expR is not None and h_expR > 0:
            sym_result["oos_degrade"]["expR_ratio"] = round(o_expR / h_expR, 3)
            sym_result["oos_degrade"]["expR_drop"] = round((h_expR - o_expR) / h_expR, 3)
        if h.get("win_rate") and o.get("win_rate"):
            sym_result["oos_degrade"]["win_rate_drop"] = round(h["win_rate"] - o["win_rate"], 3)

        # 各项检查
        checks = {}
        # 检查1: 收益为正
        checks["positive_expR"] = h_expR is not None and h_expR > MIN_EXP
        # 检查2: 交易数足够
        checks["enough_trades"] = h.get("trades", 0) >= MIN_TRADES
        # 检查3: OOS 退化程度
        expR_drop = sym_result["oos_degrade"].get("expR_drop")
        if expR_drop is not None:
            if expR_drop >= OOS_FAIL_DEGRADE:
                checks["oos_quality"] = "fail"
            elif expR_drop >= OOS_WARN_DEGRADE:
                checks["oos_quality"] = "warn"
            else:
                checks["oos_quality"] = "ok"
        else:
            checks["oos_quality"] = "unknown"
        # 检查4: 信号一致率（vs 基准）
        sig_agree = sym_result["deltas"].get("sig_agreement")
        if sig_agree is not None:
            if sig_agree < SIG_AGREE_FAIL:
                checks["signal_consistency"] = "fail"
            elif sig_agree < SIG_AGREE_WARN:
                checks["signal_consistency"] = "warn"
            else:
                checks["signal_consistency"] = "ok"
        else:
            checks["signal_consistency"] = "unknown"
        # 检查5: 交易数变化幅度
        trades_pct = abs(sym_result["deltas"].get("trades_pct", 0))
        if trades_pct > TRADE_CHANGE_FAIL:
            checks["trade_volume_stable"] = "fail"
        elif trades_pct > TRADE_CHANGE_WARN:
            checks["trade_volume_stable"] = "warn"
        else:
            checks["trade_volume_stable"] = "ok"

        sym_result["checks"] = checks
        per_symbol.append(sym_result)

    # ── 5. 汇总 + 综合判定 ──
    valid_syms = [s for s in per_symbol if s["hypothesis"]["expR"] is not None]
    n_valid = len(valid_syms)

    summary = {
        "total_symbols": len(symbols),
        "valid_symbols": n_valid,
        "avg_expR": round(sum(s["hypothesis"]["expR"] for s in valid_syms) / max(n_valid, 1), 4) if n_valid else None,
        "avg_win_rate": round(sum(s["hypothesis"]["win_rate"] for s in valid_syms) / max(n_valid, 1), 3) if n_valid else None,
        "total_trades": sum(s["hypothesis"]["trades"] for s in per_symbol),
        "positive_count": sum(1 for s in valid_syms if s["hypothesis"]["expR"] > 0),
    }

    # OOS 汇总
    oos_valid = [s for s in per_symbol if s["oos"].get("expR") is not None]
    if oos_valid:
        summary["oos_avg_expR"] = round(
            sum(s["oos"]["expR"] for s in oos_valid) / len(oos_valid), 4
        )
        # 平均退化率（只算 IS 有正收益的）
        degrade_rates = [
            s["oos_degrade"]["expR_drop"]
            for s in valid_syms
            if s["hypothesis"]["expR"] > 0 and "expR_drop" in s["oos_degrade"]
        ]
        if degrade_rates:
            summary["avg_oos_degrade"] = round(sum(degrade_rates) / len(degrade_rates), 3)

    # 综合判定
    fails = 0
    warns = 0
    reasons = []

    for s in per_symbol:
        for check_name, status in s["checks"].items():
            if status == "fail":
                fails += 1
                reasons.append(f"{s['symbol']} {check_name}=FAIL")
            elif status == "warn":
                warns += 1

    if n_valid == 0:
        verdict = "fail"
        reasons.append("没有有效回测结果")
    elif fails > 0:
        verdict = "fail"
    elif warns >= 3:
        verdict = "warn"
    elif summary.get("avg_expR", 0) <= 0:
        verdict = "fail"
        reasons.append("平均 expR ≤ 0")
    elif summary.get("positive_count", 0) < n_valid * 0.5:
        verdict = "warn"
        reasons.append(f"正收益品种不足一半（{summary['positive_count']}/{n_valid}）")
    else:
        verdict = "pass"

    return {
        "hypothesis": hypo.to_dict(),
        "per_symbol": per_symbol,
        "summary": summary,
        "verdict": verdict,
        "reasons": reasons,
        "oos_ratio": oos_ratio,
        "tail_bars": tail,
    }


def print_validation_result(result: Dict, verbose: bool = True):
    """漂亮地打印验证结果。"""
    hypo = result["hypothesis"]
    summary = result["summary"]
    verdict = result["verdict"]

    verdict_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(verdict, "?")
    verdict_color = {"pass": "92", "warn": "93", "fail": "91"}.get(verdict, "0")

    print()
    print("=" * 90)
    print(f"  策略假设验证: {hypo['name']}")
    print(f"  来源: {hypo['source']}  |  品种数: {summary['total_symbols']}  |  有效: {summary['valid_symbols']}")
    print("=" * 90)

    if verbose:
        print(f"  {'品种':<6}{'分组':<6}{'expR':>8}{'ΔexpR':>8}{'胜率':>8}{'Δ胜率':>8}"
              f"{'交易数':>7}{'Δ笔数':>7}{'信号一致':>8}{'OOS退化':>8}{'状态':>6}")
        print("  " + "─" * 86)

        for s in result["per_symbol"]:
            h = s["hypothesis"]
            d = s["deltas"]
            checks = s["checks"]

            sym = s["symbol"]
            group = s["group"]
            expR = f"{h['expR']:+.3f}" if h["expR"] is not None else "   N/A"
            d_expR = f"{d.get('expR', 0):+.3f}" if "expR" in d else "   N/A"
            wr = f"{h['win_rate']*100:.1f}%" if h.get("win_rate") else "  N/A"
            d_wr = f"{d.get('win_rate', 0)*100:+.1f}%" if "win_rate" in d else "  N/A"
            tr = str(h["trades"])
            d_tr = f"{d.get('trades_pct', 0)*100:+.0f}%" if "trades_pct" in d else " N/A"
            sig = f"{d.get('sig_agreement', 0)*100:.0f}%" if "sig_agreement" in d else " N/A"
            oos_d = f"{s['oos_degrade'].get('expR_drop', 0)*100:.0f}%" if "expR_drop" in s["oos_degrade"] else " N/A"

            # 状态图标
            worst = "ok"
            for status in checks.values():
                if status == "fail":
                    worst = "fail"
                    break
                elif status == "warn" and worst != "fail":
                    worst = "warn"
            status_icon = {"ok": "✅", "warn": "⚠️", "fail": "❌", "unknown": "❓"}.get(worst, "?")

            print(f"  {sym:<6}{group:<6}{expR:>8}{d_expR:>8}{wr:>8}{d_wr:>8}"
                  f"{tr:>7}{d_tr:>7}{sig:>8}{oos_d:>8}{status_icon:>6}")

    print("  " + "─" * 86)
    avg_exp = summary.get("avg_expR")
    avg_wr = summary.get("avg_win_rate")
    print(f"  {'加权平均':<12}"
          f"{f'{avg_exp:+.3f}':>14}"
          f"{f'{avg_wr*100:.1f}%':>16}"
          f"{str(summary['total_trades']):>15}"
          f"{summary.get('positive_count', 0)}/{summary['valid_symbols']}正收益")

    if "oos_avg_expR" in summary:
        oos_exp_str = f"{summary['oos_avg_expR']:+.3f}"
        print(f"  {'OOS平均':<12}"
              f"{oos_exp_str:>14}"
              f"  平均退化: {summary.get('avg_oos_degrade', 0)*100:.0f}%")

    print()
    print(f"  判定: {verdict_icon}  \033[{verdict_color}m{verdict.upper()}\033[0m", end="")
    if result["reasons"]:
        print(f"  ({len(result['reasons'])} 个问题)")
        for r in result["reasons"][:5]:
            print(f"    · {r}")
        if len(result["reasons"]) > 5:
            print(f"    · ... 还有 {len(result['reasons'])-5} 个")
    else:
        print()
    print("=" * 90)
    print()
