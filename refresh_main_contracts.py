#!/usr/bin/env python3
"""
refresh_main_contracts.py — 换月期全市场主力合约实时核对与强制更新

背景（2026-08-17 用户要求固化）：
  移仓换月期间，很多商品已更换主力合约。系统 minishare_live 虽按持仓量(OI)排名
  动态解析主力/次主力并每5分钟刷新，但换月过渡期新旧合约 OI 接近、旧主力仍排前二时，
  _apply_hot_contracts 的防抖逻辑会钉住旧合约不切换，导致大量品种主力停留在交割月
  旧合约（如 2608）而真实主力已前进到 2609/2610。

本脚本用 akshare match_main_contract 作为外部权威源，全市场核对系统缓存
(minishare_hot_contracts.json)，识别滞后品种并可选 --apply 用 akshare 近月主力
强制锁定（写入 forced=True 标记，minishare_live 的 _apply_hot_contracts 无条件尊重）。

红线（避免误改）：
  - 只接受 akshare 主力落在 [当前月, 当前月+4] 近月范围内的更新，避免其部分品种返回
    远月（如 FG2701、持仓最大的远月合约）被误当成当前主力。
  - 有实际持仓的品种由 _pin_account_positions 钉死，本脚本不碰（forced 分支在
    _apply_hot_contracts 中亦对持仓品种跳过）。

用法：
  python3 refresh_main_contracts.py            # 仅报告滞后品种
  python3 refresh_main_contracts.py --report   # 同上（显式）
  python3 refresh_main_contracts.py --apply    # 报告 + 用 akshare 近月强制锁定并更新缓存
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "minishare_hot_contracts.json")
LOG_FILE = os.path.join(HERE, "main_contract_refresh.log")
ALERT_STATE = os.path.join(HERE, "main_contract_alert_state.json")
_NOTIFY_COOLDOWN = 7 * 24 * 3600  # 非紧急类(源失败/临界当月): 仅在状态变化时推送, 避免刷屏
_NOTIFY_COOLDOWN_URGENT = 6 * 3600  # 紧急类(异常旧合约): 每 6h 提醒一次, 直至修复


def _log(level, msg):
    """写日志(追加) + 打印到 stdout(供 runner 捕获)。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as _f:
            _f.write(line + "\n")
    except Exception:
        pass
    print(line)


def _notify(title, msg):
    """macOS 通知中心推送(用户 gui 会话)。失败静默忽略。"""
    try:
        import subprocess

        _safe = msg.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{_safe}" with title "{title}"'], timeout=10)
    except Exception:
        pass


