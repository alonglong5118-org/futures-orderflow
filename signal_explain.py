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


def _load_dotenv():
    """ponytail: 全项目此前从未加载 .env，导致 .env 里的 OLLAMA_MODEL/LLM_* 永远进不了
    os.environ，增强层形同虚设。这里在 import 时把同目录 .env 的缺失键补进 os.environ
    （setdefault：不覆盖已显式设置的环境变量），保证无论怎么启动都能读到配置。
    纯标准库解析，零第三方依赖。"""
    try:
        _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.exists(_env):
            return
        with open(_env, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()

def llm_enabled():
    """本地 Ollama 或云端 LLM_* 任一可用即 True。
    runner 用它决定是否异步补推 AI 解读——取代原先硬编码的 DEEPSEEK_API_KEY 检查
    （该检查与 .env 的 LLM_* 命名不一致，导致增强层从未真正触发）。
    ponytail: LLM_ENABLED=0 是硬总开关，必须先判——否则配了 OLLAMA_MODEL 就关不掉 LLM。"""
    if os.environ.get("LLM_ENABLED", "1") == "0":
        return False
    if os.environ.get("OLLAMA_MODEL"):
        return True
    return bool(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


def _timeout():
    return float(os.environ.get("LLM_TIMEOUT", 8))


def llm_explain(prompt):
    """可选 LLM 增强层：云端 LLM_* 优先（快/零维护，与 M2 一致），本地 Ollama 次之（离线灾备）。
    任何失败一律返回 None（主流程回退确定性解释），永不拖累信号主流程。"""
    return _cloud_explain(prompt) or _ollama_explain(prompt)


def _ollama_explain(prompt):
    """本地后端：需设置 OLLAMA_MODEL 才启用（如 deepseek-r1:14b）。
    ponytail: 本地默认 60s、云端默认 8s——实测 14B 冷启约 17s，沿用云端 8s 会让本地
    增强层永远超时回退 None；两条路径耗时量级不同，超时必须分开。"""
    model = os.environ.get("OLLAMA_MODEL")
    # LLM_ENABLED=0 为硬总开关：与 llm_enabled()/_cloud_explain 保持同一语义
    if not model or os.environ.get("LLM_ENABLED", "1") == "0":
        return None
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    # keep_alive: Ollama 的 /api/generate 拒绝字符串 "-1"（400 Bad Request），
    # 只接受整数 -1 或时长字符串（"5m"/"-1m"）。这里把纯数字/带负号数字 coerce 成 int。
    _ka = os.environ.get("OLLAMA_KEEP_ALIVE", "5m")
    try:
        _ka = int(_ka)
    except ValueError:
        pass
    try:
        import urllib.request

        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "keep_alive": _ka,
                "options": {
                    "temperature": 0.3,
                    "num_predict": int(os.environ.get("OLLAMA_NUM_PREDICT", 800)),
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            host + "/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=float(os.environ.get("OLLAMA_TIMEOUT", 60))) as r:
            data = json.loads(r.read())
        resp = (data.get("response") or "").strip()
        # 推理模型(R1)的 <think> 链会吃掉大半预算，并让推送消息变成几百字流水账；默认剥离，留结论。
        if resp and os.environ.get("OLLAMA_STRIP_THINK", "1") != "0":
            import re as _re

            resp = _re.sub(r"<think>.*?</think>", "", resp, flags=_re.S).strip()
        return resp or None
    except Exception:
        return None


def _cloud_explain(prompt):
    """云端 OpenAI 兼容后端：优先 .env 的 LLM_* 约定，兼容旧 DEEPSEEK_*。"""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key or os.environ.get("LLM_ENABLED", "1") == "0":
        return None
    base = (
        os.environ.get("LLM_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
    ).rstrip("/")
    try:
        import urllib.request

        payload = json.dumps(
            {
                "model": os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.3,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            base + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


if __name__ == "__main__":
    # 自检：① 确定性路径零依赖可用；② 无后端时 LLM 层安全回退 None
    _sig = {
        "symbol": "FG",
        "name": "玻璃",
        "direction": "多",
        "lots": 2,
        "stop_dist": 18,
        "pipeline": {
            "T_5m": 0.62,
            "regime": "trend",
            "bias_G": 45,
            "conv": "放行",
            "F_bias": 0.2,
            "C_score": 0.1,
        },
        "risk_gate": {"pass": True, "kelly_mult": 0.5},
    }
    _out = explain_signal(_sig)
    assert _out.get("summary") and _out.get("bullets"), _out
    for _k in ("OLLAMA_MODEL", "LLM_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ.pop(_k, None)
    assert llm_explain("ping") is None, "无后端时 llm_explain 必须返回 None"
    print("OK 确定性解释可用；LLM 层未配置时安全回退 None")
    print("summary:", _out["summary"])
    for _b in _out["bullets"]:
        print("  -", _b)
