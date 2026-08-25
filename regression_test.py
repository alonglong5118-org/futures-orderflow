#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四维策略自动回归测试
====================
每次改完代码跑一下，对比基准版本，快速知道有没有改坏。

用法:
  python regression_test.py                    # 完整回归测试
  python regression_test.py --tail 100         # 只跑尾部 100 根（更快）
  python regression_test.py --symbols cu,rb,M  # 指定品种
  python regression_test.py --summary          # 只看汇总
  python regression_test.py --update-baseline --version v6.1  # 更新基准
"""

import sys
import os
import json
import argparse
from datetime import datetime

# ── 路径 ──────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import four_dim_strategy as fd


# ── 配置 ──────────────────────────────────────────────────────────────────
BASELINE_FILE = os.path.join(HERE, "regression_baseline.json")

# 8 个基准品种（覆盖全品类，平衡速度与覆盖度）
DEFAULT_SYMBOLS = [
    "cu",   # 有色 · 稳健
    "rb",   # 黑系 · 活跃
    "JM",   # 黑系 · 低胜率（边缘案例）
    "i",    # 黑系 · 中等表现
    "m",    # 农产品 · 季节性强（豆粕）
    "y",    # 农产品 · 稳健（豆油）
    "pp",   # 化工 · 代表（聚丙烯）
    "TA",   # 化工 · 交叉验证（PTA）
]

# 判定阈值
WARN_EXPR_DELTA = 0.015
CRIT_EXPR_DELTA = 0.030
WARN_WIN_DELTA = 0.03
CRIT_WIN_DELTA = 0.06
WARN_TRADES_PCT = 0.15
CRIT_TRADES_PCT = 0.30
WARN_SIG_AGREE = 0.95
CRIT_SIG_AGREE = 0.90

# 默认 tail（尾部 N 根日线，~1 年）
DEFAULT_TAIL = 250

# 信号签名保留笔数（前 N 笔用于一致性检查）
SIGNATURE_TRADE_COUNT = 50


# ── 颜色 ──────────────────────────────────────────────────────────────────
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREY = "\033[90m"


def color(text, *codes):
    return "".join(codes) + text + C.RESET


# ── 核心函数 ──────────────────────────────────────────────────────────────
def run_backtest_for_symbol(symbol, tail=DEFAULT_TAIL, cfg=None):
    """跑单个品种的 walk-forward 回测，返回标准化结果。"""
    if cfg is None:
        cfg = fd.DEFAULT_CONFIG
    try:
        r = fd.walk_forward_backtest(symbol, cfg, tail=tail)
    except Exception as e:
        return {
            "symbol": symbol,
            "name": fd.SYMBOLS.get(symbol, {}).get("name", symbol),
            "group": fd.SYMBOLS.get(symbol, {}).get("group", "未知"),
            "trades": 0,
            "expR": None,
            "win_rate": None,
            "by_regime": {},
            "signatures": [],
            "error": str(e)[:80],
        }

    trades_detail = r.get("trades_detail", [])
    # 生成信号签名：(日期, 方向, regime)
    signatures = []
    for t in trades_detail[:SIGNATURE_TRADE_COUNT]:
        date_str = t.get("date", "")
        direction = t.get("direction", "")
        regime = t.get("regime", "")
        sig = f"{date_str}_{direction}_{regime}"
        signatures.append(sig)

    return {
        "symbol": symbol,
        "name": r.get("name", fd.SYMBOLS.get(symbol, {}).get("name", symbol)),
        "group": fd.SYMBOLS.get(symbol, {}).get("group", "未知"),
        "trades": r.get("trades", 0),
        "expR": r.get("expR"),
        "win_rate": r.get("win_rate"),
        "by_regime": r.get("by_regime", {}),
        "signatures": signatures,
        "error": None,
    }


def calc_signal_agreement(current_sigs, baseline_sigs):
    """计算信号一致率：两个签名列表的交集大小 / 并集大小（Jaccard 近似）。"""
    if not current_sigs and not baseline_sigs:
        return 1.0
    if not current_sigs or not baseline_sigs:
        return 0.0
    set_cur = set(current_sigs)
    set_base = set(baseline_sigs)
    intersection = set_cur & set_base
    union = set_cur | set_base
    # 用较小集合的大小做分母（更宽松，关注"有没有"而不是"精确多少"）
    return len(intersection) / max(len(set_base), 1)


def classify_status(expr_delta, win_delta, trades_pct_delta, sig_agree):
    """根据 4 个维度判定状态：'ok' / 'warn' / 'critical' / 'error'。"""
    criticals = 0
    warns = 0

    if expr_delta is not None:
        if abs(expr_delta) > CRIT_EXPR_DELTA:
            criticals += 1
        elif abs(expr_delta) > WARN_EXPR_DELTA:
            warns += 1

    if win_delta is not None:
        if abs(win_delta) > CRIT_WIN_DELTA:
            criticals += 1
        elif abs(win_delta) > WARN_WIN_DELTA:
            warns += 1

    if trades_pct_delta is not None:
        if abs(trades_pct_delta) > CRIT_TRADES_PCT:
            criticals += 1
        elif abs(trades_pct_delta) > WARN_TRADES_PCT:
            warns += 1

    if sig_agree is not None:
        if sig_agree < CRIT_SIG_AGREE:
            criticals += 1
        elif sig_agree < WARN_SIG_AGREE:
            warns += 1

    if criticals > 0:
        return "critical", criticals, warns
    if warns > 0:
        return "warn", criticals, warns
    return "ok", criticals, warns


def load_baseline():
    """加载基准数据。"""
    if not os.path.exists(BASELINE_FILE):
        return None
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_baseline(results, version, tail):
    """保存基准数据。"""
    baseline = {
        "version": version,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tail_bars": tail,
        "symbols": {},
        "summary": {},
    }
    total_trades = 0
    sum_expr = 0.0
    sum_win = 0.0
    valid_count = 0

    for r in results:
        sym = r["symbol"]
        baseline["symbols"][sym] = {
            "name": r["name"],
            "group": r["group"],
            "trades": r["trades"],
            "expR": r["expR"],
            "win_rate": r["win_rate"],
            "by_regime": r["by_regime"],
            "signatures": r["signatures"],
        }
        if r["expR"] is not None and r["trades"] > 0:
            sum_expr += r["expR"]
            sum_win += r["win_rate"]
            total_trades += r["trades"]
            valid_count += 1

    if valid_count > 0:
        baseline["summary"] = {
            "avg_expR": round(sum_expr / valid_count, 4),
            "avg_win_rate": round(sum_win / valid_count, 4),
            "total_trades": total_trades,
            "valid_symbols": valid_count,
        }

    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    return baseline


# ── 输出 ──────────────────────────────────────────────────────────────────
def print_header(baseline, tail, symbols):
    print()
    print(color("=" * 80, C.BOLD, C.CYAN))
    title = "  四维策略回归测试"
    if baseline:
        title += f"  vs  基准 ({baseline.get('version', 'unknown')})"
    else:
        title += "  (无基准，仅展示当前值)"
    print(color(title, C.BOLD, C.CYAN))
    info = (f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            f"  |  品种数: {len(symbols)}"
            f"  |  尾部: {tail} bars")
    print(color(info, C.DIM))
    print(color("=" * 80, C.BOLD, C.CYAN))
    print()


def print_table_header():
    hdr = (f"{'品种':<5} {'分组':<6} {'expR':>8} {'ΔexpR':>8} "
           f"{'胜率':>7} {'Δ胜率':>7} {'交易数':>6} {'Δ笔数':>6} "
           f"{'信号一致':>8}  状态")
    print(color(hdr, C.BOLD))
    print(color("─" * 80, C.DIM))


def fmt_delta(value, fmt, warn_thresh, crit_thresh, is_pct=False):
    """格式化带颜色的 delta 值。"""
    if value is None:
        return color("   N/A  ", C.GREY)
    if is_pct:
        text = fmt.format(value * 100)
    else:
        text = fmt.format(value)
    if value > 0:
        text = "+" + text
    abs_val = abs(value)
    if abs_val > crit_thresh:
        return color(text, C.BOLD, C.RED)
    if abs_val > warn_thresh:
        return color(text, C.YELLOW)
    return color(text, C.GREEN)


def print_result_row(r, baseline_sym):
    """打印单个品种的结果行。"""
    sym = r["symbol"]
    name = r["name"]
    group = r["group"]

    if r.get("error"):
        err = r["error"][:30]
        print(f"{sym:<5} {group:<6} {color('ERROR', C.RED):>8} "
              f"{'':>8} {'':>7} {'':>7} {'':>6} {'':>6} {'':>8}  {err}")
        return "error"

    # 当前值
    expR = r["expR"]
    win_rate = r["win_rate"]
    trades = r["trades"]

    # 基准值 & delta
    base_expR = baseline_sym.get("expR") if baseline_sym else None
    base_win = baseline_sym.get("win_rate") if baseline_sym else None
    base_trades = baseline_sym.get("trades") if baseline_sym else None
    base_sigs = baseline_sym.get("signatures", []) if baseline_sym else []

    expr_delta = (expR - base_expR) if (expR is not None and base_expR is not None) else None
    win_delta = (win_rate - base_win) if (win_rate is not None and base_win is not None) else None
    trades_delta = (trades - base_trades) if (trades is not None and base_trades) else None
    trades_pct_delta = (trades_delta / base_trades) if (trades_delta is not None and base_trades) else None

    sig_agree = None
    if baseline_sym:
        sig_agree = calc_signal_agreement(r["signatures"], base_sigs)

    # 状态判定
    status, crits, warns = classify_status(expr_delta, win_delta, trades_pct_delta, sig_agree)

    # 格式化
    expR_str = f"{expR:+.3f}" if expR is not None else "  N/A "
    if expR is not None and expR > 0:
        expR_str = color(expR_str, C.GREEN)
    elif expR is not None and expR < 0:
        expR_str = color(expR_str, C.RED)

    win_str = f"{win_rate*100:>5.1f}%" if win_rate is not None else "  N/A "
    if win_rate is not None and win_rate >= 0.45:
        win_str = color(win_str, C.GREEN)
    elif win_rate is not None and win_rate < 0.35:
        win_str = color(win_str, C.RED)

    trades_str = f"{trades:>4}" if trades is not None else " N/A"

    expr_d_str = fmt_delta(expr_delta, "{:.3f}", WARN_EXPR_DELTA, CRIT_EXPR_DELTA) if baseline_sym else color("    -   ", C.GREY)
    win_d_str = fmt_delta(win_delta, "{:.1f}%", WARN_WIN_DELTA, CRIT_WIN_DELTA, is_pct=True) if baseline_sym else color("   -   ", C.GREY)

    if trades_delta is not None and baseline_sym:
        td_text = f"{trades_delta:+d}"
        if abs(trades_pct_delta) > CRIT_TRADES_PCT:
            td_str = color(td_text, C.BOLD, C.RED)
        elif abs(trades_pct_delta) > WARN_TRADES_PCT:
            td_str = color(td_text, C.YELLOW)
        else:
            td_str = color(td_text, C.GREEN)
    else:
        td_str = color("  -  ", C.GREY)

    if sig_agree is not None:
        sa_text = f"{sig_agree*100:>5.1f}%"
        if sig_agree < CRIT_SIG_AGREE:
            sa_str = color(sa_text, C.BOLD, C.RED)
        elif sig_agree < WARN_SIG_AGREE:
            sa_str = color(sa_text, C.YELLOW)
        else:
            sa_str = color(sa_text, C.GREEN)
    else:
        sa_str = color("   -  ", C.GREY)

    if status == "ok":
        status_str = color(" ✅ ", C.GREEN)
    elif status == "warn":
        status_str = color(" ⚠️ ", C.YELLOW)
    else:
        status_str = color(" ❌ ", C.RED)

    print(f"{sym:<5} {group:<6} {expR_str:>8} {expr_d_str:>8} "
          f"{win_str:>7} {win_d_str:>7} {trades_str:>6} {td_str:>6} "
          f"{sa_str:>8} {status_str}")

    return status


def print_summary(results, baseline):
    """打印汇总。"""
    print(color("─" * 80, C.DIM))

    # 计算加权平均
    valid = [r for r in results if r.get("expR") is not None and r["trades"] > 0]
    if not valid:
        print(color("  无有效数据", C.RED))
        return

    total_trades = sum(r["trades"] for r in valid)
    avg_expR = sum(r["expR"] for r in valid) / len(valid)
    avg_win = sum(r["win_rate"] for r in valid) / len(valid)

    base_summary = baseline.get("summary", {}) if baseline else {}
    base_expR = base_summary.get("avg_expR")
    base_win = base_summary.get("avg_win_rate")
    base_trades = base_summary.get("total_trades")

    expr_d = (avg_expR - base_expR) if (base_expR is not None) else None
    win_d = (avg_win - base_win) if (base_win is not None) else None
    trades_d = (total_trades - base_trades) if (base_trades is not None) else None

    expR_str = f"{avg_expR:+.4f}"
    win_str = f"{avg_win*100:.1f}%"

    expr_d_str = fmt_delta(expr_d, "{:.4f}", WARN_EXPR_DELTA, CRIT_EXPR_DELTA) if baseline else color("      -     ", C.GREY)
    win_d_str = fmt_delta(win_d, "{:.1f}%", WARN_WIN_DELTA, CRIT_WIN_DELTA, is_pct=True) if baseline else color("   -     ", C.GREY)

    print(f"  加权平均              {expR_str:>8} {expr_d_str:>8} "
          f"{win_str:>7} {win_d_str:>7} {total_trades:>6} "
          f"{'':>6} {'':>8}")
    print()


def print_verdict(results, baseline):
    """打印最终判定。"""
    ok_count = 0
    warn_count = 0
    crit_count = 0
    err_count = 0

    for r in results:
        status = r.get("_status", "ok")
        if status == "ok":
            ok_count += 1
        elif status == "warn":
            warn_count += 1
        elif status == "critical":
            crit_count += 1
        elif status == "error":
            err_count += 1

    if crit_count > 0 or err_count > 0:
        verdict = color("❌ 失败", C.BOLD, C.RED)
    elif warn_count > 2:
        verdict = color("⚠️  警告", C.BOLD, C.YELLOW)
    else:
        verdict = color("✅ 通过", C.BOLD, C.GREEN)

    print(f"  判定: {verdict}  "
          f"({crit_count} 个严重异常, {warn_count} 个警告, {err_count} 个错误)")
    print()
    print(color("  · 严重异常: 信号一致率 < 90% 或 expR 变化 > 0.03 或 胜率变化 > 6% 或 交易数变化 > 30%", C.DIM))
    print(color("  · 警告:     信号一致率 < 95% 或 expR 变化 > 0.015 或 胜率变化 > 3% 或 交易数变化 > 15%", C.DIM))
    print(color("=" * 80, C.BOLD, C.CYAN))
    print()

    return crit_count == 0 and err_count == 0


# ── 主流程 ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="四维策略自动回归测试")
    parser.add_argument("--tail", type=int, default=DEFAULT_TAIL,
                        help=f"回测尾部 N 根日线（默认 {DEFAULT_TAIL}）")
    parser.add_argument("--symbols", type=str, default=None,
                        help="指定品种，逗号分隔（如 cu,rb,M）")
    parser.add_argument("--update-baseline", action="store_true",
                        help="更新基准数据")
    parser.add_argument("--version", type=str, default="dev",
                        help="基准版本标签（配合 --update-baseline 使用）")
    parser.add_argument("--summary", action="store_true",
                        help="只看汇总（简洁输出）")
    args = parser.parse_args()

    # 确定测试品种
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = DEFAULT_SYMBOLS

    # 加载基准
    baseline = load_baseline()

    # 打印表头
    if not args.summary:
        print_header(baseline, args.tail, symbols)
        print_table_header()

    # 逐个品种跑回测
    results = []
    for i, sym in enumerate(symbols):
        if sym not in fd.SYMBOLS:
            print(f"  {sym:<5} 品种不存在，跳过")
            continue

        r = run_backtest_for_symbol(sym, tail=args.tail)

        # 判定状态
        baseline_sym = baseline["symbols"].get(sym) if baseline else None
        if r.get("error"):
            r["_status"] = "error"
        elif baseline_sym:
            expr_d = r["expR"] - baseline_sym["expR"] if (r["expR"] is not None and baseline_sym.get("expR") is not None) else None
            win_d = r["win_rate"] - baseline_sym["win_rate"] if (r["win_rate"] is not None and baseline_sym.get("win_rate") is not None) else None
            base_trades = baseline_sym.get("trades", 0)
            trades_pct_d = (r["trades"] - base_trades) / base_trades if base_trades else None
            sig_agree = calc_signal_agreement(r["signatures"], baseline_sym.get("signatures", []))
            status, _, _ = classify_status(expr_d, win_d, trades_pct_d, sig_agree)
            r["_status"] = status
        else:
            r["_status"] = "ok"

        results.append(r)

        if not args.summary:
            print_result_row(r, baseline_sym)

    # 汇总
    print_summary(results, baseline)

    # 判定
    passed = print_verdict(results, baseline)

    # 更新基准
    if args.update_baseline:
        baseline = save_baseline(results, args.version, args.tail)
        ver_label = args.version if args.version.startswith("v") else "v" + args.version
        print(color(f"  ✅ 基准已更新 → {ver_label}（{len(results)} 个品种）", C.GREEN))
        print(f"     文件: {BASELINE_FILE}")
        print()

    # 退出码：失败返回 1（方便 CI/hook 使用）
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