def _load_alert_state():
    try:
        if os.path.exists(ALERT_STATE):
            return json.load(open(ALERT_STATE, encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_alert_state(st):
    try:
        json.dump(st, open(ALERT_STATE, "w", encoding="utf-8"))
    except Exception:
        pass


def _alert_if_needed(cond_key, cond_detail):
    """cond_key='OK' 表示无异常；否则按需推送(去重 + 分级冷却)。
    严重度: 异常旧合约(ABNOLD)=紧急(6h)；源失败/临界当月=非紧急(状态变化才推)。"""
    st = _load_alert_state()
    now = time.time()
    last_ts = st.get("last_notify_ts", 0)
    last_key = st.get("last_key", "")
    if cond_key == "OK":
        if last_key != "OK":
            st["last_key"] = "OK"
            st["last_notify_ts"] = now
            _save_alert_state(st)
        return
    # 紧急类用短冷却, 非紧急类用长冷却(状态不变则不刷屏)
    # 2026-10-15:CONSIST（运行期在用/真实不一致）纳入紧急类 ——
    #   它意味着跟踪或取价可能跑在错误合约上，属实盘风险，不可长冷却。
    cd = (_NOTIFY_COOLDOWN_URGENT
          if (cond_key.startswith("ABNOLD") or cond_key.startswith("CONSIST"))
          else _NOTIFY_COOLDOWN)
    if cond_key != last_key or (now - last_ts > cd):
        _notify("主力合约刷新异常", cond_detail)
        st["last_key"] = cond_key
        st["last_notify_ts"] = now
        _save_alert_state(st)


def ym_of(code):
    """合约码 -> 年月整数(YYYYMM)；无法识别返回 None。"""
    m = re.match(r"^([A-Za-z]+?)(\d{3,4})$", re.sub(r"[^A-Za-z0-9]", "", str(code).upper()))
    if not m:
        return None
    d = m.group(2)
    yy = int(d[:2])
    mm = int(d[2:])
    yy += 2000 if yy < 70 else 1900
    return yy * 100 + mm


def _add_months(ym, n):
    """年月整数(YYYYMM) + n 个月，正确进位跨年（如 202608 + 12 = 202708，而非 202620）。"""
    y, m = ym // 100, ym % 100
    y += (m + n - 1) // 12
    m = (m + n - 1) % 12 + 1
    return y * 100 + m


def _next_month_code(old_code, now_ym):
    """基于旧合约前缀 + 当前月+1 生成近月主力候选（如 FG2608@202608 -> FG2609）。
    B 类兜底：akshare 给远月/缺失时，用最接近的下一个近月(当前月+1)作为新主力。"""
    m = re.match(r"^([A-Za-z]+?)(\d{3,4})$", re.sub(r"[^A-Za-z0-9]", "", str(old_code).upper()))
    if not m:
        return None
    prefix = m.group(1)
    ny = now_ym + 1
    yy = (ny // 100) % 100
    mm = ny % 100
    return f"{prefix}{yy:02d}{mm:02d}"


# ine 独有品种白名单（module 级，供 INE 主力解析与过滤共用）
# INE 目前仅 sc(原油) 一个独有品种；lu/nr/bc/ec 虽出现在 ine 结算数据中但实际归 SHFE。
_INE_ONLY_SET = {"sc"}


def _ine_main_from_hot():
    """从 minishare_hot_contracts.json 取 INE 品种的真实主力（按实时持仓量 OI 排名选出）。

    2026-10-15 新增：INE 结算价表（futures_settle_ine）**不含成交量/持仓量**
    （实测 volume/OI 全为 0），因此 _get_ine_main 的「近月优先」口径在换月期
    会选错：SC2611 尚未摘牌时它 YYMM 最小 → 被选成主力，而真实主力已是
    SC2612（OI/成交均更大），导致面板持续弹「原油系统 SC2612 vs 真主力 SC2611」
    的**反向假警**（把正确的当成错误）。
    minishare_live 的 HOT_CACHE 是按实时 OI 排名的，其 main 字段才是真主力。
    返回 {pfx_lower: code}；文件缺失/无 INE 品种时返回空 dict。
    """
    out = {}
    try:
        if not os.path.exists(CACHE):
            return out
        _cache = json.load(open(CACHE, encoding="utf-8")) or {}
        for k, rec in _cache.items():
            if not isinstance(rec, dict):
                continue
            if str(k).lower() not in _INE_ONLY_SET:
                continue
            _code = rec.get("main") or rec.get("secondary")
            if _code:
                out[str(k).lower()] = str(_code).upper()
    except Exception as _e:
        print(f"  [warn] 读 minishare_hot_contracts 取 INE 主力失败: {_e}", file=sys.stderr)
    return out


def _get_ine_main():
    """INE(上海国际能源交易中心) 主力合约获取。

    akshare match_main_contract 对 ine 无效(新浪源不支持),
    改用 futures_settle_ine 获取交易所官方结算数据, 选取近月活跃合约。
    INE 目前仅上市 SC(原油) 一个品种。
    """
    from datetime import datetime

    import akshare as ak

    out = {}
    today = datetime.now().strftime("%Y%m%d")

    for attempt in range(3):
        try:
            df = ak.futures_settle_ine(date=today)
            if df is None or len(df) == 0:
                time.sleep(1.5)
                continue

            # 提取所有合约, 按 YYMM 排序
            syms = df["symbol"].unique().tolist()

            def _ym(code):
                """sc2609 -> 2609 (YYMM 整数)"""
                code = str(code).lower()
                # 提取品种前缀 + YYMM
                m = re.match(r"^([a-z]+)(\d{4})$", code)
                if m:
                    return int(m.group(2))
                return 0

            # 转换为 YYMM 进行比较
            now_ym = datetime.now().year % 100 * 100 + datetime.now().month

            # 找每个品种的近月主力
            variety_map = {}
            for sym in syms:
                ym = _ym(sym)
                if ym == 0:
                    continue
                # 计算品种前缀 (如 sc)
                prefix = re.match(r"^([a-z]+)", str(sym).lower())
                if not prefix:
                    continue
                pfx = prefix.group(1)

                if pfx not in variety_map:
                    # 找第一个 >= 当前月的合约 (即近月主力)
                    if ym >= now_ym:
                        variety_map[pfx] = (ym, sym.upper())

            # 如果没找到 >= 当前月的, 用最大的 (最远月也比没有好)
            for pfx in set(sym[:2].lower() for sym in syms):
                if pfx not in variety_map:
                    candidates = [(ym, s.upper()) for s in syms if s.lower().startswith(pfx) and _ym(s) > 0]
                    if candidates:
                        candidates.sort()
                        variety_map[pfx] = candidates[0]

            # ine 独有品种白名单: 只有 sc(原油) 是 ine 独有
            # lu/nr/bc/ec 等品种虽出现在 ine 结算数据中, 但实际归属 SHFE
            # 必须过滤掉, 避免覆盖 SHFE 的正确主力合约
            out = {pfx: code for pfx, (_, code) in variety_map.items() if pfx in _INE_ONLY_SET}
            if out:
                break
        except Exception:
            if attempt < 2:
                time.sleep(1.5)

    return out


def ak_main_all():
    """全市场真实主力（akshare 权威源）。返回 (out, failed_ex)。
    out: {variety_lower: contract_code}；failed_ex: 全部重试失败(未验证)的交易所列表。
    每交易所重试 3 次（akshare match_main_contract 偶发空响应）。
    ine 使用独立的 _get_ine_main() 函数（新浪源不支持 ine）。"""
    import akshare as ak

    out = {}
    failed = []
    for ex in ("dce", "czce", "shfe", "cffex"):
        for _ in range(3):
            try:
                s = ak.match_main_contract(symbol=ex)
                toks = re.findall(r"[A-Za-z]+\d{3,4}", str(s))
                if toks:
                    for tok in toks:
                        m = re.match(r"([A-Za-z]+?)\d", tok)
                        if m:
                            out.setdefault(m.group(1).lower(), tok.upper())
                    break
            except Exception:
                pass
            time.sleep(1.5)
        else:
            print(f"  [warn] akshare {ex} 连续失败，跳过", file=sys.stderr)
            failed.append(ex)

    # ine: 2026-10-15 起优先用 minishare 实时 OI 排名（真实主力），
    # 结算价表无成交量只能「近月优先」，换月期会选错（SC2611 vs 真主力 SC2612）。
    # OI 源缺失时才退回 _get_ine_main 的近月兜底。
    try:
        _ine_hot = _ine_main_from_hot()
        if _ine_hot:
            out.update(_ine_hot)
        else:
            ine_out = _get_ine_main()
            out.update(ine_out)
    except Exception:
        try:
            out.update(_get_ine_main())
        except Exception:
            failed.append("ine")

    return out, failed


# —— tushare OI 交叉校验（2026-11-20）——
# 背景：akshare match_main_contract 对 INE 原油(sc)判定滞后 —— main_overrides 长期停在
#   SC2612（OI 1013 / 成交 32 手），而 tushare fut_daily 实测真实主力为 SC2701
#   （OI 30659 / 成交 38998 手）。minishare_live._authoritative_contracts() 的优先级是
#   MAIN_OVERRIDE > HOT_CACHE，即 akshare 给什么就用什么，所以必须有一个独立的 OI 真相源
#   来兜底这类误判（sc 在缓存里 forced=False，属纯误判，与持仓品种刻意的 forced=True 不同）。
_OI_RANK_LAST_DATE = ""  # 诊断用：最近一次成功拉取的 trade_date
_TUSHARE_URL_DEFAULT = "https://t.xiaodefa.top/"


def _tushare_cred():
    """从 four_dim_strategy.py 读取 tushare proxy 的 token/url（与其 _fetch_daily_tushare 同源），
    避免在本脚本里硬编码第二份 token。返回 (token, url)；无 token 时 token 为 None。"""
    try:
        src = open(os.path.join(HERE, "four_dim_strategy.py"), encoding="utf-8").read()
        m = re.search(r"_TUSHARE_CONFIG\s*=\s*\{(.*?)\}", src, re.S)
        if not m:
            return None, _TUSHARE_URL_DEFAULT
        blk = m.group(1)
        tok = re.search(r'["\']token["\']\s*:\s*["\']([^"\']+)["\']', blk)
        url = re.search(r'["\']url["\']\s*:\s*["\']([^"\']+)["\']', blk)
        return (tok.group(1) if tok else None), (url.group(1) if url else _TUSHARE_URL_DEFAULT)
    except Exception:
        return None, _TUSHARE_URL_DEFAULT


def _tushare_oi_rank() -> dict:
    """按最新交易日给出全市场持仓量(OI)排名：{品种前缀大写: [(oi, 合约代码, vol), ...]}。

    合约代码已去掉交易所后缀（AO2701.SHF -> AO2701），组内按 oi 降序。
    只取最新一个 trade_date 的切片 —— proxy 每次返回 6000 行≈6 个交易日，全量计算会被
    历史合约污染；连续/指数合约（SC.INE、SCTAS.INE、L_F.DCE 等无月份后缀）一律跳过。
    任何异常只打印一行并返回 {}，绝不抛出：校验失败时上层保持 akshare 判定不变。
    """
    global _OI_RANK_LAST_DATE
    try:
        import gzip
        import urllib.request as _ur

        token, url = _tushare_cred()
        if not token:
            raise RuntimeError("four_dim_strategy.py 中未读到 tushare token")
        body = json.dumps({"api_name": "fut_daily", "token": token}).encode()
        # 必须显式带 Accept-Encoding: gzip —— 该 proxy 不带此头会 429「请开启gzip」
        req = _ur.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
        )
        with _ur.urlopen(req, timeout=60) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        items = ((json.loads(raw.decode("utf-8")).get("data") or {}).get("items")) or []
        if not items:
            raise RuntimeError("proxy 返回空 items")

        latest = max(str(r[1]) for r in items if len(r) > 13)
        out = {}
        seen = set()
        for row in items:
            if str(row[1]) != latest:
                continue
            code = str(row[0]).upper().split(".")[0]
            m = re.match(r"^([A-Z_]+?)(\d{3,4})$", code)
            if not m or code in seen:  # 无月份后缀的连续/指数合约跳过；同码去重
                continue
            seen.add(code)
            try:
                oi = float(row[13] or 0)
                vol = float(row[11] or 0)
            except (TypeError, ValueError, IndexError):
                continue
            out.setdefault(m.group(1), []).append((oi, code, vol))
        for _lst in out.values():
            _lst.sort(key=lambda x: -x[0])
        _OI_RANK_LAST_DATE = latest
        return out
    except Exception as _e:
        print(f"[oi_rank] tushare 获取失败: {_e}")
        return {}


def main():
    ap = argparse.ArgumentParser(description="换月期全市场主力合约核对/强制更新")
    ap.add_argument("--apply", action="store_true", help="用 akshare 近月主力强制锁定(写 forced=True)")
    ap.add_argument("--report", action="store_true", help="仅报告（默认行为）")
    ap.add_argument(
        "--dump-akmap", action="store_true", help="仅打印 akshare 全市场真主力映射(JSON)后退出，不读缓存/不写文件"
    )
    args = ap.parse_args()

    # --dump-akmap：轻量只读查询，供 runner(3.13 无 akshare)经 subprocess 取「交易所真实主力」。
    # 不读缓存、不写 main_overrides.json，避免任何副作用；输出纯 JSON 便于解析。
    if args.dump_akmap:
        try:
            ak_main, failed = ak_main_all()
            print(json.dumps({"ok": True, "main": ak_main, "failed_exchanges": failed}, ensure_ascii=False))
            sys.exit(0)
        except Exception as _e:
            print(json.dumps({"ok": False, "error": str(_e)}, ensure_ascii=False))
            sys.exit(1)

    now = time.localtime()
    now_ym = now.tm_year * 100 + now.tm_mon

    if not os.path.exists(CACHE):
        print(f"[error] 找不到缓存文件: {CACHE}")
        sys.exit(1)
    cache = json.load(open(CACHE, encoding="utf-8"))
    ak_main, failed_ex = ak_main_all()

    # —— tushare OI 交叉校验：修正 akshare 的滞后判定（实测 sc: SC2612 -> SC2701）——
    # 判定口径：akshare 给的合约 != tushare OI 第一名，且其 OI < 真主力 OI 的 30% → 视为滞后。
    # 阈值 0.3 的理由：换月期间新旧主力的 OI 会交替甚至短期反超，只有「旧合约持仓不到真主力
    #   的三成」才算明显失血；30% 是保守门槛，避免把 akshare 在过渡期的合理判断误覆盖掉。
    # 持仓品种（forced=True）刻意不换月（避免盯市体系跳变），这里一律跳过，绝不碰 si/lc。
    # 品种不在 tushare 排名里 → 跳过，保持 akshare 原值。
    _oi_rank = _tushare_oi_rank()
    if _oi_rank:
        _n_fix = 0
        for _v in list(ak_main.keys()):
            if (cache.get(_v) or {}).get("forced"):
                continue
            _lst = _oi_rank.get(str(_v).upper())
            if not _lst or not ak_main.get(_v):
                continue
            _real_oi, _real_code = _lst[0][0], _lst[0][1]
            _ak_code = str(ak_main[_v]).upper()
            _ak_oi = next((_o for _o, _c, _ in _lst if _c == _ak_code), 0.0)  # 查不到按 0
            if _ak_code != _real_code and _ak_oi < _real_oi * 0.3:
                print(
                    f"[fix] {_v}: akshare={_ak_code}(oi={_ak_oi:.0f}) 滞后 "
                    f"→ 采用 tushare={_real_code}(oi={_real_oi:.0f})"
                )
                ak_main[_v] = _real_code
                _n_fix += 1
        print(
            f"[oi_rank] tushare 最新交易日={_OI_RANK_LAST_DATE} 品种={len(_oi_rank)} "
            f"交叉校验修正={_n_fix}"
        )

    # 自愈：换月锁定成功后(主力已出交割月)，清除旧 forced 恢复正常 OI 自动换月
    if args.apply:
        for v, rec in cache.items():
            if rec.get("forced") and (ym_of(rec.get("main", "")) or 0) > now_ym:
                rec["forced"] = False
                rec.pop("source", None)

    # 写全量 akshare 权威覆盖（minishare_live 优先采用，绕开 OI 滞后/卡月）。
    # 这是面板/信号主力的最终真相源；宽松 sanity 边界：不早于当前月、不晚于当前月+12
    # （农产品主力常为 2611/2701，不能像旧逻辑那样卡在 +4 月，否则会误杀真实主力）。
    # 单调向前铁律：akshare.match_main_contract 本身基于 OI，换月期会在 2609/2701 间反复横跳，
    # 故绝不把已有覆盖回退到更旧月份（如已锁 2701 不被 flaky 的 2609 冲掉）。仅当 akshare
    # 给出更新（>=现有）的近月主力时才采纳，实现换月的一次性前向推进且不可逆退。
    OVERRIDE_FILE = os.path.join(HERE, "main_overrides.json")
    prev_ov = {}
    try:
        if os.path.exists(OVERRIDE_FILE):
            prev_ov = json.load(open(OVERRIDE_FILE, encoding="utf-8")) or {}
    except Exception:
        prev_ov = {}
    override = {}
    for v, rec in cache.items():
        am = ak_main.get(v.lower())
        am_ym = ym_of(am) if am else None
        cur_ov = prev_ov.get(v) or rec.get("main", "")
        cur_ym = ym_of(cur_ov)
        if am and am_ym and now_ym <= am_ym <= _add_months(now_ym, 12):
            # 单调向前：akshare 主力比现有覆盖更旧则保留现有，避免 OI 抖动回退
            if cur_ym and am_ym < cur_ym:
                override[v] = cur_ov
            else:
                override[v] = am
        elif am and am_ym and am_ym > _add_months(now_ym, 12):
            nm = _next_month_code(rec.get("main", ""), now_ym)  # akshare 异常远月→兜底近月
            override[v] = nm if (nm and (not cur_ym or ym_of(nm) >= cur_ym)) else cur_ov
        else:
            override[v] = cur_ov  # akshare 缺失/早于当前月→保留现有
    # 2026-10-15：与运行时真实主力对齐（单调向前）。akshare 缺失/滞后时override 可能停在
    #   交割月，而 minishare_live 已按成交量解析出更新的主力 → 造成双源漂移 + 假警。
    #   此处以 minishare_hot_contracts 为补充真相源，且同样遵守「只向前、不回退」铁律。
    try:
        _hot_pre = os.path.join(HERE, "minishare_hot_contracts.json")
        if os.path.exists(_hot_pre):
            _hp = json.load(open(_hot_pre, encoding="utf-8")) or {}
            _aligned = []
            for _v, _rec in _hp.items():
                if not isinstance(_rec, dict):
                    continue
                _live = _rec.get("main", "")
                _lym = ym_of(_live) if _live else None
                if not _live or not _lym or not (now_ym <= _lym <= _add_months(now_ym, 12)):
                    continue
                _cur = override.get(_v)
                _cym = ym_of(_cur) if _cur else None
                if _cym is None or _lym > _cym:  # 只向前，绝不回退
                    override[_v] = _live
                    _aligned.append(f"{_v}:{_cur}→{_live}")
            if _aligned:
                print(f"[refresh_main_contracts] 对齐运行时真实主力 {len(_aligned)} 项: {_aligned}")
    except Exception as _e:
        print(f"[refresh_main_contracts] 对齐真实主力失败(忽略): {_e}")
    try:
        json.dump(override, open(OVERRIDE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[refresh_main_contracts] 已写权威覆盖 {len(override)} 品种 -> {OVERRIDE_FILE}")
    except Exception as _e:
        print(f"[refresh_main_contracts] 写覆盖失败: {_e}")

    # —— 防再犯（2026-08-18）：同步 trade_config.json 的 contract_specs.contract 到权威覆盖值 ——
    # 历史教训：contract_specs 是手工维护的兜底源，换月后大面积停在旧合约（曾 44 品种停在 2609），
    # 任何未优先 main_overrides 的读取路径都会拿到旧合约。此处 --apply 时一并同步，消除双源漂移。
    # 2026-10-15 修正：原先把 lc/si 列入 _FIXED_KEYS（与 SA01 指数一起排除），导致这两个
    #   品种的 contract_specs 永远不跟随换月 —— 权威覆盖已是LC2611/SI2611，contract_specs
    #   却卡在 LC2610/SI2610，形成双源漂移，且每2 小时刷一次 CURRMONTH 假警。
    #   现在只保留真正的固定标的（SA01 指数无主力换月概念）；品种一律参与同步。
    _FIXED_KEYS = {"SA01", "sa01"}
    # 品种键大小写归一：contract_specs 用大写（SI/LC），override 用小写（si/lc）
    def _spec_key(v):
        if v in _specs:
            return v
        u = v.upper()
        return u if u in _specs else None
    try:
        _tcfg_path = os.path.join(HERE, "trade_config.json")
        _tcfg = json.load(open(_tcfg_path, encoding="utf-8"))
        _specs = _tcfg.get("contract_specs", {})
        _synced = []
        for _v, _code in override.items():
            if _v in _FIXED_KEYS:
                continue
            _key = _spec_key(_v)
            if not _key:
                continue
            _sp = _specs[_key]
            if _sp.get("contract") and str(_sp.get("contract")).upper() != str(_code).upper():
                _sp["contract"] = str(_code).upper()
                _synced.append(f"{_v}:{_code}")
        if _synced:
            json.dump(_tcfg, open(_tcfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[refresh_main_contracts] 同步 contract_specs {len(_synced)} 品种 -> {_tcfg_path}")
    except Exception as _e:
        print(f"[refresh_main_contracts] 同步 contract_specs 失败: {_e}")

    # —— 异常监测：刷新失败交易所 + 卡在过往月份的旧合约 + 停在当月交割月(临界) ——
    # 2026-10-15 修正：判定「是否真异常」必须对照**运行时真实主力**（minishare_hot_contracts.json，
    # 那是minishare_live 实际用于取价的合约），而不是只看 override 文本。
    # 原因：override 是刷新脚本的输出，minishare 有独立的动态主力解析（按成交量/OI 选主力），
    # 两者本可不同步。若只按 override 判"停在当月交割月=临界"，就会在换月已正常完成
    # （真实主力已是 2611）的情况下，每 2 小时刷一次 CURRMONTH 假警。
    # 新口径：
    #   真实主力也停在当月交割月 → 真临界（需要人核对）
    #   真实主力已换月而 override 未跟上 → 归为「双源漂移」，一次性告警并自动修正
    #   override 卡在过往月份 → 确定性异常（不变）
    _abn = []          # 卡在过往月份(ym < now_ym)，确定性异常
    _cur_month = []    # 真实主力也停在当月交割月 → 真临界
    _drift = []        # override 落后于真实主力（双源漂移，可自动修正）
    _hot = {}
    try:
        _hot_path = os.path.join(HERE, "minishare_hot_contracts.json")
        if os.path.exists(_hot_path):
            _h = json.load(open(_hot_path, encoding="utf-8")) or {}
            for _v, _rec in _h.items():
                if isinstance(_rec, dict):
                    _hot[_v.lower()] = _rec.get("main", "")
    except Exception as _e:
        print(f"[refresh_main_contracts] 读 minishare_hot_contracts 失败(仅按 override 判定): {_e}")

    for v, code in override.items():
        ym = ym_of(code)
        if ym is not None and ym < now_ym:
            _abn.append(f"{v}={code}")
            continue
        if ym != now_ym:
            continue
        # ym == now_ym：临界。核对运行时真实主力
        _live = _hot.get(v.lower(), "")
        _lym = ym_of(_live) if _live else None
        if _live and _lym and _lym > now_ym:
            _drift.append(f"{v}={code}→{_live}")  # 真实已换月，override 落后
        else:
            _cur_month.append(f"{v}={code}")
    if failed_ex:
        _log("WARN", f"akshare 交易所源失败(相关品种未重新验证, 沿用上次值): {failed_ex}")
    if _abn:
        _log("ERROR", f"存在卡在过往月份的异常旧合约: {_abn}")
    if _drift:
        _log("WARN", f"override 落后于运行时真实主力(双源漂移, 将以真实主力为准修正): {_drift}")
    if _cur_month:
        _log("WARN", f"主力停在当月交割月(临界, 建议核对): {_cur_month}")

    # 2026-10-15（用户决策：写入日志 + 消息推送）：运行期合约一致性自检。
    # 背景：2026-10-15 抓到「反向假警」—— 面板说系统 SC2612、真主力 SC2611，
    #   而override/hot/specs 三方一致认为 SC2612 就是主力；假警报会诱导用户执行
    #   无效甚至破坏性的 --apply。此类问题只有把「在用合约 vs 各真相源」摊开
    #   对比才能发现，故把该维度纳入常驻自检（与GET /api/contracts 同口径）。
    # 判定：override（系统在用）与 minishare 真实主力不一致 → CONSIST。
    #   注意方向性 —— 若真实主力月份更靠后，说明 override 落后（可自动收敛，不紧急）；
    #   若 override 更靠后，则可能是真相源滞后，标记出来人工核对（可能是反向假警）。
    _inconsistent = []
    for v, code in override.items():
        _live = _hot.get(v.lower(), "")
        if not _live or not code:
            continue
        if str(_live).upper() == str(code).upper():
            continue
        _cym, _lym = ym_of(code), ym_of(_live)
        if _cym is None or _lym is None:
            _inconsistent.append(f"{v}: 在用{code} vs 真实{_live}")
        elif _lym > _cym:
            _inconsistent.append(f"{v}: 在用{code} 落后 真实{_live}")
        else:
            _inconsistent.append(f"{v}: 在用{code} 超前 真实{_live}(核对真相源, 可能是反向假警)")
    if _inconsistent:
        _log("WARN", f"运行期合约在用/真实不一致({len(_inconsistent)}个): {_inconsistent}")

    # 组装异常键(优先级: 异常旧合约 > 运行期不一致 > 双源漂移 > 源失败 > 临界当月)
    if _abn:
        cond_key = "ABNOLD:" + ",".join(_abn)
        cond_detail = f"异常旧合约(停留过往月份): {', '.join(_abn)}。请核对 /api/state"
    elif _inconsistent:
        cond_key = "CONSIST:" + ",".join(_inconsistent)
        cond_detail = (
            f"运行期在用合约与真实主力不一致({len(_inconsistent)}个): "
            f"{', '.join(_inconsistent[:5])}{' 等' if len(_inconsistent) > 5 else ''}。"
            f"请先跑 GET /api/contracts 核对四方(在用/取价/真实/静态表)，"
            f"确认哪边正确后再处理 —— 不要盲目跑 --apply"
        )
    elif _drift:
        cond_key = "DRIFT:" + ",".join(_drift)
        cond_detail = (
            f"override 落后于运行时真实主力: {', '.join(_drift)}。"
            "运行中取价用的是真实主力(不受影响)，下次刷新会自动收敛"
        )
    elif failed_ex:
        cond_key = "FETCHFAIL:" + ",".join(failed_ex)
        cond_detail = f"akshare 源失败: {', '.join(failed_ex)}。相关品种主力未重新验证, 沿用上次值"
    elif _cur_month:
        cond_key = "CURRMONTH:" + ",".join(_cur_month)
        cond_detail = f"主力停在当月交割月(临界): {', '.join(_cur_month)}。建议核对"
    else:
        cond_key = "OK"
        cond_detail = "全部正常"
    _log("INFO", f"异常监测结果: {cond_key}")
    _alert_if_needed(cond_key, cond_detail)

    lag = []  # (variety, sys_main, new_main, sys_ym, new_ym)
    for v, rec in cache.items():
        sm = rec.get("main", "")
        sm_ym = ym_of(sm)
        if sm_ym is None or sm_ym > now_ym:
            continue  # 未停在交割月旧合约，不滞后
        am = ak_main.get(v.lower())
        am_ym = ym_of(am) if am else None
        # A 类：akshare 给出近月主力(当前月+1~+4)，直接用
        if am and am_ym and now_ym < am_ym <= now_ym + 4:
            new_main, new_ym = am, am_ym
        else:
            # B 类：akshare 给远月(如 FG2701)或缺失，兜底为当前月+1 近月(如 FG2609)
            new_main = _next_month_code(sm, now_ym)
            new_ym = ym_of(new_main) if new_main else None
        if new_main and new_ym and new_main != sm:
            lag.append((v, sm, new_main, sm_ym, new_ym))

    print(f"[refresh_main_contract] 当前年月={now_ym}  全市场扫描 {len(ak_main)} 品种")
    print(f"[refresh_main_contracts] 换月滞后品种(系统停在交割月旧合约, 真实主力已前进): {len(lag)}")
    for v, sm, am, sy, ay in sorted(lag, key=lambda x: x[3]):
        print(f"  {v}: 系统={sm}({sy}) -> 真实={am}({ay})")

    if args.apply and lag:
        n = 0
        for v, sm, am, sy, ay in lag:
            cache[v]["main"] = am
            if cache[v].get("secondary") == sm:
                cache[v]["secondary"] = am
            cache[v]["ts"] = time.time()
            cache[v]["forced"] = True
            cache[v]["source"] = "akshare_refresh"
            n += 1
        json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[refresh_main_contracts] 已强制锁定 {n} 个品种主力(forced=True)。")
        print("[refresh_main_contracts] 需重启 com.ken.futures-orderflow.live 让 _apply_hot_contracts 触发换月。")
        print("[refresh_main_contracts] 提示: 持仓品种由账户钉死，不会被本脚本/forced 覆盖。")
    elif args.apply:
        print("[refresh_main_contracts] 无滞后品种需更新。")


if __name__ == "__main__":
    main()
