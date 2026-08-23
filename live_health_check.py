#!/usr/bin/env python3
# 四维策略 live 面板周期自检（set-and-forget）
# 校验：① health 可达 ② edge 数据真的非空(防 CALIB_FILE 类静默失明) ③ 合约一致性(state/account == main_overrides) ④ consistency ok
# 告警：复用项目 push_notify（Bark/Telegram/企微）；仅在 OK→FAIL 边沿 + FAIL→OK 恢复时推送，避免刷屏。
import json, os, sys, time, urllib.request, urllib.error

HERE = os.environ.get("FOUR_DIM_HOME") or os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8741"
STATUS_FILE = os.path.join(HERE, "live_health_status.json")
MAIN_OVERRIDES = os.path.join(HERE, "main_overrides.json")
CALIB = os.path.join(HERE, "calibration_params.json")
TIMEOUT = 10


def get_json(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.load(r)


def load_disk_json(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def check():
    fails = []
    summary = {}

    # 1) health 可达
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=8) as r:
            summary["health_http"] = r.status
            if r.status != 200:
                fails.append(f"health HTTP {r.status}")
    except Exception as e:
        fails.append(f"health 不可达: {e}")
        return fails, summary  # 面板挂了，后续检查无意义

    # 2) edge 数据非空（静默失明防护）
    try:
        edge = get_json(BASE + "/api/edge")
        rows = edge.get("rows", []) or []
        if edge.get("error"):
            fails.append(f"edge error: {edge['error']}")
        nn = sum(1 for r in rows if r.get("mean_oos") is not None)
        disk = load_disk_json(CALIB)
        disk_nn = sum(1 for v in disk.values()
                      if isinstance(v, dict) and v.get("mean_oos") is not None)
        summary["edge_rows"] = len(rows)
        summary["edge_mean_oos_nn"] = nn
        summary["disk_calib_nn"] = disk_nn
        if nn == 0:
            fails.append("edge mean_oos 全空（校准数据静默失明）")
        elif disk_nn and nn != disk_nn:
            fails.append(f"edge mean_oos({nn}) != 磁盘校准({disk_nn})")
    except Exception as e:
        fails.append(f"edge 检查异常: {e}")

    # 3) 合约一致性：state / account 的 contract 必须 == main_overrides 权威源
    mo = load_disk_json(MAIN_OVERRIDES)
    try:
        st = get_json(BASE + "/api/state")
        syms = st.get("symbols", {}) or {}
        mism = []
        for s, c in syms.items():
            mc = mo.get(s) or mo.get(s.lower())
            sc = c.get("contract") if isinstance(c, dict) else None
            if mc and sc != mc:
                mism.append((s, sc, mc))
        summary["state_symbols"] = len(syms)
        summary["state_contract_mismatch"] = len(mism)
        if mism:
            fails.append("state 合约不一致 " + ", ".join(f"{a}:{b}≠{c}" for a, b, c in mism[:8]))
    except Exception as e:
        fails.append(f"state 合约检查异常: {e}")

    try:
        ac = get_json(BASE + "/api/account")
        pos_raw = ac.get("positions")
        pos = pos_raw if isinstance(pos_raw, list) else list((pos_raw or {}).values())
        mism = []
        for p in pos:
            if not isinstance(p, dict):
                continue
            s = p.get("symbol")
            sc = p.get("contract")
            mc = mo.get(s) or mo.get(s.lower())
            if mc and sc != mc:
                mism.append((s, sc, mc))
        summary["account_positions"] = len(pos)
        summary["account_contract_mismatch"] = len(mism)
        if mism:
            fails.append("account 合约不一致 " + ", ".join(f"{a}:{b}≠{c}" for a, b, c in mism[:8]))
    except Exception as e:
        fails.append(f"account 合约检查异常: {e}")

    # 4) consistency ok
    try:
        cons = get_json(BASE + "/api/consistency")
        summary["consistency_ok"] = bool(cons.get("ok"))
        if not cons.get("ok"):
            sm = cons.get("report", {}).get("summary", {})
            fails.append("consistency ok=False: " + json.dumps(sm, ensure_ascii=False)[:200])
    except Exception as e:
        fails.append(f"consistency 检查异常: {e}")

    return fails, summary


def main():
    fails, summary = check()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur_ok = (len(fails) == 0)

    prev = load_disk_json(STATUS_FILE)
    first_run = "ok" not in prev
    prev_ok = bool(prev.get("ok", True))
    transition_to_fail = (not cur_ok) and (prev_ok or first_run)
    transition_to_ok = cur_ok and (not prev_ok) and (not first_run)

    status = {"ok": cur_ok, "ts": now, "fails": fails, "summary": summary}
    try:
        json.dump(status, open(STATUS_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 推送（复用项目 push_notify；仅边沿触发）
    has_ch = False
    try:
        sys.path.insert(0, HERE)
        import push_notify as pn
        ch = pn.channels_status()
        has_ch = any(ch.get(k) for k in ("telegram", "bark", "wecom"))
    except Exception:
        has_ch = False

    if cur_ok:
        if transition_to_ok and has_ch:
            pn.push("✅ 四维 live 自检已恢复", title="四维自检恢复")
        print(f"OK @ {now} | {json.dumps(summary, ensure_ascii=False)}")
        return 0
    else:
        detail = "\n".join("- " + f for f in fails)
        print(f"FAIL @ {now}\n{detail}\n| summary: {json.dumps(summary, ensure_ascii=False)}")
        if transition_to_fail and has_ch:
            pn.push("❌ 四维 live 自检失败:\n" + detail, title="四维自检告警")
        return 1


if __name__ == "__main__":
    sys.exit(main())
