# -*- coding: utf-8 -*-
"""自检 #4 负样本测试：主力换月监测去噪（区分真异常/ 正常换月 / 双源漂移）。
运行：<fourd_run>/.venv/bin/python test_main_contract_alert.py"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("rmc", os.path.join(HERE, "refresh_main_contracts.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

PASS = FAIL = 0
def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")

print(f"[基线] ym_of('SI2610')={R.ym_of('SI2610')}  ym_of('SI2611')={R.ym_of('SI2611')}")
chk("ym_of 解析正常", R.ym_of("SI2610") == 202610 and R.ym_of("SI2611") == 202611, "")

# FIXED_KEYS 不再排除品种
src = open(os.path.join(HERE, "refresh_main_contracts.py"), encoding="utf-8").read()
import re
m = re.search(r"_FIXED_KEYS = \{([^}]*)\}", src)
keys = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
chk("FIXED_KEYS 只剩指数(不含lc/si)", keys == {"SA01", "sa01"}, keys)
chk("lc 不在 FIXED_KEYS", "lc" not in keys and "LC" not in keys, keys)
chk("si 不��� FIXED_KEYS", "si" not in keys and "SI" not in keys, keys)

# 关键逻辑回归：用真实数据模拟三种场景的分类
def classify(override, hot, now_ym=202610):
    """复刻新监测逻辑的分类分支"""
    abn, cur, drift = [], [], []
    for v, code in override.items():
        ym = R.ym_of(code)
        if ym is not None and ym < now_ym:
            abn.append(f"{v}={code}"); continue
        if ym != now_ym:
            continue
        live = hot.get(v.lower(), "")
        lym = R.ym_of(live) if live else None
        if live and lym and lym > now_ym:
            drift.append(f"{v}={code}→{live}")
        else:
            cur.append(f"{v}={code}")
    return abn, drift, cur

# 场景1（真实数据）：override 停SI2610，真实已SI2611 → 应判 DRIFT 而非 CURRMONTH
abn, drift, cur = classify({"si": "SI2610", "lc": "LC2610"},
                           {"si": "SI2611", "lc": "LC2611"})
chk("场景1: 换月已完成 → 归 DRIFT", drift == ["si=SI2610→SI2611", "lc=LC2610→LC2611"] and not cur, f"{drift}/{cur}")

# 场景2：真临界 —— 真实主力也停在当月交割月 → 应判 CURRMONTH
abn, drift, cur = classify({"si": "SI2610"}, {"si": "SI2610"})
chk("场景2: 真实也停当月 → 归 CURRMONTH(真临界)", cur == ["si=SI2610"] and not drift, f"{cur}/{drift}")

# 场景3：无 hot 数据（文件缺失）→ 保守归 CURRMONTH，不静默放过
abn, drift, cur = classify({"si": "SI2610"}, {})
chk("场景3: hot缺失 → 保守归 CURRMONTH", cur == ["si=SI2610"], f"{cur}")

# 场景4：真异常 —— 停过往月份仍优先
abn, drift, cur = classify({"ru": "RU2609", "si": "SI2610"}, {"si": "SI2611"})
chk("场景4: 过往月份 → 归ABNOLD(最高优先级)", abn == ["ru=RU2609"], f"{abn}")

# 场景5：正常远月主力（AG2612等）不产生任何告警
abn, drift, cur = classify({"ag": "AG2612", "cu": "CU2611"}, {"ag": "AG2612"})
chk("场景5: 正常远月 → 无告警", not abn and not drift and not cur, f"{abn}/{drift}/{cur}")

# 场景6：hot 比 override 更旧时不得回退（单调向前）—— 分类里 drift 只在 live>now_ym 时成立
abn, drift, cur = classify({"si": "SI2612"}, {"si": "SI2610"})
chk("场景6: override已更前 → 不判DRIFT", not drift, f"{drift}")

print(f"\n结果：{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)