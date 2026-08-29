"""
核心工具模块 · 边界条件 & 异常路径补充测试
========================================================

补充各核心工具模块的边界条件、极端输入、异常路径测试，
提升这些模块的测试覆盖率到 95%+。

覆盖模块：
  · kelly_utils           — Kelly 因子计算（边界参数）
  · gap_stop_utils        — 缺口击穿止损（脏数据 / 边界值）
  · take_profit_utils     — 止盈止损（极端场景）
  · price_protection      — 价格保护（各种非法输入）
  · corr_gate_utils       — 相关性闸门（异常数据）
  · signal_trigger_utils  — 信号触发（边界阈值）
  · risk_gate_utils       — 风控仓位（零值 / 负值）
  · anomaly_scan          — 异动扫描（空数据 / 脏数据）
  · hidden_pivot          — 隐秘枢轴（边界结构）
  · t_score_utils         — T 分数（边界统计）
"""

import math

import pytest

# ═══════════════════════════════════════════════════════════════════════════
#  1. kelly_utils — 边界参数测试
# ═══════════════════════════════════════════════════════════════════════════


class TestKellyEdgeCases:
    """Kelly 因子计算的边界条件测试。"""

    def test_edge_is_none(self):
        """edge 为 None → 返回中性 1.0。"""
        from kelly_utils import compute_kelly_factor

        assert compute_kelly_factor(None) == 1.0

    def test_edge_invalid_string(self):
        """edge 为非法字符串 → 返回 1.0（防御性转换）。"""
        from kelly_utils import compute_kelly_factor

        assert compute_kelly_factor("abc") == 1.0

    def test_edge_invalid_type(self):
        """edge 为列表等不可转 float 的类型 → 返回 1.0。"""
        from kelly_utils import compute_kelly_factor

        assert compute_kelly_factor([1, 2, 3]) == 1.0

    def test_kelly_min_greater_than_max(self):
        """kelly_min > kelly_max 时自动交换，不抛异常。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=0.5, kelly_min=1.2, kelly_max=0.6)
        # 交换后等价于 min=0.6, max=1.2，edge=0.5 应在中间
        assert 0.6 <= result <= 1.2

    def test_target_edge_zero(self):
        """target_edge 为 0 → 直接拉满到 kelly_max（异常配置保护）。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=0.1, target_edge=0.0)
        assert result == pytest.approx(1.2)  # kelly_max 默认 1.2

    def test_target_edge_negative(self):
        """target_edge 为负 → 同样拉满（异常配置保护）。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=0.1, target_edge=-0.5)
        assert result == pytest.approx(1.2)

    def test_negative_edge_clamped_to_zero(self):
        """负 edge 按 0 处理 → 返回 kelly_min。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=-0.3)
        assert result == pytest.approx(0.6)  # kelly_min 默认 0.6

    def test_cur_full_expR_invalid(self):
        """近景数据类型异常 → 退回远 edge 符号判断。"""
        from kelly_utils import compute_kelly_factor

        # 远 edge 为正 + 近景数据异常 → 允许杠杆放大
        result = compute_kelly_factor(edge=0.5, cur_full_expR="invalid")
        assert result > 1.0  # 正 edge → 允许 >1.0

    def test_cur_full_expR_negative_caps_at_1(self):
        """近景期望收益为负 → 强制封顶 1.0。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=0.5, cur_full_expR=-0.1)
        assert result == 1.0

    def test_invalid_kelly_params(self):
        """kelly_min/max/target_edge 类型异常 → 返回 1.0。"""
        from kelly_utils import compute_kelly_factor

        assert compute_kelly_factor(edge=0.5, kelly_min="bad") == 1.0
        assert compute_kelly_factor(edge=0.5, kelly_max="bad") == 1.0
        assert compute_kelly_factor(edge=0.5, target_edge="bad") == 1.0

    def test_edge_exactly_target(self):
        """edge 正好等于 target_edge → 返回 kelly_max。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=0.5, target_edge=0.5)
        assert result == pytest.approx(1.2)

    def test_edge_double_target(self):
        """edge 远超 target_edge → 封顶 kelly_max（不超配）。"""
        from kelly_utils import compute_kelly_factor

        result = compute_kelly_factor(edge=2.0, target_edge=0.5)
        assert result == pytest.approx(1.2)  # 封顶


# ═══════════════════════════════════════════════════════════════════════════
#  2. gap_stop_utils — 脏数据 & 边界值测试
# ═══════════════════════════════════════════════════════════════════════════


