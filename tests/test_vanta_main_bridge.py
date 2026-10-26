#!/usr/bin/env python3
"""vanta_main_bridge 单测（C 方案，Ken 2026-10-26 拍板）

覆盖：
 1. 记号归一：CZCE 三-digit（SA701）与系统四-digit（SA2701）同一合约——这是
    2026-10-26 首跑基线的全部「分歧」来源，归一错了桥就会天天误报
 2. to_system_symbol：三→四 digit 展开（当前年代 26xx/27xx）+ 大写
 3. load_vanta 新鲜度/缺失/损坏 → None（降级安全，绝不脑补）；心跳优先判据
    （updated_at 是事件驱动时钟，无换月时会停——用心跳区分「活着」与「冻结」）
 4. compare：记号差异不算分歧；真实分歧要抓到；系统未跟踪品种不上报
 5. apply_to_cache：forced 锁写入 + 其他品种/字段保留 + 无分歧不动文件（幂等）
 6. minishare_live_running 闸与 pull 容错（mock subprocess）
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

D = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, D)

import vanta_main_bridge as bridge  # noqa: E402

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))


# ---------------------------------------------------------------- 1) 记号归一
def test_canon_contract():
    print("\n== canon_contract 记号归一 ==")
    chk("CZCE 三-digit vs 四-digit 同一合约", bridge.canon_contract("SA701") == bridge.canon_contract("SA2701") == "701")
    chk("大小写不敏感", bridge.canon_contract("al2612") == bridge.canon_contract("AL2612") == "612")
    chk("不同合约要能分开", bridge.canon_contract("rb2701") != bridge.canon_contract("rb2705"))
    chk("空/无数字回空串", bridge.canon_contract("") == "" and bridge.canon_contract("abc") == "")
    chk("不足三位补零", bridge.canon_contract("x12") == "012")


def test_to_system_symbol():
    print("\n== to_system_symbol 落地记号 ==")
    chk("SA701 → SA2701", bridge.to_system_symbol("SA701") == "SA2701")
    chk("al2612 → AL2612", bridge.to_system_symbol("al2612") == "AL2612")
    chk("rb2701 → RB2701（四-digit 原样大写）", bridge.to_system_symbol("rb2701") == "RB2701")
    chk("ta701 → TA2701", bridge.to_system_symbol("ta701") == "TA2701")
    chk("FG701 → FG2701", bridge.to_system_symbol("FG701") == "FG2701")


# ---------------------------------------------------------------- 2) load_vanta
def _write_cache(path: Path, *, age_minutes=0.0, contracts=None, updated_at=None,
                 heartbeat_age_minutes=None):
    if updated_at is None:
        updated_at = (datetime.now() - timedelta(minutes=age_minutes)).isoformat()
    payload = {"updated_at": updated_at, "source": "tq_main_continuous",
               "contracts": contracts if contracts is not None else {}}
    if heartbeat_age_minutes is not None:
        payload["_bridge_heartbeat"] = (
            datetime.now() - timedelta(minutes=heartbeat_age_minutes)).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_vanta():
    print("\n== load_vanta 新鲜度/容错 ==")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "vanta.json"
        _write_cache(p, contracts={"al": {"symbol": "al2612"}, "sa": {"symbol": "SA701"}})
        got = bridge.load_vanta(p, max_age_minutes=15)
        chk("新鲜数据返回映射", got == {"al": "al2612", "sa": "SA701"})
        _write_cache(p, age_minutes=30, contracts={"al": {"symbol": "al2612"}})
        chk("过期（30min > 15min）→ None", bridge.load_vanta(p, max_age_minutes=15) is None)
        _write_cache(p, age_minutes=10, contracts={"al": {"symbol": "al2612"}})
        chk("边界内（10min）→ 有值", bridge.load_vanta(p, max_age_minutes=15) is not None)
        p.write_text("{bad json", encoding="utf-8")
        chk("JSON 坏 → None", bridge.load_vanta(p) is None)
        _write_cache(p, contracts=None)
        chk("无 contracts → None", bridge.load_vanta(p) is None)
        _write_cache(p, updated_at="not-a-time")
        chk("时间戳坏 → None", bridge.load_vanta(p) is None)
        chk("文件缺失 → None", bridge.load_vanta(Path(td) / "nope.json") is None)


def test_load_vanta_heartbeat():
    print("\n== load_vanta 心跳优先判据（2026-10-26 修正）==")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "vanta.json"
        # 事件驱动落盘：updated_at 停在 30min 前（最后换月时刻），但心跳 2min 前
        # （runner 活着·无换月是正常态）→ 判定仍有效
        _write_cache(p, age_minutes=30, heartbeat_age_minutes=2,
                     contracts={"al": {"symbol": "al2612"}})
        got = bridge.load_vanta(p, max_age_minutes=15)
        chk("updated_at 过期但心跳新鲜 → 仍返回（活着≠换月）",
            got == {"al": "al2612"}, f"got={got}")
        # 心跳也过期（runner 大概死了）→ None
        _write_cache(p, age_minutes=30, heartbeat_age_minutes=30,
                     contracts={"al": {"symbol": "al2612"}})
        chk("updated_at 与心跳都过期 → None",
            bridge.load_vanta(p, max_age_minutes=15) is None)
        # 心跳字段坏（探测失败残留脏值）→ 退回 updated_at 判据
        payload = {"updated_at": (datetime.now() - timedelta(minutes=2)).isoformat(),
                   "contracts": {"al": {"symbol": "al2612"}},
                   "_bridge_heartbeat": "not-a-time"}
        p.write_text(json.dumps(payload), encoding="utf-8")
        chk("心跳坏 → 退回 updated_at（2min 新鲜）→ 有值",
            bridge.load_vanta(p, max_age_minutes=15) is not None)
        # 无心跳字段（旧版 cache/探测失败）→ 仍按 updated_at
        _write_cache(p, age_minutes=2, contracts={"al": {"symbol": "al2612"}})
        chk("无心跳字段 → updated_at 判据（向后兼容）",
            bridge.load_vanta(p, max_age_minutes=15) is not None)


# ---------------------------------------------------------------- 3) compare
def test_ym_of():
    print("\n== ym_of 合约年月解析 ==")
    chk("EB2612 → 202612", bridge.ym_of("EB2612") == 202612)
    chk("SA701（三-digit）→ 202701", bridge.ym_of("SA701") == 202701)
    chk("al2612 → 202612", bridge.ym_of("al2612") == 202612)
    chk("rb2701 → 202701", bridge.ym_of("rb2701") == 202701)
    chk("无数字 → None", bridge.ym_of("abc") is None)


def test_compare():
    print("\n== compare 分歧判定（有效主力口径） ==")
    vanta = {"al": "al2612", "sa": "SA701", "ta": "ta701", "rb": "rb2701", "eb": "eb2611"}
    system = {
        "al": {"main": "AL2612", "secondary": "AL2612", "ts": 1.0, "forced": False},
        "sa": {"main": "SA2701", "secondary": "SA2701", "ts": 1.0, "forced": False},
        "ta": {"main": "TA2705", "secondary": "TA2705", "ts": 1.0, "forced": False},  # 真分歧
        "cu": {"main": "CU2612", "secondary": "CU2612", "ts": 1.0, "forced": False},  # Vanta 未覆盖
        # eb：override 优先——cache 是 EB2612 且 override 也是 EB2612（双源一致）
        "eb": {"main": "EB2612", "secondary": "EB2612", "ts": 1.0, "forced": False},
    }
    overrides = {"eb": "EB2612"}
    div = bridge.compare(vanta, system, overrides)
    products = [d["product"] for d in div]
    chk("记号差异（sa SA701 vs SA2701）不算分歧", "sa" not in products)
    chk("真实分歧（ta ta701 vs TA2705）要抓到", "ta" in products)
    chk("Vanta 未覆盖品种（cu）不上报", "cu" not in products)
    chk("Vanta 多出的品种（rb 系统没有）不上报", "rb" not in products)
    chk("分歧记录带系统记号展开", any(d["product"] == "ta" and d["vanta_system_form"] == "TA2701" for d in div))
    chk("分歧记录带 rollback 标记（ta 701<2705 → 回退）",
        any(d["product"] == "ta" and d["rollback"] is True for d in div))
    # override 优先于 cache：cache 说 TA2705 但 override 说 TA2701 时，ta 与 Vanta 归一相等 → 不算分歧
    div2 = bridge.compare(vanta, system, {"eb": "EB2612", "ta": "TA2701"})
    chk("有效主力取 override（ta override=TA2701 与 Vanta 归一相等）→ 不算分歧",
        "ta" not in [d["product"] for d in div2])


# ---------------------------------------------------------------- 4) apply
def test_apply_to_cache():
    print("\n== apply_to_cache 双通道写入/单调向前/幂等/保留 ==")
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "minishare_hot_contracts.json"
        ov = Path(td) / "main_overrides.json"
        cache.write_text(json.dumps({
            "al": {"main": "AL2612", "secondary": "AL2612", "ts": 1.0, "forced": False},
            "ta": {"main": "TA2705", "secondary": "TA2705", "ts": 1.0, "forced": False},
            "cu": {"main": "CU2612", "secondary": "CU2612", "ts": 2.0, "forced": True},
        }, ensure_ascii=False), encoding="utf-8")
        ov.write_text(json.dumps({"cu": "CU2612"}, ensure_ascii=False), encoding="utf-8")
        orig_cache, orig_ov = bridge.SYSTEM_CACHE, bridge.OVERRIDE_FILE
        orig_state = bridge.BRIDGE_STATE
        bridge.SYSTEM_CACHE, bridge.OVERRIDE_FILE = cache, ov
        bridge.BRIDGE_STATE = Path(td) / "bridge_state.json"
        try:
            # ta：Vanta ta701（202701）< 系统 TA2705（202705）→ 单调向前拒绝（回退）
            # eb 不在 system cache → 不上报
            vanta = {"al": "al2612", "ta": "ta701", "cu": "cu2612"}
            result = bridge.apply_to_cache(vanta)
            applied, refused = result["applied"], result["refused_rollback"]
            chk("回退被拒（ta 701<705）且仅上报", [d["product"] for d in refused] == ["ta"])
            chk("无前进分歧 → 不写任何文件", applied == [])
            chk("override 文件未被触碰", json.loads(ov.read_text(encoding="utf-8")) == {"cu": "CU2612"})
            # 前进场景：rb 系统 RB2609，Vanta rb2701（更新）→ 双通道写入
            cache2 = Path(td) / "c2.json"
            cache2.write_text(json.dumps({
                "rb": {"main": "RB2609", "secondary": "RB2609", "ts": 1.0, "forced": False},
            }, ensure_ascii=False), encoding="utf-8")
            ov2 = Path(td) / "o2.json"
            ov2.write_text("{}", encoding="utf-8")
            bridge.SYSTEM_CACHE, bridge.OVERRIDE_FILE = cache2, ov2
            result2 = bridge.apply_to_cache({"rb": "rb2701"})
            chk("前进分歧被应用（rb）", [d["product"] for d in result2["applied"]] == ["rb"])
            after = json.loads(cache2.read_text(encoding="utf-8"))
            chk("hot cache：forced 锁 + 系统记号",
                after["rb"]["main"] == "RB2701" and after["rb"]["forced"] is True)
            chk("override：进程内权威层同步写",
                json.loads(ov2.read_text(encoding="utf-8")) == {"rb": "RB2701"})
            mtime1, mtime2 = cache2.stat().st_mtime, ov2.stat().st_mtime
            time.sleep(0.05)
            result3 = bridge.apply_to_cache({"rb": "rb2701"})
            chk("第二次 apply 无分歧 → 两文件都不动",
                result3["applied"] == [] and result3["refused_rollback"] == []
                and cache2.stat().st_mtime == mtime1 and ov2.stat().st_mtime == mtime2)
            # only_products 过滤
            result4 = bridge.apply_to_cache({"rb": "rb2799"}, only_products={"zz"})
            chk("only_products 过滤生效（不匹配 → 不写）", result4["applied"] == [])
        finally:
            bridge.SYSTEM_CACHE, bridge.OVERRIDE_FILE = orig_cache, orig_ov
            bridge.BRIDGE_STATE = orig_state


# ---------------------------------------------------------------- 5) 闸与容错
def test_guards():
    print("\n== minishare_live 闸 / pull 容错 ==")
    with mock.patch.object(bridge.subprocess, "run") as mrun:
        mrun.return_value = mock.Mock(returncode=0, stdout=b"123\n")
        chk("minishare_live 运行中 → True", bridge.minishare_live_running() is True)
        mrun.return_value = mock.Mock(returncode=1, stdout=b"")
        chk("pgrep 无匹配 → False", bridge.minishare_live_running() is False)
        mrun.side_effect = OSError("no pgrep")
        chk("pgrep 不存在 → False（放行，留痕可审计）", bridge.minishare_live_running() is False)
    # pull：prlctl 输出坏 JSON → False，不写本地缓存
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "cache.json"
        orig = bridge.LOCAL_CACHE
        bridge.LOCAL_CACHE = target
        try:
            with mock.patch.object(bridge.subprocess, "run") as mrun, \
                 mock.patch.object(bridge.os.path, "exists", return_value=False):
                mrun.return_value = mock.Mock(returncode=0, stdout=b"{not json")
                chk("pull 坏 JSON → False 且不写缓存",
                    bridge.pull_from_vm() is False and not target.exists())
                mrun.return_value = mock.Mock(returncode=0,
                                              stdout=json.dumps(
                                                  {"contracts": {"al": {"symbol": "al2612"}},
                                                   "updated_at": datetime.now().isoformat()}
                                              ).encode())
                chk("pull 正常 JSON → True 且落盘",
                    bridge.pull_from_vm() is True and target.exists())
                # pull：execution.json 心跳探测（runner 分钟级写）一并写入 payload
                def _hb_side_effect(cmd, **kw):
                    joined = " ".join(str(c) for c in cmd)
                    if "execution.json" in joined:
                        return mock.Mock(returncode=0, stdout=json.dumps(
                            {"updated_at": datetime.now().isoformat()}).encode())
                    return mock.Mock(returncode=0, stdout=json.dumps(
                        {"contracts": {"al": {"symbol": "al2612"}},
                         "updated_at": (datetime.now() - timedelta(minutes=30)).isoformat()}
                    ).encode())
                mrun.side_effect = _hb_side_effect
                ok = bridge.pull_from_vm()
                payload = json.loads(target.read_text(encoding="utf-8"))
                chk("pull 成功 → payload 带 _bridge_heartbeat",
                    ok and payload.get("_bridge_heartbeat"), f"hb={payload.get('_bridge_heartbeat')}")
                # 心跳文件拉取失败（execution.json 不可达）→ 无心跳字段，pull 仍成功
                def _hb_fail(cmd, **kw):
                    joined = " ".join(str(c) for c in cmd)
                    if "execution.json" in joined:
                        return mock.Mock(returncode=1, stdout=b"")
                    return mock.Mock(returncode=0, stdout=json.dumps(
                        {"contracts": {"al": {"symbol": "al2612"}},
                         "updated_at": datetime.now().isoformat()}
                    ).encode())
                mrun.side_effect = _hb_fail
                ok2 = bridge.pull_from_vm()
                payload2 = json.loads(target.read_text(encoding="utf-8"))
                chk("心跳拉取失败 → 无 _bridge_heartbeat（退回 updated_at 判据）",
                    ok2 and "_bridge_heartbeat" not in payload2)
        finally:
            bridge.LOCAL_CACHE = orig


def _run_all():
    test_canon_contract()
    test_to_system_symbol()
    test_ym_of()
    test_load_vanta()
    test_load_vanta_heartbeat()
    test_compare()
    test_apply_to_cache()
    test_guards()
    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
