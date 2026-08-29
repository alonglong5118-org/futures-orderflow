#!/usr/bin/env python3
"""
实盘参数注入工具 - 将GA优化后的参数注入trade_config.json

功能：
1. 读取优化结果参数 (strategy_params_v1.json)
2. 计算各品种的绝对T_thresh（基线 × T_thresh_mult）
3. 生成 trade_config 补丁（thresholds_by_symbol + per_symbol_risk）
4. 安全合并到 trade_config.json（自动备份）
5. 支持 dry-run 预览模式
6. 支持回滚到上一版备份

用法：
    python3 inject_live_params.py --dry-run        # 预览，不实际写入
    python3 inject_live_params.py --apply          # 实际应用参数
    python3 inject_live_params.py --rollback       # 回滚到备份
    python3 inject_live_params.py --verify         # 验证当前trade_config是否与预期一致
"""

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime

SCRIPT_DIR = "/Users/ken/WorkBuddy/futures-orderflow"
sys.path.insert(0, SCRIPT_DIR)

from four_dim_strategy import DEFAULT_CONFIG, SYMBOLS

OUTPUT_DIR = "/Users/ken/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a8cc690838fade9dcdef78c"
TRADE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "trade_config.json")
PARAMS_PATH = os.path.join(OUTPUT_DIR, "strategy_params_v1.json")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "trade_config_backups")


def load_params():
    """加载优化参数"""
    with open(PARAMS_PATH) as f:
        return json.load(f)


def load_trade_config():
    """加载当前trade_config"""
    with open(TRADE_CONFIG_PATH) as f:
        return json.load(f)


def get_baseline_t_thresh(symbol):
    """获取某品种的基线T_thresh值（与GA优化器逻辑一致）"""
    sym_cfg = DEFAULT_CONFIG.get("thresholds_by_symbol", {}).get(symbol, {})
    if "T_thresh" in sym_cfg:
        return float(sym_cfg["T_thresh"])
    group = SYMBOLS.get(symbol, {}).get("group", "黑系")
    group_th = DEFAULT_CONFIG["thresholds"].get(group, {})
    return float(group_th.get("T_thresh", 22))


def get_baseline_bias_hard(symbol):
    """获取某品种的基线bias_hard_base值"""
    sym_cfg = DEFAULT_CONFIG.get("thresholds_by_symbol", {}).get(symbol, {})
    if "bias_hard_base" in sym_cfg:
        return float(sym_cfg["bias_hard_base"])
    group = SYMBOLS.get(symbol, {}).get("group", "黑系")
    group_th = DEFAULT_CONFIG["thresholds"].get(group, {})
    return float(group_th.get("bias_hard", 60))


def generate_patch(params_data):
    """生成trade_config补丁

    返回: {
        "thresholds_by_symbol": {...},
        "per_symbol_risk": {...},
        "summary": {...}
    }
    """
    symbols_data = params_data["symbols"]

    thresholds_patch = {}
    per_symbol_risk_patch = {}

    for symbol, sym_data in symbols_data.items():
        params = sym_data["params"]

        # 1. 计算绝对T_thresh
        t_base = get_baseline_t_thresh(symbol)
        t_new = round(t_base * params["T_thresh_mult"], 2)

        # 保留原有的bias_hard_base和其他配置
        sym_cfg = DEFAULT_CONFIG.get("thresholds_by_symbol", {}).get(symbol, {})
        th_entry = {
            "T_thresh": t_new,
            "T_thresh_mult": round(params["T_thresh_mult"], 4),
            "T_thresh_base": t_base,
            "_note": f"GA优化 v1.0, 基线={t_base}, 倍率={params['T_thresh_mult']:.4f}",
        }
        # 继承原有配置
        for key in ["bias_hard_base", "combine_weights"]:
            if key in sym_cfg:
                th_entry[key] = sym_cfg[key]
        if "bias_hard_base" not in th_entry:
            th_entry["bias_hard_base"] = get_baseline_bias_hard(symbol)

        thresholds_patch[symbol] = th_entry

        # 2. 止盈止损参数
        per_symbol_risk_patch[symbol] = {
            "stop_atr_mult": round(params["stop_atr_mult"], 4),
            "rr_ratio": round(params["rr_ratio"], 4),
            "_note": f"GA优化 v1.0, OOS expR={sym_data.get('oos_expR', 0):.4f}",
        }

    summary = {
        "total_symbols": len(symbols_data),
        "methodology": params_data.get("methodology", "GA优化"),
        "generated_at": datetime.now().isoformat(),
        "t_thresh_changes": {},
        "stop_changes": {},
        "rr_changes": {},
    }

    # 计算变化量
    for symbol in symbols_data:
        params = symbols_data[symbol]["params"]

        t_base = get_baseline_t_thresh(symbol)
        t_new = round(t_base * params["T_thresh_mult"], 2)
        summary["t_thresh_changes"][symbol] = {
            "old": t_base,
            "new": t_new,
            "change_pct": round((t_new - t_base) / t_base * 100, 1),
        }

        # 原有的per_symbol_risk
        old_risk = DEFAULT_CONFIG.get("per_symbol_risk", {}).get(symbol, {})
        old_stop = old_risk.get("stop_atr_mult", DEFAULT_CONFIG["risk_gate"]["stop_atr_mult"])
        old_rr = old_risk.get("rr_ratio", DEFAULT_CONFIG["risk_gate"]["rr_ratio"])

        summary["stop_changes"][symbol] = {
            "old": old_stop,
            "new": round(params["stop_atr_mult"], 4),
            "change_pct": round((params["stop_atr_mult"] - old_stop) / old_stop * 100, 1),
        }
        summary["rr_changes"][symbol] = {
            "old": old_rr,
            "new": round(params["rr_ratio"], 4),
            "change_pct": round((params["rr_ratio"] - old_rr) / old_rr * 100, 1),
        }

    return {"thresholds_by_symbol": thresholds_patch, "per_symbol_risk": per_symbol_risk_patch, "summary": summary}


