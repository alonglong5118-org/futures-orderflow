#!/usr/bin/env zsh
# ============================================================================
# verify_account.sh —— 账户状态「一键对账」自检（仿 verify_fees.sh）
#
# 适用范围：四维策略面板（four_dim）账户总览与成交记录的一致性核对
# 本脚本【只做】幂等自愈 → 重启后端 → 实测对账，
# 绝不修改任何账户配置，也绝不改动任何成交数据（自愈仅对齐 journal 权威源）。
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
# 顶部：人工提醒（本脚本只读对账 + 幂等自愈，不改业务数据）
# ============================================================================
hr
printf '%s%s【账户一键对账 · 自检】%s\n' "$BOLD" "$YEL" "$RST"
hr
printf '%s①【人工·须知】%s 本脚本只做事后核对，不修改任何账户/成交数据。\n' "$BOLD" "$RST"
printf '   若对账出现 DIFF，请人工排查 trade_journal.json 与 account_state 的漂移来源，\n'
printf '   切勿直接手工改 JSON 掩盖问题（应先查漂移是怎么产生的）。\n'
printf '%s②【脚本·自动】%s 幂等自愈（以 journal 为权威源对齐 account_state）→ 重启后端\n' "$BOLD" "$RST"
printf '   → curl 实测对账（total_fee / realized_pnl，容差 0.5 元）。\n'
hr
printf '\n'

# ============================================================================
# ② 自愈：用托管 venv python 跑内联代码（先 cd 到项目根以便 import）
# ============================================================================
printf '%s【步骤二 · 自愈 account_state（以 journal 为真相源）】%s\n' "$BOLD" "$GRN"
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
import account_tracker as at
ok, changes, state = at.heal_from_journal()
print("== heal_from_journal 自愈变更 ==")
print("\n".join(changes) if changes else "  (无偏差，无需修改)")
print("  heal ok =", ok)
PYEOF
printf '\n'

# ============================================================================
# ③ 重启后端服务（launchctl）
# ============================================================================
printf '%s【步骤三 · 重启后端服务】%s\n' "$BOLD" "$GRN"
if ! command -v launchctl >/dev/null 2>&1; then
    printf '%s警告：未找到 launchctl 命令（which launchctl 无结果），跳过重启。%s\n' "$YEL" "$RST"
    printf '将继续后续 curl 实测（若后端未运行，curl 会返回空并给出诊断）。\n'
else
    SVC_PATH="gui/$(id -u)/$SVC"
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
# ④ 实测对账：/api/account vs /api/journal_list（容差 0.5 元）
# ============================================================================
printf '%s【步骤四 · 实测对账】%s\n' "$BOLD" "$GRN"

# 说明：用「环境变量」把 curl 的 JSON 传给 python（避免 echo | python - <<EOF
# 这种「管道 + heredoc」组合在 zsh 下会让 heredoc 抢占 stdin，导致 JSON 被当成
# python 代码执行而报 NameError: null is not defined）。环境变量方式最稳健。

export ACCOUNT_JSON JL_JSON
ACCOUNT_JSON=$(curl -s "http://localhost:$PORT/api/account")
JL_JSON=$(curl -s "http://localhost:$PORT/api/journal_list")

if [[ -z "$ACCOUNT_JSON" ]]; then
    printf '%s诊断：curl /api/account 返回为空，后端可能未启动或端口 %s 无响应。%s\n' "$RED" "$PORT" "$RST"
    printf '请检查 launchctl 服务状态：launchctl print gui/%s/%s\n' "$(id -u)" "$SVC"
fi
if [[ -z "$JL_JSON" ]]; then
    printf '%s诊断：curl /api/journal_list 返回为空，后端可能未启动或端口 %s 无响应。%s\n' "$RED" "$PORT" "$RST"
fi

if [[ -n "$ACCOUNT_JSON" && -n "$JL_JSON" ]]; then
    "$PYTHON" - <<'PYEOF'
import os, json

TOL = 0.5  # 容差 0.5 元

def load(raw, tag):
    try:
        return json.loads(raw)
    except Exception as e:
        print("  解析 %s JSON 失败：%s" % (tag, e))
        return None

acc = load(os.environ.get("ACCOUNT_JSON", ""), "/api/account")
jl = load(os.environ.get("JL_JSON", ""), "/api/journal_list")
if acc is None or jl is None:
    raise SystemExit(1)

trades = jl.get("trades", []) if isinstance(jl, dict) else []
closed = [t for t in trades if t.get("pnl") is not None]

# 账户侧
acc_fee = acc.get("total_fee")
acc_pnl = acc.get("realized_pnl")
# journal 侧：已平仓成交的 fee_total / pnl 之和
jnl_fee = sum(float(t.get("fee_total") or 0.0) for t in closed)
jnl_pnl = sum(float(t.get("pnl") or 0.0) for t in closed)

print("  账户 /api/account   : total_fee=%s  realized_pnl=%s" % (acc_fee, acc_pnl))
print("  journal 已平仓 %d 笔 : fee_total 合计=%.2f  pnl 合计=%.2f" % (len(closed), jnl_fee, jnl_pnl))
print("")

verdicts = []

def check(name, a, b):
    if a is None:
        print("  DIFF  %-28s 账户侧字段缺失（account=%s, journal=%.2f）" % (name, a, b))
        verdicts.append(False)
        return
    diff = abs(float(a) - b)
    if diff <= TOL:
        print("  PASS  %-28s account=%.2f  journal=%.2f  |diff|=%.4f" % (name, float(a), b, diff))
        verdicts.append(True)
    else:
        print("  DIFF  %-28s account=%.2f  journal=%.2f  |diff|=%.4f (>%.1f)" % (name, float(a), b, diff, TOL))
        verdicts.append(False)

check("total_fee vs Σfee_total", acc_fee, jnl_fee)
check("realized_pnl vs Σpnl", acc_pnl, jnl_pnl)

print("")
if all(verdicts):
    print("  总结论：PASS —— 账户总览与成交记录一致（容差 %.1f 元内）。" % TOL)
else:
    print("  总结论：DIFF —— 存在超差项，请人工排查 journal 与 account_state 漂移来源。")
    raise SystemExit(1)
PYEOF
else
    printf '%s诊断：两个接口至少一个无返回，无法对账。%s\n' "$RED" "$RST"
fi
RC=$?

# ============================================================================
# 总结
# ============================================================================
hr
if [[ $RC -eq 0 ]]; then
    printf '%s【总结】%s 对账完成。上方逐项 PASS 即账户数据可信；\n' "$BOLD" "$YEL"
    printf '  如出现 DIFF，先跑 verify_fees.sh 确认费率无误，再查 journal/account_state 漂移。\n'
else
    printf '%s【总结】%s 对账未通过或接口异常，请按上方诊断信息排查后重跑本脚本。\n' "$BOLD" "$RED"
fi
hr
exit $RC