class TestGapStopEdgeCases:
    """缺口击穿止损的边界条件与脏数据测试。"""

    def test_ds_zero_no_trigger(self):
        """方向为 0 → 不触发，返回默认结果。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=0, px=24000, stop=25000, entry_price=26000)
        assert r["triggered"] is False
        assert r["is_adverse"] is False

    def test_stop_none(self):
        """止损价为 None → 不触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=24000, stop=None, entry_price=26000)
        assert r["triggered"] is False

    def test_entry_price_none(self):
        """入场价为 None → 不触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=24000, stop=25000, entry_price=None)
        assert r["triggered"] is False

    def test_invalid_price_string(self):
        """价格为非法字符串 → 防御性返回，不抛异常。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px="N/A", stop=25000, entry_price=26000)
        assert r["triggered"] is False

    def test_invalid_stop_string(self):
        """止损价为非法字符串 → 防御性返回。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=24000, stop="bad", entry_price=26000)
        assert r["triggered"] is False

    def test_price_as_string_number(self):
        """价格为数字字符串（如 "24000"）→ 正常转换并计算。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px="24000", stop="25000", entry_price="26000")
        assert r["triggered"] is True  # 多单，价格 24000 < 止损 25000，穿透 1000 点 = 0.5R? 等等
        # entry=26000, stop=25000 → oneR = 1000
        # px=24000, stop=25000 → pen = 1000
        # pen_ratio = 1.0 > 0.5 → 触发
        assert r["pen_ratio"] == pytest.approx(1.0)

    def test_oneR_zero_entry_equals_stop(self):
        """入场价等于止损价 → oneR=0，除零保护，不触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=25000, stop=25000, entry_price=25000)
        assert r["triggered"] is False
        assert r["oneR"] == 0.0
        assert r["pen_ratio"] == 0.0

    def test_boundary_exactly_half_R(self):
        """穿透正好等于 0.5R → 不触发（严格大于）。"""
        from gap_stop_utils import check_gap_stop_triggered

        # entry=26000, stop=25000 → oneR=1000, 0.5R=500
        # px=24500 → pen=500, 正好 0.5R → 不触发
        r = check_gap_stop_triggered(ds=1, px=24500, stop=25000, entry_price=26000)
        assert r["triggered"] is False
        assert r["pen_ratio"] == pytest.approx(0.5)

    def test_just_over_half_R(self):
        """穿透略大于 0.5R → 触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=24499, stop=25000, entry_price=26000)
        assert r["triggered"] is True

    def test_short_triggered(self):
        """空单缺口击穿 → 价格向上突破止损。"""
        from gap_stop_utils import check_gap_stop_triggered

        # 空单：entry=25000, stop=26000 → oneR=1000
        # px=27000 → 向上穿止损 1000 点 = 1R → 触发
        r = check_gap_stop_triggered(ds=-1, px=27000, stop=26000, entry_price=25000)
        assert r["triggered"] is True
        assert r["is_adverse"] is True

    def test_short_favorable_direction(self):
        """空单价格下跌（有利方向）→ 不触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=-1, px=24000, stop=26000, entry_price=25000)
        assert r["triggered"] is False
        assert r["is_adverse"] is False

    def test_long_favorable_direction(self):
        """多单价格上涨（有利方向）→ 不触发。"""
        from gap_stop_utils import check_gap_stop_triggered

        r = check_gap_stop_triggered(ds=1, px=27000, stop=25000, entry_price=26000)
        assert r["triggered"] is False
        assert r["is_adverse"] is False


# ═══════════════════════════════════════════════════════════════════════════
#  3. take_profit_utils — 极端场景测试
# ═══════════════════════════════════════════════════════════════════════════


class TestTakeProfitEdgeCases:
    """止盈止损的极端场景测试。"""

    def test_calc_exit_plan_zero_atr(self):
        """ATR 为 0 → 止损距离为 0，所有价位等于入场价。"""
        from take_profit_utils import calc_exit_plan

        r = calc_exit_plan(entry=100.0, dir_T=1, atr_val=0.0)
        assert r["stop"] == pytest.approx(100.0)
        assert r["t1"] == pytest.approx(100.0)
        assert r["t2"] == pytest.approx(100.0)
        assert r["stop_dist"] == pytest.approx(0.0)

    def test_calc_exit_plan_short_direction(self):
        """空单方向验证：止损在上方，止盈在下方。"""
        from take_profit_utils import calc_exit_plan

        r = calc_exit_plan(entry=100.0, dir_T=-1, atr_val=10.0, stop_atr_mult=1.0, rr_ratio=2.0)
        assert r["stop"] > 100.0  # 空单止损在入场上方
        assert r["t1"] < 100.0  # 空单 1R 止盈在入场下方
        assert r["t2"] < r["t1"]  # t2 比 t1 更低

    def test_calc_exit_plan_regime_coef(self):
        """regime 止损系数 > 1 → 止损距离增大。"""
        from take_profit_utils import calc_exit_plan

        r_normal = calc_exit_plan(entry=100.0, dir_T=1, atr_val=10.0, stop_atr_mult=1.5)
        r_wide = calc_exit_plan(entry=100.0, dir_T=1, atr_val=10.0, stop_atr_mult=1.5, regime_stop_coef=1.2)
        assert r_wide["stop_dist"] > r_normal["stop_dist"]
        assert r_wide["stop"] < r_normal["stop"]  # 多单止损更低

    def test_calc_exit_plan_tail_enabled(self):
        """尾仓启用时 tail_stop_dist 应为正值。"""
        from take_profit_utils import calc_exit_plan

        r = calc_exit_plan(entry=100.0, dir_T=1, atr_val=10.0, tail_enabled=True, tail_trail_R=2.0)
        assert r["tail_enabled"] is True
        assert r["tail_stop_dist"] == pytest.approx(r["stop_dist"] * 2.0)
        assert r["tail_pct"] == 0.25

    def test_sim_exit_no_bars(self):
        """空 bar 列表 → 不触发出场。"""
        from take_profit_utils import sim_exit_bars

        ep = {"stop": 90.0, "t1": 110.0, "t2": 120.0, "tail_enabled": False}
        exit_price, reason, idx = sim_exit_bars([], 1, 100.0, ep)
        assert reason == "no_exit"

    def test_sim_exit_stop_hit_first_bar(self):
        """第一根 bar 就打止损 → 立即止损出场。"""
        from take_profit_utils import sim_exit_bars

        ep = {"stop": 90.0, "t1": 110.0, "t2": 120.0, "tail_enabled": False}
        bars = [(95.0, 85.0)]  # high=95, low=85 穿过 stop=90
        exit_price, reason, idx = sim_exit_bars(bars, 1, 100.0, ep)
        assert reason == "stop"
        assert idx == 0

    def test_sim_exit_short_stop(self):
        """空单止损（向上突破）。"""
        from take_profit_utils import sim_exit_bars

        ep = {"stop": 110.0, "t1": 90.0, "t2": 80.0, "tail_enabled": False}
        bars = [(115.0, 105.0)]  # high=115 穿过 stop=110
        exit_price, reason, idx = sim_exit_bars(bars, -1, 100.0, ep)
        assert reason == "stop"

    def test_sim_exit_then_t2_then_tail(self):
        """t1 平半 → t2 进入尾仓 → 尾仓跟踪止盈。"""
        from take_profit_utils import sim_exit_bars

        ep = {
            "stop": 90.0,
            "t1": 110.0,
            "t2": 120.0,
            "tail_enabled": True,
            "tail_stop_dist": 15.0,
            "tail_pct": 0.25,
        }
        # bar0: 到 t1 → 平半
        # bar1: 到 t2 → 进尾仓
        # bar2: 继续冲高后回落 → 尾仓止损出场
        bars = [
            (112.0, 98.0),  # high=112 > t1=110, 触发 t1
            (125.0, 115.0),  # high=125 > t2=120, 触发 t2，进尾仓
            (130.0, 108.0),  # 冲高后回落，尾仓止损 = t2 - tail_stop = 120 - 15 = 105
            # low=108 > 105, 还没打到...
        ]
        exit_price, reason, idx = sim_exit_bars(bars, 1, 100.0, ep)
        assert reason == "t2"  # 第二根 bar 打到 t2，进入尾仓态（但尾仓还没打到）


# ═══════════════════════════════════════════════════════════════════════════
#  4. price_protection — 各种非法输入测试
# ═══════════════════════════════════════════════════════════════════════════


class TestPriceProtectionEdgeCases:
    """价格保护的各种非法输入测试。"""

    def test_validate_price_none(self):
        """价格为 None → 不合法。"""
        from price_protection import validate_price

        r = validate_price(None)
        assert r["valid"] is False
        assert "不能为空" in r["reason"]

    def test_validate_price_negative(self):
        """价格为负 → 不合法。"""
        from price_protection import validate_price

        r = validate_price(-100)
        assert r["valid"] is False
        assert "必须大于0" in r["reason"]

    def test_validate_price_zero(self):
        """价格为 0 → 不合法。"""
        from price_protection import validate_price

        r = validate_price(0)
        assert r["valid"] is False

    def test_validate_price_string_number(self):
        """数字字符串 → 正常转换。"""
        from price_protection import validate_price

        r = validate_price("1234.5")
        assert r["valid"] is True
        assert r["price"] == pytest.approx(1234.5)

    def test_validate_price_invalid_string(self):
        """非法字符串 → 不合法。"""
        from price_protection import validate_price

        r = validate_price("abc")
        assert r["valid"] is False
        assert "格式错误" in r["reason"]

    def test_validate_price_empty_string(self):
        """空字符串 → 不合法。"""
        from price_protection import validate_price

        r = validate_price("")
        assert r["valid"] is False

    def test_validate_price_valid_float(self):
        """正常浮点数 → 合法。"""
        from price_protection import validate_price

        r = validate_price(2580.5)
        assert r["valid"] is True
        assert r["price"] == pytest.approx(2580.5)

    def test_dir_sign_variations(self):
        """_dir_sign 各种方向表示法。"""
        from price_protection import _dir_sign

        # 多方
        assert _dir_sign("多") == 1
        assert _dir_sign("long") == 1
        assert _dir_sign("duo") == 1
        assert _dir_sign("buy") == 1
        assert _dir_sign("  LONG  ") == 1  # 带空格
        # 空方
        assert _dir_sign("空") == -1
        assert _dir_sign("short") == -1
        assert _dir_sign("kong") == -1
        assert _dir_sign("sell") == -1
        # 数值
        assert _dir_sign(1) == 1
        assert _dir_sign(-1) == -1
        assert _dir_sign(0.5) == 1
        assert _dir_sign(-0.5) == -1
        assert _dir_sign(0) == 0
        # 无效
        assert _dir_sign("unknown") == 0
        assert _dir_sign([]) == 0


# ═══════════════════════════════════════════════════════════════════════════
#  5. corr_gate_utils — 异常数据测试
# ═══════════════════════════════════════════════════════════════════════════


class TestCorrGateEdgeCases:
    """相关性闸门的异常数据测试。"""

    def test_no_history(self):
        """无历史数据 → 跳过，不处理。"""
        from corr_gate_utils import apply_corr_gate

        r = apply_corr_gate(T_score=80.0, C_score=70.0, corr_hist=None)
        assert r["applied"] is False
        assert r["dropped"] == "none"
        assert "无历史数据" in r["action"]

    def test_insufficient_history(self):
        """历史数据不足 → 跳过。"""
        from corr_gate_utils import apply_corr_gate

        hist = [[1.0, 2.0], [2.0, 3.0]]  # 只有 2 条，< 默认 min_history=10
        r = apply_corr_gate(T_score=80.0, C_score=70.0, corr_hist=hist)
        assert r["applied"] is False
        assert "不足" in r["action"]

    def test_invalid_history_format(self):
        """历史数据格式错误（元素不是列表） → 跳过。"""
        from corr_gate_utils import apply_corr_gate

        hist = [[1, 2]] * 10 + ["bad_data"]
        r = apply_corr_gate(T_score=80.0, C_score=70.0, corr_hist=hist, min_history=5)
        assert r["applied"] is False
        assert "格式错误" in r["action"]

    def test_all_same_values(self):
        """某维度全部相同（方差为 0）→ 跳过。"""
        from corr_gate_utils import apply_corr_gate

        hist = [[5.0, i] for i in range(15)]  # T 全是 5.0，无波动
        r = apply_corr_gate(T_score=80.0, C_score=70.0, corr_hist=hist)
        assert r["applied"] is False
        assert "无波动" in r["action"]

    def test_low_correlation_no_action(self):
        """低相关 → 正常计权，不处理。"""
        from corr_gate_utils import apply_corr_gate

        # 构造低相关数据
        hist = [[i, (-1) ** i * i] for i in range(1, 13)]
        r = apply_corr_gate(T_score=80.0, C_score=70.0, corr_hist=hist, gate=0.7)
        assert r["applied"] is False
        assert r["dropped"] == "none"

    def test_high_correlation_drops_weaker(self):
        """高相关 → 降权绝对值较小的维度。"""
        from corr_gate_utils import apply_corr_gate

        # 构造高度正相关数据
        hist = [[i, i * 1.1] for i in range(1, 13)]
        r = apply_corr_gate(T_score=50.0, C_score=80.0, corr_hist=hist, gate=0.7)
        assert r["applied"] is True
        # T=50 < C=80 → T 被降权
        assert r["T"] == pytest.approx(0.0)
        assert r["C"] == pytest.approx(80.0)
        assert r["dropped"] == "T"

    def test_pearson_corr_perfect_positive(self):
        """完全正相关 → 相关系数接近 1.0。"""
        from corr_gate_utils import _pearson_corr

        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        corr = _pearson_corr(x, y)
        assert corr == pytest.approx(1.0)

    def test_pearson_corr_perfect_negative(self):
        """完全负相关 → 相关系数接近 -1.0。"""
        from corr_gate_utils import _pearson_corr

        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        corr = _pearson_corr(x, y)
        assert corr == pytest.approx(-1.0)

    def test_pearson_corr_mismatched_length(self):
        """长度不一致 → 返回 None。"""
        from corr_gate_utils import _pearson_corr

        assert _pearson_corr([1, 2, 3], [1, 2]) is None

    def test_pearson_corr_too_short(self):
        """数据点太少 → 返回 None。"""
        from corr_gate_utils import _pearson_corr

        assert _pearson_corr([1], [2]) is None


# ═══════════════════════════════════════════════════════════════════════════
#  6. signal_trigger_utils — 边界阈值测试
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalTriggerEdgeCases:
    """信号触发的边界阈值测试。"""

    def test_bias_FC_zero_inputs(self):
        """F 和 C 都为 0 → bias_FC = 0。"""
        from signal_trigger_utils import compute_bias_FC

        assert compute_bias_FC(0, 0) == pytest.approx(0.0)

    def test_hard_veto_dir_zero(self):
        """方向为 0 → 不否决（无方向的信号不适用否决）。"""
        from signal_trigger_utils import check_hard_veto

        vetoed, reason = check_hard_veto(bias_FC=-30.0, dir_T=0)
        assert vetoed is False
        assert reason == ""

    def test_hard_veto_same_direction(self):
        """bias_FC 与方向同向 → 不否决。"""
        from signal_trigger_utils import check_hard_veto

        vetoed, _ = check_hard_veto(bias_FC=30.0, dir_T=1)
        assert vetoed is False

    def test_hard_veto_exactly_at_threshold(self):
        """正好达到阈值 → 触发否决（>=）。"""
        from signal_trigger_utils import check_hard_veto

        vetoed, reason = check_hard_veto(bias_FC=-25.0, dir_T=1, fc_hard=25.0)
        assert vetoed is True
        assert "反向硬否决" in reason

    def test_hard_veto_below_threshold(self):
        """低于阈值 → 不否决。"""
        from signal_trigger_utils import check_hard_veto

        vetoed, _ = check_hard_veto(bias_FC=-24.9, dir_T=1, fc_hard=25.0)
        assert vetoed is False

    def test_fc_confirmation_dir_zero(self):
        """方向为 0 → 不确认。"""
        from signal_trigger_utils import check_fc_confirmation

        assert check_fc_confirmation(bias_FC=30.0, dir_T=0) is False

    def test_fc_confirmation_bias_zero(self):
        """bias_FC 为 0 → 不确认。"""
        from signal_trigger_utils import check_fc_confirmation

        assert check_fc_confirmation(bias_FC=0.0, dir_T=1) is False

    def test_fc_confirmation_exactly_threshold(self):
        """正好达到确认阈值 → 触发确认（>=）。"""
        from signal_trigger_utils import check_fc_confirmation

        assert check_fc_confirmation(bias_FC=25.0, dir_T=1, fc_confirm=25.0) is True

    def test_effective_threshold_with_confirmation(self):
        """F/C 同向确认 → 阈值降低。"""
        from signal_trigger_utils import compute_effective_threshold

        t = compute_effective_threshold(T_thresh_eff=60.0, fc_confirmed=True, confirm_relief=0.85)
        assert t == pytest.approx(51.0)  # 60 * 0.85

    def test_effective_threshold_without_confirmation(self):
        """无确认 → 阈值不变。"""
        from signal_trigger_utils import compute_effective_threshold

        t = compute_effective_threshold(T_thresh_eff=60.0, fc_confirmed=False)
        assert t == pytest.approx(60.0)

    def test_same_direction_neutral_background(self):
        """中性背景（bias_G 接近 0）→ 也算同向。"""
        from signal_trigger_utils import check_same_direction

        assert check_same_direction(bias_G=0.0, dir_T=1) is True
        assert check_same_direction(bias_G=1e-7, dir_T=-1) is True

    def test_same_direction_dir_zero(self):
        """方向为 0 → 不同向。"""
        from signal_trigger_utils import check_same_direction

        assert check_same_direction(bias_G=50.0, dir_T=0) is False


# ═══════════════════════════════════════════════════════════════════════════
#  7. risk_gate_utils — 零值 / 负值边界测试
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGateEdgeCases:
    """风控仓位计算的边界条件测试。"""

    def test_calc_risk_lots_zero_stop(self):
        """止损点数为 0 → 风险预算为 0 手。"""
        from risk_gate_utils import calc_risk_lots

        assert calc_risk_lots(equity=100000, risk_pct=1.0, stop_pts=0, multiplier=100) == 0

    def test_calc_risk_lots_negative_stop(self):
        """止损点数为负 → 风险预算为 0 手。"""
        from risk_gate_utils import calc_risk_lots

        assert calc_risk_lots(equity=100000, risk_pct=1.0, stop_pts=-10, multiplier=100) == 0

    def test_calc_risk_lots_zero_multiplier(self):
        """合约乘数为 0 → 0 手。"""
        from risk_gate_utils import calc_risk_lots

        assert calc_risk_lots(equity=100000, risk_pct=1.0, stop_pts=10, multiplier=0) == 0

    def test_calc_risk_lots_exact_one_hand(self):
        """正好够 1 手 → 返回 1。"""
        from risk_gate_utils import calc_risk_lots

        # equity=10000, risk_pct=10% → 风险预算 1000
        # stop_pts=10, multiplier=100 → 每手风险 1000
        # 正好 1 手
        assert calc_risk_lots(equity=10000, risk_pct=10.0, stop_pts=10, multiplier=100) == 1

    def test_calc_min_lot_floor_zero_risk(self):
        """风险预算为 0 但有风险 → 最小 1 手兜底。"""
        from risk_gate_utils import calc_min_lot_floor

        N, over = calc_min_lot_floor(0, 1000.0)
        assert N == 1
        assert over is True

    def test_calc_min_lot_floor_zero_per_hand(self):
        """每手风险为 0 → 不兜底（无意义）。"""
        from risk_gate_utils import calc_min_lot_floor

        N, over = calc_min_lot_floor(0, 0.0)
        assert N == 0
        assert over is False

    def test_apply_kelly_zero_risk(self):
        """风险预算为 0 → Kelly 缩放后还是 0。"""
        from risk_gate_utils import apply_kelly_scaling

        assert apply_kelly_scaling(0, 1.5) == 0

    def test_apply_kelly_scales_down(self):
        """Kelly 系数 < 1 → 仓位减少。"""
        from risk_gate_utils import apply_kelly_scaling

        assert apply_kelly_scaling(10, 0.5) == 5

    def test_apply_kelly_min_one(self):
        """缩放后 < 1 → 至少 1 手。"""
        from risk_gate_utils import apply_kelly_scaling

        assert apply_kelly_scaling(1, 0.6) == 1  # 0.6 四舍五入 = 1

    def test_calc_margin_zero_price(self):
        """价格为 0 → 保证金约束为 0 手。"""
        from risk_gate_utils import calc_margin_lots

        assert calc_margin_lots(100000, 30.0, 0, 100, 0.12) == 0

    def test_calc_t_strength_zero_thresh(self):
        """阈值为 0 → 不缩放，返回 1.0。"""
        from risk_gate_utils import calc_t_strength_scale

        assert calc_t_strength_scale(50.0, 0) == pytest.approx(1.0)

    def test_calc_t_strength_none_thresh(self):
        """阈值为 None → 返回 1.0。"""
        from risk_gate_utils import calc_t_strength_scale

        assert calc_t_strength_scale(50.0, None) == pytest.approx(1.0)

    def test_calc_t_strength_weak_signal(self):
        """弱信号（刚过阈值）→ 半仓。"""
        from risk_gate_utils import calc_t_strength_scale

        # t_strength 刚过阈值（ratio = 1/(1.5) ≈ 0.667）
        # 但最小是 0.5，所以应该是 0.667
        result = calc_t_strength_scale(t_strength=40.0, t_thresh=40.0)
        assert result == pytest.approx(1.0 / 1.5, abs=0.01)
        assert 0.5 <= result <= 1.0

    def test_calc_t_strength_strong_signal(self):
        """强信号 → 满仓 1.0。"""
        from risk_gate_utils import calc_t_strength_scale

        result = calc_t_strength_scale(t_strength=100.0, t_thresh=40.0)
        assert result == pytest.approx(1.0)

    def test_deduct_held_no_holding(self):
        """无持仓 → 不扣减。"""
        from risk_gate_utils import deduct_held_lots

        assert deduct_held_lots(5, 0, 10) == 5

    def test_deduct_held_full_position(self):
        """已满仓 → 新开 0 手。"""
        from risk_gate_utils import deduct_held_lots

        assert deduct_held_lots(5, 10, 10) == 0

    def test_deduct_held_negative_plan(self):
        """计划手数为负 → 取 0。"""
        from risk_gate_utils import deduct_held_lots

        assert deduct_held_lots(-1, 0, 10) == 0

    def test_check_limit_gate_no_limit_data(self):
        """无涨跌停数据 → 放行。"""
        from risk_gate_utils import check_limit_gate

        assert check_limit_gate(stop_pts=100, limit_pts=0) is True

    def test_check_limit_gate_close_to_limit(self):
        """止损距接近涨跌停 → 否决。"""
        from risk_gate_utils import check_limit_gate

        # 止损距 95 > 涨跌停 100 × 0.9 = 90 → 否决
        assert check_limit_gate(stop_pts=95, limit_pts=100, limit_proximity=0.9) is False

    def test_check_limit_gate_safe_distance(self):
        """止损距远小于涨跌停 → 通过。"""
        from risk_gate_utils import check_limit_gate

        assert check_limit_gate(stop_pts=50, limit_pts=100, limit_proximity=0.9) is True

    def test_calc_position_plan_full_pipeline(self):
        """完整仓位计划计算 → 所有字段都有值。"""
        from risk_gate_utils import calc_position_plan

        r = calc_position_plan(
            equity=100000,
            risk_pct=1.0,
            stop_pts=20,
            multiplier=100,
            margin_rate=0.12,
            price=2500,
            margin_cap_pct=30.0,
            max_lots=5,
            kelly_mult=1.0,
        )
        assert r["N_risk_raw"] >= 0
        assert r["N_risk"] >= 0
        assert r["N_margin"] >= 0
        assert r["N_plan"] >= 0
        assert isinstance(r["over_risk"], bool)
        assert r["t_scale"] is None  # 未启用 T 缩放
        assert r["gate3_ok"] is True
        assert isinstance(r["passed"], bool)

    def test_calc_position_plan_with_t_strength(self):
        """启用 T 强度缩放 → t_scale 有值。"""
        from risk_gate_utils import calc_position_plan

        r = calc_position_plan(
            equity=100000,
            risk_pct=1.0,
            stop_pts=20,
            multiplier=100,
            margin_rate=0.12,
            price=2500,
            max_lots=5,
            t_strength=60.0,
            t_thresh=40.0,
        )
        assert r["t_scale"] is not None
        assert 0.5 <= r["t_scale"] <= 1.0

    def test_calc_position_plan_gate3_fails(self):
        """涨跌停闸门不通过 → passed=False。"""
        from risk_gate_utils import calc_position_plan

        r = calc_position_plan(
            equity=100000,
            risk_pct=1.0,
            stop_pts=50,
            multiplier=100,
            margin_rate=0.12,
            price=2500,
            max_lots=5,
            limit_pts=50,
            limit_proximity=0.9,
        )
        assert r["gate3_ok"] is False
        assert r["passed"] is False


# ═══════════════════════════════════════════════════════════════════════════
#  8. anomaly_scan — 空数据 & 脏数据测试
# ═══════════════════════════════════════════════════════════════════════════


class TestAnomalyScanEdgeCases:
    """异动扫描的空数据和脏数据测试。"""

    def test_empty_snaps(self):
        """空输入 → 返回 ok=False。"""
        from anomaly_scan import compute

        r = compute({})
        assert r["ok"] is False
        assert r["total"] == 0
        assert r["top_up"] == []
        assert r["top_down"] == []

    def test_invalid_data_skipped(self):
        """包含无效数据 → 跳过坏数据，只处理有效数据。"""
        from anomaly_scan import compute

        snaps = {
            "FG": {"close": 910, "open": 900, "high": 915, "low": 898},
            "BAD": {"close": "N/A", "open": 900, "high": 915, "low": 898},  # 非法 close
            "MISSING": {"close": 900},  # 缺字段
        }
        r = compute(snaps)
        assert r["total"] == 1  # 只有 FG 有效
        assert "FG" in r["by_symbol"]

    def test_zero_open_skipped(self):
        """开盘价为 0 → 跳过（除零保护）。"""
        from anomaly_scan import compute

        snaps = {
            "ZERO": {"close": 910, "open": 0, "high": 915, "low": 898},
        }
        r = compute(snaps)
        assert r["ok"] is False  # 全部被跳过

    def test_with_pre_close(self):
        """传入昨收 → 使用昨收计算涨跌幅。"""
        from anomaly_scan import compute

        snaps = {"FG": {"close": 910, "open": 900, "high": 915, "low": 898}}
        pre_close = {"FG": 905}
        r = compute(snaps, pre_close_map=pre_close)
        assert r["ok"] is True
        # 用昨收 905 计算：(910-905)/905*100 ≈ 0.55%
        fg = r["by_symbol"]["FG"]
        assert fg["pct"] == pytest.approx((910 - 905) / 905 * 100, abs=0.1)

    def test_pre_close_invalid_fallback(self):
        """昨收无效 → 回退到用开盘价。"""
        from anomaly_scan import compute

        snaps = {"FG": {"close": 910, "open": 900, "high": 915, "low": 898}}
        pre_close = {"FG": "bad_price"}  # 非法昨收
        r = compute(snaps, pre_close_map=pre_close)
        assert r["ok"] is True
        fg = r["by_symbol"]["FG"]
        # 应回退到用开盘价计算
        assert fg["pct"] == pytest.approx((910 - 900) / 900 * 100, abs=0.1)

    def test_top_up_and_down(self):
        """涨跌榜排序正确。"""
        from anomaly_scan import compute

        snaps = {
            "UP": {"close": 110, "open": 100, "high": 112, "low": 99},  # +10%
            "DOWN": {"close": 90, "open": 100, "high": 101, "low": 88},  # -10%
            "FLAT": {"close": 100, "open": 100, "high": 101, "low": 99},  # ~0%
        }
        r = compute(snaps, top_n=2)
        assert r["top_up"][0]["symbol"] == "UP"
        assert r["top_down"][0]["symbol"] == "DOWN"

    def test_custom_top_n(self):
        """自定义 top_n 参数。"""
        from anomaly_scan import compute

        snaps = {f"SYM{i}": {"close": 100 + i, "open": 100, "high": 101 + i, "low": 99} for i in range(20)}
        r = compute(snaps, top_n=5)
        assert len(r["top_up"]) == 5
        assert len(r["top_down"]) == 5


# ═══════════════════════════════════════════════════════════════════════════
#  9. hidden_pivot — 边界结构测试
# ═══════════════════════════════════════════════════════════════════════════


class TestHiddenPivotEdgeCases:
    """隐秘枢轴的边界结构测试。"""

    def test_find_swings_too_few_bars(self):
        """K 线数量不足 → 返回空列表。"""
        from hidden_pivot import find_swings

        closes = [100, 101]
        swings = find_swings(closes, closes, closes, depth=3)
        assert swings == []

    def test_find_swings_flat_market(self):
        """横盘（价格不变）→ 无摆动点。"""
        from hidden_pivot import find_swings

        n = 20
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        swings = find_swings(highs, lows, closes, deviation=0.004, depth=2)
        assert len(swings) == 0

    def test_latest_abc_fewer_than_three(self):
        """摆动点少于 3 个 → 返回 None。"""
        from hidden_pivot import latest_abc

        assert latest_abc([]) is None
        assert latest_abc([(0, "high", 100)]) is None
        assert latest_abc([(0, "high", 100), (5, "low", 90)]) is None

    def test_latest_abc_no_valid_structure(self):
        """没有合法 a-b-c 结构 → 返回 None。"""
        from hidden_pivot import latest_abc

        # 三个高点（没有高低交替）→ 不构成结构
        swings = [(0, "high", 100), (5, "high", 102), (10, "high", 105)]
        assert latest_abc(swings) is None

    def test_latest_abc_bullish(self):
        """多头 a-b-c 结构（higher low）。"""
        from hidden_pivot import latest_abc

        swings = [
            (0, "low", 100),  # a
            (5, "high", 120),  # b
            (10, "low", 105),  # c (higher low)
            (15, "high", 130),  # 后面的点
        ]
        result = latest_abc(swings, direction=1)
        assert result is not None
        a, b, c, direction = result
        assert direction == 1
        assert c[2] > a[2]  # higher low

    def test_latest_abc_bearish(self):
        """空头 a-b-c 结构（lower high）。"""
        from hidden_pivot import latest_abc

        swings = [
            (0, "high", 100),  # a
            (5, "low", 80),  # b
            (10, "high", 95),  # c (lower high)
        ]
        result = latest_abc(swings, direction=-1)
        assert result is not None
        a, b, c, direction = result
        assert direction == -1
        assert c[2] < a[2]  # lower high

    def test_round_tick(self):
        """tick 取整函数。"""
        from hidden_pivot import round_tick

        assert round_tick(1234.56, 1.0) == pytest.approx(1235.0)
        assert round_tick(1234.4, 1.0) == pytest.approx(1234.0)
        assert round_tick(1234.56, 0.1) == pytest.approx(1234.6)
        assert round_tick(1234.567, 0.01) == pytest.approx(1234.57)

    def test_hidden_pivot_none_input(self):
        """abc 为 None → 返回 None。"""
        from hidden_pivot import hidden_pivot

        assert hidden_pivot(None, tick=1.0) is None

    def test_hidden_pivot_bullish_reachable(self):
        """多头结构，目标位可达到。"""
        from hidden_pivot import hidden_pivot, latest_abc

        swings = [(0, "low", 100), (5, "high", 120), (10, "low", 105)]
        abc = latest_abc(swings, direction=1)
        result = hidden_pivot(abc, tick=1.0)
        assert result is not None
        assert result["direction"] == 1
        assert result["direction_text"] == "偏多"
        assert result["p_reachable"] is True
        assert result["p"] > result["b"]  # 目标位在 b 上方

    def test_hidden_pivot_bullish_limit_up(self):
        """多头 + 涨停限制 → p 超过涨停板 → unreachable。"""
        from hidden_pivot import hidden_pivot, latest_abc

        swings = [(0, "low", 100), (5, "high", 120), (10, "low", 105)]
        abc = latest_abc(swings, direction=1)
        # 涨停板设得很低，目标位肯定超
        result = hidden_pivot(abc, tick=1.0, limit_up=110)
        assert result is not None
        assert result["p_reachable"] is False

    def test_hidden_pivot_bearish_limit_down(self):
        """空头 + 跌停限制 → p 低于跌停板 → unreachable。"""
        from hidden_pivot import hidden_pivot, latest_abc

        swings = [(0, "high", 100), (5, "low", 80), (10, "high", 95)]
        abc = latest_abc(swings, direction=-1)
        result = hidden_pivot(abc, tick=1.0, limit_down=100)  # 跌停很高
        assert result is not None
        assert result["p_reachable"] is False

    def test_find_swings_with_gap_skip(self):
        """有跳空缺口 → 摆动链重置。"""
        from hidden_pivot import find_swings

        # 构造带缺口的价格序列
        n = 20
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows = [99.0 + i * 0.5 for i in range(n)]
        closes = [99.5 + i * 0.5 for i in range(n)]
        opens = [99.5 + i * 0.5 for i in range(n)]
        # 在第 10 根制造大缺口（开盘跳涨 5%）
        opens[10] = closes[9] * 1.05

        swings_no_gap = find_swings(highs, lows, closes, opens, deviation=0.004, depth=2, gap_pct=0.0)
        swings_with_gap = find_swings(highs, lows, closes, opens, deviation=0.004, depth=2, gap_pct=0.01)

        # 启用缺口过滤后，摆动点应该更少（缺口处重置）
        assert len(swings_with_gap) <= len(swings_no_gap)


# ═══════════════════════════════════════════════════════════════════════════
#  10. t_score_utils — 边界统计测试
# ═══════════════════════════════════════════════════════════════════════════


class TestTScoreEdgeCases:
    """T 分数统计的边界条件测试。"""

    def test_single_value(self):
        """单值列表 → 标准差为 0，返回安全默认值。"""
        try:
            from t_score_utils import z_score_list
        except ImportError:
            self.skipTest("t_score_utils not available")

        scores, mean, std = z_score_list([50.0])
        # 单值时均值 = 原值，标准差 = 0
        assert mean == pytest.approx(50.0)
        assert std == pytest.approx(0.0)

    def test_empty_list(self):
        """空列表 → 返回空结果。"""
        try:
            from t_score_utils import z_score_list
        except ImportError:
            self.skipTest("t_score_utils not available")

        scores, mean, std = z_score_list([])
        assert scores == []
        assert mean == pytest.approx(0.0)
        assert std == pytest.approx(0.0)

    def test_all_same_values(self):
        """所有值相同 → 标准差为 0。"""
        try:
            from t_score_utils import z_score_list
        except ImportError:
            self.skipTest("t_score_utils not available")

        scores, mean, std = z_score_list([50.0, 50.0, 50.0])
        assert mean == pytest.approx(50.0)
        assert std == pytest.approx(0.0)
        # 标准差为 0 时 z-score 应都是 0（安全处理）
        assert all(s == 0.0 for s in scores)

    def test_z_score_standard_normal(self):
        """标准正态样本 → 均值≈0，标准差≈1。"""
        try:
            from t_score_utils import z_score_list
        except ImportError:
            self.skipTest("t_score_utils not available")

        data = [-2, -1, 0, 1, 2]
        scores, mean, std = z_score_list(data)
        assert abs(mean) < 1e-10  # 均值应为 0
        # 总体标准差
        assert std == pytest.approx(math.sqrt(2), abs=0.01)

    def test_t_score_normalization(self):
        """T 分数归一化到 50±10。"""
        from t_score_utils import normalize_to_t_score

        # z_score=0 → T=50
        assert normalize_to_t_score(0.0) == pytest.approx(50.0)
        # z_score=1 → T=60
        assert normalize_to_t_score(1.0) == pytest.approx(60.0)
        # z_score=-1 → T=40
        assert normalize_to_t_score(-1.0) == pytest.approx(40.0)

    def test_t_score_clipping(self):
        """极端 z-score → T 分数被限制在合理范围。"""
        from t_score_utils import normalize_to_t_score

        # 非常大的 z-score
        t_high = normalize_to_t_score(10.0)
        t_low = normalize_to_t_score(-10.0)
        # 应该在合理范围内（不会是 NaN 或无穷大）
        assert 0 <= t_high <= 100
        assert 0 <= t_low <= 100


# ═══════════════════════════════════════════════════════════════════════════
#  11. direction_source_monitor — 全局 tracker 函数测试
# ═══════════════════════════════════════════════════════════════════════════


class TestDirectionSourceMonitorGlobal:
    """方向源监控的全局 tracker 函数测试。"""

    def test_reset_tracker_clears_state(self):
        """reset_tracker 重置全局状态。"""
        import direction_source_monitor as dsm

        # 先添加一些数据
        dsm.alert_level("rb", 50, 30)
        dsm.alert_level("SA", 50, -30)
        # 重置
        result = dsm.reset_tracker()
        # 重置后应为空
        assert result["n"] == 0
        assert result["divergence_rate"] is None
        assert result["level"] == "OK"

    def test_alert_level_updates_global_tracker(self):
        """alert_level 更新全局 tracker 并返回当前级别。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        # 连续全分歧 → 级别升到 HIGH
        for _ in range(20):
            result = dsm.alert_level("rb", 50, -30)
        assert result["level"] == "HIGH"
        assert result["divergence_rate"] == pytest.approx(1.0)
        assert result["n"] == 20

    def test_alert_level_warn_level(self):
        """分歧率介于 WARN 和 HIGH 之间 → WARN 级别。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        # 60% 分歧 → WARN
        for _ in range(6):
            dsm.alert_level("rb", 50, -30)  # 分歧
        for _ in range(4):
            dsm.alert_level("rb", 50, 30)  # 一致
        result = dsm.alert_level("rb", 50, -30)
        # 11 个样本，7 分歧 → 7/11 ≈ 0.636 → WARN (>=0.55)
        assert result["level"] == "WARN"
        assert result["divergence_rate"] >= 0.55

    def test_alert_level_ok_level(self):
        """低分歧率 → OK 级别。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        # 只有 10% 分歧 → OK
        for _ in range(9):
            dsm.alert_level("rb", 50, 30)
        result = dsm.alert_level("rb", 50, -30)
        assert result["level"] == "OK"

    def test_get_tracker_returns_singleton(self):
        """get_tracker 返回同一个实例（单例模式）。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        t1 = dsm.get_tracker()
        t2 = dsm.get_tracker()
        assert t1 is t2  # 同一个对象

    def test_alert_level_handles_zero_direction(self):
        """alert_level 遇到无方向信号 → 不计数。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        # T_D 为 0 → 无方向
        result = dsm.alert_level("rb", 0, 30)
        assert result["n"] == 0
        assert result["divergence_rate"] is None

    def test_sa_sensitive_flag_present(self):
        """summary 中包含 sa_sensitive 标记。"""
        import direction_source_monitor as dsm

        dsm.reset_tracker()
        result = dsm.alert_level("SA", 50, 30)
        assert "sa_sensitive" in result
        assert isinstance(result["sa_sensitive"], bool)
        assert "sa_divergence_rate" in result


