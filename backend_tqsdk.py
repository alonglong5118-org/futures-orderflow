# -*- coding: utf-8 -*-
"""
backend_tqsdk.py — 天勤/快期真实 tick 订单流源（#3 接入层「生产者」）

职责：把真实 tick 流写成 tick_stream.jsonl（jsonl，每行一笔），
      供 four_dim_live_runner 的 TickFeedConnector 自动 tail 灌入 C_flow。

关键约束（踩坑必看，来自 futures-orderflow-tqsdk 技能）：
  · TqSdk 必须单线程：api.wait_update() 只能在主线程调，绝不在子线程调。
    所以：行情循环放【主线程】；HTTP 服务 / akshare 分钟线回退 放【守护线程】。
  · 免费行情账户【只给郑商所(CZCE)实时 tick】：
      FG/SA(KQ.m@CZCE.*) → 真 tick 订单流；
      JM/J/jd/lh(大商所 DCE) → 免费源完全无 tick → akshare 分钟线回退(近似 Delta)。
  · 午休/非交易时段 get_quote 会阻塞或报 nonexistent → 用交易时段门控，盘前/午休跳过订阅。
  · 合约候选列表：按顺序试，第一个 get_quote 成功且能订阅的合约使用（KQ.m 主连优先）。

tick 输出格式（严格对齐 tick_orderflow.TickOrderflow.push）：
  {"ts":epoch,"symbol":"FG","price":x,"vol":v,"side":"B"/"S"/"U",
   "bid_vol":bv,"ask_vol":av,"data_mode":"tqsdk"|"akshare_min"}

环境变量：
  TICK_STREAM_FILE  输出 jsonl 路径（默认 HERE/tick_stream.jsonl，与 runner 同名变量一致 → 两进程天然共用）
  TQ_HTTP_PORT      HTTP /api/signals 端口（默认 8742，供独立看板）
  SELFTEST=1       不连 TqSdk，用合成 tick 验证「写文件 + tof 消费」链路

用法：
  python backend_tqsdk.py                 # 生产：连天勤，写 jsonl + 起 HTTP
  SELFTEST=1 python backend_tqsdk.py      # 自测：合成 tick，不连网
"""
import os
import sys
import time
import json
import signal
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
TICK_STREAM_FILE = os.environ.get("TICK_STREAM_FILE", os.path.join(HERE, "tick_stream.jsonl"))
TQ_HTTP_PORT = int(os.environ.get("TQ_HTTP_PORT", "8742"))
SELFTEST = os.environ.get("SELFTEST", "0") == "1"

# 品种 → 天勤主连候选 / akshare 回退 symbol / 交易所
# CZCE(FG/SA) 免费真 tick；DCE(JM/J/jd/lh) 免费无 tick → akshare 分钟线近似
SYMBOL_MAP = {
    "FG": {"tq": ["KQ.m@CZCE.FG"],                          "ak": "FG0", "ex": "CZCE"},
    "SA": {"tq": ["KQ.m@CZCE.SA"],                          "ak": "SA0", "ex": "CZCE"},
    "JM": {"tq": ["DCE.JM2609", "DCE.JM2610", "KQ.m@DCE.JM"], "ak": "JM0", "ex": "DCE"},
    "J":  {"tq": ["DCE.J2609",  "DCE.J2610",  "KQ.m@DCE.J"],   "ak": "J0",  "ex": "DCE"},
    "jd": {"tq": ["DCE.JD2509", "DCE.JD2510", "KQ.m@DCE.jd"], "ak": "JD0", "ex": "DCE"},
    "lh": {"tq": ["DCE.LH2509", "DCE.LH2510", "KQ.m@DCE.lh"], "ak": "LH0", "ex": "DCE"},
}
SYMBOLS = list(SYMBOL_MAP.keys())

# 内存累积器（HTTP /api/signals 用，只读，绝不调 api）
from tick_orderflow import TickOrderflow, ticks_from_jsonl
_MEM = {s: TickOrderflow(s) for s in SYMBOLS}
_MEM_LOCK = threading.Lock()

_stop = threading.Event()
_write_lock = threading.Lock()


