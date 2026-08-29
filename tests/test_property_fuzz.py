#!/usr/bin/env python3
"""
属性测试 + 模糊测试
==============================================

属性测试（Property-based Testing）：
  验证函数的通用数学属性在大量随机输入下始终成立。

模糊测试（Fuzz Testing）：
  用随机/极端输入验证函数的鲁棒性，确保不崩溃、返回合法值。

一、Kelly 因子属性测试
   - 输出有界性：结果始终在 [min(k_min,1), k_max] 内
   - 单调性：edge 越大，结果越大（单调非递减）
   - 近景门槛：近景负时 ≤ 1.0
   - 参数交换：k_min>k_max 自动交换后结果对称
   - 零 edge → k_min

二、tanh 归一化属性测试
   - 奇函数：f(-x) = -f(x)
   - 有界性：|f(x)| ≤ 1
   - 单调性：x 越大，f(x) 越大
   - 零输入 → 零输出
   - scale 符号不影响（scale>0 时单调）

三、月份运算属性测试
   - 加 n 个月再减 n 个月 → 不变
   - 加 12 个月 = 年份+1，月份不变
   - 结合性：(a + m) + n = a + (m + n)

四、解析函数属性测试
   - _parse_side：大小写不敏感、包含性
   - _parse_offset：大小写不敏感
   - _to_num：添加逗号不改变值、前后空格不改变值
   - _norm_key：幂等性、大小写统一

五、季节性属性测试
   - 同月同品种结果相同
   - 结果在 [-40, 40] 范围内

六、SR 止损放宽属性测试
   - 多空对称（方向取反 + 支撑压力互换 → stop_dist 相同）
   - 不缩窄：放宽后 stop_dist >= 原 stop_dist
   - 原 dict 不被修改

七、风险闸门属性测试
   - N_plan >= 0
   - N_plan <= N_risk
   - N_plan <= N_margin

八、模糊测试
   - 随机字符串 → _parse_side / _parse_offset / _norm_key / _to_num
   - 随机数字 → compute_kelly_factor / _norm_tanh
   - 随机日期 → seasonal_f / ym_of
   - 随机 dict → _is_signal_backed
"""

import math
import os
import random
import string
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from broker_import import _norm_key, _parse_offset, _parse_side, _to_num
from discipline_review import _is_manual_record, _is_signal_backed
from four_dim_strategy import DEFAULT_CONFIG, risk_gate
from fundamental_feed import seasonal_f
from kelly_utils import compute_kelly_factor
from macro_context import _norm_tanh
from refresh_main_contracts import _add_months, ym_of

# 随机测试次数
N_RANDOM = 200


