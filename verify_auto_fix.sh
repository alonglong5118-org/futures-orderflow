#!/bin/bash
BASE=~/WorkBuddy/2026-09-04-07-56-14/fourd_run

echo "=== signals file (修复前卡在 11-02) ==="
ls -la "$BASE/four_dim_signals.json" 2>/dev/null | awk '{print "mtime:",$6,$7,$8,"| size:",$5}'
python3 - "$BASE/four_dim_signals.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print("count:",len(d) if isinstance(d,list) else "n/a")
    if isinstance(d,list) and d:
        for s in d:
            if isinstance(s,dict) and s.get("symbol"):
                print("newest:",s.get("time") or s.get("timestamp") or s.get("ts"),"|",s.get("symbol"),s.get("direction") or s.get("side"))
                break
        else:
            print("newest: (no entry with symbol)")
except Exception as e:
    print("read fail:",e)
PY

echo
echo "=== pti real state ==="
curl -s http://127.0.0.1:8741/api/paper-trading | python3 -c "import json,sys;d=json.load(sys.stdin);print('enabled=',d.get('enabled'),'| equity=',d.get('equity'),'| position_count=',d.get('position_count'),'| last_updated=',d.get('last_updated'))"

echo
echo "=== pti 持仓明细（止损/浮盈是否真挂上) ==="
curl -s http://127.0.0.1:8741/api/paper-trading | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('positions',[]):
    print(' -',p.get('symbol'),p.get('direction'),'lots=',p.get('remaining_lots'),'entry=',p.get('entry_price'),'stop=',p.get('stop_price'),'cur=',p.get('current_price'),'mtm=',p.get('mtm'))
"
echo
echo "=== last_signal symbols (信号管线是否活) ==="
curl -s http://127.0.0.1:8741/api/state | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('symbols',{});h=[x for x,v in s.items() if v.get('last_signal')];print('count:',len(h),h[:8])"

echo
echo "=== eval_skips (明细，判据：含'超1%上限(0)'=旧bug残留) ==="
curl -s http://127.0.0.1:8741/api/state | python3 -c "
import json,sys
d=json.load(sys.stdin)
sk=d.get('eval_skips',{})
print('eval_skips count:',len(sk))
for sym,v in sk.items():
    if isinstance(v,dict):
        print(' -',sym,'stage=',v.get('stage'),'| reason=',v.get('reason'),'| count=',v.get('count'))
    else:
        print(' -',sym,'=',v)
"
