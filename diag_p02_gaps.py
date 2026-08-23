"""P0-2 诊断：扫描全部已判定信号的前向窗口，统计开盘相对前收的真实跳变幅度。
回答两个问题：
  (1) 数据里到底有没有换月跳空（|open-prevclose|/prevclose 较大的根）？
  (2) 现有阈值 ROLL_GAP_PCT=1.0% / ROLL_GAP_MULT=1.0*stop 是否曾触发？
不写回任何文件。"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import four_dim_papertrack as fp

REPORT = fp.REPORT_JSON
SIGNALS = fp.SIGNALS_JSON
with open(REPORT, "r", encoding="utf-8") as f:
    report = json.load(f)
with open(SIGNALS, "r", encoding="utf-8") as f:
    signals = json.load(f)

old_ids = set(t["id"] for t in report["trades"])
parsed = [fp.parse_signal(s) for s in signals]
parsed = [p for p in parsed if p and p["id"] in old_ids]

print(f"扫描信号数 : {len(parsed)}")
print()
# 统计每根前向 bar 的 gap 比例分布
all_gaps = []          # 所有 gap 比例
gap_by_sym = {}        # 每品种最大 gap
triggered = 0          # 命中现有阈值的根数
for p in parsed:
    symbol = p["symbol"]
    try:
        sdt = fp.pd.to_datetime(p["time"])
    except Exception:
        continue
    bars, gran = fp._load_backtest_bars(symbol, sdt)
    if bars is None or len(bars) < 2:
        continue
    seq = bars.iloc[1:] if len(bars) > 1 else bars.iloc[0:0]
    prev_close = float(bars.iloc[0]["close"])
    stop_dist = abs(p["stop"] - p["entry"]) or 1e-9
    thr = max(fp.ROLL_GAP_PCT * prev_close, fp.ROLL_GAP_MULT * stop_dist)
    mx = 0.0
    for _, row in seq.iterrows():
        o = float(row["open"]); c = float(row["close"])
        gap = abs(o - prev_close)
        ratio = gap / prev_close if prev_close else 0
        all_gaps.append(ratio)
        if ratio > mx:
            mx = ratio
        if gap > thr:
            triggered += 1
        prev_close = c
    gap_by_sym[symbol] = max(gap_by_sym.get(symbol, 0.0), mx)

print(f"前向 bar 总数(估) : {len(all_gaps)}")
print(f"命中现有阈值的根数 : {triggered}  (P0-2 实际会跳过的数量)")
print()
import statistics
if all_gaps:
    all_gaps_sorted = sorted(all_gaps)
    n = len(all_gaps_sorted)
    def pct(q): return all_gaps_sorted[min(n-1, int(q*n))]
    print(f"gap比例  p50={pct(0.5)*100:.3f}%  p90={pct(0.9)*100:.3f}%  p99={pct(0.99)*100:.3f}%  max={max(all_gaps)*100:.3f}%")
print()
print(f"{'品种':>4s} | {'前向窗口最大开盘跳变':>22s}")
print("-"*32)
for s in sorted(gap_by_sym):
    print(f"{s:>4s} | {gap_by_sym[s]*100:>20.3f}%")

print()
print("结论判读:")
print("  - 若 '命中阈值的根数'=0 且 max gap 普遍 <1% → 主连数据已调整，换月跳空不存在，P0-2 是空转。")
print("  - 若 max gap 很大(如>3%)但命中=0 → 阈值过高或 stop_dist 算错，需下调阈值。")
print("  - 若命中>0 → 修复确实有效，需查为何本次 verify 显示 0。")
