#!/usr/bin/env python3
"""状态引擎 v3.9.0 修复 · 隔离单元测试（2026-09-01，决策 #44）

覆盖 2026-08-31 状态引擎修复的三块改动 + 纯函数逻辑：
  T1  calc_tech_market_state      技术面综合评分（短序列兜底/趋势/震荡/方向/权重结构）
  T2  determine_market_state      switch_matrix 12 组合 + 3 根确认切换机制
  T3  update_consensus_state      共识聚合（样本门槛/极端标记/百分比）
  T4  持久化                       _save_market_states_locked / restore_market_states
                                   （原子写/过期不恢复/白名单过滤/round-trip/损坏容错）
  T5  _update_market_states_impl  🔴 幽灵键回归守卫（日线驱动，state["klines_data"]
                                   必须被无视）+ 节流 + 落盘节流 + rollover 联动 + 切换日志
  T6  _update_market_states       线程壳去重（_ms_state_updating）与复位
  T7  源码级回归守卫               幽灵键访问模式不得在 impl 中复活；main 必须调 restore
  T8  运行时只读 API 交叉验证      （服务器在线时尽力而为，离线 SKIP）

导入隔离：four_dim_live_runner 模块级启动 HTTP 服务（绑 8741）+ stdout 重定向，
禁止直接 import（决策 #43 踩坑）。本测试用 AST 从源码提取目标函数与字面量常量，
在注入 mock 的沙箱命名空间执行——零 import 副作用；持久化测试写入独立临时目录，
不触碰任何生产文件与写入接口（红线）。
"""

import ast
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
RUNNER_SRC = HERE / "four_dim_live_runner.py"
SNAPSHOT_FILE = HERE / "market_state_cache.json"

FAILED = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)


# ═══════════════════════════════════════════════════════════
# AST 沙箱：从 runner 源码提取函数与常量（规避模块级 import 副作用）
# ═══════════════════════════════════════════════════════════

EXTRACT_FUNCS = [
    "_calc_ma_alignment_score", "_calc_vol_level_score", "_calc_vol_change_score",
    "_calc_volume_price_score", "_calc_trend_strength_score",
    "calc_tech_market_state", "determine_market_state", "update_consensus_state",
    "_save_market_states_locked", "restore_market_states",
    "_update_market_states", "_update_market_states_impl",
]
EXTRACT_CONSTS = [
    "MARKET_STATE_TREND_EARLY", "MARKET_STATE_TREND_MID", "MARKET_STATE_TREND_LATE",
    "MARKET_STATE_SIDEWAYS",
    "TECH_WEIGHT_MA_ALIGNMENT", "TECH_WEIGHT_VOL_LEVEL", "TECH_WEIGHT_VOL_CHANGE",
    "TECH_WEIGHT_VOLUME_PRICE", "TECH_WEIGHT_TREND_STRENGTH",
    "TECH_MA_FAST", "TECH_MA_SLOW", "TECH_ATR_PERIOD", "TECH_VOLUME_MA_PERIOD",
    "TECH_ADX_PERIOD", "TECH_LOOKBACK_BARS",
    "TECH_SCORE_TREND_MID_MIN", "TECH_SCORE_TREND_LATE_MIN", "TECH_SCORE_SIDEWAYS_MAX",
    "STATE_CONFIRM_BARS",
    "SECOND_LEVEL_THINKING_ENABLED", "CONSENSUS_EXTREME_HIGH", "CONSENSUS_EXTREME_LOW",
    "CONSENSUS_MIN_SAMPLES",
    "_MS_STATE_FILE", "_MS_STATE_SAVE_INTERVAL", "_MS_STATE_MAX_AGE",
    "_MS_STATE_UPDATE_INTERVAL", "_ms_state_last_save", "_MS_STATE_LAST_UPDATE",
    "_ms_state_updating",
]


