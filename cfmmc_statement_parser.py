#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cfmmc 期货市场监控中心「每日结算单」解析器（0 元方案 · 无需期货公司 CTP 参数）
==================================================================================

为什么需要它
------------
天勤免费版拦实盘账户查询（报"需要专业版" ¥9988/年），而期货公司 CTP 直连又拿不到
参数。但**每个期货投资者都免费拥有「中国期货市场监控中心（cfmmc.com）」查询账号**，
登录后能下载你在**所有期货公司**的每日结算单，里面含完整的 资金状况 + 持仓明细 + 盈亏。
结算单是每天收盘后生成一次 —— 这恰好够驱动四维 papertrack 的盘后持仓对账（不需要实时）。

本脚本把结算单（HTML 或 cfmmc_crawler 导出的 .xls）解析成 account_monitor.py 的
"ctp"/"snapshot" 后端读取的快照文件 account_monitor_ctp.json。与 ctp_account_feed.py
（CTP 直连）输出格式完全一致，后端无需改动。

怎么拿到结算单（二选一）
------------------------
A. 手动（干净、推荐）：登录 https://www.cfmmc.com → 结算单查询 → 选日期/账户 →
   「显示」→ 浏览器「另存为」HTML（或 cfmmc_crawler 导出的 .xls 也行）→ 丢给本脚本。
B. 自动（灰区）：用开源 cfmmc_crawler 自动批量下载（自动识别验证码），再丢给本脚本。
   注意：监控中心自动登录抓取处于合规灰区，介意就走 A。

用法
----
  python cfmmc_statement_parser.py 结算单1.html 结算单2.html
  python cfmmc_statement_parser.py --dir ./statements --out account_monitor_ctp.json
  python cfmmc_statement_parser.py 结算单.html          # 默认写到 ./account_monitor_ctp.json
  # 多账户时给每个文件标注归属（可选，仅用于展示名）：
  python cfmmc_statement_parser.py 西南.html --name 西南期货 --broker 2631
  python cfmmc_statement_parser.py 中信.html --name 中信期货 --broker 2020

依赖：仅 Python 标准库（html.parser）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "account_monitor_ctp.json")


# --------------------------------------------------------------------------- #
# 1. HTML / XLS 表格抽取（标准库，无需 bs4）
# --------------------------------------------------------------------------- #
class _TableExtractor(HTMLParser):
    """把 HTML 里的 <table> 抽成 list[table]，table = list[row]，row = list[cell_text]。"""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._in_table = False
        self._cur_table = None
        self._cur_row = None
        self._in_cell = False
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._cur_table = []
        elif tag == "tr" and self._in_table:
            self._cur_row = []
        elif tag in ("td", "th") and self._in_table and self._cur_row is not None:
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._cur_row.append("".join(self._cell_buf).strip())
            self._in_cell = False
        elif tag == "tr" and self._cur_row is not None:
            if self._cur_row:
                self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag == "table" and self._in_table:
            if self._cur_table:
                self.tables.append(self._cur_table)
            self._in_table = False
            self._cur_table = None

    def handle_data(self, data):
        if self._in_cell:
            self._cell_buf.append(data)


def _extract_tables(path):
    raw = open(path, encoding="utf-8", errors="ignore").read()
    p = _TableExtractor()
    p.feed(raw)
    return p.tables


# --------------------------------------------------------------------------- #
# 2. 数值清洗
# --------------------------------------------------------------------------- #
def _num(s):
    """数值清洗，保留负号（负值盈亏绝不能丢）。

    处理：千分位逗号、空格、货币单位「元」、全角破折号「—」(结算单里表示 0)、
    括号负数 (123) 以及 ASCII/U+2212/U+FE63 负号。
    """
    if s is None:
        return 0.0
    t = str(s).strip()
    neg = False
    # 括号负数
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    # 各类负号前缀
    if t and t[0] in ("-", "\u2212", "\ufe63"):
        neg = True
    # 去除非数字噪声（保留数字与小数点）
    t = t.replace(",", "").replace(" ", "").replace("元", "")
    t = t.replace("—", "").replace("(", "").replace(")", "")
    t = re.sub(r"[^\d.]", "", t)
    if t in ("", "."):
        return 0.0
    try:
        v = float(t)
    except ValueError:
        return 0.0
    return -v if neg else v


def _find_label(labels, *keys):
    """在 label->value 字典里找第一个 label 含任意 key 的值。"""
    for lab, val in labels.items():
        for k in keys:
            if k in lab:
                return val
    return None


# --------------------------------------------------------------------------- #
# 3. 解析单个结算单文件
# --------------------------------------------------------------------------- #
_CONTRACT_RE = re.compile(r"^[A-Za-z]{1,3}\d{3,4}$")  # 如 SA2609 / jm2609 / FG2601


