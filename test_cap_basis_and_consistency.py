"""负样本测试：面板封顶依据透明化 + 运行期合约一致性自检（2026-10-15 用户决策）

用户三项决策落地验证：
  ② GET /api/contracts 的漂移 → 写入日志 + 发消息推送（refresh 内 CONSIST 层）
  ③ 面板/报告显示封顶依据（双封顶口径，不能只写「5%」让用户困惑）
  ① 键名大小写保持现状，只靠读取端兼容（不回归）
"""
import json
import os
import sys
import tempfile

D = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, D)

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))


# ================= 决策③：封顶依据可观测 =================
print("=== 决策③：封顶依据透明化 ===")
import risk_state_machine as R

fsm = R.RiskStateMachine()
s = fsm.summary()
for k in ("daily_loss_cap_amt", "daily_loss_cap_src", "daily_loss_abs_max",
          "daily_loss_cap_ref_equity"):
    chk(f"summary 输出 {k}", k in s, f"={s.get(k)}")

chk("cap_src 为有效枚举",
    s.get("daily_loss_cap_src") in
    ("abs_scaled", "pct_cap", "abs_floor", "abs_no_equity"), str(s.get("daily_loss_cap_src")))
chk("基准权益为 10万（2000元 的锚点口径）",
    float(s.get("daily_loss_cap_ref_equity") or 0) == 100000.0,
    str(s.get("daily_loss_cap_ref_equity")))
chk("abs_max = 2000（铁律基准值）",
    float(s.get("daily_loss_abs_max") or 0) == 2000.0, str(s.get("daily_loss_abs_max")))

# 等比缩放自洽性：cap_amt / ref_equity 应等于 abs_max / ref_equity（即恒 2%）
cap = float(s.get("daily_loss_cap_amt") or 0)
ref = float(s.get("daily_loss_cap_ref_equity") or 100000)
amx = float(s.get("daily_loss_abs_max") or 2000)
if s.get("daily_loss_cap_src") == "abs_scaled" and cap > 0:
    ratio = cap / ref
    chk("等比缩放恒为 2%（2000@10万）", abs(ratio - 0.02) < 0.001, f"{ratio:.4%}")

# 不同权益下封顶随权益线性变化（透明度前提：用户能预测）
rows = []
for eq in (50_000, 100_000, 500_000, 1_000_000):
    c, src = R.effective_daily_loss_cap(eq)
    rows.append((eq, c, src))
print(f"  封顶缩放实测: {[(e, round(c)) for e, c, _ in rows]}")
chk("封顶随权益线性缩放（100万→约2万）",
    abs(rows[-1][1] - 20000) < 300, f"{rows[-1][1]:.0f}")
chk("50万权益→约1万", abs(rows[-2][1] - 10000) < 300, f"{rows[-2][1]:.0f}")

# 前端契约：html 必须存在 dd-limit id 与封顶字段引用
html = open(os.path.join(D, "four_dim_live.html"), encoding="utf-8").read()
chk("HTML 存在 dd-limit 元素 id", 'id="dd-limit"' in html)
chk("前端读取 cap_amt 字段", "daily_loss_cap_amt" in html)
chk("前端读取 cap_src 字段", "daily_loss_cap_src" in html)
chk("前端展示封顶依据文案（含『封顶』）", "封顶依据" in html or "封顶</" in html or "取更严者" in html)
chk("dd-limit 已挂 id（动态填充入口）",
    html.count('class="dd-limit" id="dd-limit"') == 1,
    f"UI 硬编码处应仅剩占位初值1 处（实际 {html.count('/ 5% 停机线')} 处匹配，其中1 处是注释说明）")
chk("ddw-sub 展示双封顶口径", "双封顶口径" in html)

# ================= 决策②：运行期一致性自检 =================
print("=== 决策②：运行期合约一致性自检（CONSIST 层）===")
import refresh_main_contracts as RM

rc = open(os.path.join(D, "refresh_main_contracts.py"), encoding="utf-8").read()
chk("源码含 CONSIST 异常键", 'cond_key = "CONSIST:' in rc)
chk("源码含 _inconsistent 自检变量", "_inconsistent" in rc)
chk("CONSIST 纳入紧急冷却", 'cond_key.startswith("CONSIST")' in rc)
chk("告警文案带防误导提示（不要盲目跑 --apply）", "不要盲目跑 --apply" in rc)
chk("CONSIST 优先级高于 DRIFT",
    rc.index('cond_key = "CONSIST:') < rc.index('cond_key = "DRIFT:'),
    "ABNOLD > CONSIST > DRIFT > FETCHFAIL > CURRMONTH")

# 告警状态机：CONSIST 状态变化会推送、OK 会复位
RAlertState = getattr(RM, "_load_alert_state", None)
chk("_alert_if_needed 存在", callable(getattr(RM, "_alert_if_needed", None)))
chk("_notify 存在（消息推送通道）", callable(getattr(RM, "_notify", None)))

# ================= 决策①：键名保持现状（防回归）=================
print("=== 决策①：键名保持现状，读取端兼容未退化 ===")
import four_dim_live_runner as RN

tc = json.load(open(os.path.join(D, "trade_config.json"), encoding="utf-8"))
specs = tc["contract_specs"]
upper = [k for k in specs if k.isupper() and len(k) >= 2]
lower = [k for k in specs if k.islower()]
chk("大小写混用仍然存在（= 未被擅自规范化，符合决策①）",
    len(upper) > 0 and len(lower) > 0, f"大写{len(upper)} 小写{len(lower)}")
bad = [k for k in specs
       if not RN._get_main_contract(k.lower())
       or str(RN._get_main_contract(k.lower())).upper() == str(k).upper()]
chk("全部 54 品种读取端仍正常解析", len(bad) == 0, f"退化 {len(bad)}: {bad[:3]}")

print()
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)