def _extract_code():
    src = RUNNER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts, funcs = [], []
    got_c, got_f = set(), set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in EXTRACT_FUNCS:
            funcs.append(node)
            got_f.add(node.name)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in EXTRACT_CONSTS):
            consts.append(node)
            got_c.add(node.targets[0].id)
    missing = (set(EXTRACT_FUNCS) - got_f) | (set(EXTRACT_CONSTS) - got_c)
    if missing:
        raise RuntimeError(f"AST 提取失败，源码缺节点: {sorted(missing)}")
    mod = ast.Module(body=consts + funcs, type_ignores=[])
    ast.fix_missing_locations(mod)
    return compile(mod, str(RUNNER_SRC), "exec"), src


_CODE, _SRC = _extract_code()
print(f"[sandbox] 已提取 {len(EXTRACT_FUNCS)} 个函数 + {len(EXTRACT_CONSTS)} 个常量（AST 沙箱，未 import runner）")

_TMPDIRS = []


class _MockExp:
    def __init__(self):
        self.roll_thin = []

    def set_roll_thin(self, sym, flag):
        self.roll_thin.append((sym, bool(flag)))


def _new_tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPDIRS.append(d)
    return d


def make_sandbox(tmpdir, daily_map=None, roll_levels=None):
    """构建隔离沙箱命名空间。daily_map: sym→DataFrame（mock 日线源）；
    roll_levels: sym→换月级别（默认全 ok）。均带调用计数。"""
    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    calls = {"load_daily": 0, "journal": 0, "log_transition": 0}

    def _load_daily(sym):
        calls["load_daily"] += 1
        return daily_map.get(sym) if daily_map else None

    def _rollover(sym):
        lv = (roll_levels or {}).get(sym, "ok")
        return {"level": lv} if lv else None

    exp = _MockExp()
    transitions = []

    def _log_transition(**kw):
        calls["log_transition"] += 1
        transitions.append(kw)

    ns = {
        "json": json, "os": os, "time": time, "threading": threading,
        "HERE": str(tmpdir),                      # 持久化写入隔离临时目录
        "market_state_cache": {},
        "consensus_state": {},
        "SYMBOLS": {"TST": {"name": "测试甲"}, "TS2": {"name": "测试乙"}, "TS3": {"name": "测试丙"}},
        "load_daily_refreshed": _load_daily,
        "load_journal_trades_for_perf": lambda: (calls.__setitem__("journal", calls["journal"] + 1) or []),
        "calc_performance_score": lambda trades: {"total_score": 50.0, "level": "mid"},
        "rollover_info": _rollover,
        "exp": exp,
        "log_state_transition": _log_transition,
    }
    exec(_CODE, ns)
    ns["_calls"] = calls
    ns["_transitions"] = transitions
    ns["_tmpdir"] = tmpdir
    return ns


# ── 合成K线（开盘/最高/最低/收盘/成交量）──

def kl_uptrend(n=120, start=100.0, step=1.0):
    return [{"open": start + i * step - 0.5, "high": start + i * step + 1.5,
             "low": start + i * step - 1.5, "close": start + i * step,
             "volume": 1000 + i * 5} for i in range(n)]


def kl_downtrend(n=120, start=300.0, step=1.2):
    return [{"open": start - i * step, "high": start - i * step + 1.5,
             "low": start - i * step - 1.5, "close": start - i * step,
             "volume": 1000} for i in range(n)]


def kl_sideways(n=120, base=100.0, amp=0.4):
    out = []
    for i in range(n):
        c = base + amp * math.sin(i * 0.5)
        out.append({"open": c, "high": c + 0.6, "low": c - 0.6, "close": c, "volume": 1000})
    return out


def df_of(klines):
    return pd.DataFrame(klines)


TREND_STATES = {"trend_early", "trend_mid", "trend_late"}


# ═══════════════════════════════════════════════════════════
# T1: calc_tech_market_state 技术面综合评分
# ═══════════════════════════════════════════════════════════
print("\n── T1 calc_tech_market_state ──")
ns = make_sandbox(_new_tmpdir(prefix="mse_t1_"))
calc = ns["calc_tech_market_state"]

