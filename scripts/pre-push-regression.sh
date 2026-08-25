#!/bin/bash
# ==============================================================================
#  pre-push hook：推送前自动跑回归测试
# ==============================================================================
#  用法：由 git 自动调用，不需要手动执行。
#  安装：./scripts/install_hooks.sh
#  跳过：git push --no-verify
# ==============================================================================

set -e

# ── 颜色 ──────────────────────────────────────────────────────────────────────
RED='\033[91m'
GREEN='\033[92m'
YELLOW='\033[93m'
CYAN='\033[96m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# ── 配置 ──────────────────────────────────────────────────────────────────────
# 哪些文件改动了才需要跑回归测试（策略相关文件）
STRATEGY_FILES=(
    "four_dim_strategy.py"
    "strategy_layer.py"
    "trade_config.json"
    "feature_flags.json"
    "calibration_drift.json"
)

# 快速模式的 tail 长度（推送前用快速模式，不耽误时间）
# 如果基准文件存在，自动用基准的 tail（保证对比公平）
FAST_TAIL=100
if [ -f "regression_baseline.json" ]; then
    baseline_tail=$(python3 -c "import json; d=json.load(open('regression_baseline.json')); print(d.get('tail_bars', $FAST_TAIL))" 2>/dev/null || echo "$FAST_TAIL")
    FAST_TAIL=$baseline_tail
fi

# ── 获取项目根目录 ────────────────────────────────────────────────────────────
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# ── 检查是否有策略相关文件改动 ────────────────────────────────────────────────
# 比较暂存区 + 工作区 vs 远端分支，看有没有策略文件改动
need_test=false

# 获取即将推送的 commit 范围
while read local_ref local_sha remote_ref remote_sha; do
    # 如果是删除分支，跳过
    if [ "$local_sha" = "0000000000000000000000000000000000000000" ]; then
        continue
    fi

    # 如果远端不存在（新分支），和 HEAD 对比
    if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        # 新分支：和 HEAD 比差异
        changed_files=$(git diff --name-only HEAD 2>/dev/null || true)
    else
        # 已有分支：和远端比差异
        changed_files=$(git diff --name-only "$remote_sha" "$local_sha" 2>/dev/null || true)
    fi

    for f in "${STRATEGY_FILES[@]}"; do
        if echo "$changed_files" | grep -q "^${f}$"; then
            need_test=true
            break 2
        fi
    done
done

# 也检查工作区未提交的改动（防止漏网之鱼）
if [ "$need_test" = false ]; then
    unstaged=$(git diff --name-only 2>/dev/null || true)
    staged=$(git diff --cached --name-only 2>/dev/null || true)
    all_uncommitted="$unstaged
$staged"
    for f in "${STRATEGY_FILES[@]}"; do
        if echo "$all_uncommitted" | grep -q "^${f}$"; then
            need_test=true
            break
        fi
    done
fi

# 没有策略文件改动 → 跳过回归测试
if [ "$need_test" = false ]; then
    echo -e "${DIM}⏭  无策略相关文件改动，跳过回归测试${RESET}"
    exit 0
fi

# ── 跑回归测试 ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  🧪  推送前回归测试（${FAST_TAIL} bars，与基准一致）${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo ""

# 检查 python 是否可用
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "${YELLOW}⚠️  找不到 Python，跳过回归测试${RESET}"
    echo -e "${DIM}   手动运行: python regression_test.py --tail ${FAST_TAIL}${RESET}"
    echo ""
    exit 0
fi

PYTHON_CMD=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)

# 检查测试脚本是否存在
if [ ! -f "regression_test.py" ]; then
    echo -e "${YELLOW}⚠️  regression_test.py 不存在，跳过回归测试${RESET}"
    echo ""
    exit 0
fi

# 跑测试
echo -e "${DIM}   运行中...（${FAST_TAIL} 根日线，约 30-60 秒）${RESET}"
echo ""

# 保存退出码
set +e
"$PYTHON_CMD" regression_test.py --tail $FAST_TAIL --summary 2>&1
test_exit=$?
set -e

echo ""

# ── 判定结果 ──────────────────────────────────────────────────────────────────
if [ $test_exit -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅  回归测试通过，允许推送${RESET}"
    echo ""
    exit 0
else
    echo -e "${RED}${BOLD}❌  回归测试失败！${RESET}"
    echo ""
    echo -e "${YELLOW}   可能的原因：${RESET}"
    echo -e "${YELLOW}   · 你改动了策略代码，导致指标偏离过大${RESET}"
    echo -e "${YELLOW}   · 信号一致性低于阈值（可能改坏了核心逻辑）${RESET}"
    echo ""
    echo -e "${DIM}   建议操作：${RESET}"
    echo -e "${DIM}   1. 仔细检查你的改动${RESET}"
    echo -e "${DIM}   2. 跑完整测试：python regression_test.py${RESET}"
    echo -e "${DIM}   3. 如果确认没问题，更新基准：python regression_test.py --update-baseline --version v6.x${RESET}"
    echo ""
    echo -e "${DIM}   强行推送（跳过检查）：${RESET}"
    echo -e "${CYAN}   git push --no-verify${RESET}"
    echo ""
    exit 1
fi
