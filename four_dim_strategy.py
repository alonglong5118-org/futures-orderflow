"""四维策略(4D)核心引擎 v1.0
=================================================================
把「管住手下单前四维自检卡」自动化为 信号发生器 + 风控闸门。
流水线：F(背景偏置) → T(触发/方向) → C(确认/强度) → 风控硬闸门。

数据源（2026-08-11 实测：minishare 当前 token 仅开放 rt_fut_k 实时快照，历史/基本面端点权限不足）：
  - 历史日线：/管住手_实盘工作区_Ken/量化回测/_XX0_daily.csv（54 品种主连，已现成，非网络源）
  - 实盘/纸面追踪另用 load_daily_refreshed() 追加 akshare 近期主连日线（minishare 无 fut_daily
    权限，按约定走免费源兜底；仅 live/papertrack 用，walk_forward_backtest 不用以免前视）
  - 5m 历史（回测）：本地缓存 _XX0_min5.csv（JD/RM 已有；缺失→sina 兜底落盘，仅近~1023根）
  - 5m 实时（盘中）：minishare_live.build_min5_live → rt_fut_k 60s 快照聚合 5m 桶（**已不用 sina**）
  - 基本面 F：fundamentals.json（fundamental_feed.py 盘前刷新；akshare 基差/库存，minishare 无 fut_basis 权限→暂留 akshare）
  - 资金面 C 实时：minishare_live.FlowAggregator（rt_fut_k 差分）+ da龘 tick 订单流，已全走 minishare
  - 资金面 C 历史：cpos_cache.json（龙虎榜历史；回测缺失→中性 0）
  - 技术面 T：复用 da龘 strategy_layer 的 8 策略合成 + regime 加权

结论：实时路径（价/5m/C_flow）已全部 minishare 化；仅「历史回测 5m」与「F 基本面」仍依赖
免费源（sina/akshare），因为 minishare 该 token 无对应历史/基本面端点权限。

详见 四维策略_规格草案.md (v1.1)。
"""

from __future__ import annotations

import bisect
import json
import math
import os
import sys
import time
from datetime import datetime

import numpy as np

# ── v5.1 集成常量 ──
SIGNAL_QUALITY_MIN_SCORE = 60
BREAKOUT_BODY_MIN_PCT = 0.5
BREAKOUT_VOLUME_MULT = 1.5
VOLUME_MA_PERIOD = 20
MULTI_TIMEFRAME_ENABLED = True
HIGHER_TF_MA_FAST = 20
HIGHER_TF_MA_SLOW = 55
COUNTER_TREND_POS_SCALE = 0.5
COUNTER_TREND_RR_BOOST = 1.3
BREAKEVEN_TRIGGER_R = 1.0
TRAILING_STOP_ATR_MULT = 2.0
MIN_RR_RATIO = 2.0
SIGMA_STOP_MULT = 3.0
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fundamental_feed as ff  # 基本面 F 数据源（基差/库存）
from strategy_layer import (
    MEAN_STRATS,
    STRATS,
    TREND_STRATS,
    _atr_array,
    _rolling_max_array,
    _rolling_min_array,
    _rolling_std_array,
    _rsi_array,
    _seasonal_month_stats,
    _sma_last,
    classify_regime,
    classify_regime_array,
    precompute_signals,
)
from strategy_layer import atr as strat_atr

# P1-16: 实时风控状态注入 —— 由 four_dim_live_runner 在每轮评估前设置
# 策略层在 live 模式下读取此状态，前置否决被锁定/熔断的信号生成
# 回测模式下 _LIVE_RISK_STATE 为 None，不影响回测逻辑
_LIVE_RISK_STATE = None  # None=回测/未设置；dict=live 实时状态


def set_live_risk_state(state_dict):
    """P1-16: runner 设置实时风控状态（每轮 update_risk_state 后调用）。"""
    global _LIVE_RISK_STATE
    _LIVE_RISK_STATE = state_dict if isinstance(state_dict, dict) else None


def get_live_risk_state():
    """P1-16: 获取当前实时风控状态（供 pipeline/risk_gate 读取）。"""
    return _LIVE_RISK_STATE


def _is_risk_locked(risk_state=None):
    """P1-16: 检查风控是否锁定/熔断。

    返回 (locked, reason) tuple。locked=True 表示禁止开仓。
    """
    rs = risk_state if risk_state is not None else _LIVE_RISK_STATE
    if rs is None:
        return False, ""
    state = rs.get("state", "")
    if state in ("HALTED", "LOCKED"):
        reason = rs.get("lock_reason", "") or rs.get("reason", "") or f"状态={state}"
        return True, reason
    scale = rs.get("scale", 1.0)
    if scale is not None and float(scale) <= 0.0:
        return True, f"风控 scale=0 (state={state})"
    return False, ""


# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
BACKTEST_DIR = "/Users/ken/WorkBuddy/管住手/2026-07-28-12-52-27/管住手_实盘工作区_Ken/量化回测"
FUNDAMENTALS_JSON = os.path.join(HERE, "fundamentals.json")
CPOS_JSON = os.path.join(HERE, "cpos_cache.json")
# score_C 缓存（walk-forward 场景下每根 K 线都调用，缓存可省 40ms+/500bars）
_CPOS_CACHE = {"mtime": 0.0, "data": None}
DATA_5M_DIR = os.path.join(HERE, "data_5m")  # 本地 5m 缓存（sina 拉取落盘）
os.makedirs(DATA_5M_DIR, exist_ok=True)

SYMBOLS = {
    # ── 上期所 SHFE ──
    "cu": {"name": "沪铜", "group": "有色", "exchange": "SHFE"},
    "al": {"name": "沪铝", "group": "有色", "exchange": "SHFE"},
    "zn": {"name": "沪锌", "group": "有色", "exchange": "SHFE"},
    "ni": {"name": "沪镍", "group": "有色", "exchange": "SHFE"},
    "sn": {"name": "沪锡", "group": "有色", "exchange": "SHFE"},
    "ao": {"name": "氧化铝", "group": "有色", "exchange": "SHFE"},
    "au": {"name": "沪金", "group": "贵金属", "exchange": "SHFE"},
    "ag": {"name": "沪银", "group": "贵金属", "exchange": "SHFE"},
    "rb": {"name": "螺纹钢", "group": "黑系", "exchange": "SHFE"},
    "hc": {"name": "热卷", "group": "黑系", "exchange": "SHFE"},
    "ss": {"name": "不锈钢", "group": "黑系", "exchange": "SHFE"},
    "bu": {"name": "沥青", "group": "能源", "exchange": "SHFE"},
    "fu": {"name": "燃油", "group": "能源", "exchange": "SHFE"},
    "ru": {"name": "橡胶", "group": "化工", "exchange": "SHFE"},
    "sp": {"name": "纸浆", "group": "化工", "exchange": "SHFE"},
    # ── 上期能源 INE ──
    "sc": {"name": "原油", "group": "能源", "exchange": "INE"},
    "ec": {"name": "欧线", "group": "航运", "exchange": "INE"},
    # ── 大商所 DCE ──
    "i": {"name": "铁矿石", "group": "黑系", "exchange": "DCE"},
    "J": {"name": "焦炭", "group": "黑系", "exchange": "DCE"},
    "JM": {"name": "焦煤", "group": "黑系", "exchange": "DCE"},
    "eb": {"name": "苯乙烯", "group": "化工", "exchange": "DCE"},
    "eg": {"name": "乙二醇", "group": "化工", "exchange": "DCE"},
    "l": {"name": "塑料", "group": "化工", "exchange": "DCE"},
    "pp": {"name": "聚丙烯", "group": "化工", "exchange": "DCE"},
    "v": {"name": "PVC", "group": "化工", "exchange": "DCE"},
    "pg": {"name": "液化气", "group": "能源", "exchange": "DCE"},
    "m": {"name": "豆粕", "group": "农产品", "exchange": "DCE"},
    "y": {"name": "豆油", "group": "农产品", "exchange": "DCE"},
    "a": {"name": "豆一", "group": "农产品", "exchange": "DCE"},
    "b": {"name": "豆二", "group": "农产品", "exchange": "DCE"},
    "p": {"name": "棕榈油", "group": "农产品", "exchange": "DCE"},
    "c": {"name": "玉米", "group": "农产品", "exchange": "DCE"},
    "cs": {"name": "淀粉", "group": "农产品", "exchange": "DCE"},
    "jd": {"name": "鸡蛋", "group": "农产品", "exchange": "DCE"},
    "lh": {"name": "生猪", "group": "农产品", "exchange": "DCE"},
    "rr": {"name": "粳米", "group": "农产品", "exchange": "DCE"},
    # ── 郑商所 CZCE ──
    "FG": {"name": "玻璃", "group": "化工", "exchange": "CZCE"},
    "SA": {"name": "纯碱", "group": "化工", "exchange": "CZCE"},
    # ── 纯碱具体交割合约（独立实时盘中卡；09/01 各接真实逐合约龙虎榜 SA609/SA701）──
    "SA01": {"name": "纯碱2701", "group": "化工", "exchange": "CZCE"},
    "MA": {"name": "甲醇", "group": "化工", "exchange": "CZCE"},
    "TA": {"name": "PTA", "group": "化工", "exchange": "CZCE"},
    "PF": {"name": "短纤", "group": "化工", "exchange": "CZCE"},
    "PX": {"name": "对二甲苯", "group": "化工", "exchange": "CZCE"},
    "SH": {"name": "烧碱", "group": "化工", "exchange": "CZCE"},
    "UR": {"name": "尿素", "group": "化工", "exchange": "CZCE"},
    "PR": {"name": "瓶片", "group": "化工", "exchange": "CZCE"},
    "SR": {"name": "白糖", "group": "农产品", "exchange": "CZCE"},
    "CF": {"name": "棉花", "group": "农产品", "exchange": "CZCE"},
    "RM": {"name": "菜粕", "group": "农产品", "exchange": "CZCE"},
    "OI": {"name": "菜油", "group": "农产品", "exchange": "CZCE"},
    "PK": {"name": "花生", "group": "农产品", "exchange": "CZCE"},
    "AP": {"name": "苹果", "group": "农产品", "exchange": "CZCE"},
    # ── 广期所 GFEX ──
    "si": {"name": "工业硅", "group": "有色", "exchange": "GFEX"},
    "lc": {"name": "碳酸锂", "group": "有色", "exchange": "GFEX"},
}

# —— G4 日/夜盘统一真值源 ——
# 不逐个改 55 个字面量，用循环注入 night 字段：有夜盘=True，无夜盘=False。
# 已知：鸡蛋(jd)/生猪(lh) 等无夜盘；玻璃(FG)/纯碱(SA)/焦煤(JM)/焦炭(J) 有夜盘 21:00–23:00。
NO_NIGHT_DEFAULT = {
    "jd",
    "lh",
    "AP",
    "CJ",
    "PK",
    "RS",
    "PM",
    "WH",
    "JR",
    "LR",
    "CS",
    "rr",
    "lc",
    "si",
    "UR",
    "RM",
    "OI",
    "c",
}  # 2026-08-19 22:45 修复：广期所碳酸锂(lc)/工业硅(si) 无夜盘，此前漏标导致夜盘用冻结数据误发信号
# 2026-08-20 修正：ss(不锈钢)/sp(纸浆) 上期所有夜盘 21:00–23:00，误在集合内导致夜盘漏推，已移除
for _s, _m in SYMBOLS.items():
    _m.setdefault("night", _s not in NO_NIGHT_DEFAULT)

# 校准判死刑的品种（walk-forward OOS 负期望，禁止实盘/纸面出信号）：2026-08-11 全市场校准
# au −0.302 · ag −0.059 · ss −0.100 · bu −0.008 · i −0.016 · eg −0.180
# m −0.029 · a −0.083 · b −0.099 · rr −0.261 · RM −0.073
# ── 2026-08-13 追加 JM/hc：近期 walk-forward 全阈值负（JM −0.97/胜0% · hc −0.62/胜18%），
#    模型+实盘双确认真死。加入硬禁，但**保留品种定义不删除**，走 AUTO_RECOVER_SYMBOLS 自适应恢复
#    （见 recovery_check：当近期 walk-forward 转正时自动解除屏蔽，恢复交易）。
# 2026-08-17 全市场 OOS 日线版应用（用户授权免签核）：解禁 JM/ag/bu/ss（整改后日线转正+Δ>+0.02+样本≥30+5m不退化+回撤改善）；
# 加禁 MA/PR（整改后日线 on 负且较 v12 退化，PR 另 5m 双负）。5m 全量跑完由 fmreport 守护出最终全量版（仅更全地收口）。
DISABLED_SYMBOLS = {"au", "i", "eg", "m", "a", "b", "rr", "RM", "hc", "MA", "PR"}

# 自适应恢复白名单：仅这些被禁品种参与周期性恢复判定（其余硬禁永不自动恢复）。
# JM/hc 因真死被禁，但保留定义→当市场结构变化、walk-forward 重新转正时自动解禁。
AUTO_RECOVER_SYMBOLS = {"hc"}

# akshare sina 主力连续代码映射（symbol → sina code）
_AKSHARE_MAP = {s: s.upper() + "0" for s in SYMBOLS}
# 特殊调整
_AKSHARE_MAP.update(
    {
        "jd": "JD0",
        "lh": "LH0",  # 原名即大写首字母
    }
)

# ── 具体交割合约映射（四维面板按合约维度独立出信号）──
# sym_key -> akshare/sina 具体合约代码（日线/5m 用）
_CONTRACT_AKSHARE = {"SA01": "SA2701"}
# 合约代码(如 SA2701) -> 四维合约 sym(如 SA01)，供持仓/账户按合约映射（逐合约卡）
CONTRACT_SYM_BY_CODE = {v: k for k, v in _CONTRACT_AKSHARE.items()}
# sym_key -> 所属品种 key（F 基本面 / 龙虎榜 C_pos 按品种级取，避免逐合约重复建库）
VARIETY_OF = {"SA01": "SA"}
# 逐合约龙虎榜缓存键：akshare 实际按合约返回 SA609/SA701（非品种级 SA），远月需独立取数
_CONTRACT_CPOS_KEY = {"SA01": "SA701"}


def variety_of(sym):
    """具体合约 -> 所属品种 key（F/龙虎榜复用品种级，避免逐合约另建基本面/持仓库）。"""
    return VARIETY_OF.get(sym, sym)


