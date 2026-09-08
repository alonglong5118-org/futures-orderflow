#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 诊断：隔离『non-existent instrument』根因。
A) 实时(非回放) get_quote 能否解析 rb2410 —— 验证账户/符号格式。
B) 回放下 get_kline_serial 能否取到历史 K 线 —— 验证历史数据权限(与 tick 区分)。
"""
import os
import sys
import json
import time
import signal
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 120


def _on_alarm(signum, frame):
    raise TimeoutError("diag timeout")


def main():
    cfg = json.load(open(os.path.join(HERE, "tq_config.json"), encoding="utf-8"))
    USER, PASS = cfg.get("tq_username"), cfg.get("tq_password")
    from tqsdk import TqApi, TqAuth, TqBacktest

    # ── A) 实时 quote 解析 ──
    print("=== A) 实时 get_quote(rb2410) ===", flush=True)
    try:
        api = TqApi(auth=TqAuth(USER, PASS))
        q = api.get_quote("rb2410")
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(TIMEOUT)
        n = 0
        while api.wait_update():
            n += 1
            if n >= 3:
                break
        signal.alarm(0)
        print(f"  ✅ live quote 解析成功：last_price={q.get('last_price')} "
              f"ins={q.get('ins_name')} datetime={q.get('datetime')}", flush=True)
        api.close()
    except TimeoutError:
        print("  ⏱ live quote 超时", flush=True)
    except Exception as e:
        print(f"  ❌ live quote 错误: {repr(e)[:160]}", flush=True)
        traceback.print_exc()

    # ── B) 回放 K 线 ──
    print("=== B) 回放 get_kline_serial(rb2410,60) ===", flush=True)
    try:
        api = TqApi(backtest=TqBacktest(start_dt=datetime(2024, 3, 4, 9, 0, 0),
                                        end_dt=datetime(2024, 3, 4, 10, 0, 0)),
                    auth=TqAuth(USER, PASS))
        k = api.get_kline_serial("rb2410", 60)
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(TIMEOUT)
        n = 0
        while api.wait_update():
            if len(k) > n:
                n = len(k)
            if n >= 5:
                break
        signal.alarm(0)
        print(f"  klines 行数={n}", flush=True)
        if n > 0:
            print(f"  ✅ 回放 K 线可取：首行 {dict(k.iloc[0])}", flush=True)
        else:
            print("  ⚠️ 回放 K 线 0 行（历史数据权限或合约问题）", flush=True)
        api.close()
    except TimeoutError:
        print("  ⏱ 回放 K 线超时", flush=True)
    except Exception as e:
        print(f"  ❌ 回放 K 线错误: {repr(e)[:160]}", flush=True)


if __name__ == "__main__":
    main()
