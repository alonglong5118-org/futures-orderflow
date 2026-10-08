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
        self.ticks = []  # (ts, signed_vol, bid_vol, ask_vol)
        self.cum_delta = 0.0  # 窗口内累计 Delta（带符号量）
        self.last_price = None
        self.last_ts = None
        # 吸收比的长期基线（慢速 EMA），用于自归一化。
        # 2026-09-23 新增：各品种「每笔成交量 / 盘口厚度」的量级相差可达 1000 倍
        # （实测 FG 中位 0.0007 vs sc 中位 0.48），固定映射会让因子再次退化为常数。
        self._abs_base = None

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
        """吸收分数 ∈ [-100,100]：主动流吃掉盘口的程度（盘口薄却被大主动流扫过=强吸收）。

        ⚠️ 2026-09-23 修复（原实现退化为常数，std 仅 0.15~0.43、取值恒 ≈-44）：
          原分母 `book = sum(t[2]+t[3])` 是 **window 个盘口快照求和**（600 笔 → 放大约 600 倍），
          而分子 flow_mag 是该窗口真实成交量 —— 量纲不一致，ratio 恒 ≈0.03，
          tanh(0.03-0.5) 恒 ≈-0.44，因子对 score 只贡献常数偏移 ≈-6.6，方差≈0。

        修复要点：
          1. 分母改为【每笔盘口厚度的均值】，与分子的「每笔平均成交量」同量纲；
          2. 与该品种自身长期基线（慢速 EMA）比较后取对数再 tanh —— 自归一化，
             避免跨品种量级差 1000 倍导致再次退化。
        """
        wt = self._window_ticks()
        if not wt:
            return 0.0
        flow_mag = sum(abs(t[1]) for t in wt)
        books = [t[2] + t[3] for t in wt if (t[2] + t[3]) > 0]
        if not books or flow_mag <= 0:
            return 0.0
        book_avg = sum(books) / len(books)
        per_tick_flow = flow_mag / len(wt)
        r = per_tick_flow / book_avg if book_avg > 0 else 0.0
        if r <= 0:
            return 0.0
        # 自归一化基线：在【对数域】取累计均值（= 几何均值）。
        # 迭代记录：① EMA(α=0.01) 有初始化偏差（开盘最活跃把基线拉高 → 整日恒为负，
        #   rb 实测 max 恰为 0.00）；② 改用算术累计均值后，因 r 重尾被极值拉高，
        #   负值仍占 73~87%。对数域均值等价于几何均值，对重尾稳健，正负基本对称。
        lr = math.log(r)
        if self._abs_base is None:
            self._abs_base = lr
            self._abs_n = 1
            return 0.0
        self._abs_n += 1
        self._abs_base += (lr - self._abs_base) / self._abs_n
        # 前 3 个窗口基线样本太少、噪声主导，不产出分数
        if self._abs_n < 4:
            return 0.0
        return max(-100.0, min(100.0, 100.0 * math.tanh(lr - self._abs_base)))

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
            "symbol": self.sym,
            "delta": self.delta_score(),
            "imbalance": self.imbalance_score(),
            "absorption": self.absorption_score(),
            "score": self.score(),
            "ticks": len(self.ticks),
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
            tof.push(t.get("price", 0), t.get("vol", 0), t.get("side"), t.get("bid_vol"), t.get("ask_vol"), t.get("ts"))
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
        tof.push(
            p,
            random.uniform(5, 20),
            "B" if buy else "S",
            bid_vol=random.uniform(100, 300),
            ask_vol=random.uniform(100, 300),
        )
    print("合成主动买单流自测:", tof.as_dict())
    assert tof.score() > 10, "强买流分数应为正"
    print("✅ tick_orderflow 自测通过（强买流 → 正分）")
