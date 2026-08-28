# -*- coding: utf-8 -*-
"""minishare 端点权限自检（随时可跑，token 换了就来验证）
用法: python minishare_permission_check.py
结论(2026-08-11, token CWIFB8... 4周不限次):
  可用: rt_fut_k(实时快照, 990合约, 不限次)
  不可用(权限不足): fut_daily/fut_5min/fut_1min/fut_tick/fut_k/fut_main/
    fut_cont/fut_daily_main/fut_kline/fut_basis/fut_inventory/fut_spot
  → 历史/基本面端点需单独权限；当前 token 只能做实时快照。
"""

import minishare as m

TOKEN = None
try:
    import json
    import os

    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "minishare.json")))
    TOKEN = cfg.get("token")
except Exception:
    pass

if not TOKEN:
    print("未找到 minishare.json token")
    raise SystemExit(1)

m.set_token(TOKEN)
pro = m.pro_api()
ENDPOINTS = [
    "rt_fut_k",
    "fut_daily",
    "fut_5min",
    "fut_1min",
    "fut_tick",
    "fut_k",
    "fut_main",
    "fut_cont",
    "fut_daily_main",
    "fut_kline",
    "fut_basis",
    "fut_inventory",
    "fut_spot",
]
print(f"{'端点':<16}{'状态':<10}说明")
print("-" * 50)
for api in ENDPOINTS:
    try:
        if api == "rt_fut_k":
            df = pro.query(api, ts_code="*")
        else:
            df = pro.query(api, ts_code="FG2509", start_date="20260801", end_date="20260810")
        print(f"{api:<16}{'OK':<10}rows={len(df) if df is not None else 'None'}")
    except Exception as e:
        msg = str(e).replace("MiniSharePermissionError: ", "")
        print(f"{api:<16}{'DENIED':<10}{msg[:40]}")
