"""#7 HMM 市场状态识别（live 专属，回测不要调用）。

用日线 close 构造 [对数收益, 滚动波动率] 二维特征，GaussianHMM(4 态) 无监督聚类，
再按每态「收益均值符号 × 波动率分位」自动映射为语义标签：
    trend_up   上涨趋势（正收益、非高波动）
    trend_down 下跌趋势（负收益、非高波动）
    choppy     震荡（收益近零）
    high_vol   高波动（波动率处上四分位，无论方向）

因果性（红线）：仅用 ≤当前 bar 的已知历史训练与预测。模型在 live 启动时训练并缓存，
后续只做 predict，不引入任何未来数据。pipeline 的 hmm_label 默认 None，回测三处调用
从不传参，故 HMM 永不进入回测路径、不污染 OOS 结果。

无 hmmlearn 时退化为规则分桶（同样输出上述 4 类标签），保证可用。
"""

import numpy as np

try:
    from hmmlearn.hmm import GaussianHMM

    _HAVE_HMML = True
except Exception:
    _HAVE_HMML = False

_N_STATES = 4
_MODELS = {}  # sym -> (trained GaussianHMM | None, mu, sd)
_LABELS = {}  # sym -> 最近一次 label 字符串
_LAST_TRAIN = {}  # sym -> 训练时特征行数（用于判断是否需重训）

# HMM 态 → 触发阈值乘数（注入 pipeline 的 T_thresh_eff）
THR_MULT = {
    "trend_up": 0.90,  # 趋势明确 → 阈值略降，顺势更易触发
    "trend_down": 0.90,
    "choppy": 1.15,  # 震荡 → 阈值抬高，抑制假突破
    "high_vol": 1.25,  # 高波动 → 阈值抬高，控风险少出手
}
DEFAULT_THR_MULT = 1.0


def _features_raw(df):
    """从日线 df 取 close，构造二维原始特征 [log_ret, rolling_vol]，不标准化。"""
    try:
        close = df["close"].astype(float).values
    except Exception:
        return None
    if len(close) < 40:
        return None
    ret = np.diff(np.log(close))
    if len(ret) < 30:
        return None
    win = 20
    vol = np.array([float(np.std(ret[max(0, i - win) : i + 1])) for i in range(len(ret))])
    X = np.column_stack([ret, vol])
    X = X[~np.isnan(X).any(axis=1)]
    if len(X) < 30:
        return None
    return X


def _semantic_map(model, Xz):
    """把每个隐状态映射为语义标签（基于该态收益均值与波动率分位）。"""
    states = model.predict(Xz)
    stats = []
    for s in range(model.n_components):
        idx = np.where(states == s)[0]
        if len(idx) == 0:
            stats.append((0.0, 0.0))
        else:
            stats.append((float(Xz[idx, 0].mean()), float(Xz[idx, 1].mean())))
    vols = np.array([v for _, v in stats])
    vhi = float(np.percentile(vols, 75)) if len(vols) else 0.0
    out = {}
    for s, (mr, mv) in enumerate(stats):
        if vhi > 0 and mv >= vhi:
            out[s] = "high_vol"
        elif mr > 0.15:
            out[s] = "trend_up"
        elif mr < -0.15:
            out[s] = "trend_down"
        else:
            out[s] = "choppy"
    return [out[s] for s in states]


def _rule_label(Xz):
    """无 hmmlearn 退化：直接按标准化后的 ret/vol 分桶。"""
    ret = Xz[:, 0]
    vol = Xz[:, 1]
    vhi = float(np.percentile(vol, 75))
    mr = float(ret[-1])
    mv = float(vol[-1])
    if vhi > 0 and mv >= vhi:
        return "high_vol"
    if mr > 0.15:
        return "trend_up"
    if mr < -0.15:
        return "trend_down"
    return "choppy"


def compute_label(sym, df, force=False):
    """返回 sym 当前 HMM 市场状态标签（str）；失败/数据不足返回 None。
    首次或数据显著增长时训练并缓存模型，其余只 predict（快）。"""
    Xraw = _features_raw(df)
    if Xraw is None:
        return None
    n = len(Xraw)
    need_train = (sym not in _MODELS) or force or (n - _LAST_TRAIN.get(sym, 0) > 60)
    try:
        if need_train:
            mu = Xraw.mean(0)
            sd = Xraw.std(0)
            sd[sd == 0] = 1.0
            Xz = (Xraw - mu) / sd
            m = None
            if _HAVE_HMML:
                m = GaussianHMM(n_components=_N_STATES, covariance_type="diag", n_iter=80, random_state=0, tol=1e-3)
                m.fit(Xz)
            _MODELS[sym] = (m, mu, sd)
            _LAST_TRAIN[sym] = n
        else:
            m, mu, sd = _MODELS[sym]
            Xz = (Xraw - mu) / sd
        if _HAVE_HMML and m is not None:
            label = _semantic_map(m, Xz)[-1]
        else:
            label = _rule_label(Xz)
        _LABELS[sym] = label
        return label
    except Exception:
        # 任何拟合异常 → 退化规则，保证可用
        try:
            mu = Xraw.mean(0)
            sd = Xraw.std(0)
            sd[sd == 0] = 1.0
            return _rule_label((Xraw - mu) / sd)
        except Exception:
            return None


def thr_mult(label):
    """返回 HMM 态对应的触发阈值乘数（供 pipeline 调制 T_thresh_eff）。"""
    return THR_MULT.get(label, DEFAULT_THR_MULT)


def cached_label(sym):
    """返回最近一次标注结果（无则 None）。"""
    return _LABELS.get(sym)
