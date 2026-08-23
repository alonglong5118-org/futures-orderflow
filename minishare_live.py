# -*- coding: utf-8 -*-
"""minishare 实时行情驱动（四维策略·实时路径）
=================================================================
用 minishare 的 rt_fut_k 不限次快照（当前 token 仅开放此端点）驱动
**实时** 数据需求，替换原 sina 在「盘中实时」路径的角色：

  1) 实时价 / 快照：rt_fut_k(ts_code="*") 每 60s 一次，全市场 990 合约。
  2) 实时 5m 合成：用 60s 快照的 close 在 5 分钟桶内聚合 OHLC → T@5m 触发源。
  3) 实时 C_flow：复用 four_dim_strategy.FlowAggregator，把 60s 快照的
     (价, 持仓, 量) 差分喂进去 → 资金净流速率评分。

⚠️ 权限边界（实测 2026-08-11，token CWIFB8...）：
  - 可用：rt_fut_k（实时快照，不限次）
  - 不可用（权限不足）：fut_daily / fut_5min / fut_1min / fut_tick /
    fut_k / fut_main / fut_cont / fut_basis / fut_inventory ...
  → 历史日线仍走本地 CSV；历史回测 5m 仍走本地缓存 + sina 兜底；
    基本面 F（基差/库存）仍走 akshare（minishare 无 fut_basis/fut_inventory 权限）。
  本模块只负责「实时」这一块，把 sina 从实时路径里彻底拿掉。

品种映射（minishare 命名坑：JM=焦炭主连，JMM=焦煤主连，务必按名匹配）：
  JM(焦煤)→JMM  J(焦炭)→JM  jd→JDM  lh→LHM
⚠️ 持仓盯市必须用真实可交割合约，禁止回落到主连（曾因错挂主连导致 CF 盯市差 580 点）。
  2026-08-14 起合约改为**动态解析**：contract_mode=auto_main/auto_secondary 时按
  持仓量(OI)排名自动取当前主力/次主力可交割合约（随换月滚动，根治 rb 写死 RB2609
  导致盯市价过时的问题）；contract_mode=fixed 时沿用 trade_config 写死的 contract
  （远月套保等固定场景）。未指定合约的品种（纯信号生成）才按中文名匹配到主连。
"""
from __future__ import annotations
import os, sys, json, time, re
from datetime import datetime
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MINISHARE_JSON = os.path.join(HERE, "minishare.json")
MAPPING_CACHE = os.path.join(HERE, "minishare_cache.json")

# ── 已知映射（已移除 _PINNED，2026-08-13）──
# 历史上 _PINNED 把 FG/SA/jd/lh/JM/J 钉到「主连」(FGM/SAM/JDM/LHM/JMM/JM)，
# 会在 _discover 第三步被回写覆盖，与第零步「权威交割合约」(trade_config.contract，
# 如 FG609/JM2609) 冲突，导致这些品种盯市退回主连价。现在 55 品种全覆盖
# trade_config.contract，一律以真实交割合约为准，_PINNED 已成死代码，移除。

# 具体交割合约：sym_key -> 合约数字串（在 minishare ts_code 中按「包含」匹配，
# 避开中文名歧义：SA2609/SA2701/主连 都叫"纯碱"，只能靠合约代码区分）。
# 主连 SA 已 pinned 到 SAM，这里只补具体合约，互不冲突。
_CONTRACTS = {"SA01": "SA2701"}

# ── 动态主力/次主力合约解析（2026-08-14 根治：合约月份写死导致盯市价过时）──
# 根因：contract_specs[sym]["contract"] 写死具体月份（如 rb→RB2609），但期货主力合约
# 随换月滚动；用户实际交易当前主力/次主力合约，写死月份会逐步偏离真实成交价
# （rb 曾显示 RB2609=2986，而实际主力在 3016）。改为按持仓量(OI)排名动态解析：
#   contract_mode = "auto_main"（默认）/ "auto_secondary" / "fixed"
#   auto_main      → 取该品种持仓量最高的可交割合约（当前主力）
#   auto_secondary → 取持仓量第二高的可交割合约（次主力）
#   fixed          → 沿用 contract_specs 写死的合约（远月套保等固定场景）
# HOT_CACHE 持久化，首次 poll 后即自动滚动换月，对任意品种通用（不再逐品种写死）。
HOT_CACHE_FILE = os.path.join(HERE, "minishare_hot_contracts.json")
HOT_CACHE = {}  # sym -> {"main": code, "secondary": code, "ts": epoch}
# akshare 权威主力覆盖：外部采集器(refresh_main_contracts.py)写入 main_overrides.json，
# 优先级高于 OI 持仓量解析，根治换月期 OI 滞后/卡月导致面板与信号主力错位
# （如农产品主力已滚至 2611/2701，而 OI 仍把 2609 排第一）。沿用 info_dimension/macro_context
# 的「外部写/内部读」架构：采集器跑系统 python3+akshare，runner 只读。
MAIN_OVERRIDE_FILE = os.path.join(HERE, "main_overrides.json")
MAIN_OVERRIDE = {}  # sym(lower) -> ts_code(upper)


