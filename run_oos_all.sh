#!/bin/bash
# 四维策略 全市场 OOS 回测：崩溃自动续跑
# 用途：替代已暂停的「OOS回测看门狗」AI 自动化 —— 零 token、不限时段、崩溃 5 秒重启。
# 依赖脚本自带的三保险：fcntl 单实例锁 / 断点续跑 / 单品种 480s 超时跳过。
# 因此循环重跑只会接着往下跑，不会重复已完成的品种；跑完自然退出。
#
# 用法：./run_oos_all.sh              # 前台跑（可 Ctrl-C）
#      nohup ./run_oos_all.sh &       # 后台跑，日志见 _oos_all.log
set -u

DIR=/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run
PY=/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3
MAX_RETRY=20

cd "$DIR" || exit 1

i=0
until $PY -u four_dim_oos_compare.py --all; do
    code=$?
    i=$((i + 1))
    echo "[$(date '+%F %T')] 进程退出（码 $code），第 $i 次重启"
    if [ "$i" -ge "$MAX_RETRY" ]; then
        echo "[$(date '+%F %T')] 已重启 $MAX_RETRY 次仍未完成，停止。请查 _oos_all.log 末尾。"
        exit 1
    fi
    sleep 5
done

echo "[$(date '+%F %T')] 全量回测完成"
