"""候选池管理：存储所有验证过的假设，支持查询、筛选、导出。"""

import json
import os
import time
from typing import Any, Dict, List, Optional


DEFAULT_POOL_PATH = "strategy_discovery/candidate_pool.json"


class CandidatePool:
    """候选池：所有验证过的策略假设都存在这里。

    数据结构:
    {
        "version": "1.0",
        "updated_at": 1234567890,
        "hypotheses": [
            {
                "hypothesis": {...},
                "validation_result": {...},
                "verdict": "pass",
                "status": "candidate",   # candidate / accepted / rejected / testing
                "notes": "",
                "tested_at": 1234567890,
            }
        ]
    }
    """

    def __init__(self, path: str = DEFAULT_POOL_PATH):
        self.path = path
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {"version": "1.0", "updated_at": time.time(), "hypotheses": []}
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._data["updated_at"] = time.time()
        with open(self.path, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def add(self, validation_result: Dict[str, Any], status: str = "candidate",
            notes: str = "") -> str:
        """添加一个验证结果到候选池。返回假设 ID。"""
        hypo_id = validation_result["hypothesis"]["id"]

        # 检查是否已存在（同一个假设 ID 覆盖）
        existing_idx = None
        for i, h in enumerate(self._data["hypotheses"]):
            if h["hypothesis"]["id"] == hypo_id:
                existing_idx = i
                break

        entry = {
            "hypothesis": validation_result["hypothesis"],
            "validation_result": {
                "summary": validation_result["summary"],
                "verdict": validation_result["verdict"],
                "reasons": validation_result["reasons"],
                "oos_ratio": validation_result.get("oos_ratio"),
                "tail_bars": validation_result.get("tail_bars"),
            },
            "verdict": validation_result["verdict"],
            "status": status,
            "notes": notes,
            "tested_at": time.time(),
        }

        if existing_idx is not None:
            self._data["hypotheses"][existing_idx] = entry
        else:
            self._data["hypotheses"].append(entry)

        self._save()
        return hypo_id

    def get(self, hypo_id: str) -> Optional[Dict]:
        for h in self._data["hypotheses"]:
            if h["hypothesis"]["id"] == hypo_id:
                return h
        return None

    def list(self, verdict: Optional[str] = None, status: Optional[str] = None,
             tag: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """列出候选池中的假设，支持筛选。"""
        results = []
        for h in self._data["hypotheses"]:
            if verdict and h.get("verdict") != verdict:
                continue
            if status and h.get("status") != status:
                continue
            if tag:
                tags = h["hypothesis"].get("tags", [])
                if tag not in tags:
                    continue
            results.append(h)

        # 按测试时间倒序
        results.sort(key=lambda x: x.get("tested_at", 0), reverse=True)
        return results[:limit]

    def update_status(self, hypo_id: str, status: str, notes: str = "") -> bool:
        """更新假设状态。status: candidate / accepted / rejected / testing。"""
        h = self.get(hypo_id)
        if not h:
            return False
        h["status"] = status
        if notes:
            h["notes"] = notes
        self._save()
        return True

    def stats(self) -> Dict[str, Any]:
        """统计信息。"""
        hypos = self._data["hypotheses"]
        total = len(hypos)
        by_verdict = {}
        by_status = {}
        for h in hypos:
            v = h.get("verdict", "unknown")
            s = h.get("status", "unknown")
            by_verdict[v] = by_verdict.get(v, 0) + 1
            by_status[s] = by_status.get(s, 0) + 1

        return {
            "total": total,
            "by_verdict": by_verdict,
            "by_status": by_status,
            "last_updated": self._data.get("updated_at"),
        }

    def export_markdown(self, hypo_id: str) -> str:
        """把单个假设导出为 Markdown（方便写入 Obsidian 策略库）。"""
        h = self.get(hypo_id)
        if not h:
            return ""

        hypo = h["hypothesis"]
        result = h["validation_result"]
        summary = result["summary"]

        lines = []
        lines.append("---")
        lines.append("tags: [策略, 期货, 候选]")
        lines.append(f"类型: 策略")
        lines.append(f"策略类型: {hypo.get('tags', [''])[0]}")
        lines.append(f"当前状态: 🟡候选")
        lines.append(f"创建日期: {time.strftime('%Y-%m-%d', time.localtime(hypo['created_at']))}")
        lines.append(f"验证状态: {result['verdict']}")
        lines.append("---")
        lines.append("")
        lines.append(f"# {hypo['name']}")
        lines.append("")
        lines.append("## 一句话逻辑")
        lines.append(f"> {hypo.get('description', '')}")
        lines.append("")
        lines.append("## 核心逻辑")
        lines.append(f"- 来源: {hypo.get('source', '')}")
        lines.append(f"- 预期效果: {hypo.get('expected_effect', '')}")
        lines.append("")
        lines.append("## 配置改动")
        lines.append("```json")
        lines.append(json.dumps(hypo.get("config_changes", {}), ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("## 回测验证结果")
        lines.append("")
        lines.append(f"- **判定**: {result['verdict'].upper()}")
        lines.append(f"- **平均 expR**: {summary.get('avg_expR', 'N/A')}")
        wr_str = f"{summary['avg_win_rate']*100:.1f}%" if summary.get("avg_win_rate") else "N/A"
        lines.append(f"- **平均胜率**: {wr_str}")
        lines.append(f"- **总交易数**: {summary.get('total_trades', 0)}")
        lines.append(f"- **正收益品种**: {summary.get('positive_count', 0)}/{summary.get('valid_symbols', 0)}")
        if "oos_avg_expR" in summary:
            lines.append(f"- **OOS 平均 expR**: {summary['oos_avg_expR']}")
        if "avg_oos_degrade" in summary:
            lines.append(f"- **平均 OOS 退化率**: {summary['avg_oos_degrade']*100:.0f}%")
        lines.append("")
        if result.get("reasons"):
            lines.append("### 问题与风险")
            for r in result["reasons"]:
                lines.append(f"- {r}")
            lines.append("")
        lines.append("## 备注")
        lines.append(h.get("notes", "（待人工审核）"))
        lines.append("")

        return "\n".join(lines)

    def print_summary(self):
        """打印候选池统计摘要。"""
        s = self.stats()
        print()
        print("=" * 60)
        print(f"  策略候选池统计")
        print("=" * 60)
        print(f"  总计: {s['total']} 个假设")
        print()
        print("  按判定:")
        for v, c in sorted(s["by_verdict"].items()):
            icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(v, "?")
            print(f"    {icon} {v.upper():<8}: {c}")
        print()
        print("  按状态:")
        for st, c in sorted(s["by_status"].items()):
            print(f"    {st:<12}: {c}")
        print("=" * 60)
        print()
