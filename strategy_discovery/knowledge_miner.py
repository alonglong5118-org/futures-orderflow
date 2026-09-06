"""知识矿工：从 Obsidian 知识库中扫描策略相关笔记，生成可验证的假设模板。

设计原则：
- 半自动化：发现候选 + 生成模板，人工补全配置后验证
- 不瞎猜参数：config_changes 留空或给选项，必须人工确认
- 分类清晰：策略库/品种笔记/技能卡分别处理
"""

import os
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class NoteInfo:
    """一篇笔记的元信息。"""
    path: str
    title: str = ""
    tags: List[str] = field(default_factory=list)
    note_type: str = ""           # 策略 / 品种 / 技能 / 知识资产
    strategy_type: str = ""        # 趋势跟踪 / 周期波段 / 对冲 ...
    symbols: List[str] = field(default_factory=list)
    group: str = ""                # 黑系 / 有色 / 农产品 / 化工
    status: str = ""
    one_liner: str = ""
    core_logic: str = ""
    entry_conditions: str = ""
    risk_rules: str = ""
    source_file: str = ""


def parse_front_matter(content: str) -> Tuple[Dict[str, str], str]:
    """解析 Markdown 的 YAML front matter。"""
    if not content.startswith("---"):
        return {}, content

    lines = content.split("\n")
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, content

    fm_lines = []
    body_start = 1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        fm_lines.append(lines[i])

    fm = {}
    for line in fm_lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # 处理列表：[a, b, c]
        if value.startswith("[") and value.endswith("]"):
            items = [x.strip() for x in value[1:-1].split(",") if x.strip()]
            fm[key] = items
        else:
            fm[key] = value

    body = "\n".join(lines[body_start:])
    return fm, body


def extract_section(body: str, section_name: str) -> str:
    """从 Markdown 正文中提取指定章节的内容。"""
    lines = body.split("\n")
    in_section = False
    result_lines = []
    current_level = 0

    for line in lines:
        # 匹配标题行
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if in_section and level <= current_level:
                # 遇到同级或更高级标题，结束
                break
            if section_name in title:
                in_section = True
                current_level = level
                continue
        if in_section:
            result_lines.append(line)

    return "\n".join(result_lines).strip()


def extract_first_quote(body: str) -> str:
    """提取第一个 blockquote（> 开头的行）。"""
    lines = body.split("\n")
    quote_lines = []
    in_quote = False
    for line in lines:
        if line.startswith(">"):
            quote_lines.append(line[1:].strip())
            in_quote = True
        elif in_quote:
            break
    return " ".join(quote_lines).strip()


def map_symbol_to_code(symbol_name: str) -> Optional[str]:
    """把中文品种名映射成交易代码（和四维策略的 SYMBOLS key 对齐）。"""
    # 注意：四维策略里的 symbol key 大多是小写，SA/FG/JD 等是大写
    mapping = {
        "焦煤": "JM", "焦炭": "J",
        "螺纹": "rb", "螺纹钢": "rb",
        "铁矿石": "i", "铁矿": "i",
        "热卷": "hc",
        "沪铜": "cu", "铜": "cu",
        "豆粕": "M", "豆油": "y", "棕榈": "P",
        "纯碱": "SA", "玻璃": "FG",
        "鸡蛋": "JD",
        "聚丙烯": "pp", "PP": "pp",
        "PTA": "TA", "pta": "TA",
        "生猪": "lh",
        "甲醇": "MA",
        "塑料": "L",
        "沪铝": "al", "铝": "al",
        "沪锌": "zn", "锌": "zn",
        "黄金": "au", "白银": "ag",
        "玉米": "c",
    }
    code = mapping.get(symbol_name)
    if not code:
        return None
    return code


def map_group(name: str) -> str:
    """根据品种/策略名推断分组。"""
    name = str(name)
    if any(k in name for k in ["焦煤", "焦炭", "螺纹", "铁矿", "热卷", "黑系", "黑色"]):
        return "黑系"
    if any(k in name for k in ["铜", "有色", "铝", "锌", "沪铜"]):
        return "有色"
    if any(k in name for k in ["豆粕", "豆油", "棕榈", "鸡蛋", "生猪", "玉米", "农产品", "农业"]):
        return "农产品"
    if any(k in name for k in ["纯碱", "玻璃", "PP", "PTA", "化工", "甲醇", "塑料"]):
        return "化工"
    if any(k in name for k in ["贵金属", "黄金", "白银"]):
        return "贵金属"
    return "未知"


