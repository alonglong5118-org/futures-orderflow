#!/bin/bash
# ==============================================================================
#  pre-commit hook：提交前自动跑冒烟测试
# ==============================================================================
#  用法：由 git 自动调用，不需要手动执行。
#  安装：./scripts/install_hooks.sh
#  跳过：git commit --no-verify
# ==============================================================================
#  设计原则：
#    1. 快：只跑 smoke 冒烟测试（<3s），不阻塞提交流程
#    2. 智能：只在相关文件改动时才跑
#    3. 清晰：失败时给出明确的下一步操作指引
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
# 改动这些文件/目录时触发测试（glob 模式）
TRIGGER_PATTERNS=(
    # 核心策略代码
    "*.py"
    # 测试文件
    "tests/*.py"
    # 测试入口
    "run_tests.py"
    # 配置
    ".coveragerc"
)

# 排除的文件（改动不触发测试）
EXCLUDE_PATTERNS=(
    "*.md"
    "*.txt"
    "*.json"
    "*.yaml"
    "*.yml"
    "docs/*"
    ".gitignore"
    "scripts/*.md"
)

# 默认跑冒烟测试（快），设为 false 跑全量
SMOKE_ONLY=true

# ── 工具函数 ──────────────────────────────────────────────────────────────────
matches_any() {
    local file="$1"
    shift
    for pattern in "$@"; do
        # 简单 glob 匹配
        if [[ "$file" == $pattern ]]; then
            return 0
        fi
    done
    return 1
}

# ── 获取项目根目录 ────────────────────────────────────────────────────────────
# 用 git rev-parse 找根目录，兼容软链接钩子和直接运行两种情况
ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
cd "$ROOT_DIR" || exit 1

# ── 收集改动文件 ──────────────────────────────────────────────────────────────
# 暂存区的改动（即将 commit 的）
staged=$(git diff --cached --name-only 2>/dev/null || true)
# 工作区的改动（未暂存但可能影响）
unstaged=$(git diff --name-only 2>/dev/null || true)
all_changed=$(echo -e "$staged\n$unstaged" | sort -u | sed '/^$/d')

# 收集暂存区的 Python 文件（用于格式化检查）
py_staged=""
py_count=0
while IFS= read -r file; do
    [ -z "$file" ] && continue
    case "$file" in
        *.py)
            if ! matches_any "$file" "${EXCLUDE_PATTERNS[@]}"; then
                py_staged="$py_staged
$file"
                py_count=$((py_count + 1))
            fi
            ;;
    esac
done <<< "$staged"

# ── 格式化检查（只检查暂存的 Python 文件） ───────────────────────────────────
if [ "$py_count" -gt 0 ]; then
    if command -v ruff &> /dev/null; then
        echo ""
        echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
        echo -e "${BOLD}${CYAN}  🎨  格式化检查${RESET}"
        echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
        echo ""
        echo -e "${DIM}   检查 $py_count 个暂存 Python 文件的格式...${RESET}"
        echo ""

        set +e
        format_failed=false
        while IFS= read -r file; do
            [ -z "$file" ] && continue
            git show ":$file" 2>/dev/null | ruff format --check --stdin-filename "$file" - > /dev/null 2>&1
            if [ $? -ne 0 ]; then
                format_failed=true
                echo -e "   ${RED}✗${RESET} $file"
            fi
        done <<< "$py_staged"
        set -e

        echo ""

        if [ "$format_failed" = true ]; then
            echo -e "${RED}${BOLD}❌  格式化检查未通过${RESET}"
            echo ""
            echo -e "${YELLOW}   修复方式：${RESET}"
            echo -e "   ${CYAN}make format${RESET}              # 自动格式化所有 Python 文件"
            echo -e "   ${CYAN}ruff format <file>${RESET}      # 只格式化单个文件"
            echo ""
            echo -e "${DIM}   格式化后重新 git add 再提交${RESET}"
            echo -e "${DIM}   跳过检查：git commit --no-verify${RESET}"
            echo ""
            exit 1
        else
            echo -e "${GREEN}${BOLD}✅  格式化检查通过${RESET}"
        fi
    else
        echo -e "${YELLOW}⚠️  未找到 ruff，跳过格式化检查${RESET}"
        echo -e "${DIM}   安装：pip install ruff${RESET}"
    fi
fi

# ── 检查是否需要跑测试 ────────────────────────────────────────────────────────
need_test=false
changed_count=0

while IFS= read -r file; do
    [ -z "$file" ] && continue
    changed_count=$((changed_count + 1))

    # 排除文件
    if matches_any "$file" "${EXCLUDE_PATTERNS[@]}"; then
        continue
    fi

    # 触发文件
    if matches_any "$file" "${TRIGGER_PATTERNS[@]}"; then
        need_test=true
        break
    fi
done <<< "$all_changed"

# 没有相关文件改动 → 跳过
if [ "$need_test" = false ]; then
    echo -e "${DIM}⏭  无测试相关文件改动（$changed_count 个文件），跳过单元测试${RESET}"
    exit 0
fi

# ── 检查 Python ───────────────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "${YELLOW}⚠️  找不到 Python，跳过单元测试${RESET}"
    exit 0
fi

PYTHON_CMD=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)

if [ ! -f "run_tests.py" ]; then
    echo -e "${YELLOW}⚠️  run_tests.py 不存在，跳过单元测试${RESET}"
    exit 0
fi

# ── 跑测试 ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  🧪  提交前测试${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${RESET}"
echo ""

if [ "$SMOKE_ONLY" = true ]; then
    echo -e "${DIM}   模式: 冒烟测试（快速验证）${RESET}"
    TEST_ARGS="smoke --py-only -f"
else
    echo -e "${DIM}   模式: 全量单元测试${RESET}"
    TEST_ARGS="--py-only -f"
fi
echo ""

set +e
"$PYTHON_CMD" run_tests.py $TEST_ARGS 2>&1
test_exit=$?
set -e

echo ""

# ── 判定结果 ──────────────────────────────────────────────────────────────────
if [ $test_exit -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅  测试通过，允许提交${RESET}"
    echo ""
    exit 0
else
    echo -e "${RED}${BOLD}❌  测试失败！${RESET}"
    echo ""
    echo -e "${YELLOW}   可能的原因：${RESET}"
    echo -e "${YELLOW}   · 你的改动破坏了已有逻辑${RESET}"
    echo -e "${YELLOW}   · 测试本身需要更新（如果是预期内的行为变更）${RESET}"
    echo ""
    echo -e "${DIM}   建议操作：${RESET}"
    echo -e "${DIM}   1. 修复代码后重新提交${RESET}"
    echo -e "${DIM}   2. 查看详细失败：python run_tests.py -v${RESET}"
    echo -e "${DIM}   3. 如果是预期变更，更新测试用例${RESET}"
    echo ""
    echo -e "${DIM}   跑全量测试：${RESET}"
    echo -e "${CYAN}   python run_tests.py${RESET}"
    echo ""
    echo -e "${DIM}   强行提交（跳过检查）：${RESET}"
    echo -e "${CYAN}   git commit --no-verify${RESET}"
    echo ""
    exit 1
fi
