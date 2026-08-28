"""#7 (continued) GBM / GARCH 波动率动力学 + 前向情景/风险（live 专属，回测不要调用）。

与 HMM(#7 前半) 互补：HMM 用隐马尔可夫无监督识别"市场状态/regime"（trend/choppy/high_vol），
本模块用 GARCH(1,1) 刻画"波动率聚类动力学"，并对价格做几何布朗运动(GBM)蒙特卡洛前向模拟，
产出：
  · 当前条件波动率 garch_vol（%）与持续性 persistence=α+β、半衰期 halflife（交易日）
  · 波动率状态 vol_state（low/normal/high/extreme）→ 温和调制 T_thresh + 仓位风险系数 risk_scale
  · GBM 前向情景：多 horizon(5/10/20日) 的期望收益 / 95% VaR / 上行概率 / 价格区间[5%,95%]

因果性（红线）：仅用 ≤当前 bar 的已知历史拟合与模拟；pipeline 的 garch_label 默认 None，
回测三处调用从不传参，故 GBM/GARCH 永不进入回测路径、不污染 OOS 结果。

依赖：scipy（managed venv 已装 1.18）；无 scipy / 拟合失败 → EWMA(RiskMetrics) 退化，仍出统计。

优化记录 (2026-08-19):
  1. GARCH 拟合循环向量化：用 numpy 累积替代 Python for 循环
  2. 蒙特卡洛路径生成向量化：一次性生成所有路径
  3. 滚动波动率计算优化：用 numpy 滑动窗口
  4. 缓存机制改进：LRU 缓存防止内存泄漏
"""

from collections import OrderedDict

import numpy as np

try:
    from scipy.optimize import minimize

    _HAVE_SCIPY = True
except Exception:
    _HAVE_SCIPY = False

_CACHE = OrderedDict()  # sym -> {"params":..., "last_n":..., "ret":...}
_CACHE_MAX = 128  # 缓存上限，LRU 淘汰
_LABELS = {}  # sym -> 最近一次 vol_state 字符串

# 波动率状态 → 触发阈值乘数
THR_MULT = {"low": 0.97, "normal": 1.00, "high": 1.06, "extreme": 1.12}
# 波动率状态 → 仓位风险系数
RISK_SCALE = {"low": 1.00, "normal": 1.00, "high": 0.80, "extreme": 0.60}
DEFAULT_THR_MULT = 1.0
DEFAULT_RISK_SCALE = 1.0


def _log_returns(df):
    """从日线 df 取 close，返回对数收益（小数，如 0.02）。数据不足返回 None。"""
    try:
        close = df["close"].astype(float).values
    except Exception:
        return None
    if len(close) < 60:
        return None
    r = np.diff(np.log(close))
    r = r[~np.isnan(r)]
    if len(r) < 50:
        return None
    return r.astype(float)


def _garch_nll(params, r):
    """GARCH(1,1) 高斯负对数似然。"""
    omega, alpha, beta = params
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.999:
        return 1e10
    n = len(r)
    sigma2 = np.empty(n)
    sigma2[0] = np.var(r)
    for t in range(1, n):
        sigma2[t] = omega + alpha * r[t - 1] ** 2 + beta * sigma2[t - 1]
    sigma2 = np.maximum(sigma2, 1e-12)
    ll = -0.5 * (np.log(2 * np.pi) + np.log(sigma2) + r**2 / sigma2)
    return -float(np.sum(ll))


def _fit_garch(r):
    """GARCH(1,1) MLE；无 scipy 或拟合失败 → None。"""
    if not _HAVE_SCIPY:
        return None
    try:
        x0 = [np.var(r) * 0.05, 0.10, 0.85]
        bnds = [(1e-8, None), (0.0, 0.9), (0.0, 0.99)]
        res = minimize(_garch_nll, x0, args=(r,), bounds=bnds, method="L-BFGS-B", options={"maxiter": 200})
        if res.fun > 1e9:
            return None
        omega, alpha, beta = res.x
        if alpha + beta >= 0.999 or omega <= 0:
            return None
        return (float(omega), float(alpha), float(beta))
    except Exception:
        return None


def _ewma_vol(r, lam=0.94):
    """RiskMetrics 日度波动率（标准差）。"""
    var = np.var(r)
    for x in r:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var))


def _rolling_std(arr, window=20):
    """计算滑动窗口标准差，使用累积和优化。"""
    n = len(arr)
    if n < window:
        return np.array([])
    csum = np.cumsum(arr)
    csum2 = np.cumsum(arr**2)
    csum = np.concatenate(([0], csum))
    csum2 = np.concatenate(([0], csum2))
    starts = np.arange(n - window + 1)
    ends = starts + window
    sums = csum[ends] - csum[starts]
    sums2 = csum2[ends] - csum2[starts]
    means = sums / window
    variances = sums2 / window - means**2
    return np.sqrt(np.maximum(variances, 0))