def parse_strategy_note(path: str, content: str) -> NoteInfo:
    """解析一篇策略库笔记。"""
    fm, body = parse_front_matter(content)

    note = NoteInfo(path=path, source_file=os.path.basename(path))
    note.note_type = "策略"
    note.title = fm.get("策略类型", "") or fm.get("title", "")
    note.tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []
    note.strategy_type = fm.get("策略类型", "")
    note.status = fm.get("当前状态", "")

    # 品种
    syms = fm.get("品种", [])
    if isinstance(syms, list):
        for s in syms:
            code = map_symbol_to_code(s)
            if code:
                note.symbols.append(code)

    # 推断分组
    if note.symbols:
        # 用第一个品种的分组
        from four_dim_strategy import SYMBOLS
        for s in note.symbols:
            if s in SYMBOLS:
                note.group = SYMBOLS[s].get("group", "")
                break
    if not note.group:
        note.group = map_group(note.title)

    # 一句话逻辑
    one_liner = extract_section(body, "一句话逻辑")
    note.one_liner = one_liner.replace("> ", "").replace(">", "").strip()

    # 核心逻辑
    note.core_logic = extract_section(body, "核心逻辑")

    # 入场条件
    note.entry_conditions = extract_section(body, "入场条件")

    # 风控规则
    note.risk_rules = extract_section(body, "风控规则")

    # 标题从 h1 取
    h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if h1_match:
        note.title = h1_match.group(1).strip()

    return note


def parse_skill_note(path: str, content: str) -> NoteInfo:
    """解析一篇技能卡笔记。"""
    fm, body = parse_front_matter(content)

    note = NoteInfo(path=path, source_file=os.path.basename(path))
    note.note_type = "技能"
    note.tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []
    note.status = fm.get("状态", "")
    note.strategy_type = fm.get("分类", "")

    # 标题
    h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if h1_match:
        note.title = h1_match.group(1).strip()

    # 一句话
    one_liner = extract_section(body, "一句话用法")
    if not one_liner:
        one_liner = extract_first_quote(body)
    note.one_liner = one_liner

    # 核心内容
    note.core_logic = extract_section(body, "核心步骤") or extract_section(body, "核心要点")

    return note


def parse_variety_note(path: str, content: str) -> NoteInfo:
    """解析一篇品种笔记。"""
    fm, body = parse_front_matter(content)

    note = NoteInfo(path=path, source_file=os.path.basename(path))
    note.note_type = "品种"
    note.tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []

    code = fm.get("代码", "")
    if code:
        note.symbols = [code.upper() if code.upper() in {"SA", "FG", "JD"} else code.lower()]

    note.group = map_group(fm.get("品种", ""))

    h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if h1_match:
        note.title = h1_match.group(1).strip()

    note.one_liner = extract_section(body, "基本面要点")

    return note


