"""da龘 战术层：隐秘枢轴 Hidden Pivot (Peter Lakos)。

纯函数、数据无关：输入摆动点序列，推导 a-b-c 结构与 p 目标位 / c 止损锚。
国内期货适配：tick 取整 + 涨跌停板校验 + 跳空跳过(夜盘/午休/隔夜缺口由 find_swings 处理)。

多头结构：a=摆动低点, b=反弹高点, c=回调低点 且 c>a(higher low)
  p = b + (b - a) * 0.618
空头结构：a=摆动高点, b=回落低点, c=反弹高点 且 c<a(lower high)
  p = b - (a - b) * 0.618
止损锚 = c 点（结构有效性锚点，被破则逻辑失效）。
"""

from __future__ import annotations


def find_swings(highs, lows, closes, opens=None, deviation=0.004, depth=3, gap_pct=0.0):
    """ZigZag 摆动点检测。

    deviation: 相对摆动阈值（相对价格，如 0.004 = 0.4%），过滤毛刺。
    depth:     确认一个极值所需的两侧根数。
    opens:     可选开盘价序列；配合 gap_pct 做跳空跳过（夜盘/午休/隔夜缺口）。
    gap_pct:   相对跳空阈值（如 0.0025 = 0.25%）；>0 时启用：本根开盘相对前收
               大幅跳空则重置摆动锚点，避免把缺口误判为 swing 高低点。
    返回 [(idx, 'high'/'low', price), ...] 按时间升序。
    """
    swings = []
    n = len(closes)
    if n < depth * 2 + 2:
        return swings
    last = None  # (type, price)
    for i in range(depth, n - depth):
        # 跳空跳过：缺口处重置摆动链（缺口本身不是真实市场摆动结构）
        if gap_pct > 0 and opens is not None and i > 0 and closes[i - 1] > 0:
            if abs(opens[i] - closes[i - 1]) / closes[i - 1] > gap_pct:
                last = None
        h, l = highs[i], lows[i]
        # ── 2026-10-15 修：横盘/平价不再伪造摆动点 ────────────────────────
        # 缺陷：原判定用 `>=` / `<=` 做「严格大于窗口内所有点」的比较，
        #   当 highs/lows/closes 全部相等（完全横盘）时，每个 i 都满足 is_high，
        #   而 last is None 分支无条件 append ⇒ 平盘被识别出一个假 swing high。
        #   实测：find_swings([100]*20, [100]*20, [100]*20) 返回 [(2,'high',100.0)]，
        #   该假摆动点会继续污染 latest_abc 的 a-b-c 形态判断（虚构成交结构）。
        # 修法：要求窗口内**存在严格更小/更大的点**，即真极值才算摆动；
        #   平价序列自然不产生任何摆动。
        _win_h = highs[i - depth:i + depth + 1]
        _win_l = lows[i - depth:i + depth + 1]
        is_high = h >= max(_win_h) and _win_h.count(h) < len(_win_h)
        is_low = l <= min(_win_l) and _win_l.count(l) < len(_win_l)
        if is_high:
            if last is None or (last[0] == "low" and h > last[1] * (1 + deviation)):
                swings.append((i, "high", h))
                last = ("high", h)
            elif last[0] == "high" and h > last[1] * (1 + deviation):
                swings[-1] = (i, "high", h)
                last = ("high", h)
        elif is_low:
            if last is None or (last[0] == "high" and l < last[1] * (1 - deviation)):
                swings.append((i, "low", l))
                last = ("low", l)
            elif last[0] == "low" and l < last[1] * (1 - deviation):
                swings[-1] = (i, "low", l)
                last = ("low", l)
    return swings


def latest_abc(swings, direction=None):
    """找最近合法 a-b-c 结构。direction=None 自动检测。
    返回 (a, b, c, dir) 或 None。
    """
    if len(swings) < 3:
        return None
    for i in range(len(swings) - 3, -1, -1):
        a, b, c = swings[i], swings[i + 1], swings[i + 2]
        if a[1] == "low" and b[1] == "high" and c[1] == "low" and c[2] > a[2]:
            if direction in (None, 1):
                return (a, b, c, 1)
        if a[1] == "high" and b[1] == "low" and c[1] == "high" and c[2] < a[2]:
            if direction in (None, -1):
                return (a, b, c, -1)
    return None


def round_tick(price, tick):
    return round(round(price / tick) * tick, 6)


def hidden_pivot(abc, tick, limit_up=None, limit_down=None):
    """算 p 目标位 + c 止损锚，tick 取整 + 停板校验。
    abc = (a, b, c, direction)。返回 dict 或 None。
    """
    if abc is None:
        return None
    a, b, c, direction = abc
    if direction == 1:
        p_raw = b[2] + (b[2] - a[2]) * 0.618
    else:
        p_raw = b[2] - (a[2] - b[2]) * 0.618
    p = round_tick(p_raw, tick)
    stop = round_tick(c[2], tick)
    reachable = True
    if direction == 1 and limit_up is not None and p > limit_up:
        reachable = False
    if direction == -1 and limit_down is not None and p < limit_down:
        reachable = False
    gain_pts = round(abs(p - c[2]) / tick) * tick
    return {
        "direction": direction,
        "direction_text": {1: "偏多", -1: "偏空"}[direction],
        "a": round_tick(a[2], tick),
        "b": round_tick(b[2], tick),
        "c": round_tick(c[2], tick),
        "p": p,
        "stop": stop,
        "p_reachable": reachable,
        "gain_pts": round(gain_pts, 4),
    }


if __name__ == "__main__":
    # 简易自测：构造一段 a-b-c 多头结构
    import numpy as np

    np.random.seed(2)
    base = np.linspace(1000, 1050, 60)
    closes = base + np.sin(np.arange(60) / 3) * 8
    highs = closes + 3
    lows = closes - 3
    sw = find_swings(highs, lows, closes, deviation=0.004, depth=3)
    abc = latest_abc(sw, direction=1)
    print("swings:", len(sw), "| abc:", abc)
    if abc:
        print("hidden_pivot:", hidden_pivot(abc, tick=1.0))
