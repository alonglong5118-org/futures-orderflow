#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-1 历史 tick 回放探针（go/no-go 闸门）
=======================================
用天勤 TqBacktest 拉指定合约在一段历史区间的逐笔 tick，确认：
  1) 能否返回数据（天勤免费历史 tick 是否覆盖该合约/时段）；
  2) 字段是否够算订单流：主动买卖方向(direction) / 盘口买卖量(bid_volume1/ask_volume1) / 成交量(volume)。
这是「tick C_flow 接入回测」整条链的前提——拿不到历史 tick，回测侧 C_flow 就无从谈起。

用法：
  python3 probe_tq_backtest.py [SYMBOL] [START] [END] [MAX_TICKS]
默认：FG2405  2024-03-04 09:00:00  2024-03-04 10:00:00  4000
"""
import sys
import os
import json
import time
import signal
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 150


def _on_alarm(signum, frame):
    raise TimeoutError("probe timeout")


def main():
    cfg = json.load(open(os.path.join(HERE, "tq_config.json"), encoding="utf-8"))
    USER = cfg.get("tq_username")
    PASS = cfg.get("tq_password")

    SYM = sys.argv[1] if len(sys.argv) > 1 else "FG2405"
    START = sys.argv[2] if len(sys.argv) > 2 else "2024-03-04 09:00:00"
    END = sys.argv[3] if len(sys.argv) > 3 else "2024-03-04 10:00:00"
    MAX_TICKS = int(sys.argv[4]) if len(sys.argv) > 4 else 4000
    START_DT = datetime.strptime(START, "%Y-%m-%d %H:%M:%S")
    END_DT = datetime.strptime(END, "%Y-%m-%d %H:%M:%S")

    from tqsdk import TqApi, TqAuth, TqBacktest

    print(f"[probe] symbol={SYM}  {START} → {END}  max_ticks={MAX_TICKS}", flush=True)
    print(f"[probe] login user={USER}", flush=True)

    api = TqApi(backtest=TqBacktest(start_dt=START_DT, end_dt=END_DT), auth=TqAuth(USER, PASS))
    ticks = api.get_tick_serial(SYM)

    n = 0
    t0 = time.time()
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(TIMEOUT)
    try:
        while api.wait_update():
            if len(ticks) > n:
                n = len(ticks)
            if n >= MAX_TICKS:
                print(f"[probe] reached MAX_TICKS={MAX_TICKS}, stop early", flush=True)
                break
    except TimeoutError:
        print(f"[probe] TIMEOUT after {time.time()-t0:.0f}s (collected {n} ticks)", flush=True)
    except Exception as e:
        print(f"[probe] ERROR: {repr(e)[:200]}", flush=True)
        traceback.print_exc()
    finally:
        signal.alarm(0)
        try:
            api.close()
        except Exception:
            pass

    print(f"[probe] total ticks={n}  elapsed={time.time()-t0:.0f}s", flush=True)

    if n == 0:
        print("[probe] ❌ NO TICKS — 天勤未返回该合约/时段历史 tick（可能：合约代码错/时段无交易/账户无该数据权限）", flush=True)
        return

    t = ticks.iloc[-1]
    keys = list(t.index)
    has_dir = "direction" in keys
    has_bv = "bid_volume1" in keys
    has_av = "ask_volume1" in keys
    has_vol = "volume" in keys
    has_last = "last_price" in keys
    print("[probe] last tick fields:", keys, flush=True)
    print(f"[probe] 订单流字段可用：direction(主动方向)={has_dir}  bid_volume1(买一量)={has_bv}  "
          f"ask_volume1(卖一量)={has_av}  volume(成交量)={has_vol}  last_price={has_last}", flush=True)

    # 能力结论
    can_delta = has_dir and has_vol
    can_ofi_absorp = has_bv and has_av
    print(f"[probe] 可算 Session Delta(主动买卖净流)={can_delta}  ｜ 可算 OFI/absorption(需盘口深度)={can_ofi_absorp}", flush=True)
    if can_delta:
        print("[probe] ✅ 至少可做订单流 Delta —— 比龙虎榜 C 强，可推进 Phase1/2", flush=True)
    else:
        print("[probe] ⚠️ 连 Delta 都算不了（缺 direction/volume）—— 需换数据源", flush=True)


if __name__ == "__main__":
    main()
