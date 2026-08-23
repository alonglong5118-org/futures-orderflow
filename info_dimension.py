# -*- coding: utf-8 -*-
"""四维策略 · 信息维度层（#1 · F 维度定性面）
=================================================
架构（刻意复用 fundamentals.json 的「外部写、runner 只读」模式）：
  · 采集器  fetch_info_dimension.py  跑在系统 python3.9（自带 akshare 1.18），
    抓 生猪现货/成本/供应、地产链、宏观(CPI/PPI/PMI/降准)、现货、宏观快讯 →
    写成 info_dimension.json 缓存（带每项 score∈[-1,1] 与关联品种）。
  · 本模块  info_dimension.py       跑在 live runner(托管 python3.13) 只读缓存，
    合并 info_dimension_manual.json（人工补 限产/疫情/USDA 等 akshare 拿不到的），
    产出每个品种的定性分 info_adj(sym) ∈ [-1,1]。

设计红线：
  · 信息分只喂「live 信号」的 F，绝不喂回测（避免前视偏差）——由调用方决定是否传 F_override。
  · 任何采集失败都不影响 live：缓存缺失/损坏 → 返回中性分(0)，runner 照常运行。
  · score 幅度有界，且仅作为「F 的定性加分/减分」，不直接改方向/手数（防失控）。
"""
from __future__ import annotations
import os, json, time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "info_dimension.json")       # 自动采集（fetch_info_dimension.py 写）
MANUAL = os.path.join(HERE, "info_dimension_manual.json")  # 人工补充（限产/疫情/USDA 等）

# 信息分对 F(∈[-100,100]，与 score_F 同尺度) 的最大平移权重：
# ±1 信息分最多挪动 10 个 F 分（定性微调，不直接改方向/手数，防失控）。
INFO_F_WEIGHT = 10.0
_F_MIN, _F_MAX = -100.0, 100.0  # 与 score_F 输出区间一致
# 宏观项（无特定品种、作用于全部 6 品种）的权重折扣：避免 PMI 等均匀信号喧宾夺主，
# 让品种特异信号（基差/现货/生猪）主导分化。
MACRO_WEIGHT = 0.4
# 单品种聚合时单来源上限，多来源按时间衰减加权求均值再夹到 [-1,1]
_FRESH_HOURS = 24

# 品种 → 关注标签（采集项文本命中的关键词 → 映射到该品种）
SYMBOL_TAGS = {
    "jd": ["鸡蛋", "蛋鸡", "禽", "存栏", "补栏", "禽流感", "种蛋"],
    "lh": ["生猪", "猪", "仔猪", "能繁母猪", "屠宰", "出栏", "猪瘟", "二育", "冻品"],
    "FG": ["玻璃", "浮法", "光伏玻璃", "竣工", "地产", "建材", "沙河"],
    "SA": ["纯碱", "碱", "轻碱", "重碱", "光伏", "玻璃", "地产"],
    "JM": ["焦煤", "蒙煤", "安监", "煤矿", "洗煤", "蒙煤通关"],
    "J":  ["焦炭", "焦化", "钢厂", "粗钢", "限产", "环保", "焦煤"],
}
# 宏观项（无特定品种）→ 影响全部 6 品种（幅度打折）
MACRO_SYMBOLS = ["jd", "lh", "FG", "SA", "JM", "J"]


def _now_ts():
    return time.time()


def load_cache():
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "items" in d:
            return d
    except Exception:
        pass
    return {"updated": None, "items": []}


def load_manual():
    try:
        with open(MANUAL, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "items" in d:
            return d
    except Exception:
        pass
    return {"items": []}


def _tag_to_symbols(text):
    """文本命中哪些品种标签 → 品种集合。"""
    out = set()
    for sym, tags in SYMBOL_TAGS.items():
        for t in tags:
            if t in text:
                out.add(sym)
                break
    return out


def _age_factor(ts):
    if not ts:
        return 1.0
    age_h = (_now_ts() - float(ts)) / 3600.0
    if age_h <= 0:
        return 1.0
    # 24h 内线性衰减到 0.3，超 48h 视为过期(0.1)
    if age_h >= 48:
        return 0.1
    return max(0.3, 1.0 - 0.7 * (age_h / 24.0))


def info_adj(symbol):
    """返回该品种的定性信息分。
    结构：{"score": -1..1, "items": [{text,score,source,ts}], "updated": str, "manual": bool}
    score=0 表示无信息（中性），绝不让异常上抛。"""
    try:
        cache = load_cache()
        manual = load_manual()
        items = []
        wsum, vsum = 0.0, 0.0
        for src in (cache, manual):
            for it in src.get("items", []):
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                # 关联品种判定优先级：macro=True→全部6品种 > 显式 syms(列表) > 显式 sym(单品种) > 文本标签命中
                # （采集器已按品种落好 sym/syms，必须尊重，否则"FG基差"这类不含标签词的项会被漏掉）
                if it.get("macro"):
                    syms = set(MACRO_SYMBOLS)
                elif it.get("syms"):
                    syms = set(it["syms"])
                elif it.get("sym"):
                    syms = {it["sym"]}
                else:
                    syms = _tag_to_symbols(text)
                if symbol not in syms:
                    continue
                s = float(it.get("score", 0.0))
                s = max(-1.0, min(1.0, s))
                af = _age_factor(it.get("ts"))
                # 权重：宏观项打折(MACRO_WEIGHT)；人工覆盖略高(0.6×)；再乘时间衰减
                w = af * (MACRO_WEIGHT if it.get("macro") else 1.0) * (0.6 if src is manual else 1.0)
                vsum += s * w
                wsum += w
                items.append({"text": text[:80], "score": round(s, 2),
                               "source": it.get("source", "?"),
                               "manual": src is manual})
        score = round(max(-1.0, min(1.0, vsum / wsum)), 3) if wsum > 0 else 0.0
        return {
            "score": score,
            "items": items[-6:],  # 最多展示最近 6 条
            "updated": cache.get("updated"),
            "manual": len(manual.get("items", [])) > 0,
            "divergence_note": None,
        }
    except Exception:
        return {"score": 0.0, "items": [], "updated": None, "manual": False,
                "divergence_note": None}


def f_override_for(symbol, base_F):
    """live 路径用：把信息分叠加到 base_F 上，夹到 [-100,100]（与 score_F 同尺度）。
    回测不要调用本函数（避免前视偏差）。"""
    try:
        adj = info_adj(symbol)
        s = adj.get("score", 0.0)
        f2 = max(_F_MIN, min(_F_MAX, float(base_F) + s * INFO_F_WEIGHT))
        return round(f2, 3), adj
    except Exception:
        return float(base_F), {"score": 0.0, "items": [], "updated": None}


def refresh():
    """触发一次采集（subprocess 调用系统 python3.9 的 fetch_info_dimension.py）。
    非阻塞思路：调用方自行决定频率；本函数失败静默返回 False，绝不抛。"""
    try:
        import subprocess
        py = "/usr/bin/python3"
        sc = os.path.join(HERE, "fetch_info_dimension.py")
        if not os.path.exists(sc):
            return False
        subprocess.Popen([py, sc], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def summary():
    """面板/API 用的总览。"""
    cache = load_cache()
    out = {"updated": cache.get("updated"), "by_symbol": {}}
    for sym in SYMBOL_TAGS:
        out["by_symbol"][sym] = info_adj(sym)
    return out


if __name__ == "__main__":
    import pprint
    pprint.pprint(summary())
