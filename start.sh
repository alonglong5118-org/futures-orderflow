#!/bin/bash
# ============================================================================
# 四维策略 · 启动入口
#   bash start.sh            → 看门狗模式（崩溃/卡死自动重启，推荐生产用）
#   bash start.sh direct     → 直连模式（前台跑，便于调试，不自动重启）
#   其余参数透传给 runner（如 --port 8741 --no-voice）
# ============================================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"

MODE="${1:-watchdog}"
shift 2>/dev/null || true

case "$MODE" in
  direct)
    # 直连调试模式：所有参数透传给 runner，便于加 --once / --no-voice / --port
    exec "$PY" "$DIR/four_dim_live_runner.py" "$@"
    ;;
  watchdog|*)
    # 看门狗模式：跑默认配置即可（端口 8741、带语音）；额外参数忽略，
    # 避免粘贴含 `# 注释` 的多行命令时把中文注释误当 runner 参数。
    chmod +x "$DIR/watchdog.sh" 2>/dev/null
    exec bash "$DIR/watchdog.sh"
    ;;
esac
