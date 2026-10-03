#!/usr/bin/env python3
"""短路原因契约 —— DISABLED_SYMBOLS 的「可核查原因」登记与校验
==================================================================

动机（借 cangjie 流水线治理件）：
    短路（禁用某品种/策略）**禁止用「结果不好」当理由** —— 原因必须是**可核查条件**：
    「无数据 / 无权限 / 已黑名单 / 历史全灭」这类**能复核的事实**。

现状（改前）：
    `four_dim_strategy.DISABLED_SYMBOLS` 是一个**裸集合**，逐条目**没有任何原因字段**；
    原因只散在几行 prose 注释里 → **不可校验、不可审计**（无法回答「这个被禁品种
    到底是什么原因、什么条件下能恢复」）。

本模块把它升级为**机器可读、可校验**的契约：
    1. 每个被禁品种**必须**有登记项            （缺 → 违规）
    2. 原因码**必须** ∈ ALLOWED_REASON_CODES   （非法 → 违规）
    3. 原因文案**不得**是「结果不好」类不可核查措辞（命中 → 违规）
    4. 登记与 `DISABLED_SYMBOLS` / `AUTO_RECOVER_SYMBOLS` **双向一致**（防漂移）

**不改任何判定** —— `DISABLED_SYMBOLS` 本身一行未动，本模块纯登记 + 校验。

用法：
    python disabled_contract.py                  # 人类可读报告，exit 0=合规 / 1=违规
    python disabled_contract.py --json           # 机器可读
    python disabled_contract.py --require-live   # 严格模式：必须与生产配置对账，读不到即违规

两档强度：能导入 `four_dim_strategy` → **完整档**（登记表 + 双向对账）；
导入失败 → **降级档**（仅登记表内部一致性，**显式声明未对账**，不产生假阳性）。
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. 允许的原因码（可核查条件）—— 只有这四类合规
# ---------------------------------------------------------------------------
ALLOWED_REASON_CODES = {
    "NO_DATA": "无数据（合约未上市 / 历史缺失 / 数据源不可用）",
    "NO_PERMISSION": "无权限（订阅 / 通道 / 资金权限不足）",
    "BLACKLIST": "已黑名单（结构性判死：品种或策略本身不可用）",
    "HISTORY_ALL_NEG": "历史全灭（walk-forward OOS 负期望，模型 + 实盘双确认）",
}

# 禁止的原因类（**不可核查**）—— 出现即校验失败。
# 对应 cangjie 对短路规则的要求：不许拿「结果不好」当理由。
FORBIDDEN_REASON_PATTERNS = (
    "结果不好",
    "表现差",
    "效果不佳",
    "不满意",
    "感觉",
    "试试",
    "不好用",
    "亏得多",
)

# ---------------------------------------------------------------------------
# 2. 登记表 —— 每个被禁品种的原因码 + 证据 + 入禁日 + 是否可自适应恢复
#
#    证据一律取自 `four_dim_strategy.py` 既有注释原文（**不新造数据**）：
#      · 2026-08-11 全市场校准（walk-forward OOS 负期望）
#      · 2026-08-13 追加 hc（模型 + 实盘双确认真死）
#      · 2026-08-17 全市场 OOS 日线版应用，加禁 MA/PR
# ---------------------------------------------------------------------------
DISABLED_REASONS: dict[str, dict] = {
    # ── 2026-08-11 全市场校准：walk-forward OOS 负期望 ──
    "au": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.302", "since": "2026-08-11", "auto_recover": False},
    "i": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.016", "since": "2026-08-11", "auto_recover": False},
    "eg": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.180", "since": "2026-08-11", "auto_recover": False},
    "m": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.029", "since": "2026-08-11", "auto_recover": False},
    "a": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.083", "since": "2026-08-11", "auto_recover": False},
    "b": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.099", "since": "2026-08-11", "auto_recover": False},
    "rr": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.261", "since": "2026-08-11", "auto_recover": False},
    "RM": {"code": "HISTORY_ALL_NEG", "evidence": "OOS expR −0.073", "since": "2026-08-11", "auto_recover": False},
    # ── 2026-08-13 追加：近期 walk-forward 全阈值负，模型 + 实盘双确认真死 ──
    "hc": {
        "code": "HISTORY_ALL_NEG",
        "evidence": "近期 walk-forward 全阈值负 −0.62 / 胜 18%（双确认真死）",
        "since": "2026-08-13",
        "auto_recover": True,
    },
    # ── 2026-08-17 OOS 日线版应用：整改后仍负 / 较 v12 退化 ──
    "MA": {
        "code": "HISTORY_ALL_NEG",
        "evidence": "整改后日线 on 负且较 v12 退化（2026-08-17 OOS 日线版）",
        "since": "2026-08-17",
        "auto_recover": False,
    },
    "PR": {
        "code": "HISTORY_ALL_NEG",
        "evidence": "整改后日线负 + 5m 双负（2026-08-17 OOS 日线版）",
        "since": "2026-08-17",
        "auto_recover": False,
    },
}

_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# 3. 读取「活」集合（与生产模块对账，防漂移）
# ---------------------------------------------------------------------------
def load_live_sets() -> tuple[set | None, set | None]:
    """从 four_dim_strategy 读 DISABLED_SYMBOLS / AUTO_RECOVER_SYMBOLS。

    导入失败（如缺 numpy）时返回 (None, None)，由调用方降级处理。
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from four_dim_strategy import AUTO_RECOVER_SYMBOLS, DISABLED_SYMBOLS  # noqa: PLC0415

        return set(DISABLED_SYMBOLS), set(AUTO_RECOVER_SYMBOLS)
    except Exception:  # noqa: BLE001
        return None, None