r = calc([], None)
check("T1a 短序列兜底（<65根 → 50分/震荡）",
      r["total_score"] == 50 and r["state_candidate"] == "sideways" and r["ma_fast"] == 0,
      f"score={r['total_score']} state={r['state_candidate']}")

up = calc(kl_uptrend(), [k["volume"] for k in kl_uptrend()])
check("T1b 强上行 → 趋势态 + 方向 long",
      up["state_candidate"] in TREND_STATES and up["trend_direction"] == "long",
      f"score={up['total_score']} state={up['state_candidate']} dir={up['trend_direction']}")

sw = calc(kl_sideways(), [k["volume"] for k in kl_sideways()])
check("T1c 区间震荡 → sideways",
      sw["state_candidate"] == "sideways",
      f"score={sw['total_score']} state={sw['state_candidate']}")

dn = calc(kl_downtrend(), None)
check("T1d 下行趋势 → 方向 short",
      dn["trend_direction"] == "short" and dn["state_candidate"] in TREND_STATES,
      f"score={dn['total_score']} state={dn['state_candidate']} dir={dn['trend_direction']}")

ind = up["indicators"]
expect_total = round((ind["ma_alignment"] * 25 + ind["vol_level"] * 20 + ind["vol_change"] * 15
                      + ind["volume_price"] * 20 + ind["trend_strength"] * 20) / 100, 1)
check("T1e 加权结构（五维权重 25/20/15/20/20 复算一致）",
      abs(up["total_score"] - expect_total) < 0.05,
      f"实现={up['total_score']} 复算={expect_total} 子分={ind}")


# ═══════════════════════════════════════════════════════════
# T2: determine_market_state switch_matrix + 确认机制
# ═══════════════════════════════════════════════════════════
print("\n── T2 determine_market_state ──")
ns = make_sandbox(_new_tmpdir(prefix="mse_t2_"))
det = ns["determine_market_state"]

MATRIX_EXPECT = {
    ("trend_early", "good"): ("trend_early", "high"),
    ("trend_early", "mid"): ("trend_early", "mid"),
    ("trend_early", "poor"): ("sideways", "low"),
    ("trend_mid", "good"): ("trend_mid", "high"),
    ("trend_mid", "mid"): ("trend_mid", "mid"),
    ("trend_mid", "poor"): ("trend_late", "low"),
    ("trend_late", "good"): ("trend_late", "mid"),
    ("trend_late", "mid"): ("trend_late", "mid"),
    ("trend_late", "poor"): ("trend_late", "high"),
    ("sideways", "good"): ("sideways", "mid"),
    ("sideways", "mid"): ("sideways", "mid"),
    ("sideways", "poor"): ("sideways", "high"),
}
ok_all, bad = True, []
for (tech_state, perf_level), (exp_state, exp_conf) in MATRIX_EXPECT.items():
    tr = {"state_candidate": tech_state, "total_score": 60.0, "trend_direction": "long"}
    pr = {"level": perf_level, "total_score": 55.0}
    res = det(tr, pr, prev_state=None)
    if res["state"] != exp_state or res["confidence"] != exp_conf:
        ok_all = False
        bad.append(f"{tech_state}+{perf_level}→({res['state']},{res['confidence']}) 期望({exp_state},{exp_conf})")
check("T2a switch_matrix 12 组合逐一一致", ok_all, "；".join(bad) if bad else "12/12")

# 确认机制：prev≠candidate 需连续 STATE_CONFIRM_BARS(3) 根确认才切换
tr_te = {"state_candidate": "trend_early", "total_score": 60.0, "trend_direction": "long"}
pr_mid = {"level": "mid", "total_score": 50.0}
seq = []
counter = 0
for _ in range(3):
    res = det(tr_te, pr_mid, prev_state="sideways", confirm_counter=counter)
    seq.append((res["state"], res["confirm_counter"], res["switched"]))
    counter = res["confirm_counter"]
