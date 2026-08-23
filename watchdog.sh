#!/bin/bash
# ============================================================================
# 四维策略 · 进程看门狗（#16）
# 崩溃自动重启 + 卡死健康检查：runner 意外退出或被判定卡死时自动拉起。
#
# 用法：  bash watchdog.sh [--port 8741] [runner 的其它参数...]
#   环境变量 PORT 可覆盖端口（默认 8741）。
#   健康检查走 /api/health：连续 90s 无响应 → 判卡死 → 强杀重启。
# ============================================================================
PORT="${PORT:-8741}"
PY="/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"
DIR="$(cd "$(dirname "$0")" && pwd)"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
STALL_SEC=90          # HTTP 连续无响应超此时长 → 判卡死
RESTART_INTERVAL=4    # 崩溃/重启间隔（秒）
LOGFILE="${DIR}/watchdog.log"

log() { echo "[watchdog $(date '+%F %T')] $*" >> "$LOGFILE"; }

log "看门狗启动 · 端口 ${PORT} · 日志 ${LOGFILE}"

while true; do
  "$PY" "$DIR/four_dim_live_runner.py" "$@" >> "$LOGFILE" 2>&1 &
  CHILD=$!
  log "已启动 runner pid=${CHILD}"
  STALL=0
  while kill -0 "$CHILD" 2>/dev/null; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      STALL=0
    else
      STALL=$((STALL+10))
      if [ "$STALL" -ge "$STALL_SEC" ]; then
        log "HTTP 无响应 ${STALL}s，判定卡死，强杀 pid=${CHILD} 并重启"
        kill -9 "$CHILD" 2>/dev/null
        break
      fi
    fi
    sleep 10
  done
  wait "$CHILD" 2>/dev/null
  CODE=$?
  log "runner 退出 code=${CODE}，${RESTART_INTERVAL}s 后重启"
  sleep "$RESTART_INTERVAL"
done
