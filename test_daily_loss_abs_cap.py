# -*- coding: utf-8 -*-
"""自检 #2 负样本测试：日内亏损绝对额封顶（铁律≤5% / max_loss=2000元）。
口径：2000 元 @10 万基准权益 = 2%，等比缩放并夹在 [200元, 权益×pct] 内。
运行：<fourd_run>/.venv/bin/python test_daily_loss_abs_cap.py"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("rsm", os.path.join(HERE, "risk_state_machine.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

PASS = FAIL = 0
def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")

print(f"[基线] MAX_DAILY_LOSS_AMT={R.MAX_DAILY_LOSS_AMT:.0f} "
      f"REF_EQUITY={R.MAX_DAILY_LOSS_AMT_REF_EQUITY:.0f}")

# ===== A. 封顶额计算口径 =====
cap, src = R.effective_daily_loss_cap(100_000.0)
chk("10万权益→封顶2000(铁律原值)", cap == 2000.0 and src == "abs_scaled", f"{cap}/{src}")

cap, src = R.effective_daily_loss_cap(1_000_000.0)
chk("100万权益→封顶2万(等比放大,≠固定2000)", cap == 20_000.0 and src == "abs_scaled", f"{cap}/{src}")

cap, src = R.effective_daily_loss_cap(50_000.0)
chk("5万权益→封顶1000(等比缩小)", cap == 1000.0, f"{cap}/{src}")

# 极小权益：等比缩放，不设地板（封顶恒=2%）
cap, src = R.effective_daily_loss_cap(1_000.0)
chk("1000权益→封顶20(2%等比)", cap == 20.0 and src == "abs_scaled", f"{cap}/{src}")

# 权益缺失：回退纯绝对额，不崩
cap, src = R.effective_daily_loss_cap(0)
chk("equity=0 → 回退2000且标no_equity", cap == 2000.0 and src == "abs_no_equity", f"{cap}/{src}")
cap, src = R.effective_daily_loss_cap(None)
chk("equity=None → 不崩", cap == 2000.0, f"{cap}/{src}")

# 封顶永远不宽于比例线（这是"双封顶"的核心不变量）
for eq in (1e3, 1e4, 1e5, 1e6, 1e7):
    c, _ = R.effective_daily_loss_cap(eq)
    chk(f"权益{eq:.0e}:封顶{c:.0f} ≤ 比例线{eq*0.05:.0f}", c <= eq * 0.05 + 1e-6, c)

# ===== B. risk_guard 判定 =====
# 10万权益：亏2000 → LOCK（绝对额线，铁律原场景）
rg = R.risk_guard(equity=100_000.0, daily_pnl=-2000.0)
chk("10万亏2000 → LOCK(绝对额线)", rg["status"] == "LOCK", rg["reasons"])
chk("  原因含元达封顶", any("元达封顶" in r for r in rg["reasons"]), rg["reasons"])

# 边界：1999 不触发
rg = R.risk_guard(equity=100_000.0, daily_pnl=-1999.0)
chk("10万亏1999 → OK(边界不误杀)", rg["status"] == "OK", rg["reasons"])

# 10万亏 1900（<绝对额2000，但<比例线5000）→ OK，证明绝对额线真的在起作用
rg = R.risk_guard(equity=100_000.0, daily_pnl=-1900.0)
chk("10万亏1900 → OK(绝对额线比比例线更严)", rg["status"] == "OK", rg["reasons"])

# 关键负样本：证明这不是死代码 —— 若实现成max(绝对额,比例)，1900不会LOCK；
# 若实现成固定2000，100万亏2000会误杀。现在 100万亏2000 必须 OK
rg = R.risk_guard(equity=1_000_000.0, daily_pnl=-2000.0)
chk("100万亏2000 → OK(固定额误杀已消除)", rg["status"] == "OK", rg["reasons"])
rg = R.risk_guard(equity=1_000_000.0, daily_pnl=-20_000.0)
chk("100万亏2万 → LOCK(等比封顶)", rg["status"] == "LOCK", rg["reasons"])
rg = R.risk_guard(equity=1_000_000.0, daily_pnl=-60_000.0)
chk("100万亏6万 → LOCK(6%>5%比例线)", rg["status"] == "LOCK", rg["reasons"])

# 盈利/持平不受影响
for p in (0.0, 5000.0, -0.0):
    rg = R.risk_guard(equity=100_000.0, daily_pnl=p)
    chk(f"daily_pnl={p} → OK", rg["status"] == "OK", rg["reasons"])

# equity 缺失不得误杀（缺数据时不能因为绝对额线就冻结）
rg = R.risk_guard(equity=0.0, daily_pnl=-500.0)
chk("equity=0 亏500 → 不误杀", rg["status"] == "OK", rg["reasons"])

# 返回值新增字段
rg = R.risk_guard(equity=100_000.0, daily_pnl=-1000.0)
for k in ("daily_loss_abs", "daily_loss_cap_amt", "daily_loss_cap_src"):
    chk(f"返回含 {k}", k in rg, list(rg.keys()))

# 保证金红线仍优先且不受影响
rg = R.risk_guard(equity=100_000.0, used_margin=50_000.0, daily_pnl=0.0)
chk("保证金50% → LOCK(红线仍生效)", rg["status"] == "LOCK", rg["reasons"])

# ===== C. 硬熔断侧绝对额线 =====
def mk_ks(opening_eq):
    ks = R.KillSwitch.__new__(R.KillSwitch)
    import threading
    ks._lock = threading.RLock()
    ks._opening_equity = opening_eq
    ks.halted = False; ks.ack = False; ks.reset_at = 0; ks.force_rest_until = 0
    ks.triggers = []; ks.metrics = {}; ks.history = []
    ks.reason = ""; ks.path = os.path.join(tempfile.mkdtemp(), "ks.json")
    ks.plan = []; ks.flatten_plan = []
    return ks

# 10万：硬线 = 2000×1.6 = 3200；亏3300 应触发绝对额硬线
out = mk_ks(100_000.0).check(equity=100_000.0, peak_equity=100_000.0,
                daily_pnl=-3300.0, consec_losses=0, positions=[])
chk("10万亏3300 → 绝对额硬线触发", any("绝对额硬线" in t for t in out["triggers"]), out["triggers"])

# 边界负样本：亏 2500（<3200硬线，且 <8%比例线 8000）→ 不应触发
out = mk_ks(100_000.0).check(equity=100_000.0, peak_equity=100_000.0,
                            daily_pnl=-2500.0, consec_losses=0, positions=[])
chk("10万亏2500 → 不触发硬线", not any("绝对额硬线" in t for t in out["triggers"]), out["triggers"])

# 100万：硬线 = 20000×1.6 = 32000；亏3300 不应触发
out = mk_ks(1_000_000.0).check(equity=1_000_000.0, peak_equity=1_000_000.0,
                              daily_pnl=-3300.0, consec_losses=0, positions=[])
chk("100万亏3300 → 不触发绝对额硬线(等比放大)",
    not any("绝对额硬线" in t for t in out["triggers"]), out["triggers"])
out = mk_ks(1_000_000.0).check(equity=1_000_000.0, peak_equity=1_000_000.0,
                              daily_pnl=-33_000.0, consec_losses=0, positions=[])
chk("100万亏3.3万 → 绝对额硬线触发", any("绝对额硬线" in t for t in out["triggers"]), out["triggers"])

# ===== D. summary 暴露 =====
fsm = R.RiskStateMachine()
fsm.peak_equity = 100_000.0
s = fsm.summary()
chk("summary 含 daily_loss_cap_amt", "daily_loss_cap_amt" in s, list(s.keys()))
chk("summary 含 daily_loss_abs_max", "daily_loss_abs_max" in s, list(s.keys()))

# ===== E. 配置化读取 =====
orig = R.MAX_DAILY_LOSS_AMT
R.MAX_DAILY_LOSS_AMT = 3000.0
cap, _ = R.effective_daily_loss_cap(100_000.0)
chk("配置改3000 → 封顶同步3000", cap == 3000.0, cap)
R.MAX_DAILY_LOSS_AMT = orig

print(f"\n结果：{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)