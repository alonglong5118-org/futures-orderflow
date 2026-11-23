#!/usr/bin/env python3
"""
风控模型升级 · 上线前验收 + 回测对比脚本
=================================================================
视角：StrategyBacktestExpert（样本外 / 对抗性 / 回归）
必须在【真实运行环境】执行：含 numpy / pandas / tqsdk / akshare 及历史行情数据。

两种模式：
  [默认] 在线模式   —— 读取真实账户持仓 + 真实行情（需交易时段 / 历史数据缓存）
  [--offline] 离线模式 —— monkey-patch 合成日线 + 注入合成持仓，
                          验证代码路径与历史模拟+EVT+缓存逻辑自洽。
                          ⚠️ 离线数值仅验证「逻辑正确」，不代表真实行情结论。

四大验收项：
  A. 烟雾测试        —— import four_dim_live_runner，调 portfolio_var() 不崩、返回结构正确
  B. 新旧方法对比    —— var_method=hist(新) vs param(旧) 下 var_95_pct / var_99_pct 数值平移
  C. 协方差缓存复验  —— ②高频逐仓调用耗时不随候选线性增长
  D. 回测对比指引    —— 切换 var_method 前后各跑 four_dim_oos_compare.py / montecarlo.py 对比

设计原则：
  - 任何一步失败都不应中断整体验收（逐步降级、明确 WARN/FAIL）。
  - 脚本结束后会把 var_method 复位为 "hist"（升级后的默认新法）。
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "trade_config.json")


def _load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def _set_method(m):
    c = _load_cfg()
    c["var_method"] = m
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def _hr(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


# ───────────────────────── 离线模式：合成数据 ─────────────────────────
def _build_synth(seed=20260816):
    """构造确定性合成日线：3 品种、320 交易日、含市场共同因子(相关性)+厚尾噪声。
    返回 (synth_df_dict, symbols)。固定 seed 保证可复现。"""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    days = 320
    syms = ["FG", "SA", "JM"]
    market = rng.standard_normal(days)  # 共同市场因子 → 品种相关性
    scales = {"FG": 0.012, "SA": 0.014, "JM": 0.016}  # 个别波动尺度
    synth = {}
    for s in syms:
        z = rng.standard_normal(days)
        tail = rng.standard_normal(days) * 3.2  # 重尾成分
        mask = rng.random(days) < 0.10  # 10% 概率厚尾
        noise = np.where(mask, tail, z)
        r = 0.35 * market + scales[s] * noise
        price = 1000.0
        series = []
        for rr in r:
            price *= 1.0 + rr
            series.append(price)
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=days, freq="B")
        synth[s] = pd.DataFrame({"close": series}, index=idx)
    return synth, syms


def _offline_setup():
    """返回 (monkey-patched module, positions 注入字典)。"""
    import four_dim_live_runner as R
    import four_dim_live_runner as R2  # noqa

    synth, _ = _build_synth()
    R.load_daily_refreshed = lambda sym, ttl=1800: synth.get(sym)
    POS = {
        "FG": {"lots": 5, "direction": "多", "price": 1500.0, "avg": 1500.0},
        "SA": {"lots": 8, "direction": "多", "price": 1900.0, "avg": 1900.0},
        "JM": {"lots": 3, "direction": "空", "price": 1600.0, "avg": 1600.0},
    }
    return R, POS


# ───────────────────────── 验收主体 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="离线模式：合成数据验证代码路径")
    args = ap.parse_args()
    offline = args.offline

    _hr("A. 烟雾测试 (var_method=hist)" + (" [离线合成]" if offline else ""))
    try:
        import four_dim_live_runner as R
    except ImportError as e:
        print("IMPORT_FAIL:", e)
        print("请在真实运行环境（含 numpy/pandas/tqsdk/akshare）执行本脚本。")
        sys.exit(2)

    POS = None
    if offline:
        R, POS = _offline_setup()
        print("离线模式：已 monkey-patch load_daily_refreshed → 合成日线(320日,3品种,含厚尾)；")
        print("          已注入合成持仓 {FG:多5手, SA:多8手, JM:空3手}。")

    try:
        v = R.portfolio_var(force=True, positions=POS)
    except Exception as e:
        print("SMOKE_FAIL:", repr(e))
        import traceback

        traceback.print_exc()
        sys.exit(1)

    print("ok        =", v.get("ok"))
    print("reason    =", v.get("reason"))
    print("method    =", v.get("method"))
    if not v.get("ok"):
        if offline:
            print("WARN: 离线合成数据仍无法计算组合 VaR —— 代码路径可能存在缺陷，需排查。")
        else:
            print("WARN: portfolio_var 未返回数据（可能无持仓/无行情）。")
            print("      烟雾测试无法完整验证；请在交易时段或含历史数据环境重跑。")
            print("      以下 B/C 项已跳过。D 项（回测对比）仍可按指引手动执行。")
        return

    print("var_95_pct   =", v.get("var_95_pct"))
    print("var_99_pct   =", v.get("var_99_pct"))
    print("cvar_95_pct  =", v.get("cvar_95_pct"))
    print("var_95_pct_hist (纯历史) =", v.get("var_95_pct_hist"))
    print("sample_days  =", v.get("sample_days"))
    print("n_positions  =", v.get("n_positions"))
    print("evt          =", v.get("evt"))
    print("contrib_var95 keys =", list((v.get("contrib_var95") or {}).keys()))
    needed = [
        "var_95",
        "var_99",
        "cvar_95",
        "cvar_99",
        "var_95_pct",
        "var_99_pct",
        "cvar_95_pct",
        "cvar_99_pct",
        "contrib_var95",
    ]
    missing = [k for k in needed if k not in v]
    print("结构完整性:", "OK" if not missing else f"缺键 {missing}")

    _hr("B. 新旧方法对比 (hist 新 vs param 旧)" + (" [离线合成]" if offline else ""))
    _set_method("param")
    vp = R.portfolio_var(force=True, positions=POS)
    _set_method("hist")
    vh = R.portfolio_var(force=True, positions=POS)
    _set_method("hist")  # 复位默认新法

    p95 = vp.get("var_95_pct") or 0.0
    h95 = vh.get("var_95_pct") or 0.0
    p99 = vp.get("var_99_pct") or 0.0
    h99 = vh.get("var_99_pct") or 0.0
    print(f"param   var_95_pct = {p95:.2f}%   var_99_pct = {p99:.2f}%")
    print(f"hist    var_95_pct = {h95:.2f}%   var_99_pct = {h99:.2f}%")
    print(f"平移    95%: {h95 - p95:+.2f}个百分点   99%: {h99 - p99:+.2f}个百分点")
    print("（历史模拟+EVT 通常比正态假设略高，属预期；若偏离 >2pp 需复核阈值/窗口）")

    cap = _load_cfg().get("portfolio_var_pct_cap", 3.3)
    margin = cap - h95
    print(f"\nportfolio_var_pct_cap = {cap:.2f}%")
    print(f"新法 var_95_pct 余量 = {margin:+.2f}个百分点")
    if h95 > cap:
        print("⚠️ 新法 VaR 已突破 cap → VaR 预交易闸会更频繁拦截新增持仓。")
        print("   建议：(a) 复校 cap（如 3.3→4.0），或 (b) 调高 var_evt_threshold_q（尾部更保守）。")
    else:
        print("✅ 新法 var_95_pct 仍在 cap 内，闸触发频率预计无明显变化。")

    evh95 = vh.get("var_95_pct_hist")
    if evh95 is not None:
        print(f"\n内部对照：纯历史 var_95_pct = {evh95:.2f}%  vs  取严后 {h95:.2f}%  → EVT 加厚 {h95 - evh95:+.2f}pp")

    _hr("C. 协方差缓存复验 (②)" + (" [离线合成]" if offline else ""))
    t0 = time.time()
    R.portfolio_var(force=False, positions=POS)
    t1 = time.time()
    R.portfolio_var(force=False, positions=POS)
    R.portfolio_var(force=False, positions=POS)
    t2 = time.time()
    first = t1 - t0
    cached = (t2 - t1) / 2.0
    print(f"首次(含载数) {first * 1000:.1f}ms   后续(应命中缓存) 均 {cached * 1000:.1f}ms")
    if cached < first:
        print("✅ 缓存生效（命中后明显更快）。")
    else:
        print("⚠️ 未观测到缓存加速，检查 _VAR_DATA_CACHE / _VAR_RETS_CACHE 是否命中。")

    _hr("D. 回测对比（可选 · 重活）")
    print("如需样本外对比新旧 VaR 方法下组合表现，分别执行：")
    print("  [旧法] 改 trade_config.json: var_method='param'  →  python four_dim_oos_compare.py")
    print("  [新法] 改 trade_config.json: var_method='hist'  →  python four_dim_oos_compare.py")
    print("  [压力] python montecarlo.py   # 蒙特卡洛极端情景穿透测试")
    print("对比指标：样本外 Sharpe / 最大回撤 / VaR 闸拦截次数 / 爆仓笔数。")

    _hr("验收结论")
    print("A 烟雾测试:", "PASS" if v.get("ok") else "SKIP(无数据)")
    print("B 新旧对比:", "已输出平移量，请人工核对余量")
    print("C 缓存复验:", "PASS" if cached < first else "WARN")
    print("D 回测对比: 按指引手动执行")
    if offline:
        print("\n⚠️ 离线模式仅验证「代码路径 + 逻辑自洽」，合成数据≠真实行情；")
        print("   数值结论须待在线模式(交易时段/真实历史数据)复核后生效。")
    print("\nvar_method 已复位为 'hist'（升级后默认）。")


if __name__ == "__main__":
    main()
