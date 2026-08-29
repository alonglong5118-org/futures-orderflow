#!/usr/bin/env python3
"""
小资金验证方案配置生成器

生成一套"小资金验证模式"的 trade_config 补丁，用于：
1. 用10万模拟权益验证GA优化参数的实盘表现
2. 控制单品种手数和总仓位
3. 严格的风控闸门（更低的连续止损锁定）
4. 只交易OOS正收益的14个品种（排除MA和i）

用法:
    python3 generate_small_cap_config.py
    # 生成 small_cap_patch.json，手动合并到 trade_config.json
    # 或者直接用 --apply 应用（会备份）
"""

import argparse
import copy
import json
import os
import shutil
from datetime import datetime

SCRIPT_DIR = "/Users/ken/WorkBuddy/futures-orderflow"
OUTPUT_DIR = "/Users/ken/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a8cc690838fade9dcdef78c"
TRADE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "trade_config.json")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "trade_config_backups")

# 小资金验证参数
SMALL_CAP_CONFIG = {
    "mode": "small_cap_validation",
    "version": "v1.0",
    "description": "GA优化参数小资金验证模式 - 10万模拟权益",
    # 账户配置
    "account": {
        "equity": 100000.0,
        "equity_note": "小资金验证模式 - 模拟权益10万元",
        "risk_pct": 0.5,  # 单品种风险0.5%（更保守）
        "margin_cap_pct": 25,  # 单品种保证金上限25%
        "max_lots": 2,  # 单品种最大2手
        "portfolio_margin_cap_pct": 40,  # 组合保证金上限40%
        "max_total_lots": 5,  # 总持仓最多5手
    },
    # 风控闸门
    "risk_gate": {
        "consec_loss_lock": 2,  # 连续2次止损就当日冻结（更严格）
        "slip_pts": 1,
    },
    # 连续止损闸门
    "consec_loss_gate": {"warn": 1, "lock": 2, "_note": "小资金验证模式：1笔警告，2笔当日冻结"},
    # 只交易OOS正收益品种（排除MA和i）
    "validation_only_positive_oos": True,
    "excluded_symbols": ["MA", "i"],
    "excluded_reason": "OOS样本外负收益，小资金验证阶段暂不纳入",
    # 验证目标
    "validation_targets": {
        "min_trades": 30,  # 至少30笔交易
        "min_win_rate": 0.35,  # 胜率不低于35%
        "min_expR": 0.1,  # 期望收益>0.1R
        "max_drawdown_pct": 5,  # 最大回撤不超过5%
        "validation_period_days": 60,  # 验证期60天
    },
    # 阶段推进条件
    "graduation_criteria": {
        "trades": 50,  # 满50笔交易
        "win_rate": 0.38,  # 胜率≥38%
        "expR": 0.2,  # 期望收益≥0.2R
        "calmar": 2.0,  # Calmar≥2.0
        "max_drawdown_pct": 3,  # 最大回撤≤3%
    },
}


def load_trade_config():
    with open(TRADE_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def backup_config():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"trade_config_{timestamp}_presmallcap.json")
    shutil.copy2(TRADE_CONFIG_PATH, backup_path)
    return backup_path


def generate_patch():
    """生成小资金配置补丁"""
    patch = {
        "account": SMALL_CAP_CONFIG["account"],
        "risk_gate": {},  # 只覆盖特定键
        "consec_loss_gate": SMALL_CAP_CONFIG["consec_loss_gate"],
        "_small_cap_mode": SMALL_CAP_CONFIG,
    }

    # risk_gate 只覆盖 consec_loss_lock 和 slip_pts
    current = load_trade_config()
    current_rg = current.get("risk_gate", {})
    patch["risk_gate"] = {
        **current_rg,
        "consec_loss_lock": SMALL_CAP_CONFIG["risk_gate"]["consec_loss_lock"],
        "slip_pts": SMALL_CAP_CONFIG["risk_gate"]["slip_pts"],
    }

    return patch


def apply_patch(patch):
    """应用补丁到 trade_config"""
    current = load_trade_config()
    new_config = copy.deepcopy(current)

    # 合并各层
    for key in ["account", "risk_gate", "consec_loss_gate"]:
        if key in patch and isinstance(patch[key], dict):
            if key in new_config and isinstance(new_config[key], dict):
                new_config[key] = {**new_config[key], **patch[key]}
            else:
                new_config[key] = patch[key]

    # 添加元数据标记
    new_config["_small_cap_mode"] = patch["_small_cap_mode"]

    # 写入
    with open(TRADE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(new_config, f, ensure_ascii=False, indent=2)

    return new_config


def main():
    parser = argparse.ArgumentParser(description="小资金验证方案配置生成器")
    parser.add_argument("--apply", action="store_true", help="直接应用到trade_config.json")
    parser.add_argument("--output", type=str, default=None, help="输出补丁文件路径")
    args = parser.parse_args()

    patch = generate_patch()

    print("=" * 70)
    print("  小资金验证方案配置")
    print("=" * 70)
    print()
    print("  模式说明:")
    print("    - 模拟权益: 100,000 元")
    print("    - 单品种风险: 0.5% (500元)")
    print("    - 单品种最大手数: 2 手")
    print("    - 总持仓上限: 5 手")
    print("    - 连续止损锁定: 2笔 (更严格)")
    print("    - 排除品种: MA, i (OOS负收益)")
    print()
    print("  验证目标:")
    targets = SMALL_CAP_CONFIG["validation_targets"]
    print(f"    - 至少 {targets['min_trades']} 笔交易")
    print(f"    - 胜率 ≥ {targets['min_win_rate'] * 100:.0f}%")
    print(f"    - 期望收益 ≥ {targets['min_expR']}R")
    print(f"    - 最大回撤 ≤ {targets['max_drawdown_pct']}%")
    print(f"    - 验证期: {targets['validation_period_days']} 天")
    print()
    print("  进阶条件 (达标后可扩大资金):")
    grad = SMALL_CAP_CONFIG["graduation_criteria"]
    print(f"    - 满 {grad['trades']} 笔交易")
    print(f"    - 胜率 ≥ {grad['win_rate'] * 100:.0f}%")
    print(f"    - 期望收益 ≥ {grad['expR']}R")
    print(f"    - Calmar ≥ {grad['calmar']}")
    print(f"    - 最大回撤 ≤ {grad['max_drawdown_pct']}%")
    print()

    if args.apply:
        print("  正在应用配置...")
        backup_path = backup_config()
        print(f"  💾 已备份: {backup_path}")

        new_config = apply_patch(patch)
        print("  ✅ 小资金验证配置已应用到 trade_config.json")
        print()
        print("  ⚠️  重要提示:")
        print("     1. 重启实盘runner后生效")
        print("     2. 验证期内密切关注表现")
        print("     3. 达标后可使用 graduate 模式扩大资金")
    else:
        output_path = args.output or os.path.join(OUTPUT_DIR, "small_cap_config_patch.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(patch, f, ensure_ascii=False, indent=2)
        print(f"  📄 配置补丁已保存: {output_path}")
        print("  使用 --apply 参数直接应用到 trade_config.json")


if __name__ == "__main__":
    main()
