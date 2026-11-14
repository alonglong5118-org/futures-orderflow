# -*- coding: utf-8 -*-
"""负样本测试：沙盒镜像快照的浮盈口径自洽（总览 vs 持仓行必须一致）。
根因：_sync_sandbox_to_account_state 丢弃上游已算好的 profit 重算，
      价真空期价格回落 open_price → 持仓行 0，而 float_total 仍是收盘 mtm。
      实测症状：面板浮盈 +0.00，账户总览 +12350。
运行：<fourd_run>/.venv/bin/python test_sandbox_float_consistency.py"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("amod", os.path.join(HERE, "account_monitor.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

PASS = FAIL = 0
def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")

# ---- 用隔离的临时 state 文件跑 _sync_sandbox_to_account_state ----
TMP = tempfile.mkdtemp()
FAKE_STATE = os.path.join(TMP, "account_state.json")
json.dump({"positions": {}, "realized_pnl": 0.0}, open(FAKE_STATE, "w"))

# 让 account_tracker.load_state/save 指向临时文件
import account_tracker as _at
_saved = {}
_at.load_state = lambda *a, **k: json.load(open(FAKE_STATE))
def _save(st, *a, **k):
    json.dump(st, open(FAKE_STATE, "w"), ensure_ascii=False)
    return True
for _fn in ("save_state", "save", "_save"):
    if hasattr(_at, _fn):
        setattr(_at, _fn, _save)
        break
else:
    _at.save_state = _save


def run(positions, mtm, prices=None, equity=1_007_025.0, margin=32_344.0, avail=974_681.0):
    json.dump({"positions": {}, "realized_pnl": 0.0}, open(FAKE_STATE, "w"))
    paper = {"cash": equity, "realized": 0.0}
    R._sync_sandbox_to_account_state(paper, positions, equity, avail, mtm, margin, prices)
    return json.load(open(FAKE_STATE))


# 场景1（真实故障复现）：价真空期 price=None，上游 profit=12350，mtm=12350
st = run(
    positions=[{"symbol": "ru", "pos": 1, "direction": "多", "open_price": 18830.0,
                "margin": 32344.0, "profit": 12350.0}],
    mtm=12350.0, prices={},
)
snap = st["snapshot"]
s_sum = round(sum(p["float_pnl"] for p in snap["positions"]), 2)
chk("场景1: sum(float_pnl) == float_total",
    abs(s_sum - snap["float_total"]) < 0.01, f"{s_sum} vs {snap['float_total']}")
chk("场景1: 持仓行保留上游 profit=12350（不再归零）",
    snap["positions"][0]["float_pnl"] == 12350.0, snap["positions"][0]["float_pnl"])
chk("场景1: float_at_sync 与 float_total 一致",
    abs(st["float_at_sync"] - snap["float_total"]) < 0.01, st["float_at_sync"])

# 场景2：上游 profit 缺失（老数据）→ 回退自算，且仍自洽
st = run(
    positions=[{"symbol": "ru", "pos": 1, "direction": "多",
                "open_price": 18830.0, "margin": 32344.0}],  # 无 profit
    mtm=0.0, prices={"ru": 18900.0},
)
snap = st["snapshot"]
s_sum = round(sum(p["float_pnl"] for p in snap["positions"]), 2)
chk("场景2: 无 profit 时回退自算(18900-18830)*10=700",
    snap["positions"][0]["float_pnl"] == 700.0, snap["positions"][0]["float_pnl"])
chk("场景2: 仍自洽", abs(s_sum - snap["float_total"]) < 0.01, f"{s_sum} vs {snap['float_total']}")

# 场景3：背离时以持仓行合计为准（护栏生效）
st = run(
    positions=[{"symbol": "ru", "pos": 1, "direction": "多", "open_price": 18830.0,
                "margin": 32344.0, "profit": 500.0}],
    mtm=9999.0, prices={},  # 故意让 mtm 与 profit 背离
)
snap = st["snapshot"]
chk("场景3: 背离被纠正为持仓行合计 500",
    snap["float_total"] == 500.0, snap["float_total"])

# 场景4：多笔持仓求和正确
st = run(
    positions=[
        {"symbol": "ru", "pos": 1, "direction": "多", "open_price": 18830.0, "margin": 100, "profit": 1000.0},
        {"symbol": "zn", "pos": 2, "direction": "空", "open_price": 23000.0, "margin": 200, "profit": -300.0},
    ],
    mtm=700.0, prices={},
)
snap = st["snapshot"]
chk("场景4: 多笔 float_total = 1000 + (-300) = 700",
    snap["float_total"] == 700.0, snap["float_total"])

# 场景5：空持仓不崩
st = run(positions=[], mtm=0.0, prices={})
chk("场景5: 空持仓 float_total=0", st["snapshot"]["float_total"] == 0.0, st["snapshot"]["float_total"])

# 场景6：hash 含浮盈 → 浮盈变化会触发写盘
def _hash_of(positions, mtm):
    import account_tracker as at
    json.dump({"positions": {}, "realized_pnl": 0.0}, open(FAKE_STATE, "w"))
    paper = {"cash": 1000000.0, "realized": 0.0}
    R._sync_sandbox_to_account_state(paper, positions, 1007025.0, 974681.0, mtm, 32344.0, {})
    return json.load(open(FAKE_STATE)).get("_sandbox_hash")

h1 = _hash_of([{"symbol": "ru", "pos": 1, "direction": "多", "open_price": 18830.0,
                "margin": 100, "profit": 1000.0}], 1000.0)
h2 = _hash_of([{"symbol": "ru", "pos": 1, "direction": "多", "open_price": 18830.0,
                "margin": 100, "profit": 2000.0}], 1000.0)
chk("场景6: 浮盈变化 → hash 变化（触发写盘）", h1 != h2, f"{h1} vs {h2}")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)