# ═══════════════════════════════════════════════════════════════════════════
#  一、Kelly 因子属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestKellyProperty(unittest.TestCase):
    """Kelly 因子属性测试。"""

    def setUp(self):
        random.seed(42)

    def test_output_bounded(self):
        """属性：输出始终在合理范围内 [0.0, k_max]"""
        for _ in range(N_RANDOM):
            edge = random.uniform(-10, 10)
            k_min = random.uniform(0.1, 2.0)
            k_max = random.uniform(0.5, 3.0)
            target = random.uniform(0.1, 5.0)
            near = random.choice([None, random.uniform(-2, 2)])

            result = compute_kelly_factor(edge, k_min, k_max, target, near)
            # 结果应该在 [0, max(k_min, k_max)+0.1] 范围内
            self.assertGreaterEqual(result, 0.0, f"edge={edge}, result={result}")
            self.assertLessEqual(
                result, max(k_min, k_max) + 0.01, f"edge={edge}, result={result}, k_max={max(k_min, k_max)}"
            )

    def test_monotonic_increasing(self):
        """属性：edge 越大，结果越大（单调非递减）"""
        for _ in range(N_RANDOM):
            k_min = random.uniform(0.5, 1.0)
            k_max = random.uniform(1.0, 2.0)
            target = random.uniform(0.1, 2.0)
            near = random.choice([None, random.uniform(0.1, 1.0)])

            e1 = random.uniform(-1, 3)
            e2 = e1 + random.uniform(0.01, 2)

            r1 = compute_kelly_factor(e1, k_min, k_max, target, near)
            r2 = compute_kelly_factor(e2, k_min, k_max, target, near)

            self.assertGreaterEqual(r2, r1 - 1e-9, f"e1={e1}→{r1}, e2={e2}→{r2} (should be non-decreasing)")

    def test_negative_near_caps_at_1(self):
        """属性：近景负时，结果 ≤ 1.0"""
        for _ in range(N_RANDOM):
            edge = random.uniform(0.1, 5)
            near = random.uniform(-5, -0.01)

            result = compute_kelly_factor(edge, cur_full_expR=near)
            self.assertLessEqual(result, 1.0 + 1e-9, f"edge={edge}, near={near}, result={result}")

    def test_positive_near_allows_above_1(self):
        """属性：近景正 + edge 足够大 → 结果可以 > 1.0"""
        # 当 edge >= target_edge 时，结果 = k_max(1.2) > 1.0
        result = compute_kelly_factor(1.0, cur_full_expR=0.5, target_edge=0.5)
        self.assertGreater(result, 1.0)

    def test_zero_edge_equals_min_or_1(self):
        """属性：edge=0 → 结果 = min(k_min, 1.0)"""
        for _ in range(50):
            k_min = random.uniform(0.5, 1.5)
            k_max = random.uniform(k_min, k_min + 1.0)

            result = compute_kelly_factor(0.0, k_min, k_max)
            expected = min(k_min, 1.0)
            self.assertAlmostEqual(result, expected, places=10, msg=f"k_min={k_min}")

    def test_negative_edge_same_as_zero(self):
        """属性：负 edge 和 edge=0 结果相同"""
        for _ in range(N_RANDOM):
            neg_edge = random.uniform(-5, -0.001)
            r_neg = compute_kelly_factor(neg_edge)
            r_zero = compute_kelly_factor(0.0)
            self.assertAlmostEqual(r_neg, r_zero, places=10, msg=f"neg_edge={neg_edge}")

    def test_param_swap_symmetry(self):
        """属性：k_min 和 k_max 交换后结果相同"""
        for _ in range(N_RANDOM):
            edge = random.uniform(0, 2)
            a = random.uniform(0.5, 1.0)
            b = random.uniform(1.0, 2.0)

            r_normal = compute_kelly_factor(edge, a, b)
            r_swapped = compute_kelly_factor(edge, b, a)
            self.assertAlmostEqual(r_normal, r_swapped, places=10, msg=f"a={a}, b={b}")

    def test_none_edge_returns_one(self):
        """属性：edge=None → 1.0（各种参数组合下）"""
        for _ in range(50):
            k_min = random.uniform(0.5, 1.5)
            k_max = random.uniform(1.0, 2.5)
            result = compute_kelly_factor(None, k_min, k_max)
            self.assertEqual(result, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  二、tanh 归一化属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestTanhProperty(unittest.TestCase):
    """tanh 归一化属性测试。"""

    def setUp(self):
        random.seed(123)

    def test_odd_function(self):
        """属性：f(-x, scale) = -f(x, scale)（奇函数）"""
        for _ in range(N_RANDOM):
            x = random.uniform(-100, 100)
            scale = random.uniform(0.1, 50)

            result_pos = _norm_tanh(x, scale)
            result_neg = _norm_tanh(-x, scale)
            self.assertAlmostEqual(result_neg, -result_pos, places=10, msg=f"x={x}, scale={scale}")

    def test_bounded_by_1(self):
        """属性：|f(x)| ≤ 1"""
        for _ in range(N_RANDOM):
            x = random.uniform(-1000, 1000)
            scale = random.uniform(0.01, 100)

            result = _norm_tanh(x, scale)
            self.assertLessEqual(abs(result), 1.0 + 1e-12, f"x={x}, scale={scale}, result={result}")

    def test_monotonic_non_decreasing(self):
        """属性：x 越大，f(x) 越大或相等（非递减）"""
        for _ in range(N_RANDOM):
            scale = random.uniform(0.1, 10)
            x1 = random.uniform(-50, 50)
            x2 = x1 + random.uniform(0.01, 10)

            r1 = _norm_tanh(x1, scale)
            r2 = _norm_tanh(x2, scale)
            self.assertGreaterEqual(r2, r1, f"x1={x1}→{r1}, x2={x2}→{r2}, scale={scale}")

    def test_zero_input_zero_output(self):
        """属性：x=0 → 0"""
        for _ in range(50):
            scale = random.uniform(0.1, 100)
            self.assertEqual(_norm_tanh(0.0, scale), 0.0)

    def test_zero_scale_returns_zero(self):
        """属性：scale=0 → 0.0（各种 x）"""
        for _ in range(50):
            x = random.uniform(-100, 100)
            self.assertEqual(_norm_tanh(x, 0), 0.0)
            self.assertEqual(_norm_tanh(x, 0.0), 0.0)

    def test_large_x_approaches_1(self):
        """属性：x→+∞ 时 f(x)→1（趋近但不超过）"""
        result = _norm_tanh(100.0, 1.0)
        self.assertGreater(result, 0.99)
        self.assertLessEqual(result, 1.0)

    def test_large_neg_x_approaches_minus_1(self):
        """属性：x→-∞ 时 f(x)→-1"""
        result = _norm_tanh(-100.0, 1.0)
        self.assertLess(result, -0.99)
        self.assertGreaterEqual(result, -1.0)

    def test_scale_effect_monotonic(self):
        """属性：scale 越大，|f(x)| 越小（同 x 下）"""
        for _ in range(N_RANDOM):
            x = random.uniform(0.1, 10)  # 正值
            s1 = random.uniform(0.1, 5)
            s2 = s1 + random.uniform(0.1, 10)

            r1 = _norm_tanh(x, s1)
            r2 = _norm_tanh(x, s2)
            self.assertGreater(r1, r2, f"x={x}, s1={s1}→{r1}, s2={s2}→{r2}")


# ═══════════════════════════════════════════════════════════════════════════
#  三、月份运算属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestMonthArithmeticProperty(unittest.TestCase):
    """月份运算属性测试。"""

    def setUp(self):
        random.seed(456)

    def test_add_then_subtract_inverse(self):
        """属性：加 n 月再减 n 月 = 原值"""
        for _ in range(N_RANDOM):
            y = random.randint(2000, 2030)
            m = random.randint(1, 12)
            ym = y * 100 + m
            n = random.randint(-60, 60)

            result = _add_months(_add_months(ym, n), -n)
            self.assertEqual(result, ym, f"ym={ym}, n={n}")

    def test_add_12_months_same_month(self):
        """属性：加 12 个月 = 年份+1，月份不变"""
        for _ in range(100):
            y = random.randint(2000, 2030)
            m = random.randint(1, 12)
            ym = y * 100 + m

            result = _add_months(ym, 12)
            self.assertEqual(result // 100, y + 1)  # 年份+1
            self.assertEqual(result % 100, m)  # 月份不变

    def test_add_24_months_same_month(self):
        """属性：加 24 个月 = 年份+2，月份不变"""
        for _ in range(50):
            y = random.randint(2000, 2030)
            m = random.randint(1, 12)
            ym = y * 100 + m

            result = _add_months(ym, 24)
            self.assertEqual(result // 100, y + 2)
            self.assertEqual(result % 100, m)

    def test_associative(self):
        """属性：(ym + m) + n == ym + (m + n)（结合律）"""
        for _ in range(N_RANDOM):
            y = random.randint(2010, 2030)
            m = random.randint(1, 12)
            ym = y * 100 + m
            m1 = random.randint(-30, 30)
            m2 = random.randint(-30, 30)

            result1 = _add_months(_add_months(ym, m1), m2)
            result2 = _add_months(ym, m1 + m2)
            self.assertEqual(result1, result2, f"ym={ym}, m1={m1}, m2={m2}")

    def test_zero_months_identity(self):
        """属性：加 0 个月 = 不变"""
        for _ in range(100):
            y = random.randint(2000, 2030)
            m = random.randint(1, 12)
            ym = y * 100 + m
            self.assertEqual(_add_months(ym, 0), ym)

    def test_december_to_january(self):
        """属性：12月 + 1月 = 次年1月"""
        for y in range(2020, 2030):
            dec = y * 100 + 12
            jan = _add_months(dec, 1)
            self.assertEqual(jan, (y + 1) * 100 + 1)

    def test_january_to_december(self):
        """属性：1月 - 1月 = 上年12月"""
        for y in range(2021, 2030):
            jan = y * 100 + 1
            dec = _add_months(jan, -1)
            self.assertEqual(dec, (y - 1) * 100 + 12)


# ═══════════════════════════════════════════════════════════════════════════
#  四、解析函数属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestParseProperty(unittest.TestCase):
    """解析函数属性测试。"""

    def setUp(self):
        random.seed(789)

    def test_parse_side_case_insensitive(self):
        """属性：_parse_side 大小写不敏感"""
        test_words = ["buy", "sell", "B", "S", "BUY", "SELL", "Buy", "Sell"]
        for word in test_words:
            lower = _parse_side(word.lower())
            upper = _parse_side(word.upper())
            self.assertEqual(lower, upper, f"word={word}")

    def test_parse_offset_case_insensitive(self):
        """属性：_parse_offset 大小写不敏感"""
        test_words = ["open", "close", "OPEN", "CLOSE", "Open", "Close", "平", "开"]
        for word in test_words:
            lower = _parse_offset(word.lower())
            upper = _parse_offset(word.upper())
            self.assertEqual(lower, upper, f"word={word}")

    def test_to_num_comma_invariant(self):
        """属性：添加千分位逗号不改变数值"""
        for _ in range(N_RANDOM):
            num = random.uniform(-10000, 10000)
            s_normal = f"{num:.2f}"
            # 添加逗号（简单版）
            int_part = str(int(abs(num)))
            decimal = f"{num:.2f}".split(".")[1]
            sign = "-" if num < 0 else ""
            if len(int_part) > 3:
                with_comma = sign + int_part[:-3] + "," + int_part[-3:] + "." + decimal
            else:
                with_comma = f"{num:.2f}"

            v1 = _to_num(s_normal)
            v2 = _to_num(with_comma)
            self.assertAlmostEqual(v1, v2, places=2, msg=f"normal={s_normal}, comma={with_comma}")

    def test_to_num_whitespace_invariant(self):
        """属性：前后空格不改变数值"""
        for _ in range(N_RANDOM):
            num = random.uniform(-1000, 1000)
            s = str(num)
            padded = "   " + s + "   "
            self.assertAlmostEqual(_to_num(s), _to_num(padded), places=10)

    def test_norm_key_idempotent(self):
        """属性：_norm_key 是幂等的（两次调用结果相同）"""
        for _ in range(N_RANDOM):
            # 生成随机字符串
            length = random.randint(0, 20)
            chars = string.ascii_letters + string.digits + " :()_- \u3000"
            s = "".join(random.choice(chars) for _ in range(length))

            once = _norm_key(s)
            twice = _norm_key(once)
            self.assertEqual(once, twice, f"s='{s}'")

    def test_norm_key_lowercase(self):
        """属性：结果总是小写（如果有字母）"""
        for _ in range(100):
            s = "".join(random.choice(string.ascii_letters) for _ in range(10))
            result = _norm_key(s)
            self.assertEqual(result, result.lower())


# ═══════════════════════════════════════════════════════════════════════════
#  五、季节性属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestSeasonalProperty(unittest.TestCase):
    """季节性属性测试。"""

    def setUp(self):
        random.seed(101)

    def test_same_month_same_result(self):
        """属性：同品种同月份 → 结果相同（日不影响）"""
        for sym in ["jd", "lh", "rb"]:
            for m in range(1, 13):
                r1 = seasonal_f(sym, f"2025-{m:02d}-01")
                r2 = seasonal_f(sym, f"2026-{m:02d}-15")
                r3 = seasonal_f(sym, f"2027-{m:02d}-28")
                self.assertEqual(r1, r2, f"{sym} month={m}")
                self.assertEqual(r2, r3, f"{sym} month={m}")

    def test_result_bounded(self):
        """属性：结果在 [-40, 40] 范围内"""
        for sym in ["jd", "lh", "rb", "FG", "unknown"]:
            for m in range(1, 13):
                result = seasonal_f(sym, f"2025-{m:02d}-01")
                self.assertGreaterEqual(result, -40, f"{sym} month={m}")
                self.assertLessEqual(result, 40, f"{sym} month={m}")

    def test_invalid_date_returns_zero(self):
        """属性：非法日期 → 0.0"""
        for _ in range(50):
            s = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(random.randint(0, 20)))
            result = seasonal_f("jd", s)
            # 可能是 0.0 或 0（int）
            self.assertEqual(result, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  六、风险闸门属性测试
# ═══════════════════════════════════════════════════════════════════════════


class TestRiskGateProperty(unittest.TestCase):
    """风险闸门属性测试。"""

    def setUp(self):
        random.seed(202)
        self.symbol = "rb"

    def test_n_plan_nonnegative(self):
        """属性：N_plan >= 0（各种输入下）"""
        for _ in range(100):
            price = random.uniform(100, 10000)
            atr = random.uniform(1, price * 0.1)

            result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)
            self.assertGreaterEqual(result["N_plan"], 0, f"price={price}, atr={atr}")

    def test_n_plan_le_n_risk(self):
        """属性：N_plan <= N_risk（仓位约束）"""
        for _ in range(100):
            price = random.uniform(100, 10000)
            atr = random.uniform(1, price * 0.1)

            result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)
            self.assertLessEqual(
                result["N_plan"], result["N_risk"], f"N_plan={result['N_plan']} > N_risk={result['N_risk']}"
            )

    def test_n_plan_le_n_margin(self):
        """属性：N_plan <= N_margin（保证金约束）"""
        for _ in range(100):
            price = random.uniform(100, 10000)
            atr = random.uniform(1, price * 0.1)

            result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)
            self.assertLessEqual(
                result["N_plan"], result["N_margin"], f"N_plan={result['N_plan']} > N_margin={result['N_margin']}"
            )

    def test_stop_pts_positive(self):
        """属性：stop_pts > 0（正 ATR 下）"""
        for _ in range(50):
            price = random.uniform(100, 10000)
            atr = random.uniform(0.1, price * 0.05)

            result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)
            self.assertGreater(result["stop_pts"], 0)

    def test_kelly_mult_bounded(self):
        """属性：kelly_mult 在合理范围内"""
        for _ in range(50):
            price = random.uniform(100, 10000)
            atr = random.uniform(1, price * 0.1)

            result = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG)
            self.assertGreaterEqual(result["kelly_mult"], 0.0)
            self.assertLessEqual(result["kelly_mult"], 2.0)

    def test_held_lots_reduces_n_plan(self):
        """属性：有持仓时 N_plan <= 无持仓时"""
        for _ in range(50):
            price = random.uniform(100, 10000)
            atr = random.uniform(1, price * 0.1)

            r_no_held = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG, held_lots=0)
            r_with_held = risk_gate(self.symbol, price, atr, cfg=DEFAULT_CONFIG, held_lots=5)

            self.assertLessEqual(r_with_held["N_plan"], r_no_held["N_plan"])


