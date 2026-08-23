#!/usr/bin/env zsh
# ============================================================================
# verify_fees.sh —— 期货费率改动「四步闸门」第 2~4 步一键校验
#
# 适用范围：四维策略面板（four_dim）费率改动后的运维对账
# 本脚本【只做】自愈历史脏数据 → 重启后端 → 实测对账，
# 绝不修改任何费率配置，也绝不改动任何成交数据。
# ============================================================================

# ---- 固定路径与常量（请按需调整）------------------------------------------
PYTHON="/Users/ken/.workbuddy/binaries/python/envs/default/bin/python3"
ROOT="/Users/ken/WorkBuddy/futures-orderflow"
SVC="com.ken.futures-orderflow.live"   # launchctl 服务 label
PORT=8741

# ---- 颜色（zsh 下用 printf，避免依赖 tput 失败）---------------------------
BOLD=$'\033[1m'; YEL=$'\033[33m'; RED=$'\033[31m'; GRN=$'\033[32m'; RST=$'\033[0m'

hr() { printf '%s\n' "============================================================================"; }

# ============================================================================
# 顶部：人工 checklist 提醒（第①步是人工步骤，脚本无法自动完成，必须显眼）
# ============================================================================
hr
printf '%s%s【期货费率改动 · 四步闸门 自检】%s\n' "$BOLD" "$YEL" "$RST"
hr
printf '%s①【人工·必做】%s 请确认你已用「真实交易所/期货公司标准费率」修改\n' "$BOLD" "$RST"
printf '   trade_journal.py 的 _FEE_SCHEDULE（联网核实：金粮/平安/建信期货公布表），\n'
printf '   切勿近似或漏配（曾误把 OI 菜油按万6近似、CF 棉花漏配回落默认万1 算错）。\n'
printf '%s②【脚本·自动】%s 本脚本将自动执行：自愈历史脏数据 → 重启后端 → 实测对账。\n' "$BOLD" "$RST"
printf '   脚本不修改任何费率或成交数据，仅做幂等自愈 + 重启 + 验证。\n'
hr
printf '\n'

# ============================================================================
# ② 自愈：用托管 venv python 跑内联代码（先 cd 到项目根以便 import）
# ============================================================================
printf '%s【步骤二 · 自愈历史脏数据】%s\n' "$BOLD" "$GRN"
if [[ ! -x "$PYTHON" ]]; then
    printf '%s错误：托管 venv python 不存在或不可执行：%s%s\n' "$RED" "$PYTHON" "$RST"
    printf '请检查 PYTHON 路径配置后重试。\n'
    exit 1
fi
if [[ ! -d "$ROOT" ]]; then
    printf '%s错误：项目根目录不存在：%s%s\n' "$RED" "$ROOT" "$RST"
    exit 1
fi

cd "$ROOT" || { printf '%s错误：无法进入项目根 %s%s\n' "$RED" "$ROOT" "$RST"; exit 1; }

"$PYTHON" - <<'PYEOF'
import trade_journal as tj
fee_changes = tj.heal_fees()
import account_tracker as at
ok, state_changes, state = at.heal_from_journal()
print("== journal 手续费自愈变更 ==")
print("\n".join(fee_changes) if fee_changes else "  (无偏差，无需修改)")
print("== account_state 同步变更 ==")
print("\n".join(state_changes) if state_changes else "  (无偏差，无需修改)")
PYEOF
printf '\n'

# ============================================================================
# ③ 重启后端服务（launchctl）
# ============================================================================
printf '%s【步骤三 · 重启后端服务】%s\n' "$BOLD" "$GRN"
# 探测 launchctl 是否存在
if ! command -v launchctl >/dev/null 2>&1; then
    printf '%s警告：未找到 launchctl 命令（which launchctl 无结果），跳过重启。%s\n' "$YEL" "$RST"
    printf '将继续后续 curl 实测（若后端未运行，curl 会返回空并给出诊断）。\n'