class KnowledgeMiner:
    """知识矿工：扫描知识库，生成策略假设候选。"""

    def __init__(self, vault_path: str):
        self.vault_path = vault_path
        self.notes: List[NoteInfo] = []

    def scan(self) -> List[NoteInfo]:
        """扫描知识库中的策略相关笔记。"""
        self.notes = []

        # 策略库
        strategy_dir = os.path.join(self.vault_path, "10-期货研究", "策略库")
        if os.path.exists(strategy_dir):
            for f in os.listdir(strategy_dir):
                if f.endswith(".md") and not f.startswith("00-") and not f.startswith("模板"):
                    path = os.path.join(strategy_dir, f)
                    try:
                        with open(path, "r") as fh:
                            content = fh.read()
                        note = parse_strategy_note(path, content)
                        self.notes.append(note)
                    except Exception as e:
                        print(f"  ⚠️  解析失败 {f}: {e}")

        # 技能卡（研究分析类，可能包含可验证的方法论）
        skill_dir = os.path.join(self.vault_path, "40-技能库", "研究分析")
        if os.path.exists(skill_dir):
            for f in os.listdir(skill_dir):
                if f.endswith(".md") and not f.startswith("00-") and not f.startswith("模板"):
                    path = os.path.join(skill_dir, f)
                    try:
                        with open(path, "r") as fh:
                            content = fh.read()
                        note = parse_skill_note(path, content)
                        self.notes.append(note)
                    except Exception as e:
                        print(f"  ⚠️  解析失败 {f}: {e}")

        # 品种笔记（基本面要点）
        variety_dir = os.path.join(self.vault_path, "10-期货研究", "品种笔记")
        if os.path.exists(variety_dir):
            for f in os.listdir(variety_dir):
                if f.endswith(".md") and not f.startswith("模板"):
                    path = os.path.join(variety_dir, f)
                    try:
                        with open(path, "r") as fh:
                            content = fh.read()
                        note = parse_variety_note(path, content)
                        self.notes.append(note)
                    except Exception as e:
                        print(f"  ⚠️  解析失败 {f}: {e}")

        return self.notes

    def generate_hypothesis_templates(self, output_dir: str = "strategy_discovery/draft_hypotheses") -> List[str]:
        """为每篇笔记生成假设模板 JSON，输出到草稿目录。

        返回生成的文件路径列表。
        """
        os.makedirs(output_dir, exist_ok=True)
        generated = []

        for note in self.notes:
            if not note.title:
                continue

            # 过滤：品种笔记不生成假设草稿（品种是研究对象，不是策略）
            if note.note_type == "品种":
                continue

            # 过滤：技能卡只保留和策略/因子/权重/优化/风控直接相关的
            if note.note_type == "技能":
                text = (note.title + " " + note.one_liner).lower()
                # 明确排除：纯运维/工程/工具类技能
                exclude_keywords = [
                    "ci", "门禁", "告警", "api", "漂移", "前端", "面板", "仪表盘",
                    "特性开关", "检查清单", "复盘", "数据诊断", "钩子优化",
                    "去重", "数据源", "冗余", "研究方法",
                ]
                if any(k in text for k in exclude_keywords):
                    continue
                # 明确保留：和策略表现直接相关的
                include_keywords = [
                    "策略", "因子", "权重", "优化", "风控", "回测", "过拟合",
                    "止损", "止盈", "趋势", "震荡", "参数", "组合", "稳健池",
                    "kelly", "nsga", "ic", "oos", "校准", "验证",
                    "护城河", "套利", "对冲",
                ]
                if not any(k in text for k in include_keywords):
                    continue

            # 生成文件名（用英文+数字+下划线，避免中文路径问题）
            # 用 source_file 做文件名更稳定
            base_name = os.path.splitext(note.source_file)[0]
            safe_name = re.sub(r'[^\w\-]', '_', base_name)[:50]
            filename = f"draft_{note.note_type}_{safe_name}.json"
            filepath = os.path.join(output_dir, filename)

            # 目标品种
            target_symbols = note.symbols if note.symbols else []

            # config_changes 提示（留空，让人工填，但给一些提示）
            config_hints = self._suggest_config_changes(note)

            hypo = {
                "_draft_note": True,
                "_note_type": note.note_type,
                "_source_file": note.source_file,
                "name": f"{note.title}（待验证）",
                "description": note.one_liner or note.title,
                "source": f"知识库-{note.note_type}-{note.source_file}",
                "expected_effect": "",
                "target_symbols": target_symbols,
                "tags": note.tags + [f"来源:{note.note_type}"],
                "config_changes": {},
                "_config_hints": config_hints,
                "_status": "draft",  # draft / ready / rejected
            }

            with open(filepath, "w") as f:
                json.dump(hypo, f, ensure_ascii=False, indent=2)

            generated.append(filepath)

        return generated

    def _suggest_config_changes(self, note: NoteInfo) -> List[str]:
        """根据笔记内容给出可能的配置改动方向提示。"""
        hints = []
        text = (note.title + " " + note.one_liner + " " + note.core_logic + " " +
                note.entry_conditions + " " + note.risk_rules).lower()

        # 趋势/均线相关
        if any(k in text for k in ["均线", "趋势跟踪", "趋势", "突破", "sma", "ma"]):
            hints.append("调整趋势策略权重（s_trend / s_breakout）")
            hints.append("调整均线周期参数（SMA20/60）")

        # 震荡/均值回归
        if any(k in text for k in ["震荡", "均值回归", "布林", "rsi", "超买", "超卖"]):
            hints.append("调整震荡策略权重（s_meanrev / s_boll）")
            hints.append("加入 RSI 过滤（rsi_filter.enabled）")

        # 季节性
        if any(k in text for k in ["季节", "节前", "周期", "存栏", "消费"]):
            hints.append("调整季节性权重（s_seasonal）")
            hints.append("调整 seasonal_boost 的分组权重")

        # 基本面 / F因子
        if any(k in text for k in ["基本面", "库存", "开工率", "产能", "因子", "ic"]):
            hints.append("调整 F 因子权重（combine_weights.F）")
            hints.append("调整 per_symbol_combine_weights 的品种级配置")

        # 止损 / 风控
        if any(k in text for k in ["止损", "风控", "回撤", "风险"]):
            hints.append("调整止损倍数（regime_coef.*.stop）")
            hints.append("调整 regime 风控系数")

        # 止盈
        if any(k in text for k in ["止盈", "目标", "移动止损", "尾仓"]):
            hints.append("调整止盈 R 倍数（t2_mult）")
            hints.append("调整尾仓跟踪参数（trailing_tail）")

        # 相关性 / 去相关
        if any(k in text for k in ["相关", "对冲", "分散", "组合"]):
            hints.append("调整 decorrelate 参数")
            hints.append("调整相关性门控阈值")

        if not hints:
            hints.append("需人工分析具体配置改动点")

        return hints

    def list_drafts(self, draft_dir: str = "strategy_discovery/draft_hypotheses") -> List[Dict]:
        """列出所有草稿假设。"""
        if not os.path.exists(draft_dir):
            return []
        drafts = []
        for f in sorted(os.listdir(draft_dir)):
            if not f.endswith(".json"):
                continue
            path = os.path.join(draft_dir, f)
            try:
                with open(path, "r") as fh:
                    d = json.load(fh)
                drafts.append({
                    "file": f,
                    "name": d.get("name", ""),
                    "type": d.get("_note_type", ""),
                    "status": d.get("_status", "draft"),
                    "symbols": d.get("target_symbols", []),
                    "hints": d.get("_config_hints", []),
                })
            except Exception:
                pass
        return drafts
