#!/bin/bash
# ==============================================================================
#  Futures OrderFlow · Docker 入口脚本
#
#  用法（通过 CMD 或 docker run 指定）：
#    docker run ... futures-orderflow live          # 实盘模式（默认）
#    docker run ... futures-orderflow once          # 单次评估（测试）
#    docker run ... futures-orderflow test          # 跑测试
#    docker run ... futures-orderflow smoke         # 冒烟测试
#    docker run ... futures-orderflow shell         # 进入 shell
#    docker run ... futures-orderflow python ...    # 运行 python 命令
#
#  环境变量：
#    PORT              面板端口（默认 8741）
#    MODE              运行模式：watchdog / direct（默认 watchdog）
#    NO_VOICE          关闭语音（1=关闭）
#    TUSHARE_TOKEN     Tushare Pro token
#    WATCHDOG_STALL    看门狗卡死判定秒数（默认 90）
# ==============================================================================

set -e

cd /app

# ── 环境变量默认值 ────────────────────────────────────────────────────────────
export PORT="${PORT:-8741}"
export MODE="${MODE:-watchdog}"
WATCHDOG_STALL="${WATCHDOG_STALL:-90}"
WATCHDOG_RESTART="${WATCHDOG_RESTART:-4}"

# ── 确保目录存在 ──────────────────────────────────────────────────────────────
mkdir -p /app/logs /app/data /app/config

# ── 配置文件软链接（如果用户挂载了 config 卷）─────────────────────────────────
if [ -d /app/config ]; then
    for f in tq_config.json trade_config.json main_overrides.json watchlist.json; do
        if [ -f "/app/config/$f" ] && [ ! -L "/app/$f" ]; then
            ln -sf "/app/config/$f" "/app/$f"
        fi
    done
fi

# ── 命令路由 ──────────────────────────────────────────────────────────────────
case "${1:-live}" in

  live)
    # ── 实盘模式 ──────────────────────────────────────────────────────────
    shift
    echo "[entrypoint] 启动四维策略实盘运行器"
    echo "[entrypoint] 端口: ${PORT} | 模式: ${MODE} | 语音: ${NO_VOICE:-开启}"

    RUNNER_ARGS=("--port" "${PORT}")
    [ "${NO_VOICE}" = "1" ] && RUNNER_ARGS+=("--no-voice")
    RUNNER_ARGS+=("$@")

    if [ "${MODE}" = "watchdog" ]; then
        # 看门狗模式：崩溃/卡死自动重启
        echo "[entrypoint] 看门狗模式：卡死判定 ${WATCHDOG_STALL}s，重启间隔 ${WATCHDOG_RESTART}s"
        while true; do
            python four_dim_live_runner.py "${RUNNER_ARGS[@]}" >> /app/logs/runner.log 2>&1
            CODE=$?
            echo "[watchdog] runner 退出 code=${CODE}，${WATCHDOG_RESTART}s 后重启" \
                >> /app/logs/watchdog.log
            sleep "${WATCHDOG_RESTART}"
        done
    else
        # 直连模式：前台运行
        exec python four_dim_live_runner.py "${RUNNER_ARGS[@]}"
    fi
    ;;

  once)
    # ── 单次评估（测试用）─────────────────────────────────────────────────
    shift
    echo "[entrypoint] 单次评估模式"
    exec python four_dim_live_runner.py --once --no-voice --port "${PORT}" "$@"
    ;;

  test)
    # ── 运行测试 ──────────────────────────────────────────────────────────
    shift
    echo "[entrypoint] 运行测试"
    exec python run_tests.py --py-only "$@"
    ;;

  smoke)
    # ── 冒烟测试 ──────────────────────────────────────────────────────────
    echo "[entrypoint] 冒烟测试"
    exec python run_tests.py smoke --py-only
    ;;

  shell|bash|sh)
    # ── 进入 shell ────────────────────────────────────────────────────────
    shift
    exec "${SHELL:-/bin/bash}" "$@"
    ;;

  python)
    # ── 运行 python 命令 ─────────────────────────────────────────────────
    shift
    exec python "$@"
    ;;

  *)
    # ── 其他命令透传 ──────────────────────────────────────────────────────
    exec "$@"
    ;;
esac