else
    SVC_PATH="gui/$(id -u)/$SVC"
    # 探测服务是否存在
    if launchctl print "$SVC_PATH" >/dev/null 2>&1; then
        printf '重启服务：%s ...\n' "$SVC_PATH"
        launchctl kickstart -k "$SVC_PATH"
        printf '已发送 kickstart，等待端口 %s 起来（sleep 3s）...\n' "$PORT"
        sleep 3
    else
        printf '%s警告：服务 %s 不存在（launchctl print 探测失败），跳过重启。%s\n' "$YEL" "$SVC_PATH" "$RST"
        printf '将继续后续 curl 实测（若后端未运行，curl 会返回空并给出诊断）。\n'
    fi
fi
printf '\n'

# ============================================================================
# ④ 实测对账
# ============================================================================
printf '%s【步骤四 · 实测对账】%s\n' "$BOLD" "$GRN"

# 说明：以下用「环境变量」把 curl 的 JSON 传给 python（避免 echo | python - <<EOF
# 这种「管道 +  heredoc」组合在 zsh 下会让 heredoc 抢占 stdin，导致 JSON 被当成
# python 代码执行而报 NameError: null is not defined）。环境变量方式最稳健。

# 4.1 账户累计 total_fee
export ACCOUNT_JSON
ACCOUNT_JSON=$(curl -s "http://localhost:$PORT/api/account")
if [[ -z "$ACCOUNT_JSON" ]]; then
    printf '%s诊断：curl /api/account 返回为空，后端可能未启动或端口 %s 无响应。%s\n' "$RED" "$PORT" "$RST"
    printf '请检查 launchctl 服务状态：launchctl print %s%s%s\n' "gui/$(id -u)/" "$SVC" ""
else
    printf '账户总览 /api/account：\n'
    "$PYTHON" - <<'PYEOF'
import os, json
raw = os.environ.get("ACCOUNT_JSON", "")
if not raw.strip():
    print("  诊断：curl 返回为空，后端可能未启动或端口无响应。")
    raise SystemExit(0)
try:
    d = json.loads(raw)
except Exception as e:
    print("  解析 JSON 失败：", e)
    raise SystemExit(0)
print("  账户累计 total_fee =", d.get("total_fee"))
PYEOF
fi

# 4.2 逐笔已平仓成交（pnl 非 None）对账
export JL_JSON
JL_JSON=$(curl -s "http://localhost:$PORT/api/journal_list")
if [[ -z "$JL_JSON" ]]; then
    printf '%s诊断：curl /api/journal_list 返回为空，后端可能未启动或端口 %s 无响应。%s\n' "$RED" "$PORT" "$RST"
else
    printf '成交明细 /api/journal_list（仅列出已平仓 pnl 非 None 的成交）：\n'
    "$PYTHON" - <<'PYEOF'
import os, json
raw = os.environ.get("JL_JSON", "")
if not raw.strip():
    print("  诊断：curl 返回为空，后端可能未启动或端口无响应。")
    raise SystemExit(0)
try:
    d = json.loads(raw)
except Exception as e:
    print("  解析 JSON 失败：", e)
    raise SystemExit(0)
trades = d.get("trades", []) if isinstance(d, dict) else []
if not trades:
    print("  (无成交记录或返回结构异常)")
for t in trades:
    if t.get("pnl") is not None:
        print("  symbol=%-5s open_fee=%-8s close_fee=%-8s fee_total=%-8s pnl=%s" % (
            t.get("symbol"), t.get("open_fee"), t.get("close_fee"),
            t.get("fee_total"), t.get("pnl")))
PYEOF
fi

# ============================================================================
# 总结
# ============================================================================
hr
printf '%s【总结】%s 请把上述「账户累计 total_fee」与预期值比对：\n' "$BOLD" "$YEL"
printf '  当前预期约 200.83（= OI 12.0 + CF 25.8 + lh 163.03 之和）。\n'
printf '  若偏差 > 1 元，回到第①步确认 _FEE_SCHEDULE 费率是否准确（OI/CF 为固定费率，\n'
printf '  lh 为按价比例费率），并重新运行本脚本。\n'
hr