def _parse_file(path, name="", broker_id="", investor_id=""):
    tables = _extract_tables(path)
    if not tables:
        print(f"[结算单解析] ⚠️ {os.path.basename(path)} 未抽出任何表格，跳过")
        return None

    # ---- 3a. 汇总所有 (label -> value) 行（资金状况表多为 [项目, 金额] 两列）----
    labels = {}
    for tbl in tables:
        for row in tbl:
            if len(row) >= 2:
                labels[row[0]] = row[-1]  # 取最后一个单元格作值，兼容多列
            elif len(row) == 1:
                labels[row[0]] = ""

    # 资金账号（尝试自动识别）
    if not investor_id:
        investor_id = _find_label(labels, "资金账号", "客户号", "投资者代码", "投资者") or ""

    # 日期
    date_str = _find_label(labels, "日期", "交易日期", "结算日期") or ""
    m = re.search(r"\d{4}[-/]?\d{1,2}[-/]?\d{1,2}", date_str)
    stmt_date = m.group(0).replace("/", "-") if m else time.strftime("%Y-%m-%d")

    balance = _num(_find_label(labels, "客户权益", "权益"))
    available = _num(_find_label(labels, "可用资金", "可用"))
    profit = _num(_find_label(labels, "当日盈亏", "浮动盈亏", "持仓盈亏"))

    # 兜底：当日盈亏 = 当日结存 - 上日结存 - 当日存取
    if profit == 0.0:
        cur = _num(_find_label(labels, "当日结存", "结存"))
        prev = _num(_find_label(labels, "上日结存", "上日"))
        dep = _num(_find_label(labels, "当日存取", "存取", "出入金"))
        if cur or prev:
            profit = cur - prev - dep

    # ---- 3b. 找持仓明细表（表头含「合约」且含「持仓量」/「买/卖」）----
    pos_table = None
    for tbl in tables:
        flat = " ".join(c for row in tbl for c in row)
        if "合约" in flat and ("持仓量" in flat or "买/卖" in flat or "买卖" in flat):
            pos_table = tbl
            break

    positions_raw = []
    if pos_table:
        # 定位表头行（含「合约」的那一行）
        header = None
        hidx = -1
        for i, row in enumerate(pos_table):
            if any("合约" in c for c in row):
                header = row
                hidx = i
                break
        if header:
            def _col(*keys):
                for j, c in enumerate(header):
                    for k in keys:
                        if k in c:
                            return j
                return -1

            i_inst = _col("合约")
            i_dir = _col("买/卖", "买卖", "方向")
            i_vol = _col("持仓量", "手数", "数量", "持有量")
            i_price = _col("开仓价", "开仓", "持仓均价", "成交价")
            i_margin = _col("保证金")
            if i_inst >= 0 and (i_vol >= 0 or i_price >= 0):
                for row in pos_table[hidx + 1:]:
                    if i_inst >= len(row):
                        continue
                    inst = (row[i_inst] or "").strip()
                    if not _CONTRACT_RE.match(inst):
                        continue  # 跳过「合计 / 小计」等非合约行
                    direction = "多"
                    if i_dir >= 0 and i_dir < len(row):
                        d = row[i_dir]
                        if "卖" in d:
                            direction = "空"
                        elif "买" in d:
                            direction = "多"
                    vol = _num(row[i_vol]) if i_vol >= 0 and i_vol < len(row) else 0.0
                    price = _num(row[i_price]) if i_price >= 0 and i_price < len(row) else 0.0
                    margin = _num(row[i_margin]) if i_margin >= 0 and i_margin < len(row) else 0.0
                    if vol <= 0:
                        continue
                    positions_raw.append({
                        "instrument": inst.upper(),
                        "direction": direction,
                        "volume": int(vol),
                        "open_price": round(price, 2),
                        "margin": round(margin, 2),
                    })

    print(f"[结算单解析] ✅ {os.path.basename(path)} | 账户 {investor_id or '?'} | "
          f"权益 {balance:,.0f} / 可用 {available:,.0f} / 盈亏 {profit:,.0f} | 持仓 {len(positions_raw)} 条")

    return {
        "name": name or (investor_id or os.path.basename(path)),
        "broker_id": broker_id,
        "investor_id": investor_id,
        "balance": round(balance, 2),
        "available": round(available, 2),
        "profit": round(profit, 2),
        "positions_raw": positions_raw,
        "_statement_date": stmt_date,
    }


# --------------------------------------------------------------------------- #
# 4. 主流程
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="cfmmc 结算单 -> account_monitor_ctp.json")
    ap.add_argument("files", nargs="*", help="结算单 HTML / .xls 文件")
    ap.add_argument("--dir", help="扫描目录（含 *.html / *.xls / *.htm）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    ap.add_argument("--name", default="", help="账户展示名（单文件时生效）")
    ap.add_argument("--broker", default="", help="期货公司 BrokerID（单文件时生效）")
    ap.add_argument("--account", default="", help="资金账号（单文件时生效，覆盖自动识别）")
    args = ap.parse_args(argv)

    paths = list(args.files)
    if args.dir:
        for fn in sorted(os.listdir(args.dir)):
            if fn.lower().endswith((".html", ".htm", ".xls")):
                paths.append(os.path.join(args.dir, fn))
    if not paths:
        print("用法: python cfmmc_statement_parser.py 结算单.html [--dir ./statements]")
        return 1

    accounts = []
    for p in paths:
        if not os.path.exists(p):
            print(f"[结算单解析] ⚠️ 找不到 {p}")
            continue
        acc = _parse_file(
            p,
            name=args.name if len(paths) == 1 else "",
            broker_id=args.broker if len(paths) == 1 else "",
            investor_id=args.account if len(paths) == 1 else "",
        )
        if acc:
            accounts.append(acc)

    if not accounts:
        print("[结算单解析] 没有任何有效结算单被解析")
        return 1

    snap = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "cfmmc_statement",
        "accounts": accounts,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"[结算单解析] 💾 已写出 {args.out}（{len(accounts)} 个账户）")
    print(f"[结算单解析] 下一步：runner 的 ctp 后端会自动读取此文件并驱动 papertrack 对账。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