check("T2b 确认机制（前2根保持旧态，第3根切换）",
      seq[0] == ("sideways", 1, False) and seq[1] == ("sideways", 2, False)
      and seq[2] == ("trend_early", 0, True),
      f"轨迹={seq}")

res = det(tr_te, pr_mid, prev_state=None)
check("T2c prev=None 直接采用候选态", res["state"] == "trend_early" and res["confirm_counter"] == 0 and not res["switched"])
res = det(tr_te, pr_mid, prev_state="trend_early", confirm_counter=2)
check("T2d prev==candidate 计数器归零", res["state"] == "trend_early" and res["confirm_counter"] == 0 and not res["switched"])
res = det({"state_candidate": "unknown_state", "total_score": 50.0}, pr_mid, prev_state=None)
check("T2e 非法 tech 态透传 + low 置信", res["state"] == "unknown_state" and res["confidence"] == "low")


# ═══════════════════════════════════════════════════════════
# T3: update_consensus_state 共识聚合
# ═══════════════════════════════════════════════════════════
print("\n── T3 update_consensus_state ──")
ns = make_sandbox(_new_tmpdir(prefix="mse_t3_"))
ucs = ns["update_consensus_state"]


def mk_states(n, state):
    return {f"s{i}": {"state": state} for i in range(n)}


ucs({})
check("T3a 空输入不更新", "consensus_score" not in ns["consensus_state"])
ucs(mk_states(9, "trend_mid"))
check("T3b 样本<门槛(10)不更新", "consensus_score" not in ns["consensus_state"])

ns["consensus_state"] = {}
ucs(mk_states(12, "trend_mid"))
cs = ns["consensus_state"]
check("T3c 全趋势 → 共识100 + 极端乐观", cs.get("consensus_score") == 100 and cs.get("extreme_high") is True and cs.get("extreme_low") is False)

ns["consensus_state"] = {}
ucs(mk_states(12, "sideways"))
cs = ns["consensus_state"]
check("T3d 全震荡 → 共识0 + 极端悲观", cs.get("consensus_score") == 0 and cs.get("extreme_low") is True and cs.get("extreme_high") is False)

ns["consensus_state"] = {}
mixed = {**{f"t{i}": {"state": "trend_mid"} for i in range(6)},
         **{f"x{i}": {"state": "sideways"} for i in range(6)}}
ucs(mixed)
cs = ns["consensus_state"]
check("T3e 半趋势半震荡 → 共识50 + 双极端皆 False", cs.get("consensus_score") == 50 and not cs.get("extreme_high") and not cs.get("extreme_low"))


# ═══════════════════════════════════════════════════════════
# T4: 持久化 save / restore（隔离临时目录）
# ═══════════════════════════════════════════════════════════
print("\n── T4 持久化 round-trip ──")
tmp4 = _new_tmpdir(prefix="mse_t4_")
ns = make_sandbox(tmp4)
snap_path = Path(ns["_MS_STATE_FILE"])
info = {"state": "trend_mid", "prev_state": "sideways", "confirm_counter": 0,
        "tech_score": 72.1, "perf_score": 55.0, "confidence": "mid",
        "trend_direction": "long", "tech_indicators": {"ma_alignment": 80.0},
        "last_update": 1750000000.5, "switched": True}

ns["market_state_cache"] = {"TST": info}
ns["_save_market_states_locked"]()
check("T4a 落盘结构（saved_at+states，原子写无 .tmp 残留）",
      snap_path.exists() and not Path(str(snap_path) + ".tmp").exists(),
      f"文件={snap_path.name}")
snap = json.loads(snap_path.read_text(encoding="utf-8"))
check("T4a2 saved_at 为当前时间且 states 与缓存一致",
      abs(snap["saved_at"] - time.time()) < 5 and snap["states"] == {"TST": info})

fresh = make_sandbox(tmp4)  # 新命名空间模拟"重启"
check("T4b 文件缺失 → 0", make_sandbox(_new_tmpdir(prefix="mse_t4b_"))["restore_market_states"]() == 0)

