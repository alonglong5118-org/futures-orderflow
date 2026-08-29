#!/usr/bin/env python3
"""
相关性闸门（corr_gate）— 单元测试
====================================

覆盖场景：
1. 不触发场景：低相关 / 历史数据不足 / 无历史 / 方差为 0
2. 触发场景：高正相关 + T弱 / 高正相关 + C弱 / 高负相关
3. 边界值：corr 恰好等于 gate
4. 历史 bug 回归：P1-2 空转修复（只改文本不改权重）
5. 特殊输入：None / 格式错误 / 数据不足

对应历史 bug（决策 26：corr_gate 空转修复）：
  - 问题：原"修复"只改了文本描述，权重并未实际降权（空转）
  - 修复：高相关时真正把弱维度降为 0
  - 验证：确保 corr > gate 时弱维度确实变为 0
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from corr_gate_utils import _pearson_corr, apply_corr_gate

# ═══════════════════════════════════════════════════════════════════════════
#  工具：生成指定相关系数的测试数据
# ═══════════════════════════════════════════════════════════════════════════


def make_corr_hist(target_corr, n=20, T_base=70.0, C_base=60.0, T_scale=10.0):
    """
    生成精确目标相关系数的 T/C 历史序列。

    方法：
    1. 构造 T 序列（线性递增）
    2. 构造与 T 完全正交的 Z 序列（Gram-Schmidt 正交化）
    3. C = sign * T + b * Z，调整 b 得到精确的目标相关系数
    """
    import math

    # T 序列：线性递增（保证有足够方差）
    T_vals = [T_base + T_scale * (i - n / 2) / (n / 2) for i in range(n)]

    # 初始 Z 候选：交替 +- 模式
    z_raw = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]

    # Gram-Schmidt 正交化：从 z_raw 中减去 T 方向的分量
    mean_T = sum(T_vals) / n
    mean_z_raw = sum(z_raw) / n

    cov_Tz = sum((T_vals[i] - mean_T) * (z_raw[i] - mean_z_raw) for i in range(n)) / n
    var_T = sum((t - mean_T) ** 2 for t in T_vals) / n

    if var_T > 0:
        z_orth = [z_raw[i] - (cov_Tz / var_T) * (T_vals[i] - mean_T) for i in range(n)]
    else:
        z_orth = z_raw[:]

    mean_z = sum(z_orth) / n
    var_z = sum((z - mean_z) ** 2 for z in z_orth) / n

    # 处理极端情况
    if abs(target_corr) >= 0.999:
        sign = 1 if target_corr >= 0 else -1
        C_vals = [C_base + sign * (t - T_base) for t in T_vals]
        return [[T_vals[i], C_vals[i]] for i in range(n)]

    if abs(target_corr) < 1e-6:
        # 零相关：只用 Z
        z_scale = T_scale / math.sqrt(var_z) if var_z > 0 else 1.0
        C_vals = [C_base + z_scale * (z - mean_z) for z in z_orth]
        return [[T_vals[i], C_vals[i]] for i in range(n)]

    # 一般情况：C = sign*T + b*Z
    # corr(T, C) = sign*sd_T / sqrt(sd_T^2 + b^2*sd_Z^2)
    # r^2 = var_T / (var_T + b^2 * var_Z)
    # b^2 = var_T * (1/r^2 - 1) / var_Z

    r_sq = target_corr**2
    sign = 1 if target_corr >= 0 else -1

    if var_z <= 0:
        b_sq = 0
    else:
        b_sq = var_T * (1 / r_sq - 1) / var_z

    b = math.sqrt(max(b_sq, 0))

    # 缩放 Z 使量级合理（C 的标准差 ≈ T 的标准差）
    # 调整 z_scale 使得 b*z_scale*sd_z 和 sd_T 匹配
    if var_z > 0 and b > 0:
        z_scale = 1.0  # b 已经包含了缩放比例
    else:
        z_scale = 1.0

    C_vals = [C_base + sign * (T_vals[i] - T_base) + b * (z_orth[i] - mean_z) for i in range(n)]

    return [[T_vals[i], C_vals[i]] for i in range(n)]


# ═══════════════════════════════════════════════════════════════════════════
#  皮尔逊相关系数内部函数测试
# ═══════════════════════════════════════════════════════════════════════════


class TestPearsonCorr(unittest.TestCase):
    """_pearson_corr 函数的基础测试。"""

    def test_perfect_positive_correlation(self):
        """完全正相关 → corr = 1.0"""
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        r = _pearson_corr(x, y)
        self.assertAlmostEqual(r, 1.0, places=5)

    def test_perfect_negative_correlation(self):
        """完全负相关 → corr = -1.0"""
        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        r = _pearson_corr(x, y)
        self.assertAlmostEqual(r, -1.0, places=5)

    def test_zero_correlation(self):
        """不相关 → corr ≈ 0"""
        # 用精确构造的零相关数据（Gram-Schmidt 正交化）
        n = 20
        x = [float(i) for i in range(n)]
        z_raw = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]
        mean_x = sum(x) / n
        mean_z = sum(z_raw) / n
        cov = sum((x[i] - mean_x) * (z_raw[i] - mean_z) for i in range(n)) / n
        var_x = sum((xi - mean_x) ** 2 for xi in x) / n
        y = [z_raw[i] - (cov / var_x) * (x[i] - mean_x) for i in range(n)]
        r = _pearson_corr(x, y)
        self.assertAlmostEqual(r, 0.0, places=5)

    def test_insufficient_data(self):
        """数据不足 → 返回 None"""
        self.assertIsNone(_pearson_corr([], []))
        self.assertIsNone(_pearson_corr([1], [2]))

    def test_length_mismatch(self):
        """长度不一致 → 返回 None"""
        self.assertIsNone(_pearson_corr([1, 2, 3], [4, 5]))

    def test_zero_variance_x(self):
        """x 方差为 0 → 返回 None"""
        x = [5, 5, 5, 5]
        y = [1, 2, 3, 4]
        self.assertIsNone(_pearson_corr(x, y))

    def test_zero_variance_y(self):
        """y 方差为 0 → 返回 None"""
        x = [1, 2, 3, 4]
        y = [5, 5, 5, 5]
        self.assertIsNone(_pearson_corr(x, y))


# ═══════════════════════════════════════════════════════════════════════════
#  不触发降权的场景
# ═══════════════════════════════════════════════════════════════════════════


class TestNoAction(unittest.TestCase):
    """不触发降权的各种场景。"""

    def test_low_correlation_no_action(self):
        """低相关（|corr| < gate）→ 正常计权，两维都保留"""
        # 生成低相关数据
        hist = make_corr_hist(0.3, n=20)
        result = apply_corr_gate(T_score=80.0, C_score=60.0, corr_hist=hist, gate=0.7)

        self.assertFalse(result["applied"], "低相关不应触发降权")
        self.assertEqual(result["dropped"], "none")
        self.assertAlmostEqual(result["T"], 80.0, places=2)
        self.assertAlmostEqual(result["C"], 60.0, places=2)
        self.assertIsNotNone(result["corr"])
        self.assertTrue(abs(result["corr"]) < 0.7)

    def test_no_history_no_action(self):
        """无历史数据 → 跳过，正常计权"""
        result = apply_corr_gate(T_score=80.0, C_score=60.0, corr_hist=None, gate=0.7)

        self.assertFalse(result["applied"])
        self.assertEqual(result["dropped"], "none")
        self.assertAlmostEqual(result["T"], 80.0)
        self.assertAlmostEqual(result["C"], 60.0)
        self.assertIsNone(result["corr"])
        self.assertIn("无历史数据", result["action"])

    def test_insufficient_history_no_action(self):
        """历史数据不足（< 10 条）→ 跳过"""
        hist = make_corr_hist(0.9, n=5)  # 只有 5 条
        result = apply_corr_gate(T_score=80.0, C_score=60.0, corr_hist=hist, gate=0.7)

        self.assertFalse(result["applied"], "历史不足不应触发降权")
        self.assertAlmostEqual(result["T"], 80.0)
        self.assertAlmostEqual(result["C"], 60.0)
        self.assertIn("历史数据不足", result["action"])

    def test_zero_variance_no_action(self):
        """某维度无波动（方差为0）→ 跳过"""
        # T 全是同一个值
        hist = [[70.0, 60.0 + i] for i in range(20)]
        result = apply_corr_gate(T_score=80.0, C_score=60.0, corr_hist=hist, gate=0.7)

        self.assertFalse(result["applied"])
        self.assertAlmostEqual(result["T"], 80.0)
        self.assertAlmostEqual(result["C"], 60.0)
        self.assertIn("无波动", result["action"])

    def test_at_threshold_boundary_no_action(self):
        """|corr| 恰好等于 gate → 不触发（严格大于才触发）"""
        # 生成 corr ≈ 0.7 的数据，然后手动调整
        hist = make_corr_hist(0.5, n=20)
        # 先计算实际 corr
        T_vals = [row[0] for row in hist]
        C_vals = [row[1] for row in hist]
        actual_corr = _pearson_corr(T_vals, C_vals)

        # 用实际 corr 作为 gate，这样 |corr| == gate → 不触发
        result = apply_corr_gate(T_score=80.0, C_score=60.0, corr_hist=hist, gate=abs(actual_corr))

        self.assertFalse(result["applied"], "|corr| == gate 不应触发（严格大于）")
        self.assertEqual(result["dropped"], "none")


# ═══════════════════════════════════════════════════════════════════════════
#  触发降权的场景
# ═══════════════════════════════════════════════════════════════════════════


class TestDropWeakerDimension(unittest.TestCase):
    """触发降权：高相关时降权较弱维度。"""

    def test_high_positive_corr_T_weaker_drop_T(self):
        """高正相关 + T 更弱 → 降权 T，保留 C"""
        hist = make_corr_hist(0.95, n=30)  # 0.95 > 0.7
        result = apply_corr_gate(T_score=60.0, C_score=80.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"], "高相关应触发降权")
        self.assertEqual(result["dropped"], "T")
        self.assertAlmostEqual(result["T"], 0.0, places=2, msg="T 应该被降为 0")
        self.assertAlmostEqual(result["C"], 80.0, places=2, msg="C 应该保留")

    def test_high_positive_corr_C_weaker_drop_C(self):
        """高正相关 + C 更弱 → 降权 C，保留 T"""
        hist = make_corr_hist(0.95, n=30)
        result = apply_corr_gate(T_score=90.0, C_score=50.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "C")
        self.assertAlmostEqual(result["T"], 90.0, places=2)
        self.assertAlmostEqual(result["C"], 0.0, places=2, msg="C 应该被降为 0")

    def test_high_negative_corr_T_weaker_drop_T(self):
        """高负相关 + T 更弱 → 降权 T（负相关也视为冗余）"""
        hist = make_corr_hist(-0.9, n=30)  # -0.9，绝对值 > 0.7
        result = apply_corr_gate(T_score=50.0, C_score=85.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"], "高负相关也应触发降权")
        self.assertEqual(result["dropped"], "T")
        self.assertAlmostEqual(result["T"], 0.0, places=2)
        self.assertAlmostEqual(result["C"], 85.0, places=2)
        self.assertLess(result["corr"], 0, "应该是负相关")

    def test_high_negative_corr_C_weaker_drop_C(self):
        """高负相关 + C 更弱 → 降权 C"""
        hist = make_corr_hist(-0.9, n=30)
        result = apply_corr_gate(T_score=85.0, C_score=50.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "C")
        self.assertAlmostEqual(result["C"], 0.0, places=2)

    def test_equal_strength_drop_T(self):
        """两维强度相等 → 降权 T（因为 <= 判断：abs_T <= abs_C 时降 T）"""
        hist = make_corr_hist(0.9, n=20)
        result = apply_corr_gate(T_score=70.0, C_score=70.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "T")
        self.assertAlmostEqual(result["T"], 0.0)
        self.assertAlmostEqual(result["C"], 70.0)

    def test_negative_scores_T_weaker(self):
        """T 和 C 都是负值 + T 更弱（绝对值更小）→ 降权 T"""
        hist = make_corr_hist(0.9, n=20)
        # T=-40, C=-80 → |T|=40 < |C|=80 → T 更弱
        result = apply_corr_gate(T_score=-40.0, C_score=-80.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "T")
        self.assertAlmostEqual(result["T"], 0.0)
        self.assertAlmostEqual(result["C"], -80.0)

    def test_negative_scores_C_weaker(self):
        """T 和 C 都是负值 + C 更弱 → 降权 C"""
        hist = make_corr_hist(0.9, n=20)
        # T=-80, C=-40 → |C|=40 < |T|=80 → C 更弱
        result = apply_corr_gate(T_score=-80.0, C_score=-40.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "C")
        self.assertAlmostEqual(result["T"], -80.0)
        self.assertAlmostEqual(result["C"], 0.0)

    def test_mixed_signs_abs_T_smaller_drop_T(self):
        """T 正 C 负 + |T| < |C| → 降权 T（比较的是绝对值）"""
        hist = make_corr_hist(-0.9, n=20)  # 负相关
        result = apply_corr_gate(T_score=30.0, C_score=-70.0, corr_hist=hist, gate=0.7)

        self.assertTrue(result["applied"])
        self.assertEqual(result["dropped"], "T")
        self.assertAlmostEqual(result["T"], 0.0)
        self.assertAlmostEqual(result["C"], -70.0)


# ═══════════════════════════════════════════════════════════════════════════
#  历史 bug 回归测试（决策 26：corr_gate 空转修复）
# ═══════════════════════════════════════════════════════════════════════════


class TestHistoricalBugRegression(unittest.TestCase):
    """
    历史 bug 回归测试 —— 确保 corr_gate 不再"空转"。

    对应决策 26（P1-2 深度重审加固）：
      - 原问题：corr_gate 只改了文本描述，权重并未实际降权（空转）
      - 修复后：高相关时弱维度必须真正变为 0
    """

    def test_high_corr_T_weak_T_must_be_zero(self):
        """回归：高相关 + T 弱 → T 必须真正变为 0（不是只改文本）"""
        hist = make_corr_hist(0.95, n=30)
        result = apply_corr_gate(T_score=50.0, C_score=90.0, corr_hist=hist, gate=0.7)

        # 核心断言：T 必须真正是 0
        self.assertAlmostEqual(result["T"], 0.0, places=5, msg="corr_gate 空转 bug 复发：T 应该被降为 0，但实际没变！")
        self.assertTrue(result["applied"], msg="corr_gate 空转 bug 复发：applied 应该为 True")

    def test_high_corr_C_weak_C_must_be_zero(self):
        """回归：高相关 + C 弱 → C 必须真正变为 0"""
        hist = make_corr_hist(0.95, n=30)
        result = apply_corr_gate(T_score=90.0, C_score=50.0, corr_hist=hist, gate=0.7)

        self.assertAlmostEqual(result["C"], 0.0, places=5, msg="corr_gate 空转 bug 复发：C 应该被降为 0，但实际没变！")
        self.assertTrue(result["applied"])

    def test_low_corr_both_preserved(self):
        """回归：低相关 → 两维都保留（不能乱降权）"""
        hist = make_corr_hist(0.3, n=30)
        T_orig = 70.0
        C_orig = 60.0
        result = apply_corr_gate(T_score=T_orig, C_score=C_orig, corr_hist=hist, gate=0.7)

        self.assertFalse(result["applied"])
        self.assertAlmostEqual(result["T"], T_orig, places=2, msg="低相关时 T 不应被修改")
        self.assertAlmostEqual(result["C"], C_orig, places=2, msg="低相关时 C 不应被修改")

    def test_action_text_matches_actual_state(self):
        """回归：action 文本描述必须与实际状态一致（不能"说降了但没降"）"""
        hist = make_corr_hist(0.9, n=20)
        result = apply_corr_gate(T_score=60.0, C_score=80.0, corr_hist=hist, gate=0.7)

        # 如果 action 说降了 T，那 T 必须真的是 0
        if "降权T" in result["action"]:
            self.assertAlmostEqual(result["T"], 0.0, places=5, msg="action 说降了 T，但 T 没变 → 空转 bug！")
            self.assertEqual(result["dropped"], "T")

        if "降权C" in result["action"]:
            self.assertAlmostEqual(result["C"], 0.0, places=5, msg="action 说降了 C，但 C 没变 → 空转 bug！")
            self.assertEqual(result["dropped"], "C")


# ═══════════════════════════════════════════════════════════════════════════
#  参数 & 边界
# ═══════════════════════════════════════════════════════════════════════════


class TestParamsAndEdges(unittest.TestCase):
    """参数和边界情况测试。"""

    def test_custom_gate_lower(self):
        """自定义更低的 gate → 更容易触发"""
        hist = make_corr_hist(0.6, n=20)  # corr ≈ 0.6
        # gate = 0.5 → 0.6 > 0.5 → 应该触发
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=0.5)
        self.assertTrue(result["applied"], "gate=0.5 时 corr=0.6 应触发")

    def test_custom_gate_higher(self):
        """自定义更高的 gate → 更难触发"""
        hist = make_corr_hist(0.8, n=20)  # corr ≈ 0.8
        # gate = 0.9 → 0.8 < 0.9 → 不触发
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=0.9)
        self.assertFalse(result["applied"], "gate=0.9 时 corr=0.8 不应触发")

    def test_gate_zero_always_triggers(self):
        """gate = 0 → 任何非零相关都触发（极端配置）"""
        hist = make_corr_hist(0.1, n=20)
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=0.0)
        # corr 不为 0 就应该触发
        if result["corr"] is not None and abs(result["corr"]) > 0:
            self.assertTrue(result["applied"])

    def test_gate_one_never_triggers(self):
        """gate = 1.0 → 永远不触发（corr 不可能严格大于 1）"""
        hist = make_corr_hist(0.99, n=20)
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=1.0)
        self.assertFalse(result["applied"])

    def test_custom_min_history(self):
        """自定义 min_history = 5 → 5 条数据就够"""
        hist = make_corr_hist(0.9, n=5)
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=0.7, min_history=5)
        self.assertTrue(result["applied"], "min_history=5 时 5 条数据应触发")

    def test_bad_data_format_no_crash(self):
        """历史数据格式错误 → 不崩溃，跳过"""
        # 不是二维数组
        hist = [1, 2, 3, 4, 5]
        try:
            result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=hist, gate=0.7)
        except Exception as e:
            self.fail(f"格式错误的数据导致崩溃: {e}")

        self.assertFalse(result["applied"])
        self.assertAlmostEqual(result["T"], 70.0)
        self.assertAlmostEqual(result["C"], 90.0)

    def test_empty_history_no_crash(self):
        """空历史 → 不崩溃，跳过"""
        result = apply_corr_gate(T_score=70.0, C_score=90.0, corr_hist=[], gate=0.7)
        self.assertFalse(result["applied"])
        self.assertAlmostEqual(result["T"], 70.0)
        self.assertAlmostEqual(result["C"], 90.0)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
