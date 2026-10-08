"""
F 因子增强方案 A/B 测试

测试组合：
- A: 基准（旧 F + 默认权重 0.6/0.25/0.15）
- B: 增强 F + 默认权重（新因子，但权重不变）
- C: 增强 F + 提高 F 权重（T:0.5, F:0.35, C:0.15）
- D: 增强 F + 提高权重 + 降低阈值（fc_confirm=15, fc_hard=15）

目标：找到最优的 F 增强方案
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy

import four_dim_strategy as fds
import fundamental_factors as nff
import fundamental_feed as ff_mod
from four_dim_strategy import (
    DEFAULT_CONFIG,
    SYMBOLS,
    load_daily,
    walk_forward_backtest,
)

# 板块
GROUPS = {}
for _sym, _meta in SYMBOLS.items():
    _g = _meta.get("group", "其他")
    if _g not in GROUPS:
        GROUPS[_g] = []
    if not any(c.isdigit() for c in _sym):
        GROUPS[_g].append(_sym)


def make_enhanced_F(symbol, date_ints):
    """构建增强版 F 的 map。"""
    sector = SYMBOLS.get(symbol, {}).get("group", "其他")
    try:
        enh_F = nff.precompute_enhanced_F_array(symbol, date_ints=date_ints, sector=sector)
    except Exception:
        return None

    F_map = {}
    for i, di in enumerate(date_ints):
        F_map[str(int(di))] = float(enh_F[i])
    return F_map


def run_config(symbol, cfg, F_map=None, window=300):
    """运行单个配置的回测。"""
    df = load_daily(symbol)
    if df is None or len(df) < 300:
        return None

    if F_map is not None:
        # patch
        orig_ff = ff_mod.precompute_F_array
        orig_fds = fds.ff.precompute_F_array

        def patched(sym, date_strs=None, date_ints=None, months=None, **kwargs):
            if date_ints is not None:
                result = np.zeros(len(date_ints), dtype=float)
                for i, di in enumerate(date_ints):
                    result[i] = F_map.get(str(int(di)), 0.0)
                return result
            elif date_strs is not None:
                result = np.zeros(len(date_strs), dtype=float)
                for i, ds in enumerate(date_strs):
                    d_clean = str(ds).replace("-", "")[:8]
                    result[i] = F_map.get(d_clean, 0.0)
                return result
            return np.zeros(100)

        ff_mod.precompute_F_array = patched
        fds.ff.precompute_F_array = patched

    try:
        res = walk_forward_backtest(symbol, cfg=cfg, window=window, cooldown_bars=5, df_in=df)
    finally:
        if F_map is not None:
            ff_mod.precompute_F_array = orig_ff
            fds.ff.precompute_F_array = orig_fds

    return res


def main():
    t0 = time.time()

    window = 300

    # 配置方案
    configs = {
        "A_基准": DEFAULT_CONFIG,
        "B_增强F": DEFAULT_CONFIG,
        "C_增强F+高权重": None,  # 下面构建
        "D_增强F+高权重+低阈值": None,
    }

    # 方案 C：增强 F + 提高 F 权重
    cfg_c = copy.deepcopy(DEFAULT_CONFIG)
    cfg_c["combine_weights"] = {"T": 0.5, "F": 0.35, "C": 0.15}
    configs["C_增强F+高权重"] = cfg_c

    # 方案 D：增强 F + 提高权重 + 降低阈值
    cfg_d = copy.deepcopy(cfg_c)
    cfg_d["bias_synthesis"]["fc_confirm"] = 15
    cfg_d["bias_synthesis"]["fc_hard"] = 15
    configs["D_增强F+高权重+低阈值"] = cfg_d

    test_sectors = ["农产品", "有色", "黑系", "化工"]
    # 每个板块选 4-5 个代表性品种
    pick = {
        "农产品": ["m", "y", "p", "c", "CF"],
        "有色": ["cu", "al", "zn", "ni", "si"],
        "黑系": ["rb", "hc", "i", "J", "JM"],
        "化工": ["MA", "TA", "pp", "l", "v"],
    }

    print("=" * 100)
    print("F 因子增强方案 A/B/C/D 对比")
    print("=" * 100)
    print(f"{'方案':<25}{'描述':<40}")
    print("  A: 基准（旧 F）              基差0.6 + 库存0.1 + 季节性0.3，权重 T:0.6 F:0.25 C:0.15")
    print("  B: 增强 F（默认权重）        7因子分板块权重，T:0.6 F:0.25 C:0.15")
    print("  C: 增强 F + 高权重           7因子分板块权重，T:0.5 F:0.35 C:0.15")
    print("  D: 增强 F + 高权重 + 低阈值  7因子 + T:0.5 F:0.35 + fc_confirm/hard=15")
    print()

    all_results = {}

    for sector in test_sectors:
        syms = pick.get(sector, [])
        print(f"【{sector}】")
        print(f"{'品种':<6}" + "".join([f"{cfg_name:>12}" for cfg_name in configs.keys()]))
        print("-" * 70)

        sector_results = {}

        for sym in syms:
            if sym not in SYMBOLS:
                continue

            df = load_daily(sym)
            if df is None or len(df) < 300:
                continue

            date_ints = df.index.year.values * 10000 + df.index.month.values * 100 + df.index.day.values

            # 预计算增强 F
            F_map = make_enhanced_F(sym, date_ints)

            sym_results = {}

            for cfg_name, cfg in configs.items():
                use_enhanced = cfg_name.startswith("B_") or cfg_name.startswith("C_") or cfg_name.startswith("D_")
                try:
                    res = run_config(sym, cfg, F_map=F_map if use_enhanced else None, window=window)
                    sym_results[cfg_name] = {
                        "expR": float(res.get("expR", 0)),
                        "win_rate": float(res.get("win_rate", 0)),
                        "trades": int(res.get("trades", 0)),
                    }
                except Exception as e:
                    sym_results[cfg_name] = {"expR": 0, "win_rate": 0, "trades": 0, "error": str(e)}

            # 打印
            def fmt_expR(v):
                return f"{v:>+12.3f}"

            line = f"{sym:<6}"
            for cfg_name in configs:
                line += fmt_expR(sym_results[cfg_name].get("expR", 0))
            print(line)

            sector_results[sym] = sym_results

        # 板块平均
        print("-" * 70)
        avg_line = f"{'平均':<6}"
        for cfg_name in configs:
            vals = [v[cfg_name]["expR"] for v in sector_results.values() if "expR" in v[cfg_name]]
            avg = np.mean(vals) if vals else 0
            avg_line += f"{avg:>+12.3f}"
        print(avg_line)

        # 变化（相对基准）
        delta_line = f"{'Δ基准':<6}"
        base_vals = [v["A_基准"]["expR"] for v in sector_results.values() if "expR" in v.get("A_基准", {})]
        base_avg = np.mean(base_vals) if base_vals else 0
        for cfg_name in configs:
            if cfg_name == "A_基准":
                delta_line += f"{'—':>12}"
            else:
                vals = [v[cfg_name]["expR"] for v in sector_results.values() if "expR" in v.get(cfg_name, {})]
                avg = np.mean(vals) if vals else 0
                delta_line += f"{avg - base_avg:>+12.3f}"
        print(delta_line)
        print()

        all_results[sector] = sector_results

    # 全板块汇总
    print("=" * 100)
    print("全板块汇总（平均 expR）")
    print("=" * 100)
    print(f"{'板块':<10}" + "".join([f"{cfg_name:>18}" for cfg_name in configs.keys()]))
    print("-" * 90)

    grand_avgs = {cfg: [] for cfg in configs}

    for sector in test_sectors:
        line = f"{sector:<10}"
        for cfg_name in configs:
            vals = []
            for sym, sres in all_results[sector].items():
                if "expR" in sres.get(cfg_name, {}):
                    vals.append(sres[cfg_name]["expR"])
            avg = np.mean(vals) if vals else 0
            grand_avgs[cfg_name].append(avg)
            line += f"{avg:>+18.3f}"
        print(line)

    print("-" * 90)
    total_line = f"{'平均':<10}"
    for cfg_name in configs:
        avg = np.mean(grand_avgs[cfg_name])
        total_line += f"{avg:>+18.3f}"
    print(total_line)

    delta_line = f"{'Δ基准':<10}"
    base_total = np.mean(grand_avgs["A_基准"])
    for cfg_name in configs:
        if cfg_name == "A_基准":
            delta_line += f"{'—':>18}"
        else:
            avg = np.mean(grand_avgs[cfg_name])
            delta_line += f"{avg - base_total:>+18.3f}"
    print(delta_line)

    # 保存
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "F_abcd_test.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=float)

    print(f"\n结果已保存: {out_path}")
    print(f"总耗时: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