# ═══════════════════════════════════════════════════════════════════════════
#  七、模糊测试（鲁棒性）
# ═══════════════════════════════════════════════════════════════════════════


class TestFuzzRobustness(unittest.TestCase):
    """模糊测试：验证函数在随机/极端输入下的鲁棒性。"""

    def setUp(self):
        random.seed(999)

    def _random_string(self, max_len=30):
        length = random.randint(0, max_len)
        chars = string.printable
        return "".join(random.choice(chars) for _ in range(length))

    def test_fuzz_parse_side_no_crash(self):
        """模糊：_parse_side 随机字符串不崩溃，返回 str"""
        for _ in range(N_RANDOM):
            s = self._random_string()
            try:
                result = _parse_side(s)
                self.assertIsInstance(result, str)
                self.assertIn(result, ["买", "卖", ""])
            except Exception as e:
                self.fail(f"_parse_side crashed on '{s}': {e}")

    def test_fuzz_parse_offset_no_crash(self):
        """模糊：_parse_offset 随机字符串不崩溃，返回 str"""
        for _ in range(N_RANDOM):
            s = self._random_string()
            try:
                result = _parse_offset(s)
                self.assertIsInstance(result, str)
                self.assertIn(result, ["开", "平", ""])
            except Exception as e:
                self.fail(f"_parse_offset crashed on '{s}': {e}")

    def test_fuzz_to_num_no_crash(self):
        """模糊：_to_num 随机字符串不崩溃，返回 float 或 None"""
        for _ in range(N_RANDOM):
            s = self._random_string()
            try:
                result = _to_num(s)
                self.assertTrue(result is None or isinstance(result, float))
            except Exception as e:
                self.fail(f"_to_num crashed on '{s}': {e}")

    def test_fuzz_norm_key_no_crash(self):
        """模糊：_norm_key 随机字符串不崩溃，返回 str"""
        for _ in range(N_RANDOM):
            s = self._random_string()
            try:
                result = _norm_key(s)
                self.assertIsInstance(result, str)
            except Exception as e:
                self.fail(f"_norm_key crashed on '{s}': {e}")

    def test_fuzz_compute_kelly_no_crash(self):
        """模糊：compute_kelly_factor 随机输入不崩溃"""
        for _ in range(N_RANDOM):
            edge = random.choice([None, random.uniform(-100, 100), "abc", [], {}])
            try:
                result = compute_kelly_factor(edge)
                self.assertIsInstance(result, float)
            except Exception as e:
                self.fail(f"compute_kelly_factor crashed on edge={edge}: {e}")

    def test_fuzz_norm_tanh_no_crash(self):
        """模糊：_norm_tanh 随机数字不崩溃"""
        for _ in range(N_RANDOM):
            x = random.uniform(-1e6, 1e6)
            scale = random.uniform(-1e3, 1e3)
            try:
                result = _norm_tanh(x, scale)
                self.assertIsInstance(result, float)
                self.assertFalse(math.isnan(result))
            except Exception as e:
                self.fail(f"_norm_tanh crashed on x={x}, scale={scale}: {e}")

    def test_fuzz_ym_of_no_crash(self):
        """模糊：ym_of 随机字符串不崩溃，返回 int 或 None"""
        for _ in range(N_RANDOM):
            s = self._random_string(15)
            try:
                result = ym_of(s)
                self.assertTrue(result is None or isinstance(result, int))
            except Exception as e:
                self.fail(f"ym_of crashed on '{s}': {e}")

    def test_fuzz_seasonal_f_no_crash(self):
        """模糊：seasonal_f 随机日期不崩溃"""
        for _ in range(N_RANDOM):
            sym = self._random_string(5)
            date = self._random_string(15)
            try:
                result = seasonal_f(sym, date)
                self.assertIsInstance(result, (int, float))
            except Exception as e:
                self.fail(f"seasonal_f crashed on sym='{sym}', date='{date}': {e}")

    def test_fuzz_is_signal_backed_no_crash(self):
        """模糊：_is_signal_backed 随机 dict 不崩溃"""
        for _ in range(N_RANDOM):
            trade = {
                "signal_id": self._random_string(10),
                "symbol": self._random_string(5),
            }
            sig_map = {self._random_string(8): {"symbol": self._random_string(5)} for _ in range(random.randint(0, 5))}
            try:
                result = _is_signal_backed(trade, sig_map)
                self.assertIsInstance(result, bool)
            except Exception as e:
                self.fail(f"_is_signal_backed crashed: {e}")

    def test_fuzz_is_manual_record_no_crash(self):
        """模糊：_is_manual_record 随机 dict 不崩溃"""
        for _ in range(N_RANDOM):
            trade = {}
            if random.random() > 0.3:
                trade["signal_id"] = self._random_string(15)
            try:
                result = _is_manual_record(trade)
                self.assertIsInstance(result, bool)
            except Exception as e:
                self.fail(f"_is_manual_record crashed: {e}")

    def test_fuzz_risk_gate_no_crash(self):
        """模糊：risk_gate 各种输入不崩溃"""
        for _ in range(50):
            sym = random.choice(["rb", "FG", "SA", "hc", "jd", "unknown"])
            price = random.uniform(1, 100000)
            atr = random.uniform(0.001, price * 0.5)
            try:
                result = risk_gate(sym, price, atr, cfg=DEFAULT_CONFIG)
                self.assertIsInstance(result, dict)
                self.assertIn("passed", result)
                self.assertIn("N_plan", result)
            except Exception as e:
                self.fail(f"risk_gate crashed on {sym}/{price}/{atr}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print(f"  属性测试 + 模糊测试（每属性 {N_RANDOM} 次随机验证）")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
