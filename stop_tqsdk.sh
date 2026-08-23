#!/bin/bash
# 停止 backend_tqsdk 订单流源
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/tqsdk.pid" ]; then
  PID="$(cat "$DIR/tqsdk.pid")"
  if kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null
    echo "backend_tqsdk 已发送 SIGTERM pid=$PID"
  else
    echo "pid=$PID 未运行"
  fi
  rm -f "$DIR/tqsdk.pid"
else
  echo "无 tqsdk.pid，尝试按名查找"
  pkill -f backend_tqsdk.py 2>/dev/null && echo "已 kill" || echo "未发现进程"
fi
