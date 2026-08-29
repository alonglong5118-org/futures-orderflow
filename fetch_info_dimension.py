"""四维策略 · 信息维度采集器（#1 · 跑在系统 python3.9，自带 akshare）
========================================================================
被 live runner 通过 info_dimension.refresh() 以 subprocess 调用（默认每 6h）。
只写 info_dimension.json；绝不读信号/下任何单。任何源失败 → 跳过该项，不抛异常。

覆盖（akshare 1.18.64 实测可用函数）：
  · 生猪 lh     : index_hog_spot_price / futures_hog_supply / futures_hog_cost
  · 玻璃FG/纯碱SA : macro_china_real_estate（地产链）+ futures_spot_price(FG/SA)
  · 焦煤JM/焦炭J  : futures_spot_price(JM/J) + 宏观(PMI/地产) 代理
  · 鸡蛋 jd     : futures_spot_price(jd) + 农产品通胀(macro_china_cpi)
  · 宏观统一     : reserve_requirement_ratio(降准利多) / pmi / cpi / ppi / news_economic_baidu
未覆盖（限产/疫情/USDA 等）交给 info_dimension_manual.json 人工补。
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "info_dimension.json")


def _ts():
    return time.time()


def _now_iso():
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _shift_ymd(ymd, days):
    """YYYYMMDD ± N 天 → YYYYMMDD（用于取对比窗口的较早交易日）。"""
    try:
        import datetime as _dt

        t = time.strptime(ymd, "%Y%m%d")
        d = _dt.date(t.tm_year, t.tm_mon, t.tm_mday) - _dt.timedelta(days=days)
        return d.strftime("%Y%m%d")
    except Exception:
        return ymd


def _col(df, candidates):
    """在 df 列里按候选名顺序找第一个存在的列。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        print(f"[fetch_info] {fn.__name__} 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _latest_change(series):
    """series: 数值 list（旧→新）。返回最近一跳的归一化变化 (-1..1)。"""
    try:
        vals = [float(x) for x in series if x is not None]
        if len(vals) < 2:
            return 0.0
        prev, cur = vals[-2], vals[-1]
        if prev == 0:
            return 0.0
        chg = (cur - prev) / abs(prev)
        return max(-1.0, min(1.0, chg * 5.0))  # 放大 5 倍便于小波动可见，封顶 ±1
    except Exception:
        return 0.0


def _hog(items):
    """生猪现货/供应/成本 → lh 评分。"""
    out = []
    try:
        import akshare as ak

        # 生猪现货成交均价走势（列: 成交均价）
        df = _safe(lambda: ak.index_hog_spot_price())
        if df is not None and len(df):
            col = _col(df, ["成交均价", "成交价格", "预售均价", "指数"])
            if col:
                s = _latest_change(df[col].tolist())
                out.append(
                    {
                        "text": f"生猪现货成交均价{'上行' if s > 0 else '下行'}（近周）",
                        "score": round(s, 2),
                        "source": "ak.index_hog_spot_price",
                        "ts": _ts(),
                        "sym": "lh",
                    }
                )
        # 供应（能繁/出栏）指标（列: value）
        sup = _safe(lambda: ak.futures_hog_supply())
        if sup is not None and len(sup):
            val_col = _col(sup, ["value", "数值", "最新值"]) or (sup.columns[-1] if sup.shape[1] >= 2 else None)
            if val_col:
                s = _latest_change(sup[val_col].tolist())
                # 供应↑ 偏空（供过于求）
                out.append(
                    {
                        "text": f"生猪供应指标{'增加' if s > 0 else '减少'}",
                        "score": round(-s, 2),
                        "source": "ak.futures_hog_supply",
                        "ts": _ts(),
                        "sym": "lh",
                    }
                )
    except Exception as e:
        print(f"[fetch_info] hog 异常: {e}", file=sys.stderr)
    return out


def _real_estate(items):
    """地产链 → FG/SA 评分。景气涨跌幅为负=走弱=偏空（玻璃/纯碱需求端）。"""
    out = []
    try:
        import akshare as ak

        df = _safe(lambda: ak.macro_china_real_estate())
        if df is not None and len(df):
            chg_col = _col(df, ["涨跌幅", "最新值"])
            if chg_col:
                # 涨跌幅本身即景气变化率（小数），直接作情绪分（夹到 [-1,1]）
                try:
                    latest = float(df[chg_col].iloc[-1])
                except Exception:
                    latest = 0.0
                s = max(-1.0, min(1.0, latest))
                out.append(
                    {
                        "text": f"地产景气指数{'改善' if s > 0 else '走弱'}（{chg_col}）",
                        "score": round(s * 0.8, 2),
                        "source": "ak.macro_china_real_estate",
                        "ts": _ts(),
                        "sym": None,
                    }
                )  # 宏观类→全部
    except Exception as e:
        print(f"[fetch_info] real_estate 异常: {e}", file=sys.stderr)
    return out


def _spot(items):
    """期货现货基差率走势 → 对应品种（FG/SA/JM/J/jd/lh）。
    futures_spot_price(date=, vars_list=) 返回每日现货/基差表；用最近两可得交易日的
    dom_basis_rate 变化作为收敛/走阔信号（contango 收敛偏多，走阔偏空）。
    现货价自身变化若有也叠加。"""
    out = []
    try:
        import akshare as ak

        ak_syms = ["SA", "FG", "JM", "J", "JD", "LH"]
        sym_key = {"SA": "SA", "FG": "FG", "JM": "JM", "J": "J", "JD": "jd", "LH": "lh"}
        today = time.strftime("%Y%m%d", time.localtime())
        frames = {}
        for back in (0, 4, 5, 6, 7):
            d = _shift_ymd(today, back)  # 取较早交易日（helper 内部已做减法）
            try:
                df = ak.futures_spot_price(date=d, vars_list=ak_syms)
            except Exception:
                df = None
            if df is not None and len(df):
                frames[d] = df
            if len(frames) >= 2:
                break
        if len(frames) < 2:
            return out
        d0, d1 = sorted(frames.keys())
        f1, f0 = frames[d1], frames[d0]
        for _, r1 in f1.iterrows():
            sym = r1.get("symbol")
            if sym not in sym_key:
                continue
            r0 = f0[f0["symbol"] == sym]
            if len(r0) == 0:
                continue
            r0 = r0.iloc[0]
            try:
                br1 = float(r1.get("dom_basis_rate") or 0)
                br0 = float(r0.get("dom_basis_rate") or 0)
            except Exception:
                continue
            br_chg = br1 - br0  # 负数回升(变不那么负)=收敛=偏多
            score = max(-1.0, min(1.0, br_chg * 10.0))
            try:
                sp1 = float(r1.get("spot_price") or 0)
                sp0 = float(r0.get("spot_price") or 0)
                if sp0:
                    score = max(-1.0, min(1.0, score + (sp1 - sp0) / abs(sp0) * 5.0))
            except Exception:
                pass
            if abs(score) < 0.02:
                continue  # 噪声过滤
            out.append(
                {
                    "text": f"{sym} 现货基差率{'收敛(偏多)' if score > 0 else '走阔(偏空)'}",
                    "score": round(score, 2),
                    "source": "ak.futures_spot_price",
                    "ts": _ts(),
                    "sym": sym_key[sym],
                }
            )
    except Exception as e:
        print(f"[fetch_info] spot 异常: {e}", file=sys.stderr)
    return out


def _macro(items):
    """宏观统一项 → 全部品种（幅度打折）。"""
    out = []
    try:
        import akshare as ak

        # 降准 → 利多商品
        rr = _safe(lambda: ak.macro_china_reserve_requirement_ratio())
        if rr is not None and len(rr):
            # 准备金率下降 = 降准 = 利多
            s = -_latest_change(rr.iloc[:, -1].tolist()) if rr.shape[1] else 0.0
            if abs(s) > 0.01:
                out.append(
                    {
                        "text": f"存款准备金率{'下调(降准·利多)' if s > 0 else '上调(利空)'}",
                        "score": round(s * 0.6, 2),
                        "source": "ak.macro_china_reserve_requirement_ratio",
                        "ts": _ts(),
                        "sym": None,
                        "macro": True,
                    }
                )
        # PMI
        pmi = _safe(lambda: ak.macro_china_pmi())
        if pmi is not None and len(pmi):
            s = _latest_change(pmi.iloc[:, -1].tolist()) if pmi.shape[1] else 0.0
            out.append(
                {
                    "text": f"制造业PMI{'扩张' if s > 0 else '收缩'}",
                    "score": round(s * 0.5, 2),
                    "source": "ak.macro_china_pmi",
                    "ts": _ts(),
                    "sym": None,
                    "macro": True,
                }
            )
        # CPI / PPI
        for fn_name, label in [("macro_china_cpi", "CPI"), ("macro_china_ppi", "PPI")]:
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            df = _safe(fn)
            if df is not None and len(df):
                s = _latest_change(df.iloc[:, -1].tolist()) if df.shape[1] else 0.0
                out.append(
                    {
                        "text": f"{label} 同比{'上行' if s > 0 else '回落'}",
                        "score": round(s * 0.4, 2),
                        "source": f"ak.{fn_name}",
                        "ts": _ts(),
                        "sym": None,
                        "macro": True,
                    }
                )
    except Exception as e:
        print(f"[fetch_info] macro 异常: {e}", file=sys.stderr)
    return out


def _news(items):
    """宏观快讯（经济日历事件名）→ 关键词情绪 → 全部（轻量）。"""
    out = []
    bull = ["降准", "降息", "宽松", "稳增长", "利好", "复苏", "超预期", "回升", "MLF", "逆回购"]
    bear = ["加息", "收紧", "限产", "疫情", "违约", "衰退", "不及预期", "回落", "累库", "缩表", "跌"]
    try:
        import akshare as ak

        today = time.strftime("%Y%m%d", time.localtime())
        df = _safe(lambda: ak.news_economic_baidu(date=today))
        if df is None or not len(df):
            df = _safe(lambda: ak.news_economic_baidu())
        if df is not None and len(df):
            txt_col = _col(df, ["事件", "标题", "新闻", "内容"])
            if txt_col:
                for v in df[txt_col].astype(str).tolist()[:20]:
                    sc = 0.0
                    for w in bull:
                        if w in v:
                            sc += 0.3
                    for w in bear:
                        if w in v:
                            sc -= 0.3
                    if sc != 0:
                        out.append(
                            {
                                "text": f"快讯：{v[:40]}",
                                "score": round(max(-0.6, min(0.6, sc)), 2),
                                "source": "ak.news_economic_baidu",
                                "ts": _ts(),
                                "sym": None,
                                "macro": True,
                            }
                        )
    except Exception as e:
        print(f"[fetch_info] news 异常: {e}", file=sys.stderr)
    return out


def main():
    items = []
    items += _hog(items)
    items += _real_estate(items)
    items += _spot(items)
    items += _macro(items)
    items += _news(items)
    # 规范化：加 sym 字段（宏运用 sym=None 标记，运行时按 macro 展开到全部）
    data = {"updated": _now_iso(), "items": items}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[fetch_info] 完成：{len(items)} 条信息项 → {os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
