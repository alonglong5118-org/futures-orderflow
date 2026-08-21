#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_macro_context_full.py — P3 数据源补全采集器（骨架版，2026-08-18）

用途：用 Tushare Pro 补齐 akshare 拿不到的外部数据，落 tushare_cache.json 缓存，
      供换月逻辑 / macro_context.py 只读合并（复用「外部写 / 内部读」隔离模式）。

跑在系统 /usr/bin/python3 (3.9)。纯 stdlib（urllib + json），**不依赖 tushare 库**：
直接 POST http://api.tushare.pro，避免污染系统环境。

补齐的 4 类缺口：
  1. INE 主力映射   → Tushare `fut_basic` + `fut_mapping`（覆盖 sc/lu/nr 等 INE 品种）
  2. 原油主连日线   → Tushare `fut_daily`（SC 主力连续）
  3. 南华商品指数   → Tushare 无直接接口，暂占位（候选：南华官网 / Wind / 东财）
  4. USDA 农产品   → Tushare 无，暂占位（候选：USDA FAS/ERS 官网 API）

token 来源（优先级）：
  1. 环境变量 TUSHARE_TOKEN
  2. 同目录 tushare_token.txt（第一行）

用法：
  /usr/bin/python3 fetch_macro_context_full.py            # 全量采集，写 tushare_cache.json
  /usr/bin/python3 fetch_macro_context_full.py --dry-run  # 只打印数据源可达性，不写盘

Tushare HTTP 约定：
  POST http://api.tushare.pro
  body = {"api_name": "...", "token": "...", "params": {...}, "fields": "..."}
  返回 = {"code": 0, "msg": "", "data": {"fields": [...], "items": [[...], ...]}}