def write_tick(rec):
    """线程安全地追加一笔 tick 到 jsonl（单 writer）。"""
    rec.setdefault("ts", time.time())
    line = json.dumps(rec, ensure_ascii=False)
    with _write_lock:
        with open(TICK_STREAM_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def in_trading_session():
    """本地时段门控（北京时间）：避免午休/休市时 get_quote 阻塞。
    覆盖日盘 + 夜盘；周末休市。"""
    lt = time.localtime()
    wd = lt.tm_wday  # 0=Mon ... 5=Sat 6=Sun
    if wd >= 5:  # 周六/日
        return False
    h, m = lt.tm_hour, lt.tm_min
    minute = h * 60 + m
    # 日盘：09:00-11:30, 13:30-15:00；夜盘：21:00-23:00（部分品种无夜盘，回退线程跳过无数据即可）
    day1 = 9 * 60 <= minute <= 11 * 60 + 30
    day2 = 13 * 60 + 30 <= minute <= 15 * 60
    night = 21 * 60 <= minute <= 23 * 60
    return day1 or day2 or night


# ─────────────────────────── TqSdk 主线程行情循环（CZCE 真 tick） ───────────────────────────
def run_tqsdk():
    from tqsdk import TqApi, TqAuth
    # P0-17 fix: 优先使用环境变量，回退到配置文件
    _tq_user = os.environ.get("TQ_USERNAME", "")
    _tq_pass = os.environ.get("TQ_PASSWORD", "")
    if not _tq_user or not _tq_pass:
        _cfg_path = os.path.join(HERE, "tq_config.json")
        if os.path.exists(_cfg_path):
            _cfg = json.load(open(_cfg_path, encoding="utf-8"))
            _tq_user = _tq_user or _cfg.get("tq_username", "")
            _tq_pass = _tq_pass or _cfg.get("tq_password", "")
    if not _tq_user or not _tq_pass:
        raise RuntimeError("天勤账号未配置：请设置环境变量 TQ_USERNAME/TQ_PASSWORD 或编辑 tq_config.json")
    api = TqApi(auth=TqAuth(_tq_user, _tq_pass))
    quotes = {}          # sym -> (code, quote)
    prev_vol = {}
    for sym, m in SYMBOL_MAP.items():
        if m["ex"] != "CZCE":
            continue     # DCE 走 akshare 回退，不在天勤订阅
        for cand in m["tq"]:
            try:
                q = api.get_quote(cand)
                quotes[sym] = (cand, q)
                print(f"[tq] 订阅 {sym} -> {cand} OK", flush=True)
                break
            except Exception as e:
                print(f"[tq] 订阅 {sym} {cand} 失败: {e}", flush=True)
    if not quotes:
        print("[tq] 无 CZCE 合约订阅成功（免费源可能限流/未开盘），仅 akshare 回退线程工作", flush=True)
    while not _stop.is_set():
        if not in_trading_session():
            time.sleep(30)
            continue
        try:
            api.wait_update()
        except Exception:
            time.sleep(5)
            continue
        for sym, (code, q) in quotes.items():
            vol = q.volume
            pv = prev_vol.get(sym)
            if pv is None or vol < pv:        # 首笔 / 换日重置
                prev_vol[sym] = vol
                continue
            dvol = vol - pv
            prev_vol[sym] = vol
            if dvol <= 0:
                continue
            lp = q.last_price
            ask1 = q.ask_price1
            bid1 = q.bid_price1
            # 盘口推导主动方向（技能坑3）：>=ask1 主动买；<=bid1 主动卖；中间裂解用 U
            if ask1 and lp >= ask1:
                side = "B"
            elif bid1 and lp <= bid1:
                side = "S"
            else:
                side = "U"
            rec = {
                "ts": time.time(), "symbol": sym, "price": float(lp),
                "vol": float(dvol), "side": side,
                "bid_vol": float(q.bid_volume1) if q.bid_volume1 else None,
                "ask_vol": float(q.ask_volume1) if q.ask_volume1 else None,
                "data_mode": "tqsdk",
            }
            write_tick(rec)
            with _MEM_LOCK:
                _MEM[sym].push(rec["price"], rec["vol"], rec["side"],
                               rec.get("bid_vol"), rec.get("ask_vol"), rec["ts"])


# ─────────────────────────── akshare 分钟线回退（DCE 近似 tick） ───────────────────────────
def _emit_ak_min(sym, close, dvol, side):
    """写一条 akshare 分钟线近似 tick 到 jsonl + 内存累积器。"""
    rec = {
        "ts": time.time(), "symbol": sym, "price": close,
        "vol": dvol, "side": side,
        "bid_vol": None, "ask_vol": None,
        "data_mode": "akshare_min",
    }
    write_tick(rec)
    with _MEM_LOCK:
        _MEM[sym].push(rec["price"], rec["vol"], rec["side"], None, None, rec["ts"])


def run_akshare_fallback():
    """DCE 品种（JM/J/jd/lh）免费源无 tick → akshare 分钟线近似 Delta。
    交易时段：实时增量（volume 差），与 TqSdk 真 tick 互斥不重复。
    非交易/休市：回放最近一个交易日的历史分钟线（按 datetime 去重），
                 让周末/午休打开面板时 C_flow 不空转；标注 data_mode='akshare_min'。"""
    try:
        import akshare as ak
    except Exception as e:
        print(f"[ak] akshare 未安装，DCE 品种无回退: {e}", flush=True)
        return
    prev_min_vol = {}
    seen_keys = {}        # sym -> set(datetime) 已回放历史，避免重复写
    while not _stop.is_set():
        trading = in_trading_session()
        try:
            for sym, m in SYMBOL_MAP.items():
                if m["ex"] != "DCE":
                    continue
                df = ak.futures_zh_minute_sina(symbol=m["ak"], period="1")
                if df is None or len(df) == 0:
                    continue
                df["_dt"] = df["datetime"].astype(str)
                day = df["_dt"].str[:10]
                latest = day.max()
                day_df = df[day == latest].reset_index(drop=True)
                if len(day_df) == 0:
                    continue
                day_df["dvol"] = day_df["volume"].diff().fillna(day_df["volume"])
                if trading:
                    # 实时增量（DCE 无真 tick，此路兜底；与 TqSdk 不冲突）
                    last = day_df.iloc[-1]
                    close = float(last["close"]); vol = float(last["volume"])
                    pv = prev_min_vol.get(sym)
                    if pv is None or vol < pv:      # 首根 / 新交易日重置
                        prev_min_vol[sym] = vol
                        continue
                    dvol = vol - pv
                    prev_min_vol[sym] = vol
                    if dvol <= 0:
                        continue
                    prev = day_df.iloc[-2] if len(day_df) > 1 else None
                    side = "B" if (prev is None or close >= float(prev["close"])) else "S"
                    _emit_ak_min(sym, close, dvol, side)
                    continue
                # 非交易时段：回放最近交易日历史分钟（去重，只写未见过的）
                seen = seen_keys.setdefault(sym, set())
                prev_close = None
                for _, row in day_df.iterrows():
                    dt = row["_dt"]
                    if f"{sym}|{dt}" in seen:
                        prev_close = float(row["close"])
                        continue
                    seen.add(f"{sym}|{dt}")
                    close = float(row["close"]); dvol = float(row["dvol"])
                    side = "B" if (prev_close is None or close >= prev_close) else "S"
                    prev_close = close
                    _emit_ak_min(sym, close, dvol, side)
        except Exception:
            time.sleep(20)
            continue
        time.sleep(25 if trading else 60)


# ─────────────────────────── HTTP 守护线程（独立看板 /api/signals） ───────────────────────────
def run_http():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/signals"):
                with _MEM_LOCK:
                    data = {s: _MEM[s].as_dict() for s in SYMBOLS}
                body = json.dumps({"ok": True, "symbols": data,
                                   "ts": time.time()}, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            elif self.path.startswith("/api/health"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    try:
        HTTPServer(("0.0.0.0", TQ_HTTP_PORT), H).serve_forever()
    except Exception:
        print(f"[http] 端口 {TQ_HTTP_PORT} 启动失败: {traceback.format_exc()}", flush=True)


# ─────────────────────────── 合成自测（不连网） ───────────────────────────
def selftest():
    print("[selftest] 合成 tick 写 jsonl + tof 消费校验 ...", flush=True)
    import random
    random.seed(7)
    # FG：强主动买流（真 tick 形态）
    p = 1500.0
    for i in range(200):
        buy = random.random() < 0.72
        p += 0.3 if buy else -0.3
        write_tick({"ts": time.time(), "symbol": "FG", "price": p,
                    "vol": random.uniform(5, 25), "side": "B" if buy else "S",
                    "bid_vol": random.uniform(100, 300), "ask_vol": random.uniform(100, 300),
                    "data_mode": "tqsdk"})
    # JM：分钟线回退近似（DCE，价格缓涨）
    q = 1480.0
    for i in range(60):
        buy = random.random() < 0.6
        q += 0.5 if buy else -0.5
        write_tick({"ts": time.time(), "symbol": "JM", "price": q,
                    "vol": random.uniform(40, 120), "side": "B" if buy else "S",
                    "bid_vol": None, "ask_vol": None, "data_mode": "akshare_min"})
    # 用 tick_orderflow 从 jsonl 回放，校验消费链路
    tof = ticks_from_jsonl(TICK_STREAM_FILE, symbol="FG")
    d = tof.as_dict()
    print("[selftest] FG 回放:", d, flush=True)
    assert d["score"] > 5, f"强买流分数应明显为正，实际 {d['score']}"
    print("[PASS] backend_tqsdk 自测通过（FG 强买流 → 正分，jsonl→tof 链路 OK）", flush=True)


def main():
    signal.signal(signal.SIGTERM, lambda *a: _stop.set())
    signal.signal(signal.SIGINT, lambda *a: _stop.set())
    # 新会话从头开始：truncate 流文件（避免与旧进程残留行混叠）
    open(TICK_STREAM_FILE, "w", encoding="utf-8").close()
    print(f"[main] tick_stream={TICK_STREAM_FILE} http={TQ_HTTP_PORT} selftest={SELFTEST}", flush=True)
    if SELFTEST:
        selftest()
        return
    threading.Thread(target=run_http, daemon=True).start()
    threading.Thread(target=run_akshare_fallback, daemon=True).start()
    try:
        run_tqsdk()                      # 主线程跑 TqSdk 行情循环
    except Exception:
        print(f"[main] TqSdk 主循环异常: {traceback.format_exc()}", flush=True)
    finally:
        _stop.set()


if __name__ == "__main__":
    main()
