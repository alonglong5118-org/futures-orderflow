#!/bin/bash
# ==============================================================================
#  pre-push hook：推送前跑全量回归测试
# ==============================================================================
#  用法：由 git 自动调用，不需要手动执行。
#  安装：./scripts/install_hooks.sh
#  跳过：git push --no-verify
#
#  与 pre-commit 的区别：
#    pre-commit → 只跑 Python 单元测试（快，<1秒），只在相关文件改动时触发
#    pre-push   → 跑全部测试（Python + JS），不管改了什么都跑
#                作为推送到远端前的最后一道防线
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

# ── 获取项目根目录 ────────────────────────────────────────────────────────────
# 用 git rev-parse 找根目录，兼容软链接钩子和直接运行两种情况
ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
cd "$ROOT_DIR" || exit 1

# ── 标题 ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  🚀  推送前全量测试${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo ""

# ── 检查 Python ──────────────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "${RED}❌  找不到 Python，无法运行测试${RESET}"
    echo -e "${DIM}   请安装 Python 3 后重试，或使用 git push --no-verify 跳过${RESET}"
    echo ""
    exit 1
fi

PYTHON_CMD=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)

# ── 检查测试入口 ──────────────────────────────────────────────────────────────
if [ ! -f "run_tests.py" ]; then
    echo -e "${YELLOW}⚠️  run_tests.py 不存在，跳过测试${RESET}"
    echo ""
    exit 0
fi

# ── 跑全量测试 ────────────────────────────────────────────────────────────────
echo -e "${DIM}   运行全部单元测试（Python + JavaScript）...${RESET}"
echo ""

set +e
"$PYTHON_CMD" run_tests.py 2>&1
test_exit=$?
set -e

echo ""

# ── 判定结果 ──────────────────────────────────────────────────────────────────
if [ $test_exit -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅  全量测试通过，允许推送${RESET}"
    echo ""
    exit 0
else
    echo -e "${RED}${BOLD}❌  全量测试失败！推送已阻止${RESET}"
    echo ""
    echo -e "${YELLOW}   可能的原因：${RESET}"
    echo -e "${YELLOW}   · 你的改动破坏了已有逻辑（回归 bug）${RESET}"
    echo -e "${YELLOW}   · 测试本身需要更新（如果是预期内的行为变更）${RESET}"
    echo -e "${YELLOW}   · 新写的功能还没补测试${RESET}"
    echo ""
    echo -e "${DIM}   建议操作：${RESET}"
    echo -e "${DIM}   1. 修复失败的测试对应的代码${RESET}"
    echo -e "${DIM}   2. 如果是预期变更，更新测试用例${RESET}"
    echo -e "${DIM}   3. 跑详细输出定位问题：python run_tests.py -v${RESET}"
    echo ""
    echo -e "${DIM}   强行推送（跳过检查）：${RESET}"
    echo -e "${CYAN}   git push --no-verify${RESET}"
    echo ""
    echo -e "${DIM}   ⚠️  不建议跳过 —— 推上去的代码应该能通过全部测试${RESET}"
    echo ""
    exit 1
fi
