#!/bin/bash
# ==============================================================================
#  Git Hook 安装/管理脚本
# ==============================================================================
#  用法：
#    ./scripts/install_hooks.sh          # 安装 pre-push hook
#    ./scripts/install_hooks.sh uninstall # 卸载
#    ./scripts/install_hooks.sh status    # 查看状态
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
HOOK_NAME="pre-push"
HOOK_SOURCE="$SCRIPT_DIR/pre-push-regression.sh"
HOOK_TARGET="$HOOKS_DIR/$HOOK_NAME"

# ── 操作 ──────────────────────────────────────────────────────────────────────
ACTION="${1:-install}"

install_hook() {
    echo ""
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  🔧  安装 Git Hook${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo ""

    # 检查 git 仓库
    if [ ! -d "$HOOKS_DIR" ]; then
        echo -e "${RED}❌  不是 git 仓库：$ROOT_DIR${RESET}"
        exit 1
    fi

    # 检查源文件
    if [ ! -f "$HOOK_SOURCE" ]; then
        echo -e "${RED}❌  Hook 脚本不存在：$HOOK_SOURCE${RESET}"
        exit 1
    fi

    # 检查是否已有 hook
    if [ -f "$HOOK_TARGET" ]; then
        if [ -L "$HOOK_TARGET" ]; then
            # 已经是软链接，指向我们的脚本
            target=$(readlink "$HOOK_TARGET")
            if [ "$target" = "$HOOK_SOURCE" ] || [ "$target" = "../../scripts/pre-push-regression.sh" ]; then
                echo -e "${GREEN}✅  已安装（软链接已存在）${RESET}"
                echo ""
                exit 0
            fi
        fi
        # 已有其他 hook，备份
        backup="$HOOK_TARGET.backup.$(date +%Y%m%d_%H%M%S)"
        mv "$HOOK_TARGET" "$backup"
        echo -e "${YELLOW}⚠️  已有旧 hook，已备份到：${backup#$ROOT_DIR/}${RESET}"
    fi

    # 创建软链接
    ln -s "../../scripts/pre-push-regression.sh" "$HOOK_TARGET"
    chmod +x "$HOOK_SOURCE"
    chmod +x "$HOOK_TARGET"

    echo -e "${GREEN}✅  安装成功！${RESET}"
    echo ""
    echo -e "  Hook 类型:  ${BOLD}pre-push${RESET}"
    echo -e "  触发时机:  ${DIM}git push 之前${RESET}"
    echo -e "  测试模式:  ${DIM}快速模式（100 bars，~30秒）${RESET}"
    echo -e "  跳过方式:  ${CYAN}git push --no-verify${RESET}"
    echo ""
    echo -e "${DIM}  智能跳过：只有策略相关文件改动时才跑测试${RESET}"
    echo -e "${DIM}  监控文件：four_dim_strategy.py / strategy_layer.py / trade_config.json 等${RESET}"
    echo ""
}

uninstall_hook() {
    echo ""
    echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${YELLOW}  🗑️   卸载 Git Hook${RESET}"
    echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════${RESET}"
    echo ""

    if [ ! -f "$HOOK_TARGET" ] && [ ! -L "$HOOK_TARGET" ]; then
        echo -e "${YELLOW}⚠️  Hook 不存在，无需卸载${RESET}"
        echo ""
        exit 0
    fi

    rm -f "$HOOK_TARGET"
    echo -e "${GREEN}✅  已卸载 pre-push hook${RESET}"

    # 检查有没有备份
    backup=$(ls "$HOOK_TARGET.backup."* 2>/dev/null | head -1 || true)
    if [ -n "$backup" ]; then
        echo ""
        read -p "  检测到旧备份，要恢复吗？(y/N) " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            mv "$backup" "$HOOK_TARGET"
            echo -e "${GREEN}✅  已恢复旧 hook${RESET}"
        fi
    fi
    echo ""
}

show_status() {
    echo ""
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  📋  Git Hook 状态${RESET}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════════════${RESET}"
    echo ""

    if [ -L "$HOOK_TARGET" ]; then
        target=$(readlink "$HOOK_TARGET")
        echo -e "  pre-push:    ${GREEN}✅ 已安装（软链接）${RESET}"
        echo -e "  指向:        ${DIM}$target${RESET}"
    elif [ -f "$HOOK_TARGET" ]; then
        echo -e "  pre-push:    ${YELLOW}⚠️  存在（非软链接）${RESET}"
    else
        echo -e "  pre-push:    ${RED}❌ 未安装${RESET}"
    fi

    echo ""
    echo -e "  Hook 脚本:   ${DIM}scripts/pre-push-regression.sh${RESET}"
    if [ -f "$HOOK_SOURCE" ]; then
        echo -e "  脚本存在:    ${GREEN}✅${RESET}"
    else
        echo -e "  脚本存在:    ${RED}❌${RESET}"
    fi

    echo ""
    echo -e "  相关文件（改动时触发测试）："
    echo -e "    ${DIM}· four_dim_strategy.py${RESET}"
    echo -e "    ${DIM}· strategy_layer.py${RESET}"
    echo -e "    ${DIM}· trade_config.json${RESET}"
    echo -e "    ${DIM}· feature_flags.json${RESET}"
    echo -e "    ${DIM}· calibration_drift.json${RESET}"
    echo ""
}

# ── 主逻辑 ────────────────────────────────────────────────────────────────────
case "$ACTION" in
    install)
        install_hook
        ;;
    uninstall|remove)
        uninstall_hook
        ;;
    status|check)
        show_status
        ;;
    *)
        echo "用法: $0 {install|uninstall|status}"
        echo ""
        echo "  install   安装 pre-push hook（默认）"
        echo "  uninstall 卸载 pre-push hook"
        echo "  status    查看 hook 状态"
        exit 1
        ;;
esac
