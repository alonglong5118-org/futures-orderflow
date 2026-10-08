#!/bin/bash
# =============================================================================
# cfmmc 结算单每日自动处理：下载(可选) → 解析 → 落盘 → 归档
# -----------------------------------------------------------------------------
# 由 launchd (com.a123.cfmmc-daily) 每天 16:00 收盘后自动调用。
# 也可手动跑：  bash cfmmc_daily.sh
#
# 下载方式（二选一，wrapper 自动适配）：
#   A. 手动（默认·推荐·干净）：登录 cfmmc.com → 结算单查询 → 另存为 HTML
#      丢进  ./statements/inbox/   （每账户 1 个文件）
#   B. 全自动（灰区）：设 CFMMC_CRAWLER 指向可自动下载的脚本，wrapper 先跑它再解析
#      export CFMMC_CRAWLER=/path/to/cfmmc_crawler_run.sh
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INBOX="$HERE/statements/inbox"
DONE="$HERE/statements/done"
LOG="$HERE/statements/cfmmc_daily.log"
PY="${PYTHON:-python3}"

mkdir -p "$INBOX" "$DONE"
ts() { date '+%F %T'; }

echo "[$(ts)] === cfmmc 每日任务启动 ===" >>"$LOG"

# ① 可选：自动下载（仅当配置了 CFMMC_CRAWLER）
if [ -n "${CFMMC_CRAWLER:-}" ] && [ -x "${CFMMC_CRAWLER}" ]; then
  echo "[$(ts)] 运行自动下载: ${CFMMC_CRAWLER}" >>"$LOG"
  "${CFMMC_CRAWLER}" >>"$LOG" 2>&1 \
    || echo "[$(ts)] ⚠️ 自动下载失败，转为处理 inbox 现有文件" >>"$LOG"
fi

# ② 收集 inbox 中的结算单
shopt -s nullglob
FILES=("$INBOX"/*.html "$INBOX"/*.htm "$INBOX"/*.xls)
if [ ${#FILES[@]} -eq 0 ]; then
  echo "[$(ts)] inbox 无结算单，跳过（请先把结算单 HTML 另存到 $INBOX）" >>"$LOG"
  exit 0
fi
echo "[$(ts)] 发现 ${#FILES[@]} 个结算单文件，开始解析" >>"$LOG"

# ③ 解析 → 落盘 account_monitor_ctp.json
"$PY" "$HERE/cfmmc_statement_parser.py" "${FILES[@]}" --out "$HERE/account_monitor_ctp.json" >>"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  echo "[$(ts)] ⚠️ 解析失败(rc=$RC)，保留 inbox 文件待下次重试" >>"$LOG"
  exit $RC
fi

# ④ 归档已处理文件（按日期），避免重复处理
DATE="$(date '+%Y%m%d')"
mkdir -p "$DONE/$DATE"
for f in "${FILES[@]}"; do
  mv "$f" "$DONE/$DATE/" 2>/dev/null || true
done

echo "[$(ts)] === 完成：已落盘 account_monitor_ctp.json，归档 ${#FILES[@]} 个文件到 done/$DATE ===" >>"$LOG"
