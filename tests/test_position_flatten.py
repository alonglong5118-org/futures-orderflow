#!/usr/bin/env python3
"""
持仓解析 + 全平计划 — 单元测试
==================================

1. _pos_lots — 从持仓 dict 提取手数
   - lots 键 → 取整绝对值
   - lot 键 → 兼容
   - qty 键 → 兼容
   - volume 键 → 兼容
   - 手数 键 → 兼容中文
   - size 键 → 兼容
   - 字符串数字 → 自动转换
   - 负数 → 取绝对值
   - 小数 → 四舍五入取整
   - 空值 → 跳过找下一个键
   - 全无效 → 返回 0
   - 优先级按键顺序

2. _pos_dir — 从持仓 dict 提取方向
   - long/buy/多/做多/多头 → 1
   - short/sell/空/做空/空头 → -1
   - 正整数 → 1
   - 负整数 → -1
   - 零 → 0
   - 字符串 "1"/"+1" → 1
   - 字符串 "-1" → -1
   - direction/dir/side/方向 → 都兼容
   - 大小写不敏感
   - 未知值 → 0
   - None/空串 → 跳过

3. build_flatten_plan — 一键全平计划
   - 空持仓 → 空列表
   - None → 空列表
   - 多单 → 平多（卖平）
   - 空单 → 平空（买平）
   - 方向未知 → 全平
   - 多个持仓 → 多条计划
   - 无 symbol → 跳过
   - 零手数 → 跳过
   - 非 dict 元素 → 跳过
   - sym 键兼容
   - 代码 键兼容（中文）
   - 每条计划含 symbol/name/lots/action/side/price
   - name 字段兼容（name/名称/symbol 兜底）
   - price 字段兼容（price/last/现价）
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from risk_state_machine import _pos_dir, _pos_lots, build_flatten_plan

# ═══════════════════════════════════════════════════════════════════════════
#  1. _pos_lots
# ═══════════════════════════════════════════════════════════════════════════


class TestPosLots(unittest.TestCase):
    """_pos_lots 从持仓 dict 提取手数。"""

    def test_lots_key(self):
        """lots 键 → 取整绝对值"""
        self.assertEqual(_pos_lots({"lots": 5}), 5)

    def test_lot_key(self):
        """lot 键 → 兼容"""
        self.assertEqual(_pos_lots({"lot": 3}), 3)

    def test_qty_key(self):
        """qty 键 → 兼容"""
        self.assertEqual(_pos_lots({"qty": 10}), 10)

    def test_volume_key(self):
        """volume 键 → 兼容"""
        self.assertEqual(_pos_lots({"volume": 2}), 2)

    def test_chinese_shoushu_key(self):
        """手数 键 → 兼容中文"""
        self.assertEqual(_pos_lots({"手数": 4}), 4)

    def test_size_key(self):
        """size 键 → 兼容"""
        self.assertEqual(_pos_lots({"size": 7}), 7)

    def test_string_number(self):
        """字符串数字 → 自动转换"""
        self.assertEqual(_pos_lots({"lots": "3"}), 3)

    def test_negative_abs(self):
        """负数 → 取绝对值"""
        self.assertEqual(_pos_lots({"lots": -5}), 5)

    def test_float_rounds(self):
        """小数 → 四舍五入取整"""
        self.assertEqual(_pos_lots({"lots": 2.6}), 3)
        self.assertEqual(_pos_lots({"lots": 2.4}), 2)

    def test_empty_value_skips(self):
        """空值 → 跳过找下一个键"""
        # lots 是空串，lot 是 5 → 返回 5
        self.assertEqual(_pos_lots({"lots": "", "lot": 5}), 5)

    def test_none_value_skips(self):
        """None → 跳过找下一个键"""
        self.assertEqual(_pos_lots({"lots": None, "qty": 8}), 8)

    def test_all_invalid_zero(self):
        """全无效 → 返回 0"""
        self.assertEqual(_pos_lots({"lots": "abc", "volume": "xyz"}), 0)

    def test_empty_dict_zero(self):
        """空 dict → 返回 0"""
        self.assertEqual(_pos_lots({}), 0)

    def test_priority_order(self):
        """优先级按键顺序（lots > lot > qty > ...）"""
        # 多个键都有值，取第一个有效键
        self.assertEqual(_pos_lots({"lots": 5, "lot": 10, "qty": 20}), 5)

    def test_zero_returns_zero(self):
        """0 → 返回 0"""
        self.assertEqual(_pos_lots({"lots": 0}), 0)


# ═══════════════════════════════════════════════════════════════════════════
#  2. _pos_dir
# ═══════════════════════════════════════════════════════════════════════════


class TestPosDir(unittest.TestCase):
    """_pos_dir 从持仓 dict 提取方向。"""

    def test_long_string(self):
        """long → 1"""
        self.assertEqual(_pos_dir({"direction": "long"}), 1)

    def test_buy_string(self):
        """buy → 1"""
        self.assertEqual(_pos_dir({"dir": "buy"}), 1)

    def test_chinese_duo(self):
        """多 → 1"""
        self.assertEqual(_pos_dir({"side": "多"}), 1)

    def test_chinese_zuoduo(self):
        """做多 → 1"""
        self.assertEqual(_pos_dir({"direction": "做多"}), 1)

    def test_chinese_duotou(self):
        """多头 → 1"""
        self.assertEqual(_pos_dir({"direction": "多头"}), 1)

    def test_short_string(self):
        """short → -1"""
        self.assertEqual(_pos_dir({"direction": "short"}), -1)

    def test_sell_string(self):
        """sell → -1"""
        self.assertEqual(_pos_dir({"dir": "sell"}), -1)

    def test_chinese_kong(self):
        """空 → -1"""
        self.assertEqual(_pos_dir({"side": "空"}), -1)

    def test_chinese_zuokong(self):
        """做空 → -1"""
        self.assertEqual(_pos_dir({"direction": "做空"}), -1)

    def test_chinese_kongtou(self):
        """空头 → -1"""
        self.assertEqual(_pos_dir({"direction": "空头"}), -1)

    def test_positive_int(self):
        """正整数 → 1"""
        self.assertEqual(_pos_dir({"direction": 1}), 1)
        self.assertEqual(_pos_dir({"direction": 100}), 1)

    def test_negative_int(self):
        """负整数 → -1"""
        self.assertEqual(_pos_dir({"direction": -1}), -1)
        self.assertEqual(_pos_dir({"direction": -50}), -1)

    def test_zero_int(self):
        """零 → 0"""
        self.assertEqual(_pos_dir({"direction": 0}), 0)

    def test_str_one(self):
        """字符串 "1" → 1"""
        self.assertEqual(_pos_dir({"direction": "1"}), 1)

    def test_str_plus_one(self):
        """字符串 "+1" → 1"""
        self.assertEqual(_pos_dir({"direction": "+1"}), 1)

    def test_str_minus_one(self):
        """字符串 "-1" → -1"""
        self.assertEqual(_pos_dir({"direction": "-1"}), -1)

    def test_direction_key(self):
        """direction 键 → 兼容"""
        self.assertEqual(_pos_dir({"direction": "long"}), 1)

    def test_dir_key(self):
        """dir 键 → 兼容"""
        self.assertEqual(_pos_dir({"dir": "short"}), -1)

    def test_side_key(self):
        """side 键 → 兼容"""
        self.assertEqual(_pos_dir({"side": "多"}), 1)

    def test_chinese_direction_key(self):
        """方向 键 → 兼容中文键名"""
        self.assertEqual(_pos_dir({"方向": "空"}), -1)

    def test_case_insensitive(self):
        """大小写不敏感"""
        self.assertEqual(_pos_dir({"direction": "LONG"}), 1)
        self.assertEqual(_pos_dir({"direction": "Short"}), -1)

    def test_unknown_zero(self):
        """未知值 → 0"""
        self.assertEqual(_pos_dir({"direction": "unknown"}), 0)

    def test_none_skips(self):
        """None → 跳过"""
        self.assertEqual(_pos_dir({"direction": None, "side": "long"}), 1)

    def test_empty_string_skips(self):
        """空串 → 跳过"""
        self.assertEqual(_pos_dir({"direction": "", "side": "short"}), -1)

    def test_empty_dict_zero(self):
        """空 dict → 0"""
        self.assertEqual(_pos_dir({}), 0)

    def test_float_positive(self):
        """正 float → 1"""
        self.assertEqual(_pos_dir({"direction": 1.5}), 1)

    def test_float_negative(self):
        """负 float → -1"""
        self.assertEqual(_pos_dir({"direction": -2.0}), -1)


# ═══════════════════════════════════════════════════════════════════════════
#  3. build_flatten_plan
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildFlattenPlan(unittest.TestCase):
    """build_flatten_plan 一键全平计划。"""

    def test_empty_list_empty_plan(self):
        """空持仓 → 空列表"""
        self.assertEqual(build_flatten_plan([]), [])

    def test_none_empty_plan(self):
        """None → 空列表"""
        self.assertEqual(build_flatten_plan(None), [])

    def test_long_position_close_long(self):
        """多单 → 平多（卖平）"""
        positions = [{"symbol": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["action"], "平多（卖平）")
        self.assertEqual(plan[0]["side"], 1)
        self.assertEqual(plan[0]["lots"], 2)
        self.assertEqual(plan[0]["symbol"], "rb")

    def test_short_position_close_short(self):
        """空单 → 平空（买平）"""
        positions = [{"symbol": "au", "lots": 1, "direction": "short"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["action"], "平空（买平）")
        self.assertEqual(plan[0]["side"], -1)

    def test_unknown_direction_flatten_all(self):
        """方向未知 → 全平"""
        positions = [{"symbol": "rb", "lots": 3}]
        plan = build_flatten_plan(positions)
        self.assertEqual(plan[0]["action"], "全平")
        self.assertEqual(plan[0]["side"], 0)

    def test_multiple_positions(self):
        """多个持仓 → 多条计划"""
        positions = [
            {"symbol": "rb", "lots": 2, "direction": "long"},
            {"symbol": "au", "lots": 1, "direction": "short"},
            {"symbol": "ag", "lots": 3, "direction": "long"},
        ]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["symbol"], "rb")
        self.assertEqual(plan[1]["symbol"], "au")
        self.assertEqual(plan[2]["symbol"], "ag")

    def test_no_symbol_skipped(self):
        """无 symbol → 跳过"""
        positions = [{"lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 0)

    def test_zero_lots_skipped(self):
        """零手数 → 跳过"""
        positions = [{"symbol": "rb", "lots": 0, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 0)

    def test_non_dict_skipped(self):
        """非 dict 元素 → 跳过"""
        positions = ["not a dict", {"symbol": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["symbol"], "rb")

    def test_sym_key_alias(self):
        """sym 键兼容"""
        positions = [{"sym": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(plan[0]["symbol"], "rb")

    def test_chinese_code_key(self):
        """代码 键兼容（中文）"""
        positions = [{"代码": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertEqual(plan[0]["symbol"], "rb")

    def test_each_entry_has_all_fields(self):
        """每条计划含 symbol/name/lots/action/side/price"""
        positions = [{"symbol": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        for key in ("symbol", "name", "lots", "action", "side", "price"):
            self.assertIn(key, plan[0], f"missing key: {key}")

    def test_name_fallback(self):
        """name 字段兼容（name/名称/symbol 兜底）"""
        # 有 name
        p1 = build_flatten_plan([{"symbol": "rb", "name": "螺纹钢", "lots": 1, "direction": "long"}])
        self.assertEqual(p1[0]["name"], "螺纹钢")
        # 有 名称
        p2 = build_flatten_plan([{"symbol": "au", "名称": "黄金", "lots": 1, "direction": "long"}])
        self.assertEqual(p2[0]["name"], "黄金")
        # 都没有 → 用 symbol
        p3 = build_flatten_plan([{"symbol": "ag", "lots": 1, "direction": "long"}])
        self.assertEqual(p3[0]["name"], "ag")

    def test_price_fallback(self):
        """price 字段兼容（price/last/现价）"""
        # 有 price
        p1 = build_flatten_plan([{"symbol": "rb", "lots": 1, "direction": "long", "price": 3500}])
        self.assertEqual(p1[0]["price"], 3500)
        # 有 last
        p2 = build_flatten_plan([{"symbol": "au", "lots": 1, "direction": "long", "last": 600}])
        self.assertEqual(p2[0]["price"], 600)
        # 有 现价
        p3 = build_flatten_plan([{"symbol": "ag", "lots": 1, "direction": "long", "现价": 8000}])
        self.assertEqual(p3[0]["price"], 8000)
        # 都没有 → None
        p4 = build_flatten_plan([{"symbol": "cu", "lots": 1, "direction": "long"}])
        self.assertIsNone(p4[0]["price"])

    def test_returns_list_of_dicts(self):
        """返回 dict 列表"""
        positions = [{"symbol": "rb", "lots": 2, "direction": "long"}]
        plan = build_flatten_plan(positions)
        self.assertIsInstance(plan, list)
        self.assertIsInstance(plan[0], dict)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  持仓解析 + 全平计划 — 单元测试")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
