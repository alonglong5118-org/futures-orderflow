#!/usr/bin/env python3
"""
fetch_macro_context.py — 跨资产宏观语境采集器（系统 python3.9 + akshare 1.18.64）

抓取"跨资产/宏观"日线序列，落 macro_context.json 缓存；读取层 macro_context.py
（纯 stdlib，跑 live runner venv 3.13）只读缓存并计算 macro_bias。复用
fundamentals.json / info_dimension.json 的「外部写/内部读」隔离模式：凡需 akshare
的采集一律在本文件（系统3.9），runner(3.13) 只读 JSON。

可用源（akshare 1.18.64 实测）：
  沪深300 日线            stock_zh_index_daily(sh000300)
  中证500 日线            stock_zh_index_daily(sh000905)
  中美国债收益率          bond_zh_us_rate（含"中国国债收益率10年"）
  美元/人民币中间价       currency_boc_safe（"美元"列）
不可得（本版本无对应函数，跳过）：原油、南华商品指数、LME铜历史序列、USDA。

用法：/usr/bin/python3 fetch_macro_context.py
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "macro_context.json")
N_TAIL = 60  # 保留近 60 个交易日


def _safe(fn, tries=2):
    for _ in range(tries):
        try:
            return fn()
        except Exception:
            time.sleep(1.5)
    return None


def _fetch_crude_series():
    """原油主连日线(close)：sina 主源 → 东财 HTTP 兜底。返回近 N_TAIL 收盘价列表。"""
    try:
        ak = __import__("akshare")
        from datetime import datetime, timedelta

        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=900)).strftime("%Y%m%d")
        df = ak.futures_main_sina(symbol="sc0", start_date=start, end_date=end)
        if df is not None and len(df):
            col = "close" if "close" in df.columns else df.columns[-1]
            return [float(x) for x in df[col].dropna().tolist()[-N_TAIL:]]
    except Exception:
        pass
    try:
        import json as _json
        import urllib.request

        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            "&klt=101&fqt=0&secid=114.sc0&beg=0&end=20500101"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = _json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
        kls = (data.get("data") or {}).get("klines") or []
        closes = [float(kl.split(",")[2]) for kl in kls if kl]
        if closes:
            return closes[-N_TAIL:]
    except Exception:
        pass
    return []


def _fetch_nh_series():
    """南华商品指数(广谱商品趋势)日线，best-effort（akshare 版本差异可能无此函数，跳过即空）。"""
    for fn_name in ("futures_nh_index", "index_value_hist_funddb"):
        try:
            ak = __import__("akshare")
            f = getattr(ak, fn_name, None)
            if f is None:
                continue
            df = f(symbol="NH0100")
            if df is not None and len(df):
                col = "close" if "close" in df.columns else df.columns[-1]
                return [float(x) for x in df[col].dropna().tolist()[-N_TAIL:]]
        except Exception:
            continue
    return []


def main():
    series = {}

    # 1) 沪深300 / 中证500 日线（风险偏好代理）
    for sym, key in (("sh000300", "hs300"), ("sh000905", "zz500")):
        df = _safe(lambda s=sym: __import__("akshare").stock_zh_index_daily(symbol=s))
        if df is not None and len(df):
            col = "close" if "close" in df.columns else df.columns[-1]
            vals = [float(x) for x in df[col].dropna().tolist()[-N_TAIL:]]
            if vals:
                series[key] = vals

    # 2) 中美国债收益率（含 10Y）
    b = _safe(lambda: __import__("akshare").bond_zh_us_rate())
    if b is not None and len(b):
        cgb_col = next((c for c in b.columns if "10年" in c), None)
        if cgb_col:
            vals = [float(x) for x in b[cgb_col].dropna().tolist()[-N_TAIL:]]
            if vals:
                series["cgb10"] = vals

    # 3) 美元/人民币中间价
    c = _safe(lambda: __import__("akshare").currency_boc_safe())
    if c is not None and len(c) and "美元" in c.columns:
        vals = [float(x) for x in c["美元"].dropna().tolist()[-N_TAIL:]]
        if vals:
            series["usdcny"] = vals

    # 4) 沪铜现货（单日快照，无历史序列 → 仅存当日水平，供后续扩展）
    cu = _safe(lambda: __import__("akshare").futures_spot_price(vars_list=["CU"]))
    if cu is not None and len(cu):
        try:
            series["cu_spot"] = float(cu.iloc[0].get("near_contract_price") or cu.iloc[0].get("spot_price"))
        except Exception:
            pass

    # 5) 原油主连日线（能源/通胀代理）：sina 主源 → 东财 HTTP 兜底
    crude = _fetch_crude_series()
    if crude:
        series["crude"] = crude

    # 6) 南华商品指数（广谱商品趋势代理）：best-effort（akshare 版本差异可能无此函数）
    nh = _fetch_nh_series()
    if nh:
        series["nh_comm"] = nh

    # 7) USDA 农产品现货代理（大豆/玉米/豆粕/菜粕）：best-effort
    ag = _safe(lambda: __import__("akshare").futures_spot_price(vars_list=["A", "M", "C", "CS", "RM"]))
    if ag is not None and len(ag):
        try:
            col = ag.get("near_contract_price", ag.get("spot_price"))
            vals = [float(x) for x in col.dropna().tolist()[-N_TAIL:]]
            if vals:
                series["ag_spot"] = vals
        except Exception:
            pass

    payload = {"as_of": time.strftime("%Y-%m-%d"), "series": series}
    json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in series.items()}
    print(f"[fetch_macro_context] 已写出 {OUT}")
    print(f"[fetch_macro_context] 序列: {summary}")


if __name__ == "__main__":
    main()
