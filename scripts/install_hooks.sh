#!/bin/bash
# ==============================================================================
#  Git Hook 安装/管理脚本
# ==============================================================================
#  用法：
#    ./scripts/install_hooks.sh              # 安装所有 hook
#    ./scripts/install_hooks.sh install      # 同上
#    ./scripts/install_hooks.sh pre-commit   # 只安装 pre-commit
#    ./scripts/install_hooks.sh pre-push     # 只安装 pre-push
#    ./scripts/install_hooks.sh uninstall    # 卸载所有
#    ./scripts/install_hooks.sh status       # 查看状态
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

# ── 路径 ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOKS_DIR="$ROOT_DIR/.git/hooks"

# ── Hook 定义 ────────────────────────────────────────────────────────────────
# 两个平行数组（兼容 bash 3.x，即 macOS 默认 bash）
HOOK_NAMES=("pre-commit" "pre-push" "commit-msg")
HOOK_SCRIPTS=("pre-commit-unittest.sh" "pre-push-regression.sh" "commit-msg-lint.sh")

# 根据 hook 名找脚本路径
get_hook_script() {
    local name="$1"
    local i
    for i in "${!HOOK_NAMES[@]}"; do
        if [ "${HOOK_NAMES[$i]}" = "$name" ]; then
            echo "${HOOK_SCRIPTS[$i]}"
            return 0
        fi
    done
    return 1
}

# 检查 hook 名是否有效
is_valid_hook() {
    local name="$1"
    local i
    for i in "${!HOOK_NAMES[@]}"; do
        if [ "${HOOK_NAMES[$i]}" = "$name" ]; then
            return 0
        fi
    done
    return 1
}

# ── 操作 ──────────────────────────────────────────────────────────────────────
ACTION="${1:-install}"
TARGET_HOOK=""

# 如果第二个参数是 hook 名，或者第一个参数就是 hook 名
if [ "$ACTION" = "install" ] && [ -n "$2" ]; then
    TARGET_HOOK="$2"
elif [ "$ACTION" != "install" ] && [ "$ACTION" != "uninstall" ] && [ "$ACTION" != "status" ] && [ "$ACTION" != "remove" ] && [ "$ACTION" != "check" ]; then
    # 第一个参数可能是 hook 名
    TARGET_HOOK="$ACTION"
    ACTION="install"
fi

# ── 工具函数 ──────────────────────────────────────────────────────────────────

install_single_hook() {
    local hook_name="$1"
    local hook_script="$2"
    local hook_source="$SCRIPT_DIR/$hook_script"
    local hook_target="$HOOKS_DIR/$hook_name"

    echo ""
    echo -e "${BOLD}${CYAN}  安装 $hook_name${RESET}"
    echo -e "${DIM}  ────────────────────────────────────────${RESET}"

    # 检查 git 仓库
    if [ ! -d "$HOOKS_DIR" ]; then
        echo -e "${RED}    ❌  不是 git 仓库：$ROOT_DIR${RESET}"
        return 1
    fi

    # 检查源文件
    if [ ! -f "$hook_source" ]; then
        echo -e "${RED}    ❌  Hook 脚本不存在：$hook_script${RESET}"
        return 1
    fi

    # 检查是否已有 hook
    if [ -f "$hook_target" ] || [ -L "$hook_target" ]; then
        if [ -L "$hook_target" ]; then
            local target
            target=$(readlink "$hook_target")
            local expected="../../scripts/$hook_script"
            if [ "$target" = "$expected" ]; then
                echo -e "${GREEN}    ✅  已安装（软链接已存在）${RESET}"
                return 0
            fi
        fi
        # 已有其他 hook，备份
        local backup="${hook_target}.backup.$(date +%Y%m%d_%H%M%S)"
        mv "$hook_target" "$backup"
        echo -e "${YELLOW}    ⚠️  已有旧 hook，已备份${RESET}"
        echo -e "${DIM}       → ${backup#$ROOT_DIR/}${RESET}"
    fi

    # 创建软链接（相对路径，这样移动仓库也能用）
    ln -s "../../scripts/$hook_script" "$hook_target"
    chmod +x "$hook_source"
    chmod +x "$hook_target"

    echo -e "${GREEN}    ✅  安装成功${RESET}"

    # 显示 hook 说明
    case "$hook_name" in
        pre-commit)
            echo -e "${DIM}       触发时机: git commit 之前${RESET}"
            echo -e "${DIM}       测试内容: Python 单元测试（快速，<1秒）${RESET}"
            echo -e "${DIM}       跳过方式: git commit --no-verify${RESET}"
            ;;
        pre-push)
            echo -e "${DIM}       触发时机: git push 之前${RESET}"
            echo -e "${DIM}       测试内容: 全量测试（Python + JS）${RESET}"
            echo -e "${DIM}       跳过方式: git push --no-verify${RESET}"
            ;;
        commit-msg)
            echo -e "${DIM}       触发时机: git commit 提交信息编写后${RESET}"
            echo -e "${DIM}       检查内容: 提交信息是否符合 Conventional Commits 规范${RESET}"
            echo -e "${DIM}       跳过方式: git commit --no-verify${RESET}"
            ;;
    esac
}

