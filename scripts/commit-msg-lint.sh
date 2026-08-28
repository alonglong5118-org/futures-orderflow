#!/bin/bash
# ==============================================================================
#  commit-msg hook：提交信息规范检查
# ==============================================================================
#  用法：由 git 自动调用，不需要手动执行。
#  安装：./scripts/install_hooks.sh
#  跳过：git commit --no-verify
# ==============================================================================
#  规范：Conventional Commits（中文友好版）
#    格式: <type>(<scope>): <subject>
#
#  type 类型：
#    feat     新功能
#    fix      Bug 修复
#    perf     性能优化
#    refactor 代码重构（非功能、非修复）
#    style    代码风格/格式（不影响逻辑）
#    docs     文档更新
#    test     测试补充/修改
#    ci       CI/CD 配置变更
#    build    构建系统/依赖变更
#    chore    杂项（工具、配置、其他）
#    revert   回滚提交
#
#  示例：
#    feat(strategy): 新增四维策略止损逻辑
#    fix(risk): 修复风控模块浮盈计算错误
#    ci(coverage): 添加覆盖率门禁
#    docs(readme): 更新安装说明
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
# 允许的 type 列表
ALLOWED_TYPES=(
    "feat" "fix" "perf" "refactor" "style"
    "docs" "test" "ci" "build" "chore" "revert"
)

# 标题最大长度
MAX_SUBJECT_LENGTH=72

# ── 获取项目根目录 ────────────────────────────────────────────────────────────
ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
cd "$ROOT_DIR" || exit 1

# ── 读取 commit message ───────────────────────────────────────────────────────
COMMIT_MSG_FILE="$1"
commit_msg=$(cat "$COMMIT_MSG_FILE" 2>/dev/null || true)

# 移除注释行（以 # 开头的）
commit_msg_clean=$(echo "$commit_msg" | grep -v '^#' | sed '/^$/d' | head -1)

# 如果是空消息（不应该到这里，git 会拦截），直接通过
if [ -z "$commit_msg_clean" ]; then
    exit 0
fi

# ── 检查 Conventional Commits 格式 ───────────────────────────────────────────
# 正则: type(scope)!?: subject
# 支持中文 subject

# 构建 type 列表的正则（兼容 bash 3.x / macOS 默认 bash）
type_regex=$(IFS='|'; echo "${ALLOWED_TYPES[*]}")
PATTERN="^(${type_regex})(\([a-zA-Z0-9._-]+\))?!?: .+"

if echo "$commit_msg_clean" | grep -Eq "$PATTERN"; then
    # 格式正确，检查长度
    subject_len=${#commit_msg_clean}

    if [ "$subject_len" -gt "$MAX_SUBJECT_LENGTH" ]; then
        echo ""
        echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════════════════════${RESET}"
        echo -e "${BOLD}${YELLOW}  ⚠️  提交信息过长${RESET}"
        echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════════════════════${RESET}"
        echo ""
        echo -e "   标题长度: ${subject_len} 字符（建议 ≤ ${MAX_SUBJECT_LENGTH}）"
        echo ""
        echo -e "   当前内容："
        echo -e "   ${DIM}$commit_msg_clean${RESET}"
        echo ""
        echo -e "${YELLOW}   💡 建议：${RESET}"
        echo -e "   · 标题简洁明了，详细说明写在正文"
        echo -e "   · 如果确实需要长标题，可以忽略此警告"
        echo ""
        # 长度超限只是警告，不阻塞
        exit 0
    fi

    # 完全通过
    exit 0
fi

# ── 格式不正确，给出详细错误 ──────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${RED}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${RED}  ❌  提交信息不符合规范${RESET}"
echo -e "${BOLD}${RED}══════════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "   当前提交信息："
echo -e "   ${DIM}$commit_msg_clean${RESET}"
echo ""
echo -e "${CYAN}   📝  规范格式（Conventional Commits）：${RESET}"
echo ""
echo -e "   ${BOLD}<type>(<scope>): <subject>${RESET}"
echo ""
echo -e "   type 类型："
echo -e "     feat     新功能"
echo -e "     fix      Bug 修复"
echo -e "     perf     性能优化"
echo -e "     refactor 代码重构（非功能、非修复）"
echo -e "     style    代码风格/格式（不影响逻辑）"
echo -e "     docs     文档更新"
echo -e "     test     测试补充/修改"
echo -e "     ci       CI/CD 配置变更"
echo -e "     build    构建系统/依赖变更"
echo -e "     chore    杂项（工具、配置、其他）"
echo -e "     revert   回滚提交"
echo ""
echo -e "   ✅ 正确示例："
echo -e "     feat(strategy): 新增四维策略止损逻辑"
echo -e "     fix(risk): 修复风控模块浮盈计算错误"
echo -e "     ci(coverage): 添加覆盖率门禁"
echo -e "     docs(readme): 更新安装说明"
echo ""
echo -e "   ❌ 错误示例："
echo -e "     修复bug              → 缺少 type，描述太模糊"
echo -e "     update code         → 英文且不明确，缺少 type"
echo -e "     feature: new thing  → 应使用 feat 而非 feature"
echo ""
echo -e "${YELLOW}   💡 提示：${RESET}"
echo -e "   · 用 ${CYAN}git commit --amend${RESET} 修改上一条提交信息"
echo -e "   · 用 ${CYAN}git commit --no-verify${RESET} 跳过检查（不建议）"
echo ""
echo -e "${DIM}   详细规范：https://www.conventionalcommits.org/zh-cn/${RESET}"
echo ""

exit 1