n = fresh["restore_market_states"]()
check("T4c 正常恢复 → 数量正确", n == 1 and fresh["market_state_cache"].get("TST") == info)

# 白名单过滤：快照里塞不在 SYMBOLS 的品种
ghost_snap = {"saved_at": time.time(), "states": {"TST": info, "GHOST": info}}
snap_path.write_text(json.dumps(ghost_snap), encoding="utf-8")
f2 = make_sandbox(tmp4)
n = f2["restore_market_states"]()
check("T4d 只恢复 SYMBOLS 内品种（GHOST 被跳过）",
      n == 1 and "GHOST" not in f2["market_state_cache"] and "TST" in f2["market_state_cache"])

# 过期快照（>7天）
stale = {"saved_at": time.time() - 8 * 86400, "states": {"TST": info}}
snap_path.write_text(json.dumps(stale), encoding="utf-8")
f3 = make_sandbox(tmp4)
check("T4e 过期快照（>7天）不恢复", f3["restore_market_states"]() == 0 and not f3["market_state_cache"])

# 损坏 JSON 容错
snap_path.write_text("{ not valid json", encoding="utf-8")
f4 = make_sandbox(tmp4)
check("T4f 损坏 JSON → 0（异常吞掉不抛出）", f4["restore_market_states"]() == 0)

# 完整 round-trip：save → 清空 → restore → 相等
snap_path.unlink(missing_ok=True)
f5 = make_sandbox(tmp4)
full = {"TST": info, "TS2": dict(info, state="sideways")}
f5["market_state_cache"] = full
f5["_save_market_states_locked"]()
f6 = make_sandbox(tmp4)
n = f6["restore_market_states"]()
check("T4g round-trip 内容逐字段相等", n == 2 and f6["market_state_cache"] == full)
check("T4h 恢复成功后刷新 _ms_state_last_save", f6["_ms_state_last_save"] > 0)


# ═══════════════════════════════════════════════════════════
# T5: _update_market_states_impl —— 幽灵键回归守卫 + 节流 + 联动
# ═══════════════════════════════════════════════════════════
print("\n── T5 _update_market_states_impl（核心回归）──")

# 🔴 幽灵键守卫：state 里塞入"强趋势"伪造 klines_data，日线 mock 返回震荡序列。
# 旧实现读 state["klines_data"] 会判成趋势；新实现必须无视幽灵键、以日线为准判震荡。
ghost_state = {"klines_data": {"TST": kl_uptrend(), "TS2": kl_uptrend()}}
daily = {"TST": df_of(kl_sideways()), "TS2": df_of(kl_uptrend()), "TS3": df_of(kl_sideways()[:30])}
ns = make_sandbox(_new_tmpdir(prefix="mse_t5a_"), daily_map=daily,
                  roll_levels={"TST": "warn"})
ns["_ms_state_last_save"] = 0.0
ns["_MS_STATE_LAST_UPDATE"] = 0.0
ns["_update_market_states_impl"](None, ghost_state)
cache = ns["market_state_cache"]
check("T5a 🔴 幽灵键守卫：state.klines_data 被无视，以日线震荡判 sideways",
      cache.get("TST", {}).get("state") == "sideways",
      f"TST={cache.get('TST', {}).get('state')}（旧实现会判成 trend）")
# 强趋势日线首轮不立即切换（确认机制）：candidate=trend_mid ≠ prev默认sideways，
# counter 1<3 → 保持 sideways；连续 3 轮后才完成切换——跨轮确认轨迹完整验证
ts2_round1 = cache.get("TS2", {})
check("T5b 首轮强趋势不切换（确认机制：counter=1 保持旧态）",
      ts2_round1.get("state") == "sideways" and ts2_round1.get("confirm_counter") == 1,
      f"state={ts2_round1.get('state')} counter={ts2_round1.get('confirm_counter')}")