uninstall_single_hook() {
    local hook_name="$1"
    local hook_target="$HOOKS_DIR/$hook_name"

    echo ""
    echo -e "${BOLD}${YELLOW}  卸载 $hook_name${RESET}"
    echo -e "${DIM}  ────────────────────────────────────────${RESET}"

    if [ ! -f "$hook_target" ] && [ ! -L "$hook_target" ]; then
        echo -e "${YELLOW}    ⚠️  Hook 不存在，无需卸载${RESET}"
        return 0
    fi

    rm -f "$hook_target"
    echo -e "${GREEN}    ✅  已卸载${RESET}"

    # 检查有没有备份
    local backup
    backup=$(ls "$hook_target.backup."* 2>/dev/null | head -1 || true)
    if [ -n "$backup" ]; then
        echo ""
        read -p "    检测到旧备份，要恢复吗？(y/N) " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            mv "$backup" "$hook_target"
            echo -e "${GREEN}    ✅  已恢复旧 hook${RESET}"
        fi
    fi
}

show_single_hook_status() {
    local hook_name="$1"
    local hook_script="$2"
    local hook_target="$HOOKS_DIR/$hook_name"
    local hook_source="$SCRIPT_DIR/$hook_script"

    if [ -L "$hook_target" ]; then
        local target
        target=$(readlink "$hook_target")
        local expected="../../scripts/$hook_script"
        if [ "$target" = "$expected" ]; then
            echo -e "  $hook_name:    ${GREEN}✅ 已安装${RESET}"
        else
            echo -e "  $hook_name:    ${YELLOW}⚠️  已安装（指向其他脚本）${RESET}"
            echo -e "  指向:          ${DIM}$target${RESET}"
        fi
    elif [ -f "$hook_target" ]; then
        echo -e "  $hook_name:    ${YELLOW}⚠️  存在（非软链接）${RESET}"
    else
        echo -e "  $hook_name:    ${RED}❌ 未安装${RESET}"
    fi
}

# ── 主操作函数 ────────────────────────────────────────────────────────────────

install_hooks() {
    echo ""
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  🔧  安装 Git Hook${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"

    if [ -n "$TARGET_HOOK" ]; then
        # 安装指定 hook
        if is_valid_hook "$TARGET_HOOK"; then
            install_single_hook "$TARGET_HOOK" "$(get_hook_script "$TARGET_HOOK")"
        else
            echo -e "${RED}  ❌  未知 hook：$TARGET_HOOK${RESET}"
            echo -e "${DIM}     可用：${HOOK_NAMES[*]}${RESET}"
            echo ""
            exit 1
        fi
    else
        # 安装所有 hook
        for i in "${!HOOK_NAMES[@]}"; do
            install_single_hook "${HOOK_NAMES[$i]}" "${HOOK_SCRIPTS[$i]}"
        done
    fi

    echo ""
    echo -e "${GREEN}${BOLD}✅  安装完成！${RESET}"
    echo ""
    echo -e "${DIM}  以后每次 git commit / git push 时会自动跑测试${RESET}"
    echo -e "${DIM}  查看状态：./scripts/install_hooks.sh status${RESET}"
    echo ""
}

uninstall_hooks() {
    echo ""
    echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${YELLOW}  🗑️   卸载 Git Hook${RESET}"
    echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════${RESET}"

    if [ -n "$TARGET_HOOK" ]; then
        if is_valid_hook "$TARGET_HOOK"; then
            uninstall_single_hook "$TARGET_HOOK"
        else
            echo -e "${RED}  ❌  未知 hook：$TARGET_HOOK${RESET}"
            echo ""
            exit 1
        fi
    else
        for i in "${!HOOK_NAMES[@]}"; do
            uninstall_single_hook "${HOOK_NAMES[$i]}"
        done
    fi

    echo ""
}

show_status() {
    echo ""
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  📋  Git Hook 状态${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo ""

    for i in "${!HOOK_NAMES[@]}"; do
        show_single_hook_status "${HOOK_NAMES[$i]}" "${HOOK_SCRIPTS[$i]}"
        echo ""
    done

    echo -e "  Git Hook 体系总览："
    echo -e "    ${DIM}· commit-msg  → 提交信息规范检查（Conventional Commits）${RESET}"
    echo -e "    ${DIM}· pre-commit  → 格式化检查 + 冒烟测试（< 5s）${RESET}"
    echo -e "    ${DIM}· pre-push    → 全量测试（Python + JS，推送前必跑）${RESET}"
    echo ""
    echo -e "  手动运行全部测试：${CYAN}python run_tests.py${RESET}"
    echo ""
}

# ── 主逻辑 ────────────────────────────────────────────────────────────────────
case "$ACTION" in
    install)
        install_hooks
        ;;
    uninstall|remove)
        uninstall_hooks
        ;;
    status|check)
        show_status
        ;;
    *)
        echo "用法: $0 {install|uninstall|status} [hook名]"
        echo ""
        echo "  install              安装所有 hook（默认）"
        echo "  install pre-commit   只安装 pre-commit"
        echo "  install pre-push     只安装 pre-push"
        echo "  uninstall            卸载所有 hook"
        echo "  status               查看 hook 状态"
        echo ""
        echo "可用 hook：${HOOK_NAMES[*]}"
        exit 1
        ;;
esac
