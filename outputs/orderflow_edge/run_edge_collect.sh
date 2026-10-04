#!/bin/bash
# run_edge_collect.sh — 订单流 edge 采集每日 wrapper (M4)
# 由 launchd (com.vanta.edgecollect) 每日 23:40 触发；采集当日日盘+夜盘。
# START_DATE 守卫：2026-10-08 之前静默跳过（见 collect_edge.py）。
set -u
PY=/Users/a123/.workbuddy/binaries/python/envs/default/bin/python3
DIR=/Users/a123/WorkBuddy/edge_collect
SCRIPT=$DIR/collect_edge.py
TALLY=$DIR/tally.csv
LOG=$DIR/run.log
DATE=$(date +%Y-%m-%d)
echo "===== $(date '+%Y-%m-%d %H:%M:%S') edge collect for $DATE =====" >> "$LOG"
$PY "$SCRIPT" --date "$DATE" --tally "$TALLY" >> "$LOG" 2>&1
echo "exit=$?" >> "$LOG"