for _ in range(2):
    ns["_MS_STATE_LAST_UPDATE"] = 0.0  # 绕过更新节流，模拟连续三轮
    ns["_update_market_states_impl"](None, ghost_state)
ts2_final = ns["market_state_cache"]["TS2"]
check("T5b2 三轮确认后切换为趋势态（counter 累积 1→2→3）",
      ts2_final["state"] in TREND_STATES and ts2_final["confirm_counter"] == 0
      and ts2_final["switched"] is True and ts2_final["trend_direction"] == "long",
      f"最终={ts2_final['state']}")
check("T5c 日线<65根的品种被跳过", "TS3" not in cache and ns["_calls"]["load_daily"] >= 3)
check("T5d 缓存条目十键结构完整",
      all(k in cache["TST"] for k in ("state", "prev_state", "confirm_counter", "tech_score",
                                      "perf_score", "confidence", "trend_direction",
                                      "tech_indicators", "last_update", "switched")))
check("T5e rollover 联动：warn 品种 set_roll_thin(True)，其余 False",
      ("TST", True) in ns["exp"].roll_thin and ("TS2", False) in ns["exp"].roll_thin
      and ("TS3", True) not in ns["exp"].roll_thin,
      f"roll_thin={ns['exp'].roll_thin}")

# 节流：缓存非空 + 上轮更新在 600s 内 → 直接返回（日线零加载）
ns["_calls"]["load_daily"] = 0
ns["_MS_STATE_LAST_UPDATE"] = time.time()
ns["_update_market_states_impl"](None, ghost_state)
check("T5f 节流（缓存非空+600s内）→ 零加载", ns["_calls"]["load_daily"] == 0)

# 空缓存不节流（首轮必须能算）
ns2 = make_sandbox(_new_tmpdir(prefix="mse_t5g_"), daily_map=daily)
ns2["_MS_STATE_LAST_UPDATE"] = time.time()
ns2["_ms_state_last_save"] = time.time()
ns2["_update_market_states_impl"](None, {})
check("T5g 空缓存不受节流拦截（首轮照常计算）",
      ns2["_calls"]["load_daily"] == 3 and len(ns2["market_state_cache"]) == 2,
      f"加载={ns2['_calls']['load_daily']} 产出={len(ns2['market_state_cache'])}（TS3 数据不足跳过）")

# 落盘节流：_ms_state_last_save=0 → 落盘一次；300s 内第二轮不再落盘
save_calls = []
ns3 = make_sandbox(_new_tmpdir(prefix="mse_t5h_"), daily_map=daily)
_orig_save = ns3["_save_market_states_locked"]
ns3["_save_market_states_locked"] = lambda: (save_calls.append(1), _orig_save())[1]
ns3["_ms_state_last_save"] = 0.0
ns3["_MS_STATE_LAST_UPDATE"] = 0.0
ns3["_update_market_states_impl"](None, {})
first = len(save_calls)
ns3["_MS_STATE_LAST_UPDATE"] = 0.0  # 强制重算（绕过更新节流，只测落盘节流）
ns3["_update_market_states_impl"](None, {})
check("T5h 落盘节流（首跑落盘1次，300s内不重复）", first == 1 and len(save_calls) == 1, f"save_calls={len(save_calls)}")

# 状态切换日志：预置 counter=2 的旧态，日线强趋势 → 第3根确认切换 → 记日志
ns4 = make_sandbox(_new_tmpdir(prefix="mse_t5i_"), daily_map={"TST": df_of(kl_uptrend())})
ns4["market_state_cache"] = {"TST": {"state": "sideways", "confirm_counter": 2}}
ns4["_MS_STATE_LAST_UPDATE"] = 0.0
ns4["_ms_state_last_save"] = time.time()
ns4["_update_market_states_impl"](None, {})
entry = ns4["market_state_cache"]["TST"]
check("T5i 确认切换 → log_state_transition 落日志",
      entry["switched"] is True and entry["prev_state"] == "sideways"
      and ns4["_calls"]["log_transition"] == 1
      and ns4["_transitions"][0].get("symbol") == "TST",
      f"new={entry['state']} 日志={ns4['_transitions']}")


