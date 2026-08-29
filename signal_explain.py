"""
signal_explain.py · #4 信号解释（确定性 driver 解释器 + 可选 LLM 增强层）
=========================================================================
为什么不用纯 LLM：
  · 本期货盯盘项目的 live runner 内没有任何 LLM 客户端（push_notify 仅做 Telegram/Bark/企微推送，
    long_hu_bang 仅抓交易所数据；preflight 里的「云端LLM叙事」只是一句计数，无真实调用）。
  · 对交易纪律工具，确定性解释优于 LLM：零幻觉、即时、免费、可审计、永不掉线。
  · 故主路径 = 确定性解释；LLM 仅作为「可选增强层」，且仅当配置了 DEEPSEEK_API_KEY 时才启用，
    调用失败一律回退确定性解释（不影响主流程）。

产出：explain_signal(sig, pipe) -> {
  "summary": 一句话综述(结论先行),
  "bullets": [逐维解释要点],
  "llm_prompt": 可直接喂 LLM 的结构化提示(若日后接入真实 LLM)
}
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_DRIFT_CACHE = {"t": 0.0, "data": None}
_DRIFT_TTL = 300  # 漂移报告 5min 缓存，避免每信号重复读盘


def _load_drift():
    now = time.time()
    if _DRIFT_CACHE["data"] is not None and (now - _DRIFT_CACHE["t"]) < _DRIFT_TTL:
        return _DRIFT_CACHE["data"]
    try:
        d = json.load(open(os.path.join(HERE, "calibration_drift.json"), encoding="utf-8"))
    except Exception:
        d = None
    _DRIFT_CACHE["t"] = now
    _DRIFT_CACHE["data"] = d
    return d


def _drift_status(symbol):
    d = _load_drift()
    if not d:
        return None
    for it in d.get("items", []):
        if it.get("symbol") == symbol:
            return it
    return None


# P1-② 门控品种定性提示（确定性文字，无需 LLM）：让被动态门控暂停发信号的品种
# 也能在解释层/面板露出"为什么没信号 + 定性建议"，补覆盖缺口。
_SYMBOL_CN = {"jd": "鸡蛋", "lh": "生猪", "FG": "玻璃", "SA": "纯碱", "JM": "焦煤", "J": "焦炭"}


def explain_gated(symbol):
    """若 symbol 处于 broken + papertrack_gated（已被动态门控暂停发信号），
    返回结构化定性提示；否则返回 None（非门控品种不插手）。"""
    d = _drift_status(symbol)
    if not d or d.get("status") != "broken" or not d.get("papertrack_gated"):
        return None
    name = _SYMBOL_CN.get(symbol, symbol)
    expR = d.get("current_expR")
    wr = d.get("current_win_rate")
    oos = d.get("calibrated_oos")
    bullets = [
        "校准漂移状态：broken（近期表现已判失效，动态门控已暂停发信号，风险已控）。",
        "期望R(current_expR)=%s，近期胜率=%s%%%s"
        % (
            expR,
            (wr * 100) if wr is not None else "?",
            ("，校准外样本期望R(calibrated_oos)=%s" % oos) if oos is not None else "",
        ),
        "门控原因：papertrack 近期胜率<1/3 或累计R<0，自动暂停发信号（守住房门，非模型故障）。",
        "定性建议：当前模型对该品种不提供做多信号——勿追多；若已有持仓建议观望/择机减；"
        "如需做空须另寻独立证据链（本模型当前不覆盖）。",
    ]
    summary = (
        "%s(%s) 模型当前判负向（期望R=%s），动态门控已暂停发信号；"
        "定性建议：勿追多、持仓观望，做空需另寻证据。（情景分析，非确定性预测）"
    ) % (name, symbol, expR)
    return {
        "symbol": symbol,
        "name": name,
        "status": "broken",
        "gated": True,
        "expR": expR,
        "win_rate": wr,
        "calibrated_oos": oos,
        "summary": summary,
        "bullets": bullets,
        "advice": "勿追多；已有持仓建议观望/择机减；如需做空须另寻独立证据链，本模型当前不提供做多信号。",
    }


def explain_signal(sig, pipe=None):
    """确定性信号解释：从信号结构化驱动因子生成自然语言阐释。
    不依赖任何外部服务；任何异常都回退到 sig 自带 reason。
    P1-②：若品种已被动态门控暂停发信号，直接返回定性提示（补覆盖缺口）。"""
    sym = sig.get("symbol")
    if sym:
        try:
            _dg = _drift_status(sym)
            if _dg and _dg.get("status") == "broken" and _dg.get("papertrack_gated"):
                return explain_gated(sym)
        except Exception:
            pass
    try:
        return _explain(sig, pipe)
    except Exception as _e:
        return {
            "summary": sig.get("reason", "信号触发"),
            "bullets": [sig.get("reason", "")],
            "llm_prompt": "",
            "error": str(_e)[:120],
        }


def _explain(sig, pipe):
    p = sig.get("pipeline", {}) or {}
    rg = sig.get("risk_gate", {}) or {}
    direction = sig.get("direction", "中性")
    sym = sig.get("symbol", "?")
    name = sig.get("name", sym)

    bullets = []
    # ① 方向 + 技术触发
    t5 = p.get("T_5m")
    regime = p.get("regime")
    bias_g = p.get("bias_G")
    dir_word = "做多" if direction == "多" else ("做空" if direction == "空" else "中性")
    conv = p.get("conv") or ""
    aligned = ("放行" in str(conv)) or (bias_g is not None and abs(float(bias_g or 0)) >= 30)
    bullets.append(
        f"技术面「{dir_word}」触发：5分钟趋势强度 T_5m={t5}，当前 regime={regime}；"
        f"背景偏置 bias_G={bias_g}（{'同向共振、放行' if aligned else '需结合其他维度确认'}）。"
    )

    # ② 基本面（含 #1 信息维度 nudges）
    f_bias = p.get("F_bias")
    info_part = ""
    if pipe:
        fs = pipe.get("F_source")
        ia = pipe.get("info_adj") or {}
        if fs == "info_override" and ia:
            items = ia.get("items", [])
            if items:
                info_part = "；信息维度近况：" + "；".join(
                    f"{it.get('text', '')}({it.get('score', 0):+.2f})" for it in items[:3]
                )
    f_word = "偏多" if (f_bias or 0) > 0 else ("偏空" if (f_bias or 0) < 0 else "中性")
    bullets.append(f"基本面 F={f_bias}（{f_word}）{info_part}。")

    # ③ 资金流
    c = p.get("C_score")
    c_word = "正向支撑" if (c or 0) > 0 else ("偏弱/中性" if (c or 0) < 0 else "中性")
    bullets.append(f"资金流维度 C_score={c}（{c_word}）。")

    # ④ 风控与手数
    kelly = rg.get("kelly_mult")
    lots = sig.get("lots")
    gate_word = "通过" if rg.get("pass") else "未过(温和提示)"
    extra = ""
    if sig.get("portfolio_reduced"):
        extra = "；组合层相关性/预算已降仓"
    if sig.get("risk_scale") is not None and sig["risk_scale"] < 1.0:
        extra += f"；事件/回撤闸门缩放×{sig['risk_scale']}"
    bullets.append(
        f"风控：闸门{gate_word}，凯利缩放×{kelly}，计划 {lots} 手；止损距 {sig.get('stop_dist')} 点{extra}。"
    )

    # ④b GBM/GARCH 波动率动力学与降仓（#7 续，live 专属）
    gbm = sig.get("gbm_garch")
    if gbm:
        _VMAP = {
            "normal": "正常",
            "low": "低",
            "low-vol": "低",
            "mid": "中",
            "中": "中",
            "high": "高",
            "高": "高",
            "extreme": "极高",
            "极高": "极高",
        }
        vs = gbm.get("vol_state")
        vs_cn = _VMAP.get(vs, vs or "?")
        gv = gbm.get("garch_vol")
        rs = gbm.get("risk_scale")
        tm = gbm.get("thr_mult")
        fwd = gbm.get("fwd") or {}
        f5 = fwd.get("5") or fwd.get(5)
        _parts = [f"波动率状态={vs_cn}" + (f"（GARCH 条件波动 {gv}%）" if gv is not None else "")]
        if rs is not None and rs < 1.0:
            _parts.append(f"高波动自动降仓×{rs}")
        if tm is not None:
            _parts.append(f"触发阈值乘数×{tm}")
        if f5:
            _parts.append(
                f"5日情景：期望{f5.get('exp_ret')}%/下行VaR{f5.get('var95')}%/价格区间{f5.get('lo')}~{f5.get('hi')}%"
            )
        bullets.append("GBM/GARCH 波动率动力学：" + "；".join(_parts) + "。")

    # ⑤ 校准漂移状态（来自 #3 漂移闭环报告）
    drift = _drift_status(sym)
    if drift:
        st = drift.get("status")
        if st == "broken":
            bullets.append(
                "⚠️ 校准漂移：该品种近期表现已判为「失效(broken)」，本信号依赖动态门控，建议谨慎轻仓或观望，勿盲目加注。"
            )
        elif st == "drift":
            bullets.append("⚠️ 校准漂移：该品种近期表现衰减(drift)，参数可能需重校，注意仓位收敛。")
        elif st == "healthy":
            bullets.append("校准状态：近期表现符合校准(healthy)，模型可信度正常。")

    # 综合一句话（结论先行）
    summary = (
        f"{name}({sym}) 触发{dir_word}信号：技术面 {bias_g} 共振 + 基本面{f_word}"
        f" + 资金面{c_word}，风控放行计划 {lots} 手。"
        f"（情景分析，非确定性预测）"
    )

    llm_prompt = _build_llm_prompt(sig, p, bullets)
    return {"summary": summary, "bullets": bullets, "llm_prompt": llm_prompt}


def _build_llm_prompt(sig, p, bullets):
    return (
        "你是期货风控教练。基于以下确定性信号因子，用中文口语化解释这笔信号的"
        "触发逻辑与主要风险（不超过120字，结论先行，并明确标注为情景分析而非确定性预测）：\n"
        + "\n".join(bullets)
        + f"\n\n原始信号摘要：{sig.get('reason', '')}"
    )


def llm_explain(prompt):
    """可选 LLM 增强层：仅当配置 DEEPSEEK_API_KEY(+可选 DEEPSEEK_BASE_URL/MODEL) 时调用。
    任何失败一律返回 None（主流程回退确定性解释）。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    url = base + "/chat/completions"
    try:
        import urllib.request

        payload = json.dumps(
            {
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.3,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
