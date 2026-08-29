"""
黑系策略重构 - 多策略原型走步法验证

诊断结论：
- 黑系平均expR +0.24（尚可），但胜率仅35%（偏低）
- 过渡市是最大短板（3品种全部亏损）
- 各品种regime表现极不一致（rb震荡+2.46 vs hc震荡-1.08）
- 止损占比64.5%，靠少数大赚维持高盈亏比

策略原型：
1. 默认策略（基准）
2. 保守型：提高T阈值 + 收紧止损，过滤低质量信号
3. 趋势强化：趋势市放大F权重，波动/震荡市提高T阈值
4. 过渡市回避：过渡市大幅提高T阈值或直接不开仓
5. F主导型：提高F权重（基本面驱动，黑系基本面强）
6. 低止损高盈亏比：stop*1.3 + rr*1.5（扩大止损，拿住大行情）
"""

import copy
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS, load_daily, walk_forward_backtest

HEI_SYMBOLS = ["i", "rb", "hc"]


def make_strategy(strategy_name):
    """生成策略配置"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if strategy_name == "默认策略":
        pass  # 保持默认

    elif strategy_name == "保守型":
        # 提高T阈值 + 收紧止损，过滤低质量信号
        cfg["regime_coef"]["趋势"]["T"] = 1.1
        cfg["regime_coef"]["趋势"]["stop"] = 0.85
        cfg["regime_coef"]["震荡"]["T"] = 1.4
        cfg["regime_coef"]["震荡"]["stop"] = 0.85
        cfg["regime_coef"]["波动"]["T"] = 1.3
        cfg["regime_coef"]["波动"]["stop"] = 0.80

    elif strategy_name == "趋势强化":
        # 趋势市增强F权重 + 放宽止损；震荡/波动市提高T阈值
        cfg["combine_weights"] = {"T": 0.5, "F": 0.35, "C": 0.15}
        cfg["regime_coef"]["趋势"]["T"] = 0.85
        cfg["regime_coef"]["趋势"]["stop"] = 1.2
        cfg["regime_coef"]["震荡"]["T"] = 1.3
        cfg["regime_coef"]["震荡"]["stop"] = 0.9
        cfg["regime_coef"]["波动"]["T"] = 1.2
        cfg["regime_coef"]["波动"]["stop"] = 0.85

    elif strategy_name == "波动市回避":
        # 波动市大幅提高T阈值 + 收紧止损（对应原"过渡市"概念）
        cfg["regime_coef"]["波动"]["T"] = 1.8
        cfg["regime_coef"]["波动"]["stop"] = 0.7
        # 其他市微调
        cfg["regime_coef"]["趋势"]["T"] = 0.9
        cfg["regime_coef"]["震荡"]["T"] = 1.1

    elif strategy_name == "F主导型":
        # 提高F权重（基本面驱动型，黑系基本面信息强）
        cfg["combine_weights"] = {"T": 0.45, "F": 0.40, "C": 0.15}
        # 同向确认阈值降低（更容易触发）
        cfg["bias_synthesis"]["fc_confirm"] = 15
        cfg["bias_synthesis"]["confirm_relief"] = 0.75

    elif strategy_name == "宽止损高盈亏比":
        # 扩大止损 + 提高止盈目标，拿住大行情
        cfg["risk_gate"]["stop_atr_mult"] = 2.0
        cfg["risk_gate"]["rr_ratio"] = 3.0
        # 清除逐品种覆盖，用全局值
        for sym in cfg["per_symbol_risk"]:
            if "stop_atr_mult" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["stop_atr_mult"]
            if "rr_ratio" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["rr_ratio"]

    elif strategy_name == "高门槛+宽止损":
        # 组合策略：提高T门槛 + 宽止损 + 高RR
        # 思路：减少交易次数，只做高质量信号，拿住大行情
        cfg["regime_coef"]["趋势"]["T"] = 1.15
        cfg["regime_coef"]["趋势"]["stop"] = 1.3
        cfg["regime_coef"]["震荡"]["T"] = 1.3
        cfg["regime_coef"]["震荡"]["stop"] = 1.2
        cfg["regime_coef"]["波动"]["T"] = 1.6
        cfg["regime_coef"]["波动"]["stop"] = 1.0

        cfg["risk_gate"]["stop_atr_mult"] = 2.0
        cfg["risk_gate"]["rr_ratio"] = 3.0
        for sym in cfg["per_symbol_risk"]:
            if "stop_atr_mult" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["stop_atr_mult"]
            if "rr_ratio" in cfg["per_symbol_risk"][sym]:
                del cfg["per_symbol_risk"][sym]["rr_ratio"]

    return cfg


def walk_forward_validate(symbol, cfg, n_folds=5):
    """走步法验证：n折滚动OOS"""
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    n = len(df)
    fold_size = n // (n_folds + 1)  # 每折的OOS大小

    oos_expRs = []
    train_expRs = []
    all_nt = []

    for fold in range(n_folds):
        oos_end = n - fold * fold_size
        oos_start = oos_end - fold_size
        train_end = oos_start

        if train_end < 200 or oos_end - oos_start < 50:
            continue

        df_train = df.iloc[:train_end]
        df_oos = df.iloc[oos_start:oos_end]

        # 训练集表现
        r_train = walk_forward_backtest(symbol, cfg=cfg, window=300, min_bars=60, df_in=df_train)
        nt_train = int(r_train.get("trades", 0))
        if nt_train >= 8:
            train_expRs.append(float(r_train.get("expR", 0)))

        # OOS表现
        r_oos = walk_forward_backtest(symbol, cfg=cfg, window=300, min_bars=60, df_in=df_oos)
        nt_oos = int(r_oos.get("trades", 0))
        if nt_oos >= 5:  # OOS 降低要求
            oos_expRs.append(float(r_oos.get("expR", 0)))
            all_nt.append(nt_oos)

    if not oos_expRs:
        return None

    return {
        "avg_train_expR": float(np.mean(train_expRs)) if train_expRs else None,
        "avg_oos_expR": float(np.mean(oos_expRs)),
        "oos_std": float(np.std(oos_expRs)),
        "oos_win_rate": float(np.mean([1 if e > 0 else 0 for e in oos_expRs])),
        "avg_trades": float(np.mean(all_nt)),
        "n_folds": len(oos_expRs),
    }


def main():
    strategies = [
        "默认策略",
        "保守型",
        "趋势强化",
        "波动市回避",
        "F主导型",
        "宽止损高盈亏比",
        "高门槛+宽止损",
    ]

    n_folds = 5

    print(f"{'=' * 100}")
    print(f"黑系策略重构 - {n_folds}折走步法OOS验证")
    print(f"{'=' * 100}")
    print(f"品种: {HEI_SYMBOLS}")
    print(f"策略数: {len(strategies)}")
    print()

    all_results = {}

    for sname in strategies:
        print(f"--- {sname} ---")
        cfg = make_strategy(sname)

        sym_results = {}
        oos_list = []

        for sym in HEI_SYMBOLS:
            if sym not in SYMBOLS:
                continue

            res = walk_forward_validate(sym, cfg, n_folds=n_folds)
            if res is None:
                print(f"  {sym}: 数据不足")
                continue

            sym_results[sym] = res
            oos_list.append(res["avg_oos_expR"])

            wr = res["oos_win_rate"] * 100
            print(
                f"  {sym}: OOS_expR={res['avg_oos_expR']:+.3f}  "
                f"胜率={wr:.0f}%/{res['n_folds']}折  "
                f"笔数={res['avg_trades']:.0f}"
            )

        if oos_list:
            avg_oos = float(np.mean(oos_list))
            print(f"  板块平均 OOS expR: {avg_oos:+.4f}")
        print()

        all_results[sname] = {
            "symbols": sym_results,
            "avg_oos_expR": float(np.mean(oos_list)) if oos_list else None,
            "oos_std": float(np.std(oos_list)) if oos_list else None,
        }

    # 汇总排名
    print(f"\n{'=' * 100}")
    print("策略排名（按板块平均 OOS expR）")
    print(f"{'=' * 100}")

    ranked = sorted(
        all_results.items(), key=lambda x: -(x[1]["avg_oos_expR"] if x[1]["avg_oos_expR"] is not None else -999)
    )

    print(f"{'排名':<6}{'策略':<18}{'平均OOS':>12}{'稳定性':>10}{'i':>10}{'rb':>10}{'hc':>10}")
    print(f"{'-' * 70}")

    for rank, (sname, data) in enumerate(ranked, 1):
        avg = data["avg_oos_expR"]
        std = data["oos_std"]
        syms = data["symbols"]

        i_val = syms.get("i", {}).get("avg_oos_expR", None)
        rb_val = syms.get("rb", {}).get("avg_oos_expR", None)
        hc_val = syms.get("hc", {}).get("avg_oos_expR", None)

        def fmt(v):
            return f"{v:+.3f}" if v is not None else "  N/A "

        # 标记是否优于默认
        default_avg = all_results["默认策略"]["avg_oos_expR"]
        if avg > default_avg + 0.05:
            tag = "✅"
        elif avg > default_avg:
            tag = "➕"
        elif avg > default_avg - 0.05:
            tag = "➖"
        else:
            tag = "❌"

        print(f"{rank:<6}{sname:<18}{avg:>+12.4f}{std:>10.3f}{fmt(i_val):>10}{fmt(rb_val):>10}{fmt(hc_val):>10}  {tag}")

    # 保存
    out_path = "logs/hei_strategy_rebuild.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