# ═══════════════════════════════════════════════════════════
# T6: 线程壳 _update_market_states 去重与复位
# ═══════════════════════════════════════════════════════════
print("\n── T6 线程壳去重 ──")
ns5 = make_sandbox(_new_tmpdir(prefix="mse_t6_"))
impl_calls = {"n": 0}


def _slow_impl(feed, state):
    impl_calls["n"] += 1
    time.sleep(0.4)


ns5["_update_market_states_impl"] = _slow_impl
ns5["_ms_state_updating"] = False
ns5["_update_market_states"](None, {})   # 启动 worker（同步置位去重标记）
ns5["_update_market_states"](None, {})   # 应被去重
flag_during = ns5["_ms_state_updating"]
t0 = time.time()
while ns5["_ms_state_updating"] and time.time() - t0 < 5:
    time.sleep(0.05)
check("T6a 去重：运行中重复调用不重入（impl 只执行一次）", impl_calls["n"] == 1,
      f"impl_calls={impl_calls['n']} 运行中标记={flag_during}")
check("T6b 线程结束后 _ms_state_updating 复位 False", ns5["_ms_state_updating"] is False)


# ═══════════════════════════════════════════════════════════
# T7: 源码级回归守卫（幽灵键不得复活）
# ═══════════════════════════════════════════════════════════
print("\n── T7 源码级回归守卫 ──")
_tree = ast.parse(_SRC)


def _find_func(name):
    for node in _tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


_impl_node = _find_func("_update_market_states_impl")
_ghost_access = []
for node in ast.walk(_impl_node):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "state" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "klines_data"):
        _ghost_access.append("state.get('klines_data')")
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == "state" and isinstance(node.slice, ast.Constant)
            and node.slice.value == "klines_data"):
        _ghost_access.append("state['klines_data']")
check("T7a 🔴 impl 内无幽灵键访问（AST 级，docstring 不误报）", not _ghost_access, f"违规={_ghost_access}")

_uses_daily = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "load_daily_refreshed"
                  for node in ast.walk(_impl_node))
check("T7b impl 以 load_daily_refreshed 为数据源", _uses_daily)

_main_node = _find_func("main")
_main_restores = _main_node is not None and any(
    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    and node.func.id == "restore_market_states" for node in ast.walk(_main_node))
check("T7c main() 启动时调用 restore_market_states", _main_restores)

check("T7d /api/market-state 路由存在", 'self.path.startswith("/api/market-state")' in _SRC)


# ═══════════════════════════════════════════════════════════
# T8: 运行时只读 API 交叉验证（在线则验，离线 SKIP）
# ═══════════════════════════════════════════════════════════
print("\n── T8 运行时只读 API 交叉验证 ──")
try:
    with urllib.request.urlopen("http://127.0.0.1:8741/api/market-state", timeout=4) as resp:
        ms = json.loads(resp.read().decode("utf-8"))
    check("T8a 线上 state_count > 0（幽灵键修复后引擎真实产出）",
          (ms.get("state_count") or 0) > 0, f"state_count={ms.get('state_count')}")
    if SNAPSHOT_FILE.exists():
        snap = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        fresh_ok = time.time() - float(snap.get("saved_at") or 0) < 7 * 86400
        check("T8b 快照文件结构合法且未过期",
              fresh_ok and len(snap.get("states") or {}) > 0,
              f"states={len(snap.get('states') or {})}")
    else:
        check("T8b 快照文件存在", False, "market_state_cache.json 不存在")
except Exception as e:
    print(f"[SKIP] T8 服务器离线（{type(e).__name__}）— 线上验证改由重启后人工/API 核对")


# ── 汇总 ─────────────────────────────────────────────────────
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)
print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✓ 全部通过 — 状态引擎 v3.9.0 修复（幽灵键/持久化/线程壳）行为符合设计")