# ---------------------------------------------------------------------------
# 4. 校验器
# ---------------------------------------------------------------------------
def validate(
    disabled_symbols: set | None = None,
    auto_recover: set | None = None,
    *,
    live_available: bool | None = None,
) -> list[str]:
    """返回违规清单（空 = 合规）。

    两档校验强度，取决于能否拿到「活」集合（真实 DISABLED_SYMBOLS / AUTO_RECOVER_SYMBOLS）：

    * **完整档**（live_available=True）—— 登记表内部一致性 **+** 与生产模块双向对账
      （缺登记 / 孤儿 / 恢复标志不一致）。仅在能导入 `four_dim_strategy` 时可用。
    * **降级档**（live_available=False）—— **只做登记表内部一致性**
      （原因码合法 / 证据非空 / 无禁止措辞 / 日期格式）。

    ⚠️ 降级档**必须跳过**所有依赖活集合的判据：读不到生产配置时，**恒不应**判「缺登记 /
    孤儿 / 恢复标志不一致」—— 那是对着空集合比对，属**假阳性**（治理门最忌讳的环境噪音）。

    :param disabled_symbols: 活集合；显式传入即视为 live_available=True（反测用）。
    :param auto_recover: 同上。
    :param live_available: None=自动探测（未显式传集合时尝试 import）。
    """
    if live_available is None:
        if disabled_symbols is None or auto_recover is None:
            live_disabled, live_auto = load_live_sets()
            if live_disabled is not None:
                disabled_symbols, auto_recover, live_available = live_disabled, live_auto, True
            else:
                live_available = False
        else:
            live_available = True

    v: list[str] = []

    if live_available:
        # ① 缺登记（依赖活集合）
        for sym in sorted(disabled_symbols - set(DISABLED_REASONS)):
            v.append(f"[缺登记] 被禁品种 `{sym}` 在 DISABLED_SYMBOLS 里，但 DISABLED_REASONS 无对应条目")

        # ② 孤儿（登记了但并未被禁）（依赖活集合）
        for sym in sorted(set(DISABLED_REASONS) - disabled_symbols):
            v.append(f"[孤儿] 登记项 `{sym}` 存在，但该品种并不在 DISABLED_SYMBOLS 里")

    # ③ 逐条目字段校验（不依赖活集合）
    for sym, rec in sorted(DISABLED_REASONS.items()):
        code = rec.get("code")
        evidence = str(rec.get("evidence", ""))
        since = str(rec.get("since", ""))

        if code not in ALLOWED_REASON_CODES:
            v.append(f"[非法原因码] `{sym}` 的 code={code!r} 不在 {sorted(ALLOWED_REASON_CODES)} 内")

        if not evidence.strip():
            v.append(f"[缺证据] `{sym}` 的 evidence 为空 —— 原因必须可核查")

        for pat in FORBIDDEN_REASON_PATTERNS:
            if pat in evidence or pat in str(code or ""):
                v.append(f"[禁止措辞] `{sym}` 的原因命中不可核查词「{pat}」—— 短路禁用「结果不好」当理由")

        if not _SINCE_RE.match(since):
            v.append(f"[日期格式] `{sym}` 的 since={since!r} 不是 YYYY-MM-DD")

        if live_available:  # ④ 恢复标志一致性（依赖活集合）
            declared = bool(rec.get("auto_recover", False))
            actual = sym in auto_recover
            if declared != actual:
                v.append(
                    f"[恢复标志不一致] `{sym}` 登记 auto_recover={declared}，"
                    f"但 AUTO_RECOVER_SYMBOLS {'含' if actual else '不含'}它"
                )

    return v


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------
def main() -> int:
    require_live = "--require-live" in sys.argv
    disabled, auto = load_live_sets()
    degraded = disabled is None
    violations = validate(disabled, auto, live_available=not degraded)

    # 严格模式：治理门要求「必须与生产配置对账」。读不到 → 视为违规（不许静默降级成绿）。
    if require_live and degraded:
        violations.append("[降级] 无法导入 four_dim_strategy，未能与生产配置对账 —— --require-live 要求必须对账")

    if "--json" in sys.argv:
        print(
            json.dumps(
                {
                    "degraded_no_live_import": degraded,
                    "live_compared": not degraded,
                    "require_live": require_live,
                    "allowed_codes": sorted(ALLOWED_REASON_CODES),
                    "forbidden_patterns": list(FORBIDDEN_REASON_PATTERNS),
                    "n_disabled": len(DISABLED_REASONS),
                    "violations": violations,
                    "pass": not violations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if violations else 0

    print("=" * 66)
    print("  短路原因契约 —— DISABLED_SYMBOLS 原因登记与校验")
    print("=" * 66)
    if degraded:
        print("⚠️  无法导入 four_dim_strategy —— **降级档**：仅校验登记表内部一致性，")
        print("    **本次未与生产配置对账**（缺登记 / 孤儿 / 恢复标志一致性未判）。")
        print("    如需完整校验，请用装了依赖的解释器重跑，或加 --require-live 强制对账。\n")
    else:
        print("✅ 完整档：已与 four_dim_strategy 对账（登记表一致性 + 双向一致）。\n")
    print(f"被禁品种 {len(DISABLED_REASONS)} 个 · 允许原因码 {sorted(ALLOWED_REASON_CODES)}")
    print()
    print(f"{'品种':<5} {'原因码':<16} {'入禁日':<12} {'可恢复':<8} 证据")
    print("-" * 66)
    for sym, rec in sorted(DISABLED_REASONS.items()):
        print(
            f"{sym:<5} {rec['code']:<16} {rec['since']:<12} "
            f"{'是' if rec.get('auto_recover') else '否':<8} {rec['evidence']}"
        )
    print("-" * 66)
    if violations:
        print(f"\n❌ 契约违规 {len(violations)} 项：")
        for line in violations:
            print(f"   · {line}")
        return 1
    if degraded:
        print("\n✅ 登记表内部一致（⚠️ 但为降级档，未与生产配置对账）。")
    else:
        print("\n✅ 契约合规：每个被禁品种都有可核查原因，且与 DISABLED_SYMBOLS 一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
