#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 修正探针：用后端真实符号格式 KQ.m@CZCE.FG 测天勤 tick 可用性。
测三项：
  L) 实时 get_quote(KQ.m@CZCE.FG)             —— 符号能否解析
  Bc) 回放 get_tick_serial(KQ.m@CZCE.FG)       —— 主连能否回放 tick
  Bs) 回放 get_tick_serial(CZCE.FG2405)        —— 具体合约(带交易所前缀)能否回放 tick
结论决定：FG/SA 真 tick C_flow 是否可行；以及回测该用主连还是具体合约。
"""
import os, sys, json, time, signal, traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 120
def _on_alarm(s, f): raise TimeoutError("timeout")

def main():
    cfg = json.load(open(os.path.join(HERE, "tq_config.json"), encoding="utf-8"))
    USER, PASS = cfg.get("tq_username"), cfg.get("tq_password")
    from tqsdk import TqApi, TqAuth, TqBacktest

    def live_quote(sym):
        print(f"=== LIVE get_quote({sym}) ===", flush=True)
        try:
            api = TqApi(auth=TqAuth(USER, PASS))
            q = api.get_quote(sym)
            signal.signal(signal.SIGALRM, _on_alarm); signal.alarm(TIMEOUT)
            n = 0
            while api.wait_update():
                n += 1
                if n >= 3: break
            signal.alarm(0)
            print(f"  ✅ live OK：last={q.get('last_price')} name={q.get('ins_name')} dt={q.get('datetime')}", flush=True)
            api.close()
        except TimeoutError:
            print("  ⏱ 超时", flush=True)
        except Exception as e:
            print(f"  ❌ {repr(e)[:140]}", flush=True)

    def backtest_tick(sym):
        print(f"=== BACKTEST get_tick_serial({sym}) ===", flush=True)
        try:
            api = TqApi(backtest=TqBacktest(start_dt=datetime(2024,3,4,9,0,0),
                                           end_dt=datetime(2024,3,4,10,0,0)),
                        auth=TqAuth(USER, PASS))
            tk = api.get_tick_serial(sym)
            signal.signal(signal.SIGALRM, _on_alarm); signal.alarm(TIMEOUT)
            n = 0
            while api.wait_update():
                if len(tk) > n: n = len(tk)
                if n >= 3000:
                    print("  (达到 3000 ticks，提前停)", flush=True); break
            signal.alarm(0)
            print(f"  ticks={n}", flush=True)
            if n > 0:
                t = tk.iloc[-1]; keys = list(t.index)
                print(f"  ✅ 回放 tick 可取！字段: {keys}", flush=True)
                print(f"     有 direction={('direction' in keys)} bid1={('bid_volume1' in keys)} "
                      f"ask1={('ask_volume1' in keys)} volume={('volume' in keys)}", flush=True)
            else:
                print("  ⚠️ 回放 0 tick（无历史 tick 权限/合约）", flush=True)
            api.close()
        except TimeoutError:
            print("  ⏱ 超时", flush=True)
        except Exception as e:
            print(f"  ❌ {repr(e)[:140]}", flush=True)

    live_quote("KQ.m@CZCE.FG")
    backtest_tick("KQ.m@CZCE.FG")
    backtest_tick("CZCE.FG2405")

if __name__ == "__main__":
    main()
