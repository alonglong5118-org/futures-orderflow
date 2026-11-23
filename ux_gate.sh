#!/usr/bin/env bash
# ============================================================
# 三感参谋 · A 方案换肤 · 体验验收门禁（终版）
# 判据原则：来自「现象」，不来自「写法」。
#   反例1：查 "999px" 会命中 left:-9999px（假阳性 → 门禁永远通过）
#   反例2：查 "rgba(var(--*-rgb))" 会漏掉裸 token / 白晕 / currentColor（假阴性）
#   正例  ：查 "0 0 Npx"（无偏移模糊 = 发光的形态），再排除 blur=0 的硬边环
# 用法：bash ux_gate.sh [文件路径]
#
# 期望值（a6085c10 起）：F 段应报「登记 13 条 ｜ FAIL 0 条」，exit=0。
#   三态 span = 5.86 / 5.44 / 5.56
#   常态 span = 5.84（L174 = var(--txt-2)）
#   若有人把 L174 改回 var(--mut2)，常态会变 4.36 —— 那时 F 段报 EXPECT，不是 PASS。
# 已作废、勿再引用：常态 4.80 / 6.43（漏了 .hard-filter-bar 自身的 rgba(--mut-rgb,.06) 层，
#   真底 #20272F 而非 #181F27）；.extreme-banner 7.86（背衬取 bg-0 而非 --card，真值 6.47）。
#   两处同一个病：背衬取错层，方向恒为高估。
#   解释与复算留在 ux_static_text.py 的「父链纪律」注里。
# 表不写期望值，就无法判断「0 FAIL」是真干净还是判据被放宽。
# ============================================================
set -u
F="${1:-/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run/four_dim_live.html}"
FAILED=0
DIRF="$(cd "$(dirname "$F")" && pwd)"
MAN_BEFORE="$(cd "$DIRF" && ls -1 | md5)"

# 自指纹：门禁自己的输入（被测物 + 三把尺子 + 附表）在运行期间不得变动。
# 理由：这轮锚点漂移八次，每次"两边都对"，因为两个人在跑两个版本。
#       一次跑在两版之间的结果无法归因，必须判"不可归因"而不是 PASS。
SIG_FILES="$F|$DIRF/ux_gate.sh|$DIRF/ux_contrast.py|$DIRF/ux_motion.py|$DIRF/ux_static_text.py"
sig() { for g in $SIG_FILES; do [ -f "$g" ] && md5 -q "$g" || echo "-"; done | md5; }
SIG_BEFORE="$(sig)"

pass(){ if [ "$2" = "$3" ]; then printf 'PASS  %-34s %s\n' "$1" "$2"; else printf 'FAIL  %-34s 实测=%s 期望=%s\n' "$1" "$2" "$3"; FAILED=$((FAILED+1)); fi; }
le  (){ if [ "$2" -le "$3" ]; then printf 'PASS  %-34s %s\n' "$1" "$2"; else printf 'FAIL  %-34s 实测=%s 上限=%s\n' "$1" "$2" "$3"; FAILED=$((FAILED+1)); fi; }
ge  (){ if [ "$2" -ge "$3" ]; then printf 'PASS  %-34s %s\n' "$1" "$2"; else printf 'FAIL  %-34s 实测=%s 下限=%s\n' "$1" "$2" "$3"; FAILED=$((FAILED+1)); fi; }
info(){ printf 'INFO  %-34s %s\n' "$1" "$2"; }

echo "════ 锚点 ════"
printf 'md5   %s\n行数  %s\n字节  %s\n文件  %s\n\n' \
  "$(md5 -q "$F")" "$(wc -l <"$F" | tr -d ' ')" "$(wc -c <"$F" | tr -d ' ')" "$F"