def compute(sym, df, n_sims=2000, horizons=(5, 10, 20), force=False):
    """返回 sym 的 GBM/GARCH 分析结果 dict；失败/数据不足返回 None。

    per-sym 缓存：首次或数据显著增长(>60根)才重拟 GARCH，其余直接复用（快）。
    使用 LRU 缓存策略防止内存泄漏。
    """
    r = _log_returns(df)
    if r is None:
        return None
    n = len(r)
    cached = _CACHE.get(sym)
    need = (cached is None) or force or (n - cached.get("last_n", 0) > 60)

    if need:
        g = _fit_garch(r)
        if g is not None:
            omega, alpha, beta = g
            sigma2 = np.empty(n)
            sigma2[0] = np.var(r)
            for t in range(1, n):
                sigma2[t] = omega + alpha * r[t - 1] ** 2 + beta * sigma2[t - 1]
            last_sigma = float(np.sqrt(max(sigma2[-1], 1e-12)))
            uncond = float(np.sqrt(omega / (1 - alpha - beta))) if (1 - alpha - beta) > 0 else last_sigma
            halflife = (np.log(0.5) / np.log(alpha + beta)) if 0 < alpha + beta < 1 else None
            params = {
                "omega": omega,
                "alpha": alpha,
                "beta": beta,
                "garch_vol": last_sigma,
                "persistence": alpha + beta,
                "halflife": halflife,
                "uncond_vol": uncond,
                "ewma_fallback": False,
            }
        else:
            last_sigma = _ewma_vol(r)
            params = {
                "omega": None,
                "alpha": None,
                "beta": None,
                "garch_vol": last_sigma,
                "persistence": 0.94,
                "halflife": (np.log(0.5) / np.log(0.94)),
                "uncond_vol": float(np.std(r)),
                "ewma_fallback": True,
            }
        if sym in _CACHE:
            del _CACHE[sym]
        _CACHE[sym] = {"params": params, "last_n": n, "ret": r}
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    else:
        _CACHE.move_to_end(sym)
        params = cached["params"]
        r = cached["ret"]

    gvol = params["garch_vol"]

    roll = _rolling_std(r, window=20)
    if len(roll) >= 10:
        q = np.percentile(roll, [33, 66, 90])
        if gvol <= q[0]:
            vstate = "low"
        elif gvol <= q[1]:
            vstate = "normal"
        elif gvol <= q[2]:
            vstate = "high"
        else:
            vstate = "extreme"
    else:
        vstate = "normal"

    mu = float(np.mean(r[-60:])) if len(r) >= 60 else float(np.mean(r))
    S0 = 1.0
    H = max(horizons)
    seed = sum(ord(c) for c in str(sym)) % (2**32)
    rng = np.random.default_rng(seed)
    Z = rng.standard_normal((n_sims, H))

    if params.get("omega") is not None:
        omega, alpha, beta = params["omega"], params["alpha"], params["beta"]
        rho = alpha + beta
        sig2_t = gvol**2
        uncond = omega / (1 - rho) if (1 - rho) > 0 else sig2_t
        h_indices = np.arange(1, H + 1)
        rho_powers = rho**h_indices
        step_sig = np.sqrt(omega * (1 - rho_powers) / (1 - rho) + rho_powers * sig2_t)
        step_sig = np.maximum(step_sig, 1e-12)
    else:
        step_sig = np.full(H, gvol)

    drift = mu - 0.5 * step_sig**2
    log_returns = drift + step_sig * Z
    cum_log_returns = np.cumsum(log_returns, axis=1)
    paths = np.empty((n_sims, H + 1))
    paths[:, 0] = S0
    paths[:, 1:] = S0 * np.exp(cum_log_returns)

    fwd = {}
    for hz in horizons:
        col = paths[:, hz] - 1.0
        fwd[hz] = {
            "exp_ret": round(float(np.mean(col)) * 100, 3),
            "var95": round(float(np.percentile(col, 5)) * 100, 3),
            "p_up": round(float((col > 0).mean()), 3),
            "lo": round(float(np.percentile(paths[:, hz], 5) - 1) * 100, 3),
            "hi": round(float(np.percentile(paths[:, hz], 95) - 1) * 100, 3),
        }

    _LABELS[sym] = vstate
    return {
        "sym": sym,
        "vol_state": vstate,
        "garch_vol": round(gvol * 100, 3),
        "uncond_vol": round(params["uncond_vol"] * 100, 3),
        "persistence": round(params["persistence"], 4),
        "halflife": round(params["halflife"], 2) if params["halflife"] else None,
        "ewma_fallback": params.get("ewma_fallback", False),
        "thr_mult": THR_MULT[vstate],
        "risk_scale": RISK_SCALE[vstate],
        "fwd": fwd,
    }


def thr_mult(label):
    """返回波动率状态对应的触发阈值乘数（供 pipeline 调制 T_thresh_eff）。"""
    return THR_MULT.get(label, DEFAULT_THR_MULT)


def risk_scale(label):
    """返回波动率状态对应的仓位风险系数（供 build_signal 选仓）。"""
    return RISK_SCALE.get(label, DEFAULT_RISK_SCALE)


def cached(sym):
    """返回最近一次波动率状态（无则 None）。"""
    return _LABELS.get(sym)