# ----------------------------------------------------------------------------
# 默认配置（§2.2 + §1.6 + §1.7）
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "account": {
        "equity": 100000,
        "risk_pct": 1.5,
        "margin_cap_pct": 30,
        "max_lots": 5,
        "per_symbol_lots": {},
        "portfolio_margin_cap_pct": 60,
        "max_total_lots": 15,
        "use_realtime_margin": True,
    },
    # 组合管理（P4，2026-08-29）：按权重分配风险预算 + 板块/相关性约束
    # enabled=False 时完全不影响原逻辑（risk_gate 中 pf_mult=1.0）。
    # 开启后：
    #   - 高权重品种仓位↑（最高 max_weight_mult 倍）
    #   - 低权重品种仓位↓（最低 min_weight_mult 倍）
    #   - 配合 portfolio_manager.py 做板块集中度 + 相关性监控 + 再平衡
    "portfolio": {
        "enabled": False,  # True=启用组合权重影响仓位；False=关闭（安全默认）
        "mode": "kelly",  # "equal" | "kelly" | "manual"
        "max_sector_weight": 0.35,  # 单板块最大权重 35%
        "high_corr_threshold": 0.7,  # 高相关阈值
        "rebalance_threshold": 0.05,  # 偏离 5% 触发再平衡
        "max_weight_mult": 2.0,  # 单品种最大风险倍率（相对于等权）
        "min_weight_mult": 0.3,  # 单品种最小风险倍率
        "active_symbols": [],  # 活跃品种列表（空=全部 per_symbol_risk 品种）
        "weights": {},  # 手动指定权重（mode="manual"时用）
        "corr_matrix": {},  # 相关性矩阵（用于监控，不影响仓位计算）
    },
    # 出场粒度（回测结论 2026-08-16：全市场5m出场 94%改善，默认开启）
    # live runner 已用 5m 派生 ATR + 实盘逐笔(stop/t2/尾仓)出场，等价于 5m 出场；
    # 此旗标固化"默认 on"口径，walk_forward_backtest_5m_exit 与之同源。
    "use_5m_exit": True,
    "risk_gate": {
        "stop_atr_mult": 1.5,
        "rr_ratio": 2.0,
        "limit_proximity": 0.9,
        "consec_loss_lock": 3,
        "slip_pts": 1,
        "kelly_slope": 2.0,  # #4 fractional-Kelly：edge→仓位缩放斜率（越大越激进）
        "kelly_min": 0.6,
        "kelly_max": 1.2,  # 缩放区间（低edge收缩/高edge放大，封顶1.2x防过押；P1-2整改：原1.6x在弱/中置信品种上过度自信→反向加杠杆）
    },
    # 逐品种止损/止盈覆盖（2026-08-13 联合校准，见 four_dim_calibrate.sweep_stop_rr）
    # 缺省用 risk_gate 全局值；命中此处则覆盖 stop_atr_mult / rr_ratio。
    # 同时作用于 walk-forward 回测与 live 风控（risk_gate/exist_plan 一致口径）。
    "per_symbol_risk": {
        "AP": {"stop_atr_mult": 1.0, "rr_ratio": 2.0},
        "CF": {"stop_atr_mult": 1.0, "rr_ratio": 3.0},
        "MA": {"stop_atr_mult": 1.5, "rr_ratio": 3.0},
        "OI": {"stop_atr_mult": 1.5, "rr_ratio": 1.5},
        "PF": {"stop_atr_mult": 1.0, "rr_ratio": 3.0},
        "SH": {"stop_atr_mult": 1.0, "rr_ratio": 3.0},
        "TA": {"stop_atr_mult": 1.5, "rr_ratio": 3.0},
        "al": {"stop_atr_mult": 1.5, "rr_ratio": 3.0},
        "ao": {"stop_atr_mult": 1.5, "rr_ratio": 2.5},  # P1: rr 1.5→2.5 (OOS+0.808 胜率100%)
        "c": {"stop_atr_mult": 2.0, "rr_ratio": 2.5},  # P1: rr 1.5→2.5 (OOS+0.223 胜率60%)
        "eb": {"stop_atr_mult": 1.5, "rr_ratio": 3.0},
        "fu": {"stop_atr_mult": 1.0, "rr_ratio": 2.5},
        "jd": {"stop_atr_mult": 1.5, "rr_ratio": 1.5},
        "lc": {"stop_atr_mult": 1.0, "rr_ratio": 3.0},
        "ni": {"stop_atr_mult": 1.5, "rr_ratio": 2.5},  # P1: rr 1.5→2.5 (OOS+0.647 胜率60%)
        "p": {"stop_atr_mult": 1.0, "rr_ratio": 3.0},
        "pp": {"stop_atr_mult": 2.0, "rr_ratio": 2.0},
        "sp": {"stop_atr_mult": 1.5, "rr_ratio": 2.5},
        "v": {"stop_atr_mult": 2.0, "rr_ratio": 1.5},
        "y": {"stop_atr_mult": 1.5, "rr_ratio": 1.5},
        "zn": {"stop_atr_mult": 1.0, "rr_ratio": 2.0},
        # P1 新增：OOS 验证 rr 提升稳健的品种
        "rb": {"stop_atr_mult": 1.5, "rr_ratio": 2.5, "note": "P1: rr=2.5 (OOS+0.492 胜率60%)"},
        "ru": {"stop_atr_mult": 1.5, "rr_ratio": 3.0, "note": "P1: rr=3.0 (OOS+0.304 胜率80%)"},
        "ss": {"stop_atr_mult": 1.5, "rr_ratio": 2.5, "note": "P1: rr=2.5 (OOS+0.199 胜率80%)"},
        "hc": {"stop_atr_mult": 1.5, "rr_ratio": 2.5, "note": "P1: rr=2.5 (OOS+0.179 胜率80%)"},
        # 低胜率品种专项（回测结论 2026-08-16）：单笔保证金占比收紧至 18%，
        # 与 risk_state_machine.PER_SYMBOL_RISK 同步（账户级状态机 + 信号级手数双重约束）。
        "JM": {"margin_cap_pct": 18, "note": "焦煤低胜率(27%)：单笔占比≤18%"},
        "J": {"margin_cap_pct": 18, "note": "焦炭低胜率(34%)：单笔占比≤18%"},
    },
    # 逐品种 regime 风控系数覆盖（P2，2026-08-29）
    # 解决弱品种在特定 regime 下大亏问题：对差表现 regime 调整 T 阈值和止损系数。
    # 覆盖逻辑：effective_regime_coef(symbol, cfg) 逐 regime 逐键合并，未覆盖项沿用全局 regime_coef。
    # 所有配置均经 walk-forward OOS 5 折验证，胜率 ≥ 60% 才上线。
    "per_symbol_regime_coef": {
        # P2 第一批：OOS 稳健通过（胜率≥60%）
        "RM": {
            # 菜粕：波动市大亏(-0.498) + 趋势市小亏(-0.231)
            # OOS: -0.635 → -0.359 (+0.277), 100% 胜率
            "波动": {"T": 0.80, "stop": 0.85, "note": "降T阈值+收紧止损，波动市减亏"},
            "趋势": {"T": 0.80, "stop": 1.30, "note": "降T阈值+放宽止损，趋势市提升胜率"},
        },
        "rr": {
            # 粳米：趋势市大亏(-0.920) + 震荡市大亏(-1.148)
            # OOS: -0.534 → -0.293 (+0.240), 75% 胜率
            "趋势": {"T": 0.80, "stop": 1.07, "note": "降T阈值，趋势市减少假信号"},
            "过渡": {"T": 1.30, "stop": 1.15, "note": "提T阈值+放宽止损，过渡市过滤噪音"},
        },
        "MA": {
            # 甲醇：波动市大亏(-0.350) + 过渡市小亏(-0.029)
            # OOS: -0.364 → -0.161 (+0.203), 60% 胜率
            "趋势": {"T": 0.80, "stop": 0.85, "note": "降T阈值+收紧止损，趋势市增效"},
            "过渡": {"T": 0.80, "stop": 1.00, "note": "降T阈值，过渡市增加有效信号"},
        },
        "b": {
            # 豆二：趋势市小亏(-0.105) + 波动市小亏(-0.203)
            # OOS: -0.385 → -0.300 (+0.085), 60% 胜率
            "趋势": {"T": 1.00, "stop": 1.30, "note": "放宽止损，趋势市避免震荡出局"},
        },
        # ========== 黑系专项（2026-08-29 走步法 OOS 验证）==========
        # 诊断：黑系胜率仅35%，波动市/过渡市是主要亏损来源
        # 策略：提高波动市开仓门槛（T×1.8）+ 收紧止损（stop×0.7），
        #       同时适度放宽趋势/震荡市止损（stop×1.2），让盈利单奔跑
        # 验证：5折走步法OOS，板块平均 +0.157 → +0.237（+51.5%），3/3 品种全正
        "i": {
            # 铁矿石：波动市(-0.144) + 过渡市(-0.267) 双亏
            # OOS: +0.036 → +0.110（+205%）
            "波动": {"T": 1.8, "stop": 0.7, "note": "黑系专项：提T门槛+收紧止损，回避波动市假突破"},
            "趋势": {"stop": 1.2, "note": "黑系专项：放宽止损，趋势市拿住大行情"},
            "震荡": {"stop": 1.2, "note": "黑系专项：放宽止损，震荡市增加容错"},
        },
        "rb": {
            # 螺纹钢：表现最好的黑系品种，锦上添花
            # OOS: +0.330 → +0.520（+57.6%）
            "波动": {"T": 1.8, "stop": 0.7, "note": "黑系专项：提T门槛+收紧止损，回避波动市假突破"},
            "趋势": {"stop": 1.2, "note": "黑系专项：放宽止损，趋势市拿住大行情"},
            "震荡": {"stop": 1.2, "note": "黑系专项：放宽止损，震荡市增加容错"},
        },
        "hc": {
            # 热卷：旧版为过渡市降T，新版改为黑系统一的波动市回避策略
            # 注：OOS 微降（+0.104 → +0.082）但仍正收益，换取板块整体稳健性
            # OOS: +0.104 → +0.082（-21%，仍正）
            "波动": {"T": 1.8, "stop": 0.7, "note": "黑系专项：提T门槛+收紧止损，回避波动市假突破"},
            "趋势": {"stop": 1.2, "note": "黑系专项：放宽止损，趋势市拿住大行情"},
            "震荡": {"stop": 1.2, "note": "黑系专项：放宽止损，震荡市增加容错"},
        },
    },
    # 合约参数（§2.1 占位；fee=单边每手元近似，回测扣费用）
    "contract_specs": {
        # 上期所 SHFE
        "cu": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 36.0},
        "al": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 6.0},
        "zn": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 6.0},
        "ni": {"multiplier": 1, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 3.0},
        "sn": {"multiplier": 1, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 3.0},
        "ao": {"multiplier": 20, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 6.0},
        "au": {"multiplier": 1000, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 10.0},
        "ag": {"multiplier": 15, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 7.5},
        "rb": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 4.0},
        "hc": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 4.0},
        "ss": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 8.0},
        "bu": {"multiplier": 10, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 4.0},
        "fu": {"multiplier": 10, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 3.0},
        "ru": {"multiplier": 10, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 6.0},
        "sp": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 4.0},
        # 上期能源 INE
        "sc": {"multiplier": 1000, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 20.0},
        "ec": {"multiplier": 50, "margin_rate": 0.18, "limit_pct": 0.10, "fee": 30.0},
        # 大商所 DCE
        "i": {"multiplier": 100, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 8.0},
        "J": {"multiplier": 100, "margin_rate": 0.13, "limit_pct": 0.08, "fee": 28.45},
        "JM": {"multiplier": 60, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 8.07},
        "eb": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 3.0},
        "eg": {"multiplier": 10, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 4.0},
        "l": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.0},
        "pp": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.0},
        "v": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.0},
        "pg": {"multiplier": 20, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 6.0},
        "m": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.5},
        "y": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.5},
        "a": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.5},
        "b": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.5},
        "p": {"multiplier": 10, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 2.5},
        "c": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 1.5},
        "cs": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 1.5},
        "jd": {"multiplier": 10, "margin_rate": 0.07, "limit_pct": 0.08, "fee": 5.0},
        "lh": {"multiplier": 16, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 30.0},
        "rr": {"multiplier": 10, "margin_rate": 0.08, "limit_pct": 0.05, "fee": 1.5},
        # 郑商所 CZCE
        "FG": {"multiplier": 20, "margin_rate": 0.13, "limit_pct": 0.04, "fee": 2.3},
        "SA": {"multiplier": 20, "margin_rate": 0.09, "limit_pct": 0.04, "fee": 4.0},
        "SA01": {"multiplier": 20, "margin_rate": 0.09, "limit_pct": 0.04, "fee": 4.0},
        "MA": {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.0},
        "TA": {"multiplier": 5, "margin_rate": 0.09, "limit_pct": 0.05, "fee": 3.0},
        "PF": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 2.0},
        "PX": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 3.0},
        "SH": {"multiplier": 30, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 6.0},
        "UR": {"multiplier": 20, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 4.0},
        "PR": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 2.0},
        "SR": {"multiplier": 10, "margin_rate": 0.09, "limit_pct": 0.06, "fee": 3.0},
        "CF": {"multiplier": 5, "margin_rate": 0.09, "limit_pct": 0.06, "fee": 4.3},
        "RM": {"multiplier": 10, "margin_rate": 0.09, "limit_pct": 0.06, "fee": 2.5},
        "OI": {"multiplier": 10, "margin_rate": 0.09, "limit_pct": 0.06, "fee": 2.0},
        "PK": {"multiplier": 5, "margin_rate": 0.10, "limit_pct": 0.06, "fee": 2.0},
        "AP": {"multiplier": 10, "margin_rate": 0.12, "limit_pct": 0.08, "fee": 5.0},
        # 广期所 GFEX
        "si": {"multiplier": 5, "margin_rate": 0.12, "limit_pct": 0.06, "fee": 4.0},
        "lc": {"multiplier": 1, "margin_rate": 0.15, "limit_pct": 0.08, "fee": 3.0},
    },
    # 阈值（§1.6 初值；2026-08-11 经嵌套 walk-forward OOS 校准，见 four_dim_calibrate.py）
    # 实测：8 策略日线合成的 T_D 实际 max 仅 ≈50（p80≈25, p90≈33），原稿 40/45/55 基于
    # "T_D 可达100" 的错误假设，实际几乎不可达（|T_D|>=45 仅占 0.2%）。初值下调到
    # 黑系/化工 22、农产品 25（对应 |T_D| p80 附近，仍属"少而精"，约 25% 时间触发）。
    # OOS 校准后为按品种覆盖（见 thresholds_by_symbol）。
    "thresholds": {
        "黑系": {"T_thresh": 22, "T_small_thresh": 15, "conv_thresh": 50, "bias_hard": 60},
        "化工": {"T_thresh": 22, "T_small_thresh": 15, "conv_thresh": 55, "bias_hard": 60},
        "农产品": {"T_thresh": 25, "T_small_thresh": 18, "conv_thresh": 60, "bias_hard": 50},
        "有色": {"T_thresh": 22, "T_small_thresh": 15, "conv_thresh": 55, "bias_hard": 55},
        "贵金属": {"T_thresh": 20, "T_small_thresh": 14, "conv_thresh": 55, "bias_hard": 55},
        "能源": {"T_thresh": 22, "T_small_thresh": 15, "conv_thresh": 55, "bias_hard": 60},
        "航运": {"T_thresh": 24, "T_small_thresh": 17, "conv_thresh": 60, "bias_hard": 65},
    },
    # Regime 自适应系数（§1.7）
    "regime_coef": {
        "趋势": {"T": 0.85, "conv": 0.90, "stop": 1.0, "cooldown": 300},
        "震荡": {"T": 1.20, "conv": 1.15, "stop": 1.0, "cooldown": 450},
        "波动": {"T": 1.00, "conv": 1.00, "stop": 1.2, "cooldown": 300},
    },
    "bias_hard_by_regime": {"趋势": 60, "波动": 65, "震荡": 70},
    "corr_gate": 0.70,
    # 背景偏置合成（P-B/P-C，2026-08-14）：让 F/C 真正参与方向/触发决策
    # 旧逻辑：方向仅由 T_5m 决定，F/C 只做"同向放行/反向打折/硬否决"，
    #   且 hard_veto 阈值 ≈ bias_hard(60+) 几乎不可达 → F/C 实质无效（P-C）。
    # 新逻辑：
    #   direction_mode="threshold"(默认/安全)：dir=sign(T_5m)；F/C 经 bias_FC(0.25F+0.15C) 调制 T 阈值
    #       · 同向强确认(|bias_FC|>=fc_confirm) → T 阈值 ×confirm_relief（更易触发，正向加成，P-B）
    #       · 反向强否决(|bias_FC|>=fc_hard 且反向) → 硬否决（阈值降到可达区间，P-C）
    #   direction_mode="combined"(可选/需回测)：dir=sign(T_5m + direction_alpha·bias_G)，F/C 可直接翻转方向
    "bias_synthesis": {
        "direction_mode": "threshold",  # "threshold" | "combined"
        "direction_alpha": 0.5,  # combined 模式：bias_G 相对 T_5m 的权重
        "fc_confirm": 25,  # |bias_FC| 达此且同向 → 降 T 阈值（正向加成）
        "confirm_relief": 0.85,  # 同向确认时 T 阈值折让（0.85=降15%）
        "fc_hard": 25,  # |bias_FC| 达此且反向 → 硬否决（替代原偏高的 bias_hard）
        "fc_hard_regime_offset": {"趋势": 0, "波动": 5, "震荡": 10},
        "bias_g_min": 50,  # combined 模式：|bias_G| 达此且 T_5m 弱时亦可触发
    },
    # 背景偏置合成权重（P2-④ 新增，供 OOS 扫参）。默认值与原硬编码 0.6/0.25/0.15 一致。
    "combine_weights": {"T": 0.6, "F": 0.25, "C": 0.15},
    # 分板块合成权重（P0 基本面因子增强，2026-08-29）：
    #   基本面因子效果好的板块（农产品、黑系、有色、贵金属）提高 F 权重，
    #   效果不好的板块（化工、能源）保持原权重。
    # 优先级：品种覆盖 > 板块权重 > 全局默认
    "sector_combine_weights": {
        "农产品": {"T": 0.50, "F": 0.35, "C": 0.15},  # ✅ 增强F+高权重
        "黑系": {"T": 0.50, "F": 0.35, "C": 0.15},  # ✅ 增强F+高权重
        "有色": {"T": 0.55, "F": 0.30, "C": 0.15},  # ✅ 增强F+中权重
        "贵金属": {"T": 0.55, "F": 0.30, "C": 0.15},  # ✅ 增强F+中权重
        "化工": {"T": 0.60, "F": 0.25, "C": 0.15},  # ❌ 旧版F，保持原权重
        "能源": {"T": 0.60, "F": 0.25, "C": 0.15},  # ❌ 旧版F，保持原权重
        "航运": {"T": 0.60, "F": 0.25, "C": 0.15},  # 无数据影响
        "其他": {"T": 0.60, "F": 0.25, "C": 0.15},
    },
    # 技术面 T 去相关（P-A，2026-08-14）：8 策略共线性 → 簇坍缩 + 趋势簇拥挤降权 + 趋势/均值背离阻尼。
    #   解决"趋势市5策略共线=5次投同一方向、T顶满、趋势末端追高杀低"问题。
    #   enabled=False 即退化为旧逐策略加权逻辑（一键回退 / A-B 对照）。
    #   ⚠️ 改动会系统性平移 T 分布（峰值与触发频次变化），上线前必须在 walk-forward / papertrack
    #      上做一轮 OOS 对比，并按新分布重校准 T_thresh（参数见 four_dim_calibrate）。
    "decorrelate": {
        "enabled": True,  # True=启用去相关合成；False=旧逻辑
        "crowd_penalty": 0.35,  # 趋势簇拥挤降权强度（0=关闭）：一致度超阈时该簇贡献最多×0.65
        "crowd_thresh": 0.8,  # 趋势簇内部同向占比超此（如≥80%）才触发降权
        "contrarian_damp": 0.25,  # 趋势 vs 均值回归 反向时的整体 T 幅值阻尼（0=关闭）
    },
    # 季节性加权（P-D，2026-08-14）：按品种分组提升 T 内 seasonal 簇权重。
    #   鸡蛋/生猪(农产品) 与 玻璃/纯碱(化工) 为强季节性品种；原 seasonal 簇权重仅 0.1~0.3 几乎不起作用。
    #   有效权重 = 基础簇权重(由 regime 决定) × global_mult × by_group[group]（未列分组取 1.0）。
    #   enabled=False 即不提升（退回原 uniform 弱权重）。注意：仅影响 T 内 seasonal 簇；
    #   F 内季节性分量提升见 fundamental_feed.SEASONAL_F_WEIGHT（两套独立杠杆）。
    "seasonal_boost": {
        "enabled": True,  # True=按组提升 seasonal 簇权重
        "global_mult": 1.5,  # 全局倍率
        "by_group": {"农产品": 1.8, "化工": 1.4},  # 分组额外倍率（命中才乘）
    },
    # 趋势市尾仓 trailing（P-G，2026-08-14）：解决 t2=2R 强制全平截断利润的问题。
    #   趋势市 t2(2R) 触发后不强制全平，改为平掉 (1-tail_pct) 锁 2R 利润、保留 tail_pct 尾仓
    #   用更宽(tail_trail_R×1R)的移动止损跟出，直到趋势回撤触及尾仓止损才离场——让利润奔跑。
    #   tail_trail_R 以 1R 为单位（stop_dist），与 stop_atr_mult 解耦，回测/实盘共用同一距离定义。
    #   trend_only=True：仅趋势 regime 启用；波动/震荡仍 t2 全平（保住既得利）。
    #   enabled=False 即退回旧逻辑（t2 全平），A-B 对照 / 一键回退。
    "trailing_tail": {
        "enabled": True,  # True=趋势市 t2 后保留尾仓跟出
        "trend_only": True,  # 仅趋势 regime 启用（波动/震荡仍 t2 全平）
        "tail_pct": 0.25,  # 尾仓比例（已平 75%，留 25%）
        "tail_trail_R": 2.0,  # 尾仓跟踪距离 = 2×1R（比原 1R 跟踪宽一倍，让利润奔跑）
        "min_profit_R": 2.0,  # 达到此 R(原 t2) 才进入尾仓态
    },
    # 分品种 regime 阈值（P-F，2026-08-14）：解决 classify_regime 全局阈值导致跨品种 regime 错配。
    #   焦煤(黑系) ATR/c 常态远高于鸡蛋(农产品)，统一 atr_thresh=0.025 会让高波动品种长期被分"波动"、
    #   低波动品种几乎到不了"波动/趋势" —— 权重/止损/触发全程错配。
    #   修复：按分组典型波动率缩放阈值（波动越高→阈值越大，避免长期错配）；逐品种可再微调。
    #   解析：enabled=False → 全部回落 default(=旧全局行为，A-B 对照/一键回退)；
    #        default → by_group[group] → by_symbol[sym] 逐级覆盖（后者优先）。
    #   注意：仅影响 classify_regime 的 regime 判定；T_score 计算/权重/触发阈值不在此处。
    "regime_params": {
        "enabled": True,  # True=启用分品种 regime 阈值；False=全部回落 default(旧行为)
        "default": {
            "atr_thresh": 0.025,
            "flat_dev": 0.008,
            "flat_atr": 0.012,
            "trend_slope": 0.003,
            "trend_dev": 0.010,
        },
        # 分组覆盖：按该组典型 ATR/c 相对基线缩放（行业常识初值，需 walk-forward 组内校准）。
        #   黑系/能源/航运波动大→阈值放大；农产品波动小→阈值缩小；其余接近基线。
        "by_group": {
            "黑系": {
                "atr_thresh": 0.035,
                "flat_dev": 0.010,
                "flat_atr": 0.018,
                "trend_slope": 0.0035,
                "trend_dev": 0.012,
            },
            "化工": {
                "atr_thresh": 0.029,
                "flat_dev": 0.009,
                "flat_atr": 0.014,
                "trend_slope": 0.0032,
                "trend_dev": 0.011,
            },
            "农产品": {
                "atr_thresh": 0.021,
                "flat_dev": 0.006,
                "flat_atr": 0.009,
                "trend_slope": 0.0025,
                "trend_dev": 0.008,
            },
            "有色": {
                "atr_thresh": 0.026,
                "flat_dev": 0.008,
                "flat_atr": 0.012,
                "trend_slope": 0.0030,
                "trend_dev": 0.010,
            },
            "贵金属": {
                "atr_thresh": 0.028,
                "flat_dev": 0.009,
                "flat_atr": 0.013,
                "trend_slope": 0.0032,
                "trend_dev": 0.011,
            },
            "能源": {
                "atr_thresh": 0.032,
                "flat_dev": 0.009,
                "flat_atr": 0.016,
                "trend_slope": 0.0034,
                "trend_dev": 0.012,
            },
            "航运": {
                "atr_thresh": 0.038,
                "flat_dev": 0.011,
                "flat_atr": 0.020,
                "trend_slope": 0.0038,
                "trend_dev": 0.013,
            },
        },
        # 逐品种微调（优先级最高；OOS 校准后填写，例：某品种常态波动偏离分组可单独标）。
        "by_symbol": {},
    },
    # P-H (2026-08-14): 稳健池准入门槛动态回灌(依赖 four_dim_recalibrate 产出的 calibration_drift.json)
    "robust_pool_gate": {
        "enabled": True,  # False=严格 v12(锁死 0.70/0.15, 忽略回灌文件)
        "auto_adapt": False,  # True=重校准调度时回灌并应用(默认关, 上线前需 OOS 验证)
        "relax_pp": 0.5,  # ensemble 近期 expR 低于 v12 门槛时, 放松量=缺口×此系数
        "max_relax": 0.05,  # 单次最多放松(相对 v12 门槛 0.15)
        "floor_oos": 0.10,  # OOS_expR 门槛硬下限(永不低于此; 仍高于 0 避免无脑放行)
    },
    # 按品种校准覆盖（2026-08-11 嵌套 walk-forward OOS 校准 v2，全市场 53 品种）。
    # ✅=稳健(OOS正期望+胜率≥保本线), ⚠️=无稳健候选含池内最优供参考, 其余沿用 group 阈值。
    "thresholds_by_symbol": {
        # ── 上期所 SHFE ──
        "cu": {"T_thresh": 28, "bias_hard_base": 50},  # ✅ OOS+0.195 胜42%
        "al": {
            "T_thresh": 12,
            "bias_hard_base": 50,
        },  # 🔧2026-08-13重校准: 28→12 放宽后样本充足且正期望(+0.26/胜40%)→解除门控
        "zn": {
            "T_thresh": 34,
            "bias_hard_base": 50,
        },  # 🔧2026-08-13重校准: 12→34 严格化后近期walk-forward转正(+0.007/胜40%)
        "ni": {
            "T_thresh": 12,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ✅ OOS+0.229 胜45% | P0: F权重OOS+0.134
        # sn: 交易数不足(锡) → 沿用 group 有色
        # ao: 交易数不足(氧化铝) → 沿用 group 有色
        "au": {"T_thresh": 22, "bias_hard_base": 50},  # ⚠️ OOS−0.302(无稳健)
        "ag": {"T_thresh": 16, "bias_hard_base": 50},  # ⚠️ OOS−0.059(无稳健)
        "rb": {"T_thresh": 22, "bias_hard_base": 50},  # ✅ OOS+0.123 胜41%
        "hc": {
            "T_thresh": 14,
            "bias_hard_base": 50,
        },  # ⚠️2026-08-13重校准: 近期walk-forward全阈值负(-0.62)，模型实盘双确认衰减→维持门控/建议剔除
        "ss": {
            "T_thresh": 14,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ⚠️ OOS−0.100(无稳健) | P0: F权重OOS+0.133
        "bu": {"T_thresh": 22, "bias_hard_base": 50},  # ⚠️ OOS−0.008(无稳健)
        "fu": {"T_thresh": 14, "bias_hard_base": 50},  # ✅ OOS+0.149 胜41%
        "ru": {
            "T_thresh": 28,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ✅ OOS+0.248 胜46% | P0: F权重OOS+0.114
        "sp": {"T_thresh": 12, "bias_hard_base": 50},  # ✅ OOS+0.058 胜39%
        # sc: 交易数不足(原油) → 沿用 group 能源
        # ── 上期能源 INE ──
        # ec: 交易数不足(欧线) → 沿用 group 航运
        # ── 大商所 DCE ──
        "i": {"T_thresh": 14, "bias_hard_base": 50},  # ⚠️ OOS−0.016(无稳健)
        "J": {"T_thresh": 22, "bias_hard_base": 50},  # ✅ OOS+0.273 胜45%
        "JM": {
            "T_thresh": 14,
            "bias_hard_base": 50,
        },  # ⚠️2026-08-13重校准: 近期walk-forward全阈值负(-0.97/胜0%)，模型实盘双确认衰减→维持门控/建议剔除
        "eb": {
            "T_thresh": 16,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # 🔧2026-08-13重校准: 模型健康(+0.62/胜55%)，实盘连亏为近期运气→解除门控 | P0: F权重OOS+0.276
        "eg": {"T_thresh": 12, "bias_hard_base": 50},  # ⚠️ OOS−0.180(无稳健)
        "l": {"T_thresh": 22, "bias_hard_base": 50},  # ✅ OOS+0.064 胜38%
        "pp": {"T_thresh": 28, "bias_hard_base": 50},  # ✅ OOS+0.029 胜37%
        "v": {"T_thresh": 28, "bias_hard_base": 50},  # ✅ OOS+0.189 胜42%
        # pg: 交易数不足(液化气) → 沿用 group 能源
        "m": {"T_thresh": 12, "bias_hard_base": 50},  # ⚠️ OOS−0.029(无稳健)
        "y": {"T_thresh": 12, "bias_hard_base": 50},  # ✅ OOS+0.154 胜41%
        "a": {"T_thresh": 14, "bias_hard_base": 50},  # ⚠️ OOS−0.084(无稳健)
        "b": {"T_thresh": 16, "bias_hard_base": 50},  # ⚠️ OOS−0.099(无稳健)
        "p": {"T_thresh": 12, "bias_hard_base": 50},  # ✅ OOS+0.089 胜40%
        "c": {"T_thresh": 26, "bias_hard_base": 50},  # ✅ OOS+0.226 胜45%
        "cs": {"T_thresh": 12, "bias_hard_base": 50},  # ✅ OOS+0.145 胜41%
        "jd": {"T_thresh": 30, "bias_hard_base": 50},  # ✅ OOS+0.066 胜39%
        # lh: 交易数不足(生猪) → 沿用 group 农产品
        "rr": {"T_thresh": 12, "bias_hard_base": 50},  # ⚠️ OOS−0.261(无稳健)
        # ── 郑商所 CZCE ──
        "FG": {"T_thresh": 18, "bias_hard_base": 50},  # ✅ OOS+0.105 胜38%
        # SA: 交易数不足(纯碱) → 沿用 group 化工
        "MA": {"T_thresh": 16, "bias_hard_base": 50},  # ✅ OOS+0.161 胜42%
        "TA": {"T_thresh": 12, "bias_hard_base": 50},  # ✅ OOS+0.208 胜42%
        # PF: 交易数不足(短纤) → 沿用 group 化工
        # PX: 交易数不足(对二甲苯) → 沿用 group 化工
        # SH: 交易数不足(烧碱) → 沿用 group 化工
        "UR": {
            "T_thresh": 12,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ✅ OOS+0.018 胜35% | P0: F权重OOS+0.149
        # PR: 交易数不足(瓶片) → 沿用 group 化工
        "SR": {"T_thresh": 30, "bias_hard_base": 50},  # ✅ OOS+0.110 胜40%
        "CF": {
            "T_thresh": 30,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ✅ OOS+0.231 胜44% | P0: F权重OOS+0.251
        "RM": {
            "T_thresh": 28,
            "bias_hard_base": 50,
            "combine_weights": {"T": 0.45, "F": 0.40, "C": 0.15},
        },  # ⚠️ OOS−0.073(无稳健) | P0: F权重OOS+0.084
        "OI": {"T_thresh": 20, "bias_hard_base": 50},  # ✅ OOS+0.073 胜38%
        # PK: 交易数不足(花生) → 沿用 group 农产品
        "AP": {"T_thresh": 14, "bias_hard_base": 50},  # ✅ OOS+0.080 胜40%
        # ── 广期所 GFEX ──
        # si: 交易数不足(工业硅) → 沿用 group 有色
        # lc: 交易数不足(碳酸锂) → 沿用 group 有色
    },
}

COLMAP = {
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "持仓量": "oi",
    "动态结算价": "settlement",
}


# ----------------------------------------------------------------------------
# 数据层
# ----------------------------------------------------------------------------
def load_daily(code):
    """读主连日线 _XX0_daily.csv，中文列→标准列，DatetimeIndex。
    带进程内只读缓存，重复调用省 CSV 解析开销。"""
    code_u = code.upper()
    if code_u in _DAILY_CACHE:
        return _DAILY_CACHE[code_u][0]
    for c in (code, code.upper(), code.lower()):
        p = os.path.join(BACKTEST_DIR, f"_{c}0_daily.csv")
        if os.path.exists(p):
            df = pd.read_csv(p).rename(columns=COLMAP)
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            _DAILY_CACHE[code_u] = (df, 0.0)  # 0.0 = 永不过期（只读 CSV，无刷新需求）
            return df
    return None


_DAILY_CACHE = {}  # symbol -> (df, timestamp) 进程内缓存


def _norm_daily_cols(raw):
    """把 sina(英/中) 或 东财(中) 的日线 DataFrame 统一为标准列并设 date 索引。"""
    if raw is None or len(raw) == 0:
        return raw
    ren = {
        "hold": "oi",
        "settle": "settlement",
        "open_interest": "oi",
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "持仓量": "oi",
        "开盘价": "open",
        "收盘价": "close",
    }
    raw = raw.rename(columns=ren)
    if "date" in raw.columns:
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw.set_index("date").sort_index()
    return raw


def _fetch_daily_eastmoney(code):
    """东财期货主连日K（公开 HTTP，无需 token；best-effort）。返回已标准化(date索引)的 df。"""
    import json as _json
    import urllib.request

    secid = "114." + code.lower()
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        "?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=0&secid={secid}&beg=0&end=20500101"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = _json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
    kls = (data.get("data") or {}).get("klines") or []
    rows = []
    for kl in kls:
        p = kl.split(",")
        if len(p) < 7:
            continue
        rows.append(
            {
                "date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "oi": float(p[6]),
            }
        )
    if not rows:
        raise RuntimeError("东财返回空")
    return _norm_daily_cols(pd.DataFrame(rows))


def _fetch_daily_robust(code):
    """多源日线兜底：① sina 主源 → ② sina-main(带日期范围) → ③ 东财 HTTP。
    任一阵列失败即跳下一源；全失败抛 RuntimeError（由上层沿用上次值）。"""
    from datetime import datetime, timedelta

    import akshare as ak

    last_err = None
    # ① sina 主源（现有逻辑）
    try:
        raw = ak.futures_zh_daily_sina(symbol=code)
        if raw is not None and len(raw) >= 60:
            return _norm_daily_cols(raw)
    except Exception as e:
        last_err = e
        print(f"  [daily] {code} sina主源失败: {e}")
    # ② sina-main（带日期范围，有时比主源稳，尤原油/sc 等 INE 品种）
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=900)).strftime("%Y%m%d")
        raw = ak.futures_main_sina(symbol=code, start_date=start, end_date=end)
        if raw is not None and len(raw) >= 60:
            return _norm_daily_cols(raw)
    except Exception as e:
        last_err = e
        print(f"  [daily] {code} sina-main失败: {e}")
    # ③ 东财 HTTP（公开，无需 token）
    try:
        return _fetch_daily_eastmoney(code)
    except Exception as e:
        last_err = e
        print(f"  [daily] {code} 东财HTTP失败: {e}")
    raise RuntimeError(f"{code} 所有日线源失败: {last_err}")


def load_daily_refreshed(symbol, ttl=1800):
    """P1-5 重构：load_daily + akshare 近期日线追加（minishare 无 fut_daily 权限，按分工走免费源兜底）。
    仅用于实盘/纸面追踪，绝不在 walk_forward_backtest 中使用（避免前视）。ttl 秒缓存。

    具体交割合约（_CONTRACT_AKSHARE 内，如 SA01）：直接拉 akshare 该合约日线，
    不走主连缓存 CSV（主连 _XX0_daily.csv 与该合约日线不同）。
    深度重审修复：
      - B3 fall-through bug：具体合约条数不足60时不再落到主连 load_daily() 混用数据源
      - 消除重复 import time as _t
      - 缓存检查仅保留一处（函数开头），不再在中间重复判断"""
    import time as _t

    cached = _DAILY_CACHE.get(symbol)
    if cached and (_t.time() - cached[1]) < ttl:
        return cached[0]

    # 分支 1: 具体交割合约（如 SA01）—— 只走 akshare 单源，fall-through 被根治
    if symbol in _CONTRACT_AKSHARE:
        try:
            code = _CONTRACT_AKSHARE[symbol]
            raw = _fetch_daily_robust(code)
            if len(raw) >= 60:
                _DAILY_CACHE[symbol] = (raw, _t.time())
            elif len(raw) > 0:
                print(f"  [daily refresh] {symbol}({code}) 条数偏少({len(raw)}<60)，仍返回部分数据")
            else:
                print(f"  [daily refresh] {symbol}({code}) akshare 返回空数据")
            return raw if len(raw) > 0 else None
        except Exception as e:
            print(f"  [daily refresh] {symbol}({code}) 全部源失败: {e}")
            return None

    # 分支 2: 常规品种 —— load_daily 本地主连 + akshare 近期追加
    df = load_daily(symbol)
    try:
        # akshare sina 主力连续代码（symbol → sina code）
        code = _AKSHARE_MAP.get(symbol, symbol.upper() + "0")
        raw = _fetch_daily_robust(code)
        if raw is not None and len(raw) >= 1:
            if df is not None and len(df) >= 1:
                new = raw[raw.index > df.index[-1]]
                if len(new):
                    df = pd.concat([df, new])
            else:
                df = raw
        _DAILY_CACHE[symbol] = (df, _t.time())
    except Exception as e:
        print(f"  [daily refresh] {symbol} akshare 兜底失败: {e}")
    return df


def load_min5(code, fetch_if_missing=True, live=False):
    """读主连 5m：
    - live=True（盘中实时）：优先 minishare 实时快照聚合 5m（minishare_live.build_min5_live），
      彻底不走 sina；minishare 不可用时回退 None（触发判定退化为 T@D）。
    - live=False（回测/离线）：先查本地缓存（管住手量化回测目录 + 本地 data_5m），
      缺失则 sina 拉取落盘。返回 DatetimeIndex 的 OHLCV DataFrame；仍缺失返回 None。"""
    if live:
        try:
            import minishare_live as ml

            df = ml.build_min5_live(code)
            if df is not None and len(df) >= 1:
                return df
        except Exception as e:
            print(f"  [live 5m] minishare 失败，回退: {e}")
        return None
    for base in (BACKTEST_DIR, DATA_5M_DIR):
        for c in (code, code.upper(), code.lower()):
            p = os.path.join(base, f"_{c}0_min5.csv")
            if os.path.exists(p):
                df = pd.read_csv(p)
                # 列名兼容：日期/时间/datetime → date
                for src in ("日期", "时间", "datetime", "Datetime", "time", "Time"):
                    if src in df.columns and "date" not in df.columns:
                        df = df.rename(columns={src: "date"})
                        break
                df = df.rename(columns=COLMAP)
                if "date" not in df.columns:
                    continue
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
                return df
    if fetch_if_missing:
        return _fetch_min5_sina(code)
    return None


def _fetch_min5_sina(code):
    """sina 拉 5m 主连（具体合约如 FG2509），落盘到 data_5m/_XX0_min5.csv。仅近 ~1023 根。"""
    import akshare as ak

    # 主连代码 -> 近期主力合约（用当前年份 9 月 / 次年 1 月近似；实盘应跟换月，回测取最近即可）
    yr = datetime.now().year % 100
    contract = f"{code}{yr}09"
    try:
        df = ak.futures_zh_minute_sina(symbol=contract, period="5")
    except Exception as e:
        print(f"  sina 5m 拉取失败 {code}({contract}):", repr(e)[:80])
        return None
    if df is None or getattr(df, "empty", True):
        return None
    df = df.rename(columns={"datetime": "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df.rename(
        columns={"hold": "oi", "volume": "volume", "open": "open", "high": "high", "low": "low", "close": "close"}
    )
    out = df[["open", "high", "low", "close", "volume", "oi"]].copy()
    out.to_csv(os.path.join(DATA_5M_DIR, f"_{code}0_min5.csv"))
    print(f"  已缓存 {code} 5m -> {len(out)} 根")
    return out


def score_F(symbol, date_str=None):
    """基本面 F ∈ [-100,100]；读 fundamentals.json（基差/库存派生），缺失→中性 0（不阻断）。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    try:
        return float(ff.compute_F(symbol, date_str))
    except Exception:
        return 0.0


def _load_cpos_cached():
    """带 mtime 缓存的 cpos_cache.json 加载。walk-forward 回测热点路径用。"""
    global _CPOS_CACHE
    try:
        mtime = os.path.getmtime(CPOS_JSON)
    except OSError:
        return None
    if _CPOS_CACHE["data"] is not None and mtime == _CPOS_CACHE["mtime"]:
        return _CPOS_CACHE["data"]
    try:
        with open(CPOS_JSON, encoding="utf-8") as _f:
            d = json.load(_f)
        _CPOS_CACHE = {"mtime": mtime, "data": d}
        return d
    except (json.JSONDecodeError, OSError, ValueError):
        _CPOS_CACHE = {"mtime": mtime, "data": None}
        return None


def score_C(symbol, date_str=None):
    """资金面 C ∈ [-100,100]；龙虎榜历史代理（缺 cpos_cache.json 时中性 0）。
    实时 C_flow 另由 compute_C_flow 提供（minishare 差分 + da龘 tick），不在本函数。
    若给定 date_str 且该日历史存在则取该日 C_score（回测用），否则取最新可用值。
    性能优化：带 mtime 缓存的文件加载，避免 walk-forward 中重复 json.load。"""
    d = _load_cpos_cached()
    if d is None:
        return 0.0
    try:
        ckey = _CONTRACT_CPOS_KEY.get(symbol.upper(), symbol.upper())
        sym = d.get(ckey) if isinstance(d, dict) else None
        if sym:
            if date_str:
                for h in sym.get("history", []):
                    if h.get("date") == date_str and h.get("C_score") is not None:
                        return float(h["C_score"])
            v = sym.get("C_score")
            if v is not None:
                return float(v)
    except (ValueError, TypeError):
        pass
    return 0.0


def precompute_C_array(symbol, date_strs=None, date_ints=None):
    """预计算 C 值数组（双指针 O(n)，省逐次 history 遍历查找）。
    支持 date_strs（字符串）或 date_ints（整数 YYYYMMDD），优先用整数（更快）。
    返回 float64 数组。无 cpos_cache 或无历史数据时返回全 0。"""
    import numpy as np

    if date_ints is not None:
        n = len(date_ints)
        use_int = True
    elif date_strs is not None:
        n = len(date_strs)
        use_int = False
    else:
        return np.array([], dtype=np.float64)

    if n == 0:
        return np.array([], dtype=np.float64)

    d = _load_cpos_cached()
    if d is None:
        return np.zeros(n, dtype=np.float64)

    try:
        ckey = _CONTRACT_CPOS_KEY.get(symbol.upper(), symbol.upper())
        sym = d.get(ckey) if isinstance(d, dict) else None
        if not sym:
            return np.zeros(n, dtype=np.float64)

        history = sym.get("history", [])
        if not history:
            v = sym.get("C_score")
            if v is not None:
                return np.full(n, float(v), dtype=np.float64)
            return np.zeros(n, dtype=np.float64)

        # 构建排序后的日期 + C_score 列表
        c_list = [(h["date"], float(h["C_score"])) for h in history if h.get("C_score") is not None and h.get("date")]
        c_list.sort(key=lambda x: x[0])

        result = np.zeros(n, dtype=np.float64)
        latest = sym.get("C_score")
        latest_val = float(latest) if latest is not None else 0.0

        if not c_list:
            return np.full(n, latest_val, dtype=np.float64) if latest_val != 0 else np.zeros(n, dtype=np.float64)

        if use_int:
            # 转成整数日期（支持 YYYY-MM-DD 和 YYYYMMDD 两种格式）
            sorted_vals = [int(x[0].replace("-", "")) for x in c_list]
            c_vals = [x[1] for x in c_list]
            j = 0
            nd = len(sorted_vals)
            current_val = 0.0
            for i in range(n):
                di = date_ints[i]
                while j < nd and sorted_vals[j] <= di:
                    current_val = c_vals[j]
                    j += 1
                result[i] = current_val if j > 0 else 0.0
        else:
            sorted_dates = [x[0] for x in c_list]
            c_vals = [x[1] for x in c_list]
            j = 0
            nd = len(sorted_dates)
            current_val = 0.0
            for i in range(n):
                d_str = date_strs[i]
                while j < nd and sorted_dates[j] <= d_str:
                    current_val = c_vals[j]
                    j += 1
                result[i] = current_val if j > 0 else 0.0

        return result
    except (ValueError, TypeError):
        pass
    return np.zeros(n, dtype=np.float64)


# ----------------------------------------------------------------------------
# 资金面流量组 C_flow（实盘）：minishare 60s 快照差分 + da龘 tick 订单流
# ----------------------------------------------------------------------------
class FlowAggregator:
    """累积 minishare 60s 快照 + da龘 tick 增量，构造「盘中实时净流速率」序列。
    组内只取一个主信号（§1.3 流量组）：净流入速率方向（量×价方向派生），不重复用裸成交量。
    用 minishare 不限次 60s 轮询 → 真·minishare 驱动 T@5m 与 C_flow。"""

    def __init__(self, symbol, window=30):
        self.sym = symbol
        self.window = window
        self.snaps = []  # (ts, last, oi, vol)
        self.deltas = []  # 净流入速率分量序列
        self.tick_delta = 0.0  # da龘 tick 订单流累计（最新一窗）

    def push_minishare(self, last, oi=None, vol=None, ts=None):
        ts = ts or time.time()
        if self.snaps:
            p_last, p_oi, p_vol = self.snaps[-1][1], self.snaps[-1][2], self.snaps[-1][3]
            dP = last - p_last
            dOI = (oi - p_oi) if (oi is not None and p_oi is not None) else 0.0
            dVol = (vol - p_vol) if (vol is not None and p_vol is not None) else 0.0
            # 净流入代理 = 价格变动 × 持仓变动（双增=资金流入accumulation，看多）
            flow = dP * dOI if dOI != 0 else dP * (dVol if dVol != 0 else 0.0)
            self.deltas.append(flow)
        self.snaps.append((ts, last, oi, vol))
        if len(self.snaps) > self.window * 2:
            self.snaps.pop(0)
            self.deltas.pop(0)

    def push_tick(self, delta):
        """da龘 tick 订单流增量（买压为正）。叠加进净流速率。"""
        self.tick_delta = delta

    def c_flow_score(self):
        """返回 C_flow ∈ [-100,100]：近 window 个净流分量的累计方向 + da龘 tick 加权。"""
        if len(self.deltas) < 3:
            return 0.0
        recent = self.deltas[-self.window :]
        s = sum(recent)
        mag = max(1e-9, max(abs(x) for x in recent))
        base = max(-100.0, min(100.0, s / (mag * self.window) * 100))
        # da龘 tick 订单流加成（同向增强，反向制衡）
        if self.tick_delta != 0:
            td = max(-100.0, min(100.0, self.tick_delta))
            base = max(-100.0, min(100.0, 0.7 * base + 0.3 * td))
        return round(base, 1)


def compute_C_flow(symbol, snapshots):
    """一次性计算 C_flow（snapshots: [(last, oi, vol), ...] 时序）。"""
    agg = FlowAggregator(symbol)
    for s in snapshots:
        agg.push_minishare(*s)
    return agg.c_flow_score()


# ----------------------------------------------------------------------------
# 技术面 T（复用 da龘 8 策略 + regime 加权）
# ----------------------------------------------------------------------------
# 策略簇（P-A 去相关，2026-08-14）：8 策略按经济含义聚为 3 簇。
#   同簇策略高度共线（5 个趋势都是"价在均线上/突破"变体），不再各自加权累加，
#   而是先坍缩为「簇投票」(簇内 mean signal)，簇间再加权合成 → 消除共线放大。
STRAT_CLUSTERS = {
    "trend": TREND_STRATS,  # ma_break / dma / turtle / donchian / pullback
    "mean": MEAN_STRATS,  # boll / rsi
    "seasonal": ["seasonal"],  # seasonal
}


def regime_weights(regime):
    if regime == "趋势":
        w = {k: 1.0 for k in TREND_STRATS}
        w.update({k: 0.3 for k in MEAN_STRATS})
        w["seasonal"] = 0.2
    elif regime == "震荡":
        w = {k: 0.3 for k in TREND_STRATS}
        w.update({k: 1.0 for k in MEAN_STRATS})
        w["seasonal"] = 0.3
    elif regime == "波动":
        w = {k: 0.5 for k in TREND_STRATS}
        w.update({k: 0.2 for k in MEAN_STRATS})
        w["seasonal"] = 0.1
    else:
        w = {k: 0.5 for k in STRATS}
    return w


# 预计算每个 regime 的基础簇权重（cluster_weights 的无 cfg/group 版本）
# 回测高频调用时直接查表，省 dict 遍历 + 生成器表达式开销
def _precompute_cluster_base():
    result = {}
    for regime in ("趋势", "震荡", "波动", "过渡", "未知"):
        rw = regime_weights(regime)
        cw = {}
        for cname, members in STRAT_CLUSTERS.items():
            if not members:
                cw[cname] = 0.0
            else:
                cw[cname] = sum(rw.get(m, 0.0) for m in members) / len(members)
        result[regime] = cw
    return result


_CLUSTER_WEIGHTS_BASE = _precompute_cluster_base()


def cluster_weights(regime, cfg=None, group=None, feat_mgr=None):
    """簇级权重（P-A 去相关核心 + P-D 季节性分组加权）。
    基础权重查表（_CLUSTER_WEIGHTS_BASE），只在 seasonal_boost 开启时动态调整。
    注意：返回的 dict 是共享引用，只读使用；如需修改请先 copy。"""
    # 基础簇权重：直接查表，O(1)
    base = _CLUSTER_WEIGHTS_BASE.get(regime, _CLUSTER_WEIGHTS_BASE["过渡"])
    # 无 cfg / group / seasonal_boost → 直接返回基础值（只读，省 dict copy）
    if cfg is None and group is None:
        return base
    # P-D：seasonal 簇按品种分组加权提升
    sb = (cfg or {}).get("seasonal_boost", {})
    # 开关优先级：特性开关 > 旧配置 > 默认关闭
    sb_enabled = None
    if feat_mgr is not None:
        try:
            sb_enabled = feat_mgr.is_enabled("seasonal_boost")
        except Exception:
            sb_enabled = None
    if sb_enabled is None:
        sb_enabled = bool(sb.get("enabled", False))
    if sb_enabled and group is not None:
        cw = dict(base)
        mult = float(sb.get("global_mult", 1.0))
        mult *= float(sb.get("by_group", {}).get(group, 1.0))
        cw["seasonal"] = cw.get("seasonal", 0.0) * mult
        return cw
    return base


_DECORR_OFF_WARNED = [False]  # P2a 守卫：decorrelate.enabled=False 时一次性告警


def precompute_T_array(sig_arrays, regime_codes, cfg=DEFAULT_CONFIG, group=None, feat_mgr=None):
    """向量化预计算完整 T 值序列（簇投票 + 拥挤降权 + 反向阻尼 + 归一化）。
    返回 T_arr (float64 数组, 已 round 到 1 位小数)。
    与 compute_T 逐点结果一致（decorrelate.enabled=True 路径）。"""
    from strategy_layer import REGIME_CODE_TO_NAME

    n = len(regime_codes)
    dc = (cfg or DEFAULT_CONFIG).get("decorrelate", {})
    crowd_pen = float(dc.get("crowd_penalty", 0.35))
    crowd_th = float(dc.get("crowd_thresh", 0.8))
    contr_damp = float(dc.get("contrarian_damp", 0.25))

    # 1) 簇投票 + 一致度（全向量化）
    # trend 簇：5 个策略
    trend_sigs = np.column_stack([sig_arrays[m] for m in STRAT_CLUSTERS["trend"]]).astype(np.float64)
    cluster_trend = trend_sigs.mean(axis=1)
    # 一致度：同方向信号比例
    sgn_trend = np.sign(cluster_trend)
    agree_trend = np.zeros(n)
    for col in range(trend_sigs.shape[1]):
        agree_trend += (trend_sigs[:, col] == sgn_trend).astype(np.float64)
    agree_trend = np.where(sgn_trend != 0, agree_trend / trend_sigs.shape[1], 0.0)

    # mean 簇：2 个策略
    mean_sigs = np.column_stack([sig_arrays[m] for m in STRAT_CLUSTERS["mean"]]).astype(np.float64)
    cluster_mean = mean_sigs.mean(axis=1)
    sgn_mean = np.sign(cluster_mean)
    agree_mean = np.zeros(n)
    for col in range(mean_sigs.shape[1]):
        agree_mean += (mean_sigs[:, col] == sgn_mean).astype(np.float64)
    agree_mean = np.where(sgn_mean != 0, agree_mean / mean_sigs.shape[1], 0.0)

    # seasonal 簇：1 个策略
    cluster_seasonal = sig_arrays["seasonal"].astype(np.float64)
    agree_seasonal = np.where(cluster_seasonal != 0, 1.0, 0.0)

    # 2) 为每个 regime 预计算簇权重（cw 和 cw_base）
    # cw: 含 seasonal_boost 的实际权重；cw_base: 未加权基础权重（用于归一化分母）
    cw_by_regime = {}
    cw_base_by_regime = {}
    for code, name in REGIME_CODE_TO_NAME.items():
        cw_by_regime[code] = cluster_weights(name, cfg, group, feat_mgr)
        cw_base_by_regime[code] = cluster_weights(name, None, None, feat_mgr)

    # 向量化：用 regime_codes 选择对应权重
    cw_trend = np.zeros(n)
    cw_mean = np.zeros(n)
    cw_seasonal = np.zeros(n)
    cw_base_sum = np.zeros(n)
    for code in REGIME_CODE_TO_NAME:
        mask = regime_codes == code
        cw_trend[mask] = cw_by_regime[code]["trend"]
        cw_mean[mask] = cw_by_regime[code]["mean"]
        cw_seasonal[mask] = cw_by_regime[code]["seasonal"]
        cb = cw_base_by_regime[code]
        cw_base_sum[mask] = cb["trend"] + cb["mean"] + cb["seasonal"]

    # 3) 拥挤降权（仅趋势簇）
    crowd_factor = np.ones(n)
    if crowd_pen > 0:
        denom = (1.0 - crowd_th) if (1.0 - crowd_th) > 0 else 1.0
        applies = (agree_trend > crowd_th) & (cluster_trend != 0)
        over = np.minimum(1.0, (agree_trend - crowd_th) / denom)
        crowd_factor = np.where(applies, np.maximum(0.0, 1.0 - crowd_pen * over), 1.0)

    # 4) 各簇贡献 + 原始 raw
    trend_contrib = cw_trend * cluster_trend * crowd_factor
    mean_contrib = cw_mean * cluster_mean
    seas_contrib = cw_seasonal * cluster_seasonal
    raw = trend_contrib + mean_contrib + seas_contrib

    # 5) 反向阻尼（趋势 vs 均值 背离）
    if contr_damp > 0:
        diverge = (trend_contrib * mean_contrib < 0) & (trend_contrib != 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            div = np.minimum(np.abs(trend_contrib), np.abs(mean_contrib)) / (np.abs(trend_contrib) + 1e-9)
        damp = np.where(diverge, 1.0 - contr_damp * div, 1.0)
        raw = raw * damp

    # 6) 归一化到 [-100, 100]
    with np.errstate(divide="ignore", invalid="ignore"):
        abs_T = np.minimum(100.0, np.abs(raw) / np.maximum(cw_base_sum, 1e-9) * 100.0)
    T_arr = np.copysign(abs_T, raw)
    # cw_base_sum <= 0 → T = 0
    T_arr = np.where(cw_base_sum > 0, T_arr, 0.0)
    # round 到 1 位小数
    T_arr = np.round(T_arr, 1)

    return T_arr


def compute_T(
    df,
    cfg=DEFAULT_CONFIG,
    group=None,
    symbol=None,
    feat_mgr=None,
    _c=None,
    _h=None,
    _l=None,
    _m=None,
    _atr14=None,
    _rets=None,
    _sma5=None,
    _sma20=None,
    _sma60=None,
    _sma5_prev=None,
    _sma20_prev=None,
    _sma20_slope_prev=None,
    _rsi14=None,
    _std20=None,
    _seasonal_cnt=None,
    _seasonal_sum=None,
    _seasonal_sumsq=None,
    _signal_arrays=None,
    _sig_idx=None,
    _T_array=None,
    _regime_code=None,
):
    """8 策略 → regime 加权 → 去相关合成 → T_score ∈ [-100,100]（P-A 整改，2026-08-14）。

    去相关设计（对照 P-A 三条建议）：
      · ① 同簇共线策略先坍缩为「簇投票」(簇内 mean signal∈[-1,1])，簇间再加权合成。
        旧逻辑逐策略加权累加，趋势市 5 个共线策略 = "5 次投同一方向" → T 易顶满 100。
        新逻辑趋势簇只算 1 票（权重 = 簇总权重），T 上限由簇间合力决定，不再被共线放大。
      · ② 拥挤降权（crowd_penalty）：趋势簇内部一致度（同向占比）过高 → 对该簇贡献打折，
        抑制趋势末端"一致度最高→最易触发"的追高杀低。
      · ③ 反向阻尼（contrarian_damp）：趋势簇与均值回归簇反向（动量末端背离）→ 整体 T 幅值再打折，
        显式引入 contrarian 维度平衡动量末端风险。
    配置：cfg["decorrelate"]（DEFAULT_CONFIG + trade_config.json 可覆盖）；
      cfg["seasonal_boost"]（P-D 季节性分组加权，需传入 group=品种分组）；
      cfg["regime_params"]（P-F 分品种 regime 阈值，需传入 symbol 解析分组 → 喂 classify_regime）。
      enabled=False 时退化为旧逐策略加权逻辑（A-B 对照 / 一键回退）。
    返回形状不变：(T_score, regime, rdesc) —— 与 pipeline 调用契约兼容。
    group: 品种分组（如 "农产品"/"化工"），用于 seasonal_boost 分组倍率；None 则不分组加权。
    symbol: 品种代号（如 "JM"/"jd"），用于 P-F 分品种 regime 阈值解析；None 则用 classify_regime 默认全局阈值。
    """
    if df is None or len(df) < 60:
        return 0.0, "未知", "数据不足"

    # 最快路径：T 值 + regime 均已预计算 → 直接索引返回
    if _T_array is not None and _sig_idx is not None and _regime_code is not None:
        from strategy_layer import REGIME_CODE_TO_NAME

        T_val = float(_T_array[_sig_idx])
        regime_name = REGIME_CODE_TO_NAME.get(int(_regime_code), "未知")
        return T_val, regime_name, ""

    # 预提取 numpy 数组 + 预计算 SMA5/20/60（去重：8 策略 + classify_regime 共用）
    # 若外部已传入（walk-forward 预切片）则直接使用，省掉 DataFrame 列访问开销
    if _c is None:
        _c = df["close"].values
        _h = df["high"].values
        _l = df["low"].values
        if _m is None:
            _m = df.index.month.values if isinstance(df.index, pd.DatetimeIndex) else None
    # 预计算 SMA5/20/60（外部已传入则直接用，省 _sma_last 切片+均值开销）
    _sma5 = _sma5 if _sma5 is not None else _sma_last(_c, 5)
    _sma20 = _sma20 if _sma20 is not None else _sma_last(_c, 20)
    _sma60 = _sma60 if _sma60 is not None else _sma_last(_c, 60)
    # 是否走纯 numpy 路径（外部预传入数组 → 回测高频调用）
    _has_np = _c is not None and _h is not None and _l is not None

    # P-F：分品种 regime 阈值（波动大的品种放大阈值，避免长期被分错 regime）
    regime_rp = regime_params_for(symbol, cfg, feat_mgr) if symbol else {}
    regime, rdesc = classify_regime(
        df, regime_rp, _close=_c, _high=_h, _low=_l, _atr14=_atr14, _sma20=_sma20, _sma20_slope_prev=_sma20_slope_prev
    )
    dc = (cfg or DEFAULT_CONFIG).get("decorrelate", {})
    # 开关优先级：特性开关 > 旧配置 > 默认开启
    enabled = None
    if feat_mgr is not None:
        try:
            enabled = feat_mgr.is_enabled("decorrelate")
        except Exception:
            enabled = None
    if enabled is None:
        enabled = bool(dc.get("enabled", True))

    # 1) 各策略信号（与旧逻辑一致，异常防御）
    # 快速路径：外部已预计算全部信号数组 → 直接索引（省 8 次函数调用 + dict 展开）
    sig = {}
    if _signal_arrays is not None and _sig_idx is not None:
        idx = _sig_idx
        for name, arr in _signal_arrays.items():
            sig[name] = int(arr[idx])
    else:
        # 通用 kwargs：所有策略都能识别 _close/_high/_low 等前缀参数，未使用的会被忽略
        # _detail=False：回测高频调用时跳过详情 dict 构造（省 round + dict 分配开销）
        _kw = {
            "_close": _c,
            "_high": _h,
            "_low": _l,
            "_months": _m,
            "_sma5": _sma5,
            "_sma20": _sma20,
            "_sma60": _sma60,
            "_atr14": _atr14,
            "_rets": _rets,
            "_detail": not _has_np,
            "_sma5_prev": _sma5_prev,
            "_sma20_prev": _sma20_prev,
            "_rsi": _rsi14,
            "_std20": _std20,
            "_seasonal_cnt": _seasonal_cnt,
            "_seasonal_sum": _seasonal_sum,
            "_seasonal_sumsq": _seasonal_sumsq,
        }
        # 若外部已传入全部 numpy 数组，则纯 numpy 路径（跳过 df，省 DataFrame 访问开销）
        for name, fn in STRATS.items():
            try:
                if _has_np:
                    s, _ = fn(**_kw)
                else:
                    s, _ = fn(df, **_kw)
            except Exception:
                s = 0
            sig[name] = int(s)

    # 兼容 / A-B 对照：旧逐策略 regime 加权累加
    if not enabled:
        # P2a 守卫（2026-08-19）：退回旧逐策略累加路径=共线放大、信号更激进，非有意关闭应告警
        if not _DECORR_OFF_WARNED[0]:
            _DECORR_OFF_WARNED[0] = True
            print(
                "[RISK-WARN] compute_T: decorrelate.enabled=False → 退回旧逐策略累加路径"
                "（5个共线趋势策略=5票放大，信号更激进）。若非有意关闭，请检查 trade_config.json。",
                flush=True,
            )
        w = regime_weights(regime)
        score = sum(sig[k] * w[k] for k in STRATS)
        maxw = sum(abs(w[k]) for k in STRATS)
        if maxw <= 0:
            return 0.0, regime, rdesc
        T = math.copysign(min(100.0, abs(score) / maxw * 100.0), score)
        return round(T, 1), regime, rdesc

    # 2) 簇投票（坍缩共线）+ 簇内一致度
    cw = cluster_weights(regime, cfg, group, feat_mgr)
    cluster_vote, cluster_consensus = {}, {}
    for cname, members in STRAT_CLUSTERS.items():
        votes = [sig[m] for m in members]
        if not votes:
            cluster_vote[cname] = 0.0
            cluster_consensus[cname] = 0.0
            continue
        mean_v = sum(votes) / len(votes)
        cluster_vote[cname] = mean_v
        sgn = 1 if mean_v > 0 else (-1 if mean_v < 0 else 0)
        agree = (sum(1 for v in votes if v == sgn) / len(votes)) if sgn != 0 else 0.0
        cluster_consensus[cname] = agree

    # 3) 拥挤降权（仅趋势簇，P-A ②）
    crowd_pen = float(dc.get("crowd_penalty", 0.35))
    crowd_th = float(dc.get("crowd_thresh", 0.8))
    consensus = cluster_consensus.get("trend", 0.0)
    crowd_factor = 1.0
    if crowd_pen > 0 and consensus > crowd_th and cluster_vote["trend"] != 0:
        denom = (1.0 - crowd_th) if (1.0 - crowd_th) > 0 else 1.0
        over = min(1.0, (consensus - crowd_th) / denom)
        crowd_factor = max(0.0, 1.0 - crowd_pen * over)

    trend_contrib = cw["trend"] * cluster_vote["trend"] * crowd_factor
    mean_contrib = cw["mean"] * cluster_vote["mean"]
    seas_contrib = cw["seasonal"] * cluster_vote["seasonal"]
    raw = trend_contrib + mean_contrib + seas_contrib

    # 4) 反向阻尼（趋势 vs 均值回归 背离，P-A ③）
    contr_damp = float(dc.get("contrarian_damp", 0.25))
    if contr_damp > 0 and trend_contrib * mean_contrib < 0:
        div = min(abs(trend_contrib), abs(mean_contrib)) / (abs(trend_contrib) + 1e-9)
        raw = raw * (1.0 - contr_damp * div)

    # 5) 归一化到 [-100,100]
    #    分母用【未加权】簇权重之和（seasonal_boost 仅放大 seasonal 簇的"发言权"，不膨胀分母），
    #    保证：seasonal 不触发时 T 分布与未加权一致（不被稀释）；触发时 T 被抬升 —— 加法语义。
    cw_base = cluster_weights(regime, None, None, feat_mgr)
    maxw = cw_base["trend"] + cw_base["mean"] + cw_base["seasonal"]
    if maxw <= 0:
        return 0.0, regime, rdesc
    T = math.copysign(min(100.0, abs(raw) / maxw * 100.0), raw)
    return round(T, 1), regime, rdesc


def compute_T_subfactors(df, cfg=DEFAULT_CONFIG, group=None, symbol=None, feat_mgr=None):
    """从 K 线提取 T 维度的 3 个簇子因子（趋势/均值回归/季节性）。
    返回 (T_trend, T_mean, T_seasonal, regime, rdesc)
    每个子因子范围 [-100, 100]，独立归一化。"""
    if df is None or len(df) < 60:
        return 0.0, 0.0, 0.0, "未知", "数据不足"
    regime_rp = regime_params_for(symbol, cfg, feat_mgr) if symbol else {}
    regime, rdesc = classify_regime(df, regime_rp)
    dc = (cfg or DEFAULT_CONFIG).get("decorrelate", {})
    enabled = bool(dc.get("enabled", True))

    # 各策略信号
    sig = {}
    for name, fn in STRATS.items():
        try:
            s, _ = fn(df)
        except Exception:
            s = 0
        sig[name] = int(s)

    if not enabled:
        # 退化成旧逻辑：trend 拿全部权重，mean/seasonal 给 0
        w = regime_weights(regime)
        score = sum(sig[k] * w[k] for k in STRATS)
        maxw = sum(abs(w[k]) for k in STRATS)
        T = math.copysign(min(100.0, abs(score) / maxw * 100.0), score) if maxw > 0 else 0.0
        return round(T, 1), 0.0, 0.0, regime, rdesc

    # 簇投票（[-1, 1]）
    cluster_vote = {}
    for cname, members in STRAT_CLUSTERS.items():
        votes = [sig[m] for m in members]
        cluster_vote[cname] = sum(votes) / len(votes) if votes else 0.0

    # 每个簇独立归一化到 [-100, 100]
    # cluster_vote 范围 [-1, 1]，直接 ×100
    T_trend = round(cluster_vote.get("trend", 0.0) * 100, 1)
    T_mean = round(cluster_vote.get("mean", 0.0) * 100, 1)
    T_seasonal = round(cluster_vote.get("seasonal", 0.0) * 100, 1)

    return T_trend, T_mean, T_seasonal, regime, rdesc


# ----------------------------------------------------------------------------
# 流水线：F(背景) → T(触发) → C(确认) → 风控
# ----------------------------------------------------------------------------
def precompute_trigger_info(T_arr, F_arr, C_arr, regime_codes, cfg, symbol):
    """向量化预计算触发信息：返回 (dir_T_arr, triggered_arr, bias_G_arr)。
    对应 pipeline 中 direction_mode="threshold" 的默认路径（回测常用）。
    与 pipeline 逐点结果一致（无 HMM/GARCH/sentiment/SR/5m 等 live 专属特性时）。"""
    from strategy_layer import REGIME_CODE_TO_NAME

    n = len(T_arr)
    bs = cfg.get("bias_synthesis", {})
    direction_mode = bs.get("direction_mode", "threshold")
    fc_confirm = float(bs.get("fc_confirm", 25))
    confirm_relief = float(bs.get("confirm_relief", 0.85))
    fc_hard_base = float(bs.get("fc_hard", 25))
    _so = cfg.get("thresholds_by_symbol", {}).get(symbol)
    if _so and _so.get("bias_fc_hard") is not None:
        fc_hard_base = float(_so["bias_fc_hard"])
    _off = bs.get("fc_hard_regime_offset", {"趋势": 0, "波动": 5, "震荡": 10})

    # combine_weights（支持按品种覆盖）
    cw = effective_weights(symbol, cfg)
    w_T, w_F, w_C = cw["T"], cw["F"], cw["C"]

    # bias_G 和 bias_FC
    # bias_FC 用 F/C 的绝对权重（用于确认/硬否决的强度判断），与 combine_weights 一致
    bias_G = w_T * T_arr + w_F * F_arr + w_C * C_arr
    bias_FC = np.round(w_F * F_arr + w_C * C_arr, 1)

    # dir_T_raw = sign(T_D)
    dir_T_raw = np.sign(T_arr).astype(np.int8)

    # direction_mode: threshold → dir_T = dir_T_raw
    if direction_mode == "combined":
        bias_g_min = float(bs.get("bias_g_min", 50))
        direction_alpha = float(bs.get("direction_alpha", 0.5))
        _combined = T_arr + direction_alpha * bias_G
        dir_T = np.where(_combined > 0, 1, np.where(_combined < 0, -1, 0)).astype(np.int8)
        # 当 dir_T_raw == 0 且 abs(bias_G) < bias_g_min 时，dir_T 保持 0
        keep_zero = (dir_T_raw == 0) & (np.abs(bias_G) < bias_g_min)
        dir_T[keep_zero] = 0
    else:
        dir_T = dir_T_raw

    # T_thresh_eff = T_base * rc["T"]（按 regime 变化，P2: 支持 per-symbol 覆盖）
    T_base, _ = effective_params(symbol, cfg)
    rc_all = effective_regime_coef(symbol, cfg)

    # 按 regime code 计算 T_thresh_eff 和 fc_hard
    T_thresh_eff = np.full(n, T_base, dtype=np.float64)
    fc_hard_arr = np.full(n, fc_hard_base, dtype=np.float64)
    for code, name in REGIME_CODE_TO_NAME.items():
        mask = regime_codes == code
        rc = rc_all.get(name, rc_all["波动"])
        T_thresh_eff[mask] = T_base * rc["T"]
        fc_hard_arr[mask] = fc_hard_base + _off.get(name, 0)

    # 硬否决：|bias_FC| >= fc_hard 且 bias_FC 与 dir_T 反向
    with np.errstate(invalid="ignore"):
        fc_sign = np.sign(bias_FC)
        hard_veto = (np.abs(bias_FC) >= fc_hard_arr) & (fc_sign != dir_T) & (dir_T != 0)

    # triggered 计算（threshold 模式）
    triggered = np.zeros(n, dtype=bool)
    dir_nonzero = (dir_T != 0) & ~hard_veto

    if direction_mode == "combined":
        bias_g_min = float(bs.get("bias_g_min", 50))
        cond1 = np.abs(T_arr) >= T_thresh_eff
        cond2 = (np.abs(bias_G) >= bias_g_min) & (np.sign(bias_G) == dir_T)
        triggered[dir_nonzero] = (cond1 | cond2)[dir_nonzero]
    else:
        # same_dir: bias_G 与 dir_T 同向（或 bias_G 近似 0）
        same_dir = ((bias_G >= 0) & (dir_T > 0)) | ((bias_G <= 0) & (dir_T < 0)) | (np.abs(bias_G) < 1e-6)
        # fc_align: bias_FC 与 dir_T 同向且 |bias_FC| >= fc_confirm
        fc_align = (fc_sign == dir_T) & (np.abs(bias_FC) >= fc_confirm)
        _thr = T_thresh_eff * np.where(fc_align, confirm_relief, 1.0)
        cond = same_dir & (np.abs(T_arr) >= _thr)
        triggered[dir_nonzero] = cond[dir_nonzero]

    return dir_T, triggered, bias_G


def combine_bias(F, T, C, cfg=DEFAULT_CONFIG, symbol=None):
    """背景偏置合成（§1.5）。F/C 中性时退化为 T 主导。
    P2-④：权重改读 cfg["combine_weights"]（默认 0.6/0.25/0.15，与原硬编码一致），
    使 OOS harness 可扫参验证，向后兼容。
    P0 优化（2026-08-29）：支持按品种覆盖权重（thresholds_by_symbol[sym].combine_weights）。
    P0 基本面增强（2026-08-29）：支持分板块权重（sector_combine_weights）。
    #10 GA 权重优化（2026-08-26）：若 ga_weights_cache.json 有该品种的优化权重，
    则用 GA 权重覆盖默认权重（仅 live 路径；回测走 cfg 不受影响）。"""
    # 优先级：GA 覆盖 > 按品种覆盖 > 分板块 > 全局默认
    w = None
    # #10: GA 权重覆盖（live 路径，set_ga_weights_for_symbol 设置当前品种权重）
    _ga_current = getattr(combine_bias, "_ga_current", None)
    if _ga_current:
        ga_w = _ga_current.get("best_weights", {})
        base = ga_w.get("base", {})
        if base:
            w = base
    # 按品种覆盖（P0 优化）
    if w is None and symbol:
        sym_cfg = (cfg or DEFAULT_CONFIG).get("thresholds_by_symbol", {}).get(symbol, {})
        override = sym_cfg.get("combine_weights")
        if override:
            default = (cfg or DEFAULT_CONFIG).get("combine_weights", {"T": 0.6, "F": 0.25, "C": 0.15})
            t = float(override.get("T", default["T"]))
            f = float(override.get("F", default["F"]))
            c = float(override.get("C", default["C"]))
            s = t + f + c
            if s > 0:
                w = {"T": t / s, "F": f / s, "C": c / s}
    # 分板块权重（P0 基本面增强）
    if w is None and symbol:
        sector = SYMBOLS.get(symbol, {}).get("group", "其他")
        sector_w = (cfg or DEFAULT_CONFIG).get("sector_combine_weights", {}).get(sector)
        if sector_w:
            w = sector_w
    # 全局默认
    if w is None:
        w = (cfg or DEFAULT_CONFIG).get("combine_weights", {"T": 0.6, "F": 0.25, "C": 0.15})
    return round(w["T"] * T + w["F"] * F + w["C"] * C, 1)


# #10 GA 权重缓存（live runner 启动时加载 ga_weights_cache.json）
def _load_ga_weights():
    """加载 GA 权重缓存到 combine_bias._ga_cache。"""
    try:
        _path = os.path.join(HERE, "ga_weights_cache.json")
        if os.path.exists(_path):
            with open(_path, encoding="utf-8") as f:
                _data = json.load(f)
            combine_bias._ga_cache = _data
            return _data
    except Exception:
        pass
    return None


def set_ga_weights_for_symbol(symbol):
    """为指定品种设置 GA 优化权重（live runner 每轮 evaluate 调用）。
    自动检测 ga_weights_cache.json 文件变化并重新加载。"""
    _path = os.path.join(HERE, "ga_weights_cache.json")
    _mtime = os.path.getmtime(_path) if os.path.exists(_path) else 0
    _last_mtime = getattr(combine_bias, "_ga_mtime", 0)
    if _mtime > _last_mtime or not hasattr(combine_bias, "_ga_cache"):
        _load_ga_weights()
        combine_bias._ga_mtime = _mtime
    _cache = getattr(combine_bias, "_ga_cache", {})
    if symbol in _cache:
        combine_bias._ga_current = _cache[symbol]
    else:
        combine_bias._ga_current = None


def effective_params(symbol, cfg=DEFAULT_CONFIG):
    """解析某品种生效的阈值参数（per-symbol 覆盖优先，否则回退 group + 全局）。
    返回 (T_thresh_base, bias_hard_dict)。bias_hard_dict: 趋势=base, 波动=base+5, 震荡=base+10。"""
    sym = cfg.get("thresholds_by_symbol", {}).get(symbol)
    th = cfg["thresholds"][SYMBOLS[symbol]["group"]]
    if sym and "T_thresh" in sym:
        T_base = sym["T_thresh"]
        bh_base = sym.get("bias_hard_base", th.get("bias_hard", 60))
    else:
        T_base = th["T_thresh"]
        bh_base = th.get("bias_hard", 60)  # 用 group 级 bias_hard
    bhd = {"趋势": bh_base, "波动": bh_base + 5, "震荡": bh_base + 10}
    return T_base, bhd


def effective_weights(symbol, cfg=DEFAULT_CONFIG):
    """解析某品种生效的 F/T/C 合成权重。
    优先级：品种覆盖 > 分板块 > 全局默认。
    返回 dict: {"T", "F", "C"}，三者之和 = 1.0。"""
    default = cfg.get("combine_weights", {"T": 0.6, "F": 0.25, "C": 0.15})
    sym = cfg.get("thresholds_by_symbol", {}).get(symbol, {})
    override = sym.get("combine_weights")
    if override:
        # 归一化确保和为 1
        t = float(override.get("T", default["T"]))
        f = float(override.get("F", default["F"]))
        c = float(override.get("C", default["C"]))
        s = t + f + c
        if s > 0:
            return {"T": t / s, "F": f / s, "C": c / s}
    # 分板块权重
    sector = SYMBOLS.get(symbol, {}).get("group", "其他")
    sector_w = cfg.get("sector_combine_weights", {}).get(sector)
    if sector_w:
        return {"T": float(sector_w["T"]), "F": float(sector_w["F"]), "C": float(sector_w["C"])}
    return {"T": float(default["T"]), "F": float(default["F"]), "C": float(default["C"])}


def effective_regime_coef(symbol, cfg=DEFAULT_CONFIG):
    """解析某品种生效的 regime 风控系数（per-symbol 覆盖优先，否则回退全局）。
    返回完整的 regime_coef dict: {regime_name: {"T", "conv", "stop", "cooldown"}}。

    覆盖层级：全局 regime_coef → per_symbol_regime_coef[symbol]（后者优先，逐键覆盖）。
    未在 per-symbol 中定义的 regime 或键，沿用全局值。
    """
    base = dict(cfg.get("regime_coef", {}))
    # 深拷贝每个 regime 的 dict，避免修改共享引用
    result = {rg: dict(params) for rg, params in base.items()}

    sym_override = cfg.get("per_symbol_regime_coef", {}).get(symbol, {})
    if not sym_override:
        return result

    for rg, overrides in sym_override.items():
        if rg not in result:
            # 新 regime（如"过渡"），以波动 regime 为基准再覆盖
            base_rg = dict(result.get("波动", {"T": 1.0, "conv": 1.0, "stop": 1.2, "cooldown": 300}))
            result[rg] = base_rg
        for k, v in overrides.items():
            result[rg][k] = v
    return result


def regime_params_for(symbol, cfg=DEFAULT_CONFIG, feat_mgr=None):
    """解析某品种生效的 regime 分类阈值（P-F，2026-08-14）。

    覆盖层级：default → by_group[group] → by_symbol[sym]（后者优先）。
    返回 dict（含 atr_thresh/flat_dev/flat_atr/trend_slope/trend_dev），可直接喂 classify_regime(params=...)。

    开关优先级（从高到低）：
      1. feature_flags.market_state_engine （特性开关，新）
      2. regime_params.enabled （trade_config 旧位置，向后兼容）
      3. 默认启用（True）
    任一关闭 → 回落 default（=旧全局阈值行为，便于 A-B 对照）。
    """
    rp = (cfg or DEFAULT_CONFIG).get("regime_params", {})

    # 优先级1：特性开关（新）
    enabled = None
    if feat_mgr is not None:
        try:
            enabled = feat_mgr.is_enabled("market_state_engine")
        except Exception:
            enabled = None

    # 优先级2：旧配置（向后兼容）
    if enabled is None:
        enabled = bool(rp.get("enabled", True))

    if not enabled:
        # 关闭分品种：全部回落 default（即 classify_regime 旧全局行为）
        return dict(
            rp.get(
                "default",
                {"atr_thresh": 0.025, "flat_dev": 0.008, "flat_atr": 0.012, "trend_slope": 0.003, "trend_dev": 0.010},
            )
        )
    default = rp.get(
        "default", {"atr_thresh": 0.025, "flat_dev": 0.008, "flat_atr": 0.012, "trend_slope": 0.003, "trend_dev": 0.010}
    )
    merged = dict(default)
    grp = SYMBOLS.get(symbol, {}).get("group")
    by_group = rp.get("by_group", {})
    if grp and grp in by_group:
        merged.update(by_group[grp])
    by_symbol = rp.get("by_symbol", {})
    if symbol in by_symbol:
        merged.update(by_symbol[symbol])
    return merged


# ── 流动性敏感滑点（P2）────────────────────────────────────────────────────
# 原滑点 slip_pts=1 是全局固定值，对所有品种一视同仁，显然不合理：
#   螺纹/玻璃等超流动品种 1 跳即可成交；生猪/苹果/工业硅等低流动品种盘口薄、
#   大单冲击成本高，固定 1 点会严重低估真实损耗 → 回测期望R 虚高。
# 改为「按品种流动性分级」的动态滑点：
#   · 优先取 contract_specs[sym]["slip"] 逐合约微调（未来逐合约校准入口）
#   · 否则查 LIQUIDITY_SLIP 流动性分级表
#   · 再否则回退全局 risk_gate.slip_pts
# 分级依据：主力合约近期日均成交/持仓规模（行业常识），可分 1.0 / 1.5 / 2.0 三档。
# 维护：recompute_liquidity_tiers() 可按近期真实成交量重排档位（非热路径，按需手动跑）。
LIQUIDITY_SLIP = {
    # ── A 档·超流动（1.0 点）── 2026-08-13 按近60日日均成交额重排(recompute_liquidity_tiers)
    #   注意：键名必须与 SYMBOLS 主键严格同大小写(全小写)，否则 get_slip_pts  miss → 回退全局1.0
    "au": 1.0,
    "ag": 1.0,
    "sn": 1.0,
    "jm": 1.0,
    "p": 1.0,
    "ru": 1.0,
    "cu": 1.0,
    "lc": 1.0,
    "sc": 1.0,
    "cf": 1.0,
    "ni": 1.0,
    "m": 1.0,
    "pp": 1.0,
    "lh": 1.0,
    "ta": 1.0,
    "oi": 1.0,
    "ma": 1.0,
    "v": 1.0,
    "al": 1.0,
    "jd": 1.0,
    "sh": 1.0,
    "y": 1.0,
    "bu": 1.0,
    "rb": 1.0,
    # ── B 档·中流动（1.5 点）── 成交中等，盘口适中
    "fg": 1.5,
    "l": 1.5,
    "sr": 1.5,
    "eg": 1.5,
    "eb": 1.5,
    "i": 1.5,
    "ao": 1.5,
    "rm": 1.5,
    "fu": 1.5,
    "sa": 1.5,
    "hc": 1.5,
    "c": 1.5,
    "zn": 1.5,
    "ss": 1.5,
    "sp": 1.5,
    "a": 1.5,
    "pg": 1.5,
    "si": 1.5,
    "ur": 1.5,
    # ── C 档·低流动（2.0 点）── 盘口薄/大合约/远月，冲击成本高
    "ap": 2.0,
    "j": 2.0,
    "pf": 2.0,
    "px": 2.0,
    "b": 2.0,
    "cs": 2.0,
    "pk": 2.0,
    "pr": 2.0,
    "ec": 2.0,
    "rr": 2.0,
}


def get_slip_pts(symbol, cfg=DEFAULT_CONFIG):
    """流动性敏感滑点（单位：最小价格变动点数 × 档位）。
    返回该品种单腿成交应计的滑点（点数）。实盘下单双向各滑一次。
    查表对大小写不敏感（SYMBOLS 主键大小写不统一：J/JM/FG/SA 大写，其余小写）。"""
    sp = cfg.get("contract_specs", {}).get(symbol, {})
    if sp.get("slip") is not None:  # ① 逐合约微调优先
        return float(sp["slip"])
    key = symbol.lower()
    if key in LIQUIDITY_SLIP:  # ② 流动性分级表（键统一小写）
        return LIQUIDITY_SLIP[key]
    if symbol in LIQUIDITY_SLIP:  # ② 兜底原大小写
        return LIQUIDITY_SLIP[symbol]
    return float(cfg.get("risk_gate", {}).get("slip_pts", 1))  # ③ 全局兜底


def recompute_liquidity_tiers(tail: int = 60, top_n: int = 12):
    """按近期主力日均成交额重排流动性档位（非热路径，按需手动跑）。
    成交额 = 日均成交量 × 近期均价 × 合约乘数。输出建议分级，供人工更新 LIQUIDITY_SLIP。"""
    rows = []
    for sym in SYMBOLS:
        try:
            df = load_daily(sym)
            if df is None or len(df) < tail:
                continue
            win = df.tail(tail)
            vol = float(win["volume"].mean()) if "volume" in win else 0.0
            px = float(win["close"].iloc[-1])
            mult = cfg_mult = DEFAULT_CONFIG["contract_specs"].get(sym, _FALLBACK_SPEC)["multiplier"]
            turnover = vol * px * mult
            rows.append((sym, turnover))
        except Exception:
            continue
    rows.sort(key=lambda x: -x[1])
    n = len(rows)
    out = []
    for i, (sym, tv) in enumerate(rows):
        frac = i / n if n else 0
        tier = 1.0 if frac < 0.45 else (1.5 if frac < 0.80 else 2.0)
        out.append((sym, round(tv / 1e8, 2), tier))
    print(f"### 流动性档位建议 (tail={tail}, 按日均成交额分位) ###")
    for sym, tv, tier in out:
        mark = " ◀当前不同" if LIQUIDITY_SLIP.get(sym.lower(), LIQUIDITY_SLIP.get(sym)) != tier else ""
        print(f"  {sym:>4}: 日均成交额≈{tv}亿  → 建议 slip={tier}{mark}")
    return out


def pipeline(
    symbol,
    df_daily,
    df_5m=None,
    cfg=DEFAULT_CONFIG,
    corr_hist=None,
    date=None,
    c_override=None,
    ablate=None,
    F_override=None,
    hmm_label=None,
    macro_label=None,
    garch_label=None,
    gbm_garch=None,
    risk_state=None,
    feat_mgr=None,
    sentiment_label=None,
    sr_result=None,
    _precalc=None,
):
    """算三维修分 + 流水线合成，返回触发判定与中间量。
    date: 当前交易日(YYYYMMDD)，用于查真实基本面 F（缺失→中性）。
    c_override: 实盘可传实时 C_flow 评分覆盖 score_C（默认 None=用 score_C）。
    ablate: 模型健康分解用——"F"/"C"/"T" 之一置中性（留一维度消融），隔离该维边际贡献。
            注意 T 消融仅从 bias_G 移除 T 确认、保留 dir_T 触发（否则零交易无意义）。
    hmm_label: #7 HMM 市场状态标签（live 专属）。传入则在 T_thresh_eff 上按市况调制触发阈值
             （trend_up/down×0.90、choppy×1.15、high_vol×1.25）。默认 None，回测三处调用不传参，
             HMM 永不进入回测路径（无前视偏差红线）。"""
    group = SYMBOLS.get(symbol, {}).get("group")  # 用于 P-D seasonal_boost 分组加权

    # P1-16: 实时风控前置检查 —— live 模式下若已锁定/熔断，直接返回空信号
    _locked, _lock_reason = _is_risk_locked(risk_state)
    if _locked:
        return {
            "F": 0.0,
            "T_D": 0.0,
            "T_5m": 0.0,
            "C": 0.0,
            "bias_G": 0.0,
            "bias_FC": 0.0,
            "dir_T": 0,
            "dir_T_raw": 0,
            "regime": "",
            "rdesc": "",
            "regime_hmm": None,
            "garch_label": None,
            "gbm_garch": None,
            "risk_scale": 1.0,
            "macro_bias": None,
            "triggered": False,
            "T_thresh_eff": 0,
            "T_thresh_used": 0,
            "conv": "风控锁定",
            "used_5m": False,
            "hard_veto": True,
            "bs_mode": "",
            "corr_action": "",
            "risk_blocked": True,
            "risk_block_reason": _lock_reason,
            "sentiment_label": None,
            "sr_quality_note": "",
        }

    T_D, regime, rdesc = compute_T(
        df_daily,
        cfg,
        group,
        symbol=symbol,
        feat_mgr=feat_mgr,
        _c=_precalc.get("_c") if _precalc else None,
        _h=_precalc.get("_h") if _precalc else None,
        _l=_precalc.get("_l") if _precalc else None,
        _m=_precalc.get("_m") if _precalc else None,
        _atr14=_precalc.get("_atr14") if _precalc else None,
        _rets=_precalc.get("_rets") if _precalc else None,
        _sma5=_precalc.get("_sma5") if _precalc else None,
        _sma20=_precalc.get("_sma20") if _precalc else None,
        _sma60=_precalc.get("_sma60") if _precalc else None,
        _sma5_prev=_precalc.get("_sma5_prev") if _precalc else None,
        _sma20_prev=_precalc.get("_sma20_prev") if _precalc else None,
        _sma20_slope_prev=_precalc.get("_sma20_slope_prev") if _precalc else None,
        _rsi14=_precalc.get("_rsi14") if _precalc else None,
        _std20=_precalc.get("_std20") if _precalc else None,
        _seasonal_cnt=_precalc.get("_seasonal_cnt") if _precalc else None,
        _seasonal_sum=_precalc.get("_seasonal_sum") if _precalc else None,
        _seasonal_sumsq=_precalc.get("_seasonal_sumsq") if _precalc else None,
        _signal_arrays=_precalc.get("_signal_arrays") if _precalc else None,
        _sig_idx=_precalc.get("_sig_idx") if _precalc else None,
        _T_array=_precalc.get("_T_array") if _precalc else None,
        _regime_code=_precalc.get("_regime_code") if _precalc else None,
    )
    if date is None:
        date = df_daily.index[-1].strftime("%Y%m%d") if len(df_daily) else None
    F = F_override if F_override is not None else score_F(symbol, date)
    # C 值快速路径：预计算值直接使用，省 score_C 内 history 遍历查找
    if _precalc and _precalc.get("_C_val") is not None and c_override is None:
        C = _precalc["_C_val"]
    else:
        C = c_override if c_override is not None else score_C(symbol, date)
    if ablate == "F":
        F = 0.0
    elif ablate == "C":
        C = 0.0
    elif ablate == "T":
        T_D = 0.0
    bias_G = combine_bias(F, T_D, C, cfg, symbol=symbol)

    # ── #11 GA 6 因子挖掘模式（可选，默认关闭） ──
    # 子因子：T_trend / T_mean / T_seasonal / F_basis / F_seasonal / C
    # 可选扩展：SR_breakout（突破强度因子）、V_vol（波动率因子）
    # 配置了 subfactor_weights 时用子因子加权覆盖原 bias_G
    _sf_w = (cfg or {}).get("subfactor_weights", {})
    if _sf_w:
        try:
            # 防前视偏差：walk_forward 模式下用 _sig_idx 截取到当前 bar 的历史数据
            # 否则直接用 df_daily（live 模式 / 单根调用 = 最后一根就是当前）
            _sf_idx = _precalc.get("_sig_idx") if _precalc else None
            if _sf_idx is not None and isinstance(df_daily, pd.DataFrame):
                _df_sf = df_daily.iloc[: _sf_idx + 1]
            else:
                _df_sf = df_daily
            _t_trend, _t_mean, _t_seas, _, _ = compute_T_subfactors(
                _df_sf, cfg, group, symbol=symbol, feat_mgr=feat_mgr
            )
            import fundamental_feed as _ff

            _f_basis, _f_seas = _ff.compute_F_subfactors(symbol, date)
            # ablate 兼容
            if ablate == "T":
                _t_trend, _t_mean, _t_seas = 0.0, 0.0, 0.0
            if ablate == "F":
                _f_basis, _f_seas = 0.0, 0.0
            if ablate == "C":
                C_sf = 0.0
            else:
                C_sf = C

            # SR_breakout 因子（可选）：近压力位 → 正（即将突破），近支撑位 → 负（即将跌破）
            _sr_breakout = 0.0
            if "SR_breakout" in _sf_w:
                try:
                    import sr_analyzer as _sra

                    _sr_res = _sra.find_sr_levels(_df_sf, symbol=symbol)
                    if _sr_res and _sr_res.get("levels"):
                        _cur = float(_df_sf["close"].iloc[-1])
                        _nr = _sr_res.get("nearest_resistance")
                        _ns = _sr_res.get("nearest_support")
                        _rd = _nr["distance_pct"] if _nr else 999.0
                        _sd = _ns["distance_pct"] if _ns else 999.0
                        # 压力近 = 正分（突破潜力），支撑近 = 负分（跌破风险）
                        _res_score = max(0.0, 1.0 - _rd / 5.0) * 100
                        _sup_score = max(0.0, 1.0 - _sd / 5.0) * 100
                        _sr_breakout = round(max(-100.0, min(100.0, _res_score - _sup_score)), 1)
                except Exception:
                    _sr_breakout = 0.0

            # V_vol 波动率因子（可选）：低波动率+波动率下降 → 正分（平静=利多）
            # 注意：不同板块方向可能不同，GA 会通过权重符号自动学习
            _v_vol = 0.0
            if "V_vol" in _sf_w:
                try:
                    _close = _df_sf["close"].astype(float).values
                    if len(_close) >= 40:
                        _ret = np.diff(_close[-30:]) / (_close[-30:-1] + 1e-8)  # 29日收益率
                        _vol = float(np.std(_ret) * 100)  # 20日波动率（%）
                        # 波动率分位：用过去 120 根做参考
                        _long_ret = np.diff(_close[-120:]) / (_close[-120:-1] + 1e-8)
                        _long_vols = np.array(
                            [float(np.std(_long_ret[max(0, i - 20) : i + 1]) * 100) for i in range(19, len(_long_ret))]
                        )
                        if len(_long_vols) > 10:
                            _vol_pct = float(np.mean(_long_vols < _vol))  # 当前波动率分位
                            # 波动率变化：近 5 日平均 vs 近 20 日平均
                            _vol_5 = float(np.std(_ret[-5:]) * 100) if len(_ret) >= 5 else _vol
                            _vol_chg = (_vol_5 - _vol) / (_vol + 1e-8)  # 变化率
                            # 合成：低波动分位（低波=正）+ 波动率下降（降波=正）
                            _score = (1.0 - 2 * _vol_pct) * 50 + (-_vol_chg * 200)
                            _v_vol = round(max(-100.0, min(100.0, _score)), 1)
                except Exception:
                    _v_vol = 0.0

            # Vol_vol 成交量因子（可选）：量价配合/量能异动
            # 放量上涨 = 正，缩量上涨 = 负（假突破）；放量下跌 = 负，缩量下跌 = 正（假跌破）
            _vol_vol = 0.0
            if "Vol_vol" in _sf_w:
                try:
                    _close_arr = _df_sf["close"].astype(float).values
                    _vol_arr = _df_sf["volume"].astype(float).values
                    if len(_close_arr) >= 30 and len(_vol_arr) >= 30:
                        _c20 = _close_arr[-20:]
                        _v20 = _vol_arr[-20:]
                        # 成交量分位（相对过去 60 日）
                        if len(_vol_arr) >= 60:
                            _v_long = _vol_arr[-60:]
                            _v_pct = float(np.mean(_v_long < _v20[-1]))
                        else:
                            _v_pct = 0.5
                        # 量价配合：价格方向 × 成交量方向
                        _price_chg = (_c20[-1] - _c20[-6]) / (_c20[-6] + 1e-8)  # 5日涨跌
                        _vol_chg = (_v20[-1] - np.mean(_v20[-10:-1])) / (np.mean(_v20[-10:-1]) + 1e-8)
                        # 放量同向 = 正（趋势确认），放量反向 = 负（衰竭/反转）
                        _direction = 1.0 if _price_chg > 0 else (-1.0 if _price_chg < 0 else 0.0)
                        _score = _direction * _vol_chg * 80 + (_v_pct - 0.5) * 20
                        _vol_vol = round(max(-100.0, min(100.0, _score)), 1)
                except Exception:
                    _vol_vol = 0.0

            # OI_int 持仓量因子（可选）：持仓变化反映资金流入流出
            # 持仓增加+上涨 = 正（新资金入场），持仓减少+下跌 = 负（资金离场）
            _oi_int = 0.0
            if "OI_int" in _sf_w:
                try:
                    _close_arr = _df_sf["close"].astype(float).values
                    if "open_interest" in _df_sf.columns:
                        _oi = _df_sf["open_interest"].astype(float).values
                    elif "oi" in _df_sf.columns:
                        _oi = _df_sf["oi"].astype(float).values
                    else:
                        _oi = None
                    if _oi is not None and len(_oi) >= 20 and len(_close_arr) >= 20:
                        # 持仓变化率（5日平均）
                        if len(_oi) >= 10:
                            _oi_chg = (_oi[-1] - np.mean(_oi[-10:-1])) / (np.mean(_oi[-10:-1]) + 1e-8)
                        else:
                            _oi_chg = 0.0
                        # 价格方向
                        _price_chg = (_close_arr[-1] - _close_arr[-6]) / (_close_arr[-6] + 1e-8)
                        _direction = 1.0 if _price_chg > 0 else (-1.0 if _price_chg < 0 else 0.0)
                        # 量价配合：增仓同向 = 正（资金推动趋势）
                        _score = _direction * _oi_chg * 150
                        _oi_int = round(max(-100.0, min(100.0, _score)), 1)
                except Exception:
                    _oi_int = 0.0

            # Inv_stock 库存因子（可选）：库存变化反映供需
            # 库存下降 = 正（需求旺/供应紧），库存上升 = 负（供应过剩）
            _inv_stock = 0.0
            if "Inv_stock" in _sf_w:
                try:
                    import fundamental_feed as _ff_inv

                    _inv_rate = _ff_inv.stock_change_on(symbol, date)
                    if _inv_rate is not None:
                        # 库存下降 → 正分，库存上升 → 负分
                        # 假设年化 10% 的库存变化对应 100 分
                        _score = -_inv_rate / 0.10 * 100
                        _inv_stock = round(max(-100.0, min(100.0, _score)), 1)
                except Exception:
                    _inv_stock = 0.0

            _bias = (
                _sf_w.get("T_trend", 0) * _t_trend
                + _sf_w.get("T_mean", 0) * _t_mean
                + _sf_w.get("T_seasonal", 0) * _t_seas
                + _sf_w.get("F_basis", 0) * _f_basis
                + _sf_w.get("F_seasonal", 0) * _f_seas
                + _sf_w.get("C", 0) * C_sf
                + _sf_w.get("SR_breakout", 0) * _sr_breakout
                + _sf_w.get("V_vol", 0) * _v_vol
                + _sf_w.get("Vol_vol", 0) * _vol_vol
                + _sf_w.get("OI_int", 0) * _oi_int
                + _sf_w.get("Inv_stock", 0) * _inv_stock
            )
            bias_G = round(max(-100.0, min(100.0, _bias)), 1)
        except Exception:
            pass  # 子因子计算失败时回退到原 bias_G

    # ── #6 跨资产宏观语境调制（live 专属，回测 macro_label=None 不进，零前视污染）──
    # macro_bias∈[-1,1]（股/债/汇跨资产语境）。bias_G 量程≈[-100,100]（0.6*T+0.25*F+0.15*C），
    # 故按量程比例温和调制：调制量 = macro_bias * _MACRO_BIAS_SCALE（约12%量程的宏观偏见微调，不直接改方向）。
    _MACRO_BIAS_SCALE = 12.0
    macro_bias_applied = None
    if macro_label is not None:
        macro_bias_applied = float(macro_label)
        bias_G = bias_G + macro_bias_applied * _MACRO_BIAS_SCALE

    # 小周期触发：有 5m 用 5m，否则 T@D 降频代理
    if df_5m is not None and len(df_5m) >= 60:
        T_5m, _, _ = compute_T(df_5m, cfg, group, symbol=symbol, feat_mgr=feat_mgr)
        used_5m = True
    else:
        T_5m, used_5m = T_D, False
    dir_T_raw = 1 if T_5m > 0 else (-1 if T_5m < 0 else 0)

    rc_all = effective_regime_coef(symbol, cfg)
    rc = rc_all.get(regime, rc_all["波动"])
    T_base, bh_dict = effective_params(symbol, cfg)
    T_thresh_eff = T_base * rc["T"]

    # ── #7 HMM 市场状态调制（live 专属；回测默认 hmm_label=None 不进）──
    if hmm_label is not None:
        _hmm_mult = {"trend_up": 0.90, "trend_down": 0.90, "choppy": 1.15, "high_vol": 1.25}
        T_thresh_eff = round(T_thresh_eff * _hmm_mult.get(hmm_label, 1.0), 1)

    # ── #7 (GBM/GARCH) 波动率动力学调制（live 专属；回测默认 garch_label=None 不进）──
    # 比 HMM 更轻（0.97~1.12），避免两路调制叠加过度抑制触发。
    if garch_label is not None:
        _g_mult = {"low": 0.97, "normal": 1.00, "high": 1.06, "extreme": 1.12}
        T_thresh_eff = round(T_thresh_eff * _g_mult.get(garch_label, 1.0), 1)

    # ── #8 市场情绪调制（live 专属；回测默认 sentiment_label=None 不进）──
    # 方向感知：极端贪婪时做多门槛↑（防追涨）、做空门槛↓；极端恐惧反之。
    # sentiment_label 是 sentiment_engine.compute() 返回的 band 字符串。
    if sentiment_label is not None:
        import sentiment_engine as _se

        _s_mult = _se.get_thr_mult(dir_T_raw)  # dir_T_raw 此时已由 T_5m 决定
        T_thresh_eff = round(T_thresh_eff * _s_mult, 1)

    # ── #9 支撑压力位信号质量过滤（live 专属；回测默认 sr_result=None 不进）──
    # 价格在支撑位附近做多 / 压力位附近做空 → 信号质量提升（T 阈值降低）
    # 价格在压力位附近做多 / 支撑位附近做空 → 信号质量降低（T 阈值升高）
    sr_quality_note = ""
    if sr_result is not None:
        import sr_analyzer as _sra

        _sr_boost, _sr_reason = _sra.signal_quality_boost(sr_result, dir_T_raw)
        if _sr_boost != 0.0:
            T_thresh_eff = round(T_thresh_eff * (1.0 - _sr_boost), 1)
            sr_quality_note = _sr_reason

    # ── P-B / P-C（2026-08-14）：让 F/C 真正参与方向/触发决策 ──
    bs = cfg.get("bias_synthesis", {})
    direction_mode = bs.get("direction_mode", "threshold")
    direction_alpha = float(bs.get("direction_alpha", 0.5))
    fc_confirm = float(bs.get("fc_confirm", 25))
    confirm_relief = float(bs.get("confirm_relief", 0.85))
    fc_hard = float(bs.get("fc_hard", 25))
    _so = cfg.get("thresholds_by_symbol", {}).get(symbol)
    if _so and _so.get("bias_fc_hard") is not None:
        fc_hard = float(_so["bias_fc_hard"])
    _off = bs.get("fc_hard_regime_offset", {"趋势": 0, "波动": 5, "震荡": 10})
    fc_hard = fc_hard + _off.get(regime, 0)
    bias_g_min = float(bs.get("bias_g_min", 50))

    # 非技术面背景偏置（P-B/P-C 仅看 F/C，避免 T 自我否决）
    # 用 effective_weights 中的 F/C 权重，与 bias_G 保持一致
    _ew = effective_weights(symbol, cfg)
    bias_FC = round(_ew["F"] * F + _ew["C"] * C, 1)

    # 方向（P-B）：threshold 模式方向仍由 T_5m 决定；combined 模式 F/C 可翻转方向
    if direction_mode == "combined" and (dir_T_raw != 0 or abs(bias_G) >= bias_g_min):
        _combined = T_5m + direction_alpha * bias_G
        dir_T = 1 if _combined > 0 else (-1 if _combined < 0 else 0)
    else:
        dir_T = dir_T_raw

    triggered = False
    hard_veto = False
    hard_veto_reason = ""
    sentiment_filter_note = ""
    _thr = T_thresh_eff  # P0-1 fix: ensure _thr always defined
    if dir_T != 0:
        # P-C：硬否决基于 F/C 反向强度（bias_FC 上限 40，阈值 25 可达；原 bias_G≥60 几乎不可达）
        hard_veto = (abs(bias_FC) >= fc_hard) and (math.copysign(1, bias_FC) != dir_T)
        if hard_veto:
            hard_veto_reason = f"F/C反向硬否决(|bias_FC|={abs(bias_FC):.1f}≥{fc_hard:.0f})"

        # #8 情绪硬过滤：极端情绪期直接禁止某个方向
        if not hard_veto and sentiment_label is not None:
            import sentiment_engine as _se

            _sf, _sf_reason = _se.is_hard_filtered(sentiment_label, dir_T)
            if _sf:
                hard_veto = True
                hard_veto_reason = _sf_reason
                sentiment_filter_note = _sf_reason
        if not hard_veto:
            if direction_mode == "combined":
                # F/C 可定方向：T_5m 强 或 bias_G 强同向 → 触发
                if abs(T_5m) >= T_thresh_eff or (abs(bias_G) >= bias_g_min and math.copysign(1, bias_G) == dir_T):
                    triggered = True
            else:
                same_dir = (bias_G >= 0 and dir_T > 0) or (bias_G <= 0 and dir_T < 0) or (abs(bias_G) < 1e-6)
                # P-B：F/C 强同向确认 → 降 T 阈值（正向加成）
                fc_align = (math.copysign(1, bias_FC) == dir_T) and (abs(bias_FC) >= fc_confirm)
                _thr = T_thresh_eff * (confirm_relief if fc_align else 1.0)
                if same_dir and abs(T_5m) >= _thr:
                    triggered = True
    else:
        _thr = T_thresh_eff

    # 资金确认（§0）：同向加成 / 反向打折
    if C == 0:
        conv = "无C确认(中性)"
    elif math.copysign(1, C) == dir_T:
        conv = "资金确认(同向加成)"
    else:
        conv = "资金反向(打折)"

    # 相关性闸门（§1.4）：滚动 corr(T,C) 降权低置信维
    # P1-2 深度重审加固：原修复只写文本描述但未实际降权，属空转。
    # 现改为：|corr(T,C)|>gate 时，把 T 和 C 中绝对值较小的一维强制降为中性(0)，
    # 避免冗余维度在同向/反向贡献中加权，提高信号熵纯度。
    corr_action = "无冗余,正常计权"
    if corr_hist is not None and len(corr_hist) >= 10:
        arr = np.array(corr_hist)
        if np.ptp(arr.std(0)) > 0:
            ctc = np.corrcoef(arr[:, 0], arr[:, 1])[0, 1]
            if not math.isnan(ctc) and abs(ctc) > cfg["corr_gate"]:
                # 选取绝对值较小的一维降权至 0（保留更强的一维）
                if abs(T_D) <= abs(C):
                    T_D_orig = T_D
                    T_D = 0.0
                    corr_action = f"corr(T,C)={ctc:.2f}>gate,降权T(|T|={abs(T_D_orig):.1f}≤|C|={abs(C):.1f})"
                else:
                    C_orig = C
                    C = 0.0
                    corr_action = f"corr(T,C)={ctc:.2f}>gate,降权C(|C|={abs(C_orig):.1f}<|T|={abs(T_D):.1f})"

    return {
        "F": F,
        "T_D": T_D,
        "T_5m": T_5m,
        "C": C,
        "bias_G": bias_G,
        "bias_FC": bias_FC,
        "dir_T": dir_T,
        "dir_T_raw": dir_T_raw,
        "regime": regime,
        "rdesc": rdesc,
        "regime_hmm": hmm_label,
        "garch_label": garch_label,
        "gbm_garch": gbm_garch,
        "risk_scale": (gbm_garch or {}).get("risk_scale", 1.0),
        "macro_bias": macro_bias_applied,
        "triggered": triggered,
        "T_thresh_eff": round(T_thresh_eff, 1),
        "T_thresh_used": round(_thr, 1),
        "conv": conv,
        "used_5m": used_5m,
        "hard_veto": hard_veto,
        "hard_veto_reason": hard_veto_reason,
        "bs_mode": direction_mode,
        "corr_action": corr_action,
        "sentiment_label": sentiment_label,
        "sentiment_filter_note": sentiment_filter_note,
        "sr_quality_note": sr_quality_note,
    }


# ----------------------------------------------------------------------------
# 风控硬闸门（§3）
# ----------------------------------------------------------------------------
# 缺省合约规格兜底：任何未登记合约_specs 的品种，用此通用值代替，
# 避免 risk_gate/build_signal 因 KeyError 抛异常拖垮整轮 evaluate（曾致 SA01 崩溃循环）。
_FALLBACK_SPEC = {"multiplier": 10, "margin_rate": 0.10, "limit_pct": 0.05, "fee": 3.0}

# —— #4 fractional-Kelly 仓位缩放：用 walk-forward edge(mean_oos) 放大/缩小风险预算仓位 ——
_CALIB_CACHE = {}


def _load_calib_params():
    global _CALIB_CACHE
    if not _CALIB_CACHE:
        try:
            _CALIB_CACHE = json.load(open(os.path.join(HERE, "calibration_params.json")))
        except Exception:
            _CALIB_CACHE = {}
    return _CALIB_CACHE


# —— ③ (2026-08-16) 阈值全参数化：kelly_min/kelly_max 可由 trade_config.json 顶层覆盖 ——
# 与 runner 的 _tc_num 同源自洽（均读 trade_config.json 顶层，带 60s 缓存）。
# 必须 strategy 内自带读取：回测路径直接 compute_kelly_factor(cfg=DEFAULT_CONFIG)，不经过 runner 合并。
_TC_CACHE = {"t": 0.0, "v": None}


def _load_tc():
    """读取 trade_config.json 顶层（60s 缓存），供阈值参数化。"""
    global _TC_CACHE
    _now = time.time()
    if _TC_CACHE["v"] is not None and (_now - _TC_CACHE["t"]) < 60:
        return _TC_CACHE["v"]
    try:
        v = json.load(open(os.path.join(HERE, "trade_config.json"), encoding="utf-8")) or {}
    except Exception:
        v = {}
    _TC_CACHE = {"t": _now, "v": v}
    return v


def _tc_num(key, default):
    """从 trade_config.json 顶层读数值型风控阈值，非数值/缺失则回退 default。"""
    try:
        v = _load_tc().get(key)
        if isinstance(v, (int, float)):
            return float(v)
    except Exception:
        pass
    return default


def compute_kelly_factor(symbol, cfg=DEFAULT_CONFIG):
    """由 walk-forward edge(mean_oos) 推导 fractional-Kelly 仓位缩放系数 ∈ [kelly_min, kelly_max]。
    低 edge→收缩(最低 kelly_min)，高 edge→放大(最高 kelly_max)。无校准数据→1.0(中性)。
    P2-A（2026-08-14 整改）：近景 edge 门槛——仅当 walk-forward edge 与近景期望收益(cur_full_expR)
    同为正时，才允许 >1.0 的杠杆放大；否则强制封顶 1.0，杜绝弱/负 edge 品种反向加杠杆。"""
    rg = cfg["risk_gate"]
    calib = _load_calib_params().get(symbol, {})
    edge = calib.get("mean_oos")
    if edge is None:
        edge = calib.get("full_expR")
    if edge is None:
        return 1.0
    # P1-4 深度重审：改用线性映射替代原 0.6 + slope*edge 公式
    # 原公式 edge=0.5 → 1.6x（过度自信），靠 max/min 截断到 1.2x，未根治弱 edge 加杠杆问题。
    # 新公式：mult = kelly_min + (kelly_max - kelly_min) * clip(edge / target_edge, 0, 1)
    # target_edge 来自 trade_config.json（默认 0.5 即历史平均优良 edge），超线性封顶。
    kelly_min_eff = _tc_num("kelly_min", rg.get("kelly_min", 0.6))
    kelly_max_eff = _tc_num("kelly_max", rg.get("kelly_max", 1.2))
    target_edge = _tc_num("kelly_target_edge", rg.get("kelly_target_edge", 0.5))
    _edge_pos = max(float(edge), 0.0)
    _ratio = min(_edge_pos / target_edge, 1.0) if target_edge > 0 else 1.0
    mult = kelly_min_eff + (kelly_max_eff - kelly_min_eff) * _ratio
    # 近景 edge 门槛：cur_full_expR(近景期望收益) 需 >0 才放杠杆；缺近景数据则退回远 edge 符号
    near = calib.get("cur_full_expR")
    near_pos = (float(near) > 0) if near is not None else (float(edge) > 0)
    if not near_pos:
        mult = min(mult, 1.0)
    return mult


def risk_gate(symbol, price, atr_val, cfg=DEFAULT_CONFIG, t_strength=None, t_thresh=None, held_lots=0, risk_state=None):
    # P1-16: 实时风控前置检查 —— 锁定/熔断时直接否决
    _locked, _lock_reason = _is_risk_locked(risk_state)
    if _locked:
        return {
            "passed": False,
            "N_risk": 0,
            "N_margin": 0,
            "N_plan": 0,
            "stop_pts": 0,
            "limit_pts": 0,
            "gate3_ok": False,
            "over_risk": False,
            "kelly_mult": 0.0,
            "t_scale": None,
            "risk_blocked": True,
            "risk_block_reason": _lock_reason,
        }

    ac, sp = cfg["account"], cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    # 逐品种覆盖：命中 per_symbol_risk 则改写 stop_atr_mult / rr_ratio
    rg = dict(cfg["risk_gate"])
    for _k in ("stop_atr_mult", "rr_ratio"):
        if _k in cfg.get("per_symbol_risk", {}).get(symbol, {}):
            rg[_k] = cfg["per_symbol_risk"][symbol][_k]
    mv, margin_rate, limit_pct = sp["multiplier"], sp["margin_rate"], sp["limit_pct"]
    equity = ac["equity"]
    stop_pts = rg["stop_atr_mult"] * atr_val
    risk_hand = stop_pts * mv
    N_risk_raw = int(equity * ac["risk_pct"] / 100 // risk_hand) if risk_hand > 0 else 0
    over_risk = False
    if N_risk_raw < 1 and risk_hand > 0:
        N_risk = 1  # 最小 1 手（超风险预算，标注⚠️，不裸奔但不超加仓）
        over_risk = True
    else:
        N_risk = N_risk_raw
    # #4 fractional-Kelly：按 edge 缩放风险预算仓位（等风险占比基础之上再调）
    kelly_mult = compute_kelly_factor(symbol, cfg)
    N_risk = max(1, int(round(N_risk * kelly_mult))) if N_risk >= 1 else 0
    # #5 组合权重调整（P4，2026-08-29）：portfolio.enabled=True 时按权重调整风险预算
    #    高权重品种 → 仓位更大；低权重品种 → 仓位更小
    #    关闭时 mult=1.0 不影响原逻辑
    pf_mult = 1.0
    if cfg.get("portfolio", {}).get("enabled", False):
        try:
            from portfolio_manager import portfolio_risk_mult

            pf_mult = portfolio_risk_mult(symbol, cfg)
        except Exception:
            pf_mult = 1.0
    if pf_mult != 1.0 and N_risk >= 1:
        N_risk = max(1, int(round(N_risk * pf_mult)))
    margin_per = price * mv * margin_rate
    # 分品种保证金上限覆盖（回测结论 2026-08-16：JM/J 低胜率→单笔占比收紧）
    ac_margin_cap = cfg.get("per_symbol_risk", {}).get(symbol, {}).get("margin_cap_pct", ac["margin_cap_pct"])
    N_margin = int(equity * ac_margin_cap / 100 // margin_per) if margin_per > 0 else 0
    max_lots = ac["per_symbol_lots"].get(symbol, ac["max_lots"])
    N_plan = min(N_risk, N_margin, max_lots)
    # P1-仓位随 T 强度缩放（2026-08-19）：弱过阈降仓，|T|≥1.5×阈值满仓；t_strength 为 None 则跳过（回测兼容）
    t_scale = None
    if t_strength is not None and t_thresh and float(t_thresh) > 0:
        t_scale = max(0.5, min(1.0, abs(float(t_strength)) / (float(t_thresh) * 1.5)))
        N_plan = max(0, int(N_plan * t_scale))
    # P2b-扣减已有同品种持仓（2026-08-19）：单品种总持仓不超 per_symbol_lots/max_lots，加仓不超配
    if held_lots > 0:
        N_plan = max(0, min(N_plan, max_lots - held_lots))

    limit_pts = price * limit_pct
    # 第三道闸门：止损距必须小于一个涨跌停幅度（否则一个停板即直达止损=极端风险）。
    # limit_proximity 作为缓冲系数（<1 留余量），默认 0.9：止损距达涨跌停 90% 才预警否决。
    gate3_ok = (stop_pts < limit_pts * rg["limit_proximity"]) if limit_pts > 0 else True
    passed = (N_plan >= 1) and gate3_ok
    return {
        "passed": passed,
        "N_risk": N_risk,
        "N_margin": N_margin,
        "N_plan": max(0, N_plan),
        "stop_pts": round(stop_pts, 2),
        "limit_pts": round(limit_pts, 2),
        "gate3_ok": gate3_ok,
        "over_risk": over_risk,
        "kelly_mult": round(kelly_mult, 3),
        "t_scale": round(t_scale, 3) if t_scale is not None else None,
    }


# ----------------------------------------------------------------------------
# 出场计划（§1.8）
# ----------------------------------------------------------------------------
def exit_plan(symbol, entry, dir_T, atr_val, regime, cfg=DEFAULT_CONFIG, feat_mgr=None, sr_result=None):
    rg = dict(cfg["risk_gate"])
    for _k in ("stop_atr_mult", "rr_ratio"):
        if _k in cfg.get("per_symbol_risk", {}).get(symbol, {}):
            rg[_k] = cfg["per_symbol_risk"][symbol][_k]
    rc_all = effective_regime_coef(symbol, cfg)
    rc = rc_all.get(regime, rc_all["波动"])
    stop_mult = rg["stop_atr_mult"] * rc["stop"]
    stop_dist = stop_mult * atr_val
    if dir_T > 0:
        stop, t1, t2 = entry - stop_dist, entry + stop_dist, entry + rg["rr_ratio"] * stop_dist
    else:
        stop, t1, t2 = entry + stop_dist, entry - stop_dist, entry - rg["rr_ratio"] * stop_dist
    # ── #9 SR 位动态止损（v2：分板块放宽止损）──
    # 回测验证：收紧止损有害(-4.5%)，放宽止损整体+18.2%，板块差异大
    # 用分板块差异化配置替代旧的收紧止损方案
    sr_note = ""
    if sr_result is not None and sr_result.get("levels"):
        import sr_analyzer as _sra

        _sym_meta = SYMBOLS.get(symbol, {})
        _widen_mult = _sra.get_widen_stop_mult(symbol, _sym_meta)
        if _widen_mult is not None:
            _orig = {"stop": stop, "t1": t1, "t2": t2, "stop_dist": stop_dist}
            _adj = _sra.widen_stop_with_sr(_orig, sr_result, dir_T, entry, max_mult=_widen_mult)
            if _adj.get("sr_stop_widen"):
                stop = _adj.get("stop", stop)
                stop_dist = _adj.get("stop_dist", stop_dist)
                # t1/t2 按比例跟随止损调整（保持 R 倍数不变）
                _ratio = _adj["stop_dist"] / _orig["stop_dist"] if _orig["stop_dist"] > 0 else 1.0
                if dir_T > 0:
                    t1 = round(entry + _ratio * (t1 - entry), 2)
                    t2 = round(entry + _ratio * (t2 - entry), 2)
                else:
                    t1 = round(entry - _ratio * (entry - t1), 2)
                    t2 = round(entry - _ratio * (entry - t2), 2)
                sr_note += f"SR放宽止损至{stop}({_widen_mult}R上限); "
    tt = dict(cfg.get("trailing_tail", {}))
    # 开关优先级：特性开关 > 旧配置 > 默认关闭
    tail_on = None
    if feat_mgr is not None:
        try:
            tail_on = feat_mgr.is_enabled("trailing_stop")
        except Exception:
            tail_on = None
    if tail_on is None:
        tail_on = bool(tt.get("enabled", False))
    trend_only = bool(tt.get("trend_only", True))
    tail_enabled = tail_on and (not trend_only or regime == "趋势")
    tail_trail_R = float(tt.get("tail_trail_R", 2.0))
    tail_pct = float(tt.get("tail_pct", 0.25))
    tail_stop_dist = tail_trail_R * stop_dist  # 尾仓跟踪距离（×1R=stop_dist）
    return {
        "stop": round(stop, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "stop_dist": round(stop_dist, 2),
        "trailing": regime in ("趋势", "波动"),
        "style": "单批(震荡)" if regime == "震荡" else "两批+移动止损",
        "tail_enabled": tail_enabled,
        "tail_stop_dist": round(tail_stop_dist, 2),
        "tail_pct": tail_pct,
        "sr_note": sr_note,
    }


# ----------------------------------------------------------------------------
# 信号包裹（§5）
# ----------------------------------------------------------------------------
def build_signal(symbol, pipe, rg, ep, cfg=DEFAULT_CONFIG, entry_ref=None):
    direction = "多" if pipe["dir_T"] > 0 else ("空" if pipe["dir_T"] < 0 else "中性")
    slip = get_slip_pts(symbol, cfg)
    slip_cost_r = 2 * slip / ep["stop_dist"] if ep["stop_dist"] > 0 else 0.0
    reason = (
        f"技术面触发偏{'多' if pipe['dir_T'] > 0 else '空'}(T_5m={pipe['T_5m']}，"
        f"regime={pipe['regime']})；背景偏置 bias_G={pipe['bias_G']}"
        f"({'同向放行' if pipe['triggered'] else '抑制/否决'})；"
        f"{pipe['conv']}；风控{'通过' if rg['passed'] else '未过→温和提示'}，"
        f"建议{pipe['dir_T'] and rg['N_plan']}手，"
        f"止损{ep['stop']}(距{ep['stop_dist']})；"
        f"分批 t1={ep['t1']}(1R平半)→t2={ep['t2']}(2R全平)"
        f"{'，趋势/波动开启移动止损' if ep['trailing'] else ''}。"
    )
    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "direction": direction,
        "entry_ref": (float(entry_ref) if entry_ref is not None else None),
        "stop": ep["stop"],
        "target": ep["t2"],
        "t1": ep["t1"],
        "t2": ep["t2"],
        "stop_dist": ep["stop_dist"],
        "lots": rg["N_plan"],
        "pipeline": {
            "F_bias": pipe["F"],
            "T_D": pipe["T_D"],
            "T_5m": pipe["T_5m"],
            "C_score": pipe["C"],
            "bias_G": pipe["bias_G"],
            "regime": pipe["regime"],
            "conv": pipe["conv"],
            "corr_gate": pipe["corr_action"],
            "used_5m": pipe["used_5m"],
        },
        "risk_gate": {
            "pass": rg["passed"],
            "N_risk": rg["N_risk"],
            "N_margin": rg["N_margin"],
            "N_plan": rg["N_plan"],
            "kelly_mult": rg["kelly_mult"],
            "limit_check": "ok" if rg["gate3_ok"] else "near_limit",
        },
        "cost": {
            "slip_pts": slip,
            "slip_cost_r": round(slip_cost_r, 4),
            "note": f"流动性敏感滑点 {slip} 点(双向)，约占止损距 {slip_cost_r * 100:.1f}%(回测已扣)",
        },
        "exit_plan": ep,
        "reason": reason,
    }


# ----------------------------------------------------------------------------
# walk-forward 回测自检（F+T 两维，C 中性；扣费扣滑点；分 regime）
# ----------------------------------------------------------------------------
# ── 换月/跳空 跳空识别（P0-2）────────────────────────────────────────────
# 主连日线在合约换月处会出现巨大"展期缺口"（旧合约收盘→新合约开盘的跳变），
# 该缺口非真实价格运动，却会被误判"触止损/触止盈" → 假交易，污染 walk-forward 校准。
# 判定规则：某根 K 线的开盘相对前收跳变超过以下任一阈值 → 视为展期/涨跌停缺口，
# 跳过该根的止损/止盈判定（沿用上一根未平状态继续）。
ROLL_GAP_PCT = 0.010
ROLL_GAP_MULT = 1.0


def walk_forward_backtest(
    symbol,
    cfg=DEFAULT_CONFIG,
    min_bars=60,
    window=300,
    tail=None,
    cooldown_bars=5,
    ablate=None,
    F_override=None,
    hmm_label=None,
    macro_label=None,
    garch_label=None,
    df_in=None,
):
    """逐 bar 推进：用截至当日数据算 pipeline（含真实基本面 F），下一根开盘入场，stop/2R 出场，扣费扣滑点。
    触发用日线 T_D（5m 历史仅近 ~10 日，不足以跨年回测；T@5m 实盘另走 minishare 快照聚合）。
    冷却：触发入场后冷却 cooldown_bars 根日线才允许下一次信号（无论方向），
    否则趋势市长期同向只翻仓才交易、严重低估信号数。
    tail: 仅回测尾部 N 根（快速验证用）。
    ── 红线守卫（P2-④）：回测严禁注入 live 专属维度，避免前视偏差 ──
    info 维度(F_override) / HMM(hmm_label) / 宏观(macro_label) / GBM-GARCH(garch_label)
    必须全为 None；任何非 None 调用立即抛错，永久锁死"info 不喂回测"红线。
    df_in: 可选预切分 DataFrame（OOS harness 注入 IS/OOS 切片用）；None=内部 load_daily。"""
    assert F_override is None, "walk_forward_backtest 禁止 F_override（info 维度不得进回测）"
    assert hmm_label is None and macro_label is None and garch_label is None, (
        "walk_forward_backtest 禁止 hmm/macro/garch_label（live 专属维度不得进回测）"
    )
    df = df_in if df_in is not None else load_daily(symbol)
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    if tail and df_in is None:
        df = df.tail(tail)
    if len(df) < min_bars + 20:
        return {"symbol": symbol, "trades": 0, "note": "数据不足"}
    n = len(df)
    sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]

    # 预提取 numpy 数组：循环内用索引访问代替 .iloc，省 ~15%
    _open = df["open"].values
    _high = df["high"].values
    _low = df["low"].values
    _close = df["close"].values
    _months = df.index.month.values if isinstance(df.index, pd.DatetimeIndex) else None
    # 预计算完整 ATR(14) 序列：O(n) 一次算出，循环内直接索引（代替每根 O(n) 重算）
    _atr14_arr = _atr_array(_high, _low, _close, 14)

    # 预计算收益率序列：O(n) 一次算出，s_seasonal 直接用切片，省逐次 np.diff
    _rets_arr = np.empty(n)
    _rets_arr[0] = np.nan
    _rets_arr[1:] = np.diff(_close) / _close[:-1]

    # 预计算 SMA5/20/60 完整序列（cumsum O(n)），循环内直接索引
    # 省：compute_T(3次) + classify_regime(1次斜率) + s_dma(2次prev) 重复计算
    from strategy_layer import _sma_array

    _sma5_arr = _sma_array(_close, 5)
    _sma20_arr = _sma_array(_close, 20)
    _sma60_arr = _sma_array(_close, 60)

    # 预计算 RSI(14) 完整序列（cumsum O(n)），循环内直接索引（省 _rsi_last 逐次计算）
    _rsi14_arr = _rsi_array(_close, 14)

    # 预计算 rolling std(20) 完整序列（cumsum O(n)），s_boll 直接用（省 _rolling_std_last）
    _std20_arr = _rolling_std_array(_close, 20)

    # 预计算季节性同月统计量（12 个月前缀和 O(n)），s_seasonal O(1) 查询
    # 省：每次全量 mask + np.mean + np.std
    _seas_cnt_arr, _seas_sum_arr, _seas_sumsq_arr = _seasonal_month_stats(_rets_arr, _months)

    # 预计算 rolling max/min（唐奇安/海龟用）
    _hh20_arr = _rolling_max_array(_high, 20)
    _ll20_arr = _rolling_min_array(_low, 20)
    _hh55_arr = _rolling_max_array(_high, 55)
    _ll55_arr = _rolling_min_array(_low, 55)

    # 预计算全部 8 个策略的信号数组（全向量化 O(n)），compute_T 直接索引
    # 省：循环内 8 次策略函数调用 + dict 展开开销
    _sig_arrays = precompute_signals(
        _close,
        _high,
        _low,
        _months,
        _rets_arr,
        _sma5_arr,
        _sma20_arr,
        _sma60_arr,
        _rsi14_arr,
        _std20_arr,
        _hh20_arr,
        _ll20_arr,
        _hh55_arr,
        _ll55_arr,
        _seas_cnt_arr,
        _seas_sum_arr,
        _seas_sumsq_arr,
    )

    # 预计算 regime 分类数组（向量化 classify_regime）
    # sma20_slope_prev = 4 根前的 SMA20（对应 classify_regime 的 ma20_prev）
    _sma20_slope_prev_arr = np.concatenate([np.full(4, np.nan), _sma20_arr[:-4]])
    _regime_rp = regime_params_for(symbol, cfg, feat_mgr=None)
    _regime_codes_arr = classify_regime_array(_close, _atr14_arr, _sma20_arr, _sma20_slope_prev_arr, _regime_rp)

    # 预计算完整 T 值数组（向量化簇投票 + 拥挤降权 + 反向阻尼 + 归一化）
    # compute_T 最快路径直接索引返回，省掉循环内所有 Python 逻辑开销
    _group = SYMBOLS.get(symbol, {}).get("group")
    _T_arr = precompute_T_array(_sig_arrays, _regime_codes_arr, cfg, _group, feat_mgr=None)

    # 预计算 F 分数数组：O(n) 一次算出，循环内直接索引（代替每根 bisect+load 重复查基本面）
    import fundamental_feed as _ff

    # 整数日期（YYYYMMDD）比 strftime 快 ~15×，双指针比较也更快
    _date_ints = df.index.year.values * 10000 + df.index.month.values * 100 + df.index.day.values
    _F_arr = _ff.precompute_F_array(symbol, date_ints=_date_ints, months=_months)

    # 预计算 C 值数组（双指针 O(n)，整数日期省 strftime）
    _C_arr = precompute_C_array(symbol, date_ints=_date_ints)

    # 预计算触发信息（dir_T / triggered / bias_G 全向量化），循环内直接索引
    # 非触发时跳过 pipeline 调用，省 ~20% 总耗时
    _dir_T_arr, _triggered_arr, _bias_G_arr = precompute_trigger_info(
        _T_arr, _F_arr, _C_arr, _regime_codes_arr, cfg, symbol
    )

    trades = []
    roll_skipped = 0
    i = min_bars
    last_trade_i = -999
    _df_index = df.index  # 预存引用，触发时按需 strftime
    while i < n - 1:
        # 预切片 numpy 数组 + 预计算指标值（compute_T/pipeline 直接用，省重复计算）
        # 注意：df 传全量但策略函数走 numpy 路径时只看预切片，
        #       省去循环内 df.iloc 切片开销（约占总耗时 15-20%）
        _i = i + 1
        # 快速路径：用预计算的 triggered/dir_T 直接判断，非触发时跳过 pipeline + _prec 构造
        if not _triggered_arr[i] or _dir_T_arr[i] == 0 or (i - last_trade_i) < cooldown_bars:
            i += 1
            continue

        # 触发时才生成日期字符串（500 根只需 12~19 次，省 ~0.8ms strftime）
        date_str = _df_index[i].strftime("%Y%m%d")

        _prec = {
            "_c": _close[:_i],
            "_h": _high[:_i],
            "_l": _low[:_i],
            "_m": _months[:_i] if _months is not None else None,
            "_atr14": _atr14_arr[i],
            "_rets": _rets_arr[:_i],
            "_sma5": _sma5_arr[i],
            "_sma20": _sma20_arr[i],
            "_sma60": _sma60_arr[i],
            # s_dma 用：前一根的 SMA5/SMA20
            "_sma5_prev": _sma5_arr[i - 1] if i >= 1 else np.nan,
            "_sma20_prev": _sma20_arr[i - 1] if i >= 1 else np.nan,
            # classify_regime 用：4 根前的 SMA20（斜率计算基准）
            "_sma20_slope_prev": _sma20_arr[i - 4] if i >= 4 else np.nan,
            # s_rsi 用：当前 RSI(14) 值
            "_rsi14": _rsi14_arr[i],
            # s_boll 用：当前 rolling std(20) 值
            "_std20": _std20_arr[i],
            # s_seasonal 用：同月统计量（count, sum, sum_sq），O(1) 计算均值/标准差
            "_seasonal_cnt": int(_seas_cnt_arr[i]),
            "_seasonal_sum": float(_seas_sum_arr[i]),
            "_seasonal_sumsq": float(_seas_sumsq_arr[i]),
            # 预计算信号数组 + 当前索引（compute_T 直接索引，省 8 次函数调用）
            "_signal_arrays": _sig_arrays,
            "_sig_idx": i,
            # 预计算 T 值 + regime code（compute_T 最快路径直接返回，省全部 Python 逻辑）
            "_T_array": _T_arr,
            "_regime_code": int(_regime_codes_arr[i]),
            # 预计算 C 值（省 score_C 内 history 遍历查找）
            "_C_val": float(_C_arr[i]),
        }

        try:
            pipe = pipeline(
                symbol, df, None, cfg, date=date_str, ablate=ablate, _precalc=_prec, F_override=float(_F_arr[i])
            )
        except Exception:
            i += 1
            continue
        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry = float(_open[i + 1])
            # 预计算的 ATR（循环外一次性算出，O(1) 索引）
            atr_val = _atr14_arr[i]
            if atr_val <= 0 or math.isnan(atr_val):
                i += 1
                continue
            rg = risk_gate(symbol, entry, atr_val, cfg)
            if not rg["passed"]:
                i += 1
                continue
            dir_T = pipe["dir_T"]
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], cfg)
            sd = ep["stop_dist"]
            # 出场模拟
            exit_price, reason = None, ""
            tail_active, tail_stop = False, None
            for j in range(i + 1, n):
                hi, lo = float(_high[j]), float(_low[j])
                # ── 换月跳空识别（P0-2）──
                # 入场根(j==i+1)的跳空是真实入場缺口，不跳過；
                # 后续根若开盘相对前收出现超阈值跳变，视为展期/涨跌停缺口，跳过本根判定。
                if j > i + 1:
                    prev_close = float(_close[j - 1])
                    gap = abs(float(_open[j]) - prev_close)
                    if gap > max(ROLL_GAP_PCT * prev_close, ROLL_GAP_MULT * sd):
                        roll_skipped += 1
                        continue
                # ── 尾仓态（P-G）：t2 已达，用宽 trail 跟随，触及才离场 ──
                if tail_active:
                    if dir_T > 0:
                        if lo <= tail_stop:
                            exit_price, reason = tail_stop, "尾仓离场"
                            break
                        tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
                    else:
                        if hi >= tail_stop:
                            exit_price, reason = tail_stop, "尾仓离场"
                            break
                        tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
                    continue
                if dir_T > 0:
                    if lo <= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"
                        break
                    if hi >= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]
                            continue
                        exit_price, reason = ep["t2"], "止盈2R"
                        break
                else:
                    if hi >= ep["stop"]:
                        exit_price, reason = ep["stop"], "止损"
                        break
                    if lo <= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]
                            continue
                        exit_price, reason = ep["t2"], "止盈2R"
                        break
            if exit_price is None:
                exit_price, reason = float(_close[-1]), "期末平"
            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R
            trades.append(
                {
                    "dir": dir_T,
                    "R": round(R, 3),
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "entry_date": df.index[i + 1],
                    "F": pipe["F"],
                    "T_D": pipe["T_D"],
                    "C": pipe["C"],
                }
            )
            last_trade_i = i
            i = j + 1 if exit_price is not None else i + 1
            continue
        i += 1
    if not trades:
        return {"symbol": symbol, "trades": 0, "note": "无触发信号", "roll_skipped": roll_skipped}
    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    by_regime = {}
    for t in trades:
        by_regime.setdefault(t["regime"], []).append(t["R_adj"])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "trades": len(trades),
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades_detail": trades,  # 逐笔明细(含 R_adj/方向/reason/regime)，供 OOS 对比算最大回撤
        "by_regime": {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()},
        "exit_reasons": reasons,
        "roll_skipped": roll_skipped,
    }


def _sim_exit_5m(df5_seg, dir_T, entry, ep, sd):
    """在 5m 序列上逐 bar 做 stop / t2 / 尾仓(P-G) 出场，返回 (exit_price, reason, exit_idx)。

    P0-2 fix: 额外返回退出 bar 索引，使回测可正确跳过持仓期内的日线。"""
    tail_active, tail_stop = False, None
    for j in range(len(df5_seg)):
        hi = float(df5_seg["high"].iloc[j])
        lo = float(df5_seg["low"].iloc[j])
        if tail_active:
            if dir_T > 0:
                if lo <= tail_stop:
                    return tail_stop, "尾仓离场", j
                tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
            else:
                if hi >= tail_stop:
                    return tail_stop, "尾仓离场", j
                tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
            continue
        if dir_T > 0:
            if lo <= ep["stop"]:
                return ep["stop"], "止损", j
            if hi >= ep["t2"]:
                if ep["tail_enabled"]:
                    tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]
                    continue
                return ep["t2"], "止盈2R", j
        else:
            if hi >= ep["stop"]:
                return ep["stop"], "止损", j
            if lo <= ep["t2"]:
                if ep["tail_enabled"]:
                    tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]
                    continue
                return ep["t2"], "止盈2R", j
    # P0-2 fix: 返回最后一个 bar 的索引
    return float(df5_seg["close"].iloc[-1]), "期末平", len(df5_seg) - 1


def walk_forward_backtest_5m_exit(symbol, cfg=DEFAULT_CONFIG, min_bars=60, cooldown_bars=5, ablate=None, tf="5m"):
    """日线定信号 + 细粒度(5m/1h)出场的 P-G 尾仓验证。

    信号仍由日线 T_D 决定（pipeline 用日线），但出场模拟下沉到 5m/1h bar 序列，
    使 P-G 尾仓的"盘中回撤触止损 / 趋势中途跟出"被真实验证（日线回测看不到）。
    tf="1h" 时把本地 5m 数据 resample 为 1h 再跑同一套逻辑（1h 无原生数据，
    但可由 5m 零成本聚合得到，作为 日线→1h→5m 粒度阶梯的中间档）。
    仅统计细粒度数据覆盖窗口（约近 3 周）内入场、且有序列可做出场的信号。
    返回结构与 walk_forward_backtest 一致（含 trades_detail / by_regime / exit_reasons）。"""
    df = load_daily(symbol)
    df5 = load_min5(symbol, fetch_if_missing=False)  # 仅本地，不联网
    if df is None:
        return {"symbol": symbol, "trades": 0, "note": "日线不足"}
    if df5 is None or len(df5) < 60:
        return {"symbol": symbol, "trades": 0, "note": "5m不足"}
    if tf == "1h":
        df5 = (
            df5.resample("1h")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "sum"})
            .dropna()
        )
    if len(df5) < 60:
        return {"symbol": symbol, "trades": 0, "note": "1h(resample)不足"}
    n = len(df)
    sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
    mv, fee = sp["multiplier"], sp["fee"]

    # 预提取 numpy 数组：循环内用索引访问代替 .iloc
    _open = df["open"].values
    _high = df["high"].values
    _low = df["low"].values
    _close = df["close"].values
    _months = df.index.month.values if isinstance(df.index, pd.DatetimeIndex) else None
    # 预计算完整 ATR(14) 序列：O(n) 一次算出，循环内直接索引
    _atr14_arr = _atr_array(_high, _low, _close, 14)

    trades = []
    roll_skipped = 0
    i = min_bars
    last_trade_i = -999
    while i < n - 1:
        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        # 预切片 numpy 数组 + 当前 ATR 值
        _i = i + 1
        _prec = {
            "_c": _close[:_i],
            "_h": _high[:_i],
            "_l": _low[:_i],
            "_m": _months[:_i] if _months is not None else None,
            "_atr14": _atr14_arr[i],
        }
        try:
            pipe = pipeline(symbol, hist, None, cfg, date=date_str, ablate=ablate, _precalc=_prec)
        except Exception:
            i += 1
            continue
        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry_date = df.index[i + 1]
            entry = float(_open[i + 1])
            # 预计算的 ATR（循环外一次性算出，O(1) 索引）
            atr_val = _atr14_arr[i]
            if atr_val <= 0 or math.isnan(atr_val):
                i += 1
                continue
            rg = risk_gate(symbol, entry, atr_val, cfg)
            if not rg["passed"]:
                i += 1
                continue
            dir_T = pipe["dir_T"]
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], cfg)
            sd = ep["stop_dist"]
            # ── 5m 出场：截取从入场日起的 5m 序列 ──
            seg = df5[df5.index >= entry_date.normalize()]
            if len(seg) < 3:
                i += 1
                continue
            # P0-2 fix: 接收 exit_idx 以正确跳过持仓期
            exit_price, reason, exit_idx = _sim_exit_5m(seg, dir_T, entry, ep, sd)
            if exit_price is None:
                i += 1
                continue
            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * fee / (sd * mv) if sd > 0 else 0
            R_adj = R - slip_R - fee_R
            # P0-2 fix: 计算退出日期，将 i 推进到至少退出日的下一天
            exit_date = seg.index[min(exit_idx, len(seg) - 1)]
            trades.append(
                {
                    "dir": dir_T,
                    "R": round(R, 3),
                    "R_adj": round(R_adj, 3),
                    "reason": reason,
                    "regime": pipe["regime"],
                    "F": pipe["F"],
                    "T_D": pipe["T_D"],
                    "C": pipe["C"],
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": exit_date.strftime("%Y-%m-%d"),
                }
            )
            last_trade_i = i
            # P0-2 fix (强化): 用 bisect 精确定位退出日，跳过持仓期内所有日线
            # 原 Bug: range(i+1, min(i+30, n)) 上限 30 天，持仓>30天的趋势单漏跳
            exit_day = exit_date.normalize()
            # 用 bisect 找到日线中 >= exit_day 的第一个位置，确保不超范围
            _di = df.index.normalize()
            pos = bisect.bisect_left(_di, exit_day)
            if pos < n:
                i = pos + 1  # 退出日的下一天
            else:
                i = n - 1  # 兜底：退出日在日线窗口外，跳到末尾
            roll_skipped += max(0, pos - i) if pos > i else 0
            continue
        i += 1
    if not trades:
        return {"symbol": symbol, "trades": 0, "note": "窗口内无5m可验证信号", "roll_skipped": roll_skipped}
    Rs = [t["R_adj"] for t in trades]
    wins = [r for r in Rs if r > 0]
    by_regime = {}
    for t in trades:
        by_regime.setdefault(t["regime"], []).append(t["R_adj"])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "trades": len(trades),
        "expR": round(float(np.mean(Rs)), 4),
        "win_rate": round(len(wins) / len(Rs), 3),
        "trades_detail": trades,
        "by_regime": {k: round(float(np.mean(v)), 4) for k, v in by_regime.items()},
        "exit_reasons": reasons,
        "roll_skipped": roll_skipped,
        "note": f"{tf}_exit",
    }


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def run_backtest_all(cfg=DEFAULT_CONFIG):
    print("################ 四维策略 walk-forward 回测自检（F+T 两维，C 中性）################")
    print(
        f"权益={cfg['account']['equity']} 风险%={cfg['account']['risk_pct']} "
        f"止损ATR×{cfg['risk_gate']['stop_atr_mult']} 风险回报={cfg['risk_gate']['rr_ratio']}\n"
    )
    rows = []
    for sym in SYMBOLS:
        if sym in DISABLED_SYMBOLS:  # 校准判死刑的品种不参与回测
            continue
        try:
            r = walk_forward_backtest(sym, cfg)
        except Exception as e:
            r = {"symbol": sym, "trades": 0, "note": f"异常:{repr(e)[:50]}"}
        rows.append(r)
        if r.get("trades", 0) == 0:
            print(f"  {sym:3} {SYMBOLS[sym]['name']:4} 无信号/数据不足 {r.get('note', '')}")
        else:
            br = " ".join(f"{k}:{v}" for k, v in r["by_regime"].items())
            print(
                f"  {sym:3} {SYMBOLS[sym]['name']:4} 笔={r['trades']:>4} "
                f"期望R={r['expR']:>7} 胜率={r['win_rate'] * 100:>5.1f}%  regime[{br}]"
            )
    return rows


if __name__ == "__main__":
    run_backtest_all()


# ----------------------------------------------------------------------------
# 自适应恢复判定（2026-08-13）：被禁品种，当近期 walk-forward 重新转正时自动解禁
# ----------------------------------------------------------------------------
def recovery_check(symbol, cfg=DEFAULT_CONFIG, tail=250, min_trades=10, min_expr=0.0, min_win=0.45):
    """对当前被禁品种跑近期 walk-forward，判断是否值得恢复交易。

    对称于 DISABLED_SYMBOLS 的入禁逻辑（walk-forward OOS 负期望→禁）；
    恢复条件：近期窗口 expR>=min_expr 且 胜率>=min_win 且 样本>=min_trades。
    被禁品种不进 run_backtest_all，但恢复判定需要它 → 此处直接 walk_forward_backtest
    （load_daily 从本地 _XX0_daily.csv 读，不依赖 runner feed，可独立运行）。

    返回 dict: {recover, symbol, expR, win_rate, trades, note}
    """
    try:
        r = walk_forward_backtest(symbol, cfg, tail=tail)
    except Exception as e:
        return {
            "recover": False,
            "symbol": symbol,
            "note": f"回测异常:{repr(e)[:60]}",
            "expR": None,
            "win_rate": None,
            "trades": 0,
        }
    tr = int(r.get("trades", 0))
    if tr < min_trades:
        return {
            "recover": False,
            "symbol": symbol,
            "expR": r.get("expR"),
            "win_rate": r.get("win_rate"),
            "trades": tr,
            "note": f"样本不足({tr}<{min_trades})，暂不恢复",
        }
    expR = float(r.get("expR") or 0)
    win = float(r.get("win_rate") or 0)
    ok = (expR >= min_expr) and (win >= min_win)
    note = "转正·可恢复" if ok else f"仍负(expR={expR:.3f}/胜{win * 100:.0f}%)·维持屏蔽"
    return {
        "recover": ok,
        "symbol": symbol,
        "expR": round(expR, 4),
        "win_rate": round(win, 4),
        "trades": tr,
        "note": note,
    }


# ----------------------------------------------------------------------------
# 模型健康分解（2026-08-13 · #2）：留一维度消融，定位 F/T/C 谁在退化
# ----------------------------------------------------------------------------


def decompose_model_health(symbol, cfg=DEFAULT_CONFIG, tail=250, min_trades=8):
    """模型健康分解（#2）：双视角定位 F/T/C 谁在退化。
    ① 留一维度消融：各维边际 expR 贡献 = full - 消融后；
    ② 实际成交方向一致性：在被 FULL 模型实际触发的成交上，
       统计该维投票(符号)与成交方向一致时的平均 R_adj vs 不一致时，
       直接反映该维「投对票的能力」——比消融更灵敏。
    返回 {symbol, full_expR, full_trades, contrib, agree:{F,T,C}, worst, verdict}
    """
    full = walk_forward_backtest(symbol, cfg, tail=tail)
    f_ab = walk_forward_backtest(symbol, cfg, tail=tail, ablate="F")
    c_ab = walk_forward_backtest(symbol, cfg, tail=tail, ablate="C")
    t_ab = walk_forward_backtest(symbol, cfg, tail=tail, ablate="T")
    fe = float(full.get("expR") or 0)
    ft = int(full.get("trades", 0))
    contrib = {
        "F": round(fe - float(f_ab.get("expR") or 0), 4),
        "T": round(fe - float(t_ab.get("expR") or 0), 4),
        "C": round(fe - float(c_ab.get("expR") or 0), 4),
    }
    det = _wf_trades_detail(symbol, cfg, tail=tail)
    agree = {}
    for dim in ("F", "T", "C"):
        same, diff = [], []
        for t in det:
            v = t.get(dim, 0) or 0
            if v == 0:
                continue
            match = math.copysign(1, v) == math.copysign(1, t["dir"])
            (same if match else diff).append(t["R_adj"])
        avg_same = float(np.mean(same)) if same else 0.0
        avg_diff = float(np.mean(diff)) if diff else 0.0
        agree[dim] = round(avg_same - avg_diff, 4)
    neg = {k: v for k, v in agree.items() if v < 0}
    worst = min(neg, key=lambda k: agree[k]) if neg else None
    if not neg:
        verdict = "健康·各维方向一致性正向"
    else:
        # 列出所有退化维（方向一致性为负），按严重度排序
        deg = sorted(neg.items(), key=lambda kv: kv[1])
        parts = [f"{k}({v:+.3f})" for k, v in deg]
        verdict = "退化维: " + " · ".join(parts) + " → 优先重训 " + "、".join(k for k, _ in deg)
    return {
        "symbol": symbol,
        "full_expR": round(fe, 4),
        "full_trades": ft,
        "contrib": contrib,
        "agree": agree,
        "worst": worst,
        "verdict": verdict,
    }


def _wf_trades_detail(symbol, cfg=DEFAULT_CONFIG, tail=250, min_bars=60, cooldown_bars=5):
    """walk_forward_backtest 明细版：返回每笔成交(含 F/T_D/C 入场值)，供分解使用。"""
    df = load_daily(symbol)
    if df is None or len(df) < min_bars + 20:
        return []
    if tail:
        df = df.tail(tail)
    n = len(df)
    trades = []
    i = min_bars
    last_trade_i = -999
    while i < n - 1:
        hist = df.iloc[: i + 1]
        date_str = df.index[i].strftime("%Y%m%d")
        try:
            pipe = pipeline(symbol, hist, None, cfg, date=date_str)
        except Exception:
            i += 1
            continue
        if pipe["triggered"] and pipe["dir_T"] != 0 and (i - last_trade_i) >= cooldown_bars:
            entry = float(df["open"].iloc[i + 1])
            atr_val = strat_atr(hist).iloc[-1]
            if atr_val <= 0 or math.isnan(atr_val):
                i += 1
                continue
            rg = risk_gate(symbol, entry, atr_val, cfg)
            if not rg["passed"]:
                i += 1
                continue
            dir_T = pipe["dir_T"]
            ep = exit_plan(symbol, entry, dir_T, atr_val, pipe["regime"], cfg)
            sd = ep["stop_dist"]
            exit_price = None
            tail_active, tail_stop = False, None
            for j in range(i + 1, n):
                hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
                if j > i + 1:
                    prev_close = float(df["close"].iloc[j - 1])
                    gap = abs(float(df["open"].iloc[j]) - prev_close)
                    if gap > max(ROLL_GAP_PCT * prev_close, ROLL_GAP_MULT * sd):
                        continue
                # ── 尾仓态（P-G）：t2 已达，用宽 trail 跟随，触及才离场 ──
                if tail_active:
                    if dir_T > 0:
                        if lo <= tail_stop:
                            exit_price = tail_stop
                            break
                        tail_stop = max(tail_stop, hi - ep["tail_stop_dist"])
                    else:
                        if hi >= tail_stop:
                            exit_price = tail_stop
                            break
                        tail_stop = min(tail_stop, lo + ep["tail_stop_dist"])
                    continue
                if dir_T > 0:
                    if lo <= ep["stop"]:
                        exit_price = ep["stop"]
                        break
                    if hi >= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] - ep["tail_stop_dist"]
                            continue
                        exit_price = ep["t2"]
                        break
                else:
                    if hi >= ep["stop"]:
                        exit_price = ep["stop"]
                        break
                    if lo <= ep["t2"]:
                        if ep["tail_enabled"]:
                            tail_active, tail_stop = True, ep["t2"] + ep["tail_stop_dist"]
                            continue
                        exit_price = ep["t2"]
                        break
            if exit_price is None:
                exit_price = float(df["close"].iloc[-1])
            R = (exit_price - entry) / sd if dir_T > 0 else (entry - exit_price) / sd
            sp = cfg["contract_specs"].get(symbol, _FALLBACK_SPEC)
            slip_R = 2 * get_slip_pts(symbol, cfg) / sd if sd > 0 else 0
            fee_R = 2 * sp["fee"] / (sd * sp["multiplier"]) if sd > 0 else 0
            R_adj = R - slip_R - fee_R
            trades.append({"dir": dir_T, "R_adj": round(R_adj, 3), "F": pipe["F"], "T_D": pipe["T_D"], "C": pipe["C"]})
            last_trade_i = i
            i = j + 1 if exit_price is not None else i + 1
            continue
        i += 1
    return trades