def _load_main_override():
    """读取 main_overrides.json（akshare 全市场真实主力），供合约解析优先采用。
    文件缺失/异常时清空并回退 OI 解析。每次调用重读，确保采集器更新后即时生效。"""
    global MAIN_OVERRIDE
    try:
        if os.path.exists(MAIN_OVERRIDE_FILE):
            d = json.load(open(MAIN_OVERRIDE_FILE, encoding="utf-8")) or {}
            MAIN_OVERRIDE = {str(k).lower(): str(v).upper() for k, v in d.items() if v}
            return
    except Exception:
        pass
    MAIN_OVERRIDE = {}


def _load_hot_cache():
    global HOT_CACHE
    try:
        if os.path.exists(HOT_CACHE_FILE):
            raw = json.load(open(HOT_CACHE_FILE, encoding="utf-8")) or {}
            HOT_CACHE = {}
            for sym, v in raw.items():
                HOT_CACHE[sym] = {
                    "main": v.get("main", ""),
                    "secondary": v.get("secondary", ""),
                    "ts": v.get("ts", 0),
                    "forced": bool(v.get("forced", False)),
                    "source": v.get("source"),
                }
    except Exception:
        HOT_CACHE = {}
    _load_main_override()


def _save_hot_cache():
    try:
        json.dump(HOT_CACHE, open(HOT_CACHE_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass


_load_hot_cache()


def _contract_mode(sym):
    """返回品种合约模式：auto_main(默认)/auto_secondary/fixed。"""
    try:
        tcfg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
        return str(tcfg.get("contract_specs", {}).get(sym, {}).get("contract_mode", "auto_main")).lower()
    except Exception:
        return "auto_main"


def normalize_contract_code(code):
    """统一合约代码为 4 位年份+月份格式。
    数据源常返回 3 位缩略（如 FG608=FG2608、CF609=CF2609、AP610=AP2610），
    本函数按当前年份推断世纪并补齐为 4 位，避免面板显示混乱。
    已是 4 位者原样返回；无法识别者原样返回。
    """
    code = str(code).upper().strip()
    c = re.sub(r"[^A-Za-z0-9]", "", code)
    m = re.match(r"^([A-Za-z]+?)(\d{3,4})$", c)
    if not m:
        return code
    prefix, digits = m.groups()
    if len(digits) == 4:
        return f"{prefix}{digits}"
    # 3 位：末位年份 + 两位月份
    y_digit = int(digits[0])
    month = digits[1:]
    try:
        if not (1 <= int(month) <= 12):
            return code
    except Exception:
        return code
    cur_y = datetime.now().year % 100
    # 候选世纪：0~4（未来 40 年内）
    candidates = sorted((y_digit + 10 * k) % 100 for k in range(5))
    # 优先选择 >= 当前年份-1 的最小值（允许近月，但主力不会太远）
    valid = [y for y in candidates if y >= cur_y - 1]
    best = valid[0] if valid else max(candidates)
    return f"{prefix}{best:02d}{month}"


def _is_tradeable_contract(code):
    """判断 ts_code 是否为真实可交割合约（排除主连/连续合成系列）。
    主连系列形如 RBM/SAM/JMM/JM(焦炭主连)/JDM/LHM/FGM，无年份月份数字尾，被排除。"""
    c = re.sub(r"[^A-Za-z0-9]", "", str(code).upper())
    return bool(re.match(r"^([A-Za-z]+?)(\d{3,4})", c))


def _contract_ym(code):
    """合约代码 -> 年月整数(YYYYMM)，用于换月判定；无法识别返回 None。"""
    m = re.match(r"^([A-Za-z]+?)(\d{3,4})$", re.sub(r"[^A-Za-z0-9]", "", str(code).upper()))
    if not m:
        return None
    d = m.group(2)
    yy = int(d[:2]); mm = int(d[2:])
    yy += 2000 if yy < 70 else 1900
    return yy * 100 + mm


def _now_ym():
    """当前年月整数 YYYYMM。"""
    n = datetime.now()
    return n.year * 100 + n.month


def _authoritative_contracts():
    """返回每个品种的「生效交割合约」（持仓盯市权威源）。
    2026-08-14 起为动态解析：contract_mode=auto_main/auto_secondary 时返回当前
    主力/次主力可交割合约（按持仓量 OI 排名，HOT_CACHE 提供），fixed 时返回
    trade_config 写死的 contract。无 HOT_CACHE 时回退写死值（首次启动前兜底）。

    ⚠️ 历史坑（2026-08-13）：品种映射曾按中文名匹配到「主连」，导致 CF 错挂
    「棉花主连」(CFM=16720) 而非真实持仓合约 CF609(=16140)，盯市价差 580 点；
    rb 同理错挂「螺纹主连」(RBM=3016)。凡 auto 模式解析出的仍是**真实可交割合约**
    （最高持仓量者），绝非主连合成系列，故不回归该坑。
    返回 {sym: ts_code}。"""
    out = {}
    _load_main_override()  # 重读最新 akshare 权威覆盖（采集器可能刚更新文件）
    tcfg = {}
    try:
        tcfg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
    except Exception:
        tcfg = {}
    for sym, spec in tcfg.get("contract_specs", {}).items():
        mode = str(spec.get("contract_mode", "auto_main")).lower()
        hc = HOT_CACHE.get(sym)
        if mode in ("auto_main", "auto_secondary") and hc:
            key = "main" if mode == "auto_main" else "secondary"
            # akshare 权威覆盖优先：绕开 OI 滞后/卡月（如农产品主力已滚至 2611/2701）
            code = MAIN_OVERRIDE.get(sym.lower()) or MAIN_OVERRIDE.get(sym) or hc.get(key) or hc.get("main")
            if code:
                out[sym] = str(code).upper()
                continue
        ctr = spec.get("contract")
        if ctr:
            out[sym] = normalize_contract_code(ctr)
    return out

_poll_interval = 60
BARS_CACHE = os.path.join(HERE, "live_bars.json")


def _load_token():
    try:
        cfg = json.load(open(MINISHARE_JSON))
        return cfg.get("token"), int(cfg.get("interval", 60))
    except Exception:
        return None, 60


def _api():
    global _poll_interval
    token, iv = _load_token()
    if not token:
        return None, 60
    try:
        import minishare as m
        m.set_token(token)
        _poll_interval = iv
        return m.pro_api(), iv
    except Exception as e:
        print(f"[minishare_live] 加载失败: {e}")
        return None, 60


def _load_mapping_cache():
    try:
        if os.path.exists(MAPPING_CACHE):
            raw = json.load(open(MAPPING_CACHE))
            return {k: normalize_contract_code(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}  # 无缓存时回退空，由 _discover/_init_from_cache 的权威钉死 + 按名/按代码匹配重建


def _save_mapping_cache(data):
    try:
        json.dump(data, open(MAPPING_CACHE, "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass


class MinishareLiveFeed:
    """维护所有活跃品种的实时快照 + 5m 合成 K 线 + C_flow 累加器。
    品种映射通过 rt_fut_k 全量数据的 name 字段自动发现，缓存到 minishare_cache.json。"""

    def __init__(self):
        self.pro, self.iv = _api()
        self.sym2code = {}       # sym → minishare ts_code（动态）
        self.code2sym = {}       # ts_code → sym（反向）
        self.last_snap = {}
        self.bars = {}
        self.last_bar_minute = {}
        self.flow = {}
        self._mapped = False     # 是否已从 rt_fut_k 自动发现映射
        self._prefix_top2 = {}   # prefix -> [主力, 次主力] 合约代码（防抖用）
        self._init_from_cache()
        self._auth = _authoritative_contracts()  # 生效交割合约映射（动态：主力/次主力/固定），供 poll 安全阀使用
        # 若已有持久化的主力合约缓存，启动时即应用（避免首轮 poll 前的空窗）
        if HOT_CACHE:
            try:
                self._apply_hot_contracts()
            except Exception as e:
                print(f"[minishare_live] 启动应用主力合约失败: {e}")
        self._load_bars()
        # 持仓合约优先钉死（覆盖 auto_main 动态换月）：账户总览的现价/浮盈亏
        # 必须以用户真实开仓合约为盯市源，不能跟随主力合约漂移。
        self._pin_account_positions()

    def _pin_account_positions(self):
        """读取 account_state 持仓，把有持仓品种强制钉到其开仓合约（未记录则回退 trade_config），
        覆盖 auto_main 的 OI 换月，避免账户总览现价/浮盈亏错用非持仓合约。"""
        try:
            st_path = os.path.join(HERE, "account_state.json")
            if not os.path.exists(st_path):
                return
            st = json.load(open(st_path, encoding="utf-8"))
            positions = st.get("positions", {})
            tcfg = {}
            try:
                tcfg = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8"))
            except Exception:
                pass
            specs = tcfg.get("contract_specs", {})
            pinned = []
            for sym, pos in positions.items():
                if not (pos and int(pos.get("lots", 0) or 0) > 0):
                    continue
                contract = (pos.get("contract")
                            or MAIN_OVERRIDE.get(sym.lower()) or MAIN_OVERRIDE.get(sym)
                            or specs.get(sym, {}).get("contract"))
                if not contract:
                    continue
                contract = normalize_contract_code(contract)
                # 若持仓合约发生变化，清除旧快照避免错价继续显示
                old_code = self.sym2code.get(sym)
                if old_code and old_code != contract and sym in self.last_snap:
                    del self.last_snap[sym]
                self._set_pin(sym, contract)
                self._auth[sym] = contract
                pinned.append(f"{sym}={contract}")
            if pinned:
                print(f"[minishare_live] 持仓合约钉死: {', '.join(pinned)}")
        except Exception as e:
            print(f"[minishare_live] 持仓合约钉死失败: {e}")

    def _set_pin(self, sym, code):
        """钉死 sym→code（权威映射），并清除该 sym 的旧反向映射，
        避免旧 code（如主连 CFM）在 poll 时重复回灌、覆盖正确合约价。"""
        code = normalize_contract_code(code)
        old = self.sym2code.get(sym)
        if old and old != code and self.code2sym.get(old) == sym:
            del self.code2sym[old]
        self.sym2code[sym] = code
        self.code2sym[code] = sym

    def _init_from_cache(self):
        """从缓存加载映射（避免每次启动都等首次 poll），再用 trade_config 的
        真实交割合约**强制覆盖**持仓品种映射（主连/缓存污染一律让位）。"""
        cached = _load_mapping_cache()
        from four_dim_strategy import SYMBOLS, FlowAggregator
        for sym, code in cached.items():
            if sym in SYMBOLS:
                self.sym2code[sym] = code
                self.code2sym[code] = sym
        # 权威覆盖：trade_config 指定了真实交割合约的品种，必须以合约价盯市，
        # 不能用缓存/主连映射（CF 曾错挂棉花主连，差 580 点；rb 错挂螺纹主连）。
        for sym, code in _authoritative_contracts().items():
            if sym in SYMBOLS:
                _old = self.sym2code.get(sym)
                if _old and _old != code:
                    print(f"[minishare_live][自检] {sym} 合约映射纠错: {_old} -> {code}（防止主连价污染盯市）")
                self._set_pin(sym, code)
                print(f"[minishare_live] 缓存覆盖·权威合约: {sym} -> {code}")
        missing = [s for s in SYMBOLS if s not in self.sym2code]
        if missing:
            self._mapped = False  # 缓存不全，首次 poll 时重新发现补齐
        elif self.sym2code:
            self._mapped = True
        for s in self.sym2code:
            self.bars.setdefault(s, [])
            self.last_bar_minute.setdefault(s, None)
            self.flow.setdefault(s, FlowAggregator(s))

    def _discover(self, df):
        """从 rt_fut_k 全量数据自动发现品种→代码映射（按中文名匹配）。"""
        import re as _re
        from four_dim_strategy import SYMBOLS

        # 第零步（权威）：trade_config 指定了真实交割合约的品种，先钉死到该合约，
        # 不用主连/中文名匹配（CF 曾错挂棉花主连，差 580 点；rb 错挂螺纹主连）。
        # 后续按名/按代码匹配见到已钉死的 sym 会 skip，互不冲突。
        for sym, ctr in _authoritative_contracts().items():
            if sym not in SYMBOLS:
                continue
            ctr_up = ctr.upper()
            ctr_digits = _re.sub(r"[^A-Z0-9]", "", ctr_up)
            hit = None
            for _, r in df.iterrows():
                code = str(r["ts_code"]).upper()
                if ctr_digits and ctr_digits in code:
                    hit = str(r["ts_code"]); break
            if hit:
                self._set_pin(sym, hit)
                print(f"[minishare_live] 权威合约钉死: {sym} -> {hit} (trade_config.contract={ctr})")

        # 第一步：建立 name→code 反向表
        name2code = {}
        for _, r in df.iterrows():
            code = str(r["ts_code"])
            name = str(r.get("name", ""))
            if not name:
                continue
            # 去掉 「主连」「连续」「连一」 等后缀，取基础名
            base = name
            for suffix in ("主连", "连续", "连一", "连二"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            if base and base not in name2code:
                name2code[base] = code

        # 第二步：用 SYMBOLS 名称匹配（具体交割合约同名"纯碱"无法区分，跳过，留待第四步）
        for sym, info in SYMBOLS.items():
            if sym in self.sym2code:
                continue  # 已有的不覆盖
            if sym in _CONTRACTS:
                continue  # 具体合约按代码匹配，不按名
            nm = info["name"]
            code = normalize_contract_code(name2code.get(nm))
            if code:
                self.sym2code[sym] = code
                self.code2sym[code] = sym

        # 第三步（已移除 _PINNED）：原逻辑会把 FG/SA/jd/lh/JM/J 回写覆盖为「主连」，
        # 与第零步权威交割合约冲突，删除后权威合约生效。

        # 第三步之b：具体交割合约按「合约代码」匹配（名称都叫"纯碱"，只能靠代码）
        import re as _re
        for sym, digits in _CONTRACTS.items():
            if sym in self.sym2code or sym not in SYMBOLS:
                continue
            hit = None
            for _, r in df.iterrows():
                code = str(r["ts_code"]).upper()
                if digits in _re.sub(r"[^A-Z0-9]", "", code):
                    hit = str(r["ts_code"]); break
            if hit:
                hit = normalize_contract_code(hit)
                self.sym2code[sym] = hit
                self.code2sym[hit] = sym
                print(f"[minishare_live] 合约映射: {sym} -> {hit}")

        # 第四步：为新发现的品种初始化 bars/flow
        from four_dim_strategy import FlowAggregator
        for s in self.sym2code:
            if s not in self.bars:
                self.bars[s] = []
                self.last_bar_minute[s] = None
                self.flow[s] = FlowAggregator(s)

        self._mapped = True
        _save_mapping_cache(dict(self.sym2code))
        print(f"[minishare_live] 品种映射: {len(self.sym2code)}/{len(SYMBOLS)} 已匹配")

    def _load_bars(self):
        try:
            if os.path.exists(BARS_CACHE):
                d = json.load(open(BARS_CACHE))
                for s in self.sym2code:
                    if s in d and d[s]:
                        self.bars[s] = d[s]
                        for b in self.bars[s]:
                            try:
                                b["date"] = pd.to_datetime(b["date"])
                            except Exception:
                                pass
                        if self.bars[s]:
                            self.last_bar_minute[s] = pd.to_datetime(d[s][-1]["date"])
        except Exception:
            pass

    def _save_bars(self):
        try:
            d = {}
            for s in self.sym2code:
                if s in self.bars:
                    d[s] = self.bars[s]
            json.dump(d, open(BARS_CACHE, "w"), default=str)
        except Exception:
            pass

    def available(self):
        return self.pro is not None

    def _code_matches_auth(self, sym, code):
        """安全阀：sym 有权威交割合约（如 rb→RB2609）时，仅当 code 命中该合约
        才允许写入其快照；否则（如螺纹主连 RBM 命中 rb）直接拒绝，防止主连价
        污染具体合约盯市（曾出现 RB2609 错挂 RBM=3016 的偏差）。"""
        auth = (getattr(self, "_auth", None) or {}).get(sym)
        if not auth:
            return True
        # code 已在外层 poll 归一化；此处再防一次 3 位/4 位格式差异
        code = normalize_contract_code(code)
        auth_digits = re.sub(r"[^A-Z0-9]", "", str(auth).upper())
        code_digits = re.sub(r"[^A-Z0-9]", "", str(code).upper())
        return bool(auth_digits) and auth_digits in code_digits

    def active_symbols(self):
        """返回已匹配的品种代码列表，供 runner 遍历。"""
        return list(self.sym2code.keys())

    # ── 动态主力/次主力合约解析（2026-08-14）──
    def _refresh_hot_contracts(self, df):
        """从 rt_fut_k 全量快照，按持仓量(OI)排名解析各品种当前主力/次主力可交割合约。
        结果写入模块级 HOT_CACHE（持久化），供 _authoritative_contracts / _apply_hot_contracts 使用。"""
        try:
            from four_dim_strategy import SYMBOLS
        except Exception:
            return
        _load_main_override()  # 重读最新 akshare 权威覆盖（采集器可能刚更新文件）
        # 1) 按商品前缀聚合可交割合约（排除主连/连续合成系列）
        prefix_contracts = {}  # prefix -> [(code, oi), ...]
        for _, r in df.iterrows():
            code = normalize_contract_code(str(r.get("ts_code", "")))
            if not _is_tradeable_contract(code):
                continue
            m = re.match(r"^([A-Za-z]+?)(\d{3,4})", re.sub(r"[^A-Za-z0-9]", "", code))
            if not m:
                continue
            prefix = m.group(1)
            try:
                oi = float(r.get("oi", r.get("hold", 0)) or 0)
            except Exception:
                oi = 0.0
            prefix_contracts.setdefault(prefix, []).append((code, oi))
        # 2) 持仓量降序排序
        for p in prefix_contracts:
            prefix_contracts[p].sort(key=lambda x: x[1], reverse=True)
        self._prefix_top2 = {p: [c for c, _ in grp[:2]] for p, grp in prefix_contracts.items()}
        # 3) 建立 prefix -> sym（用当前已映射合约的前缀反查）
        prefix2sym = {}
        for sym in list(self.sym2code.keys()):
            code = self.sym2code[sym]
            m = re.match(r"^([A-Za-z]+?)(\d{3,4})", re.sub(r"[^A-Za-z0-9]", "", str(code).upper()))
            if m and m.group(1) not in prefix2sym:
                prefix2sym[m.group(1)] = sym
        sym2prefix = {v: k for k, v in prefix2sym.items()}
        # 4) 写入 HOT_CACHE
        changed = False
        for sym in SYMBOLS:
            prefix = sym2prefix.get(sym)
            if not prefix:
                code = self.sym2code.get(sym)
                if code:
                    m = re.match(r"^([A-Za-z]+?)(\d{3,4})", re.sub(r"[^A-Za-z0-9]", "", str(code).upper()))
                    prefix = m.group(1) if m else None
            if not prefix:
                continue
            grp = prefix_contracts.get(prefix, [])
            if not grp:
                continue
            main = grp[0][0]
            secondary = grp[1][0] if len(grp) > 1 else main
            # akshare 权威覆盖优先：绕开 OI 滞后/卡月（如农产品主力已滚至 2611/2701 而 OI 仍排 2609）
            # 键大小写兼容：_load_main_override 恒存小写键，contract_specs 键为混合大小写（cu 小写/FG 大写）
            ov = MAIN_OVERRIDE.get(sym.lower()) or MAIN_OVERRIDE.get(sym)
            if ov:
                main = ov
            prev = HOT_CACHE.get(sym, {})
            forced = prev.get("forced", False)  # 保留外部强制锁定标记(refresh_main_contracts --apply)
            if forced:
                # 外部强制锁定：保留锁定的主力/次主力，不被 OI 排名覆盖（换月期真实主力已前进）
                HOT_CACHE[sym] = {"main": prev.get("main"), "secondary": prev.get("secondary"),
                                  "ts": time.time(), "forced": True, "source": prev.get("source")}
                changed = True
                continue
            if prev.get("main") != main or prev.get("secondary") != secondary:
                changed = True
            HOT_CACHE[sym] = {"main": main, "secondary": secondary, "ts": time.time(), "forced": forced}
        if changed:
            _save_hot_cache()
            print("[minishare_live] 主力合约解析: " + ", ".join(
                f"{s}={HOT_CACHE[s]['main']}" for s in HOT_CACHE if s in SYMBOLS))

    def _apply_hot_contracts(self):
        """将各品种 sym2code 钉到当前主力/次主力合约；仅在发生换月（当前合约跌出 OI 前二）时重钉，
        避免盘中 OI 抖动导致合约来回切换。同时同步 self._auth 安全阀，使新合约快照可被接受。
        有实际持仓的品种由 _pin_account_positions 钉死到开仓合约，不参与动态换月。"""
        try:
            from four_dim_strategy import SYMBOLS
        except Exception:
            return
        _load_main_override()  # 重读最新 akshare 权威覆盖（采集器可能刚更新文件）
        # 持仓品种不参与 auto_main 换月：账户总览盯市价必须与真实持仓合约一致
        held = set()
        try:
            st_path = os.path.join(HERE, "account_state.json")
            if os.path.exists(st_path):
                st = json.load(open(st_path, encoding="utf-8"))
                held = {s for s, p in st.get("positions", {}).items() if p and int(p.get("lots", 0) or 0) > 0}
        except Exception:
            pass
        for sym in SYMBOLS:
            if sym in held:
                continue
            mode = _contract_mode(sym)
            if mode == "fixed":
                continue
            hc = HOT_CACHE.get(sym)
            if not hc:
                continue
            # akshare 权威覆盖优先（绕开 OI 滞后/卡月，确保信号/盯市跑在真实主力上）
            cur = normalize_contract_code(self.sym2code.get(sym) or "")
            ov = MAIN_OVERRIDE.get(sym.lower()) or MAIN_OVERRIDE.get(sym)
            if ov:
                # 权威覆盖：直接钉到真实主力，跳过 OI 防抖（防抖仅用于无覆盖时的 OI 检测，
                # 否则旧合约残存高 OI 会卡住换月，导致策略跑在错误合约上产生无效信号）
                desired = normalize_contract_code(ov)
                if cur != desired:
                    self._set_pin(sym, desired)
                    self._auth[sym] = desired
                    print(f"[minishare_live] 合约权威覆盖钉合: {sym} {cur} -> {desired}")
                continue
            desired = normalize_contract_code(hc["main"] if mode == "auto_main" else (hc.get("secondary") or hc["main"]))
            # 强制锁定分支（refresh_main_contracts --apply 写入 forced）：无条件尊重，跳过 OI 防抖
            if hc.get("forced"):
                if cur != desired:
                    self._set_pin(sym, desired)
                    self._auth[sym] = desired
                    print(f"[minishare_live] 合约强制锁定(forced): {sym} {cur} -> {desired}")
                continue
            if cur == desired:
                continue
            # 换月期豁免：旧主力已到/过交割月，忽略 OI 前二防抖直接切换（修复 2026-08-17 换月滞后）
            cur_ym = _contract_ym(cur); now_ym = _now_ym()
            if cur_ym and cur_ym <= now_ym:
                self._set_pin(sym, desired)
                self._auth[sym] = desired
                print(f"[minishare_live] 换月强制切换: {sym} {cur} -> {desired}")
                continue
            # 防抖：当前合约仍在 OI 前二则保持，避免 flicker
            prefix = None
            if cur:
                m = re.match(r"^([A-Za-z]+?)(\d{3,4})", re.sub(r"[^A-Za-z0-9]", "", str(cur).upper()))
                if m:
                    prefix = m.group(1)
            top2 = self._prefix_top2.get(prefix, []) if prefix else []
            if cur and cur in top2:
                continue  # 仍在主力阵营，保持
            old = cur
            self._set_pin(sym, desired)
            self._auth[sym] = desired
            print(f"[minishare_live] 合约换月: {sym} {old} -> {desired}（{mode}）")

    def contract_of(self, sym):
        """返回该品种当前生效的可交割合约代码（如 'RB2610'），供前端展示合约名。"""
        return normalize_contract_code(self.sym2code.get(sym) or "")

    def poll(self):
        """拉一次 rt_fut_k，更新快照/5m 桶/C_flow。返回 {sym: snap}。"""
        if self.pro is None:
            return {}
        try:
            df = self.pro.query("rt_fut_k", ts_code="*")
        except Exception as e:
            print(f"[minishare_live] rt_fut_k 失败: {e}")
            return {}
        if df is None or getattr(df, "empty", True):
            return {}

        # 首次拉取：自动发现所有品种映射
        if not self._mapped:
            self._discover(df)

        # 动态主力/次主力合约解析（2026-08-14 根治写死月份导致盯市价过时）
        self._refresh_hot_contracts(df)
        self._apply_hot_contracts()

        now = datetime.now()
        bucket = now.replace(second=0, microsecond=0)
        bucket = bucket - pd.Timedelta(minutes=bucket.minute % 5)
        for _, r in df.iterrows():
            # P3（2026-08-14）：数据源常返回 3 位缩略合约（FG609），而映射表/权威合约
            # 已规范为 4 位（FG2609）。必须先归一化再查映射与安全阀，否则玻璃/纯碱/苹果
            # 等郑商所品种会被安全阀误杀，导致账户总览现价与浮盈亏无法自动更新。
            code = normalize_contract_code(str(r["ts_code"]))
            sym = self.code2sym.get(code)
            if sym is None:
                continue
            # 安全阀：带权威交割合约的品种，禁止被非该合约（如螺纹主连 RBM）的快照覆盖，
            # 否则会出现 RB2609 错挂 RBM(3016) 这类盯市/展示偏差。
            if not self._code_matches_auth(sym, code):
                continue
            close = float(r["close"]); oi = float(r.get("oi", r.get("hold", 0)) or 0)
            vol = float(r.get("vol", r.get("volume", 0)) or 0)
            ts = str(r.get("date", ""))
            # P2（2026-08-14）：把昨结算/pre_close 也存进快照，供「涨跌停锁死头寸」精确封板判定
            _pc = r.get("pre_close", r.get("settlement"))
            try:
                _pc = float(_pc) if _pc is not None else 0.0
            except Exception:
                _pc = 0.0
            self.last_snap[sym] = {"close": close, "open": float(r.get("open", close)),
                                   "high": float(r.get("high", close)),
                                   "low": float(r.get("low", close)),
                                   "vol": vol, "oi": oi, "ts": ts,
                                   "pre_close": _pc,
                                   "name": str(r.get("name", sym))}
            # 5m 桶聚合
            lbm = self.last_bar_minute.get(sym)
            if lbm != bucket:
                self.last_bar_minute[sym] = bucket
                self.bars.setdefault(sym, [])
                self.bars[sym].append({"date": bucket, "open": close, "high": close,
                                       "low": close, "close": close, "volume": 0, "oi": oi})
            else:
                if sym in self.bars and self.bars[sym]:
                    b = self.bars[sym][-1]
                    b["high"] = max(b["high"], close); b["low"] = min(b["low"], close)
                    b["close"] = close; b["oi"] = oi
            # C_flow 差分
            if sym in self.flow:
                self.flow[sym].push_minishare(close, oi, vol, time.time())
            # 只保留近 200 根 5m
            if sym in self.bars and len(self.bars[sym]) > 200:
                self.bars[sym].pop(0)
        self._save_bars()
        return self.last_snap

    def price(self, sym):
        s = self.last_snap.get(sym)
        return s["close"] if s else None

    def get_5m(self, sym, n_bars=60):
        """返回近 n_bars 根 5m 合成 K 线（DatetimeIndex OHLCV DataFrame）。
        品种未映射或无数据返回 None。"""
        rows = self.bars.get(sym, [])[-n_bars:]
        if not rows:
            return None
        df = pd.DataFrame(rows).set_index("date")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df

    def c_flow(self, sym):
        """返回实时 C_flow ∈ [-100,100]；无解算数据（未跟踪 / 样本<3）时返回 None，
        让 pipeline 回退到 T+1 龙虎榜 score_C（优雅降级）。
        # P-E 修正（2026-08-14）：原实现无解算时返回 0.0，而 pipeline 的 c_override 用
        `is not None` 判定，0.0 会覆盖掉龙虎榜信号 → C 维度在冷启动/断流时被静默置 0。
        改为返回 None，让其回退到 score_C（龙虎榜），修掉静默清零缺陷。"""
        if sym in self.flow and self.flow[sym] is not None:
            agg = self.flow[sym]
            if len(agg.deltas) >= 3:
                return agg.c_flow_score()
        return None


# 模块级单例（盘中常驻，避免重复 set_token）
_feed = None

def feed():
    global _feed
    if _feed is None:
        _feed = MinishareLiveFeed()
    return _feed


def build_min5_live(sym, n_bars=120):
    """实时路径的 5m 数据源：优先 minishare 合成 5m；不可用返回 None（回退 sina）。"""
    f = feed()
    if not f.available():
        return None
    if sym not in f.sym2code:
        return None
    if not f.last_snap:
        f.poll()
    if not f.bars.get(sym):
        return None
    return f.get_5m(sym, n_bars)


if __name__ == "__main__":
    f = feed()
    print("available:", f.available())
    if not f.available():
        print("minishare 不可用（无 token 或 import 失败）")
    else:
        snap = f.poll()
        print(f"快照时间: {datetime.now():%H:%M:%S}  采样 {len(snap)}/{len(f.sym2code)} 品种")
        for s in sorted(f.sym2code):
            p = f.price(s)
            bars_n = len(f.bars.get(s, []))
            cf = f.c_flow(s)
            print(f"  {s:3} {f.sym2code[s]:4} 价={p}  5m={bars_n}根  C_flow={cf}")
