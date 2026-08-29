"""四维策略 · 经纪商成交回灌（#9）
=====================================
背景：实盘持仓只读接口要天勤专业版（9988/年，不买），所以真实成交一直靠手敲记账 ——
手敲必然漏、必然错时间、必然忘手续费，导致 trade_journal 的绩效统计和真实账户对不上。

本模块走「结算单 / 成交明细导出」这条免费路线：
  中信期货客户端 / 博易 / 文华 / CTP 客户端 都能导出当日成交明细（CSV / TXT / XLS）。
  丢进导入目录（默认 ~/Downloads 和本目录 broker_fills/），本模块自动：
    ① 识别中文表头（成交日期/合约/买卖/开平/成交价/手数/手续费/成交编号…）
    ② 归一化为标准 fill 记录，合约代码 → 四维品种主键（FG609→FG, rb2610→rb）
    ③ 按成交编号去重（已导入的记 broker_import_state.json，重复导入不会双записи）
    ④ 回灌到 trade_journal（开→record_entry，平→record_exit）与 account_tracker

安全设计：默认 **dry-run 只预览**，必须显式 apply=True 才写账。

用法：
    import broker_import as bi
    bi.scan()                      # 扫描导入目录，预览可导入成交
    bi.import_file(path)           # 预览单文件
    bi.import_file(path, apply=True)   # 真正回灌
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "broker_import_state.json")
INBOX_DIRS = [
    os.path.join(HERE, "broker_fills"),
    os.path.expanduser("~/Downloads"),
]
FILE_PATTERNS = ("*.csv", "*.txt", "*.xls", "*.xlsx")
# 只认文件名里带这些关键词的，避免把下载目录里所有 csv 都当成交单
NAME_HINTS = ("成交", "结算", "trade", "fill", "settle", "对账")

_lock = threading.RLock()

# ── 中文表头别名表（各家客户端叫法不一，全部归一） ──────────────────────────
_ALIAS = {
    "date": ("成交日期", "日期", "交易日", "date", "tradingday", "trade_date"),
    "time": ("成交时间", "时间", "time", "tradetime"),
    "contract": ("合约", "合约代码", "品种合约", "instrument", "instrumentid", "code", "合约号"),
    "side": ("买卖", "买卖方向", "方向", "direction", "bs", "买/卖"),
    "offset": ("开平", "开平标志", "开仓平仓", "offset", "offsetflag", "开/平"),
    "price": ("成交价", "成交价格", "价格", "price", "成交均价"),
    "lots": ("手数", "成交手数", "数量", "成交量", "volume", "qty"),
    "fee": ("手续费", "佣金", "fee", "commission"),
    "tid": ("成交编号", "成交号", "流水号", "tradeid", "成交序号", "编号"),
}


def _norm_key(s):
    return re.sub(r"[\s\u3000:：()（）\-_]", "", str(s or "")).lower()


def _map_columns(header):
    """表头 → 标准字段位置。返回 {field: index}。"""
    idx = {}
    for i, h in enumerate(header):
        k = _norm_key(h)
        for field, names in _ALIAS.items():
            if field in idx:
                continue
            if any(_norm_key(n) == k or _norm_key(n) in k for n in names):
                idx[field] = i
                break
    return idx


def contract_to_symbol(contract):
    """合约代码 → 四维品种主键。FG609→FG, rb2610→rb, jd2609→jd, SA2609→SA。
    大小写按 SYMBOLS 主键还原（郑商所大写、上期/大商所小写）。"""
    c = str(contract or "").strip()
    m = re.match(r"^([A-Za-z]{1,3})\d{3,4}$", c)
    if not m:
        m = re.match(r"^([A-Za-z]{1,3})", c)
        if not m:
            return None
    raw = m.group(1)
    try:
        import four_dim_strategy as fd

        for key in fd.SYMBOLS:
            if key.lower() == raw.lower():
                return key
    except Exception:
        pass
    return raw


def _parse_side(v):
    s = str(v or "").strip().lower()
    if any(x in s for x in ("买", "buy", "b", "多")):
        return "买"
    if any(x in s for x in ("卖", "sell", "s", "空")):
        return "卖"
    return ""


def _parse_offset(v):
    s = str(v or "").strip().lower()
    if "平" in s or "close" in s or "offset" in s:
        return "平"
    if "开" in s or "open" in s:
        return "开"
    return ""


def _to_num(v):
    try:
        return float(str(v).replace(",", "").replace("¥", "").strip())
    except Exception:
        return None


def _read_rows(path):
    """读文件为 [[cell,...],...]。csv/txt 用 csv 模块（多编码尝试），xls/xlsx 用 pandas。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xls", ".xlsx"):
        try:
            import pandas as pd

            df = pd.read_excel(path, header=None, dtype=str)
            return df.fillna("").values.tolist()
        except Exception as e:
            raise RuntimeError(f"Excel 读取失败({e})，可另存为 CSV 再导入") from e
    for enc in ("utf-8-sig", "gbk", "gb18030", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
                except Exception:
                    dialect = csv.excel
                return [r for r in csv.reader(f, dialect)]
        except UnicodeDecodeError:
            continue
        except Exception:
            continue
    raise RuntimeError("文件编码无法识别（试过 utf-8/gbk/gb18030）")


def parse_file(path):
    """解析成交明细文件 → [fill]。fill = {tid,date,time,symbol,contract,side,offset,price,lots,fee}"""
    rows = _read_rows(path)
    if not rows:
        return [], "空文件"
    # 找表头行：前 20 行里能映射出 contract+price+lots 的那一行
    hdr_i, idx = None, None
    for i, r in enumerate(rows[:20]):
        m = _map_columns(r)
        if all(k in m for k in ("contract", "price", "lots")):
            hdr_i, idx = i, m
            break
    if hdr_i is None:
        return [], "未识别到表头（需含 合约/成交价/手数 三列）"
    out = []
    for r in rows[hdr_i + 1 :]:
        if not r or all(not str(x).strip() for x in r):
            continue

        def g(f, row=r):
            j = idx.get(f)
            return row[j] if (j is not None and j < len(row)) else ""

        contract = str(g("contract")).strip()
        if not contract or not re.search(r"[A-Za-z]", contract):
            continue  # 合计行/分隔行
        price = _to_num(g("price"))
        lots = _to_num(g("lots"))
        if price is None or not lots or lots <= 0:
            continue
        sym = contract_to_symbol(contract)
        if not sym:
            continue
        d = str(g("date")).strip() or datetime.now().strftime("%Y-%m-%d")
        d = re.sub(r"^(\d{4})(\d{2})(\d{2})$", r"\1-\2-\3", d)
        tid = str(g("tid")).strip()
        if not tid:
            tid = f"{d}_{contract}_{g('time')}_{price}_{int(lots)}"
        out.append(
            {
                "tid": tid,
                "date": d,
                "time": str(g("time")).strip(),
                "symbol": sym,
                "contract": contract,
                "side": _parse_side(g("side")),
                "offset": _parse_offset(g("offset")),
                "price": price,
                "lots": int(lots),
                "fee": _to_num(g("fee")) or 0.0,
                "source": os.path.basename(path),
            }
        )
    return out, ("ok" if out else "识别到表头但无有效成交行")


# ── 已导入去重 ────────────────────────────────────────────────────────────
def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[回灌] 状态落盘失败: {repr(e)[:60]}")


def _imported_ids():
    return set(_load_state().get("imported", []))


def _mark_imported(tids, path, applied):
    st = _load_state()
    ids = set(st.get("imported", []))
    ids.update(tids)
    st["imported"] = sorted(ids)[-5000:]
    st.setdefault("files", []).append(
        {
            "file": os.path.basename(path),
            "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "n": len(tids),
            "applied": applied,
        }
    )
    st["files"] = st["files"][-100:]
    _save_state(st)


# ── 回灌 ──────────────────────────────────────────────────────────────────
def _apply_fill(f):
    """把一条成交写进 trade_journal + account_tracker。返回 (ok, msg)。"""
    import account_tracker as at
    import trade_journal as tj

    sym, lots, px = f["symbol"], f["lots"], f["price"]
    side, offset = f["side"], f["offset"]
    if not side:
        return False, "买卖方向缺失"
    if not offset:
        # 无开平列：按当前是否有反向持仓推断（有反向仓 = 平，否则 = 开）
        st = at.load_state()
        pos = st.get("positions", {}).get(sym)
        if pos and ((pos["direction"] == "多" and side == "卖") or (pos["direction"] == "空" and side == "买")):
            offset = "平"
        else:
            offset = "开"
    direction = "多" if side == "买" else "空"
    msgs = []
    if offset == "开":
        try:
            tj.record_entry(sym, direction, lots, px, signal_id=f"broker:{f['tid']}")
        except Exception as e:
            msgs.append(f"journal:{repr(e)[:40]}")
        st = at.load_state()
        act = "add" if st.get("positions", {}).get(sym) else "open"
        ok, m, _ = at.record_trade(sym, act, direction, lots, px)
        if not ok:
            msgs.append(f"tracker:{m}")
    else:
        # 平仓：持仓方向与成交方向相反
        pos_dir = "空" if side == "买" else "多"
        try:
            tj.record_exit(sym, pos_dir, lots, px, reason="经纪商回灌")
        except Exception as e:
            msgs.append(f"journal:{repr(e)[:40]}")
        st = at.load_state()
        pos = st.get("positions", {}).get(sym)
        if pos:
            act = "close" if lots >= pos["lots"] else "reduce"
            ok, m, _ = at.record_trade(sym, act, pos["direction"], lots, px)
            if not ok:
                msgs.append(f"tracker:{m}")
        else:
            msgs.append("tracker:无对应持仓(仅记 journal)")
    return (not msgs), ("；".join(msgs) or "ok")


def import_file(path, apply=False, skip_imported=True):
    """导入单个文件。apply=False 只预览。"""
    with _lock:
        try:
            fills, note = parse_file(path)
        except Exception as e:
            return {"ok": False, "file": os.path.basename(path), "error": str(e), "fills": [], "new": 0}
        seen = _imported_ids() if skip_imported else set()
        new = [f for f in fills if f["tid"] not in seen]
        res = {
            "ok": True,
            "file": os.path.basename(path),
            "path": path,
            "note": note,
            "total": len(fills),
            "new": len(new),
            "skipped": len(fills) - len(new),
            "fills": new,
            "applied": False,
            "results": [],
        }
        if apply and new:
            done = errs = 0
            for f in new:
                ok, m = _apply_fill(f)
                res["results"].append(
                    {
                        "tid": f["tid"],
                        "symbol": f["symbol"],
                        "side": f["side"],
                        "offset": f["offset"],
                        "lots": f["lots"],
                        "price": f["price"],
                        "ok": ok,
                        "msg": m,
                    }
                )
                done += 1 if ok else 0
                errs += 0 if ok else 1
            _mark_imported([f["tid"] for f in new], path, True)
            res["applied"] = True
            res["done"], res["errors"] = done, errs
        return res


def find_files(dirs=None, max_age_days=7):
    """扫描导入目录，返回候选成交明细文件（按修改时间倒序）。"""
    dirs = dirs or INBOX_DIRS
    now = time.time()
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for pat in FILE_PATTERNS:
            for p in glob.glob(os.path.join(d, pat)):
                base = os.path.basename(p).lower()
                # broker_fills/ 目录内不设关键词门槛（专用目录，放进来就是要导的）
                in_dedicated = os.path.abspath(d) == os.path.abspath(INBOX_DIRS[0])
                if not in_dedicated and not any(h in base for h in NAME_HINTS):
                    continue
                try:
                    mt = os.path.getmtime(p)
                except Exception:
                    continue
                if (now - mt) > max_age_days * 86400:
                    continue
                out.append((mt, p))
    out.sort(reverse=True)
    return [p for _, p in out]


def scan(apply=False, max_files=5):
    """扫描并（可选）回灌所有候选文件。"""
    files = find_files()[:max_files]
    reports = [import_file(p, apply=apply) for p in files]
    total_new = sum(r.get("new", 0) for r in reports)
    return {
        "ok": True,
        "files": len(files),
        "new_fills": total_new,
        "reports": reports,
        "inbox": INBOX_DIRS,
        "scanned": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import sys

    os.makedirs(INBOX_DIRS[0], exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] not in ("--scan", "--apply"):
        r = import_file(sys.argv[1], apply=("--apply" in sys.argv))
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
    else:
        r = scan(apply=("--apply" in sys.argv))
        print(f"导入目录: {r['inbox']}")
        print(f"候选文件 {r['files']} 个，新成交 {r['new_fills']} 笔")
        for rep in r["reports"]:
            print(
                f"  · {rep['file']}: {rep.get('note') or rep.get('error')} "
                f"总{rep.get('total', 0)}/新{rep.get('new', 0)}"
            )
