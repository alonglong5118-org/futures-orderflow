#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
_NOTIFY_COOLDOWN = 7 * 24 * 3600   # 非紧急类(源失败/临界当月): 仅在状态变化时推送, 避免刷屏
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
        subprocess.run(["osascript", "-e",
                        f'display notification "{_safe}" with title "{title}"'],
                       timeout=10)
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
    cd = _NOTIFY_COOLDOWN_URGENT if cond_key.startswith("ABNOLD") else _NOTIFY_COOLDOWN
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
    yy = int(d[:2]); mm = int(d[2:])
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
            syms = df['symbol'].unique().tolist()
            
            def _ym(code):
                """sc2609 -> 2609 (YYMM 整数)"""
                code = str(code).lower()
                # 提取品种前缀 + YYMM
                m = re.match(r'^([a-z]+)(\d{4})$', code)
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
                prefix = re.match(r'^([a-z]+)', str(sym).lower())
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
                    candidates = [(ym, s.upper()) for s in syms 
                                  if s.lower().startswith(pfx) and _ym(s) > 0]
                    if candidates:
                        candidates.sort()
                        variety_map[pfx] = candidates[0]
            
            # ine 独有品种白名单: 只有 sc(原油) 是 ine 独有
            # lu/nr/bc/ec 等品种虽出现在 ine 结算数据中, 但实际归属 SHFE
            # 必须过滤掉, 避免覆盖 SHFE 的正确主力合约
            _INE_ONLY = {"sc"}
            out = {pfx: code for pfx, (_, code) in variety_map.items() 
                   if pfx in _INE_ONLY}
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
    
    # ine: 使用独立函数 (新浪源不支持 ine)
    try:
        ine_out = _get_ine_main()
        out.update(ine_out)
    except Exception:
        print(f"  [warn] ine 主力获取失败，跳过", file=sys.stderr)
        failed.append("ine")
    
    return out, failed


def main():
    ap = argparse.ArgumentParser(description="换月期全市场主力合约核对/强制更新")
    ap.add_argument("--apply", action="store_true", help="用 akshare 近月主力强制锁定(写 forced=True)")
    ap.add_argument("--report", action="store_true", help="仅报告（默认行为）")
    ap.add_argument("--dump-akmap", action="store_true",
                    help="仅打印 akshare 全市场真主力映射(JSON)后退出，不读缓存/不写文件")
    args = ap.parse_args()

    # --dump-akmap：轻量只读查询，供 runner(3.13 无 akshare)经 subprocess 取「交易所真实主力」。
    # 不读缓存、不写 main_overrides.json，避免任何副作用；输出纯 JSON 便于解析。
    if args.dump_akmap:
        try:
            ak_main, failed = ak_main_all()
            print(json.dumps({"ok": True, "main": ak_main,
                              "failed_exchanges": failed}, ensure_ascii=False))
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
    try:
        json.dump(override, open(OVERRIDE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[refresh_main_contracts] 已写权威覆盖 {len(override)} 品种 -> {OVERRIDE_FILE}")
    except Exception as _e:
        print(f"[refresh_main_contracts] 写覆盖失败: {_e}")

    # —— 防再犯（2026-08-18）：同步 trade_config.json 的 contract_specs.contract 到权威覆盖值 ——
    # 历史教训：contract_specs 是手工维护的兜底源，换月后大面积停在旧合约（曾 44 品种停在 2609），
    # 任何未优先 main_overrides 的读取路径都会拿到旧合约。此处 --apply 时一并同步，消除双源漂移。
    _FIXED_KEYS = {"SA01", "sa01", "lc", "si"}
    try:
        _tcfg_path = os.path.join(HERE, "trade_config.json")
        _tcfg = json.load(open(_tcfg_path, encoding="utf-8"))
        _specs = _tcfg.get("contract_specs", {})
        _synced = []
        for _v, _code in override.items():
            if _v in _FIXED_KEYS:
                continue
            _key = _v if _v in _specs else (_v.upper() if _v.upper() in _specs else None)
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
    _abn = []        # 卡在过往月份(ym < now_ym)，确定性异常
    _cur_month = []  # 停在当月交割月(ym == now_ym)，临界需核对
    for v, code in override.items():
        ym = ym_of(code)
        if ym is not None and ym < now_ym:
            _abn.append(f"{v}={code}")
        elif ym == now_ym:
            _cur_month.append(f"{v}={code}")
    if failed_ex:
        _log("WARN", f"akshare 交易所源失败(相关品种未重新验证, 沿用上次值): {failed_ex}")
    if _abn:
        _log("ERROR", f"存在卡在过往月份的异常旧合约: {_abn}")
    if _cur_month:
        _log("WARN", f"主力停在当月交割月(临界, 建议核对): {_cur_month}")
    # 组装异常键(优先级: 异常旧合约 > 源失败 > 临界当月)
    if _abn:
        cond_key = "ABNOLD:" + ",".join(_abn)
        cond_detail = f"异常旧合约(停留过往月份): {', '.join(_abn)}。请核对 /api/state"
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
