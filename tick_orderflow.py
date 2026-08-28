# -*- coding: utf-8 -*-
"""
tick_orderflow.py — 盘口级订单流 → C 维度增量（#3）

把真实 tick 流（买/卖主动成交、买卖盘口量）转成「订单流分数」Δ∈[-100,100]，
喂给 four_dim_strategy.FlowAggregator.push_tick()，与 minishare 快照净流混合进 C_flow。

三大订单流指标：
  · Delta        : 累计主动买量 − 主动卖量（带符号成交量），衡量资金主动方向。
  · Absorption   : 盘口大单被主动流吃掉的程度（吸收 = 盘口薄却被扫，反向压力释放）。
  · Imbalance    : 买盘量 / 卖盘量 失衡比，衡量盘口供需倾斜。

tick 输入格式（每笔，由接入层产生）：
  {"ts": epoch, "symbol": "FG", "price": x, "vol": v, "side": "B"/"S"/"U"|None,
   "bid_vol": bv, "ask_vol": av}
  side 推导：若未给，用 price 相对上一笔 last 的方向近似（涨=主动买 B，跌=主动卖 S）。

本模块纯计算，不负责网络；网络接入在 four_dim_live_runner 的 TickFeedConnector。
"""
import math
import time


def _side_from_tick(price, last, side):
    """确定主动买卖方向。"""
    if side in ("B", "b", "buy", 1):
        return 1
    if side in ("S", "s", "sell", -1):
        return -1
    if last is not None and price > last:
        return 1
    if last is not None and price < last:
        return -1
    return 0


class TickOrderflow:
    """单品种订单流累积器。window 为滚动窗口（秒/笔数）。"""

    def __init__(self, symbol, window=600):
        self.sym = symbol
        self.window = window
        self.ticks = []          # (ts, signed_vol, bid_vol, ask_vol)
        self.cum_delta = 0.0     # 窗口内累计 Delta（带符号量）
        self.last_price = None
        self.last_ts = None

    def push(self, price, vol, side=None, bid_vol=None, ask_vol=None, ts=None):
        ts = ts or time.time()
        s = _side_from_tick(price, self.last_price, side)
        signed = s * float(vol or 0)
        self.cum_delta += signed
        self.ticks.append((ts, signed, bid_vol or 0.0, ask_vol or 0.0))
        # 滚动修剪
        if self.window and len(self.ticks) > self.window:
            old = self.ticks.pop(0)
            self.cum_delta -= old[1]
        self.last_price = price
        self.last_ts = ts

    def _window_ticks(self):
        if not self.window:
            return self.ticks
        return self.ticks  # 已在 push 中滚动修剪

    def delta_score(self):
        """Delta 分数 ∈ [-100,100]：窗口累计带符号量 / 窗口总成交量。"""
        wt = self._window_ticks()
        if not wt:
            return 0.0
        tot = sum(abs(t[1]) for t in wt)
        if tot <= 0:
            return 0.0
        return max(-100.0, min(100.0, 100.0 * self.cum_delta / tot))

    def imbalance_score(self):
        """盘口失衡 ∈ [-100,100]：净盘口量 / 总盘口量。"""
        wt = self._window_ticks()
        bv = sum(t[2] for t in wt)
        av = sum(t[3] for t in wt)
        tot = bv + av
        if tot <= 0:
            return 0.0
        return max(-100.0, min(100.0, 100.0 * (bv - av) / tot))

    def absorption_score(self):
        """吸收分数 ∈ [-100,100]：主动流吃掉盘口的程度。
        用 带符号量 的绝对值 相对 盘口总量 的占比近似（盘口薄却大主动流=强吸收）。"""
        wt = self._window_ticks()
        if not wt:
            return 0.0
        flow_mag = sum(abs(t[1]) for t in wt)
        book = sum(t[2] + t[3] for t in wt)
        if book <= 0:
            return 0.0
        ratio = flow_mag / book
        # ratio>=1 表示主动流超过盘口总量（强吸收），封顶 100
        return max(-100.0, min(100.0, 100.0 * math.tanh(ratio - 0.5)))

    def score(self):
        """综合订单流分数（喂给 push_tick）。Delta 为主，失衡/吸收加权。"""
        d = self.delta_score()
        im = self.imbalance_score()
        ab = self.absorption_score()
        # 同向增强：Delta 主导，失衡同向加成，吸收同向加成
        s = 0.6 * d + 0.25 * im + 0.15 * ab
        # 若三者同向，放大；反向则抵消已在加权中体现
        return round(max(-100.0, min(100.0, s)), 1)

    def as_dict(self):
        return {
            "symbol": self.sym, "delta": self.delta_score(),
            "imbalance": self.imbalance_score(), "absorption": self.absorption_score(),
            "score": self.score(), "ticks": len(self.ticks),
        }


def ticks_from_jsonl(path, symbol=None, limit=None):
    """从 tick 流文件(jsonl) 读取并灌入 TickOrderflow（测试/回放用）。"""
    import json
    tof = TickOrderflow(symbol or "TEST")
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if symbol and t.get("symbol") != symbol:
                continue
            tof.push(t.get("price", 0), t.get("vol", 0), t.get("side"),
                     t.get("bid_vol"), t.get("ask_vol"), t.get("ts"))
            n += 1
            if limit and n >= limit:
                break
    return tof


if __name__ == "__main__":
    # 合成自测：强主动买单流 → 分数应明显为正
    tof = TickOrderflow("FG")
    import random
    random.seed(1)
    p = 1500.0
    for i in range(200):
        # 70% 主动买，价格缓涨
        buy = random.random() < 0.7
        p += 0.2 if buy else -0.2
        tof.push(p, random.uniform(5, 20), "B" if buy else "S",
                 bid_vol=random.uniform(100, 300), ask_vol=random.uniform(100, 300))
    print("合成主动买单流自测:", tof.as_dict())
    assert tof.score() > 10, "强买流分数应为正"
    print("✅ tick_orderflow 自测通过（强买流 → 正分）")
