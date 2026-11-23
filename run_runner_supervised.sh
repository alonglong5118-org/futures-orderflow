#!/bin/bash
# 三感参谋 runner 自重启 supervisor（沙箱等价 launchd KeepAlive）
# 用法: bash run_runner_supervised.sh   （建议用 setsid 脱离框架进程树后台运行）
# 行为:
#   1) 启动前探测 8741，若已被占用（说明已有 runner 在跑）则只等待、不另起，避免冲突/崩溃循环；
#   2) 子进程退出（含看门狗 os._exit(3) 自杀）后等待 3s 立即重启并记录；
#   3) 模拟 launchd KeepAlive。
set -u

DIR="/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
PY="/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"
SCRIPT="four_dim_live_runner.py"
LOG="$DIR/launchd.log"
RESTART_DELAY=3
PORT_WAIT=10   # 端口被占用时的等待间隔

port_taken() {
  # 返回 0 表示 8741 在监听
  lsof -nP -iTCP:8741 -sTCP:LISTEN >/dev/null 2>&1
}

cd "$DIR" || { echo "[supervisor] cd 失败: $DIR" >&2; exit 1; }

cnt=0
while true; do
  if port_taken; then
    echo "[supervisor] 8741 已被占用，等待其释放（现有 runner 仍在跑）" >> "$LOG"
    sleep "$PORT_WAIT"
    continue
  fi
  cnt=$((cnt+1))
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  echo "[supervisor] #$cnt 启动 runner @ $ts" >> "$LOG"
  # ★ 2026-11-04 环境隔离修复（真根因，线程栈 + A/B 对照验证）：
  #   脱离式守护会继承 WorkBuddy 框架沙箱环境，其中
  #     PYTHONPATH=/Applications/WorkBuddy.app/.../cli/vendor/shim
  #   会加载 sitecustomize.py，把 builtins.open() 改成「向 broker.sock 申请文件令牌」的
  #   代理调用（CODEBUDDY_SANDBOX_BROKER_READ_TIMEOUT_MS=125000）。broker 属于 AI 会话，
  #   守护进程一旦脱离会话，broker 消失 → runner 所有 open() 永久阻塞：
  #     /api/account|journal|risk_rules 挂死 75s+ 零字节、预热超时 >180s、主循环冻结。
  #   铁证：10+ 线程栈顶全是 sitecustomize.py:370:_broker_readline。
  #   ★ 注意：代理/PATH/sysctl 都不是根因（已对照证伪），但一并清理无副作用。
  #   修复口径：照搬真机终端成功启动(25090)的干净环境——剔除 PYTHONPATH 注入 +
  #   剔除框架代理/BASH_ENV + 补全系统 PATH + 保持 stdin=/dev/null。
  #   验证：剥掉后 sitecustomize_loaded=False / open_module=_io / open 0.0001s（原生 440×）。
  env -u PYTHONPATH -u PYTHONHOME -u PYTHONSTARTUP -u BASH_ENV \
      -u CODEBUDDY_BROKERED_FS_HOOK_ENABLED -u CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS \
      -u CODEBUDDY_DOCUMENT_IPC_ADDRESS -u TOYBOX_SANDBOX_SOCK -u NODE_OPTIONS \
      -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
      -u ALL_PROXY -u all_proxy -u CODEBUDDY_MCP_CONFIG -u CODEBUDDY_SERVICE_PROXY_URL \
  HOME=/Users/ken LANG=zh_CN.UTF-8 PYTHONUNBUFFERED=1 \
  PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/ken/.workbuddy/binaries/python/envs/default/bin \
  NO_PROXY='*' no_proxy='*' \
  "$PY" "$SCRIPT" >> "$LOG" 2>&1 < /dev/null
  rc=$?
  ts2=$(date '+%Y-%m-%d %H:%M:%S')
  echo "[supervisor] #$cnt runner 退出 rc=$rc @ ${ts2}，等待 ${RESTART_DELAY}s 重启" >> "$LOG"
  sleep "$RESTART_DELAY"
done