def merge_patch(current_config, patch):
    """将补丁合并到当前配置中（浅合并，保留未涉及的品种配置）"""
    new_config = copy.deepcopy(current_config)

    # 合并 thresholds_by_symbol
    current_th = new_config.get("thresholds_by_symbol", {})
    patch_th = patch["thresholds_by_symbol"]
    merged_th = {**current_th, **patch_th}
    new_config["thresholds_by_symbol"] = merged_th

    # 合并 per_symbol_risk
    current_risk = new_config.get("per_symbol_risk", {})
    patch_risk = patch["per_symbol_risk"]
    merged_risk = {**current_risk, **patch_risk}
    new_config["per_symbol_risk"] = merged_risk

    return new_config


def backup_config():
    """备份当前trade_config"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"trade_config_{timestamp}.json")
    shutil.copy2(TRADE_CONFIG_PATH, backup_path)

    # 同时保存一个 latest 软链接/副本
    latest_path = os.path.join(BACKUP_DIR, "trade_config_latest_backup.json")
    shutil.copy2(TRADE_CONFIG_PATH, latest_path)

    return backup_path


def rollback_config():
    """回滚到最新备份"""
    latest_path = os.path.join(BACKUP_DIR, "trade_config_latest_backup.json")
    if not os.path.exists(latest_path):
        return None, "没有找到备份文件"

    backup_time = datetime.fromtimestamp(os.path.getmtime(latest_path))
    shutil.copy2(latest_path, TRADE_CONFIG_PATH)
    return latest_path, backup_time


def print_changes_summary(patch):
    """打印变更摘要"""
    summary = patch["summary"]
    print(f"\n{'=' * 70}")
    print(f"  参数变更摘要 ({summary['total_symbols']} 个品种)")
    print(f"{'=' * 70}")
    print(f"  方法: {summary['methodology']}")
    print(f"  生成时间: {summary['generated_at']}")

    print(f"\n  {'品种':6s} {'T_thresh':>14s} {'stop_atr_mult':>15s} {'rr_ratio':>12s}")
    print(f"  {'':6s} {'旧→新 (变化%)':>14s} {'旧→新 (变化%)':>15s} {'旧→新 (变化%)':>12s}")
    print(f"  {'-' * 60}")

    for symbol in sorted(summary["t_thresh_changes"].keys()):
        t = summary["t_thresh_changes"][symbol]
        s = summary["stop_changes"][symbol]
        r = summary["rr_changes"][symbol]

        t_sign = "+" if t["change_pct"] >= 0 else ""
        s_sign = "+" if s["change_pct"] >= 0 else ""
        r_sign = "+" if r["change_pct"] >= 0 else ""

        t_color = "\033[32m" if t["change_pct"] > 0 else "\033[31m" if t["change_pct"] < 0 else ""
        s_color = "\033[32m" if s["change_pct"] > 0 else "\033[31m" if s["change_pct"] < 0 else ""
        r_color = "\033[32m" if r["change_pct"] > 0 else "\033[31m" if r["change_pct"] < 0 else ""
        reset = "\033[0m"

        print(
            f"  {symbol:6s} "
            f"{t_color}{t['old']:.1f}→{t['new']:.1f} ({t_sign}{t['change_pct']:.1f}%){reset}  "
            f"{s_color}{s['old']:.2f}→{s['new']:.2f} ({s_sign}{s['change_pct']:.1f}%){reset}  "
            f"{r_color}{r['old']:.2f}→{r['new']:.2f} ({r_sign}{r['change_pct']:.1f}%){reset}"
        )

    # 统计变化方向
    t_up = sum(1 for v in summary["t_thresh_changes"].values() if v["change_pct"] > 0)
    t_down = sum(1 for v in summary["t_thresh_changes"].values() if v["change_pct"] < 0)
    t_same = sum(1 for v in summary["t_thresh_changes"].values() if v["change_pct"] == 0)

    s_up = sum(1 for v in summary["stop_changes"].values() if v["change_pct"] > 0)
    s_down = sum(1 for v in summary["stop_changes"].values() if v["change_pct"] < 0)

    r_up = sum(1 for v in summary["rr_changes"].values() if v["change_pct"] > 0)
    r_down = sum(1 for v in summary["rr_changes"].values() if v["change_pct"] < 0)

    print(f"\n  T_thresh: ↑{t_up} ↓{t_down} →{t_same}")
    print(f"  stop_atr_mult: ↑{s_up} ↓{s_down}")
    print(f"  rr_ratio: ↑{r_up} ↓{r_down}")


def verify_config(params_data):
    """验证当前trade_config中的参数是否与预期一致"""
    current = load_trade_config()
    patch = generate_patch(params_data)

    issues = []

    # 检查 thresholds_by_symbol
    current_th = current.get("thresholds_by_symbol", {})
    for symbol, expected in patch["thresholds_by_symbol"].items():
        actual = current_th.get(symbol, {})
        if abs(actual.get("T_thresh", 0) - expected["T_thresh"]) > 0.01:
            issues.append(f"{symbol}: T_thresh 不匹配 (实际={actual.get('T_thresh')}, 期望={expected['T_thresh']})")

    # 检查 per_symbol_risk
    current_risk = current.get("per_symbol_risk", {})
    for symbol, expected in patch["per_symbol_risk"].items():
        actual = current_risk.get(symbol, {})
        if abs(actual.get("stop_atr_mult", 0) - expected["stop_atr_mult"]) > 0.001:
            issues.append(
                f"{symbol}: stop_atr_mult 不匹配 (实际={actual.get('stop_atr_mult')}, 期望={expected['stop_atr_mult']})"
            )
        if abs(actual.get("rr_ratio", 0) - expected["rr_ratio"]) > 0.001:
            issues.append(f"{symbol}: rr_ratio 不匹配 (实际={actual.get('rr_ratio')}, 期望={expected['rr_ratio']})")

    return issues


def main():
    parser = argparse.ArgumentParser(description="实盘参数注入工具 - GA优化参数 → trade_config.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="预览变更，不实际写入")
    group.add_argument("--apply", action="store_true", help="应用参数到trade_config.json")
    group.add_argument("--rollback", action="store_true", help="回滚到上一版备份")
    group.add_argument("--verify", action="store_true", help="验证当前配置是否与预期一致")

    args = parser.parse_args()

    if args.rollback:
        print("=" * 70)
        print("  回滚 trade_config.json")
        print("=" * 70)
        backup_path, backup_time = rollback_config()
        if backup_path:
            print(f"\n✅ 已回滚到备份: {backup_path}")
            print(f"   备份时间: {backup_time}")
        else:
            print(f"\n❌ 回滚失败: {backup_time}")
        return

    params_data = load_params()
    print(f"加载优化参数: {len(params_data['symbols'])} 个品种")

    if args.verify:
        print("\n" + "=" * 70)
        print("  验证当前 trade_config.json 配置")
        print("=" * 70)
        issues = verify_config(params_data)
        if issues:
            print(f"\n❌ 发现 {len(issues)} 个不一致项:")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print("\n✅ 所有参数与预期一致！")
        return

    # 生成补丁
    patch = generate_patch(params_data)

    if args.dry_run:
        print_changes_summary(patch)
        print(f"\n{'=' * 70}")
        print("  DRY-RUN 模式 - 未实际修改文件")
        print("  使用 --apply 实际应用变更")
        print(f"{'=' * 70}")

        # 输出补丁JSON供检查
        patch_preview = {
            "thresholds_by_symbol": patch["thresholds_by_symbol"],
            "per_symbol_risk": patch["per_symbol_risk"],
        }
        preview_path = os.path.join(OUTPUT_DIR, "trade_config_patch_preview.json")
        with open(preview_path, "w", encoding="utf-8") as f:
            json.dump(patch_preview, f, ensure_ascii=False, indent=2)
        print(f"\n📄 补丁预览已保存: {preview_path}")
        return

    if args.apply:
        print_changes_summary(patch)

        print(f"\n{'=' * 70}")
        print("  应用参数变更")
        print(f"{'=' * 70}")

        # 1. 备份
        backup_path = backup_config()
        print(f"\n💾 已备份当前配置: {backup_path}")

        # 2. 加载当前配置
        current = load_trade_config()

        # 3. 合并补丁
        new_config = merge_patch(current, patch)

        # 4. 写入
        with open(TRADE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)

        print(f"✅ 参数已成功注入 {TRADE_CONFIG_PATH}")
        print(f"   涉及品种: {len(patch['thresholds_by_symbol'])} 个")
        print("\n⚠️  重要提示:")
        print("   1. 重启实盘runner后新参数生效")
        print("   2. 可用 --rollback 回滚到备份")
        print("   3. 可用 --verify 验证配置是否正确")


if __name__ == "__main__":
    main()