"""
import os
import sys
import json
import time
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "tushare_cache.json")
TOKEN_FILE = os.path.join(HERE, "tushare_token.txt")
TUSHARE_URL = "http://api.tushare.pro"
N_TAIL = 60  # 日线序列保留近 60 个交易日


# --------------------------------------------------------------------------- #
# token 与通用调用
# --------------------------------------------------------------------------- #
def _load_token():
    """读 token：环境变量 TUSHARE_TOKEN → 同目录 tushare_token.txt 首行。"""
    t = os.environ.get("TUSHARE_TOKEN", "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        try:
            lines = [ln.strip() for ln in open(TOKEN_FILE, encoding="utf-8") if ln.strip()]
            if lines:
                return lines[0]
        except Exception:
            pass
    return ""


def _tushare_call(api_name, params=None, fields=""):
    """POST 调 Tushare HTTP API。返回 data dict（含 fields/items），失败抛异常。"""
    token = _load_token()
    if not token:
        raise RuntimeError("无 TUSHARE_TOKEN：请设环境变量 TUSHARE_TOKEN 或写 tushare_token.txt")
    payload = {"api_name": api_name, "token": token,
               "params": params or {}, "fields": fields}
    req = urllib.request.Request(
        TUSHARE_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("code") != 0:
        raise RuntimeError(f"Tushare {api_name} 返回错误 code={resp.get('code')} msg={resp.get('msg')}")
    return resp.get("data") or {}


def _rows_to_dicts(data):
    """把 Tushare 的 {fields, items} 转成 list[dict]。"""
    fields = data.get("fields") or []
    items = data.get("items") or []
    return [dict(zip(fields, it)) for it in items]


# --------------------------------------------------------------------------- #
# 各数据源采集函数（每个返回 {"available": bool, "note": str, ...}）
# --------------------------------------------------------------------------- #
def fetch_ine_main():
    """① INE（上海国际能源中心）主力合约映射。

    Tushare 接口：fut_basic(exchange=INE) 拿品种列表 → fut_mapping 拿各品种当前主力。
    返回 {"available": bool, "note": str, "main": {品种代码: 主力合约代码}}。
    供换月逻辑替代 akshare 的 match_main_contract(symbol='ine')（该接口当前 JSONDecodeError）。
    """
    out = {"available": False, "source": "tushare", "note": "", "main": {}}
    try:
        # 1) INE 全部品种（去合约月份的品种前缀，如 SC、LU、NR）
        data = _tushare_call("fut_basic", {"exchange": "INE", "fut_type": "1"}, "ts_code,name")
        rows = _rows_to_dicts(data)
        syms = sorted({r.get("ts_code", "").split(".")[0].rstrip("0123456789") for r in rows if r.get("ts_code")})
        if not syms:
            out["note"] = "fut_basic 未返回 INE 品种"
            return out
        # 2) 逐个品种取当前主力合约
        for sym in syms:
            try:
                m = _tushare_call("fut_mapping", {"ts_code": f"{sym}.INE"}, "ts_code,trade_date,mapping_ts_code")
                mrows = _rows_to_dicts(m)
                if mrows:
                    # 最近一条 mapping_ts_code 即当前主力
                    out["main"][sym] = mrows[-1].get("mapping_ts_code")
            except Exception:
                continue
        out["available"] = bool(out["main"])
        out["note"] = f"已取 {len(out['main'])} 个 INE 品种主力" if out["available"] else "未取到 INE 主力映射"
    except Exception as e:
        out["note"] = f"INE 主力采集失败：{e}"
    return out


def fetch_crude_daily():
    """② 原油主连日线（能源/通胀代理）。

    Tushare 接口：fut_daily(ts_code=SC 主力连续)。主力连续代码规则 = 品种 + 'L' + 交易所，
    即 'SCL.INE'（若该代码不可用，回退 fut_mapping 取当前主力合约后按单合约拉日线）。
    返回 {"available": bool, "note": str, "closes": [近 N_TAIL 收盘价]}。
    """
    out = {"available": False, "source": "tushare", "note": "", "closes": []}
    try:
        ts_codes = ["SCL.INE", "SC0.INE"]  # 主力连续候选
        closes = []
        for code in ts_codes:
            try:
                d = _tushare_call("fut_daily", {"ts_code": code}, "trade_date,close")
                rows = _rows_to_dicts(d)
                rows.sort(key=lambda r: r.get("trade_date", ""))
                closes = [float(r["close"]) for r in rows if r.get("close") is not None][-N_TAIL:]
                if closes:
                    break
            except Exception:
                continue
        if closes:
            out["closes"] = closes
            out["available"] = True
            out["note"] = f"原油主连日线 {len(closes)} 根"
        else:
            out["note"] = "未取到原油主连日线（fut_daily 返回空或连续代码需调整）"
    except Exception as e:
        out["note"] = f"原油日线采集失败：{e}"
    return out


def fetch_nh_comm():
    """③ 南华商品指数（广谱商品趋势代理）。

    ⚠️ Tushare 无南华商品指数直接接口，此函数暂占位。
    候选实现（后续按需接）：
      - 南华期货官网 / 南华商品指数发布页
      - Wind 金融终端（需机构账号）
      - 东方财富（若有对应 secid）
      - 或自行用 Tushare fut_daily 加权合成（工程量大，暂不做）
    """
    return {"available": False, "source": "pending", "note": "南华商品指数：Tushare 无接口，待接南华官网/Wind/东财"}


def fetch_usda():
    """④ USDA 农产品数据（大豆/玉米/豆粕/菜粕现货/库存）。

    ⚠️ Tushare 无 USDA 接口，此函数暂占位。
    候选实现（后续按需接）：
      - USDA FAS（海外农业服务）API：https://apps.fas.usda.gov
      - USDA ERS（经济研究服务）数据 API
      - 或用 akshare 已有 futures_spot_price(A/M/C/CS/RM) 作农产品现货代理（已覆盖 ag_spot）
    """
    return {"available": False, "source": "pending", "note": "USDA：Tushare 无接口，待接 USDA FAS/ERS 官网 API"}


# --------------------------------------------------------------------------- #
# 汇总 & 落盘
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印数据源可达性，不写盘")
    args = parser.parse_args()

    token = _load_token()
    if not token:
        print("[fetch_macro_context_full] ⚠️ 未配置 TUSHARE_TOKEN（设环境变量 TUSHARE_TOKEN 或写 tushare_token.txt 首行）")
        if args.dry_run:
            return
        # 无 token 也写出占位缓存，便于读取层优雅降级（不中断 runner）
        payload = {"as_of": time.strftime("%Y-%m-%d"),
                   "token_configured": False,
                   "ine_main": {"available": False, "note": "无 TUSHARE_TOKEN"},
                   "crude": {"available": False, "note": "无 TUSHARE_TOKEN"},
                   "nh_comm": {"available": False, "note": "无 TUSHARE_TOKEN"},
                   "usda": {"available": False, "note": "无 TUSHARE_TOKEN"}}
        json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[fetch_macro_context_full] 已写出占位缓存 {OUT}（无 token）")
        return

    print(f"[fetch_macro_context_full] token 已配置（{token[:6]}...），开始采集")
    results = {
        "ine_main": fetch_ine_main(),
        "crude": fetch_crude_daily(),
        "nh_comm": fetch_nh_comm(),
        "usda": fetch_usda(),
    }

    if args.dry_run:
        print("\n=== dry-run：数据源可达性 ===")
        for k, v in results.items():
            print(f"  {k:10s} available={v.get('available')}  {v.get('note')}")
        return

    payload = {"as_of": time.strftime("%Y-%m-%d"),
               "token_configured": True,
               **results}
    json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[fetch_macro_context_full] 已写出 {OUT}")
    for k, v in results.items():
        print(f"  {k:10s} available={v.get('available')}  {v.get('note')}")


if __name__ == "__main__":
    main()
