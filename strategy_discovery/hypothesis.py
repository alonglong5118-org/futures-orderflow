"""策略假设数据模型。

一个假设 = 改什么配置 + 预期什么效果 + 来源是什么。
"""

import json
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional


class Hypothesis:
    """策略假设。

    Attributes:
        id: 唯一标识（自动生成）
        name: 简短名称
        description: 一句话描述
        source: 来源（如 "焦煤焦炭-趋势跟踪.md"、"手动输入"）
        config_changes: 配置改动（嵌套 dict，会 deep merge 到 DEFAULT_CONFIG）
        expected_effect: 预期效果（文字描述）
        target_symbols: 目标品种列表（空=全部基准品种）
        tags: 标签
        created_at: 创建时间戳
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        source: str = "manual",
        config_changes: Optional[Dict[str, Any]] = None,
        expected_effect: str = "",
        target_symbols: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ):
        self.id = f"hypo_{int(time.time()*1000)}"
        self.name = name
        self.description = description
        self.source = source
        self.config_changes = config_changes or {}
        self.expected_effect = expected_effect
        self.target_symbols = target_symbols or []
        self.tags = tags or []
        self.created_at = time.time()

    def apply_to_config(self, base_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把配置改动 deep merge 到基础配置，返回新配置（不修改原配置）。"""
        result = deepcopy(base_cfg)
        _deep_merge(result, self.config_changes)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "config_changes": self.config_changes,
            "expected_effect": self.expected_effect,
            "target_symbols": self.target_symbols,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Hypothesis":
        h = cls(
            name=d["name"],
            description=d.get("description", ""),
            source=d.get("source", "manual"),
            config_changes=d.get("config_changes", {}),
            expected_effect=d.get("expected_effect", ""),
            target_symbols=d.get("target_symbols", []),
            tags=d.get("tags", []),
        )
        h.id = d.get("id", h.id)
        h.created_at = d.get("created_at", h.created_at)
        return h

    def __repr__(self) -> str:
        return f"Hypothesis({self.id}: {self.name})"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """递归合并 override 到 base（in-place 修改 base）。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_hypothesis_from_json(path: str) -> Hypothesis:
    """从 JSON 文件加载假设。"""
    with open(path, "r") as f:
        d = json.load(f)
    return Hypothesis.from_dict(d)