# ═══════════════════════════════════════════════════════════════════════════
#  12. price_protection — 止损方向校验 & 入口价格保护
# ═══════════════════════════════════════════════════════════════════════════


class TestPriceProtectionExtended:
    """价格保护的扩展测试（止损方向校验 & 价格保护）。"""

    def test_dir_sign_integer_values(self):
        """_dir_sign 整数方向值。"""
        from price_protection import _dir_sign

        assert _dir_sign(1) == 1
        assert _dir_sign(-1) == -1
        assert _dir_sign(0) == 0
        assert _dir_sign(100) == 1
        assert _dir_sign(-100) == -1

    def test_dir_sign_float_values(self):
        """_dir_sign 浮点方向值。"""
        from price_protection import _dir_sign

        assert _dir_sign(0.5) == 1
        assert _dir_sign(-0.5) == -1
        assert _dir_sign(0.0) == 0

    def test_dir_sign_chinese_variants(self):
        """_dir_sign 中文方向表示。"""
        from price_protection import _dir_sign

        assert _dir_sign("多") == 1
        assert _dir_sign("空") == -1
        assert _dir_sign("duo") == 1
        assert _dir_sign("kong") == -1

    def test_validate_entry_stop_valid_long(self):
        """validate_entry_stop 多单有效：止损在入场下方。"""
        from price_protection import validate_entry_stop

        result = validate_entry_stop("多", entry_price=1000.0, stop=980.0)
        assert result["direction_valid"] is True
        assert result["fixed"] is False
        assert result["stop"] == pytest.approx(980.0)

    def test_validate_entry_stop_valid_short(self):
        """validate_entry_stop 空单有效：止损在入场上方。"""
        from price_protection import validate_entry_stop

        result = validate_entry_stop("空", entry_price=1000.0, stop=1020.0)
        assert result["direction_valid"] is True
        assert result["fixed"] is False

    def test_validate_entry_stop_fixes_wrong_direction(self):
        """validate_entry_stop 止损方向错误 → 自动镜像修正。"""
        from price_protection import validate_entry_stop

        # 多单但止损在入场上方（错误）→ 应被镜像修正到下方
        result = validate_entry_stop("多", entry_price=1000.0, stop=1020.0)
        assert result["fixed"] is True
        assert result["stop"] < 1000.0  # 修正后在入场下方
        assert result["fix_note"] != ""

    def test_validate_entry_stop_invalid_direction(self):
        """validate_entry_stop 方向无效 → direction_valid=False。"""
        from price_protection import validate_entry_stop

        result = validate_entry_stop("unknown", entry_price=1000.0, stop=980.0)
        assert result["direction_valid"] is False

    def test_validate_entry_stop_none_stop(self):
        """validate_entry_stop 止损为 None → 原样返回。"""
        from price_protection import validate_entry_stop

        result = validate_entry_stop("多", entry_price=1000.0, stop=None)
        assert result["stop"] is None
        assert result["fixed"] is False

    def test_validate_entry_stop_invalid_price(self):
        """validate_entry_stop 价格无效 → 不修正，原样返回。"""
        from price_protection import validate_entry_stop

        result = validate_entry_stop("多", entry_price="bad", stop=980.0)
        assert result["fixed"] is False

    def test_protect_user_price_preserves_original(self):
        """protect_user_price 用户提供价格 → 强制使用用户价。"""
        from price_protection import protect_user_price

        original = 2580.5
        computed = 2580.8  # 计算价略有不同

        result = protect_user_price(original, computed, user_provided_price=True)
        # 用户提供了价格 → 最终价 = 用户原始价
        assert result["final_price"] == pytest.approx(original)
        assert result["was_protected"] is True
        assert result["price_changed"] is True

    def test_protect_user_price_not_provided(self):
        """protect_user_price 用户未提供价格 → 使用计算价。"""
        from price_protection import protect_user_price

        original = 2580.5
        computed = 2580.8

        result = protect_user_price(original, computed, user_provided_price=False)
        # 用户没提供 → 用计算价
        assert result["final_price"] == pytest.approx(computed)
        assert result["was_protected"] is False

    def test_protect_user_price_same_values(self):
        """protect_user_price 计算价与用户价相同 → 未触发保护。"""
        from price_protection import protect_user_price

        result = protect_user_price(100.0, 100.0, user_provided_price=True)
        assert result["final_price"] == pytest.approx(100.0)
        assert result["was_protected"] is False
        assert result["price_changed"] is False

    def test_protect_user_price_invalid_input(self):
        """protect_user_price 输入价格无效 → 安全处理（不抛异常）。"""
        from price_protection import protect_user_price

        result = protect_user_price("bad_price", 100.0, user_provided_price=True)
        # 原始价无效时转为 0.0，但不抛异常
        assert result["final_price"] == pytest.approx(0.0)
        assert isinstance(result["was_protected"], bool)
