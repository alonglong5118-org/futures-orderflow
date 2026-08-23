#!/bin/bash
# ============================================================================
# 天勤真实 tick 订单流源（#3 生产者）启动入口
#   bash start_tqsdk.sh          → 后台 nohup 拉起 backend_tqsdk.py
# 停止：bash stop_tqsdk.sh   或  kill $(cat tqsdk.pid)
# 日志：tqsdk.log
# 注意：backend_tqsdk 主线程跑 TqSdk 行情循环（单线程约束），HTTP/akshare 回退为守护线程。
#       TICK_STREAM_FILE 默认 HERE/tick_stream.jsonl，与 four_dim_live_runner 同名变量一致 → 两进程天然共用。
# ============================================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"
cd "$DIR" || exit 1

if [ -f tqsdk.pid ] && kill -0 "$(cat tqsdk.pid)" 2>/dev/null; then
  echo "backend_tqsdk 已在运行 pid=$(cat tqsdk.pid)"
  exit 0
fi

nohup "$PY" "$DIR/backend_tqsdk.py" >> "$DIR/tqsdk.log" 2>&1 &
echo $! > "$DIR/tqsdk.pid"
echo "backend_tqsdk 已启动 pid=$(cat tqsdk.pid)，日志 tqsdk.log"
