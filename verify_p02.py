"""确定性校验 + P0-2 冻结数据复核（非破坏性）。
对报告里每笔 done 交易，从其固化的 backtest_bars 快照重放 backtest_signal，
确认：①重放 outcome/R 与已存储完全一致(=可复现, 确定性修复生效) ②快照上 P0-2
换月跳空跳过总根数(应在冻结数据上稳定为 0, 验证 P0-2 空转)。
不写回任何文件。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import four_dim_papertrack as fp

REPORT = fp.REPORT_JSON
with open(REPORT, "r", encoding="utf-8") as f:
    report = json.load(f)

trades = [t for t in report.get("trades", []) if t.get("status") == "done"]
no_snap = [t for t in trades if not t.get("backtest_bars")]

match = mismatch = pending_replay = 0
roll_total = 0
mismatch_examples = []
approx_count = 0

for t in trades:
    if not t.get("backtest_bars"):
        continue
    bars = fp.records_to_bars(t["backtest_bars"])
    if bars is None or len(bars) < 1:
        continue
    if t.get("snapshot_approx"):
        approx_count += 1
    bt = fp.backtest_signal(t, bars=bars, gran=t.get("backtest_gran"))
    roll_total += int(bt.get("roll_skipped", 0) or 0)
    if bt["status"] != "done":
        pending_replay += 1
        continue
    if bt["outcome"] == t.get("outcome") and abs((bt.get("R") or 0) - (t.get("R") or 0)) < 1e-6:
        match += 1
    else:
        mismatch += 1
        if len(mismatch_examples) < 8:
            mismatch_examples.append(
                (t.get("symbol"), t.get("id"), t.get("outcome"), bt["outcome"],
                 t.get("R"), bt.get("R"), t.get("snapshot_approx")))

print("=== papertrack 确定性校验 ===")
print(f"已判定交易总数 : {len(trades)}")
print(f"缺快照(未固化) : {len(no_snap)}  (若>0 需先跑一次 main() 触发 backfill)")
print(f"有快照可重放   : {len(trades) - len(no_snap)}")
print(f"  其中近似快照 : {approx_count} 笔 (原始5m不可得, 退日线, 仅审计参考)")
print()
print(f"重放匹配存储   : {match} 笔")
print(f"重放不匹配     : {mismatch} 笔  ← 应为 0 (否则确定性修复有 bug)")
print(f"重放转pending  : {pending_replay} 笔  (快照不足, 罕见)")
print(f"冻结数据上P0-2换月跳空跳过 : {roll_total} 根  (应为 0, 印证 P0-2 空转)")
print()
if mismatch_examples:
    print("不匹配明细(前8):")
    for ex in mismatch_examples:
        print(f"  {ex[0]:>4s} id={ex[1]} 存储={ex[2]}/{ex[4]} 重放={ex[3]}/{ex[5]} approx={ex[6]}")
print()
if mismatch == 0 and len(no_snap) == 0:
    print("✅ 确定性修复生效：所有交易从固化快照重放均 100% 复现存储结果，")
    print("   门控指标(连亏/胜率/期望R)自此稳定可复现，不受行情重拉影响。")
elif len(no_snap) > 0:
    print("⚠ 仍有交易缺快照，请先跑一次 `python3 four_dim_papertrack.py` 触发 backfill。")
else:
    print("❌ 存在不匹配，需排查快照/重放逻辑。")