echo "════ A 结构层（不随色值变；必须全 0）════"
# 光晕：形态判据 = 无偏移 + 有模糊；排除 blur=0 的硬边环（focus ring / inset 描边）
le "彩色光晕" "$(grep -oE 'box-shadow:[^;}]*\b0 0 [0-9]+px' "$F" | grep -v '0 0 0 ' | wc -l | tr -d ' ')" 0
le "drop-shadow" "$(grep -oE 'drop-shadow\([^)]*rgba\(var\(--[a-z-]*-rgb\)' "$F" | wc -l | tr -d ' ')" 0
pass "OK 态无状态色引用" "$(grep -o 'var(--st-ok' "$F" | wc -l | tr -d ' ')" 0
pass "DOM class 保留（零 DOM）" "$(grep -o 'class="[^"]*st-ok' "$F" | wc -l | tr -d ' ')" 6
pass "品牌单点金" "$(grep -o 'var(--brand)' "$F" | wc -l | tr -d ' ')" 1

echo
echo "════ B 回归保护（版本绑定；改动后需重新定标）════"
pass "999px 胶囊"     "$(grep -o 'border-radius:999px' "$F" | wc -l | tr -d ' ')" 15
pass "50% 圆点"       "$(grep -oE 'border-radius:[[:space:]]*50%' "$F" | wc -l | tr -d ' ')" 15
# 注：必须匹配 0?\.5px —— 改造前有 4 处写作 .5px（无前导零），只匹配 0.5px 会少数 4，构成假阴性
pass "0.5px 发丝线"   "$(grep -oE 'border[a-z-]*:0?\.5px solid' "$F" | wc -l | tr -d ' ')" 72

echo
echo "════ C 通道层 ════"
# C1 反色块：亮字压纯红底 = 2.34（远低于 4.5），合规墨色 --st-danger-ink on --st-danger = 5.55
le "亮字压纯红底（CSS 通道）" \
   "$(grep -nE 'background:var\(--(up|red|st-danger)\)' "$F" | grep -c 'color:var(--txt')" 0
ge "反色块用墨字 danger-ink ≥4" "$(grep -o 'var(--st-danger-ink)' "$F" | wc -l | tr -d ' ')" 4
# C2 退化渐变：必须按「解析后 hex」判，不能比 token 名（别名同值，如 --gold==--accent）
info "退化渐变（按 hex 解析）" "$(python3 - "$F" <<'PY'
import re,sys,collections
src=open(sys.argv[1],encoding='utf-8').read()
toks=dict(re.findall(r'(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})',src))
def resolve(v,d=0,seen=None):
    seen=seen or set()
    m=re.fullmatch(r'var\((--[a-z0-9-]+)\)',v.strip())
    if not m or d>6: return v.strip() if not m else '?'+m.group(1)
    t=m.group(1)
    if t in seen: return 'CYCLE:'+t
    if t not in toks: return '?'+t
    return resolve(toks[t],d+1,seen|{t})
bad=[]
for m in re.finditer(r'linear-gradient\(([0-9]+deg),([^)]*)\)',src):
    parts=[p.strip() for p in m.group(2).split(',') if 'var(' in p or p.strip().startswith('#')]
    vals=[resolve(p) for p in parts]
    if len(vals)>=2 and len(set(vals))==1:
        bad.append((src[:m.start()].count(chr(10))+1,vals[0]))
print('%d 处 %s'%(len(bad),bad[:6]))
PY
)"
# C3 同一标签两种颜色（内容层判据，跨 CSS/JS/内联）
info "同标签多色冲突（映射表）" "$(python3 - "$F" <<'PY'
import re,sys,collections
src=open(sys.argv[1],encoding='utf-8').read()
toks=dict(re.findall(r'(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})',src))
def resolve(v,d=0,seen=None):
    seen=seen or set()
    m=re.fullmatch(r'var\((--[a-z0-9-]+)\)',v.strip())
    if not m or d>6: return v.strip() if not m else '?'+m.group(1)
    t=m.group(1)
    if t in seen: return 'CYCLE:'+t
    if t not in toks: return '?'+t
    return resolve(toks[t],d+1,seen|{t})
