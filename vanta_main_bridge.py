#!/usr/bin/env python3
"""vanta_main_bridge.py — Vanta 主力合约判定单向只读桥（C 方案，Ken 2026-10-26 拍板）

背景：
  Vanta（日内交易系统）天勤侧跑 KQ.m@ 主力连续判定，落盘
  state/main_contracts.json（17 品种，VM C:\\intraday_trading_system_zz\\state\\）；
  三感参谋自跑 minishare_live OI 排名解析主力（54 品种，缓存
  minishare_hot_contracts.json）。换月期两套判定可能分歧 ⇒ 订阅/信号/
  盯市基准不一致，正是「各板块数据不一致」类事故的源头。

C 方案（单向只读桥）：把 Vanta 落盘文件拉取到本地缓存，与系统缓存比对：
  - 记号归一：CZCE 三-digit（SA701）与系统四-digit（SA2701）是**同一合约**
    （tqsdk CZCE 记号 vs 系统记号），比较取末三位；落地写系统四-digit 记号。
    2026-10-26 首跑基线：17 品种初比 3 个「分歧」（ta/sa/fg）全是记号差异，
    归一后 0 真实分歧。
  - --apply：把 Vanta 判定写入系统（双通道，复刻 refresh_main_contracts --apply
    既有模式）：① main_overrides.json 进程内权威层（_apply_hot_contracts 每周期
    重读 ⇒ **runner 运行中也生效**）；② minishare_hot_contracts.json forced=True
    锁（minishare_live OI 防抖分支无条件尊重）。**单调向前铁律**：Vanta 合约比
    现有有效主力更旧 ⇒ 拒绝写入、仅上报（换月期源抖动横跳，回退会把系统拽回旧合约）。
    只覆盖 Vanta 覆盖且系统已跟踪的品种（不新增品种）。
  - Vanta 数据缺失/不新鲜/损坏 → 不动系统缓存（降级安全：绝不脑补）。

红线：
  - **单向**：只读 Vanta 文件，绝不回写 Vanta 任何东西（零账号/席位/流量冲突，
    不经天勤 API——这是 C 方案区别于 B 方案的根本）。
  - minishare_live **独立进程**运行时（pgrep 可判）拒绝 --apply：其 poll 持久化
    会与外部写竞态（lost update）。runner 进程内跑 minishare_live 时 pgrep 判不出
    （cmdline 是 four_dim_live_runner.py）——此时生效通道是 main_overrides.json
    （每周期重读，无竞态）；hot cache 的 forced 写与 refresh_main_contracts --apply
    同款竞态量级（既有模式，2h/10min 级 cadence，窗口极小），可接受。
  - 记号末三位比较是安全网：即便四-digit 展开猜错年代，也只会报分歧而不会静默改名。

用法：
  python3 vanta_main_bridge.py              # 拉取 + 比对报告（默认不动缓存）
  python3 vanta_main_bridge.py --no-pull    # 不拉 VM，用本地缓存比对
  python3 vanta_main_bridge.py --apply      # 报告 + 写入 forced 锁（需 minishare_live 停）
  python3 vanta_main_bridge.py --json       # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_CACHE = HERE / "minishare_hot_contracts.json"
OVERRIDE_FILE = HERE / "main_overrides.json"               # 进程内权威层（_apply_hot_contracts 每周期重读）
LOCAL_CACHE = HERE / "vanta_main_contracts.cache.json"   # Vanta 文件的本地拉取副本
BRIDGE_STATE = HERE / "vanta_main_bridge_state.json"     # 桥自身上次 apply 留痕

# Vanta 侧文件位置（Parallels VM）。共享盘优先（快），prlctl 兜底（共享盘卸载时）。
VM_NAME = "Windows 11"
VM_SRC = r"C:\intraday_trading_system_zz\state\main_contracts.json"
VM_EXEC_SRC = r"C:\intraday_trading_system_zz\state\execution.json"  # runner 心跳（分钟级写）
SHARED_SRC = "/Volumes/[C] Windows 11.hidden/intraday_trading_system_zz/state/main_contracts.json"
SHARED_EXEC_SRC = ("/Volumes/[C] Windows 11.hidden/intraday_trading_system_zz/"
                   "state/execution.json")
PRLCTL = "/Applications/Parallels Desktop.app/Contents/MacOS/prlctl"

_DEFAULT_MAX_AGE_MINUTES = 15.0     # Vanta updated_at 超过此时长视为不新鲜
_DIGITS = re.compile(r"\d+")


# ---------------------------------------------------------------- 基础工具
def _now() -> datetime:
    return datetime.now()


def _parse_ts(value) -> datetime | None:
    """解析 ISO 时间戳（兼容 naive/aware）。失败回 None。"""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is not None:
        t = t.replace(tzinfo=None)   # 统一按本地时钟比较（Vanta 写的是本地时间）
    return t


def canon_contract(code: str) -> str:
    """合约记号归一：取数字末三位。

    CZCE 三-digit（SA701）与系统四-digit（SA2701）是同一合约 ⇒ 末三位相等。
    也天然兼容大小写（al2612 / AL2612）。
    """
    digits = _DIGITS.findall(str(code or ""))
    if not digits:
        return ""
    return digits[-1][-3:].zfill(3)


def to_system_symbol(vanta_symbol: str) -> str:
    """Vanta 记号 → 系统缓存记号：大写 + CZCE 三-digit 展开四-digit。

    三-digit「YMM」按当前年代（2026-2027）展开为「2YMM」：701→2701、612→2612。
    ⚠️ 年代假设：2026-2027 在期合约只可能是 26xx/27xx；跨年代（如 912→2912）
    不会静默发生——末三位比较会把它报为分歧而不是改名。
    """
    s = str(vanta_symbol or "").strip().upper()
    m = _DIGITS.search(s)
    if not m:
        return s
    digits = m.group(0)
    if len(digits) == 3:
        digits = "2" + digits
    return s[: m.start()] + digits + s[m.end():]


def ym_of(code: str) -> int | None:
    """合约年月：EB2612→202612，SA701→202701（三-digit 按 2020s 年代展开）。

    返回 None 表示无法解析（调用方按「不可比」处理，不参与单调判定）。
    """
    digits = _DIGITS.findall(str(code or ""))
    if not digits:
        return None
    d = digits[-1]
    try:
        if len(d) == 3:                      # CZCE 三-digit：YMM → 202Y MM
            return (2020 + int(d[0])) * 100 + int(d[1:])
        if len(d) >= 4:                      # 四-digit：YYMM → 20YY MM
            return (2000 + int(d[:2])) * 100 + int(d[2:4])
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------- 拉取（单向读）
def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fetch_vm_text(vm_src: str, shared_src: str, timeout: float = 20.0) -> str | None:
    """读 VM 侧一个文本文件：共享盘优先 → prlctl exec 兜底。失败返回 None。"""
    try:
        if os.path.exists(shared_src):
            return Path(shared_src).read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        out = subprocess.run(
            [PRLCTL, "exec", VM_NAME, "cmd", "/c", f"type {vm_src}"],
            capture_output=True, timeout=timeout)
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _heartbeat_ts(raw: str | None) -> str | None:
    """从 execution.json 原文提取 updated_at（runner 分钟级心跳的权威时钟）。

    解析失败/缺失 → None（调用方退回 updated_at 判据，不脑补）。
    """
    if not raw or not raw.strip():
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    ts = str(rec.get("updated_at") or "").strip()
    return ts or None


def pull_from_vm(timeout: float = 20.0) -> bool:
    """把 VM 上 Vanta 的 main_contracts.json 拉到本地缓存（原子写）。

    路由：Parallels 共享盘（快）→ prlctl exec（共享盘卸载兜底）。
    任何一步失败（VM 睡眠/文件不存在/JSON 坏）→ False，不动本地缓存。

    同时探测 VM 侧 execution.json 的 updated_at 作为**runner 心跳**
    （runner 主循环分钟级写，2026-10-26 实证 11:28→11:29 推进）：
    main_contracts.json 是**事件驱动落盘**（仅换月/迟到解析时写，见 zz
    runner._write_main_contracts_state），无换月时文件本就不动——用心跳
    区分「runner 活着·判定持续有效」与「runner 死·文件冻结」。
    心跳探测失败不阻断主拉取（payload 无 _bridge_heartbeat，退回
    updated_at 判据）。
    """
    raw = _fetch_vm_text(VM_SRC, SHARED_SRC, timeout=timeout)
    if not raw or not raw.strip():
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or "contracts" not in payload:
        return False
    payload["_bridge_pulled_at"] = _now().isoformat(timespec="seconds")
    hb = _heartbeat_ts(_fetch_vm_text(VM_EXEC_SRC, SHARED_EXEC_SRC, timeout=timeout))
    if hb:
        payload["_bridge_heartbeat"] = hb
    _atomic_write_json(LOCAL_CACHE, payload)
    return True


def load_vanta(path: Path | None = None, max_age_minutes: float = _DEFAULT_MAX_AGE_MINUTES
               ) -> dict[str, str] | None:
    """读本地缓存的 Vanta 主力判定：{product: symbol}（小写 product → 原样 symbol）。

    新鲜度判据（2026-10-26 修正，**心跳优先**）：
      1. `_bridge_heartbeat`（pull 时探测的 VM 侧 execution.json updated_at，
         runner 主循环分钟级写）新鲜 → 判定有效。main_contracts.json 是
         事件驱动落盘（仅换月/迟到解析时写），runner 活着但无换月时
         updated_at 会一直停在最后事件时刻——用心跳区分「活着·有效」
         与「死了·冻结」（10-26 实证：runner 活着，文件停在 11:06，
         按 updated_at 判 15min 会把正常态误判过期）。
      2. 无心跳字段/心跳坏 → 退回 updated_at（Vanta 自己的事件时钟）。
    两者都缺失/过期/损坏 → None（调用方降级：不动系统缓存）。
    """
    p = path or LOCAL_CACHE
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    limit = max_age_minutes * 60
    hb = _parse_ts(rec.get("_bridge_heartbeat"))
    fresh = hb is not None and (_now() - hb).total_seconds() <= limit
    if not fresh:
        ts = _parse_ts(rec.get("updated_at"))
        if ts is None or (_now() - ts).total_seconds() > limit:
            return None
    contracts = rec.get("contracts")
    if not isinstance(contracts, dict):
        return None
    out: dict[str, str] = {}
    for product, info in contracts.items():
        if not isinstance(info, dict):
            continue
        symbol = str(info.get("symbol") or "").strip()
        if symbol:
            out[str(product).strip().lower()] = symbol
    return out or None


# ---------------------------------------------------------------- 系统缓存侧
def load_system_cache(path: Path | None = None) -> dict:
    p = path or SYSTEM_CACHE
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return rec if isinstance(rec, dict) else {}


def load_overrides(path: Path | None = None) -> dict:
    """main_overrides.json：进程内权威层（_apply_hot_contracts 每周期重读）。

    {product_lower: "SYMBOL"}；缺失/损坏 → {}。
    """
    p = path or OVERRIDE_FILE
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return rec if isinstance(rec, dict) else {}


def effective_system_symbol(system: dict, overrides: dict, product: str) -> str:
    """系统当前**真实在用**的主力记号：override 优先，hot cache 兜底。

    minishare_live._apply_hot_contracts 的优先级就是 override > HOT_CACHE
    （akshare 权威覆盖优先，绕开 OI 滞后/卡月）——比对必须与运行时同源，
    否则拿 HOT_CACHE 判分歧会漏掉「override 已锁但 cache 未锁」的双源漂移。
    """
    ov = overrides.get(product) or overrides.get(str(product).lower())
    if ov:
        return str(ov).strip()
    entry = system.get(product)
    if isinstance(entry, dict):
        return str(entry.get("main") or "").strip()
    return ""


def compare(vanta: dict[str, str], system: dict, overrides: dict | None = None
            ) -> list[dict]:
    """比对 Vanta 判定与系统**有效**主力，返回真实分歧列表（记号归一后）。

    只覆盖「Vanta 有且系统已跟踪」的品种；Vanta 多出的品种不上报（不新增品种）。
    每条分歧：{product, vanta, vanta_system_form, system, system_forced,
              vanta_ym, system_ym, rollback}（rollback=Vanta 比系统更旧——
    单调向前铁律下拒绝 apply，仅上报）。
    """
    overrides = overrides if overrides is not None else {}
    out: list[dict] = []
    for product, v_symbol in sorted(vanta.items()):
        entry = system.get(product)
        if not isinstance(entry, dict):
            continue                      # 系统未跟踪该品种 → 不新增
        s_symbol = effective_system_symbol(system, overrides, product)
        if not s_symbol:
            continue
        if canon_contract(v_symbol) != canon_contract(s_symbol):
            v_ym, s_ym = ym_of(v_symbol), ym_of(s_symbol)
            out.append({
                "product": product,
                "vanta": v_symbol,
                "vanta_system_form": to_system_symbol(v_symbol),
                "system": s_symbol,
                "system_forced": bool(entry.get("forced", False)),
                "vanta_ym": v_ym,
                "system_ym": s_ym,
                "rollback": bool(v_ym and s_ym and v_ym < s_ym),
            })
    return out


def minishare_live_running() -> bool:
    """minishare_live 是否在运行（--apply 前置闸：运行中其内存缓存会覆盖外部写入）。"""
    try:
        out = subprocess.run(["pgrep", "-f", "minishare_live"],
                             capture_output=True, timeout=10)
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False   # 查不到时放行（不做无证据拦截；apply 留痕可事后审计）


def apply_to_cache(vanta: dict[str, str], system_path: Path | None = None,
                   only_products: set[str] | None = None) -> dict:
    """把 Vanta 判定写入系统（双通道 + 单调向前），返回 {applied, refused_rollback}。

    双通道（复刻 refresh_main_contracts --apply 的既有模式）：
      ① main_overrides.json —— 进程内权威层，_apply_hot_contracts 每周期重读
         ⇒ **运行中也能生效**（这是关键：hot cache 的外部写在 runner 活着时
         会被下一 poll 覆盖，override 不会）；
      ② minishare_hot_contracts.json forced=True —— minishare_live 自身 OI
         防抖分支无条件尊重，双保险。
    单调向前铁律（同 akshare 路径）：Vanta 合约年月比现有有效主力**更旧** →
    拒绝写入（换月期源抖动会横跳，回退会把系统拽回旧合约），仅上报。
    无有效分歧 → 两个文件都不碰（幂等）。
    """
    path = system_path or SYSTEM_CACHE
    system = load_system_cache(path)
    overrides = load_overrides()
    diverged = compare(vanta, system, overrides)
    if only_products:
        diverged = [d for d in diverged if d["product"] in only_products]
    applied = [d for d in diverged if not d["rollback"]]
    refused = [d for d in diverged if d["rollback"]]
    if applied:
        now = time.time()
        for d in applied:
            sym = d["product"]
            form = d["vanta_system_form"]
            prev = system.get(sym) if isinstance(system.get(sym), dict) else {}
            entry = {
                "main": form,
                "secondary": form,
                "ts": now,
                "forced": True,           # 外部权威锁：minishare_live 无条件尊重
            }
            # 保留原 secondary 若非空且与新 main 不同（不丢信息）
            if prev.get("secondary") and canon_contract(prev["secondary"]) != canon_contract(form):
                entry["secondary"] = prev["secondary"]
            system[sym] = entry
            overrides[sym] = form        # 进程内权威层（运行中生效的通道）
        _atomic_write_json(path, system)
        _atomic_write_json(OVERRIDE_FILE, overrides)
        # 桥留痕（audit 用）
        state = {}
        try:
            state = json.loads(BRIDGE_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        state["last_apply"] = {
            "at": _now().isoformat(timespec="seconds"),
            "applied": [{k: d[k] for k in ("product", "vanta", "system")} for d in applied],
            "refused_rollback": [{k: d[k] for k in ("product", "vanta", "system")}
                                 for d in refused],
        }
        _atomic_write_json(BRIDGE_STATE, state)
    return {"applied": applied, "refused_rollback": refused}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Vanta 主力判定单向只读桥（C 方案）")
    ap.add_argument("--no-pull", action="store_true", help="不拉 VM，用本地缓存")
    ap.add_argument("--apply", action="store_true",
                    help="把 Vanta 判定写入系统缓存（forced 锁；需 minishare_live 停）")
    ap.add_argument("--max-age", type=float, default=_DEFAULT_MAX_AGE_MINUTES,
                    help=f"Vanta 数据最大年龄（分钟，默认 {_DEFAULT_MAX_AGE_MINUTES:g}）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    pulled = None
    if not args.no_pull:
        pulled = pull_from_vm()
        if not pulled and not args.json:
            print("[bridge] VM 拉取失败（VM 睡眠/文件缺失/共享盘与 prlctl 均不可达）"
                  "——尝试用本地缓存继续")

    vanta = load_vanta(max_age_minutes=args.max_age)
    if vanta is None:
        msg = "Vanta 主力数据不可用（缺失/不新鲜/损坏）——不动系统缓存（降级安全）"
        if args.json:
            print(json.dumps({"ok": False, "reason": msg}, ensure_ascii=False))
        else:
            print(f"[bridge] {msg}")
        return 1

    system = load_system_cache()
    overrides = load_overrides()
    diverged = compare(vanta, system, overrides)

    if args.apply:
        if minishare_live_running():
            msg = ("minishare_live 正在运行——拒绝 apply（override 通道虽运行中生效，"
                   "但同源写入需避开 poll 持久化窗口；先停 minishare_live 再 --apply）")
            if args.json:
                print(json.dumps({"ok": False, "reason": msg,
                                  "diverged": diverged}, ensure_ascii=False))
            else:
                print(f"[bridge] {msg}")
            return 3
        result = apply_to_cache(vanta)
        applied, refused = result["applied"], result["refused_rollback"]
        if args.json:
            print(json.dumps({"ok": True, "pulled": pulled,
                              "vanta_products": len(vanta),
                              "diverged": diverged, "applied": applied,
                              "refused_rollback": refused}, ensure_ascii=False))
        else:
            print(f"[bridge] Vanta 覆盖 {len(vanta)} 品种；分歧 {len(diverged)} 个，"
                  f"已写 {len(applied)} 个（单调向前拒绝回退 {len(refused)} 个）")
            for d in applied:
                print(f"  [APPLIED] {d['product']}: {d['system']} -> "
                      f"{d['vanta_system_form']}（Vanta {d['vanta']}）")
            for d in refused:
                print(f"  [REFUSED-ROLLBACK] {d['product']}: Vanta {d['vanta']} "
                      f"比系统 {d['system']} 更旧——单调向前铁律拒绝回退（仅上报）")
        return 0

    if args.json:
        print(json.dumps({"ok": True, "pulled": pulled,
                          "vanta_products": len(vanta),
                          "diverged": diverged}, ensure_ascii=False))
    else:
        print(f"[bridge] Vanta 覆盖 {len(vanta)} 品种（记号归一后）"
              f"与系统有效主力分歧 {len(diverged)} 个")
        for d in diverged:
            tag = "（回退，apply 会拒绝）" if d["rollback"] else ""
            print(f"  [DIVERGE] {d['product']}: Vanta {d['vanta']}"
                  f"（系统记号 {d['vanta_system_form']}） vs 系统 {d['system']}"
                  f"{'（系统已 forced）' if d['system_forced'] else ''}{tag}")
        if not diverged:
            print("  ✓ 无真实分歧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