pairs=[(l,c) for l,c in re.findall(r"'([^']{1,10})'\s*:\s*'(var\(--[a-z0-9-]+\)|#[0-9a-fA-F]{3,8})'",src) if not l.startswith('var(')]
g=collections.defaultdict(collections.Counter)
for l,c in pairs: g[l][resolve(c)]+=1
conf={l:c for l,c in g.items() if len(c)>1}
print('%d 处 %s'%(len(conf),{k:dict(v) for k,v in conf.items()}))
PY
)"
# C4 零值形态（信息项，不判 FAIL；二期处理）
info "零值符号不一致（>=0 给 +）" "$(grep -o ">=0?'+':''" "$F" | wc -l | tr -d ' ')"
# C5 中性近黑残留（#0D0D0D 一类，非 token 的等值灰）
info "非 token 中性灰 rgba(N,N,N)" "$(grep -cE 'rgba\((1[0-9]|2[0-9]|3[0-9]),\1,\1,' "$F")"
echo
echo "-- C6 静态对比度·自动扫描（逐对合成；三数披露：判定 / SKIP / color-only 未覆盖）"
# C1 只认「红底+亮字」一个方向，镜像（浅底+红字）在它下面 12 行都抓不到 —— .kb-reset 就是这么漏的。
# 三数（判定 N / SKIP M / color-only 未覆盖 K）由脚本自己打印；缺一个该段即视为无效 ——
#   不是 PASS，是没测。同一件事算两遍必然出现两个数，两个数会变成下一轮的锚点之争。
python3 "$(dirname "$0")/ux_contrast.py" "$F" > "${TMPDIR:-/tmp}/ux_contrast.out" 2>&1
CH=$?
grep -v ' ← ' "${TMPDIR:-/tmp}/ux_contrast.out" | sed 's/^/   /'
for K in '判定' 'SKIP' 'color-only 未覆盖'; do
  if ! grep -q "$K" "${TMPDIR:-/tmp}/ux_contrast.out"; then
    printf 'FAIL  %-34s %s\n' "C6 三数缺失" "$K"
    FAILED=$((FAILED+1))
  fi
done
if [ "$CH" -ne 0 ]; then FAILED=$((FAILED+1)); fi

echo
echo "════ D 动画极值（硬门禁：极值必须 ≥4.5 且不得低于静止基线）════"
python3 "$(dirname "$0")/ux_motion.py" "$F"; MH=$?
[ "$MH" -ne 0 ] && FAILED=$((FAILED+1))

echo
echo "════ E 人工判读项（脚本判不了，必须人看）════"
echo "-- keyframes 全表（形态：只许动装饰性属性或提高对比度的属性）"
grep -nE '@keyframes' "$F" | sed 's/^/   /' | cut -c1-150
echo "-- 一次性入场动画（豁免，不进 D 段）"
grep -nE 'animation:[a-zA-Z-]+[^;}]*[^e] (ease|linear|ease-out|ease-in)' "$F" | grep -v infinite | sed 's/^/   /' | cut -c1-130 | head -12
echo
echo
echo "════ F 静态对比度·人工登记表（DOM 嵌套、分母在祖先的项；自动扫描覆盖不到的那部分）════"
# 存在的理由：C6 自动扫描有 164 条 color-only 未覆盖，其中就包括 .hf-text span 三态。
# F 段的项必须包含已知 FAIL，否则它证明不了自己活着。
python3 "$(dirname "$0")/ux_static_text.py" "$F" > "${TMPDIR:-/tmp}/ux_static.out" 2>&1
FH=$?
sed 's/^/   /' "${TMPDIR:-/tmp}/ux_static.out"
if [ "$FH" -ne 0 ]; then FAILED=$((FAILED+1)); fi

echo
echo "════ G 目录清单（防误写文件：本目录曾冒出 2 字节文件 =0?var(--up))════"
MAN_AFTER="$(cd "$DIRF" && ls -1 | md5)"
if [ "$MAN_BEFORE" = "$MAN_AFTER" ]; then
  printf 'PASS  %-34s %s\n' "目录清单未变" "$MAN_AFTER"
else
  printf 'FAIL  %-34s 前=%s 后=%s\n' "目录清单变化（本轮有写操作）" "$MAN_BEFORE" "$MAN_AFTER"
  FAILED=$((FAILED+1))
fi

echo
echo "════ 归因自检（输入在运行期间是否被改动）════"
SIG_AFTER="$(sig)"
if [ "$SIG_BEFORE" = "$SIG_AFTER" ]; then
  printf 'PASS  %-34s %s\n' "门禁输入运行期未变（可归因）" "$SIG_AFTER"
else
  printf 'FAIL  %-34s 前=%s 后=%s\n' "门禁输入在运行期被改动" "$SIG_BEFORE" "$SIG_AFTER"
  echo "      本次结果不可归因（跑在两版之间），结论作废，必须在静止状态下重跑。"
  FAILED=$((FAILED+1))
fi

echo
echo "════ 结果 ════"
if [ "$FAILED" -eq 0 ]; then echo "PASS 项全部通过；INFO 与 D 段需人工判读后归档。"; else echo "FAIL $FAILED 项，逐条见上。"; fi
exit "$FAILED"
